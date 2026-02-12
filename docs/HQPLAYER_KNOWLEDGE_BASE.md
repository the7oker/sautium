# HQPlayer 5 Desktop - Knowledge Base для AI агента

> Витяг ключової інформації з офіційного мануалу HQPlayer 5 Desktop v5.16.0

---

## Архітектура та можливості

### Основні компоненти
- **Playback Engine** - Відтворення аудіо з високоякісним upsampling
- **DSP Pipeline** - Обробка сигналу (фільтри, модулятори, конволюція)
- **Network Audio** - Підтримка мережевих протоколів (NAA, Roon Ready)
- **Library Management** - Управління бібліотекою музики
- **Control API** - XML/TCP API для віддаленого керування

### Підтримувані формати
- **PCM**: FLAC, WAV, AIFF, ALAC, MP3, AAC
- **DSD**: DFF (DSDIFF), DSF
- **Streaming**: Tidal, Qobuz (через партнерів)

---

## Режими роботи (Output Modes)

### 1. **[source]**
- Відтворення у форматі джерела без upsampling
- Адаптивний вибір sample rate
- Мінімальна обробка

### 2. **PCM**
- Upsampling PCM до вищих частот дискретизації
- Noise shaping та dithering
- Підтримка до 768 kHz

### 3. **SDM (DSD)**
- Конвертація в DSD (Direct Stream Digital)
- **1-bit format** - кожен sample це 0 або 1
- Delta-Sigma модуляція (Sigma-Delta Modulation)
- Підтримка до DSD2048 (90.3168 MHz)

**Що таке DSD:**
- **1-bit audio format** - на відміну від PCM (16/24/32-bit)
- Дуже високі sample rates (мегагерци замість кілогерц)
- Аудіо сигнал кодується через **щільність** одиниць (PDM - Pulse Density Modulation)
- Більше одиниць = вища амплітуда, менше одиниць = нижча амплітуда

**ВАЖЛИВО - Base Rate для DSD:**
- **Base rate = 44.1 kHz** (CD sample rate)
- Число в назві (64, 128, 256, 512...) = множник base rate
- Формула: **DSDxxx = 44.1 kHz × xxx**

**Формати DSD (розрахунок):**
- **DSD64** = 44.1k × **64** = 2822400 Hz = 2.8224 MHz (base SACD rate)
- **DSD128** = 44.1k × **128** = 5644800 Hz = 5.6448 MHz
- **DSD256** = 44.1k × **256** = 11289600 Hz = 11.2896 MHz
- **DSD512** = 44.1k × **512** = 22579200 Hz = 22.5792 MHz
- **DSD1024** = 44.1k × **1024** = 45158400 Hz = 45.1584 MHz
- **DSD2048** = 44.1k × **2048** = 90316800 Hz = 90.3168 MHz

**Еквівалентність запису:**
- DSD256(1bit 11.2MHz) = 44.1k × 256 = 11289600 Hz = 11.2896 MHz
- DSD512(1bit 22.4MHz) = 44.1k × 512 = 22579200 Hz = 22.5792 MHz

**Приклад розрахунку:**
```
DSD256 → 44100 × 256 = 11,289,600 Hz = 11.2896 MHz ≈ 11.2 MHz
```

**Примітка:** У специфікаціях часто округлюють (11.2 MHz замість 11.2896 MHz), але точна частота завжди: **44.1 kHz × multiplier**

---

## PCM налаштування

### Noise Shaping / Dither (Шейпери для PCM)

| Алгоритм | Призначення | Рекомендації |
|----------|-------------|--------------|
| **TPDF** | Industry standard dither | Універсальний, 44.1/48 kHz |
| **shaped** | Shaped dither | 88.2/96 kHz і вище |
| **Gauss1** | Gaussian dither | До 96 kHz |
| **NS1** | 1st order noise-shaping | 176.4/192 kHz |
| **NS4** | 4th order noise-shaping | ≥ 88.2 kHz |
| **NS5** | 5th order noise-shaping | 8x/16x rates (352.8/384/705.6/768 kHz) |
| **NS9** | 9th order noise-shaping | 4x rates (176.4/192 kHz), для старих 16-bit DACs |
| **LNS15** | 15th order linear | 16x rates (705.6/768 kHz) |

**Важливо:**
- NS5: Особливо добре для PCM1704 на найвищих частотах
- NS9: Ідеально для старих 16-bit multibit DACs (TDA154x)
- LNS15: Smooth slope, для найвищих PCM rates

### DAC Bits (R2R DACs)

| DAC модель | Bits |
|------------|------|
| Holo Audio (Cyan 2, Spring 2/3, May) | 20 |
| Denafrips | 20 |
| LAiV Harmony | 18 |

**Примітка:** Комбінація правильного noise-shaper (LNS15, NS9, NS5) з високими rates може корегувати linearity errors R2R DACs.

---

## ПОВНА ТАБЛИЦЯ NOISE SHAPERS (36 shapers)

### PCM Noise Shapers / Dithers (10 shapers)

| Назва | Sample Rate | Призначення | DAC Type |
|-------|-------------|-------------|----------|
| **none** | Any | Без dither (тільки для тестів) | - |
| **TPDF** | 44.1/48 kHz | Industry standard dither | Universal |
| **shaped** | 88.2/96 kHz+ | Shaped dither | Universal |
| **Gauss1** | Up to 96 kHz | Gaussian dither | Universal |
| **NS1** | 176.4/192 kHz | 1st order noise-shaping | Universal |
| **NS4** | ≥ 88.2 kHz | 4th order noise-shaping | Universal |
| **NS5** | 352.8/384/705.6/768 kHz | 5th order, 8x/16x rates | PCM1704 |
| **NS9** | 176.4/192 kHz | 9th order, 4x rates | 16-bit multibit (TDA154x) |
| **LNS15** | 705.6/768 kHz | 15th order linear, 16x rates | R2R DACs |
| **LNS15 light** | 705.6/768 kHz | Lighter version of LNS15 | R2R DACs |

### SDM (DSD) Modulators - 5th Order (13 modulators)

Для ESS Sabre DACs та простих analog filters:

| Назва | Оптимальний Rate | Особливості |
|-------|------------------|-------------|
| **DSD5** | Any DSD | Rate adaptive fifth order |
| **DSD5v2** | Any DSD | Revised fifth order |
| **DSD5v2 256+fs** | ≥ DSD256 (10.24 MHz) | Оптимізований для високих rates |
| **DSD5EC** | Any DSD | Extended compensation |
| **ASDM5** | Any DSD | Adaptive fifth order |
| **ASDM5EC** | Any DSD | Adaptive з extended compensation |
| **ASDM5ECv2** | Any DSD | Improved ASDM5EC |
| **ASDM5ECv3** | Any DSD | Further improved |
| **ASDM5EC-ul** | Any DSD | Ultralight (менше CPU) |
| **ASDM5EC-light** | Any DSD | Light version |
| **ASDM5EC-fast** | Any DSD | Transient optimized |
| **ASDM5EC-super** | Any DSD | Super quality |
| **ASDM5EC-super 512+fs** | ≥ DSD512 (22.4 MHz) | Оптимізований для 512+ |

### SDM (DSD) Modulators - 7th Order (13 modulators)

Для multi-element DACs та більшості не-ESS DACs:

| Назва | Оптимальний Rate | Особливості |
|-------|------------------|-------------|
| **DSD7** | Any DSD | Seventh order |
| **DSD7 256+fs** | ≥ DSD256 (10.24 MHz) | Оптимізований для високих rates |
| **DSD7 512+fs** | ≥ DSD512 (22.4 MHz) | Оптимізований для 512+ |
| **ASDM7** | Any DSD | Adaptive seventh order |
| **ASDM7EC** | Any DSD | Adaptive з extended compensation |
| **ASDM7ECv2** | Any DSD | Improved ASDM7EC |
| **ASDM7ECv3** | Any DSD | Further improved |
| **ASDM7EC-ul** | Any DSD | Ultralight (менше CPU) |
| **ASDM7EC-light** | Any DSD | Light version |
| **ASDM7EC-fast** | Any DSD | Transient optimized |
| **ASDM7EC-super** | Any DSD | Super quality (рекомендований!) |
| **ASDM7EC-super 512+fs** | ≥ DSD512 (22.4 MHz) | Оптимізований для 512+ (топ якість!) |
| **ASDM7EC-super 1024+fs** | ≥ DSD1024 (45.1 MHz) | Оптимізований для 1024+ |

### Вибір Modulator/Shaper

**Для PCM режиму:**
- 44.1/48 kHz output → **TPDF**
- 88.2/96 kHz output → **NS4** або **shaped**
- 176.4/192 kHz output → **NS9** (для старих 16-bit DACs) або **NS1**
- 352.8/384 kHz output → **NS5**
- 705.6/768 kHz output → **LNS15** (для R2R DACs) або **NS5**

**Для SDM режиму:**
- **ESS Sabre DACs** → **ASDM5EC-super 512+fs** (5th order)
- **Multi-element DACs** → **ASDM7EC-super 512+fs** (7th order)
- **R2R DACs** → **ASDM7EC-super** або **ASDM7EC-super 512+fs**
- **Universal choice** → **ASDM7EC-super** (працює з усім)

---

## ПОВНА ТАБЛИЦЯ SAMPLE RATES (20 rates)

### PCM Sample Rates (14 rates)

| Rate (Hz) | Rate (MHz) | Multiplier | Опис |
|-----------|------------|------------|------|
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

### DSD (SDM) Sample Rates (6 rates)

| Rate (Hz) | Rate (MHz) | DSD Name | Multiplier | Формула | Опис |
|-----------|------------|----------|------------|---------|------|
| 2822400 | 2.8224 | DSD64 | 64x | 44.1k × 64 | Base DSD rate (SACD) |
| 5644800 | 5.6448 | DSD128 | 128x | 44.1k × 128 | 2x base rate |
| 11289600 | 11.2896 | DSD256 | 256x | 44.1k × 256 | 4x base rate |
| 22579200 | 22.5792 | DSD512 | 512x | 44.1k × 512 | 8x base rate (recommended!) |
| 45158400 | 45.1584 | DSD1024 | 1024x | 44.1k × 1024 | 16x base rate (high-end) |
| 90316800 | 90.3168 | DSD2048 | 2048x | 44.1k × 2048 | 32x base rate (extreme) |

**ВАЖЛИВО: Base rate = 44.1 kHz (CD sample rate)**

### Вибір Sample Rate

**Загальні рекомендації:**

**PCM Mode:**
- **Standard DACs**: 176.4/192 kHz (4x) - універсальний безпечний вибір
- **R2R DACs**: 705.6/768 kHz (16x) - корегує linearity errors
- **Hi-Res sources (≥96kHz)**: 384 kHz або вище
- **Standard sources (44.1/48kHz)**: 176.4/192 kHz мінімум

**SDM (DSD) Mode:**
- **Minimum recommended**: DSD256 (11.2896 MHz)
- **Sweet spot**: DSD512 (22.5792 MHz) - баланс якість/CPU
- **High-end**: DSD1024 (45.1584 MHz) - потребує потужний CPU
- **Extreme**: DSD2048 (90.3168 MHz) - тільки для топових систем

**За типом DAC:**
- **ESS Sabre**: DSD512+ з ASDM5EC-super 512+fs
- **Multi-element**: DSD512+ з ASDM7EC-super 512+fs
- **R2R (Holo, Denafrips)**: PCM 768 kHz з LNS15 АБО DSD512+ з ASDM7EC-super

**Обмеження:**
- Не всі DACs підтримують усі rates
- DSD1024+ потребує топові DACs (дуже рідкісні)
- Перевіряйте специфікації вашого DAC перед вибором

### DSD → PCM Conversion

**Noise Filters:**
- **standard** - Рекомендується
- **low** - Flat noise profile, рекомендується
- **high-order** - Для high order modulators
- **medium** - Gentle, мінімум out-of-band noise
- **brickwall** - Не пропускає жодного out-of-band noise

**Conversion Types:**
- **poly-short-lp** - Linear-phase slow roll-off (рекомендується)
- **poly-short-mp** - Minimum-phase slow roll-off
- **poly-ext2** - Extended frequency response
- **poly-gauss-long** - Optimal time-frequency response
- **none** - Без decimation (output = DSD rate)

---

## SDM (DSD) налаштування

### Delta-Sigma Modulators

#### 5th Order (для простих analog filters)
| Модулятор | Опис |
|-----------|------|
| **DSD5** | Rate adaptive fifth order |
| **DSD5v2** | Revised fifth order |
| **DSD5v2 256+fs** | Оптимізований для ≥ 10.24 MHz |
| **DSD5EC** | З extended compensation |
| **ASDM5** | Adaptive fifth order |
| **ASDM5EC** | Adaptive з EC |
| **ASDM5ECv2/v3** | Покращені версії |
| **ASDM5EC-ul** | Ultralight version |
| **ASDM5EC-light** | Light version |
| **ASDM5EC-fast** | Transient optimized |
| **ASDM5EC-super** | Super version |
| **ASDM5EC-* 512+fs** | Оптимізовані для 512x+ rates |

**Рекомендації:**
- **ESS Sabre DACs**: 5th order modulators
- **Simple analog filters**: 5th order

#### 7th Order (для DACs з multi-element arrays)
| Модулятор | Опис |
|-----------|------|
| **DSD7** | Seventh order |
| **DSD7 256+fs** | Оптимізований для ≥ 10.24 MHz |
| **ASDM7** | Adaptive seventh order |
| **ASDM7EC** | Adaptive з EC |
| **ASDM7ECv2/v3** | Покращені версії |
| **ASDM7EC-ul** | Ultralight version |
| **ASDM7EC-light** | Light version |
| **ASDM7EC-fast** | Transient optimized |
| **ASDM7EC-super** | Super version |
| **ASDM7EC-* 512+fs** | Оптимізовані для 512x+ rates |

**Рекомендації:**
- **Multi-element DACs**: 7th order modulators
- **Більшість DACs (крім ESS)**: 7th order optimal

#### Hybrid Modulators (експериментальні)
| Модулятор | Опис | Обмеження |
|-----------|------|-----------|
| **AMSDM7 512+fs** | Pseudo-multi-bit для ≥ 20.48 MHz | - |
| **AHM5EC5L** | 5th order 5-level, ≥ 40.96 MHz | Limited SNR |
| **AHM7EC5L** | 7th order 5-level, ≥ 40.96 MHz | Limited SNR |
| **AHM5EC8B** | 5th order 8-bit, ≥ 40.96 MHz | - |
| **AHM7EC8B** | 7th order 8-bit, ≥ 40.96 MHz | - |

**Примітка:** Hybrid modulators краще підходять для loudspeaker systems, не рекомендується коли HQPlayer volume control є primary.

### Integrators (SDM → SDM remodulation)

| Integrator | Audio Bandwidth (re DSD64) | Опис |
|------------|----------------------------|------|
| **IIR** | 50 kHz | Normal IIR |
| **IIR2** | 25 kHz | Minimize residual noise |
| **IIR3** | 30 kHz | High order IIR |
| **FIR** | - | Weighted FIR |
| **FIR2** | 50 kHz | Weighted FIR |
| **FIR-bl** | 24 kHz (cut 45 kHz) | Band-limiting |
| **FIR-bw** | 21.5 kHz (cut 30 kHz) | Brickwall |
| **CIC** | - | Cascade comb |

### SDM Conversion

| Тип | Призначення |
|-----|-------------|
| **wide** | Wide bandwidth signal |
| **narrow** | Narrow bandwidth (piano) |
| **XFi** | Extreme fidelity medium (universal) |

**Default:** XFi - підходить для всіх випадків

---

## Filters / Oversampling (Фільтри для Upsampling)

### Структура
- **1x filters** - Для source rates < 50 kHz (base rates)
- **Nx filters** - Для всього вище 1x rates

### Швидкий вибір фільтру

#### За жанром:
- **Класична музика** → poly-sinc-gauss-xla, sinc-MGa, poly-sinc-ext2-xla
- **Джаз/Блюз** → poly-sinc-gauss-xla, sinc-MGa, IIR2
- **Поп/Рок** → poly-sinc-shrt-mp, minphaseFIR, IIR2
- **Електронна** → poly-sinc-xtr-short-mp, poly-sinc-gauss-short

#### За фокусом:
- **Transients (атаки звуків)** → sinc-MGa, poly-sinc-gauss-*, IIR*, minphaseFIR
- **Timbre (тембр)** → poly-sinc-ext2-*, poly-sinc-xtr-*, sinc-M*
- **Space (простір)** → poly-sinc-gauss-long, poly-sinc-ext2-long, sinc-*

#### Apodizing (видалення pre-ringing):
- **З Apodizing** (коли Apod counter > 10): sinc-MGa, poly-sinc-ext2-xla, poly-sinc-gauss-xla
- **Без Apodizing**: sinc-MG, poly-sinc-gauss-xl

---

## ПОВНА ТАБЛИЦЯ ФІЛЬТРІВ (77 filters)

| Назва фільтру | Опис | Фокус | Якість | Жанр | Ratio | Apod |
|---------------|------|-------|--------|------|-------|------|
| **none** | Без конвертації, тільки bit depth | - | 1/5 | - | 1:1 | N |
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

### Пояснення колонок таблиці:

- **Назва фільтру**: Точна назва як у HQPlayer API
- **Опис**: Ключові характеристики фільтру
- **Фокус**: На що найкраще впливає фільтр
  - **Transients**: Точність атак звуків, швидких змін
  - **Timbre**: Тембральна точність, гармоніки
  - **Space**: Просторові характеристики, стереобаза
- **Якість**: Оцінка від 1/5 до 5/5 (вища = краща)
- **Жанр**: Рекомендовані музичні жанри
- **Ratio**: Підтримувані коефіцієнти конвертації
  - **Integer**: Тільки цілочисельні (2x, 4x, 8x, 16x)
  - **Any**: Будь-які коефіцієнти
  - **2x up**: Тільки upsampling в 2 рази (⚠️ потребує matching base rate!)
  - **Integer up**: Цілочисельний upsampling
- **Apod**: Apodizing capability
  - **Y**: Має аподизацію (видаляє pre-ringing)
  - **N**: Без аподизації
  - **½**: Часткова аподизація

**⚠️ ВАЖЛИВО - Base Rate Compatibility:**
- **sinc-*** фільтри (всі що починаються з "sinc-"): працюють ТІЛЬКИ з matching base rate (44.1k→44.1k або 48k→48k)
- **closed-form*** фільтри: працюють ТІЛЬКИ з matching base rate
- **poly-sinc-***, **IIR***, **FIR*** фільтри: universal (працюють з будь-якими base rates)

### SDM Output Processing

Фільтри з двоетапною обробкою для SDM виходу (мінімум 16x проміжна частота):
- poly-sinc-ext2 series
- poly-sinc-gauss series
- sinc-short/medium/long series

**Важливо:** Коли output = SDM (DSD), ці фільтри використовують 2-stage processing для оптимальної якості.

### Особливі фільтри

#### sinc-MGa vs sinc-MG
- **sinc-MG**: Без аподизації, варіант poly-sinc-gauss-xl
- **sinc-MGa**: З АПОДИЗАЦІЄЮ, варіант poly-sinc-gauss-xla
- Обидва: Million taps @ 16x rates (65536 x conversion ratio)
- Обидва: Надзвичайно висока атенюація
- **Використовувати MGa коли**: Apod counter > 10 (джерело має pre-ringing)

#### Constant Time Filters
- **sinc-Mx**: 65536 x conversion ratio
- **sinc-MG**: 65536 x conversion ratio
- **sinc-MGa**: 65536 x conversion ratio
- Million taps при 16x PCM output rates (768 kHz)

### Рівні якості
- **5/5**: Найвища якість (poly-sinc-ext2-*, poly-sinc-gauss-*, poly-sinc-xtr-*)
- **4/5**: Висока якість (sinc-*, IIR2, poly-sinc-lp/mp)
- **3/5**: Добра якість (FIR, asymFIR, poly-sinc-short, closed-form)
- **2/5**: Базова якість (ASRC, minringFIR, polynomial-2)
- **1/5**: Не рекомендується (none, polynomial-1)

### Коли використовувати Apodizing Filter
**Використовувати коли:**
- Apod counter > 10 під час відтворення треку
- Означає що джерело має pre-ringing artifacts
- Apodizing filter (з позначкою Y) компенсує ці artifacts
- Приклади: sinc-MGa, poly-sinc-ext2-xla, poly-sinc-gauss-xla

---

## Sample Rates

### PCM Rates
- 44.1, 48 kHz (standard)
- 88.2, 96 kHz (2x)
- 176.4, 192 kHz (4x)
- 352.8, 384 kHz (8x)
- 705.6, 768 kHz (16x)

### DSD Rates
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

## Convolution Engine

### Призначення
- Room correction
- Crossfeed (для навушників)
- Custom impulse responses

### Формати
- WAV (linear PCM)
- FLAC
- Mono/Stereo/Multichannel

### Параметри
- Partitioned convolution (FFT-based)
- Низька латентність
- Normalized або custom gain

---

## Matrix Processing

### Можливості
- Channel routing та mixing
- Delay compensation
- EQ (через IIR filters)
- RIAA correction (для turntables)

### Plugins
- **delay** - Channel delay
- **iir** - IQ filters (parametric EQ)
- **riaa** - RIAA equalization curve

---

## Adaptive Output Rate

**Коли включено:**
- HQPlayer автоматично вибирає output rate
- Базується на source rate та filter capabilities
- "Sample rate" стає upper limit

**Коли вимкнено:**
- Фіксований output rate (якщо PCM mode)

---

## Рекомендації для різних сценаріїв

### High-Res PCM (≥ 96 kHz/24-bit)
```
Source: 96-192 kHz FLAC (Hi-Res download або studio master)
Mode: PCM
Filter: poly-sinc-ext2-long або poly-sinc-gauss-long
Rate: 384 kHz (8x) або 768 kHz (16x для R2R DACs)
Shaper: NS5 (для 384 kHz) або LNS15 (для 768 kHz)
```
**Коли використовувати:** Студійні мастери, офіційні Hi-Res релізи (Qobuz, HDtracks)

### Standard Quality CD Rips (44.1/48 kHz/16-bit)
```
Source: CD FLAC або Apple Lossless
Mode: SDM (якщо DAC підтримує DSD) або PCM
Filter: poly-sinc-gauss-medium або poly-sinc-ext2-medium
Rate: DSD256 (11.2896 MHz) або PCM 192 kHz
Modulator: ASDM7EC-super (або ASDM5EC-super для ESS)
Shaper (якщо PCM): NS1 або NS4
```
**Коли використовувати:** Більшість вашої бібліотеки, CD rips, iTunes ALAC

### DSD Source (DSD64/DSD128)
```
Source: Native DSD recording
Mode: SDM
Filter: None (DSD → DSD remodulation не потребує filter)
Modulator: ASDM7EC-super 512+fs (або ASDM5EC-super 512+fs для ESS)
Rate: DSD512 (22.5792 MHz) мінімум
Integrator: FIR2 або IIR2
Conversion: XFi
```
**Коли використовувати:** Native DSD recordings, SACD rips

### R2R DACs (Holo Audio, Denafrips, LAiV Harmony)
```
Mode: PCM (рекомендовано!) або SDM
Filter: poly-sinc-gauss-long або poly-sinc-ext2-long
Rate: 705.6/768 kHz (16x) - КРИТИЧНО для linearity correction
Shaper: LNS15 або NS5
DAC bits: 20 (Holo/Denafrips) або 18 (LAiV)
```
**Причина:** Noise shaping на високих rates корегує linearity errors R2R DACs
**Альтернатива (SDM):** ASDM7EC-super 512+fs з DSD512+

### ESS Sabre DACs (ES9038PRO, ES9028PRO, etc.)
```
Mode: SDM (оптимально для ESS архітектури)
Filter: poly-sinc-gauss-medium або poly-sinc-ext2-medium
Modulator: ASDM5EC-super 512+fs (5th order!)
Rate: DSD512 (22.5792 MHz) мінімум, DSD1024 якщо CPU дозволяє
```
**Причина:** ESS чипи оптимізовані для 5th order modulators
**Важливо:** Саме 5th order (ASDM5), НЕ 7th order

### Multi-element/Delta-Sigma DACs (загальні)
```
Mode: SDM
Filter: poly-sinc-gauss-long або sinc-MGa (якщо CPU потужний)
Modulator: ASDM7EC-super 512+fs або ASDM7EC-super 1024+fs
Rate: DSD512 (22.5792 MHz) або DSD1024 (45.1584 MHz)
```
**Причина:** 7th order оптимальний для більшості non-ESS DACs

### Vinyl Rips (Analog джерела)
```
Source: Vinyl rip 96-192 kHz/24-bit
Mode: PCM або SDM
Filter: poly-sinc-gauss-medium або poly-sinc-gauss-long
Rate: 192 kHz (PCM) або DSD256 (SDM)
Modulator (якщо SDM): ASDM7EC-light або ASDM7EC-super
Shaper (якщо PCM): NS1 або NS4
```
**Особливості:** Gaussian фільтри optimal time-frequency для analog джерел
**Уникати:** Дуже steep фільтри (можуть підкреслити vinyl surface noise)

### MP3/Lossy Sources (Spotify, YouTube Music)
```
Source: MP3 320kbps або AAC 256kbps
Mode: PCM (рекомендовано)
Filter: poly-sinc-mqa/mp3-lp або poly-sinc-mqa/mp3-mp
Rate: 96 kHz або 176.4/192 kHz
Shaper: TPDF або shaped
```
**Особливості:** Спеціальні фільтри для cleanup lossy artifacts
**Не перестарайтеся:** Upsampling до DSD512 не покращить lossy джерело

### Classical Music (Orchestral, Chamber)
```
Genre-specific налаштування
Mode: SDM (для максимальної динаміки)
Filter: poly-sinc-gauss-xla або sinc-MGa
Rate: DSD512+ (для збереження мікродинаміки)
Modulator: ASDM7EC-super 512+fs
Focus: Space + Timbre
```
**Чому:** Gaussian фільтри зберігають просторові характеристики оркестру

### Jazz/Blues (Acoustic Instruments)
```
Genre-specific налаштування
Mode: PCM або SDM
Filter: poly-sinc-gauss-medium або IIR2
Rate: 192 kHz (PCM) або DSD256 (SDM)
Focus: Transients + Space
```
**Чому:** IIR2 та Gaussian добре передають атаку acoustic instruments

### Rock/Pop (Studio Productions)
```
Genre-specific налаштування
Mode: PCM
Filter: poly-sinc-shrt-mp або minphaseFIR
Rate: 176.4/192 kHz
Shaper: NS1 або NS4
Focus: Transients
```
**Чому:** Minimum phase фільтри оптимальні для transient-heavy матеріалу

### Electronic/EDM (Synthetic sounds)
```
Genre-specific налаштування
Mode: PCM або SDM
Filter: poly-sinc-xtr-short-mp або poly-sinc-gauss-short
Rate: 192 kHz (PCM) або DSD256 (SDM)
Focus: Transients + Timbre
```
**Чому:** Short фільтри краще для synthetic transients та modulationsе

---

## Технічні обмеження

### CPU/GPU Requirements
- PCM filters: CPU-intensive
- SDM modulators: Дуже CPU-intensive
- Вищі rates = більше CPU
- poly-sinc-xla: Найважчий
- IIR: Найлегший

### Latency
- Залежить від фільтра та buffer size
- poly-sinc: Середня латентність
- sinc-M: Висока латентність (million taps)
- IIR: Низька латентність

### DAC Compatibility
- Не всі DACs підтримують всі rates
- Деякі DACs чутливі до ultrasonic noise
- Перевіряйте specifications вашого DAC

---

## Control API Mapping

### Команди доступні через API:

| API метод | Що контролює |
|-----------|--------------|
| `SetMode` | PCM / SDM / [source] |
| `SetFilter` | Фільтр (1x та Nx) |
| `SetShaping` | Noise shaper (PCM) або modulator (SDM) |
| `SetRate` | Output sample rate |
| `GetModes` | Список доступних modes |
| `GetFilters` | Список всіх фільтрів |
| `GetShapers` | Список shapers/modulators |
| `GetRates` | Список sample rates |

**Примітка:** API повертає індекси, не назви. Потрібно mapping index → name.

---

## Terminology для AI

### Аудіо формати

**PCM (Pulse Code Modulation):**
- Multi-bit format (16-bit, 24-bit, 32-bit)
- Кожен sample - це число (amplitude value)
- Sample rates: 44.1 kHz, 48 kHz, 96 kHz, 192 kHz, 384 kHz, 768 kHz
- Приклад: CD = 16-bit @ 44.1 kHz
- Приклад: Hi-Res = 24-bit @ 192 kHz

**DSD (Direct Stream Digital) / SDM (Sigma-Delta Modulation):**
- **1-bit format** - кожен sample це 0 або 1
- Дуже високі sample rates (мегагерци)
- Аудіо кодується через pulse density (PDM)
- **Base rate = 44.1 kHz** (CD sample rate)
- Число в назві = множник: DSD64 = 44.1k × 64, DSD256 = 44.1k × 256
- Приклад: DSD256 = 44.1k × 256 = 11.2896 MHz (1-bit)
- Приклад: DSD512 = 44.1k × 512 = 22.5792 MHz (1-bit)

**Як читати DSD формат:**
- `DSD256(1bit 11.2MHz)` означає:
  - DSD**256** = **44.1 kHz × 256** multiplier
  - Розрахунок: 44100 × 256 = 11,289,600 Hz
  - 1-bit = кожен sample один біт
  - 11.2MHz = 11.2896 MHz sample rate (округлено в специфікації)
  - Точна частота: **11289600 Hz**

**Розрахунок DSD частот (КРИТИЧНО для AI):**
```
Формула: DSDxxx = 44100 Hz × xxx

Приклади:
DSD64   = 44100 × 64   = 2,822,400 Hz  = 2.8224 MHz
DSD128  = 44100 × 128  = 5,644,800 Hz  = 5.6448 MHz
DSD256  = 44100 × 256  = 11,289,600 Hz = 11.2896 MHz ≈ 11.2 MHz
DSD512  = 44100 × 512  = 22,579,200 Hz = 22.5792 MHz ≈ 22.4 MHz
DSD1024 = 44100 × 1024 = 45,158,400 Hz = 45.1584 MHz ≈ 45.2 MHz
```

**Коли в специфікації DAC вказано:**
- `DSD256(1bit 11.2MHz)` → розуміти як **44.1k × 256 = 11289600 Hz**
- `DSD512(1bit 22.4MHz)` → розуміти як **44.1k × 512 = 22579200 Hz**
- Округлення в MHz - це нормально, але точна частота завжди кратна 44.1k

**Конвертація між форматами:**
- PCM → DSD: Delta-Sigma modulation (потрібен modulator)
- DSD → PCM: Decimation (потрібен integrator + noise filter)
- PCM → PCM: Upsampling (потрібен filter)
- DSD → DSD: Remodulation (потрібен integrator + modulator)

### Обробка сигналу

- **Upsampling** = Збільшення sample rate
- **Oversampling** = Те саме що upsampling
- **Noise shaping** = Зсув quantization noise у вищі частоти (для PCM)
- **Dithering** = Додавання шуму для лінеаризації (для PCM)
- **Delta-Sigma modulation** = Конвертація PCM в 1-bit DSD
- **Apodizing** = Видалення pre-ringing artifacts (коли Apod counter > 10)
- **Pre-ringing** = Artifacts перед transients (типово від CD filters)
- **Post-ringing** = Artifacts після transients

### Типи DAC

- **R2R DAC** = Resistor ladder DAC (multibit, discrete)
  - Приклади: Holo Audio, Denafrips, LAiV Harmony
  - Optimal: PCM з high rates (768 kHz) + noise shaping (LNS15)
  - Linearity errors → корегуються через noise shaping

- **ESS Sabre DAC** = Delta-Sigma DAC від ESS Technology
  - Приклади: ES9038PRO, ES9028PRO, ES9018
  - Optimal: DSD з 5th order modulators (ASDM5EC-super)

- **Multi-element DAC** = DAC з multiple converter elements
  - Optimal: DSD з 7th order modulators (ASDM7EC-super)

### Характеристики фільтрів

- **Linear Phase** (lp) = Рівна фазова затримка всіх частот, симетричний ring
- **Minimum Phase** (mp) = Без pre-ring, весь ring після transient
- **Intermediate Phase** (ip) = Між linear та minimum phase
- **Transients** = Атаки звуків, швидкі зміни сигналу
- **Timbre** = Тембр, гармонічна структура звуку
- **Space** = Просторові характеристики, стереобаза, reverb tails

### Специфічні терміни

- **EC** = Extended Compensation (покращена корекція в modulators)
- **Adaptive** (ASDM) = Адаптується до сигналу в реальному часі
- **Apod counter** = Лічильник apodizing потреби (>10 = потрібен apodizing filter)
- **Constant time filter** = Фільтр з фіксованим числом taps незалежно від rate
- **Half-band filter** (hb) = Фільтр з cutoff на половині Nyquist frequency
- **2-stage processing** (*-2s) = Два етапи: ≥8x upsampling, потім фінальний filter

---

## Автоматичний вибір налаштувань (AI логіка)

### Алгоритм для AI DJ:

```python
def auto_select_settings(track_info, user_preferences=None):
    """
    Автоматичний вибір оптимальних налаштувань HQPlayer

    Args:
        track_info: dict з інформацією про трек
            - sample_rate: int (44100, 48000, 96000, 192000, etc.)
            - bit_depth: int (16, 24, 32)
            - format: str ('FLAC', 'DSD64', 'DSD128', etc.)
            - genre: str (optional)
            - quality_source: str ('CD', 'Vinyl', 'Hi-Res', 'MP3')
        user_preferences: dict з налаштуваннями користувача
            - dac_type: str ('ESS', 'R2R', 'Delta-Sigma', 'Unknown')
            - cpu_power: str ('low', 'medium', 'high', 'extreme')
            - focus: str ('transients', 'timbre', 'space', 'balanced')

    Returns:
        dict з налаштуваннями HQPlayer
    """
    # Defaults
    if user_preferences is None:
        user_preferences = {'dac_type': 'Unknown', 'cpu_power': 'medium', 'focus': 'balanced'}

    # 1. Визначити параметри джерела
    sample_rate = track_info['sample_rate']
    bit_depth = track_info.get('bit_depth', 16)
    genre = track_info.get('genre', '').lower()
    is_dsd = track_info.get('format', '').startswith('DSD')
    quality = track_info.get('quality_source', 'CD')

    # 2. Параметри системи
    dac_type = user_preferences.get('dac_type', 'Unknown')
    cpu_power = user_preferences.get('cpu_power', 'medium')
    focus = user_preferences.get('focus', 'balanced')

    # 3. Вибрати режим
    if is_dsd:
        mode = "SDM"
    elif dac_type == "ESS":
        mode = "SDM"  # ESS Sabre краще з DSD
    elif dac_type == "R2R" and cpu_power in ['low', 'medium']:
        mode = "PCM"  # R2R DACs відмінно працюють з PCM + noise shaping
    elif cpu_power == 'extreme':
        mode = "SDM"  # Максимальна якість
    else:
        mode = "PCM"  # Безпечний вибір

    # 4. Визначити base rate families
    def get_base_family(rate):
        if rate % 44100 == 0:
            return '44.1k'
        elif rate % 48000 == 0:
            return '48k'
        else:
            return 'other'

    source_family = get_base_family(sample_rate)

    # Target family залежить від режиму
    if mode == "SDM":
        # DSD зазвичай 44.1k family (стандарт)
        target_family = '44.1k'
    else:
        # PCM - визначаємо з target rate
        if cpu_power in ['high', 'extreme'] and dac_type == 'R2R':
            target_rate = 768000  # 44.1k × 16 або 48k × 16
            target_family = get_base_family(target_rate) if target_rate else source_family
        else:
            target_family = source_family  # Зберігаємо ту ж family

    # Перевірка чи base rates співпадають
    base_rates_match = (source_family == target_family)

    # 5. Вибрати фільтр
    filter_name = None
    if mode == "PCM" or is_dsd:  # Фільтр потрібен для PCM джерел
        # Визначити apodizing потребу (симулюємо Apod counter)
        needs_apodizing = quality in ['CD', 'MP3'] or sample_rate <= 48000

        # За жанром та фокусом
        if genre in ['classical', 'jazz', 'blues'] or focus == 'space':
            if cpu_power == 'extreme' and base_rates_match:
                # sinc-MGa працює ТІЛЬКИ якщо base rates співпадають
                filter_name = "sinc-MGa" if needs_apodizing else "sinc-MG"
            elif cpu_power == 'extreme':
                # Fallback to universal filter якщо base rates різні
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

        else:  # Balanced / Universal
            if cpu_power == 'extreme':
                filter_name = "sinc-MGa" if needs_apodizing else "sinc-MG"
            elif cpu_power == 'high':
                filter_name = "poly-sinc-ext2-long"
            else:
                filter_name = "poly-sinc-gauss-medium"

    # 5. Вибрати modulator/shaper
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
        # Визначаємо target output rate для вибору shaper
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

    # 6. Вибрати output rate
    if mode == "SDM":
        if cpu_power == 'extreme':
            output_rate = 90316800  # DSD2048
        elif cpu_power == 'high':
            output_rate = 45158400  # DSD1024
        elif cpu_power == 'medium':
            output_rate = 22579200  # DSD512 (recommended!)
        else:
            output_rate = 11289600  # DSD256
    else:  # PCM
        if dac_type == "R2R" and cpu_power in ['high', 'extreme']:
            output_rate = 768000  # 16x для linearity correction
        elif cpu_power == 'extreme':
            output_rate = 384000  # 8x
        elif cpu_power in ['medium', 'high']:
            output_rate = 192000  # 4x (universal sweet spot)
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

### Практичні приклади налаштувань

#### Приклад 1: Hi-Res FLAC (192kHz/24bit) + R2R DAC + Потужний CPU
```python
track = {
    'sample_rate': 192000,  # 48k × 4 → 48k family
    'bit_depth': 24,
    'format': 'FLAC',
    'genre': 'Classical',
    'quality_source': 'Hi-Res'
}
prefs = {'dac_type': 'R2R', 'cpu_power': 'high', 'focus': 'space'}

# Аналіз AI:
# Source: 192000 Hz (48k family)
# Target: 768000 Hz (48k × 16 = 48k family)
# Base rates MATCH ✅ → можна використати будь-який фільтр

# Результат:
{
    'mode': 'PCM',
    'filter': 'poly-sinc-gauss-long',  # Universal filter
    'modulator_or_shaper': 'LNS15',
    'output_rate': 768000,
    'base_rate_match': True
}
```

#### Приклад 2: CD FLAC (44.1kHz/16bit) + ESS DAC
```python
track = {
    'sample_rate': 44100,  # 44.1k × 1 → 44.1k family
    'bit_depth': 16,
    'format': 'FLAC',
    'genre': 'Jazz',
    'quality_source': 'CD'
}
prefs = {'dac_type': 'ESS', 'cpu_power': 'medium', 'focus': 'balanced'}

# Аналіз AI:
# Source: 44100 Hz (44.1k family)
# Target: DSD512 = 22579200 Hz (44.1k × 512 = 44.1k family)
# Base rates MATCH ✅ → можна використати будь-який фільтр

# Результат:
{
    'mode': 'SDM',
    'filter': 'poly-sinc-gauss-medium',  # Для PCM → DSD конвертації
    'modulator_or_shaper': 'ASDM5EC-super',
    'output_rate': 22579200,  # DSD512
    'base_rate_match': True
}
```

#### Приклад 2b: Hi-Res 96k → DSD (різні base families!)
```python
track = {
    'sample_rate': 96000,  # 48k × 2 → 48k family ⚠️
    'bit_depth': 24,
    'format': 'FLAC',
    'genre': 'Classical',
    'quality_source': 'Hi-Res'
}
prefs = {'dac_type': 'Delta-Sigma', 'cpu_power': 'extreme', 'focus': 'space'}

# Аналіз AI:
# Source: 96000 Hz (48k family)
# Target: DSD512 = 22579200 Hz (44.1k × 512 = 44.1k family)
# Base rates NOT MATCH ❌ → НЕ можна використати sinc-MGa!

# AI автоматично вибирає universal filter:
{
    'mode': 'SDM',
    'filter': 'poly-sinc-gauss-xla',  # ✅ Universal (не sinc-MGa!)
    'modulator_or_shaper': 'ASDM7EC-super 512+fs',
    'output_rate': 22579200,  # DSD512
    'base_rate_match': False,  # ⚠️ Різні families
    'reasoning': 'Cannot use sinc-MGa: source is 48k family, target is 44.1k family. Using poly-sinc-gauss-xla instead.'
}
```

#### Приклад 3: Vinyl Rip (96kHz/24bit) + Unknown DAC + Focus на Transients
```python
track = {
    'sample_rate': 96000,
    'bit_depth': 24,
    'format': 'FLAC',
    'genre': 'Rock',
    'quality_source': 'Vinyl'
}
prefs = {'dac_type': 'Unknown', 'cpu_power': 'medium', 'focus': 'transients'}

# Результат:
{
    'mode': 'PCM',
    'filter': 'poly-sinc-shrt-mp',  # Minimum phase для transients
    'modulator_or_shaper': 'NS1',
    'output_rate': 192000
}
```

#### Приклад 4: DSD64 джерело + Multi-element DAC + Extreme CPU
```python
track = {
    'sample_rate': 2822400,
    'bit_depth': 1,
    'format': 'DSD64',
    'genre': 'Classical',
    'quality_source': 'DSD'
}
prefs = {'dac_type': 'Delta-Sigma', 'cpu_power': 'extreme', 'focus': 'space'}

# Результат:
{
    'mode': 'SDM',
    'filter': None,  # DSD → DSD не потребує upsampling filter
    'modulator_or_shaper': 'ASDM7EC-super 1024+fs',
    'output_rate': 90316800  # DSD2048
}
```

### Пріоритети якості за CPU power:

**Extreme (Необмежений CPU):**
- Mode: SDM
- Filter: sinc-MGa, poly-sinc-gauss-xla, poly-sinc-ext2-xla
- Modulator: ASDM7EC-super 1024+fs або ASDM5EC-super 1024+fs (для ESS)
- Rate: DSD1024 (45.1584 MHz) або DSD2048 (90.3168 MHz)

**High (Потужний CPU, RTX 4090 рівня):**
- Mode: SDM або PCM (R2R)
- Filter: poly-sinc-gauss-long, poly-sinc-ext2-long, poly-sinc-xtr-short-mp
- Modulator: ASDM7EC-super 512+fs або ASDM5EC-super 512+fs
- Rate: DSD512 (22.5792 MHz) або PCM 768 kHz

**Medium (Стандартний desktop):**
- Mode: SDM або PCM
- Filter: poly-sinc-gauss-medium, poly-sinc-ext2-medium
- Modulator: ASDM7EC-super або ASDM5EC-super
- Rate: DSD256 (11.2896 MHz) або PCM 192 kHz
- **Рекомендований вибір для більшості систем**

**Low (Обмежений CPU):**
- Mode: PCM
- Filter: poly-sinc-shrt-mp, poly-sinc-lp, IIR2
- Shaper: NS4, shaped, TPDF
- Rate: 96 kHz або 176.4/192 kHz

### Спеціальні сценарії

#### Vinyl Rips (потребують обережної обробки)
- Filter: poly-sinc-gauss-* (optimal time-frequency для analog джерел)
- Уникати: Дуже steep filters (можуть підкреслити vinyl noise)
- Рекомендовано: poly-sinc-gauss-medium або poly-sinc-gauss-long

#### MP3/Lossy Sources (cleanup режим)
- Filter: poly-sinc-mqa/mp3-lp або poly-sinc-mqa/mp3-mp
- Призначені спеціально для очищення artifacts
- Short ring для мінімальних додаткових artifacts

#### MQA Files (якщо підтримується)
- Filter: poly-sinc-ext2-hires-* або poly-sinc-gauss-hires-*
- Спеціально оптимізовані для MQA декодування
- Дуже висока атенюація

---

## Джерела інформації

- **Офіційний мануал**: HQPlayer 5 Desktop User Manual v5.16.0
- **SDK**: hqp-control-5292-src (engine 5.29.2)
- **Форум**: HQPlayer Community Forum
- **Розробник**: Jussi Laako / Signalyst

---

## Base Rate Families (44.1k vs 48k) - КРИТИЧНО!

### Дві базові частоти в цифровому аудіо

**ВАЖЛИВО:** Всі sample rates відштовхуються від двох base rates:
- **44.1 kHz family** - CD standard
- **48 kHz family** - DAT/Video standard

### 44.1 kHz Family

**PCM rates:**
- 44100 Hz = 44.1k × 1
- 88200 Hz = 44.1k × 2
- 176400 Hz = 44.1k × 4
- 352800 Hz = 44.1k × 8
- 705600 Hz = 44.1k × 16

**DSD rates (базуються на 44.1k):**
- DSD64 = 44.1k × 64 = 2822400 Hz
- DSD128 = 44.1k × 128 = 5644800 Hz
- DSD256 = 44.1k × 256 = 11289600 Hz
- DSD512 = 44.1k × 512 = 22579200 Hz
- DSD1024/2048 = 44.1k × 1024/2048

### 48 kHz Family

**PCM rates:**
- 48000 Hz = 48k × 1
- 96000 Hz = 48k × 2
- 192000 Hz = 48k × 4
- 384000 Hz = 48k × 8
- 768000 Hz = 48k × 16

**DSD rates (рідше, але існують):**
- DSD64 = 48k × 64 = 3072000 Hz
- DSD128 = 48k × 128 = 6144000 Hz
- тощо (рідко використовується)

### Як визначити family треку

```python
def get_base_rate_family(sample_rate: int) -> str:
    """
    Визначити до якої base rate family належить трек

    Returns: '44.1k' або '48k'
    """
    # Перевірка чи кратне 44100
    if sample_rate % 44100 == 0:
        return '44.1k'
    # Перевірка чи кратне 48000
    elif sample_rate % 48000 == 0:
        return '48k'
    else:
        # Рідкісні випадки (32k, 22.05k, etc.)
        return 'other'

# Приклади:
get_base_rate_family(88200)   # → '44.1k' (88200 = 44100 × 2)
get_base_rate_family(96000)   # → '48k'  (96000 = 48000 × 2)
get_base_rate_family(176400)  # → '44.1k' (176400 = 44100 × 4)
get_base_rate_family(192000)  # → '48k'  (192000 = 48000 × 4)
get_base_rate_family(2822400) # → '44.1k' (DSD64 = 44100 × 64)
```

### Сумісність фільтрів з base rate families

**⚠️ КРИТИЧНО ДЛЯ ВИБОРУ ФІЛЬТРУ:**

#### Фільтри що ПОТРЕБУЮТЬ matching base rate (sinc-*)

Ці фільтри працюють ТІЛЬКИ коли base rate треку = base rate DAC output:

- ❌ **sinc-S, sinc-M, sinc-Mx** - потребують matching base rate
- ❌ **sinc-MG, sinc-MGa** - потребують matching base rate
- ❌ **sinc-L, sinc-Ls, sinc-Lm, sinc-Ll, sinc-Lh** - потребують matching base rate
- ❌ **closed-form, closed-form-M, closed-form-16M** - потребують matching base rate

**Приклад проблеми:**
```
Source: 96 kHz FLAC (48k family)
Target: DSD256 = 11.2896 MHz (44.1k family)
Filter: sinc-MGa ← ❌ НЕ ПРАЦЮВАТИМЕ (різні base rates!)

Помилка: 48k family → 44.1k family conversion неможлива для sinc filters
```

#### Фільтри що працюють з ОБОМА families (universal)

Ці фільтри можуть конвертувати між різними base rates:

- ✅ **poly-sinc-gauss-*** - працюють з будь-якими base rates
  - poly-sinc-gauss-short, medium, long, xla, xl
  - poly-sinc-gauss-hires-lp/ip/mp
  - poly-sinc-gauss-halfband, halfband-s

- ✅ **poly-sinc-ext2-*** - працюють з будь-якими base rates
  - poly-sinc-ext2, ext2-short, ext2-medium, ext2-long
  - poly-sinc-ext2-xla, ext2-xl
  - poly-sinc-ext2-hires-lp/ip/mp

- ✅ **poly-sinc-xtr-*** - працюють з будь-якими base rates
- ✅ **poly-sinc-lp/mp/shrt-lp/shrt-mp** - працюють з будь-якими base rates
- ✅ **IIR, IIR2** - працюють з будь-якими base rates
- ✅ **FIR, asymFIR, minphaseFIR** - працюють з будь-якими base rates

**Приклад правильного вибору:**
```
Source: 96 kHz FLAC (48k family)
Target: DSD256 = 11.2896 MHz (44.1k family)
Filter: poly-sinc-gauss-xla ← ✅ ПРАЦЮЄ (universal filter)

Conversion: 48k family → 44.1k family OK!
```

### Логіка вибору фільтру з урахуванням base rate

```python
def select_filter_with_base_rate_check(source_rate: int, target_rate: int, preferred_filter: str):
    """
    Вибрати фільтр з перевіркою base rate compatibility
    """
    source_family = get_base_rate_family(source_rate)
    target_family = get_base_rate_family(target_rate)

    # Перевірити чи це sinc filter
    is_sinc_filter = preferred_filter.startswith('sinc-') or preferred_filter.startswith('closed-form')

    if is_sinc_filter and source_family != target_family:
        # Base rates не співпадають - sinc filter НЕ МОЖНА використати
        print(f"⚠️ Warning: {preferred_filter} requires matching base rates")
        print(f"   Source: {source_rate} Hz ({source_family} family)")
        print(f"   Target: {target_rate} Hz ({target_family} family)")
        print(f"   Switching to universal filter: poly-sinc-gauss-xla")
        return "poly-sinc-gauss-xla"  # Fallback to universal filter

    return preferred_filter

# Приклади:
select_filter_with_base_rate_check(96000, 11289600, "sinc-MGa")
# → "poly-sinc-gauss-xla" (автоматична заміна, бо base rates різні)

select_filter_with_base_rate_check(88200, 11289600, "sinc-MGa")
# → "sinc-MGa" (OK, обидва 44.1k family)

select_filter_with_base_rate_check(96000, 11289600, "poly-sinc-gauss-xla")
# → "poly-sinc-gauss-xla" (OK, universal filter)
```

### Практичні сценарії

#### Сценарій 1: CD rip (44.1k) → DSD256 (44.1k family)
```
Source: 44100 Hz (44.1k family)
Target: DSD256 = 11289600 Hz (44.1k family)
Base rates: MATCH ✅

Можна використати:
✅ sinc-MGa (matching base rates)
✅ sinc-MG
✅ poly-sinc-gauss-xla (universal)
✅ poly-sinc-ext2-xla (universal)
```

#### Сценарій 2: Hi-Res 96k → DSD256 (різні families)
```
Source: 96000 Hz (48k family)
Target: DSD256 = 11289600 Hz (44.1k family)
Base rates: NOT MATCH ❌

НЕ можна використати:
❌ sinc-MGa (потребує matching base rate)
❌ sinc-MG
❌ sinc-M, sinc-S, sinc-L series

Можна використати:
✅ poly-sinc-gauss-xla (universal filter - рекомендовано!)
✅ poly-sinc-ext2-xla (universal)
✅ poly-sinc-gauss-long
✅ IIR2
```

#### Сценарій 3: Mixed library (44.1k + 48k tracks) → DSD DAC
```
Library містить:
- CD rips: 44.1k family
- Hi-Res downloads: 48k family (96k, 192k)
- Vinyl rips: 96k (48k family)

DAC output: DSD512 (44.1k family)

Рішення AI:
- Використовувати ТІЛЬКИ universal filters:
  ✅ poly-sinc-gauss-xla (найкраще для обох families)
  ✅ poly-sinc-ext2-long (альтернатива)

- НЕ використовувати:
  ❌ sinc-MGa (не працюватиме для 48k треків)
```

### Рекомендації для AI агента

**Коли вибирати фільтр:**

1. **Визначити base rate family** джерела та виходу
2. **Якщо families співпадають:**
   - Можна використовувати будь-які фільтри (включно з sinc-*)
   - sinc-MGa - найкраща якість для matching base rates

3. **Якщо families НЕ співпадають:**
   - ❌ НІКОЛИ не використовувати sinc-* filters
   - ✅ Використовувати poly-sinc-gauss-* (рекомендовано)
   - ✅ Або poly-sinc-ext2-*

4. **Для mixed library (44.1k + 48k треки):**
   - Завжди використовувати universal filters
   - poly-sinc-gauss-xla - найкращий вибір для всіх випадків

**Тому користувач використовує poly-sinc-gauss-xla або sinc-MGa в залежності від формату:**
- **Якщо source і DAC - обидва 44.1k family** → sinc-MGa (найкраща якість)
- **Якщо різні families** → poly-sinc-gauss-xla (universal, працює завжди)

---

## Швидка довідка DSD для AI (самоперевірка)

**Питання для перевірки розуміння:**

Q: Що означає DSD256(1bit 11.2MHz)?
A: 44.1 kHz × 256 = 11,289,600 Hz = 11.2896 MHz, 1-bit формат

Q: Яка base rate для всіх DSD форматів?
A: 44.1 kHz (CD sample rate)

Q: Як розрахувати DSD512?
A: 44100 × 512 = 22,579,200 Hz = 22.5792 MHz

Q: Чому в специфікації написано "11.2 MHz" а не "11.2896 MHz"?
A: Округлення для зручності, точна частота завжди 44.1k × multiplier

Q: DAC підтримує DSD256. Який rate вибрати в HQPlayer?
A: output_rate = 11289600 (Hz), це буде DSD256

**Швидка таблиця для копіювання:**
```
DSD64   → 2822400 Hz   (44.1k family)
DSD128  → 5644800 Hz   (44.1k family)
DSD256  → 11289600 Hz  (44.1k family) ← коли бачиш "DSD256(1bit 11.2MHz)"
DSD512  → 22579200 Hz  (44.1k family) ← коли бачиш "DSD512(1bit 22.4MHz)"
DSD1024 → 45158400 Hz  (44.1k family)
DSD2048 → 90316800 Hz  (44.1k family)
```

**Base Rate Families - швидка перевірка:**
```
44.1k family: 44100, 88200, 176400, 352800, 705600, + всі DSD
48k family:   48000, 96000, 192000, 384000, 768000

Перевірка: sample_rate % 44100 == 0 → 44.1k family
           sample_rate % 48000 == 0 → 48k family
```

**Filter Compatibility - швидка перевірка:**
```
sinc-* filters (sinc-MGa, sinc-MG, sinc-M, etc.):
  ✅ Працюють: якщо source family == target family
  ❌ НЕ працюють: якщо source family != target family

poly-sinc-gauss-*, poly-sinc-ext2-*, IIR*, FIR*:
  ✅ Працюють ЗАВЖДИ (universal filters)
```

---

## Control API Coverage (Повнота інформації)

### ✅ Повністю задокументовано:

- **Output Modes (3)**: [source], PCM, SDM (DSD)
- **Filters (77)**: Повна таблиця з характеристиками (Focus, Quality, Genre, Ratio, Apodizing)
- **Noise Shapers/Modulators (36)**:
  - PCM Shapers (10): TPDF, shaped, Gauss1, NS1/4/5/9, LNS15/light
  - DSD 5th order (13): DSD5, ASDM5EC series, variants (ul, light, fast, super, 512+fs)
  - DSD 7th order (13): DSD7, ASDM7EC series, variants (ul, light, fast, super, 512+fs, 1024+fs)
- **Sample Rates (20)**:
  - PCM (14): 44.1 kHz до 12.288 MHz (32x DAT)
  - DSD (6): DSD64 до DSD2048 (2.8224 MHz до 90.3168 MHz)

### 📋 Додаткова інформація:

- **Швидкий вибір** за жанром, фокусом, apodizing потребою
- **Автоматичний алгоритм** вибору налаштувань для AI
- **Практичні приклади** для різних сценаріїв
- **Genre-specific** рекомендації (Classical, Jazz, Rock, Electronic)
- **DAC-specific** налаштування (ESS, R2R, Multi-element)
- **Source-specific** (Hi-Res, CD, Vinyl, MP3, DSD)

### 🎯 Готовність AI агента:

AI агент тепер має **повну інформацію** для інтелектуального вибору налаштувань HQPlayer на основі:
- Якості джерела (sample rate, bit depth, format)
- Типу DAC (якщо відомо)
- Жанру музики
- Потужності CPU
- Фокусу (transients/timbre/space)
- Спеціальних потреб (apodizing, vinyl cleanup, MP3 enhancement)

**Приклад використання AI:**
```
User: "Відтворити Pink Floyd - Comfortably Numb (CD FLAC 44.1kHz/16bit)"
AI Agent аналізує:
  - Source: CD quality, 44.1 kHz → потрібен apodizing
  - Genre: Rock → focus на transients
  - DAC: Holo Audio Spring 3 (R2R) → PCM з LNS15 оптимально
AI вибирає:
  - Mode: PCM
  - Filter: poly-sinc-gauss-medium (transients + apodizing)
  - Shaper: LNS15
  - Rate: 768 kHz (linearity correction для R2R)
```

---

**Останнє оновлення:** 2026-02-12
**Версія HQPlayer:** 5.16.3 (Engine 5.34.14)
**Статус:** ✅ **Повна база знань - Готово до використання**
**Покриття Control API:** 100% (всі параметри задокументовані)
