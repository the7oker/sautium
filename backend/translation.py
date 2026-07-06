"""
Local query translation for the English-only CLAP text encoder.

NLLB-200 distilled 600M through transformers — the runtime that already
serves CLAP/BGE — with weights BYO-downloaded from the official facebook/
repo into the shared HF cache on first load; the app never redistributes
them.

NLLB weights are CC-BY-NC 4.0: compatible with the free, donation-supported
product, but must be swapped for a clean-licensed model (MADLAD-400 /
OPUS-MT) before any commercial tier ships. The contract here is plain
text-in → text-out, so the swap is a model-id change.
"""

import logging
from typing import Optional

import torch
# Module-level so main.py's eager import resolves these through the
# main-thread import lock — transformers' _LazyModule attr resolution is
# not thread-safe across sibling pre-warm threads (see main.py startup).
from transformers import AutoModelForSeq2SeqLM, AutoTokenizer

logger = logging.getLogger(__name__)

MODEL_ID = "facebook/nllb-200-distilled-600M"

# і/ї/є/ґ are Ukrainian-only, ы/э/ъ/ё Russian-only. Shared-subset queries
# default to Ukrainian (primary user base) — NLLB tolerates a near-miss
# src tag between close languages.
_UK_ONLY = set("іїєґ")
_RU_ONLY = set("ыэъё")


def has_cyrillic(text: str) -> bool:
    return any("\u0400" <= ch <= "\u04ff" for ch in text)


class QueryTranslator:
    """Cyrillic search queries → English for CLAP encoding.

    Not a general MT surface: inputs are ≤255-char search phrases, decoding
    is capped accordingly, and repeats (debounced search-as-you-type) hit a
    bounded in-process cache.
    """

    def __init__(self, device: Optional[str] = None):
        from device import get_device
        self.device = device or get_device()
        self.tokenizer = None
        self.model = None
        self._cache: dict[str, str] = {}

    def load_model(self):
        from device import get_model_dtype
        logger.info(f"Loading translation model {MODEL_ID} on {self.device}")
        self.tokenizer = AutoTokenizer.from_pretrained(MODEL_ID)
        self.model = AutoModelForSeq2SeqLM.from_pretrained(
            MODEL_ID, dtype=get_model_dtype(self.device),
        ).to(self.device).eval()
        self._eng = self.tokenizer.convert_tokens_to_ids("eng_Latn")
        logger.info("Translation model loaded")

    def _src_lang(self, text: str) -> str:
        letters = set(text.lower())
        if letters & _UK_ONLY:
            return "ukr_Cyrl"
        if letters & _RU_ONLY:
            return "rus_Cyrl"
        return "ukr_Cyrl"

    def to_english(self, text: str) -> str:
        cached = self._cache.get(text)
        if cached is not None:
            return cached
        self.tokenizer.src_lang = self._src_lang(text)
        inputs = self.tokenizer(text, return_tensors="pt").to(self.device)
        with torch.inference_mode():
            out = self.model.generate(
                **inputs,
                forced_bos_token_id=self._eng,
                max_new_tokens=64,
                num_beams=4,
            )
        result = self.tokenizer.batch_decode(out, skip_special_tokens=True)[0]
        if len(self._cache) >= 512:
            self._cache.clear()
        self._cache[text] = result
        logger.debug(f"Translated {text!r} -> {result!r}")
        return result
