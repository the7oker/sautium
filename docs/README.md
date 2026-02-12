# Music AI DJ - Документація

## Структура документації

### 📘 Основна документація

- **[HQPLAYER_INTEGRATION.md](HQPLAYER_INTEGRATION.md)** - Технічна документація інтеграції з HQPlayer
  - API reference
  - Приклади використання
  - Troubleshooting
  - ~60 KB повної інформації

- **[HQPLAYER_KNOWLEDGE_BASE.md](HQPLAYER_KNOWLEDGE_BASE.md)** - База знань для AI агента
  - Витяг з офіційного мануалу
  - Опис всіх DSP налаштувань
  - Рекомендації для різних сценаріїв
  - Алгоритми автоматичного вибору налаштувань
  - ~18 KB структурованої інформації

### 📖 Оригінальний мануал

- **[hqplayer5desktop-manual.pdf](hqplayer5desktop-manual.pdf)** - Офіційний мануал HQPlayer 5 Desktop v5.16.0
  - 63 сторінки повної документації
  - Детальні описи всіх функцій
  - Технічні специфікації

### 🚀 Quick Start гайди

- **[../HQPLAYER_QUICKSTART.md](../HQPLAYER_QUICKSTART.md)** - Швидкий старт
  - Базові інструкції
  - Перші кроки
  - Швидке тестування

- **[../DSP_CONTROLS_SUMMARY.md](../DSP_CONTROLS_SUMMARY.md)** - Підсумок DSP контролю
  - Практичні приклади
  - Всі доступні налаштування
  - Код snippets

## Використання для AI агента

### Контекст для розуміння HQPlayer

AI агент має доступ до:

1. **Технічних специфікацій** (HQPLAYER_INTEGRATION.md)
   - Як підключитися
   - Які команди доступні
   - Як тестувати

2. **Знань про аудіо обробку** (HQPLAYER_KNOWLEDGE_BASE.md)
   - Що таке PCM/DSD
   - Які фільтри для чого
   - Як вибирати налаштування

3. **Оригінальної документації** (PDF manual)
   - Детальні технічні описи
   - Специфікації алгоритмів

### Рекомендований порядок вивчення

1. **Спочатку**: HQPLAYER_QUICKSTART.md (швидке розуміння)
2. **Потім**: HQPLAYER_KNOWLEDGE_BASE.md (детальні знання)
3. **Якщо потрібно**: HQPLAYER_INTEGRATION.md (технічна імплементація)
4. **Для референсу**: hqplayer5desktop-manual.pdf (повна документація)

## Ключові концепції

### Режими роботи
- **[source]** - Без обробки
- **PCM** - Upsampling до високих PCM частот
- **SDM (DSD)** - Конвертація в DSD формат

### DSP Pipeline
```
Source → Filter → Modulator/Shaper → Output
         (upsampling)  (noise shaping)
```

### Автоматичний вибір налаштувань

AI агент може автоматично вибирати оптимальні налаштування на основі:
- Якості джерела (sample rate, bit depth)
- Типу DAC (якщо відомо)
- Жанру музики
- Потужності CPU

**Приклад:**
```
Hi-res FLAC (192 kHz/24-bit) + R2R DAC
→ PCM mode
→ poly-sinc-ext2 filter
→ 768 kHz output
→ LNS15 noise shaping
```

## Практичне застосування

### Сценарії використання

1. **Базове відтворення**
   - Додати трек в плейлист
   - Відтворити
   - Контроль гучності

2. **Оптимізація якості**
   - Визначити тип джерела
   - Вибрати оптимальний режим
   - Налаштувати фільтри

3. **Голосове керування** (майбутнє)
   - "Встанови найкращу якість"
   - "Переключи на DSD режим"
   - "Адаптуй під цей трек"

## Інтеграція з Music AI DJ

### Можливості

- ✅ Автоматичний вибір налаштувань на основі треку
- ✅ Профілі для різних жанрів
- ✅ Оптимізація під конкретний DAC
- ✅ Голосове керування (Phase 4)

### Приклад інтеграції

```python
from hqplayer_client import HQPlayerConnection
from database import get_db_context
from models import Track

def play_track_optimized(track_id: int):
    """
    Відтворити трек з автоматичною оптимізацією HQPlayer
    """
    with get_db_context() as db:
        track = db.query(Track).get(track_id)

        # Визначити оптимальні налаштування
        settings = auto_select_hqplayer_settings(track)

        with HQPlayerConnection() as hqp:
            # Налаштувати HQPlayer
            hqp.set_mode(settings['mode'])
            hqp.set_filter(settings['filter'])
            hqp.set_rate(settings['rate'])

            # Відтворити
            hqp.playlist_add(track.file_path, clear=True)
            hqp.play()
```

## Оновлення документації

При появі нових версій HQPlayer:
1. Оновити hqplayer5desktop-manual.pdf
2. Переглянути HQPLAYER_KNOWLEDGE_BASE.md
3. Додати нові функції в HQPLAYER_INTEGRATION.md
4. Оновити приклади коду

## Контрибуція

При додаванні нової інформації:
- Підтримувати структуру
- Додавати приклади
- Перевіряти актуальність
- Оновлювати версії

---

**Статус документації:** ✅ Актуально
**Версія HQPlayer:** 5.16.3 (Engine 5.34.14)
**Останнє оновлення:** 2026-02-12
