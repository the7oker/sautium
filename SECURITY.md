# Security

Sautium is a single-user home appliance: one node, one account, reachable on
the owner's LAN and — for the peer network only — on the internet. This page
states the model, what it defends against, and what it deliberately does not.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting on this repository (Security →
Report a vulnerability). Please do not open a public issue for a security
problem; the maintainer answers reports there.

## Audit it yourself

The tree is source-available, so none of this page has to be taken on
faith. `docs/AUDIT.md` is a ready-to-paste prompt for an AI coding agent (or
a checklist for a person) that clones the repository at one commit and
checks the code against this page: every outbound destination and listening
port, every credential in the tree, the one-full-listen demo ledger, the
sync gate that drops unsigned records, the support-diagnostics switch, and
the provenance of a downloaded installer through its payload stamp. A
discrepancy that weakens the model is a vulnerability report (above); one
that does not is an ordinary issue.

## Surfaces

| Surface | Bind | Authentication |
|---|---|---|
| Web UI and API (backend, 8800) | all interfaces, plain HTTP | HMAC-SHA256 request signing with a per-browser device token, earned once with the account password or a pairing PIN shown on the host, through an exchange boxed end to end (NaCl box to a per-exchange key the node's identity signs; the browser pins that identity on first sign-in); 60 s replay window |
| Media proxy and DLNA eventing (8830/8831; launcher 8832/8833) | LAN, plain HTTP | unguessable per-queue tokens — exist because HQPlayer and DLNA renderers cannot sign requests |
| Peer surface (launcher: a random port in 20000–29999, UPnP-mapped; Docker: 8801) | internet, TLS pinned to the node's Ed25519 key | per-IP limits; sync pulls are open by design; chat, relay and diagnostics need an invite-code↔pubkey binding plus timestamp-bound Ed25519 signatures |
| Peer discovery (DHT `19001/udp`, launcher: peer port + 1; LAN discovery `19002/udp`, launcher only) | all interfaces | none — public DHT announces and a LAN JSON broadcast carry no secrets; everything that follows goes through the peer surface |
| PostgreSQL (5432; launcher 15432) | loopback only | — |

Design rules: the device token lives in `localStorage`, which is per-origin,
so a foreign page cannot forge a signature (CSRF); a client-supplied `Host`
is never resolved and only the node's own addresses pass the unsigned
whitelist (DNS rebinding); no credential crosses the network in the clear —
the token never travels, and the exchange that earns it is boxed; the peer
surface writes to P2P tables only and never reveals configuration or
secrets; a source address is a signal, never an authentication input.

There is no TLS on the Web UI, by design: a certificate a stock phone trusts
cannot exist for a private address (public CAs may not issue one, and a
name that resolves to one is blocked by many routers), and a self-signed one
only produced the warning. TLS for the Web UI is a front with a real name —
Tailscale Serve, a reverse proxy — terminating in front of the plain-HTTP
port; list that name in `SAUTIUM_ALLOWED_HOSTS`.

## Threat model

Defended: internet scanners (nothing but the peer surface is forwarded);
hostile devices on the LAN (they get the page and must pass the password or
PIN); a listener on the LAN (it sees signatures with a 60 s life, never a
credential or the token); something answering in the node's place (the
browser pins the node's identity on its first sign-in and asks before
accepting another); DNS rebinding; peer impersonation (TLS pinned to the
node key); data poisoning over the peer network (author signatures,
content-addressed analysis, recomputation as the detector —
`docs/design/P2P-SYNC-INTEGRITY.md`).

Accepted, out of scope: a targeted attacker on the LAN who holds the account
password, or who is answering in the node's place on a browser's very first
sign-in; what the API and the music say, which a device on the same network
can read off the plain-HTTP traffic; a malicious process or browser
extension on the host (any process running as the user can read the signing
secret); a compromised build of the client. Exposure beyond the LAN — a
"headless" mode, multiple users — is outside this model: it would need real
per-user credentials, not only a TLS front.

## Secrets

The node's Ed25519 identity and the API signing secret live in the identity
directory with owner-only permissions. The repository ships no credentials
except the Last.fm desktop-application keys, which are semi-public by design;
`.gitleaks.toml` allowlists exactly those.
