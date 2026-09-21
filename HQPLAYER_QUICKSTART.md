# HQPlayer Integration - Quick Start

## ✅ Ready to use

The HQPlayer integration is implemented and tested. The same client drives
HQPlayer Desktop 5 and 6 — the control protocol is forward-compatible.

## Quick test

### 1. Make sure HQPlayer is running on Windows
- Start HQPlayer Desktop 5 or 6
- Confirm it is up (port 4321 open)

### 2. Check the connection from WSL
```bash
nc -zv <windows-host-ip> 4321
```
The backend's `/health` then reports the HQPlayer state, and the assistant
tool `hqplayer_get_status` returns version, engine and playback state.

### 3. Using it from code

```python
from hqplayer_client import HQPlayerConnection, file_path_to_uri
from config import settings

# Connect to HQPlayer
with HQPlayerConnection(host=settings.hqplayer_host) as hqp:
    # Read the status
    status = hqp.get_status()
    print(f"State: {status.state.name}")

    # Add a track
    uri = file_path_to_uri("E:\\Music\\Artist\\Album\\Track.flac")
    hqp.playlist_add(uri, clear=True)

    # Play
    hqp.play()

    # Volume control
    hqp.volume_up()
```

## Configuration

### .env file
```env
HQPLAYER_HOST=<windows-host-ip>  # Windows host IP as seen from WSL
HQPLAYER_PORT=4321
HQPLAYER_ENABLED=true
```

### For Docker
Set in `.env`:
```env
HQPLAYER_HOST=host.docker.internal
```

## Capabilities

✅ **Playback control**
- play, pause, stop
- next, previous
- seek, forward, backward

✅ **Playlist**
- playlist_add
- playlist_clear
- playlist_remove

✅ **Status**
- get_status (track, position, metadata)
- get_info (HQPlayer version)

✅ **Volume**
- set_volume
- volume_up, volume_down
- volume_mute

✅ **DSP**
- modes, filters, shapers, rates (discovered at runtime)
- matrix profiles, convolution
- parametric-EQ preset generation (`generate_eq_preset`)

## Files

```
backend/
  └── hqplayer_client.py          # The client

docs/
  └── HQPLAYER_INTEGRATION.md     # Full documentation
```

## Checking connectivity

### From WSL
```bash
# Find the Windows host IP
ip route show | grep default
# Output: default via <windows-host-ip> ...

# Check the port is reachable
nc -zv <windows-host-ip> 4321
# Output: Connection to <windows-host-ip> 4321 port [tcp/*] succeeded!
```

### From Docker (once the container is running)
```bash
docker exec sautium-backend nc -zv host.docker.internal 4321
```

## Troubleshooting

### Connection refused
1. Make sure HQPlayer is running
2. Check Windows Firewall (port 4321)
3. Check the host IP: `ip route show | grep default`

### No connection from Docker
1. Add `extra_hosts` to docker-compose.yml (already there)
2. Use `host.docker.internal` for HQPLAYER_HOST
3. Or pin the address: `<windows-host-ip>`

## Next steps

1. ✅ Basic integration — **DONE**
2. ✅ AI assistant integration — **DONE** (the `hqplayer_*` MCP tools; the
   assistant reads status and drives transport and DSP)
3. ✅ DSP settings, matrix profiles, convolution, EQ presets — **DONE**
4. ✅ HQPlayer is one output among several — **DONE** (`backend/playback/`:
   HQPlayer, DLNA, browser, local; one canonical queue mirrors into HQP)
5. ⏳ Real-time metering (port 4322) — not started

## Full documentation

Detailed docs: [docs/HQPLAYER_INTEGRATION.md](docs/HQPLAYER_INTEGRATION.md)

---

**Status**: ✅ Ready to use
**Tested with**: HQPlayer Desktop 5.16.3 (Engine 5.34.14); HQPlayer 6 speaks
the same protocol
**Platform**: Windows (reachable from WSL2 and Docker)
