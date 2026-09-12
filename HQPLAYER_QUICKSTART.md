# HQPlayer Integration - Quick Start

## ✅ Ready to use

The basic HQPlayer Desktop 5 integration is implemented and tested.

## Quick test

### 1. Make sure HQPlayer is running on Windows
- Start HQPlayer Desktop 5
- Confirm it is up (port 4321 open)

### 2. Run the automated test from WSL
```bash
cd <repo>/backend
python3 test_hqplayer_auto.py
```

Expected output:
```
✅ All tests completed successfully!

📋 Summary:
   • HQPlayer is accessible at <windows-host-ip>:4321
   • Version: 5 / Engine: 5.34.14
   • Control API working correctly
```

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

## Files

```
backend/
  ├── hqplayer_client.py          # The client
  ├── test_hqplayer_auto.py       # Automated test
  └── test_hqplayer.py            # Interactive test

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
2. ⏳ AI assistant integration (recommendations → HQPlayer)
3. ⏳ Voice control (Phase 4.3)
4. ⏳ Extra features (metering, DSP settings)

## Full documentation

Detailed docs: [docs/HQPLAYER_INTEGRATION.md](docs/HQPLAYER_INTEGRATION.md)

---

**Status**: ✅ Ready to use
**Tested with**: HQPlayer Desktop 5.16.3 (Engine 5.34.14)
**Platform**: Windows (reachable from WSL2 and Docker)
