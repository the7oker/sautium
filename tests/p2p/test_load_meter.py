from desktop.p2p import load_meter as lm


class Fake:
    """Injected clock + CPU counter: each `advance(dt, busy)` burns `busy`
    fraction of `cores` cores for dt seconds."""

    def __init__(self, cores=4):
        self.t = 1000.0
        self.cpu = 0.0
        self.cores = cores

    def advance(self, dt, busy):
        self.t += dt
        self.cpu += dt * busy * self.cores


def _meter(profile="standard", cores=4, playback=None):
    f = Fake(cores)
    m = lm.LoadMeter(profile, playback_probe=playback, cpu_seconds=lambda: f.cpu,
                     clock=lambda: f.t, cores=cores, mem_available_kib=lambda: 8 * 1024 * 1024)
    return m, f


def test_headroom_tracks_cpu_against_the_ceiling():
    m, f = _meter()
    assert m.headroom == 1.0 and m.dormant and m.announce_pace() == 1.0
    for _ in range(40):                       # 25 % busy of the machine = at the ceiling
        f.advance(2.0, 0.25); m.sample()
    assert abs(m.cpu_frac - 0.25) < 0.01
    assert m.headroom < 0.05 and not m.dormant
    assert 3.5 < m.announce_pace() <= 4.0
    for _ in range(40):
        f.advance(2.0, 0.0); m.sample()
    assert m.headroom > 0.95 and m.dormant


def test_lite_ceiling_is_lower():
    m, f = _meter("lite")
    for _ in range(40):
        f.advance(2.0, 0.15); m.sample()
    assert m.headroom < 0.05
    m2, f2 = _meter("standard")
    for _ in range(40):
        f2.advance(2.0, 0.15); m2.sample()
    assert m2.headroom > 0.35


def test_playback_halves_headroom_and_holds_mining_via_probe_or_lease():
    playing = {"on": False}
    m, f = _meter(playback=lambda: playing["on"])
    assert m.mining_hold() is None
    playing["on"] = True
    m.sample()
    assert m.playback_active and m.headroom == 0.5 and m.mining_hold() == "playback"
    assert m.announce_pace() == 2.0 * (1.0 + 3.0 * 0.5)      # not dormant while playing → 2×(1+1.5)
    playing["on"] = False
    m.sample()
    assert not m.playback_active and m.headroom == 1.0
    # event path: a lease that expires
    m.set_playback_active(True)
    assert m.playback_active
    f.advance(lm.PLAYBACK_LEASE_SECONDS + 1, 0.0)
    assert not m.playback_active
    m.set_playback_active(True); m.set_playback_active(False)
    assert not m.playback_active


def test_subscribers_fire_only_on_band_or_flag_changes():
    m, f = _meter()
    seen = []
    m.subscribe(seen.append)
    for _ in range(5):
        f.advance(2.0, 0.0); m.sample()
    assert len(seen) == 1                      # the initial state once, then silence while idle
    for _ in range(40):
        f.advance(2.0, 0.25); m.sample()
    assert 4 <= len(seen) <= 13               # bands crossed on the way down, not one per sample
    assert seen[-1]["dormant"] is False and seen[-1]["headroom"] < 0.1
    n = len(seen)
    m.set_playback_active(True)
    assert len(seen) == n + 1 and seen[-1]["playback"] is True
    snap = m.snapshot()
    assert set(snap) >= {"profile", "ceiling", "cpu_frac", "mem_available_mib", "playback", "headroom", "dormant", "pace", "updated_at"}
    assert snap["mem_available_mib"] == 8 * 1024


def test_install_current_and_default_profile():
    assert lm.default_profile() in ("lite", "standard")
    m, _ = _meter()
    assert lm.install(m) is m and lm.current() is m
