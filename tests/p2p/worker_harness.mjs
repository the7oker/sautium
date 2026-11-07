// Drives the REAL worker/verify.js fetch handler in Node with an in-memory KV,
// its Durable Objects on node:sqlite, a mail provider that records what it
// is sent and a throwaway authority key, so the Python side can check what
// the Worker code actually does (test_birth_cert.py, test_worker_limits.py).
//
// usage: node worker_harness.mjs <authority_seed_hex> <email_pepper> [--scenario NAME] [--armed]
//                                [--owed-timeout MS]
//        node worker_harness.mjs <authority_seed_hex> <email_pepper> --serve [--stamp-brake N]
// Without --serve: runs one scenario (default: contract) and prints one JSON
// object on stdout; --armed runs it with the stamp budget armed, as a refusal
// (shipped shadow). With --serve: answers HTTP on 127.0.0.1 (a free port) with
// the same Worker code and env — what a node's notary is pointed at in a test
// (sign_audio.WORKER_URL) — prints {"url", "authority_pub"} as its first line
// and serves until killed. Its address brake on /timestamp lets N stamps
// through (default: no limit; the scenario's 8 would turn the ninth stamp of a
// test into a 429), GET /__harness/calls answers how many /timestamp
// requests reached the Worker, and GET /__harness/mails the mails it sent.
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
const owedTimeoutAt = harnessArgs.indexOf("--owed-timeout");
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
if (owedTimeoutAt >= 0) {
  // How long the birth instance waits on a handover the owners instance does
  // not answer, cut for the scenario that waits it out.
  const before = swapped;
  swapped = before.replace("const OWED_CALL_TIMEOUT_MS = 10 * 1000;",
                           `const OWED_CALL_TIMEOUT_MS = ${Number(harnessArgs[owedTimeoutAt + 1])};`);
  if (swapped === before) throw new Error("OWED_CALL_TIMEOUT_MS pattern not found");
}
if (armed) {
  const shadow = swapped;
  swapped = shadow.replace("const STAMP_BUDGET_ARMED = false;", "const STAMP_BUDGET_ARMED = true;");
  if (swapped === shadow) throw new Error("STAMP_BUDGET_ARMED pattern not found");
}
const tmp = join(mkdtempSync(join(tmpdir(), "sautium-worker-")), "verify.mjs");
// The small-order key filter, exported for its own test.
writeFileSync(tmp, swapped + "\nexport { isSmallOrderKey };\n");
const workerModule = await import(pathToFileURL(tmp).href);
const worker = workerModule.default;

// SQL handles for the Durable Objects: each class the Worker exports runs
// in-process on node:sqlite — its SQL is exercised for real, only the storage
// handle is emulated. `trace`, when set to an array, collects every statement
// and its parameters; `failOn`, when set to a pattern, makes a statement
// that matches it fail as a full disk would.
function sqlHandle(db) {
  const handle = {
    trace: null,
    failOn: null,
    exec(query, ...params) {
      if (handle.trace) handle.trace.push([query, params]);
      if (handle.failOn && handle.failOn.test(query)) throw new Error("SQLITE_FULL: database or disk is full");
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
  // fire(): the alarm runs as Cloudflare runs one — one at a time, spent as
  // it fires, so only a handler that sets it again leaves one; a handler
  // that throws has it retried. Cloudflare retries it from 2 s on, six times
  // at most; here the retry is left set for the test to fire, and no test
  // depends on its timing or its limit.
  let firing = Promise.resolve();
  const fire = () => {
    const run = firing.then(async () => {
      alarmAt = null;
      try {
        await obj.alarm();
      } catch (e) {
        if (alarmAt === null) alarmAt = Date.now();
        throw e;
      }
    });
    firing = run.catch(() => {});
    return run;
  };
  return { obj, stub, db, sql: storage.sql, ready, alarm: () => alarmAt, fire,
           namespace: { idFromName: (name) => name, get: () => stub } };
}

// KV: a Map, every read and write counted, a switch that makes calls fail the
// way the runtime's own KV errors do, and (when asked) KV's rule of one write
// a second to a key.
const store = new Map();
// fail: null | "put" | "get"; failGetAfter: that many gets succeed, then they fail;
// shortPages: a page that is not the last comes back with fewer keys than asked
const kvState = { writes: 0, reads: 0, lists: 0, fail: null, failGetAfter: null, perKeyRule: false,
                  shortPages: false };
const lastWrite = new Map();
const kv = {
  async get(k, type) {
    if (kvState.fail === "get") throw new Error("KV GET failed: 429 Too Many Requests");
    if (kvState.failGetAfter !== null && kvState.failGetAfter-- <= 0) {
      throw new Error("KV GET failed: 429 Too Many Requests");
    }
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
  async list({ prefix, limit = 1000, cursor }) {
    // A page at a time, as KV lists: the cursor names where the next begins.
    const all = [...store.keys()].filter((k) => k.startsWith(prefix) && (!cursor || k > cursor)).sort();
    const complete = all.length <= limit;
    const keys = all.slice(0, complete || !kvState.shortPages ? limit : Math.max(1, Math.floor(limit / 3)));
    kvState.lists += 1;
    return { keys: keys.map((name) => ({ name })), list_complete: complete,
             ...(complete ? {} : { cursor: keys[keys.length - 1] }) };
  },
};

const db = new DatabaseSync(":memory:");
const ledger = objectOf(workerModule.BirthLedger, db);
const mailDb = new DatabaseSync(":memory:");
const mailbox = objectOf(workerModule.MasterMailbox, mailDb);
const dirDb = new DatabaseSync(":memory:");
const directory = objectOf(workerModule.NodeDirectory, dirDb);
// The Limiter: one instance per name, each on its own database, as Cloudflare
// keeps one object per id. Its env is the Worker's, as an object's is: the
// KV the owners' backfill reads, the namespace the birth instance hands its
// claims over through, and the owner's brake (call() sets it as the request
// it makes has it; a test sets it itself for an alarm).
const limiterEnv = { RATE_LIMITS: kv };
const limiterShards = new Map();
function limiterShard(name) {
  if (!limiterShards.has(name)) {
    limiterShards.set(name, objectOf(workerModule.Limiter, new DatabaseSync(":memory:"), limiterEnv));
  }
  return limiterShards.get(name);
}
const limiter = workerModule.Limiter ? {
  namespace: { idFromName: (name) => name, get: (name) => limiterShard(name).stub },
} : null;
if (limiter) limiterEnv.LIMITER = limiter.namespace;
async function settleBirths() {
  // The birth instance's alarm, as Cloudflare runs it right after a birth
  // set it: the claims births left there handed to the owners instance.
  const births = limiterShards.get("birth");
  if (births && births.alarm() !== null && births.alarm() <= Date.now()) await births.fire();
}

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
  limiterEnv.WRITES_PAUSED = useEnv.WRITES_PAUSED;   // one variable for the Worker and its objects
  const res = await worker.fetch(req, useEnv);
  const text = await res.text();
  let parsed;
  try { parsed = JSON.parse(text); } catch { parsed = text; }
  return { status: res.status, body: parsed, headers: Object.fromEntries(res.headers) };
}

async function issue(key, opts) {
  const answer = await call("POST", "/birth-certificate", {
    pubkey_hex: key.pubHex, signature: await sign(key, `birth:${key.pubHex}`),
  }, opts);
  await settleBirths();
  return answer;
}
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
// The invite wire as a node speaks it: the signed texts in the short code a
// client builds, keys in lower-case hex, `ts` in integer seconds.
const nowS = () => Math.floor(Date.now() / 1000);
function canonical(code) {
  const [name, h = ""] = code.trim().split("#");
  const x = h.replace(/-/g, "").toUpperCase();
  return `${name}#${x.slice(0, 4)}-${x.slice(4, 8)}-${x.slice(8)}`;
}
async function inviteIdOf(token) {
  const h = await sha256Hex(`sautium-invite-id:v1:${token}`);
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20, 32)}`;
}
async function invite(key, name, to, opts = {}, message = "") {
  const code = opts.invite || await inviteOf(key, name);
  const ts = opts.ts ?? nowS();
  const text = `sautium-invite-send:v1:${ts}:${code}:${to}:${await sha256Hex(opts.signedMessage ?? message ?? "")}`;
  return call("POST", "/send-invite", {
    to, invite_code: code, public_key_hex: key.pubHex, ts, signature: await sign(key, text), message,
  }, opts);
}
async function accept(key, name, theirs, opts = {}) {
  // With opts.token, an acceptance of the invite it names; without, an add
  // by plain code.
  const mine = await inviteOf(key, name);
  const ts = opts.ts ?? nowS();
  const their = /^[A-Za-z0-9_-]{3,32}#[0-9A-Fa-f-]{12,}$/.test(theirs.trim()) ? canonical(theirs) : theirs;
  const text = opts.token !== undefined
    ? `sautium-invite-accept:v1:${ts}:${await inviteIdOf(opts.token)}:${their}:${mine}:${key.pubHex}`
    : `sautium-invite-add:v1:${ts}:${their}:${mine}:${key.pubHex}`;
  return call("POST", "/accept-invite", {
    my_invite_code: mine, their_invite_code: theirs, public_key_hex: key.pubHex, ts,
    ...(opts.token !== undefined ? { invite_token: opts.token } : {}),
    signature: await sign(key, text),
  }, opts);
}
async function pending(key, name, opts = {}) {
  const mine = await inviteOf(key, name);
  const ts = opts.ts ?? nowS();
  return call("POST", "/pending-accepts", {
    invite_code: mine, public_key_hex: key.pubHex, ts,
    signature: await sign(key, `sautium-invite-pickup:v1:${ts}:${mine}:${key.pubHex}`),
  }, opts);
}
const tokenIn = (mail) => mail.html.match(/#i-([0-9a-f]{32})</)[1];
const invitesTable = () => limiterShard("invites").db.prepare("SELECT * FROM invites ORDER BY sent, id").all();
const addsTable = () => limiterShard("invites").db.prepare("SELECT * FROM adds ORDER BY added, adder").all();
async function stamp(key, roots, opts, ts = Math.floor(Date.now() / 1000)) {
  return call("POST", "/timestamp", {
    ...(roots.length === 1 ? { root: roots[0] } : { roots }),
    pubkey: key.pubHex, ts,
    signature: await sign(key, `sautium-timestamp-request:v1:${ts}:${roots.join(",")}`),
  }, opts);
}
const root = (n) => n.toString(16).padStart(64, "0");
const DAY_MS = 86400e3;

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
  // (s5's own birth, stored before: every invite route needs a key's code to be its own)
  const s5 = await newKey();
  store.set(`born:${s5.pubHex}`, JSON.stringify({ v: 4, issued_at: "2026-07-07T00:00:00Z", method: "pow",
    difficulty: 32, params_version: 1, sig_v4: "cd".repeat(64) }));
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
  await issue(fresh);
  await sendCode(fresh, "first", "b@example.org", { ip: "198.51.100.4" });
  out.boundByFirstUse = await sendCode(fresh, "second", "b2@example.org", { ip: "198.51.100.4" });
  out.firstNameStill = await sendCode(fresh, "first", "b3@example.org", { ip: "198.51.100.4" });
  // a read fixes no name: check-email under any name, then a write under another
  const reader = await newKey();
  await issue(reader);
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
    const asker = await newKey();
    await issue(asker, { ip: `198.51.100.${40 + i}` });
    out.codesToOneMailbox.push((await sendCode(asker, "tom", i % 2 ? "tom@example.org" : "Tom+x@example.org",
                                               { ip: `198.51.100.${40 + i}` })).status);
  }
  // the day's 60th code goes, the 61st waits for 00:00 UTC
  preset("code:all:", 59);
  out.codeCeiling = [];
  for (let i = 0; i < 2; i++) {
    const asker = await newKey();
    await issue(asker, { ip: `198.51.100.${70 + i}` });
    out.codeCeiling.push((await sendCode(asker, "cleo", `cleo${i}@example.org`,
                                         { ip: `198.51.100.${70 + i}` })).status);
  }
  return out;
}

// -------------------------------------------------------------------------
// accepts: an invite's token, its one spending, the add by plain code, the pickup
// -------------------------------------------------------------------------

async function accepts() {
  const out = {};
  const masha = await newKey(), sasha = await newKey(), pasha = await newKey();
  await issue(masha); await issue(sasha); await issue(pasha);
  await verifyEmail(masha, "masha", "masha@example.org", { ip: "198.51.100.50" });
  await verifyEmail(sasha, "sasha", "sasha@example.net", { ip: "198.51.100.51" });
  const mashaCode = await inviteOf(masha, "masha");
  // an invite mail carries the inviter's long code and a token; the Worker
  // keeps the invite under the token's hash, and nothing of the mail
  let sent = mails.length;
  out.sent = await invite(masha, "masha", "sasha@example.net", { ip: "198.51.100.50" }, "come and listen");
  const [mail] = mails.slice(sent);
  const token = tokenIn(mail);
  out.inviteMailTo = mail.to;
  out.tokenIdMatches = out.sent.body.invite_id === await inviteIdOf(token);
  out.stringInMail = mail.html.includes(`${(await inviteOf(masha, "masha")).split("#")[0]}#`)
    && /masha#[0-9A-F]{4}(-[0-9A-F]{4}){7}#i-[0-9a-f]{32}/.test(mail.html);
  out.rowsAfterSend = invitesTable();
  out.rowHoldsNothingOfTheMail = JSON.stringify(out.rowsAfterSend).match(
    new RegExp([token, "sasha@example.net", "come and listen"].join("|"))) === null;
  // the token spent once: accepted, then the same answer again with no
  // second mail, then refused for another key
  sent = mails.length;
  const notifyToBefore = new Set(counterKeys("notify:to:"));
  out.accepted = await accept(sasha, "sasha", mashaCode, { ip: "198.51.100.51", token });
  out.acceptMail = mails.slice(sent);
  out.addsAfterAccept = addsTable().length;                  // an acceptance is no add
  const mashaNotifyTo = counterKeys("notify:to:").find((key) => !notifyToBefore.has(key));
  sent = mails.length;
  out.acceptedAgain = await accept(sasha, "sasha", mashaCode, { ip: "198.51.100.51", token });
  out.mailsByAgain = mails.length - sent;
  out.usedByAnother = await accept(pasha, "pasha", mashaCode, { ip: "198.51.100.52", token });
  // two keys spending one token at once: one of them (another inviter, whose
  // day of invites is its own)
  const nina = await newKey();
  await issue(nina, { ip: "198.51.100.61" });
  await verifyEmail(nina, "nina", "nina@example.org", { ip: "198.51.100.61" });
  const ninaCode = await inviteOf(nina, "nina");
  await invite(nina, "nina", "both@example.net", { ip: "198.51.100.61" });
  const raced = tokenIn(mails[mails.length - 1]);
  const [r1, r2] = [await newKey(), await newKey()];
  await issue(r1); await issue(r2);
  out.raced = (await Promise.all([accept(r1, "rhea", ninaCode, { ip: "198.51.100.58", token: raced }),
                                  accept(r2, "rita", ninaCode, { ip: "198.51.100.59", token: raced })]))
    .map((r) => r.status).sort();
  out.racedRows = invitesTable().filter((r) => r.acceptor === r1.pubHex || r.acceptor === r2.pubHex).length;
  // what a token can be refused for
  out.badToken = await accept(pasha, "pasha", mashaCode, { ip: "198.51.100.52", token: "0".repeat(31) });
  out.upperToken = await accept(pasha, "pasha", mashaCode, { ip: "198.51.100.52", token: "A".repeat(32) });
  out.unknownToken = await accept(pasha, "pasha", mashaCode, { ip: "198.51.100.52", token: "ab".repeat(16) });
  sent = mails.length;
  await invite(masha, "masha", "pasha@example.net", { ip: "198.51.100.50" });
  const token2 = tokenIn(mails[mails.length - 1]);
  const vera = await newKey();
  await issue(vera);
  out.notThisCode = await accept(pasha, "pasha", await inviteOf(vera, "vera"), { ip: "198.51.100.52", token: token2 });
  out.own = await accept(masha, "masha", mashaCode, { ip: "198.51.100.50", token: token2 });
  out.ownByCode = await accept(masha, "masha", mashaCode, { ip: "198.51.100.50" });
  // an expired invite is gone
  limiterShard("invites").db.prepare("UPDATE invites SET expires = ? WHERE id = ?")
    .run(Date.now() - 1, await inviteIdOf(token2));
  out.expired = await accept(pasha, "pasha", mashaCode, { ip: "198.51.100.52", token: token2 });
  // a request is fresh: each signed text carries its time
  const stale = nowS() - 3600;
  out.staleSend = await invite(masha, "masha", "x@example.net", { ip: "198.51.100.50", ts: stale });
  out.staleAccept = await accept(pasha, "pasha", mashaCode, { ip: "198.51.100.52", ts: stale });
  out.stalePickup = await pending(masha, "masha", { ip: "198.51.100.50", ts: stale });
  out.unsignedMessage = await invite(masha, "masha", "y@example.net",
                                     { ip: "198.51.100.50", signedMessage: "something else" }, "this text");
  out.textTs = await call("POST", "/pending-accepts", {
    invite_code: mashaCode, public_key_hex: masha.pubHex, ts: String(nowS()),
    signature: await sign(masha, `sautium-invite-pickup:v1:${nowS()}:${mashaCode}:${masha.pubHex}`),
  }, { ip: "198.51.100.50" });
  // the pickup: every accepted invite, each with its acceptor's statement; a
  // read, so a second pickup finds the same
  out.pickup = await pending(masha, "masha", { ip: "198.51.100.50" });
  out.pickupAgain = await pending(masha, "masha", { ip: "198.51.100.50" });
  out.pickupUnderAnotherName = await pending(masha, "mallory", { ip: "198.51.100.50" });
  out.pickupByGet = await call("GET", `/pending-accepts?invite_code=${encodeURIComponent(mashaCode)}`);
  // acceptances kept before this Worker are never read again
  store.set(`accepted:${mashaCode.replace("#", ":")}:old:AAAA-BBBB-CCCC`,
            JSON.stringify({ invite_code: "old#AAAA-BBBB-CCCC", accepted_at: "2026-11-01T00:00:00Z" }));
  out.pickupAfterOldKey = await pending(masha, "masha", { ip: "198.51.100.50" });
  // an add by plain code spends no invite and mails the code's owner the
  // username and the code to add back
  const rowsBefore = JSON.stringify(invitesTable());
  sent = mails.length;
  const typed = mashaCode.toLowerCase().replace(/-/g, "");            // as a person might type it
  out.added = await accept(sasha, "sasha", typed, { ip: "198.51.100.51" });
  out.addMail = mails.slice(sent);
  out.addKeptNoInvite = JSON.stringify(invitesTable()) === rowsBefore;
  // it is kept for the code's owner under the code's digest, with the
  // adder's statement; the owner's pickup brings it, nobody else's does,
  // and a repeat refreshes its one row
  out.addRows = addsTable();
  out.pickupAfterAdd = await pending(masha, "masha", { ip: "198.51.100.50" });
  out.pickupOfAnother = await pending(pasha, "pasha", { ip: "198.51.100.52" });
  // a key with an invite kept asks on `pending`, one with none on `requests`
  out.pickupCounters = {
    pashaOnRequests: counterKeys("requests:key:").includes(`requests:key:${pasha.pubHex}`),
    pashaOnPending: counterKeys("pending:key:").includes(`pending:key:${pasha.pubHex}`),
    mashaOnPending: counterKeys("pending:key:").includes(`pending:key:${masha.pubHex}`),
    mashaOnRequests: counterKeys("requests:key:").includes(`requests:key:${masha.pubHex}`),
  };
  // an add refused at its cap keeps nothing
  const capped = await newKey();
  await issue(capped, { ip: "198.51.100.63" });
  preset(`accept:key:${capped.pubHex}`, 20);
  out.addAtKeyCap = await accept(capped, "cappy", mashaCode, { ip: "198.51.100.63" });
  out.addsByCapped = addsTable().filter((a) => a.adder === capped.pubHex).length;
  // a code keeps its newest page of adds
  const crowd = limiterShard("invites").db.prepare(
    `INSERT INTO adds (owner, adder, adder_code, their_code, added, statement_ts, signature, expires)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?)`);
  for (let i = 0; i < 300; i++) {
    crowd.run("AAAABBBBCCCC", i.toString(16).padStart(64, "0"), `c${i}#AAAA-BBBB-CCCC`, "zed#AAAA-BBBB-CCCC",
              Date.now() - DAY_MS + i, nowS(), "00".repeat(64), Date.now() + DAY_MS);
  }
  await accept(sasha, "sasha", "zed#AAAA-BBBB-CCCC", { ip: "198.51.100.51" });
  const crowdRows = limiterShard("invites").db.prepare("SELECT adder FROM adds WHERE owner = 'AAAABBBBCCCC'").all();
  out.crowd = { length: crowdRows.length, newestKept: crowdRows.some((r) => r.adder === sasha.pubHex),
                oldestLeft: !crowdRows.some((r) => r.adder === "0".repeat(64)) };
  await new Promise((r) => setTimeout(r, 5));
  out.addedAgain = await accept(sasha, "sasha", mashaCode, { ip: "198.51.100.51" });
  out.addRowsAgain = addsTable();
  out.addToNoMailbox = await accept(masha, "masha", await inviteOf(pasha, "pasha"), { ip: "198.51.100.50" });
  // a verified address under a code whose digest another key holds is
  // nobody's to mail
  const quin = await newKey();
  await issue(quin, { ip: "198.51.100.62" });
  await verifyEmail(quin, "quin", "quin@example.org", { ip: "198.51.100.62" });
  const quinDigest = (await inviteOf(quin, "quin")).split("#")[1].replace(/-/g, "");
  limiterShard("owners").db.prepare("UPDATE owners SET pubkey = ?, issued = ? WHERE digest = ?")
    .run("ee".repeat(32), Date.parse("2026-01-01T00:00:00Z"), quinDigest);
  sent = mails.length;
  out.addToAnothersRecord = await accept(sasha, "sasha", await inviteOf(quin, "quin"), { ip: "198.51.100.51" });
  out.mailsToAnothersRecord = mails.slice(sent).filter((m) => m.to[0] === "quin@example.org").length;
  // a record under a code whose digest is not its key's is nobody's to
  // mail, though that key holds a digest of its own
  const xeno = "xeno#ABCD-EF01-2345";
  store.set(`verified:${xeno.replace("#", ":")}`, JSON.stringify({
    email: "xeno@example.org", pubkey: nina.pubHex, born_at: "2026-11-01T00:00:00Z",
    verified_at: "2026-11-01T00:00:00Z" }));
  sent = mails.length;
  out.addToMismatchedRecord = await accept(sasha, "sasha", xeno, { ip: "198.51.100.51" });
  out.mailsToMismatchedRecord = mails.slice(sent).filter((m) => m.to[0] === "xeno@example.org").length;
  out.invalidTheirs = await accept(sasha, "sasha", "nonsense", { ip: "198.51.100.51" });
  // a mail that fails leaves no invite behind
  const idsBeforeDown = new Set(invitesTable().map((r) => r.id));
  mailState.down = true;
  out.sendMailDown = await invite(masha, "masha", "down@example.net", { ip: "198.51.100.50" });
  mailState.down = false;
  out.invitesLeftByMailDown = invitesTable().filter((r) => !idsBeforeDown.has(r.id)).length;
  // the acceptance's caps come before its step; the mail's are its own
  sent = mails.length;
  await invite(masha, "masha", "kasia@example.net", { ip: "198.51.100.50" });
  const token3 = tokenIn(mails[mails.length - 1]);
  preset(`accept:key:${pasha.pubHex}`, 20);
  const rowsAtCap = JSON.stringify(invitesTable());
  out.acceptAtKeyCap = await accept(pasha, "pasha", mashaCode, { ip: "198.51.100.52", token: token3 });
  out.stepUntouchedAtCap = JSON.stringify(invitesTable()) === rowsAtCap;
  unset(`accept:key:${pasha.pubHex}`);
  preset(mashaNotifyTo, 3);
  sent = mails.length;
  out.acceptAtNotifyCap = await accept(pasha, "pasha", mashaCode, { ip: "198.51.100.52", token: token3 });
  out.mailsAtNotifyCap = mails.length - sent;
  // the mails one address may set off in a day
  unset(mashaNotifyTo);
  const notifyAddrsBefore = new Set(counterKeys("notify:addr:"));
  const adder = async (name, ip) => { const k = await newKey(); await issue(k, { ip }); return accept(k, name, mashaCode, { ip }); };
  await adder("kasia", "198.51.100.53");
  const notifyAddr = counterKeys("notify:addr:").find((key) => !notifyAddrsBefore.has(key));
  if (notifyAddr) preset(notifyAddr, 3);
  sent = mails.length;
  out.addAtNotifyAddrCap = await adder("lena", "198.51.100.53");
  out.mailsAtNotifyAddrCap = mails.length - sent;
  out.addFromOtherAddr = await adder("olha", "198.51.100.54");
  // a pickup takes a page; at its cap it is refused before any read
  const inviter = masha.pubHex;
  const insert = limiterShard("invites").db.prepare(
    `INSERT INTO invites (id, inviter, inviter_code, sent, expires, acceptor, acceptor_code, accepted, statement_ts, signature)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`);
  for (let i = 0; i < 400; i++) {
    insert.run(`00000000-0000-4000-8000-${String(i).padStart(12, "0")}`, inviter, mashaCode, Date.now() - 2 * DAY_MS,
               Date.now() + DAY_MS, "ee".repeat(32), `x${i}#AAAA-BBBB-CCCC`, Date.now() - DAY_MS + i, nowS(), "00".repeat(64));
  }
  const page = (await pending(masha, "masha", { ip: "198.51.100.50" })).body.accepts;
  const codesOf = page.map((a) => a.acceptor.invite_code);
  out.page = { length: page.length, newestFirst: page[0].accepted_at >= page[1].accepted_at
                                                  && page[1].accepted_at >= page[299].accepted_at,
               oldestLeft: !codesOf.includes("x0#AAAA-BBBB-CCCC"), newestInserted: codesOf.includes("x399#AAAA-BBBB-CCCC") };
  preset(`pending:key:${masha.pubHex}`, 100);
  out.pickupAtCap = await pending(masha, "masha", { ip: "198.51.100.50" });
  // the day's 12th acceptance mail goes, the 13th waits for 00:00 UTC; the
  // acceptance is kept either way
  const wren = await newKey();
  await issue(wren);
  await verifyEmail(wren, "wren", "wren@example.org", { ip: "198.51.100.55" });
  preset("notify:all:", 11);
  out.notifyCeiling = [];
  for (let i = 0; i < 2; i++) {
    await invite(wren, "wren", `fan${i}@example.net`, { ip: "198.51.100.55" });
    const fan = await newKey();
    await issue(fan, { ip: `198.51.100.${56 + i}` });
    const r = await accept(fan, `fan${i}`, await inviteOf(wren, "wren"),
                           { ip: `198.51.100.${56 + i}`, token: tokenIn(mails[mails.length - 1]) });
    out.notifyCeiling.push([r.status, r.body.notified]);
  }
  // an expired invite is removed by the invites instance's alarm
  const shard = limiterShard("invites");
  out.alarmSet = shard.alarm() !== null;
  shard.db.prepare("UPDATE invites SET expires = ? WHERE id = ?").run(Date.now() - 1, out.sent.body.invite_id);
  await shard.obj.alarm();
  out.expiredLeft = shard.db.prepare("SELECT count(*) AS n FROM invites WHERE id = ?").get(out.sent.body.invite_id).n;
  // and an expired add by its alarm
  shard.db.prepare("UPDATE adds SET expires = ? WHERE adder = ?").run(Date.now() - 1, sasha.pubHex);
  await shard.obj.alarm();
  out.expiredAddsLeft = shard.db.prepare("SELECT count(*) AS n FROM adds WHERE adder = ?").get(sasha.pubHex).n;
  out.addsLeft = shard.db.prepare("SELECT count(*) AS n FROM adds").get().n;
  // the alarm is due at the soonest add's end too
  const soon = Date.now() + 60_000;
  shard.db.prepare("UPDATE adds SET expires = ? WHERE adder = ?").run(soon, masha.pubHex);
  await shard.obj.alarm();
  out.alarmAtAdd = shard.alarm() === soon;
  out.keys = { masha: masha.pubHex, sasha: sasha.pubHex, masha_code: mashaCode,
               sasha_code: await inviteOf(sasha, "sasha") };
  return out;
}

// -------------------------------------------------------------------------
// vectors: the invite wire's test vectors, through the Worker itself
// -------------------------------------------------------------------------

async function inviteVectors() {
  // The vectors' fixed keys, time and token (tests/p2p/vectors/invite_wire_v1.json):
  // the Worker must take their signatures as its own texts' and answer with
  // their id, string and codes.
  const V = JSON.parse(readFileSync(resolve(here, "vectors/invite_wire_v1.json"), "utf-8"));
  const out = {};
  const T = V.ts;
  const realNow = Date.now;
  Date.now = () => T * 1000;
  const masha = await keyFromSeed(V.inviter_seed), sasha = await keyFromSeed(V.acceptor_seed);
  out.keys = { inviter: masha.pubHex, acceptor: sasha.pubHex };
  await issue(masha); await issue(sasha);
  await verifyEmail(masha, V.inviter.name, "masha@example.org", { ip: "198.51.100.40" });
  await verifyEmail(sasha, V.acceptor.name, V.to, { ip: "198.51.100.41" });
  const mashaShort = V.inviter.short_code, sashaShort = V.acceptor.short_code;
  out.sendSig = await sign(masha, `sautium-invite-send:v1:${T}:${mashaShort}:${V.to}:${await sha256Hex(V.message)}`);
  // (Date.now is the vectors' time; `new Date()`, which births and records
  // use, keeps the real clock — no value checked here reads it.)
  const real = crypto.getRandomValues.bind(crypto);
  crypto.getRandomValues = (a) => (a.length === 16 ? (a.set(unhex(V.token)), a) : real(a));
  let sent = mails.length;
  try {
    out.sent = await call("POST", "/send-invite", {
      to: V.to, invite_code: mashaShort, public_key_hex: masha.pubHex, ts: T, message: V.message,
      signature: out.sendSig }, { ip: "198.51.100.40" });
  } finally {
    crypto.getRandomValues = real;
  }
  out.inviteMail = mails.slice(sent);
  const inviteId = out.sent.body.invite_id;
  out.acceptSig = await sign(sasha, `sautium-invite-accept:v1:${T}:${inviteId}:${mashaShort}:${sashaShort}:${sasha.pubHex}`);
  sent = mails.length;
  out.accepted = await call("POST", "/accept-invite", {
    my_invite_code: sashaShort, their_invite_code: mashaShort, public_key_hex: sasha.pubHex, ts: T,
    invite_token: V.token, signature: out.acceptSig }, { ip: "198.51.100.41" });
  out.acceptMail = mails.slice(sent);
  out.pickupSig = await sign(masha, `sautium-invite-pickup:v1:${T}:${mashaShort}:${masha.pubHex}`);
  out.pickup = await call("POST", "/pending-accepts", {
    invite_code: mashaShort, public_key_hex: masha.pubHex, ts: T, signature: out.pickupSig }, { ip: "198.51.100.40" });
  out.addSig = await sign(sasha, `sautium-invite-add:v1:${T}:${mashaShort}:${sashaShort}:${sasha.pubHex}`);
  sent = mails.length;
  out.added = await call("POST", "/accept-invite", {
    my_invite_code: sashaShort, their_invite_code: mashaShort, public_key_hex: sasha.pubHex, ts: T,
    signature: out.addSig }, { ip: "198.51.100.41" });
  out.addMail = mails.slice(sent);
  out.pickupAfterAdd = await call("POST", "/pending-accepts", {
    invite_code: mashaShort, public_key_hex: masha.pubHex, ts: T, signature: out.pickupSig }, { ip: "198.51.100.40" });
  Date.now = realNow;
  return out;
}

// -------------------------------------------------------------------------
// owners: an invite code belongs to the first key born under its digest
// -------------------------------------------------------------------------

async function inviteOwners() {
  const out = {};
  const owners = limiterShard("owners");
  const digestOf = async (key) => (await inviteOf(key, "x")).split("#")[1].replace(/-/g, "");
  // births stored before claims were recorded: more than one backfill page
  const old = [];
  for (let i = 0; i < 1200; i++) {
    const pub = hex(webcrypto.getRandomValues(new Uint8Array(32)));
    old.push(pub);
    store.set(`born:${pub}`, JSON.stringify({ born_at: "2026-07-05T10:11:12Z", sig: "ab".repeat(64) }));
  }
  const early = await newKey();
  store.set(`born:${early.pubHex}`, JSON.stringify({ v: 4, issued_at: "2026-06-01T00:00:00Z", method: "pow",
    difficulty: 32, params_version: 1, sig_v4: "cd".repeat(64) }));
  // a later key already claimed the early one's digest (born after claims
  // began, before the backfill reached its elder): the elder wins
  const usurper = "ff".repeat(32);
  owners.db.prepare("INSERT INTO owners (digest, pubkey, issued) VALUES (?, ?, ?)")
    .run(await digestOf(early), usurper, Date.now());
  const check = async (key, name, email = "x@example.org") => call("GET",
    `/check-email?invite_code=${encodeURIComponent(await inviteOf(key, name))}&email=${encodeURIComponent(email)}`
    + `&public_key_hex=${key.pubHex}&signature=${await sign(key, `check-email:${await inviteOf(key, name)}:${email}`)}`);
  // until every stored birth is read, no owner is answered
  kvState.shortPages = true;
  out.beforeBackfill = await check(early, "ella");
  out.alarmAfterFirstStep = owners.alarm() !== null;
  out.listsSoFar = kvState.lists;
  // a step whose reads fail part-way writes nothing, and the next one reads
  // the same page again
  const metaOf = () => owners.db.prepare("SELECT v FROM meta WHERE k = 'owners_backfill'").get()?.v ?? null;
  const [rowsBefore, cursorBefore] = [owners.db.prepare("SELECT count(*) AS n FROM owners").get().n, metaOf()];
  kvState.failGetAfter = 10;
  out.failedStep = await owners.fire().then(() => "ran", (e) => String(e.message));
  kvState.failGetAfter = null;
  out.failedStepWroteNothing = owners.db.prepare("SELECT count(*) AS n FROM owners").get().n === rowsBefore
    && metaOf() === cursorBefore;
  // a step whose cursor cannot be written keeps none of its page's claims
  owners.sql.failOn = /INSERT INTO meta/;
  out.fullStep = await owners.fire().then(() => "ran", (e) => String(e.message));
  owners.sql.failOn = null;
  out.fullStepWroteNothing = owners.db.prepare("SELECT count(*) AS n FROM owners").get().n === rowsBefore
    && metaOf() === cursorBefore;
  // the alarm chains itself to the end: each step sets the next
  let steps = 0;
  while (owners.alarm() !== null && steps < 20) {
    await owners.fire();
    steps += 1;
  }
  kvState.shortPages = false;
  out.steps = steps;
  out.backfillDone = metaOf() === "done";
  out.backfilled = owners.db.prepare("SELECT count(*) AS n FROM owners").get().n;
  out.elderHolds = owners.db.prepare("SELECT pubkey FROM owners WHERE digest = ?").get(await digestOf(early)).pubkey
    === early.pubHex;
  out.afterBackfill = await check(early, "ella");
  // a key born now under a digest an elder holds is refused on every route
  const late = await newKey();
  owners.db.prepare("INSERT INTO owners (digest, pubkey, issued) VALUES (?, ?, ?)")
    .run(await digestOf(late), early.pubHex, Date.parse("2026-06-01T00:00:00Z"));
  await issue(late);
  out.lateHolds = owners.db.prepare("SELECT pubkey FROM owners WHERE digest = ?").get(await digestOf(late)).pubkey;
  const lateCode = await inviteOf(late, "lena");
  out.lateRoutes = [
    await sendCode(late, "lena", "lena@example.org"),
    await register(late, "lena", "lena@example.org", "AAAAAA"),
    await check(late, "lena"),
    await invite(late, "lena", "z@example.net"),
    await accept(late, "lena", await inviteOf(early, "ella")),
    await pending(late, "lena"),
  ].map((r) => [r.status, r.body]);
  out.lateCode = lateCode;
  // a birth claims its digest at issuance
  const fresh = await newKey();
  await issue(fresh);
  out.freshHolds = owners.db.prepare("SELECT pubkey FROM owners WHERE digest = ?").get(await digestOf(fresh))?.pubkey
    === fresh.pubHex;
  // a stored birth the backfill could not have seen claims at its first use
  const missed = await newKey();
  store.set(`born:${missed.pubHex}`, JSON.stringify({ v: 4, issued_at: "2026-10-01T00:00:00Z", method: "pow",
    difficulty: 32, params_version: 1, sig_v4: "cd".repeat(64) }));
  out.missedFirstUse = await check(missed, "mira");
  out.missedHolds = owners.db.prepare("SELECT issued FROM owners WHERE digest = ?").get(await digestOf(missed))?.issued
    === Date.parse("2026-10-01T00:00:00Z");
  // an elder whose claim never landed, its digest held meanwhile by a key
  // born later: its first use claims, and the earlier birth holds it
  const elder = await newKey();
  store.set(`born:${elder.pubHex}`, JSON.stringify({ v: 4, issued_at: "2026-05-01T00:00:00Z", method: "pow",
    difficulty: 32, params_version: 1, sig_v4: "cd".repeat(64) }));
  owners.db.prepare("INSERT INTO owners (digest, pubkey, issued) VALUES (?, ?, ?)")
    .run(await digestOf(elder), "dd".repeat(32), Date.parse("2026-10-30T00:00:00Z"));
  out.elderFirstUse = await check(elder, "eden");
  out.elderNowHolds = owners.db.prepare("SELECT pubkey FROM owners WHERE digest = ?").get(await digestOf(elder))
    .pubkey === elder.pubHex;
  // two born in one second: the lesser key, whichever claims first
  const tie = Date.parse("2026-09-01T00:00:00Z");
  owners.obj._claim("TIE000000000", "bb".repeat(32), tie);
  owners.obj._claim("TIE000000000", "aa".repeat(32), tie);
  owners.obj._claim("TIE000000000", "cc".repeat(32), tie);
  out.tieHolder = owners.obj._owner("TIE000000000");
  // a stored birth whose date says nothing is no birth
  const undated = await newKey();
  store.set(`born:${undated.pubHex}`, JSON.stringify({ v: 4, issued_at: "soon", method: "pow" }));
  out.undated = await check(undated, "ugo");
  // no birth, no code
  out.unborn = await check(await newKey(), "una");
  return out;
}

// -------------------------------------------------------------------------
// reserve: the share of the stamp and directory ceilings kept for nodes already known
// -------------------------------------------------------------------------

async function reserveShare() {
  const out = {};
  const known = await newKey(), newcomer = await newKey();
  await issue(known, { ip: "198.51.100.140" });
  await issue(newcomer, { ip: "198.51.100.141" });
  // a node already known: its certificate is older than a week
  const rec = JSON.parse(store.get(`born:${known.pubHex}`));
  rec.issued_at = new Date(Date.now() - 8 * DAY_MS).toISOString().replace(/\.\d{3}Z$/, "Z");
  store.set(`born:${known.pubHex}`, JSON.stringify(rec));
  // the newcomers' share of the stamp ceiling spent: newcomers wait, the known stamp
  preset("stamp:new:", 37500);
  out.stampNewcomer = (await stamp(newcomer, [root(9001)], { ip: "198.51.100.141" })).status;
  out.stampKnown = (await stamp(known, [root(9002)], { ip: "198.51.100.140" })).status;
  preset("stamp:all:", 50000);
  out.stampKnownAtCeiling = (await stamp(known, [root(9003)], { ip: "198.51.100.140" })).status;
  unset("stamp:new:"); unset("stamp:all:");
  // the lookups' ceiling likewise: a filled lookup ceiling would stop the stamps before them
  preset("lookup:new:", 187500);
  out.lookupNewcomer = (await stamp(newcomer, [root(9004)], { ip: "198.51.100.141" })).status;
  out.lookupKnown = (await stamp(known, [root(9005)], { ip: "198.51.100.140" })).status;
  unset("lookup:new:");
  out.newcomerAfter = (await stamp(newcomer, [root(9006)], { ip: "198.51.100.141" })).status;
  // the directory's day
  const ts = () => Math.floor(Date.now() / 1000);
  const reg = async (key, ip, t = ts()) => call("POST", "/node-register", {
    pubkey: key.pubHex, port: 20000, capabilities: ["sync"], ts: t,
    signature: await sign(key, `sautium-directory:v1:20000:sync:${t}`),
  }, { ip });
  presetIn(dirDb, "directory:new:", 37500);
  out.dirNewcomer = (await reg(newcomer, "203.0.113.141")).status;
  out.dirKnown = (await reg(known, "203.0.113.140")).status;
  out.newCountedForKnown = countIn(dirDb, "directory:new:") === 37500;
  dirDb.prepare("DELETE FROM counters WHERE key = 'directory:new:'").run();
  const older = await directory.stub.fetch("https://object/register", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ pubkey: "ab".repeat(32), host: "192.0.2.88", fam: 4, port: 20000, caps: "sync",
                           addr: "4.older", now: Date.now() }),
  });
  out.olderCallCountsAsNew = older.status === 200 && countIn(dirDb, "directory:new:") === 1;
  return out;
}

// -------------------------------------------------------------------------
// smallorder: a key anyone can sign for is no key
// -------------------------------------------------------------------------

async function smallOrder() {
  // The identity point's encoding, and a signature every message verifies
  // under it with a verifier that does not refuse it.
  const identity = "01" + "00".repeat(31);
  const trivial = "01" + "00".repeat(63);
  const out = {};
  const ys = ["0000000000000000000000000000000000000000000000000000000000000000",
              "0100000000000000000000000000000000000000000000000000000000000000",
              "26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc05",
              "c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac037a",
              "ecffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
              "edffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
              "eeffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f"];
  const withSign = (y) => y.slice(0, 62) + (parseInt(y.slice(62), 16) | 0x80).toString(16).padStart(2, "0");
  out.everyEncoding = ys.flatMap((y) => [y, withSign(y), y.toUpperCase()]).map(workerModule.isSmallOrderKey);
  out.ordinaryKeys = [(await newKey()).pubHex, "02" + "00".repeat(31), "ef" + "ff".repeat(30) + "7f"]
    .map(workerModule.isSmallOrderKey);
  out.birth = await call("POST", "/birth-certificate", { pubkey_hex: identity, signature: trivial });
  out.signedHigh = await call("POST", "/birth-certificate",
                              { pubkey_hex: "ec" + "ff".repeat(30) + "ff", signature: trivial });
  out.certified = store.has(`born:${identity}`);
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
  const addrsBefore = new Set(counterKeys("birth:addr:"));
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
  preset(counterKeys("birth:addr:").find((key) => !addrsBefore.has(key)), 49);
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
  if (dayKey) presetIn(dirDb, dayKey, 480);
  const dayRowsBefore = nodes();
  out.addrDayCap = (await reg(k5, "203.0.113.230", 20000, ts() + 4)).status;
  out.rowsUnchangedByDayCap = JSON.stringify(nodes()) === JSON.stringify(dayRowsBefore);
  presetIn(dirDb, "directory:all:", 50000);
  const ceiling = await reg(k5, "203.0.113.231", 20000, ts() + 5);
  out.dayCeiling = ceiling.status;
  out.dayCeilingRetryAfter = ceiling.headers["retry-after"] ?? null;
  dirDb.prepare("DELETE FROM counters WHERE key = 'directory:all:'").run();
  // a day's registrations per key: a listed volunteer past its own day is
  // refused, and its network's others are not
  const k6 = await newKey(), k7 = await newKey();
  await issue(k6, { ip: "198.51.100.231" });
  await issue(k7, { ip: "198.51.100.232" });
  await reg(k6, "2001:db8:aa:1::1");
  presetIn(dirDb, `directory:key:${k6.pubHex}`, 30);
  out.keyDayCap = (await reg(k6, "2001:db8:aa:1::1", 20000, ts() + 6)).status;
  out.neighbourAtKeyCap = (await reg(k7, "2001:db8:aa:2::1")).status;
  // a registration from an older Worker, with no address to count, is
  // refused before anything is written
  const writtenBefore = JSON.stringify([nodes(), keysIn(dirDb, "")]);
  const older = await directory.stub.fetch("https://object/register", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ pubkey: "cc".repeat(32), host: "192.0.2.77", fam: 4, port: 20000, caps: "sync",
                           now: Date.now() }),
  });
  out.olderWorker = [older.status, await older.json()];
  out.olderWorkerWrote = JSON.stringify([nodes(), keysIn(dirDb, "")]) !== writtenBefore;
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
  // a read of the invite family: the owners' backfill takes no step and no
  // stored birth claims under the brake
  const ownersOf = () => JSON.stringify([
    limiterShard("owners").db.prepare("SELECT * FROM owners ORDER BY digest").all(),
    limiterShard("owners").db.prepare("SELECT * FROM meta").all()]);
  const ownersBefore = ownersOf();
  const kc = await inviteOf(k, "kim");
  out.checkPaused = await call("GET", `/check-email?invite_code=${encodeURIComponent(kc)}&email=kim%40example.org`
    + `&public_key_hex=${k.pubHex}&signature=${await sign(k, `check-email:${kc}:kim@example.org`)}`,
                                undefined, { useEnv: paused });
  out.ownersUntouched = ownersOf() === ownersBefore;
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
  // the owners' backfill begun, then the brake in the objects' env: its
  // alarm takes no step, lists nothing and waits a minute; lifted, it goes on
  for (let i = 0; i < 600; i++) {
    store.set(`born:${hex(webcrypto.getRandomValues(new Uint8Array(32)))}`,
              JSON.stringify({ born_at: "2026-07-05T10:11:12Z", sig: "ab".repeat(64) }));
  }
  const owners = limiterShard("owners");
  const metaOf = () => owners.db.prepare("SELECT v FROM meta WHERE k = 'owners_backfill'").get()?.v ?? null;
  const check = async (key, name, opts) => {
    const c = await inviteOf(key, name);
    return call("GET", `/check-email?invite_code=${encodeURIComponent(c)}&email=x%40example.org`
      + `&public_key_hex=${key.pubHex}&signature=${await sign(key, `check-email:${c}:x@example.org`)}`, undefined, opts);
  };
  out.firstStep = (await check(k, "kim")).status;
  out.cursorAfterFirstStep = ![null, "done"].includes(metaOf());
  limiterEnv.WRITES_PAUSED = "1";
  const [lists, cursor, at] = [kvState.lists, metaOf(), Date.now()];
  await owners.fire();
  out.alarmUnderBrake = { lists: kvState.lists - lists, cursorKept: metaOf() === cursor,
                          waitsMs: owners.alarm() - at };
  limiterEnv.WRITES_PAUSED = undefined;
  for (let steps = 0; owners.alarm() !== null && steps < 5; steps++) await owners.fire();
  out.backfillDoneAfterBrake = metaOf() === "done";
  // past the backfill, an elder whose digest a later key holds: under the
  // brake its first use claims nothing; lifted, it holds its code
  const elder = await newKey();
  store.set(`born:${elder.pubHex}`, JSON.stringify({ v: 4, issued_at: "2026-05-01T00:00:00Z", method: "pow",
    difficulty: 32, params_version: 1, sig_v4: "cd".repeat(64) }));
  const elderDigest = (await inviteOf(elder, "x")).split("#")[1].replace(/-/g, "");
  owners.db.prepare("INSERT INTO owners (digest, pubkey, issued) VALUES (?, ?, ?)")
    .run(elderDigest, "dd".repeat(32), Date.parse("2026-10-30T00:00:00Z"));
  const beforeElder = ownersOf();
  out.elderUnderBrake = await check(elder, "eden", { useEnv: paused });
  out.elderClaimedNothing = ownersOf() === beforeElder;
  out.elderAfterBrake = (await check(elder, "eden")).status;
  out.elderHolds = owners.db.prepare("SELECT pubkey FROM owners WHERE digest = ?").get(elderDigest).pubkey
    === elder.pubHex;
  // a birth's claim its instance keeps waits out the brake as well
  limiterEnv.LIMITER = { idFromName: (n) => n, get: (n) => n === "owners"
    ? { fetch: async () => { throw new Error("Durable Object is overloaded"); } } : limiterShard(n).stub };
  await issue(await newKey());
  limiterEnv.LIMITER = limiter.namespace;
  const births = limiterShard("birth");
  const owed = () => births.db.prepare("SELECT count(*) AS n FROM owed").get().n;
  limiterEnv.WRITES_PAUSED = "1";
  const [beforeOwed, atOwed] = [ownersOf(), Date.now()];
  await births.fire();
  out.owedUnderBrake = { ownersUntouched: ownersOf() === beforeOwed, kept: owed(), waitsMs: births.alarm() - atOwed };
  limiterEnv.WRITES_PAUSED = undefined;
  await births.fire();
  out.owedAfterBrake = owed();
  return out;
}

// -------------------------------------------------------------------------
// names: the name a key is held to, and how long
// -------------------------------------------------------------------------

async function nameRetention() {
  const out = {};
  const k = await newKey();
  await issue(k, { ip: "198.51.100.80" });
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
  const heldOf = () => hasTable(oldDb, "held") ? oldDb.prepare("SELECT n FROM held").get().n : null;
  const upgraded = {
    held: heldOf(),
    indexes: oldDb.prepare("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'mail' AND sql IS NOT NULL ORDER BY name")
      .all().map((r) => r.name),
    triggers: oldDb.prepare("SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'mail' ORDER BY name")
      .all().map((r) => r.name),
    rows: oldDb.prepare("SELECT count(*) AS n FROM mail").get().n,
  };
  objectOf(workerModule.MasterMailbox, oldDb);                 // its next start counts nothing again
  upgraded.heldAfterRestart = heldOf();
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
  // a drop whose step fails after its writes leaves nothing of them
  const countersNow = () => JSON.stringify(mailDb.prepare("SELECT key, n FROM counters ORDER BY key").all());
  const [heldPre, rowsPre, countersPre] = [held(), rows(), countersNow()];
  const exec = mailbox.obj.sql.exec;
  mailbox.obj.sql.exec = (query, ...params) => {
    const result = exec(query, ...params);
    if (/^\s*INSERT INTO mail\b/.test(query)) throw new Error("storage failed after the write");
    return result;
  };
  const failed = await drop(b, 16, "198.51.100.123");
  mailbox.obj.sql.exec = exec;
  out.failedAfterWrite = [failed.status, failed.body];
  out.failedAfterWriteLeft = held() !== heldPre || rows() !== rowsPre || countersNow() !== countersPre;
  // the count of mail held follows the rows whoever writes them: this code,
  // or an older Worker deployed over it
  const exact = () => held() === rows();
  out.heldExact = [exact()];
  mailDb.prepare("UPDATE mail SET expires_at = ? WHERE id = (SELECT min(id) FROM mail)").run(Date.now() - 1);
  await drop(b, 17, "198.51.100.124");                       // its expiry runs first
  out.heldExact.push(exact());
  mailDb.prepare(`INSERT INTO mail (uuid, sender, encrypted, ts, received_at, expires_at)
                  VALUES ('older-1', 'x', 'y', 't', ?, ?)`).run(Date.now(), Date.now() + 86400e3);
  out.heldExact.push(exact());
  mailDb.prepare("DELETE FROM mail WHERE id IN (SELECT id FROM mail ORDER BY id LIMIT 2)").run();
  out.heldExact.push(exact());
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
// told: an acceptance's inviter is mailed once
// -------------------------------------------------------------------------

async function toldOnce() {
  // Each case its own inviter, acceptor and addresses, so that no notify
  // cap (three a day per key, per mailbox, per address) decides one.
  const out = {};
  const invites = limiterShard("invites");
  let n = 0;
  async function invited() {
    n += 1;
    const [ip, theirIp, name] = [`198.51.100.${120 + n}`, `198.51.100.${160 + n}`, `teo${n}`];
    const inviter = await newKey(), acceptor = await newKey();
    await issue(inviter, { ip });
    await verifyEmail(inviter, name, `${name}@example.org`, { ip });
    await invite(inviter, name, `guest${n}@example.net`, { ip });
    const token = tokenIn(mails[mails.length - 1]);
    await issue(acceptor, { ip: theirIp });
    const code = await inviteOf(inviter, name);
    return {
      id: await inviteIdOf(token), code,
      go: (opts = {}) => accept(acceptor, `ada${n}`, code, { ip: theirIp, token, ...opts }),
      told: (from) => mails.slice(from).filter((m) => m.to[0] === `${name}@example.org`).length,
    };
  }
  // the invites instance failing on one path only, as an overloaded object
  const failingOn = (path) => ({ ...env, LIMITER: { idFromName: (x) => x, get: (x) => x !== "invites"
    ? limiterShard(x).stub
    : { fetch: (url, init) => String(url).endsWith(path)
        ? Promise.reject(new Error("Durable Object is overloaded")) : limiterShard(x).stub.fetch(url, init) } } });
  let sent;
  // a mail that failed is sent at the acceptor's retry, once
  let c = await invited();
  mailState.down = true;
  out.toldFirst = (await c.go()).body.notified;
  mailState.down = false;
  sent = mails.length;
  out.toldRetry = (await c.go()).body.notified;
  out.toldAgain = (await c.go()).body.notified;
  out.mailsByRetries = c.told(sent);
  // one acceptor's two first acceptances at once: one mail
  c = await invited();
  sent = mails.length;
  out.atOnce = (await Promise.all([c.go(), c.go()])).map((r) => r.status);
  out.mailsAtOnce = c.told(sent);
  // a failed mail, then two retries at once: one mail
  c = await invited();
  mailState.down = true;
  await c.go();
  mailState.down = false;
  sent = mails.length;
  out.retriesAtOnce = (await Promise.all([c.go(), c.go()])).map((r) => r.status);
  out.mailsByRetriesAtOnce = c.told(sent);
  // the mail sent, its record failing: the answer is the acceptance's, and
  // the right still held keeps a retry from mailing again
  c = await invited();
  sent = mails.length;
  out.recordFailed = await c.go({ useEnv: failingOn("/invite/mailed") });
  out.retryAfterRecordFailed = (await c.go()).body.notified;
  out.mailsAfterRecordFailed = c.told(sent);
  // a mail that failed, its right not given back: a retry mails only once
  // the right has run out
  c = await invited();
  mailState.down = true;
  out.releaseFailed = (await c.go({ useEnv: failingOn("/invite/mailed") })).status;
  mailState.down = false;
  sent = mails.length;
  out.retryWhileHeld = (await c.go()).body.notified;
  invites.db.prepare("UPDATE invites SET mailing = ? WHERE id = ?").run(Date.now() - 1, c.id);
  out.retryAfterLease = (await c.go()).body.notified;
  out.mailsAfterLease = c.told(sent);
  // the verified record under the inviter's code naming another key: the
  // right to mail is nobody's, and no address is mailed
  c = await invited();
  const other = await newKey();
  store.set(`verified:${c.code.replace("#", ":")}`, JSON.stringify({
    email: "someone-else@example.org", pubkey: other.pubHex, born_at: "2026-11-01T00:00:00Z",
    verified_at: "2026-11-01T00:00:00Z" }));
  sent = mails.length;
  out.anothersRecord = (await c.go()).body.notified;
  out.mailsToAnothersRecord = mails.slice(sent).filter((m) => m.to[0] === "someone-else@example.org").length;
  // the notify instance failing after the right was taken: the answer is a
  // 503, and the right is given back for the retry, which mails
  c = await invited();
  const notifyDown = { ...env, LIMITER: { idFromName: (x) => x, get: (x) => x === "notify"
    ? { fetch: () => Promise.reject(new Error("Durable Object is overloaded")) } : limiterShard(x).stub } };
  out.notifyDown = (await c.go({ useEnv: notifyDown })).status;
  sent = mails.length;
  out.retryAfterNotifyDown = (await c.go()).body.notified;
  out.mailsAfterNotifyDown = c.told(sent);
  // a right whose lease ran out is another call's: the first call's give-back
  // leaves the second's lease as it is
  c = await invited();
  const step = async (path, body) => (await (await invites.stub.fetch(`https://object${path}`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })).json());
  const row = invites.db.prepare("SELECT inviter, inviter_code FROM invites WHERE id = ?").get(c.id);
  const acceptance = { id: c.id, acceptor: "ab".repeat(32), acceptor_code: "x#AAAA-BBBB-CCCC",
                       their_code: row.inviter_code, statement_ts: nowS(), signature: "00".repeat(64),
                       mailable: row.inviter };
  const first = await step("/invite/accept", { ...acceptance, now: Date.now() });
  const second = await step("/invite/accept", { ...acceptance, now: first.lease + 1 });
  await step("/invite/mailed", { id: c.id, lease: first.lease, sent: false });
  out.leases = { firstHeld: first.mail, secondHeld: second.mail,
                 secondKept: Number(invites.db.prepare("SELECT mailing FROM invites WHERE id = ?").get(c.id).mailing)
                   === second.lease };
  return out;
}

// -------------------------------------------------------------------------
// owed: the claims births leave in their instance, handed over
// -------------------------------------------------------------------------

const birthOf = async (key) => call("POST", "/birth-certificate", {
  pubkey_hex: key.pubHex, signature: await sign(key, `birth:${key.pubHex}`) });
const holdsDigest = (key) => limiterShard("owners").db
  .prepare("SELECT count(*) AS n FROM owners WHERE pubkey = ?").get(key.pubHex).n === 1;
const owedRows = () => limiterShard("birth").db.prepare("SELECT count(*) AS n FROM owed").get().n;
const ownersOnly = (fetch) => ({ idFromName: (n) => n, get: (n) => n === "owners" ? { fetch } : limiterShard(n).stub });

async function owedClaims() {
  const out = {};
  const births = limiterShard("birth");
  // a claim owed while a handover is out stays for the next one
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  limiterEnv.LIMITER = ownersOnly(async (url, init) => { await gate; return limiterShard("owners").stub.fetch(url, init); });
  const [k1, k2] = [await newKey(), await newKey()];
  await birthOf(k1);
  const handing = births.fire();
  await birthOf(k2);
  release();
  await handing;
  limiterEnv.LIMITER = limiter.namespace;
  out.afterFirstHandover = { k1: holdsDigest(k1), k2: holdsDigest(k2), owed: owedRows(),
                             again: births.alarm() !== null };
  await births.fire();
  out.afterSecond = { k1: holdsDigest(k1), k2: holdsDigest(k2), owed: owedRows(), alarm: births.alarm() };
  // more than a page: every claim lands, a page a handover
  const owe = (pubkey) => births.stub.fetch("https://object/owner/owe", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ digest: pubkey.slice(0, 12).toUpperCase(), pubkey, issued: Date.now(), now: Date.now() }) });
  const many = Array.from({ length: 600 }, () => hex(webcrypto.getRandomValues(new Uint8Array(32))));
  for (const pubkey of many) await owe(pubkey);
  let handovers = 0;
  while (births.alarm() !== null && handovers < 5) { await births.fire(); handovers += 1; }
  const held = limiterShard("owners").db.prepare(
    `SELECT count(*) AS n FROM owners WHERE pubkey IN (${many.map(() => "?").join(",")})`).get(...many).n;
  out.page = { handovers, held, owed: owedRows() };
  // a claim owed while a failed handover waits its minute waits with it
  limiterEnv.LIMITER = ownersOnly(() => Promise.reject(new Error("Durable Object is overloaded")));
  await birthOf(await newKey());
  await births.fire();
  const waiting = births.alarm();
  await birthOf(await newKey());
  out.secondOweLeavesTheWait = births.alarm() === waiting && waiting - Date.now() > 50_000;
  // an answer that is not the claims' taking is no handover
  limiterEnv.LIMITER = ownersOnly(async () => new Response(JSON.stringify({ ok: false }), { status: 500 }));
  await births.fire();
  out.refusedAnswer = { owed: owedRows(), waits: births.alarm() - Date.now() > 50_000 };
  limiterEnv.LIMITER = limiter.namespace;
  return out;
}

async function owedHang() {
  // Run with --owed-timeout: an owners instance that never answers holds a
  // handover no longer than that, then the claims wait their minute.
  const births = limiterShard("birth");
  limiterEnv.LIMITER = ownersOnly(() => new Promise(() => {}));
  const k = await newKey();
  await birthOf(k);
  const started = Date.now();
  await births.fire();
  const out = { tookMs: Date.now() - started, owed: owedRows(), waits: births.alarm() - Date.now() > 50_000 };
  limiterEnv.LIMITER = limiter.namespace;
  await births.fire();
  out.landed = holdsDigest(k) && owedRows() === 0;
  return out;
}

// -------------------------------------------------------------------------
// shards: one route's limiter failing leaves the others working
// -------------------------------------------------------------------------

async function shardFailure() {
  // A namespace whose instance `name` fails on `path` (on every path when
  // none is given) the way an overloaded object does, or never answers.
  const failing = (name, path = null, hang = false) => ({ idFromName: (n) => n, get: (n) => n !== name
    ? limiterShard(n).stub
    : { fetch: (url, init) => (path === null || String(url).endsWith(path))
        ? (hang ? new Promise(() => {}) : Promise.reject(new Error("Durable Object is overloaded")))
        : limiterShard(n).stub.fetch(url, init) } });
  const holds = (key) => limiterShard("owners").db
    .prepare("SELECT count(*) AS n FROM owners WHERE pubkey = ?").get(key.pubHex).n === 1;
  const births = limiterShard("birth");
  const owed = () => births.db.prepare("SELECT count(*) AS n FROM owed").get().n;
  const out = {};
  const useEnv = { ...env, LIMITER: failing("requests") };    // a pickup by a key with no invite kept
  const k = await newKey();
  out.birth = (await issue(k, { useEnv })).status;
  out.pickup = (await pending(k, "pia", { useEnv })).status;
  // the add's store failing (or an object still on the code before adds):
  // the add is answered, and its owner mailed, as before adds were kept
  const owner = await newKey(), adder = await newKey();
  await issue(owner, { ip: "198.51.100.70" }); await issue(adder, { ip: "198.51.100.71" });
  await verifyEmail(owner, "olena", "olena@example.org", { ip: "198.51.100.70" });
  const sentBefore = mails.length;
  out.addStoreDown = await accept(adder, "artem", await inviteOf(owner, "olena"),
                                  { ip: "198.51.100.71", useEnv: { ...env, LIMITER: failing("invites", "/add/put") } });
  out.addStoreDownMails = mails.slice(sentBefore).filter((m) => m.to[0] === "olena@example.org").length;
  // a birth never calls the owners instance: one that never answers holds
  // no birth up, and the claim comes from the birth's own instance
  const k2 = await newKey();
  out.birthOwnersHang = (await issue(k2, { useEnv: { ...env, LIMITER: failing("owners", null, true) } })).status;
  out.claimedFromBirth = holds(k2);
  // the owners instance failing: the claim waits in the birth instance, a
  // minute at a time, and lands when that instance answers again, whether
  // or not the key ever calls an invite route
  limiterEnv.LIMITER = failing("owners");
  const k3 = await newKey();
  const before = Date.now();
  out.birthOwnersDown = (await issue(k3, { useEnv: { ...env, LIMITER: failing("owners") } })).status;
  out.owedWhileDown = owed();
  out.retryAfterMs = births.alarm() - before;
  limiterEnv.LIMITER = limiter.namespace;
  await births.fire();
  out.claimedLater = holds(k3);
  out.owedAfter = owed();
  out.alarmAfter = births.alarm();
  // the birth's own instance unable to keep the claim: the birth is
  // answered, and the key claims at its first invite route
  const k4 = await newKey();
  out.oweFails = (await issue(k4, { useEnv: { ...env, LIMITER: failing("birth", "/owner/owe") } })).status;
  out.claimedAfterOweFailed = holds(k4);
  const c4 = await inviteOf(k4, "otto");
  out.firstUse = (await call("GET", `/check-email?invite_code=${encodeURIComponent(c4)}&email=o%40example.org`
    + `&public_key_hex=${k4.pubHex}&signature=${await sign(k4, `check-email:${c4}:o@example.org`)}`)).status;
  out.claimedAtFirstUse = holds(k4);
  return out;
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
  drops: mailboxDrops, armed: armedStamps, vectors: inviteVectors, owners: inviteOwners,
  reserve: reserveShare, smallorder: smallOrder, told: toldOnce, owed: owedClaims, owedhang: owedHang,
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
    if (req.url === "/__harness/mails") {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify(mails));
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
      await settleBirths();
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
