# Sautium — Documentation

## Layout

### 📐 Design & architecture

- **[design/POSITIONING.md](design/POSITIONING.md)** — product positioning,
  audience, tone, design principles and the palette (source of truth)
- **[design/INFORMATION-ARCHITECTURE.md](design/INFORMATION-ARCHITECTURE.md)**
  — navigation model, screen inventory, state flows, layout modes
- **[design/DISCOVERY-SEARCH-ENGINE.md](design/DISCOVERY-SEARCH-ENGINE.md)** —
  the search engine: tools, sources, bridges, corpus layers
- **[design/PHANTOM-DISCOVERY.md](design/PHANTOM-DISCOVERY.md)** — music
  beyond the local catalog (phantom artists, albums, tracklists)
- **[design/HARDWARE-TIERS.md](design/HARDWARE-TIERS.md)** — the measured
  resource map and the `full/standard/lite` profile layer
- **[design/P2P-SYNC-INTEGRITY.md](design/P2P-SYNC-INTEGRITY.md)** —
  provenance, seals and the recompute detector on the peer network
- **[design/BACKUP.md](design/BACKUP.md)** — node backup, share export/import,
  own life-data merge
- **[design/GEAR-ADVISOR.md](design/GEAR-ADVISOR.md)** — system analysis and
  upgrade strategy for the audio chain
- **[DATABASE_ARCHITECTURE.md](DATABASE_ARCHITECTURE.md)** — the Last.fm
  enrichment layer and the `external_metadata` fetch ledger

The repository root carries the rest: `CLAUDE.md` (spec and conventions),
`PROGRESS.md` (design log), `P2P_NETWORK.md` (peer network), `SECURITY.md`.

### 🔍 Security

- **[AUDIT.md](AUDIT.md)** — "Audit it yourself": the ready-to-paste agent
  prompt that checks the tree at one commit against `SECURITY.md` —
  destinations, ports, credentials, the demo ledger, the sync gate, support
  diagnostics, installer provenance

### 📘 HQPlayer integration

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

### 📖 Official manuals

- **[HQPLAYER_MANUALS.md](HQPLAYER_MANUALS.md)** — where to get the HQPlayer 5
  and 6 Desktop user manuals (Signalyst's documents, not redistributed here)
  and the version notes that matter to Sautium

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

3. **The official manual** (see HQPLAYER_MANUALS.md)
   - Detailed technical descriptions
   - Algorithm specifications

### Recommended reading order

1. **First**: HQPLAYER_QUICKSTART.md (quick orientation)
2. **Then**: HQPLAYER_KNOWLEDGE_BASE.md (detailed knowledge)
3. **If needed**: HQPLAYER_INTEGRATION.md (technical implementation)
4. **For reference**: the official manual (see HQPLAYER_MANUALS.md)

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

## Integration with Sautium

### Capabilities

- ✅ Automatic setting selection based on the track — the assistant reads the
  status and applies filters/shapers/matrix profiles through its MCP tools
- ✅ Profiles per genre
- ✅ Optimization for a specific DAC

### Where the integration lives

HQPlayer is one `PlayerBackend` among several (`backend/playback/` —
HQPlayer, DLNA, the browser and the local output). Playback goes through
the canonical queue and the playback manager, which speak the track UUID;
the file path is resolved from `media_files` at the moment the active
backend needs it, and a phantom track resolves to a stream instead. The
DSP side is `backend/hqplayer_client.py`, reached by the assistant through
the `hqplayer_*` MCP tools — that is the path "pick settings for this
track" actually takes, not a helper in application code.

## Keeping the documentation current

When a new HQPlayer version appears:
1. Note the new manual version in HQPLAYER_MANUALS.md
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

**HQPlayer version tested:** Desktop 6 — the daily configuration as of
2026-09-21; Desktop 5.16.3 (Engine 5.34.14) also tested, same client
**Last reviewed:** 2026-09-21
