"""Every model loads at a pinned commit, from the cache alone once every
file its loaders read at that commit is there (backend/model_cache.py
from_hub, HUB_FILES). The name of a model is its identity in the database and
on the network, so the commit is what that identity means; a model with no
pinned commit is not loaded. PaSST's checkpoint, fetched by URL outside the
Hub, is checked by its SHA-256 before anything reads it, where hear21passt
reads it; MADLAD's local conversion is taken only when the prebuilt model is
gone, never on a network that failed, and reads the pinned source's own
folder."""

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
BGE = "BAAI/bge-m3"


@pytest.fixture
def hub(tmp_path, monkeypatch):
    """A Hugging Face cache of the test's own, and a Hub that records what it
    is asked for."""
    hf = pytest.importorskip("huggingface_hub")
    import huggingface_hub.constants
    monkeypatch.setattr(huggingface_hub.constants, "HF_HUB_CACHE", str(tmp_path / "hub"))
    asked = []

    def snapshot(name):
        return (tmp_path / "hub" / f"models--{name.replace('/', '--')}" / "snapshots"
                / model_cache.HUB_REVISIONS[name])

    def cache(name, files):
        for f in files:
            (snapshot(name) / f).parent.mkdir(parents=True, exist_ok=True)
            (snapshot(name) / f).write_bytes(b"")

    def download(name, *, revision, allow_patterns=None, local_files_only=False, **kw):
        if not local_files_only:
            asked.append((name, revision, sorted(allow_patterns or [])))
            if online:
                cache(name, allow_patterns or [])
        return str(snapshot(name))            # offline too: whatever folder there is

    online = True
    monkeypatch.setattr(hf, "snapshot_download", download)
    ns = types.SimpleNamespace(cache=cache, asked=asked, snapshot=snapshot)

    def go_offline():
        nonlocal online
        online = False

    ns.go_offline = go_offline
    return ns


def test_a_cached_commit_is_read_from_its_snapshot_folder_alone(hub):
    """A folder, not the name: sentence-transformers' processor lookups drop
    the revision and the cache-only flag, and would ask for `main`."""
    calls = []

    def load(name, **kwargs):
        calls.append((name, kwargs))
        return "model"

    hub.cache(CLAP, model_cache.HUB_FILES[CLAP])
    assert model_cache.from_hub(load, CLAP, torch_dtype="bf16") == "model"
    assert calls == [(str(hub.snapshot(CLAP)), {"local_files_only": True, "torch_dtype": "bf16"})]
    assert hub.asked == []


def test_a_cache_without_one_file_of_the_commit_fetches_its_files_then_reads_the_cache(hub):
    """BGE-M3 with its weights but not modules.json loads, as a model
    sentence-transformers makes up; the files come first."""
    calls = []
    hub.cache(BGE, [f for f in model_cache.HUB_FILES[BGE] if f != "modules.json"])
    assert model_cache.from_hub(lambda name, **kw: calls.append(name) or "model", BGE) == "model"
    revision = model_cache.HUB_REVISIONS[BGE]
    assert hub.asked == [(BGE, revision, sorted(model_cache.HUB_FILES[BGE]))]
    assert calls == [str(hub.snapshot(BGE))]


def test_a_partial_cache_that_cannot_be_fetched_is_never_loaded(hub):
    """Offline, snapshot_download hands back the folder it has, unchecked."""
    calls = []
    hub.cache(BGE, [f for f in model_cache.HUB_FILES[BGE] if f != "modules.json"])
    hub.go_offline()
    with pytest.raises(OSError, match="modules.json"):
        model_cache.from_hub(lambda name, **kw: calls.append(name), BGE)
    assert calls == []


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
    assert set(model_cache.HUB_FILES) == set(model_cache.HUB_REVISIONS) - {translation.CT2_REPO}


def test_clap_loads_at_its_pinned_commit(hub, monkeypatch):
    pytest.importorskip("transformers")
    import clap_model
    hub.cache(CLAP, model_cache.HUB_FILES[CLAP])
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
    folder = str(hub.snapshot(CLAP))
    assert seen == [("ClapProcessor", folder, None, True), ("ClapModel", folder, None, True)]


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


def test_the_pinned_passt_checkpoint_is_the_file_hear21passt_loads(passt, monkeypatch, tmp_path):
    """hear21passt reads its checkpoint through timm's download cache, by the
    URL's file name under torch.hub's folder: the pinned file must be that
    one, or it fetches its own, unchecked."""
    pytest.importorskip("hear21passt")
    import torch
    from hear21passt.models.passt import default_cfgs
    try:
        from timm.models._hub import download_cached_file
    except ImportError:
        from timm.models.hub import download_cached_file
    assert default_cfgs["passt_s_swa_p16_128_ap476"]["url"] == passt.ei.PASST_CHECKPOINT_URL
    monkeypatch.setattr(torch.hub, "_hub_dir", str(tmp_path))
    passt.path.write_bytes(passt.good)
    assert Path(download_cached_file(passt.ei.PASST_CHECKPOINT_URL)) == passt.ei.pinned_passt_checkpoint()
    assert passt.served["count"] == 0


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

    def download(repo, *, revision, local_files_only=False, **kw):
        calls.append(local_files_only)
        return str(snapshot)

    monkeypatch.setattr(madlad.hub, "snapshot_download", download)
    monkeypatch.setattr(madlad.hub, "hf_hub_download", lambda *a, **kw: pytest.fail("fetched"))
    assert madlad.translation.ensure_ct2_model() == snapshot
    assert calls == [True] and madlad.converted == []


def _partial(madlad, monkeypatch, fetch):
    """A cached snapshot of the prebuilt translator without its weights, and
    `fetch` answering for each file the cache lacks."""
    from huggingface_hub.errors import LocalEntryNotFoundError
    snapshot = madlad.tmp / "snapshot"
    snapshot.mkdir(exist_ok=True)
    for name in madlad.translation.CT2_FILES:
        if name != "model.bin":
            (snapshot / name).write_bytes(b"")
    source = madlad.tmp / "source-snapshot"

    def download(repo, *, revision, local_files_only=False, allow_patterns=None):
        if repo == madlad.translation.MODEL_ID:
            return str(source)
        if local_files_only:
            return str(snapshot)
        raise AssertionError("snapshot_download's answer is never taken for a fetch")

    def one_file(repo, name, *, revision):
        if (snapshot / name).exists():
            return str(snapshot / name)
        return fetch(repo, name)

    monkeypatch.setattr(madlad.hub, "snapshot_download", download)
    monkeypatch.setattr(madlad.hub, "hf_hub_download", one_file)
    return snapshot, source, LocalEntryNotFoundError


def test_a_network_that_failed_converts_nothing(madlad, monkeypatch):
    from huggingface_hub.errors import LocalEntryNotFoundError

    def offline(repo, name):
        raise LocalEntryNotFoundError("the Hub did not answer")

    _partial(madlad, monkeypatch, offline)
    with pytest.raises(LocalEntryNotFoundError):
        madlad.translation.ensure_ct2_model()
    assert madlad.converted == []


def test_a_fetched_file_completes_the_prebuilt_translator(madlad, monkeypatch):
    snapshot, _, _ = _partial(madlad, monkeypatch, lambda repo, name: (
        (madlad.tmp / "snapshot" / name).write_bytes(b"") or str(madlad.tmp / "snapshot" / name)))
    assert madlad.translation.ensure_ct2_model() == snapshot and madlad.converted == []


def test_a_prebuilt_translator_that_is_gone_is_converted_from_the_pinned_source(madlad, monkeypatch):
    """Its partial cache included: the repository gone, the conversion runs
    from the pinned source's own snapshot folder."""
    from huggingface_hub.errors import RepositoryNotFoundError

    def gone(repo, name):
        raise RepositoryNotFoundError("no such repository")

    _, source, _ = _partial(madlad, monkeypatch, gone)
    out = madlad.translation.ensure_ct2_model()
    assert (out / "model.bin").exists()
    assert madlad.converted == [(str(source), None)]


@pytest.mark.parametrize("which", ["prebuilt", "converted"])
def test_the_translators_tokenizer_comes_from_the_model_it_runs(madlad, monkeypatch, which):
    """The prebuilt snapshot holds its tokenizer whole; a local conversion has
    none, and takes the pinned source's."""
    t = madlad.translation
    model_dir = t._ct2_model_dir() if which == "converted" else madlad.tmp / "snapshot"
    monkeypatch.setattr(t, "ensure_ct2_model", lambda: model_dir)
    loaded = []
    monkeypatch.setattr(t.AutoTokenizer, "from_pretrained",
                        lambda name, **kw: loaded.append((str(name), kw)) or object())
    monkeypatch.setattr(model_cache, "from_hub", lambda load, name, **kw: loaded.append(name))
    sys.modules["ctranslate2"].Translator = lambda *a, **kw: object()
    monkeypatch.setattr(t.QueryTranslator, "to_english", lambda self, text: text)
    t.QueryTranslator().load_model()
    assert loaded == ([t.MODEL_ID] if which == "converted"
                      else [(str(model_dir), {"local_files_only": True})])
