"""SyncClient's inventory routing (desktop/sync_client.py): core and bulk
gaps alike go through the peer's holdings filter when the ask outweighs the
filter — only the hits reach the exact inventory — and directly when the
peer publishes no filter."""

import uuid

from desktop.p2p.bloom import BloomFilter
from desktop.sync_client import EMPTY_INVENTORY, SyncClient


def _uuids(n):
    return [str(uuid.uuid4()) for _ in range(n)]


class _Api:
    """The peer as the client sees it: a holdings filter over what it
    holds, and an inventory that records exactly what it was asked."""
    base_url = "https://peer:1"
    peer_pubkey = "ab" * 32

    def __init__(self, held_tracks, held_artists, capabilities=("holdings",)):
        self.capabilities = list(capabilities)
        self.held_tracks, self.held_artists = held_tracks, held_artists
        self.asked = []

    def get_health(self):
        return {"capabilities": self.capabilities,
                "holdings": {"version": "v1", "tracks": len(self.held_tracks),
                             "artists": len(self.held_artists)}}

    def sync_holdings(self, have=None):
        t = BloomFilter.sized(max(len(self.held_tracks), 1000)); t.update(self.held_tracks)
        a = BloomFilter.sized(max(len(self.held_artists), 1000)); a.update(self.held_artists)
        return {"version": "v1", "tracks": t.to_dict(), "artists": a.to_dict()}

    def sync_inventory(self, track_uuids, artist_uuids):
        self.asked.append((list(track_uuids or []), list(artist_uuids or [])))
        return dict(EMPTY_INVENTORY, tracks=list(track_uuids))


def _client(api):
    c = SyncClient(api_client=api, db_dsn="postgresql://unused", holdings_cache={})
    c.peer_capabilities = set(api.capabilities)
    return c


def test_core_gaps_are_filtered_like_the_bulk():
    core_t, core_a, bulk_t, bulk_a = _uuids(3000), _uuids(40), _uuids(3000), _uuids(40)
    held_t, held_a = core_t[:2] + bulk_t[:3], core_a[:1] + bulk_a[:1]
    api = _Api(held_t, held_a)
    inv = _client(api)._holdings_inventory(api.get_health(), core_t + bulk_t, core_a + bulk_a)
    assert inv is not None
    asked_t = [u for tracks, _ in api.asked for u in tracks]
    asked_a = [u for _, artists in api.asked for u in artists]
    # every held element is asked about — core and bulk alike — and a 1 %
    # filter adds at most noise on top of it, never a full ask
    assert set(held_t) <= set(asked_t) and set(held_a) <= set(asked_a)
    assert len(asked_t) < 200 and len(asked_a) < 10


def test_small_ask_goes_directly_and_no_filter_means_direct():
    core_t, core_a = _uuids(5), _uuids(2)
    big = _Api(_uuids(20000), _uuids(2000))         # filter heavier than a 7-uuid ask
    _client(big)._holdings_inventory(big.get_health(), core_t, core_a)
    assert big.asked == [(core_t, []), ([], core_a)]
    plain = _Api([], [], capabilities=("segments",))   # no holdings capability at all
    _client(plain)._holdings_inventory(plain.get_health(), core_t, core_a)
    assert plain.asked == [(core_t, []), ([], core_a)]


def test_nothing_held_is_an_empty_inventory_not_a_failure():
    api = _Api([], [])                      # an empty filter never hits
    inv = _client(api)._holdings_inventory(api.get_health(), _uuids(3000), _uuids(50))
    assert inv == dict(EMPTY_INVENTORY) and api.asked == []

    class _Down(_Api):
        def sync_holdings(self, have=None):
            return None
    down = _Down(_uuids(5), _uuids(5))
    assert _client(down)._holdings_inventory(down.get_health(), _uuids(3000), _uuids(50)) is None
