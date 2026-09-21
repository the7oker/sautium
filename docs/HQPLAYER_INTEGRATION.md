# HQPlayer Desktop 5 / 6 Integration

## Overview

Integration with the HQPlayer Desktop Control API for playback control and status
monitoring. Supports **HQPlayer Desktop 5 and 6** — the XML-over-TCP control protocol is
forward-compatible between the two releases, and Sautium discovers all DSP options
(filters, shapers, modes, rates) dynamically at runtime, so the same code drives either
version with no version switch.

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

**Note**: For Docker, you may need to add to docker-compose.yml:
```yaml
backend:
  extra_hosts:
    - "host.docker.internal:host-gateway"
```

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
- 📁 **Library Browse** - HQPlayer library browsing
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
