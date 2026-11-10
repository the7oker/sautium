"""SyncClient's inventory routing (desktop/sync_client.py): core and bulk
gaps alike go through the peer's holdings filter when the ask outweighs the
filter — only the hits reach the exact inventory — and directly when the
peer publishes no filter; a track whose one gap is its instruments is
tested against the peer's instruments filter alone; a refusal that names a
wait is waited out within the run."""

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

    def __init__(self, held_tracks, capabilities=("holdings",), instrumented=None):
        self.capabilities = list(capabilities)
        self.held_tracks = held_tracks
        self.instrumented = instrumented        # None: a peer that publishes no such filter
        self.asked = []

    def get_health(self):
        return {"capabilities": self.capabilities,
                "holdings": {"version": "v1", "tracks": len(self.held_tracks),
                             "instruments": len(self.instrumented or [])}}

    def sync_holdings(self, have=None):
        t = BloomFilter.sized(max(len(self.held_tracks), 1000)); t.update(self.held_tracks)
        payload = {"version": "v1", "tracks": t.to_dict()}
        if self.instrumented is not None:
            i = BloomFilter.sized(max(len(self.instrumented), 1000)); i.update(self.instrumented)
            payload["instruments"] = i.to_dict()
        return payload

    def sync_inventory(self, track_uuids):
        self.asked.append(list(track_uuids))
        return dict(EMPTY_INVENTORY, tracks=list(track_uuids))


def _client(api, only_instruments=()):
    """The client, what this node holds stated: the tracks whose segments
    and features it holds and whose instruments it lacks."""
    c = SyncClient(api_client=api, db_dsn="postgresql://unused", holdings_cache={})
    c.peer_capabilities = set(api.capabilities)
    c._lacking_only_instruments = lambda: set(only_instruments)
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


def test_an_instruments_gap_is_asked_only_where_the_peers_instruments_filter_holds_it():
    """A track whose segments and features are held here and whose
    instruments are not is asked of a peer only when the peer holds its
    instruments — the peer holding its segments says nothing of them, and
    such a gap no peer fills would be an inventory request every run. A
    track lacking everything, or holding everything, is asked as before."""
    only_instruments = _uuids(4)        # sonic records held here, no instruments
    lacking, complete = _uuids(2), _uuids(2)
    noise = _uuids(3000)                # the ask outweighs the filters
    peer_sonic = only_instruments + lacking + complete
    api = _Api(peer_sonic, instrumented=only_instruments[:1] + lacking[1:])
    _client(api, only_instruments=only_instruments)._holdings_inventory(
        api.get_health(), only_instruments + lacking + complete + noise)
    asked = {u for tracks in api.asked for u in tracks}
    assert only_instruments[0] in asked and not set(only_instruments[1:]) & asked
    assert set(lacking) <= asked and set(complete) <= asked
    assert len(asked) < 100

    old = _Api(peer_sonic)              # a peer that publishes no instruments filter
    _client(old, only_instruments=only_instruments)._holdings_inventory(
        old.get_health(), only_instruments + lacking + noise)
    asked = {u for tracks in old.asked for u in tracks}
    assert not set(only_instruments) & asked and set(lacking) <= asked


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
    assert EMPTY_INVENTORY == {"tracks": [], "embeddings": [], "audio_features": [],
                               "instruments": []}
