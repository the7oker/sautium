"""Address canonicalization/formatting — the IPv6-Ф0 invariants: one
spelling per address in every pseudonym, unambiguous host:port text."""

from desktop.p2p import contact_log, mb_slice_queries
from desktop.p2p.addrs import canon_host, fmt_addr, fmt_host, is_ipv6


def test_canonical_spelling_is_unique_per_address():
    forms = ["2a00:0DB8::1", "2a00:db8:0:0:0:0:0:1", "[2a00:db8::1]", "2a00:db8::1%eth0"]
    assert {canon_host(f) for f in forms} == {"2a00:db8::1"}
    assert canon_host("203.0.113.7") == "203.0.113.7"
    assert canon_host("Example.COM") == "example.com"
    assert canon_host("") == "" and canon_host(None) == ""


def test_fmt_addr_is_unambiguous_for_both_families():
    assert fmt_addr("203.0.113.7", 8801) == "203.0.113.7:8801"
    assert fmt_addr("2a00:0DB8::1", 8801) == "[2a00:db8::1]:8801"
    assert fmt_addr("[2a00:db8::1]", "8801") == "[2a00:db8::1]:8801"
    assert fmt_host("example.com") == "example.com"
    assert is_ipv6("2a00:db8::1") and not is_ipv6("203.0.113.7") and not is_ipv6("host")


def test_pseudonym_formulas_agree_across_v6_spellings():
    a = mb_slice_queries.addr_uuid("2a00:0DB8::1")
    assert a == mb_slice_queries.addr_uuid("2a00:db8:0:0:0:0:0:1")
    assert a == mb_slice_queries.addr_uuid("https://[2a00:db8::1]:8801/api/x")
    assert a != mb_slice_queries.addr_uuid("2a00:db8::2")
    addr1, sub1 = contact_log.addr_ids("2a00:0DB8::1")
    addr2, sub2 = contact_log.addr_ids("[2a00:db8:0:0:0:0:0:1]")
    assert addr1 == addr2 == a and sub1 == sub2
    # the /48 axis groups the household, splits strangers
    _, sub_same48 = contact_log.addr_ids("2a00:db8:0:beef::7")
    _, sub_other = contact_log.addr_ids("2a00:db9::1")
    assert sub1 == sub_same48 and sub1 != sub_other
    # v4 behaviour unchanged
    assert mb_slice_queries.addr_uuid("203.0.113.7") == mb_slice_queries.addr_uuid("HTTPS://203.0.113.7:8801/x")
