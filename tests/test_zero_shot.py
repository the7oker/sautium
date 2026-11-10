"""The zero-shot label constants (backend/zero_shot.py, its labels.json): the
file loads only under the digest the code pins, names the CLAP revision the
node loads, and holds what scripts/make_zero_shot_labels.py says it encodes;
and the read-time meaning of a score. The derivation itself, in the database,
is tests/test_zero_shot_db.py."""

import base64
import importlib.util
import json
import math
import struct
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO / "backend") not in sys.path:
    sys.path.insert(0, str(REPO / "backend"))

import model_cache  # noqa: E402
import zero_shot  # noqa: E402


def _vector(label: dict) -> tuple:
    raw = base64.b64decode(label["vector"])
    return struct.unpack(f"<{len(raw) // 4}f", raw)


def test_the_labels_are_the_pinned_models_unit_vectors():
    doc = zero_shot.load_labels()
    assert doc["digest"] == zero_shot.LABELS_DIGEST
    assert doc["revision"] == model_cache.HUB_REVISIONS[doc["model"]]
    assert doc["logit_scale"] == pytest.approx(18.661177, abs=1e-6)
    for labels in doc["sets"].values():
        for label in labels:
            vector = _vector(label)
            assert len(vector) == doc["dimension"] == 512
            assert math.sqrt(math.fsum(x * x for x in vector)) == pytest.approx(1.0, abs=1e-6)


def test_the_file_holds_the_prompts_its_script_encodes():
    spec = importlib.util.spec_from_file_location(
        "make_zero_shot_labels", REPO / "scripts" / "make_zero_shot_labels.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    doc = zero_shot.load_labels()
    assert doc["model"] == script.MODEL
    assert {name: tuple((l["key"], l["prompt"]) for l in labels)
            for name, labels in doc["sets"].items()} == script.SETS


def test_readers_names_are_labels_of_the_file_and_a_key_names_one_label():
    doc = zero_shot.load_labels()
    keys = [l["key"] for labels in doc["sets"].values() for l in labels]
    assert len(keys) == len(set(keys)) == 12
    assert len(zero_shot.label_keys(zero_shot.MOODS)) == 8
    assert zero_shot.DANCEABLE in [l["key"] for l in doc["sets"]["dance"]]
    assert "vocal" in [l["key"] for l in doc["sets"]["vocal"]]


@pytest.mark.parametrize("change", [
    lambda doc: doc["sets"]["moods"][0].__setitem__("prompt", "This is cheerful music"),
    lambda doc: doc.__setitem__("logit_scale", 18.0),
    lambda doc: doc.__setitem__("revision", "0" * 40),
    lambda doc: doc["sets"]["vocal"].reverse(),
    lambda doc: doc["sets"]["dance"][0].__setitem__(
        "vector", base64.b64encode(struct.pack("<512f", *([0.0] * 512))).decode()),
])
def test_a_file_that_is_not_the_releases_is_refused(tmp_path, monkeypatch, change):
    doc = json.loads(zero_shot.LABELS_PATH.read_text(encoding="utf-8"))
    change(doc)
    other = tmp_path / "labels.json"
    other.write_text(json.dumps(doc), encoding="utf-8")
    monkeypatch.setattr(zero_shot, "LABELS_PATH", other)
    with pytest.raises(ValueError, match="not this release's label file"):
        zero_shot.load_labels()


def test_the_digest_a_file_states_about_itself_decides_nothing(tmp_path, monkeypatch):
    doc = json.loads(zero_shot.LABELS_PATH.read_text(encoding="utf-8"))
    doc["sets"]["moods"][0]["prompt"] = "This is cheerful music"
    doc["digest"] = zero_shot.LABELS_DIGEST
    other = tmp_path / "labels.json"
    other.write_text(json.dumps(doc), encoding="utf-8")
    monkeypatch.setattr(zero_shot, "LABELS_PATH", other)
    with pytest.raises(ValueError):
        zero_shot.load_labels()


def test_the_moods_present_are_those_at_the_threshold_strongest_first():
    raw = {"calm and relaxing": 0.61234, "dark and ominous": zero_shot.MOOD_MIN,
           "happy and upbeat": zero_shot.MOOD_MIN - 1e-9, "sad and melancholic": 0.3}
    assert list(zero_shot.present_moods(raw).items()) == [
        ("calm and relaxing", 0.612), ("sad and melancholic", 0.3),
        ("dark and ominous", round(zero_shot.MOOD_MIN, 3))]
    assert zero_shot.present_moods(None) == {} == zero_shot.present_moods({})
