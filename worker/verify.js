/**
 * Sautium Email Verification, Invite & Birth Certificate Worker (Cloudflare Workers)
 *
 * Trusted server that:
 * 1. Verifies email ownership (server-side code flow — the Worker generates
 *    and checks the code; clients never see the expected value)
 * 2. Stores verified email records in KV: {email, pubkey, born_at, verified_at}
 * 3. Sends signed invite emails with verified sender info
 * 4. Issues identity (birth) certificates — the network's identity-age
 *    anchor and, since v2, the challenge for the identity proof-of-work (see
 *    docs/design/P2P-SYNC-INTEGRITY.md §§ "Birth certificates",
 *    "Proof-of-work certificates": cert = initial trust weight, never
 *    immunity; the Worker is a pure NOTARY — it signs facts, it verifies no
 *    work). Issuance is idempotent: the first request for a pubkey signs
 *    {pubkey, now, policy} into KV; every later request returns the stored
 *    certificate — recreating an account on another device (Argon2id
 *    login+password → same keypair) never changes the birth date. A
 *    successful email verification upgrades the same record to
 *    `method: email` (issued_at unchanged) and adds the peppered email token.
 *
 * All state-changing requests require Ed25519 signature verification.
 * Masha can't impersonate Alice because she can't sign with Alice's keys.
 *
 * KV key naming: invite codes contain '#', which is a URL fragment marker and
 * breaks wrangler CLI (list silently hides such keys, get truncates at '#').
 * Storage keys therefore use ':' in place of '#' (kvKey()); the invite code
 * itself is unchanged everywhere else in the product. Usernames are
 * restricted to [A-Za-z0-9_-]{3,32} at both boundaries (client account
 * creation and verifyInviteCode here), so the substitution is unambiguous.
 *
 * Deploy: wrangler deploy
 * Secrets: wrangler secret put RESEND_API_KEY
 *          wrangler secret put BIRTH_SIGNING_KEY   (64-char hex Ed25519 seed;
 *            offline backup lives in data/authority/master_signing.key on the
 *            master host)
 *          wrangler secret put IP_PEPPER           (data/authority/ip_pepper.key)
 *          wrangler secret put EMAIL_PEPPER        (data/authority/email_pepper.key;
 *            NEVER rotate — every email_token link across the network is
 *            keyed under it, the IP_HASH_VERSION=2 lesson)
 *
 * Endpoints:
 *   POST /send-verification    — generate + send verification code to email
 *   POST /register-email       — store verified email (requires code + birth cert);
 *                                the mailbox moves to this key, the previous
 *                                holder becomes the certificate's predecessor
 *   POST /send-invite          — send signed invite email with verified sender
 *   GET  /check-email          — check if email already verified for invite code
 *   POST /accept-invite        — accept an invite (stores reciprocal + notifies sender)
 *   GET  /pending-accepts      — poll for accepted invites (one-time pickup)
 *   POST /birth-certificate    — issue (or return existing) identity certificate
 *   GET  /birth-certificate    — public read of an issued certificate
 *   GET  /issuance-stats       — public issuance counters + policy + birth-ledger
 *                                aggregates (CT-lite)
 *   POST /mailbox              — drop an E2E-encrypted chat message for the
 *                                master while it is offline (sender-signed,
 *                                needs a birth certificate issued here)
 *   GET  /mailbox              — master-signed drain; DELETE /mailbox — ack
 *   GET  /mailbox/wake         — master-signed WebSocket: "mail" on arrival;
 *                                also records the master's current address
 *                                (egress IP + declared peer port) as the
 *                                bootstrap hint below
 *   POST /node-register        — a reachable volunteer node lists itself in
 *                                the capability directory (address from the
 *                                edge, port+caps inside the signature)
 *   GET  /node-hints           — K random fresh volunteers for a capability:
 *                                the DHT-less bootstrap stops funneling every
 *                                role onto the master
 *   GET  /master-hint          — the master's last known peer address, for
 *                                nodes whose DHT is dead (mobile carriers
 *                                throttle UDP): rides HTTPS/443, the one
 *                                transport every newborn provably has
 *   GET  /health               — health check
 *
 * Birth ledger (Durable Object BIRTH_LEDGER, SQLite): every first issuance is
 * recorded with its time, ASN, country and a peppered /24 token, scored
 * against the recent births that look like it, and stored with the
 * would-be difficulty multiplier. SHADOW ONLY until
 * ADAPTIVE_DIFFICULTY_ARMED flips: certificates keep the base difficulty,
 * the ledger just measures — a botnet is a mass event, and only the notary
 * sees the whole birth process (docs/design/P2P-SYNC-INTEGRITY.md
 * § "Defense strategy"; plan phase Ф2b). Pricing is per CLUSTER (same /24,
 * same ASN, then a mild capped global term), never a global switch — an
 * honest launch wave from one ISP must not pay for a cloud flood.
 */

const RATE_LIMIT_PER_IP = 20;
const RATE_LIMIT_PER_RECIPIENT = 10;
const RATE_LIMIT_WINDOW = 3600;
const ACCEPT_TTL_SECONDS = 30 * 24 * 3600; // 30 days — must match SENT_INVITES_TTL_DAYS on backend/desktop
const VERIFY_CODE_TTL_SECONDS = 15 * 60;
const FROM_EMAIL = "noreply@sautium.net";
const FROM_NAME = "Sautium";

// Birth certificate authority keys. MIRRORS backend/birth_authority.py and
// desktop/p2p/birth_cert.py — update all three together. [0] is the active
// signing key (its private half is the BIRTH_SIGNING_KEY secret); later
// entries would be co-authorities (accepted for verification, not signing).
const TRUSTED_AUTHORITIES = [
  "a9f40f70a796926828d894d4384655963ae5bdce38d2c502ede75792552d33cd",
];
// v2 (2026-08-17): {pubkey, issued_at, method: pow|email, difficulty,
// params_version, email_token, email_class}; v3 (2026-08-18) adds
// email_domain_token — the peppered DOMAIN of a verified mailbox, a
// similarity axis on its own ("50 identities on one rare domain") that the
// whole-address token cannot express; v4 (2026-08-18) adds predecessor —
// the pubkey that held this mailbox before (succession across a password
// change: the notary names the link, nodes carry standing AND bans over it;
// email records only, "" otherwise). Payload format is mirrored by
// canonical_payload() in the two Python files above. A version bump
// re-signs every record; a pow identity's proof (mined over the signature)
// goes stale and is re-mined by the node — the price of "challenge = the
// authority signature", acceptable pre-release, a grace policy later.
const BIRTH_CERT_VERSION = 4;
const SIG_FIELD = `sig_v${BIRTH_CERT_VERSION}`;
const CERT_METHODS = new Set(["pow", "email"]);
// Identity proof-of-work policy pinned into every certificate at issuance
// (desktop/p2p/identity_pow.py owns the parameters themselves). difficulty is
// the EXPECTED number of ~2 GiB Argon2id attempts — a continuous price scale.
// Golden-age level: ~45 s of background mining on a desktop; raising it later
// touches new births only, existing certificates keep the price they paid.
const POW_PARAMS_VERSION = 1;
const POW_DIFFICULTY = 32;
// Adaptive birth pricing: shadow (measure only) until flipped. When armed the
// certificate difficulty becomes POW_DIFFICULTY x shadowMultiplier(), capped
// at x8 — the cap bounds both a false positive (an honest wave mines a few
// minutes longer, and a newborn needs no proof for hours anyway) and the
// blast radius of a formula bug.
const ADAPTIVE_DIFFICULTY_ARMED = false;
const ADAPTIVE_MULT_CAP = 8;
const LEDGER_RETENTION_MS = 90 * 24 * 3600 * 1000;
// The shipped master node (MIRRORS desktop/p2p/master_node.py and
// backend/master_node.py — the only identity with a mailbox here). The
// mailbox de-specializes the master (P2P-SYNC-INTEGRITY.md § "Defense
// strategy"): a message for it that finds no route is parked here,
// ciphertext only (NaCl Box to the master's chat key — the Worker cannot
// read it), and drained when the master is back. Blocking the master
// achieves nothing; the master being offline costs nothing.
const MASTER_PUBKEY = "3aa2ae91bc41863468ea4df3346811bcb0f7e0d6a9644b931cfd628d81247042";
const MAIL_MAX_CHARS = 8192;             // encrypted payload (chat caps are far below)
const MAIL_PER_SENDER_DAY = 30;
const MAIL_PER_DAY = 5000;
const MAIL_CAP = 20000;                  // rows kept; oldest evicted beyond this
const MAIL_TTL_MS = 30 * 24 * 3600 * 1000;
const MAIL_IP_PER_HOUR = 60;
const MAIL_SIG_SKEW_S = 300;
// Capability directory (Ф16c): reachable volunteers re-register on their
// existing periodic cycles; an entry older than the TTL is dead. K keeps
// the answer a SAMPLE, not a map of the network.
const DIRECTORY_TTL_MS = 2 * 3600 * 1000;
const DIRECTORY_K = 5;
const DIRECTORY_CAPS = new Set(["sync", "mbdump", "relay", "mbslices"]);
const DIRECTORY_REG_PER_IP_HOUR = 12;
// email_class is a coarse, Worker-side hint (the node never sees the domain):
// disposable → reduced similarity weight, major → shared-provider (many honest
// users collide on it), other → custom/ISP domains.
const MAJOR_EMAIL_DOMAINS = new Set([
  "gmail.com", "outlook.com", "hotmail.com", "live.com", "msn.com",
  "yahoo.com", "ymail.com", "icloud.com", "me.com", "mac.com", "proton.me",
  "protonmail.com", "pm.me", "fastmail.com", "gmx.com", "gmx.de", "gmx.net",
  "web.de", "aol.com", "zoho.com", "mail.com", "yandex.com", "yandex.ru",
  "mail.ru", "ukr.net", "i.ua", "meta.ua", "email.ua", "seznam.cz", "wp.pl",
  "o2.pl", "onet.pl", "interia.pl", "orange.fr", "free.fr", "laposte.net",
  "libero.it", "t-online.de", "qq.com", "163.com", "126.com", "naver.com",
]);
const DISPOSABLE_EMAIL_DOMAINS = new Set([
  "mailinator.com", "guerrillamail.com", "guerrillamail.net", "sharklasers.com",
  "grr.la", "10minutemail.com", "10minutemail.net", "temp-mail.org",
  "tempmail.com", "tempmail.net", "tempr.email", "yopmail.com", "yopmail.fr",
  "throwawaymail.com", "getnada.com", "nada.email", "dispostable.com",
  "trashmail.com", "trashmail.me", "maildrop.cc", "fakeinbox.com", "mohmal.com",
  "emailondeck.com", "mintemail.com", "discard.email", "mailnesia.com",
  "spamgourmet.com", "mytemp.email", "tmpmail.org", "tmpmail.net",
  "burnermail.io", "moakt.com", "tempail.com", "crazymailing.com",
  "guerrillamailblock.com", "spam4.me", "mailcatch.com", "inboxkitten.com",
]);

// Timestamp-notary payload version. v2 binds the submitter's ip_hash into the
// signed root (accountability for who notarized a batch). Decoupled from
// BIRTH_CERT_VERSION — they were incidentally equal at v1 — and mirrored by
// TIMESTAMP_VERSION in backend/record_sig.py; bump both together.
const TIMESTAMP_VERSION = 2;

// How ip_hash is derived. v1 was uuid5 over a namespace shipped in this file —
// reversible by brute-forcing the IPv4 space, which published the submitter's
// address to every peer holding a sealed record. v2 is HMAC under IP_PEPPER:
// same equality (so Sybil merging by address still works), no search.
// A stamp whose ipv is older is re-issued on the next submission of that root,
// keeping its original date — authorship priority never regresses.
const IP_HASH_VERSION = 2;

// Enforced on account creation client-side; re-checked here because invite
// codes arrive from the network. Guarantees '#' appears exactly once in an
// invite code and keeps kvKey() substitution unambiguous.
const USERNAME_RE = /^[A-Za-z0-9_-]{3,32}$/;

export default {
  async fetch(request, env) {
    const corsHeaders = {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "POST, GET, DELETE, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type",
    };

    if (request.method === "OPTIONS") {
      return new Response(null, { headers: corsHeaders });
    }

    const url = new URL(request.url);

    try {
      if (url.pathname === "/health" && request.method === "GET") {
        return json({ status: "ok", service: "sautium-verify" }, corsHeaders);
      }

      if (url.pathname === "/send-verification" && request.method === "POST") {
        return await handleVerification(request, env, corsHeaders);
      }

      if (url.pathname === "/register-email" && request.method === "POST") {
        return await handleRegisterEmail(request, env, corsHeaders);
      }

      if (url.pathname === "/send-invite" && request.method === "POST") {
        return await handleInvite(request, env, corsHeaders);
      }

      if (url.pathname === "/check-email" && request.method === "GET") {
        return await handleCheckEmail(url, env, corsHeaders);
      }

      if (url.pathname === "/accept-invite" && request.method === "POST") {
        return await handleAcceptInvite(request, env, corsHeaders);
      }

      if (url.pathname === "/pending-accepts" && request.method === "GET") {
        return await handlePendingAccepts(url, env, corsHeaders);
      }

      if (url.pathname === "/mailbox" && request.method === "POST") {
        return await handleMailboxPut(request, env, corsHeaders);
      }
      if (url.pathname === "/mailbox" && (request.method === "GET" || request.method === "DELETE")) {
        return await handleMailboxDrain(request, url, env, corsHeaders);
      }
      if (url.pathname === "/mailbox/wake" && request.method === "GET") {
        return await handleMailboxWake(request, url, env, corsHeaders);
      }
      if (url.pathname === "/master-hint" && request.method === "GET") {
        return await handleMasterHint(env, corsHeaders);
      }
      if (url.pathname === "/node-register" && request.method === "POST") {
        return await handleNodeRegister(request, env, corsHeaders);
      }
      if (url.pathname === "/node-hints" && request.method === "GET") {
        return await handleNodeHints(url, env, corsHeaders);
      }

      if (url.pathname === "/birth-certificate" && request.method === "POST") {
        return await handleBirthCertificate(request, env, corsHeaders);
      }

      if (url.pathname === "/issuance-stats" && request.method === "GET") {
        return await handleIssuanceStats(env, corsHeaders);
      }
      if (url.pathname === "/birth-certificate" && request.method === "GET") {
        return await handleBirthCertificateGet(url, env, corsHeaders);
      }

      if (url.pathname === "/timestamp" && request.method === "POST") {
        return await handleTimestamp(request, env, corsHeaders);
      }

      return json({ error: "not found" }, corsHeaders, 404);
    } catch (e) {
      return json({ error: e.message }, corsHeaders, 500);
    }
  },
};

// -----------------------------------------------------------------------
// Birth certificates
// -----------------------------------------------------------------------

function birthPayload(pubkeyHex, rec) {
  // Fixed eleven-field shape; the three email fields and the predecessor are
  // empty for method:pow. Field values never contain ':' (hex, ISO seconds,
  // enum names, integers).
  return new TextEncoder().encode(
    `sautium-birth:v${BIRTH_CERT_VERSION}:${pubkeyHex}:${rec.issued_at}:` +
    `${rec.method}:${rec.difficulty}:${rec.params_version}:` +
    `${rec.email_token || ""}:${rec.email_class || ""}:${rec.email_domain_token || ""}:` +
    `${rec.predecessor || ""}`
  );
}

function isValidCertShape(cert) {
  if (!cert || cert.v !== BIRTH_CERT_VERSION) return false;
  if (!TRUSTED_AUTHORITIES.includes(cert.issuer)) return false;
  if (!isValidPubkeyHex(cert.pubkey)) return false;
  if (!CERT_METHODS.has(cert.method)) return false;
  if (typeof cert.issued_at !== "string" || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(cert.issued_at)) return false;
  if (!Number.isInteger(cert.difficulty) || cert.difficulty < 1) return false;
  if (!Number.isInteger(cert.params_version) || cert.params_version < 1) return false;
  if (cert.method === "email") {
    if (!/^[0-9a-f]{64}$/.test(cert.email_token || "")) return false;
    if (!["major", "other", "disposable"].includes(cert.email_class)) return false;
    // Domain token: 64 hex, or absent on a record migrated before the field
    // existed (recomputed on the next email touch — register/check-email).
    if (cert.email_domain_token && !/^[0-9a-f]{64}$/.test(cert.email_domain_token)) return false;
    if (cert.predecessor && (!isValidPubkeyHex(cert.predecessor) || cert.predecessor === cert.pubkey)) return false;
  } else if (cert.email_token || cert.email_class || cert.email_domain_token || cert.predecessor) {
    return false;
  }
  return true;
}

function nowIsoSeconds() {
  return new Date().toISOString().replace(/\.\d{3}Z$/, "Z");
}

function isValidPubkeyHex(pubkeyHex) {
  return typeof pubkeyHex === "string" && /^[0-9a-f]{64}$/.test(pubkeyHex);
}

async function birthSigningKey(env) {
  // Secret is the raw 32-byte Ed25519 seed in hex; WebCrypto imports private
  // keys as PKCS8, so wrap the seed in the fixed ASN.1 prefix for Ed25519.
  const seed = hexToBytes(env.BIRTH_SIGNING_KEY);
  const pkcs8Prefix = hexToBytes("302e020100300506032b657004220420");
  const pkcs8 = new Uint8Array(pkcs8Prefix.length + seed.length);
  pkcs8.set(pkcs8Prefix);
  pkcs8.set(seed, pkcs8Prefix.length);
  return await crypto.subtle.importKey(
    "pkcs8", pkcs8, { name: "Ed25519" }, false, ["sign"]
  );
}

async function verifyBirthCertificate(cert) {
  try {
    if (!isValidCertShape(cert)) return false;
    return await verifySignatureBytes(
      birthPayload(cert.pubkey, cert), cert.sig, cert.issuer
    );
  } catch {
    return false;
  }
}

async function signBirthRecord(env, pubkey, rec) {
  const key = await birthSigningKey(env);
  return bytesToHex(new Uint8Array(
    await crypto.subtle.sign("Ed25519", key, birthPayload(pubkey, rec))
  ));
}

function certFromRecord(pubkey, rec) {
  return {
    v: BIRTH_CERT_VERSION,
    pubkey,
    issued_at: rec.issued_at,
    method: rec.method,
    difficulty: rec.difficulty,
    params_version: rec.params_version,
    email_token: rec.email_token || null,
    email_class: rec.email_class || null,
    email_domain_token: rec.email_domain_token || null,
    predecessor: rec.predecessor || null,
    issuer: TRUSTED_AUTHORITIES[0],
    sig: rec[SIG_FIELD],
  };
}

async function loadBirthRecord(env, pubkey) {
  /**
   * The KV record behind `born:{pubkey}`, upgraded in place from the v1
   * shape {born_at, sig} on first touch: issued_at keeps the original
   * born_at (the age anchor never moves), method starts as pow under the
   * current policy. The v1 fields stay in the record so a Worker rollback
   * still serves the old certificate byte-for-byte.
   */
  const record = await env.RATE_LIMITS.get(`born:${pubkey}`, "json");
  if (!record) return null;
  if (record.v === BIRTH_CERT_VERSION && record[SIG_FIELD]) return record;
  // v1 {born_at, sig} → policy fields start as pow; v2/v3 → keep every field
  // (an email record keeps its tokens; the domain token arrives on the next
  // email touch; predecessor is empty — succession is only ever recorded at
  // a registration). Older signature fields stay for rollback.
  const upgraded = record.v >= 2 ? {
    ...record,
    v: BIRTH_CERT_VERSION,
    email_domain_token: record.email_domain_token || null,
    predecessor: record.predecessor || null,
  } : {
    ...record,
    v: BIRTH_CERT_VERSION,
    issued_at: record.born_at,
    method: "pow",
    difficulty: POW_DIFFICULTY,
    params_version: POW_PARAMS_VERSION,
    email_token: null,
    email_class: null,
    email_domain_token: null,
    predecessor: null,
  };
  upgraded[SIG_FIELD] = await signBirthRecord(env, pubkey, upgraded);
  await env.RATE_LIMITS.put(`born:${pubkey}`, JSON.stringify(upgraded));
  if (upgraded.method === "email" && upgraded.email_token) {
    await claimMailbox(env, upgraded.email_token, pubkey);   // backfill the owner index lazily
  }
  return upgraded;
}

async function claimMailbox(env, emailToken, pubkey, { takeOver = false } = {}) {
  /**
   * `mailbox:{email_token}` → the pubkey that currently holds this mailbox.
   * With takeOver (a fresh /register-email) the record moves to the new key
   * and the previous holder is returned as the successor's predecessor;
   * without it (backfill from check-email / migration) an existing owner is
   * left alone — the newest REGISTRATION owns the mailbox, not the newest
   * touch. Returns the previous holder when it differs from `pubkey`.
   */
  const key = `mailbox:${emailToken}`;
  const current = await env.RATE_LIMITS.get(key, "json");
  if (current && current.pubkey === pubkey) return null;
  if (current && !takeOver) return current.pubkey;
  await env.RATE_LIMITS.put(key, JSON.stringify({ pubkey, since: nowIsoSeconds() }));
  return current ? current.pubkey : null;
}

async function bumpIssuance(env, field) {
  // Best-effort daily counters (KV read-modify-write races only undercount).
  // Public via /issuance-stats: mass minting under a compromised authority
  // key would show up as certificates the notary never counted.
  const day = nowIsoSeconds().slice(0, 10);
  const stats = (await env.RATE_LIMITS.get("stats:issuance", "json")) || {};
  const row = stats[day] || { births: 0, email: 0 };
  row[field] = (row[field] || 0) + 1;
  stats[day] = row;
  for (const k of Object.keys(stats)) {
    if (k < new Date(Date.now() - 90 * 86400 * 1000).toISOString().slice(0, 10)) delete stats[k];
  }
  await env.RATE_LIMITS.put("stats:issuance", JSON.stringify(stats));
}

async function handleIssuanceStats(env, corsHeaders) {
  const stats = (await env.RATE_LIMITS.get("stats:issuance", "json")) || {};
  return json({
    policy: {
      cert_version: BIRTH_CERT_VERSION,
      pow_difficulty: POW_DIFFICULTY,
      pow_params_version: POW_PARAMS_VERSION,
      adaptive_armed: ADAPTIVE_DIFFICULTY_ARMED,
      adaptive_cap: ADAPTIVE_MULT_CAP,
    },
    days: stats,
    ledger: await ledgerCall(env, "/stats"),
  }, corsHeaders);
}

async function handleBirthCertificate(request, env, corsHeaders) {
  /**
   * Issue (or return the existing) birth certificate for an identity.
   *
   * Body: {pubkey_hex, signature}
   * Signature is over: "birth:{pubkey_hex}" — signed by the subject key
   * itself, so only the key's owner can trigger first issuance (keeps the
   * registry free of junk keys; reading an issued cert needs no signature).
   *
   * born_at is the FIRST issuance moment. Idempotent: later requests return
   * the stored certificate. Ed25519 is deterministic and born_at has second
   * precision, so even a same-second concurrent first issuance produces
   * byte-identical certificates — KV needs no transaction.
   */
  const body = await request.json();
  const { pubkey_hex, signature } = body;

  if (!pubkey_hex || !signature) {
    return json({ error: "missing fields" }, corsHeaders, 400);
  }

  const pubkey = pubkey_hex.toLowerCase();
  if (!isValidPubkeyHex(pubkey)) {
    return json({ error: "invalid pubkey" }, corsHeaders, 400);
  }

  const valid = await verifySignature(`birth:${pubkey}`, signature, pubkey);
  if (!valid) {
    return json({ error: "invalid signature" }, corsHeaders, 403);
  }

  const ip = request.headers.get("CF-Connecting-IP") || "unknown";
  const rateLimitResult = await checkRateLimit(env, ip, `birth:${pubkey}`);
  if (rateLimitResult) {
    return json({ error: rateLimitResult }, corsHeaders, 429);
  }

  let record = await loadBirthRecord(env, pubkey);
  if (!record) {
    const issuedAt = nowIsoSeconds();
    const verdict = await ledgerBirth(env, request, pubkey, issuedAt);
    const mult = ADAPTIVE_DIFFICULTY_ARMED && verdict ? verdict.m_shadow : 1;
    record = {
      v: BIRTH_CERT_VERSION,
      issued_at: issuedAt,
      method: "pow",
      difficulty: Math.min(POW_DIFFICULTY * mult, POW_DIFFICULTY * ADAPTIVE_MULT_CAP),
      params_version: POW_PARAMS_VERSION,
      email_token: null,
      email_class: null,
      email_domain_token: null,
    };
    record[SIG_FIELD] = await signBirthRecord(env, pubkey, record);
    await env.RATE_LIMITS.put(`born:${pubkey}`, JSON.stringify(record));
    await bumpIssuance(env, "births");
  }

  return json(certFromRecord(pubkey, record), corsHeaders);
}

async function handleBirthCertificateGet(url, env, corsHeaders) {
  /**
   * Public read of an issued certificate: certificates are public facts
   * (anyone may relay/verify one), so no signature is required.
   *
   * GET /birth-certificate?pubkey=HEX
   */
  const pubkey = (url.searchParams.get("pubkey") || "").toLowerCase();
  if (!isValidPubkeyHex(pubkey)) {
    return json({ error: "invalid pubkey" }, corsHeaders, 400);
  }

  const record = await loadBirthRecord(env, pubkey);
  if (!record) {
    return json({ error: "not issued" }, corsHeaders, 404);
  }

  return json(certFromRecord(pubkey, record), corsHeaders);
}

// -----------------------------------------------------------------------
// Birth ledger — adaptive difficulty (shadow)
// -----------------------------------------------------------------------

function subnetOf(ip) {
  if (!ip) return "";
  if (ip.includes(":")) {
    // IPv6: /48 — the customer allocation size, one household or one VPS.
    // Expand :: first: textual slicing put 2a00:db8::x and 2a00:db8:0:1::x
    // (the same /48) into different buckets.
    let groups;
    if (ip.includes("::")) {
      const [head, tail] = ip.split("::", 2);
      const h = head ? head.split(":") : [];
      const t = tail ? tail.split(":") : [];
      groups = h.concat(Array(Math.max(0, 8 - h.length - t.length)).fill("0"), t);
    } else {
      groups = ip.split(":");
    }
    const norm = groups.slice(0, 3).map((g) => (parseInt(g || "0", 16) || 0).toString(16));
    return norm.join(":") + "::/48";
  }
  return ip.split(".").slice(0, 3).join(".") + ".0/24";
}

async function subnetToken(env, ip) {
  // Peppered like ipHashUuid: equality is all the ledger needs, and a bare
  // /24 hash would be a 2^24 dictionary. Empty when there is nothing to hash.
  const subnet = subnetOf(ip);
  if (!subnet || !env.IP_PEPPER) return "";
  const key = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(env.IP_PEPPER),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const mac = new Uint8Array(await crypto.subtle.sign(
    "HMAC", key, new TextEncoder().encode(`sub:${subnet}`)));
  return bytesToHex(mac).slice(0, 16);
}

function shadowMultiplier(c) {
  // v0 placeholder thresholds — the whole point of shadow mode is to replace
  // these with measured distributions (/issuance-stats). Counts are PRIOR
  // births in the window that share the axis with this one. Conjunction
  // over single axis: same /24 within a day (CGNAT-tolerant: the 3rd birth
  // from one /24 in 24 h doubles), an ASN minting fast, and a mild global
  // term for a flood that is spread across networks.
  let m = 1;
  if (c.n_addr24 >= 2) m *= 2;          // 3rd birth from one exact address in a day
  if (c.n_sub24 >= 2) m *= 2;
  if (c.n_sub24 >= 5) m *= 2;
  if (c.n_asn1 >= 20) m *= 2;
  if (c.n_glob1 >= 60) m *= 2;
  return Math.min(m, ADAPTIVE_MULT_CAP);
}

export class BirthLedger {
  constructor(state, env) {
    this.state = state;
    this.sql = state.storage.sql;
    state.blockConcurrencyWhile(async () => this._init());
  }

  _init() {
    this.sql.exec(`CREATE TABLE IF NOT EXISTS births (
      pubkey TEXT PRIMARY KEY, ts INTEGER NOT NULL, asn INTEGER NOT NULL,
      cc TEXT NOT NULL, sub TEXT NOT NULL, method TEXT NOT NULL,
      m_shadow REAL NOT NULL, n_sub24 INTEGER NOT NULL, n_asn1 INTEGER NOT NULL,
      n_asn24 INTEGER NOT NULL, n_glob1 INTEGER NOT NULL, n_glob24 INTEGER NOT NULL,
      addr TEXT NOT NULL DEFAULT '', n_addr24 INTEGER NOT NULL DEFAULT 0)`);
    // Columns added after the first deployment (SQLite: ADD COLUMN only).
    for (const ddl of [
      "ALTER TABLE births ADD COLUMN addr TEXT NOT NULL DEFAULT ''",
      "ALTER TABLE births ADD COLUMN n_addr24 INTEGER NOT NULL DEFAULT 0",
      "ALTER TABLE births ADD COLUMN fam INTEGER NOT NULL DEFAULT 4",
    ]) {
      try { this.sql.exec(ddl); } catch (e) { /* already there */ }
    }
    this.sql.exec("CREATE INDEX IF NOT EXISTS births_ts ON births (ts)");
    this.sql.exec("CREATE INDEX IF NOT EXISTS births_sub_ts ON births (sub, ts)");
    this.sql.exec("CREATE INDEX IF NOT EXISTS births_asn_ts ON births (asn, ts)");
    this.sql.exec("CREATE INDEX IF NOT EXISTS births_addr_ts ON births (addr, ts)");
  }

  async fetch(request) {
    const url = new URL(request.url);
    if (request.method === "POST" && url.pathname === "/birth") {
      return Response.json(this.recordBirth(await request.json()));
    }
    if (request.method === "POST" && url.pathname === "/method") {
      const { pubkey, method } = await request.json();
      this.sql.exec("UPDATE births SET method = ? WHERE pubkey = ?", method, pubkey);
      return Response.json({ ok: true });
    }
    if (request.method === "GET" && url.pathname === "/stats") {
      return Response.json(this.stats(Date.now()));
    }
    return new Response("not found", { status: 404 });
  }

  _count(query, ...params) {
    return Number(this.sql.exec(query, ...params).one().n);
  }

  recordBirth({ pubkey, ts, asn, cc, sub, addr, fam }) {
    const prior = this.sql.exec(
      "SELECT m_shadow, n_sub24, n_asn1, n_asn24, n_glob1, n_glob24, n_addr24 FROM births WHERE pubkey = ?",
      pubkey).toArray();
    if (prior.length) return prior[0];               // idempotent, like issuance
    addr = addr || "";
    const hour = ts - 3600 * 1000, day = ts - 86400 * 1000;
    const counts = {
      n_addr24: addr ? this._count("SELECT count(*) AS n FROM births WHERE addr = ? AND ts > ?", addr, day) : 0,
      n_sub24: sub ? this._count("SELECT count(*) AS n FROM births WHERE sub = ? AND ts > ?", sub, day) : 0,
      n_asn1: asn ? this._count("SELECT count(*) AS n FROM births WHERE asn = ? AND ts > ?", asn, hour) : 0,
      n_asn24: asn ? this._count("SELECT count(*) AS n FROM births WHERE asn = ? AND ts > ?", asn, day) : 0,
      n_glob1: this._count("SELECT count(*) AS n FROM births WHERE ts > ?", hour),
      n_glob24: this._count("SELECT count(*) AS n FROM births WHERE ts > ?", day),
    };
    const m_shadow = shadowMultiplier(counts);
    this.sql.exec(
      `INSERT INTO births (pubkey, ts, asn, cc, sub, method, m_shadow, n_sub24, n_asn1, n_asn24, n_glob1, n_glob24, addr, n_addr24, fam)
       VALUES (?, ?, ?, ?, ?, 'pow', ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      pubkey, ts, asn, cc, sub, m_shadow, counts.n_sub24, counts.n_asn1,
      counts.n_asn24, counts.n_glob1, counts.n_glob24, addr, counts.n_addr24,
      Number(fam) === 6 ? 6 : 4);
    this.sql.exec("DELETE FROM births WHERE ts < ?", ts - LEDGER_RETENTION_MS);
    return { m_shadow, ...counts };
  }

  stats(now) {
    const day = now - 86400 * 1000, week = now - 7 * 86400 * 1000;
    const hist = (since) => ({
      births: this._count("SELECT count(*) AS n FROM births WHERE ts > ?", since),
      email: this._count("SELECT count(*) AS n FROM births WHERE ts > ? AND method = 'email'", since),
      distinct_asn: this._count("SELECT count(DISTINCT asn) AS n FROM births WHERE ts > ?", since),
      distinct_sub: this._count("SELECT count(DISTINCT sub) AS n FROM births WHERE ts > ?", since),
      distinct_addr: this._count("SELECT count(DISTINCT addr) AS n FROM births WHERE ts > ? AND addr <> ''", since),
      m2: this._count("SELECT count(*) AS n FROM births WHERE ts > ? AND m_shadow >= 2", since),
      m4: this._count("SELECT count(*) AS n FROM births WHERE ts > ? AND m_shadow >= 4", since),
      m8: this._count("SELECT count(*) AS n FROM births WHERE ts > ? AND m_shadow >= 8", since),
      // Working-IPv6-egress share — the upper bound of the dual-stack prize.
      v6: this._count("SELECT count(*) AS n FROM births WHERE ts > ? AND fam = 6", since),
    });
    const topAsn = this.sql.exec(
      "SELECT asn, cc, count(*) AS n FROM births WHERE ts > ? GROUP BY asn, cc ORDER BY n DESC LIMIT 5",
      week).toArray().map((r) => ({ asn: Number(r.asn), cc: r.cc, births: Number(r.n) }));
    return {
      total: this._count("SELECT count(*) AS n FROM births"),
      last_24h: hist(day),
      last_7d: hist(week),
      top_asn_7d: topAsn,
    };
  }
}

async function ledgerCall(env, path, body) {
  // The ledger is advisory: issuance never fails because the ledger did.
  if (!env.BIRTH_LEDGER) return null;
  try {
    const stub = env.BIRTH_LEDGER.get(env.BIRTH_LEDGER.idFromName("births"));
    const res = await stub.fetch(`https://ledger${path}`, {
      method: body === undefined ? "GET" : "POST",
      headers: { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    return await res.json();
  } catch (e) {
    console.error("birth ledger unavailable:", e);
    return null;
  }
}

async function ledgerBirth(env, request, pubkey, issuedAtIso) {
  const cf = request.cf || {};
  const ip = request.headers.get("CF-Connecting-IP") || "";
  return await ledgerCall(env, "/birth", {
    pubkey,
    ts: Date.parse(issuedAtIso),
    asn: Number(cf.asn) || 0,
    cc: String(cf.country || ""),
    sub: await subnetToken(env, ip),
    // The exact address too (peppered, same pseudonym the timestamp notary
    // uses): a /24 in a cloud provider spans many tenants, one VPS minting
    // twenty identities is a sharper conjunction than its subnet.
    addr: ip && env.IP_PEPPER ? await ipHashUuid(env, ip) : "",
    // Address family: the working-IPv6-egress share of the audience — the
    // upper bound of what dual-stack support (plan Ф19/Ф1) could win,
    // measured before any client supports v6 at all.
    fam: ip.includes(":") ? 6 : 4,
  });
}

// -----------------------------------------------------------------------
// Timestamp notary (authorship priority for signed enrichment records)
// -----------------------------------------------------------------------

// -----------------------------------------------------------------------
// Master mailbox (Ф16) — store-and-forward for ONE recipient, E2E opaque
// -----------------------------------------------------------------------

function mailboxStub(env) {
  return env.MAILBOX.get(env.MAILBOX.idFromName(MASTER_PUBKEY));
}

async function mailboxCall(env, path, body, init) {
  const stub = mailboxStub(env);
  const res = await stub.fetch(`https://mailbox${path}`, init || {
    method: body === undefined ? "GET" : "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  return { status: res.status, body: await res.json() };
}

function tsFresh(ts) {
  const n = Number(ts);
  return Number.isFinite(n) && Math.abs(Date.now() / 1000 - n) <= MAIL_SIG_SKEW_S;
}

async function bumpWindow(env, key, limit, ttlSeconds) {
  // Same pessimistic KV counter as checkRateLimit, on its own key.
  const count = (parseInt(await env.RATE_LIMITS.get(key)) || 0) + 1;
  await env.RATE_LIMITS.put(key, String(count), { expirationTtl: ttlSeconds });
  return count > limit;
}

async function handleMailboxPut(request, env, corsHeaders) {
  /**
   * Body: {to, from_public_key, encrypted, timestamp, message_uuid, signature}
   * — the exact chat wire payload plus a sender signature over
   *   mailbox:v1:{to}:{message_uuid}:{timestamp}:{sha256hex(encrypted)}
   * `to` must be the master; the sender must hold a certificate issued
   * here (no free keys); per-IP, per-sender and global caps bound the
   * storage. The Worker never sees plaintext.
   */
  if (!env.MAILBOX) return json({ error: "mailbox not configured" }, corsHeaders, 503);
  const body = await request.json();
  const { to, from_public_key, encrypted, timestamp, message_uuid, signature } = body;
  if (!to || !from_public_key || !encrypted || !timestamp || !message_uuid || !signature) {
    return json({ error: "missing fields" }, corsHeaders, 400);
  }
  if (to.toLowerCase() !== MASTER_PUBKEY) {
    return json({ error: "no mailbox for this recipient" }, corsHeaders, 404);
  }
  const sender = from_public_key.toLowerCase();
  if (!isValidPubkeyHex(sender) || sender === MASTER_PUBKEY) {
    return json({ error: "invalid sender" }, corsHeaders, 400);
  }
  if (typeof encrypted !== "string" || encrypted.length > MAIL_MAX_CHARS) {
    return json({ error: "message too large" }, corsHeaders, 413);
  }
  if (!/^[0-9a-f-]{36}$/.test(message_uuid) || typeof timestamp !== "string" || timestamp.length > 40) {
    return json({ error: "invalid message" }, corsHeaders, 400);
  }
  const digest = await sha256Hex(encrypted);
  const message = `mailbox:v1:${MASTER_PUBKEY}:${message_uuid}:${timestamp}:${digest}`;
  if (!await verifySignature(message, signature, sender)) {
    return json({ error: "invalid signature" }, corsHeaders, 403);
  }
  const ip = request.headers.get("CF-Connecting-IP") || "unknown";
  if (await bumpWindow(env, `rl:mbx:${ip}`, MAIL_IP_PER_HOUR, 3600)) {
    return json({ error: "rate limited" }, corsHeaders, 429);
  }
  if (!await env.RATE_LIMITS.get(`born:${sender}`, "json")) {
    return json({ error: "sender has no certificate" }, corsHeaders, 403);
  }
  const r = await mailboxCall(env, "/put", {
    uuid: message_uuid, sender, encrypted, ts: timestamp, now: Date.now(),
  });
  return json(r.body, corsHeaders, r.status);
}

async function verifyMasterRequest(url, label, extra) {
  // Query: public_key_hex (must be the master), ts, signature over
  //   {label}:v1:{ts}[:{extra}]
  const pubkey = (url.searchParams.get("public_key_hex") || "").toLowerCase();
  const ts = url.searchParams.get("ts") || "";
  const signature = url.searchParams.get("signature") || "";
  if (pubkey !== MASTER_PUBKEY) return "not the master";
  if (!tsFresh(ts)) return "stale timestamp";
  const message = extra === undefined ? `${label}:v1:${ts}` : `${label}:v1:${ts}:${extra}`;
  if (!await verifySignature(message, signature, pubkey)) return "invalid signature";
  return null;
}

async function handleMailboxDrain(request, url, env, corsHeaders) {
  /**
   * GET  /mailbox?public_key_hex&ts&signature&after=N  → {messages, more}
   *      signature over mailbox-drain:v1:{ts}:{after}
   * DELETE /mailbox?public_key_hex&ts&signature&upto=N → {deleted}
   *      signature over mailbox-ack:v1:{ts}:{upto}
   * The master imports what it drains (dedup by message_uuid on its side)
   * and acks by id; an unacked batch is served again next time.
   */
  if (!env.MAILBOX) return json({ error: "mailbox not configured" }, corsHeaders, 503);
  if (request.method === "GET") {
    const after = String(parseInt(url.searchParams.get("after") || "0") || 0);
    const err = await verifyMasterRequest(url, "mailbox-drain", after);
    if (err) return json({ error: err }, corsHeaders, 403);
    const r = await mailboxCall(env, `/drain?after=${after}`);
    return json(r.body, corsHeaders, r.status);
  }
  const upto = String(parseInt(url.searchParams.get("upto") || "0") || 0);
  const err = await verifyMasterRequest(url, "mailbox-ack", upto);
  if (err) return json({ error: err }, corsHeaders, 403);
  const r = await mailboxCall(env, "/ack", { upto: Number(upto) });
  return json(r.body, corsHeaders, r.status);
}

async function handleMailboxWake(request, url, env, corsHeaders) {
  /**
   * GET /mailbox/wake?public_key_hex&ts&signature[&peer_port] (Upgrade: websocket)
   *   signature over mailbox-wake:v1:{ts}[:{peer_port}]
   * The master holds this socket (hibernating on the Durable Object side);
   * every stored message sends "mail" — the master drains on it and on
   * (re)connect. No polling anywhere.
   *
   * When peer_port is declared, the connection doubles as the MASTER
   * ADDRESS HINT: the egress IP the edge observed + that port are stored
   * for /master-hint. The port is inside the master's signature and the
   * update is single-use per signature (KV nonce, TTL = the skew window) —
   * a captured URL replayed from another host can neither change the port
   * nor point the hint at the replayer's address.
   */
  if (!env.MAILBOX) return json({ error: "mailbox not configured" }, corsHeaders, 503);
  const peerPort = url.searchParams.get("peer_port") || "";
  const err = await verifyMasterRequest(url, "mailbox-wake",
                                        peerPort === "" ? undefined : peerPort);
  if (err) return json({ error: err }, corsHeaders, 403);
  // The hint is recorded on the SIGNED request itself (before the upgrade
  // gate): the signature is the authority, the upgrade only carries the
  // wake stream.
  const port = parseInt(peerPort);
  if (Number.isInteger(port) && port > 0 && port < 65536) {
    const sigNonce = `wakesig:${await sha256Hex(url.searchParams.get("signature") || "")}`;
    if (!await env.RATE_LIMITS.get(sigNonce)) {
      await env.RATE_LIMITS.put(sigNonce, "1", { expirationTtl: 2 * MAIL_SIG_SKEW_S });
      const host = request.headers.get("CF-Connecting-IP") || "";
      if (host) {
        // Two family slots: a connect updates the slot of ITS family and
        // preserves the other — if the master's egress ever moves to IPv6,
        // v4-only clients must not lose their half of the hint (`host`
        // stays the v4 slot for wire compatibility).
        const prev = (await env.RATE_LIMITS.get("master:hint", "json")) || {};
        const hint = { host: prev.host || null, host6: prev.host6 || null };
        hint[host.includes(":") ? "host6" : "host"] = host;
        await env.RATE_LIMITS.put("master:hint", JSON.stringify({
          ...hint, port, updated_at: nowIsoSeconds(),
        }));
      }
    }
  }
  if ((request.headers.get("Upgrade") || "").toLowerCase() !== "websocket") {
    return json({ error: "websocket upgrade required" }, corsHeaders, 426);
  }
  return await mailboxStub(env).fetch(new Request("https://mailbox/wake", request));
}

async function handleMasterHint(env, corsHeaders) {
  /**
   * The master's last known peer address — a DISCOVERY tier of last
   * resort, never a trust statement: whoever dials it still faces the
   * full peer handshake (pinned pubkey, certificates, signatures), and
   * the address itself is public anyway through the master's own DHT
   * announces. Empty until the master's wake socket has connected once.
   */
  const hint = await env.RATE_LIMITS.get("master:hint", "json");
  return json(hint || {}, corsHeaders);
}

async function directoryCall(env, path, body) {
  if (!env.NODE_DIRECTORY) return { status: 503, body: { error: "directory not configured" } };
  const stub = env.NODE_DIRECTORY.get(env.NODE_DIRECTORY.idFromName("nodes"));
  const res = await stub.fetch(`https://directory${path}`, {
    method: body === undefined ? "GET" : "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  return { status: res.status, body: await res.json() };
}

async function handleNodeRegister(request, env, corsHeaders) {
  /**
   * Body: {pubkey, port, capabilities: [..], ts, signature}
   *   signature over sautium-directory:v1:{port}:{caps sorted, comma}:{ts}
   * The ADDRESS is never taken from the client: the edge observed it
   * (CF-Connecting-IP), so a registration cannot point at somebody else —
   * and the signature is single-use (KV nonce), so a captured request
   * replayed from another host cannot re-point the entry either (the same
   * two guards the master hint uses). Requires a certificate issued here;
   * the master itself is refused — it has /master-hint, and clients keep
   * it as the LAST tier by design.
   */
  const body = await request.json();
  const { pubkey, port, capabilities, ts, signature } = body;
  const key = String(pubkey || "").toLowerCase();
  if (!isValidPubkeyHex(key) || !signature) {
    return json({ error: "missing fields" }, corsHeaders, 400);
  }
  if (key === MASTER_PUBKEY) {
    return json({ error: "the master publishes /master-hint" }, corsHeaders, 403);
  }
  const p = parseInt(port);
  if (!Number.isInteger(p) || p <= 0 || p >= 65536) {
    return json({ error: "invalid port" }, corsHeaders, 400);
  }
  if (!Array.isArray(capabilities) || capabilities.length === 0 || capabilities.length > 4
      || !capabilities.every((c) => DIRECTORY_CAPS.has(c))) {
    return json({ error: "invalid capabilities" }, corsHeaders, 400);
  }
  if (!tsFresh(ts)) {
    return json({ error: "stale timestamp" }, corsHeaders, 403);
  }
  const caps = [...new Set(capabilities)].sort();
  const message = `sautium-directory:v1:${p}:${caps.join(",")}:${Number(ts)}`;
  if (!await verifySignature(message, signature, key)) {
    return json({ error: "invalid signature" }, corsHeaders, 403);
  }
  const ip = request.headers.get("CF-Connecting-IP") || "";
  if (await bumpWindow(env, `rl:dir:${ip}`, DIRECTORY_REG_PER_IP_HOUR, 3600)) {
    return json({ error: "rate limited" }, corsHeaders, 429);
  }
  if (!await env.RATE_LIMITS.get(`born:${key}`, "json")) {
    return json({ error: "node has no certificate" }, corsHeaders, 403);
  }
  const sigNonce = `dirsig:${await sha256Hex(signature)}`;
  if (await env.RATE_LIMITS.get(sigNonce)) {
    return json({ error: "signature already used" }, corsHeaders, 403);
  }
  await env.RATE_LIMITS.put(sigNonce, "1", { expirationTtl: 2 * MAIL_SIG_SKEW_S });
  if (!ip) return json({ error: "no source address" }, corsHeaders, 400);
  const r = await directoryCall(env, "/register", {
    pubkey: key, host: ip, fam: ip.includes(":") ? 6 : 4, port: p,
    caps: caps.join(","), now: Date.now(),
  });
  return json(r.body, corsHeaders, r.status);
}

async function handleNodeHints(url, env, corsHeaders) {
  const cap = url.searchParams.get("cap") || "";
  if (!DIRECTORY_CAPS.has(cap)) {
    return json({ error: "unknown capability" }, corsHeaders, 400);
  }
  const r = await directoryCall(env, `/hints?cap=${cap}&now=${Date.now()}`);
  return json(r.body, corsHeaders, r.status);
}

export class NodeDirectory {
  constructor(state, env) {
    this.state = state;
    this.sql = state.storage.sql;
    state.blockConcurrencyWhile(async () => this._init());
  }

  _init() {
    this.sql.exec(`CREATE TABLE IF NOT EXISTS nodes (
      pubkey TEXT PRIMARY KEY, host4 TEXT, host6 TEXT, port INTEGER NOT NULL,
      caps TEXT NOT NULL, ts INTEGER NOT NULL)`);
    this.sql.exec("CREATE INDEX IF NOT EXISTS nodes_ts ON nodes (ts)");
  }

  async fetch(request) {
    const url = new URL(request.url);
    if (request.method === "POST" && url.pathname === "/register") {
      const { pubkey, host, fam, port, caps, now } = await request.json();
      const prev = this.sql.exec("SELECT host4, host6 FROM nodes WHERE pubkey = ?", pubkey).toArray()[0] || {};
      const host4 = fam === 4 ? host : (prev.host4 || null);
      const host6 = fam === 6 ? host : (prev.host6 || null);
      this.sql.exec(
        `INSERT INTO nodes (pubkey, host4, host6, port, caps, ts) VALUES (?, ?, ?, ?, ?, ?)
         ON CONFLICT(pubkey) DO UPDATE SET host4 = excluded.host4, host6 = excluded.host6,
           port = excluded.port, caps = excluded.caps, ts = excluded.ts`,
        pubkey, host4, host6, port, caps, now);
      this.sql.exec("DELETE FROM nodes WHERE ts < ?", now - 7 * 24 * 3600 * 1000);
      return Response.json({ registered: true });
    }
    if (request.method === "GET" && url.pathname === "/hints") {
      const cap = url.searchParams.get("cap") || "";
      const now = parseInt(url.searchParams.get("now")) || 0;
      const rows = this.sql.exec(
        `SELECT pubkey, host4, host6, port, caps FROM nodes
          WHERE ts > ? AND (',' || caps || ',') LIKE ?
          ORDER BY RANDOM() LIMIT ?`,
        now - DIRECTORY_TTL_MS, `%,${cap},%`, DIRECTORY_K).toArray();
      return Response.json({ nodes: rows.map((r) => ({
        pubkey: r.pubkey, host: r.host4 || null, host6: r.host6 || null, port: Number(r.port),
      })) });
    }
    if (request.method === "GET" && url.pathname === "/stats") {
      return Response.json({ nodes: Number(this.sql.exec("SELECT count(*) AS n FROM nodes").one().n) });
    }
    return new Response("not found", { status: 404 });
  }
}

export class MasterMailbox {
  constructor(state, env) {
    this.state = state;
    this.sql = state.storage.sql;
    state.blockConcurrencyWhile(async () => this._init());
  }

  _init() {
    this.sql.exec(`CREATE TABLE IF NOT EXISTS mail (
      id INTEGER PRIMARY KEY AUTOINCREMENT, uuid TEXT NOT NULL UNIQUE,
      sender TEXT NOT NULL, encrypted TEXT NOT NULL, ts TEXT NOT NULL,
      received_at INTEGER NOT NULL, expires_at INTEGER NOT NULL)`);
    this.sql.exec("CREATE INDEX IF NOT EXISTS mail_sender_recv ON mail (sender, received_at)");
    this.sql.exec("CREATE INDEX IF NOT EXISTS mail_recv ON mail (received_at)");
    if (typeof WebSocketRequestResponsePair !== "undefined" && this.state.setWebSocketAutoResponse) {
      // Keepalives answered by the runtime without waking the object.
      this.state.setWebSocketAutoResponse(new WebSocketRequestResponsePair("ping", "pong"));
    }
  }

  async fetch(request) {
    const url = new URL(request.url);
    if (request.method === "POST" && url.pathname === "/put") {
      return this._json(this.put(await request.json()));
    }
    if (request.method === "GET" && url.pathname === "/drain") {
      return Response.json(this.drain(parseInt(url.searchParams.get("after") || "0") || 0));
    }
    if (request.method === "POST" && url.pathname === "/ack") {
      const { upto } = await request.json();
      const before = this._count("SELECT count(*) AS n FROM mail");
      this.sql.exec("DELETE FROM mail WHERE id <= ?", Number(upto) || 0);
      return Response.json({ deleted: before - this._count("SELECT count(*) AS n FROM mail") });
    }
    if (request.method === "GET" && url.pathname === "/wake") {
      if (typeof WebSocketPair === "undefined") {
        return Response.json({ error: "websockets unavailable" }, { status: 501 });
      }
      const pair = new WebSocketPair();
      this.state.acceptWebSocket(pair[1]);
      return new Response(null, { status: 101, webSocket: pair[0] });
    }
    if (request.method === "GET" && url.pathname === "/stats") {
      return Response.json({ pending: this._count("SELECT count(*) AS n FROM mail"),
                             sockets: this.state.getWebSockets ? this.state.getWebSockets().length : 0 });
    }
    return new Response("not found", { status: 404 });
  }

  _json(result) {
    return Response.json(result.body, { status: result.status });
  }

  _count(query, ...params) {
    return Number(this.sql.exec(query, ...params).one().n);
  }

  put({ uuid, sender, encrypted, ts, now }) {
    now = Number(now) || Date.now();
    this.sql.exec("DELETE FROM mail WHERE expires_at <= ?", now);
    const day = now - 86400 * 1000;
    if (this._count("SELECT count(*) AS n FROM mail WHERE uuid = ?", uuid) > 0) {
      return { status: 200, body: { stored: false, duplicate: true } };
    }
    if (this._count("SELECT count(*) AS n FROM mail WHERE sender = ? AND received_at > ?", sender, day) >= MAIL_PER_SENDER_DAY) {
      return { status: 429, body: { error: "sender cap reached" } };
    }
    if (this._count("SELECT count(*) AS n FROM mail WHERE received_at > ?", day) >= MAIL_PER_DAY) {
      return { status: 429, body: { error: "mailbox busy" } };
    }
    const total = this._count("SELECT count(*) AS n FROM mail");
    if (total >= MAIL_CAP) {
      this.sql.exec("DELETE FROM mail WHERE id IN (SELECT id FROM mail ORDER BY id LIMIT ?)", total - MAIL_CAP + 1);
    }
    const id = Number(this.sql.exec(
      "INSERT INTO mail (uuid, sender, encrypted, ts, received_at, expires_at) VALUES (?, ?, ?, ?, ?, ?) RETURNING id",
      uuid, sender, encrypted, ts, now, now + MAIL_TTL_MS).one().id);
    this._wake();
    return { status: 200, body: { stored: true, id } };
  }

  drain(after) {
    const rows = this.sql.exec(
      "SELECT id, uuid, sender, encrypted, ts, received_at FROM mail WHERE id > ? ORDER BY id LIMIT 201", after).toArray();
    const more = rows.length > 200;
    return {
      messages: rows.slice(0, 200).map((r) => ({
        id: Number(r.id), message_uuid: r.uuid, from_public_key: r.sender,
        encrypted: r.encrypted, timestamp: r.ts, received_at: Number(r.received_at),
      })),
      more,
    };
  }

  _wake() {
    if (!this.state.getWebSockets) return;
    for (const ws of this.state.getWebSockets()) {
      try { ws.send("mail"); } catch (e) { /* closing socket */ }
    }
  }

  webSocketMessage(ws, message) { /* keepalives are auto-answered; nothing else is expected */ }

  webSocketClose(ws, code, reason, wasClean) {
    try { ws.close(code, reason); } catch (e) { /* already closed */ }
  }

  webSocketError(ws, error) {
    try { ws.close(1011, "error"); } catch (e) { /* already closed */ }
  }
}

async function handleTimestamp(request, env, corsHeaders) {
  /**
   * Notarize a Merkle batch root at the current date — the "when" a signed
   * enrichment record needs for authorship priority (see docs/design/
   * P2P-SYNC-INTEGRITY.md: The karma curve / Notary scaling). The Worker
   * signs roots, not records: a node submits one 32-byte root per batch.
   *
   * Body: {root}  (64-hex Merkle root)
   * Returns {root, date, sig, authority}; sig is over
   *   sautium-timestamp:v1:{root}:{date}
   * by the master authority (BIRTH_SIGNING_KEY, domain-separated from birth
   * certs by the payload prefix). Idempotent: a root is stamped once (the
   * KV record is the transparency log entry). No auth — notarizing a hash
   * reveals nothing; rate-limited against flooding.
   */
  const body = await request.json();
  const root = (body.root || "").toLowerCase();
  if (!/^[0-9a-f]{64}$/.test(root)) {
    return json({ error: "invalid root" }, corsHeaders, 400);
  }

  const ip = request.headers.get("CF-Connecting-IP") || "unknown";
  const rateLimitResult = await checkRateLimit(env, ip, `ts:${ip}`);
  if (rateLimitResult) {
    return json({ error: rateLimitResult }, corsHeaders, 429);
  }

  // Idempotent per root, but re-sign any record still in an older format so a
  // re-submitted v1 root upgrades to v2 (adds ip_hash). The date is the
  // FIRST-issuance moment — preserved across a re-sign so authorship priority
  // never regresses; only ip_hash reflects whoever re-submits.
  let record = await env.RATE_LIMITS.get(`ts:${root}`, "json");
  if (!record || record.v !== TIMESTAMP_VERSION
      || record.ipv !== IP_HASH_VERSION) {
    const date = record?.date || nowIsoSeconds();
    const ipHash = await ipHashUuid(env, ip);
    const key = await birthSigningKey(env);
    const sig = bytesToHex(new Uint8Array(await crypto.subtle.sign(
      "Ed25519", key,
      new TextEncoder().encode(
        `sautium-timestamp:v${TIMESTAMP_VERSION}:${root}:${date}:${ipHash}`))));
    record = { v: TIMESTAMP_VERSION, ipv: IP_HASH_VERSION, date,
               ip_hash: ipHash, sig };
    await env.RATE_LIMITS.put(`ts:${root}`, JSON.stringify(record));
  }

  return json({
    root,
    date: record.date,
    ip_hash: record.ip_hash,
    sig: record.sig,
    authority: TRUSTED_AUTHORITIES[0],
  }, corsHeaders);
}

// -----------------------------------------------------------------------
// Email verification & invites
// -----------------------------------------------------------------------

async function handleVerification(request, env, corsHeaders) {
  /**
   * Generate a verification code, remember its hash, email it.
   *
   * Body: {to, invite_code, public_key_hex, signature, from_username?}
   * Signature is over: "sendcode:{invite_code}:{to}"
   *
   * The code is generated HERE and only its hash is stored (KV, 15 min TTL).
   * The client never learns the expected value, so possession of the mailbox
   * is proven to the Worker at /register-email — not asserted by the client.
   */
  const body = await request.json();
  const { to, invite_code, public_key_hex, signature, from_username } = body;

  if (!to || !invite_code || !public_key_hex || !signature) {
    return json({ error: "missing fields" }, corsHeaders, 400);
  }

  if (!isValidEmail(to)) {
    return json({ error: "invalid email" }, corsHeaders, 400);
  }

  if (!await verifyInviteCode(invite_code, public_key_hex)) {
    return json({ error: "invite code doesn't match public key" }, corsHeaders, 403);
  }

  const sigMessage = `sendcode:${invite_code}:${to}`;
  const valid = await verifySignature(sigMessage, signature, public_key_hex);
  if (!valid) {
    return json({ error: "invalid signature" }, corsHeaders, 403);
  }

  const ip = request.headers.get("CF-Connecting-IP") || "unknown";
  const rateLimitResult = await checkRateLimit(env, ip, to);
  if (rateLimitResult) {
    return json({ error: rateLimitResult }, corsHeaders, 429);
  }

  const code = randomCode(6);
  await env.RATE_LIMITS.put(
    `emailcode:${kvKey(invite_code)}`,
    JSON.stringify({ hash: await sha256Hex(code), email: to }),
    { expirationTtl: VERIFY_CODE_TTL_SECONDS },
  );

  const html = verificationEmailHtml(code, from_username, invite_code);
  const subject = `Sautium — Verification code`;

  const result = await sendEmail(env, to, subject, html);
  if (!result.ok) {
    return json({ error: "email send failed" }, corsHeaders, 502);
  }

  return json({ status: "sent" }, corsHeaders);
}

async function handleRegisterEmail(request, env, corsHeaders) {
  /**
   * Register a verified email for an invite code.
   *
   * Body: {invite_code, email, public_key_hex, signature, code, birth_cert}
   * Signature is over: "register:{invite_code}:{email}"
   *
   * Requires:
   * - the verification code the Worker generated and emailed (proves mailbox
   *   ownership to the Worker, not to the client itself);
   * - a valid identity certificate for the same pubkey (the KV record carries
   *   born_at, so a verified badge can cite identity age).
   *
   * Stored value: {email, pubkey, born_at, verified_at}. The identity's
   * certificate record is upgraded to method:email with the peppered
   * email_token + email_class (issued_at unchanged) and the new certificate is
   * returned as `birth_cert` so the client persists it in one round-trip.
   */
  const body = await request.json();
  const { invite_code, email, public_key_hex, signature, code, birth_cert } = body;

  if (!invite_code || !email || !public_key_hex || !signature || !code || !birth_cert) {
    return json({ error: "missing fields" }, corsHeaders, 400);
  }

  if (!await verifyInviteCode(invite_code, public_key_hex)) {
    return json({ error: "invite code doesn't match public key" }, corsHeaders, 403);
  }

  const message = `register:${invite_code}:${email}`;
  const valid = await verifySignature(message, signature, public_key_hex);
  if (!valid) {
    return json({ error: "invalid signature" }, corsHeaders, 403);
  }

  const codeKey = `emailcode:${kvKey(invite_code)}`;
  const pending = await env.RATE_LIMITS.get(codeKey, "json");
  if (!pending || pending.email !== email ||
      pending.hash !== await sha256Hex(code.trim().toUpperCase())) {
    return json({ error: "invalid or expired code" }, corsHeaders, 403);
  }

  const pubkey = public_key_hex.toLowerCase();
  if (!await verifyBirthCertificate(birth_cert) || birth_cert.pubkey !== pubkey) {
    return json({ error: "invalid birth certificate" }, corsHeaders, 403);
  }
  const record = await loadBirthRecord(env, pubkey);
  if (!record) {
    return json({ error: "certificate not issued here" }, corsHeaders, 403);
  }

  const normalized = normalizeEmail(email);
  const token = await emailToken(env, normalized);
  // Succession: the mailbox moves to this key; whoever held it before is
  // named in the certificate as the predecessor (a password change makes a
  // new key — nodes carry witnessed age and bans across the link).
  const previous = await claimMailbox(env, token, pubkey, { takeOver: true });
  const upgraded = {
    ...record,
    method: "email",
    email_token: token,
    email_class: emailClass(normalized),
    email_domain_token: await emailDomainToken(env, normalized),
    predecessor: previous || record.predecessor || null,
  };
  upgraded[SIG_FIELD] = await signBirthRecord(env, pubkey, upgraded);
  await env.RATE_LIMITS.put(`born:${pubkey}`, JSON.stringify(upgraded));
  if (record.method !== "email") {
    await bumpIssuance(env, "email");
    await ledgerCall(env, "/method", { pubkey, method: "email" });
  }
  if (previous) await bumpIssuance(env, "succession");

  await env.RATE_LIMITS.put(`verified:${kvKey(invite_code)}`, JSON.stringify({
    email,
    pubkey,
    born_at: upgraded.issued_at,
    verified_at: nowIsoSeconds(),
  }));
  await env.RATE_LIMITS.delete(codeKey);

  return json({
    status: "registered", invite_code, email,
    birth_cert: certFromRecord(pubkey, upgraded),
  }, corsHeaders);
}

async function handleInvite(request, env, corsHeaders) {
  /**
   * Send an invite email with verified sender information.
   *
   * Body: {to, invite_code, public_key_hex, signature, message?}
   * Signature is over: "invite:{invite_code}:to:{to}"
   */
  const body = await request.json();
  const { to, invite_code, public_key_hex, signature, message: userMessage } = body;

  if (!to || !invite_code || !public_key_hex || !signature) {
    return json({ error: "missing fields" }, corsHeaders, 400);
  }

  if (!isValidEmail(to)) {
    return json({ error: "invalid email" }, corsHeaders, 400);
  }

  if (!await verifyInviteCode(invite_code, public_key_hex)) {
    return json({ error: "invite code doesn't match public key" }, corsHeaders, 403);
  }

  const sigMessage = `invite:${invite_code}:to:${to}`;
  const valid = await verifySignature(sigMessage, signature, public_key_hex);
  if (!valid) {
    return json({ error: "invalid signature" }, corsHeaders, 403);
  }

  const ip = request.headers.get("CF-Connecting-IP") || "unknown";
  const rateLimitResult = await checkRateLimit(env, ip, to);
  if (rateLimitResult) {
    return json({ error: rateLimitResult }, corsHeaders, 429);
  }

  const verifiedEmail = (await getVerified(env, invite_code))?.email;
  const username = invite_code.split("#")[0];

  const html = inviteEmailHtml(username, invite_code, verifiedEmail, userMessage);
  const subject = `${username} invites you to Sautium`;

  const result = await sendEmail(env, to, subject, html);
  if (!result.ok) {
    return json({ error: "email send failed" }, corsHeaders, 502);
  }

  return json({ status: "sent", verified_sender: !!verifiedEmail }, corsHeaders);
}

async function handleCheckEmail(url, env, corsHeaders) {
  /**
   * Check if an email is already verified for an invite code.
   *
   * GET /check-email?invite_code=X&email=Y&public_key_hex=Z&signature=S
   * Signature over: "check-email:{invite_code}:{email}"
   *
   * Returns {verified: true} only if the stored email matches exactly.
   * Safe: requires signature (proves key ownership), doesn't leak emails.
   * When verified for this very pubkey, also returns (and, if needed,
   * upgrades to) the method:email identity certificate.
   */
  const inviteCode = url.searchParams.get("invite_code");
  const email = url.searchParams.get("email");
  const publicKeyHex = url.searchParams.get("public_key_hex");
  const signature = url.searchParams.get("signature");

  if (!inviteCode || !email || !publicKeyHex || !signature) {
    return json({ error: "missing fields" }, corsHeaders, 400);
  }

  if (!await verifyInviteCode(inviteCode, publicKeyHex)) {
    return json({ error: "invite code doesn't match public key" }, corsHeaders, 403);
  }

  const message = `check-email:${inviteCode}:${email}`;
  const valid = await verifySignature(message, signature, publicKeyHex);
  if (!valid) {
    return json({ error: "invalid signature" }, corsHeaders, 403);
  }

  const stored = await getVerified(env, inviteCode);
  const pubkey = publicKeyHex.toLowerCase();
  const verified = stored?.email === email;
  if (!verified || stored.pubkey !== pubkey) {
    return json({ verified }, corsHeaders);
  }
  // Mailbox ownership was already proven for THIS key when the record was
  // stored, so an identity verified before certificate v2 gets its
  // method:email certificate here without a second code round — same
  // proof, same binding (signed request + invite↔pubkey + stored pubkey).
  let record = await loadBirthRecord(env, pubkey);
  if (record && (record.method !== "email" || !record.email_domain_token)) {
    const wasEmail = record.method === "email";
    const normalized = normalizeEmail(email);
    record = {
      ...record,
      method: "email",
      email_token: await emailToken(env, normalized),
      email_class: emailClass(normalized),
      email_domain_token: await emailDomainToken(env, normalized),
    };
    record[SIG_FIELD] = await signBirthRecord(env, pubkey, record);
    await env.RATE_LIMITS.put(`born:${pubkey}`, JSON.stringify(record));
    if (!wasEmail) {
      await bumpIssuance(env, "email");
      await ledgerCall(env, "/method", { pubkey, method: "email" });
    }
  }
  if (record && record.email_token) {
    await claimMailbox(env, record.email_token, pubkey);   // owner index, no take-over
  }
  return json({
    verified: true,
    birth_cert: record ? certFromRecord(pubkey, record) : null,
  }, corsHeaders);
}

async function handleAcceptInvite(request, env, corsHeaders) {
  /**
   * Accept an invite: store reciprocal mapping + notify sender via email.
   *
   * When Sasha adds Masha's invite code, Sasha's app calls this endpoint.
   * The worker stores the acceptance and (if Masha has a verified email)
   * sends Sasha's invite code to Masha automatically.
   *
   * Body: {my_invite_code, their_invite_code, public_key_hex, signature}
   * Signature is over: "accept:{my_invite_code}:{their_invite_code}"
   */
  const body = await request.json();
  const { my_invite_code, their_invite_code, public_key_hex, signature } = body;

  if (!my_invite_code || !their_invite_code || !public_key_hex || !signature) {
    return json({ error: "missing fields" }, corsHeaders, 400);
  }

  if (!await verifyInviteCode(my_invite_code, public_key_hex)) {
    return json({ error: "invite code doesn't match public key" }, corsHeaders, 403);
  }

  const message = `accept:${my_invite_code}:${their_invite_code}`;
  const valid = await verifySignature(message, signature, public_key_hex);
  if (!valid) {
    return json({ error: "invalid signature" }, corsHeaders, 403);
  }

  // Store acceptance in KV (TTL 30 days). The acceptor's invite code lives
  // in the VALUE — storage keys are '#'-free (kvKey), so the key alone can't
  // reconstruct it.
  const acceptKey = `accepted:${kvKey(their_invite_code)}:${kvKey(my_invite_code)}`;
  await env.RATE_LIMITS.put(acceptKey, JSON.stringify({
    invite_code: my_invite_code,
    accepted_at: new Date().toISOString(),
  }), { expirationTtl: ACCEPT_TTL_SECONDS });

  // Try to notify the sender (Masha) via their verified email
  let notified = false;
  const senderEmail = (await getVerified(env, their_invite_code))?.email;
  if (senderEmail) {
    const myUsername = my_invite_code.split("#")[0];
    const myVerifiedEmail = (await getVerified(env, my_invite_code))?.email;

    const ip = request.headers.get("CF-Connecting-IP") || "unknown";
    const rateLimitResult = await checkRateLimit(env, ip, senderEmail);
    if (!rateLimitResult) {
      const html = acceptNotificationEmailHtml(myUsername, my_invite_code, myVerifiedEmail);
      const result = await sendEmail(env, senderEmail, `${myUsername} accepted your Sautium invite`, html);
      notified = result.ok;
    }
  }

  return json({ status: "accepted", notified }, corsHeaders);
}

async function handlePendingAccepts(url, env, corsHeaders) {
  /**
   * Check who accepted my invites (polling endpoint).
   *
   * GET /pending-accepts?invite_code=user%23XXXX&signature=HEX&public_key_hex=HEX
   *
   * Returns list of invite codes that accepted, then deletes them (one-time pickup).
   */
  const inviteCode = url.searchParams.get("invite_code");
  const publicKeyHex = url.searchParams.get("public_key_hex");
  const signature = url.searchParams.get("signature");

  if (!inviteCode || !publicKeyHex || !signature) {
    return json({ error: "missing fields" }, corsHeaders, 400);
  }

  if (!await verifyInviteCode(inviteCode, publicKeyHex)) {
    return json({ error: "invite code doesn't match public key" }, corsHeaders, 403);
  }

  const message = `pending-accepts:${inviteCode}`;
  const valid = await verifySignature(message, signature, publicKeyHex);
  if (!valid) {
    return json({ error: "invalid signature" }, corsHeaders, 403);
  }

  const prefix = `accepted:${kvKey(inviteCode)}:`;
  const listed = await env.RATE_LIMITS.list({ prefix });

  const accepts = [];
  for (const key of listed.keys) {
    const record = await env.RATE_LIMITS.get(key.name, "json");
    if (record) {
      accepts.push({ invite_code: record.invite_code, accepted_at: record.accepted_at });
    }

    // Delete after pickup (one-time)
    await env.RATE_LIMITS.delete(key.name);
  }

  return json({ accepts }, corsHeaders);
}

// -----------------------------------------------------------------------
// Crypto helpers
// -----------------------------------------------------------------------

async function verifySignature(message, signatureHex, publicKeyHex) {
  return await verifySignatureBytes(
    new TextEncoder().encode(message), signatureHex, publicKeyHex
  );
}

async function verifySignatureBytes(messageBytes, signatureHex, publicKeyHex) {
  try {
    const publicKeyBytes = hexToBytes(publicKeyHex);
    const signatureBytes = hexToBytes(signatureHex);

    const key = await crypto.subtle.importKey(
      "raw",
      publicKeyBytes,
      { name: "Ed25519" },
      false,
      ["verify"]
    );

    return await crypto.subtle.verify(
      "Ed25519",
      key,
      signatureBytes,
      messageBytes
    );
  } catch (e) {
    console.error("Signature verification failed:", e);
    return false;
  }
}

async function verifyInviteCode(inviteCode, publicKeyHex) {
  /**
   * Verify that invite code matches the public key.
   * invite_code = "username#XXXX-XXXX-XXXX"
   * where XXXX-XXXX-XXXX = SHA256(public_key_bytes)[:6] in uppercase hex
   * and username matches USERNAME_RE (enforced at account creation too).
   */
  try {
    const parts = inviteCode.split("#");
    if (parts.length !== 2) return false;
    if (!USERNAME_RE.test(parts[0])) return false;

    const hashPart = parts[1].replace(/-/g, "").toUpperCase();
    if (hashPart.length !== 12) return false;

    const publicKeyBytes = hexToBytes(publicKeyHex);
    const hashBuffer = await crypto.subtle.digest("SHA-256", publicKeyBytes);
    const hashArray = new Uint8Array(hashBuffer);
    const first6Hex = bytesToHex(hashArray.slice(0, 6)).toUpperCase();

    return first6Hex === hashPart;
  } catch {
    return false;
  }
}

async function sha256Hex(text) {
  const digest = await crypto.subtle.digest(
    "SHA-256", new TextEncoder().encode(text)
  );
  return bytesToHex(new Uint8Array(digest));
}

function randomCode(length) {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789";
  const values = new Uint8Array(length);
  crypto.getRandomValues(values);
  return Array.from(values, (v) => alphabet[v % alphabet.length]).join("");
}

// -----------------------------------------------------------------------
// KV helpers
// -----------------------------------------------------------------------

function kvKey(inviteCode) {
  // '#' is a URL fragment marker and breaks wrangler CLI KV commands;
  // usernames can't contain ':' (USERNAME_RE), so this is unambiguous.
  return inviteCode.replace("#", ":");
}

async function getVerified(env, inviteCode) {
  /** Verified-email record {email, pubkey, born_at, verified_at} or null. */
  return await env.RATE_LIMITS.get(`verified:${kvKey(inviteCode)}`, "json");
}

// -----------------------------------------------------------------------
// Email sending
// -----------------------------------------------------------------------

async function sendEmail(env, to, subject, html) {
  return await fetch("https://api.resend.com/emails", {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${env.RESEND_API_KEY}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      from: `${FROM_NAME} <${FROM_EMAIL}>`,
      to: [to],
      subject,
      html,
    }),
  });
}

// -----------------------------------------------------------------------
// Rate limiting
// -----------------------------------------------------------------------

async function checkRateLimit(env, ip, recipient) {
  if (!env.RATE_LIMITS) return null;

  const ipKey = `rl:ip:${ip}`;
  const recipientKey = `rl:to:${recipient}`;

  // Increment FIRST, then check (pessimistic).
  // KV has no atomic increment; concurrent requests may read
  // the same base value.  By writing the incremented value
  // before the check we ensure that at least one racing writer
  // sees the over-limit value on its next request.  Worst case:
  // N concurrent requests all read count C and all write C+1,
  // allowing up to N extra emails — but the NEXT wave sees
  // the (still only C+1) value and blocks.  Halved limits
  // compensate for this one-burst tolerance.
  const ipCount = (parseInt(await env.RATE_LIMITS.get(ipKey)) || 0) + 1;
  await env.RATE_LIMITS.put(ipKey, String(ipCount), {
    expirationTtl: RATE_LIMIT_WINDOW,
  });
  if (ipCount > RATE_LIMIT_PER_IP) {
    return "rate limited (too many requests from your IP)";
  }

  const recipientCount = (parseInt(await env.RATE_LIMITS.get(recipientKey)) || 0) + 1;
  await env.RATE_LIMITS.put(recipientKey, String(recipientCount), {
    expirationTtl: RATE_LIMIT_WINDOW,
  });
  if (recipientCount > RATE_LIMIT_PER_RECIPIENT) {
    return "rate limited (too many emails to this address)";
  }

  return null;
}

// -----------------------------------------------------------------------
// Email templates
// -----------------------------------------------------------------------

function verificationEmailHtml(code, fromUsername, inviteCode) {
  return `<!DOCTYPE html>
<html>
<body style="font-family: -apple-system, sans-serif; max-width: 480px; margin: 0 auto; padding: 20px;">
  <h2 style="color: #333;">Sautium — Email Verification</h2>
  <p>Enter this code in the app to verify your email:</p>
  <div style="background: #f0f0f0; padding: 20px; text-align: center; border-radius: 8px; margin: 20px 0;">
    <span style="font-size: 32px; font-weight: bold; letter-spacing: 4px; font-family: monospace;">${escapeHtml(code)}</span>
  </div>
  ${inviteCode ? `<p style="color: #666; font-size: 13px;">Your invite code: <code>${escapeHtml(inviteCode)}</code></p>` : ""}
  <p style="color: #999; font-size: 12px;">If you didn't request this, ignore this email.</p>
  <hr style="border: none; border-top: 1px solid #eee; margin: 20px 0;">
  <p style="color: #999; font-size: 11px;">
    <a href="https://sautium.net" style="color: #666;">Sautium</a> — AI-powered music library for FLAC collectors.
  </p>
</body>
</html>`;
}

function inviteEmailHtml(fromUsername, inviteCode, verifiedEmail, message) {
  const verifiedBadge = verifiedEmail
    ? `<div style="background: #e8f5e9; border: 1px solid #a5d6a7; border-radius: 6px; padding: 8px 12px; margin: 12px 0; font-size: 13px;">
        &#9989; Verified sender: <strong>${escapeHtml(verifiedEmail)}</strong>
       </div>`
    : `<div style="background: #fff3e0; border: 1px solid #ffcc80; border-radius: 6px; padding: 8px 12px; margin: 12px 0; font-size: 13px;">
        &#9888; Unverified sender — verify this person through another channel
       </div>`;

  return `<!DOCTYPE html>
<html>
<body style="font-family: -apple-system, sans-serif; max-width: 480px; margin: 0 auto; padding: 20px;">
  <h2 style="color: #333;">You're invited to Sautium</h2>
  <p><strong>${escapeHtml(fromUsername)}</strong> invites you to connect on Sautium.</p>
  ${verifiedBadge}
  ${message ? `<blockquote style="border-left: 3px solid #ddd; padding-left: 12px; color: #555;">${escapeHtml(message)}</blockquote>` : ""}
  <p>Their invite code:</p>
  <div style="background: #f0f0f0; padding: 12px 16px; border-radius: 8px; margin: 12px 0;">
    <code style="font-size: 16px; font-weight: bold;">${escapeHtml(inviteCode)}</code>
  </div>
  <p style="font-size: 13px; color: #666;">
    To connect: open Sautium → Friends → paste this invite code → Add.
    Then share your invite code back so both of you can chat.
  </p>
  <h3 style="color: #333; margin-top: 24px;">What is Sautium?</h3>
  <ul style="color: #555;">
    <li>AI-powered search across your FLAC/music collection</li>
    <li>Audio analysis: find similar tracks by sound, mood, tempo</li>
    <li>P2P network: share music metadata with friends</li>
    <li>End-to-end encrypted chat</li>
    <li>HQPlayer integration for audiophile playback</li>
  </ul>
  <p>
    <a href="https://sautium.net/download"
       style="display: inline-block; background: #2563eb; color: white; padding: 10px 24px;
              border-radius: 6px; text-decoration: none; font-weight: bold;">
      Download Sautium
    </a>
  </p>
  <hr style="border: none; border-top: 1px solid #eee; margin: 20px 0;">
  <p style="color: #999; font-size: 11px;">
    <a href="https://sautium.net" style="color: #666;">sautium.net</a>
  </p>
</body>
</html>`;
}

function acceptNotificationEmailHtml(fromUsername, inviteCode, verifiedEmail) {
  const verifiedBadge = verifiedEmail
    ? `<div style="background: #e8f5e9; border: 1px solid #a5d6a7; border-radius: 6px; padding: 8px 12px; margin: 12px 0; font-size: 13px;">
        &#9989; Verified: <strong>${escapeHtml(verifiedEmail)}</strong>
       </div>`
    : `<div style="background: #fff3e0; border: 1px solid #ffcc80; border-radius: 6px; padding: 8px 12px; margin: 12px 0; font-size: 13px;">
        &#9888; Unverified sender
       </div>`;

  return `<!DOCTYPE html>
<html>
<body style="font-family: -apple-system, sans-serif; max-width: 480px; margin: 0 auto; padding: 20px;">
  <h2 style="color: #333;">Invite accepted!</h2>
  <p><strong>${escapeHtml(fromUsername)}</strong> accepted your invite and wants to connect on Sautium.</p>
  ${verifiedBadge}
  <p>Their invite code:</p>
  <div style="background: #f0f0f0; padding: 12px 16px; border-radius: 8px; margin: 12px 0;">
    <code style="font-size: 16px; font-weight: bold;">${escapeHtml(inviteCode)}</code>
  </div>
  <p style="font-size: 13px; color: #666;">
    Sautium will add them automatically next time you open the app.
    Or paste the code manually: Friends &rarr; Add Friend.
  </p>
  <hr style="border: none; border-top: 1px solid #eee; margin: 20px 0;">
  <p style="color: #999; font-size: 11px;">
    <a href="https://sautium.net" style="color: #666;">sautium.net</a>
  </p>
</body>
</html>`;
}

// -----------------------------------------------------------------------
// Utilities
// -----------------------------------------------------------------------

function hexToBytes(hex) {
  const bytes = new Uint8Array(hex.length / 2);
  for (let i = 0; i < hex.length; i += 2) {
    bytes[i / 2] = parseInt(hex.substr(i, 2), 16);
  }
  return bytes;
}

function bytesToHex(bytes) {
  return Array.from(bytes).map(b => b.toString(16).padStart(2, "0")).join("");
}

function normalizeText(text) {
  // Mirror backend/uuid_utils.py normalize(): strip, lowercase, NFC, collapse
  // whitespace — same input canonicalisation feeds the same UUID.
  return text.trim().toLowerCase().normalize("NFC").replace(/\s+/g, " ");
}


async function ipHashUuid(env, ip) {
  // Pseudonym for the notary submitter's IP. It exists to MERGE identities:
  // keys are free, addresses are not, so a thousand Sybil keys stamped from
  // one address collapse into one subject — a signal no keypair can fake.
  //
  // Keyed, not plain: a uuid5 over a namespace that ships in this file is
  // reversible by brute force (the whole IPv4 space is ~4.3e9 SHA-1s, minutes
  // on a laptop), which would publish the author's address to every peer that
  // pulls a sealed record. HMAC under IP_PEPPER keeps equality — the only
  // property the merge needs — while making that search impossible without
  // the secret. UUID shape is preserved so the wire format, the signed
  // timestamp string and the `uuid` column all stay as they are.
  const pepper = env.IP_PEPPER;
  if (!pepper) throw new Error("IP_PEPPER is not configured");
  const key = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(pepper),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const mac = new Uint8Array(await crypto.subtle.sign(
    "HMAC", key, new TextEncoder().encode(`ip:${normalizeText(ip)}`)));
  const h = mac.slice(0, 16);
  h[6] = (h[6] & 0x0f) | 0x40; // version 4 shape: this is keyed, not a uuid5
  h[8] = (h[8] & 0x3f) | 0x80; // RFC 4122 variant
  const x = bytesToHex(h);
  return `${x.slice(0, 8)}-${x.slice(8, 12)}-${x.slice(12, 16)}-${x.slice(16, 20)}-${x.slice(20)}`;
}

function isValidEmail(email) {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email);
}

function normalizeEmail(email) {
  // One person, one token: case, gmail dots and +tags must not mint distinct
  // identities. Sub-addressing is stripped for every domain — the rare
  // provider where alice+a@ and alice+b@ are different people costs a false
  // link, the common case (one mailbox, many tags) would otherwise cost a
  // free identity multiplier.
  const at = email.lastIndexOf("@");
  let local = email.slice(0, at).trim().toLowerCase().normalize("NFC");
  let domain = email.slice(at + 1).trim().toLowerCase().normalize("NFC");
  if (domain === "googlemail.com") domain = "gmail.com";
  const plus = local.indexOf("+");
  if (plus > 0) local = local.slice(0, plus);
  if (domain === "gmail.com") local = local.replace(/\./g, "");
  return `${local}@${domain}`;
}

function emailClass(normalizedEmail) {
  const domain = normalizedEmail.slice(normalizedEmail.lastIndexOf("@") + 1);
  if (DISPOSABLE_EMAIL_DOMAINS.has(domain)) return "disposable";
  if (MAJOR_EMAIL_DOMAINS.has(domain)) return "major";
  return "other";
}

function emailDomain(normalizedEmail) {
  return normalizedEmail.slice(normalizedEmail.lastIndexOf("@") + 1);
}

async function emailDomainToken(env, normalizedEmail) {
  // The mailbox's DOMAIN under the same pepper, separate prefix: for a
  // major provider it is shared by millions (no information), for a rare
  // domain it is a cluster axis on its own — the whole-address token cannot
  // say "these fifty identities live on one odd domain". No local-part
  // token on purpose: near-zero signal, and it would link a person across
  // providers by name — beyond the accepted mailbox-level trade.
  const pepper = env.EMAIL_PEPPER;
  if (!pepper) throw new Error("EMAIL_PEPPER is not configured");
  const key = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(pepper),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const mac = new Uint8Array(await crypto.subtle.sign(
    "HMAC", key, new TextEncoder().encode(`email-domain:${emailDomain(normalizedEmail)}`)));
  return bytesToHex(mac);
}

async function emailToken(env, normalizedEmail) {
  // Same construction as ipHashUuid: HMAC under a Worker-only pepper keeps
  // equality (same token = same mailbox = a hard similarity link and the
  // succession key across a password change) while a bare hash of a
  // low-entropy address would be dictionary-reversible on any node.
  const pepper = env.EMAIL_PEPPER;
  if (!pepper) throw new Error("EMAIL_PEPPER is not configured");
  const key = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(pepper),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const mac = new Uint8Array(await crypto.subtle.sign(
    "HMAC", key, new TextEncoder().encode(`email:${normalizedEmail}`)));
  return bytesToHex(mac);
}

function escapeHtml(str) {
  if (!str) return "";
  return str
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function json(data, corsHeaders, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json", ...corsHeaders },
  });
}
