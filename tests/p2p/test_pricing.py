from desktop.p2p import pricing as pr


class Meter:
    def __init__(self, headroom=1.0):
        self.headroom = headroom
        self.sample_subs = []

    def subscribe_samples(self, cb):
        self.sample_subs.append(cb)


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


COSTS = {"mb.slice": (2000.0, 1_400_000.0), "mb.search": (50.0, 300.0), "sync.inventory": (20.0, 170.0)}


def _pricer(headroom=1.0, mode="enforce", w=40.0):
    m, c = Meter(headroom), Clock()
    p = pr.Pricer(m, costs=lambda: COSTS, mode=lambda: mode, w_ms=w, clock=c)
    return p, m, c


def test_base_is_our_cost_in_units_of_w():
    p, _, _ = _pricer()
    assert p.base_tasks("mb.slice") == (2000.0 + 1_400_000.0 / pr.BYTES_PER_MS) / 40.0     # ≈ 51.75
    assert p.base_tasks("sync.inventory") == pr.MIN_BASE_TASKS                              # floor
    assert p.base_tasks("unknown.endpoint") == pr.MIN_BASE_TASKS
    p.w_ms = 80.0
    assert p.base_tasks("mb.search") == pr.MIN_BASE_TASKS                                    # 50 ms + tiny bytes < 2 w
    p.w_ms = 20.0
    assert abs(p.base_tasks("mb.search") - 2.50075) < 0.001


def test_dormant_and_identity_lane_are_free_and_modes_gate_the_wire():
    p, m, _ = _pricer(headroom=0.9)
    assert p.dormant() and p.load_mult() == 1.0
    assert p.would_be("c", "mb.slice", "stranger") == 0 and p.price("c", "mb.slice", "stranger") == 0
    m.headroom = 0.2                                     # not dormant any more
    assert not p.dormant()
    assert p.would_be("c", "mb.slice", "identity") == 0                                     # reserved lane
    n = p.would_be("c", "mb.slice", "stranger")
    assert n == pr.MAX_TASKS                                                                # 51.75 × 5.2 → capped
    assert p.would_be("c", "sync.inventory", "anonymous") == 6                              # 1 × load_mult 5.2 → 6
    assert p.price("c", "sync.inventory", "anonymous") == 6                                 # enforce
    p2, m2, _ = _pricer(headroom=0.2, mode="shadow")
    assert p2.would_be("c", "sync.inventory", "anonymous") == 6 and p2.price("c", "sync.inventory", "anonymous") == 0
    p3, _, _ = _pricer(headroom=0.2, mode="off")
    assert p3.price("c", "sync.inventory", "anonymous") == 0 and p3.mode() == "off"
    p4, _, _ = _pricer(headroom=0.2, mode="nonsense")
    assert p4.mode() == pr.DEFAULT_MODE


def test_load_mult_is_progressive_to_the_ceiling():
    p, m, _ = _pricer()
    m.headroom = 0.5; assert p.load_mult() == 1.0
    m.headroom = 0.25; assert abs(p.load_mult() - 4.5) < 1e-9
    m.headroom = 0.0; assert p.load_mult() == pr.LOAD_MULT_MAX


def test_siege_integrates_pressure_and_decays():
    p, m, c = _pricer(headroom=1.0)
    for _ in range(30):                          # a minute of calm
        c.t += 2.0; p.tick()
    assert p.pi_mult() == 1.0
    m.headroom = 0.0                             # full pressure
    for _ in range(150):                         # 300 s: ∫ with decay ≈ 254 s → ≈ +0.85×
        c.t += 2.0; p.tick()
    assert 1.7 < p.pi_mult() < 1.95
    for _ in range(600):                         # 20 more min → near the steady state
        c.t += 2.0; p.tick()
    steady = 1.0 + (pr.PI_HALF_LIFE_S / 0.6931) * pr.PI_GAIN_PER_S     # ≈ 3.9× at these constants
    assert 3.0 < p.pi_mult() < steady + 0.01
    m.headroom = 1.0                             # relief: decays with the half-life
    for _ in range(300):                         # 10 min → about half of the siege is gone
        c.t += 2.0; p.tick()
    assert 1.8 < p.pi_mult() < 2.6
    siege_now = p.snapshot()["siege_s"]
    for _ in range(3000):                        # 100 min → back to ≈ 1
        c.t += 2.0; p.tick()
    assert p.pi_mult() < 1.05 and p.snapshot()["siege_s"] < siege_now
    assert set(p.snapshot()) >= {"mode", "w_ms", "w_calibrated", "headroom", "dormant", "load_mult", "pi_mult", "siege_s"}


def test_meter_hookup_and_calibration():
    p, m, _ = _pricer()
    assert m.sample_subs == [p.tick]
    p2 = pr.Pricer(None, costs=lambda: {}, mode=lambda: "shadow")
    assert p2.headroom() == 1.0 and p2.would_be("c", "x", "stranger") == 0
    w = p2.calibrate_w()                        # one real 64 MiB task
    assert 5.0 < w < 2000.0 and p2.snapshot()["w_calibrated"] is True
