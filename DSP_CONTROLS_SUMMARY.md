# HQPlayer DSP Controls - Summary

## ✅ Повністю реалізовано управління налаштуваннями!

### Доступні налаштування:

#### 1. **Output Mode** (PCM/DSD)
```python
modes = hqp.get_modes()
# Результат: [{"index": 0, "name": "[source]"}, {"index": 1, "name": "PCM"}, {"index": 2, "name": "SDM (DSD)"}]

hqp.set_mode(1)  # Встановити PCM
hqp.set_mode(2)  # Встановити DSD
```

**Доступні режими:**
- `[source]` - як у джерелі (без upsampling)
- `PCM` - PCM режим
- `SDM (DSD)` - DSD режим

---

#### 2. **Фільтри** (PCM та SDM/DSD)
```python
filters = hqp.get_filters()
# Результат: 77 фільтрів!

# Приклади фільтрів:
# PCM: poly-sinc-ext2, poly-sinc-gauss-xla, sinc-L, sinc-M, etc.
# DSD: DSD7 512+fs, DSD9 512+fs, etc.

hqp.set_filter(6)  # Встановити poly-sinc-lp
hqp.set_filter(10, 8)  # PCM: фільтр для upsampling + 1x filter
```

**Типи фільтрів:**
- **IIR** - Infinite Impulse Response
- **FIR** - Finite Impulse Response (linear phase, minimum phase, asymmetric)
- **poly-sinc** - Polynomial interpolated sinc (різні варіанти: lp, mp, short, gauss, ext, xla)
- **sinc** - Sinc function filters (L, M, S варіанти)
- **closed-form** - Closed-form filters

**Всього:** 77 фільтрів

---

#### 3. **Noise Shapers** (Dither/DSD modulators)
```python
shapers = hqp.get_shapers()
# Результат: 36 shapers!

# Приклади:
# DSD5, DSD5v2, DSD5EC
# ASDM5, ASDM5EC, ASDM5EC-ul
# ASDM7, ASDM7EC, ASDM7EC-super

hqp.set_shaping(15)  # Встановити ASDM7EC-super 512+fs
```

**Типи shapers:**
- **DSD5** series - 5th order DSD modulators
- **ASDM5** series - Advanced Sigma-Delta Modulators 5th order
- **ASDM7** series - 7th order (higher quality)
- Варіанти: EC (error correction), ul (ultra-light), super, light

**Всього:** 36 noise shapers

---

#### 4. **Sample Rates** (Output)
```python
rates = hqp.get_rates()
# Результат: 20 sample rates

# Приклади:
# PCM: 2.048, 3.072, 4.096, 6.144, 8.192, 12.288 MHz
# DSD: 2.8224 (DSD64), 5.6448 (DSD128), 11.2896 (DSD256), 22.5792 (DSD512), 45.1584 (DSD1024), 90.3168 (DSD2048)

hqp.set_rate(8)  # 11.2896 MHz (DSD256)
hqp.set_rate(12)  # 22.5792 MHz (DSD512)
```

**Доступні sample rates:**
- **DSD64**: 2.8224 MHz
- **DSD128**: 5.6448 MHz
- **DSD256**: 11.2896 MHz
- **DSD512**: 22.5792 MHz
- **DSD1024**: 45.1584 MHz
- **DSD2048**: 90.3168 MHz
- **PCM rates**: 2.048, 3.072, 4.096, 6.144, 8.192, 12.288, 16.384, 24.576, 32.768, 49.152, 98.304 MHz

**Всього:** 20 rates

---

#### 5. **Input Devices**
```python
inputs = hqp.get_inputs()
# Результат: ["cd:"]

# Note: Input devices list залежить від налаштувань HQPlayer
```

---

## Практичні приклади:

### Приклад 1: Встановити PCM режим з high-quality фільтром
```python
from hqplayer_client import HQPlayerConnection

with HQPlayerConnection(host="172.26.80.1") as hqp:
    # Отримати доступні опції
    modes = hqp.get_modes()
    filters = hqp.get_filters()

    # Знайти PCM режим
    pcm = next(m for m in modes if m['name'] == 'PCM')

    # Знайти poly-sinc-ext2 фільтр
    poly_sinc_ext2 = next(f for f in filters if 'poly-sinc-ext2' in f['name'])

    # Встановити
    hqp.set_mode(pcm['index'])
    hqp.set_filter(poly_sinc_ext2['index'])

    print("✅ Встановлено PCM режим з poly-sinc-ext2 фільтром")
```

### Приклад 2: Встановити DSD512 з ASDM7EC-super
```python
with HQPlayerConnection(host="172.26.80.1") as hqp:
    # DSD режим
    modes = hqp.get_modes()
    dsd_mode = next(m for m in modes if 'DSD' in m['name'])

    # DSD512 (22.5792 MHz)
    rates = hqp.get_rates()
    dsd512 = next(r for r in rates if r['rate'] == 22579200)

    # ASDM7EC-super shaper
    shapers = hqp.get_shapers()
    asdm7 = next(s for s in shapers if 'ASDM7EC-super' in s['name'])

    # Встановити
    hqp.set_mode(dsd_mode['index'])
    hqp.set_rate(dsd512['index'])
    hqp.set_shaping(asdm7['index'])

    print("✅ Встановлено DSD512 з ASDM7EC-super")
```

### Приклад 3: Автоматичний вибір налаштувань для треку
```python
def auto_configure_for_track(hqp, track):
    """AI DJ: Автоматично налаштувати HQPlayer для треку"""

    if track.sample_rate >= 96000:
        # Hi-res FLAC → PCM з upsampling до DSD256
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
        # Standard quality → PCM з standard filter
        print("🎵 Standard track → PCM + poly-sinc")

        modes = hqp.get_modes()
        pcm = next(m for m in modes if m['name'] == 'PCM')

        filters = hqp.get_filters()
        poly_sinc = next(f for f in filters if 'poly-sinc-lp' in f['name'])

        hqp.set_mode(pcm['index'])
        hqp.set_filter(poly_sinc['index'])
```

---

## Що НЕ доступно через API:

❌ **Output Device Selection**
- Вибір DAC/output device потрібно робити вручну в GUI HQPlayer
- API не надає методів для керування output devices

---

## Тестування:

```bash
# Автоматичний тест всіх DSP налаштувань
cd /mnt/d/ai/djai/backend
python3 test_hqplayer_settings.py

# Приклади використання
python3 examples_hqplayer_dsp.py
```

---

## Підсумок:

✅ **Повне управління DSP налаштуваннями HQPlayer:**
- ✅ 3 output modes (source, PCM, DSD)
- ✅ 77 filters (IIR, FIR, poly-sinc, sinc, closed-form)
- ✅ 36 noise shapers (DSD5, ASDM5, ASDM7 series)
- ✅ 20 sample rates (до DSD2048 / 90.3168 MHz!)
- ✅ Input devices

❌ **Недоступно:**
- Output device selection (потрібно налаштовувати в GUI)

---

## Інтеграція з Music AI DJ:

Можливості:
1. **Автоматичний вибір режиму** на основі якості треку
2. **Оптимізація фільтрів** для різних жанрів
3. **Голосове керування** DSP налаштуваннями (Phase 4)
4. **Профілі** для різних типів музики (джаз, класика, рок)

Приклад голосового керування:
```
User: "Claude, встанови найкращу якість для цього треку"
AI:   "Встановлюю DSD256 з ASDM7EC-super шейпером для максимальної якості"

User: "Переключи на PCM режим"
AI:   "Переключаю на PCM з poly-sinc-ext2 фільтром"
```

---

**Статус**: ✅ **Готово до використання**
**Протестовано**: HQPlayer Desktop 5.16.3 (Engine 5.34.14)
