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
JSONB. Storage keeps the RAW top-N score distribution (like moods and
embeddings do) — "is this instrument present" is decided at READ time
against INSTRUMENT_THRESHOLDS below, so thresholds are tunable without
re-running the GPU ensemble. Vocals mask instruments (a saxophone
scoring 0.35 on an instrumental mix scores ~0.11 on the vocal version
of the same song), which is why a write-time cutoff destroys data.

ML imports (torch/librosa/transformers) are lazy, inside the class:
every instruments reader — including the MCP server running on the
host without the ML stack — imports its presence thresholds from this
module, so module import must stay lightweight.
"""

from __future__ import annotations

import contextlib
import io
import logging
from typing import Dict, List, Optional, Set

logger = logging.getLogger(__name__)


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

# Storage cutoffs — noise floor, NOT a semantic presence threshold.
STORE_TOP_N = 20
STORE_MIN_SCORE = 0.01

DEFAULT_THRESHOLD = 0.10

# Read-time presence thresholds, keyed by the lowercased DB label.
# The single source of truth for every instruments reader (discovery
# engine, search filters, MCP/CLI display, DJ prompt). Labels not
# listed here use DEFAULT_THRESHOLD; calibrated per-class overrides
# (e.g. saxophone, whose score is suppressed by vocals) go here.
INSTRUMENT_THRESHOLDS: Dict[str, float] = {}


def threshold_for(label: str) -> float:
    """Presence threshold for a lowercased instruments JSONB key."""
    return INSTRUMENT_THRESHOLDS.get(label.lower(), DEFAULT_THRESHOLD)


def present_instruments(instruments: Optional[Dict[str, float]]) -> Dict[str, float]:
    """Stored raw-score dict → only the labels passing their read thresholds."""
    if not instruments:
        return {}
    return {k: v for k, v in instruments.items() if v >= threshold_for(k)}


class InstrumentEnsembleTagger:
    """AST + PaSST ensemble. Call load() once, then tag(audio_48k) per track."""

    AST_MODEL_NAME = "MIT/ast-finetuned-audioset-10-10-0.4593"
    AST_SR = 16000
    PASST_SR = 32000
    WINDOW_SECONDS = 10

    def __init__(self, device: Optional[str] = None):
        from device import get_device
        self.device = device or get_device()
        if self.device == "cpu":
            logger.warning("No GPU accelerator detected; AST/PaSST will be slow on CPU")

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
        try:
            from hear21passt.base import get_basic_model as _passt_get_basic_model
        except ImportError:
            raise RuntimeError(
                "hear21passt not installed. "
                "Add `hear21passt>=0.0.26` to requirements and rebuild the backend image."
            )
        import torch
        from transformers import ASTFeatureExtractor, ASTForAudioClassification

        from device import get_model_dtype
        dtype = get_model_dtype(self.device)

        logger.info("Loading AST model: %s on %s (%s)", self.AST_MODEL_NAME, self.device, dtype)
        self.ast_extractor = ASTFeatureExtractor.from_pretrained(self.AST_MODEL_NAME)
        self.ast = ASTForAudioClassification.from_pretrained(
            self.AST_MODEL_NAME, torch_dtype=dtype,
        )
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

        # PaSST: hear21passt's get_basic_model() doesn't expose torch_dtype,
        # so build in fp32 then convert. The model is small (~340MB) — the
        # transient peak is fine.
        logger.info("Loading PaSST model (hear21passt, logits mode)")
        with contextlib.redirect_stdout(io.StringIO()):
            self.passt = _passt_get_basic_model(mode="logits")
        if dtype == torch.float16:
            self.passt = self.passt.half()
        self.passt = self.passt.to(self.device).eval()

        if self.device == "cuda":
            mem = torch.cuda.memory_allocated() / 1e9
            logger.info("AST+PaSST loaded, GPU memory: %.2f GB", mem)

    def unload(self):
        import torch
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

    def _run_ast(self, audio_48k):
        import librosa
        import numpy as np
        import torch

        from device import autocast, cast_inputs
        audio_16k = librosa.resample(audio_48k, orig_sr=48000, target_sr=self.AST_SR)
        want = self.AST_SR * self.WINDOW_SECONDS
        if len(audio_16k) >= want:
            audio_16k = audio_16k[:want]
        else:
            audio_16k = np.pad(audio_16k, (0, want - len(audio_16k)))
        inputs = self.ast_extractor(
            audio_16k, sampling_rate=self.AST_SR, return_tensors="pt"
        )
        inputs = cast_inputs(inputs, self.ast.dtype)
        inputs = {k: v.to(self.device) for k, v in inputs.items()}
        with torch.no_grad(), autocast(self.device):
            logits = self.ast(**inputs).logits
        # sigmoid in fp32 — fp16 saturates around |x|>10 and clips probabilities
        return torch.sigmoid(logits.float())[0].cpu().numpy()

    def _run_passt(self, audio_48k):
        import librosa
        import numpy as np
        import torch

        from device import autocast
        audio_32k = librosa.resample(audio_48k, orig_sr=48000, target_sr=self.PASST_SR)
        want = self.PASST_SR * self.WINDOW_SECONDS
        if len(audio_32k) >= want:
            audio_32k = audio_32k[:want]
        else:
            audio_32k = np.pad(audio_32k, (0, want - len(audio_32k)))
        passt_dtype = next(self.passt.parameters()).dtype
        tensor = torch.from_numpy(audio_32k).to(self.device, dtype=passt_dtype).unsqueeze(0)
        with torch.no_grad(), autocast(self.device), contextlib.redirect_stdout(io.StringIO()):
            logits = self.passt(tensor)
        return torch.sigmoid(logits.float())[0].cpu().numpy()

    def tag(self, audio_48k) -> Dict[str, float]:
        """Return {lowercase_label: max(ast_score, passt_score)} — the raw
        top-STORE_TOP_N distribution above the STORE_MIN_SCORE noise floor,
        sorted by score descending.

        Deliberately NOT presence-filtered: readers decide presence against
        INSTRUMENT_THRESHOLDS. A near-empty dict is still a valid result for
        silence-like audio where even the noise floor isn't reached.
        """
        if self.ast is None or self.passt is None:
            raise RuntimeError("Call load() before tag()")

        ast_probs = self._run_ast(audio_48k)
        passt_probs = self._run_passt(audio_48k)

        results: Dict[str, float] = {}
        for idx in self.instrument_ids:
            score = max(float(ast_probs[idx]), float(passt_probs[idx]))
            if score >= STORE_MIN_SCORE:
                lowercase = self._label_lower[self.id2label[idx]]
                results[lowercase] = round(score, 3)
        top = sorted(results.items(), key=lambda x: -x[1])[:STORE_TOP_N]
        return dict(top)
