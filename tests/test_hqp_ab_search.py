"""AB-1's choices that need no HQPlayer (backend/playback/hqp_ab_search.py):
who is compared and who is left out with what reason, the search that
probes the strongest contrast first and narrows a bracket per axis, the
chain's replay and focus check, the swap rules, the pair's balanced order
and its stops, and the sitting's silence and lead (AB-1-DESIGN §9 tests
4–10 and 12, on the logic itself)."""

import random
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import pytest  # noqa: E402

from playback import hqp_ab_search as S  # noqa: E402


def F(name, order, *, speed=2.0, klass="ok", phase=0.0, cutoff=0.9, ringing=100.0,
      group=None, reaches=True, at=True):
    return S.Filter(name, order, speed, klass, reaches, group,
                    {"phase": phase, "cutoff": cutoff, "ringing": ringing} if at else None)


CHAMP = F("champ", 0, speed=2.0, phase=0.0, cutoff=0.90, ringing=100)
FIELD = [
    F("min-long", 1, phase=1.0, cutoff=0.91, ringing=110),        # far on phase only
    F("lin-low", 2, phase=0.0, cutoff=0.70, ringing=105),         # far on the cut
    F("lin-ring", 3, phase=0.0, cutoff=0.89, ringing=5000),       # far on ringing
    F("lin-mid", 4, phase=0.0, cutoff=0.80, ringing=120),
    F("int-a", 5, phase=0.5, cutoff=0.88, ringing=90),
    F("lin-ring2", 6, phase=0.0, cutoff=0.90, ringing=800),
]


# -- the screen (§4.1, test 12) -------------------------------------------------------------

def test_the_screen_leaves_out_each_with_its_reason_and_one_of_each_identical_group():
    filters = [CHAMP,
               F("slow", 1, speed=0.7, klass="no"),
               F("unmeasured", 2, speed=None, klass=None),
               F("stops", 3, reaches=False),
               F("no-atlas", 4, at=False),
               F("twin-of-champ", 5, group="g0"),
               F("a1", 6, speed=1.5, group="g1"),
               F("a2", 7, speed=2.5, group="g1"),           # the faster of its group stays
               F("tight", 8, klass="tight", speed=1.1)]
    champ = S.Filter(**{**CHAMP.__dict__, "group": "g0"})
    sc = S.screen([champ] + filters[1:], champ, atlas=True)
    assert [f.name for f in sc.candidates] == ["a2", "tight"]
    assert sc.left_out == {"too_slow": ["slow"], "not_measured_here": ["unmeasured"],
                           "rate": ["stops"], "not_in_atlas": ["no-atlas"],
                           "identical": ["twin-of-champ", "a1"]}


def test_before_the_atlas_the_pairs_list_keeps_what_runs_here_a_to_z():
    filters = [CHAMP, F("zeta", 1), F("Alpha", 2), F("slow", 3, klass="no", speed=0.5),
               F("unmeasured", 4, klass=None, speed=None), F("no-atlas", 5, at=False)]
    sc = S.seconds(filters, CHAMP, atlas=False)
    assert [f.name for f in sc.candidates] == ["Alpha", "no-atlas", "zeta"]
    assert sc.left_out == {"too_slow": ["slow"], "not_measured_here": ["unmeasured"]}


def test_the_atlas_axes_come_from_its_context_fields():
    ctx = {"phase": "minimum", "f_3db_khz": 21.0, "pre_ring_ms_100": 0.0, "post_ring_ms_100": 2.0}
    assert S.axes_of(ctx, 44100) == {"phase": 1.0, "cutoff": pytest.approx(21000 / 22050),
                                     "ringing": pytest.approx(88.2)}
    assert S.axes_of({"void": ["x"]}, 44100) is None
    assert S.axes_of({"phase": "linear"}, 44100) is None


# -- the search (§14.3) --------------------------------------------------------------------

def test_the_first_probe_is_the_strongest_contrast_and_nearest_on_the_others():
    s = S.Search(FIELD, CHAMP)
    p = s.next_probe()
    # phase moves 0 → 1 (the whole axis) with the others nearly the champion's
    assert (p.name, p.axis) == ("min-long", "phase")


def test_no_difference_closes_the_axis_and_the_next_axis_is_probed():
    s = S.Search(FIELD, CHAMP)
    p = s.next_probe()
    s.record(p.axis, "champ", p.name, "same", champion="champ")
    q = s.next_probe()
    assert q.axis != "phase" and s.brackets["phase"].closed


def test_worse_closes_the_far_side_and_the_next_probe_lies_between():
    s = S.Search(FIELD, CHAMP)
    s.axis = "ringing"
    p = s.next_probe()
    assert p.name == "lin-ring"                           # the farthest on ringing
    s.record("ringing", "champ", "lin-ring", "worse", champion="champ")
    q = s.next_probe()
    assert q.axis == "ringing" and q.name == "lin-ring2"  # between, not past it
    assert s.brackets["ringing"].hi == s.place["lin-ring"]["ringing"]


def test_better_moves_the_champion_and_closes_the_side_it_left():
    s = S.Search(FIELD, CHAMP)
    s.axis = "ringing"
    s.record("ringing", "champ", "lin-ring", "better", champion="lin-ring")
    assert s.champion == "lin-ring"
    assert s.brackets["ringing"].lo == s.place["champ"]["ringing"]
    q = s.next_probe()
    assert q.axis == "ringing" and s.place["champ"]["ringing"] < s.place[q.name]["ringing"]


def test_the_log_replayed_gives_the_next_sitting_its_brackets():
    s = S.Search(FIELD, CHAMP)
    S.replay(s, [{"axis": "phase", "first_name": "champ", "second_name": "min-long",
                  "answer": "same", "check_step": False, "counted": True},
                 {"axis": "cutoff", "first_name": "champ", "second_name": "lin-low",
                  "answer": "first", "check_step": False, "counted": True},
                 {"axis": "ringing", "first_name": "champ", "second_name": "lin-ring",
                  "answer": "same", "check_step": False, "counted": False}])   # set aside
    assert s.brackets["phase"].closed and not s.brackets["ringing"].closed
    assert s.brackets["cutoff"].lo == s.place["lin-low"]["cutoff"]
    assert s.champion == "champ" and not s.heard


def test_the_pool_is_the_answer_trees_first_probes_and_bounded():
    s = S.Search(FIELD, CHAMP)
    pool = S.pool(s, limit=4)
    assert len(pool) == 4 and pool[0] == "min-long"
    assert set(S.pool(s)) <= {f.name for f in FIELD}


# -- the chain (§1.1, tests 4–8) -----------------------------------------------------------------

def _chain(field=FIELD, champ=CHAMP, **kw):
    return S.Chain(S.Search(field, champ), champ, **kw)


def _play(chain, *answers):
    played = []
    for a in answers:
        step = chain.step()
        played.append(step.plays)
        chain.answer(a)
    return played


def test_the_champion_replays_after_worse_and_the_duels_name_who_was_heard():
    c = _chain()
    plays = _play(c, "better", "worse", "better", "same")
    a, b = plays[0][1], plays[1][0]
    assert plays[0] == ["champ", a] and plays[1] == [b]
    assert plays[2] == [a]                    # the champion (a, since the swap) again first
    nxt = plays[3][0]
    d = [(x.first, x.second, x.answer, x.check) for x in c.duels]
    assert d == [("champ", a, "second", False), (a, b, "first", False),
                 (b, a, "second", True), (a, nxt, "same", False)]


def test_a_failed_focus_check_sets_the_answers_since_aside_and_undoes_the_swap():
    c = _chain()
    _play(c, "better", "worse", "same")       # the replay: no difference
    assert c.ended == "break" and c.champion == "champ"
    assert [x.counted for x in c.duels] == [False, False, False]
    assert c.step() is None


def test_a_passed_check_keeps_what_came_before_it():
    c = _chain()
    _play(c, "better", "worse", "better", "worse", "worse")
    assert c.ended == "break" and c.champion == c.duels[0].second
    assert [x.counted for x in c.duels] == [True, True, True, False, False]


def test_better_swaps_at_once():
    c = _chain()
    step = c.step()
    c.answer("better")
    assert c.champion == step.plays[1] and c.duels[0].swapped is True


def test_no_difference_keeps_the_faster_only_past_ten_per_cent():
    fast = [F("fast", 1, speed=3.0, phase=1.0)]
    close = [F("close", 1, speed=2.1, phase=1.0)]
    c = _chain(fast)
    _play(c, "same")
    assert c.champion == "fast" and c.duels[0].swapped is True
    assert c.note == {"same": True, "faster": True, "a": 3.0, "b": 2.0}
    c = _chain(close)
    _play(c, "same")
    assert c.champion == "champ" and c.duels[0].swapped is False


def test_a_costly_probe_asks_before_the_swap_and_keep_mine_keeps_it():
    for field in ([F("tight", 1, klass="tight", speed=1.9, phase=1.0)],
                  [F("slow-ish", 1, speed=0.9, phase=1.0)]):        # under half of 2.0
        c = _chain(field)
        c.step()
        c.answer("better")
        assert c.deciding is not None and c.note["cost"] and c.step() is None
        c.decide(False)
        assert c.champion == "champ" and c.duels[0].swapped is False


def test_the_chain_ends_at_its_cap_or_when_no_probe_is_left():
    c = _chain(steps=2)
    _play(c, "same", "same")
    assert c.step() is None and c.ended == "done"
    c = _chain([F("only", 1, phase=1.0)])
    _play(c, "same")
    assert c.step() is None and c.ended == "done"


# -- two filters, blind (§14.3, tests 9–10) --------------------------------------------------------

def test_after_every_second_round_each_side_has_played_first_in_half():
    orders = set()
    for seed in range(1000):
        p = S.Pair("yours", "other", random.Random(seed), cap=10)
        firsts = []
        for _ in range(10):
            o = p.order()
            if o is None:
                break
            firsts.append(o[0])
            p.pending = None                  # heard, not answered: the order alone
            if len(firsts) % 2 == 0:
                assert firsts.count("yours") == len(firsts) // 2
        orders.add(tuple(firsts[:2]))
    assert orders == {("yours", "other"), ("other", "yours")}      # not fixed within a block


def test_the_pair_stops_on_two_no_differences_a_clear_pick_or_the_cap():
    p = S.Pair("a", "b", random.Random(1))
    p.order(); p.answer("same")
    p.order(); p.answer("same")
    assert p.ended == "done" and p.why == "same"
    p = S.Pair("a", "b", random.Random(1))
    for _ in range(4):
        first, _second = p.order()
        p.answer("first" if first == "b" else "second")            # b each time
    assert p.why == "clear" and p.winner() == "b" and p.steady()
    p = S.Pair("a", "b", random.Random(1))
    for i in range(10):
        first, _second = p.order()
        picked_a = i % 2 == 0
        p.answer("first" if (first == "a") == picked_a else "second")
    assert p.why == "cap" and p.winner() is None and not p.steady()


def test_the_beta_rule():
    assert S.sure(4, 0) >= S.PAIR_SURE > S.sure(3, 0)
    assert S.sure(5, 0) == pytest.approx(1 - 1 / 64)
    assert S.sure(3, 3) == pytest.approx(0.5)


# -- the silence and the lead (§15.1, §16) ------------------------------------------------------------

def test_the_lead_covers_the_latest_pause_and_never_under_a_second():
    t = [S.Timing(d=1.0, start=0.0, r=2.0, p=0.1, tail=0.4, rate=705600),
         S.Timing(d=3.2, start=0.1, r=4.3, p=0.9, tail=2.6, rate=705600)]
    assert S.lead(t[:1]) == 1.0
    assert S.lead(t) == pytest.approx(1.2)


def test_the_silence_waits_for_the_slowest_end_start_and_resume():
    t = [S.Timing(d=1.0, start=0.0, r=2.0, p=0.1, tail=0.4, rate=705600),
         S.Timing(d=3.2, start=0.1, r=4.3, p=0.1, tail=2.6, rate=705600)]
    # the slowest end, then the slowest start to a first frame, the lead, the resume's way
    assert S.silence(t, 1.0) == pytest.approx(2.6 + (0.1 + 1.0 + 4.3) + 0.5)
    # a longer lead lengthens every resume's way to the output; where the pause
    # landed (p) does not enter
    assert S.silence(t, 1.5) == pytest.approx(2.6 + (0.1 + 1.5 + 4.3) + 0.5)
    assert S.silence([S.Timing(d=3.2, start=0.1, r=4.3, p=0.6, tail=2.6, rate=705600)], 1.0) \
        == pytest.approx(S.silence(t[1:], 1.0))


# -- the assistant's door (§7.1, §14.3) ----------------------------------------------------------

def test_an_hqp_ab_block_carries_one_filter_name():
    from tools.block_parser import parse_blocks_payload
    blocks = parse_blocks_payload('[{"kind": "hqp_ab", "items": [{"filter": " sinc-M "}, {"filter": "xla"}]},'
                                  ' {"kind": "hqp_ab", "items": [{"filter": ""}]}]')
    assert blocks == [{"kind": "hqp_ab", "items": [{"filter": "sinc-M"}]}]


def test_the_dominant_reason_left_out_the_most_ties_in_its_order():
    s = S.Screened([])
    assert s.dominant() is None
    for name in ("a", "b"):
        s.leave("too_slow", name)
    s.leave("not_measured_here", "c")
    assert s.dominant() == "too_slow"
    s.leave("not_measured_here", "d")
    assert s.dominant() == "not_measured_here"          # a tie: the earlier reason
    s.leave("hung", "e")
    s.leave("hung", "f")
    s.leave("hung", "g")
    assert s.dominant() == "hung"


def test_a_filter_asked_and_answered_without_a_speed_is_left_out_as_its_answer():
    champion = S.Filter("own", 0, 2.0, "ok", at={"phase": 0.0, "cutoff": 0.9, "ringing": 300.0})
    hung = S.Filter("h", 1, None, None, asked="hung")
    never = S.Filter("n", 2, None, None)
    out = S.screen([champion, hung, never], champion, atlas=False)
    assert out.left_out == {"hung": ["h"], "not_measured_here": ["n"]}
