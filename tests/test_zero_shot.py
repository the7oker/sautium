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


def test_the_moods_present_are_those_at_their_own_thresholds_strongest_first():
    low, high = "romantic and dreamy", "aggressive and angry"
    assert zero_shot.MOOD_MIN[low] < 0.07 < zero_shot.MOOD_MIN[high]
    raw = {"calm and relaxing": 0.61234, "dark and ominous": zero_shot.MOOD_MIN["dark and ominous"],
           "happy and upbeat": zero_shot.MOOD_MIN["happy and upbeat"] - 1e-9,
           "sad and melancholic": 0.3, low: 0.07, high: 0.07, "no such mood": 0.9}
    assert list(zero_shot.present_moods(raw).items()) == [
        ("calm and relaxing", 0.612), ("sad and melancholic", 0.3), (low, 0.07),
        ("dark and ominous", zero_shot.MOOD_MIN["dark and ominous"])]
    assert zero_shot.present_moods(None) == {} == zero_shot.present_moods({})


def test_every_mood_of_the_labels_has_a_threshold_and_nothing_else_does():
    assert set(zero_shot.MOOD_MIN) == set(zero_shot.label_keys(zero_shot.MOODS))
    assert all(0 < value < 1 for value in zero_shot.MOOD_MIN.values())
    assert json.loads(zero_shot.mood_thresholds(["calm and relaxing", "no such mood"])) == {
        "calm and relaxing": zero_shot.MOOD_MIN["calm and relaxing"]}


def test_the_importers_model_id_is_the_backends():
    """desktop/sync_client.py carries its own copy of the rule (the launcher
    imports no backend module): the two agree, spelling by spelling."""
    from desktop import sync_client
    from uuid_utils import embedding_model_uuid
    for name in (zero_shot.load_labels()["model"], "  Laion/CLAP-htsat-unfused ", "clap",
                 "a  model\twith   spaces", "Mode\u0301le"):
        assert sync_client.model_uuid(name) == str(embedding_model_uuid(name)), name
    good = {"model_name": "clap", "model_uuid": sync_client.model_uuid("clap").upper()}
    assert sync_client._names_its_model(good)
    for item in ({**good, "model_uuid": "00000000-0000-0000-0000-000000000b22"},
                 {**good, "model_name": "another"}, {"model_uuid": good["model_uuid"]},
                 {"model_name": "clap"}, {**good, "model_name": None}, {**good, "model_uuid": 7}):
        assert not sync_client._names_its_model(item), item


def test_a_unit_vector_is_one_within_the_band_the_derivation_scores():
    from desktop import sync_client
    low, high = sync_client.UNIT_NORM
    assert (low, high) == (0.99, 1.01)                      # the literals of zero_shot_derive
    axis = [1.0] + [0.0] * 511
    for norm, unit in ((1.0, True), (low, True), (high, True), (0.98, False), (1.02, False),
                       (0.0, False), (40.0, False), (-1.0, True)):
        assert sync_client._unit([norm * x for x in axis]) is unit, norm
