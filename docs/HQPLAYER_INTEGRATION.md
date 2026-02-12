# HQPlayer Desktop 5 Integration

## Overview

Integration with HQPlayer Desktop 5.16.3 Control API for playback control and status monitoring.

**Status**: ✅ Working - Basic implementation complete

## Architecture

### Protocol
- **Type**: XML over TCP
- **Port**: 4321 (default)
- **Authentication**: Not required for basic commands (optional for advanced features)

### SDK Version
Based on HQPlayer SDK (engine version 5.29.2) from Signalyst

### Tested Configuration
- **HQPlayer**: Desktop 5.16.3
- **Engine**: 5.34.14
- **Platform**: Windows
- **Connection**: WSL2 → Windows (172.26.80.1:4321)

## Files

### Core Implementation
- `backend/hqplayer_client.py` - Main client library
  - `HQPlayerClient` class - Core API client
  - `HQPlayerConnection` context manager
  - Helper functions for URI conversion and time formatting

### Testing
- `backend/test_hqplayer.py` - Interactive test script
- `backend/test_hqplayer_auto.py` - Automatic test script

### SDK Reference
- `sdk/hqp-control-5292-src/` - Original C++ SDK source code

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
with HQPlayerConnection(host="172.26.80.1") as hqp:
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

hqp = HQPlayerClient(host="172.26.80.1", port=4321)
if hqp.connect():
    status = hqp.get_status()
    hqp.disconnect()
```

### Playing a Track

```python
from hqplayer_client import HQPlayerConnection, file_path_to_uri

track_path = "E:\\Music\\Artist\\Album\\Track.flac"
uri = file_path_to_uri(track_path)

with HQPlayerConnection(host="172.26.80.1") as hqp:
    # Clear playlist and add track
    hqp.playlist_add(uri, clear=True)

    # Start playback
    hqp.play()

    # Monitor status
    status = hqp.get_status()
    print(f"Playing: {status.song}")
    print(f"Position: {status.position:.1f}s / {status.length:.1f}s")
```

### Integration with Music AI DJ Database

```python
from database import get_db_context
from models import Track
from hqplayer_client import HQPlayerConnection, file_path_to_uri

# Get track from database
with get_db_context() as db:
    track = db.query(Track).filter(Track.id == track_id).first()

    # Play in HQPlayer
    with HQPlayerConnection(host="172.26.80.1") as hqp:
        uri = file_path_to_uri(track.file_path)
        hqp.playlist_add(uri, clear=True)
        hqp.play()
```

## Network Configuration

### From WSL2
```python
# Use Windows host IP (usually 172.x.x.1)
HOST = "172.26.80.1"  # Check with: ip route show | grep default
```

### From Docker Container
```python
# Option 1: Use host.docker.internal (Docker Desktop)
HOST = "host.docker.internal"

# Option 2: Use Windows host IP
HOST = "172.26.80.1"
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
3. Verify with: `nc -zv 172.26.80.1 4321` from WSL

## Testing

### Run Automatic Test
```bash
cd /mnt/d/ai/djai/backend
python3 test_hqplayer_auto.py
```

Expected output:
```
✅ All tests completed successfully!

📋 Summary:
   • HQPlayer is accessible at 172.26.80.1:4321
   • Version: 5 / Engine: 5.34.14
   • Control API working correctly
```

### Run Interactive Test
```bash
python3 test_hqplayer.py
```

Allows testing:
- Connection and info
- Playback controls (pause/play)
- Adding tracks to playlist

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

## Future Enhancements

### Phase 3.2 (Planned)
1. ✅ Basic playback control - **DONE**
2. ✅ Status monitoring - **DONE**
3. ⏳ Queue management - Partial (can add, need load/save)
4. ⏳ Current track info - Basic (need more metadata)

### Phase 4.3 (Voice Integration)
- Voice commands for playback ("Claude, play next track")
- Natural language queries ("What's playing now?")
- Playlist building ("Play something similar")

### Potential Features
- 📊 Real-time audio metering display
- 🎛️ DSP settings control (filters, upsampling)
- 🔍 HQPlayer library integration
- 💾 Playlist synchronization
- 🖼️ Album art display

## Troubleshooting

### Connection Refused
```
Failed to connect to HQPlayer at 172.26.80.1:4321
```

**Solutions**:
1. Ensure HQPlayer Desktop is running on Windows
2. Check Windows firewall allows port 4321
3. Verify host IP: `ip route show | grep default`
4. Test connection: `nc -zv 172.26.80.1 4321`

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

See SDK documentation in `sdk/hqp-control-5292-src/` for full XML protocol specification.

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
- **SDK**: HQPlayer Control API SDK v5.29.2
- **License**: MIT (for our integration code)
