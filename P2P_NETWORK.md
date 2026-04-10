# P2P Network — Sautium

Design notes for the P2P layer. Describes **why** things are the way they are —
implementation details live in the code (`desktop/p2p/`, `backend/dht_service.py`).

---

## Vision

Перетворити Sautium з локального плеєра на **безсерверну P2P мережу**, де люди
з великими офлайновими FLAC бібліотеками можуть ділитись метаданими, audio
embeddings, features, знаходити однодумців і спілкуватись — без центрального
сервера (тільки публічні bootstrap ресурси).

**Що шариться**: metadata, CLAP embeddings, audio features, bios/tags, chat
(P4), lyrics (P4+), audio files (P5, лише легальний контент).

**Що НЕ шариться**: локальні шляхи, стан плеєра, приватні нотатки, історія
прослуховування (якщо користувач не обрав ділитись).

---

## Architecture

```
Sautium Node
├── Local layer: PostgreSQL+pgvector, FastAPI, CLAP/text embeddings, HQPlayer, Web UI
├── P2P layer:    Account identity (Argon2id+Ed25519) → aiohttp sync server
│                 libtorrent DHT (per-artist + per-user announces)
│                 E2E chat (NaCl Box) → NAT traversal (UPnP)
│                 Sync client (HTTP pull, layered LAN→DHT)
└── UI layer:     Connect/Disconnect, peers list, Friends/Chat, cross-library search
```

Node A (Kyiv) ↔ Node B (Berlin) ↔ Node C (Tokyo) — прямі UDP/TCP з'єднання
через NAT (UPnP + STUN-подібний hole punching), bootstrap через публічну
BitTorrent DHT.

---

## Technology Choices

| Component | Library | Why |
|-----------|---------|-----|
| DHT + file transfer | **libtorrent** (C++ with Python bindings) | Доступ до публічної BT DHT, вбудований файлообмін, pip-installable |
| NAT traversal | **miniupnpc** (UPnP) + STUN | UPnP для роутера, STUN для визначення зовнішнього IP |
| Transport | **HTTP + JSON + gzip** | Той самий протокол sync що вже працює; без кастомного бінарного формату |
| Identity | **cryptography** (Ed25519) + **argon2-cffi** | Стандарт, компактні 32-byte ключі; Argon2id для deterministic identity |
| Chat encryption | **PyNaCl** (NaCl Box) | Curve25519 + XSalsa20-Poly1305, проста API |
| TLS | Self-signed ECDSA P-256 | Node ID в CN, HTTPS для всіх P2P з'єднань |
| Async networking | **asyncio** + **aiohttp** | Вже використовується в проєкті |

### Чому libtorrent, а НЕ `kademlia` (pure Python) — **критичне рішення**

> Бібліотека `kademlia` (bmuller) **несумісна** з BitTorrent DHT:
> - MsgPack serialization (BT DHT використовує Bencode)
> - Різні RPC операції (STORE/FIND_VALUE vs get_peers/announce_peer)
> - Неможливо підключитись до `router.bittorrent.com`
> - Фактично створює **окрему приватну мережу** яку треба будувати з нуля

libtorrent дає доступ до мільйонів існуючих нод + вбудований файлообмін для
майбутньої Phase P5, pip-installable на Windows/Linux/Mac.

### "Безсерверність"

Безкоштовні публічні ресурси (не потрібно орендувати сервер):
- **DHT bootstrap**: `router.bittorrent.com:6881`, `dht.transmissionbt.com:6881`
- **STUN**: `stun.l.google.com:19302`, `stun.cloudflare.com:3478`
- **Relay fallback**: Oracle Cloud Free Tier або relay через інших пірів

---

## Content-Addressable IDs (Deterministic UUID v5)

Для P2P обміну однакові дані мусять мати однаковий ID на всіх нодах. Namespace
`adc1ec0b-2c81-5e26-9938-a369c6f7a5e1` (в `backend/uuid_utils.py`).

| Entity | Formula |
|--------|---------|
| Artist | `uuid5(NS, "artist:{normalize(name)}")` |
| Album | `uuid5(NS, "album:{normalize(artist)}:{normalize(title)}")` |
| Track | `uuid5(NS, "song:{normalize(artist)}:{normalize(title)}")` |
| Genre | `uuid5(NS, "genre:{normalize(name)}")` |
| Tag | `uuid5(NS, "tag:{normalize(name)}")` |
| EmbeddingModel | `uuid5(NS, "embedding_model:{normalize(name)}")` |

Embeddings ідентифікуються через `(track_uuid, model_uuid)` — обидва
детерміновані. Сам embedding PK може залишитись SERIAL (не шариться — шариться
вектор, прив'язаний до `track_uuid`).

---

## DHT Discovery Strategy

### Принцип: announce **по артистах**, не по ноді

Launcher не анонсує себе як єдину ноду. Замість цього — анонсує **кожного
артиста**, на якого має enrichment (embedding або audio_features хоча б для 1
треку):

```python
artist_infohash = SHA1("Sautium-artist:" + artist_uuid)
session.dht_announce(artist_infohash, port=sync_port, flags=0)
```

**Переваги:**
- Точний пошук: "хто має Pink Floyd?" → прямий DHT lookup
- Без broadcast/flood: шукаємо тільки потрібних артистів
- Природне масштабування: більше учасників → більше артистів
- ~2550 announces кожні 15 хв = ~3/sec — мізер для libtorrent

### Layered sync flow (P3)

```
Sync trigger
     │
     ▼
LAN Discovery (UDP broadcast на 19002 + localhost Docker probe)
     │
     ├── Peers found → direct HTTP (швидко, надійно)
     └── None → DHT lookup для одного артиста → знайти seed
                        │
                        ▼
                  Inventory call до seed про ВСІ unenriched артисти
                        │
                        ▼
                  Batch pull (gzip JSON) → import
```

**Smart seed reuse**: 1 DHT lookup + 1 inventory call замість N lookups для N
артистів.

### Account Identity + Chat Discovery

Для Phase P4 крім per-artist announces додано **per-user announces**:
```python
user_infohash = SHA1("Sautium-user:" + invite_code)
session.dht_announce(user_infohash, port=sync_port, flags=0)
```

Друг з `invite_code` робить DHT lookup → отримує IP:port → встановлює HTTPS
з'єднання для handshake/chat. Offline queue: недоставлені повідомлення
зберігаються, retry кожну хвилину через повторний DHT lookup.

---

## Account System (Phase P4)

Deterministic identity: однаковий username+password на будь-якому пристрої =
та сама ідентичність (ті ж ключі, той же invite code).

```
username + password → Argon2id KDF (256MB, 4 iter) → 32-byte seed → Ed25519 keypair
```

**Invite Code**: `username#XXXX-XXXX-XXXX` де XXXX — `SHA-256(public_key)[:6]` у
hex. Людино-читабельний, але hash частина захищає від підробки (інший "bob42"
матиме інший hash).

**Key rotation** (зміна пароля):
1. Нова пара ключів (з нового пароля)
2. Повідомлення `{new_public_key}` підписується **старим** приватним ключем
3. Розсилається друзям через `/api/chat/key-rotation`
4. Друг перевіряє підпис старим ключем → оновлює record

### Email verification (optional)

Cloudflare Worker (`sautium-verify.sautium.workers.dev`) + Resend:
- Signed requests (Ed25519) — модифікований клієнт не може підробити запит
- KV store маппить `invite_code → verified_email`
- Invite emails показують ✅ Verified / ⚠️ Unverified badge
- Auto-reciprocate: якщо обидва акаунти verified через Worker KV

### Mutual invite exchange (anti-impersonation)

Витік invite code не створює friendship автоматично — **обидва** мусять додати
invite code один одного. Handshake завершується успішно тільки коли seen_by_both.
Без цього — витік одного коду давав би fake friendship.

---

## Data Format for P2P Exchange

### Catalog entry (per track)
```json
{
  "track_uuid": "550e8400-...",
  "title": "Comfortably Numb",
  "artist_uuid": "6ba7b810-...",
  "artist_name": "Pink Floyd",
  "album_uuid": "7ca7b810-...",
  "album_title": "The Wall",
  "year": 1979,
  "genres": [{"uuid": "...", "name": "Progressive Rock"}],
  "duration_seconds": 382,
  "available_formats": [
    {"format": "FLAC", "sample_rate": 96000, "bit_depth": 24, "lossless": true}
  ]
}
```

### Embedding exchange (on demand)
```json
{
  "track_uuid": "...",
  "model_uuid": "...",
  "model_name": "laion/clap-htsat-unfused",
  "vector": [0.123, -0.456, ...]
}
```

### Bulk protocol
```
Phase 1: Catalog sync
  A → B: artist UUIDs set (compact)
  B → A: overlap report + unique artists (gzip JSON)

Phase 2 & 3: Embeddings + features (lazy, on demand, gzip)
```

**Compression**: 30k tracks metadata ≈ 15MB JSON → ~3MB gzip. Embeddings
(512 floats × 30k) ≈ 60MB → ~25MB gzip.

---

## Security Considerations

### Phase 1–3 (MVP + sync)
- Ed25519 identity (portable, deterministic)
- Connect/Disconnect kill switch (повний контроль користувача)
- Тільки metadata шариться (ніяких файлових шляхів)
- Rate limiting на вхідні запити від пірів
- Self-signed ECDSA P-256 TLS для всього P2P трафіку

### Phase P4 (Chat)
- E2E encryption NaCl Box — пароль/ключі ніколи не передаються по мережі
- Mutual invite exchange — витік invite code не дає friendship
- Email verification опціональна — Worker діє як CA, не як relay
- Friend blocklist — blocked friends не можуть надсилати повідомлення

### Future
- Selective sharing (вибір які артисти/альбоми видимі)
- Bandwidth limiting
- IP reputation (авто-бан flood/spam)

---

## Design Decisions (lessons learned)

| Decision | Rationale |
|----------|-----------|
| **libtorrent over pure-python kademlia** | kademlia несумісна з BT DHT, створила б приватну мережу з нуля |
| **HTTP+JSON+gzip over custom binary** | Той самий протокол що sync; дебажиться curl'ом |
| **Per-artist DHT announces** | Точний пошук без broadcast, природне масштабування |
| **Deterministic identity (Argon2id)** | Однаковий username+password = та сама нода на будь-якому пристрої |
| **Mutual invite exchange** | Витік invite code не дає friendship — обидві сторони мусять підтвердити |
| **Email as convenience, not trust root** | Worker доставляє і флагує verified badge, але mutual exchange все одно P2P |
| **Smart seed reuse** | 1 DHT lookup + 1 inventory call замість N lookups для N артистів |
| **Random P2P port 20000–29999** | Уникає конфліктів кількох інстансів на одній машині; зберігається в конфігу |
| **Event-driven chat delivery (SSE + direct HTTP)** | Polling коштував ~8s latency; SSE + direct push — миттєвий |
| **Persistent DB connections in long-lived services** | ChatService з per-call connection коштував 2s/повідомлення |
| **alert_mask += dht_operation_notification** | Без цього `dht_get_peers_alert` мовчки не генерується (libtorrent gotcha) |
| **libtorrent 2.1+ `peers()` compat** | Повертає `(ip, port)` tuples замість об'єктів — треба handle обидва |
| **Idempotent enrichment** | Кожен enrichment task мусить бути безпечний для re-run — це correctness, не оптимізація |

---

## Open Questions

1. **Embedding quantization**: чи варто квантизувати 512 floats для передачі
   (float16, int8)? Економія bandwidth vs втрата точності.
2. **Conflict resolution**: якщо 2 піри мають різні Last.fm теги для одного
   артиста — хто "правий"?
3. **PyInstaller + libtorrent**: чи добре працює bundling C++ extension (.pyd)
   в .exe? Треба протестувати.
4. **DHT announce rate limits**: реальні ліміти libtorrent при 2500+ announces.

---

## Future Phases

- **P3b: Cross-library search** — "хто з мережі має щось схоже на цей трек?"
  через embedding similarity. Distributed query паралельно до знайдених пірів,
  5s timeout.
- **P4b: Music recommendations** — broadcast "рекомендую альбом" до друзів,
  shared playlists (список track metadata, не файли).
- **P5: File sharing** — libtorrent BitTorrent для легального контенту
  (indie artists, Creative Commons, self-released). Opt-in, система тегів
  ліцензій (CC-BY, CC-SA, Public Domain, Self-Released).
