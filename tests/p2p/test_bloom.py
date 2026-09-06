"""The holdings filter primitive: sizing, no false negatives, a false-
positive rate near the target, and a lossless wire round trip."""

import random
import uuid

from desktop.p2p.bloom import BloomFilter


def _uuids(n, seed):
    rnd = random.Random(seed)
    return [str(uuid.UUID(int=rnd.getrandbits(128), version=5)) for _ in range(n)]


def test_sizing_matches_the_textbook():
    bf = BloomFilter.sized(1_000_000, 0.01)
    assert 9.5 <= bf.m / 1_000_000 <= 9.7      # ~9.6 bits per element at 1%
    assert bf.k == 7
    assert bf.m % 8 == 0


def test_no_false_negatives_and_fpr_near_target():
    members = _uuids(50_000, 1)
    strangers = _uuids(50_000, 2)
    bf = BloomFilter.sized(len(members), 0.01)
    bf.update(members)
    assert bf.n == len(members)
    assert all(u in bf for u in members)
    fp = sum(1 for u in strangers if u in bf) / len(strangers)
    assert fp < 0.02, fp                           # target 1%, allow 2×
    assert abs(bf.expected_fpr() - 0.01) < 0.005


def test_hits_keeps_order_and_only_probable_members():
    members = _uuids(1_000, 3)
    strangers = _uuids(1_000, 4)
    bf = BloomFilter.sized(len(members), 0.001)
    bf.update(members)
    mixed = strangers[:500] + members[:10] + strangers[500:]
    hits = bf.hits(mixed)
    assert hits[: len([h for h in hits if h in members[:10]])] or True
    assert all(m in hits for m in members[:10])
    assert [h for h in hits if h in members] == members[:10]   # order preserved
    assert len(hits) - 10 <= 5                     # ≤0.5% of 1990 strangers


def test_wire_round_trip_is_lossless():
    members = _uuids(5_000, 5)
    bf = BloomFilter.sized(5_000, 0.01)
    bf.update(members)
    d = bf.to_dict()
    back = BloomFilter.from_dict(d)
    assert (back.m, back.k, back.n) == (bf.m, bf.k, bf.n)
    assert back.bits == bf.bits
    assert all(u in back for u in members)


def test_from_dict_rejects_a_truncated_bit_array():
    bf = BloomFilter.sized(100, 0.01)
    d = bf.to_dict()
    d["bits"] = d["bits"][:-8]
    try:
        BloomFilter.from_dict(d)
    except ValueError:
        return
    raise AssertionError("truncated bits accepted")


def test_dashed_and_plain_uuid_forms_agree():
    u = str(uuid.uuid5(uuid.NAMESPACE_DNS, "sautium"))
    bf = BloomFilter.sized(10, 0.01)
    bf.add(u)
    assert u.replace("-", "") in bf
