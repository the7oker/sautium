"""scripts/sync-atlas.sh: the published HQPlayer atlas's app subset copied into
the backend, on a synthetic published set in a sautium-private of the test's
own. Its app subset is the synthetic fixture (fixtures/hqp_atlas/
app-synthetic.json, every name in it invented), its releases dated here; the
copy lands in a directory of the test's own (--into). Nothing is published."""

import hashlib
import json
import os
import subprocess
import sys

from tests.conftest import REPO_ROOT

if str(REPO_ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "backend"))

import pytest  # noqa: E402

import hqp_atlas  # noqa: E402

SCRIPT = REPO_ROOT / "scripts" / "sync-atlas.sh"
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "hqp_atlas" / "app-synthetic.json"
DATE = "2001-05-06"
COMMIT = "c0de" * 10


def _app(dated=("pcm", "sdm")):
    doc = json.loads(FIXTURE.read_text(encoding="utf-8"))
    doc["releases"] = {f"9.4.1/{family}": DATE if family in dated else None for family in ("pcm", "sdm")}
    return json.dumps(doc, ensure_ascii=False).encode()


def _publish(private, files, dated=("pcm", "sdm")):
    """`files` as the publish step writes public/: each listed in the
    manifest by its size and sha256, the releases `dated` named in it."""
    public = private / "atlas" / "v1" / "public"
    public.mkdir(parents=True, exist_ok=True)
    for rel, data in files.items():
        (public / rel).parent.mkdir(parents=True, exist_ok=True)
        (public / rel).write_bytes(data)
    manifest = {
        "dataset": "hqp-atlas", "schema": "v1",
        "code": {"commit": COMMIT, "sources_sha256": "0" * 16, "schema_sha256": "1" * 16},
        "releases": [{"engine": "9.4.1", "family": family, "published": DATE} for family in dated],
        "outputs": [{"path": rel, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
                    for rel, data in sorted(files.items())],
    }
    (public / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    return public


@pytest.fixture
def private(tmp_path):
    p = tmp_path / "sautium-private"
    _publish(p, {"app/hqp-atlas.json": _app(), "engines/9.4.1/filters.json": b'{"filters": {}}\n',
                 "errata.json": b"[]\n"})
    return p


@pytest.fixture
def into(tmp_path):
    d = tmp_path / "backend"
    d.mkdir()
    return d


def _sync(private, into):
    return subprocess.run(["bash", str(SCRIPT), "--into", str(into)], capture_output=True, text=True,
                          env={**os.environ, "SAUTIUM_PRIVATE_DIR": str(private)})


def test_the_app_subset_is_copied_byte_for_byte_with_the_sets_version_beside_it(private, into):
    # full/ holds another app subset beside public/: only the published one is read
    full = private / "atlas" / "v1" / "full" / "app"
    full.mkdir(parents=True)
    (full / "hqp-atlas.json").write_bytes(b"{}")
    run = _sync(private, into)
    assert run.returncode == 0, run.stderr
    public = private / "atlas" / "v1" / "public"
    assert (into / "hqp_atlas.json").read_bytes() == (public / "app" / "hqp-atlas.json").read_bytes()
    assert json.loads((into / "hqp_atlas.version.json").read_text(encoding="utf-8")) == {
        "dataset": "hqp-atlas", "schema": "v1",
        "manifest_sha256": hashlib.sha256((public / "manifest.json").read_bytes()).hexdigest(),
        "app_sha256": hashlib.sha256((public / "app" / "hqp-atlas.json").read_bytes()).hexdigest(),
        "code": COMMIT,
        "releases": [{"engine": "9.4.1", "family": "pcm", "published": DATE},
                     {"engine": "9.4.1", "family": "sdm", "published": DATE}]}
    assert run.stdout.startswith("sync-atlas: app/hqp-atlas.json copied to ")
    assert sorted(p.name for p in into.iterdir()) == ["hqp_atlas.json", "hqp_atlas.version.json"]
    # the node reads what was copied, its releases dated
    atlas = hqp_atlas.read(into / "hqp_atlas.json", None)
    assert atlas is not None and atlas.releases == {"9.4.1/pcm": DATE, "9.4.1/sdm": DATE}


def _refused(private, into, why):
    """The sync refuses, saying `why`, and the copy a sync before it left is
    the one that stays."""
    (into / "hqp_atlas.json").write_bytes(b"before")
    run = _sync(private, into)
    assert run.returncode != 0 and why in run.stderr, run.stderr
    assert "nothing was copied" in run.stderr or "sync-atlas: no " in run.stderr
    assert sorted(p.name for p in into.iterdir()) == ["hqp_atlas.json"]
    assert (into / "hqp_atlas.json").read_bytes() == b"before"


def test_a_file_the_manifest_does_not_name_refuses_the_sync(private, into):
    public = private / "atlas" / "v1" / "public"
    (public / "curves").mkdir()
    (public / "curves" / "extra.json").write_bytes(b"{}")
    _refused(private, into, "curves/extra.json is in public/ and not in its manifest")
    (public / "curves" / "extra.json").unlink()
    # a link, whatever it points at: one that leads nowhere, one to a directory
    (public / "curves" / "gone.json").symlink_to(public / "nowhere.json")
    _refused(private, into, "curves/gone.json is in public/ and not in its manifest")
    (public / "curves" / "gone.json").unlink()
    (public / "engines-too").symlink_to(public / "engines", target_is_directory=True)
    _refused(private, into, "engines-too is in public/ and not in its manifest")


def test_a_listed_file_missing_or_changed_refuses_the_sync(private, into):
    public = private / "atlas" / "v1" / "public"
    (public / "errata.json").unlink()
    _refused(private, into, "errata.json, which the manifest lists, is missing")
    (public / "errata.json").write_bytes(b"[{}]\n")
    _refused(private, into, "errata.json is not the file the manifest names (size or sha256)")
    # the same size, other bytes
    (public / "errata.json").write_bytes(b"{}\n")
    _refused(private, into, "errata.json is not the file the manifest names (size or sha256)")


def test_a_set_with_no_release_dated_holds_no_app_subset_to_copy(tmp_path, into):
    # the publish step with every release null: public/ is its manifest alone
    private = tmp_path / "unpublished"
    _publish(private, {}, dated=())
    _refused(private, into, "the published set holds no app/hqp-atlas.json: no release is dated")


def test_a_release_not_dated_as_the_manifest_dates_it_refuses_the_sync(tmp_path, into):
    # the manifest dates PCM alone, while the subset holds the SDM entries too
    private = tmp_path / "half"
    _publish(private, {"app/hqp-atlas.json": _app()}, dated=("pcm",))
    _refused(private, into, "release 9.4.1/sdm is dated 2001-05-06 in the subset and none in the manifest")
    # the subset's own date missing where the manifest gives one
    private = tmp_path / "undated"
    _publish(private, {"app/hqp-atlas.json": _app(dated=("pcm",))})
    _refused(private, into, "release 9.4.1/sdm is dated none in the subset and 2001-05-06 in the manifest")
    # a release with no entry, undated: it would still decide which engine the screen shows
    doc = json.loads(_app())
    doc["releases"]["9.4.2/sdm"] = None
    private = tmp_path / "null-key"
    _publish(private, {"app/hqp-atlas.json": json.dumps(doc).encode()})
    _refused(private, into, "release 9.4.2/sdm is dated none in the subset and none in the manifest")
    # a date other than the manifest's
    doc["releases"] = {"9.4.1/pcm": "2001-05-07", "9.4.1/sdm": DATE}
    private = tmp_path / "other-date"
    _publish(private, {"app/hqp-atlas.json": json.dumps(doc).encode()})
    _refused(private, into, "release 9.4.1/pcm is dated 2001-05-07 in the subset and 2001-05-06 in the manifest")


def test_a_subset_the_node_would_refuse_is_never_copied(tmp_path, into):
    doc = json.loads(_app())
    doc["schema"] = "hqp-atlas-app/v0"
    private = tmp_path / "other-schema"
    _publish(private, {"app/hqp-atlas.json": json.dumps(doc).encode()})
    _refused(private, into, "backend/hqp_atlas.py refuses app/hqp-atlas.json (above)")
    run = _sync(private, into)
    assert "a schema other than hqp-atlas-app/v1" in run.stderr


def test_another_dataset_or_no_published_set_refuses_the_sync(tmp_path, into):
    private = tmp_path / "other"
    public = _publish(private, {"app/hqp-atlas.json": _app()})
    manifest = json.loads((public / "manifest.json").read_text(encoding="utf-8"))
    (public / "manifest.json").write_text(json.dumps({**manifest, "dataset": "another"}), encoding="utf-8")
    _refused(private, into, "manifest.json names no hqp-atlas set")
    (public / "manifest.json").write_text(json.dumps({k: v for k, v in manifest.items() if k != "outputs"}),
                                          encoding="utf-8")
    _refused(private, into, "manifest.json holds no outputs")
    _refused(tmp_path / "nowhere", into, "sync-atlas: no ")
