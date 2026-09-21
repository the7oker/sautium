# Audit it yourself

Sautium is source-available: everything a node does is in this tree, and
nothing on the website or in `SECURITY.md` has to be taken on faith. This
page is a ready-to-paste prompt for an AI coding agent — or a checklist for a
person with the same tools — that clones the repository at one commit and
checks what it actually does against what the documentation says it does.

## 1. What this is

A static audit of one commit: every outbound destination, every listening
port, every credential in the tree, and four product-level claims that a
description alone cannot prove — the demo-listen ledger, the sync gate, the
support-diagnostics switch and the provenance of a downloaded installer. The
prompt asks for a fixed report format so two runs, or two agents, can be
compared line by line.

What it is not: a penetration test of a running node, a review of the
models, or a "reproducible build" check. The installers are not
byte-for-byte deterministic (Inno Setup and `hdiutil` embed timestamps, and
each carrier bundles runtimes it downloaded), so the provenance check
compares the **payload stamp** — `VERSION+sha256(tracked files)[:12]`,
written by `desktop/build_common.py` `stage_payload()` — and the payload tree
itself, not the installer bytes.

## 2. What you need

- `git`, and an agent CLI that can run shell commands and read files
  (Claude Code, OpenAI Codex, or any other). About 200 MB for the clone; no
  models, no database, no music.
- Python 3.11+ for the provenance step (staging the payload is plain Python
  over `git ls-files`).
- Optional: `gitleaks` for task C (the agent can fall back to `git grep`);
  `innoextract` to open a published `Setup.exe` without running it; a Mac
  with `hdiutil` for the DMG (7-Zip opens the image elsewhere); Docker only
  if you also want to run the node — the audit does not need it.

## 3. The prompt

Paste the block below into the agent. Replace `<tag-or-commit>` with the
release tag you downloaded (for example `v0.1.0`) or `main`.

````text
You are auditing the source-available project Sautium
(https://github.com/the7oker/sautium), a self-hosted music companion that
runs on the owner's machine. You are a careful, sceptical reviewer. Read the
code; do not trust comments, docstrings or documentation as evidence — quote
the code that proves or disproves each claim. Do not modify the repository,
do not run the application against anyone's music, and make no network
requests other than `git clone` and the downloads named in task G.

Setup
- Clone https://github.com/the7oker/sautium.git, `git checkout <tag-or-commit>`,
  record `git rev-parse HEAD`. If you audit a working tree instead of a fresh
  clone, run `git status --ignored` and leave ignored paths out: a
  bring-your-own provider module with its own secrets may sit there, outside
  the commit.
- Read SECURITY.md, the "Security Posture" section of CLAUDE.md,
  THIRD_PARTY_NOTICES.md and .gitleaks.toml. Treat them as the CLAIMS this
  audit tests, not as findings.

Tasks

A. Outbound destinations. List every host the software can connect to, with
   the file that does it, what triggers the connection (always at start / a
   user action / an opt-in setting / only with a key the user supplies), and
   what data leaves the machine. Start with:
     backend/mb_dump_load.py and backend/lb_dump_load.py (MusicBrainz and
       ListenBrainz dump downloads — opt-in dump nodes),
     backend/lrclib.py (lyrics), backend/lastfm.py, backend/musicbrainz.py,
       backend/caa.py, backend/routers/covers.py,
       backend/deezer_photos.py, backend/streaming/deezer_catalog.py,
       backend/streaming/deezer_preview.py (metadata, artwork, 30 s excerpts),
     backend/streaming/ — the demo channel: the provider whose manifest sets
       demo_limited, and what fetches from it,
     desktop/p2p/email_verify.py, desktop/p2p/birth_cert.py,
       desktop/p2p/mailbox_client.py, desktop/p2p/node_hints.py,
       desktop/p2p/master_hint.py, backend/routers/p2p.py and
       backend/sign_audio.py (the Cloudflare Worker in worker/verify.js:
       email verification codes, invites, birth certificates, the node
       directory, the master hint, the mailbox, notary timestamps — read the
       Worker's handlers too and state what each one stores; only the
       source is auditable, not the deployment),
     backend/seed_export.py RELEASE_URL and backend/seed_import.py (the
       cold-start seed bundle), desktop/updater.py (self-update from
       origin/main),
     desktop/p2p/dht_service.py (DHT bootstrap routers), desktop/p2p/ and
       backend/p2p_app.py (connections to other nodes: sync, relay, chat),
     backend/routers/peer_diag.py, desktop/p2p/diag_events.py,
       desktop/diag_bundle.py (support diagnostics to the master node),
     backend/providers/, backend/claude_code_runner.py,
       backend/codex_runner.py (assistant providers — only with the user's
       sign-in or key),
     desktop/db_init.py, desktop/python_env.py and desktop/service_manager.py
       (components the launcher downloads: PostgreSQL, Python, packages,
       ffmpeg, Node.js), backend/streaming/service.py (the audition
       downloader updates itself from PyPI at backend start),
     backend/gear_registry.py and backend/routers/gear_models.py (the
       loudspeaker measurement registry, on the owner's request),
     backend/static/index.html (requests the BROWSER makes when it opens the
       Web UI page).
   Then run `git grep -nE 'https?://[a-zA-Z0-9.-]+' -- backend desktop worker mcp`
   and account for every host you did not already list.

B. Listening ports and bind addresses. From backend/config.py, backend/main.py,
   backend/p2p_app.py, docker-compose.yml, docker-compose.mac.yml,
   docker-compose.wsl.yml, desktop/config_manager.py,
   desktop/service_manager.py (the launcher's backend bind), desktop/portmap.py
   (UPnP and PCP), desktop/p2p/upnp_service.py, desktop/p2p/p2p_manager.py,
   desktop/p2p/sync_server.py, desktop/p2p/dht_service.py and
   desktop/p2p/lan_discovery.py (the UDP ports): every port, its bind address (all interfaces /
   LAN / loopback), what authenticates a request on it, and whether anything
   can forward it to the internet (UPnP, portmap). Compare with the Surfaces
   table in SECURITY.md and report every difference.

C. Credentials in the tree. Run `gitleaks git -c .gitleaks.toml .` (or
   `gitleaks detect` on older versions; without gitleaks, grep for
   key/token/secret/password literals). Read every allowlist in
   .gitleaks.toml and say what it hides. The maintainer declares exactly one
   set of shipped credentials — the Last.fm desktop-application keys in
   backend/app_keys.py — plus public identity pins (backend/master_node.py,
   desktop/p2p/master_node.py, and the birth-authority key in
   backend/birth_authority.py / desktop/p2p/birth_cert.py). Check that each
   backend↔desktop mirror pair is identical. Anything else is a finding.

D. Demo policy. The claim: a track not owned by the node streams in full from
   the demo channel at most ONCE; after that, and whenever the demo channel
   has nothing, it plays as the catalogue's 30 s excerpt; an excerpt is never
   analysed, never counted as a listen, never scrobbled. Verify in
   backend/streaming/demo.py (CONSUMED_FRACTION, consumed(), link_admissible(),
   the demo_plays ledger and the status observer), backend/routers/player.py
   (the resolve chain), backend/streaming/enrichment.py (PreviewEnricher
   refusing FetchedAudio.excerpt), backend/streaming/deezer_preview.py
   (manifest.excerpt) and backend/playback/ (the play tracker). State
   whether any code path plays a spent track in full from the demo channel,
   and whether any setting disables the ledger.

E. Sync gate. The claim: a record arriving over the peer network is
   inserted only if its author seal verifies; unsigned or mis-signed records
   are dropped; the Last.fm tables never travel. One exception is by
   design: the mean `embeddings` row carries no seal — the signed unit is
   the segment (docs/design/P2P-SYNC-INTEGRITY.md) — so state it rather
   than report it, and confirm nothing else lands unsigned. Verify in
   desktop/sync_client.py (_import_items — the one import gate, imported by
   the Docker backend from the read-only desktop mount), desktop/p2p/record_sig.py
   (verify_seal / verify), backend/routers/sync.py (_load_carry and the
   pull endpoints), desktop/p2p/sync_queries.py, and
   docs/design/P2P-SYNC-INTEGRITY.md for the intended model. Report every
   table the peer surface can write to.

F. Support diagnostics. The claim: the node sends content-free event
   reports to the maintainer's node and answers signed diagnostic warrants
   only from that node; the setting support.diagnostics_enabled (default on)
   switches both off; EVENT REPORTS never contain track names, file paths,
   chat text or credentials — check whether free-text fields (error,
   message) pass through scrub_secrets; BUNDLES answer a warrant only, are
   boxed so that only the master can read them, and by design may carry
   assistant dialogs, the library path and log tails (friends' chat never).
   Also state whether a node that is not the master serves any diag route. Verify in backend/routers/settings.py (_DEFAULTS),
   desktop/p2p/diag_events.py (what an event report contains),
   desktop/diag_bundle.py (what a bundle contains and how it is encrypted),
   backend/routers/peer_diag.py (who may ask), and the switch labelled
   "Share diagnostics with support" in backend/static/app-shell.js. Read
   P2P_NETWORK.md § "Support diagnostics" for the intended model and report
   every difference.

G. Payload provenance of a published installer (skip if you have none).
   1. Integrity: download the installer and SHA256SUMS.txt from
      https://github.com/the7oker/sautium/releases/tag/<tag> and run
      `sha256sum -c SHA256SUMS.txt --ignore-missing` beside them.
   2. Provenance: on the checked-out tag, stage the payload without building
      an installer —
        python3 -c "from pathlib import Path; from desktop.build_common import stage_payload; print(stage_payload(Path('/tmp/sautium-payload')))"
      (on Windows or WSL, `python desktop/build_windows.py --stage-only`
      does the same into build/windows/payload). This prints the stamp and
      writes it to <payload>/.sautium_build.
   3. Open the published artefact without running it: for Setup.exe,
      `innoextract -e Sautium-<ver>-Setup.exe -d out` and read
      out/app/payload/.sautium_build; for the DMG, `hdiutil attach` it and
      read Sautium.app/Contents/Resources/payload/.sautium_build.
   4. The stamps must be equal, and `diff -r` between the extracted payload
      and the staged one must be empty. Any difference means the published
      payload was not built from the tagged tree — report it as a
      discrepancy, quoting the files that differ.

Report — use exactly these headings, in this order, and nothing else:

Commit audited
Outbound destinations        (table: host · file · trigger · data sent)
Listening ports              (table: port · bind · auth · forwardable · matches SECURITY.md?)
Credentials found
Demo policy verdict          (confirmed / refuted, with file:line evidence)
Sync gate verdict            (confirmed / refuted, with file:line evidence)
Support diagnostics verdict  (confirmed / refuted, with file:line evidence)
Payload/provenance           (stamps compared, diff result, or "not checked")
Discrepancies                (one per line: severity · file:line · what the code does · what the docs say)
Not checked                  (what you could not verify and why)

A discrepancy is a difference between the code and SECURITY.md, CLAUDE.md,
README.md or THIRD_PARTY_NOTICES.md, or between the code and one of the
claims stated in tasks D–F. Rate severity as the impact on the owner of the
node, not on the project. Do not pad the report with things that matched;
say "matches" in the table and move on.
````

## 4. Reading the report

The threat model in `SECURITY.md` names what is defended and what is
accepted. These are the things a first run tends to "find" that are known
and by design; the report should list them as matches, not discrepancies:

- The backend (`8800`; the launcher's `18000`) listens on **all interfaces**
  over **plain HTTP**. LAN use from phones is the primary workflow, the
  device token never travels, and the exchange that earns it is boxed to a
  key the node's identity signs. What the API and the music *say* is readable
  by a device on the same network — accepted in `SECURITY.md` § Threat model.
- Sync pulls on the peer surface are **unauthenticated by design** (gated by
  the `sync.p2p_enabled` setting); chat, relay and diagnostics are not.
- The **Last.fm keys ship in source** (`backend/app_keys.py`), as in every
  desktop scrobbler; `.gitleaks.toml` allowlists exactly those and the master
  node's public pins.
- Support diagnostics are **on by default**, with the switch "Share
  diagnostics with support" in Settings; the reports are content-free events,
  and a bundle answers only a warrant the node received from the pinned
  master.
- The Web UI page pulls its two typefaces from **Google Fonts**
  (`backend/static/index.html`) — the one third-party request the browser
  makes on its own; the node itself never talks to Google.
- The peer network bootstraps through the **public BitTorrent DHT routers**
  named in `desktop/p2p/dht_service.py` — the DHT is what makes the network
  serverless; the routers see the node's address and the announce keys.
- The Cloudflare Worker (`worker/verify.js`) receives: an email address only
  when the owner opts into verification (kept in the Worker's KV); the
  Merkle roots the notary timestamps, signed by the node's key (no content);
  a public key and its own signature for a birth certificate; invite mails
  the owner sends; a reachable node's public key, port and capabilities for
  the directory (with the address the edge saw); boxed envelopes for the
  master's mailbox; a peppered hash of the requesting address beside each
  timestamped root.
- The audition downloader (yt-dlp) **updates itself from PyPI** at every
  backend start (`backend/streaming/service.py`) — a supply-chain channel
  the project accepts knowingly, because the tool breaks weekly otherwise.
- A **third public pin** ships beside the master's: the birth authority's
  key (`backend/birth_authority.py`, `desktop/p2p/birth_cert.py`).
- The updater follows `origin/main`; an installed app pulls whatever lands
  there. That is the shipping model (`README.md` § Desktop launcher), and
  the reason the audit is per commit.

Accepted and out of scope, from `SECURITY.md`: an attacker on the LAN who
holds the account password, or who answers in the node's place on a
browser's very first sign-in; a malicious process or browser extension on the
host; a compromised build of the client; exposure beyond the LAN, "headless"
or multi-user deployments. A report that flags these is repeating the
threat model, not contradicting it.

Everything else under **Discrepancies** is either a documentation error or a
real finding. Both are worth sending.

## 5. Reporting a discrepancy

Anything that weakens the model — a port the docs say is loopback-only
binding wider, a credential the tree should not carry, a path that plays a
spent demo track in full, a sync path that inserts an unverified record, a
diagnostic payload with content in it — goes through GitHub's **private
vulnerability reporting** on the repository (Security → Report a
vulnerability), as `SECURITY.md` asks. Paste the report's *Commit audited*
and *Discrepancies* sections; the quoted `file:line` is what makes it
actionable.

A documentation error with no security effect — a stale port number, a
renamed file, a claim the code no longer makes — is an ordinary public issue
at https://github.com/the7oker/sautium/issues.

## 6. Maintenance

Written 2026-09-21 against the tree of that date. The prompt names files and
symbols, never line numbers, so it survives edits inside a file; moving or
renaming a file it names is a change to this page in the same commit (the
documentation rule in `CLAUDE.md`). The claims in tasks D–F restate
`CLAUDE.md` § "Enrichment Pipeline Conventions" (the demo ledger),
`docs/design/P2P-SYNC-INTEGRITY.md` (the sync gate) and `P2P_NETWORK.md`
§ "Support diagnostics"; when one of those changes, the claim here changes
with it. The list in § 4 is what the maintainer expects the report to say
about the tree of 2026-09-21 — re-derive it from a fresh run before each
release rather than editing it by hand.
