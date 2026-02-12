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
- Delta-Sigma модуляція
- Підтримка до DSD2048 (90.3168 MHz)

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

### Типи фільтрів

#### IIR Filters
- **IIR** - Infinite Impulse Response
- **IIR2** - Variant
- Легкі, швидкі, але можуть мати phase distortion

#### FIR Filters
- **FIR** - Finite Impulse Response (linear phase)
- **asymFIR** - Asymmetric FIR
- **minphaseFIR** - Minimum phase FIR
- Linear phase, no pre-ringing (minimum phase)

#### Poly-sinc Filters (Рекомендовані!)
Polynomial interpolated sinc filters - найкращий баланс якості та performance.

**Варіанти:**
- **poly-sinc-lp** - Linear phase
- **poly-sinc-mp** - Minimum phase
- **poly-sinc-short-lp** - Slow roll-off, linear phase
- **poly-sinc-short-mp** - Slow roll-off, minimum phase
- **poly-sinc-gauss** - Gaussian, optimal time-frequency
- **poly-sinc-ext** - Extended frequency response
- **poly-sinc-ext2** - Extended v2 (sharp roll-off, high attenuation)
- **poly-sinc-xla** - Extra-long aperture

**Рекомендації автора:** Варіанти poly-sinc - найкращі!

#### Sinc Filters
- **sinc-L** - Long (million taps)
- **sinc-M** - Medium (65536 taps)
- **sinc-S** - Short
- Sharp roll-off, high attenuation

#### Closed-form Filters
Математично оптимальні фільтри для певних критеріїв.

### Apodizing Filter
**Використовувати коли:**
- Apod counter > 10 під час відтворення треку
- Означає що джерело має pre-ringing artifacts
- Apodizing filter компенсує ці artifacts

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

### High-Res PCM (≥ 96 kHz)
```
Mode: PCM
Filter: poly-sinc-ext2 (high quality)
Rate: DSD256 (11.2896 MHz)
Dither: LNS15 (if upsampling to 768 kHz PCM)
```

### Standard Quality (44.1/48 kHz)
```
Mode: PCM
Filter: poly-sinc-gauss or poly-sinc-lp
Rate: 176.4/192 kHz (4x) or DSD128
Dither: TPDF or shaped
```

### DSD Source
```
Mode: SDM
Modulator: ASDM7EC-super (general) or ASDM5EC-super (ESS DACs)
Rate: DSD512 or higher
Integrator: FIR2 or IIR
Conversion: XFi
```

### R2R DACs (Holo, Denafrips)
```
Mode: PCM
DAC bits: 20
Filter: poly-sinc-ext2
Rate: 705.6/768 kHz (16x)
Dither: LNS15 or NS5
```
**Причина:** Noise shaping корегує linearity errors R2R DACs

### ESS Sabre DACs
```
Mode: SDM
Modulator: ASDM5EC-super або ASDM5EC-super 512+fs
Rate: DSD512+
```
**Причина:** 5th order краще для ESS чипів

### Multi-element/Delta-Sigma DACs
```
Mode: SDM
Modulator: ASDM7EC-super або ASDM7EC-super 512+fs
Rate: DSD512+
```
**Причина:** 7th order оптимальний для таких DACs

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

**Важливі терміни:**

- **Upsampling** = Збільшення sample rate
- **Oversampling** = Те саме що upsampling
- **Noise shaping** = Зсув quantization noise у вищі частоти
- **Dithering** = Додавання шуму для лінеаризації
- **Delta-Sigma modulation** = Конвертація в 1-bit DSD
- **Apodizing** = Видалення pre-ringing artifacts
- **R2R DAC** = Resistor ladder DAC (multibit)
- **SDM** = Sigma-Delta Modulation (DSD)
- **EC** = Extended Compensation
- **Adaptive** (ASDM) = Адаптується до сигналу

---

## Автоматичний вибір налаштувань (AI логіка)

### Алгоритм для AI DJ:

```python
def auto_select_settings(track_info):
    """
    Автоматичний вибір оптимальних налаштувань HQPlayer
    """
    # 1. Визначити якість джерела
    sample_rate = track_info['sample_rate']
    bit_depth = track_info['bit_depth']
    is_dsd = track_info['format'].startswith('DSD')

    # 2. Визначити тип DAC (якщо відомо)
    dac_type = get_user_dac_type()  # ESS, R2R, Delta-Sigma, Unknown

    # 3. Вибрати режим
    if is_dsd:
        mode = "SDM"
    elif dac_type == "ESS" or dac_type == "Delta-Sigma":
        mode = "SDM"  # ESS краще працює з DSD
    else:
        mode = "PCM"  # R2R та невідомі DACs

    # 4. Вибрати фільтр
    if mode == "PCM":
        if sample_rate >= 96000:
            filter_name = "poly-sinc-ext2"  # Hi-res
        else:
            filter_name = "poly-sinc-gauss"  # Standard
    else:  # SDM
        filter_name = None  # Не потрібен для SDM

    # 5. Вибрати modulator/shaper
    if mode == "SDM":
        if dac_type == "ESS":
            modulator = "ASDM5EC-super 512+fs"  # 5th order для ESS
        else:
            modulator = "ASDM7EC-super 512+fs"  # 7th order для інших
    else:  # PCM
        if sample_rate >= 352800:  # 8x+
            shaper = "LNS15"
        elif sample_rate >= 176400:  # 4x
            shaper = "NS9"
        elif sample_rate >= 88200:  # 2x
            shaper = "NS4" або "shaped"
        else:
            shaper = "TPDF"

    # 6. Вибрати output rate
    if mode == "SDM":
        # Більше = краще, але залежить від CPU
        output_rate = "DSD512"  # 22.5792 MHz (безпечний вибір)
    else:  # PCM
        if dac_type == "R2R":
            output_rate = "768000"  # 16x для корекції linearity
        else:
            output_rate = "192000"  # 4x (safe)

    return {
        "mode": mode,
        "filter": filter_name,
        "modulator_or_shaper": modulator if mode == "SDM" else shaper,
        "output_rate": output_rate
    }
```

### Пріоритети якості:

**Найвища якість (CPU не проблема):**
- Mode: SDM
- Modulator: ASDM7EC-super 512+fs
- Rate: DSD1024 або DSD2048
- Filter: poly-sinc-xla (для PCM sources)

**Баланс якість/CPU:**
- Mode: SDM або PCM
- Modulator: ASDM5/7EC-super
- Rate: DSD256 або DSD512
- Filter: poly-sinc-ext2

**Економ CPU:**
- Mode: PCM
- Filter: poly-sinc-short-lp
- Rate: 192 kHz
- Dither: shaped або TPDF

---

## Джерела інформації

- **Офіційний мануал**: HQPlayer 5 Desktop User Manual v5.16.0
- **SDK**: hqp-control-5292-src (engine 5.29.2)
- **Форум**: HQPlayer Community Forum
- **Розробник**: Jussi Laako / Signalyst

---

**Останнє оновлення:** 2026-02-12
**Версія HQPlayer:** 5.16.3 (Engine 5.34.14)
**Статус:** Актуально
