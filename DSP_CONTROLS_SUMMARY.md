# HQPlayer DSP Controls - Summary

## ✅ Setting control is fully implemented

### Available settings

#### 1. **Output Mode** (PCM/DSD)
```python
modes = hqp.get_modes()
# Result: [{"index": 0, "name": "[source]"}, {"index": 1, "name": "PCM"}, {"index": 2, "name": "SDM (DSD)"}]

hqp.set_mode(1)  # PCM
hqp.set_mode(2)  # DSD
```

**Modes:**
- `[source]` — as in the source (no upsampling)
- `PCM` — PCM mode
- `SDM (DSD)` — DSD mode

---

#### 2. **Filters** (PCM and SDM/DSD)
```python
filters = hqp.get_filters()
# Result: 77 filters

# Examples:
# PCM: poly-sinc-ext2, poly-sinc-gauss-xla, sinc-L, sinc-M, etc.
# DSD: DSD7 512+fs, DSD9 512+fs, etc.

hqp.set_filter(6)      # poly-sinc-lp
hqp.set_filter(10, 8)  # PCM: upsampling filter + 1x filter
```

**Filter families:**
- **IIR** — Infinite Impulse Response
- **FIR** — Finite Impulse Response (linear phase, minimum phase, asymmetric)
- **poly-sinc** — polynomial-interpolated sinc (variants: lp, mp, short, gauss,
  ext, xla)
- **sinc** — sinc-function filters (L, M, S variants)
- **closed-form** — closed-form filters

**Total:** 77 filters

---

#### 3. **Noise Shapers** (dither / DSD modulators)
```python
shapers = hqp.get_shapers()
# Result: 36 shapers

# Examples:
# DSD5, DSD5v2, DSD5EC
# ASDM5, ASDM5EC, ASDM5EC-ul
# ASDM7, ASDM7EC, ASDM7EC-super

hqp.set_shaping(15)  # ASDM7EC-super 512+fs
```

**Shaper families:**
- **DSD5** series — 5th-order DSD modulators
- **ASDM5** series — Advanced Sigma-Delta Modulators, 5th order
- **ASDM7** series — 7th order (higher quality)
- Variants: EC (error correction), ul (ultra-light), super, light

**Total:** 36 noise shapers

---

#### 4. **Sample Rates** (output)
```python
rates = hqp.get_rates()
# Result: 20 sample rates

# Examples:
# PCM: 2.048, 3.072, 4.096, 6.144, 8.192, 12.288 MHz
# DSD: 2.8224 (DSD64), 5.6448 (DSD128), 11.2896 (DSD256), 22.5792 (DSD512), 45.1584 (DSD1024), 90.3168 (DSD2048)

hqp.set_rate(8)   # 11.2896 MHz (DSD256)
hqp.set_rate(12)  # 22.5792 MHz (DSD512)
```

**Available rates:**
- **DSD64**: 2.8224 MHz
- **DSD128**: 5.6448 MHz
- **DSD256**: 11.2896 MHz
- **DSD512**: 22.5792 MHz
- **DSD1024**: 45.1584 MHz
- **DSD2048**: 90.3168 MHz
- **PCM rates**: 2.048, 3.072, 4.096, 6.144, 8.192, 12.288, 16.384, 24.576,
  32.768, 49.152, 98.304 MHz

**Total:** 20 rates

---

#### 5. **Input Devices**
```python
inputs = hqp.get_inputs()
# Result: ["cd:"]

# Note: the input device list depends on HQPlayer's own configuration
```

---

## Practical examples

### Example 1: PCM mode with a high-quality filter
```python
from hqplayer_client import HQPlayerConnection

with HQPlayerConnection(host="172.26.80.1") as hqp:
    # Read the available options
    modes = hqp.get_modes()
    filters = hqp.get_filters()

    # Find PCM mode
    pcm = next(m for m in modes if m['name'] == 'PCM')

    # Find the poly-sinc-ext2 filter
    poly_sinc_ext2 = next(f for f in filters if 'poly-sinc-ext2' in f['name'])

    # Apply
    hqp.set_mode(pcm['index'])
    hqp.set_filter(poly_sinc_ext2['index'])

    print("✅ PCM mode with the poly-sinc-ext2 filter")
```

### Example 2: DSD512 with ASDM7EC-super
```python
with HQPlayerConnection(host="172.26.80.1") as hqp:
    # DSD mode
    modes = hqp.get_modes()
    dsd_mode = next(m for m in modes if 'DSD' in m['name'])

    # DSD512 (22.5792 MHz)
    rates = hqp.get_rates()
    dsd512 = next(r for r in rates if r['rate'] == 22579200)

    # ASDM7EC-super shaper
    shapers = hqp.get_shapers()
    asdm7 = next(s for s in shapers if 'ASDM7EC-super' in s['name'])

    # Apply
    hqp.set_mode(dsd_mode['index'])
    hqp.set_rate(dsd512['index'])
    hqp.set_shaping(asdm7['index'])

    print("✅ DSD512 with ASDM7EC-super")
```

### Example 3: choosing settings for a track automatically
```python
def auto_configure_for_track(hqp, track):
    """AI assistant: configure HQPlayer for a track."""

    if track.sample_rate >= 96000:
        # Hi-res FLAC → PCM upsampled to DSD256
        print("🎵 Hi-res track → PCM + upsample to DSD256")

        modes = hqp.get_modes()
        pcm = next(m for m in modes if m['name'] == 'PCM')

        filters = hqp.get_filters()
        best_filter = next(f for f in filters if 'poly-sinc-ext2' in f['name'])

        rates = hqp.get_rates()
        dsd256 = next(r for r in rates if r['rate'] == 11289600)

        hqp.set_mode(pcm['index'])
        hqp.set_filter(best_filter['index'])
        hqp.set_rate(dsd256['index'])

    else:
        # Standard quality → PCM with a standard filter
        print("🎵 Standard track → PCM + poly-sinc")

        modes = hqp.get_modes()
        pcm = next(m for m in modes if m['name'] == 'PCM')

        filters = hqp.get_filters()
        poly_sinc = next(f for f in filters if 'poly-sinc-lp' in f['name'])

        hqp.set_mode(pcm['index'])
        hqp.set_filter(poly_sinc['index'])
```

---

## What the API does NOT expose

❌ **Output device selection**
- The DAC / output device has to be chosen by hand in the HQPlayer GUI
- The API offers no methods for output devices

---

## Testing

```bash
# Automated test of every DSP setting
cd /mnt/d/ai/djai/backend
python3 test_hqplayer_settings.py

# Usage examples
python3 examples_hqplayer_dsp.py
```

---

## Summary

✅ **Full control over HQPlayer's DSP settings:**
- ✅ 3 output modes (source, PCM, DSD)
- ✅ 77 filters (IIR, FIR, poly-sinc, sinc, closed-form)
- ✅ 36 noise shapers (DSD5, ASDM5, ASDM7 series)
- ✅ 20 sample rates (up to DSD2048 / 90.3168 MHz)
- ✅ Input devices

❌ **Not available:**
- Output device selection (configure it in the GUI)

---

## Integration with Sautium

Possibilities:
1. **Automatic mode selection** based on track quality
2. **Filter optimization** per genre
3. **Voice control** of DSP settings (Phase 4)
4. **Profiles** per kind of music (jazz, classical, rock)

Voice-control sketch:
```
User: "Claude, set the best quality for this track"
AI:   "Setting DSD256 with the ASDM7EC-super shaper for maximum quality"

User: "Switch to PCM mode"
AI:   "Switching to PCM with the poly-sinc-ext2 filter"
```

---

**Status**: ✅ **Ready to use**
**Tested with**: HQPlayer Desktop 5.16.3 (Engine 5.34.14)
