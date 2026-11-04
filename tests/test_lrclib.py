"""LRCLIB's lookups (backend/lrclib.py): what /api/get and /api/search are
asked, which record is taken, and what each answer leaves in the ledger.
Until 2026-11-04 an owned track longer than an hour was sent with its length,
LRCLIB refused each lookup (duration "must be between 1 and 3600"), and the
refusal was stored as a retryable 'error': the background loop asked again
on every pass, and the search fallback never ran. The search itself sent
`q`, with which LRCLIB ignores the artist.

LRCLIB is a fake that answers as its docs, its source and its answers of
2026-11-04 say: /api/get refuses a duration outside 1..3600 and, without
one, answers the oldest record of the names; /api/search validates nothing,
matches `q`'s words in any column with the artist ignored, the structured
fields each in its own column, and a miss is an empty list. The ledger is a real PostgreSQL —
the module's scratch database (conftest.scratch_dsn)."""

import itertools
import sys
import uuid
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

httpx = pytest.importorskip("httpx")

import lrclib  # noqa: E402

_ids = itertools.count(1)


class FakeLrclib:
    """/api/get and /api/search over a list of records, oldest first; every
    request kept. `search_status` stands for a search that fails as a front
    or an overloaded server would make it fail."""

    def __init__(self, records=(), refuse_get=False, search_status=None):
        self.records = list(records)
        self.refuse_get = refuse_get
        self.search_status = search_status
        self.requests: list[tuple[str, dict]] = []

    def __call__(self, request: "httpx.Request") -> "httpx.Response":
        path, params = request.url.path, dict(request.url.params)
        self.requests.append((path, params))
        if path == "/api/get":
            return self._get(params)
        if path == "/api/search":
            if self.search_status:
                return httpx.Response(self.search_status, json={"statusCode": self.search_status})
            return httpx.Response(200, json=self._search(params))
        return httpx.Response(404)

    def _get(self, params):
        if self.refuse_get or not params.get("track_name"):
            return httpx.Response(400, json={"message": "track_name: must not be empty",
                                             "name": "ValidationError", "statusCode": 400})
        if "duration" in params and not 1 <= float(params["duration"]) <= 3600:
            return httpx.Response(400, json={"message": "duration: must be between 1 and 3600",
                                             "name": "ValidationError", "statusCode": 400})
        for r in self.records:
            if (r["trackName"], r["artistName"]) != (params["track_name"], params["artist_name"]):
                continue
            if "album_name" in params and r["albumName"] != params["album_name"]:
                continue
            if "duration" in params and abs(r["duration"] - float(params["duration"])) > 2:
                continue
            return httpx.Response(200, json=r)
        return httpx.Response(404, json={"message": "Failed to find specified track",
                                         "name": "TrackNotFound", "statusCode": 404})

    def _search(self, params):
        if "q" in params:
            words = params["q"].lower().split()
            return [r for r in self.records
                    if all(any(w in r[f].lower() for f in ("trackName", "artistName", "albumName"))
                           for w in words)]
        if "track_name" not in params:
            return []
        return [r for r in self.records
                if params["track_name"].lower() in r["trackName"].lower()
                and params.get("artist_name", "").lower() in r["artistName"].lower()]


def _record(title, artist, album, duration, **lyrics):
    return {"id": next(_ids), "trackName": title, "artistName": artist,
            "albumName": album, "duration": duration, "instrumental": False,
            "plainLyrics": lyrics.get("plain"), "syncedLyrics": lyrics.get("synced")}


@pytest.fixture
def service():
    made = []

    def make(fake: FakeLrclib) -> lrclib.LrclibService:
        svc = lrclib.LrclibService()
        svc.client.close()
        svc.client = httpx.Client(base_url=lrclib.LRCLIB_BASE_URL,
                                  transport=httpx.MockTransport(fake))
        made.append(svc)
        return svc
    yield make
    for svc in made:
        svc.close()


@pytest.fixture
def db(scratch_dsn):
    import database
    from sqlalchemy.orm import sessionmaker
    engine = database.make_engine(scratch_dsn)
    session = sessionmaker(bind=engine, autoflush=False)()
    yield session
    session.close()
    engine.dispose()


def _track(db, title: str) -> uuid.UUID:
    from sqlalchemy import text
    tid = uuid.uuid4()
    db.execute(text("INSERT INTO tracks (id, title) VALUES (:id, :t)"), {"id": tid, "t": title})
    db.commit()
    return tid


def _ledger(db, tid) -> tuple:
    from sqlalchemy import text
    return tuple(db.execute(text(
        "SELECT fetch_status, data->>'external_id' FROM external_metadata "
        "WHERE entity_type = 'track' AND entity_id = :id AND source = 'lrclib' "
        "AND metadata_type = 'lyrics'"), {"id": str(tid)}).one())


def test_a_duration_lrclib_refuses_is_left_out_of_the_exact_lookup(service):
    fake = FakeLrclib()
    svc = service(fake)
    for duration in (0, 1, 3600, 3601, 4748):
        svc.get_lyrics("A Title", "An Artist", "An Album", duration)
    sent = [params.get("duration") for path, params in fake.requests if path == "/api/get"]
    assert sent == [None, "1", "3600", None, None]
    assert all(params["album_name"] == "An Album" for _, params in fake.requests)


def test_a_track_longer_than_an_hour_is_found_by_its_names_or_marked_missing(service, db):
    """The B-49 case: the lookup LRCLIB used to refuse ends in a hit or a
    miss — never an 'error' the loop asks again about. By the names alone
    LRCLIB answers its oldest record of them, here a shorter edit: lyrics are
    kept per track, every edition of the song, so it is taken."""
    edit = _record("A Long Suite", "An Artist", "An Album", 3600, plain="An edit's line.")
    suite = _record("A Long Suite", "An Artist", "An Album", 4747, plain="The suite's line.")
    fake = FakeLrclib([edit, suite])
    svc = service(fake)
    found = _track(db, "A Long Suite")
    svc._store_external_metadata(db, str(found), "error", None)   # as the loop left it
    result = svc.fetch_and_store(db, found, "A Long Suite", "An Artist",
                                 album_name="An Album", duration=4748)
    assert result["status"] == "plain" and result["data"]["id"] == edit["id"]
    assert _ledger(db, found) == ("success", str(edit["id"]))
    assert fake.requests == [("/api/get", {"track_name": "A Long Suite", "artist_name": "An Artist",
                                           "album_name": "An Album"})]

    missing = _track(db, "A Radio Show 002")
    svc._store_external_metadata(db, str(missing), "error", None)
    result = svc.fetch_and_store(db, missing, "A Radio Show 002", "A DJ",
                                 album_name="A Show", duration=3633)
    assert result["status"] == "not_found" and _ledger(db, missing) == ("not_found", None)


def test_the_search_takes_this_artists_record_never_another_with_the_title(service, db):
    other = _record("A Common Title", "Another Artist", "Another Album", 200, plain="Not ours.")
    svc = service(FakeLrclib([other]))
    tid = _track(db, "A Common Title")
    result = svc.fetch_and_store(db, tid, "A Common Title", "An Artist",
                                 album_name="An Album", duration=200)
    assert result["status"] == "not_found" and _ledger(db, tid) == ("not_found", None)


def test_a_refused_exact_lookup_falls_through_to_the_search(service, db):
    fake = FakeLrclib([_record("A Title", "An Artist", "An Album", 200, synced="[00:01.00] A line")],
                      refuse_get=True)
    svc = service(fake)
    tid = _track(db, "A Title")
    result = svc.fetch_and_store(db, tid, "A Title", "An Artist", album_name="An Album", duration=200)
    assert result["status"] == "synced" and _ledger(db, tid)[0] == "success"
    assert [path for path, _ in fake.requests] == ["/api/get", "/api/search"]   # the 400 asked once


@pytest.mark.parametrize("status, searches", [(400, 1), (404, 1), (503, 4)])
def test_a_search_that_fails_after_a_refused_lookup_is_asked_again_later(
        service, db, monkeypatch, status, searches):
    """LRCLIB's search neither validates nor answers a miss with a 404, so
    either comes from in front of it, like a 503: a retryable 'error', never
    a miss the track would keep."""
    monkeypatch.setattr(lrclib.time, "sleep", lambda s: None)
    fake = FakeLrclib(refuse_get=True, search_status=status)
    svc = service(fake)
    tid = _track(db, "A Title")
    result = svc.fetch_and_store(db, tid, "A Title", "An Artist", album_name="An Album", duration=200)
    assert result["status"] == "error" and _ledger(db, tid)[0] == "error"
    assert [path for path, _ in fake.requests] == ["/api/get"] + ["/api/search"] * searches
