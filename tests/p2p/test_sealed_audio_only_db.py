"""Audio analysis travels sealed or not at all (desktop/sync_client.py,
desktop/p2p/sync_queries.py, backend/routers/sync.py), on a real
PostgreSQL built from the migrations — skipped without a cluster: a peer
without the `segments` capability is asked for no embeddings, its sealed
audio features still; an import takes
only the categories it knows, whatever a payload names itself; a local mean
without segments is a gap, in the plan and in the walk's gap sets; neither
peer surface serves an unsealed mean."""

import asyncio
import sys
import uuid
from pathlib import Path

import psycopg2
import pytest

REPO = Path(__file__).resolve().parents[2]
for p in (REPO, REPO / "backend"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from desktop import sync_client  # noqa: E402
from desktop.p2p import sync_queries  # noqa: E402

TRACK = str(uuid.UUID(int=101))
OTHER = str(uuid.UUID(int=102))          # a track here the peer was never asked about
ARTIST = str(uuid.UUID(int=201))
MODEL = str(uuid.UUID(int=301))


@pytest.fixture
def conn(scratch_dsn):
    c = psycopg2.connect(scratch_dsn)
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute("TRUNCATE tracks, artists, embedding_models CASCADE")
        cur.execute("INSERT INTO artists (id, name) VALUES (%s, 'Imported Only')", (ARTIST,))
        cur.execute("INSERT INTO tracks (id, title) VALUES (%s, 'mean only'), (%s, 'not asked')",
                    (TRACK, OTHER))
        cur.execute("INSERT INTO track_artists (track_id, artist_id, role) VALUES (%s, %s, 'primary')",
                    (TRACK, ARTIST))
        cur.execute("INSERT INTO embedding_models (id, name, dimension) VALUES (%s, 'clap', 512)", (MODEL,))
    yield c
    c.close()


def _mean_without_segments(conn):
    """What the unsealed mean pull left: an imported source, a mean, no segments."""
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO analysis_sources (track_id, chromaprint, imported)
                       VALUES (%s, 'AQAA', true) RETURNING id""", (TRACK,))
        source = cur.fetchone()[0]
        cur.execute("""INSERT INTO embeddings (track_id, model_id, vector, analysis_source_id,
                                               analysis_version)
                       VALUES (%s, %s, %s::vector, %s, 2)""",
                    (TRACK, MODEL, "[" + ",".join(["0.01"] * 512) + "]", source))


class _Peer:
    """A peer's inventory for one track, and the pulls asked of it."""
    base_url = "https://peer:1"
    peer_pubkey = "ab" * 32

    def __init__(self, capabilities, segments=12, features=False):
        self.capabilities, self.segments, self.pulls = list(capabilities), segments, []
        self.features = features

    def get_health(self):
        return {"capabilities": self.capabilities}

    def sync_inventory(self, track_uuids):
        return {"tracks": list(track_uuids) + [OTHER],
                "embeddings": [[TRACK, 2, self.segments], [OTHER, 2, 12]],
                "audio_features": [[TRACK, 2]] if self.features else []}

    def sync_pull(self, category, uuids):
        self.pulls.append(category)
        self.asked = getattr(self, "asked", []) + list(uuids)
        return {"category": category, "items": [], "batches": {}}


def test_a_peer_without_segments_is_asked_for_no_embeddings(conn, scratch_dsn):
    peer = _Peer(capabilities=[], features=True)
    sync_client.SyncClient(peer, scratch_dsn).run_sync([TRACK])
    assert peer.pulls == ["audio-features"]                 # sealed rows of their own


def test_a_mean_without_segments_is_a_gap_in_the_plan_and_the_gap_sets(conn, scratch_dsn):
    _mean_without_segments(conn)
    peer = _Peer(capabilities=["segments"])
    sync_client.SyncClient(peer, scratch_dsn).run_sync([TRACK])
    assert peer.pulls == ["segments"]
    assert peer.asked == [TRACK]                              # the inventory's word on OTHER: no answer
    assert sync_queries.get_unenriched_artist_uuids(conn) == [ARTIST]
    assert sync_queries.get_incomplete_artist_uuids(conn) == [ARTIST]
    unsealed = _Peer(capabilities=["segments"], segments=0)       # nothing sealed to give
    sync_client.SyncClient(unsealed, scratch_dsn).run_sync([TRACK])
    assert unsealed.pulls == []


def test_an_import_takes_only_the_categories_it_knows(conn, scratch_dsn):
    payload = {"items": [{"track_uuid": TRACK, "model_uuid": MODEL, "model_name": "clap",
                          "vector": [0.01] * 512, "analysis_version": 9}]}
    client = sync_client.SyncClient(None, scratch_dsn)
    try:
        assert client._import_items("embeddings", payload) == 0
        assert client._import_items("tracks", payload) == 0
    finally:
        client._close_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM embeddings")
        assert cur.fetchone()[0] == 0


def test_no_surface_serves_an_unsealed_mean():
    assert "embeddings" not in sync_queries.PULL_HANDLERS
    routers_sync = pytest.importorskip("routers.sync")
    paths = {r.path for r in routers_sync.router.routes}
    assert "/api/sync/pull/embeddings" not in paths and "/api/sync/pull/segments" in paths
