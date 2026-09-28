# HQPlayer Desktop 5 / 6 and HQPlayer Embedded Integration

## Overview

Integration with the HQPlayer Control API for playback control and status
monitoring. Supports **HQPlayer Desktop 5 and 6** and **HQPlayer Embedded 6** — the
XML-over-TCP control protocol is the same across them (forward-compatible between the
Desktop releases, and Embedded speaks it unchanged: verified 2026-09-27 against
Embedded 6 / engine 6.2.3 on HQPlayer OS, Raspberry Pi 5), and Sautium discovers all
DSP options (filters, shapers, modes, rates) dynamically at runtime, so the same code
drives any of them with no version switch. What differs between an HQPlayer on this
machine and one on another box is how a track reaches it — see "Where HQPlayer runs".

**Status**: ✅ Working - Basic implementation complete

## Architecture

### Protocol
- **Type**: XML over TCP
- **Port**: 4321 (default)
- **Authentication**: Not required for basic commands (optional for advanced features)

### SDK Version
Based on Signalyst's HQPlayer Control SDK (`hqp-control-*-src`, obtained from
Signalyst — not vendored here). Both SDK generations are supported:
- **HQP5**: engine version 5.29.2 (hqp-control 5.29.2)
- **HQP6**: SDK 6.0.1 (hqp-control 6.0.1)

The control protocol is the same on both, so a single client implementation talks to
either desktop version.

### Tested Configuration
- **HQPlayer**: Desktop 6 — the daily configuration as of 2026-09-21;
  Desktop 5.16.3 (Engine 5.34.14) also tested, same client
- **Platform**: Windows
- **Connection**: WSL2 → Windows (<windows-host-ip>:4321)
- **HQPlayer Embedded 6** (engine 6.2.3, HQPlayer OS image on a Raspberry Pi 5,
  reached by LAN address from the Docker node): control protocol, status and the
  DSP lists verified 2026-09-27; streaming passed live the same day (album
  playback, gapless advance, seek, transport, tracking; a backend restart
  mid-album and an HQPlayer power cycle both recovered). A mount mode — the
  box reading a Windows share of the library through HQPlayer OS's
  NetworkMounts — passed too and was retired 2026-09-28: the same bytes for
  an extra setup on the HQPlayer side

### HQP6-only additions
HQPlayer 6 exposes two extra fields that Sautium now uses when present. Both degrade
gracefully to absent on HQP5 — the integration reads them opportunistically and works
unchanged without them.

- **Per-filter `description`** — a human-readable blurb returned alongside each filter in
  the discovery response. Sautium surfaces it live in the HQPlayer settings screen and
  passes it to the AI assistant, so filter choices are explained in the player's own words.
- **`process_speed` in Status** — a DSP load readout reported by `GetStatus`.

## Files

### Core Implementation
- `backend/hqplayer_client.py` - Main client library
  - `HQPlayerClient` class - Core API client
  - `HQPlayerConnection` context manager
  - Helper functions for URI conversion and time formatting

### Testing
- the assistant tools (`hqplayer_get_status`, `hqplayer_get_settings`, …) against a running HQPlayer; `/health` reports the connection state

### SDK Reference
- HQPlayer Control SDK (`hqp-control-*-src`, C++) — Signalyst's reference code,
  available from https://www.signalyst.com/; it is not linked into Sautium

## Features Implemented

### ✅ Playback Control
- `play()` - Start playback
- `pause()` - Pause playback
- `stop()` - Stop playback
- `next()` - Next track
- `previous()` - Previous track
- `forward()` - Fast forward
- `backward()` - Rewind
- `seek(position)` - Seek to position in seconds
- `select_track(index)` - Select track by playlist index

### ✅ Volume Control
- `set_volume(value)` - Set volume level
- `volume_up()` - Increase volume
- `volume_down()` - Decrease volume
- `volume_mute()` - Toggle mute

### ✅ Playlist Management
- `playlist_add(uri, clear, queued)` - Add track to playlist
- `playlist_clear()` - Clear playlist
- `playlist_remove(index)` - Remove track by index

### ✅ Status & Information
- `get_status()` - Get current playback status
  - State (stopped/playing/paused)
  - Track index and ID
  - Position and length
  - Volume level
  - Metadata (artist, album, song, genre)
  - `process_speed` - DSP load readout (HQP6 only; absent on HQP5)
- `get_info()` - Get HQPlayer info
  - Product name
  - Version
  - Platform
  - Engine version

### ✅ Settings
- `set_repeat(mode)` - Set repeat mode (NONE/SINGLE/ALL)
- `set_random(enabled)` - Enable/disable shuffle

### ✅ DSP Settings & Control
- `get_modes()` / `set_mode(index)` - Output mode (PCM/DSD)
  - Example modes: [source], PCM, SDM (DSD)
- `get_filters()` / `set_filter(index, index_1x)` - Audio filters (PCM and SDM)
  - 77+ filters available: IIR, FIR, poly-sinc, closed-form, etc.
  - Separate 1x filter for PCM
- `get_shapers()` / `set_shaping(index)` - Noise shaping/dither
  - 36+ shapers: DSD5, ASDM5, ASDM7, etc.
- `get_rates()` / `set_rate(index)` - Output sample rate
  - 20 rates: 2.048 MHz to 98.304 MHz (DSD)
- `get_inputs()` - Available input devices

## Usage

### Basic Connection

```python
from hqplayer_client import HQPlayerConnection

# Context manager (recommended)
with HQPlayerConnection(host="<windows-host-ip>") as hqp:
    # Get info
    info = hqp.get_info()
    print(f"Connected to {info['product']} {info['version']}")

    # Get status
    status = hqp.get_status()
    if status.is_playing:
        print(f"Now playing: {status.artist} - {status.song}")
```

### Manual Connection

```python
from hqplayer_client import HQPlayerClient

hqp = HQPlayerClient(host="<windows-host-ip>", port=4321)
if hqp.connect():
    status = hqp.get_status()
    hqp.disconnect()
```

### Playing a Track

```python
from hqplayer_client import HQPlayerConnection, file_path_to_uri

track_path = "E:\\Music\\Artist\\Album\\Track.flac"
uri = file_path_to_uri(track_path)

with HQPlayerConnection(host="<windows-host-ip>") as hqp:
    # Clear playlist and add track
    hqp.playlist_add(uri, clear=True)

    # Start playback
    hqp.play()

    # Monitor status
    status = hqp.get_status()
    print(f"Playing: {status.song}")
    print(f"Position: {status.position:.1f}s / {status.length:.1f}s")
```

### Integration with Sautium

Application code does not talk to this client directly. HQPlayer is one
`PlayerBackend` (`backend/playback/hqp_backend.py`) behind the playback
manager: the canonical queue holds track UUIDs, the HQP backend mirrors
that queue into HQPlayer's playlist, and the path comes from
`media_files.file_path` — `tracks.id` identifies the music, the media file
identifies the bytes (CLAUDE.md § Data Model Conventions). A phantom track
has no file and resolves to a stream through the media proxy instead.

```python
from hqplayer_client import HQPlayerConnection, file_path_to_uri

# file_path comes from media_files, resolved for the track being played
with HQPlayerConnection(host="<windows-host-ip>") as hqp:
    hqp.playlist_add(file_path_to_uri(file_path), clear=True)
    hqp.play()
```

### Where HQPlayer runs: file access (read off the address, since 2026-09-28)

How an owned track is handed to HQPlayer is not a setting — it follows
from the endpoint's address (`playback.hqp_backend._stream_mode`,
`auth_hmac.is_own_address`): an HQPlayer on THIS machine — `localhost`,
`host.docker.internal` (the Docker host), one of the node's own addresses
or names — shares its disks and gets `file:///E:/Music/…`, the stored path
itself; an HQPlayer anywhere else — a Desktop on another computer, an
Embedded box on the LAN — gets `http://<our LAN address>:8830/file/{token}`
on the media proxy, the same URLs a DLNA renderer gets. A mount mode (the
same path under the root HQPlayer mounted the library at) existed for one
day, 2026-09-27: the bytes are the same either way and the share is an
extra setup on the HQPlayer side, so it went with its setting
(`hqplayer.file_access` / `library_root`, migration 031). It returns only
if Sautium ever scans files over the network itself.

CUE slices (a cached FLAC cut), m4a (transcoded to FLAC in memory — HQPlayer
decodes neither AAC nor ALAC) and phantom previews are http URLs either
way. The address inside a media URL is chosen for the machine HQPlayer runs
on (`streaming/media_host.py`, shared with the DLNA output): a name for this
machine keeps `MEDIA_PROXY_ADVERTISED_HOST`, any other host is resolved and
handed the local address on its network. Owned-file tokens are an HMAC of the
path under the node secret, so they are the same after a backend restart and
the playlist a remote HQPlayer still holds keeps resolving.

The playlist canary (`_check_drift`) and adopt-on-attach read HQPlayer's
playlist back through the same rules — a `file://` URI through the root
remap and HQPlayer's percent-escapes, a `/file/{token}` URL through the
proxy's registry — so both work in every mode. While the playlist differs
from the queue (edited in HQPlayer's own GUI, another source playing), the
status is reported as external playback (slot 0) and nothing is tracked
against the wrong track.

**A restarted HQPlayer re-mirrors on the next play.** The status poller
notices HQPlayer coming back — a dropped socket answered on the immediate
retry, or the first answer after failed polls (a power cycle, a restart that
takes longer than one poll; that second shape was missed until a live Pi
reboot on 2026-09-27) — reads the playlist at once and, when it differs from
the queue, marks the mirror lost; the play-intent gate then re-attaches the
backend, which mirrors the queue afresh. An HQPlayer Embedded in trial mode
stops every 30 minutes and must be restarted — its control port stays open
but closes every connection, which is why the play-intent probe asks
`GetInfo` instead of trusting a TCP connect. Auto-resume after such a
restart is deliberately not done.

### The HQPlayer library as a catalog source (researched 2026-09-27)

HQPlayer keeps its own library (Desktop: File → Library; Embedded: the web
interface's Library tab, which is also the only place a scan is started).
`<LibraryGet picture="0"/>` returns the whole of it in one line of XML — for a
39 618-file library 7.4 MB in 1.2 s — declared UTF-8 but carrying raw bytes
from tags, so it must be decoded leniently. Schema: `LibraryDirectory` (one per
album folder: `hash`, `path`, `album`, `artist` = album artist, `date`, `genre`,
`composer`, `performer`, `rate`, `bits`, `bitrate`, `channels`, `has_cover`,
`has_booklet`) → `LibraryFile` (`hash`, `name` = file name, `song`, `number` =
track number — absent for ~11 % of files, `length` in seconds, and `artist` /
`date` / `genre` / `composer` / `performer` only where they differ from the
folder's). A track's URI is `file://{directory.path}/{file.name}`, which is how
HQPDcontrol, HQPlayer Client and HQPWV queue library tracks. `LibraryGetHash`
answers "has the library changed" cheaply. `LibraryGet picture="1"` embeds no
pictures; `PlaylistGet picture="1"` embeds a base64 JPEG per playlist item.
HQPlayer indexes neither m4a/ape nor CUE sheets (a CUE image is one file).
Sautium does not build its catalogue from this yet; the notes are the basis
for a library-on-HQPlayer node (a disk attached to the HQPlayer box).

## Network Configuration

### From WSL2
```python
# Use Windows host IP (usually 172.x.x.1)
HOST = "<windows-host-ip>"  # Check with: ip route show | grep default
```

### From Docker Container
```python
# Option 1: Use host.docker.internal (Docker Desktop)
HOST = "host.docker.internal"

# Option 2: Use Windows host IP
HOST = "<windows-host-ip>"
```

### Finding HQPlayers (2026-09-29)
HQPlayer has a discovery protocol of its own: a `<discover/>` datagram to
its control port — UDP 4321 — is answered by Desktop and Embedded alike
with `<discover name="VH11" result="NA" version="Signalyst HQPlayer
Desktop 6"/>` (what HQPlayer Client broadcasts for; verified 2026-09-28
against both). Sautium sends it unicast, so it crosses the docker bridge
like the DLNA sweep's M-SEARCH, and the reply comes back from 4321, which
the bridge's port-restricted NAT lets through (`hqp_library.discover`).
The Output picker's scan (`POST /api/player/outputs/scan`, on opening the
picker and on Rescan) asks every LAN address for a renderer and for an
HQPlayer in the same breath (`routers/player._unicast_sweep`), and lists
each HQPlayer it heard as its own entry beside the renderers, shaped like
every other row — "HQPlayer Desktop · VH11 / Online · this computer",
"HQPlayer Embedded / Online · 192.168.1.253" (`hqp_library.label`: the
product and the name that tells two apart, a box's generic self-name
dropped; the state dot is the box's, the check is the selection; the order
never follows the selection). The dot's word is discovery AND control: an
Embedded past its trial half-hour still answers the datagram while its
control port closes at once, and reads "Not answering" whether or not it
is the selected output (the scan's `control` verdict, the selected row's
live check).
HQPlayer OS is a UPnP renderer as well (manufacturer Signalyst): a box that
answered as an HQPlayer is offered once, as the HQPlayer — its renderer
would be the same DSP with no queue and no filter control. Tapping an entry
makes it THE HQPlayer this node drives (`PUT /api/settings/output` with
`hqplayer: {host, port}`; the address is the selection, as a renderer's
record is for DLNA), and the way files reach it follows from that address.
There is no connection editor any more: what the scan cannot see — another
subnet, a box that was off — goes in through **Add device by address**
(`POST /api/player/outputs/add`), shared with DLNA: a bare address is
probed as an HQPlayer first (the datagram, then `<GetInfo/>` over TCP where
a firewall passes only that) and as a renderer second; a full
device-description URL is a renderer by definition.

Every HQPlayer the owner chose or added is an `hqp_endpoints` row
(`hqp_library.register`), a Desktop included — the row is the device's
identity; a LIBRARY is only what an Embedded box has, and the sync refuses
a Desktop without touching its row. This machine's aliases — `localhost`,
`host.docker.internal`, its LAN address — are one HQPlayer
(`hqp_library.address_key`): every lookup by address finds the row another
alias registered, and the row keeps the address it was registered at (on a
Docker node `host.docker.internal` for a Desktop the scan sees at the LAN
address). A row is listed while its box is off, like a pinned renderer;
its `×` in the picker forgets it — the same confirmed goodbye Settings ›
Library offers when the row holds copies. The HQPlayer screen (the gear
on the selected row) carries a Library section for an HQPlayer on another
machine — what this node holds of its library and a tap that syncs it (the
same previewed first import, the progress on the Library screen). The
mini-player and the status name the selected one by the same label
(`HQPlayer Desktop · VH11`).

### HQPlayer Embedded on the LAN
Open More → Audio output: the scan lists the box, or **Add device by
address** takes its LAN address (a Docker node cannot resolve `.local`
names; a DHCP reservation keeps the address stable) — that is all: a box is
not this machine, so it streams. The box fetches from the media proxy — on a Windows host
with Docker Desktop that is the same port forward and firewall allow the
DLNA output needs (CLAUDE.md Security Posture, rule 3); a launcher node
serves it natively on 8832. The web interface (port 8088, default login
`hqplayer` / `password`) is where the library is configured and scanned.

**Note**: For Docker, you may need to add to docker-compose.yml:
```yaml
backend:
  extra_hosts:
    - "host.docker.internal:host-gateway"
```

### The HQPlayer library as a source (2026-09-27)
HQPlayer scans its own library and answers `<LibraryGet/>` with every
directory and file it holds, tags included — the whole library as one line
of XML (39 618 files, 7.4 MB, 1.2 s from Desktop 6). `backend/hqp_library.py`
brings it into the catalogue as **album variants located at that HQPlayer**
(`album_variants.location = 'hqplayer'` + endpoint, files in
`hqp_library_files`, media_files' mirror without bytes): a copy of an album
the HQPlayer holds — a disk on an HQPlayer Embedded box, a share it mounts,
a hi-res subset copied there — is one more variant, like a CD rip next to a
vinyl rip. The entries go through the scanner's own import
(`scanner.import_metadata`), so an album a local scan minted gains a
variant instead of a twin and keeps its analysis and history; the canon
reads the `owned_files` view (a file here or one the HQPlayer holds) and
stamps recording MBIDs on both file tables. An album only the HQPlayer holds
plays natively there and streams like a phantom elsewhere; its tracks count
as owned for the gates. Playback (live on the Pi, 2026-09-27 late): when
that HQPlayer is the output, the album page offers only its copies and
`play-album` queues them as `kind: "hqp"` items HQPlayer opens itself —
`file:///media/FLASH_SG/…` straight from its own disk, no proxy, the play
tracker keyed on the track UUID; the drift canary and the playlist adoption
recognise those paths; the album page renders such tracks as held rows that
play by track UUID (`play-entities`), and the engagement gates (the sync's
core, the similars backfill, owned-vs-phantom) count a held file as owned
through the `owned_files` view. On another output the same album streams
like a phantom (the copy is left out of the pick). The same night the rule
reached everything that means "owned": the artist and genre pages (albums,
counts, popular tracks by the output's copy), the Home shelves, the library
totals (`library_stats`, migration 029), session replay, radio and
play-similar, text embeddings and the lyrics planner (metadata only, so a
held track qualifies), the scrobble canon (a listen lands on a held track,
its length from the held copy), the MB slice tiers, the share scope and the
assistant's prompt; a held album bound to its release group carries the
Cover Art Archive front (`caa.fill_held_album_covers`, after a sync and on
every discography reconcile).

Since 2026-09-28 the library has an identity of its own — an `hqp_endpoints`
row (the owner's name for it, seeded from `<GetInfo/>`; what HQPlayer
reports about itself; the `<LibraryGetHash/>` the last complete sync saw —
since 2026-09-29 the row of every HQPlayer the owner picked, a Desktop's
included, see "Finding HQPlayers") and `album_variants.hqp_endpoint_id`
points at it: the same library answering from a new address after a DHCP
lease is recognised by its hash and the row moves; a different HQPlayer
that inherits the address (name or product differ) is a new library and the
old row keeps its files without an address. A friend's streamer brought over
is one more row, imported through the same previewed first import, and goes
with Settings › "Forget this library" (`forget_endpoint_id`) — never on its
own. The queue survives an output switch, and since 2026-09-28 so does its
playability: the canonical queue holds identities (the track uuid and the
enqueue-time origin, `QueueItem.source`, which never changes), and on every
switch each slot is re-read for the NEW output (`QueueItem.play`,
`playback/substitute.py`, the album page's rule applied to the queue): a rip
here for any output; this HQPlayer's own copy when it is the output — and
preferred there over the rip, as on the album page; else a stream through
the media proxy standing in for a copy the output cannot reach, resolved
lazily a lead window (3 slots) ahead of the playhead on the status ticks
and landed on the slot as it buffers (`PlayerBackend.slot_ready` starts a
play parked on it — the browser output stands on the slot as "loading"
until then). Switching back to the HQPlayer that holds the copies plays
them natively again, nothing rebuilt. The HQPlayer output takes only native
copies — it mirrors one playlist entry per slot and HQPlayer fetches an
http entry at ADD time, so a stream it has not buffered cannot sit there; a
slot only a stream could serve on it (a file held at another HQPlayer with
no rip here) stays unplayable and the stop names it. The demo ledger applies
to a substituted stream as to any stream.
Settings › Library shows the endpoint's section, and one row per library
the node holds copies from, each with its own Sync (and Forget): a
library that is not the configured output syncs only from that button —
the attach-time re-check follows the output alone. Only an HQPlayer on
ANOTHER machine has a library of its own — an Embedded box, a Desktop on a
second computer; one on this machine, whatever the product, reads this
node's music folder and is refused everywhere (`has_own_library`, by
address like the way files reach it — since 2026-09-29; the rule was
"Embedded only" for a day) — importing it brought every album in as a copy
once (2026-09-27) and was reverted with `python -m hqp_library
--forget-endpoint`. The FIRST import of an Embedded box is a previewed
decision (which HQPlayer, how many files, how many new, how many of them
this node's own); after it the output re-checks
`<LibraryGetHash/>` whenever it attaches or a restarted HQPlayer comes back
and syncs only when the hash moved. A sync never removes rows; "Rescan"
(confirmed) forgets what the library no longer lists and refuses an empty
answer. Measured on the reference library: HQPlayer reads FLAC tags as the
scanner does (99.7 % identical), mp3 tags worse (ID3v1 truncation, encodings,
folder names standing in) — such files become twin tracks without analysis.
A local rip and its copy that the canon had named as two editions of one
release group ("X" / "X (Alt)") fold into one album since 2026-09-27
night: `_split_album_editions` asks, before minting an "(Alt)" edition
beside an existing row of the RG, whether the two are ONE edition —
the same track identities, the same scan tag, a disc marker apart, or a
tracklist Jaccard past the drift threshold with recordings counted only
where both sides are well stamped — and merges the copy into the row
named for the release group (`_update_album_uuid`). The same rule folds
a rip pair ("[Vinyl]" / "[TR24]") and a per-disc split ("Disc 2") that
different tags had kept as rows; a box ripped as CD1/CD2/CD3 folders
under one tag folds on the folder names (a local rip's `disc_number`
stays 1 when only the folder says CD3), and fingerprint titles fold
typography the way the identity does. The import stamps `album_variants.raw_title`
(the scanner never had, since the folder-album migration), and a sync
re-stamps a variant imported without it from the library's album tag.
Inside a folded album, two track rows whose titles differ only by a
featuring credit — "Eyesdown (feat. Andreya Triana)" here, "Eyesdown ft.
Andreya Triana" as HQPlayer read the copy — are one track read twice:
`canon.content.fold_credit_duplicates` (after the edition pass, and
`python -m mb_split_editions --fold-credits`) folds them when the rows
never share a variant (a folder holding "X (feat. A)" and "X (feat. B)"
holds two tracks) and a file of each sits at the same slot or within 3 s
of length; the row with a file here keeps the identity, the copy's file,
listens and analysis follow it (`_update_track_uuid`).
The scan itself starts in HQPlayer's web interface (or by a Digest-
authenticated `POST /library` — an unauthenticated POST is dropped without a
401; leave "Perform analysis" off, it costs ~1.5 min per album on a Pi 5).

### Windows Firewall
Ensure port 4321 is accessible:
1. Open Windows Firewall settings
2. Allow inbound connections on TCP port 4321
3. Verify with: `nc -zv <windows-host-ip> 4321` from WSL

## Testing

Check the port from WSL (`nc -zv <windows-host-ip> 4321`), then use the
assistant tools against the running instance: `hqplayer_get_status`
(version, engine, playback state), `hqplayer_get_settings`,
`hqplayer_set_filter`, `hqplayer_play` / `hqplayer_pause`. The backend's
`/health` reports the HQPlayer connection state.

## Known Limitations

### Not Implemented Yet
- 🔒 **Authentication** - Advanced security features (ECDH + Ed25519)
- 🔒 **Encrypted Commands** - ChaCha20Poly1305 encryption for file paths
- 📊 **Metering** - Real-time audio metering (port 4322)
- 📁 **Library Browse** - HQPlayer library browsing (the wire format is documented
  above; nothing consumes it yet)
- 💾 **Playlist Load/Save** - Saved playlists management
- 🖼️ **Album Art** - Cover art retrieval
- 🔊 **Output Device Selection** - Not available in API (configure in GUI)

### Workarounds
- **Authentication**: Not required for basic playback control
- **File paths**: Using unencrypted URIs works fine on local network
- **Metering**: Can be added later if needed for visualizations

## Shipped since the first iteration

- ✅ Playback control, status monitoring, DSP settings (filters, shapers,
  modes, rates), matrix profiles, convolution and parametric-EQ presets.
- ✅ Queue ownership moved to Sautium: `backend/playback/` holds the
  canonical queue and mirrors it into HQPlayer's playlist, so HQPlayer is
  one output among several (DLNA, browser, local) rather than the only one.
- ✅ Natural-language control through the assistant's `hqplayer_*` MCP tools
  ("what's playing", "play something similar", filter changes).

## Future Enhancements

### A VU meter, and where its signal comes from

The idea: a level meter on Now Playing, drawn in the VU idiom the palette
already names (`#4A7FA7`, "McIntosh VU blue"). HQPlayer's metering port
(4322) is one source for it, not the source — a meter is a property of the
ACTIVE output, so each backend answers the question differently:

| Output | Signal source | Cost |
|---|---|---|
| HQPlayer | the metering port (4322) | a second socket + a new protocol to implement |
| Local | the engine's own ring buffer — the PortAudio callback already holds `int32` frames and counts them; RMS/peak per block is an accumulator read from the other side | ~nothing, but it must not slow the RT callback |
| Browser | `AnalyserNode` over a `MediaElementAudioSourceNode`; media is same-origin, so Web Audio is allowed and the http context is no obstacle | none — it runs entirely in the page |
| DLNA | **none.** The renderer is across the network and GENA carries transport state, not signal | — |

So DLNA shows no meter, by the same rule that hides a seek affordance on an
output that cannot seek (POSITIONING §9): a control may not appear unless it
reflects the audio actually playing. That is correct behaviour, not a gap.

Before implementing the HQPlayer side, settle three things from the SDK
(`hqp-control-*-src`, not vendored here): what the port emits (peak or RMS,
per channel or summed), at what rate, and **whether it is measured before or
after the DSP chain**. Post-DSP is the one worth the socket — it shows
clipping introduced by upsampling and modulation, which nothing else in the
UI can reveal; pre-DSP only restates what the file already says.

Two implementation notes that apply to every source. The subscription lives
**while the meter is on screen**, not while music plays — 20–60 updates per
second pushed to a phone over Wi-Fi is real traffic, and Now Playing being
collapsed is the signal to drop it. And VU ballistics are ours to apply:
what a digital meter reports is peak or RMS in dBFS, so the ~300 ms
integration, the separate fall time and any peak-hold are rendering, done
once above whichever backend supplied the numbers.

The cheap honest start is local + browser: the data is already in hand, no
new protocol, and the meter works on the two most accessible outputs.

### Other

- **HQPlayer library browse / playlist load-save** — not needed while
  Sautium owns the queue.

## Troubleshooting

### Connection Refused
```
Failed to connect to HQPlayer at <windows-host-ip>:4321
```

**Solutions**:
1. Ensure HQPlayer Desktop is running on Windows
2. Check Windows firewall allows port 4321
3. Verify host IP: `ip route show | grep default`
4. Test connection: `nc -zv <windows-host-ip> 4321`

### Commands Not Working
```
⚠️ Pause command failed
```

**Solutions**:
1. Check HQPlayer is not in error state
2. Ensure playlist has tracks loaded
3. Verify command response in logs
4. Try reconnecting

### Docker Connection Issues

**Solutions**:
1. Add `extra_hosts` to docker-compose.yml
2. Use Windows host IP instead of host.docker.internal
3. Check Docker network mode

## API Reference

See the HQPlayer Control SDK documentation (Signalyst, `hqp-control-*-src`) for the full XML protocol specification.

### Key Data Structures

```python
class PlaybackState(IntEnum):
    STOPPED = 0
    PAUSED = 1
    PLAYING = 2
    STOPREQ = 3

class RepeatMode(IntEnum):
    NONE = 0
    SINGLE = 1
    ALL = 2

@dataclass
class TrackStatus:
    state: PlaybackState
    track_index: int
    track_id: str
    position: float  # seconds
    length: float    # seconds
    volume: float
    artist: str
    album: str
    song: str
    genre: str
```

## Performance Notes

- **Connection**: < 100ms
- **Commands**: < 50ms response time
- **Status polling**: Can be done every 1-2 seconds without issues
- **Playlist add**: < 100ms per track

## Security Considerations

**Current Implementation**:
- No authentication required
- Unencrypted XML over TCP
- Suitable for local network only

**Production Recommendations**:
- Use only on trusted local network
- Do not expose port 4321 to internet
- Consider implementing authentication if needed
- File paths transmitted in cleartext (use encrypted commands for sensitive paths)

## Credits

- **HQPlayer Desktop**: Signalyst (https://www.signalyst.com/)
- **SDK**: HQPlayer Control API SDK v5.29.2 (HQP5) / v6.0.1 (HQP6)
- **License**: the integration code is Sautium's own, under the
  PolyForm Noncommercial License 1.0.0 (`LICENSE`)
