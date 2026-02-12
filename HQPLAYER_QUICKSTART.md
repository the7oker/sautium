# HQPlayer Integration - Quick Start

## ✅ Готово до використання!

Базова інтеграція з HQPlayer Desktop 5 успішно реалізована і протестована.

## Швидке тестування

### 1. Переконайтеся що HQPlayer запущений на Windows
- Запустіть HQPlayer Desktop 5
- Переконайтеся що він працює (порт 4321 відкритий)

### 2. Запустіть автоматичний тест з WSL
```bash
cd /mnt/d/ai/djai/backend
python3 test_hqplayer_auto.py
```

Очікуваний результат:
```
✅ All tests completed successfully!

📋 Summary:
   • HQPlayer is accessible at 172.26.80.1:4321
   • Version: 5 / Engine: 5.34.14
   • Control API working correctly
```

### 3. Використання в коді

```python
from hqplayer_client import HQPlayerConnection, file_path_to_uri
from config import settings

# Підключення до HQPlayer
with HQPlayerConnection(host=settings.hqplayer_host) as hqp:
    # Отримати статус
    status = hqp.get_status()
    print(f"State: {status.state.name}")

    # Додати трек
    uri = file_path_to_uri("E:\\Music\\Artist\\Album\\Track.flac")
    hqp.playlist_add(uri, clear=True)

    # Відтворити
    hqp.play()

    # Керування гучністю
    hqp.volume_up()
```

## Конфігурація

### .env файл
```env
HQPLAYER_HOST=172.26.80.1  # Windows host IP від WSL
HQPLAYER_PORT=4321
HQPLAYER_ENABLED=true
```

### Для Docker
У `.env` встановіть:
```env
HQPLAYER_HOST=host.docker.internal
```

## Основні можливості

✅ **Управління відтворенням**
- play, pause, stop
- next, previous
- seek, forward, backward

✅ **Плейлист**
- playlist_add
- playlist_clear
- playlist_remove

✅ **Статус**
- get_status (трек, позиція, метадані)
- get_info (версія HQPlayer)

✅ **Гучність**
- set_volume
- volume_up, volume_down
- volume_mute

## Файли

```
backend/
  ├── hqplayer_client.py          # Основний клієнт
  ├── test_hqplayer_auto.py       # Автоматичний тест
  └── test_hqplayer.py            # Інтерактивний тест

docs/
  └── HQPLAYER_INTEGRATION.md     # Повна документація

sdk/
  └── hqp-control-5292-src/       # HQPlayer SDK (C++)
```

## Перевірка підключення

### З WSL
```bash
# Знайти IP хоста Windows
ip route show | grep default
# Output: default via 172.26.80.1 ...

# Перевірити доступність порту
nc -zv 172.26.80.1 4321
# Output: Connection to 172.26.80.1 4321 port [tcp/*] succeeded!
```

### З Docker (після запуску контейнера)
```bash
docker exec music-ai-backend nc -zv host.docker.internal 4321
```

## Troubleshooting

### Connection refused
1. Переконайтеся що HQPlayer запущений
2. Перевірте Windows Firewall (порт 4321)
3. Перевірте IP хоста: `ip route show | grep default`

### З Docker не підключається
1. Додайте `extra_hosts` в docker-compose.yml (вже додано)
2. Використовуйте `host.docker.internal` в HQPLAYER_HOST
3. Або вкажіть конкретний IP: `172.26.80.1`

## Наступні кроки

1. ✅ Базова інтеграція - **ГОТОВО**
2. ⏳ Інтеграція з AI DJ (рекомендації → HQPlayer)
3. ⏳ Голосове керування (Phase 4.3)
4. ⏳ Додаткові функції (metering, DSP settings)

## Повна документація

Детальна документація: [docs/HQPLAYER_INTEGRATION.md](docs/HQPLAYER_INTEGRATION.md)

---

**Статус**: ✅ Готово до використання
**Тестовано**: HQPlayer Desktop 5.16.3 (Engine 5.34.14)
**Платформа**: Windows (доступ з WSL2 та Docker)
