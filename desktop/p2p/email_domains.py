"""Mailbox domains — node-side policy over the certificate's domain token
(Valerii, 2026-08-18: "the domain hash can be a reliability index too").

The certificate carries `email_domain_token = HMAC(EMAIL_PEPPER,
"email-domain:" + domain)`: nodes see EQUALITY, never the domain. This
table is the maintainer's precomputed tokens for the domains worth having
an opinion about, so a node can read two things off a token without the
Worker pinning a judgement into the certificate:

- `tier` — how POPULOUS the provider is, which decides whether sharing the
  domain is informative for similarity (Ф14): `protected` and `open` are
  shared by millions (a collision says nothing), `disposable` shared is a
  fleet marker, and a domain absent from the table is rare (own / ISP /
  corporate) — a cluster axis on its own.
- `reliability` ∈ [0, 1] — the RELATIVE COST OF ONE MAILBOX on that
  provider: 1.0 = phone/ML-gated major provider, ~0.3 = captcha only,
  0.0 = disposable. Not trust: a price prior (PVA-account markets exist,
  but at $0.2–1 a mailbox that is orders of magnitude above a proof of
  work). Consumers: the standing seed of a newborn (Ф15); anything later
  that wants "how expensive was this identity to mint". Values are
  conservative estimates by signup gate until measured — the meaning is
  fixed so that tuning later is measurement, not taste.

Why here and not in the certificate: policy per DOMAIN, changed by a
release and applied retroactively to every certificate already issued; the
Worker stays a notary (it still writes the coarse `email_class` at
issuance — that is where fresh disposable births are caught, since the
Worker sees the domain). Cost stated once: publishing these tokens
de-anonymises exactly the listed domains — the ones with millions of
users. Rare domains stay behind the pepper.

Regenerating tokens needs the pepper (`data/authority/email_pepper.key`,
maintainer only, never rotated by design):

    python -m desktop.p2p.email_domains --regen        # fills/updates every token in place
    python -m desktop.p2p.email_domains --check        # verifies them (release ritual)

Edit domains and values by hand; leave the token field to --regen (an
empty token means "not generated yet" and the tests refuse it).
"""

import hashlib
import hmac
import re
from typing import Optional

TIERS = ("protected", "open", "disposable")

# domain: (tier, reliability, HMAC(EMAIL_PEPPER, "email-domain:" + domain))
EMAIL_DOMAINS = {
    # phone / ML-gated major providers
    "gmail.com": ("protected", 1.0, "81f7d761fde0e493b38e8a83d0dd8c4eceb2e2eb812138b6b2853e87c670869a"),
    "icloud.com": ("protected", 0.9, "4d2cd8d95497cdc54605a66717d6f3ab5e7c0fc2a239b2d6db11267c0a70d421"),
    "me.com": ("protected", 0.9, "ebc6e3d6997780c7bae5f3a61b81f4720334ccc489337618f45224d2f7efd495"),
    "mac.com": ("protected", 0.9, "700c80a08a77f3eb984691c35fa8fbfc5af22f43edf8f3dfbfd13b186f9f212f"),
    "yahoo.com": ("protected", 0.8, "5b90b67fa00c298b825ac286deda3ce8e3c9d6eca9b4abff4790c6f426cab5b0"),
    "ymail.com": ("protected", 0.8, "bb0888f90d21bbf137a93d1c085589af869b584ef1f2f0e89a7b252792ae9431"),
    "aol.com": ("protected", 0.7, "1e7f1a8e984b435bb9d9e046139a5a69aa99a58c75627175e0f01fa426f19f18"),
    "outlook.com": ("protected", 0.7, "e606b49a5add7539b47a5b9cd5d2b43809c5529619bafc7e26a3840afe8927e2"),
    "hotmail.com": ("protected", 0.7, "fb8f9ab17a41bc0c65fbe5e9ea8bc30eb63e65b0c9c35d7a98927c1cc60538ff"),
    "live.com": ("protected", 0.7, "1bbbfd5cf8afb6a0a686fb7cd0ef4d1eab019fb41a1ea7ad33b2455566d65f5e"),
    "msn.com": ("protected", 0.7, "920a68016d139a32b7cf418329f16b5fcb5865188b32a50d8ce9627a4f9a21e2"),
    "yandex.com": ("protected", 0.8, "d7b3bf051e7d83c7795d3ff1503bfd1876eae4632ee0460f956403e3dd85bf2b"),
    "yandex.ru": ("protected", 0.8, "ea53a89bf15e0006d0466d84807ec31c4a9747607259af1c0aa25b5164e64c67"),
    "mail.ru": ("protected", 0.7, "0f61834d535d9409b8b7e502d8cffbec83d06cfb28b2e54d24fa241079695e92"),
    "qq.com": ("protected", 0.8, "7fa421506935b8c8e977f0f4ca7d3f292db25f4032f22f87212ce8988abdd0b8"),
    "163.com": ("protected", 0.8, "db656449b65a4a7c5ea2e41db16b249e09e116188e9837d6fe1f2330fb56ff87"),
    "126.com": ("protected", 0.8, "5258f002d31db92cbf06766bcc8e22cbd0dd91ed3181c02eaa95e1f73e2c1b7f"),
    "naver.com": ("protected", 0.8, "4f2deb53cd1a47d562215c0894bca0d24a267e13c857a7de9825369bd6e2d670"),
    # ISP-tied: a mailbox comes with a subscription
    "orange.fr": ("protected", 0.9, "64b0c06d7c3a74b6fcb2bdb7e3233635e4215e20e50701cda68d3f90b52e6875"),
    "free.fr": ("protected", 0.9, "17bdb45731e0ac769a071f1b1e76896e326c1b505d7b2281451be444c51edad8"),
    "t-online.de": ("protected", 0.9, "d3cd7d5091bbafcaceee69f3b4691011d308d7fddbbfd4ffe32fa5508fbbe260"),
    # captcha-only or lightly gated free providers
    "proton.me": ("open", 0.4, "36d87d2fbe5813946a55d033c37e910c96ab3d0b275311f060e37df576b8b253"),
    "protonmail.com": ("open", 0.4, "b5d7f029d5319355371f35cfe927898f1919c9aab8254eb9731fafb7163a565c"),
    "pm.me": ("open", 0.4, "ca39de56047903831bc16bccbc5953204dca28a229a798a24314b8b1a3988da3"),
    "fastmail.com": ("open", 0.6, "83699ecc253f5f0a90f24d5a8cf10b3228148f937646527c59bbd1065b2f7c80"),
    "zoho.com": ("open", 0.6, "1646395e09fe916226c307a139d041abe084daeb4aee8d72e9cecd64b10828e4"),
    "gmx.com": ("open", 0.3, "c9d6175e51b0e5768792f0b4865cf6897e09a58b5a9fda0ff1d238c901ab20b0"),
    "gmx.de": ("open", 0.3, "aacf61f79288b2248e719be43e14a18405f3067fb01e61bdb26e062b12a45b7d"),
    "gmx.net": ("open", 0.3, "e65a631242c68690ea175e2d40117f0126509dc985fe65278b725412f6e684c5"),
    "web.de": ("open", 0.3, "90e4c0dd49c43a5a8add43e9623517dec020243dbaf2d661164f2f70d280ad50"),
    "mail.com": ("open", 0.3, "ca396a7b7d276227849f939cab5ddf9b57c279e1ccc79c8869c15c4ce4d10d1c"),
    "ukr.net": ("open", 0.5, "b99ffeea938b6a122505e7e36bdf47420b9c014d1978291cb69e8d411a41a48c"),
    "i.ua": ("open", 0.3, "7d0da0d5eaffbb1e55365961ee1167da6a57c9538606567f321b46f81e1ad5a7"),
    "meta.ua": ("open", 0.3, "70ad3260f02f0381aa678a48dcf25027a26616f869eb8dea120876d8a04c1143"),
    "email.ua": ("open", 0.3, "d2a99602beefdc40531bbd3a8f3bd15bfe6c14f67d87fd4b0302b383381bdae8"),
    "seznam.cz": ("open", 0.4, "2a6eb2d3296b0f96e5967bd9ced72925c8c2a11bbb8b5cd44975a543e5918c5e"),
    "wp.pl": ("open", 0.4, "2f19a2b63fc53c9f37203e660033f52922d1c446630a54ccf43c56672f92fbee"),
    "o2.pl": ("open", 0.4, "356df8689e4b19651000e1c0c1128f56a976b8c66fe056ed750f83f4cf434e4e"),
    "onet.pl": ("open", 0.4, "b625ac1d1497c34552f7c31e73cae4af1417fd9061b9399d3216e49dd14dd030"),
    "interia.pl": ("open", 0.4, "f0dd67df7cc6de2640f7312c3f400a374a67372d69ba791f4c8f955a85980138"),
    "laposte.net": ("open", 0.4, "ed7559079790111c3f8b02fcd8bd4cf812c46841754d0e096a92381b979c5e0c"),
    "libero.it": ("open", 0.5, "0adfc84229c38bd88415bde94013343986b1149b43e84e1a042c3ea6f7f4ccb6"),
    # disposable (mirrors the Worker's issuance-time list)
    "mailinator.com": ("disposable", 0.0, "f4ce45ece4397e33311df4fee0b8fdcc68b3c885e46c3d14a7dd87988ef58a57"),
    "guerrillamail.com": ("disposable", 0.0, "48488bb15e6e0d016eb24aaa76827e6f2bf0e3b06ee824e744a3295a3f9eb372"),
    "guerrillamail.net": ("disposable", 0.0, "b78315229043e3c7c7c76cb0ea231b7554abd5afa5423819e181606f6cc2644f"),
    "sharklasers.com": ("disposable", 0.0, "c2137edc6865933a679fc186e0a44f48739f113c5bd581361e58e6358f271200"),
    "grr.la": ("disposable", 0.0, "3c6485759fc6b52299fa7b1b47af63b5137d62fece7f6b3a07d9b64f1e17e62d"),
    "10minutemail.com": ("disposable", 0.0, "082f16eaa53abb62eb0cab3d4474c7aa818aa81b21dedd2ec9e112e8cfda18cc"),
    "10minutemail.net": ("disposable", 0.0, "890aa3e9cf42f65f0714c76714815bd88ce89d88d29826b21902ad6d656b60ae"),
    "temp-mail.org": ("disposable", 0.0, "be93e7ee92bb724a0d11ff157282d0b1a6232548fd15be5f4ccf63b7d3e9aec8"),
    "tempmail.com": ("disposable", 0.0, "62e3a137ace99703394a700655bef8aa6adf37ab9ddff075ae61d63c233140e4"),
    "tempmail.net": ("disposable", 0.0, "151495b3a0bb61ddbd23238d651cebb91175d055c32bc07b5600e160b860d7d3"),
    "tempr.email": ("disposable", 0.0, "e5aabf04c081d40e8d9933a02afd70c846011d032b409ebbb8bf08e200dd8a2b"),
    "yopmail.com": ("disposable", 0.0, "b8030898638b4c5541834f4ae89809ac72c8b7b81cd40207872ca91cb645c296"),
    "yopmail.fr": ("disposable", 0.0, "19f7599b1736cb958024071e26f5319f2d8c20cd3458708a3b517628462dd8ee"),
    "throwawaymail.com": ("disposable", 0.0, "3f5dc5ab2a690ca0f4e6412b50f60c6ed9ab6218b9eff84e6d32a0851f246b69"),
    "getnada.com": ("disposable", 0.0, "002d82e5f55deff45f4da113d3dc363adf5d605496b3394370ed5acb405d6e44"),
    "nada.email": ("disposable", 0.0, "38031333beb8267bc3590036dca72c924361c84ff2d9b6f1d9f6acf67cda3e22"),
    "dispostable.com": ("disposable", 0.0, "c3219794adc4f1be6401dbea83270001723a12a9281198935054b694806e7148"),
    "trashmail.com": ("disposable", 0.0, "3edb67751c9ec14a91c5a77cac7b5004ea152cb6c7db8d86aab6b0b063a915b8"),
    "trashmail.me": ("disposable", 0.0, "62ea1c9d5afb4643702b2338beced30d38389e4e6d5b1ecb90499e31ab08affb"),
    "maildrop.cc": ("disposable", 0.0, "1a6bceda5657ac9079b064d5e1dd9b74679bcc2c48df2c7378896977e7e7a1f8"),
    "fakeinbox.com": ("disposable", 0.0, "ff0af1934d8c7708022bc24cd33a8a5b8606ec3fd82883a53197103c16ef2efd"),
    "mohmal.com": ("disposable", 0.0, "eb03af233dc41c08d31e5c64b571bc1e4c640b281c943f21e5d4193cbe787cce"),
    "emailondeck.com": ("disposable", 0.0, "2749cb82011e84696d9161bc385ead4e79062c9d5f05dbe71c605eb83165d8ce"),
    "mintemail.com": ("disposable", 0.0, "50a012fdadca7a806ac56aaad10d50ac8e0c9e8c7b7b8731ad685dcd72436aa4"),
    "discard.email": ("disposable", 0.0, "48d2daf20e123bdd16163c9880a718b24d38469fc1ef8bc51a993ee0722b4881"),
    "mailnesia.com": ("disposable", 0.0, "b3bb7f1ff55d156db29133b9a8fee0b25e1b254bf28933b4dd37109dbfa217da"),
    "spamgourmet.com": ("disposable", 0.0, "282807e6d3eae5d3e3d58578911f991ffc623554fb4eb23b6faa6958c5db96d6"),
    "mytemp.email": ("disposable", 0.0, "b8b4a7885246188a42cc1f8d2661dc5b7d4592a49f012782e1207622d479af19"),
    "tmpmail.org": ("disposable", 0.0, "3d200948f1fce4f10033a9353045b34513be72a6f1ccf7ad0be7ce6995f400fd"),
    "tmpmail.net": ("disposable", 0.0, "840e32725dd61315737e91d12ea319b4bbe4225d8752c9346bf9c8c2b9ec2e3d"),
    "burnermail.io": ("disposable", 0.0, "112c1a0a63cc6542cc51458979a8bad647f7268bb6f681cc2d0426b450220353"),
    "moakt.com": ("disposable", 0.0, "9f2f760e2e06167f6cdd6b2b2af7c874314a38bfcad1fb70cb9a684c94ef90f7"),
    "tempail.com": ("disposable", 0.0, "e760dae2e3943032cabbb74d26e11b17129121e84ed9e64b45692590f3d897fd"),
    "crazymailing.com": ("disposable", 0.0, "c1274639e225c48748ccdbe5372816ed24781ce68889ba5ef1a2cb2432d0a69c"),
    "guerrillamailblock.com": ("disposable", 0.0, "ee4e5918c72ae4164f887d7e7818909fb1a37522a47c8c23f0f4868555ddc69a"),
    "spam4.me": ("disposable", 0.0, "84d50c277c7903cb628c89ee54a0c82bc3edeb12d401379c9a25ec3cf5575632"),
    "mailcatch.com": ("disposable", 0.0, "06b108fb173e4e5c845c93d2d406791707cf1e3e5f572829cdf1b670d7292437"),
    "inboxkitten.com": ("disposable", 0.0, "a4436ae2b90018ae2cb2d233c561a2dfb71b05db5b9c658fa2baf2f7871e6f02"),
}

_BY_TOKEN = {token: (tier, reliability) for tier, reliability, token in EMAIL_DOMAINS.values() if token}


def tier_of(domain_token: Optional[str]) -> Optional[str]:
    """protected | open | disposable, or None for a domain this table has no
    opinion about (own / ISP / corporate / unknown)."""
    if not domain_token:
        return None
    hit = _BY_TOKEN.get(domain_token.lower())
    return hit[0] if hit else None


def reliability(domain_token: Optional[str]) -> Optional[float]:
    """Relative cost of one mailbox on the domain, or None when unknown —
    the consumer's policy decides what an unknown domain is worth."""
    if not domain_token:
        return None
    hit = _BY_TOKEN.get(domain_token.lower())
    return hit[1] if hit else None


def informative(domain_token: Optional[str]) -> bool:
    """Whether two identities SHARING this domain says anything about them
    (similarity axis): a populous provider — no; disposable — yes (a fleet
    on one throwaway service); unknown — yes (a rare domain)."""
    return tier_of(domain_token) not in ("protected", "open")


def compute_token(pepper: str, domain: str) -> str:
    """Mirror of emailDomainToken() in worker/verify.js."""
    return hmac.new(pepper.encode("utf-8"), f"email-domain:{domain.lower()}".encode("utf-8"),
                    hashlib.sha256).hexdigest()


# ----------------------------------------------------------------------------
# --regen / --check (maintainer)
# ----------------------------------------------------------------------------

_LINE_RE = re.compile(r'^(?P<head>\s+"(?P<domain>[^"]+)":\s*\("(?P<tier>\w+)",\s*(?P<rel>[0-9.]+),\s*")'
                      r'(?P<token>[0-9a-f]*)(?P<tail>"\),.*)$')


def _read_pepper(path: str) -> str:
    lines = [ln.strip() for ln in open(path, encoding="utf-8") if ln.strip() and not ln.lstrip().startswith("#")]
    if not lines:
        raise SystemExit(f"no pepper in {path}")
    return lines[-1]


def _rewrite(pepper: str, check_only: bool) -> int:
    from pathlib import Path
    path = Path(__file__)
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    changed = 0
    for i, ln in enumerate(lines):
        m = _LINE_RE.match(ln.rstrip("\n"))
        if not m:
            continue
        want = compute_token(pepper, m.group("domain"))
        if m.group("token") != want:
            changed += 1
            if not check_only:
                lines[i] = f"{m.group('head')}{want}{m.group('tail')}\n"
    if not check_only and changed:
        path.write_text("".join(lines), encoding="utf-8")
    return changed


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Mailbox-domain policy table (tokens under EMAIL_PEPPER)")
    ap.add_argument("--regen", action="store_true", help="fill/update every token in place")
    ap.add_argument("--check", action="store_true", help="verify every token; exit 1 on drift")
    ap.add_argument("--pepper-file", default="data/authority/email_pepper.key")
    args = ap.parse_args()
    if not (args.regen or args.check):
        ap.print_help()
        raise SystemExit(0)
    pepper = _read_pepper(args.pepper_file)
    n = _rewrite(pepper, check_only=args.check)
    if args.check:
        print(f"{n} token(s) out of date" if n else "all tokens current")
        raise SystemExit(1 if n else 0)
    print(f"{n} token(s) written; {len(EMAIL_DOMAINS)} domains")
