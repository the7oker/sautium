# P2P Network — Music AI DJ

## Vision

Перетворити Music AI DJ з локального плеєра на **безсерверну P2P мережу**, де люди з великими офлайновими музичними бібліотеками можуть:

- **Ділитись аналітикою** — метадані, audio embeddings, аудіо фічі
- **Шукати нову музику** — "хто з мережі має щось схоже на цей трек?"
- **Знаходити однодумців** — люди зі схожими музичними смаками
- **Спілкуватись** — чат між учасниками мережі
- **Обмінюватись файлами** (майбутнє) — легальний контент, незалежні виконавці

### Чому це потрібно

Стрімінгові сервіси домінують, але велика аудиторія все ще:
- Збирає FLAC бібліотеки з торентів, CD, вінілу
- Хоче якісний звук (HQPlayer, DSD upsampling)
- Не має інструментів для discovery нової музики у своїй офлайн-колекції
- Ізольовані — не бачать що слухають інші колекціонери

### Що шариться

| Дані | Фаза | Опис |
|------|-------|------|
| Метадані треків | P2 | Artist, album, title, year, genre, duration |
| Audio embeddings | P2 | CLAP 512d вектори для пошуку схожості |
| Audio features | P2 | Tempo, key, energy, danceability, etc. |
| Artist enrichment | P2 | Bios, tags, similar artists |
| Text embeddings | P3 | Multilingual 384d вектори (опис, теги) |
| Тексти пісень | P4 | Lyrics (якщо не захищені авторським правом) |
| Аудіо файли | P5 | Тільки легальний контент (незалежні виконавці, CC-ліцензії) |

### Що НЕ шариться

- Локальні шляхи до файлів
- Дані HQPlayer/плеєра
- Приватні нотатки користувача
- Історія прослуховування (якщо користувач не обрав шарити)

---

## Architecture Overview

```
┌─────────────────────────────────────────────┐
│              Music AI DJ Node                │
├─────────────────────────────────────────────┤
│  LOCAL LAYER (існуюче)                       │
│  ├── PostgreSQL + pgvector                   │
│  ├── FastAPI Backend (search, AI DJ, etc.)   │
│  ├── CLAP Audio Embeddings (512d)            │
│  ├── Text Embeddings (384d)                  │
│  ├── Audio Features (tempo, key, energy...)  │
│  ├── HQPlayer Control                        │
│  └── Web UI                                  │
├─────────────────────────────────────────────┤
│  P2P LAYER (нове)                            │
│  ├── Node Identity (Ed25519 keypair)         │
│  ├── aiohttp Sync Server (HTTP + JSON + gz)  │
│  ├── libtorrent DHT (per-artist announces)   │
│  ├── NAT Traversal (UPnP)                   │
│  └── Sync Client (HTTP pull from peers)      │
├─────────────────────────────────────────────┤
│  UI LAYER                                    │
│  ├── Connect/Disconnect toggle               │
│  ├── Network peers list                      │
│  ├── Cross-library search (Phase P3)         │
│  └── Chat (Phase P4)                         │
└─────────────────────────────────────────────┘
```

### Network Topology

```
                ┌──────────────────────┐
                │ BitTorrent DHT       │
                │ (router.bittorrent   │
                │  .com, etc.)         │
                └──────┬───────────────┘
                       │ bootstrap
            ┌──────────┴──────────┐
            │                     │
    ┌───────┴───────┐    ┌───────┴───────┐
    │   Node A      │    │   Node B      │
    │ (Kyiv)        │◄──►│ (Berlin)      │
    │ 12,000 tracks │    │ 8,000 tracks  │
    └───────┬───────┘    └───────┬───────┘
            │                     │
            │    ┌───────────┐    │
            └───►│  Node C   │◄──┘
                 │ (Tokyo)   │
                 │ 45k tracks│
                 └───────────┘

Зв'язки: прямі UDP/TCP через NAT (UPnP + hole punching)
```

---

## Technology Choices

### P2P & Networking

| Component | Library | Why |
|-----------|---------|-----|
| DHT + file transfer | **`libtorrent`** (C++ з Python bindings) | Доступ до публічної BT DHT (мільйони нод), вбудований файловий обмін, pip-installable |
| NAT traversal | **`miniupnpc`** (UPnP) + STUN | UPnP для роутера, STUN для визначення зовнішнього IP |
| Transport | **HTTP + JSON + gzip** | Той самий sync протокол що вже працює (inventory → batch pull), без кастомного бінарного формату |
| Identity | **`cryptography`** (Ed25519) | Стандарт, швидкий, компактні ключі (32 bytes) |
| Async networking | **`asyncio`** + `aiohttp` | Вже використовується в проєкті |

### Чому libtorrent, а НЕ kademlia (pure Python)

> **ВАЖЛИВО**: Бібліотека `kademlia` (bmuller) **несумісна** з BitTorrent DHT!
> - Використовує MsgPack serialization (BT DHT використовує Bencode)
> - Різні RPC операції (STORE/FIND_VALUE vs get_peers/announce_peer)
> - Неможливо підключитись до router.bittorrent.com
> - Створює **окрему приватну мережу** яку треба будувати з нуля

**libtorrent переваги:**
- Доступ до публічної BitTorrent DHT (мільйони нод — не треба будувати мережу з нуля)
- BEP44 — зберігання довільних даних в DHT (mutable/immutable items)
- Вбудований файловий обмін (BitTorrent protocol) — для фази P5
- Pre-built wheels: `pip install libtorrent` працює на Windows (Python 3.10-3.13)
- Додатковий пакет `libtorrent-windows-dll` для OpenSSL DLL на Windows

### "Безсерверність"

Зовнішні безкоштовні ресурси (не потрібно орендувати сервер):
- **Bootstrap DHT**: `router.bittorrent.com:6881`, `dht.transmissionbt.com:6881`
- **STUN servers**: `stun.l.google.com:19302`, `stun.cloudflare.com:3478`
- **Relay fallback**: якщо потрібно — Oracle Cloud Free Tier, або relay через інших пірів мережі

### Windows Firewall & NAT — вирішено

**Firewall (OS рівень):**
- Windows автоматично показує prompt "Windows Security Alert" коли додаток слухає порт
- Користувач натискає "Allow" один раз → правило зберігається назавжди
- **Inno Setup інсталятор** (`desktop/installer/musicaidj.iss`) вже є в проєкті і працює з правами адміна
  → можемо додати `netsh advfirewall firewall add rule` в секцію `[Run]`
  (як робить qBittorrent — checkbox "Add Windows Firewall rule" в інсталяторі)
- Для тонкого лаунчера (без інсталятора): Windows сам покаже prompt при першому запуску

**NAT (роутер рівень):**
- UPnP (`miniupnpc`) — автоматичне відкриття порту на роутері, без взаємодії з користувачем
- Увімкнений на більшості домашніх роутерів за замовчуванням (~80%)
- Fallback: DHT все одно працює через UDP outbound (outbound завжди дозволений)

---

## Content-Addressable IDs (Детерміновані UUID)

### Принцип

Для P2P обміну важливо щоб **однакові дані мали однаковий ID** на всіх нодах.
Проєкт вже використовує UUID v5 для core entities — треба розширити на всі shareable дані.

### Поточний стан (вже реалізовано)

| Entity | ID Type | Формула | Статус |
|--------|---------|---------|--------|
| Artist | UUID v5 | `uuid5(NS, "artist:{normalize(name)}")` | ✅ Готово |
| Album | UUID v5 | `uuid5(NS, "album:{normalize(artist)}:{normalize(title)}")` | ✅ Готово |
| Track | UUID v5 | `uuid5(NS, "song:{normalize(artist)}:{normalize(title)}")` | ✅ Готово |

Namespace: `5ba7a9d0-1f8c-4c3d-9e7a-2b4f6c8d0e1f` (фіксований, в `backend/uuid_utils.py`)

### Конвертовано в UUID v5 (Phase P1) ✅

| Entity | Формула | Статус |
|--------|---------|--------|
| Genre | `uuid5(NS, "genre:{normalize(name)}")` | ✅ Готово (міграція 002) |
| Tag | `uuid5(NS, "tag:{normalize(name)}")` | ✅ Готово (міграція 002) |
| EmbeddingModel | `uuid5(NS, "embedding_model:{normalize(name)}")` | ✅ Готово (міграція 002) |

Міграція `002_uuid_genres_tags_models.sql` включає дедуплікацію case-варіантів (напр. "Blues"/"blues") перед конвертацією.

### Embeddings та Audio Features

Embeddings ідентифікуються через комбінацію **(track_uuid, model_uuid)** — обидва вже детерміновані.
Сам embedding ID може залишитись SERIAL (він не шариться — шариться вектор прив'язаний до track_uuid).

**Стратегія обміну:**
1. Пір А надсилає список своїх track UUIDs
2. Пір Б відповідає які з них у нього є / яких нема
3. Для спільних треків — можна порівняти embeddings / features
4. Для відсутніх — отримати metadata + embeddings від піра

### Протокол обміну даними

```
Пір A (запитувач)                     Пір B (відповідач)
─────────────────                     ─────────────────
1. "Ось мої track UUIDs"  ──────►
                           ◄──────  2. "Ось які я маю / не маю"
3. "Дай metadata для цих"  ──────►
                           ◄──────  4. Metadata + embeddings (gzip)
```

**Стиснення**: 30k треків metadata ≈ 15MB JSON → ~3MB gzip. Embeddings (512 floats × 30k) ≈ 60MB → ~25MB gzip.

---

## DHT Discovery Strategy

### Принцип: Анонсування по артистах

Launcher **не анонсує себе як ноду** — він анонсує **кожного артиста**, на якого має enrichment дані
(embedding або audio_features для хоча б 1 треку).

```python
# Launcher з enriched Pink Floyd анонсує:
artist_infohash = SHA1("MusicAIDJ-artist:" + artist_uuid)
session.dht_announce(artist_infohash, port=19000)

# Інший launcher шукає enrichment для Pink Floyd:
peers = session.dht_get_peers(artist_infohash)
# → отримує IP:port launchers які мають enrichment для Pink Floyd
```

**Критерій анонсу**: артист має хоча б 1 трек з embedding АБО audio_features.

**Масштаб** (на прикладі master бази):
- 2 550 enriched артистів → 2 550 DHT announces
- Re-announce кожні 15 хв: ~3 announce/sec — мізер для libtorrent
- Навіть 10k артистів (~11/sec) — в межах норми

**Переваги:**
- Точний пошук: "хто має Pink Floyd?" → прямий DHT lookup
- Launcher шукає тільки конкретних артистів, не весь каталог
- Не потрібен загальний infohash мережі (немає broadcast/flood)
- Масштабується природно — чим більше учасників, тим більше артистів доступно

---

## Implementation Plan

### Phase P0: Launcher ↔ Backend Bridge

**Мета**: Windows launcher отримує дані з FastAPI backend через REST API.
Це фундамент — P2P layer буде обмінюватись саме цими даними.

**Що зробити**:

1. **Backend API для експорту даних** (`/api/export/`)
   ```
   GET /api/export/catalog          → повний каталог (artists, albums, tracks metadata)
   GET /api/export/embeddings       → audio embeddings (track_uuid → vector)
   GET /api/export/text-embeddings  → text embeddings
   GET /api/export/audio-features   → аудіо фічі (tempo, key, energy, etc.)
   GET /api/export/stats            → агреговані статистики бібліотеки
   ```

2. **Launcher API client** (`desktop/api_client.py`)
   - HTTP клієнт для з'єднання з `localhost:8000`
   - Кешування відповідей (SQLite або JSON файли)
   - Health check / connection status

3. **Launcher UI updates**
   - Показати статистику бібліотеки в головному вікні
   - Індикатор з'єднання з backend
   - Кнопка "Library Info" з деталями

**Критерій готовності**: Launcher показує реальні дані з backend (кількість треків, артистів, статус embeddings).

---

### Phase P1: DB Refactoring + Node Identity

**Мета**: Підготувати базу до P2P обміну та створити криптографічну ідентичність ноди.

**DB Refactoring:**

1. **Genre** → UUID v5 primary key
   - `uuid5(NS, "genre:{normalize(name)}")`
   - Оновити `track_genres`, `genre_descriptions` foreign keys
   - Міграційний скрипт (як існуючий `migrate_to_uuid.py`)

2. **Tag** → UUID v5 primary key
   - `uuid5(NS, "tag:{normalize(name)}")`
   - Оновити `artist_tags`, `album_tags` foreign keys

3. **EmbeddingModel** → UUID v5 primary key
   - `uuid5(NS, "model:{name}")`
   - Оновити `embeddings`, `text_embeddings`, `lyrics_embeddings` foreign keys

**Node Identity** (`backend/p2p/identity.py`):
- Генерація Ed25519 keypair при першому запуску
- Збереження в `%LOCALAPPDATA%/MusicAIDJ/identity.key` (Windows)
- Node ID = SHA-256(public_key)[:20] (20 bytes, як у BitTorrent)
- Nickname (user-configurable, default = random adjective+noun)

---

### Phase P2: Launcher Sync Server + DHT Discovery

**Мета**: Launcher стає і клієнтом, і сервером. Знаходить пірів через DHT, синхронізується через HTTP.

**Шлях користувача**:
```
Scan Library → Sync Library (DHT пошук пірів) → Enrich Tracks (те що не знайшов) → Анонс своїх артистів
```

**Архітектура процесів в launcher**:
```
Main Thread:   CustomTkinter GUI (tkinter mainloop)
Background:    asyncio event loop (окремий потік)
               ├── aiohttp HTTP server (порт 19000) — обслуговує sync запити від пірів
               └── libtorrent DHT polling — анонси enriched артистів + пошук пірів
```

**Що зробити**:

1. **Sync Server** (`desktop/p2p/sync_server.py`)
   - aiohttp HTTP server на конфігурованому порті (default: 19000)
   - Ті ж endpoints що в `backend/routers/sync.py`:
     - `POST /api/sync/inventory` — inventory по track UUIDs
     - `POST /api/sync/pull/{category}` — batch pull (11 категорій)
   - JSON + gzip compression (стандартний HTTP Content-Encoding)
   - Rate limiting на вхідні запити

2. **Sync Queries** (`desktop/p2p/sync_queries.py`)
   - SQL запити витягнуті з `backend/routers/sync.py` у спільний модуль
   - Працює з будь-яким PostgreSQL з'єднанням (Docker або локальна БД)
   - Без залежності від FastAPI/aiohttp — чиста бізнес-логіка

3. **DHT Service** (`desktop/p2p/dht_service.py`)
   - libtorrent DHT session з bootstrap від публічних нод
   - **Announce**: для кожного enriched артиста (embedding або audio_features)
     ```python
     infohash = SHA1("MusicAIDJ-artist:" + artist_uuid)
     session.dht_announce(infohash, port=19000)
     ```
   - **Lookup**: знайти пірів з enrichment для конкретного артиста
     ```python
     peers = dht.get_peers(SHA1("MusicAIDJ-artist:" + artist_uuid))
     # → [(ip, port), ...]
     ```
   - Periodic re-announce кожні 15 хвилин
   - Кеш знайдених пірів (щоб не робити DHT lookup кожен раз)

4. **P2P Manager** (`desktop/p2p/p2p_manager.py`)
   - Оркестрація: старт/стоп DHT + HTTP server
   - Інтеграція з `SyncClient`: замість фіксованого `source_url` → DHT lookup
   - Логіка Sync Library:
     ```
     1. Отримати список артистів без enrichment
     2. Для кожного артиста: DHT lookup → знайти пір(и)
     3. HTTP sync з кожним знайденим піром (стандартний inventory → pull)
     4. Імпорт отриманих даних в локальну БД
     ```

5. **Launcher UI**
   - Connect/Disconnect toggle (вмикає/вимикає DHT + HTTP server)
   - Показати node ID
   - Статус: "Online (announcing N artists, M peers found)"

**Docker backend = ще один пір**:
Docker backend використовує той самий протокол. Різниця лише в обгортці —
FastAPI замість aiohttp, але SQL запити ті ж самі через `sync_queries.py`.

**Структура файлів**:
```
desktop/p2p/
  __init__.py
  sync_server.py      # aiohttp HTTP server (sync endpoints)
  sync_queries.py      # SQL запити (спільна логіка)
  dht_service.py       # libtorrent DHT: announce + lookup
  p2p_manager.py       # Оркестрація: старт/стоп, інтеграція з SyncClient

backend/
  dht_service.py       # Та ж DHT логіка для Docker backend
  main.py              # DHT інтегровано в FastAPI lifespan
  config.py            # P2P_ENABLED, P2P_DHT_PORT, P2P_ANNOUNCE_PORT
```

**Тестування**: 2 інстанси launcher на localhost (різні порти, різні бази),
один з enrichment — інший робить Sync Library і отримує дані.

---

### Phase P3: NAT Traversal + Cross-Library Search

**Мета**: Забезпечити роботу через NAT (реальний інтернет) та додати крос-бібліотечний пошук.

**NAT Traversal** (`desktop/p2p/nat.py`):
- UPnP port mapping через `miniupnpc` (автоматичне, без UI)
- STUN для визначення зовнішнього IP:port
- Fallback: DHT все одно працює через UDP outbound

**Peer Cache** (розширення `p2p_manager.py`):
- Persistent список відомих пірів (зберігати між сесіями)
- Timeout на повільних/недоступних пірів
- Пріоритизація пірів з більшою кількістю enriched артистів

**Cross-Library Search**:
- "Хто з мережі має щось схоже на цей трек?" → embedding similarity
- Distributed query до знайдених пірів паралельно
- Library comparison (overlap analysis, taste similarity)
- Timeout 5 секунд на повільних пірів

**Handshake Protocol** (підготовка до P4):
- Обмін node ID, library stats, capabilities
- Потрібен для соціальних фіч (друзі, чат)
- Ed25519 підпис повідомлень

---

### Phase P4: Social Features

**Мета**: Комунікація між учасниками мережі.

1. **Peer-to-Peer Chat**
   - Прямі повідомлення між нодами
   - End-to-end encryption (X25519 key exchange + AES-256-GCM)
   - Offline message queue (зберігати до наступного з'єднання)

2. **Nickname System**
   - User-configurable nickname
   - Публічний ключ як stable identifier
   - Nickname uniqueness не гарантується (як у IRC)

3. **Music Recommendations**
   - "Рекомендую цей альбом" → broadcast до друзів
   - Shared playlists (список track metadata, не файли)
   - "Що зараз слухає [nickname]?" (opt-in)

4. **Friends / Trust**
   - Додати пір як "друг" (mutual follow)
   - Приоритет з'єднання для друзів
   - Автоматичне перепідключення до друзів

---

### Phase P5: File Sharing (BitTorrent)

**Мета**: Обмін аудіо файлами для легального контенту.

**Легальні кейси:**
- Незалежні виконавці (indie artists, hobby musicians)
- Creative Commons ліцензії
- Авторські релізи
- Демо-записи

**Реалізація** (libtorrent вже вміє все це):
- Створення .torrent файлів для шарених альбомів/треків
- Seeding через libtorrent (DHT tracker, без центрального трекера)
- Piece-based transfer з swarming (декілька пірів → швидше)
- Resume downloads (перервані завантаження продовжуються)
- Верифікація цілісності (piece hashes)

**UI:**
- Позначка "Share this album" для легального контенту
- Download progress / seeding status
- Bandwidth limiting

**Юридична safety:**
- Користувач явно обирає що шарити (opt-in)
- Попередження про авторські права
- Система тегів ліцензій (CC-BY, CC-SA, Public Domain, Self-Released)

---

## Data Format for P2P Exchange

### Shared Catalog Entry (per track)
```json
{
  "track_uuid": "550e8400-e29b-41d4-a716-446655440000",
  "title": "Comfortably Numb",
  "artist_uuid": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
  "artist_name": "Pink Floyd",
  "album_uuid": "7ca7b810-9dad-11d1-80b4-00c04fd430c8",
  "album_title": "The Wall",
  "year": 1979,
  "genres": [
    {"uuid": "...", "name": "Progressive Rock"},
    {"uuid": "...", "name": "Art Rock"}
  ],
  "duration_seconds": 382,
  "available_formats": [
    {"format": "FLAC", "sample_rate": 96000, "bit_depth": 24, "lossless": true},
    {"format": "FLAC", "sample_rate": 44100, "bit_depth": 16, "lossless": true}
  ]
}
```

### Embedding Exchange (lazy, on demand)
```json
{
  "track_uuid": "550e8400-e29b-41d4-a716-446655440000",
  "model_uuid": "...",
  "model_name": "laion/clap-htsat-unfused",
  "vector": [0.123, -0.456, ...]
}
```

### Audio Features Exchange
```json
{
  "track_uuid": "550e8400-e29b-41d4-a716-446655440000",
  "tempo": 63.2,
  "key": 2, "mode": 0,
  "energy": 0.45, "danceability": 0.22,
  "acousticness": 0.35, "brightness": 0.38
}
```

### Bulk Exchange Protocol

```
Фаза 1: Catalog sync (lightweight)
  A → B: "Ось мої artist UUIDs" (compact set)
  B → A: "Маю 80% overlap. Ось мої унікальні artists + їх tracks" (gzip)

Фаза 2: Embedding sync (on demand)
  A → B: "Дай embeddings для цих track UUIDs" (list)
  B → A: Embedding vectors (gzip, ~2 bytes/float з quantization)

Фаза 3: Feature sync (on demand)
  A → B: "Дай audio features для цих track UUIDs" (list)
  B → A: Feature values (gzip JSON)
```

---

## Security Considerations

### Phase 1 (MVP)
- Ed25519 keypair для ідентифікації
- **Connect/Disconnect кнопка** (повний контроль користувача)
- Шариться тільки metadata (ніяких файлових шляхів!)
- Rate limiting на вхідні запити від пірів

### Future
- End-to-end encryption для чату (X25519 + AES-256-GCM)
- Selective sharing (вибрати які артисти/альбоми видимі)
- Blocklist для небажаних нод
- Bandwidth limiting (конфігурація в Settings)
- IP reputation (автоматичний бан для flood/spam)

---

## Testing Strategy

### One Machine Testing (primary method)
```
Terminal 1: Launcher A (port 19000, DB: musicaidj_a) — має enrichment
Terminal 2: Launcher B (port 19001, DB: musicaidj_b) — без enrichment, тільки scan
```
Launcher A анонсує enriched артистів в DHT.
Launcher B шукає тих самих артистів через DHT, знаходить Launcher A, синхронізується.

### Integration Test Scenario
1. Launcher A starts → DHT bootstrap → announces enriched artists (per-artist infohash)
2. Launcher B starts → DHT bootstrap → Sync Library
3. B визначає артистів без enrichment → DHT lookup для кожного
4. B знаходить A через DHT → HTTP sync (inventory → pull)
5. B імпортує enrichment дані (embeddings, audio_features, bios, tags)
6. B тепер сам може анонсувати ці артисти в DHT
7. A goes offline → B продовжує працювати локально з отриманими даними

---

## Immediate Next Steps (Priority Order)

1. ~~**[Phase P0]** Додати export API в backend~~ ✅ (backend вже має `/stats` endpoint)
2. ~~**[Phase P0]** Створити API client в launcher~~ ✅ (`desktop/api_client.py`)
3. ~~**[Phase P0]** Показати library stats в launcher UI~~ ✅ (stats section в launcher)
4. ~~**[Phase P1]** DB refactoring: Genre, Tag, EmbeddingModel → UUID v5~~ ✅ (міграція 002)
5. ~~**[Phase P1]** Реалізувати node identity (Ed25519 keypair)~~ ✅ (`desktop/node_identity.py`)
6. ~~**[Phase S1]** HTTP sync: inventory → batch pull → import~~ ✅ (`backend/routers/sync.py`, `desktop/sync_client.py`)
   - 12 API endpoints (1 inventory + 11 pull categories)
   - Batch INSERT via `execute_values`, 500/batch, single DB connection
   - Compound artist UUID fix in scanner
   - Performance: ~125 tracks/sec (424 tracks in 3.4 sec)
7. ~~**[Phase P2]** Витягнути sync SQL логіку в `desktop/p2p/sync_queries.py`~~ ✅
8. ~~**[Phase P2]** Реалізувати aiohttp sync server в launcher (`desktop/p2p/sync_server.py`)~~ ✅
9. ~~**[Phase P2]** Реалізувати DHT service з per-artist announces (`desktop/p2p/dht_service.py`)~~ ✅
10. ~~**[Phase P2]** P2P manager + інтеграція з SyncClient (`desktop/p2p/p2p_manager.py`)~~ ✅
11. ~~**[Phase P2]** DHT в Docker backend (`backend/dht_service.py` + `main.py` lifespan)~~ ✅
    - Docker анонсує enriched артистів в DHT, порт 8800 (зовнішній HTTP)
    - Той самий `dht_service.py`, інтегрований в FastAPI lifespan
    - `docker-compose.yml`: UDP порт 19001 для DHT
12. **[Phase P2]** Тест: Docker backend + launcher знаходять один одного через DHT і синхронізуються
13. **[Phase P3]** NAT traversal (UPnP) для роботи через інтернет
14. **[Phase P3]** Cross-library search (embedding similarity між пірами)

---

## Resolved Questions

| Питання | Рішення | Обґрунтування |
|---------|---------|---------------|
| libtorrent vs kademlia | **libtorrent** | kademlia несумісна з BT DHT (різні протоколи). libtorrent дає доступ до мільйонів нод + вбудований файлообмін |
| Windows Firewall | **Inno Setup** + автопромпт | Інсталятор додає правило. Або Windows сам показує prompt при першому запуску |
| NAT traversal | **UPnP** (`miniupnpc`) | Автоматичне відкриття порту на роутері без взаємодії з користувачем |
| Embedding compatibility | **Model UUID v5** | ID моделі = uuid5(NS, model_name). Однакові моделі → однакові UUID на всіх нодах |
| Bandwidth (70MB) | **gzip + lazy loading** | Metadata стиснений ~3MB. Embeddings — on demand, не при першому з'єднанні |
| File sharing | **Phase P5** (libtorrent) | libtorrent підтримує повний BT protocol — використаємо коли буде готова платформа |

## Resolved Questions (Phase P2)

| Питання | Рішення | Обґрунтування |
|---------|---------|---------------|
| Transport protocol | **HTTP + JSON + gzip** | Той самий протокол що вже працює для sync. Без кастомного TCP/msgpack — простіше, надійніше, легше дебажити |
| DHT strategy | **Per-artist announces** | Launcher анонсує не себе, а кожного enriched артиста. Точний пошук без broadcast |
| Artist DHT announcement count | **Всіх enriched** (~2550) | 2550 announces кожні 15 хв = ~3/sec — мізер для libtorrent. Навіть 10k OK |
| Handshake | **Не потрібен для P2** | Handshake потрібен тільки для соціальних фіч (друзі, чат) — це Phase P3/P4 |
| Docker backend | **Той самий протокол** | Docker = ще один пір, та сама sync логіка, різниця тільки в обгортці (FastAPI vs aiohttp) |
| libtorrent `dht_announce` API | **3 аргументи** (sha1, port, flags=0) | libtorrent 2.0.11 Python bindings вимагають explicit flags parameter |
| libtorrent в Docker | **pip wheel** (cp311 manylinux) | Pre-built wheel 8.5MB, boost build deps не потрібні |

## Open Questions

1. **Embedding quantization**: Чи варто квантизувати 512 floats для передачі (float16, int8)? Економія bandwidth vs втрата точності?
2. **Conflict resolution**: Якщо 2 піри мають різні Last.fm теги для одного артиста — хто "правий"?
3. **PyInstaller + libtorrent**: Чи добре працює bundling C++ extension (.pyd) в .exe? Потрібно протестувати.
4. **DHT announce rate limits**: Перевірити реальні ліміти libtorrent при 2500+ announces.
