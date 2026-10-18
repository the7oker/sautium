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

### Additions of recent builds
Two fields Sautium uses when present. Both are read opportunistically and absent on
older builds — the integration works unchanged without them.

- **Per-filter `description`** (HQPlayer 6) — HQPlayer's own line for each filter in
  the discovery response: on 6.2.3 its technical rating, focus and ratio ("4/5 space ⥣
  Any"); `<GetShapers/>` names a modulator's generation the same way ("Gen7"). Sautium
  surfaces the filter's live in the HQPlayer settings screen and passes it to the AI
  assistant, so filter choices are explained in the player's own words.
- **`process_speed` in Status** (since 5.17.0, March 2026, not only HQPlayer 6;
  5.17.0 computed it wrongly for a DSD source, fixed in the next release) —
  processing speed over playback speed, a running average of the last processing
  units (Jussi Laako; Signalyst publishes no definition). What the DSP load
  measurements are made of (§ "DSP load").

## Files

### Core Implementation
- `backend/hqplayer_client.py` - Main client library
  - `HQPlayerClient` class - Core API client
  - `HQPlayerConnection` context manager
  - Helper functions for URI conversion and time formatting

### Testing
- the assistant tools (`hqplayer_get_status`, `hqplayer_get_settings`, …)
  against a running HQPlayer that is the selected output; `GET
  /api/hqplayer/state` (`connected`) and the HQPlayer row of the More drawer
  report the connection state

### SDK Reference
- HQPlayer Control SDK (`hqp-control-*-src`, C++) — Signalyst's reference code,
  available from https://www.signalyst.com/; it is not linked into Sautium

## Features Implemented

A command returns True only when HQPlayer took it: `result="OK"` or a bare
element — the one rule the error ring reads (`CommandOutcome.failed`,
`_accepted`); `play`, `select_track` and `playlist_add` ask for an explicit
OK — and `refusal()` gives HQPlayer's reason.
Desktop 6.2.3 answers `<Volume result="Error" />` while its volume is fixed, and
HQPlayer 6 refuses every state change of an unauthenticated client while it has
no internet access ("not authenticated and no internet access").

### ✅ Playback Control
- `play()` - Start playback
- `pause()` - Pause playback
- `stop()` - Stop playback
- `next()` - Next track
- `previous()` - Previous track
- `seek(position)` - Seek to position in seconds
- `select_track(index)` - Select track by playlist index. A STOPPED HQPlayer
  starts playing it (its log: "GoTo 1", then "Play (1/0)"; Desktop 6.2.4,
  2026-10-11), so nothing sends it just to ready a slot: a queue rebuilt
  while stopped leaves the slot to the play press

### ✅ Volume Control
- `set_volume(value)` - Set volume level
- `volume_up()` - Increase volume
- `volume_down()` - Decrease volume
- `volume_range()` - `{min, max, enabled, adaptive}`: `<VolumeRange min="-60" max="0"
  enabled="1" adaptive="1"/>` on Desktop 6.2.3; `enabled` is false when the volume is
  fixed on the output — and when the attribute is missing, as Signalyst's own client
  reads it. `None` for a refusal or an answer without `min`/`max`: nothing may lower
  the volume by a range HQPlayer did not give. The benchmark lowers HQPlayer to `min`
  (§ "DSP load"); the HQPlayer screen's ±1 dB steps stay within `min`…`max` (never
  above 0 dB when there is no range) and rest while `enabled` is false — Direct SDM
  holds PCM at −3 dB (Desktop 6.2.3)
- `volume_refusal()` - why a volume command was not taken: after a bare Error —
  what a fixed volume answers — `VolumeRange` is asked and a fixed one named
  (`VOLUME_FIXED`); HQPlayer's own words, or a lost connection, stand as
  `refusal()` gives them

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
  - The SOURCE HQPlayer decodes, from the `<metadata>` child while something
    plays (`samplerate`, `bits`, `channels`, `sdm`; it also carries `float`,
    `bitrate`, `uri`, `gain`): `src_rate`, `src_bits`, `src_channels`,
    `src_sdm`, None while stopped. `active_rate`, `active_bits` and
    `active_channels` are the OUTPUT — 11 289 600 Hz, 1 bit, 2 channels at
    DSD256 (read 2026-10-02)
  - `process_speed` - processing speed over playback speed (since 5.17.0;
    `None` when HQPlayer does not report it)
  - `input_fill` / `output_fill` - the engine's input and output buffer fill
    (the SDK's `statusIO`): fractions of the buffer, 0.0 while stopped; measured
    on Desktop 6.2.3 playing a local file at DSD256 (2026-10-02): `output_fill`
    0.98–1.0 and `process_speed` 2.3–3.0, `input_fill` **-1** — a file HQPlayer
    opens itself has no input buffer; `tracks_total` - the playlist's
    length, `active_mode` / `active_filter` / `active_shaper` / `active_rate` -
    what the engine runs right now, by name (read since 2026-10-02 for the
    playback trace; `active_mode` rides the player status since 2026-10-07,
    for the peak meter's SDM arc; `<Status>` carries more still: `track_serial`,
    `transport_serial`, `queued`)
  - HQPlayer 6's further readings (since 2026-10-11, `STATUS_READINGS`):
    `apod` (the Apod counter of the track playing), `active_bits` and
    `active_channels` (the OUTPUT), `output_delay` (in seconds — HQPlayer
    reports microseconds, the SDK's `output_delay_us`), `display_position`,
    `correction`, `filter_20k`, `filter_junk`; `None` from a build that does
    not report them. They ride the player status while HQPlayer gives them;
    no screen shows them
  - `limited` - HQPlayer's "Limited" counter (`<Status clips>`): how often
    its soft-knee limiter acted because the OUTPUT would have passed 0 dB
    (manual 6 §2.5: the threshold depends on the filter and the oversampling
    ratio). HQPlayer starts it again at every volume change (Desktop 6.2.3,
    2026-10-07), so it speaks of the volume set now. Read since 2026-10-07
    and carried on the player status as `limited`; `None` when HQPlayer does
    not report it
  - `track_gain` - the adaptive gain HQPlayer applies to the track
    (`<metadata gain>`, dB): −9.68 on a loud master with adaptive gain on, 0
    with it off; with the volume, what stands ahead of the limiter
- `get_info()` - Get HQPlayer info
  - Product name
  - Version
  - Platform
  - Engine version

### In the client, not exposed
Methods `hqplayer_client.py` implements and nothing calls — no route, no
assistant tool, no control in the Web UI (as of 2026-09-29):
- `forward()` / `backward()` - Fast forward, rewind
- `volume_mute()` - Toggle mute — a toggle whose state neither `<State/>` nor
  `<Status/>` reports, which is why nothing trusts it with the owner's ears
- `set_repeat(mode)` - Repeat mode (NONE/SINGLE/ALL)
- `set_random(enabled)` - Shuffle
- `get_inputs()` - Available input devices (there is no `set_input`)
- `set_adaptive_volume(enabled)` - adaptive volume on/off (State `adaptive`;
  since 2026-10-11)

### ✅ DSP Settings & Control
- The list getters (`get_modes`, `get_filters`, `get_shapers`, `get_rates`,
  `get_inputs`, `matrix_list_profiles`) return `None` for a list HQPlayer did
  not give — a refusal, another element, a lost or silent connection — and
  `[]` only for one it answered empty (since 2026-10-11). `GET
  /api/hqplayer/state` names the missing ones in `missing` (their keys null)
  and the HQPlayer screen keeps their pickers shut; the assistant is told; a
  benchmark neither starts nor plans on one
- `get_modes()` / `set_mode(index)` - Output mode (PCM/DSD)
  - Example modes: [source], PCM, SDM (DSD)
- `get_filters()` / `set_filter(index, index_1x)` - Audio filters (PCM and SDM)
  - 77+ filters available: IIR, FIR, poly-sinc, closed-form, etc.
  - Separate 1x filter for PCM
  - `parsed` - the description read (`parse_filter_description`): rating,
    the focus words in HQPlayer's order, ratio class, `up`, and the mode by
    its arrow. One filter's two descriptions need not agree: 6.2.4 orders
    poly-sinc-short-lp's focus words one way in PCM and the other in SDM,
    gives poly-sinc-xtr-short-lp/-mp "timbre" in PCM and "timbre, transients"
    in SDM, and poly-sinc-mqa/mp3-lp/-mp "Int up" in PCM and "Any" in SDM.
    `None` for no description (HQPlayer 5) or a shape not read here — the raw
    line stays, and the shape is logged once
- `get_shapers()` / `set_shaping(index)` - Noise shaping/dither
  - 36+ shapers: DSD5, ASDM5, ASDM7, etc.
  - `parsed` - a modulator's generation (`Gen7` → `{"generation": 7}`);
    `None` for a dither
- `get_rates()` / `set_rate(index)` - Output sample rate
  - 20 rates: 2.048 MHz to 98.304 MHz (DSD)
- `set_convolution(enabled)` - Convolution engine on/off — reached through
  the assistant (`hqplayer_set_convolution`), as the last of `DSP_KNOBS` on
  the DSP change path (`convolution` of `POST /api/hqplayer/config`); the Web
  UI has no control for it
- `apply_settings(mode, rate, filter, filter1x, shaper, matrix_profile, convolution)` - any
  subset, one Set* command each, in the order HQPlayer needs (`DSP_KNOBS`: the
  mode decides which lists the indices after it point into); returns what was
  applied and each refusal in HQPlayer's words — the benchmark's. `POST
  /api/hqplayer/config` sends the same knobs in the same order through
  `hqp_backend.change_dsp`, which waits out each knob's build (below)
- `get_state()` - the selection as indices into the current mode's lists, plus
  `filterNx` (the Nx slot `SetFilter value` sets — `filter` read as the 1x
  slot while a 44.1 kHz source played), `filter1x`, `adaptive`,
  `matrix_profile`, `volume`, and since 2026-10-11 `filter_20k`,
  `filter_junk`, `random`, `repeat` (`None` from a build that does not report
  them); no mute flag. HQPlayer keeps one selection per mode (its `settings.xml`
  `<defaults>` hold `filter`/`dither`/`samplerate` for PCM and
  `oversampling`/`modulator`/`bitrate` for SDM)

### The atlas: measured facts of filters and modulators (since 2026-10-17)
`backend/hqp_atlas.py` reads Sautium's measurements of HQPlayer's filters and
modulators, the atlas, and words each fact for the HQPlayer screen's pickers
and a card per name: HQPlayer's own facts from its live lists (the rating, the
focus words in HQPlayer's order, the ratio class, a modulator's generation)
and, while the node holds an atlas file, the measured ones. It reads the file
shipped beside it (`backend/hqp_atlas.json`, not in the tree as of
2026-10-17) or, only while none is shipped, the node's own copy at
`HQP_ATLAS_OVERLAY`: on Docker `./data/atlas/hqp_atlas.json`, mounted
read-only at `/app/data/atlas/` (a directory, so a node without the file gets
an empty folder, never a directory named like the file); under the launcher
`<data dir>/hqp_atlas.json`. The node's copy stays on the node: no backup,
share export, sync or support bundle carries it. The file is read whole and
checked against the shape the module reads; one that breaks it is refused
whole, logged in one line that names a place in the file and never a value,
and the pickers keep HQPlayer's own facts. A changed file is read again on the
next read; no restart.

The HQPlayer screen's own read, `GET /api/hqplayer/state?dsp=1`, joins it by
name in HQPlayer's order: each row of the filter and shaper lists gains
`facts` (its chips, the rating tag, a caption, a twin's line, an identical
group's bracket, whether it has a card), and while the node holds an atlas
file `atlas` carries the sheets' head lines and where the file was read from
— without a file there is no `atlas` key and `facts` are HQPlayer's alone.
The list's mode is its filters' own arrow (⥮ PCM, ⥣ SDM), else the mode
HQPlayer runs (HQPlayer 5 describes nothing); a PCM shapers list is dithers,
which the atlas holds none of. A name's card is `GET
/api/hqplayer/facts?kind=filter|modulator&name=`, read from HQPlayer's list
as the screen reads it: 404 for a name it does not list now (a modulator only
while its list is SDM's), 409 while the output is lent or HQPlayer builds,
503 when it cannot be reached. The plain state carries no facts.

The screen draws them as given (since 2026-10-17): each row of the filter
sheet, and in SDM of the modulator sheet (the Modulator knob opens the
filter sheet's frame; a PCM dither keeps the plain picker), shows its chips,
the focus words, the rating tag, the caption and a twin's line under its
name; the sheet opens with the atlas's head lines; an identical group of
three or more gets one head above each run of its members; ⓘ opens the
name's card over the sheet (layer 210). The Filter row's hint shows the
active filter's facts in place of HQPlayer's raw line. A number is drawn
from its token in `<data value>`, a leading hyphen-minus as the minus sign.

The module also holds the node's one rate namer (since 2026-10-17):
`hqp_atlas.rate_name` names a rate HQPlayer reports as the screen's
`fmtRateLabel` does — kHz up to 768 kHz, a DSD rate as "DSD256" or "DSD512
(48k)", anything else in MHz. The benchmark's point labels (the run's
progress), a verdict's DSP line and the assistant's lists of rates use it.

### Answers: `result` and HQPlayer's reason
Setters and transport commands echo their element with `result="OK"` or
`result="Error"`, and on an error the element's TEXT is HQPlayer's reason —
`<Play result="Error">Empty transport</Play>`. Queries carry no `result`, and
some setters answer with a bare element, which is acceptance; only an explicit
`Error` is a refusal. The Signalyst SDK's own client reads that text at the
start tag, where Qt's stream reader holds none, so SDK-based controllers never
show it; `hqplayer_client` parses the whole line and keeps it (since
2026-10-02): every command leaves a `CommandOutcome`, a refusal is
`last_error` on the client (`refusal()` words it), and every failure — a
refusal, a dropped connection (`command="<connection>"`, `during` naming the
command it carried), a `Status`/`State` that did not parse — lands in one ring
of 50 shared by all client instances (`HQPlayerClient.last_errors()`, paths
cut to their last two components). The DSP screen's `/config`, the
assistant's HQPlayer tools and the playback backend's warnings quote it.
HQPlayer logs the same reason on its side as `clControlThread::ParseMsg():
<reason>`. A `PlaylistAdd` of a file HQPlayer cannot open may still answer OK
and add nothing (seen on the Pi, 2026-09-27); its log then names the file in
`clPlaylist::AddURI("<uri>"): <reason>`.

### One link per HQPlayer (since 2026-10-12)
Everything this node says to an HQPlayer goes through that HQPlayer's link
(`backend/playback/hqp_link.py`), one per `hqp_library.address_key`, so this
machine's aliases are one link: the status poller, the commands, the
HQPlayer screen's reads, the play gate's probe, the library's hash and
listing, the in-process assistant's questions, and putting back a benchmark
run cut short. A link owns one connection, taken by each request for itself
in the order they came — a question goes between the requests of a command
sequence — so at most one request of this node is outstanding at HQPlayer
(its one control thread answers in order anyway; two connections only sent
requests into each other's silences). After a command a silence may follow
— one that may set off a build, a Stop, a PlaylistClear — the connection
passes to the sequence's next request or to a Status the link holds itself,
so that silence never falls on a question with a short wait — the request
it passes to waits as that command may make it: watched up to the build
limit after one that may set off a build, up to `STOP_ANSWER_S` after a
Stop or a PlaylistClear, past which HQPlayer is not answering (said before
the connection goes to anyone else); a request
HQPlayer leaves unanswered, with no build of ours behind the silence, ends
its sequence or question block — the rest is refused, never sent into the
same silence. What a request came to (its refusal, its silence) is the
thread's that sent it, read in the same call. It also owns the command
lock (one command sequence at a time, in the order they came; a person's
command is refused while HQPlayer builds or answers nothing past a short
silence's wait), the build watch (below), the intent clock — the order of
intents is the order of the asks (since 2026-10-14): each takes the next
number as it is asked and holds once its command sequence has the command
lock, so one the lock refuses as busy supersedes nothing (one refused at its
first request, the lock its own, keeps its number: an older word never beats
the person's newer one, and the route says the word did not go); a new queue
and a rebuild hold from their ask — a play press's hand-over is cut short at
its next add — and are withdrawn if the lock then refuses them, or the
hand-over fails before it holds (making its URIs): the hand-over owns its
number (`HqpBackend._handing`). An item left for later (a start that met a
build, a play press's starts and its hand-over, the resume after a DSP
change, the knobs after a build, a benchmark run's owed put-back, the Stop a
switch away owes) is dropped at its send once a newer intent of a kind that
supersedes it holds (`SUPERSEDED_BY`) or its queue was replaced — and the
Stop every switch away owes (the late Stop, below). Picking another HQPlayer
retires the link of the one picked before — no other: a library read of a
third box goes on — the Stop it owes still goes (only while it plays or
pauses an entry of ours), everything else gives up, then it closes; picking
the same HQPlayer again keeps its link and its build watched, and the link
dials the alias picked last (on a Docker node the host's address, not a
loopback that reaches nothing — the meter follows it). While the benchmark
holds the output the link is lent — before the backend is detached, so what
it still sends (a play press's hand-over) is refused at its next request: a
request already sent is answered first (the lend waits for it, a connection
kept for a sequence's next request let go), then nothing of it goes out, and
a person's command meanwhile (the HQPlayer screen's DSP change, the volume)
is refused with 409 naming the job. The node stop closes every link first
(`hqp_link.close_all`): its waiters and owed items give up at once, and
every wait on HQPlayer — a read, a connect, the meter's session included —
is woken through its wake channel and closes its own socket (since
2026-10-13). A socket closed by a thread that does not read it raced the one
that does: CPython reads the descriptor, lets the GIL go, then polls it, so
a close in between left the reader polling a number that was free or already
another socket's — and a close does not wake a poll on macOS. Each wait
watches its socket and a socketpair together (`hqplayer_client.Waker`,
`wait_on`, through the selectors module's default, never `select.select`,
which refuses a descriptor past 1023); a wake-up given before the wait began
is not lost. A benchmark cancel wakes the run's read the same way. A request
that holds the connection outside any wait when its link closes — between
its answer and its end — closes the socket itself as it lets the connection
go. A connect to a name looks the name up on a helper thread the same wait
watches (getaddrinfo can be neither woken nor bounded: a `.local` HQPlayer
switched off took the resolver seconds, and held the node stop with it), so
a close or the connect's own bound ends the lookup too; an address is
dialled as it is.

### Builds: a DSP change waits one out (since 2026-10-11)
HQPlayer answers nothing on its control port while it builds a setting — a
filter, a modulator, a rate, and every start — and carries out what it was
sent meanwhile once it is done, in the order it came: a command given up on
is not withdrawn. Desktop 6.2.4 answers a `SetFilter` sent mid-play at once
and falls silent on the command after it; a PCM filter swaps without a
silence, an SDM one built for ten seconds on a laptop. It accepts and
starts new connections all through a build, and the meter's first frame
comes after the control port answers again — so neither can end the wait.
- **Waits by command.** A link's client waits for an answer as long as
  its command may take (`hqp_link._command_waits`, read when a link is
  made): 5 s for a query (4 s for the poller's),
  `STOP_ANSWER_S` (15 s) for a Stop and a PlaylistClear — the output's
  teardown, a second in PCM and up to three and a half in SDM on a Mac; a
  short silence, never a build: a command behind it waits, and past it
  HQPlayer is "not answering" (people's commands refused with that line,
  questions go on until it answers) — and `BUILD_LIMIT_S` (10 min) for what may set off a build or meet
  one: the Set* commands, Play, SelectTrack, Seek, Next, Previous.
- **The build watch** (`hqp_link.BuildWatch`, one per link). An answer
  later than `BUILDING_AFTER_S` (1 s) is HQPlayer building: the player
  status carries `building` (since when, after which command), the status
  poller says the build instead of asking, and `GET /api/hqplayer/state`
  answers from the watch — the HQPlayer screen keeps what it read last and
  holds its controls, Now Playing shows "Building the filter…". The build
  ends where the late answer comes. Past `BUILD_LIMIT_S` it is
  `unfinished` — "HQPlayer did not finish building the filter — restart
  HQPlayer" — until HQPlayer answers a request again: only questions go
  then, with the back-off of an HQPlayer away, and while it takes the
  poller's connection and says nothing that stays the build's, never
  "disconnected"; an HQPlayer that refuses or drops the connection counts
  as away. A silence no build of this node's came
  before — HQPlayer taking the connection and answering nothing on its own —
  is neither: the status says "HQPlayer is not answering" (`not_answering`). A held
  answer whose connection breaks — HQPlayer closed it, restarted, or its
  machine stopped answering (keepalive; on Linux `TCP_USER_TIMEOUT` keeps
  the same 35 s bound for a command not yet acknowledged) — ends no build
  and is no `unfinished`: HQPlayer went away, the poller asks again, and its
  return is read as any other (its playlist checked; one that restarted has
  lost the mirror, and gets the queue back — below).
- **People are refused, background work waits.** A command someone is
  waiting for — a play intent, a transport button, a queue edit, the radio
  start, a DSP change, the volume — is refused with 409 in the link's own
  words (`HqpLink.refuse`): building the filter (`Building`), a build it did
  not finish (restart HQPlayer), or not answering (`NotAnswering`). The
  output settings carry that line (`busy`), and both assistants' gates quote
  it, beside the benchmark's hold. The play gate's liveness probe
  (`HqpLink.probe`) is a question on the link: a command on its way is
  answered first, and its silence turning out a build is busy, never
  "offline"; so is a probe HQPlayer takes and leaves unanswered
  (`NotAnswering`, 409). Work no one waits for — a filler adding an album's
  tracks as they arrive, HQPlayer following a re-bind of the queue's files,
  the Stop an output switch owes, the resume after a DSP change, a start
  that met someone else's build — waits for the build's end through one
  mechanism (`hqp_link.through`: the watch's event, never a poll, bounded by
  the build's limit, called off when the work goes elsewhere or a newer
  intent supersedes it), checks again whether it is still wanted (the
  queue's generation, the output) and goes on. A build HQPlayer does not
  finish, or goes away during, gives it up with why in the log — and a
  filler loses nothing to it: what it took goes into the queue alone, the
  mirror marked lost. A wait whose link was let go (another HQPlayer
  picked) does not stop a filler either: it goes on through the manager, on
  the output there is then; an add the link did not send waits for the
  output change to be over (`PlaybackManager.wait_for_output_change`, an
  event), never a sleep. The node stopping ends a filler; a file the output
  refused is not offered again.
- **Output switch, address change.** A switch away stops HQPlayer if it
  plays or pauses an entry this node gave it — one this attachment handed
  it (a slot removed since from a playlist that is no mirror is still
  ours), or a slot of the queue as it stood at the switch, never as it is
  once the Stop goes; another controller's entry is left playing, a file
  this node handed some HQPlayer once among them. The Stop
  is the link's late Stop, on its own thread, which outlives the backend
  that owed it: its Status is held until HQPlayer answers — through a build
  it is in, or a silence no build of ours explains, up to the build limit,
  nothing else sent meanwhile — and the switch waits for its first outcome
  (the Stop sent, or not needed) a second at most: past it the answer says
  HQPlayer builds or is not answering and will be stopped once it answers,
  and the new output starts. The detach waits for no teardown — that Stop's
  answer, or a person's Stop tearing HQPlayer's output down or still waiting
  its turn on the connection — and not
  for the old poller: a question of its still waiting for the connection is
  not asked, and one HQPlayer has yet to answer is not waited for. A lost mirror owes the Stop too: it
  is no health matter. Picking HQPlayer again supersedes it — the picker
  and an address set back register the attach before anything is released
  — and it is never sent while the benchmark drives HQPlayer, nor by a
  stopping node, nor after a connection that broke. Picking the HQPlayer
  already driven again (any alias) changes nothing in playback — no detach,
  no attach: a DSP change, a resume, its spot go on. Another HQPlayer's
  address retires the old link: what is left of a DSP change, a start, a
  resume, a mirror send is not sent there.
- **The DSP change** (`change_dsp`, behind `/config` and the assistant's
  filter, shaper, matrix profile and convolution tools). Each knob's Set*, then a
  `<Status/>` held on the same connection until HQPlayer answers; the next knob
  goes only then. Past `BUILDING_AFTER_S` the change answers at once with
  what was taken and refused so far, the knobs still to go and the build;
  the rest follows behind the answer, and its outcome rides the player status
  (`dsp_change`) of the attachment it was made in, under an id no restart
  repeats — a knob refused there is toasted in HQPlayer's words, once per
  change and tab, and an output switched away forgets it. A held Status
  HQPlayer did not answer — a build it did not finish, a connection that
  broke — an output lent meanwhile, or a newer intent that supersedes the
  rest (another DSP change, a switch away) ends the change there: the knobs
  after it are reported as not sent. Built, a change made while HQPlayer played
  an entry of ours resumes that queue track at its second only if HQPlayer
  came back stopped (6.2.3 on Windows did; 6.2.4 plays on) — the entry read
  in the change's own command sequence, before its first knob, against the
  queue as it is then (an old queue's entry while a replace still adds is
  none of the queue's), HQPlayer's state read again right before the
  resume, in the same command sequence, after any build begun meanwhile; a
  newer intent or another queue drops the resume, another controller's entry
  gets none, and on a playlist that is no mirror the resume is a start of a
  play press (below), which a newer DSP change still drops.
- **A start is a build.** Play waits for its own answer; a SelectTrack starts
  a stopped HQPlayer by itself (its log: "GoTo 1", then "Play (1/0)"), so a
  slot is resumed as SelectTrack, then Seek — landing in the start's opening
  run of silent frames, before anything is heard — then Play
  (`HqpBackend._start_at`); a jump to a slot HQPlayer refuses sends no Play.
  A start waits without the queue lock: a replace that plays, and a rebuild
  that plays on, mirror the playlist under it and start once it is let go
  (`PlayerBackend.queue_start`), so a queue edit meanwhile is refused (409)
  or waits as background work, never parked behind the build. A start that
  meets someone else's build there is not refused — the queue was
  committed: it waits for the build's end on the link, behind the answer; one
  a newer intent drops (a Pause first) leaves its slot and second as the
  spot, for the play press — unless a start was taken or a spot set since it
  was owed (the spot's generation). A paused
  HQPlayer whose playlist is rebuilt is left stopped at its spot, never
  played to be paused again: the status names the spot's slot and carries
  `resume`, Now Playing says "Stopped at m:ss — Play resumes here", the next
  reorder and a "play next" anchor on that slot, and the play press resumes
  it there. The spot names a queue item, not a slot: an insert or a removal
  before it moves its index, an append after it changes nothing, and it goes
  with its item. Stopped there, Next and Previous only move it a slot, to
  its start — no command, no sound; a tap on a track plays that track. A
  start taken spends it — a plain Play too — spent under the command lock
  the start held, so a start dropped meanwhile puts none back. One
  HQPlayer answered past the build limit, given up on, is judged at
  HQPlayer's first answer since that names a slot (an opening tick names
  none): playing or paused on its track, it was taken after all. A command refused,
  as busy or by HQPlayer, leaves it. Back on HQPlayer after another output,
  the slot the queue was on is a spot when the attach found HQPlayer
  stopped; one that plays keeps its own place.
- **The mirror's state** (`HqpBackend._mirror`): in step; a tail mirror;
  lost; drifted.
  - **Lost** when a build ran out or HQPlayer went away under a queue edit,
    when an append or a "play next" found it not answering (a "play next"
    whose playlist read HQPlayer did not give included — no reading is no
    shorter playlist — and one read while HQPlayer opens an entry it names
    no slot of yet), when it left a
    removal or the radio's clear unanswered within its wait (it carries
    them out once its silence is over: the slot leaves the queue, the radio
    goes on), when a replace was cut short by a silence (the queue takes
    every track of it), when a radio began while HQPlayer was stopped (its
    clear keeps no entry of the seed the queue keeps) or had moved on from
    the slot the queue keeps since its last status, when a step of a new
    order or of this repair went unanswered within its wait — a Play's
    repair of a drifted playlist included (HQPlayer carries it out once its
    silence is over; the repair is then due again),
    when a re-bind's read of HQPlayer's playlist went unanswered or was
    refused, when HQPlayer moved on from the slot a new order was planned on,
    or reads none such an edit may go by — another controller's entry and,
    in step, a state the SDK does not name or a stop under way — read in the
    edit's own sequence (since 2026-10-14): the entry it plays is never
    removed, nor restarted by a rebuild (a "play next" that finds it moved
    on to a slot of the queue's loses nothing: it goes behind the entry it
    reads, in the queue as in HQPlayer) — or when it came
    back restarted — through a silence: from then on appends, inserts,
    removals and new orders are the queue's alone, and once HQPlayer answers
    again it gets the queue back, once per loss (`HqpBackend._reconverge`):
    playing or paused on an entry of ours, what lies behind that entry is
    replaced by remove-after / re-append, the sound never stopped; stopped,
    it takes the queue afresh; playing or paused on an entry not ours, or on
    a file queued at more than one slot it holds (no verdict which one it
    reads), it stays lost — stopped there it takes the queue afresh, at no
    spot; the repair reads HQPlayer's entry again in its own sequence, and
    one that moved on meanwhile is repaired at its next answer. Lost too
    when HQPlayer refused every file of a mirror, or of a person's replace
    after its Stop: no slot becomes a gap (every slot a gap would read as an
    empty queue held), and the next Play hands the queue over again.
  - **A tail mirror** when that repair leaves a difference before the entry
    HQPlayer reads: what touches only the tail (an append, a "play next",
    HQPlayer's own advance) goes to HQPlayer as on a mirror, the entry it
    reads naming the slot — a "play next" run piece by piece behind the
    piece before it; what touches the part before it is the queue's alone.
    An edit behind it while HQPlayer reads no slot of the queue's, or a file
    queued at more than one slot it holds, lands on no entry by a guess: it
    is the queue's, the mirror lost. A radio begun on it while HQPlayer
    reads the slot the queue keeps leaves it in step — the clear keeps the
    entry HQPlayer reads, the queue the slot it reads; begun once HQPlayer
    moved on from that slot, it loses it.
  - **Drifted** by an edit made outside this node or a mirror step HQPlayer
    did not take: never reconciled on its own, and never edited by the
    queue's places — a remove, a reorder, a "play next" are the queue's
    alone until the play press re-mirrors. An attach that finds HQPlayer
    playing or paused leaves its playlist alone and judges it at once, as
    the canary does (since 2026-10-13): another controller's, or one that is
    no longer the queue, is drifted before a tap can select into it — and so
    does the poller's first answer that reads an entry not ours while the
    mirror is in step (once per entry and queue version).
  - **The play press** on a playlist that is no mirror: Play on a pause
    only resumes (the tail behind the entry read is repaired as above);
    Play from a stop, a tap, Next or Previous while it plays hand HQPlayer
    the queue afresh — once — and start a track, in the background: the
    route answers at once, naming the track it will start (`starts`), and
    the status says "HQPlayer is taking the queue again". The queue lock is
    held only to read the queue and to judge what HQPlayer took, never
    through the hand-over: no route waits for it. Every play intent while it
    runs is a start of its own, numbered as it comes, and at its end the
    newest one no newer intent superseded goes: a Play starts the newest
    target again at the newest start's second (the press's own spot's, or a
    Seek's made meanwhile), a tap its track, Next and Previous count from
    the newest target — those three from 0 s. A Pause or a Stop drops the
    start and the hand-over still completes — HQPlayer stopped at the
    target, the next Play starting there without one; a press that found
    the playlist the
    queue's again (a reconverge got there first) hands nothing over and
    stops HQPlayer for a Pause or a Stop made meanwhile. A Seek decides
    nothing about playing: it changes the second of the newest word — the
    newest start's (a start of its own, the same target), or after a Pause
    or a Stop only the spot the press leaves. A new queue (a replace, the
    radio) is the newest word as it is asked: what the hand-over still had
    to send is dropped, a press it overtook takes no more words — a Play, a
    tap or a DSP change's resume made then is one of its own — and hands
    HQPlayer nothing the reconverge should wait for. One the command lock
    refuses (HQPlayer building, not answering) withdraws its number: it
    supersedes nothing, and a DSP change's resume still goes. While the
    press drives HQPlayer the play gate asks it nothing (a question would
    wait behind its Stop and adds). A queue edit meanwhile is the queue's: an
    append follows behind what HQPlayer took (in step); anything else leaves
    the playlist drifted, the start going at HQPlayer's own entry for the
    target — a target removed starts the next track still queued. Next and
    Previous at a stopped spot only move it; at either end of the queue they
    send nothing.
- **Words during a queue mutation** (since 2026-10-13). A replace, a new
  order, a re-bind's or the reconverge's rebuild, the radio's clear — and,
  since 2026-10-14, a "play next" and a removal, which move HQPlayer's
  entries under a slot — hand HQPlayer a playlist under the queue lock, and
  commit the queue only after it: the person's play intents made meanwhile
  are held as a play press holds them (`PlayerBackend.mutation`,
  `HqpBackend._hold_words`, which numbers the mutation in the same step:
  every word held is newer than it), answered at once (`starts`, the play
  gate asking nothing), and the newest one no newer intent superseded is
  carried out once the queue lock is let go (`queue_start`) — on the queue
  as it then stands, the mutation committed or not (since 2026-10-14), as
  the same word would go then: a start at its slot's entry while the mirror
  is in step, else as a play press of its track (HQPlayer's entries are no
  slots of a tail mirror's or a lost one's). A word names a track, never a
  slot, translated against what HQPlayer is handed — the queue sheet still
  shows the old queue while a new one loads, so a tap carries the track it
  showed (`/jump`'s `track_id`) and plays it wherever the new queue holds
  it, from its start; a track the mutation takes away is refused in words,
  and the words before stand. The same holds with nothing in flight, on
  every output: a sheet not refreshed yet after a queue played from
  elsewhere taps the track it showed, wherever the queue holds it now
  (`CanonicalQueue.slot_of`), and one the queue no longer holds is refused
  in words. HQPlayer decides the tap as its start goes, under the command
  lock (since 2026-10-14): a mutation begun between the tap and its start
  holds it, one committed meanwhile moved its track's slot (found by its
  track, as the first check finds it), and a playlist that is no mirror by
  then makes it a play press — whatever it becomes numbered as the tap was
  asked: its number holds only once it goes, so a tap refused there
  supersedes nothing and a word made after it stays newer. A word made while
  it waited its turn was taken on what played, without it, and stands: the
  tap yields to it — a held tap is not carried out, and a play press it
  became, holding no Pause or Stop, rebuilds nothing, never leaving HQPlayer
  stopped (since 2026-10-14). An output that holds no words (a renderer, the
  browser) is handed that slot even while a mutation is in flight
  (`PlayerBackend.holds_words`). A Play asks again for the start the
  mutation owes — the replace's first track, the rebuild's track at its
  second — and that start goes, numbered as the Play: a Play during a new
  order's rebuild resumes the track that played, at its second, never
  HQPlayer's entry 1. Next and Previous count from the newest start (the one
  owed before any) — with no start made and HQPlayer at a spot (a rebuild
  that does not resume leaves it at one, or it stands at one) they only move
  the spot, as with nothing in flight; with nothing to count from — no
  start, no spot, the queue handed afresh to an HQPlayer back from a restart
  — they move nothing (since 2026-10-14); a held Next or Previous that finds
  a slot among what HQPlayer is handed counts on the queue as it stands from
  where it began — the layout HQPlayer was handed once the mutation changed
  the queue, the queue it left when it did not (a "play next" HQPlayer
  refused whole, a removal it refused), a Play or a Seek after it keeping
  the count — and it begins at the track HQPlayer plays as the edit's own
  read finds it (a "play next" placed behind a track HQPlayer moved on to);
  one that finds no slot there moves nothing, even where the queue left has
  one. A Seek changes the newest word's second — after a Pause or a Stop,
  the second the next Play starts at, with no word of its own; a Pause or a
  Stop drops the starts before it and always goes — sent, unless the
  mutation's own Stop stopped HQPlayer: then nothing starts and the spot is
  left at the newest start's track — and a DSP change made while it is held
  resumes nothing over it (since 2026-10-14). With no track to count from —
  HQPlayer plays on, or the reconverge knows no slot it stood on — a Play or
  a Seek acts once the queue stands, as it would then (a Seek before a Pause
  still holds when a Play follows). Each word goes under the number it was
  held with, never numbered again — a word the person made after it, waiting
  its turn, is never undone by it — and its outcome is its own, never the
  mutation's route's: overtaken by a person's newer word it does not go, one
  that meets a build goes once it is done, one HQPlayer refuses is logged. A
  start whose track the queue it lands on does not hold — a tap on a new
  queue that did not commit — is said with that queue's refusal; a new queue
  HQPlayer refused whole is answered as one (`QueueRefused`, in HQPlayer's
  words). A play press asked while a mutation holds words is one of them,
  and the reconverge yields to a press asked before it.
- **Gaps** (since 2026-10-13). HQPlayer's playlist is the queue without its
  gaps, in the same order: a gap is a slot this HQPlayer does not hold —
  nothing it can open (a file held only in another HQPlayer's library, one
  that left the library: the slot's way in, `QueueItem.play`, read again at
  every mirroring, so a copy that came back is handed over), or a file it
  refused while it took the rest of the batch (until the next full mirror
  hands it over again, or the slot is re-bound to another copy — kept by
  the HQPlayer's link, so a backend attached to it again sees the same
  gaps). A gap stays in the queue, named in `/api/player/playlist` (`gap`),
  and is never handed to HQPlayer: the queue sheet fades the row and names
  why on a line of its own; a tap on it is refused, the jump route
  answering the output's words (`refusal`), which the sheet shows; a start
  at a spot on it goes from the next slot HQPlayer holds at 0 s, Next or
  Previous at a stopped spot skip it, and a "play next" anchored on it goes
  after the last entry HQPlayer holds before it (a new order made while
  stopped there is a rebuild). A gap is never the entry HQPlayer reads,
  though the same track may be queued at a slot it holds. A "play next"
  whose place HQPlayer's playlist cannot give (shorter than the queue it
  holds) is the queue's alone, the playlist marked drifted — never added at
  its end. A queue slot and HQPlayer's entry are told apart by type
  (`hqp_backend.Entry`), the layout (`_Layout`) is the one translation
  between them, and SelectTrack and PlaylistRemove take only an entry. The
  clear of a full mirror rides on the first add HQPlayer takes.
- **What HQPlayer refuses whole** — every file it was handed (the disk or
  the proxy out of its reach, or the files themselves) — commits nothing of
  a person's replace or append and keeps a mirror's queue whole, none of it
  a gap: the mirror is lost and the next Play hands it over again; a replace
  none of whose files this HQPlayer could open is refused before anything
  is sent. One notice per batch names what was refused and both causes it
  may have; a batch it takes whole ends the last one.
- **The assistants' transport** (both surfaces: `tools/definitions.py` and
  the MCP server) goes through the backend's own routes (`/api/player/*`,
  whose answer carries HQPlayer's refusal for the assistant to quote): the
  link the watch hears, the play press's spot, the playback trace — and a
  play command waits as long as its start may build. Their questions go
  through the link too: the in-process assistant's on it directly, the MCP
  server's (a process of its own) through the backend's routes
  (`/api/hqplayer/state`, `/api/player/status`), so it keeps no connection
  to HQPlayer at all. A list HQPlayer did not give comes with its refusal
  (`refused`), and a name is picked exactly as written before any case of
  it or any part. A filter, modulator or matrix profile set by name is a
  command from its first step: the list it names from is read behind the
  command gate, refused at once in the link's words while HQPlayer refuses
  people (since 2026-10-13 — read as a question, it waited into the
  silence and answered with another line).
- **History rotates once the output took the queue.** A play route's
  session rotation runs inside the replace (`PlaybackManager.replace_queue`,
  `archive`): once HQPlayer has taken the mirror, under the queue lock,
  before the commit — a play refused at the gate, at the mirror or by
  HQPlayer itself leaves the listening history as it was; a rotation that
  fails commits nothing, and HQPlayer, its start never sent, gets the queue
  it lost back. The radio start rotates under HQPlayer's command hold,
  before its PlaylistClear: busy or lent, refused before either; a rotation
  that fails clears nothing; a clear HQPlayer refuses leaves the queue as it
  is and the radio follows it, history rotated (one it leaves unanswered is
  the queue's: the radio goes on, the mirror lost).
- **Embedded answers through its builds**, saying it plays with nothing out
  yet (`hqp_benchmark._building`, 25–70 s on a Pi 5): the poller reports that
  as a build too, and refuses nothing — there is no silence to queue into.
  On Desktop the same shape is only a start's opening ticks.

### Ports
Checked with netstat on the Desktop 6 host (2026-10-02):

| Port | What |
|---|---|
| TCP 4321 | the control protocol |
| UDP 4321 | discovery (`<discover/>`) |
| TCP 4322 | the meter stream (below) — the control port + 1, HQPlayer's default (its `HQPLAYER_METERPORT` and `HQPLAYER_CTRLPORT` environment variables move them; Sautium follows the SDK's + 1); HQPlayer logs "Meter connection from …", "Metering started from …", "Metering ended from …" |
| TCP 8019, UDP 1900 | the UPnP renderer and its SSDP |
| TCP 8088 | Embedded: the web interface. Desktop 6 listens too but answers `404 Error` to `/` and `/log` |
| TCP 4323 | nothing listens |

### The meter stream (TCP 4322)
From the SDK's `clMeterInterface`, measured on Desktop 6.2.3 against the FLAC
being played (2026-10-07):

- The client connects and sends nothing; HQPlayer streams frames. While it is
  paused they carry silence (−386 dB); while it is stopped none come — the
  connection stays open and quiet. Two clients can read at once.
- A frame is a 32-byte header `<IIIiffff` (version 1, channels, xformLength =
  1025, xformBits = the source's bits, bandwidth = the source's Nyquist,
  xformTime = 1024 / source rate, xformGain = 2, reserved), then per channel
  `peakMax, peak, rms, rmsMax` (float32, dB) followed by two xformLength
  float32 blocks: the Re and Im of the block's spectrum, which the official
  client draws its spectrogram from. Stereo: 16 464 bytes.
- Frames are cut at the SOURCE rate (a hop of 1024 samples, 43 a second at
  44.1 kHz) and sent in bursts every 160 ms; the levels change once per
  burst. A reading covers ~160 ms, 6.25 a second, and arrives ~20 ms after
  HQPlayer's reported position passes it — `output_delay` (1.05 s there) is
  already accounted for. ~0.7 MB/s at 44.1 kHz, proportional to the source
  rate.
- The levels are measured after adaptive gain, volume AND the limiter, before
  upsampling. Below 0 dB they are exact: RMS read −12.679 dB against the file
  at an expected −12.690 (gain −9.68 + volume −3.01), and `peak` is a TRUE
  peak — it matched a 4×-oversampled peak of the file within 0.023 dB (MAD),
  the sample peak only within 0.18. Past 0 dB they say nothing of the over:
  with adaptive gain off and the volume at 0 dB a master with +2.5 dBTP read
  +0.00 while `limited` climbed 5, 6, 7, 8, 10. `rms` is 10·log10 of the mean
  square (a sine reads −3.01 dB); `peakMax`/`rmsMax` hold since the
  connection opened.
- The spectra are the SOURCE itself, before gain, volume and limiter: each the
  2048-point FFT of a periodic-Hann-windowed block, scaled 2/N, Im negated,
  blocks a hop (1024) apart. An inverse FFT and an overlap-add (the window
  halves sum to one) gave the FLAC back within 2·10⁻⁸ — float32's precision —
  and its true peak to the thousandth.

### The peak meter (HQPlayer screen)
A gain-staging tool (decided 2026-10-07: "not a toy, a tool for setting
gain"), so it lives by the volume control, not on Now Playing: the Meter chip
on the Volume row opens a sheet with an analog true-peak needle per channel
and its own ±1 dB, a step and its effect side by side.

The needle reads what HQPlayer's own levels cannot past 0 dB: the node
rebuilds the source from the stream's spectra, takes its true peak (4×
oversampled) and adds the volume and the track's adaptive gain from the
status poller — the peak before the limiter. Where nothing is limited it
matches HQPlayer's own peak burst for burst (median −0.005 dB, MAD 0.013 dB);
above 0 dBTP it is the over the limiter takes away. A gain stage outside
that sum (convolution, an EQ) is not in the reading. A reading covers 0.16 s
of the source rebuilt — HQPlayer's own burst, however TCP cuts it on the
way — and carries the gain it was made with. While HQPlayer names no track
(stopped) no reading is made: the gain of what plays next is not known, and
the needles rest.

The scale is a classic VU's geometry — deflection proportional to amplitude,
−20…+3 dBTP, a third of the arc on −3…+3 — and the needle rises at once and
falls 20 dB in 1.5 s. OVER lights while it is above 0 dBTP; Max holds the
highest peak since the sheet opened (Reset, or a change of the gain each
reading carries — a volume step, another track's adaptive gain — clears it);
Limited is `limited`, counted from the last volume step. In SDM mode an amber
arc marks −3…0 dBTP: the manual asks for 3 dB of room for the modulator
(§2.15). The design reference is `docs/design/reference/hqp-meter/`.

The socket to 4322 exists only while some page shows the sheet
(`playback.hqp_meter`). A page states its wish with `PUT /api/hqplayer/meter
{tab, on, seq}` for the `/api/events?tab=` stream it holds; the readings ride
that stream as `meter` messages, and the wish dies with the stream. A
reconnect of the same page takes the wish over, and every connection opens
with `hello`, the page's cue to re-state it. One thread owns the socket and
connects on edges only — an attach, a page's wish, the status poller finding
HQPlayer again, one retry when a flowing stream drops — and reports a port
that refuses or stays silent instead of retrying it. A page that turns the
meter off and on again while it connects gets it: the stop ends that
connect and the newest wish connects again, and a stop's wake-up the newest
wish undid is spent as the session opens, never read as the stream's end
(since 2026-10-13). While the socket is open
no DSP sample is written (`hqp_load`): metering costs HQPlayer work.

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
that queue into HQPlayer's playlist, and each slot is opened the way the
active HQPlayer can reach it (`HqpBackend._uri_for`): `media_files.file_path`
for a rip here — by path on this machine, as a stream anywhere else —
and `hqp_library_files.hqp_path` for a copy in that HQPlayer's own library.
`tracks.id` identifies the music, the file row identifies the bytes
(CLAUDE.md § Data Model Conventions). A phantom track has no file and
resolves to a stream through the media proxy instead.

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

The playlist canary (`_judge_mirror`) and adopt-on-attach read HQPlayer's
playlist back through the same rules — a `file://` URI with HQPlayer's
percent-escapes undone (the stored path, or the path of a copy in its own
library), a `/file/{token}` URL through the proxy's registry — so both
work whether HQPlayer opens paths or is handed streams.

**What plays is checked on every status tick, not by the canary.** HQPlayer's
`<Status>` names the entry it reads (`<metadata uri>`, sent to an
unauthenticated client by Desktop 6.2.3), and `_playing` resolves it against
one view of the queue (`CanonicalQueue.view()`: items, version and whether a
mutation is in flight, read together) to the slot it is, nearest HQPlayer's
index first. A slot is the entry when either says so: the hand-over ledger —
the track Sautium handed that URI for, still right after the slot was re-bound
to another copy — or the URI itself by the same rules, a `/preview/` stream by
its session's track, an m4a transcode by its file. A CUE image whose cut
failed is handed for every slice, so the ledger alone would name the last one;
the path keeps the slice at HQPlayer's slot. A token counts only at the
address this backend hands HQPlayer: another node's media proxy or a NAS URL
with `/file/` in its path is foreign. The status carries the item it found,
and the manager reads the slot off the item: a mutation that committed between
the two looks cannot make an index name another track. An entry that is not in
the queue (a file played from HQPlayer's own GUI, another controller) is
external playback (slot 0) from its first tick: no listen, no Last.fm call, no
DSP sample, the benchmark holds back, and the listen that was open ends there.
Two ticks give no verdict and are not emitted. One is the tick on which
HQPlayer opens an entry: PLAYING at track 0 and length 0, with the URI already
named; a listen opened there froze a length of 0. The other is an entry the
queue does not hold at the slot HQPlayer's entry names (through the layout:
the queue without its gaps) while a mutation is in flight — unless the
asker is the backend's own mirror step, whose mutation it is. Sautium's
own mutations mirror into HQPlayer before the queue commits them, and every
queue mutation of the manager runs inside `CanonicalQueue.mutation()`: a
replace plays its first track before its commit, and a removal shifts
HQPlayer's index first. While a mutation is in flight the canary judges
nothing either, nor a read the mirror's state changed under (a loss its
stale "in step" would undo), and its verdict comes before the slot is read,
since the fallback rests on it. A verdict is kept per (entry, its place, the slot the
layout names, queue version, playing or not): a foreign entry played for an
hour is looked for once. A stopped HQPlayer's slot is named the same way, by
what its entry is — never by its place in a playlist that is no mirror.
Until 2026-10-04 the slot came from the index alone and only the canary (every
30 polls) caught an external edit, so for up to half a minute such a file was
tracked and scrobbled as the queued track at that index; and a queue holding a
slot HQPlayer drops (one it cannot open) went untracked while the canary said
drift. A build whose status names no entry, or a stream token of an earlier
session, leaves the layout's slot standing while the playlist is the mirror —
external (playing) or no slot (stopped) while it is not.

**A restarted HQPlayer gets the queue back.** The status poller
notices HQPlayer coming back — HQPlayer ended a connection since its last
answer (closed, reset or refused it: a restart drops the connection; a
reconnect of our own after a silence is no return), or the first answer
after failed polls (a power cycle, a restart that takes longer than one
poll; that second shape was missed until a live Pi reboot on 2026-09-27) —
reads the playlist at once and, when it differs from the queue, marks the
mirror lost; HQPlayer, back and stopped, takes the queue afresh at once (the
mirror's state, above), and the play press rebuilds whatever is left. An HQPlayer Embedded in trial mode
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
Sautium reads it since 2026-09-27 (`backend/hqp_library.py`): the library of
an HQPlayer on another machine comes in as album variants located at that
HQPlayer — see "The HQPlayer library as a source" below.

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

The compose files map the name already (`extra_hosts:
host.docker.internal:host-gateway`, all three of them).

### Finding HQPlayers (2026-09-28)
HQPlayer has a discovery protocol of its own: a `<discover/>` datagram to
its control port — UDP 4321 — is answered by Desktop and Embedded alike
with `<discover name="STUDIO-PC" result="NA" version="Signalyst HQPlayer
Desktop 6"/>` (what HQPlayer Client broadcasts for; verified 2026-09-28
against both). Sautium sends it unicast, so it crosses the docker bridge
like the DLNA sweep's M-SEARCH, and the reply comes back from 4321, which
the bridge's port-restricted NAT lets through (`hqp_library.discover`).
The Output picker's scan (`POST /api/player/outputs/scan`, on opening the
picker and on Rescan) asks every LAN address for a renderer and for an
HQPlayer in the same breath (`routers/player._unicast_sweep`), and lists
each HQPlayer it heard as its own entry beside the renderers, shaped like
every other row — "HQPlayer Desktop · STUDIO-PC / Online · this computer",
"HQPlayer Embedded / Online · `<lan-ip>`" (`hqp_library.label`: the
product and the name that tells two apart, a box's generic self-name
dropped; the state dot is the box's, the check is the selection; the order
never follows the selection). The dot's word is discovery AND control: an
Embedded past its trial half-hour still answers the datagram while its
control port closes at once, and reads "Not answering" whether or not it
is the selected output (the scan's `control` verdict, the selected row's
live check). A box whose link refuses the scan's question — it builds or
did not finish a build, answers nothing past a short silence, is lent to
the benchmark, or its link was let go — is busy, not gone: listed by its
registered row as answering, nothing asked of it.
HQPlayer OS is a UPnP renderer as well (manufacturer Signalyst), and so is a
Desktop: a box that answered as an HQPlayer is offered once, as the HQPlayer
— its renderer would be the same DSP with no queue and no filter control.
The match is by `address_key`, never the literal address (since 2026-09-28):
one box answers the datagram on the LAN address and the interface-bound
M-SEARCH on a virtual adapter's (WSL, Hyper-V), and every alias of this
machine is one key — by address the Desktop's renderer was a second row.
Tapping an entry
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
(`hqp_library.register`), one on this machine included — the row is the
device's identity; a LIBRARY is only what an HQPlayer on another machine
has (`has_own_library`, by address), and the sync refuses one on this
machine without touching its row. This machine's aliases — `localhost`,
`host.docker.internal`, its LAN address — are one HQPlayer
(`hqp_library.address_key`): every lookup by address finds the row another
alias registered, and the row keeps the address it was registered at (on a
Docker node `host.docker.internal` for a Desktop the scan sees at the LAN
address). A row is listed while its box is off, like a pinned renderer;
its `×` in the picker forgets it — the same confirmed goodbye Settings ›
Library offered before 2026-09-28, when the block moved (below). The
mini-player and the status name the selected one by the same label
(`HQPlayer Desktop · STUDIO-PC`).

### HQPlayer Embedded on the LAN
Open More → Audio output: the scan lists the box, or **Add device by
address** takes its LAN address (a Docker node cannot resolve `.local`
names; a DHCP reservation keeps the address stable) — that is all: a box is
not this machine, so it streams. The box fetches from the media proxy — on a Windows host
with Docker Desktop that is the same port forward and firewall allow the
DLNA output needs (CLAUDE.md Security Posture, rule 3); a launcher node
serves it natively on 8832. The web interface (port 8088, default login
`hqplayer` / `password`) is where the library is configured and scanned.
HQPlayer's own "Allow control from network" has to be on: discovery and
control from another machine both depend on it.

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
`file:///media/<volume>/…` straight from its own disk, no proxy, the play
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
since 2026-09-28 the row of every HQPlayer the owner picked, a Desktop's
included, see "Finding HQPlayers") and `album_variants.hqp_endpoint_id`
points at it: the same library answering from a new address after a DHCP
lease is recognised by its hash and the row moves; a different HQPlayer
that inherits the address (name or product differ) is a new library and the
old row keeps its files without an address. A friend's streamer brought over
is one more row, imported through the same previewed first import, and goes
with "Forget this library" on its HQPlayer screen or the `×` on its picker
row (`forget_endpoint_id`) — never on its own. The queue survives an output switch, and since 2026-09-28 so does its
playability: the canonical queue holds identities (the track uuid and the
enqueue-time origin, `QueueItem.source`, which no switch touches — since
2026-09-30 only a file leaving the library moves it to a live copy of the
track, or to the track itself), and on every
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
The library belongs to one HQPlayer, so its one block is at the bottom of
that HQPlayer's screen (the gear on the selected picker row; since
2026-09-28, before that a section of Settings › Library): what this node
holds of it, then chips — Import / Sync, Rescan, Forget this library,
Cancel while a job runs with its progress in the hint (the library wake
channel, in place). Used rarely, so it sits last; found anyway, because
the guidance trail (`hqp_library`: More tab → the HQPlayer drawer row → the
Import chip, with the puck) lights while the HQPlayer chosen as the output
runs on another machine and its library was never imported, and retires
once the block was shown, per address. A library of an HQPlayer that is not
the output is forgotten from its picker row (`×`) and syncs once that
HQPlayer is the output — the attach-time re-check follows the output alone. Only an HQPlayer on
ANOTHER machine has a library of its own — an Embedded box, a Desktop on a
second computer; one on this machine, whatever the product, reads this
node's music folder and is refused everywhere (`has_own_library`, by
address like the way files reach it — since 2026-09-28; the rule was
"Embedded only" for a day) — importing it brought every album in as a copy
once (2026-09-27) and was reverted with `python -m hqp_library
--forget-endpoint`. The FIRST import of a library is a previewed
decision (which HQPlayer, how many files it lists, how many of them new
here); after it the output re-checks
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

### Diagnostics — why didn't it play? (2026-10-02)

Every play intent on the HQPlayer output — play, a jump, next/previous, a
queue replaced with play, a rebuild that resumes, the resume after a DSP
change — is watched as one ATTEMPT (`backend/playback/hqp_diagnostics.py`, fed
by `hqp_backend`): what the slot was handed as (a path HQPlayer opens, a
stream from the media proxy, a CUE cut, a transcode, a preview, a copy in
HQPlayer's own library) with the PlaylistAdd's final answer, every command of
the intent with HQPlayer's answer (the link's client reports them through
`on_outcome`), the status for ten seconds after the first transport command
(state, track, position, speed, buffer fills, the DSP by name) — longer, up to
a minute, while HQPlayer is still making the start: it says it plays, or its
control port is silent, and the position has not moved (a long filter
initialises before any audio leaves: sinc-MGa at DSD256 on a laptop's CPU took
5–7 s, the port answering no Status meanwhile, 2026-10-03; judged at ten
seconds, those plays read `unknown`) — what the media
proxy saw for that file's token (each request's status, range and bytes —
`MediaProxy.hits`), and, read as it closes, HQPlayer's playlist: whether the
expected entry is in it and whose entry it plays. A stop, a pause, a DSP change
or a replaced playlist sent through the HQPlayer's link (the
Web UI, the HQPlayer screen, both assistants) ends an attempt as the owner's,
and so does a DSP change made anywhere — it shows in the status, and a stop
HQPlayer makes for its build (6.2.3 on Windows did; 6.2.4 plays on) is that
change, not a failure. A Stop from HQPlayer's own window or another
controller cannot be told from HQPlayer stopping by itself. Two intents may
overlap (a Next pressed while a replace still adds): each one's commands
stay its own steps. A step the link did not send — a newer intent came
first, the output was switched away or lent — ends its attempt as
superseded: interrupted, never "HQPlayer did not answer". The
last 20 are kept in memory (never in the database), and each gets a verdict,
checked in this order — positive evidence before any absence of it:

| Verdict | When | The owner's next step |
|---|---|---|
| `unreachable` | a command never reached HQPlayer (or the play gate's GetInfo got no answer) | start HQPlayer; an Embedded in trial mode stops every 30 minutes |
| `played` | `playing`, the position moving, on our entry (over http: its token was served) | — |
| `too_slow` | it played, but past start-up `process_speed` < 1 on three ticks in a row (one dip plays through), or the output buffer drained while the input held (`input_fill` -1 = a file, no input buffer) | a lighter filter or modulator, a lower rate (the setting is named) |
| `external` | HQPlayer plays an entry Sautium did not put there | stop the other controller (Roon?), play again here |
| `proxy_error` | the media proxy answered 404 (token not registered — the restart race), 500 (file gone, CUE cut failed), 502/504 (provider), 401/403 (a bug) | play again / check the music folder |
| `no_fetch` | an http hand-over HQPlayer never requested (none from the hand-over to 5 s after Play) | same LAN, the firewall for the media port, the Docker forward — host and port named |
| `rejected` | PlaylistAdd or Play refused (HQPlayer's words quoted; "Empty transport" = nothing was taken), or the expected entry missing from its playlist | the file must be readable here; a copy in HQPlayer's library: Rescan it |
| `rate_refused` | taken, then stopped at its start without moving, where the rate check (`backend/hqp_rate_check.py`) names why — the filter HQPlayer ran, by the ratio class HQPlayer describes it with; in SDM first the modulator, at a rate the atlas as published (the shipped file, its release dated) measured it refused for the track's family — or HQPlayer's log names a refused rate combination (its line quoted, attributed to nothing); judged at HQPlayer's first stop when the check names a cause — HQPlayer's own advance at its deadline, or at once on its log line here | a filter HQPlayer describes as Any, the adaptive output rate in HQPlayer's own settings, a rate of HQPlayer's list the tracks reach; another modulator or rate |
| `not_played` | taken (bytes served, or a path) but never `playing`, or it stopped again | HQPlayer's log, the DSP line |
| `unknown` / `interrupted` | nothing explains it / another action took over first (not counted) | HQPlayer's log |

The brief's order put the proxy before positive evidence and drift last;
measured traces show why not: a Next onto a track HQPlayer pre-buffered for
gapless sends no GET, a stream-mode PlaylistAdd refusal usually means the
add-time fetch could not reach us, and with another controller in charge our
file is never asked for. A failed attempt rides the status as `diagnosis`
while HQPlayer is not playing: one toast, a "Why didn't it play?" tag on Now
Playing beside that track, and a sheet with the sentence, the next step and
the raw trace; the HQPlayer screen lists the recent attempts. Once HQPlayer
plays that very slot — the position moving, the speed keeping up — the
failure is cleared and offered no more: before, the end of an album brought
back a failure from its start. The same failure three plays in a row is the
`hqplayer.failing` notice (a run ends on HQPlayer's first answer after an
`unreachable`, or on that slot playing). API: `GET /api/player/diagnostics/hqplayer` (attempts, the failing
run, `client_errors`), `…/hqplayer/{id}` (one whole trace), `…/hqplayer/log`.

HQPlayer's own advance is an attempt too (`advance`, since 2026-10-18).
Onto an entry of another rate HQPlayer stops on no entry at the end of the
one before — `track` 0, its `track_serial` one more (Desktop 6.2.4: one more
at each Play, a refused one included, and at each Next of its own, the
playlist's end too) — plays out its tail, then builds the next entry or
refuses it, and Status shows neither. So the attempt opens at that first
stopped tick when the tick before played an entry of ours in its last
seconds with one after it, no intent of ours watched and no mutation or play
press running, and is judged at its deadline: the first stopped tick + 2 ×
the last playing tick's `output_delay` + 11 s — the tail measured up to
1.71 × the delay (an output delay of 3.05 s: decided at +5.2 s, playing at
+6.2 s), the 11 s the longest build measured (a heavy SDM filter changed
mid-play, ten seconds); with no delay reported, 3.5 s, and the trace says
so. On this machine HQPlayer's log is followed meanwhile, and its refusal
line closes the attempt at once as `rate_refused`. The close at the first
stopped tick stays a Play's. An advance that played, or one another action
took over, is not kept; a start on the same take after the deadline (the
same `track_serial`) makes it `played` and clears its failure — a Play of
that entry is another take, an attempt of its own.

The rate check reads the adaptive output rate of an HQPlayer on this machine
from its `settings.xml`: the three-state box is two values of `<engine>`,
`adaptive_rate` and `auto_family` — `"0"`/`"0"` not checked, `"1"`/`"0"`
grayed, `"1"`/`"1"` checked (Desktop 6.2.4, observed 2026-10-18) — named as
the manual names them. Grayed or checked, HQPlayer picks the rate as it plays
(grayed took 705.6 kHz for a 44.1 kHz track under a 768 kHz default and a 2^x
filter), so the check says nothing; another pair, or an HQPlayer elsewhere,
reads as not known.

Before a play the check speaks on the HQPlayer screen (since 2026-10-18):
`GET /api/hqplayer/state?dsp=1` carries `rate_check`, `{"filter": […],
"modulator": […]}` — its phrases under the row each names — for the queue
from the slot HQPlayer reads (the first while nothing has played) to the
end, judged on that read's State and lists; there the node's own atlas copy
speaks too, the screen being the node's own. The key is absent with nothing
to say, while HQPlayer is not the output, and while another controller's
playlist plays (an entry not ours, a mirror that drifted). The screen draws
the phrases in the limiter line's colour, the filter's after its facts, the
modulator's under its row. While a track of such a queue that can play
plays, the same is said once per queue and setting as the notice
`hqplayer.rate_check`, its toast's Why? opening the HQPlayer screen: the
HQPlayer backend holds the check, judged again on the poller's tick after an
event — a queue change, a DSP change of this node's, HQPlayer coming back,
the attach, the slot read moving, a sampled State that differs — reading
State for a new queue, a DSP change, a return or the attach and the lists
when none are held, the mode moved or HQPlayer came back, never while a
mutation or a play press runs nor before a watched intent's first transport
command. A started track that cannot play speaks through its verdict alone.

**HQPlayer's own log** is read on demand — the Diagnostics screen, an
attempt that ends `not_played` / `unknown` / `rate_refused` / `rejected`
without HQPlayer's words, a support warrant — and followed only while an
advance attempt waits for its deadline, from the offset at its first stopped
tick, on this machine only: opened once per attempt by a thread of its own,
each status tick reads what was appended, and a log gone, cut or unreadable
leaves the attempt to its deadline with one line in Sautium's log; while an
earlier attempt's reader still hangs (an open on a mount that dropped), a
newer one starts none and has its deadline alone, as its trace says. A refused
rate combination it names judges a `not_played`, `unknown` or
`rate_refused` attempt again, in place: `rate_refused`, the line quoted. A verdict reads the
atlas only as the site published it — the shipped file, and of it only an
entry whose release has a date: a trace and the log travel in a support
warrant, and neither the node's own copy nor a file put at the shipped path
before its release may. The log line of a `rate_refused` verdict says the
cause's kind alone. Lines read `<mark> YYYY/MM/DD
HH:MM:SS <text>` on Desktop and Embedded alike, in HQPlayer's LOCAL time (the
Pi logs UTC, the Desktop here local time, the Docker node runs UTC), so an
attempt's lines are found by its URI, never by time — the cause is read from
its own add onward, and when no line names it, the newest cause in the tail
answers. Where it lives:

- **HQPlayer Desktop, Windows**: `%LOCALAPPDATA%\HQPlayer\HQPlayer6Desktop.log`
  (Desktop 6) and `HQPlayer5Desktop.log` (Desktop 5) side by side, named by
  the major version, beside `settings.xml`, whose `<log enabled="1"/>` turns it
  on (on by default on the maintainer's machine; hundreds of MB, so it is read
  backwards to the last 200 meaningful lines — the NAA discovery heartbeat,
  `clUPnP::OnRequest()` and startup listings are dropped).
- **HQPlayer Desktop, macOS**: data in `~/.hqplayer/`; with logging on the log
  is `HQPlayer6Desktop.log` beside `settings.xml` (Desktop 6, the test Mac,
  2026-10-18).
- **HQPlayer Embedded / HQPlayer OS**: `GET http://<box>:8088/log` — plain
  text, the whole log since hqplayerd started, **no login** (verified
  2026-10-02 on HQPlayer OS, Embedded 6.1.0 / engine 6.2.3; the other admin
  pages answer HTTP Digest).
- **HQPlayer Desktop on another computer**: no source from here; the sheet
  says where its log lives.

A Docker node reads the Desktop's folder through a read-only mount
(`HQPLAYER_DATA_DIR` in `.env` → `/hqplayer`; only `settings.xml` and
`HQPlayer*Desktop.log` are opened); the launcher reads its platform's own
place. Recognised causes — every pattern seen in the maintainer's Desktop 5/6
and Embedded logs, anything else shown raw: a file not found
(`AddURI(…): … CreateFile(): The system cannot find the path specified`), the
proxy's 404 (`AddURI(…): …GetHead(): 404`, `404 for range request`), a stream
type refused (`unknown mime type`), the media server unreachable
(`GetHead(): … socket error`), an empty playlist (`Empty transport`), the
device busy or missing (`snd_pcm_open()`, `ASIOInit()`), the NAA lost, a
rate combination refused (`Requested filter not possible with this rate
combination`, which names neither the filter nor the modulator), an output accepting no format
(`StartAudioClient(): no formats available`), a decode failure, a file that
would not open (`SetTransport(): failed`). `ReadFLACErrorCB(): lost sync`
alone is a transient of FLAC over HTTP, not a cause. No licence or trial line
appears in either installation's logs.

Traces and logs are content (paths, track identities): whole through the
local API, and in a support warrant only under the `playback` scope, asked
for by name, with every path cut to its last two components (drive, UNC and
any absolute POSIX path alike) and every media-proxy token to its first six
characters — a token is the capability that serves the file.

### DSP load — what this HQPlayer keeps up with (2026-10-02)

Whether a filter or a modulator keeps up depends on the HQPlayer host — the CPU
build, CUDA offload (filters can run on the GPU, modulators never do), memory,
cooling — so Sautium measures it instead of predicting it
(`backend/playback/hqp_load.py`, `hqp_benchmark.py`):

- **process_speed: observed** on Desktop 6.2.3 (i9-14900HX, RTX 4090, full CUDA
  offload) playing a 44.1 kHz FLAC at DSD256 with poly-sinc-gauss-xla and
  ASDM7ECv3: 2.50–2.64 from one second to the next (±3 % of jitter on a steady
  setting), `output_fill` 0.99–1.0, `input_fill` -1. What it means at the
  boundary is measured per host: the benchmark records the speed at which a
  point dropped out.
- **The build is the key.** `<GetInfo version="6"/>` is the product generation,
  `engine="6.2.3"` the build — filter names and costs hold for a build (5.15
  renamed poly-sinc-ext3, 6.1 added AHMxEC4B). Every answered GetInfo keeps
  version, engine and platform on the `hqp_endpoints` row, and for an HQPlayer
  on this machine the machine (CPU, threads, GPU, RAM as this node sees them)
  and the CUDA offload from its `settings.xml`, whose `<engine cuda>` keeps
  the three-state box: `"1"` fully checked (filters and convolution,
  observed 2026-10-02), the word `"convolution"` grayed (convolution only,
  2026-10-03), `"0"` clear; a value not seen yet reads as not known. For an
  HQPlayer elsewhere the owner says it once on the HQPlayer screen.
- **Listens leave samples** (the status poller): HQPlayer playing a slot of
  ours (never while another controller drives it), its speed and the source
  reported, 15 s into the track and 15 s past the last change of the setting
  or the source — a matrix profile or convolution switched under the same
  `<Status/>`, seen in the `<State/>` read with the sample, counts as one;
  then on every change, or once a minute. A sample is keyed by
  what HQPlayer reports it RUNS — `active_mode`, `active_rate`,
  `active_filter` (the 1x or Nx one the source needs), `active_shaper` — with
  the build, CUDA, matrix profile, convolution and the source's rate and
  channels: each changes the cost. Samples live 90 days (pruned at most once
  a day by the writer); `hqp_dsp_speed` is the per-key rollup (`n`, p10,
  median), refreshed in the same transaction from the key's newest benchmark
  point and every listen after it.
- **Headroom** on the HQPlayer screen: `GET /api/hqplayer/state?dsp=1` (the
  screen's own read; the More drawer and the Output picker's dot ask the plain
  state, a connection check) carries `headroom` — every filter at the current modulator and rate, every modulator
  at the current filter and rate, every rate at the current filter and
  modulator, for the source the owner hears (the one playing, else the file the
  queue plays next) — and each measured picker entry carries a dot and its
  speed: ok from 1.25×, tight from 1.0×, no under it. The boundaries are the
  host's own once a benchmark point dropped out there: "no" starts just above
  the fastest speed one dropped out at — every key of that build and mode
  whose newest benchmark point dropped out within 90 days, however its run
  ended (a run a trial-mode Embedded cut short at the half hour included),
  at the speed that point itself showed, never the key's p10 that later
  listens move. A key measured again without a dropout no longer counts.
  Nothing is blocked; an entry never measured is unmarked.
- **The benchmark** (`POST /api/hqplayer/benchmark`, `GET` for its state and
  the results, `…/cancel`): one mode — the one HQPlayer is in; the other only
  when asked (`{"mode": "pcm"}`) — and only the points nothing covers yet
  within 90 days: a benchmark sample or three listens of the key, or an
  earlier run's point that ASKED for it and ended in an answer
  (`hqp_benchmark_points`: measured, unsettled, dropped out, refused, never
  started on this host — not one that failed to run). A repeat run fills gaps: a combination HQPlayer refuses
  ("…512+fs" below 512×) and an adaptive output rate that plays another rate
  than the one asked are never samples of the asked key, and the ledger is
  what keeps them from being planned again. HQPlayer refuses a combination at
  the Set* command, or at Play: it stops at once ("Requested filter not
  possible with this rate combination 44100/49152000, stop" — sinc-MGa asked
  to take a 44.1 kHz source to 48k × 1024, 2026-10-03). A point stopped again
  after its one re-Play, its position never having moved, is refused in
  seconds, not waited out — the first run on that HQPlayer waited 60 s each
  and gave up after three — once a setting that started earlier in the run
  starts again: an output that went away stops every Play the same way, and
  then the run ends with nothing marked refused. A cancel wakes whatever the
  run waits on, a silent build included; putting HQPlayer back waits 5 s an
  answer and leaves a still-silent HQPlayer to the recovery, never resending.
  SDM: every modulator across the DSD rates with the owner's filter, every
  filter at the owner's rate with the owner's modulator — once the owner's
  own setting keeps up there: with one that does not, every filter measures
  the modulator (on a Pi 5, ASDM7EC-ul at DSD128 ran 0.26–0.30× under four
  filters from halfband to long-ip-2s), so the filters wait for a setting
  that does and the run says so; PCM: every filter across the rates, from
  44.1 and 96 kHz sources. A source
  plays at the rates of its own family — a whole power of two away, 44.1 kHz:
  88.2 kHz … 44.1k × 1024 — and at the owner's rate: another family takes an
  asynchronous conversion some filters refuse. A PCM source is never asked
  under its own rate: HQPlayer does not downsample PCM (Embedded 6.2.3, a
  96 kHz source at 48 kHz: "clHQPlayerEngine::Execute(): lInRate >
  lOutRate", then it reset itself and played another entry); a DSD source
  goes down to PCM rates, its conversion. Each modulator (PCM: filter)
  on a source is a ladder over its family's rates (Valerii, 2026-10-03): it
  starts at DSD256 (PCM: 8×, 352.8 or 384 kHz), climbs while the setting
  keeps up — at the host's "ok" boundary or above, no dropout — and goes
  under its start only while the rate above does not (a start HQPlayer
  refuses leaves it to the rungs above), that rung measured
  next, before anything else: a weak host shows what it does run first. A setting's speed falls as the rate rises (every ladder on a laptop:
  ASDM7ECv2 5.47× → 2.79× → 2.11× → 1.09× → 0.64× from 64× to 1024×), so a
  rate a neighbour answers for — above one that is tight or too slow, below
  one that keeps up — is never measured, during a run or by a later one. A
  refusal says nothing about speed and stops no climb: HQPlayer refuses the
  AHM modulators from 64× to 512× and runs them at 1024×. The owner's own
  setting goes first and last (the thermal drift check); between them the
  ladders' starts with every point of a single rate, then the steps by their
  distance from the start, down before up, each shuffled — the dropout
  boundary shows where the ladders stop. Until 2026-10-03 every modulator was measured at every rate,
  the highest first (the probe row). On Desktop 6.2.3 in
  SDM (77 filters, 36 modulators) a first run plans 149 points with four DSD
  rates of one family and 185 with the ten rates of both families an NAA
  output offers (221 and 262 before the ladders, 447 when every rate was
  measured), and asks fewer: a rung above one that does not keep up is
  passed over as the run goes. HQPlayer answering is the whole precondition
  of a start — not the backend's queue mirror: the attach may not have
  mirrored the queue (HQPlayer was not up yet), the run sets HQPlayer's
  playlist itself and the attach that takes the output back mirrors afresh;
  what holds a run back is HQPlayer playing an entry that is not in the
  queue, read off the status the start itself asks for
  (`HqpBackend.reads_foreign` — another controller). The run borrows the output
  (`PlaybackManager.hold`): it begins once a queue replace still adding
  tracks and a play intent still attaching are through, the link is lent and
  the backend detaches without a switch away — no Stop, the run stops
  HQPlayer itself; a command of that backend still on its way is refused
  from then on — every other Sautium path to HQPlayer answers 409
  (busy, not broken: the UI points at the run's Cancel, not at the Output
  picker), an output or address change included, and the queue mirrors back
  when it ends. HQPlayer goes to the bottom of its `VolumeRange` first, read
  back, when that lies below the owner's level (otherwise the owner confirms
  the amplifier is down); the signals are pink noise at about -20 dBFS,
  24-bit FLAC, 120 s, handed over as media proxy URLs — and the owner's last
  DSD file, while it is still there, only when the plan has DSD-source
  points. A point is Stop → its changed knobs → State read back (the
  selection the run set last is kept on its row) → SelectTrack, then
  `<Status/>` until it names the slot (2 s at most — a stopped HQPlayer plays
  the slot it is given; the read confirms which) → Play; another entry playing is selected
  once more. Its readings count after 5 s of moving position (counted again
  when the position stalls for two ticks before them), are accepted when the
  last five stay within ±2 % of their mean with no trend (a running average
  can be equal three times and still lag), and are recorded unsettled at
  15 s; the p10 is stored with the time to start and to settle. A dropout is
  the DSP falling behind and only that: once the readings run, the output
  under 0.1 for three ticks while the input holds, the position frozen for
  two, or the transport stopped, at a speed under 1.5×; before the readings
  (the buffer still filling, the average still holding the initialisation)
  the same signs count only under 1×. A tick counts only when HQPlayer
  refreshed its `<Status/>` since the last one: Desktop refreshes it every
  second, Embedded on a Pi 5 per output block — position (in steps of about
  one second of audio), speed and fills frozen together for up to 7.6 s at
  32× (engine 6.2.3, 2026-10-03). Read as a frozen position, that reset the
  count before the readings forever ("no verdict within 90 s" at every
  setting near or above real time) and failed a point at 9–32× once they ran;
  the same snapshot again is no news, and a frozen position is a refreshed
  status that did not move. An output that keeps no time — HQPlayer's ALSA
  null device, an Embedded with no DAC chosen — plays the signal out at the
  DSP's speed (120 s in five on a Pi 5 at 24×, 2026-10-04): the stable
  seconds count in audio as well as in time, and a slot played faster than
  twice real time that stops, or goes on to the next entry, ran out — its
  readings so far are the point, unsettled; three of them in a row ended
  runs as "did not play" until then. A dropout is recorded only once
  HQPlayer answers the next read: a trial-mode Embedded running out stops its
  transport and then drops every connection, and that stop is not the DSP —
  the point is made again or the run ends. Anything else that ends a point — a
  stop at a healthy speed, a stream that starved, a position that does not
  move 60 s after Play while HQPlayer answers, no verdict 90 s after Play —
  fails it, and a later run measures it again. HQPlayer answers nothing on
  its control port while it builds a setting — from the start until the
  build is done, on every connection: on a laptop (Desktop 6.2.3,
  2026-10-03) 10 to 27 s for sinc-MGa at 512× and 1024×, 56 s for
  poly-sinc-mp and 3 min 13 s for poly-sinc-long-lp at 44.1k × 256 — and
  carries out what it was sent meanwhile once it is free (Embedded does not:
  a command on a connection that closed meanwhile is dropped). Embedded on a
  Pi 5 answers all through a build instead — playing, position 0, speed 0,
  nothing out, for 15 to 70 s — and that time too is the build's, not held
  against the start (`_building`). It keeps a design
  it built: the same setting asked again starts in a second, so a slow
  first build is a start time (`init_s`), not a setting the host cannot
  run. A point starts at the SelectTrack (a stopped HQPlayer plays the slot
  it is given — "GoTo 1", then "Play (1/0)" in its log), and from there the
  run asks for `<Status/>` again on a new connection every 45 s until
  HQPlayer answers, then watches the point on — the silence is not held
  against its start and point deadlines. The second live run gave up after
  10 s instead and set HQPlayer up again: its Stop arrived right after the
  build and stopped what had just been built, and a build it interrupted
  failed inside HQPlayer (`ThreadPoolCreate(): lThreadCntr != 0`, `Stop
  request (reset)`). A point still building after 10 minutes never started
  on this host (`unstarted`): not asked again by a later run, which would
  only take the machine back where the build took it (the second run met
  HQPlayer grown to 38 GB on a 32 GB laptop, swapping); its ladder steps
  down from it, and three points in a row that do not play stop the run.
  Kept for good, that mark waits for a control: the first setting the run
  saw start is played again, and only if it starts (within three times its
  first start, a minute at least) is the point recorded — otherwise HQPlayer
  stopped starting anything and the run ends with nothing marked. An
  Embedded whose output engine no longer came up after its stream had fed
  it one file over and over (2026-10-03) left a light filter "never
  started"; a DAC switched off mid-run would do the same. Before anything
  in the run has started there is no control: the point failed instead,
  asked again by the next run, and three in a row end it — an engine hung
  before the run would otherwise have marked the owner's own setting, and an
  owner's setting HQPlayer stops at Play from the 44.1 kHz signal would have
  ended every run at its first point. A silence after which HQPlayer
  answers stopped is a reset or a build that stopped: the point is made
  again, never a dropout at the speed it showed last; a silence while it
  played starts the point's reading afresh, its drained output then ticks. On HQPlayer's ALSA null device the lightest
  filters (`none`, polynomial, IIR) make Embedded's stream reader cut its
  stream short, resume it, seek past its end ("416 for range request",
  `FLAC__STREAM_DECODER_SEEK_ERROR`) and stop — after which its output
  engine may not start again until HQPlayer restarts (2026-10-04): a DAC
  as its output is what the benchmark should run against.
  An HQPlayer silent that long is taken for hung, and the run ends there. A
  setting far below real time can take a weak host down with it: on a Pi 5,
  poly-sinc-long-lp with ASDM7EC-ul at DSD128 left HQPlayer playing it after
  a cancel, its control port answering nothing for minutes, until it was
  restarted (2026-10-03). A run does not start while Sautium still loads
  its models on the same computer (the startup pre-warm included: a run
  begun a minute after a restart measured the owner's own setting at 1.73×
  under the translation model's CPU load, 2.18× after). The run's
  connection reconnects when HQPlayer drops it (at once, then after 1, 2, 4
  and 8 s): HQPlayer may have restarted under the run, so the volume is
  lowered and read back and the signals loaded again before the point plays
  again. Everything is put back at the end, on cancel,
  on failure and when the app stops (it waits up to six seconds); a run cut
  short before that — the process died, HQPlayer stayed away or silent — is
  put back the next time HQPlayer answers, at the attach or at the poller's
  next answer, while HQPlayer still shows a mark of the run: its signals, the
  lowered volume, or the selection it set last (an HQPlayer that restarted
  keeps that, maybe nothing else) — only once HQPlayer answers, never on a
  trial-stopped box that takes a connection and drops it. A run HQPlayer
  went away under — it closed, reset or refused the connection as the
  put-back at the end tried it, the app not stopping (`lost_at`, counted
  as it happens, a silence after it included; a silence alone is no loss
  — a busy HQPlayer's run is one cut short like any other) — is put back
  whole while HQPlayer shows what a box that just booted shows: stopped,
  reading no entry of another controller's — none, a source of the run (a
  signal, the owner's DSD file it measured, known by the URI the run handed
  — `dsd_uri`, migration 038), or one this node gave it, a
  stream at its own media proxy of an earlier session included (since
  2026-10-14). What it booted
  with is no one's change; playing, pausing or reading another
  controller's entry, it shows more, and the run is judged as any run cut
  short — nothing stopped, nothing reset over another controller's
  playback. Its volume is lowered to the owner's level when HQPlayer came
  back louder, raised only from the level the run set itself, and
  otherwise left as it is; one notice (`hqplayer.run_restored`) says
  Sautium lost the connection to HQPlayer during the run of that date —
  the date keeps a put-back days later from reading as a change made on
  its own — and that the settings from before it were put back, with the
  level the volume was left at beside the owner's, until the owner saw it
  or a new run opened its row. The put-back is a
  DSP item of the HQPlayer's link as of the run's end (one made afresh
  owes it anew, as it does after the process restarted — an item kept in
  memory — unless a person superseded it on the link it was owed on): a
  DSP change the owner makes first, their switch to another output or to
  another HQPlayer supersedes it — written in the run's row where it
  happens, so no later answer and no restart puts it back
  (`hqp_benchmark.left_by_person`) — and so does any knob the owner set in
  HQPlayer's own window since: judged knob by knob (mode, rate, filter,
  modulator, volume) on HQPlayer's State as it answers it (a Desktop says
  nothing while it builds, an Embedded box names the selection it builds),
  one that is neither the run's last point nor what was there before the
  run is theirs, whatever the others still hold — for a run HQPlayer went
  away under, only once it shows more than what it booted with. The mode
  the run switched into, which State does not show while HQPlayer is in
  another, is judged the same way: entered and read, its selection put back
  from the run's last point or left as the owner's, then HQPlayer back in
  the mode it was found in, that mode's selection read again — but only on
  an HQPlayer at rest (stopped, reading no other controller's entry), since
  switching its mode restarts its output. Busy, the run is marked
  (`measured_unchecked_at`, migration 039) and a notice says the mode is not
  checked yet; it is judged at HQPlayer's nearest rest, or at a new run's
  start, at the rest the run's own Stop makes, whatever entry HQPlayer
  stopped on — the start is the owner's own act, and the run takes HQPlayer
  over (2026-10-15), before that run reads the owner's selection — a test
  point is never taken for theirs (since 2026-10-14). A selection goes back
  only while HQPlayer shows its mode, and one HQPlayer will not take back is
  no verdict: the mode stays unchecked, the refusal said (2026-10-15) — on a
  run of the mode HQPlayer was in too, the owner's selection there the one
  the run found (2026-10-16). Each rest tries once; HQPlayer at rest that
  does not let the mode be read (a SetMode refused or not shown, no State
  there, a selection or the way back refused, the lists of the mode it was
  found in not given) is tried again only at a new run's start
  (`measured_refused_at`, migration 040), and one that still does not
  refuses only a run that would read that mode — a run of it, or HQPlayer
  found in it — while a run of another mode goes, the mark standing
  (2026-10-15). Before a check leaves the mode it found, that mode and its
  selection by name go on the run's row as the return point
  (`measured_from_*`, migration 041): the next check — a put-back's own too
  — or run start takes HQPlayer back there first, on a stopped HQPlayer, and
  only then judges — after a node restart too — and a run never reads the
  mode Sautium left HQPlayer in as the owner's: when HQPlayer does not go
  back, the run is refused. The point goes once HQPlayer is back there — the
  check's own way back, a return, or the owner's own switch, which the
  poller sees in State — or once a person's change makes HQPlayer theirs
  (2026-10-15). Neither the put-back nor a person's change waiting for the
  command lock behind it closes the run the other way: the row is closed
  while the put-back still holds the lock, and a switch away or another
  HQPlayer chosen meanwhile — accepted at once, without the lock — leaves
  the run to the put-back's own end: it went; or the switch drops its next
  request, wakes the one on its way or leaves it reading no state, and the
  run is left to the owner. Never the node's own re-attach of the same
  HQPlayer, nor a new run's hold. The run is then closed as left to the
  owner (since 2026-10-13): nothing more is put back, and the volume is
  never raised for them — save one HQPlayer still holds at the level the run
  lowered it to when a knob of theirs leaves the run to them: that level is
  the run's, and goes back to theirs (since 2026-10-14) — and one notice —
  through the notices channel and on the HQPlayer screen — names what still
  holds the run's test values, each as now → before the run (named from
  HQPlayer's lists of the mode the run measured, read in that mode — a list
  HQPlayer did not give is asked again at its next answer there), and a
  volume left at the bottom of the range for them to raise, until they
  changed it all or a new run opened its row. In another mode State shows
  that mode's selection, and HQPlayer keeps one per mode: the notice names
  the knobs the measured mode still keeps by that mode's name, as they were
  last read there, until HQPlayer is read in it again. The poller reads
  HQPlayer's State for it while it stands; a write, a read or a wake the
  database fails costs the notice — done again at a later answer — never the
  poll, and a read a close or a new run overtook is read again. The notice
  stands on the run's row, whatever the output is now: a restart or another
  output never ends it to raise it again; until the poller has read HQPlayer
  it says the test settings may still be there. A new run takes a put-back
  still owed first — at its start, and again as its first step — and reads
  what HQPlayer holds after it; the run it took is closed then with no
  notice, however it was put back or left — the new run reads HQPlayer as
  theirs — and a mode it leaves unchecked is said only once no run will
  judge it: the start refused after all, or the run over before it got there
  (2026-10-15). A run of the mode HQPlayer is in takes that mode from what
  it reads at its own rest, not from the start's read: a check of ours may
  hold HQPlayer in the other mode for a moment, or have left it there for
  the run to take it back first (2026-10-15). Forgetting the HQPlayer takes
  its measurements with it; it is refused while a run measures it. The
  estimate of a run's length takes the pace of the last run that ended or
  was cancelled (on a trial-mode Embedded the owner cancels before the half
  hour is up and runs again: a repeat run measures only the gaps). Without
  an estimate the block says why (`no_estimate`, since 2026-10-11): the
  HQPlayer is not registered yet, it did not report its state, a list a run
  is planned from is missing, or its mode follows the source.

Measurements stay on this node: in the `.sbk` backup, not in the life-data
merge (their endpoint is this node's registry), not in P2P sync.

### Windows Firewall
Ensure port 4321 is accessible:
1. Open Windows Firewall settings
2. Allow inbound TCP 4321 (control) and UDP 4321 (HQPlayer's discovery
   datagram — without it the picker's scan does not see this HQPlayer, and
   it goes in through Add device by address), and TCP 4322 (the peak meter's
   stream — without it the meter says the port gives no answer)
3. Verify with: `nc -zv <windows-host-ip> 4321` from WSL

## Testing

Check the port from WSL (`nc -zv <windows-host-ip> 4321`), make HQPlayer the
output (More → Audio output), then use the assistant tools against the
running instance: `hqplayer_get_settings` (product, version, engine, the DSP
lists), `hqplayer_get_status` (playback state, track, position, volume),
`hqplayer_set_filter`, `hqplayer_play` / `hqplayer_pause` — they refuse
while another output is selected. `GET /api/hqplayer/state` reports the
connection state (`connected`), and so does the HQPlayer row of the More
drawer.

## Known Limitations

### Not Implemented Yet
- 🔒 **Authentication** - Advanced security features (ECDH + Ed25519)
- 🔒 **Encrypted Commands** - ChaCha20Poly1305 encryption for file paths
- 💾 **Playlist Load/Save** - Saved playlists management
- 🖼️ **Album Art** - Cover art retrieval
- 🔊 **Output Device Selection** - Not available in API (configure in GUI)

### Workarounds
- **Authentication**: Not required for basic playback control
- **File paths**: Using unencrypted URIs works fine on local network

## Shipped since the first iteration

- ✅ Playback control, status monitoring, DSP settings (filters, shapers,
  modes, rates), matrix profiles, convolution and parametric-EQ presets.
- ✅ Queue ownership moved to Sautium: `backend/playback/` holds the
  canonical queue and mirrors it into HQPlayer's playlist, so HQPlayer is
  one output among several (DLNA, browser, local) rather than the only one.
- ✅ Natural-language control through the assistant's `hqplayer_*` MCP tools
  ("what's playing", "play something similar", filter changes).
- ✅ HQPlayer Embedded on the LAN, fed by streams from the media proxy
  (2026-09-27); an HQPlayer's own library imported as album variants
  (`hqp_library.sync`, 2026-09-27) — the library is not browsed as HQPlayer's
  tree, it joins the catalogue; HQPlayers found by the network scan and
  picked in the Output picker (2026-09-28).
- ✅ The peak meter on the HQPlayer screen (2026-10-07, § "The peak meter").

## Future Enhancements

### Meters for the other outputs

The peak meter reads HQPlayer's own meter stream. The other outputs hold
their signal elsewhere: the local engine in its ring buffer (the PortAudio
callback already holds `int32` frames — a peak/RMS accumulator read from the
other side, which must not slow the RT callback), the browser in the page
(an `AnalyserNode` over a `MediaElementAudioSourceNode`; media is
same-origin, so Web Audio is allowed). DLNA has no signal to meter — the
renderer is across the network and GENA carries transport state — so it
shows no meter, by the rule that hides seek on an output that cannot seek
(POSITIONING §9). Ballistics stay rendering, applied once above whichever
source supplies the numbers.

### Other

- **HQPlayer playlist load-save** — not needed while Sautium owns the queue.

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
3. A refused command is quoted in HQPlayer's words — in the toast, in the
   HQPlayer screen's Diagnostics, in `GET /api/player/diagnostics/hqplayer`
   (`client_errors`)
4. Try reconnecting

### Docker Connection Issues

**Solutions**:
1. Check that `extra_hosts` is still in the compose file you run (all three
   ship it)
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
    # states the SDK does not name stay their number (Embedded 6.2.3 said 5)

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
    convolution: bool
    matrix_profile: str
    process_speed: Optional[float]  # since 5.17.0; None before
    input_fill: Optional[float]   # None when not reported
    output_fill: Optional[float]
    tracks_total: int
    active_mode: str
    active_filter: str
    active_shaper: str
    active_rate: int
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
