# Sautium — Documentation

## Layout

### 📘 Core documents

- **[HQPLAYER_INTEGRATION.md](HQPLAYER_INTEGRATION.md)** — technical
  documentation of the HQPlayer integration
  - API reference
  - Usage examples
  - Troubleshooting
  - ~60 KB, the complete picture

- **[HQPLAYER_KNOWLEDGE_BASE.md](HQPLAYER_KNOWLEDGE_BASE.md)** — knowledge base
  for the AI agent
  - Distilled from the official manual
  - Every DSP setting explained
  - Recommendations per scenario
  - Algorithms for choosing settings automatically
  - ~18 KB of structured information

### 📖 Original manual

- **[hqplayer5desktop-manual.pdf](hqplayer5desktop-manual.pdf)** — the official
  HQPlayer 5 Desktop v5.16.0 manual
  - 63 pages of complete documentation
  - Detailed descriptions of every function
  - Technical specifications

### 🚀 Quick-start guides

- **[../HQPLAYER_QUICKSTART.md](../HQPLAYER_QUICKSTART.md)** — quick start
  - Basic instructions
  - First steps
  - Fast smoke test

- **[../DSP_CONTROLS_SUMMARY.md](../DSP_CONTROLS_SUMMARY.md)** — DSP control
  summary
  - Practical examples
  - Every available setting
  - Code snippets

## Use by the AI agent

### Context for understanding HQPlayer

The agent has access to:

1. **Technical specifications** (HQPLAYER_INTEGRATION.md)
   - How to connect
   - Which commands exist
   - How to test

2. **Audio-processing knowledge** (HQPLAYER_KNOWLEDGE_BASE.md)
   - What PCM/DSD are
   - Which filter serves which purpose
   - How to choose settings

3. **The original documentation** (PDF manual)
   - Detailed technical descriptions
   - Algorithm specifications

### Recommended reading order

1. **First**: HQPLAYER_QUICKSTART.md (quick orientation)
2. **Then**: HQPLAYER_KNOWLEDGE_BASE.md (detailed knowledge)
3. **If needed**: HQPLAYER_INTEGRATION.md (technical implementation)
4. **For reference**: hqplayer5desktop-manual.pdf (the full documentation)

## Key concepts

### Operating modes
- **[source]** — no processing
- **PCM** — upsampling to high PCM rates
- **SDM (DSD)** — conversion to DSD

### DSP pipeline
```
Source → Filter → Modulator/Shaper → Output
         (upsampling)  (noise shaping)
```

### Automatic setting selection

The agent can pick optimal settings from:
- Source quality (sample rate, bit depth)
- DAC type (when known)
- Musical genre
- CPU headroom

**Example:**
```
Hi-res FLAC (192 kHz/24-bit) + R2R DAC
→ PCM mode
→ poly-sinc-ext2 filter
→ 768 kHz output
→ LNS15 noise shaping
```

## Practical use

### Scenarios

1. **Basic playback**
   - Add a track to the playlist
   - Play
   - Volume control

2. **Quality optimization**
   - Identify the source type
   - Pick the right mode
   - Configure filters

3. **Voice control** (future)
   - "Set the best quality"
   - "Switch to DSD mode"
   - "Adapt to this track"

## Integration with Sautium

### Capabilities

- ✅ Automatic setting selection based on the track
- ✅ Profiles per genre
- ✅ Optimization for a specific DAC
- ✅ Voice control (Phase 4)

### Integration example

```python
from hqplayer_client import HQPlayerConnection
from database import get_db_context
from models import Track

def play_track_optimized(track_id: int):
    """Play a track with automatic HQPlayer optimization."""
    with get_db_context() as db:
        track = db.query(Track).get(track_id)

        # Work out the optimal settings
        settings = auto_select_hqplayer_settings(track)

        with HQPlayerConnection() as hqp:
            # Configure HQPlayer
            hqp.set_mode(settings['mode'])
            hqp.set_filter(settings['filter'])
            hqp.set_rate(settings['rate'])

            # Play
            hqp.playlist_add(track.file_path, clear=True)
            hqp.play()
```

## Keeping the documentation current

When a new HQPlayer version appears:
1. Update hqplayer5desktop-manual.pdf
2. Review HQPLAYER_KNOWLEDGE_BASE.md
3. Add new functions to HQPLAYER_INTEGRATION.md
4. Refresh the code examples

## Contributing

When adding information:
- Keep the structure
- Add examples
- Check that it is still accurate
- Update version numbers

---

**Documentation status:** ✅ Current
**HQPlayer version:** 5.16.3 (Engine 5.34.14)
**Last updated:** 2026-02-12
