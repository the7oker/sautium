# Security

Sautium is a single-user home appliance: one node, one account, reachable on
the owner's LAN and — for the peer network only — on the internet. This page
states the model, what it defends against, and what it deliberately does not.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting on this repository (Security →
Report a vulnerability). Please do not open a public issue for a security
problem; the maintainer answers reports there.

## Surfaces

| Surface | Bind | Authentication |
|---|---|---|
| Web UI and API (backend, 8800) | all interfaces, HTTPS only, self-signed certificate | HMAC-SHA256 request signing with a per-browser device token, earned once with the account password or a pairing PIN shown on the host; 60 s replay window |
| Media proxy and DLNA eventing (8830/8831; launcher 8832/8833) | LAN, plain HTTP | unguessable per-queue tokens — exist because HQPlayer and DLNA renderers can neither sign requests nor trust the self-signed certificate |
| Peer surface (launcher: a random port in 20000–29999, UPnP-mapped; Docker: 8801) | internet, TLS pinned to the node's Ed25519 key | per-IP limits; sync pulls are open by design; chat, relay and diagnostics need an invite-code↔pubkey binding plus timestamp-bound Ed25519 signatures |
| PostgreSQL (5432) | loopback only | — |

Design rules: the device token lives in `localStorage`, which is per-origin,
so a foreign page cannot forge a signature (CSRF); a client-supplied `Host`
is never resolved and only the node's own addresses pass the unsigned
whitelist (DNS rebinding); the certificate's SAN carries private addresses
only; the peer surface writes to P2P tables only and never reveals
configuration or secrets; a source address is a signal, never an
authentication input.

## Threat model

Defended: internet scanners (nothing but the peer surface is forwarded);
hostile devices on the LAN (they get the page and must pass the password or
PIN); DNS rebinding; peer impersonation (TLS pinned to the node key); data
poisoning over the peer network (author signatures, content-addressed
analysis, recomputation as the detector — `docs/design/P2P-SYNC-INTEGRITY.md`).

Accepted, out of scope: a targeted attacker on the LAN who holds the account
password; a malicious process or browser extension on the host (any process
running as the user can read the signing secret); a compromised build of the
client. Exposure beyond the LAN — Tailscale, reverse proxies, a "headless"
mode — is outside this model: it would need real per-user credentials and a
CA-signed certificate.

## Secrets

The node's Ed25519 identity and the API signing secret live in the identity
directory with owner-only permissions. The repository ships no credentials
except the Last.fm desktop-application keys, which are semi-public by design;
`.gitleaks.toml` allowlists exactly those.
