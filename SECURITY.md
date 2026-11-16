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
| Web UI and API (backend, 8800; launcher 18000) | all IPv4 interfaces, plain HTTP | HMAC-SHA256 request signing with a per-browser device token, earned once with the account password or a pairing code the launcher shows as a QR code (a Docker node has no surface for one: its doors are the password and the first-run account), through an exchange boxed end to end (NaCl box to a per-exchange key the node's identity signs; the browser pins that identity on first sign-in); 60 s replay window |
| Media proxy and DLNA eventing (8830/8831; launcher 8832/8833) | all IPv4 interfaces, plain HTTP; for the LAN (the owner's own tailnet counts as his LAN), never forwarded | unguessable tokens — an owned file's is an HMAC of its path under the node secret, served only while a queue registers the file; a stream buffer's and a cover's are random and die with the queue — exist because HQPlayer, on this machine or on another one on the LAN, and DLNA renderers cannot sign requests |
| Peer surface (launcher: a random port in 20000–29999, UPnP-mapped; Docker: 8801) | internet, TLS pinned to the node's Ed25519 key | per-IP limits; sync pulls are open by design; chat, relay and diagnostics need an invite-code↔pubkey binding plus timestamp-bound Ed25519 signatures |
| Peer discovery (DHT `19001/udp`, launcher: peer port + 1; LAN discovery `19002/udp`, launcher only) | all interfaces | none — public DHT announces and a LAN JSON broadcast carry no secrets; everything that follows goes through the peer surface |
| PostgreSQL (5432; launcher 15432) | loopback only | — |
| Host agent (a Docker node on Linux, `scripts/host_agent.py`) | no port | the node asks by the name of an empty file — `check`, `update`, `diagnostics` — in the one folder of the agent's it can write; the agent reads no content, opens nothing there but by handles, and answers through the Web UI port on loopback, signed with a key of the channel's own that signs nothing else |

A Docker node runs its code read-only (since 2026-11-15): a node that could
write the tree it runs from could stop an update, outlive it, or choose the
next image built from it. What it still writes — its database, its data
folders, its caches, its assistant's own folder — is treated as untrusted
to run: each process runs the yt-dlp build in its cache only after checking
it again against yt-dlp's release key, and its assistant CLI reads no settings or hooks from its own folder.
Its assistants' sign-ins live in its own folders, never in the person's own
`~/.claude` or `~/.codex`, which the node cannot reach (since 2026-11-16).
A node that was fully controlled can still keep its database and data as it
left them, and so make a new release fail its start and come back to the
one before; that is the limit of an update made on the node's own
machine. The Compose files for macOS and WSL publish the Web UI and the
media ports on IPv4 alone — published with no address, Docker answers on
IPv6 as well, which a router passing IPv6 would put on the internet. On a
Windows host with Docker Desktop the LAN reaches them through the host's own
forward and firewall, which hold the IPv4 address themselves, so that file
publishes them with no address. The
Linux file runs the backend on the host's network: the node binds its ports
itself, and the container holds no capability beyond the few its entrypoint
needs to hand its folders over and drop root — none that would let it read
or forge the LAN's traffic. On the host's network the node also reaches
what this machine serves on its own loopback; keep nothing there that trusts
any local process (an unauthenticated Docker API, an admin page).

The table lists what listens. One thing only sends: opening the Output
picker (or Rescan) asks the LAN for players — an SSDP M-SEARCH to UDP 1900
and HQPlayer's `<discover/>` datagram to UDP 4321, unicast to every address
of the node's own subnets and, on a natively run node, to the online peers
of its tailnet (since 2026-09-28). The datagrams carry no identity and no
secret; an HQPlayer's control port (TCP 4321) is then spoken to in the
clear, as its protocol requires, and only on the LAN.

Design rules: the device token lives in `localStorage`, which is per-origin,
so a foreign page cannot forge a signature (CSRF); a client-supplied `Host`
is never resolved and only the node's own addresses pass the unsigned
whitelist (DNS rebinding); no credential crosses the network in the clear —
the token never travels, and the exchange that earns it is boxed; the peer
surface writes to P2P tables only and never reveals configuration or
secrets; a source address is a signal, never an authentication input.

Every page of the Web UI carries a Content-Security-Policy
(`backend/csp.py`) that names the node's own origin alone — for its scripts,
styles, typefaces, images, media and API calls — and allows no frame around
the page; the browser is also told not to look up the hosts of the page's
links before one is followed. Enforced since 2026-11-03, after a
report-only stage from 2026-10-25: a browser refuses a load from elsewhere, or a frame around the
page, and reports it to the node's log.

There is no TLS on the Web UI, by design: a certificate a stock phone trusts
cannot exist for a private address (public CAs may not issue one, and a
name that resolves to one is blocked by many routers), and a self-signed one
only produced the warning. TLS for the Web UI is a front with a real name —
Tailscale Serve, a reverse proxy — terminating in front of the plain-HTTP
port; list that name in `SAUTIUM_ALLOWED_HOSTS`, also for a front that
rewrites `Host`: the policy's reports name the page by it.

## Threat model

Defended: internet scanners (nothing but the peer surface is forwarded);
hostile devices on the LAN (they get the page and must pass the password or
PIN); a listener on the LAN (it sees signatures with a 60 s life, never a
credential or the token); something answering in the node's place (the
browser pins the node's identity on its first sign-in and asks before
accepting another); DNS rebinding; peer impersonation (TLS pinned to the
node key); data poisoning over the peer network (author signatures,
content-addressed analysis, recomputation as the detector —
`docs/design/P2P-SYNC-INTEGRITY.md`); a push to the repository that the
maintainer did not intend — a leaked token, a phished login, a misled agent
holding the push credential. An installed launcher takes only a release tag
signed along the release chain — each release by a key on the list of the
release before it, the first by its own installed list — and only one that
descends from what it runs; a security-key signature also needs the key's PIN
(`docs/RELEASING.md`). A rotation therefore revokes a stolen key for every
launcher, also one still below the rotating release; a tag the stolen key signs
beside it is a fork, which stops those launchers until the tag is deleted. The
installers' `SHA256SUMS.txt` is signed by the same key.

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
`.gitleaks.toml` allowlists those, the master's public token id, the test
vectors and fixtures, and one type annotation — none of them a secret.
