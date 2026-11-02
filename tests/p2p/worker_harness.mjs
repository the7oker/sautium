// Drives the REAL worker/verify.js fetch handler in Node with an in-memory KV,
// its Durable Objects on node:sqlite, a mail provider that records what it
// is sent and a throwaway authority key, so the Python side can check what
// the Worker code actually does (test_birth_cert.py, test_worker_limits.py).
//
// usage: node worker_harness.mjs <authority_seed_hex> <email_pepper> [--scenario NAME] [--armed]
//        node worker_harness.mjs <authority_seed_hex> <email_pepper> --serve [--stamp-brake N]
// Without --serve: runs one scenario (default: contract) and prints one JSON
// object on stdout; --armed runs it with the stamp budget armed, as a refusal
// (shipped shadow). With --serve: answers HTTP on 127.0.0.1 (a free port) with
// the same Worker code and env — what a node's notary is pointed at in a test
// (sign_audio.WORKER_URL) — prints {"url", "authority_pub"} as its first line
// and serves until killed. Its address brake on /timestamp lets N stamps
// through (default: no limit; the scenario's 8 would turn the ninth stamp of a
// test into a 429), and GET /__harness/calls answers how many /timestamp
// requests reached the Worker.
import { readFileSync, writeFileSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname, resolve } from "node:path";
import { pathToFileURL, fileURLToPath } from "node:url";
import { webcrypto } from "node:crypto";
import { DatabaseSync } from "node:sqlite";

const subtle = webcrypto.subtle;
const harnessArgs = process.argv.slice(2);
const [authoritySeedHex, emailPepper] = harnessArgs;
const scenarioAt = harnessArgs.indexOf("--scenario");
const scenarioName = scenarioAt >= 0 ? harnessArgs[scenarioAt + 1] : "contract";
const serveMode = harnessArgs.includes("--serve");
const brakeAt = harnessArgs.indexOf("--stamp-brake");
const stampBrake = brakeAt >= 0 ? Number(harnessArgs[brakeAt + 1]) : Infinity;
const armed = harnessArgs.includes("--armed");
const here = dirname(fileURLToPath(import.meta.url));
const workerPath = resolve(here, "../../worker/verify.js");

const hex = (u8) => Array.from(u8).map((b) => b.toString(16).padStart(2, "0")).join("");
const unhex = (h) => new Uint8Array(h.match(/../g).map((x) => parseInt(x, 16)));
const PKCS8_PREFIX = "302e020100300506032b657004220420";

async function keyFromSeed(seedHex) {
  const priv = await subtle.importKey("pkcs8", unhex(PKCS8_PREFIX + seedHex),
    { name: "Ed25519" }, true, ["sign"]);
  // Public half: derive by exporting JWK (x = raw public key, base64url).
  const jwk = await subtle.exportKey("jwk", priv);
  const pub = Buffer.from(jwk.x, "base64url");
  return { priv, pubHex: hex(pub), pubBytes: pub };
}
const newKey = () => keyFromSeed(hex(webcrypto.getRandomValues(new Uint8Array(32))));
async function sign(key, message) {
  return hex(new Uint8Array(await subtle.sign("Ed25519", key.priv, new TextEncoder().encode(message))));
}
async function sha256Hex(text) {
  return hex(new Uint8Array(await subtle.digest("SHA-256", new TextEncoder().encode(text))));
}
async function inviteOf(key, name) {
  // The invite code a client builds for this key and username.
  const d = hex(new Uint8Array(await subtle.digest("SHA-256", key.pubBytes))).slice(0, 12).toUpperCase();
  return `${name}#${d.slice(0, 4)}-${d.slice(4, 8)}-${d.slice(8, 12)}`;
}

const authority = await keyFromSeed(authoritySeedHex);

// Swap the pinned authority and master for test keys (and shrink the
// per-sender mailbox cap so the harness can hit it), then import the module.
const master = await newKey();
const src = readFileSync(workerPath, "utf-8");
let swapped = src.replace(
  /const TRUSTED_AUTHORITIES = \[\n  "[0-9a-f]{64}",\n\];/,
  `const TRUSTED_AUTHORITIES = [\n  "${authority.pubHex}",\n];`,
);
if (swapped === src) throw new Error("TRUSTED_AUTHORITIES pattern not found");
const swapped2 = swapped.replace(/const MASTER_PUBKEY = "[0-9a-f]{64}";/, `const MASTER_PUBKEY = "${master.pubHex}";`);
if (swapped2 === swapped) throw new Error("MASTER_PUBKEY pattern not found");
swapped = swapped2.replace("const MAIL_PER_SENDER_DAY = 30;", "const MAIL_PER_SENDER_DAY = 3;");
if (swapped === swapped2) throw new Error("MAIL_PER_SENDER_DAY pattern not found");
if (armed) {
  const shadow = swapped;
  swapped = shadow.replace("const STAMP_BUDGET_ARMED = false;", "const STAMP_BUDGET_ARMED = true;");
  if (swapped === shadow) throw new Error("STAMP_BUDGET_ARMED pattern not found");
}
const tmp = join(mkdtempSync(join(tmpdir(), "sautium-worker-")), "verify.mjs");
writeFileSync(tmp, swapped);
const workerModule = await import(pathToFileURL(tmp).href);
const worker = workerModule.default;

// SQL handles for the Durable Objects: each class the Worker exports runs
// in-process on node:sqlite — its SQL is exercised for real, only the storage
// handle is emulated. `trace`, when set to an array, collects every statement
// and its parameters.
function sqlHandle(db) {
  const handle = {
    trace: null,
    exec(query, ...params) {
      if (handle.trace) handle.trace.push([query, params]);
      const stmt = db.prepare(query);
      const rows = /^\s*SELECT|\bRETURNING\b/i.test(query) ? stmt.all(...params) : (stmt.run(...params), []);
      return { toArray: () => rows, one: () => rows[0] };
    },
  };
  return handle;
}
function objectOf(Cls, db, objEnv = {}) {
  let alarmAt = null;
  const storage = {
    sql: sqlHandle(db),
    async getAlarm() { return alarmAt; },
    async setAlarm(t) { alarmAt = Number(t); },
    transactionSync(fn) {
      db.exec("BEGIN");
      try {
        const result = fn();
        db.exec("COMMIT");
        return result;
      } catch (e) {
        db.exec("ROLLBACK");
        throw e;
      }
    },
  };
  let ready;
  const obj = new Cls({ storage, blockConcurrencyWhile: (fn) => { ready = fn(); } }, objEnv);
  const stub = { fetch: (url, init) => obj.fetch(url instanceof Request ? url : new Request(url, init)) };
  return { obj, stub, db, ready, alarm: () => alarmAt, namespace: { idFromName: (name) => name, get: () => stub } };
}

// KV: a Map, every read and write counted, a switch that makes calls fail the
// way the runtime's own KV errors do, and (when asked) KV's rule of one write
// a second to a key.
const store = new Map();
const kvState = { writes: 0, reads: 0, fail: null, perKeyRule: false };   // fail: null | "put" | "get"
const lastWrite = new Map();
const kv = {
  async get(k, type) {
    if (kvState.fail === "get") throw new Error("KV GET failed: 429 Too Many Requests");
    kvState.reads += 1;
    const v = store.get(k);
    if (v === undefined) return null;
    return type === "json" ? JSON.parse(v) : v;
  },
  async put(k, v) {
    if (kvState.fail === "put") throw new Error("KV PUT failed: 429 Too Many Requests");
    const now = Date.now();
    if (kvState.perKeyRule && now - (lastWrite.get(k) ?? -Infinity) < 1000) {
      throw new Error("KV PUT failed: 429 Too Many Requests");
    }
    lastWrite.set(k, now);
    await new Promise((resolve) => setImmediate(resolve));   // a write takes a turn of the loop, as on the network
    kvState.writes += 1;
    store.set(k, v);
  },
  async delete(k) {
    if (kvState.fail === "put") throw new Error("KV DELETE failed: 429 Too Many Requests");
    kvState.writes += 1;
    store.delete(k);
  },
  async list({ prefix, limit = 1000 }) {
    const all = [...store.keys()].filter((k) => k.startsWith(prefix)).sort();
    return { keys: all.slice(0, limit).map((name) => ({ name })), list_complete: all.length <= limit };
  },
};

const db = new DatabaseSync(":memory:");
const ledger = objectOf(workerModule.BirthLedger, db);
const mailDb = new DatabaseSync(":memory:");
const mailbox = objectOf(workerModule.MasterMailbox, mailDb);
const dirDb = new DatabaseSync(":memory:");
const directory = objectOf(workerModule.NodeDirectory, dirDb);
// The Limiter: one instance per name, each on its own database, as Cloudflare
// keeps one object per id.
const limiterShards = new Map();
function limiterShard(name) {
  if (!limiterShards.has(name)) {
    limiterShards.set(name, objectOf(workerModule.Limiter, new DatabaseSync(":memory:")));
  }
  return limiterShards.get(name);
}
const limiter = workerModule.Limiter ? {
  namespace: { idFromName: (name) => name, get: (name) => limiterShard(name).stub },
} : null;

// Rate Limiting bindings emulated as plain counters (the real ones count per
// location over a 60 s window; the contract is only `limit({key}) → {success}`).
const brakeCounts = new Map();
const fakeBrake = (limit) => ({
  async limit({ key }) {
    const c = (brakeCounts.get(key) || 0) + 1;
    brakeCounts.set(key, c);
    return { success: c <= limit };
  },
});

// The mail provider: every mail the Worker sends, as the provider got it.
const mails = [];
const mailState = { down: false };
globalThis.fetch = async (url, init) => {
  if (String(url) !== "https://api.resend.com/emails") throw new Error(`unexpected fetch ${url}`);
  if (mailState.down) throw new Error("connect ECONNREFUSED");
  const m = JSON.parse(init.body);
  mails.push({ to: m.to, subject: m.subject, html: m.html });
  return new Response(JSON.stringify({ id: `m${mails.length}` }), { status: 200 });
};
const codeIn = (mail) => mail.html.match(/font-family: monospace;">([A-Z0-9]{6})</)[1];

const env = {
  BIRTH_SIGNING_KEY: authoritySeedHex,
  EMAIL_PEPPER: emailPepper,
  IP_PEPPER: "test-ip-pepper",
  BRAKE_STAMP: fakeBrake(serveMode ? stampBrake : 8),
  BRAKE_BIRTH: fakeBrake(100),
  BRAKE_ROUTE: fakeBrake(serveMode ? Infinity : 1000),
  BIRTH_LEDGER: ledger.namespace,
  MAILBOX: mailbox.namespace,
  NODE_DIRECTORY: directory.namespace,
  ...(limiter ? { LIMITER: limiter.namespace } : {}),
  RATE_LIMITS: kv,
};

async function call(method, path, body, { ip = "203.0.113.7", cf, type = "application/json", raw, headers = {}, useEnv = env } = {}) {
  const init = { method, headers: { "Content-Type": type, "CF-Connecting-IP": ip, ...headers } };
  if (raw !== undefined) init.body = raw;
  else if (body !== undefined) init.body = JSON.stringify(body);
  const req = new Request(`https://worker.test${path}`, init);
  if (cf) req.cf = cf;                       // request.cf as the edge would set it
  const res = await worker.fetch(req, useEnv);
  const text = await res.text();
  let parsed;
  try { parsed = JSON.parse(text); } catch { parsed = text; }
  return { status: res.status, body: parsed, headers: Object.fromEntries(res.headers) };
}

const issue = async (key, opts) => call("POST", "/birth-certificate", {
  pubkey_hex: key.pubHex, signature: await sign(key, `birth:${key.pubHex}`),
}, opts);
const certOf = async (key) => (await call("GET", `/birth-certificate?pubkey=${key.pubHex}`)).body;

async function sendCode(key, name, email, opts) {
  const invite = await inviteOf(key, name);
  return call("POST", "/send-verification", {
    to: email, invite_code: invite, public_key_hex: key.pubHex,
    signature: await sign(key, `sendcode:${invite}:${email}`),
  }, opts);
}
async function register(key, name, email, code, opts) {
  const invite = await inviteOf(key, name);
  return call("POST", "/register-email", {
    invite_code: invite, email, public_key_hex: key.pubHex,
    signature: await sign(key, `register:${invite}:${email}`),
    code, birth_cert: await certOf(key),
  }, opts);
}
async function verifyEmail(key, name, email, opts) {
  // The whole round: a code mailed by the Worker, read from the mail, entered.
  await sendCode(key, name, email, opts);
  return register(key, name, email, codeIn(mails[mails.length - 1]), opts);
}
async function invite(key, name, to, opts = {}, message = "") {
  const code = opts.invite || await inviteOf(key, name);
  return call("POST", "/send-invite", {
    to, invite_code: code, public_key_hex: key.pubHex,
    signature: await sign(key, `invite:${code}:to:${to}`), message,
  }, opts);
}
async function accept(key, name, theirs, opts) {
  const mine = await inviteOf(key, name);
  return call("POST", "/accept-invite", {
    my_invite_code: mine, their_invite_code: theirs, public_key_hex: key.pubHex,
    signature: await sign(key, `accept:${mine}:${theirs}`),
  }, opts);
}
async function pending(key, name, opts) {
  const mine = await inviteOf(key, name);
  const q = `invite_code=${encodeURIComponent(mine)}&public_key_hex=${key.pubHex}`
    + `&signature=${await sign(key, `pending-accepts:${mine}`)}`;
  return call("GET", `/pending-accepts?${q}`, undefined, opts);
}
async function stamp(key, roots, opts, ts = Math.floor(Date.now() / 1000)) {
  return call("POST", "/timestamp", {
    ...(roots.length === 1 ? { root: roots[0] } : { roots }),
    pubkey: key.pubHex, ts,
    signature: await sign(key, `sautium-timestamp-request:v1:${ts}:${roots.join(",")}`),
  }, opts);
}
const root = (n) => n.toString(16).padStart(64, "0");

// A counter set by hand — in its route's limiter instance, or in an object
// that keeps its own (the mailbox, the directory): how close a route already
// is to its ceiling, without making the requests that brought it there.
const shardOf = (key) => limiterShard(key.slice(0, key.indexOf(":")));
function presetIn(counterDb, key, n) {
  counterDb.prepare(`INSERT INTO counters (key, n, until) VALUES (?, ?, ?)
                     ON CONFLICT(key) DO UPDATE SET n = excluded.n`).run(key, n, Date.now() + 86400e3);
}
function preset(key, n) {
  if (limiter) presetIn(shardOf(key).db, key, n);
}
function unset(key) {
  if (limiter) shardOf(key).db.prepare("DELETE FROM counters WHERE key = ?").run(key);
}
const keysIn = (counterDb, prefix) =>
  counterDb.prepare("SELECT key FROM counters WHERE key LIKE ? ORDER BY key").all(`${prefix}%`).map((r) => r.key);
const counterKeys = (prefix) => [...limiterShards.values()].flatMap((shard) => keysIn(shard.db, prefix));
const countIn = (counterDb, key) => counterDb.prepare("SELECT n FROM counters WHERE key = ?").get(key)?.n ?? 0;
const hasTable = (anyDb, name) =>
  anyDb.prepare("SELECT count(*) AS n FROM sqlite_master WHERE type = 'table' AND name = ?").get(name).n > 0;
const namesTable = () => limiter
  ? limiterShard("names").db.prepare("SELECT pubkey, name, seen FROM names").all()
  : [];

// -------------------------------------------------------------------------
// contract: what the Worker emits, checked by the Python mirrors
// -------------------------------------------------------------------------

async function contract() {
  // --- subject 1: fresh identity, then email upgrade ---
  const s1 = await newKey();
  const issue1 = await issue(s1);
  const issue1Again = await issue(s1);
  const read1 = await call("GET", `/birth-certificate?pubkey=${s1.pubHex}`);

  const inviteCode = await inviteOf(s1, "alice");
  const email = "Alice.Example+tag@GoogleMail.com";
  const register1 = await verifyEmail(s1, "alice", email);
  const read1AfterEmail = await call("GET", `/birth-certificate?pubkey=${s1.pubHex}`);

  // A second identity verifying the SAME mailbox (dots/tag/case differ) must
  // get the same email_token — that equality is the whole point of the token.
  const s3 = await newKey();
  await issue(s3);
  const register3 = await verifyEmail(s3, "bob", "aliceexample@gmail.com");
  // the mailbox owner index moved to s3; s1 is named as s3's predecessor
  const mailboxAfter3 = JSON.parse(store.get(`mailbox:${register3.body.birth_cert.email_token}`));

  // --- subject 2: legacy v1 record already in KV ---
  const s2 = await newKey();
  store.set(`born:${s2.pubHex}`, JSON.stringify({ born_at: "2026-07-05T10:11:12Z", sig: "ab".repeat(64) }));
  const legacyRead = await call("GET", `/birth-certificate?pubkey=${s2.pubHex}`);
  const legacyRecord = JSON.parse(store.get(`born:${s2.pubHex}`));

  // --- subject 6: a v2 EMAIL record from before the domain token existed ---
  const s6 = await newKey();
  store.set(`born:${s6.pubHex}`, JSON.stringify({
    v: 2, issued_at: "2026-07-06T00:00:00Z", method: "email", difficulty: 32, params_version: 1,
    email_token: "ef".repeat(32), email_class: "other", sig_v2: "cd".repeat(64) }));
  const legacyV2Read = await call("GET", `/birth-certificate?pubkey=${s6.pubHex}`);
  const legacyV2Record = JSON.parse(store.get(`born:${s6.pubHex}`));
  const inviteCode6 = await inviteOf(s6, "frank");
  const email6 = "frank@example.net";
  store.set(`verified:${inviteCode6.replace("#", ":")}`,
    JSON.stringify({ email: email6, pubkey: s6.pubHex, born_at: "2026-07-06T00:00:00Z", verified_at: "2026-07-06T00:10:00Z" }));
  const legacyV2Check = await call("GET", `/check-email?invite_code=${encodeURIComponent(inviteCode6)}&email=${encodeURIComponent(email6)}&public_key_hex=${s6.pubHex}&signature=${await sign(s6, `check-email:${inviteCode6}:${email6}`)}`);

  // --- a disposable-domain registration for the class marker ---
  const s4 = await newKey();
  await issue(s4);
  const register4 = await verifyEmail(s4, "carol", "x@mailinator.com");

  // check-email for an already-upgraded identity returns the email cert…
  const check1 = await call("GET", `/check-email?invite_code=${encodeURIComponent(inviteCode)}&email=${encodeURIComponent(email)}&public_key_hex=${s1.pubHex}&signature=${await sign(s1, `check-email:${inviteCode}:${email}`)}`);
  // …and for a pre-v2 identity (v1 born record + existing verified: record)
  // it upgrades the certificate without a second code round.
  const inviteCode2 = await inviteOf(s2, "dave");
  const email2 = "dave@example.org";
  store.set(`verified:${inviteCode2.replace("#", ":")}`,
    JSON.stringify({ email: email2, pubkey: s2.pubHex, born_at: "2026-07-05T10:11:12Z", verified_at: "2026-07-05T10:20:00Z" }));
  const check2 = await call("GET", `/check-email?invite_code=${encodeURIComponent(inviteCode2)}&email=${encodeURIComponent(email2)}&public_key_hex=${s2.pubHex}&signature=${await sign(s2, `check-email:${inviteCode2}:${email2}`)}`);
  const legacyRecordAfterCheck = JSON.parse(store.get(`born:${s2.pubHex}`));
  // a verified: record bound to ANOTHER pubkey must not upgrade anything
  const s5 = await newKey();
  const inviteCode5 = await inviteOf(s5, "eve");
  store.set(`verified:${inviteCode5.replace("#", ":")}`,
    JSON.stringify({ email: email2, pubkey: s2.pubHex, born_at: "2026-07-05T10:11:12Z", verified_at: "2026-07-05T10:20:00Z" }));
  const check5 = await call("GET", `/check-email?invite_code=${encodeURIComponent(inviteCode5)}&email=${encodeURIComponent(email2)}&public_key_hex=${s5.pubHex}&signature=${await sign(s5, `check-email:${inviteCode5}:${email2}`)}`);

  // --- birth ledger (shadow): a burst from one /24 + one ASN ---
  const burst = [];
  for (let i = 0; i < 4; i++) {
    burst.push(await issue(await newKey(), { ip: `198.51.100.${10 + i}`, cf: { asn: 64500, country: "UA" } }));
  }
  const ledgerRows = db.prepare("SELECT asn, cc, method, m_shadow, n_sub24, n_asn1, n_glob1, addr, n_addr24, sub, fam FROM births ORDER BY rowid").all();

  // s1 takes the mailbox back (a second registration by the earlier holder):
  // its certificate now names s3 as predecessor, the anchor does not move
  const retake = await verifyEmail(s1, "alice", email);
  const mailboxAfterRetake = JSON.parse(store.get(`mailbox:${register3.body.birth_cert.email_token}`));
  // …and registering the same mailbox for the same key again changes nothing
  const retakeAgain = await verifyEmail(s1, "alice", email);

  // --- master mailbox: s1 (certified) parks messages for the offline master ---
  const mailPayload = async (sender, uuid, text) => {
    const encrypted = Buffer.from(`box:${text}`).toString("base64");
    const timestamp = "2026-08-18T14:00:00Z";
    const digest = await sha256Hex(encrypted);
    return {
      to: master.pubHex, from_public_key: sender.pubHex, encrypted, timestamp, message_uuid: uuid,
      signature: await sign(sender, `mailbox:v1:${master.pubHex}:${uuid}:${timestamp}:${digest}`),
    };
  };
  const mailUuid = (n) => `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
  const mail1 = await call("POST", "/mailbox", await mailPayload(s1, mailUuid(1), "hello"));
  const mail2 = await call("POST", "/mailbox", await mailPayload(s1, mailUuid(2), "again"));
  const mailDup = await call("POST", "/mailbox", await mailPayload(s1, mailUuid(1), "hello"));
  const mail3 = await call("POST", "/mailbox", await mailPayload(s1, mailUuid(3), "third"));
  const mailCap = await call("POST", "/mailbox", await mailPayload(s1, mailUuid(4), "fourth"));   // per-sender cap 3
  const stranger = await newKey();
  const mailNoCert = await call("POST", "/mailbox", await mailPayload(stranger, mailUuid(5), "x"));
  const forged = await mailPayload(s3, mailUuid(6), "x");
  forged.signature = "00".repeat(64);
  const mailBadSig = await call("POST", "/mailbox", forged);
  const wrongTo = await mailPayload(s3, mailUuid(7), "x");
  wrongTo.to = s1.pubHex;
  const mailWrongTo = await call("POST", "/mailbox", wrongTo);
  const big = await mailPayload(s3, mailUuid(8), "x".repeat(9000));
  const mailTooBig = await call("POST", "/mailbox", big);
  const nowTs = String(Math.floor(Date.now() / 1000));
  const drainSig = await sign(master, `mailbox-drain:v1:${nowTs}:0`);
  const drain = await call("GET", `/mailbox?public_key_hex=${master.pubHex}&ts=${nowTs}&after=0&signature=${drainSig}`);
  const drainForeign = await call("GET", `/mailbox?public_key_hex=${s1.pubHex}&ts=${nowTs}&after=0&signature=${await sign(s1, `mailbox-drain:v1:${nowTs}:0`)}`);
  const staleTs = String(Math.floor(Date.now() / 1000) - 3600);
  const drainStale = await call("GET", `/mailbox?public_key_hex=${master.pubHex}&ts=${staleTs}&after=0&signature=${await sign(master, `mailbox-drain:v1:${staleTs}:0`)}`);
  const lastId = drain.body.messages[drain.body.messages.length - 1].id;
  const ack = await call("DELETE", `/mailbox?public_key_hex=${master.pubHex}&ts=${nowTs}&upto=${lastId}&signature=${await sign(master, `mailbox-ack:v1:${nowTs}:${lastId}`)}`);
  const drainAfterAck = await call("GET", `/mailbox?public_key_hex=${master.pubHex}&ts=${nowTs}&after=0&signature=${drainSig}`);
  const wakeNoUpgrade = await call("GET", `/mailbox/wake?public_key_hex=${master.pubHex}&ts=${nowTs}&signature=${await sign(master, `mailbox-wake:v1:${nowTs}`)}`);

  // --- master address hint: set by a signed wake with peer_port, replay-proof ---
  const hintEmpty = await call("GET", "/master-hint");
  const wakeSig = await sign(master, `mailbox-wake:v1:${nowTs}:8801`);
  const wakeWithPort = await call("GET", `/mailbox/wake?public_key_hex=${master.pubHex}&ts=${nowTs}&peer_port=8801&signature=${wakeSig}`, undefined, { ip: "198.51.100.77" });
  const hintSet = await call("GET", "/master-hint");
  // the same signature replayed from another address with another port: refused by
  // the signature (port differs) …
  const hintReplayPort = await call("GET", `/mailbox/wake?public_key_hex=${master.pubHex}&ts=${nowTs}&peer_port=9999&signature=${wakeSig}`, undefined, { ip: "203.0.113.99" });
  // …and replayed verbatim from another address: signature valid, but the
  // single-use nonce blocks the hint update
  const hintReplaySame = await call("GET", `/mailbox/wake?public_key_hex=${master.pubHex}&ts=${nowTs}&peer_port=8801&signature=${wakeSig}`, undefined, { ip: "203.0.113.99" });
  const hintAfterReplay = await call("GET", "/master-hint");
  const hintBadPort = await call("GET", `/mailbox/wake?public_key_hex=${master.pubHex}&ts=${nowTs}&peer_port=abc&signature=${await sign(master, `mailbox-wake:v1:${nowTs}:abc`)}`);

  // --- births over IPv6: the fam sensor + the canonical /48 bucket ---
  const v6birth = await issue(await newKey(), { ip: "2a00:db8::210e", cf: { asn: 64502, country: "DE" } });
  const v6birth2 = await issue(await newKey(), { ip: "2a00:db8:0:beef::7", cf: { asn: 64502, country: "DE" } });   // same /48, another spelling shape
  // tunneled/ULA v6 must NOT count as native v6 (2002::/16 6to4, 2001:0::/32 Teredo)
  const tun6to4 = await issue(await newKey(), { ip: "2002:5be3:b5b3::1", cf: { asn: 64503, country: "DE" } });
  const tunTeredo = await issue(await newKey(), { ip: "2001:0:5ef5:79fb:0:59d3:1234:5678", cf: { asn: 64504, country: "DE" } });
  const ledgerRowsV6 = db.prepare("SELECT sub, fam, n_sub24 FROM births WHERE fam = 6 ORDER BY rowid").all();
  const tunnelFams = db.prepare("SELECT fam FROM births WHERE asn IN (64503, 64504)").all();

  // --- a wake over IPv6 fills host6 and PRESERVES the v4 slot ---
  const ts6 = String(Math.floor(Date.now() / 1000) + 1);
  const wake6 = await call("GET", `/mailbox/wake?public_key_hex=${master.pubHex}&ts=${ts6}&peer_port=8801&signature=${await sign(master, `mailbox-wake:v1:${ts6}:8801`)}`, undefined, { ip: "2a00:db8::99" });
  const hintDual = await call("GET", "/master-hint");

  // --- capability directory (Ф16c): certified volunteers register, K random served ---
  const dirTs = Math.floor(Date.now() / 1000);
  const regPayload = async (key, port, caps, ts = dirTs) => ({
    pubkey: key.pubHex, port, capabilities: caps, ts,
    signature: await sign(key, `sautium-directory:v1:${port}:${[...caps].sort().join(",")}:${ts}`),
  });
  const reg1 = await call("POST", "/node-register", await regPayload(s1, 20246, ["sync", "mbdump"]), { ip: "198.51.100.50" });
  const reg3 = await call("POST", "/node-register", await regPayload(s3, 20300, ["sync", "relay"]), { ip: "2a00:db8::77" });
  const stranger2 = await newKey();
  const regNoCert = await call("POST", "/node-register", await regPayload(stranger2, 20000, ["sync"]));
  const regBadSig = await call("POST", "/node-register",
    { ...(await regPayload(s4, 20400, ["sync"])), signature: "00".repeat(64) });
  const masterReg = await call("POST", "/node-register", await regPayload(master, 8801, ["sync"]));
  const regReplay = await call("POST", "/node-register", await regPayload(s1, 20246, ["sync", "mbdump"]), { ip: "203.0.113.66" });
  const hintsDump = await call("GET", "/node-hints?cap=mbdump");
  const hintsRelay = await call("GET", "/node-hints?cap=relay");
  const hintsSlices = await call("GET", "/node-hints?cap=mbslices");
  const hintsBadCap = await call("GET", "/node-hints?cap=nonsense");
  // an expired entry (older than the TTL) is never served
  dirDb.prepare("INSERT INTO nodes (pubkey, host4, host6, port, caps, ts) VALUES (?, ?, NULL, ?, ?, ?)")
    .run("ee".repeat(32), "203.0.113.200", 21000, "mbdump", Date.now() - 3 * 3600 * 1000);
  const hintsAfterStale = await call("GET", "/node-hints?cap=mbdump");

  // --- timestamp notary (Ф20): identity-signed stamps, ledger-priced in shadow ---
  const tsNow = Math.floor(Date.now() / 1000);
  const rootA = "a1".repeat(32), rootB = "b2".repeat(32), rootC = "c3".repeat(32);
  const tsSigned = await stamp(s1, [rootA], { ip: "198.51.100.90" }, tsNow);
  const tsSignedAgain = await stamp(s1, [rootA], { ip: "198.51.100.90" }, tsNow);
  const tsMulti = await stamp(s1, [rootA, rootB], { ip: "198.51.100.90" }, tsNow);
  const tsUnsigned = await call("POST", "/timestamp", { root: rootC }, { ip: "198.51.100.91" });
  const tsBadSig = await call("POST", "/timestamp",
    { root: rootA, pubkey: s1.pubHex, ts: tsNow, signature: "00".repeat(64) }, { ip: "198.51.100.90" });
  const tsStale = await stamp(s1, [rootA], { ip: "198.51.100.90" }, tsNow - 3600);
  const tsBadRoot = await call("POST", "/timestamp", { root: "zz" }, { ip: "198.51.100.90" });
  const tsTooMany = await stamp(s1, Array.from({ length: 101 }, (_, i) => root(i)), { ip: "198.51.100.90" }, tsNow);
  // no certificate issued here: not stamped
  const tsStranger = await stamp(stranger2, ["d4".repeat(32)], { ip: "198.51.100.92" }, tsNow);
  // the address brake (8 per window in the harness), one IPv6 /64 across many interface ids
  const tsBrake = [];
  for (let i = 0; i < 10; i++) {
    const r = await stamp(s3, [(i + 0x10).toString(16).padStart(64, "e")], { ip: `2a00:db8:1:2::${i + 1}` }, tsNow);
    tsBrake.push({ status: r.status, retry: r.body.retry_after ?? null });
  }
  const stampRows = db.prepare("SELECT pubkey, n_new, n_roots, m, allowed, addr FROM stamps ORDER BY id").all();
  const budgetRow = db.prepare("SELECT tokens FROM notary_budget WHERE pubkey = ?").get(s1.pubHex);
  // ip_hash merges a /64: two interface ids, one pseudonym; another /64, another one
  const ipHash64a = (await stamp(s4, ["f0".repeat(32)], { ip: "2a00:db8:1:9::1" }, tsNow)).body.ip_hash;
  const ipHash64b = (await stamp(s4, ["f1".repeat(32)], { ip: "2a00:db8:1:9:aaaa:bbbb:cccc:dddd" }, tsNow)).body.ip_hash;
  const ipHash64c = (await stamp(s4, ["f2".repeat(32)], { ip: "2a00:db8:1:a::1" }, tsNow)).body.ip_hash;

  const stats = await call("GET", "/issuance-stats");
  const badSig = await call("POST", "/birth-certificate", {
    pubkey_hex: s1.pubHex, signature: "00".repeat(64),
  });

  return {
    authority_pub: authority.pubHex,
    issue1, issue1Again, read1, register: register1, read1AfterEmail,
    register3, mailboxAfter3, retake, retakeAgain, mailboxAfterRetake,
    legacyRead, legacyRecord,
    register4,
    check1, check2, legacyRecordAfterCheck, check5,
    legacyV2Read, legacyV2Record, legacyV2Check,
    burst, ledgerRows,
    stats, badSig,
    master_pub: master.pubHex,
    mail1, mail2, mailDup, mail3, mailCap, mailNoCert, mailBadSig, mailWrongTo, mailTooBig,
    drain, drainForeign, drainStale, ack, drainAfterAck, wakeNoUpgrade,
    hintEmpty, wakeWithPort, hintSet, hintReplayPort, hintReplaySame, hintAfterReplay, hintBadPort,
    v6birth, v6birth2, tun6to4, tunTeredo, tunnelFams, wake6, hintDual, ledgerRowsV6,
    reg1, reg3, regNoCert, regBadSig, masterReg, regReplay,
    hintsDump, hintsRelay, hintsSlices, hintsBadCap, hintsAfterStale,
    tsSigned, tsSignedAgain, tsMulti, tsUnsigned, tsBadSig, tsStale, tsBadRoot, tsTooMany,
    tsStranger, tsBrake, stampRows, budgetRow, ipHash64a, ipHash64b, ipHash64c,
    s1_pub: s1.pubHex, s3_pub: s3.pubHex,
    brakeStampKey: [...brakeCounts.keys()].find((k) => k.startsWith("ts:2a00:db8:1:2")) ?? null,
    verified_record: JSON.parse(store.get(`verified:${inviteCode.replace("#", ":")}`)),
  };
}

// -------------------------------------------------------------------------
// mail: who may make the Worker send mail, and how much
// -------------------------------------------------------------------------

async function mail() {
  const out = {};
  // A certified key with a verified mailbox, a certified one without, and a
  // key with no certificate at all.
  const alice = await newKey();
  await issue(alice);
  await verifyEmail(alice, "alice", "alice@example.org", { ip: "198.51.100.1" });
  const plain = await newKey();
  await issue(plain);
  const bare = await newKey();

  let sent = mails.length;
  out.invited = await invite(alice, "alice", "friend@example.net", { ip: "198.51.100.1" }, "hello there");
  out.invitedMail = mails.slice(sent);
  sent = mails.length;
  out.unverified = await invite(plain, "pat", "friend@example.net", { ip: "198.51.100.2" });
  out.uncertified = await invite(bare, "bart", "friend@example.net", { ip: "198.51.100.3" });
  out.mailsAfterRefusals = mails.length - sent;

  // the free text: capped, plain
  out.messageAtCap = await invite(alice, "alice", "cap@example.net", { ip: "198.51.100.1" }, "x".repeat(500));
  out.messageOverCap = await invite(alice, "alice", "cap2@example.net", { ip: "198.51.100.1" }, "x".repeat(501));
  out.messageNotText = await invite(alice, "alice", "cap3@example.net", { ip: "198.51.100.1" }, { html: "<b>" });
  out.messageOfWideCharacters = await invite(alice, "alice", "cap4@example.net", { ip: "198.51.100.1" }, "\u{1F3B5}".repeat(300));

  // the code must be the key's own, spelled as its client builds it, under
  // the one name the key speaks under
  const aliceCode = await inviteOf(alice, "alice");
  out.lowercaseCode = await invite(alice, "alice", "a1@example.net",
    { ip: "198.51.100.1", invite: aliceCode.replace(/#.*/, (m) => m.toLowerCase()) });
  out.otherName = await invite(alice, "mallory", "a2@example.net", { ip: "198.51.100.1" });
  out.otherNameCode = await sendCode(alice, "mallory", "a3@example.net", { ip: "198.51.100.1" });
  const fresh = await newKey();
  await sendCode(fresh, "first", "b@example.org", { ip: "198.51.100.4" });
  out.boundByFirstUse = await sendCode(fresh, "second", "b2@example.org", { ip: "198.51.100.4" });
  out.firstNameStill = await sendCode(fresh, "first", "b3@example.org", { ip: "198.51.100.4" });
  // a read fixes no name: check-email under any name, then a write under another
  const reader = await newKey();
  out.readUnderAName = await call("GET", `/check-email?invite_code=${encodeURIComponent(await inviteOf(reader, "rita"))}&email=r%40example.org&public_key_hex=${reader.pubHex}&signature=${await sign(reader, `check-email:${await inviteOf(reader, "rita")}:r@example.org`)}`);
  out.namesAfterRead = namesTable().filter((r) => r.pubkey === reader.pubHex).length;
  out.writeUnderAnother = await sendCode(reader, "rosa", "r@example.org", { ip: "198.51.100.4" });

  // the recipient: one plain address, counted by its normalised mailbox
  out.listSyntax = await invite(alice, "alice", "a@example.net,b@example.net", { ip: "198.51.100.1" });
  out.displayName = await invite(alice, "alice", "<bob@example.net>", { ip: "198.51.100.1" });
  const sam = await newKey();
  await issue(sam);
  await verifyEmail(sam, "sam", "sam@example.org", { ip: "198.51.100.9" });
  out.sameMailbox = [];
  for (const to of ["Bob.Smith+x@GoogleMail.com", "bobsmith@gmail.com", "BOBSMITH+y@gmail.com", "b.o.b.smith@gmail.com"]) {
    out.sameMailbox.push((await invite(sam, "sam", to, { ip: "198.51.100.9" })).status);
  }

  // per key: five invites a day, from any address
  const many = await newKey();
  await issue(many);
  await verifyEmail(many, "many", "many@example.org", { ip: "198.51.100.5" });
  out.perKey = [];
  for (let i = 0; i < 6; i++) {
    out.perKey.push((await invite(many, "many", `p${i}@example.net`, { ip: `198.51.100.${60 + i}` })).status);
  }

  // per sender mailbox: two keys verified on one mailbox share its five
  const k1 = await newKey(), k2 = await newKey();
  await issue(k1); await issue(k2);
  await verifyEmail(k1, "kim", "shared@example.org", { ip: "198.51.100.6" });
  await verifyEmail(k2, "kai", "shared@example.org", { ip: "198.51.100.7" });
  out.perMailbox = [];
  for (let i = 0; i < 3; i++) out.perMailbox.push((await invite(k1, "kim", `m${i}@example.net`, { ip: "198.51.100.6" })).status);
  for (let i = 3; i < 6; i++) out.perMailbox.push((await invite(k2, "kai", `m${i}@example.net`, { ip: "198.51.100.7" })).status);

  // concurrent requests cannot pass a counter together: twelve invites to one
  // mailbox at once, three may go
  const crowd = await newKey();
  await issue(crowd);
  await verifyEmail(crowd, "crowd", "crowd@example.org", { ip: "198.51.100.8" });
  sent = mails.length;
  const burst = await Promise.all(Array.from({ length: 12 }, () =>
    invite(crowd, "crowd", "target@example.net", { ip: "198.51.100.8" })));
  out.concurrentStatuses = burst.map((r) => r.status).sort();
  out.concurrentMails = mails.length - sent;

  // the route's ceiling — the day's 24th invite goes, the 25th waits for
  // 00:00 UTC — and a Retry-After that spreads the waiting nodes
  const cyra = await newKey();
  await issue(cyra);
  await verifyEmail(cyra, "cyra", "cyra@example.org", { ip: "198.51.100.11" });
  preset("invite:all:", 23);
  out.ceiling = [(await invite(cyra, "cyra", "c1@example.net", { ip: "198.51.100.11" })).status,
                 (await invite(cyra, "cyra", "c2@example.net", { ip: "198.51.100.11" })).status];
  out.ceilingRetries = [];
  out.retriesFrom = Date.now();
  for (let i = 0; i < 6; i++) {
    out.ceilingRetries.push((await invite(cyra, "cyra", `c${3 + i}@example.net`, { ip: "198.51.100.11" })).body.retry_after);
  }
  out.retriesTo = Date.now();
  if (limiter) {
    out.ceilingUntil = Number(limiterShard("invite").db
      .prepare("SELECT until FROM counters WHERE key = 'invite:all:'").get().until);
  }

  // one limiter per route: codes to a mailbox spend nothing of its invites
  const coder = await newKey();
  await issue(coder);
  for (let i = 0; i < 10; i++) await sendCode(coder, "coder", "shared-route@example.net", { ip: `198.51.100.${100 + i}` });
  unset("invite:all:");
  out.inviteAfterCodes = await invite(cyra, "cyra", "shared-route@example.net", { ip: "198.51.100.11" });

  // a refusal writes nothing to KV
  const writes = kvState.writes;
  out.refusedCode = await sendCode(coder, "coder", "shared-route@example.net", { ip: "198.51.100.120" });
  out.refusedInvite = await invite(plain, "pat", "z@example.net", { ip: "198.51.100.2" });
  out.writesByRefusals = kvState.writes - writes;
  const held = counterKeys("");
  const names = namesTable().map((r) => r.name);
  out.counterKeysHoldNoAddress = held.length > 0 && names.length > 0
    && held.every((k) => !k.includes("@") && !k.includes("198.51.100"))
    && names.every((n) => /^[0-9a-f]{64}$/.test(n));

  // the provider unreachable: a 502, never an error's text
  mailState.down = true;
  out.providerDown = await invite(cyra, "cyra", "down@example.net", { ip: "198.51.100.11" });
  mailState.down = false;
  return out;
}

// -------------------------------------------------------------------------
// codes: the email code's tries
// -------------------------------------------------------------------------

async function codes() {
  const out = {};
  const k = await newKey();
  await issue(k);
  const t0 = Date.now();
  await sendCode(k, "tess", "tess@example.org", { ip: "198.51.100.30" });
  const t1 = Date.now();
  // the day's codes end at 00:00 UTC, the mail provider's day; a person's
  // own count runs 24 h from its first code
  if (limiter) {
    const until = (key) => Number(limiterShard("code").db
      .prepare("SELECT until FROM counters WHERE key = ?").get(key).until);
    out.window = { t0, t1, ceiling: until("code:all:"), person: until(`code:key:${k.pubHex}`) };
  }
  const right = codeIn(mails[mails.length - 1]);
  const wrong = right === "AAAAAA" ? "BBBBBB" : "AAAAAA";
  out.wrong = [];
  for (let i = 0; i < 5; i++) out.wrong.push((await register(k, "tess", "tess@example.org", wrong, { ip: "198.51.100.30" })).status);
  out.rightAfterFive = await register(k, "tess", "tess@example.org", right, { ip: "198.51.100.30" });
  await sendCode(k, "tess", "tess@example.org", { ip: "198.51.100.30" });
  out.newCode = await register(k, "tess", "tess@example.org", codeIn(mails[mails.length - 1]), { ip: "198.51.100.30" });
  // four wrong, then the right one: still good
  const k2 = await newKey();
  await issue(k2);
  await sendCode(k2, "toni", "toni@example.org", { ip: "198.51.100.31" });
  const right2 = codeIn(mails[mails.length - 1]);
  for (let i = 0; i < 4; i++) await register(k2, "toni", "toni@example.org", wrong === right2 ? "CCCCCC" : wrong, { ip: "198.51.100.31" });
  out.rightAfterFour = await register(k2, "toni", "toni@example.org", right2, { ip: "198.51.100.31" });
  // a code for one mailbox proves nothing for another
  const k3 = await newKey();
  await issue(k3);
  await sendCode(k3, "tara", "tara@example.org", { ip: "198.51.100.32" });
  out.otherMailbox = await register(k3, "tara", "someone-else@example.org", codeIn(mails[mails.length - 1]), { ip: "198.51.100.32" });
  // an expired code (it holds the address) is removed by the code instance's alarm
  if (limiter) {
    const codeShard = limiterShard("code");
    out.alarmSet = codeShard.alarm() !== null;
    codeShard.db.prepare("INSERT INTO codes (invite, email, hash, tries, expires) VALUES (?, ?, ?, 0, ?)")
      .run("old#AAAA-BBBB-CCCC", "old@example.org", "00", Date.now() - 1000);
    await codeShard.obj.alarm();
    out.expiredLeft = codeShard.db.prepare("SELECT count(*) AS n FROM codes WHERE invite = 'old#AAAA-BBBB-CCCC'").get().n;
  }
  // five codes a day to one mailbox, however many keys ask, however spelled
  out.codesToOneMailbox = [];
  for (let i = 0; i < 6; i++) {
    out.codesToOneMailbox.push((await sendCode(await newKey(), "tom", i % 2 ? "tom@example.org" : "Tom+x@example.org",
                                               { ip: `198.51.100.${40 + i}` })).status);
  }
  // the day's 60th code goes, the 61st waits for 00:00 UTC
  preset("code:all:", 59);
  out.codeCeiling = [];
  for (let i = 0; i < 2; i++) {
    out.codeCeiling.push((await sendCode(await newKey(), "cleo", `cleo${i}@example.org`,
                                         { ip: `198.51.100.${70 + i}` })).status);
  }
  return out;
}

// -------------------------------------------------------------------------
// accepts: the acceptance and its pickup
// -------------------------------------------------------------------------

async function accepts() {
  const out = {};
  const masha = await newKey(), sasha = await newKey();
  await issue(masha); await issue(sasha);
  await verifyEmail(masha, "masha", "masha@example.org", { ip: "198.51.100.50" });
  const mashaCode = await inviteOf(masha, "masha");
  const typed = mashaCode.toLowerCase().replace(/-/g, "");            // as a person might type it
  out.invalidTheirs = await accept(sasha, "sasha", "nonsense", { ip: "198.51.100.51" });
  out.ownCode = await accept(sasha, "sasha", await inviteOf(sasha, "sasha"), { ip: "198.51.100.51" });
  const sent = mails.length;
  out.accepted = await accept(sasha, "sasha", typed, { ip: "198.51.100.51" });
  out.notifiedTo = mails.slice(sent).map((m) => m.to);
  out.pickup = await pending(masha, "masha", { ip: "198.51.100.50" });
  out.pickupAgain = await pending(masha, "masha", { ip: "198.51.100.50" });
  out.pickupUnderAnotherName = await pending(masha, "mallory", { ip: "198.51.100.50" });
  // the acceptance's caps come before its write; the mail's are its own
  preset(`accept:key:${sasha.pubHex}`, 20);
  let writes = kvState.writes;
  out.acceptAtKeyCap = await accept(sasha, "sasha", typed, { ip: "198.51.100.51" });
  out.writesByAcceptRefusal = kvState.writes - writes;
  const pasha = await newKey();
  const notifyTo = counterKeys("notify:to:")[0];
  if (notifyTo) preset(notifyTo, 3);
  const sent2 = mails.length;
  out.acceptAtNotifyCap = await accept(pasha, "pasha", typed, { ip: "198.51.100.52" });
  out.mailsAtNotifyCap = mails.length - sent2;
  // the acceptance mails one address may set off in a day
  if (notifyTo) unset(notifyTo);
  const notifyAddrsBefore = new Set(counterKeys("notify:addr:"));
  await accept(await newKey(), "kasia", typed, { ip: "198.51.100.53" });
  const notifyAddr = counterKeys("notify:addr:").find((key) => !notifyAddrsBefore.has(key));
  if (notifyAddr) preset(notifyAddr, 3);
  const sent3 = mails.length;
  out.acceptAtNotifyAddrCap = await accept(await newKey(), "lena", typed, { ip: "198.51.100.53" });
  out.mailsAtNotifyAddrCap = mails.length - sent3;
  out.acceptFromOtherAddr = await accept(await newKey(), "olha", typed, { ip: "198.51.100.54" });
  // a pickup takes a page at a time (KV: a thousand operations a call)
  const mashaKv = (await inviteOf(masha, "masha")).replace("#", ":");
  await pending(masha, "masha", { ip: "198.51.100.50" });            // what the steps above left
  for (let i = 0; i < 400; i++) {
    store.set(`accepted:${mashaKv}:x${String(i).padStart(3, "0")}:AAAA-BBBB-CCCC`,
              JSON.stringify({ invite_code: `x${i}#AAAA-BBBB-CCCC`, accepted_at: "2026-11-02T00:00:00Z" }));
  }
  out.pages = [(await pending(masha, "masha", { ip: "198.51.100.50" })).body.accepts.length,
               (await pending(masha, "masha", { ip: "198.51.100.50" })).body.accepts.length];
  preset(`pending:key:${masha.pubHex}`, 100);
  writes = kvState.writes;
  out.pickupAtCap = await pending(masha, "masha", { ip: "198.51.100.50" });
  out.writesByPickupRefusal = kvState.writes - writes;
  // the day's 12th acceptance mail goes, the 13th waits for 00:00 UTC; the
  // acceptance is kept either way
  const vera = await newKey();
  await issue(vera);
  await verifyEmail(vera, "vera", "vera@example.org", { ip: "198.51.100.55" });
  const veraCode = await inviteOf(vera, "vera");
  preset("notify:all:", 11);
  out.notifyCeiling = [];
  for (let i = 0; i < 2; i++) {
    const r = await accept(await newKey(), `fan${i}`, veraCode, { ip: `198.51.100.${56 + i}` });
    out.notifyCeiling.push([r.status, r.body.notified]);
  }
  return out;
}

// -------------------------------------------------------------------------
// stamps: who writes a ts: key, and how often
// -------------------------------------------------------------------------

async function stamps() {
  const out = {};
  const k = await newKey();
  await issue(k);
  const uncertified = await newKey();
  out.unsigned = await call("POST", "/timestamp", { root: root(1) });
  out.uncertified = await stamp(uncertified, [root(2)]);
  out.certified = await stamp(k, [root(3)]);
  out.certifiedAgain = await stamp(k, [root(3)]);
  // the key's daily cap on NEW roots; a known root stays free
  preset(`stamp:key:${k.pubHex}`, 499);
  const writes = kvState.writes;
  const ledgerOf = (key) => JSON.stringify([
    db.prepare("SELECT count(*) AS n FROM stamps WHERE pubkey = ?").get(key.pubHex).n,
    db.prepare("SELECT tokens FROM notary_budget WHERE pubkey = ?").get(key.pubHex)?.tokens ?? null]);
  const ledgerBefore = ledgerOf(k);
  out.keyCapTwo = await stamp(k, [root(4), root(5)]);
  out.writesByRefusal = kvState.writes - writes;
  out.ledgerUntouchedByRefusal = ledgerOf(k) === ledgerBefore;
  out.duplicateRoots = await stamp(k, [root(7), root(7)]);
  out.keyCapOne = await stamp(k, [root(4)]);
  out.keyCapFull = await stamp(k, [root(6)]);
  out.knownRootAtCap = await stamp(k, [root(3)]);
  // the address's daily cap, another key from the same address
  const k2 = await newKey();
  await issue(k2, { ip: "198.51.100.60" });
  await stamp(k2, [root(10)], { ip: "198.51.100.60" });
  const addrKey = counterKeys("stamp:addr:").find((key) => key.startsWith("stamp:addr:4."));
  out.addrKeyShape = addrKey ? addrKey.slice(0, 13) : null;
  if (addrKey) preset(addrKey, 1000);
  const k3 = await newKey();
  await issue(k3, { ip: "198.51.100.61" });
  out.addrCapFull = await stamp(k3, [root(11)], { ip: "198.51.100.60" });
  out.otherAddr = await stamp(k3, [root(11)], { ip: "198.51.100.61" });
  // the route's ceiling
  preset("stamp:all:", 49999);
  out.ceiling = [(await stamp(k3, [root(12)], { ip: "198.51.100.61" })).status,
                 (await stamp(k3, [root(13)], { ip: "198.51.100.61" })).status];
  out.knownAtCeiling = await stamp(k3, [root(12)], { ip: "198.51.100.61" });
  // the lookups (KV reads) are counted before any is made
  const rows = () => db.prepare("SELECT count(*) AS n FROM stamps").get().n;
  let before = rows();
  out.knownRootAgain = await stamp(k3, [root(12)], { ip: "198.51.100.61" });
  out.ledgerRowsByKnownRoot = rows() - before;
  unset("stamp:all:");
  preset(`lookup:key:${k3.pubHex}`, 2000 - 99);
  const reads = kvState.reads;
  out.lookupsOverKeyCap = await stamp(k3, Array.from({ length: 100 }, (_, i) => root(1000 + i)), { ip: "198.51.100.61" });
  out.readsByLookupRefusal = kvState.reads - reads;
  out.lookupsUnderKeyCap = await stamp(k3, Array.from({ length: 99 }, (_, i) => root(1000 + i)), { ip: "198.51.100.61" });
  // a root re-signed from an older format is a write, counted like a new root
  const k4 = await newKey();
  await issue(k4, { ip: "198.51.100.62" });
  for (const n of [5000, 5001]) {
    store.set(`ts:${root(n)}`, JSON.stringify({ v: 1, date: "2026-01-01T00:00:00Z", ip_hash: "x", sig: "y" }));
  }
  preset(`stamp:key:${k4.pubHex}`, 499);
  out.resignOverKeyCap = await stamp(k4, [root(5000), root(5001)], { ip: "198.51.100.62" });
  out.resignAtKeyCap = await stamp(k4, [root(5000)], { ip: "198.51.100.62" });
  out.resignedRecord = JSON.parse(store.get(`ts:${root(5000)}`));
  // … and on its address's count
  const k5 = await newKey();
  await issue(k5, { ip: "198.51.100.63" });
  const addrKeysBefore = new Set(counterKeys("stamp:addr:"));
  await stamp(k5, [root(5100)], { ip: "198.51.100.63" });
  const addrKey63 = counterKeys("stamp:addr:").find((key) => !addrKeysBefore.has(key));
  for (const n of [5101, 5102]) {
    store.set(`ts:${root(n)}`, JSON.stringify({ v: 1, date: "2026-01-01T00:00:00Z", ip_hash: "x", sig: "y" }));
  }
  if (addrKey63) preset(addrKey63, 999);
  out.resignOverAddrCap = await stamp(k5, [root(5101), root(5102)], { ip: "198.51.100.63" });
  out.resignAtAddrCap = await stamp(k5, [root(5101)], { ip: "198.51.100.63" });
  return out;
}

// -------------------------------------------------------------------------
// store: what a failing store and a malformed request get
// -------------------------------------------------------------------------

async function storeFaults() {
  const out = {};
  const k = await newKey();
  kvState.fail = "put";
  out.birthKvDown = await issue(k);
  kvState.fail = null;
  out.ledgerRowsAfterFailedBirth = db.prepare("SELECT count(*) AS n FROM births WHERE pubkey = ?").get(k.pubHex).n;
  out.birthAfter = await issue(k);
  out.ledgerRowsAfterBirth = db.prepare("SELECT count(*) AS n FROM births WHERE pubkey = ?").get(k.pubHex).n;
  kvState.fail = "get";
  out.readKvDown = await call("GET", `/birth-certificate?pubkey=${k.pubHex}`);
  kvState.fail = null;
  // an object that fails: the same 503
  const brokenLimiter = { idFromName: (n) => n, get: () => ({ fetch: async () => { throw new Error("Durable Object reset because its code was updated"); } }) };
  out.limiterDown = await issue(await newKey(), { useEnv: { ...env, LIMITER: brokenLimiter } });
  // any other failure: a 500 that carries no error text
  const { IP_PEPPER, ...noPepper } = env;
  out.internal = await stamp(k, [root(1)], { useEnv: noPepper });
  // the ceiling refuses before any write
  preset("birth:all:", 20000);
  const writes = kvState.writes;
  const k2 = await newKey();
  out.birthAtCeiling = await issue(k2, { ip: "198.51.100.70" });
  out.writesByRefusal = kvState.writes - writes;
  out.ledgerRowsAfterRefusal = db.prepare("SELECT count(*) AS n FROM births WHERE pubkey = ?").get(k2.pubHex).n;
  unset("birth:all:");
  // births per address and day
  out.perAddress = [];
  preset(counterKeys("birth:addr:")[0] || "none", 49);
  out.perAddress.push((await issue(await newKey())).status, (await issue(await newKey())).status);
  // the request itself
  const k3 = await newKey();
  const body = { pubkey_hex: k3.pubHex, signature: await sign(k3, `birth:${k3.pubHex}`) };
  out.textPlain = await call("POST", "/birth-certificate", undefined, { type: "text/plain", raw: JSON.stringify(body) });
  out.formPost = await call("POST", "/birth-certificate", undefined, { type: "application/x-www-form-urlencoded", raw: "a=1" });
  out.badJson = await call("POST", "/birth-certificate", undefined, { raw: "{not json" });
  out.notObject = await call("POST", "/birth-certificate", undefined, { raw: "[1,2]" });
  out.tooLarge = await call("POST", "/birth-certificate", undefined, { raw: JSON.stringify({ ...body, pad: "x".repeat(17000) }) });
  out.charsetJson = await call("POST", "/birth-certificate", body,
                              { type: "application/json; charset=utf-8", ip: "198.51.100.71" });
  out.preflight = await call("OPTIONS", "/birth-certificate", undefined, { headers: { Origin: "https://example.com" } });
  out.anyAnswerCors = Object.keys(out.charsetJson.headers).filter((h) => h.startsWith("access-control"));
  // signatures: one spelling each
  const k4 = await newKey();
  const sig4 = await sign(k4, `birth:${k4.pubHex}`);
  out.sigOddLength = await call("POST", "/birth-certificate", { pubkey_hex: k4.pubHex, signature: sig4 + "0" },
                                { ip: "198.51.100.72" });
  out.sigUpper = await call("POST", "/birth-certificate", { pubkey_hex: k4.pubHex, signature: sig4.toUpperCase() },
                            { ip: "198.51.100.72" });
  return out;
}

// -------------------------------------------------------------------------
// directory: entries per network, the total, the registration limit
// -------------------------------------------------------------------------

async function directoryLimits() {
  const out = {};
  const ts = () => Math.floor(Date.now() / 1000);
  const reg = async (key, ip, port = 20000, t = ts()) => call("POST", "/node-register", {
    pubkey: key.pubHex, port, capabilities: ["sync"], ts: t,
    signature: await sign(key, `sautium-directory:v1:${port}:sync:${t}`),
  }, { ip });
  const keys = [];
  for (let i = 0; i < 18; i++) {
    const key = await newKey();
    await issue(key, { ip: `198.51.100.${200 + i}` });
    keys.push(key);
  }
  // seventeen volunteers in one IPv4 /24: sixteen are listed
  out.v4 = [];
  for (let i = 0; i < 17; i++) out.v4.push((await reg(keys[i], `192.0.2.${i + 1}`)).status);
  out.v4Again = await reg(keys[0], "192.0.2.1", 20000, ts() + 1);     // a listed one renews
  out.v4Elsewhere = await reg(keys[16], "198.51.100.199", 20000, ts() + 2);   // a refused signature is spent
  out.total = (await call("GET", "/node-hints?cap=sync")).body.total;
  // IPv6: one /48 across its /64s
  const v6keys = [];
  for (let i = 0; i < 17; i++) {
    const key = await newKey();
    await issue(key, { ip: `203.0.113.${100 + i}` });
    v6keys.push(key);
  }
  out.v6 = [];
  for (let i = 0; i < 17; i++) out.v6.push((await reg(v6keys[i], `2001:db8:77:${(i + 1).toString(16)}::1`)).status);
  // the one refused there spends none of its network's day
  const dayCounts = (prefix) => [
    ...(hasTable(dirDb, "counters") ? keysIn(dirDb, prefix).map((key) => countIn(dirDb, key)) : []),
    ...counterKeys(prefix).map((key) => countIn(shardOf(key).db, key))];
  out.v6DayCount = dayCounts("directory:addr:6.");
  // the registration limit is one /64's, whatever its interface ids
  const one = keys[17];
  out.sameSlash64 = [];
  for (let i = 0; i < 13; i++) out.sameSlash64.push((await reg(one, `2001:db8:99:1::${(i + 1).toString(16)}`, 20000 + i)).status);
  const writes = kvState.writes;
  const nodes = () => dirDb.prepare("SELECT count(*) AS n, max(ts) AS t FROM nodes").get();
  const rowsBefore = nodes();
  await reg(one, "2001:db8:99:1::ff", 20100);
  out.writesByRefusal = kvState.writes - writes;
  out.rowsUnchangedByRefusal = JSON.stringify(nodes()) === JSON.stringify(rowsBefore);
  const crowdBefore = nodes();
  out.crowdRefusal = (await reg(keys[16], "192.0.2.200", 20000, ts() + 3)).status;     // its /24 holds sixteen
  out.rowsUnchangedByCrowdRefusal = JSON.stringify(nodes()) === JSON.stringify(crowdBefore);
  // a day's registrations, per address and in all
  const k5 = await newKey();
  await issue(k5, { ip: "198.51.100.230" });
  const dayKeysBefore = new Set(keysIn(dirDb, "directory:addr:"));
  out.firstOfDay = (await reg(k5, "203.0.113.230")).status;
  const dayKey = keysIn(dirDb, "directory:addr:").find((key) => !dayKeysBefore.has(key));
  if (dayKey) presetIn(dirDb, dayKey, 400);
  const dayRowsBefore = nodes();
  out.addrDayCap = (await reg(k5, "203.0.113.230", 20000, ts() + 4)).status;
  out.rowsUnchangedByDayCap = JSON.stringify(nodes()) === JSON.stringify(dayRowsBefore);
  presetIn(dirDb, "directory:all:", 50000);
  const ceiling = await reg(k5, "203.0.113.231", 20000, ts() + 5);
  out.dayCeiling = ceiling.status;
  out.dayCeilingRetryAfter = ceiling.headers["retry-after"] ?? null;
  dirDb.prepare("DELETE FROM counters WHERE key = 'directory:all:'").run();
  // a directory kept before the network columns existed: its rows get them
  const oldDb = new DatabaseSync(":memory:");
  oldDb.exec(`CREATE TABLE nodes (pubkey TEXT PRIMARY KEY, host4 TEXT, host6 TEXT, port INTEGER NOT NULL,
              caps TEXT NOT NULL, ts INTEGER NOT NULL)`);
  oldDb.prepare("INSERT INTO nodes VALUES (?, ?, ?, ?, ?, ?)").run("aa".repeat(32), "192.0.2.9", null, 20000, "sync", Date.now());
  oldDb.prepare("INSERT INTO nodes VALUES (?, ?, ?, ?, ?, ?)").run("bb".repeat(32), null, "2001:db8:5:6::1", 20000, "sync", Date.now());
  objectOf(workerModule.NodeDirectory, oldDb);
  out.backfilled = oldDb.prepare("SELECT pubkey, net4, net6 FROM nodes ORDER BY pubkey").all();
  return out;
}

// -------------------------------------------------------------------------
// ledger: births in one hour, spread over networks
// -------------------------------------------------------------------------

async function ledgerWave() {
  for (let i = 0; i < 61; i++) {
    await issue(await newKey(), { ip: `2001:db8:${(i + 1).toString(16)}::1`, cf: { asn: 65000 + i, country: "UA" } });
  }
  const rows = db.prepare("SELECT n_glob1, m_shadow FROM births ORDER BY rowid").all();
  return { last: rows[rows.length - 1], count: rows.length };
}

// -------------------------------------------------------------------------
// burst: births at the same moment, under KV's one write a second to a key
// -------------------------------------------------------------------------

async function birthBurst() {
  kvState.perKeyRule = true;
  const keys = await Promise.all(Array.from({ length: 6 }, newKey));
  const answers = await Promise.all(keys.map((k, i) => issue(k, { ip: `198.51.100.${150 + i}` })));
  return {
    statuses: answers.map((a) => a.status),
    ledgerRows: db.prepare("SELECT count(*) AS n FROM births").get().n,
    dailyBirths: db.prepare("SELECT coalesce(sum(n), 0) AS n FROM daily WHERE field = 'births'").get()?.n ?? null,
  };
}

// -------------------------------------------------------------------------
// paused: the owner's brake
// -------------------------------------------------------------------------

async function pausedWrites() {
  const out = {};
  const k = await newKey();
  await issue(k);                                       // before the brake
  const legacy = await newKey();
  store.set(`born:${legacy.pubHex}`, JSON.stringify({ born_at: "2026-07-05T10:11:12Z", sig: "ab".repeat(64) }));
  const paused = { ...env, WRITES_PAUSED: "1" };
  const writes = kvState.writes;
  const counted = JSON.stringify(counterKeys("").map((key) => [key, shardOf(key).db
    .prepare("SELECT n FROM counters WHERE key = ?").get(key).n]));
  out.birth = await issue(await newKey(), { useEnv: paused });
  out.stamp = await stamp(k, [root(1)], { useEnv: paused });
  out.code = await sendCode(k, "kim", "kim@example.org", { useEnv: paused });
  out.pickup = await pending(k, "kim", { useEnv: paused });
  out.read = await call("GET", `/birth-certificate?pubkey=${k.pubHex}`, undefined, { useEnv: paused });
  out.stats = await call("GET", "/issuance-stats", undefined, { useEnv: paused });
  out.legacyRead = await call("GET", `/birth-certificate?pubkey=${legacy.pubHex}`, undefined, { useEnv: paused });
  out.writesWhilePaused = kvState.writes - writes;
  out.limiterUntouched = counted === JSON.stringify(counterKeys("").map((key) => [key, shardOf(key).db
    .prepare("SELECT n FROM counters WHERE key = ?").get(key).n]));
  const t = String(Math.floor(Date.now() / 1000));
  const hintBefore = store.get("master:hint") ?? null;
  out.masterWake = await call("GET", `/mailbox/wake?public_key_hex=${master.pubHex}&ts=${t}&peer_port=8801&signature=${await sign(master, `mailbox-wake:v1:${t}:8801`)}`, undefined, { useEnv: paused, ip: "198.51.100.77" });
  out.hintUnchanged = (store.get("master:hint") ?? null) === hintBefore;
  out.masterDrain = await call("GET", `/mailbox?public_key_hex=${master.pubHex}&ts=${t}&after=0&signature=${await sign(master, `mailbox-drain:v1:${t}:0`)}`, undefined, { useEnv: paused });
  out.zeroIsOff = (await issue(await newKey(), { useEnv: { ...env, WRITES_PAUSED: "0" } })).status;
  return out;
}

// -------------------------------------------------------------------------
// names: the name a key is held to, and how long
// -------------------------------------------------------------------------

async function nameRetention() {
  const out = {};
  const k = await newKey();
  await sendCode(k, "nora", "nora@example.org", { ip: "198.51.100.80" });
  const spent = () => JSON.stringify(counterKeys("code:").map((key) => [key, shardOf(key).db
    .prepare("SELECT n FROM counters WHERE key = ?").get(key).n]));
  const before = spent();
  out.otherNameAtOnce = await sendCode(k, "nina", "nina@example.org", { ip: "198.51.100.80" });
  out.countersUntouchedByName = spent() === before;
  if (limiter) {
    limiterShard("names").db.prepare("UPDATE names SET seen = ? WHERE pubkey = ?")
      .run(Date.now() - 366 * 86400e3, k.pubHex);
  }
  out.otherNameAfterAYear = await sendCode(k, "nina", "nina@example.org", { ip: "198.51.100.80" });
  return out;
}

// -------------------------------------------------------------------------
// mailbox: a full one refuses
// -------------------------------------------------------------------------

async function mailboxFull() {
  const s = await newKey();
  await issue(s);
  const now = Date.now();
  mailDb.exec(`WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 20000)
               INSERT INTO mail (uuid, sender, encrypted, ts, received_at, expires_at)
               SELECT printf('%08d-0000-4000-8000-000000000000', i), 'x', 'y', 't', ${now - 2 * 86400e3}, ${now + 86400e3} FROM n`);
  // the rows above came in past the object: its count of the mail held follows them
  if (hasTable(mailDb, "held")) mailDb.exec("UPDATE held SET n = (SELECT count(*) FROM mail)");
  const encrypted = Buffer.from("box:hi").toString("base64");
  const uuid = "00000000-0000-4000-8000-999999999999";
  const timestamp = "2026-11-02T14:00:00Z";
  const answer = await call("POST", "/mailbox", {
    to: master.pubHex, from_public_key: s.pubHex, encrypted, timestamp, message_uuid: uuid,
    signature: await sign(s, `mailbox:v1:${master.pubHex}:${uuid}:${timestamp}:${await sha256Hex(encrypted)}`),
  });
  // a mailbox kept by an older object: its mail is counted once at the first
  // start, the indexes its caps read go, the expiry gets one
  const oldDb = new DatabaseSync(":memory:");
  oldDb.exec(`CREATE TABLE mail (id INTEGER PRIMARY KEY AUTOINCREMENT, uuid TEXT NOT NULL UNIQUE,
              sender TEXT NOT NULL, encrypted TEXT NOT NULL, ts TEXT NOT NULL,
              received_at INTEGER NOT NULL, expires_at INTEGER NOT NULL)`);
  oldDb.exec("CREATE INDEX mail_sender_recv ON mail (sender, received_at)");
  oldDb.exec("CREATE INDEX mail_recv ON mail (received_at)");
  for (let i = 0; i < 5; i++) {
    oldDb.prepare("INSERT INTO mail (uuid, sender, encrypted, ts, received_at, expires_at) VALUES (?, 'x', 'y', 't', ?, ?)")
      .run(`old-${i}`, now, now + 86400e3);
  }
  objectOf(workerModule.MasterMailbox, oldDb);
  const upgraded = {
    held: hasTable(oldDb, "held") ? oldDb.prepare("SELECT n FROM held").get().n : null,
    indexes: oldDb.prepare("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'mail' AND sql IS NOT NULL ORDER BY name")
      .all().map((r) => r.name),
    rows: oldDb.prepare("SELECT count(*) AS n FROM mail").get().n,
  };
  return { answer, oldestKept: mailDb.prepare("SELECT count(*) AS n FROM mail WHERE uuid = '00000001-0000-4000-8000-000000000000'").get().n,
           rows: mailDb.prepare("SELECT count(*) AS n FROM mail").get().n, upgraded };
}

// -------------------------------------------------------------------------
// drops: the mailbox's daily caps count the mail it stores
// -------------------------------------------------------------------------

async function mailboxDrops() {
  const out = {};
  const drop = async (sender, n, ip) => {
    const encrypted = Buffer.from(`box:${n}`).toString("base64");
    const uuid = `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
    const timestamp = "2026-11-02T14:00:00Z";
    return call("POST", "/mailbox", {
      to: master.pubHex, from_public_key: sender.pubHex, encrypted, timestamp, message_uuid: uuid,
      signature: await sign(sender, `mailbox:v1:${master.pubHex}:${uuid}:${timestamp}:${await sha256Hex(encrypted)}`),
    }, { ip });
  };
  const drops = () => countIn(mailDb, "mailbox:all:");
  const a = await newKey(), b = await newKey();
  await issue(a, { ip: "198.51.100.120" });
  await issue(b, { ip: "198.51.100.121" });
  // a sender at its cap (three in the harness): what it is refused counts for nothing
  for (let i = 1; i <= 3; i++) await drop(a, i, "198.51.100.120");
  const before = drops();
  out.senderRefusals = [];
  for (let i = 4; i <= 6; i++) out.senderRefusals.push((await drop(a, i, "198.51.100.120")).status);
  out.dropsByRefusals = drops() - before;
  // mail the master has drained still counts for the day
  const t = String(Math.floor(Date.now() / 1000));
  const drained = await call("GET", `/mailbox?public_key_hex=${master.pubHex}&ts=${t}&after=0&signature=${await sign(master, `mailbox-drain:v1:${t}:0`)}`);
  const last = drained.body.messages[drained.body.messages.length - 1].id;
  await call("DELETE", `/mailbox?public_key_hex=${master.pubHex}&ts=${t}&upto=${last}&signature=${await sign(master, `mailbox-ack:v1:${t}:${last}`)}`);
  out.afterDrain = (await drop(a, 7, "198.51.100.120")).status;
  // an address: one drop left there, then another sender from it is refused
  presetIn(mailDb, keysIn(mailDb, "mailbox:addr:")[0], 100 - 1);
  out.addressCap = [await drop(b, 10, "198.51.100.120"), await drop(b, 11, "198.51.100.120")]
    .map((r) => [r.status, r.body.error ?? null]);
  out.otherAddress = (await drop(b, 12, "198.51.100.121")).status;
  // the day's drops in all
  presetIn(mailDb, "mailbox:all:", 5000);
  out.dayCap = await drop(b, 13, "198.51.100.122");
  // a refused drop reads by index only, whatever the mailbox holds: no
  // statement it runs scans a table (the one-row count of mail held aside)
  mailbox.obj.sql.trace = [];
  await drop(b, 14, "198.51.100.122");
  const statements = mailbox.obj.sql.trace;
  mailbox.obj.sql.trace = null;
  out.scans = statements.flatMap(([query, params]) =>
    mailDb.prepare(`EXPLAIN QUERY PLAN ${query}`).all(...params).map((r) => r.detail))
    .filter((detail) => /^SCAN /.test(detail) && !/^SCAN held\b/.test(detail));
  // a day later none of them counts
  mailDb.prepare("UPDATE counters SET until = ?").run(Date.now() - 1);
  out.nextDay = (await drop(a, 15, "198.51.100.120")).status;
  out.dropsNextDay = drops();
  // a drop from an older Worker, with no address to count, is refused before
  // anything is written
  const held = () => mailDb.prepare("SELECT n FROM held").get().n;
  const rows = () => mailDb.prepare("SELECT count(*) AS n FROM mail").get().n;
  const [heldBefore, rowsBefore] = [held(), rows()];
  const older = await mailbox.stub.fetch("https://object/put", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ uuid: "00000000-0000-4000-8000-0000000000ff", sender: a.pubHex,
                           encrypted: "eA==", ts: "2026-11-02T14:00:00Z", now: Date.now() }),
  });
  out.olderWorker = [older.status, await older.json()];
  out.olderWorkerWrote = held() !== heldBefore || rows() !== rowsBefore;
  return out;
}

// -------------------------------------------------------------------------
// armed: the stamp budget as a refusal (--armed)
// -------------------------------------------------------------------------

async function armedStamps() {
  const out = {};
  const k = await newKey();
  await issue(k, { ip: "198.51.100.90" });
  // More one-root requests at one moment than a new identity's burst, every
  // one reading the verdict before any is charged: the ledger holds each
  // charge until all eight have read theirs.
  let read = 0, allRead;
  const reads = new Promise((resolve) => { allRead = resolve; });
  const atOneMoment = { idFromName: (n) => n, get: () => ({ fetch: async (url, init) => {
    const path = new URL(url).pathname;
    if (path === "/stamp") await reads;
    const res = await ledger.stub.fetch(url, init);
    if (path === "/stamp-score" && ++read === 8) allRead();
    return res;
  } }) };
  const answers = await Promise.all(Array.from({ length: 8 }, (_, i) =>
    stamp(k, [root(7000 + i)], { ip: "198.51.100.90", useEnv: { ...env, BIRTH_LEDGER: atOneMoment } })));
  out.statuses = answers.map((a) => a.status).sort();
  out.tsKeys = Array.from({ length: 8 }, (_, i) => store.has(`ts:${root(7000 + i)}`)).filter(Boolean).length;
  // the budget spent: refused before the caps, nothing recorded or charged
  const ledgerOf = () => JSON.stringify([
    db.prepare("SELECT count(*) AS n FROM stamps WHERE pubkey = ?").get(k.pubHex).n,
    db.prepare("SELECT tokens, updated FROM notary_budget WHERE pubkey = ?").get(k.pubHex)]);
  const countersOf = () => JSON.stringify(counterKeys("stamp:").map((key) => [key, shardOf(key).db
    .prepare("SELECT n FROM counters WHERE key = ?").get(key).n]));
  const ledgerBefore = ledgerOf(), countersBefore = countersOf();
  out.spent = await stamp(k, [root(7100)], { ip: "198.51.100.91" });
  out.ledgerUntouched = ledgerOf() === ledgerBefore;
  out.countersUntouched = countersOf() === countersBefore;
  out.spentWrote = store.has(`ts:${root(7100)}`);
  // no verdict, no stamp
  const brokenLedger = { idFromName: (n) => n, get: () => ({ fetch: async () => { throw new Error("Durable Object is overloaded"); } }) };
  const k2 = await newKey();
  await issue(k2, { ip: "198.51.100.92" });
  out.ledgerDown = await stamp(k2, [root(7200)], { ip: "198.51.100.92", useEnv: { ...env, BIRTH_LEDGER: brokenLedger } });
  out.ledgerDownWrote = store.has(`ts:${root(7200)}`);
  // the verdict that decides is the charging step's: a ledger that reads the
  // budget and then fails to charge it stamps nothing
  const chargeFails = { idFromName: (n) => n, get: () => ({ fetch: async (url, init) => {
    if (new URL(url).pathname === "/stamp") throw new Error("Durable Object is overloaded");
    return ledger.stub.fetch(url, init);
  } }) };
  const k3 = await newKey();
  await issue(k3, { ip: "198.51.100.93" });
  out.chargeDown = await stamp(k3, [root(7300)], { ip: "198.51.100.93", useEnv: { ...env, BIRTH_LEDGER: chargeFails } });
  out.chargeDownWrote = store.has(`ts:${root(7300)}`);
  return out;
}

// -------------------------------------------------------------------------
// shards: one route's limiter failing leaves the others working
// -------------------------------------------------------------------------

async function shardFailure() {
  const broken = {
    idFromName: (n) => n,
    get: (n) => n === "pending"
      ? { fetch: async () => { throw new Error("Durable Object is overloaded"); } }
      : limiterShard(n).stub,
  };
  const useEnv = { ...env, LIMITER: broken };
  const k = await newKey();
  return {
    pickup: (await pending(k, "pia", { useEnv })).status,
    birth: (await issue(k, { useEnv })).status,
  };
}

// -------------------------------------------------------------------------
// history: the daily issuance counts the ledger adopts from KV once
// -------------------------------------------------------------------------

async function issuanceHistory() {
  const legacyKv = { ...kv, async get(k, type) { return k === "stats:issuance"
    ? { "2026-11-01": { births: 3, email: 1 }, "2026-10-31": { births: 2, email: 0, succession: 1 } } : null; } };
  const histDb = new DatabaseSync(":memory:");
  await objectOf(workerModule.BirthLedger, histDb, { RATE_LIMITS: legacyKv }).ready;
  await objectOf(workerModule.BirthLedger, histDb, { RATE_LIMITS: legacyKv }).ready;   // a later start adopts nothing again
  return { daily: histDb.prepare("SELECT day, field, n FROM daily ORDER BY day, field").all() };
}

const scenarios = {
  contract, mail, codes, accepts, stamps, store: storeFaults,
  directory: directoryLimits, ledger: ledgerWave, burst: birthBurst, paused: pausedWrites,
  names: nameRetention, mailbox: mailboxFull, shards: shardFailure, history: issuanceHistory,
  drops: mailboxDrops, armed: armedStamps,
};
if (serveMode) {
  const { createServer } = await import("node:http");
  let timestampCalls = 0;
  const server = createServer(async (req, res) => {
    if (req.url === "/__harness/calls") {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ timestamp: timestampCalls }));
      return;
    }
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    const body = Buffer.concat(chunks);
    if (req.url.startsWith("/timestamp")) timestampCalls += 1;
    try {
      const response = await worker.fetch(new Request(`https://worker.test${req.url}`, {
        method: req.method,
        headers: { "Content-Type": req.headers["content-type"] || "application/json",
                   "CF-Connecting-IP": "203.0.113.7" },
        body: body.length ? body : undefined,
      }), env);
      res.writeHead(response.status, Object.fromEntries(response.headers));
      res.end(Buffer.from(await response.arrayBuffer()));
    } catch (e) {
      res.writeHead(500, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: String(e) }));
    }
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  console.log(JSON.stringify({ url: `http://127.0.0.1:${server.address().port}`,
                               authority_pub: authority.pubHex }));
  await new Promise(() => {});        // serves until the test ends the process
} else {
  if (!scenarios[scenarioName]) throw new Error(`unknown scenario ${scenarioName}`);
  console.log(JSON.stringify(await scenarios[scenarioName]()));
}
