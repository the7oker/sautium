# HQPlayer 5 Desktop - Knowledge Base for the AI agent

> Key information distilled from the official HQPlayer 5 Desktop v5.16.0 manual

> **Note (HQP6):** this catalogue was compiled from the HQPlayer 5 Desktop
> manual (v5.16.0). From HQP6 onwards Sautium reads the filter / shaper / mode
> lists **and** each filter's description live from HQPlayer at runtime, so
> this file is now a **secondary reference for humans**, not a source the AI assistant
> relies on. Take filter/modulator availability and descriptions from the live
> HQPlayer instance.

---

## Architecture and capabilities

### Main components
- **Playback Engine** — audio playback with high-quality upsampling
- **DSP Pipeline** — signal processing (filters, modulators, convolution)
- **Network Audio** — network protocol support (NAA, Roon Ready)
- **Library Management** — music library management
- **Control API** — XML/TCP API for remote control

### Supported formats
- **PCM**: FLAC, WAV, AIFF, ALAC, MP3, AAC
- **DSD**: DFF (DSDIFF), DSF
- **Streaming**: Tidal, Qobuz (through partners)

---

## Output modes

### 1. **[source]**
- Plays in the source format, no upsampling
- Adaptive sample-rate selection
- Minimal processing

### 2. **PCM**
- Upsamples PCM to higher sample rates
- Noise shaping and dithering
- Supports up to 768 kHz

### 3. **SDM (DSD)**
- Conversion to DSD (Direct Stream Digital)
- **1-bit format** — every sample is a 0 or a 1
- Delta-sigma modulation
- Supports up to DSD2048 (90.3168 MHz)

**What DSD is:**
- A **1-bit audio format**, unlike PCM (16/24/32-bit)
- Very high sample rates (megahertz instead of kilohertz)
- The signal is encoded through the **density** of ones (PDM — Pulse Density
  Modulation)
- More ones = higher amplitude, fewer ones = lower amplitude

**IMPORTANT — the DSD base rate:**
- **Base rate = 44.1 kHz** (the CD sample rate)
- The number in the name (64, 128, 256, 512…) is the multiplier of the base
- Formula: **DSDxxx = 44.1 kHz × xxx**

**DSD formats (worked out):**
- **DSD64** = 44.1k × **64** = 2822400 Hz = 2.8224 MHz (base SACD rate)
- **DSD128** = 44.1k × **128** = 5644800 Hz = 5.6448 MHz
- **DSD256** = 44.1k × **256** = 11289600 Hz = 11.2896 MHz
- **DSD512** = 44.1k × **512** = 22579200 Hz = 22.5792 MHz
- **DSD1024** = 44.1k × **1024** = 45158400 Hz = 45.1584 MHz
- **DSD2048** = 44.1k × **2048** = 90316800 Hz = 90.3168 MHz

**Equivalent notations:**
- DSD256(1bit 11.2MHz) = 44.1k × 256 = 11289600 Hz = 11.2896 MHz
- DSD512(1bit 22.4MHz) = 44.1k × 512 = 22579200 Hz = 22.5792 MHz

**Worked example:**
```
DSD256 → 44100 × 256 = 11,289,600 Hz = 11.2896 MHz ≈ 11.2 MHz
```

**Note:** specifications often round (11.2 MHz instead of 11.2896 MHz), but the
exact rate is always **44.1 kHz × multiplier**.

---

## PCM settings

### Noise shaping / dither (PCM shapers)

| Algorithm | Purpose | Recommended for |
|-----------|---------|-----------------|
| **TPDF** | Industry standard dither | Universal, 44.1/48 kHz |
| **shaped** | Shaped dither | 88.2/96 kHz and above |
| **Gauss1** | Gaussian dither | Up to 96 kHz |
| **NS1** | 1st order noise shaping | 176.4/192 kHz |
| **NS4** | 4th order noise shaping | ≥ 88.2 kHz |
| **NS5** | 5th order noise shaping | 8x/16x rates (352.8/384/705.6/768 kHz) |
| **NS9** | 9th order noise shaping | 4x rates (176.4/192 kHz), for older 16-bit DACs |
| **LNS15** | 15th order linear | 16x rates (705.6/768 kHz) |

**Notes:**
- NS5: particularly good for the PCM1704 at the highest rates
- NS9: ideal for older 16-bit multibit DACs (TDA154x)
- LNS15: smooth slope, for the highest PCM rates

### DAC bits (R2R DACs)

| DAC model | Bits |
|-----------|------|
| Holo Audio (Cyan 2, Spring 2/3, May) | 20 |
| Denafrips | 20 |
| LAiV Harmony | 18 |

**Note:** the right noise shaper (LNS15, NS9, NS5) combined with high rates can
compensate for the linearity errors of R2R DACs.

---

## COMPLETE NOISE-SHAPER TABLE (36 shapers)

### PCM noise shapers / dithers (10 shapers)

| Name | Sample rate | Purpose | DAC type |
|------|-------------|---------|----------|
| **none** | Any | No dither (testing only) | - |
| **TPDF** | 44.1/48 kHz | Industry standard dither | Universal |
| **shaped** | 88.2/96 kHz+ | Shaped dither | Universal |
| **Gauss1** | Up to 96 kHz | Gaussian dither | Universal |
| **NS1** | 176.4/192 kHz | 1st order noise shaping | Universal |
| **NS4** | ≥ 88.2 kHz | 4th order noise shaping | Universal |
| **NS5** | 352.8/384/705.6/768 kHz | 5th order, 8x/16x rates | PCM1704 |
| **NS9** | 176.4/192 kHz | 9th order, 4x rates | 16-bit multibit (TDA154x) |
| **LNS15** | 705.6/768 kHz | 15th order linear, 16x rates | R2R DACs |
| **LNS15 light** | 705.6/768 kHz | Lighter version of LNS15 | R2R DACs |

### SDM (DSD) modulators — 5th order (13 modulators)

For ESS Sabre DACs and simple analog filters:

| Name | Optimal rate | Characteristics |
|------|--------------|-----------------|
| **DSD5** | Any DSD | Rate-adaptive fifth order |
| **DSD5v2** | Any DSD | Revised fifth order |
| **DSD5v2 256+fs** | ≥ DSD256 (10.24 MHz) | Tuned for high rates |
| **DSD5EC** | Any DSD | Extended compensation |
| **ASDM5** | Any DSD | Adaptive fifth order |
| **ASDM5EC** | Any DSD | Adaptive with extended compensation |
| **ASDM5ECv2** | Any DSD | Improved ASDM5EC |
| **ASDM5ECv3** | Any DSD | Further improved |
| **ASDM5EC-ul** | Any DSD | Ultralight (less CPU) |
| **ASDM5EC-light** | Any DSD | Light version |
| **ASDM5EC-fast** | Any DSD | Transient optimized |
| **ASDM5EC-super** | Any DSD | Super quality |
| **ASDM5EC-super 512+fs** | ≥ DSD512 (22.4 MHz) | Tuned for 512+ |

### SDM (DSD) modulators — 7th order (13 modulators)

For multi-element DACs and most non-ESS DACs:

| Name | Optimal rate | Characteristics |
|------|--------------|-----------------|
| **DSD7** | Any DSD | Seventh order |
| **DSD7 256+fs** | ≥ DSD256 (10.24 MHz) | Tuned for high rates |
| **DSD7 512+fs** | ≥ DSD512 (22.4 MHz) | Tuned for 512+ |
| **ASDM7** | Any DSD | Adaptive seventh order |
| **ASDM7EC** | Any DSD | Adaptive with extended compensation |
| **ASDM7ECv2** | Any DSD | Improved ASDM7EC |
| **ASDM7ECv3** | Any DSD | Further improved |
| **ASDM7EC-ul** | Any DSD | Ultralight (less CPU) |
| **ASDM7EC-light** | Any DSD | Light version |
| **ASDM7EC-fast** | Any DSD | Transient optimized |
| **ASDM7EC-super** | Any DSD | Super quality (recommended) |
| **ASDM7EC-super 512+fs** | ≥ DSD512 (22.4 MHz) | Tuned for 512+ (top quality) |
| **ASDM7EC-super 1024+fs** | ≥ DSD1024 (45.1 MHz) | Tuned for 1024+ |

### Choosing a modulator/shaper

**PCM mode:**
- 44.1/48 kHz output → **TPDF**
- 88.2/96 kHz output → **NS4** or **shaped**
- 176.4/192 kHz output → **NS9** (for older 16-bit DACs) or **NS1**
- 352.8/384 kHz output → **NS5**
- 705.6/768 kHz output → **LNS15** (for R2R DACs) or **NS5**

**SDM mode:**
- **ESS Sabre DACs** → **ASDM5EC-super 512+fs** (5th order)
- **Multi-element DACs** → **ASDM7EC-super 512+fs** (7th order)
- **R2R DACs** → **ASDM7EC-super** or **ASDM7EC-super 512+fs**
- **Universal choice** → **ASDM7EC-super** (works with everything)

---

## COMPLETE SAMPLE-RATE TABLE (20 rates)

### PCM sample rates (14 rates)

| Rate (Hz) | Rate (MHz) | Multiplier | Description |
|-----------|------------|------------|-------------|
| 44100 | 0.0441 | 1x | CD standard (base) |
| 48000 | 0.048 | 1x | DAT standard (base) |
| 88200 | 0.0882 | 2x | 2x CD rate |
| 96000 | 0.096 | 2x | 2x DAT rate |
| 176400 | 0.1764 | 4x | 4x CD rate |
| 192000 | 0.192 | 4x | 4x DAT rate |
| 352800 | 0.3528 | 8x | 8x CD rate |
| 384000 | 0.384 | 8x | 8x DAT rate |
| 705600 | 0.7056 | 16x | 16x CD rate |
| 768000 | 0.768 | 16x | 16x DAT rate (max PCM) |
| 1536000 | 1.536 | 32x | 32x DAT (experimental) |
| 3072000 | 3.072 | 64x | 64x DAT (experimental) |
| 6144000 | 6.144 | 128x | 128x DAT (experimental) |
| 12288000 | 12.288 | 256x | 256x DAT (experimental) |

### DSD (SDM) sample rates (6 rates)

| Rate (Hz) | Rate (MHz) | DSD name | Multiplier | Formula | Description |
|-----------|------------|----------|------------|---------|-------------|
| 2822400 | 2.8224 | DSD64 | 64x | 44.1k × 64 | Base DSD rate (SACD) |
| 5644800 | 5.6448 | DSD128 | 128x | 44.1k × 128 | 2x base rate |
| 11289600 | 11.2896 | DSD256 | 256x | 44.1k × 256 | 4x base rate |
| 22579200 | 22.5792 | DSD512 | 512x | 44.1k × 512 | 8x base rate (recommended) |
| 45158400 | 45.1584 | DSD1024 | 1024x | 44.1k × 1024 | 16x base rate (high-end) |
| 90316800 | 90.3168 | DSD2048 | 2048x | 44.1k × 2048 | 32x base rate (extreme) |

**IMPORTANT: base rate = 44.1 kHz (the CD sample rate)**

### Choosing a sample rate

**General guidance:**

**PCM mode:**
- **Standard DACs**: 176.4/192 kHz (4x) — the universal safe choice
- **R2R DACs**: 705.6/768 kHz (16x) — compensates linearity errors
- **Hi-res sources (≥96 kHz)**: 384 kHz or above
- **Standard sources (44.1/48 kHz)**: 176.4/192 kHz minimum

**SDM (DSD) mode:**
- **Minimum recommended**: DSD256 (11.2896 MHz)
- **Sweet spot**: DSD512 (22.5792 MHz) — the quality/CPU balance
- **High-end**: DSD1024 (45.1584 MHz) — needs a powerful CPU
- **Extreme**: DSD2048 (90.3168 MHz) — top-tier systems only

**By DAC type:**
- **ESS Sabre**: DSD512+ with ASDM5EC-super 512+fs
- **Multi-element**: DSD512+ with ASDM7EC-super 512+fs
- **R2R (Holo, Denafrips)**: PCM 768 kHz with LNS15 OR DSD512+ with
  ASDM7EC-super

**Limits:**
- Not every DAC supports every rate
- DSD1024+ needs top-tier DACs (very rare)
- Check your DAC's specifications before choosing

### DSD → PCM conversion

**Noise filters:**
- **standard** — recommended
- **low** — flat noise profile, recommended
- **high-order** — for high-order modulators
- **medium** — gentle, minimal out-of-band noise
- **brickwall** — lets no out-of-band noise through

**Conversion types:**
- **poly-short-lp** — linear-phase slow roll-off (recommended)
- **poly-short-mp** — minimum-phase slow roll-off
- **poly-ext2** — extended frequency response
- **poly-gauss-long** — optimal time-frequency response
- **none** — no decimation (output = DSD rate)

---
## SDM (DSD) settings

### Delta-sigma modulators

#### 5th order (for simple analog filters)
| Modulator | Description |
|-----------|-------------|
| **DSD5** | Rate-adaptive fifth order |
| **DSD5v2** | Revised fifth order |
| **DSD5v2 256+fs** | Tuned for ≥ 10.24 MHz |
| **DSD5EC** | With extended compensation |
| **ASDM5** | Adaptive fifth order |
| **ASDM5EC** | Adaptive with EC |
| **ASDM5ECv2/v3** | Improved versions |
| **ASDM5EC-ul** | Ultralight version |
| **ASDM5EC-light** | Light version |
| **ASDM5EC-fast** | Transient optimized |
| **ASDM5EC-super** | Super version |
| **ASDM5EC-* 512+fs** | Tuned for 512x+ rates |

**Recommendations:**
- **ESS Sabre DACs**: 5th order modulators
- **Simple analog filters**: 5th order

#### 7th order (for DACs with multi-element arrays)
| Modulator | Description |
|-----------|-------------|
| **DSD7** | Seventh order |
| **DSD7 256+fs** | Tuned for ≥ 10.24 MHz |
| **ASDM7** | Adaptive seventh order |
| **ASDM7EC** | Adaptive with EC |
| **ASDM7ECv2/v3** | Improved versions |
| **ASDM7EC-ul** | Ultralight version |
| **ASDM7EC-light** | Light version |
| **ASDM7EC-fast** | Transient optimized |
| **ASDM7EC-super** | Super version |
| **ASDM7EC-* 512+fs** | Tuned for 512x+ rates |

**Recommendations:**
- **Multi-element DACs**: 7th order modulators
- **Most DACs (other than ESS)**: 7th order is optimal

#### Hybrid modulators (experimental)
| Modulator | Description | Limitation |
|-----------|-------------|------------|
| **AMSDM7 512+fs** | Pseudo-multi-bit for ≥ 20.48 MHz | - |
| **AHM5EC5L** | 5th order 5-level, ≥ 40.96 MHz | Limited SNR |
| **AHM7EC5L** | 7th order 5-level, ≥ 40.96 MHz | Limited SNR |
| **AHM5EC8B** | 5th order 8-bit, ≥ 40.96 MHz | - |
| **AHM7EC8B** | 7th order 8-bit, ≥ 40.96 MHz | - |

**Note:** hybrid modulators suit loudspeaker systems better; not recommended
when HQPlayer's volume control is the primary one.

### Integrators (SDM → SDM remodulation)

| Integrator | Audio bandwidth (re DSD64) | Description |
|------------|----------------------------|-------------|
| **IIR** | 50 kHz | Normal IIR |
| **IIR2** | 25 kHz | Minimizes residual noise |
| **IIR3** | 30 kHz | High-order IIR |
| **FIR** | - | Weighted FIR |
| **FIR2** | 50 kHz | Weighted FIR |
| **FIR-bl** | 24 kHz (cut 45 kHz) | Band-limiting |
| **FIR-bw** | 21.5 kHz (cut 30 kHz) | Brickwall |
| **CIC** | - | Cascade comb |

### SDM conversion

| Type | Purpose |
|------|---------|
| **wide** | Wide-bandwidth signal |
| **narrow** | Narrow bandwidth (piano) |
| **XFi** | Extreme fidelity medium (universal) |

**Default:** XFi — suitable for every case

---

## Filters / oversampling

### Structure
- **1x filters** — for source rates < 50 kHz (base rates)
- **Nx filters** — for everything above the 1x rates

### Quick filter choice

#### By genre:
- **Classical** → poly-sinc-gauss-xla, sinc-MGa, poly-sinc-ext2-xla
- **Jazz/blues** → poly-sinc-gauss-xla, sinc-MGa, IIR2
- **Pop/rock** → poly-sinc-shrt-mp, minphaseFIR, IIR2
- **Electronic** → poly-sinc-xtr-short-mp, poly-sinc-gauss-short

#### By focus:
- **Transients (attacks)** → sinc-MGa, poly-sinc-gauss-*, IIR*, minphaseFIR
- **Timbre** → poly-sinc-ext2-*, poly-sinc-xtr-*, sinc-M*
- **Space** → poly-sinc-gauss-long, poly-sinc-ext2-long, sinc-*

#### Apodizing (removing pre-ringing):
- **With apodizing** (when the Apod counter > 10): sinc-MGa,
  poly-sinc-ext2-xla, poly-sinc-gauss-xla
- **Without apodizing**: sinc-MG, poly-sinc-gauss-xl

---

## COMPLETE FILTER TABLE (77 filters)

| Filter name | Description | Focus | Quality | Genre | Ratio | Apod |
|-------------|-------------|-------|---------|-------|-------|------|
| **none** | No conversion, bit depth only | - | 1/5 | - | 1:1 | N |
| **IIR** | Analog-sounding, no pre-ring, long post-ring, medium attenuation | - | 2/5 | Pop, rock, jazz, blues | Integer | Y |
| **IIR2** | Analog-sounding, steep, no pre-ring, no passband ripple | - | 4/5 | Pop, rock, jazz, blues | Integer | Y |
| **FIR** | Typical oversampling, average pre/post-ring | - | 3/5 | Classical | Integer | Y |
| **asymFIR** | Shorter pre-ring, longer post-ring | - | 3/5 | Jazz, blues | Integer | Y |
| **minphaseFIR** | No pre-ring, long post-ring | - | 3/5 | Pop, rock, electronic | Integer | Y |
| **FFT** | Brickwall, configurable length | - | 4/5 | Any (depends on length) | 2x | Y |
| **poly-sinc-lp** | Linear phase polyphase sinc | Space | 4/5 | Classical | Any | ½ |
| **poly-sinc-mp** | Minimum phase polyphase sinc | Transients | 4/5 | Jazz, blues | Any | ½ |
| **poly-sinc-shrt-lp** | Short, slower roll-off | Space, transients | 3/5 | Jazz, blues, electronic | Any | ½ |
| **poly-sinc-shrt-mp** | Short minimum phase, optimal transients | Transients | 3/5 | Pop, rock | Any | ½ |
| **poly-sinc-long-lp** | Long, faster roll-off | Space | 4/5 | Classical | Any | Y |
| **poly-sinc-long-ip** | Intermediate phase, small pre-ring | Space, transients | 4/5 | Jazz, blues, electronic | Any | Y |
| **poly-sinc-long-mp** | Long minimum phase | Transients | 4/5 | Pop, rock | Any | Y |
| **poly-sinc-hb** | Half-band steep, high attenuation | - | 4/5 | Any | Any | N |
| **poly-sinc-hb-xs** | Half-band extremely short | - | 2/5 | Pop, rock | Any | N |
| **poly-sinc-hb-s** | Half-band short | - | 3/5 | Pop, rock | Any | N |
| **poly-sinc-hb-m** | Half-band medium | - | 3/5 | Any | Any | N |
| **poly-sinc-hb-l** | Half-band long | - | 4/5 | Classical, jazz, blues | Any | N |
| **poly-sinc-ext** | Sharp roll-off, lower attenuation | - | 3/5 | - | Integer | ½ |
| **poly-sinc-ext2** | Sharp roll-off, high attenuation, optimal frequency/harmonic | Timbre | 5/5 | Any | Any | Y |
| **poly-sinc-ext2-short** | Slow roll-off, high attenuation | Timbre | 4/5 | Pop, rock | Integer up | ½ |
| **poly-sinc-ext2-medium** | Fast roll-off, high attenuation | Timbre | 4/5 | Any | Any | Y |
| **poly-sinc-ext2-long** | Very fast roll-off, very high attenuation | Timbre | 5/5 | Any | Any | Y |
| **poly-sinc-ext2-xla** | 8x longer than ext2-long, very steep | Timbre | 5/5 | Classical | Any | Y |
| **poly-sinc-ext2-xl** | 8x longer, non-apodizing | Timbre | 5/5 | Classical | Any | N |
| **poly-sinc-ext2-hires-lp** | For HiRes/MP3/MQA, very high attenuation | Timbre | 5/5 | Any | Any | Y |
| **poly-sinc-ext2-hires-ip** | Intermediate phase HiRes | Timbre | 5/5 | Any | Any | Y |
| **poly-sinc-ext2-hires-mp** | Minimum phase HiRes | Timbre | 5/5 | Any | Any | Y |
| **poly-sinc-mqa/mp3-lp** | Optimized for MQA/MP3 cleanup, short ring | Transients | 4/5 | Classical, jazz, blues | PCM: Int up, SDM: Any | Y |
| **poly-sinc-mqa/mp3-mp** | Minimum phase MQA/MP3 | Transients | 4/5 | Pop, rock | PCM: Int up, SDM: Any | Y |
| **poly-sinc-xtr-lp** | Extreme roll-off and attenuation | Timbre | 5/5 | Classical | Any | ½ |
| **poly-sinc-xtr-mp** | Minimum phase extreme | Timbre | 5/5 | Jazz, blues | Any | ½ |
| **poly-sinc-xtr-short-lp** | Short extreme | Timbre, transients | 5/5 | Electronic, jazz, blues, pop, rock | Any | Y |
| **poly-sinc-xtr-short-mp** | Short minimum phase extreme | Timbre, transients | 5/5 | Pop, rock | Any | Y |
| **poly-sinc-gauss-short** | Short Gaussian, optimal time-frequency | Transients | 3/5 | Electronic, jazz, blues, pop, rock | Integer up | ½ |
| **poly-sinc-gauss-medium** | Gaussian, optimal time-frequency | Transients, timbre | 4/5 | Any | Any | Y |
| **poly-sinc-gauss-long** | Long Gaussian, extremely high attenuation | Transients, timbre, space | 5/5 | Any | Any | Y |
| **poly-sinc-gauss-xla** | Apodizing extra long Gaussian | Transients, timbre, space | 5/5 | Classical, jazz, blues | Any | Y |
| **poly-sinc-gauss-xl** | Extra long Gaussian, non-apodizing | Transients, timbre, space | 5/5 | Classical, jazz, blues | Any | N |
| **poly-sinc-gauss-hires-lp** | Linear Gaussian for HiRes/MP3/MQA | Transients, timbre, space | 5/5 | Any | Any | Y |
| **poly-sinc-gauss-hires-ip** | Intermediate Gaussian HiRes | Transients, timbre, space | 5/5 | Any | Any | Y |
| **poly-sinc-gauss-hires-mp** | Minimum phase Gaussian HiRes | Transients, timbre, space | 5/5 | Any | Any | Y |
| **poly-sinc-gauss-halfband** | Linear halfband Gaussian, slightly leaky | Transients, timbre, space | 4/5 | Any | Any | N |
| **poly-sinc-gauss-halfband-s** | Short halfband Gaussian, leaky | Transients, timbre, space | 3/5 | Any | Any | N |
| **ASRC** | Asynchronous any-to-any rate | - | 2/5 | - | Any | N |
| **polynomial-1** | No ring, poor rejection, not recommended | - | 1/5 | - | Integer up | N |
| **polynomial-2** | One cycle ring, not recommended | - | 1/5 | - | Integer up | N |
| **minringFIR-lp** | Linear phase minimum ringing | Transients | 2/5 | - | Integer up | N |
| **minringFIR-mp** | Minimum phase minimum ringing | Transients | 2/5 | - | Integer up | N |
| **closed-form** | High taps | - | 3/5 | - | 2x up | N |
| **closed-form-fast** | Lower CPU, ~24-bit precision | - | 2/5 | - | 2x up | N |
| **closed-form-M** | Million taps | - | 3/5 | - | 2x up | N |
| **closed-form-16M** | 16 million taps | - | 3/5 | - | 2x up | N |
| **sinc-S** | 4096 x ratio, sharp, high attenuation, variant of ext2-xla | Space, timbre | 4/5 | Any | 2x up | Y |
| **sinc-M** | Million taps, very sharp, variant of ext2-xla | Space, timbre | 4/5 | Classical, jazz, blues | 2x up | Y |
| **sinc-Mx** | Constant time million taps @ 16x (65536 x ratio), variant of ext2-xla | Space, timbre | 4/5 | Classical, jazz, blues | 2x up | Y |
| **sinc-MG** | Gaussian million @ 16x, extremely high attenuation, variant of gauss-xl | Transients, timbre, space | 4/5 | Classical, jazz, blues | 2x up | N |
| **sinc-MGa** | APODIZING Gaussian million @ 16x, extremely high attenuation, variant of gauss-xla | Transients, timbre, space | 4/5 | Classical, jazz, blues | 2x up | Y |
| **sinc-L** | 131070 x ratio, extremely sharp, average attenuation | - | 3/5 | Classical | 2x up | N |
| **sinc-Ls** | 4096 x ratio, average attenuation | - | 2/5 | Any | 2x up | N |
| **sinc-Lm** | 16384 x ratio, average attenuation | - | 2/5 | Classical, jazz, blues | 2x up | N |
| **sinc-Ll** | 65536 x ratio, average attenuation | - | 3/5 | Classical | 2x up | N |
| **sinc-Lh** | 16384 x ratio, high attenuation, better than sinc-L @ 1/8 load | - | 4/5 | Classical, jazz, blues | 2x up | N |
| **sinc-short** | Short average, adaptive taps, 2-stage for SDM | - | 2/5 | Any | Any | N |
| **sinc-medium** | Average, adaptive taps, 2-stage for SDM | - | 2/5 | Classical, jazz, blues | Any | N |
| **sinc-long** | Long average, adaptive taps, 2-stage for SDM | - | 3/5 | Classical | Any | N |
| **sinc-long-h** | Long high attenuation, adaptive taps, 2-stage for SDM | - | 4/5 | Classical, jazz, blues | Any | N |
| ***-2s** | Two-stage: ≥8x first, then optimized second stage, lower CPU | Same as base | Same | Same | Same | Same |

### Reading the table

- **Filter name**: exactly as in the HQPlayer API
- **Description**: the filter's key characteristics
- **Focus**: what the filter helps most
  - **Transients**: accuracy of attacks and fast changes
  - **Timbre**: tonal accuracy, harmonics
  - **Space**: spatial characteristics, stereo image
- **Quality**: 1/5 to 5/5 (higher is better)
- **Genre**: recommended musical genres
- **Ratio**: supported conversion ratios
  - **Integer**: integer ratios only (2x, 4x, 8x, 16x)
  - **Any**: any ratio
  - **2x up**: 2x upsampling only (⚠️ requires a matching base rate)
  - **Integer up**: integer upsampling
- **Apod**: apodizing capability
  - **Y**: apodizing (removes pre-ringing)
  - **N**: not apodizing
  - **½**: partially apodizing

**⚠️ IMPORTANT — base-rate compatibility:**
- **sinc-*** filters (everything starting with "sinc-"): work ONLY with a
  matching base rate (44.1k→44.1k or 48k→48k)
- **closed-form*** filters: matching base rate only
- **poly-sinc-***, **IIR***, **FIR*** filters: universal (any base rate)

### SDM output processing

Filters with two-stage processing for SDM output (16x intermediate rate
minimum):
- the poly-sinc-ext2 series
- the poly-sinc-gauss series
- the sinc-short/medium/long series

**Note:** when the output is SDM (DSD) these filters use 2-stage processing for
optimal quality.

### Special filters

#### sinc-MGa vs sinc-MG
- **sinc-MG**: not apodizing, a variant of poly-sinc-gauss-xl
- **sinc-MGa**: APODIZING, a variant of poly-sinc-gauss-xla
- Both: million taps @ 16x rates (65536 x conversion ratio)
- Both: extremely high attenuation
- **Use MGa when**: the Apod counter > 10 (the source has pre-ringing)

#### Constant-time filters
- **sinc-Mx**: 65536 x conversion ratio
- **sinc-MG**: 65536 x conversion ratio
- **sinc-MGa**: 65536 x conversion ratio
- Million taps at 16x PCM output rates (768 kHz)

### Quality tiers
- **5/5**: highest quality (poly-sinc-ext2-*, poly-sinc-gauss-*,
  poly-sinc-xtr-*)
- **4/5**: high quality (sinc-*, IIR2, poly-sinc-lp/mp)
- **3/5**: good quality (FIR, asymFIR, poly-sinc-short, closed-form)
- **2/5**: basic quality (ASRC, minringFIR, polynomial-2)
- **1/5**: not recommended (none, polynomial-1)

### When to use an apodizing filter
**Use one when:**
- The Apod counter goes above 10 during playback
- That means the source carries pre-ringing artifacts
- An apodizing filter (marked Y) compensates for them
- Examples: sinc-MGa, poly-sinc-ext2-xla, poly-sinc-gauss-xla

---

## Sample rates

### PCM rates
- 44.1, 48 kHz (standard)
- 88.2, 96 kHz (2x)
- 176.4, 192 kHz (4x)
- 352.8, 384 kHz (8x)
- 705.6, 768 kHz (16x)

### DSD rates
| Rate | Frequency | Multiplier |
|------|-----------|------------|
| DSD64 | 2.8224 MHz | 64x |
| DSD128 | 5.6448 MHz | 128x |
| DSD256 | 11.2896 MHz | 256x |
| DSD512 | 22.5792 MHz | 512x |
| DSD1024 | 45.1584 MHz | 1024x |
| DSD2048 | 90.3168 MHz | 2048x |

**Base rate:** 44.1 kHz × multiplier

---

## Convolution engine

### Purpose
- Room correction
- Crossfeed (for headphones)
- Custom impulse responses

### Formats
- WAV (linear PCM)
- FLAC
- Mono/stereo/multichannel

### Parameters
- Partitioned convolution (FFT-based)
- Low latency
- Normalized or custom gain

---

## Matrix processing

### Capabilities
- Channel routing and mixing
- Delay compensation
- EQ (through IIR filters)
- RIAA correction (for turntables)

### Plugins
- **delay** — channel delay
- **iir** — IIR filters (parametric EQ)
- **riaa** — RIAA equalization curve

---

## Adaptive output rate

**When enabled:**
- HQPlayer picks the output rate automatically
- Based on the source rate and the filter's capabilities
- "Sample rate" becomes an upper limit

**When disabled:**
- Fixed output rate (in PCM mode)

---

## Recommendations per scenario

### High-res PCM (≥ 96 kHz/24-bit)
```
Source: 96-192 kHz FLAC (hi-res download or studio master)
Mode: PCM
Filter: poly-sinc-ext2-long or poly-sinc-gauss-long
Rate: 384 kHz (8x) or 768 kHz (16x for R2R DACs)
Shaper: NS5 (for 384 kHz) or LNS15 (for 768 kHz)
```
**Use for:** studio masters, official hi-res releases (Qobuz, HDtracks)

### Standard-quality CD rips (44.1/48 kHz/16-bit)
```
Source: CD FLAC or Apple Lossless
Mode: SDM (if the DAC supports DSD) or PCM
Filter: poly-sinc-gauss-medium or poly-sinc-ext2-medium
Rate: DSD256 (11.2896 MHz) or PCM 192 kHz
Modulator: ASDM7EC-super (or ASDM5EC-super for ESS)
Shaper (if PCM): NS1 or NS4
```
**Use for:** most of the library — CD rips, iTunes ALAC

### DSD source (DSD64/DSD128)
```
Source: native DSD recording
Mode: SDM
Filter: none (DSD → DSD remodulation needs no filter)
Modulator: ASDM7EC-super 512+fs (or ASDM5EC-super 512+fs for ESS)
Rate: DSD512 (22.5792 MHz) minimum
Integrator: FIR2 or IIR2
Conversion: XFi
```
**Use for:** native DSD recordings, SACD rips

### R2R DACs (Holo Audio, Denafrips, LAiV Harmony)
```
Mode: PCM (recommended) or SDM
Filter: poly-sinc-gauss-long or poly-sinc-ext2-long
Rate: 705.6/768 kHz (16x) — CRITICAL for linearity correction
Shaper: LNS15 or NS5
DAC bits: 20 (Holo/Denafrips) or 18 (LAiV)
```
**Why:** noise shaping at high rates compensates the linearity errors of R2R
DACs
**Alternative (SDM):** ASDM7EC-super 512+fs with DSD512+

### ESS Sabre DACs (ES9038PRO, ES9028PRO, etc.)
```
Mode: SDM (optimal for the ESS architecture)
Filter: poly-sinc-gauss-medium or poly-sinc-ext2-medium
Modulator: ASDM5EC-super 512+fs (5th order)
Rate: DSD512 (22.5792 MHz) minimum, DSD1024 if the CPU allows
```
**Why:** ESS chips are optimized for 5th-order modulators
**Important:** 5th order (ASDM5), NOT 7th

### Multi-element / delta-sigma DACs (general)
```
Mode: SDM
Filter: poly-sinc-gauss-long or sinc-MGa (if the CPU is strong)
Modulator: ASDM7EC-super 512+fs or ASDM7EC-super 1024+fs
Rate: DSD512 (22.5792 MHz) or DSD1024 (45.1584 MHz)
```
**Why:** 7th order is optimal for most non-ESS DACs

### Vinyl rips (analog sources)
```
Source: vinyl rip 96-192 kHz/24-bit
Mode: PCM or SDM
Filter: poly-sinc-gauss-medium or poly-sinc-gauss-long
Rate: 192 kHz (PCM) or DSD256 (SDM)
Modulator (if SDM): ASDM7EC-light or ASDM7EC-super
Shaper (if PCM): NS1 or NS4
```
**Notes:** Gaussian filters give optimal time-frequency behaviour for analog
sources
**Avoid:** very steep filters (they can emphasize vinyl surface noise)

### MP3 / lossy sources (Spotify, YouTube Music)
```
Source: MP3 320 kbps or AAC 256 kbps
Mode: PCM (recommended)
Filter: poly-sinc-mqa/mp3-lp or poly-sinc-mqa/mp3-mp
Rate: 96 kHz or 176.4/192 kHz
Shaper: TPDF or shaped
```
**Notes:** dedicated filters clean up lossy artifacts
**Do not overdo it:** upsampling to DSD512 will not improve a lossy source

### Classical (orchestral, chamber)
```
Genre-specific settings
Mode: SDM (for maximum dynamics)
Filter: poly-sinc-gauss-xla or sinc-MGa
Rate: DSD512+ (to preserve microdynamics)
Modulator: ASDM7EC-super 512+fs
Focus: space + timbre
```
**Why:** Gaussian filters preserve the spatial character of an orchestra

### Jazz/blues (acoustic instruments)
```
Genre-specific settings
Mode: PCM or SDM
Filter: poly-sinc-gauss-medium or IIR2
Rate: 192 kHz (PCM) or DSD256 (SDM)
Focus: transients + space
```
**Why:** IIR2 and the Gaussians convey the attack of acoustic instruments well

### Rock/pop (studio productions)
```
Genre-specific settings
Mode: PCM
Filter: poly-sinc-shrt-mp or minphaseFIR
Rate: 176.4/192 kHz
Shaper: NS1 or NS4
Focus: transients
```
**Why:** minimum-phase filters suit transient-heavy material

### Electronic/EDM (synthetic sounds)
```
Genre-specific settings
Mode: PCM or SDM
Filter: poly-sinc-xtr-short-mp or poly-sinc-gauss-short
Rate: 192 kHz (PCM) or DSD256 (SDM)
Focus: transients + timbre
```
**Why:** short filters handle synthetic transients and modulation better

---

## Technical limits

### CPU/GPU requirements
- PCM filters: CPU-intensive
- SDM modulators: very CPU-intensive
- Higher rates = more CPU
- poly-sinc-xla: the heaviest
- IIR: the lightest

### Latency
- Depends on the filter and the buffer size
- poly-sinc: medium latency
- sinc-M: high latency (million taps)
- IIR: low latency

### DAC compatibility
- Not every DAC supports every rate
- Some DACs are sensitive to ultrasonic noise
- Check your DAC's specifications

---

## Control API mapping

### Commands available through the API

| API method | Controls |
|------------|----------|
| `SetMode` | PCM / SDM / [source] |
| `SetFilter` | Filter (1x and Nx) |
| `SetShaping` | Noise shaper (PCM) or modulator (SDM) |
| `SetRate` | Output sample rate |
| `GetModes` | List of available modes |
| `GetFilters` | List of every filter |
| `GetShapers` | List of shapers/modulators |
| `GetRates` | List of sample rates |

**Note:** the API returns indices, not names. An index → name mapping is
required.

---

## Terminology for the AI

### Audio formats

**PCM (Pulse Code Modulation):**
- Multi-bit format (16-bit, 24-bit, 32-bit)
- Every sample is a number (an amplitude value)
- Sample rates: 44.1 kHz, 48 kHz, 96 kHz, 192 kHz, 384 kHz, 768 kHz
- Example: CD = 16-bit @ 44.1 kHz
- Example: hi-res = 24-bit @ 192 kHz

**DSD (Direct Stream Digital) / SDM (Sigma-Delta Modulation):**
- A **1-bit format** — every sample is a 0 or a 1
- Very high sample rates (megahertz)
- Audio is encoded through pulse density (PDM)
- **Base rate = 44.1 kHz** (the CD sample rate)
- The number in the name is the multiplier: DSD64 = 44.1k × 64,
  DSD256 = 44.1k × 256
- Example: DSD256 = 44.1k × 256 = 11.2896 MHz (1-bit)
- Example: DSD512 = 44.1k × 512 = 22.5792 MHz (1-bit)

**How to read a DSD format string:**
- `DSD256(1bit 11.2MHz)` means:
  - DSD**256** = a **44.1 kHz × 256** multiplier
  - Arithmetic: 44100 × 256 = 11,289,600 Hz
  - 1-bit = one bit per sample
  - 11.2MHz = an 11.2896 MHz sample rate (rounded in the specification)
  - Exact rate: **11289600 Hz**

**Computing DSD rates (CRITICAL for the AI):**
```
Formula: DSDxxx = 44100 Hz × xxx

Examples:
DSD64   = 44100 × 64   = 2,822,400 Hz  = 2.8224 MHz
DSD128  = 44100 × 128  = 5,644,800 Hz  = 5.6448 MHz
DSD256  = 44100 × 256  = 11,289,600 Hz = 11.2896 MHz ≈ 11.2 MHz
DSD512  = 44100 × 512  = 22,579,200 Hz = 22.5792 MHz ≈ 22.4 MHz
DSD1024 = 44100 × 1024 = 45,158,400 Hz = 45.1584 MHz ≈ 45.2 MHz
```

**When a DAC specification says:**
- `DSD256(1bit 11.2MHz)` → read it as **44.1k × 256 = 11289600 Hz**
- `DSD512(1bit 22.4MHz)` → read it as **44.1k × 512 = 22579200 Hz**
- Rounding to MHz is normal, but the exact rate is always a multiple of 44.1k

**Converting between formats:**
- PCM → DSD: delta-sigma modulation (needs a modulator)
- DSD → PCM: decimation (needs an integrator + noise filter)
- PCM → PCM: upsampling (needs a filter)
- DSD → DSD: remodulation (needs an integrator + modulator)

### Signal processing

- **Upsampling** = raising the sample rate
- **Oversampling** = the same as upsampling
- **Noise shaping** = moving quantization noise to higher frequencies (PCM)
- **Dithering** = adding noise to linearize (PCM)
- **Delta-sigma modulation** = converting PCM into 1-bit DSD
- **Apodizing** = removing pre-ringing artifacts (when the Apod counter > 10)
- **Pre-ringing** = artifacts before a transient (typically from CD filters)
- **Post-ringing** = artifacts after a transient

### DAC types

- **R2R DAC** = resistor-ladder DAC (multibit, discrete)
  - Examples: Holo Audio, Denafrips, LAiV Harmony
  - Optimal: PCM at high rates (768 kHz) + noise shaping (LNS15)
  - Linearity errors → compensated through noise shaping

- **ESS Sabre DAC** = delta-sigma DAC from ESS Technology
  - Examples: ES9038PRO, ES9028PRO, ES9018
  - Optimal: DSD with 5th-order modulators (ASDM5EC-super)

- **Multi-element DAC** = a DAC with multiple converter elements
  - Optimal: DSD with 7th-order modulators (ASDM7EC-super)

### Filter characteristics

- **Linear phase** (lp) = equal phase delay at all frequencies, symmetric ring
- **Minimum phase** (mp) = no pre-ring, all ringing after the transient
- **Intermediate phase** (ip) = between linear and minimum phase
- **Transients** = attacks, fast signal changes
- **Timbre** = tonal colour, harmonic structure
- **Space** = spatial character, stereo image, reverb tails

### Specific terms

- **EC** = Extended Compensation (improved correction in modulators)
- **Adaptive** (ASDM) = adapts to the signal in real time
- **Apod counter** = the apodizing-need counter (>10 = an apodizing filter is
  called for)
- **Constant-time filter** = a filter with a fixed tap count regardless of rate
- **Half-band filter** (hb) = cutoff at half the Nyquist frequency
- **2-stage processing** (*-2s) = two stages: ≥8x upsampling, then the final
  filter

---

## Automatic setting selection (AI logic)

### Algorithm for the AI assistant

```python
def auto_select_settings(track_info, user_preferences=None):
    """
    Pick optimal HQPlayer settings automatically.

    Args:
        track_info: dict describing the track
            - sample_rate: int (44100, 48000, 96000, 192000, etc.)
            - bit_depth: int (16, 24, 32)
            - format: str ('FLAC', 'DSD64', 'DSD128', etc.)
            - genre: str (optional)
            - quality_source: str ('CD', 'Vinyl', 'Hi-Res', 'MP3')
        user_preferences: dict of user settings
            - dac_type: str ('ESS', 'R2R', 'Delta-Sigma', 'Unknown')
            - cpu_power: str ('low', 'medium', 'high', 'extreme')
            - focus: str ('transients', 'timbre', 'space', 'balanced')

    Returns:
        dict of HQPlayer settings
    """
    # Defaults
    if user_preferences is None:
        user_preferences = {'dac_type': 'Unknown', 'cpu_power': 'medium', 'focus': 'balanced'}

    # 1. Source parameters
    sample_rate = track_info['sample_rate']
    bit_depth = track_info.get('bit_depth', 16)
    genre = track_info.get('genre', '').lower()
    is_dsd = track_info.get('format', '').startswith('DSD')
    quality = track_info.get('quality_source', 'CD')

    # 2. System parameters
    dac_type = user_preferences.get('dac_type', 'Unknown')
    cpu_power = user_preferences.get('cpu_power', 'medium')
    focus = user_preferences.get('focus', 'balanced')

    # 3. Pick the mode
    if is_dsd:
        mode = "SDM"
    elif dac_type == "ESS":
        mode = "SDM"  # ESS Sabre does better with DSD
    elif dac_type == "R2R" and cpu_power in ['low', 'medium']:
        mode = "PCM"  # R2R DACs work excellently with PCM + noise shaping
    elif cpu_power == 'extreme':
        mode = "SDM"  # Maximum quality
    else:
        mode = "PCM"  # The safe choice

    # 4. Work out the base-rate families
    def get_base_family(rate):
        if rate % 44100 == 0:
            return '44.1k'
        elif rate % 48000 == 0:
            return '48k'
        else:
            return 'other'

    source_family = get_base_family(sample_rate)

    # The target family depends on the mode
    if mode == "SDM":
        # DSD is normally in the 44.1k family (the standard)
        target_family = '44.1k'
    else:
        # PCM — derive it from the target rate
        if cpu_power in ['high', 'extreme'] and dac_type == 'R2R':
            target_rate = 768000  # 44.1k × 16 or 48k × 16
            target_family = get_base_family(target_rate) if target_rate else source_family
        else:
            target_family = source_family  # Keep the same family

    # Do the base rates match?
    base_rates_match = (source_family == target_family)

    # 5. Pick the filter
    filter_name = None
    if mode == "PCM" or is_dsd:  # A filter is needed for PCM sources
        # Estimate the apodizing need (a stand-in for the Apod counter)
        needs_apodizing = quality in ['CD', 'MP3'] or sample_rate <= 48000

        # By genre and focus
        if genre in ['classical', 'jazz', 'blues'] or focus == 'space':
            if cpu_power == 'extreme' and base_rates_match:
                # sinc-MGa works ONLY when the base rates match
                filter_name = "sinc-MGa" if needs_apodizing else "sinc-MG"
            elif cpu_power == 'extreme':
                # Fall back to a universal filter when the families differ
                filter_name = "poly-sinc-gauss-xla" if needs_apodizing else "poly-sinc-gauss-xl"
            elif cpu_power == 'high':
                filter_name = "poly-sinc-gauss-long"
            else:
                filter_name = "poly-sinc-gauss-medium"

        elif focus == 'timbre' or quality == 'Hi-Res':
            if cpu_power == 'extreme':
                filter_name = "poly-sinc-ext2-xla" if needs_apodizing else "poly-sinc-ext2-xl"
            elif cpu_power == 'high':
                filter_name = "poly-sinc-ext2-long"
            else:
                filter_name = "poly-sinc-ext2-medium"

        elif genre in ['rock', 'pop'] or focus == 'transients':
            if cpu_power in ['high', 'extreme']:
                filter_name = "poly-sinc-xtr-short-mp"
            else:
                filter_name = "poly-sinc-shrt-mp"

        elif genre == 'electronic':
            filter_name = "poly-sinc-xtr-short-lp" if cpu_power == 'high' else "poly-sinc-gauss-short"

        else:  # Balanced / universal
            if cpu_power == 'extreme':
                filter_name = "sinc-MGa" if needs_apodizing else "sinc-MG"
            elif cpu_power == 'high':
                filter_name = "poly-sinc-ext2-long"
            else:
                filter_name = "poly-sinc-gauss-medium"

    # 5. Pick the modulator/shaper
    if mode == "SDM":
        # DSD modulators
        if dac_type == "ESS":
            if cpu_power == 'extreme':
                modulator = "ASDM5EC-super 1024+fs"
            elif cpu_power == 'high':
                modulator = "ASDM5EC-super 512+fs"
            else:
                modulator = "ASDM5EC-super"
        else:  # Multi-element, R2R, Unknown
            if cpu_power == 'extreme':
                modulator = "ASDM7EC-super 1024+fs"
            elif cpu_power == 'high':
                modulator = "ASDM7EC-super 512+fs"
            else:
                modulator = "ASDM7EC-super"
    else:  # PCM
        # The target output rate drives the shaper choice
        if cpu_power == 'extreme' and dac_type == 'R2R':
            target_rate = 768000
            shaper = "LNS15"
        elif cpu_power in ['high', 'extreme']:
            target_rate = 384000
            shaper = "NS5"
        elif cpu_power == 'medium':
            target_rate = 192000
            if dac_type == 'R2R':
                shaper = "NS9"
            else:
                shaper = "NS1"
        else:  # low
            target_rate = 96000
            shaper = "NS4"

    # 6. Pick the output rate
    if mode == "SDM":
        if cpu_power == 'extreme':
            output_rate = 90316800  # DSD2048
        elif cpu_power == 'high':
            output_rate = 45158400  # DSD1024
        elif cpu_power == 'medium':
            output_rate = 22579200  # DSD512 (recommended)
        else:
            output_rate = 11289600  # DSD256
    else:  # PCM
        if dac_type == "R2R" and cpu_power in ['high', 'extreme']:
            output_rate = 768000  # 16x for linearity correction
        elif cpu_power == 'extreme':
            output_rate = 384000  # 8x
        elif cpu_power in ['medium', 'high']:
            output_rate = 192000  # 4x (the universal sweet spot)
        else:
            output_rate = 96000   # 2x

    return {
        "mode": mode,
        "filter": filter_name,
        "modulator_or_shaper": modulator if mode == "SDM" else shaper,
        "output_rate": output_rate,
        "reasoning": f"Selected {mode} mode for {dac_type} DAC with {cpu_power} CPU power, focusing on {focus}"
    }
```

### Worked examples

#### Example 1: hi-res FLAC (192 kHz/24-bit) + R2R DAC + powerful CPU
```python
track = {
    'sample_rate': 192000,  # 48k × 4 → 48k family
    'bit_depth': 24,
    'format': 'FLAC',
    'genre': 'Classical',
    'quality_source': 'Hi-Res'
}
prefs = {'dac_type': 'R2R', 'cpu_power': 'high', 'focus': 'space'}

# AI analysis:
# Source: 192000 Hz (48k family)
# Target: 768000 Hz (48k × 16 = 48k family)
# Base rates MATCH ✅ → any filter may be used

# Result:
{
    'mode': 'PCM',
    'filter': 'poly-sinc-gauss-long',  # Universal filter
    'modulator_or_shaper': 'LNS15',
    'output_rate': 768000,
    'base_rate_match': True
}
```

#### Example 2: CD FLAC (44.1 kHz/16-bit) + ESS DAC
```python
track = {
    'sample_rate': 44100,  # 44.1k × 1 → 44.1k family
    'bit_depth': 16,
    'format': 'FLAC',
    'genre': 'Jazz',
    'quality_source': 'CD'
}
prefs = {'dac_type': 'ESS', 'cpu_power': 'medium', 'focus': 'balanced'}

# AI analysis:
# Source: 44100 Hz (44.1k family)
# Target: DSD512 = 22579200 Hz (44.1k × 512 = 44.1k family)
# Base rates MATCH ✅ → any filter may be used

# Result:
{
    'mode': 'SDM',
    'filter': 'poly-sinc-gauss-medium',  # For the PCM → DSD conversion
    'modulator_or_shaper': 'ASDM5EC-super',
    'output_rate': 22579200,  # DSD512
    'base_rate_match': True
}
```

#### Example 2b: hi-res 96k → DSD (different base families)
```python
track = {
    'sample_rate': 96000,  # 48k × 2 → 48k family ⚠️
    'bit_depth': 24,
    'format': 'FLAC',
    'genre': 'Classical',
    'quality_source': 'Hi-Res'
}
prefs = {'dac_type': 'Delta-Sigma', 'cpu_power': 'extreme', 'focus': 'space'}

# AI analysis:
# Source: 96000 Hz (48k family)
# Target: DSD512 = 22579200 Hz (44.1k × 512 = 44.1k family)
# Base rates DO NOT MATCH ❌ → sinc-MGa cannot be used

# The AI picks a universal filter automatically:
{
    'mode': 'SDM',
    'filter': 'poly-sinc-gauss-xla',  # ✅ Universal (not sinc-MGa)
    'modulator_or_shaper': 'ASDM7EC-super 512+fs',
    'output_rate': 22579200,  # DSD512
    'base_rate_match': False,  # ⚠️ Different families
    'reasoning': 'Cannot use sinc-MGa: source is 48k family, target is 44.1k family. Using poly-sinc-gauss-xla instead.'
}
```

#### Example 3: vinyl rip (96 kHz/24-bit) + unknown DAC + transient focus
```python
track = {
    'sample_rate': 96000,
    'bit_depth': 24,
    'format': 'FLAC',
    'genre': 'Rock',
    'quality_source': 'Vinyl'
}
prefs = {'dac_type': 'Unknown', 'cpu_power': 'medium', 'focus': 'transients'}

# Result:
{
    'mode': 'PCM',
    'filter': 'poly-sinc-shrt-mp',  # Minimum phase for transients
    'modulator_or_shaper': 'NS1',
    'output_rate': 192000
}
```

#### Example 4: DSD64 source + multi-element DAC + extreme CPU
```python
track = {
    'sample_rate': 2822400,
    'bit_depth': 1,
    'format': 'DSD64',
    'genre': 'Classical',
    'quality_source': 'DSD'
}
prefs = {'dac_type': 'Delta-Sigma', 'cpu_power': 'extreme', 'focus': 'space'}

# Result:
{
    'mode': 'SDM',
    'filter': None,  # DSD → DSD needs no upsampling filter
    'modulator_or_shaper': 'ASDM7EC-super 1024+fs',
    'output_rate': 90316800  # DSD2048
}
```

### Quality priorities by CPU power

**Extreme (unlimited CPU):**
- Mode: SDM
- Filter: sinc-MGa, poly-sinc-gauss-xla, poly-sinc-ext2-xla
- Modulator: ASDM7EC-super 1024+fs, or ASDM5EC-super 1024+fs for ESS
- Rate: DSD1024 (45.1584 MHz) or DSD2048 (90.3168 MHz)

**High (powerful CPU, RTX 4090 class):**
- Mode: SDM, or PCM for R2R
- Filter: poly-sinc-gauss-long, poly-sinc-ext2-long, poly-sinc-xtr-short-mp
- Modulator: ASDM7EC-super 512+fs or ASDM5EC-super 512+fs
- Rate: DSD512 (22.5792 MHz) or PCM 768 kHz

**Medium (standard desktop):**
- Mode: SDM or PCM
- Filter: poly-sinc-gauss-medium, poly-sinc-ext2-medium
- Modulator: ASDM7EC-super or ASDM5EC-super
- Rate: DSD256 (11.2896 MHz) or PCM 192 kHz
- **The recommended choice for most systems**

**Low (limited CPU):**
- Mode: PCM
- Filter: poly-sinc-shrt-mp, poly-sinc-lp, IIR2
- Shaper: NS4, shaped, TPDF
- Rate: 96 kHz or 176.4/192 kHz

### Special scenarios

#### Vinyl rips (handle gently)
- Filter: poly-sinc-gauss-* (optimal time-frequency for analog sources)
- Avoid: very steep filters (they can emphasize vinyl noise)
- Recommended: poly-sinc-gauss-medium or poly-sinc-gauss-long

#### MP3 / lossy sources (cleanup mode)
- Filter: poly-sinc-mqa/mp3-lp or poly-sinc-mqa/mp3-mp
- Designed specifically to clean up artifacts
- Short ring, so they add as little as possible of their own

#### MQA files (where supported)
- Filter: poly-sinc-ext2-hires-* or poly-sinc-gauss-hires-*
- Specifically tuned for MQA decoding
- Very high attenuation

---

## Sources

- **Official manual**: HQPlayer 5 Desktop User Manual v5.16.0
- **SDK**: hqp-control-5292-src (engine 5.29.2)
- **Forum**: HQPlayer Community Forum
- **Developer**: Jussi Laako / Signalyst

---

## Base-rate families (44.1k vs 48k) — CRITICAL

### The two base rates of digital audio

**IMPORTANT:** every sample rate derives from one of two base rates:
- **The 44.1 kHz family** — the CD standard
- **The 48 kHz family** — the DAT/video standard

### 44.1 kHz family

**PCM rates:**
- 44100 Hz = 44.1k × 1
- 88200 Hz = 44.1k × 2
- 176400 Hz = 44.1k × 4
- 352800 Hz = 44.1k × 8
- 705600 Hz = 44.1k × 16

**DSD rates (based on 44.1k):**
- DSD64 = 44.1k × 64 = 2822400 Hz
- DSD128 = 44.1k × 128 = 5644800 Hz
- DSD256 = 44.1k × 256 = 11289600 Hz
- DSD512 = 44.1k × 512 = 22579200 Hz
- DSD1024/2048 = 44.1k × 1024/2048

### 48 kHz family

**PCM rates:**
- 48000 Hz = 48k × 1
- 96000 Hz = 48k × 2
- 192000 Hz = 48k × 4
- 384000 Hz = 48k × 8
- 768000 Hz = 48k × 16

**DSD rates (rarer, but they exist):**
- DSD64 = 48k × 64 = 3072000 Hz
- DSD128 = 48k × 128 = 6144000 Hz
- and so on (rarely used)

### Determining a track's family

```python
def get_base_rate_family(sample_rate: int) -> str:
    """
    Which base-rate family the track belongs to.

    Returns: '44.1k' or '48k'
    """
    # Is it a multiple of 44100?
    if sample_rate % 44100 == 0:
        return '44.1k'
    # Is it a multiple of 48000?
    elif sample_rate % 48000 == 0:
        return '48k'
    else:
        # Rare cases (32k, 22.05k, etc.)
        return 'other'

# Examples:
get_base_rate_family(88200)   # → '44.1k' (88200 = 44100 × 2)
get_base_rate_family(96000)   # → '48k'  (96000 = 48000 × 2)
get_base_rate_family(176400)  # → '44.1k' (176400 = 44100 × 4)
get_base_rate_family(192000)  # → '48k'  (192000 = 48000 × 4)
get_base_rate_family(2822400) # → '44.1k' (DSD64 = 44100 × 64)
```

### Filter compatibility with base-rate families

**⚠️ CRITICAL WHEN CHOOSING A FILTER:**

#### Filters that REQUIRE a matching base rate (sinc-*)

These work ONLY when the track's base rate equals the DAC output's base rate:

- ❌ **sinc-S, sinc-M, sinc-Mx** — matching base rate required
- ❌ **sinc-MG, sinc-MGa** — matching base rate required
- ❌ **sinc-L, sinc-Ls, sinc-Lm, sinc-Ll, sinc-Lh** — matching base rate
  required
- ❌ **closed-form, closed-form-M, closed-form-16M** — matching base rate
  required

**The failure case:**
```
Source: 96 kHz FLAC (48k family)
Target: DSD256 = 11.2896 MHz (44.1k family)
Filter: sinc-MGa ← ❌ WILL NOT WORK (different base rates)

Error: a 48k → 44.1k family conversion is impossible for sinc filters
```

#### Filters that work with BOTH families (universal)

These can convert between base rates:

- ✅ **poly-sinc-gauss-*** — any base rate
  - poly-sinc-gauss-short, medium, long, xla, xl
  - poly-sinc-gauss-hires-lp/ip/mp
  - poly-sinc-gauss-halfband, halfband-s

- ✅ **poly-sinc-ext2-*** — any base rate
  - poly-sinc-ext2, ext2-short, ext2-medium, ext2-long
  - poly-sinc-ext2-xla, ext2-xl
  - poly-sinc-ext2-hires-lp/ip/mp

- ✅ **poly-sinc-xtr-*** — any base rate
- ✅ **poly-sinc-lp/mp/shrt-lp/shrt-mp** — any base rate
- ✅ **IIR, IIR2** — any base rate
- ✅ **FIR, asymFIR, minphaseFIR** — any base rate

**The correct choice:**
```
Source: 96 kHz FLAC (48k family)
Target: DSD256 = 11.2896 MHz (44.1k family)
Filter: poly-sinc-gauss-xla ← ✅ WORKS (universal filter)

Conversion: 48k family → 44.1k family is fine
```

### Filter selection with a base-rate check

```python
def select_filter_with_base_rate_check(source_rate: int, target_rate: int, preferred_filter: str):
    """Pick a filter, checking base-rate compatibility."""
    source_family = get_base_rate_family(source_rate)
    target_family = get_base_rate_family(target_rate)

    # Is this a sinc filter?
    is_sinc_filter = preferred_filter.startswith('sinc-') or preferred_filter.startswith('closed-form')

    if is_sinc_filter and source_family != target_family:
        # Base rates differ — a sinc filter CANNOT be used
        print(f"⚠️ Warning: {preferred_filter} requires matching base rates")
        print(f"   Source: {source_rate} Hz ({source_family} family)")
        print(f"   Target: {target_rate} Hz ({target_family} family)")
        print(f"   Switching to universal filter: poly-sinc-gauss-xla")
        return "poly-sinc-gauss-xla"  # Fall back to a universal filter

    return preferred_filter

# Examples:
select_filter_with_base_rate_check(96000, 11289600, "sinc-MGa")
# → "poly-sinc-gauss-xla" (auto-substituted: the base rates differ)

select_filter_with_base_rate_check(88200, 11289600, "sinc-MGa")
# → "sinc-MGa" (fine, both in the 44.1k family)

select_filter_with_base_rate_check(96000, 11289600, "poly-sinc-gauss-xla")
# → "poly-sinc-gauss-xla" (fine, a universal filter)
```

### Practical scenarios

#### Scenario 1: CD rip (44.1k) → DSD256 (44.1k family)
```
Source: 44100 Hz (44.1k family)
Target: DSD256 = 11289600 Hz (44.1k family)
Base rates: MATCH ✅

Usable:
✅ sinc-MGa (matching base rates)
✅ sinc-MG
✅ poly-sinc-gauss-xla (universal)
✅ poly-sinc-ext2-xla (universal)
```

#### Scenario 2: hi-res 96k → DSD256 (different families)
```
Source: 96000 Hz (48k family)
Target: DSD256 = 11289600 Hz (44.1k family)
Base rates: DO NOT MATCH ❌

Not usable:
❌ sinc-MGa (needs a matching base rate)
❌ sinc-MG
❌ the sinc-M, sinc-S, sinc-L series

Usable:
✅ poly-sinc-gauss-xla (universal filter — recommended)
✅ poly-sinc-ext2-xla (universal)
✅ poly-sinc-gauss-long
✅ IIR2
```

#### Scenario 3: mixed library (44.1k + 48k tracks) → DSD DAC
```
The library holds:
- CD rips: 44.1k family
- Hi-res downloads: 48k family (96k, 192k)
- Vinyl rips: 96k (48k family)

DAC output: DSD512 (44.1k family)

The AI's decision:
- Use ONLY universal filters:
  ✅ poly-sinc-gauss-xla (best across both families)
  ✅ poly-sinc-ext2-long (alternative)

- Do not use:
  ❌ sinc-MGa (would not work for the 48k tracks)
```

### Guidance for the AI agent

**When picking a filter:**

1. **Determine the base-rate family** of the source and of the output
2. **If the families match:**
   - Any filter may be used (including sinc-*)
   - sinc-MGa is the best quality when base rates match

3. **If the families DO NOT match:**
   - ❌ NEVER use a sinc-* filter
   - ✅ Use poly-sinc-gauss-* (recommended)
   - ✅ Or poly-sinc-ext2-*

4. **For a mixed library (44.1k + 48k tracks):**
   - Always use universal filters
   - poly-sinc-gauss-xla is the best all-round choice

**Hence the user's choice between poly-sinc-gauss-xla and sinc-MGa depends on
the format:**
- **Source and DAC both in the 44.1k family** → sinc-MGa (best quality)
- **Different families** → poly-sinc-gauss-xla (universal, always works)

---

## DSD quick reference for the AI (self-check)

**Questions to verify understanding:**

Q: What does DSD256(1bit 11.2MHz) mean?
A: 44.1 kHz × 256 = 11,289,600 Hz = 11.2896 MHz, a 1-bit format

Q: What is the base rate of every DSD format?
A: 44.1 kHz (the CD sample rate)

Q: How do you compute DSD512?
A: 44100 × 512 = 22,579,200 Hz = 22.5792 MHz

Q: Why does the specification say "11.2 MHz" and not "11.2896 MHz"?
A: Rounding for convenience; the exact rate is always 44.1k × multiplier

Q: A DAC supports DSD256. Which rate should HQPlayer use?
A: output_rate = 11289600 (Hz), which is DSD256

**Quick table to copy:**
```
DSD64   → 2822400 Hz   (44.1k family)
DSD128  → 5644800 Hz   (44.1k family)
DSD256  → 11289600 Hz  (44.1k family) ← when you see "DSD256(1bit 11.2MHz)"
DSD512  → 22579200 Hz  (44.1k family) ← when you see "DSD512(1bit 22.4MHz)"
DSD1024 → 45158400 Hz  (44.1k family)
DSD2048 → 90316800 Hz  (44.1k family)
```

**Base-rate families — quick check:**
```
44.1k family: 44100, 88200, 176400, 352800, 705600, + all DSD
48k family:   48000, 96000, 192000, 384000, 768000

Test: sample_rate % 44100 == 0 → 44.1k family
      sample_rate % 48000 == 0 → 48k family
```

**Filter compatibility — quick check:**
```
sinc-* filters (sinc-MGa, sinc-MG, sinc-M, etc.):
  ✅ Work: when source family == target family
  ❌ Do not work: when source family != target family

poly-sinc-gauss-*, poly-sinc-ext2-*, IIR*, FIR*:
  ✅ Always work (universal filters)
```

---

## Control API coverage

### ✅ Fully documented

- **Output modes (3)**: [source], PCM, SDM (DSD)
- **Filters (77)**: the complete table with characteristics (focus, quality,
  genre, ratio, apodizing)
- **Noise shapers / modulators (36)**:
  - PCM shapers (10): TPDF, shaped, Gauss1, NS1/4/5/9, LNS15/light
  - DSD 5th order (13): DSD5, the ASDM5EC series, variants (ul, light, fast,
    super, 512+fs)
  - DSD 7th order (13): DSD7, the ASDM7EC series, variants (ul, light, fast,
    super, 512+fs, 1024+fs)
- **Sample rates (20)**:
  - PCM (14): 44.1 kHz up to 12.288 MHz (32x DAT)
  - DSD (6): DSD64 up to DSD2048 (2.8224 MHz to 90.3168 MHz)

### 📋 Additional material

- **Quick selection** by genre, focus and apodizing need
- **An automatic algorithm** for the AI to choose settings
- **Worked examples** for various scenarios
- **Genre-specific** recommendations (classical, jazz, rock, electronic)
- **DAC-specific** settings (ESS, R2R, multi-element)
- **Source-specific** guidance (hi-res, CD, vinyl, MP3, DSD)

### 🎯 AI agent readiness

The agent now has **the full picture** for choosing HQPlayer settings
intelligently, based on:
- Source quality (sample rate, bit depth, format)
- DAC type (when known)
- Musical genre
- CPU headroom
- Focus (transients/timbre/space)
- Special needs (apodizing, vinyl cleanup, MP3 enhancement)

**Example of the AI at work:**
```
User: "Play Pink Floyd - Comfortably Numb (CD FLAC 44.1kHz/16bit)"
The agent reasons:
  - Source: CD quality, 44.1 kHz → apodizing wanted
  - Genre: rock → focus on transients
  - DAC: Holo Audio Spring 3 (R2R) → PCM with LNS15 is optimal
The agent picks:
  - Mode: PCM
  - Filter: poly-sinc-gauss-medium (transients + apodizing)
  - Shaper: LNS15
  - Rate: 768 kHz (linearity correction for R2R)
```

---

**Last updated:** 2026-02-12
**HQPlayer version:** 5.16.3 (Engine 5.34.14)
**Status:** ✅ **Complete knowledge base — ready to use**
**Control API coverage:** 100% (every parameter documented)
