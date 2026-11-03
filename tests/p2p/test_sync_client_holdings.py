"""SyncClient's inventory routing (desktop/sync_client.py): core and bulk
gaps alike go through the peer's holdings filter when the ask outweighs the
filter — only the hits reach the exact inventory — and directly when the
peer publishes no filter; a refusal that names a wait is waited out within
the run."""

import uuid

from desktop import sync_client
from desktop.p2p.bloom import BloomFilter
from desktop.sync_client import EMPTY_INVENTORY, SyncClient


def _uuids(n):
    return [str(uuid.uuid4()) for _ in range(n)]


class _Api:
    """The peer as the client sees it: a holdings filter over the tracks it
    holds, and an inventory that records exactly what it was asked."""
    base_url = "https://peer:1"
    peer_pubkey = "ab" * 32

    def __init__(self, held_tracks, capabilities=("holdings",)):
        self.capabilities = list(capabilities)
        self.held_tracks = held_tracks
        self.asked = []

    def get_health(self):
        return {"capabilities": self.capabilities,
                "holdings": {"version": "v1", "tracks": len(self.held_tracks)}}

    def sync_holdings(self, have=None):
        t = BloomFilter.sized(max(len(self.held_tracks), 1000)); t.update(self.held_tracks)
        return {"version": "v1", "tracks": t.to_dict()}

    def sync_inventory(self, track_uuids):
        self.asked.append(list(track_uuids))
        return dict(EMPTY_INVENTORY, tracks=list(track_uuids))


def _client(api):
    c = SyncClient(api_client=api, db_dsn="postgresql://unused", holdings_cache={})
    c.peer_capabilities = set(api.capabilities)
    return c


def test_core_gaps_are_filtered_like_the_bulk():
    core_t, bulk_t = _uuids(3000), _uuids(3000)
    held_t = core_t[:2] + bulk_t[:3]
    api = _Api(held_t)
    inv = _client(api)._holdings_inventory(api.get_health(), core_t + bulk_t)
    assert inv is not None
    asked_t = [u for tracks in api.asked for u in tracks]
    # every held element is asked about — core and bulk alike — and a 1 %
    # filter adds at most noise on top of it, never a full ask
    assert set(held_t) <= set(asked_t)
    assert len(asked_t) < 200


def test_small_ask_goes_directly_and_no_filter_means_direct():
    core_t = _uuids(5)
    big = _Api(_uuids(20000))         # filter heavier than a 5-uuid ask
    _client(big)._holdings_inventory(big.get_health(), core_t)
    assert big.asked == [core_t]
    plain = _Api([], capabilities=("segments",))   # no holdings capability at all
    _client(plain)._holdings_inventory(plain.get_health(), core_t)
    assert plain.asked == [core_t]


def test_nothing_held_is_an_empty_inventory_not_a_failure(monkeypatch):
    monkeypatch.setattr(sync_client.time, "sleep", lambda s: None)
    api = _Api([])                      # an empty filter never hits
    inv = _client(api)._holdings_inventory(api.get_health(), _uuids(3000))
    assert inv == dict(EMPTY_INVENTORY) and api.asked == []

    class _Down(_Api):
        def sync_holdings(self, have=None):
            return None
    down = _Down(_uuids(5))
    assert _client(down)._holdings_inventory(down.get_health(), _uuids(3000)) is None


def test_a_busy_peer_is_asked_again_after_the_wait_it_names(monkeypatch):
    """A refusal that names a wait — a 429's window, a busy 503 — at the
    run's first bulk step is waited out and asked again within the run,
    for the holdings filter and the exact inventory alike."""
    waits = []
    monkeypatch.setattr(sync_client.time, "sleep", waits.append)
    busy = {"detail": "busy", "retry_after": 30}

    class _Busy(_Api):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.refusals = {"holdings": 1, "inventory": 1}

        def sync_holdings(self, have=None):
            if self.refusals["holdings"]:
                self.refusals["holdings"] -= 1
                return dict(busy)
            return super().sync_holdings(have)

        def sync_inventory(self, track_uuids):
            if self.refusals["inventory"]:
                self.refusals["inventory"] -= 1
                return dict(busy)
            return super().sync_inventory(track_uuids)

    held = _uuids(3000)
    api = _Busy(held[:2])
    inv = _client(api)._holdings_inventory(api.get_health(), held)
    assert inv is not None and set(held[:2]) <= set(inv["tracks"])
    assert waits == [30, 30]

    waits.clear()
    direct = _Busy([], capabilities=("segments",))
    assert _client(direct)._fetch_inventory(_uuids(3))["tracks"]
    assert waits == [30]


def test_merged_inventory_never_aliases_the_shared_empty_constant():
    api = _Api([], capabilities=("segments",))
    tracks = _uuids(3)
    inv = _client(api)._fetch_inventory(tracks)
    assert inv["tracks"] == tracks
    assert EMPTY_INVENTORY == {"tracks": [], "embeddings": [], "audio_features": []}
