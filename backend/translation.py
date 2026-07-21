"""
Local query translation for the English-only CLAP text encoder.

MADLAD-400-3B-MT (google/madlad400-3b-mt, Apache 2.0) through transformers —
the runtime that already serves CLAP/BGE — with weights BYO-downloaded from
the official repo into the shared HF cache on first load.

MADLAD needs NO source-language detection: the model infers the input
language itself and only the target is declared, as a `<2en>` prefix token.
That property is load-bearing — the previous NLLB setup required a source
tag, and its Cyrillic-only char heuristic silently skipped French/German/
Spanish/CJK queries (they hit CLAP untranslated, as noise). Every sound
query now goes through translation unconditionally; English input passes as
an identity translation.

Apache 2.0 also clears the commercial path — the NLLB CC-BY-NC swap-before-
monetization caveat is gone. The contract stays text-in → text-out, so any
future model swap is a model-id change.
"""

import logging
from typing import Optional

import torch
# Module-level so main.py's eager import resolves these through the
# main-thread import lock — transformers' _LazyModule attr resolution is
# not thread-safe across sibling pre-warm threads (see main.py startup).
from transformers import AutoModelForSeq2SeqLM, AutoTokenizer

logger = logging.getLogger(__name__)

MODEL_ID = "google/madlad400-3b-mt"


class QueryTranslator:
    """Search queries in any language → English for CLAP encoding.

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
        logger.info("Translation model loaded")

    def to_english(self, text: str) -> str:
        cached = self._cache.get(text)
        if cached is not None:
            return cached
        inputs = self.tokenizer(f"<2en> {text}", return_tensors="pt").to(self.device)
        with torch.inference_mode():
            out = self.model.generate(**inputs, max_new_tokens=64, num_beams=4)
        result = self.tokenizer.batch_decode(out, skip_special_tokens=True)[0]
        if len(self._cache) >= 512:
            self._cache.clear()
        self._cache[text] = result
        logger.debug(f"Translated {text!r} -> {result!r}")
        return result
