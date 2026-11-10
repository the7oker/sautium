"""Write backend/zero_shot_labels/labels.json, the text-label embeddings every
node derives its zero-shot scores from (backend/zero_shot.py, the
zero_shot_labels table).

The pinned CLAP revision (model_cache.HUB_REVISIONS), fp32 on CPU, one torch
thread, encodes each prompt and normalizes it — the same call the analyser's
own pre-encoding made — and the file keeps the model's name and revision, its
logit scale, and each vector as base64 of float32 little-endian
(record_sig.vector_to_bytes). The file is what ships: no node computes a
label vector, so no device and no dtype enters the constants. This script
only says how it was made; a rebuild lands within ~1.3e-7 of the committed
vectors, never on them, so `--check` compares instead of rewriting.

A new file is a new digest (zero_shot.LABELS_DIGEST) and a new data step in
backend/db_migrate.py; other prompts or another model are a design question
first.

    python scripts/make_zero_shot_labels.py            # writes the file, prints its digest
    python scripts/make_zero_shot_labels.py --check    # a rebuild against the committed file
    python scripts/make_zero_shot_labels.py --out DIR  # writes DIR/labels.json instead
"""

import argparse
import base64
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

MODEL = "laion/clap-htsat-unfused"

# set → ((key, prompt), …); the softmax runs within a set, in this order.
# Readers name a label by its key. The mood keys are the names the moods
# were stored under before 2026-11.
MOODS = ("happy and upbeat", "sad and melancholic", "energetic and intense",
         "calm and relaxing", "dark and ominous", "romantic and dreamy",
         "aggressive and angry", "mysterious and atmospheric")
SETS = {
    "moods": tuple((m, f"This is {m} music") for m in MOODS),
    "vocal": (("vocal", "This is singing vocals"),
              ("instrumental", "This is instrumental music without vocals")),
    "dance": (("danceable", "This is highly danceable music with strong beat"),
              ("not_danceable", "This is music that is not danceable")),
}


def build(threads: int) -> dict:
    import torch
    from transformers import ClapModel, ClapProcessor

    import model_cache
    import zero_shot
    from record_sig import vector_to_bytes

    torch.set_num_threads(threads)
    processor = model_cache.from_hub(ClapProcessor.from_pretrained, MODEL)
    model = model_cache.from_hub(ClapModel.from_pretrained, MODEL,
                                 torch_dtype=torch.float32).eval()
    sets = {}
    for name, labels in SETS.items():
        inputs = processor(text=[prompt for _key, prompt in labels],
                           return_tensors="pt", padding=True)
        with torch.no_grad():
            features = model.get_text_features(**inputs)
        vectors = torch.nn.functional.normalize(features.float(), p=2, dim=1).tolist()
        sets[name] = [
            {"key": key, "prompt": prompt,
             "vector": base64.b64encode(vector_to_bytes(vector)).decode("ascii")}
            for (key, prompt), vector in zip(labels, vectors)]
    doc = {
        "model": MODEL,
        "revision": model_cache.HUB_REVISIONS[MODEL],
        "dimension": model.config.projection_dim,
        "logit_scale": float(model.logit_scale_a.detach().exp()),
        "sets": sets,
    }
    doc["digest"] = zero_shot.labels_digest(doc)
    return doc


def main() -> int:
    import zero_shot

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true",
                        help="rebuild and compare with the committed file; write nothing")
    parser.add_argument("--out", type=Path, default=zero_shot.LABELS_PATH.parent,
                        help="the folder labels.json is written to")
    parser.add_argument("--threads", type=int, default=1,
                        help="torch threads; the committed file was made with one")
    args = parser.parse_args()

    doc = build(args.threads)
    if args.check:
        from record_sig import vector_from_bytes
        kept = zero_shot.load_labels()
        shape = lambda d: (d["model"], d["revision"], d["dimension"],
                           [(name, [(l["key"], l["prompt"]) for l in labels])
                            for name, labels in d["sets"].items()])
        if shape(doc) != shape(kept):
            print("the committed file names another model, revision or label set")
            return 1
        worst = max(
            abs(a - b)
            for name in doc["sets"]
            for new, old in zip(doc["sets"][name], kept["sets"][name])
            for a, b in zip(vector_from_bytes(base64.b64decode(new["vector"])),
                            vector_from_bytes(base64.b64decode(old["vector"]))))
        print(f"logit scale {doc['logit_scale']!r} against {kept['logit_scale']!r}; "
              f"vectors within {worst:.3g}")
        return 0 if worst < 1e-6 and abs(doc["logit_scale"] - kept["logit_scale"]) < 1e-6 else 1

    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / zero_shot.LABELS_PATH.name
    path.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    print(f"{path}: {sum(len(v) for v in doc['sets'].values())} labels, "
          f"logit scale {doc['logit_scale']!r}, digest {doc['digest']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
