"""The fingerprinter's self-test (backend/provenance.py fingerprinter): every
process runs fpcalc on the probe signal shipped in the tree and compares the
print with the release image's before it analyses anything. A failed fpcalc
or another print stops the analysis passes and the stream enrichment, and
the notices say so beside tools.missing.

Fake fpcalcs are shell scripts on a PATH of the test's own; the release
fpcalc's own run is the one test that needs the image."""

import os
import shutil
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import provenance  # noqa: E402

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="fake fpcalcs are shell scripts")


@pytest.fixture
def fake_fpcalc(tmp_path, monkeypatch):
    """A PATH with only a fake fpcalc on it, whose body the test writes;
    no verdict kept from another test."""
    monkeypatch.setattr(provenance, "_verdicts", {})
    tools = tmp_path / "bin"
    tools.mkdir()
    monkeypatch.setenv("PATH", f"{tools}:/usr/bin:/bin")

    def write(body: str) -> Path:
        fpcalc = tools / "fpcalc"
        fpcalc.write_text("#!/bin/sh\n" + body + "\n")
        fpcalc.chmod(0o755)
        return fpcalc
    return write


def test_an_fpcalc_that_exits_3_after_printing_fails_and_analysis_refuses(fake_fpcalc):
    """Debian bookworm's fpcalc on any track under its 120 s window: a
    fingerprint printed, then an error and exit 3."""
    reference = provenance.PROBE_REFERENCE.read_text(encoding="ascii").strip()
    fake_fpcalc(f"echo {reference}\necho 'ERROR: Error decoding audio frame (End of file)' >&2\nexit 3")
    verdict = provenance.fingerprinter()
    assert (verdict.state, verdict.detail) == (
        "failed", "exit 3: ERROR: Error decoding audio frame (End of file)")
    with pytest.raises(RuntimeError, match="fingerprint self-test: failed"):
        provenance.require_fingerprinter()


def test_an_fpcalc_that_prints_another_fingerprint_is_refused(fake_fpcalc):
    fake_fpcalc("echo AQAAZFMUJq0U4VaOkhRC_another")
    assert provenance.fingerprinter().state == "mismatch"
    with pytest.raises(RuntimeError, match="fingerprint self-test: mismatch"):
        provenance.require_fingerprinter()


def test_an_fpcalc_that_prints_nothing_fails(fake_fpcalc):
    fake_fpcalc("exit 0")
    assert provenance.fingerprinter() == provenance.Fingerprinter(
        "failed", provenance.fingerprinter().path, "no fingerprint printed")


def test_the_reference_passes_and_the_verdict_is_kept_per_fpcalc_found(fake_fpcalc, tmp_path,
                                                                      monkeypatch):
    reference = provenance.PROBE_REFERENCE.read_text(encoding="ascii").strip()
    runs = tmp_path / "runs"
    fake_fpcalc(f'[ "$1 $2" = "-plain {provenance.PROBE}" ] || exit 9\n'
                f"echo run >> {runs}\necho {reference}")
    assert provenance.fingerprinter().ok
    provenance.require_fingerprinter()
    assert runs.read_text().count("run") == 1          # asked twice, run once
    other = tmp_path / "other"
    other.mkdir()
    shutil.copy(tmp_path / "bin" / "fpcalc", other / "fpcalc")
    monkeypatch.setenv("PATH", f"{other}:{os.environ['PATH']}")
    assert provenance.fingerprinter().path == str(other / "fpcalc")
    assert runs.read_text().count("run") == 2          # a new fpcalc is tested anew


def test_no_fpcalc_is_missing_and_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(provenance, "_verdicts", {})
    monkeypatch.setenv("PATH", str(tmp_path))
    assert provenance.fingerprinter() == provenance.Fingerprinter(
        "missing", None, "fpcalc (Chromaprint) is not on PATH")
    with pytest.raises(RuntimeError, match="fingerprint self-test: missing"):
        provenance.require_fingerprinter()


def test_the_stream_enrichment_accepts_nothing_from_a_failing_fpcalc(fake_fpcalc):
    pytest.importorskip("numpy")
    from streaming import enrichment
    fake_fpcalc("exit 3")
    enricher = enrichment.PreviewEnricher()
    assert enricher._fpcalc is False
    submitted = []
    enricher._pool.submit = lambda *a: submitted.append(a)
    enricher.submit("a track", b"stream", (60.0,), True, "youtube")
    assert submitted == []


def test_the_comparison_is_one_function():
    """A similarity rule will turn equality into a bound on the bit error rate:
    the verdict compares through same_as_reference alone."""
    import inspect
    source = inspect.getsource(provenance._probe_verdict)
    assert "same_as_reference(" in source and "==" not in source


def test_the_probe_is_twelve_seconds_of_cd_audio_and_one_reference_line():
    soundfile = pytest.importorskip("soundfile")
    info = soundfile.info(str(provenance.PROBE))
    assert (info.samplerate, info.channels, info.subtype, round(info.duration, 3)) == (
        44100, 2, "PCM_16", 12.0)
    assert provenance.MIN_MATERIAL_SECONDS <= info.duration < 120   # under fpcalc's window
    lines = provenance.PROBE_REFERENCE.read_text(encoding="ascii").splitlines()
    assert len(lines) == 1 and lines[0].startswith("AQAA")


@pytest.mark.skipif(shutil.which("fpcalc") is None, reason="no fpcalc here (the image has it)")
def test_this_fpcalc_fingerprints_the_probe_as_the_release(monkeypatch):
    """Run in the release image (lab_suite) and in the arm64 image: the same
    print, which is what the reference was made from."""
    monkeypatch.setattr(provenance, "_verdicts", {})
    assert provenance.fingerprinter().state == "ok"
