"""Content-address provenance for audio analysis (analysis_sources rows).

Every analysis pass — scanner embedding/feature runs and streamed preview
enrichment alike — registers WHAT material it analyzed before its results are
saved: the AcoustID chromaprint of the decoded audio (fpcalc) plus its
duration. Record signatures bind these values, so they must exist at analysis
time, not be recomputed later — recomputing after the fact is exactly the
design flaw this module replaced (sign_audio's lazy second decode could hash
a different file than the one the analysis actually saw).

The fingerprint is the whole content address (2026-09-18; a BLAKE2b hash of
the decoded PCM stood beside it before): a public recording identity that any
node's decode of the same material reproduces, where the PCM hash changed
with the decoder build and with every lossy decode. A row is one
declaration of one copy of one origin — keyed on (track_id, chromaprint_key,
duration_seconds, imported, copy_rank), chromaprint_key the database's
digest of the fingerprint since ~3 KB of base64 is too long for a btree row:
this module registers only the node's own (first-hand) rows, the sync import
only the imported row of its record's declaration, nothing turns one into
the other — the flag is what sign_audio signs by (since 2026-11-10; a
registration used to flip an imported row first-hand, and another author's
records linked to it became this node's to sign) — and nothing rewrites a
row's declaration or copy under the records made from it. Re-analyzing
unchanged material reuses the row, a different master, another cut or a
better copy mints a new one, two equal rips of one master collapse. No fingerprint means no address, and an analysis with no
address is never saved — it could neither sign nor travel, and the pending
predicates would re-derive it on every run.

A digest that differs can still be the same material — a decoder that moved
the print, a lossy copy (desktop/p2p/material.py, FP-SIM's rule). A track's
rows of one material form a group (analysis_sources.material_of → the
group's root): a registration joins its group, a pass skips the compute the
group already holds at a rank no lower (held_in_group, file_in_group_sql),
a stream the group holds higher is already held, and a returned file is
re-linked to its analysis by the rule rather than by the equal digest.
"""

import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

from config import settings
from desktop.p2p import material

logger = logging.getLogger(__name__)

# Material shorter than one grid window (embeddings.WINDOW_SECONDS) is not
# analysed: one window is the smallest thing the analysis means, and fpcalc
# needs a few seconds of audio for any fingerprint at all — the floor is what
# guarantees an address (Valerii, 2026-09-18).
MIN_MATERIAL_SECONDS = 10

# Overwrite precedence between analysis passes of the same track, read from the
# MATERIAL alone: the node's own file beats any stream, a lossless stream beats
# a lossy one, an imported (second-hand) source and a record linked to no
# source at all rank below everything and are always upgradable. Never the
# provider's brand — the sync ranks peers' sources on is_lossless alone, and
# this is the same rule. The SQL form expects analysis_sources aliased `s`
# (LEFT JOINed or not: a missing row ranks -1).
MATERIAL_RANK_SQL = ("CASE WHEN s.id IS NULL OR s.imported THEN -1 "
                     "WHEN s.provider_id IS NULL THEN 2 "
                     "WHEN s.is_lossless THEN 1 ELSE 0 END")


def material_rank(provider_id: Optional[str], is_lossless: Optional[bool],
                  imported: bool = False) -> int:
    """MATERIAL_RANK_SQL for a pass about to be written (or a row already
    read): provider_id None = the node's own file."""
    if imported:
        return -1
    if provider_id is None:
        return 2
    return 1 if is_lossless else 0


def _rank_sql(row: str) -> str:
    """MATERIAL_RANK_SQL over a row that exists."""
    return (f"CASE WHEN {row}.imported THEN -1 "
            f"WHEN {row}.provider_id IS NULL THEN 2 "
            f"WHEN {row}.is_lossless THEN 1 ELSE 0 END")


def quality_score(bit_depth, is_lossless) -> int:
    """Between two copies of one material rank: CD (16-bit lossless) = 100 >
    other lossless = 50 > lossy = 10."""
    if is_lossless and bit_depth == 16:
        return 100
    if is_lossless:
        return 50
    return 10


def copy_rank(provider_id, is_lossless, bit_depth, imported: bool = False) -> int:
    """A copy's place among the copies of one material — the material rank,
    then the quality score — as analysis_sources.copy_rank stores it for a
    row: every imported row ranks as imported, whatever its author claims."""
    if imported:
        return -1000
    return material_rank(provider_id, is_lossless) * 1000 + quality_score(bit_depth, is_lossless)


def _group_sql(row: str) -> str:
    return f"COALESCE({row}.material_of, {row}.id)"


def file_in_group_sql(source: str = "asrc", file: str = "mf") -> str:
    """The pending predicates' test of whether a file's analysis is made: a
    row of the file `file` (media_files) in the material group of the
    analysis's row `source` at a rank no higher than that row's. The
    analysis's own row is such a row when it was made from the file; a row
    the file registered since, which the rule joined to the analysis's
    group, is one when the analysis was made from material at least as good
    (another rip of the master, a decoder that moved the print) — so a pass
    that skipped the compute is not asked again."""
    return f"""EXISTS (SELECT 1 FROM analysis_sources r
                        WHERE r.media_file_id = {file}.id
                          AND {_group_sql('r')} = {_group_sql(source)}
                          AND r.copy_rank <= {source}.copy_rank)"""


def held_in_group(db: Session, source_id: int, table: str, model_id=None) -> bool:
    """Whether the analysis of `table` (embeddings, audio_features; the
    instrument pass's table when its pass registers before it computes) on
    the source's track is linked to a row of the source's material group
    that ranks at least as high — the pass that just registered `source_id`
    then has nothing to compute: the better version replaces the worse,
    never the reverse. `model_id` narrows embeddings to the pass's model."""
    by_model = "AND x.model_id = :mid" if model_id is not None else ""
    return bool(db.execute(sa_text(f"""
        SELECT EXISTS (
            SELECT 1 FROM {table} x
              JOIN analysis_sources a ON a.id = x.analysis_source_id
              JOIN analysis_sources n ON n.id = :sid
             WHERE x.track_id = n.track_id {by_model}
               AND {_group_sql('a')} = {_group_sql('n')}
               AND a.copy_rank >= n.copy_rank)"""),
        {"sid": source_id, "mid": str(model_id) if model_id is not None else None}).scalar())


def outranked(db: Session, existing_id: Optional[int], source_id: int) -> bool:
    """Whether a record linked to `existing_id` outranks one a pass is about
    to save from `source_id`, so the save leaves it: a higher material rank
    (own file > lossless stream > lossy stream > imported) always, and the
    quality score only between copies of one material — the track's elected
    file replaces another material's analysis, of whatever quality, rather
    than being analysed and refused on every run. Each pass replaces its own
    kind at its save by this rule; a record linked to no source is
    outranked by every save."""
    if existing_id is None:
        return False
    return bool(db.execute(sa_text(f"""
        SELECT {_rank_sql('o')} > {_rank_sql('n')}
               OR ({_group_sql('o')} = {_group_sql('n')} AND o.copy_rank > n.copy_rank)
          FROM analysis_sources o, analysis_sources n
         WHERE o.id = :old AND n.id = :new"""), {"old": existing_id, "new": source_id}).scalar())


# The tables of analysis records, each row linked to an analysis_sources row
# of its track: what the identity merge weighs kind by kind.
ANALYSIS_KINDS = ("embeddings", "audio_features")

_TRACK_ROWS_SQL = sa_text("""
    SELECT id, material_of, chromaprint, duration_seconds, imported,
           provider_id, is_lossless, media_file_id, copy_rank
      FROM analysis_sources WHERE track_id = :tid ORDER BY id""")

_LOCK_ROOT_SQL = sa_text("SELECT material_of FROM analysis_sources WHERE id = :sid FOR KEY SHARE")

# A row per declaration, copy and origin (UNIQUE NULLS NOT DISTINCT
# (track_id, chromaprint_key, duration_seconds, imported, copy_rank)).
_INSERT_SQL = sa_text("""
    INSERT INTO analysis_sources
        (track_id, provider_id, media_file_id, chromaprint,
         duration_seconds, sample_rate, bit_depth, is_lossless, material_of)
    VALUES (:tid, :pid, :mfid, :fp, :dur, :sr, :bd, :ll, :root)
    ON CONFLICT (track_id, chromaprint_key, duration_seconds, imported, copy_rank) DO NOTHING
    RETURNING id
""")

_FILE_SQL = sa_text("UPDATE analysis_sources SET media_file_id = :mfid WHERE id = :sid")


def _held_root(db: Session, root: Optional[int]) -> Optional[int]:
    """The root a new row is born under, held while the row is inserted: a
    root deleted meanwhile leaves the new row a root of its own, one that
    joined another group meanwhile hands over that group's root."""
    for _ in range(3):
        if root is None:
            return None
        held = db.execute(_LOCK_ROOT_SQL, {"sid": root}).first()
        if held is None:
            return None
        if held.material_of is None:
            return root
        root = held.material_of
    return None


def _register(db: Session, track_id, fp: str, *, provider_id, media_file_id,
              duration, sample_rate, bit_depth, is_lossless) -> Optional[int]:
    """The first-hand row of `fp`, `duration` and this copy on the track;
    None when a stream's material is already held here by a better copy in
    its material group.

    A row describes the copy its records were made from, and a record is
    sealed with its row's declaration, so nothing rewrites a row: another
    print, another duration or another copy (a lossless stream after a
    lossy one, the owned file after a stream, a CD rip after a 24-bit one)
    is a row of its own, born in the group of the first row it is one
    material with (material.root_among: within the duration bound, so an
    edit with the opening of the album version is another material). The
    passes then replace their own kind at their saves by the rank
    (outranked), and until they do the group's ranks keep them pending
    (file_in_group_sql) and their skip off (held_in_group). An equal copy
    registered again only takes the row's file."""
    tid, mine = str(track_id), copy_rank(provider_id, is_lossless, bit_depth)
    for _attempt in range(2):
        rows = db.execute(_TRACK_ROWS_SQL, {"tid": tid}).fetchall()
        own = next((r for r in rows if not r.imported and r.chromaprint == fp
                    and r.duration_seconds == duration and r.copy_rank == mine), None)
        if own is not None:
            if media_file_id is not None and own.media_file_id != media_file_id:
                db.execute(_FILE_SQL, {"mfid": media_file_id, "sid": own.id})
            return own.id
        group = material.root_among(
            fp, duration, [(r.id, r.material_of, r.chromaprint, r.duration_seconds) for r in rows])
        if provider_id is not None and group is not None and any(
                (r.material_of or r.id) == group and r.copy_rank > mine for r in rows):
            return None
        sid = db.execute(_INSERT_SQL, {
            "tid": tid, "pid": provider_id, "mfid": media_file_id, "fp": fp, "dur": duration,
            "sr": sample_rate, "bd": bit_depth, "ll": is_lossless,
            "root": _held_root(db, group)}).scalar()
        if sid is not None:
            return sid
        # a registration of the same declaration and copy got there first
    raise RuntimeError(f"the analysis source of track {tid} changed under two registrations")


def join_existing_rows(db: Session, track_ids=None) -> int:
    """Join the rows of each track that has two or more into their material
    groups, as a registration would have: in id order, each root that holds
    no members of its own joins the first earlier row it is one material
    with (material.root_among — the declared durations filtered before any
    print is decoded). For rows registered before the groups existed (delta
    055): ANALYSIS-3's cutover calls it once over every track; `track_ids`
    narrows it. Idempotent — a member is never compared again. Returns the
    rows joined."""
    scope = "" if track_ids is None else "WHERE track_id = ANY(CAST(:tids AS uuid[]))"
    params = {} if track_ids is None else {"tids": [str(t) for t in track_ids]}
    tracks = db.execute(sa_text(f"""
        SELECT track_id::text FROM analysis_sources {scope}
         GROUP BY track_id HAVING count(*) >= 2 ORDER BY 1"""), params).scalars().all()
    joined = 0
    for tid in tracks:
        rows = [list(r) for r in db.execute(sa_text("""
            SELECT id, material_of, chromaprint, duration_seconds FROM analysis_sources
             WHERE track_id = :tid ORDER BY id"""), {"tid": tid}).fetchall()]
        for me in rows:
            if me[1] is not None or any(r[1] == me[0] for r in rows):
                continue
            root = material.root_among(me[2], me[3], [r for r in rows if r[0] < me[0]])
            if root is not None:
                db.execute(sa_text("UPDATE analysis_sources SET material_of = :root WHERE id = :sid"),
                           {"root": root, "sid": me[0]})
                me[1] = root
                joined += 1
    return joined


# The fingerprinter's self-test (2026-11-09). The fingerprint is the content
# address, and fpcalc's FFmpeg is part of it: Debian bookworm's fpcalc exits 3
# on every track under its 120 s window, Debian trixie's FFmpeg 7.1 moved the
# prints of 17 of 90 synthetic test files. So before any analysis this process runs
# fpcalc on a generated signal shipped in the tree (scripts/
# make_fingerprint_probe.py) and compares the print with the release image's.
PROBE = Path(__file__).resolve().parent / "fingerprint_probe" / "probe.flac"
PROBE_REFERENCE = PROBE.with_name("reference.txt")


@dataclass(frozen=True)
class Fingerprinter:
    """The self-test's verdict on the fpcalc at `path`: `ok`, `missing` (not
    on PATH), `failed` (fpcalc gave no fingerprint of the probe) or
    `mismatch` (another fingerprint than the reference); `detail` says why."""
    state: str
    path: Optional[str]
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.state == "ok"


_verdicts: dict = {}
_verdicts_lock = threading.Lock()


def _probe_verdict(path: Optional[str]) -> Fingerprinter:
    if path is None:
        return Fingerprinter("missing", None, "fpcalc (Chromaprint) is not on PATH")
    try:
        reference = PROBE_REFERENCE.read_text(encoding="ascii").strip()
        fingerprint = _run_fpcalc(path, str(PROBE))
    except subprocess.CalledProcessError as e:
        said = (e.stderr or "").strip().splitlines()
        return Fingerprinter("failed", path, f"exit {e.returncode}" + (f": {said[-1]}" if said else ""))
    except (subprocess.SubprocessError, OSError) as e:
        return Fingerprinter("failed", path, str(e))
    if not fingerprint:
        return Fingerprinter("failed", path, "no fingerprint printed")
    if not material.matches_probe(fingerprint, reference):
        return Fingerprinter("mismatch", path, "another fingerprint than the release's")
    return Fingerprinter("ok", path)


def _binary(path: Optional[str]) -> Optional[tuple]:
    """What identifies the fpcalc file a path runs: a link moved to another
    version (a Homebrew upgrade) or a file replaced in place is another."""
    if path is None:
        return None
    try:
        st = os.stat(path)
    except OSError:
        return (path,)
    return (os.path.realpath(path), st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


def fingerprinter() -> Fingerprinter:
    """The self-test's verdict on the fpcalc this process would run, tested
    once per fpcalc file found: the backend's start asks first, every
    analysis pass and the stream enrichment ask before they decode, each
    fingerprint is made with the fpcalc it names, the notices read it. A tool
    installed, upgraded or replaced later is another file, tested anew; a
    decoder library upgraded under an unchanged fpcalc is tested at the
    next start."""
    path = shutil.which("fpcalc")
    key = _binary(path)
    with _verdicts_lock:
        verdict = _verdicts.get(key)
        if verdict is None:
            verdict = _verdicts[key] = _probe_verdict(path)
            if verdict.ok:
                logger.info("fpcalc %s fingerprints the probe as the release does", path)
            elif verdict.state != "missing":
                logger.error("fpcalc %s failed the fingerprint self-test (%s): %s — audio "
                             "analysis refuses until it passes", path, verdict.state, verdict.detail)
    return verdict


def require_fingerprinter() -> None:
    """Analysis passes call this first: without a fingerprint nothing they
    compute can be addressed, and unaddressed analysis is never saved; with
    another fingerprinter's, what they saved would carry other addresses
    than every release node's. Stop before decoding a library rather than
    skip, or mis-address, every track of it."""
    verdict = fingerprinter()
    if not verdict.ok:
        raise RuntimeError(f"audio analysis refused: {verdict.detail} "
                           f"(fingerprint self-test: {verdict.state})")


def _run_fpcalc(fpcalc: str, path: str) -> str:
    return subprocess.run([fpcalc, "-plain", "-length", str(material.FPCALC_SECONDS), path],
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=120,
                          check=True).stdout.strip()


def _fpcalc(fpcalc: str, path: str) -> Optional[str]:
    try:
        return _run_fpcalc(fpcalc, path) or None
    except (subprocess.SubprocessError, OSError) as e:
        logger.debug("fpcalc failed for %s: %s", path, e)
        return None


def chromaprint_file(local_path: str, cue_start=None,
                     cue_end=None) -> Optional[str]:
    """AcoustID fingerprint (fpcalc, its default 120 s window) of a file or of
    a CUE image slice. fpcalc takes only a path, so a slice is decoded to a
    temp WAV first — fingerprinting the image would stamp every track of the
    disc with one identical anchor. The same WAV path is the fallback when
    fpcalc's own decoder rejects a whole file that ffmpeg can read. None when
    neither yields a fingerprint: material with no address. The fingerprint
    is made by the fpcalc file the self-test passed, by its path — one that
    fails it, or appeared since and fails it, gives no address."""
    verdict = fingerprinter()
    if not verdict.ok:
        logger.debug("no fingerprint for %s: fpcalc %s (%s)", local_path, verdict.state, verdict.detail)
        return None
    if cue_start is None and cue_end is None:
        fp = _fpcalc(verdict.path, local_path)
        if fp:
            return fp
        logger.info("fpcalc could not read %s directly — decoding through ffmpeg",
                    local_path)
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.close()
    try:
        cmd = ["ffmpeg", "-v", "error", "-y"]
        if cue_start is not None:
            cmd += ["-ss", f"{cue_start:.6f}"]
        cmd += ["-i", local_path]
        if cue_end is not None:
            cmd += ["-t", f"{cue_end - (cue_start or 0.0):.6f}"]
        cmd += [tmp.name]
        try:
            subprocess.run(cmd, capture_output=True, timeout=300, check=True)
        except (subprocess.SubprocessError, OSError) as e:
            logger.warning("ffmpeg decode for the fingerprint failed for %s: %s",
                           local_path, e)
            return None
        fp = _fpcalc(verdict.path, tmp.name)
        if fp is None:
            logger.warning("no fingerprint for %s: fpcalc produced nothing", local_path)
        return fp
    finally:
        os.unlink(tmp.name)


def get_or_create_local(db: Session, track_id, media_file_id: int,
                        file_path: str, sample_rate, bit_depth,
                        is_lossless, duration_seconds=None,
                        cue_start=None, cue_end=None) -> Optional[int]:
    """analysis_sources.id for a local analysis-source file, fingerprinting it
    on first sight. duration_seconds is the scan-known material duration
    (media_files.duration_seconds) — part of the signed material declaration.
    cue_start/cue_end bound a CUE image slice so its fingerprint addresses the
    track's material, not the whole disc. None when the file yields no
    fingerprint: the caller saves nothing (an analysis with no address could
    neither sign nor travel) and the pending predicate retries next run, as
    for any file that fails to decode."""
    sid = db.execute(sa_text(
        "SELECT id FROM analysis_sources "
        "WHERE track_id = :tid AND media_file_id = :mfid "
        "ORDER BY id DESC LIMIT 1"),
        {"tid": str(track_id), "mfid": media_file_id}).scalar()
    if sid is not None:
        return sid

    fp = chromaprint_file(settings.translate_to_local_path(file_path),
                          cue_start, cue_end)
    if fp is None:
        return None
    # Only a stream is ever "already held": an own-file registration always gets an id.
    return _register(db, track_id, fp, provider_id=None, media_file_id=media_file_id,
                     duration=int(round(float(duration_seconds))) if duration_seconds else None,
                     sample_rate=sample_rate, bit_depth=bit_depth, is_lossless=is_lossless)


# A first-hand file row (a stream row is the stream→owned upgrade policy's, an
# imported one the reanalyze_imported policy's, and neither is this node's
# file) whose file left the library (media_file_id SET NULL on the file row's
# delete) or belongs to a copy that is away (album_variants.absent_since): the
# material has no readable file here, and a present file of the same material
# takes it over — the copy found again elsewhere keeps its analysis while the
# old one, away too, waits for the owner's answer.
_UNLINKED = """s.provider_id IS NULL AND NOT s.imported
    AND (s.media_file_id IS NULL
         OR EXISTS (SELECT 1 FROM media_files x
                    JOIN album_variants xv ON xv.id = x.album_variant_id
                    WHERE x.id = s.media_file_id AND xv.absent_since IS NOT NULL))"""

# Such a row and the present analysis-source file now holding its track: a
# file that came back, at its old path or a new one. Read from the state alone,
# so whatever a cancel, a restart or a missing fpcalc left is found again by
# the next run.
_RETURNED_SQL = f"""
    SELECT mf.id, mf.track_id::text AS track_id, mf.file_path,
           mf.cue_start_seconds, mf.cue_end_seconds, mf.duration_seconds,
           mf.bit_depth, mf.is_lossless,
           mf.file_size_bytes, mf.file_modified_at,
           ARRAY(SELECT s.id FROM analysis_sources s
                 WHERE s.track_id = mf.track_id AND {_UNLINKED}
                 ORDER BY s.id) AS rows
    FROM media_files mf
    JOIN album_variants av ON av.id = mf.album_variant_id
    WHERE mf.is_analysis_source AND av.absent_since IS NULL
      AND (mf.duration_seconds IS NULL OR mf.duration_seconds >= :min_seconds)
      {{scope}}
      AND NOT EXISTS (SELECT 1 FROM analysis_sources s WHERE s.media_file_id = mf.id)
      AND EXISTS (SELECT 1 FROM analysis_sources s
                  WHERE s.track_id = mf.track_id AND {_UNLINKED})
    ORDER BY mf.id
"""

# Its candidates: the track's unlinked rows, the equal print first. The rule
# decides (material.same_material), not the equal digest: a library moved to
# a node whose fpcalc prints some of it a bit otherwise (another FFmpeg)
# keeps its analysis. Only a row of the file's own copy (copy_rank) is the
# file's: one made from a lesser copy (an MP3 the FLAC replaced, its copy
# removed or away) leaves the file pending, so its pass registers it in the
# row's group and derives the better version; one made from a better copy
# (a CD rip an MP3 now stands in for) keeps its claims for the better copy's
# return, while the lesser file registers a row of its own. The fingerprint covers fpcalc's first 120 s; the
# duration is the other half of the material declaration a seal binds
# (record_sig.segment_payload), so a longer or shorter cut of the same
# opening is another material, and a file of unknown length matches only a
# row that declares none either.
_RELINK_CANDIDATES_SQL = sa_text(f"""
    SELECT s.id, s.chromaprint, s.duration_seconds, s.copy_rank
      FROM analysis_sources s
     WHERE s.track_id = :tid AND {_UNLINKED}
     ORDER BY s.chromaprint = :fp DESC, s.id
""")

_RELINK_SQL = sa_text(f"""
    UPDATE analysis_sources s SET media_file_id = :mfid WHERE s.id = :sid AND {_UNLINKED}
""")

_RELINK_CHUNK = 100

# One look at a time: the import's, the yes's and each analysis pass's would
# otherwise fingerprint the same list side by side. A caller that waits reads
# the list again once the look before it is done, so it finds what is left.
_relink_lock = threading.Lock()

# A file compared and matching nothing — another master, another cut, or
# material fpcalc cannot read — is not fingerprinted again by every import
# and pass while nothing about it or its track's unlinked rows changed: until
# an analysis pass registers the file's own row it would stay a candidate.
# Per process: a restart compares it once more.
_unmatched: dict = {}


def relink_returned_files(track_ids=None, cancel_check=None, progress_cb=None) -> int:
    """Link the node's own analysis back to the file it was made from when
    that file returns — a folder moved back, a drive re-attached, a library
    moved under a new root. The analysis passes would see the track as
    pending (its source names no file, or another one) and recompute it in
    full; the fingerprint and the duration say the material is the same
    (material.same_material: a decoder that moved the print a little keeps
    it), and the content address is what the analysis describes. A file
    that matches no unlinked row (another master, another cut) stays
    pending, as any new file is. `track_ids` narrows the look to those tracks; without it every
    such file is looked at.

    Each chunk's fingerprints are taken in a pool before its transaction
    opens (fpcalc decodes up to 120 s of audio per file), and each chunk
    commits on its own, so a cancel or a crash keeps what was linked.
    progress_cb(done, total) after each chunk. Returns how many rows were
    linked."""
    from database import get_db_context

    params = {"min_seconds": MIN_MATERIAL_SECONDS}
    scope = ""
    if track_ids is not None:
        if not track_ids:
            return 0
        scope = "AND mf.track_id = ANY(CAST(:tids AS uuid[]))"
        params["tids"] = [str(t) for t in track_ids]
    with _relink_lock:
        with get_db_context() as db:
            files = [f for f in db.execute(sa_text(_RETURNED_SQL.format(scope=scope)),
                                           params).fetchall()
                     if _unmatched.get(f.id) != _compared(f)]
        if not files:
            return 0
        return _relink(files, cancel_check, progress_cb)


def _compared(f) -> tuple:
    """What a comparison of a file was made against: the file as scanned and
    its track's unlinked rows."""
    return (f.file_size_bytes, f.file_modified_at, tuple(f.rows))


def _relink(files, cancel_check, progress_cb) -> int:
    from concurrent.futures import ThreadPoolExecutor

    from database import get_db_context

    verdict = fingerprinter()
    if not verdict.ok:
        logger.warning("%d returned file(s) keep their analysis unlinked: %s (fingerprint "
                       "self-test: %s), so their material cannot be compared",
                       len(files), verdict.detail, verdict.state)
        return 0

    def fingerprint(f):
        return f, chromaprint_file(settings.translate_to_local_path(f.file_path),
                                   f.cue_start_seconds, f.cue_end_seconds)

    linked = done = 0
    with ThreadPoolExecutor(max_workers=min(4, os.cpu_count() or 4)) as pool:
        for start in range(0, len(files), _RELINK_CHUNK):
            if cancel_check and cancel_check():
                break
            chunk = list(pool.map(fingerprint, files[start:start + _RELINK_CHUNK]))
            with get_db_context() as db:
                for f, fp in chunk:
                    hit = fp is not None and _relink_one(db, f, fp)
                    if hit:
                        linked += 1
                        _unmatched.pop(f.id, None)
                    else:
                        _unmatched[f.id] = _compared(f)
            done += len(chunk)
            if progress_cb:
                progress_cb(done, len(files))
    logger.info("Re-linked the analysis of %d of %d returned file(s) by their "
                "fingerprint", linked, done)
    return linked


def _relink_one(db: Session, f, fp: str) -> bool:
    """Link the first of the file's track's unlinked rows that is its
    material and its copy to it; whether one was."""
    duration = int(round(float(f.duration_seconds))) if f.duration_seconds else None   # as declared
    mine = copy_rank(None, f.is_lossless, f.bit_depth)
    for row in db.execute(_RELINK_CANDIDATES_SQL, {"tid": f.track_id, "fp": fp}).fetchall():
        if (row.copy_rank == mine
                and material.same_material(fp, duration, row.chromaprint, row.duration_seconds)):
            return bool(db.execute(_RELINK_SQL, {"mfid": f.id, "sid": row.id}).rowcount)
    return False


def create_stream_source(db: Session, track_id, audio_bytes: bytes,
                         provider_id: str, is_lossless: bool) -> Optional[int]:
    """analysis_sources.id for streamed provider audio held in memory. None
    when the bytes yield no fingerprint, or when the same material is already
    registered here at a higher rank (the node's own file, or a lossless
    stream over a lossy one) — under the stream's address or a similar one
    of its material group — so the stream has nothing to add and the caller
    skips the analysis. provider_id is the serving provider's manifest
    id — registered in stream_providers at start, which is what the row's FK
    holds it to."""
    tmp = tempfile.NamedTemporaryFile(suffix=".audio", delete=False)
    try:
        tmp.write(audio_bytes)
        tmp.close()
        fp = chromaprint_file(tmp.name)
        if fp is None:
            return None
        try:
            sample_rate, bit_depth, duration = _probe_audio(tmp.name)
        except (subprocess.SubprocessError, OSError, ValueError, LookupError) as e:
            logger.warning("stream provenance failed for track %s (%s): %s",
                           track_id, provider_id, e)
            return None
    finally:
        os.unlink(tmp.name)

    sid = _register(db, track_id, fp, provider_id=provider_id, media_file_id=None,
                    duration=duration, sample_rate=sample_rate, bit_depth=bit_depth,
                    is_lossless=is_lossless)
    if sid is None:
        logger.info("stream provenance for track %s (%s): the material is already "
                    "registered first-hand at a higher rank — nothing to add",
                    track_id, provider_id)
    return sid


def _probe_audio(local_path: str):
    """(sample_rate, bit_depth, duration_seconds) of the container's audio
    stream; bit_depth is None for lossy codecs that carry no raw sample size,
    duration rounds to whole seconds (the signed-declaration granularity)."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries",
         "stream=sample_rate,bits_per_raw_sample,bits_per_sample,duration",
         "-show_entries", "format=duration",
         "-of", "json", local_path],
        capture_output=True, text=True, timeout=30, check=True)
    probed = json.loads(out.stdout)
    stream = probed["streams"][0]
    rate = int(stream["sample_rate"]) if stream.get("sample_rate") else None
    bits = int(stream.get("bits_per_raw_sample") or
               stream.get("bits_per_sample") or 0) or None
    raw_dur = stream.get("duration") or probed.get("format", {}).get("duration")
    duration = int(round(float(raw_dur))) if raw_dur else None
    return rate, bits, duration
