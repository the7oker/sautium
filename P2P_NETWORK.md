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
| Метадані треків | P1 | Artist, album, title, year, genre, duration |
| Audio embeddings | P3 | CLAP 512d вектори для пошуку схожості |
| Audio features | P3 | Tempo, key, energy, danceability, etc. |
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
│  ├── libtorrent DHT (peer discovery)         │
│  ├── NAT Traversal (UPnP + STUN)            │
│  ├── Peer Protocol (metadata exchange)       │
│  ├── Network Search (cross-library queries)  │
│  └── Peer Manager (connections, reputation)  │
├─────────────────────────────────────────────┤
│  UI LAYER                                    │
│  ├── Connect/Disconnect toggle               │
│  ├── Network peers list                      │
│  ├── Cross-library search                    │
│  └── Chat (peer-to-peer messaging)           │
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
| Transport | TCP + **msgpack** serialization | Для P2P протоколу поверх DHT discovery |
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

### Базовий рівень: Загальний infohash

Всі Music AI DJ ноди анонсують себе під одним infohash:
```python
MUSICAIDJ_INFOHASH = SHA1("MusicAIDJ-network-v1")
```
Будь-яка нода може знайти інших учасників мережі через `get_peers(MUSICAIDJ_INFOHASH)`.

### Розширений рівень: Анонсування по артистах (ідея)

Для targeted discovery — анонсувати infohash для кожного артиста в бібліотеці:
```python
# Нода з Pink Floyd в бібліотеці анонсує:
artist_infohash = SHA1("MusicAIDJ-artist:" + artist_uuid)
session.dht_announce(artist_infohash)
```

**Переваги:**
- Швидкий пошук "хто має Pink Floyd?" без опитування всіх пірів
- Автоматичне з'єднання з людьми зі схожими смаками
- Масштабується краще ніж broadcast до всіх пірів

**Обмеження:**
- 30k треків ≈ 3-5k унікальних артистів ≈ 3-5k announce операцій
- DHT re-announce кожні 15 хвилин — помірне навантаження
- Можна обмежити: анонсувати тільки top-100 артистів або тільки тих де є embeddings

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

### Phase P2: P2P Service + Local Testing

**Мета**: Базовий P2P сервіс з handshake, тестування на одному комп'ютері.

**Що зробити**:

1. **P2P Service** (`backend/p2p/service.py`)
   - Async TCP server на конфігурованому порті (default: 6881)
   - Message protocol: length-prefixed msgpack frames
   - Basic handshake: exchange node IDs, capabilities, library stats

2. **Peer Protocol** (`backend/p2p/protocol.py`)
   ```python
   # Handshake
   HELLO = {
       "type": "hello",
       "node_id": bytes,           # 20 bytes
       "nickname": str,
       "version": str,             # app version
       "library_stats": {
           "tracks": int,
           "artists": int,
           "albums": int,
           "top_genres": list[str],
           "has_embeddings": bool,
           "has_audio_features": bool,
       },
       "public_key": bytes,        # Ed25519 public key
       "signature": bytes,         # signs the message
   }

   # Catalog exchange
   CATALOG_REQUEST = {"type": "catalog_req", "page": int, "page_size": int}
   CATALOG_RESPONSE = {"type": "catalog_res", "tracks": [...], "total": int}

   # UUID set exchange (for sync)
   UUID_SET = {"type": "uuid_set", "track_uuids": list[str]}
   UUID_DIFF = {"type": "uuid_diff", "have": list[str], "missing": list[str]}

   # Search
   SEARCH_REQUEST = {"type": "search", "query": str, "filters": dict}
   SEARCH_RESPONSE = {"type": "search_res", "results": [...]}

   # Similarity
   SIMILAR_REQUEST = {"type": "similar", "embedding": list[float], "limit": int}
   SIMILAR_RESPONSE = {"type": "similar_res", "results": [...]}

   # Ping/Pong
   PING = {"type": "ping", "timestamp": float}
   PONG = {"type": "pong", "timestamp": float}
   ```

3. **Launcher integration**
   - Connect/Disconnect toggle в UI
   - Показати node ID та nickname
   - Список з'єднаних пірів

**Тестування**: 2-3 інстанси на localhost (різні порти, різні бази), handshake + catalog exchange.

---

### Phase P3: DHT Peer Discovery + Data Exchange

**Мета**: Ноди знаходять одне одного через BitTorrent DHT, обмінюються аналітикою.

**DHT Client** (`backend/p2p/dht.py`):
- `libtorrent` DHT session
- Bootstrap від публічних DHT нод
- Announce під infohash `SHA1("MusicAIDJ-network-v1")`
- Опціонально: announce per-artist infohashes
- Periodic re-announce (кожні 15 хвилин)
- Get peers → список IP:port Music AI DJ нод

**NAT Traversal** (`backend/p2p/nat.py`):
- UPnP port mapping через `miniupnpc` (автоматичне, без UI)
- STUN для визначення зовнішнього IP:port
- UDP hole punching для з'єднання через NAT
- Fallback: працювати через outbound-only з'єднання

**Peer Manager** (`backend/p2p/peer_manager.py`):
- Persistent список відомих пірів
- Connection pooling (max 20-50 одночасних)
- Ping/pong heartbeat
- Peer exchange (PEX) — піри діляться списками інших пірів

**Cross-Library Search:**
- Distributed query до всіх з'єднаних пірів паралельно
- Embedding-based similarity search по бібліотеках пірів
- Library comparison (overlap analysis, taste similarity)
- Timeout 5 секунд на повільних пірів

**Web UI:**
- Вкладка "Network" з списком пірів та їх library stats
- Network search bar
- "Users with similar taste" рейтинг

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
Terminal 1: Node A (port 6881, DB: musicaidj_a)
Terminal 2: Node B (port 6882, DB: musicaidj_b)
Terminal 3: Node C (port 6883, DB: musicaidj_c)
```
Кожна нода з різним набором музики (3 маленькі тестові бібліотеки).

### Docker Testing (network simulation)
```yaml
services:
  node-a:
    environment:
      P2P_PORT: 6881
      MUSIC_PATH: /music-a
    volumes:
      - ./test-library-a:/music-a:ro

  node-b:
    environment:
      P2P_PORT: 6881
      MUSIC_PATH: /music-b
    volumes:
      - ./test-library-b:/music-b:ro
```

### Integration Test Scenario
1. Node A starts → joins DHT → announces under MusicAIDJ infohash
2. Node B starts → joins DHT → finds Node A via get_peers
3. B connects to A → handshake → exchange library stats
4. B sends artist UUIDs → A returns overlap + unique catalog
5. B requests embeddings for interesting tracks → A returns vectors
6. B does similarity search using A's embeddings locally
7. A goes offline → B detects disconnect → continues working locally

---

## Immediate Next Steps (Priority Order)

1. ~~**[Phase P0]** Додати export API в backend~~ ✅ (backend вже має `/stats` endpoint)
2. ~~**[Phase P0]** Створити API client в launcher~~ ✅ (`desktop/api_client.py`)
3. ~~**[Phase P0]** Показати library stats в launcher UI~~ ✅ (stats section в launcher)
4. ~~**[Phase P1]** DB refactoring: Genre, Tag, EmbeddingModel → UUID v5~~ ✅ (міграція 002)
5. ~~**[Phase P1]** Реалізувати node identity (Ed25519 keypair)~~ ✅ (`desktop/node_identity.py`)
6. **[Phase P2]** Базовий P2P service з handshake (TCP + msgpack)
7. **[Phase P2]** Тест: 2 ноди на localhost обмінюються hello + catalog

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

## Open Questions

1. **Artist-based DHT announcement**: Скільки артистів анонсувати? Тільки top-N? Або всіх? Навантаження на DHT?
2. **Embedding quantization**: Чи варто квантизувати 512 floats для передачі (float16, int8)? Економія bandwidth vs втрата точності?
3. **Conflict resolution**: Якщо 2 піри мають різні Last.fm теги для одного артиста — хто "правий"?
4. **PyInstaller + libtorrent**: Чи добре працює bundling C++ extension (.pyd) в .exe? Потрібно протестувати.
5. **DHT announce rate**: libtorrent може мати обмеження на кількість announce операцій. Перевірити ліміти.
