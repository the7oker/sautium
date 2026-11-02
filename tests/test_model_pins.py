"""Every model loads at a pinned commit, from the cache alone once that
commit is there whole (backend/model_cache.py from_hub). The name of a model
is its identity in the database and on the network, so the commit is what
that identity means; a model with no pinned commit is not loaded. PaSST's
checkpoint, fetched by URL outside the Hub, is checked by its SHA-256 before
anything reads it; MADLAD's local conversion is taken only when the prebuilt
model is gone, never on a network that failed."""

import hashlib
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO / "backend") not in sys.path:
    sys.path.insert(0, str(REPO / "backend"))

import model_cache  # noqa: E402

CLAP = "laion/clap-htsat-unfused"


def test_a_cached_commit_is_read_from_the_cache_alone():
    calls = []

    def load(name, **kwargs):
        calls.append((name, kwargs))
        return "model"

    assert model_cache.from_hub(load, CLAP, torch_dtype="bf16") == "model"
    assert calls == [(CLAP, {"revision": model_cache.HUB_REVISIONS[CLAP],
                             "local_files_only": True, "torch_dtype": "bf16"})]


def test_a_commit_not_in_the_cache_whole_is_fetched_at_the_same_commit():
    calls = []

    def load(name, **kwargs):
        calls.append(kwargs)
        if kwargs.get("local_files_only"):
            raise OSError("model.safetensors is not in the cache")
        return "model"

    assert model_cache.from_hub(load, CLAP) == "model"
    revision = model_cache.HUB_REVISIONS[CLAP]
    assert calls == [{"revision": revision, "local_files_only": True}, {"revision": revision}]


def test_a_model_with_no_pinned_commit_is_not_loaded():
    with pytest.raises(KeyError):
        model_cache.from_hub(lambda name, **kw: "model", "someone/else")


def test_every_hub_model_the_node_loads_has_a_pinned_commit():
    from config import settings
    from ensemble_instruments import InstrumentEnsembleTagger
    names = {settings.embedding_model, settings.text_embedding_model,
             InstrumentEnsembleTagger.AST_MODEL_NAME}
    pytest.importorskip("transformers")
    import translation
    names |= {translation.MODEL_ID, translation.CT2_REPO}
    assert names <= set(model_cache.HUB_REVISIONS)
    assert all(len(rev) == 40 for rev in model_cache.HUB_REVISIONS.values())


def test_clap_loads_at_its_pinned_commit(monkeypatch):
    pytest.importorskip("transformers")
    import clap_model
    seen = []

    class Fake:
        @classmethod
        def from_pretrained(cls, name, **kwargs):
            seen.append((cls.__name__, name, kwargs.get("revision"), kwargs.get("local_files_only")))
            return types.SimpleNamespace(to=lambda device: types.SimpleNamespace(eval=lambda: None))

    monkeypatch.setattr(clap_model, "ClapProcessor", type("ClapProcessor", (Fake,), {}))
    monkeypatch.setattr(clap_model, "ClapModel", type("ClapModel", (Fake,), {}))
    monkeypatch.setattr(clap_model, "_cache", {})
    clap_model.get_clap_model(CLAP, "cpu")
    revision = model_cache.HUB_REVISIONS[CLAP]
    assert seen == [("ClapProcessor", CLAP, revision, True), ("ClapModel", CLAP, revision, True)]


# -- PaSST ---------------------------------------------------------------------

@pytest.fixture
def passt(tmp_path, monkeypatch):
    torch = pytest.importorskip("torch")
    import dump_common
    import ensemble_instruments as ei
    monkeypatch.setattr(torch.hub, "get_dir", lambda: str(tmp_path))
    good = b"the pinned checkpoint"
    monkeypatch.setattr(ei, "PASST_CHECKPOINT_SHA256", hashlib.sha256(good).hexdigest())
    served = {"bytes": good, "count": 0}

    def download(url, path, progress_cb=None, *, label, **kw):
        assert url == ei.PASST_CHECKPOINT_URL
        served["count"] += 1
        Path(path).write_bytes(served["bytes"])

    monkeypatch.setattr(dump_common, "download_resumable", download)
    path = tmp_path / "checkpoints" / ei.PASST_CHECKPOINT_URL.rsplit("/", 1)[1]
    path.parent.mkdir(parents=True)
    return types.SimpleNamespace(ei=ei, dump_common=dump_common, path=path, good=good, served=served)


def test_a_passt_checkpoint_that_is_not_the_pinned_file_is_fetched_again(passt):
    passt.path.write_bytes(b"some other weights")
    assert passt.ei.pinned_passt_checkpoint() == passt.path
    assert passt.path.read_bytes() == passt.good
    assert passt.served["count"] == 1


def test_a_downloaded_passt_checkpoint_that_does_not_match_is_refused_and_removed(passt):
    passt.served["bytes"] = b"tampered on the way"
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        passt.ei.pinned_passt_checkpoint()
    assert not passt.path.exists()
    assert list(passt.path.parent.iterdir()) == []


def test_a_verified_passt_checkpoint_is_not_hashed_again(passt, monkeypatch):
    passt.path.write_bytes(passt.good)
    passt.ei.pinned_passt_checkpoint()
    hashed = []
    real = passt.dump_common.sha256_file
    monkeypatch.setattr(passt.dump_common, "sha256_file", lambda p, *a: hashed.append(p) or real(p, *a))
    passt.ei.pinned_passt_checkpoint()
    assert hashed == [] and passt.served["count"] == 0


# -- MADLAD --------------------------------------------------------------------

@pytest.fixture
def madlad(tmp_path, monkeypatch):
    pytest.importorskip("transformers")
    import huggingface_hub
    import translation
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    converted = []

    class Converter:
        def __init__(self, name, **kwargs):
            converted.append((name, kwargs.get("revision")))

        def convert(self, out, **kwargs):
            Path(out).mkdir(parents=True)
            (Path(out) / "model.bin").write_bytes(b"")

    monkeypatch.setitem(sys.modules, "ctranslate2", types.SimpleNamespace())
    monkeypatch.setitem(sys.modules, "ctranslate2.converters",
                        types.SimpleNamespace(TransformersConverter=Converter))
    sys.modules["ctranslate2"].converters = sys.modules["ctranslate2.converters"]
    return types.SimpleNamespace(translation=translation, hub=huggingface_hub, tmp=tmp_path,
                                 converted=converted)


def test_the_prebuilt_translator_is_read_from_the_cache_alone_once_whole(madlad, monkeypatch):
    snapshot = madlad.tmp / "snapshot"
    snapshot.mkdir()
    for name in madlad.translation.CT2_FILES:
        (snapshot / name).write_bytes(b"")
    calls = []

    def download(repo, *, revision, local_files_only=False):
        calls.append(local_files_only)
        return str(snapshot)

    monkeypatch.setattr(madlad.hub, "snapshot_download", download)
    assert madlad.translation.ensure_ct2_model() == snapshot
    assert calls == [True] and madlad.converted == []


def test_a_network_that_failed_converts_nothing(madlad, monkeypatch):
    from huggingface_hub.errors import LocalEntryNotFoundError

    def download(repo, *, revision, local_files_only=False):
        if local_files_only:
            raise LocalEntryNotFoundError("not cached")
        raise ConnectionError("the Hub did not answer")

    monkeypatch.setattr(madlad.hub, "snapshot_download", download)
    with pytest.raises(ConnectionError):
        madlad.translation.ensure_ct2_model()
    assert madlad.converted == []


def test_a_prebuilt_translator_that_is_gone_is_converted_from_the_pinned_source(madlad, monkeypatch):
    from huggingface_hub.errors import LocalEntryNotFoundError, RepositoryNotFoundError

    def download(repo, *, revision, local_files_only=False):
        if local_files_only:
            raise LocalEntryNotFoundError("not cached")
        raise RepositoryNotFoundError("no such repository")

    monkeypatch.setattr(madlad.hub, "snapshot_download", download)
    out = madlad.translation.ensure_ct2_model()
    assert (out / "model.bin").exists()
    source = madlad.translation.MODEL_ID
    assert madlad.converted == [(source, model_cache.HUB_REVISIONS[source])]
