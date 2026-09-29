# HQPlayer Integration - Quick Start

## ✅ Ready to use

The HQPlayer integration is implemented and tested. The same client drives
HQPlayer Desktop 5 and 6 and HQPlayer Embedded 6 — one control protocol.

## Quick test

### 1. Make sure HQPlayer is running
- Start HQPlayer Desktop 5 or 6 on this computer, or power the HQPlayer
  Embedded box on the LAN
- Turn on "Allow control from network" in HQPlayer when it runs on another
  machine
- Confirm it is up (port 4321 open)

### 2. Check the connection from WSL
```bash
nc -zv <windows-host-ip> 4321
```

### 3. Pick it in the Web UI
**More → Audio output** lists every HQPlayer the network scan finds; tap one
and it is the output. The HQPlayer row of the More drawer then shows whether
it answers, the assistant tool `hqplayer_get_settings` returns product,
version and engine, and `hqplayer_get_status` the playback state.

### 4. Using it from code

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

HQPlayer is chosen in the Web UI, not in a file (since 2026-09-28): **More →
Audio output** lists every HQPlayer the scan finds, **Add device by
address** takes one the scan cannot see (another subnet, a box that was
off), and tapping a row makes it the output. There is no connection form.
How files reach it follows from its address: by path on this machine,
streamed from the media proxy anywhere else.

`HQPLAYER_HOST` / `HQPLAYER_PORT` in `.env` are only the address a node
starts from before anything was picked (the compose files default to
`host.docker.internal:4321`); a pick in the Web UI is stored in the
database and wins. `HQPLAYER_ENABLED` does not make HQPlayer the output —
the pick does.

## Capabilities

✅ **Playback control**
- play, pause, stop
- next, previous
- seek

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

✅ **DSP**
- modes, filters, shapers, rates (discovered at runtime)
- matrix profiles; convolution through the assistant
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
1. Check that `extra_hosts` is in the compose file you run (all three ship
   it)
2. Pick the HQPlayer again in More → Audio output, or add it by address
   (`host.docker.internal`, or `<windows-host-ip>`)

## Next steps

1. ✅ Basic integration — **DONE**
2. ✅ AI assistant integration — **DONE** (the `hqplayer_*` MCP tools; the
   assistant reads status and drives transport and DSP)
3. ✅ DSP settings, matrix profiles, convolution, EQ presets — **DONE**
4. ✅ HQPlayer is one output among several — **DONE** (`backend/playback/`:
   HQPlayer, DLNA, browser, local; one canonical queue mirrors into HQP)
5. ✅ HQPlayer Embedded, the HQPlayer's own library as a catalogue source,
   HQPlayers found by the network scan — **DONE** (2026-09-27 / 28)
6. ⏳ Real-time metering (port 4322) — not started

## Full documentation

Detailed docs: [docs/HQPLAYER_INTEGRATION.md](docs/HQPLAYER_INTEGRATION.md)

---

**Status**: ✅ Ready to use
**Tested with**: HQPlayer Desktop 6 — the daily configuration as of
2026-09-21; Desktop 5.16.3 (Engine 5.34.14); HQPlayer Embedded 6 (engine
6.2.3, HQPlayer OS on a Raspberry Pi 5) verified 2026-09-27 — one client,
one protocol
**Platform**: Desktop on Windows (reachable from WSL2 and Docker); Embedded
on a box on the LAN
