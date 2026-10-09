# HQPlayer — a reference for people

> **What this is.** What HQPlayer's modes, filters, modulators and rates are,
> the lists HQPlayer itself reports, and the advice that has a source: the
> HQPlayer manual, cited by version and section where it has one, and the
> author's dated public statements, attributed. Nothing here ranks one
> setting above another, and nothing here is Sautium's own recommendation.
>
> **Which HQPlayer.** The lists are what HQPlayer engine 6.2.3 (HQPlayer
> Desktop 6.1.0) reports at runtime — `GetFilters` and `GetShapers`, in PCM
> mode and in SDM mode. Names, descriptions and ratio classes belong to a
> build: another HQPlayer may add, rename or drop names (see "Changes since
> the HQPlayer 5 manual"). Sautium reads them live from the running HQPlayer,
> and the assistant works from those live lists, not from this file.

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

## Changes since the HQPlayer 5 manual

From Signalyst's public release notes:

- **5.14 (July 2025):** the `poly-sinc-hb` filters became two-stage (`-2s`).
- **5.15 (September 2025):** the `poly-sinc-ext2` group was completed to
  mirror the Gaussian group (`-short`, `-medium`, `-long`, `-xl`, `-xla`,
  `-hires-lp`, `-hires-ip`, `-hires-mp`). `poly-sinc-ext3` is gone; its
  successor is `poly-sinc-ext2-xla`.
- **5.17 (March 2026):** the processing-speed factor (Status
  `process_speed`).
- **6.0.0 (May 2026):** an "extended set of source content cleanup filters"
  ([release note](https://signalyst.com/hqplayer-6-desktop-6-0-0-released/)).
- **6.1.0 (September 2026):** the `AHM5EC4B` and `AHM7EC4B` modulators, "to
  make most out of DSD1024+ output rates", and the modulator generation passed
  to control apps — the `Gen1`…`Gen8` in `GetShapers`
  ([release note](https://signalyst.com/hqplayer-6-desktop-6-1-0-released/)).

---

## Output modes

### 1. **[source]**
- Plays in the source format, no upsampling
- Adaptive sample-rate selection
- Minimal processing

### 2. **PCM**
- Upsamples PCM to higher sample rates
- Noise shaping and dithering

### 3. **SDM (DSD)**
- Conversion to DSD (Direct Stream Digital)
- **1-bit format** — every sample is a 0 or a 1
- Delta-sigma modulation

HQPlayer lists the output rates the current output offers (`GetRates`), so
the rates on offer depend on the output device and on HQPlayer's settings.

**What DSD is:**
- A **1-bit audio format**, unlike PCM (16/24/32-bit)
- Very high sample rates (megahertz instead of kilohertz)
- The signal is encoded through the **density** of ones (PDM — Pulse Density
  Modulation)
- More ones = higher amplitude, fewer ones = lower amplitude

**DSD rates are multiples of a base rate.** The number in the name is the
multiplier of 44.1 kHz, the CD rate: **DSDxxx = 44.1 kHz × xxx**. HQPlayer also
offers DSD rates of the 48 kHz family (48 kHz × 64 = 3.072 MHz, and so on)
when 48k-family DSD is enabled in its settings and the output takes them.

**DSD rates of the 44.1 kHz family:**
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

**Note:** specifications often round (11.2 MHz instead of 11.2896 MHz); the
exact rate is the base rate × the multiplier.

---

## PCM settings

### Dither and noise shapers (PCM)

HQPlayer 6.2.3 lists ten in PCM mode (`GetShapers`), in this order:

`none` · `NS1` · `NS4` · `NS5` · `NS9` · `LNS15` · `RPDF` · `TPDF` · `Gauss1` · `shaped`

HQPlayer gives no description for them. The names read: `RPDF`, `TPDF` and
`Gauss1` are dither with a rectangular, triangular or Gaussian probability
density; `shaped` is shaped dither; `NS1`, `NS4`, `NS5`, `NS9` and `LNS15` are
noise shapers of the order in the name.

**The manual's notes** (HQPlayer Desktop manual 5.7.3):
- LNS15 is designed for 16x rates (705.6/768 kHz), and the manual advises
  against it below 8x rates.
- Noise shaping with NS9, NS5 or LNS15 at high output rates reduces the
  linearity errors of an R2R ladder.

Named in earlier references, **not listed by 6.2.3**: `LNS15 light`.

### DAC bits (R2R DACs)

The DAC bits setting is the word length HQPlayer dithers and noise-shapes to.

- **The manual** (5.7.3) gives 20 DAC bits for Holo Audio and Denafrips R2R
  DACs, together with high-rate noise shaping, to reduce the ladder's
  low-level nonlinearity.
- **Jussi Laako** (Roon › HQ Player,
  ["Which HQP Filter are you using? [2024]"](https://community.roonlabs.com/t/which-hqp-filter-are-you-using-2024/261032),
  17 January 2024, posts #11, #15 and #17): for a Denafrips Ares II (NOS,
  updated FPGA firmware) he "arrived at 19 bits giving best results" — DSD at
  the highest possible rate, or PCM with 19 DAC bits and LNS15.

The manual and the post differ for Denafrips; both are given here.

---

## SDM (DSD) settings

### Modulators

HQPlayer 6.2.3 lists 36 modulators in SDM mode (`GetShapers`), in this
order. The generation is HQPlayer's own field (since 6.1.0).

| # | Modulator | Generation |
|---|---|---|
| 0 | `DSD5` | Gen1 |
| 1 | `DSD5v2` | Gen2 |
| 2 | `DSD5v2 256+fs` | Gen2 |
| 3 | `DSD5EC` | Gen4 |
| 4 | `ASDM5` | Gen3 |
| 5 | `ASDM5EC` | Gen4 |
| 6 | `ASDM5ECv2` | Gen5 |
| 7 | `ASDM5ECv3` | Gen6 |
| 8 | `ASDM5EC-ul` | Gen7 |
| 9 | `ASDM5EC-light` | Gen7 |
| 10 | `ASDM5EC-fast` | Gen7 |
| 11 | `ASDM5EC-super` | Gen7 |
| 12 | `ASDM5EC-ul 512+fs` | Gen7 |
| 13 | `ASDM5EC-light 512+fs` | Gen7 |
| 14 | `ASDM5EC-fast 512+fs` | Gen7 |
| 15 | `ASDM5EC-super 512+fs` | Gen7 |
| 16 | `DSD7` | Gen2 |
| 17 | `DSD7 256+fs` | Gen2 |
| 18 | `ASDM7` | Gen3 |
| 19 | `ASDM7EC` | Gen4 |
| 20 | `ASDM7ECv2` | Gen5 |
| 21 | `ASDM7ECv3` | Gen6 |
| 22 | `ASDM7EC-ul` | Gen7 |
| 23 | `ASDM7EC-light` | Gen7 |
| 24 | `ASDM7EC-fast` | Gen7 |
| 25 | `ASDM7EC-super` | Gen7 |
| 26 | `ASDM7EC-ul 512+fs` | Gen7 |
| 27 | `ASDM7EC-light 512+fs` | Gen7 |
| 28 | `ASDM7EC-fast 512+fs` | Gen7 |
| 29 | `ASDM7EC-super 512+fs` | Gen7 |
| 30 | `AMSDM7 512+fs` | Gen3 |
| 31 | `AMSDM7EC 512+fs` | Gen4 |
| 32 | `AHM5EC4B` | Gen8 |
| 33 | `AHM7EC4B` | Gen8 |
| 34 | `AHM5EC8B` | Gen8 |
| 35 | `AHM7EC8B` | Gen8 |

The names read:
- `5` / `7` — the modulator's order;
- `ASDM` — adaptive;
- `EC` — extended compensation;
- `256+fs`, `512+fs` — the rate multiple the variant is named for;
- `AMSDM` — adaptive modulators the manual describes as pseudo-multi-bit;
- `AHM` — hybrid modulators. Signalyst's release notes place both AHM pairs
  at DSD1024+ output rates (the `8B` pair in 5.13, the `4B` pair in 6.1.0).

Named in earlier references, **not listed by 6.2.3**: `DSD7 512+fs`,
`ASDM7EC-super 1024+fs`, `AHM5EC5L`, `AHM7EC5L`.

**Advice with a source:**
- **ESS Sabre DACs:** 5th-order modulators — the manual (5.7.3).
- **Jussi Laako** (Roon › HQ Player, "HQPlayer Configuration Strategy for
  Various DACs", 19 June 2022,
  [#29](https://community.roonlabs.com/t/hqplayer-configuration-strategy-for-various-dacs/170467/29)):
  for ESS DACs, SDM at 44.1k × 256, with 48k-family DSD disabled unless the
  DAC actually supports it — a "common source for such noises".

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
| **XFi** | Extreme fidelity medium |

**Default:** XFi

### DSD → PCM conversion

**Noise filters:**
- **standard**
- **low** — flat noise profile
- **high-order** — for high-order modulators
- **medium** — gentle, minimal out-of-band noise
- **brickwall** — lets no out-of-band noise through

**Conversion types:**
- **poly-short-lp** — linear-phase slow roll-off
- **poly-short-mp** — minimum-phase slow roll-off
- **poly-ext2** — extended frequency response
- **poly-gauss-long** — Gaussian, long
- **none** — no decimation (output = DSD rate)

---

## Filters / oversampling

### Structure
- **1x filters** — for source rates < 50 kHz (base rates)
- **Nx filters** — for everything above the 1x rates

### The list

HQPlayer 6.2.3 lists 67 filters in PCM mode and 77 in SDM mode — 84 names.
Each cell is HQPlayer's own description of the filter in that mode:
**Signalyst's rating · the author's focus words · the ratio class**. Rows
follow PCM mode's order; the names only SDM mode lists come after, in its
order. "—" means the mode does not list the name.

| Filter | PCM mode | SDM mode |
|---|---|---|
| `none` | 1/5 · 1:1 | — |
| `IIR` | 2/5 · Int | 2/5 · Int |
| `IIR2` | 4/5 · Int | 4/5 · Int |
| `FIR` | 3/5 · Int | 3/5 · Int |
| `asymFIR` | 3/5 · Int | 3/5 · Int |
| `minphaseFIR` | 3/5 · Int | 3/5 · Int |
| `FFT` | 4/5 · 2ˣ | 4/5 · 2ˣ |
| `poly-sinc-lp` | 4/5 · space · Any | 4/5 · space · Any |
| `poly-sinc-mp` | 4/5 · transients · Any | 4/5 · transients · Any |
| `poly-sinc-short-lp` | 3/5 · space, transients · Any | 3/5 · transients, space · Any |
| `poly-sinc-short-mp` | 3/5 · transients · Any | 3/5 · transients · Any |
| `poly-sinc-long-lp` | 4/5 · space · Any | 4/5 · space · Any |
| `poly-sinc-long-ip` | 4/5 · space, transients · Any | 4/5 · space, transients · Any |
| `poly-sinc-long-mp` | 4/5 · transients · Any | 4/5 · transients · Any |
| `poly-sinc-hb` | 4/5 · Any | 4/5 · Any |
| `poly-sinc-hb-xs` | 2/5 · Any | — |
| `poly-sinc-hb-s` | 3/5 · Any | — |
| `poly-sinc-hb-m` | 3/5 · Any | — |
| `poly-sinc-hb-l` | 4/5 · Any | — |
| `poly-sinc-ext` | 3/5 · Int | 3/5 · Int |
| `poly-sinc-ext2` | 5/5 · timbre · Any | 5/5 · timbre · Any |
| `poly-sinc-ext2-short` | 4/5 · timbre · Int up | 4/5 · timbre · Int |
| `poly-sinc-ext2-medium` | 4/5 · timbre · Any | 4/5 · timbre · Any |
| `poly-sinc-ext2-long` | 5/5 · timbre · Any | 5/5 · timbre · Any |
| `poly-sinc-ext2-xla` | 5/5 · timbre · Any | 5/5 · timbre · Any |
| `poly-sinc-ext2-xl` | 5/5 · timbre · Any | 5/5 · timbre · Any |
| `poly-sinc-ext2-hires-lp` | 5/5 · timbre · Any | 5/5 · timbre · Any |
| `poly-sinc-ext2-hires-ip` | 5/5 · timbre · Any | 5/5 · timbre · Any |
| `poly-sinc-ext2-hires-mp` | 5/5 · timbre · Any | 5/5 · timbre · Any |
| `poly-sinc-mqa/mp3-lp` | 4/5 · transients · Int up | 4/5 · transients · Any |
| `poly-sinc-mqa/mp3-mp` | 4/5 · transients · Int up | 4/5 · transients · Any |
| `poly-sinc-xtr-lp` | 5/5 · timbre · Any | 5/5 · timbre · Any |
| `poly-sinc-xtr-mp` | 5/5 · timbre · Any | 5/5 · timbre · Any |
| `poly-sinc-xtr-short-lp` | 5/5 · timbre · Any | 5/5 · timbre, transients · Any |
| `poly-sinc-xtr-short-mp` | 5/5 · timbre · Any | 5/5 · timbre, transients · Any |
| `poly-sinc-gauss-short` | 3/5 · transients · Int up | 3/5 · transients · Int |
| `poly-sinc-gauss-medium` | 4/5 · transients, timbre · Any | 4/5 · transients, timbre · Any |
| `poly-sinc-gauss-long` | 5/5 · transients, timbre, space · Any | 5/5 · transients, timbre, space · Any |
| `poly-sinc-gauss-xla` | 5/5 · transients, timbre, space · Any | 5/5 · transients, timbre, space · Any |
| `poly-sinc-gauss-xl` | 5/5 · transients, timbre, space · Any | 5/5 · transients, timbre, space · Any |
| `poly-sinc-gauss-hires-lp` | 5/5 · transients, timbre, space · Any | 5/5 · transients, timbre, space · Any |
| `poly-sinc-gauss-hires-ip` | 5/5 · transients, timbre, space · Any | 5/5 · transients, timbre, space · Any |
| `poly-sinc-gauss-hires-mp` | 5/5 · transients, timbre, space · Any | 5/5 · transients, timbre, space · Any |
| `poly-sinc-gauss-halfband` | 4/5 · transients, timbre, space · Any | 4/5 · transients, timbre, space · Any |
| `poly-sinc-gauss-halfband-s` | 3/5 · transients, timbre, space · Any | 3/5 · transients, timbre, space · Any |
| `ASRC` | 2/5 · Any | — |
| `polynomial-1` | 1/5 · Int up | 1/5 · Int |
| `polynomial-2` | 1/5 · Int up | 1/5 · Int |
| `minringFIR-lp` | 2/5 · transients · Int up | 2/5 · transients · Int |
| `minringFIR-mp` | 2/5 · transients · Int up | 2/5 · transients · Int |
| `closed-form` | 3/5 · 2ˣ up | 3/5 · 2ˣ |
| `closed-form-fast` | 2/5 · 2ˣ up | 2/5 · 2ˣ |
| `closed-form-M` | 3/5 · 2ˣ up | — |
| `sinc-S` | 4/5 · space, timbre · 2ˣ up | 4/5 · space, timbre · 2ˣ |
| `sinc-M` | 4/5 · space, timbre · 2ˣ up | 4/5 · space, timbre · 2ˣ |
| `sinc-Mx` | 4/5 · space, timbre · 2ˣ up | 4/5 · space, timbre · 2ˣ |
| `sinc-MG` | 4/5 · transients, timbre, space · 2ˣ up | 4/5 · transients, timbre, space · 2ˣ |
| `sinc-MGa` | 4/5 · transients, timbre, space · 2ˣ up | 4/5 · transients, timbre, space · 2ˣ |
| `sinc-L` | 3/5 · 2ˣ up | 3/5 · 2ˣ |
| `sinc-Ls` | 2/5 · 2ˣ up | 2/5 · 2ˣ |
| `sinc-Lm` | 2/5 · 2ˣ up | 2/5 · 2ˣ |
| `sinc-Ll` | 3/5 · 2ˣ up | 3/5 · 2ˣ |
| `sinc-Lh` | 4/5 · 2ˣ up | 4/5 · 2ˣ |
| `sinc-short` | 2/5 · Any up | 2/5 · Any |
| `sinc-medium` | 2/5 · Any up | 2/5 · Any |
| `sinc-long` | 3/5 · Any up | 3/5 · Any |
| `sinc-long-h` | 4/5 · Any | 4/5 · Any |
| `poly-sinc-hb-xs-2s` | — | 2/5 · Any |
| `poly-sinc-hb-s-2s` | — | 3/5 · Any |
| `poly-sinc-hb-m-2s` | — | 3/5 · Any |
| `poly-sinc-hb-l-2s` | — | 4/5 · Any |
| `poly-sinc-lp-2s` | — | 4/5 · space · Any |
| `poly-sinc-mp-2s` | — | 4/5 · transients · Any |
| `poly-sinc-short-lp-2s` | — | 3/5 · transients, space · Any |
| `poly-sinc-short-mp-2s` | — | 3/5 · transients · Any |
| `poly-sinc-long-lp-2s` | — | 4/5 · space · Any |
| `poly-sinc-long-ip-2s` | — | 4/5 · space, transients · Any |
| `poly-sinc-long-mp-2s` | — | 4/5 · transients · Any |
| `poly-sinc-hb-2s` | — | 4/5 · Any |
| `poly-sinc-xtr-lp-2s` | — | 5/5 · timbre · Any |
| `poly-sinc-xtr-mp-2s` | — | 5/5 · timbre · Any |
| `poly-sinc-xtr-short-lp-2s` | — | 5/5 · timbre, transients · Any |
| `poly-sinc-xtr-short-mp-2s` | — | 5/5 · timbre, transients · Any |
| `closed-form-16M` | — | 3/5 · 2ˣ |

### Reading the list

- **Signalyst's rating (n/5)** is Signalyst's technical rating relative to the
  other filters — **not a listening score**. It says nothing about what a
  listener will prefer on a given system.
- **Focus words** (transients, timbre, space) are the author's, as HQPlayer
  shows them — not a recommendation for a kind of music.
- **Ratio class**: `Int` — integer ratios; `2ˣ` — power-of-two ratios;
  `Any` — any ratio; `1:1` — no conversion; `up` — upsampling only. The class
  can differ between modes: `poly-sinc-mqa/mp3-lp` is `Int up` in PCM mode
  and `Any` in SDM mode.
- Names ending in **`-2s`** are two-stage variants. 6.2.3 lists them in SDM
  mode only.
- **The manual's notes** (5.7.3): it advises against ASRC (heavy to compute)
  and against polynomial-1 and polynomial-2 (weak stop-band rejection).

### Ratio class and rate families

- A **`2ˣ`** filter converts only by powers of two, so the output stays in
  the source's family: 44.1 kHz → 88.2 … 705.6 kHz, 48 kHz → 96 … 768 kHz.
- An **`Int`** filter converts by integer ratios, which also stay inside a
  family: 48/44.1 is not an integer.
- Only an **`Any`** filter converts across families — 44.1 kHz to 768 kHz, or
  a 96 kHz source to a 44.1k-family DSD rate.
- **In 6.2.3:**
  - `sinc-short`, `sinc-medium` and `sinc-long` are `Any up` in PCM mode and
    `Any` in SDM mode;
  - `sinc-long-h` is `Any`;
  - `sinc-S`, `sinc-M`, `sinc-Mx`, `sinc-MG`, `sinc-MGa`, `sinc-L`, `sinc-Ls`,
    `sinc-Lm`, `sinc-Ll`, `sinc-Lh` and the `closed-form` filters are `2ˣ up`
    in PCM mode and `2ˣ` in SDM mode;
  - `FFT` is `2ˣ`.
- **When the filter's class cannot reach the output rate,** HQPlayer refuses
  the conversion. Its log says "Requested filter not possible with this rate
  combination". With adaptive output rate on, HQPlayer picks an output rate
  the filter can reach.

### Special filters

#### sinc-MGa vs sinc-MG
- **sinc-MG**: not apodizing, a variant of poly-sinc-gauss-xl
- **sinc-MGa**: APODIZING, a variant of poly-sinc-gauss-xla
- Both: million taps @ 16x rates (65536 x conversion ratio)
- Both: extremely high attenuation

#### Constant-time filters
- **sinc-Mx**: 65536 x conversion ratio
- **sinc-MG**: 65536 x conversion ratio
- **sinc-MGa**: 65536 x conversion ratio
- Million taps at 16x PCM output rates (768 kHz)

### Apodizing

HQPlayer's Apod counter flags errors it detects in the source. The manual
(5.7.3, §4.5) advises an apodizing filter at least when the counter goes
above 10 during a single track.

### Choosing

No filter, modulator or rate is the best in general, and this reference
recommends none. The author on choosing:

> "higher load doesn't necessarily mean better result… poly-sinc-ext2 and
> poly-sinc-gauss-long/poly-sinc-gauss-hires are just as good"
>
> "don't worry too much, focus more on what you like. Although if manual says
> 'not recommended' about some algorithm you can take a note about that."

— Jussi Laako, Roon › HQ Player, "Which HQP Filter are you using? [2024]",
16 January 2024,
[#9](https://community.roonlabs.com/t/which-hqp-filter-are-you-using-2024/261032/9).

---

## Sample rates

### PCM rates

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
| 768000 | 0.768 | 16x | 16x DAT rate |
| 1536000 | 1.536 | 32x | 32x DAT (experimental) |
| 3072000 | 3.072 | 64x | 64x DAT (experimental) |
| 6144000 | 6.144 | 128x | 128x DAT (experimental) |
| 12288000 | 12.288 | 256x | 256x DAT (experimental) |

### DSD rates

| Rate (Hz) | Rate (MHz) | DSD name | Multiplier | Formula |
|-----------|------------|----------|------------|---------|
| 2822400 | 2.8224 | DSD64 | 64x | 44.1k × 64 |
| 5644800 | 5.6448 | DSD128 | 128x | 44.1k × 128 |
| 11289600 | 11.2896 | DSD256 | 256x | 44.1k × 256 |
| 22579200 | 22.5792 | DSD512 | 512x | 44.1k × 512 |
| 45158400 | 45.1584 | DSD1024 | 1024x | 44.1k × 1024 |
| 90316800 | 90.3168 | DSD2048 | 2048x | 44.1k × 2048 |

The 48 kHz family's DSD rates are 48k × the same multipliers (3.072 MHz,
6.144 MHz, …).

**Limits:**
- Not every DAC supports every rate
- HQPlayer lists what the current output offers (`GetRates`)
- Check your DAC's specifications

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

## Technical limits

### CPU/GPU requirements
- PCM filters: CPU-intensive
- SDM modulators: very CPU-intensive
- Higher rates = more CPU
- Sautium's benchmark (the HQPlayer screen) measures what this host keeps up
  with

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

**Note:** the Set* commands take indices. The lists carry an index and a
name for each entry, and the indices differ between PCM and SDM mode, so a
name is resolved to its index in the current mode.

---

## Terminology

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
- The number in the name is the multiplier of 44.1 kHz: DSD64 = 44.1k × 64,
  DSD256 = 44.1k × 256; 48k-family DSD multiplies 48 kHz
- Example: DSD256 = 44.1k × 256 = 11.2896 MHz (1-bit)
- Example: DSD512 = 44.1k × 512 = 22.5792 MHz (1-bit)

**How to read a DSD format string:**
- `DSD256(1bit 11.2MHz)` means:
  - DSD**256** = a **44.1 kHz × 256** multiplier
  - Arithmetic: 44100 × 256 = 11,289,600 Hz
  - 1-bit = one bit per sample
  - 11.2MHz = an 11.2896 MHz sample rate (rounded in the specification)
  - Exact rate: **11289600 Hz**

**Computing DSD rates:**
```
Formula: DSDxxx = 44100 Hz × xxx (48k family: 48000 Hz × xxx)

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
- Rounding to MHz is normal; the exact rate is the base rate × the multiplier

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
- **Apodizing** = removing pre-ringing artifacts from the source
- **Pre-ringing** = artifacts before a transient (typically from CD filters)
- **Post-ringing** = artifacts after a transient

### DAC types

- **R2R DAC** = resistor-ladder DAC (multibit, discrete)
  - Examples: Holo Audio, Denafrips, LAiV Harmony
  - What the manual and the author say about R2R DACs: "DAC bits (R2R
    DACs)" and "Dither and noise shapers (PCM)" above

- **ESS Sabre DAC** = delta-sigma DAC from ESS Technology
  - Examples: ES9038PRO, ES9028PRO, ES9018
  - What the manual and the author say about ESS DACs: "Modulators" above

- **Multi-element DAC** = a DAC with multiple converter elements

### Filter characteristics

- **Linear phase** (lp) = equal phase delay at all frequencies, symmetric ring
- **Minimum phase** (mp) = no pre-ring, all ringing after the transient
- **Intermediate phase** (ip) = between linear and minimum phase
- **Transients** = attacks, fast signal changes
- **Timbre** = tonal colour, harmonic structure
- **Space** = spatial character, stereo image, reverb tails

### Specific terms

- **EC** = Extended Compensation
- **Adaptive** (ASDM) = adapts to the signal in real time
- **Apod counter** = HQPlayer's count of the errors it detects in the source;
  the manual advises an apodizing filter at least when it goes above 10
  during a track
- **Constant-time filter** = a filter with a fixed tap count regardless of rate
- **Half-band filter** (hb) = cutoff at half the Nyquist frequency
- **2-stage processing** (*-2s) = two stages: ≥8x upsampling, then the final
  filter

---

## Base-rate families (44.1k vs 48k)

Every sample rate derives from one of two base rates:
- **The 44.1 kHz family** — the CD standard: 44.1, 88.2, 176.4, 352.8,
  705.6 kHz, and DSD64 … DSD2048
- **The 48 kHz family** — the DAT/video standard: 48, 96, 192, 384, 768 kHz,
  and 48k-family DSD (48k × 64 = 3.072 MHz, …)

A rate belongs to the 44.1k family when it is a multiple of 44100, to the 48k
family when it is a multiple of 48000.

Which filters convert between the families is a matter of their ratio class
("Ratio class and rate families" above): only `Any` filters do. A mixed
library — 44.1k CD rips beside 96k downloads — played at one fixed output
rate meets both cases; adaptive output rate, or an `Any` filter, covers both.

---

## Control API coverage

- **Output modes (3)**: [source], PCM, SDM (DSD)
- **Filters**: 67 in PCM mode and 77 in SDM mode on 6.2.3 (84 names), each
  with HQPlayer's own description
- **Modulators (36, SDM mode)** with HQPlayer's generation field, and **PCM
  dither and noise shapers (10)**
- **Sample rates**: what the current output offers (`GetRates`)

The assistant itself works from its prompt and the lists it reads live from
HQPlayer, not from this file.

---

## Sources

- **HQPlayer Desktop user manuals**: 5.16.0 (from which this file was first
  compiled) and 5.7.3 (the sections cited above) — Signalyst's documents, see
  `HQPLAYER_MANUALS.md`
- **Signalyst's release notes**: 5.13 – 6.1.0 (signalyst.com)
- **HQPlayer engine 6.2.3's runtime lists**: `GetFilters`, `GetShapers`, in
  PCM and SDM mode
- **Jussi Laako's posts** on Roon › HQ Player, as dated and linked above
- **SDK**: HQPlayer Control SDK (hqp-control, Signalyst)

---

**Last updated:** 2026-10-09
**HQPlayer version:** the lists are engine 6.2.3's (HQPlayer Desktop 6.1.0);
a running HQPlayer's own lists are read live
**Status:** a reference for people; it ranks nothing
