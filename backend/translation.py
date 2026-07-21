"""
Local query translation for the English-only CLAP text encoder.

MADLAD-400-3B-MT (google/madlad400-3b-mt, Apache 2.0) running on
**CTranslate2 int8, CPU** — 0 VRAM, ~3GB RAM, and the artifact doubles as
the future launcher-side chat translator (no torch in this module's
runtime path) and shares the runtime family with faster-whisper for the
voice roadmap. The one-time conversion (below) reads the BYO HF checkpoint
from the shared cache; the ~3GB int8 model then loads in seconds, so lazy
loading on the standard profile is painless (the torch bf16 path it
replaced held 5.5GB VRAM and cold-loaded for 3.5 minutes).

MADLAD needs NO source-language detection: the model infers the input
language itself and only the target is declared, as a `<2en>` prefix token.
That property is load-bearing — the previous NLLB setup required a source
tag, and its Cyrillic-only char heuristic silently skipped French/German/
Spanish/CJK queries (they hit CLAP untranslated, as noise). Every sound
query goes through translation when the translator is available; English
input passes as an identity translation. (LID-based alternatives were
considered and rejected for exactly this reason.)

Apache 2.0 also clears the commercial path — and permits redistributing
our own int8 conversion later so fresh nodes can skip the 12GB source
download. The contract stays text-in → text-out.
"""

import logging
import os
import time
from pathlib import Path

# Module-level so main.py's eager import resolves transformers through the
# main-thread import lock — its _LazyModule attr resolution is not
# thread-safe across sibling pre-warm threads (see main.py startup).
from transformers import AutoTokenizer

logger = logging.getLogger(__name__)

MODEL_ID = "google/madlad400-3b-mt"


def _ct2_model_dir() -> Path:
    """Converted-model home, sibling of the HF hub cache so it rides the
    same persistent mount (Docker: ./data/cache; launcher: ~/.cache)."""
    hub = os.environ.get("HF_HUB_CACHE") or os.environ.get("HF_HOME")
    root = Path(hub).parent if hub else Path.home() / ".cache"
    return root / "sautium-ct2" / "madlad400-3b-mt-int8"


def ensure_ct2_model() -> Path:
    """Convert the HF checkpoint to CTranslate2 int8 once; idempotent.

    Loads the source as fp16 (≈6GB transient RAM instead of 12GB fp32),
    writes to a tmp dir and renames — a crashed conversion never leaves a
    half-model that loads. The 12GB HF source stays cached (it is still
    the upgrade/verification base); publishing our converted artifact so
    fresh nodes skip it entirely is the follow-up documented in
    HARDWARE-TIERS.
    """
    out = _ct2_model_dir()
    if (out / "model.bin").exists():
        return out
    import ctranslate2.converters

    tmp = out.with_name(out.name + ".tmp")
    logger.info(f"Converting {MODEL_ID} to CTranslate2 int8 → {out} "
                "(one-time; downloads the 12GB source if not cached)")
    converter = ctranslate2.converters.TransformersConverter(
        MODEL_ID, load_as_float16=True,
    )
    converter.convert(str(tmp), quantization="int8", force=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        import shutil
        shutil.rmtree(out)
    os.replace(tmp, out)
    logger.info("CTranslate2 int8 conversion complete")
    return out


class QueryTranslator:
    """Search queries in any language → English for CLAP encoding.

    Not a general MT surface: inputs are ≤255-char search phrases, decoding
    is capped accordingly, and repeats (debounced search-as-you-type) hit a
    bounded in-process cache.
    """

    def __init__(self):
        self.tokenizer = None
        self.translator = None
        self._cache: dict[str, str] = {}

    def load_model(self):
        import ctranslate2
        model_dir = ensure_ct2_model()
        logger.info(f"Loading translation model {MODEL_ID} (ct2 int8, cpu)")
        self.tokenizer = AutoTokenizer.from_pretrained(MODEL_ID)
        self.translator = ctranslate2.Translator(
            str(model_dir), device="cpu", compute_type="int8",
            inter_threads=1,
            intra_threads=max(2, min(8, (os.cpu_count() or 4) // 2)),
        )
        # ct2 mmaps model.bin lazily; on slow storage (NTFS/drvfs) the
        # first real translation page-faults the whole 3GB in (~60s
        # measured cold). One warm-up call moves that cost into the
        # pre-warm thread, off the first user query.
        warm = time.monotonic()
        self.to_english("прогрів моделі")
        self._cache.clear()
        logger.info(f"Translation model loaded (warm-up "
                    f"{time.monotonic() - warm:.1f}s)")

    def to_english(self, text: str) -> str:
        cached = self._cache.get(text)
        if cached is not None:
            return cached
        source = self.tokenizer.convert_ids_to_tokens(
            self.tokenizer.encode(f"<2en> {text}")
        )
        results = self.translator.translate_batch(
            [source], beam_size=4, max_decoding_length=64,
            # Degeneration guards: MADLAD loops on prose without them
            # ("ThreeThree…" observed live on a bio sentence); short
            # queries are unaffected but the chat/prose path needs both.
            no_repeat_ngram_size=3, repetition_penalty=1.2,
        )
        tokens = results[0].hypotheses[0]
        result = self.tokenizer.decode(
            self.tokenizer.convert_tokens_to_ids(tokens),
            skip_special_tokens=True,
        ).strip()
        if len(self._cache) >= 512:
            self._cache.clear()
        self._cache[text] = result
        logger.debug(f"Translated {text!r} -> {result!r}")
        return result
