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

### Invite tokens (auto-confirm) — SHIPPED 2026-07-31

Share-рядок отримує третій сегмент: `username#XXXX-XXXX-XXXX#<token-uuid>`.
Токен (`invite_tokens`) мінтить будь-яка нода зі своїми параметрами: права
(`p2p_right`: `can_message`, `can_search`), ліміт використань, expiry,
revocation, welcome-повідомлення, `require_birth_cert`. Пред'явлення живого
токена в handshake **обходить mutual-add**: токен АВТОРИЗУЄ дружбу, а
`verify_invite_code` (код↔ключ) далі ІДЕНТИФІКУЄ гостя — тож викрадений
share-рядок нікого не видає за іншого. Оскільки цей шлях обходить згоду,
гість обов'язково підписує `token_handshake:{ts}:{token}:{issuer_invite}`
(вікно ±60 с).

Кожен accept мінтить **grant**, підписаний ключем емітента:
`sautium-grant:v1:{token}:{rights}:{guest_pubkey}:{issued_at}:{expires}`.
Гість зберігає його (`friend_grants`) і пред'являє, коли емітент переїхав на
новий пристрій: таблиця friends там порожня, але детермінована ідентичність
усе ще верифікує власний старий підпис — grant заміняє втрачений рядок БД.
Права фіксуються знімком на момент accept (`friend_rights`): редагування чи
revoke токена діє лише на майбутні входи.

### Master node + relay protocol — SHIPPED 2026-07-31

Мастер-нода (Docker мейнтейнера) вшита константами в `master_node.py`
(дзеркала `desktop/p2p/` ↔ `backend/`): invite code, ПОВНИЙ pubkey (48-бітний
фінгерпринт коду сам по собі вгадуваний) і UUID публічного support-токена з
`require_birth_cert=TRUE` — атакер мусить пройти Worker-ліміт на видачу
birth-сертифікатів, щоб масово генерувати ідентичності. `_ensure_master_contact`
сіє її pending-другом при старті P2P; існуючий резолвер (LAN → кеш → DHT
`lookup_user`) робить token-handshake, зберігає grant і підтягує welcome
звичайним history-pull. Видалення контакту ставить `p2p.master_removed` —
авто-адд більше не воскресає (ручне повторне додавання коду знімає прапор).

`/api/relay/*` — свідомо **proxy-агностичний** контракт (обидві поверхні):
- `GET /api/relay/wake-stream?pubkey&ts&sig` — SSE-канал "тобі пошта".
  Нода за CGNAT тримає ОДНЕ вихідне з'єднання (вихідні працюють з-за будь-якого
  NAT) і тягне історію на кожен пінг: так відповідь мейнтейнера долітає за
  ~0.2 с замість "до наступного рестарту". Реєстр підписок = живий presence.
- `POST /api/relay/probe-connect` — relay стукає НАЗАД на адресу-джерело
  запиту (ніколи на IP із тіла — інакше це рефлектор/сканер портів) і звіряє
  `node_id` у `/health`. Так торент-трекери виводять прапорець connectable.

Недосяжна нода **глушить власні DHT-анонси** (`set_announces_enabled`):
мертва адреса в DHT засмічує лукапи всім. Лукапи та LAN-beacon працюють далі.

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

### Relay forwarding — SHIPPED 2026-08-02

Пара, де ОБИДВА за CGNAT, не мала каналу взагалі: повідомлення існувало лише
у відправника, досяжного дублера немає (у торентах цей випадок маскує
реплікація — тут маскувати нічим), тож ретрай ходив по колу вічно, і жодна
сторона цього не бачила.

Закрито **пересиланням**, не сховищем. Релей — чистий пересилач:

```
A --POST /api/relay/forward--> R
                               R --SSE {type:"deliver", envelope}--> Б
                               R <--POST /api/relay/ack------------- Б
A <--{delivered, ack}--------- R      A перевіряє підпис Б
```

Обидві дії вихідні, тож працюють з-за будь-якого NAT; релею потрібен лише
той, хто ПРИЙМАЄ — відправнику не потрібен ніхто. Конверт — байт-у-байт тіло
`/api/chat/message`, тож у отримувача це дослівний реплей через
`handle_incoming` (та сама дешифровка, той самий дедуп за `message_uuid`).

**Підтвердження — кінець-у-кінець.** Ack = підпис отримувача над
`sautium-delivery:v1:{message_uuid}:{sha256(ciphertext)}`. Релей не може ні
збрехати про доставку (немає приватного ключа), ні підсунути свій конверт
(підробка не розшифрується, тож підпису не буде). `delivered` перемикається
лише за перевіреним підписом.

**Релей не зберігає нічого** — ані рядка, ані таблиці. Єдиний його стан:
in-memory future на час очікування квитанції (`FORWARD_ACK_TIMEOUT = 10 с`) і
черга конвертів на підписку (`FORWARD_QUEUE_MAX = 100`). Немає TTL, пруна,
квот на диск, reconciliation. Адресат не підключений → 409 миттєво, а не
таймаут: відправник має одразу знати, що чекати марно.

Дзеркала: `backend/routers/peer_chat.py` і `desktop/p2p/sync_server.py` —
будь-який досяжний лаунчер стає релеєм без змін протоколу.

**Deposit/collect (поштова скринька) — СКАСОВАНО.** Причини, зафіксовані
2026-08-02: (1) мастер — це ноутбук мейнтейнера, а не інфраструктура, і
мережа, де гарантована доставка тримається на одній машині, — клієнт-сервер
із P2P-фасадом; (2) щоб ПЕРЕДАТИ повідомлення, зберігати його не треба —
скринька вирішувала дві різні задачі (транспорт і персистентність) і тягла за
собою всю машинерію заради другої; (3) у світі багатьох релеїв вона породжує
rendezvous ("де лежить моя пошта"), випадок "релей Б offline поки Б online" і
хибні сподівання на доставку.

Якщо store-and-forward колись знадобиться — робити його **не** як скриньку
адресата, а як **вихідний проксі відправника** ("мій релей дотискає мої
відправлення за мене"): свій релей нода знає завжди, чужий довелося б шукати,
тож rendezvous зникає. Але й це поки зайве: черга відправника ВЖЕ є
offline-буфером (`delivered=FALSE` + вічний ретрай), а `import_history`
домальовує втрачене з обох боків при першому контакті. Реальна діра — лише
"A і Б ніколи не бувають онлайн одночасно".
### Carry — push-seeding підписаних записів — SHIPPED 2026-08-02

Синк — **тільки pull**. Отже нода, яка не приймає вхідних, бере від мережі, але
не дає їй нічого: до неї нема кому підключитися. Її власний аналіз рідкісного
хвоста — саме те, чого нема більше ні в кого, — вмирає разом із нею.

Виправляється напрямок, не довіра. Підписаний запис самоавтентифікований, тож
роздавати його може будь-хто; носій усе одно жене його через **звичайні ворота
імпорту** (`sync_client.import_pushed` → `_verify_enrichment`), де непідписаний
рядок відкидається — умови "тільки від друга" немає й не потрібно.

```
A (за CGNAT) --POST /api/sync/offer   {artists:[…]}--> C
             <--{wanted:{artist_bios:[…], …}}--------- C
             --POST /api/sync/push/{category} ───────> C   (тіло = відповідь pull)
```

**Що виштовхується.** `get_pushable_artists`: sealed **І** NOT imported **І**
канон із не-`phantom` MB-якорем, рідкісні першими. Кожна умова несуча:
непідписане не має їхати взагалі; re-push чужого не додає мережі нічого й палить
бюджет носія; фантом — це здогадка по імені без жодного власного трека для
звірки, і схема прямо каже перевіряти її наново перед довірою по P2P. Виміряно
на еталонній бібліотеці: 21 308 артистів мають first-hand підписи, канонічних із
них 2 587 (88% відсіяно, з-поміж викинутих 18 069 — саме фантомні якорі).

**Фільтр «не слати те, що вже є»** — це offer/answer: 16 байтів на артиста, щоб
спитати, проти ~21 КБ, щоб послати наосліп. Носій відповідає лише тим, чого не
має **нічого** (свіжість — робота звичайного pull, не push).

**Бюджет** — `sync.carry_limit` (типово 2000 артистів ≈ 42 МБ), лічильник
чесний: рахуються тільки артисти, чиєї музики нода не має взагалі. Матеріал про
власних артистів — це бібліотека, а не носійство, хай як він приїхав.

**Провенанс не витирається.** `_ENRICHMENT_PRECEDENCE` вимагає, щоб локальний
рядок був `imported`, тож push НІКОЛИ не перезапише first-hand спостереження
(перевірено: push власних 25 рядків назад у ноду-автора — 21 312 first-hand
рядків до і після).

**Два кроки, без яких перенесене було б мертвим вантажем:**

1. **Inventory вміє питати по артистах.** Артист-шар виводився через
   `track_artists`, тобто носій міг відповісти лише про артистів, чию музику
   ВОЛОДІЄ САМ, — а носійство означає рівно протилежне. Тепер запит несе ще й
   `artist_uuids` (UUID v5 від імені — однакові на всіх нодах), і відповідь =
   об'єднання двох множин. Старий пір, що шле лише треки, працює як раніше.
   Виміряно на порожньому носії з 25 перенесеними артистами: 50 треків того ж
   артиста → `bios=0`; назвати артиста прямо → `bios=5`.
2. **Перенесене входить в announce-хвіст.** `sync.announce_limit` (~300) тепер
   ранжує об'єднання «власне проаналізоване» + «імпортоване», рідкісні першими.
   Автор перенесеного глушить власні анонси (він недосяжний), тож у DHT носій —
   єдина його адреса. Фантоми не входять у жодну з множин: тест — власний трек
   або імпортований запис, а здогадка по імені не дає ні того, ні того.

Дзеркала: `desktop/p2p/sync_server.py` і `backend/routers/sync.py`. Docker
монтує `./desktop:/app/desktop:ro` і використовує **ту саму**
`sync_client.import_pushed`, а не власну копію — друга копія воріт перевірки
підписів це та копія, яка з часом розійдеться й почне пропускати.

### Далі в роадмапі

Кожна фаза самоцінна й розширює ТОЙ САМИЙ `/api/relay/*` контракт. Наступний
крок — **D**, після якого мастер стає просто одним із релеїв.

- **D: Peer-relays — НАСТУПНИЙ КРОК.** Протокол пересилання вже працює;
  бракує лише того, щоб релеєм міг стати не тільки мастер. Досяжні ноди
  анонсять `Sautium-cap:relay`; клієнт
  реєструється в K=2–3 проксі, і проксі анонсить `Sautium-user:{invite}`
  клієнта від себе (BT DHT не перевіряє власника інфохеша), тож відправник
  знаходить його ЗВИЧАЙНИМ `lookup_user` — і в клієнті розширюється рівно
  один шов, `_resolve_relay_for` (зараз повертає мастера). Побічний виграш:
  анонс живе лише поки живе SSE клієнта, тож присутність у DHT стає
  справжньою, а не 15-30-хвилинним привидом. Захист від чорнодірного
  самозванця — **voucher**: клієнт підписує `{proxy_pubkey, until}`, проксі
  пред'являє підпис відправнику. Самодіагностика ролі: досяжність +
  hardware-профіль (lite — ніколи) + диск + uptime-ratio; аплінк міряється
  пасивно з реального serve-трафіку, причому пропускна пари обмежена
  найповільнішою стороною і НІКОЛИ не завищує власний аплінк → оцінка =
  високий перцентиль по трансферах до ≥N різних пірів (ratchet-up, без
  дискваліфікацій на малій вибірці). Проксі сам відписує клієнтів при
  деградації — graceful degradation замість глобальної репутації.
- **E: MB-слайси через relay ("другий ешелон").** Дамп не реплікується
  (десятки ГБ), тож єдиний варіант — live-форвардинг по відкритому
  з'єднанню; вмикається лише коли прямих `mbdump`-нод не знайдено.
