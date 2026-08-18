"""The node-side mailbox-domain table: shape, semantics, and the HMAC mirror
of worker/verify.js (checked against what the Worker really emits under the
test pepper in test_birth_cert)."""

import re

from desktop.p2p import email_domains as ed

HEX64 = re.compile(r"^[0-9a-f]{64}$")


def test_table_is_complete_and_well_formed():
    assert len(ed.EMAIL_DOMAINS) >= 60
    seen_tokens = set()
    for domain, (tier, reliability, token) in ed.EMAIL_DOMAINS.items():
        assert domain == domain.lower().strip() and "@" not in domain
        assert tier in ed.TIERS, domain
        assert 0.0 <= reliability <= 1.0, domain
        assert HEX64.match(token), f"{domain}: run --regen"          # never ship an empty token
        assert token not in seen_tokens
        seen_tokens.add(token)
        if tier == "disposable":
            assert reliability == 0.0, domain
        if tier == "protected":
            assert reliability >= 0.7, domain


def test_lookups_and_informativeness():
    gmail = ed.EMAIL_DOMAINS["gmail.com"][2]
    assert ed.tier_of(gmail) == "protected" and ed.reliability(gmail) == 1.0
    assert ed.tier_of(gmail.upper()) == "protected"
    assert not ed.informative(gmail)                                   # millions share it
    gmx = ed.EMAIL_DOMAINS["gmx.de"][2]
    assert ed.tier_of(gmx) == "open" and not ed.informative(gmx)
    trash = ed.EMAIL_DOMAINS["mailinator.com"][2]
    assert ed.tier_of(trash) == "disposable" and ed.reliability(trash) == 0.0 and ed.informative(trash)
    assert ed.tier_of("ff" * 32) is None and ed.reliability("ff" * 32) is None and ed.informative("ff" * 32)
    assert ed.tier_of(None) is None and ed.reliability("") is None and ed.informative(None)


def test_token_mirror_is_deterministic_and_domain_only():
    t = ed.compute_token("pepper", "Gmail.com")
    assert HEX64.match(t) and t == ed.compute_token("pepper", "gmail.com")
    assert t != ed.compute_token("other-pepper", "gmail.com")
    assert t != ed.compute_token("pepper", "googlemail.com")
