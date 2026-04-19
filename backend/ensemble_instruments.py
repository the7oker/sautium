"""
Instrument multi-label tagger ensemble: AST + PaSST.

Both are native AudioSet sigmoid multi-label classifiers — unlike CLAP
zero-shot (softmax), they don't rely on prompt engineering and are
calibrated for per-class presence.

Ensemble strategy: max(ast_score, passt_score) per class. AST and
PaSST have complementary blind spots — AST catches choir/gong, PaSST
is stronger on synths and percussion detail.

Output keys are lowercased AudioSet labels (e.g. "piano",
"acoustic guitar", "drum kit") stored in audio_features.instruments
JSONB. Threshold 0.10 is conservative; tracks without identifiable
instrument events (dark ambient, drone) may yield an empty dict,
which is the correct answer.
"""

import contextlib
import io
import logging
from typing import Dict, List, Optional, Set

import librosa
import numpy as np
import torch
from transformers import ASTFeatureExtractor, ASTForAudioClassification

logger = logging.getLogger(__name__)

try:
    from hear21passt.base import get_basic_model as _passt_get_basic_model
    _PASST_AVAILABLE = True
except ImportError:
    _PASST_AVAILABLE = False


# AudioSet class names (exactly as in the model config) we treat as
# musical instruments. Curated from
# https://research.google.com/audioset/ontology/musical_instrument_1.html
AST_INSTRUMENT_LABELS: Set[str] = {
    # Plucked string
    "Plucked string instrument", "Guitar", "Electric guitar", "Bass guitar",
    "Acoustic guitar", "Steel guitar, slide guitar", "Banjo", "Sitar",
    "Mandolin", "Zither", "Ukulele",
    # Keyboard
    "Keyboard (musical)", "Piano", "Electric piano", "Clavinet",
    "Rhodes piano", "Organ", "Electronic organ", "Hammond organ",
    "Synthesizer", "Sampler", "Mellotron", "Harpsichord",
    # Percussion
    "Percussion", "Drum kit", "Drum machine", "Drum", "Snare drum",
    "Bass drum", "Timpani", "Tabla", "Cymbal", "Hi-hat", "Crash cymbal",
    "Cowbell", "Wood block", "Tambourine", "Rattle (instrument)",
    "Maraca", "Gong", "Tubular bells", "Mallet percussion",
    "Marimba, xylophone", "Glockenspiel", "Vibraphone", "Steelpan",
    # Brass
    "Brass instrument", "French horn", "Trumpet", "Trombone", "Cornet",
    "Bugle",
    # Bowed string
    "Bowed string instrument", "String section", "Violin, fiddle",
    "Pizzicato", "Cello", "Double bass",
    # Woodwind
    "Wind instrument, woodwind instrument", "Flute", "Saxophone",
    "Clarinet", "Oboe", "Bassoon",
    # Other
    "Harp", "Choir", "Harmonica", "Accordion", "Bagpipes",
    "Didgeridoo", "Shofar", "Theremin", "Singing bowl", "Orchestra",
    "Musical ensemble",
}

DEFAULT_THRESHOLD = 0.10


class InstrumentEnsembleTagger:
    """AST + PaSST ensemble. Call load() once, then tag(audio_48k) per track."""

    AST_MODEL_NAME = "MIT/ast-finetuned-audioset-10-10-0.4593"
    AST_SR = 16000
    PASST_SR = 32000
    WINDOW_SECONDS = 10

    def __init__(
        self,
        device: Optional[str] = None,
        threshold: float = DEFAULT_THRESHOLD,
    ):
        if device:
            self.device = device
        elif torch.cuda.is_available():
            self.device = "cuda"
        else:
            self.device = "cpu"
            logger.warning("CUDA not available; AST/PaSST will be slow on CPU")

        self.threshold = threshold
        self.ast = None
        self.ast_extractor = None
        self.passt = None
        self.instrument_ids: List[int] = []
        self.id2label: Dict[int, str] = {}
        # Map original AudioSet label -> lowercase key used in DB
        self._label_lower: Dict[str, str] = {}

    def load(self):
        if self.ast is not None and self.passt is not None:
            return
        if not _PASST_AVAILABLE:
            raise RuntimeError(
                "hear21passt not installed. "
                "Add `hear21passt>=0.0.26` to requirements and rebuild the backend image."
            )

        logger.info("Loading AST model: %s on %s", self.AST_MODEL_NAME, self.device)
        self.ast_extractor = ASTFeatureExtractor.from_pretrained(self.AST_MODEL_NAME)
        self.ast = ASTForAudioClassification.from_pretrained(self.AST_MODEL_NAME)
        self.ast = self.ast.to(self.device).eval()
        self.id2label = self.ast.config.id2label

        self.instrument_ids = [
            idx for idx, lbl in self.id2label.items()
            if lbl in AST_INSTRUMENT_LABELS
        ]
        for idx in self.instrument_ids:
            self._label_lower[self.id2label[idx]] = self.id2label[idx].lower()

        missing = AST_INSTRUMENT_LABELS - {self.id2label[i] for i in self.instrument_ids}
        if missing:
            logger.warning(
                "Ensemble: %d curated labels not in AudioSet config: %s",
                len(missing), sorted(missing),
            )
        logger.info("Ensemble: %d instrument classes resolved", len(self.instrument_ids))

        logger.info("Loading PaSST model (hear21passt, logits mode)")
        with contextlib.redirect_stdout(io.StringIO()):
            self.passt = _passt_get_basic_model(mode="logits")
        self.passt = self.passt.to(self.device).eval()

        if self.device == "cuda":
            mem = torch.cuda.memory_allocated() / 1e9
            logger.info("AST+PaSST loaded, GPU memory: %.2f GB", mem)

    def unload(self):
        if self.ast is not None:
            del self.ast, self.ast_extractor
            self.ast = None
            self.ast_extractor = None
        if self.passt is not None:
            del self.passt
            self.passt = None
        if self.device == "cuda":
            torch.cuda.empty_cache()
        logger.info("AST+PaSST unloaded")

    def _run_ast(self, audio_48k: np.ndarray) -> np.ndarray:
        audio_16k = librosa.resample(audio_48k, orig_sr=48000, target_sr=self.AST_SR)
        want = self.AST_SR * self.WINDOW_SECONDS
        if len(audio_16k) >= want:
            audio_16k = audio_16k[:want]
        else:
            audio_16k = np.pad(audio_16k, (0, want - len(audio_16k)))
        inputs = self.ast_extractor(
            audio_16k, sampling_rate=self.AST_SR, return_tensors="pt"
        )
        inputs = {k: v.to(self.device) for k, v in inputs.items()}
        with torch.no_grad():
            logits = self.ast(**inputs).logits
        return torch.sigmoid(logits)[0].cpu().numpy()

    def _run_passt(self, audio_48k: np.ndarray) -> np.ndarray:
        audio_32k = librosa.resample(audio_48k, orig_sr=48000, target_sr=self.PASST_SR)
        want = self.PASST_SR * self.WINDOW_SECONDS
        if len(audio_32k) >= want:
            audio_32k = audio_32k[:want]
        else:
            audio_32k = np.pad(audio_32k, (0, want - len(audio_32k)))
        tensor = torch.from_numpy(audio_32k).float().unsqueeze(0).to(self.device)
        with torch.no_grad(), contextlib.redirect_stdout(io.StringIO()):
            logits = self.passt(tensor)
        return torch.sigmoid(logits)[0].cpu().numpy()

    def tag(self, audio_48k: np.ndarray) -> Dict[str, float]:
        """Return {lowercase_label: max(ast_score, passt_score)} filtered by threshold.

        Sorted by score descending. Empty dict is a valid result for
        ambient/drone tracks with no identifiable instrument events.
        """
        if self.ast is None or self.passt is None:
            raise RuntimeError("Call load() before tag()")

        ast_probs = self._run_ast(audio_48k)
        passt_probs = self._run_passt(audio_48k)

        results: Dict[str, float] = {}
        for idx in self.instrument_ids:
            score = max(float(ast_probs[idx]), float(passt_probs[idx]))
            if score >= self.threshold:
                lowercase = self._label_lower[self.id2label[idx]]
                results[lowercase] = round(score, 3)
        return dict(sorted(results.items(), key=lambda x: -x[1]))
