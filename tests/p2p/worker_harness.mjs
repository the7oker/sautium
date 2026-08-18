// Drives the REAL worker/verify.js fetch handler in Node with an in-memory KV
// and a throwaway authority key, so the Python mirrors can be checked against
// certificates the Worker code actually emits (see test_birth_cert.py).
//
// usage: node worker_harness.mjs <authority_seed_hex> <email_pepper>
// prints one JSON object on stdout.
import { readFileSync, writeFileSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname, resolve } from "node:path";
import { pathToFileURL, fileURLToPath } from "node:url";
import { webcrypto } from "node:crypto";
import { DatabaseSync } from "node:sqlite";

const subtle = webcrypto.subtle;
const [authoritySeedHex, emailPepper] = process.argv.slice(2);
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
async function sign(key, message) {
  return hex(new Uint8Array(await subtle.sign("Ed25519", key.priv, new TextEncoder().encode(message))));
}
async function sha256Hex(text) {
  return hex(new Uint8Array(await subtle.digest("SHA-256", new TextEncoder().encode(text))));
}

const authority = await keyFromSeed(authoritySeedHex);

// Swap the pinned authority for the test key, then import the module.
const src = readFileSync(workerPath, "utf-8");
const swapped = src.replace(
  /const TRUSTED_AUTHORITIES = \[\n  "[0-9a-f]{64}",\n\];/,
  `const TRUSTED_AUTHORITIES = [\n  "${authority.pubHex}",\n];`,
);
if (swapped === src) throw new Error("TRUSTED_AUTHORITIES pattern not found");
const tmp = join(mkdtempSync(join(tmpdir(), "sautium-worker-")), "verify.mjs");
writeFileSync(tmp, swapped);
const workerModule = await import(pathToFileURL(tmp).href);
const worker = workerModule.default;

// The birth-ledger Durable Object, run in-process on node:sqlite through the
// same class the Worker exports — the SQL is exercised for real, only the
// storage handle is emulated.
const db = new DatabaseSync(":memory:");
const sqlAdapter = {
  exec(query, ...params) {
    const rows = db.prepare(query).all(...params);
    return { toArray: () => rows, one: () => rows[0] };
  },
};
const ledger = new workerModule.BirthLedger(
  { storage: { sql: sqlAdapter }, blockConcurrencyWhile: (fn) => fn() }, {});
const ledgerStub = { fetch: (url, init) => ledger.fetch(new Request(url, init)) };

const store = new Map();
const env = {
  BIRTH_SIGNING_KEY: authoritySeedHex,
  EMAIL_PEPPER: emailPepper,
  IP_PEPPER: "test-ip-pepper",
  BIRTH_LEDGER: { idFromName: (name) => name, get: () => ledgerStub },
  RATE_LIMITS: {
    async get(k, type) {
      const v = store.get(k);
      if (v === undefined) return null;
      return type === "json" ? JSON.parse(v) : v;
    },
    async put(k, v) { store.set(k, v); },
    async delete(k) { store.delete(k); },
  },
};

async function call(method, path, body, { ip = "203.0.113.7", cf } = {}) {
  const req = new Request(`https://worker.test${path}`, {
    method,
    headers: { "Content-Type": "application/json", "CF-Connecting-IP": ip },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (cf) req.cf = cf;                       // request.cf as the edge would set it
  const res = await worker.fetch(req, env);
  return { status: res.status, body: await res.json() };
}

// --- subject 1: fresh identity, then email upgrade ---
const s1 = await keyFromSeed(hex(webcrypto.getRandomValues(new Uint8Array(32))));
const issue1 = await call("POST", "/birth-certificate", {
  pubkey_hex: s1.pubHex, signature: await sign(s1, `birth:${s1.pubHex}`),
});
const issue1Again = await call("POST", "/birth-certificate", {
  pubkey_hex: s1.pubHex, signature: await sign(s1, `birth:${s1.pubHex}`),
});
const read1 = await call("GET", `/birth-certificate?pubkey=${s1.pubHex}`);

const pubDigest = hex(new Uint8Array(await subtle.digest("SHA-256", s1.pubBytes))).slice(0, 12).toUpperCase();
const inviteCode = `alice#${pubDigest.slice(0, 4)}-${pubDigest.slice(4, 8)}-${pubDigest.slice(8, 12)}`;
const email = "Alice.Example+tag@GoogleMail.com";
const code = "ABCD1234";
store.set(`emailcode:${inviteCode.replace("#", ":")}`,
  JSON.stringify({ email, hash: await sha256Hex(code) }));
const register = await call("POST", "/register-email", {
  invite_code: inviteCode, email, public_key_hex: s1.pubHex,
  signature: await sign(s1, `register:${inviteCode}:${email}`),
  code, birth_cert: issue1.body,
});
const read1AfterEmail = await call("GET", `/birth-certificate?pubkey=${s1.pubHex}`);

// A second identity verifying the SAME mailbox (dots/tag/case differ) must
// get the same email_token — that equality is the whole point of the token.
const s3 = await keyFromSeed(hex(webcrypto.getRandomValues(new Uint8Array(32))));
const issue3 = await call("POST", "/birth-certificate", {
  pubkey_hex: s3.pubHex, signature: await sign(s3, `birth:${s3.pubHex}`),
});
const pubDigest3 = hex(new Uint8Array(await subtle.digest("SHA-256", s3.pubBytes))).slice(0, 12).toUpperCase();
const inviteCode3 = `bob#${pubDigest3.slice(0, 4)}-${pubDigest3.slice(4, 8)}-${pubDigest3.slice(8, 12)}`;
const email3 = "aliceexample@gmail.com";
store.set(`emailcode:${inviteCode3.replace("#", ":")}`,
  JSON.stringify({ email: email3, hash: await sha256Hex(code) }));
const register3 = await call("POST", "/register-email", {
  invite_code: inviteCode3, email: email3, public_key_hex: s3.pubHex,
  signature: await sign(s3, `register:${inviteCode3}:${email3}`),
  code, birth_cert: issue3.body,
});

// --- subject 2: legacy v1 record already in KV ---
const s2 = await keyFromSeed(hex(webcrypto.getRandomValues(new Uint8Array(32))));
store.set(`born:${s2.pubHex}`, JSON.stringify({ born_at: "2026-07-05T10:11:12Z", sig: "ab".repeat(64) }));
const legacyRead = await call("GET", `/birth-certificate?pubkey=${s2.pubHex}`);
const legacyRecord = JSON.parse(store.get(`born:${s2.pubHex}`));

// --- subject 6: a v2 EMAIL record from before the domain token existed ---
const s6 = await keyFromSeed(hex(webcrypto.getRandomValues(new Uint8Array(32))));
store.set(`born:${s6.pubHex}`, JSON.stringify({
  v: 2, issued_at: "2026-07-06T00:00:00Z", method: "email", difficulty: 32, params_version: 1,
  email_token: "ef".repeat(32), email_class: "other", sig_v2: "cd".repeat(64) }));
const legacyV2Read = await call("GET", `/birth-certificate?pubkey=${s6.pubHex}`);
const legacyV2Record = JSON.parse(store.get(`born:${s6.pubHex}`));
const pubDigest6 = hex(new Uint8Array(await subtle.digest("SHA-256", s6.pubBytes))).slice(0, 12).toUpperCase();
const inviteCode6 = `frank#${pubDigest6.slice(0, 4)}-${pubDigest6.slice(4, 8)}-${pubDigest6.slice(8, 12)}`;
const email6 = "frank@example.net";
store.set(`verified:${inviteCode6.replace("#", ":")}`,
  JSON.stringify({ email: email6, pubkey: s6.pubHex, born_at: "2026-07-06T00:00:00Z", verified_at: "2026-07-06T00:10:00Z" }));
const legacyV2Check = await call("GET", `/check-email?invite_code=${encodeURIComponent(inviteCode6)}&email=${encodeURIComponent(email6)}&public_key_hex=${s6.pubHex}&signature=${await sign(s6, `check-email:${inviteCode6}:${email6}`)}`);

// --- a disposable-domain registration for the class marker ---
const s4 = await keyFromSeed(hex(webcrypto.getRandomValues(new Uint8Array(32))));
const issue4 = await call("POST", "/birth-certificate", {
  pubkey_hex: s4.pubHex, signature: await sign(s4, `birth:${s4.pubHex}`),
});
const pubDigest4 = hex(new Uint8Array(await subtle.digest("SHA-256", s4.pubBytes))).slice(0, 12).toUpperCase();
const inviteCode4 = `carol#${pubDigest4.slice(0, 4)}-${pubDigest4.slice(4, 8)}-${pubDigest4.slice(8, 12)}`;
const email4 = "x@mailinator.com";
store.set(`emailcode:${inviteCode4.replace("#", ":")}`,
  JSON.stringify({ email: email4, hash: await sha256Hex(code) }));
const register4 = await call("POST", "/register-email", {
  invite_code: inviteCode4, email: email4, public_key_hex: s4.pubHex,
  signature: await sign(s4, `register:${inviteCode4}:${email4}`),
  code, birth_cert: issue4.body,
});

// check-email for an already-upgraded identity returns the email cert…
const check1 = await call("GET", `/check-email?invite_code=${encodeURIComponent(inviteCode)}&email=${encodeURIComponent(email)}&public_key_hex=${s1.pubHex}&signature=${await sign(s1, `check-email:${inviteCode}:${email}`)}`);
// …and for a pre-v2 identity (v1 born record + existing verified: record)
// it upgrades the certificate without a second code round.
const pubDigest2 = hex(new Uint8Array(await subtle.digest("SHA-256", s2.pubBytes))).slice(0, 12).toUpperCase();
const inviteCode2 = `dave#${pubDigest2.slice(0, 4)}-${pubDigest2.slice(4, 8)}-${pubDigest2.slice(8, 12)}`;
const email2 = "dave@example.org";
store.set(`verified:${inviteCode2.replace("#", ":")}`,
  JSON.stringify({ email: email2, pubkey: s2.pubHex, born_at: "2026-07-05T10:11:12Z", verified_at: "2026-07-05T10:20:00Z" }));
const check2 = await call("GET", `/check-email?invite_code=${encodeURIComponent(inviteCode2)}&email=${encodeURIComponent(email2)}&public_key_hex=${s2.pubHex}&signature=${await sign(s2, `check-email:${inviteCode2}:${email2}`)}`);
const legacyRecordAfterCheck = JSON.parse(store.get(`born:${s2.pubHex}`));
// a verified: record bound to ANOTHER pubkey must not upgrade anything
const s5 = await keyFromSeed(hex(webcrypto.getRandomValues(new Uint8Array(32))));
const pubDigest5 = hex(new Uint8Array(await subtle.digest("SHA-256", s5.pubBytes))).slice(0, 12).toUpperCase();
const inviteCode5 = `eve#${pubDigest5.slice(0, 4)}-${pubDigest5.slice(4, 8)}-${pubDigest5.slice(8, 12)}`;
store.set(`verified:${inviteCode5.replace("#", ":")}`,
  JSON.stringify({ email: email2, pubkey: s2.pubHex, born_at: "2026-07-05T10:11:12Z", verified_at: "2026-07-05T10:20:00Z" }));
const check5 = await call("GET", `/check-email?invite_code=${encodeURIComponent(inviteCode5)}&email=${encodeURIComponent(email2)}&public_key_hex=${s5.pubHex}&signature=${await sign(s5, `check-email:${inviteCode5}:${email2}`)}`);

// --- birth ledger (shadow): a burst from one /24 + one ASN ---
const burst = [];
for (let i = 0; i < 4; i++) {
  const k = await keyFromSeed(hex(webcrypto.getRandomValues(new Uint8Array(32))));
  const r = await call("POST", "/birth-certificate", {
    pubkey_hex: k.pubHex, signature: await sign(k, `birth:${k.pubHex}`),
  }, { ip: `198.51.100.${10 + i}`, cf: { asn: 64500, country: "UA" } });
  burst.push(r);
}
const ledgerRows = db.prepare("SELECT asn, cc, method, m_shadow, n_sub24, n_asn1, n_glob1, addr, n_addr24 FROM births ORDER BY rowid").all();

const stats = await call("GET", "/issuance-stats");
const badSig = await call("POST", "/birth-certificate", {
  pubkey_hex: s1.pubHex, signature: "00".repeat(64),
});

console.log(JSON.stringify({
  authority_pub: authority.pubHex,
  issue1, issue1Again, read1, register, read1AfterEmail,
  register3,
  legacyRead, legacyRecord,
  register4,
  check1, check2, legacyRecordAfterCheck, check5,
  legacyV2Read, legacyV2Record, legacyV2Check,
  burst, ledgerRows,
  stats, badSig,
  verified_record: JSON.parse(store.get(`verified:${inviteCode.replace("#", ":")}`)),
}));
