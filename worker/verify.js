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
 * An invite code (name#XXXX-XXXX-XXXX, 48 bits of the key's digest) belongs
 * to the first key born under its digest: every invite route refuses any
 * other key (the Limiter's "owners", filled from each birth through the
 * birth route's own instance and, once, from the births stored before it).
 *
 * Admission (Durable Object LIMITER, one SQLite instance per route): every
 * write a caller can repeat and every mail passes its route's counters there
 * first — per key, per mailbox, per address and a ceiling for the route — so
 * a refused request writes nothing, and concurrent requests cannot pass a
 * counter together. The master's mailbox and the directory keep the same
 * counters in their own objects, counting only what they store, in the step
 * that stores it. (Outside them: a legacy record upgraded on its first
 * read, once, and the master's own wake.) A store that fails answers 503,
 * never an error's text; the owner's WRITES_PAUSED stops every write a
 * request makes at once, and the Durable Objects' own alarms as each object
 * takes the new version (within Cloudflare's code-update delay, five
 * minutes by default). Callers are nodes, never browsers: no CORS grant, and
 * a body is application/json or it is refused.
 *
 * KV key naming: invite codes contain '#', which is a URL fragment marker and
 * breaks wrangler CLI (list silently hides such keys, get truncates at '#').
 * Storage keys therefore use ':' in place of '#' (kvKey()); the invite code
 * itself is unchanged everywhere else in the product. Usernames are
 * restricted to [A-Za-z0-9_-]{3,32} at both boundaries (client account
 * creation and ownInviteName here), so the substitution is unambiguous.
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
 *   POST /send-invite          — mail an invite with a single-use token from a
 *                                key with a certificate and a verified email;
 *                                the invite is kept under the token's hash
 *   GET  /check-email          — check if email already verified for invite code
 *   POST /accept-invite        — spend an invite's token (kept as accepted, the
 *                                inviter told by mail), or, without a token,
 *                                tell a code's owner it was added (nothing kept)
 *   POST /pending-accepts      — the inviter's accepted invites, each with its
 *                                acceptor's signed statement (a read)
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
 * § "Defense strategy"; plan phase Ф2b). Pricing is per CLUSTER (same
 * address, same /24, same ASN), never a global term — an honest launch wave
 * from one ISP must not pay for a cloud flood, and free births must not raise
 * everyone's price. The global count is recorded and reported, never priced.
 */

const PICKUP_PAGE = 300;                   // accepted invites one pickup returns (5 a day per key, kept up to 60 days)
const VERIFY_CODE_TTL_MS = 15 * 60 * 1000;
// Wrong tries that spend a pending verification code: after them even the
// right code is refused, and only a new code (its own caps) opens another round.
const VERIFY_CODE_TRIES = 5;
const INVITE_MESSAGE_MAX_CHARS = 500;     // the backend's own cap on the intro line
const MAX_BODY_BYTES = 16 * 1024;         // the largest real body is a full mailbox drop
const FROM_EMAIL = "noreply@sautium.net";
const FROM_NAME = "Sautium";

// Admission (the Limiter object, one instance per route): a route's
// counters, per 24 h from a counter's first use (the mail routes' ceilings
// per UTC day: MAIL_ROUTES). Workers Paid has no hard
// spending cap, so these ceilings are the cap: `all` bounds the route's KV
// writes, reads or mail on a bad day — a day with every route at its ceiling
// costs well under a dollar in KV and Durable Object charges at 2026 prices
// (writes $5 a million, reads and lists by the million at $0.50 and $5). A
// ceiling met refuses everyone, so the per-address caps sit far above one
// person's use and far below the ceiling: filling a route's ceiling takes at
// least 50 addresses on every route but the three that send mail. Their
// ceilings are the mail plan's: it sends 100 mails a day and 3 000 a month,
// so the three give 96 a day (96 × 31 = 2 976, and the four left over take
// a mail admitted just before midnight that reaches the provider after it),
// codes first — 60 codes, 24 invites, 12 acceptance mails; 12, 5 and 4
// addresses fill them. `addr` is volumeToken(): the exact IPv4, the IPv6
// /48. The master's mailbox (MAIL_PER_*) and the directory keep their daily
// caps in their own objects, where they count only what they store; the
// directory's per-address cap holds sixteen keys' (DIRECTORY_PER_NETWORK ×
// `key`), so the volunteers one network lists never spend one another's day.
// `new` is the share of a ceiling that keys not yet known may fill — a
// certificate issued less than KNOWN_AFTER_MS ago —, so a flood of fresh keys
// leaves the rest of the stamp and directory ceilings to the nodes already
// there.
const HOUR_MS = 3600 * 1000;
const DAY_MS = 24 * HOUR_MS;
const LIMITS = {
  birth:     { addr: 50, all: 20000 },                          // new identities
  stamp:     { key: 500, addr: 1000, all: 50000, new: 37500 },  // roots written: new, or re-signed
  lookup:    { key: 2000, addr: 5000, all: 250000, new: 187500 }, // roots a /timestamp asks about (KV reads)
  code:      { key: 5, to: 5, addr: 5, all: 60 },               // verification mails
  register:  { key: 10, addr: 50, all: 5000 },                  // tries; `all` = registrations written
  invite:    { key: 5, from: 5, to: 3, addr: 5, all: 24 },      // invite mails
  accept:    { key: 20, addr: 20, all: 10000 },                 // acceptances and adds by code
  notify:    { key: 3, to: 3, addr: 3, all: 12 },               // acceptance and code-add mails
  pending:   { key: 100, addr: 200, all: 20000 },               // pickups (one read of the invites each)
  directory: { key: 30, addr: 480, all: 50000, new: 37500 },    // stored registrations (a volunteer renews hourly)
};
const KNOWN_AFTER_MS = 7 * DAY_MS;
// An invite: open for 30 days from its mail, then kept 30 days from its
// acceptance for its inviter's pickup. A node asks while an untaken invite
// of its own is under twice that old.
const INVITE_TTL_MS = 30 * DAY_MS;
// Births the owners' backfill reads from KV in one step: a list and a read of
// each, inside the thousand operations one invocation may make.
const OWNER_BACKFILL_PAGE = 500;
const BACKFILL_DONE = "done";
// Births' claims the birth instance hands to the owners instance in one
// call; while that instance fails, or the owner's brake holds, they wait a
// minute at a time.
const OWED_PAGE = 500;
const OWED_RETRY_MS = 60 * 1000;
// A handover the owners instance has not answered in this long is taken for
// failed, and waits its minute like one that failed outright.
const OWED_CALL_TIMEOUT_MS = 10 * 1000;
// The right to mail an invite's inviter: one call holds it while it sends,
// and a call that never says how its mail went loses it after this long.
const MAIL_LEASE_MS = 10 * 60 * 1000;
// The routes that send mail. Their ceilings count the provider's day, from
// 00:00 UTC, not 24 h from first use: two windows that each straddle a
// midnight would otherwise let both ceilings' mail into one of its days.
const MAIL_ROUTES = new Set(["code", "invite", "notify"]);
// A route's ceiling, once met, refuses everyone until its window ends; each
// refusal's Retry-After adds up to this much at random, so the waiting
// nodes do not all come back in the same second.
const CEILING_JITTER_S = 3600;
// A key's name is kept while the key uses the invite routes; a year unused
// and it is forgotten.
const NAME_RETENTION_MS = 365 * DAY_MS;

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
// Drops stored in a day (24 h from a counter's first use), drained or not,
// per sender, per address (volumeToken) and in all; a refused drop counts
// for none of them.
const MAIL_PER_SENDER_DAY = 30;
const MAIL_PER_ADDR_DAY = 100;
const MAIL_PER_DAY = 5000;
const MAIL_CAP = 20000;                  // rows kept; a full mailbox refuses, it never drops accepted mail
const MAIL_TTL_MS = 30 * 24 * 3600 * 1000;
const MAIL_SIG_SKEW_S = 300;
// Capability directory (Ф16c): reachable volunteers re-register on their
// existing periodic cycles; an entry older than the TTL is dead. K keeps
// the answer a SAMPLE, not a map of the network.
const DIRECTORY_TTL_MS = 2 * 3600 * 1000;
// Volunteers per answer. A client asks once per sync run (cached 10 min on
// its side), so K sets how much of the directory one run enumerates, not
// how often the Worker is asked: at 20 a directory of a few dozen nodes is
// walked in a couple of runs instead of a day of five-at-a-time samples.
const DIRECTORY_K = 20;
const DIRECTORY_CAPS = new Set(["sync", "mbdump", "relay", "mbslices", "lbdump", "lbslices"]);
const DIRECTORY_REG_PER_IP_HOUR = 12;
// Fresh entries one network (IPv4 /24, IPv6 /48) may hold: a provider's block
// shared by honest volunteers fits, and one network cannot fill the directory
// with entries that do not answer.
const DIRECTORY_PER_NETWORK = 16;
// /issuance-stats costs some forty statements across two objects; an isolate
// answers it from memory for this long.
const STATS_MEMO_MS = 60 * 1000;
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
// same equality (so Sybil merging by address still works), no search. v3
// (2026-09-06) hashes addrKey(): the /64 for IPv6 — privacy extensions rotate
// the interface half daily and a merge over the exact address dissolved with
// them; IPv4 input is unchanged, so every existing v4 stamp re-issues
// byte-identical. A stamp whose ipv is older is re-issued on the next
// submission of that root, keeping its original date — authorship priority
// never regresses.
const IP_HASH_VERSION = 3;

// Timestamp-notary budget (Ф20, shadow). Per IDENTITY, not per address — a
// CGNAT cohort shares an address, a botnet does not share a birth history:
//   budget/day = STAMP_BASE_PER_DAY × weight(method, email_class)
//                × age_ramp(witnessed age) ÷ m(conjunction)
// spent as a token bucket STAMP_BURST deep, refilled at budget/day, so a
// re-seal's back-to-back batches pass and a day-old key still gets its first
// batch. Shadow until armed: the ledger records the verdict of each request
// that brings a new root and the response carries it as `not_before`
// advice; what answers 429 today is the per-address brake (BRAKE_STAMP in
// wrangler.toml — WAF-grade, no KV write, sized for a cohort), the route's
// brake per network (BRAKE_ROUTE) and the hard daily caps — on the roots a
// request asks about (LIMITS.lookup) and on the roots it writes, new or
// re-signed (LIMITS.stamp), per key, per address and the route's ceiling —
// far above a node's pace. Only
// a key with a certificate issued here is stamped at all. Inputs the Worker
// can vouch for itself: the mailbox class it verified, the birth it
// witnessed, the stamps it issued.
const STAMP_BUDGET_ARMED = false;
const STAMP_BASE_PER_DAY = 48;
const STAMP_BURST = 6;
const STAMP_AGE_RAMP_DAYS = 7;
const STAMP_MAX_ROOTS = 100;
const STAMP_REQUEST_VERSION = 1;
const BRAKE_RETRY_AFTER_S = 60;

// Enforced on account creation client-side; re-checked here because invite
// codes arrive from the network. Guarantees '#' appears exactly once in an
// invite code and keeps kvKey() substitution unambiguous.
const USERNAME_RE = /^[A-Za-z0-9_-]{3,32}$/;

export default {
  async fetch(request, env) {
    const paused = writesPaused(env);
    if (paused && writes(request)) return unavailable();
    try {
      return await route(request, guardStore(env, paused));
    } catch (e) {
      if (e instanceof StoreUnavailable) {
        console.error("store unavailable:", e.cause);
        return unavailable();
      }
      console.error("unhandled:", e);
      return json({ error: "internal error" }, 500);
    }
  },
};

function unavailable() {
  return json({ error: "temporarily unavailable" }, 503,
              { "Retry-After": String(BRAKE_RETRY_AFTER_S) });
}

function writesPaused(env) {
  // The owner's brake: WRITES_PAUSED, a plain-text variable set in
  // Cloudflare's dashboard, no deploy needed (wrangler.toml keeps dashboard
  // variables across deploys). While it holds anything but "0", every
  // request that writes answers 503 and reads go on.
  const value = String(env.WRITES_PAUSED || "").trim();
  return value !== "" && value !== "0";
}

function writes(request) {
  // Every POST and DELETE. A legacy record upgraded while reading is stopped
  // at the store itself (guardStore); the master's wake goes on — it is how
  // the master drains — and only its address hint waits (handleMailboxWake).
  return request.method === "POST" || request.method === "DELETE";
}

class StoreUnavailable extends Error {
  // A KV or Durable Object call that failed: the request answers 503 and the
  // cause goes to the log only.
  constructor(cause) {
    super("store unavailable");
    this.cause = cause;
  }
}

function guardStore(env, paused) {
  // The KV binding as every handler sees it: a failing call throws
  // StoreUnavailable, whatever the runtime's own error says — and while the
  // owner's brake is on, so does every write.
  const kv = env.RATE_LIMITS;
  const guarded = (method) => async (...args) => {
    if (paused && (method === "put" || method === "delete")) {
      throw new StoreUnavailable(new Error("writes paused by the owner"));
    }
    try {
      return await kv[method](...args);
    } catch (e) {
      throw new StoreUnavailable(e);
    }
  };
  return Object.create(env, {
    RATE_LIMITS: { value: { get: guarded("get"), put: guarded("put"),
                            delete: guarded("delete"), list: guarded("list") } },
  });
}

async function route(request, env) {
  const url = new URL(request.url);
  const { pathname } = url;
  const method = request.method;
  if (pathname === "/health" && method === "GET") {
    return json({ status: "ok", service: "sautium-verify" });
  }
  if (pathname === "/send-verification" && method === "POST") {
    return await handleVerification(request, env);
  }
  if (pathname === "/register-email" && method === "POST") {
    return await handleRegisterEmail(request, env);
  }
  if (pathname === "/send-invite" && method === "POST") {
    return await handleInvite(request, env);
  }
  if (pathname === "/check-email" && method === "GET") {
    return await handleCheckEmail(request, url, env);
  }
  if (pathname === "/accept-invite" && method === "POST") {
    return await handleAcceptInvite(request, env);
  }
  if (pathname === "/pending-accepts" && method === "POST") {
    return await handlePendingAccepts(request, env);
  }
  if (pathname === "/mailbox" && method === "POST") {
    return await handleMailboxPut(request, env);
  }
  if (pathname === "/mailbox" && (method === "GET" || method === "DELETE")) {
    return await handleMailboxDrain(request, url, env);
  }
  if (pathname === "/mailbox/wake" && method === "GET") {
    return await handleMailboxWake(request, url, env);
  }
  if (pathname === "/master-hint" && method === "GET") {
    return await handleMasterHint(request, env);
  }
  if (pathname === "/node-register" && method === "POST") {
    return await handleNodeRegister(request, env);
  }
  if (pathname === "/node-hints" && method === "GET") {
    return await handleNodeHints(request, url, env);
  }
  if (pathname === "/birth-certificate" && method === "POST") {
    return await handleBirthCertificate(request, env);
  }
  if (pathname === "/issuance-stats" && method === "GET") {
    return await handleIssuanceStats(request, env);
  }
  if (pathname === "/birth-certificate" && method === "GET") {
    return await handleBirthCertificateGet(request, url, env);
  }
  if (pathname === "/timestamp" && method === "POST") {
    return await handleTimestamp(request, env);
  }
  return json({ error: "not found" }, 404);
}

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

function knownBirth(record) {
  // A node already known: its certificate was issued KNOWN_AFTER_MS ago or
  // more (a stored v1 record names its birth born_at).
  return Date.now() - Date.parse(record.issued_at || record.born_at) >= KNOWN_AFTER_MS;
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

let statsMemo = null;      // {at, body}: this isolate's last /issuance-stats answer

async function handleIssuanceStats(request, env) {
  const refusal = await routeBrake(env, request, "stats");
  if (refusal) return refusal;
  const now = Date.now();
  if (!statsMemo || now - statsMemo.at > STATS_MEMO_MS) {
    const { days = {}, ...ledger } = (await ledgerCall(env, "/stats")) || {};
    statsMemo = { at: now, body: {
      policy: {
        cert_version: BIRTH_CERT_VERSION,
        pow_difficulty: POW_DIFFICULTY,
        pow_params_version: POW_PARAMS_VERSION,
        adaptive_armed: ADAPTIVE_DIFFICULTY_ARMED,
        adaptive_cap: ADAPTIVE_MULT_CAP,
        stamp_budget_armed: STAMP_BUDGET_ARMED,
        stamp_base_per_day: STAMP_BASE_PER_DAY,
        stamp_burst: STAMP_BURST,
      },
      // The issuance counts per UTC day (births, email upgrades,
      // successions), kept by the ledger: mass minting under a compromised
      // authority key would show up as certificates the notary never counted.
      days,
      ledger,
      // Live reachable-volunteer count (fresh directory entries) — the
      // relay/volunteer share the T-points want, visible for free.
      directory: (await directoryCall(env, `/stats?now=${now}`)).body,
    } };
  }
  return json(statsMemo.body);
}

async function handleBirthCertificate(request, env) {
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
   *
   * The birth is in KV before the ledger records it: a write that fails
   * leaves no ledger row for a birth that never happened. A first issuance
   * then claims the key's invite code for it (the earliest-born key under a
   * digest holds it) through the birth route's own instance, which keeps the
   * claim until the owners instance has taken it (Limiter.alarm): the invite
   * routes' traffic reaches that instance, and no state of it reaches a
   * birth's answer. A claim the birth's own instance could not keep is made
   * at the key's first invite route, under the same birth date
   * (ownerRefusal), as the claim of a key born before claims existed; a
   * repeat issuance claims nothing.
   */
  const { body, refusal: bad } = await readJson(request);
  if (bad) return bad;
  const { pubkey_hex, signature } = body;
  if (!isText(pubkey_hex) || !isText(signature)) {
    return json({ error: "missing fields" }, 400);
  }

  const pubkey = pubkey_hex.toLowerCase();
  if (!isValidPubkeyHex(pubkey)) {
    return json({ error: "invalid pubkey" }, 400);
  }

  const valid = await verifySignature(`birth:${pubkey}`, signature, pubkey);
  if (!valid) {
    return json({ error: "invalid signature" }, 403);
  }

  // Per-address limits as brakes only (plan invariant 6): a birth is a
  // once-per-life event, its real price is the proof peers verify and the
  // ledger's adaptive difficulty. Ten a minute, and LIMITS.birth.addr a day —
  // far above what a cohort behind one address onboards, low enough that one
  // address cannot spend the route's daily ceiling.
  const ip = clientIp(request);
  if (!await brake(env.BRAKE_BIRTH, `birth:${addrKey(ip)}`)) {
    return throttled("rate limited (too many requests from your address)",
                     BRAKE_RETRY_AFTER_S);
  }
  const braked = await routeBrake(env, request, "birth");
  if (braked) return braked;

  let record = await loadBirthRecord(env, pubkey);
  if (!record) {
    const refusal = await admit(env, [
      daily("birth", "addr", await volumeToken(env, ip)),
      daily("birth", "all"),
    ]);
    if (refusal) return refusal;
    const facts = await birthFacts(env, request, pubkey, nowIsoSeconds());
    // Armed, the price is read before signing; the birth is recorded after.
    const score = ADAPTIVE_DIFFICULTY_ARMED ? await ledgerCall(env, "/birth-score", facts) : null;
    const mult = score ? score.m_shadow : 1;
    record = {
      v: BIRTH_CERT_VERSION,
      issued_at: facts.issued_at,
      method: "pow",
      difficulty: Math.min(POW_DIFFICULTY * mult, POW_DIFFICULTY * ADAPTIVE_MULT_CAP),
      params_version: POW_PARAMS_VERSION,
      email_token: null,
      email_class: null,
      email_domain_token: null,
    };
    record[SIG_FIELD] = await signBirthRecord(env, pubkey, record);
    await env.RATE_LIMITS.put(`born:${pubkey}`, JSON.stringify(record));
    await ledgerCall(env, "/birth", score ? { ...facts, m: mult } : facts);
    try {
      await limiterCall(env, "birth", "/owner/owe", {
        digest: await inviteDigest(pubkey), pubkey, issued: Date.parse(record.issued_at), now: Date.now(),
      });
    } catch (e) {
      if (!(e instanceof StoreUnavailable)) throw e;
      console.error("owner claim left to the key's first invite route:", e.cause);
    }
  }

  return json(certFromRecord(pubkey, record));
}

async function handleBirthCertificateGet(request, url, env) {
  /**
   * Public read of an issued certificate: certificates are public facts
   * (anyone may relay/verify one), so no signature is required.
   *
   * GET /birth-certificate?pubkey=HEX
   */
  const refusal = await routeBrake(env, request, "birth-read");
  if (refusal) return refusal;
  const pubkey = (url.searchParams.get("pubkey") || "").toLowerCase();
  if (!isValidPubkeyHex(pubkey)) {
    return json({ error: "invalid pubkey" }, 400);
  }

  const record = await loadBirthRecord(env, pubkey);
  if (!record) {
    return json({ error: "not issued" }, 404);
  }

  return json(certFromRecord(pubkey, record));
}

// -----------------------------------------------------------------------
// Birth ledger — adaptive difficulty (shadow)
// -----------------------------------------------------------------------

function isNativeGlobalV6(ip) {
  // A "real" IPv6 for the dual-stack-prize sensor: native global unicast
  // only. 2000::/3 already excludes ULA (fc00::/7) and link-local
  // (fe80::/10) by range; inside global space we carve out the transition
  // tunnels, which say NOTHING about whether the user could be a v6 peer:
  // 2002::/16 (6to4), 2001:0000::/32 (Teredo), 2001:db8::/32 (docs). A
  // node that reaches Cloudflare only through one of these must not inflate
  // the "v6 works" count that decides whether Ф1 is worth building.
  if (!ip || !ip.includes(":")) return false;
  const head = ip.split("::", 1)[0];
  const groups = head === "" ? [] : head.split(":");
  const g0 = parseInt(groups[0] || "0", 16) || 0;
  const g1 = parseInt(groups[1] || "0", 16) || 0;
  if (g0 < 0x2000 || g0 > 0x3fff) return false;      // not global unicast
  if (g0 === 0x2002) return false;                   // 6to4
  if (g0 === 0x2001 && g1 === 0x0000) return false;  // Teredo
  if (g0 === 0x2001 && g1 === 0x0db8) return false;  // documentation
  return true;
}

function expandV6(ip) {
  // Eight hex groups with :: expanded and each group normalised — textual
  // slicing put 2a00:db8::x and 2a00:db8:0:1::x (the same /48) into
  // different buckets.
  let groups;
  if (ip.includes("::")) {
    const [head, tail] = ip.split("::", 2);
    const h = head ? head.split(":") : [];
    const t = tail ? tail.split(":") : [];
    groups = h.concat(Array(Math.max(0, 8 - h.length - t.length)).fill("0"), t);
  } else {
    groups = ip.split(":");
  }
  return groups.map((g) => (parseInt(g || "0", 16) || 0).toString(16));
}

function subnetOf(ip) {
  if (!ip) return "";
  if (ip.includes(":")) {
    // IPv6: /48 — the customer allocation size, one household or one VPS.
    return expandV6(ip).slice(0, 3).join(":") + "::/48";
  }
  return ip.split(".").slice(0, 3).join(".") + ".0/24";
}

function addrKey(ip) {
  // The per-address axis (brakes, ip_hash): the exact IPv4, the /64 for
  // IPv6 — one household's prefix, stable while privacy extensions rotate
  // the interface half of the address daily.
  if (!ip || !ip.includes(":")) return ip || "";
  return expandV6(ip).slice(0, 4).join(":") + "::/64";
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
  // from one /24 in 24 h doubles), an ASN minting fast. No global term: one
  // address — or an honest launch wave — would raise the price of every
  // birth in its window, for life (n_glob1/n_glob24 stay measurements).
  let m = 1;
  if (c.n_addr24 >= 2) m *= 2;          // 3rd birth from one exact address in a day
  if (c.n_sub24 >= 2) m *= 2;
  if (c.n_sub24 >= 5) m *= 2;
  if (c.n_asn1 >= 20) m *= 2;
  return Math.min(m, ADAPTIVE_MULT_CAP);
}

export class BirthLedger {
  constructor(state, env) {
    this.state = state;
    this.env = env;
    this.sql = state.storage.sql;
    state.blockConcurrencyWhile(async () => this._init());
  }

  async _init() {
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
    // The notary's contact history (Ф20): one row per /timestamp request,
    // signed or not, with the verdict it got — the axes a stamp farm shares
    // (address, subnet, ASN, birth window, cadence) and the distributions
    // the thresholds are tuned on before the budget is armed.
    this.sql.exec(`CREATE TABLE IF NOT EXISTS stamps (
      id INTEGER PRIMARY KEY AUTOINCREMENT, pubkey TEXT NOT NULL, ts INTEGER NOT NULL,
      addr TEXT NOT NULL DEFAULT '', sub TEXT NOT NULL DEFAULT '', asn INTEGER NOT NULL DEFAULT 0,
      n_new INTEGER NOT NULL, n_roots INTEGER NOT NULL, m REAL NOT NULL,
      budget REAL NOT NULL, allowed INTEGER NOT NULL)`);
    this.sql.exec("CREATE INDEX IF NOT EXISTS stamps_ts ON stamps (ts)");
    this.sql.exec("CREATE INDEX IF NOT EXISTS stamps_pubkey_ts ON stamps (pubkey, ts)");
    this.sql.exec("CREATE INDEX IF NOT EXISTS stamps_addr_ts ON stamps (addr, ts)");
    // Per-identity token bucket — atomic here, which the KV counters never were.
    this.sql.exec(`CREATE TABLE IF NOT EXISTS notary_budget (
      pubkey TEXT PRIMARY KEY, tokens REAL NOT NULL, updated INTEGER NOT NULL)`);
    // The issuance counts per UTC day (births, email upgrades, successions),
    // public in /issuance-stats. They were one KV key, stats:issuance, which
    // takes one write a second: two births in a second failed one of them.
    this.sql.exec(`CREATE TABLE IF NOT EXISTS daily (
      day TEXT NOT NULL, field TEXT NOT NULL, n INTEGER NOT NULL, PRIMARY KEY (day, field))`);
    this.sql.exec("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)");
    await this._adoptKvCounts();
  }

  async _adoptKvCounts() {
    // The KV key's history, added once to whatever was counted here since.
    if (!this.env?.RATE_LIMITS
        || this.sql.exec("SELECT v FROM meta WHERE k = 'kv_counts_adopted'").toArray().length) return;
    let legacy;
    try {
      legacy = await this.env.RATE_LIMITS.get("stats:issuance", "json");
    } catch (e) {
      console.error("issuance history unreadable, adopted at the next start:", e);
      return;
    }
    for (const [day, row] of Object.entries(legacy || {})) {
      for (const [field, n] of Object.entries(row || {})) {
        if (Number.isInteger(n) && n > 0) {
          this.sql.exec(
            `INSERT INTO daily (day, field, n) VALUES (?, ?, ?)
             ON CONFLICT(day, field) DO UPDATE SET n = n + excluded.n`, day, field, n);
        }
      }
    }
    this.sql.exec("INSERT INTO meta (k, v) VALUES ('kv_counts_adopted', ?)", nowIsoSeconds());
  }

  _bump(field, now) {
    const day = new Date(now).toISOString().slice(0, 10);
    this.sql.exec(
      `INSERT INTO daily (day, field, n) VALUES (?, ?, 1)
       ON CONFLICT(day, field) DO UPDATE SET n = n + 1`, day, field);
    this.sql.exec("DELETE FROM daily WHERE day < ?",
                  new Date(now - LEDGER_RETENTION_MS).toISOString().slice(0, 10));
  }

  async fetch(request) {
    const url = new URL(request.url);
    if (request.method === "POST" && url.pathname === "/birth") {
      return Response.json(this.recordBirth(await request.json()));
    }
    if (request.method === "POST" && url.pathname === "/birth-score") {
      return Response.json(this.scoreBirth(await request.json()));
    }
    if (request.method === "POST" && url.pathname === "/stamp") {
      return Response.json(this.recordStamp(await request.json()));
    }
    if (request.method === "POST" && url.pathname === "/stamp-score") {
      return Response.json(this.recordStamp(await request.json(), false));
    }
    if (request.method === "POST" && url.pathname === "/method") {
      // An identity upgraded to method:email — counted, and marked on its
      // birth row when this ledger saw the birth.
      const { pubkey, method, now } = await request.json();
      this.sql.exec("UPDATE births SET method = ? WHERE pubkey = ?", method, pubkey);
      this._bump(method, Number(now) || Date.now());
      return Response.json({ ok: true });
    }
    if (request.method === "POST" && url.pathname === "/count") {
      const { field, now } = await request.json();
      this._bump(String(field), Number(now) || Date.now());
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

  scoreBirth({ ts, asn, sub, addr }) {
    // The births that look like this one, and its multiplier — read only.
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
    return { m_shadow: shadowMultiplier(counts), ...counts };
  }

  recordBirth({ pubkey, ts, asn, cc, sub, addr, fam, m }) {
    // `m`: the multiplier the certificate was signed with, once armed — the
    // row keeps what was charged, not a score taken a moment later.
    const prior = this.sql.exec(
      "SELECT m_shadow, n_sub24, n_asn1, n_asn24, n_glob1, n_glob24, n_addr24 FROM births WHERE pubkey = ?",
      pubkey).toArray();
    if (prior.length) return prior[0];               // idempotent, like issuance
    addr = addr || "";
    const { m_shadow: scored, ...counts } = this.scoreBirth({ ts, asn, sub, addr });
    const m_shadow = m ?? scored;
    this.sql.exec(
      `INSERT INTO births (pubkey, ts, asn, cc, sub, method, m_shadow, n_sub24, n_asn1, n_asn24, n_glob1, n_glob24, addr, n_addr24, fam)
       VALUES (?, ?, ?, ?, ?, 'pow', ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      pubkey, ts, asn, cc, sub, m_shadow, counts.n_sub24, counts.n_asn1,
      counts.n_asn24, counts.n_glob1, counts.n_glob24, addr, counts.n_addr24,
      Number(fam) === 6 ? 6 : 4);
    this.sql.exec("DELETE FROM births WHERE ts < ?", ts - LEDGER_RETENTION_MS);
    this._bump("births", ts);
    return { m_shadow, ...counts };
  }

  recordStamp({ pubkey, issued_at, method, email_class, n_new, n_roots, addr, sub, asn, now }, commit = true) {
    // The per-identity verdict for one /timestamp request (see the STAMP_*
    // constants). Only signed requests from certified keys reach the ledger
    // now; a row with no pubkey is one recorded before the Worker refused
    // unsigned requests. Without `commit` the verdict is only read: nothing
    // is charged or recorded (the armed Worker asks it before its caps).
    pubkey = pubkey || ""; addr = addr || ""; sub = sub || "";
    n_new = Number(n_new) || 0; n_roots = Number(n_roots) || 0;
    let verdict = { allowed: true, not_before: null, budget_per_day: null,
                    m: 1, weight: 1, age_days: null, tokens: null, signed: false };
    if (pubkey) {
      const birth = this.sql.exec(
        "SELECT ts, m_shadow FROM births WHERE pubkey = ?", pubkey).toArray()[0];
      // Witnessed age: the birth this ledger saw, else the certificate's own
      // issued_at (identities older than the ledger), else "born now".
      const bornAt = birth ? Number(birth.ts) : (Number(issued_at) || now);
      const ageDays = Math.max(0, (now - bornAt) / 86400e3);
      const ramp = Math.min(1, 0.25 + 0.75 * ageDays / STAMP_AGE_RAMP_DAYS);
      const weight = method === "email" && email_class !== "disposable" ? 2 : 1;
      // Live conjunction (invariant 3 — axes conjoined, never alone): other
      // identities that stamped from this address within the day AND were
      // born within a day of this one. The address alone is a CGNAT cohort;
      // the address and the birth window together is a farm.
      const cohort = addr ? this._count(
        `SELECT count(DISTINCT s.pubkey) AS n
           FROM stamps s JOIN births b ON b.pubkey = s.pubkey
          WHERE s.addr = ? AND s.ts > ? AND s.pubkey <> ? AND abs(b.ts - ?) < ?`,
        addr, now - 86400e3, pubkey, bornAt, 86400e3) : 0;
      let m = Math.max(1, birth ? Number(birth.m_shadow) : 1);
      if (cohort >= 2) m *= 2;
      if (cohort >= 5) m *= 2;
      m = Math.min(m, ADAPTIVE_MULT_CAP);
      const budget = STAMP_BASE_PER_DAY * weight * ramp / m;   // per day
      const refill = budget / 86400e3;                          // per ms
      const row = this.sql.exec(
        "SELECT tokens, updated FROM notary_budget WHERE pubkey = ?", pubkey).toArray()[0];
      let tokens = row
        ? Math.min(STAMP_BURST, Number(row.tokens) + (now - Number(row.updated)) * refill)
        : STAMP_BURST;
      const allowed = n_new === 0 || tokens >= n_new;
      // Shadow charges even a would-be refusal (bounded debt), so the deficit
      // an armed notary would have enforced is visible in not_before.
      if (allowed || !STAMP_BUDGET_ARMED) tokens = Math.max(-STAMP_BURST, tokens - n_new);
      const notBeforeMs = tokens >= 1 ? now : now + (1 - tokens) / refill;
      if (commit) {
        this.sql.exec(
          `INSERT INTO notary_budget (pubkey, tokens, updated) VALUES (?, ?, ?)
           ON CONFLICT(pubkey) DO UPDATE SET tokens = excluded.tokens, updated = excluded.updated`,
          pubkey, tokens, now);
      }
      verdict = { allowed, not_before: Math.ceil(notBeforeMs / 1000),
                  budget_per_day: Number(budget.toFixed(2)), m, weight,
                  age_days: Number(ageDays.toFixed(2)),
                  tokens: Number(tokens.toFixed(3)), signed: true };
    }
    if (!commit) return verdict;
    this.sql.exec(
      `INSERT INTO stamps (pubkey, ts, addr, sub, asn, n_new, n_roots, m, budget, allowed)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      pubkey, now, addr, sub, Number(asn) || 0, n_new, n_roots, verdict.m,
      verdict.budget_per_day ?? 0, verdict.allowed ? 1 : 0);
    this.sql.exec("DELETE FROM stamps WHERE ts < ?", now - LEDGER_RETENTION_MS);
    return verdict;
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
    // The notary in shadow: what the budget WOULD have refused — read at T1
    // before arming. `unsigned` counts requests recorded before the Worker
    // refused them.
    const notary = (since) => ({
      requests: this._count("SELECT count(*) AS n FROM stamps WHERE ts > ?", since),
      new_roots: this._count("SELECT coalesce(sum(n_new), 0) AS n FROM stamps WHERE ts > ?", since),
      unsigned: this._count("SELECT count(*) AS n FROM stamps WHERE ts > ? AND pubkey = ''", since),
      identities: this._count("SELECT count(DISTINCT pubkey) AS n FROM stamps WHERE ts > ? AND pubkey <> ''", since),
      would_throttle: this._count("SELECT count(*) AS n FROM stamps WHERE ts > ? AND allowed = 0", since),
      m2: this._count("SELECT count(*) AS n FROM stamps WHERE ts > ? AND m >= 2", since),
    });
    const days = {};
    for (const r of this.sql.exec("SELECT day, field, n FROM daily ORDER BY day").toArray()) {
      (days[r.day] ||= {})[r.field] = Number(r.n);
    }
    return {
      days,
      total: this._count("SELECT count(*) AS n FROM births"),
      last_24h: hist(day),
      last_7d: hist(week),
      top_asn_7d: topAsn,
      notary: { last_24h: notary(day), last_7d: notary(week) },
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

async function birthFacts(env, request, pubkey, issuedAtIso) {
  // What the ledger scores and records about one birth.
  const cf = request.cf || {};
  const ip = clientIp(request);
  return {
    pubkey,
    issued_at: issuedAtIso,
    ts: Date.parse(issuedAtIso),
    asn: Number(cf.asn) || 0,
    cc: String(cf.country || ""),
    sub: await subnetToken(env, ip),
    // The exact address too (peppered, same pseudonym the timestamp notary
    // uses): a /24 in a cloud provider spans many tenants, one VPS minting
    // twenty identities is a sharper conjunction than its subnet.
    addr: ip && env.IP_PEPPER ? await ipHashUuid(env, ip) : "",
    // NATIVE-IPv6-egress share of the audience — the upper bound of what
    // dual-stack support (plan Ф19/Ф1) could win, measured before any
    // client supports v6. Tunneled/ULA v6 counts as 4: it is not a v6-peer
    // signal (isNativeGlobalV6).
    fam: isNativeGlobalV6(ip) ? 6 : 4,
  };
}

// -----------------------------------------------------------------------
// Admission — the Limiter object, the brakes, the request body
// -----------------------------------------------------------------------

// Counters a Durable Object keeps beside what they bound: the Limiter's, and
// the master mailbox's and the directory's own, where a check, its write and
// its count are one step.
function counterTable(sql) {
  sql.exec(`CREATE TABLE IF NOT EXISTS counters (
    key TEXT PRIMARY KEY, n INTEGER NOT NULL, until INTEGER NOT NULL)`);
  sql.exec("CREATE INDEX IF NOT EXISTS counters_until ON counters (until)");
}

function takeCounters(sql, now, counters) {
  // All or nothing: every counter is read, and only when all have room are
  // all incremented. A window runs from a counter's first use (a single-use
  // nonce is limit 1 for its lifetime); a `utc` counter's ends at the next
  // multiple of its length since the epoch — a day's at 00:00 UTC, Unix time
  // having no leap seconds. → {ok: true}, or {ok: false, refused, retry_after}.
  sql.exec("DELETE FROM counters WHERE until <= ?", now);
  const end = (c) => c.utc ? (Math.floor(now / c.window) + 1) * c.window : now + c.window;
  const rows = counters.map((c) => sql.exec(
    "SELECT n, until FROM counters WHERE key = ?", c.key).toArray()[0]);
  for (let i = 0; i < counters.length; i++) {
    const c = counters[i], row = rows[i];
    if ((row ? Number(row.n) : 0) + c.amount > c.limit) {
      const until = row ? Number(row.until) : end(c);
      const jitter = /:(all|new):/.test(c.key) ? Math.floor(Math.random() * CEILING_JITTER_S) : 0;
      return { ok: false, refused: c.key,
               retry_after: Math.max(1, Math.ceil((until - now) / 1000)) + jitter };
    }
  }
  for (const c of counters) {
    sql.exec(
      `INSERT INTO counters (key, n, until) VALUES (?, ?, ?)
       ON CONFLICT(key) DO UPDATE SET n = n + excluded.n`,
      c.key, c.amount, end(c));
  }
  return { ok: true };
}

export class Limiter {
  /**
   * The state the Worker's route limits key on (the master's mailbox and the
   * directory count what they store themselves). One instance per route
   * (its name is the route's: "birth", "invite", …), so a check and its
   * increment are one synchronous step, and one route's load or failure
   * never reaches another's; four more instances hold what spans routes:
   *   counters — the route's (takeCounters).
   *   names    — ("names") the username each key speaks under, as a
   *              peppered token, fixed at its first use of a writing
   *              invite-family route (the username is the salt the key was
   *              derived from: an honest key never has two); a year unused
   *              and it is forgotten.
   *   codes    — ("code") pending email verification codes (their hash) and
   *              the wrong tries each has taken; an alarm removes each when
   *              it expires.
   *   owners   — ("owners") the key each invite code's digest belongs to:
   *              the earliest born, whichever claims first — every birth's
   *              claim comes from the birth instance, the births stored
   *              before claims existed are read once from KV (the backfill,
   *              a page a step; until it is done no owner is answered), and
   *              a key that does not hold its digest claims at its first
   *              invite route under its stored birth.
   *   owed     — (the route's own, "birth") each first birth's claim, until
   *              the owners instance has taken it: the alarm hands them over
   *              a page at a time.
   *   invites  — ("invites") each mailed invite under its id (its token's
   *              hash: never the token, the recipient or the message), its
   *              inviter, and once spent its acceptor's signed statement; an
   *              alarm removes each when it expires.
   * Counter keys and names hold public keys and peppered tokens, never an
   * address, a mailbox or a username; an invite keeps its two invite codes
   * while it lives, as an acceptance in KV did.
   */
  constructor(state, env) {
    this.state = state;
    this.env = env;
    this.sql = state.storage.sql;
    this.backfill = null;      // the owners' backfill step in flight, if any
    state.blockConcurrencyWhile(async () => this._init());
  }

  _init() {
    counterTable(this.sql);
    this.sql.exec(`CREATE TABLE IF NOT EXISTS names (
      pubkey TEXT PRIMARY KEY, name TEXT NOT NULL, seen INTEGER NOT NULL)`);
    this.sql.exec("CREATE INDEX IF NOT EXISTS names_seen ON names (seen)");
    this.sql.exec(`CREATE TABLE IF NOT EXISTS codes (
      invite TEXT PRIMARY KEY, email TEXT NOT NULL, hash TEXT NOT NULL,
      tries INTEGER NOT NULL, expires INTEGER NOT NULL)`);
    this.sql.exec(`CREATE TABLE IF NOT EXISTS owners (
      digest TEXT PRIMARY KEY, pubkey TEXT NOT NULL, issued INTEGER NOT NULL)`);
    this.sql.exec(`CREATE TABLE IF NOT EXISTS invites (
      id TEXT PRIMARY KEY, inviter TEXT NOT NULL, inviter_code TEXT NOT NULL,
      sent INTEGER NOT NULL, expires INTEGER NOT NULL,
      acceptor TEXT, acceptor_code TEXT, accepted INTEGER, statement_ts INTEGER, signature TEXT,
      notified INTEGER NOT NULL DEFAULT 0, mailing INTEGER)`);
    this.sql.exec(`CREATE INDEX IF NOT EXISTS invites_accepted ON invites (inviter, accepted)
                   WHERE acceptor IS NOT NULL`);
    this.sql.exec("CREATE INDEX IF NOT EXISTS invites_expires ON invites (expires)");
    this.sql.exec("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)");
    this.sql.exec(`CREATE TABLE IF NOT EXISTS owed (
      pubkey TEXT PRIMARY KEY, digest TEXT NOT NULL, issued INTEGER NOT NULL)`);
  }

  async fetch(request) {
    const path = new URL(request.url).pathname;
    const body = await request.json();
    if (path === "/take") return Response.json(takeCounters(this.sql, body.now, body.counters));
    if (path === "/bind") return Response.json(this.bind(body));
    if (path === "/name") {
      const row = this.sql.exec("SELECT name FROM names WHERE pubkey = ? AND seen > ?",
                                body.pubkey, body.now - NAME_RETENTION_MS).toArray()[0];
      return Response.json({ name: row ? row.name : null });
    }
    if (path === "/code/put") return Response.json(await this.putCode(body));
    if (path === "/code/check") return Response.json(this.checkCode(body));
    if (path === "/code/drop") {
      this.sql.exec("DELETE FROM codes WHERE invite = ?", body.invite);
      return Response.json({ ok: true });
    }
    if (path === "/owner") {
      // The brake as the request carries it, or as this object's env has
      // it: an object takes a new version (a dashboard variable's included)
      // only within Cloudflare's code-update delay, and a request must not
      // wait that long to stop.
      const paused = body.paused === true || writesPaused(this.env);
      if (!await this._ownersReady(paused)) return Response.json({ ready: false });
      return Response.json({ ready: true, owner: this._owner(body.digest) });
    }
    if (path === "/owner/claim") {
      this._claim(body.digest, body.pubkey, body.issued);
      return Response.json({ owner: this._owner(body.digest) });
    }
    if (path === "/owner/claims") {
      for (const [digest, pubkey, issued] of body.claims) this._claim(digest, pubkey, issued);
      return Response.json({ ok: true });
    }
    if (path === "/owner/owe") return Response.json(await this.owe(body));
    if (path === "/invite/put") return Response.json(await this.putInvite(body));
    if (path === "/invite/drop") {
      this.sql.exec("DELETE FROM invites WHERE id = ? AND acceptor IS NULL", body.id);
      return Response.json({ ok: true });
    }
    if (path === "/invite/accept") return Response.json(this.acceptInvite(body));
    if (path === "/invite/mailed") {
      // Told is told; a right given back is cleared only while it is still
      // this call's (a lease that ran out may be another call's by now).
      if (body.sent) this.sql.exec("UPDATE invites SET notified = 1, mailing = NULL WHERE id = ?", body.id);
      else this.sql.exec("UPDATE invites SET mailing = NULL WHERE id = ? AND mailing = ?", body.id, body.lease);
      return Response.json({ ok: true });
    }
    if (path === "/invite/pickup") return Response.json(this.pickup(body));
    return new Response("not found", { status: 404 });
  }

  bind({ pubkey, name, now }) {
    this.sql.exec("DELETE FROM names WHERE seen <= ?", now - NAME_RETENTION_MS);
    const row = this.sql.exec("SELECT name FROM names WHERE pubkey = ?", pubkey).toArray()[0];
    if (row && row.name !== name) return { ok: false };
    this.sql.exec(
      `INSERT INTO names (pubkey, name, seen) VALUES (?, ?, ?)
       ON CONFLICT(pubkey) DO UPDATE SET seen = excluded.seen`, pubkey, name, now);
    return { ok: true };
  }

  async putCode({ invite, email, hash, now }) {
    // A new code replaces the pending one, and its tries start again.
    this.sql.exec("DELETE FROM codes WHERE expires <= ?", now);
    this.sql.exec(
      `INSERT INTO codes (invite, email, hash, tries, expires) VALUES (?, ?, ?, 0, ?)
       ON CONFLICT(invite) DO UPDATE SET email = excluded.email, hash = excluded.hash,
         tries = 0, expires = excluded.expires`,
      invite, email, hash, now + VERIFY_CODE_TTL_MS);
    if (await this.state.storage.getAlarm() === null) {
      await this.state.storage.setAlarm(now + VERIFY_CODE_TTL_MS);
    }
    return { ok: true };
  }

  async alarm() {
    // An expired code (it holds the address it was sent to) or invite is
    // removed when it expires, not when the next one happens to be asked
    // for; an owners' backfill under way takes its next step; births'
    // claims kept here go to the owners instance. Under the owner's brake
    // neither of the last two writes, and each waits a minute at a time.
    const now = Date.now();
    this.sql.exec("DELETE FROM codes WHERE expires <= ?", now);
    this.sql.exec("DELETE FROM invites WHERE expires <= ?", now);
    const at = this._backfillAt();
    const paused = writesPaused(this.env);
    if (at !== null && at !== BACKFILL_DONE && !paused) await this._backfillStep();
    const handed = !paused && await this._handOverClaims();
    const owed = this.sql.exec("SELECT count(*) AS n FROM owed").one().n > 0;
    const due = [
      this.sql.exec("SELECT min(expires) AS t FROM codes").one().t,
      this.sql.exec("SELECT min(expires) AS t FROM invites").one().t,
      at !== null && this._backfillAt() !== BACKFILL_DONE
        ? Date.now() + (paused ? BRAKE_RETRY_AFTER_S * 1000 : 0) : null,
      owed ? Date.now() + (handed ? 0 : OWED_RETRY_MS) : null,
    ].filter((t) => t !== null).map(Number);
    if (due.length) await this.state.storage.setAlarm(Math.min(...due));
  }

  async owe({ digest, pubkey, issued, now }) {
    // A birth's claim, kept until the owners instance has taken it; the
    // alarm hands it over at once unless one is already set (a handover
    // waiting out a failure takes the new claim with it).
    this.sql.exec("INSERT INTO owed (pubkey, digest, issued) VALUES (?, ?, ?) ON CONFLICT(pubkey) DO NOTHING",
                  pubkey, digest, issued);
    if (await this.state.storage.getAlarm() === null) await this.state.storage.setAlarm(now);
    return { ok: true };
  }

  async _handOverClaims() {
    // The oldest page of the claims kept here, in one call to the owners
    // instance; only the claims it took are dropped, by their keys (a claim
    // owed while the call was out stays for the next page). → false when
    // the owners instance did not take them: it failed, answered anything
    // but their taking, or did not answer in time.
    const page = this.sql.exec("SELECT digest, pubkey, issued FROM owed ORDER BY rowid LIMIT ?",
                               OWED_PAGE).toArray();
    if (!page.length) return true;
    let taken, timer;
    const deadline = new Promise((_, fail) => {
      timer = setTimeout(() => fail(new StoreUnavailable(new Error(`no answer in ${OWED_CALL_TIMEOUT_MS} ms`))),
                         OWED_CALL_TIMEOUT_MS);
    });
    try {
      taken = await Promise.race([
        objectCall(this.env.LIMITER, "owners", "/owner/claims",
                   { claims: page.map((c) => [c.digest, c.pubkey, Number(c.issued)]) }),
        deadline,
      ]);
    } catch (e) {
      if (!(e instanceof StoreUnavailable)) throw e;
      console.error("owner claims wait for the owners instance:", e.cause);
      return false;
    } finally {
      clearTimeout(timer);
    }
    if (taken.status !== 200 || taken.body?.ok !== true) {
      console.error("owner claims wait for the owners instance: it answered", taken.status);
      return false;
    }
    this.state.storage.transactionSync(() => {
      for (const c of page) this.sql.exec("DELETE FROM owed WHERE pubkey = ?", c.pubkey);
    });
    return true;
  }

  _owner(digest) {
    const row = this.sql.exec("SELECT pubkey FROM owners WHERE digest = ?", digest).toArray()[0];
    return row ? row.pubkey : null;
  }

  _claim(digest, pubkey, issued) {
    // The earliest-born key under a digest holds it, in whatever order the
    // births and the backfill bring their claims; two born in one second,
    // the lesser key.
    this.sql.exec(
      `INSERT INTO owners (digest, pubkey, issued) VALUES (?, ?, ?)
       ON CONFLICT(digest) DO UPDATE SET pubkey = excluded.pubkey, issued = excluded.issued
       WHERE excluded.issued < owners.issued
          OR (excluded.issued = owners.issued AND excluded.pubkey < owners.pubkey)`, digest, pubkey, issued);
  }

  _backfillAt() {
    // null: not begun; BACKFILL_DONE; else the KV cursor of the next page
    // ("" for the first).
    const row = this.sql.exec("SELECT v FROM meta WHERE k = 'owners_backfill'").toArray()[0];
    return row ? row.v : null;
  }

  async _ownersReady(paused) {
    // An owner is answered only once every birth stored before claims were
    // recorded has been read: until then the earliest of a digest's keys
    // may not have claimed yet. One step per call, the alarm doing the rest
    // — none while the owner's brake holds.
    if (this._backfillAt() === BACKFILL_DONE) return true;
    if (paused) return false;
    await this._backfillStep();
    if (this._backfillAt() === BACKFILL_DONE) return true;
    if (await this.state.storage.getAlarm() === null) await this.state.storage.setAlarm(Date.now());
    return false;
  }

  async _backfillStep() {
    // One at a time: a call and the alarm that meet here share the step.
    this.backfill ||= this._backfillPage().finally(() => { this.backfill = null; });
    await this.backfill;
  }

  async _backfillPage() {
    const at = this._backfillAt() || "";
    if (at === BACKFILL_DONE) return;
    const kv = this.env.RATE_LIMITS;
    const page = await kv.list({ prefix: "born:", limit: OWNER_BACKFILL_PAGE, ...(at ? { cursor: at } : {}) });
    const records = await Promise.all(page.keys.map((k) => kv.get(k.name, "json")));
    const claims = [];
    for (let i = 0; i < page.keys.length; i++) {
      const pubkey = page.keys[i].name.slice("born:".length);
      const issued = records[i] ? Date.parse(records[i].issued_at || records[i].born_at) : NaN;
      if (isValidPubkeyHex(pubkey) && Number.isFinite(issued)) {
        claims.push([await inviteDigest(pubkey), pubkey, issued]);
      }
    }
    // The page's claims and the cursor past it in one transaction.
    this.state.storage.transactionSync(() => {
      for (const [digest, pubkey, issued] of claims) this._claim(digest, pubkey, issued);
      this.sql.exec(
        `INSERT INTO meta (k, v) VALUES ('owners_backfill', ?)
         ON CONFLICT(k) DO UPDATE SET v = excluded.v`,
        page.list_complete ? BACKFILL_DONE : page.cursor);
    });
  }

  async putInvite({ id, inviter, inviter_code, now }) {
    this.sql.exec("DELETE FROM invites WHERE expires <= ?", now);
    this.sql.exec(
      "INSERT INTO invites (id, inviter, inviter_code, sent, expires) VALUES (?, ?, ?, ?, ?)",
      id, inviter, inviter_code, now, now + INVITE_TTL_MS);
    if (await this.state.storage.getAlarm() === null) {
      await this.state.storage.setAlarm(now + INVITE_TTL_MS);
    }
    return { ok: true };
  }

  acceptInvite({ id, acceptor, acceptor_code, their_code, statement_ts, signature, mailable, now }) {
    // The token's one spending, in one step: an open invite becomes this
    // acceptor's, kept 30 days for its inviter's pickup; the same acceptor
    // asking again finds its answer again, and whether its inviter was told.
    // The right to mail the inviter (`mail`, until `lease`) goes to one call
    // at a time, and only while the inviter is untold and `mailable` — the
    // key the caller found a verified address for — is the inviter's.
    const row = this.sql.exec(
      "SELECT inviter, inviter_code, acceptor, notified, mailing FROM invites WHERE id = ? AND expires > ?",
      id, now).toArray()[0];
    if (!row) return { ok: false, reason: "not_found" };
    if (row.inviter === acceptor) return { ok: false, reason: "own" };
    if (row.inviter_code !== their_code) return { ok: false, reason: "not_this_code" };
    if (row.acceptor !== null && row.acceptor !== acceptor) return { ok: false, reason: "used" };
    const told = Number(row.notified) === 1;
    const mail = !told && mailable === row.inviter && (row.mailing === null || Number(row.mailing) <= now);
    const lease = mail ? now + MAIL_LEASE_MS : row.mailing;
    if (row.acceptor === null) {
      this.sql.exec(
        `UPDATE invites SET acceptor = ?, acceptor_code = ?, accepted = ?, statement_ts = ?,
           signature = ?, expires = ?, mailing = ? WHERE id = ?`,
        acceptor, acceptor_code, now, statement_ts, signature, now + INVITE_TTL_MS, lease, id);
    } else if (mail) {
      this.sql.exec("UPDATE invites SET mailing = ? WHERE id = ?", lease, id);
    }
    return { ok: true, inviter: row.inviter, inviter_code: row.inviter_code, notified: told, mail,
             lease: mail ? lease : null };
  }

  pickup({ inviter, now }) {
    // Every accepted, unexpired invite of this inviter, the newest acceptance
    // first: a page past which older ones wait never hides a fresh one, and
    // the oldest are the ones the inviter's node took long ago. A read: that
    // node keeps which it has taken.
    return { accepts: this.sql.exec(
      `SELECT id, accepted, acceptor, acceptor_code, statement_ts, signature FROM invites
        WHERE inviter = ? AND acceptor IS NOT NULL AND expires > ?
        ORDER BY accepted DESC, id DESC LIMIT ?`, inviter, now, PICKUP_PAGE).toArray() };
  }

  checkCode({ invite, email, hash, now }) {
    // Every wrong try counts against the pending code; past VERIFY_CODE_TRIES
    // the code is spent, the right one included. A right code stays until
    // the registration it proves is written (/code/drop).
    const row = this.sql.exec(
      "SELECT email, hash, tries FROM codes WHERE invite = ? AND expires > ?",
      invite, now).toArray()[0];
    if (!row) return { ok: false, spent: false };
    if (Number(row.tries) >= VERIFY_CODE_TRIES) return { ok: false, spent: true };
    if (row.email === email && row.hash === hash) return { ok: true };
    this.sql.exec("UPDATE codes SET tries = tries + 1 WHERE invite = ?", invite);
    return { ok: false, spent: Number(row.tries) + 1 >= VERIFY_CODE_TRIES };
  }
}

async function objectCall(namespace, name, path, body) {
  // One call to a Durable Object the request cannot do without: a failure is
  // the store's (503), never an error text in the answer.
  try {
    const stub = namespace.get(namespace.idFromName(name));
    const res = await stub.fetch(`https://object${path}`, body === undefined ? undefined : {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    return { status: res.status, body: await res.json() };
  } catch (e) {
    throw new StoreUnavailable(e);
  }
}

async function limiterCall(env, instance, path, body) {
  return (await objectCall(env.LIMITER, instance, path, body)).body;
}

function daily(route, axis, id = "", amount = 1) {
  // One of a route's counters (LIMITS) over 24 h; a mail route's ceiling
  // over the UTC day (MAIL_ROUTES).
  return { key: `${route}:${axis}:${id}`, limit: LIMITS[route][axis], window: DAY_MS, amount,
           utc: axis === "all" && MAIL_ROUTES.has(route) };
}

async function admit(env, counters, bind) {
  // Take the counters — all of one route, in that route's own Limiter — and,
  // when `bind` is given, hold the key to its name: null when admitted, else
  // the refusal to answer with. The name is read before the counters, so a
  // key under another name spends nobody's counters, and bound after them,
  // so the names table grows by admitted requests only.
  const name = bind && await nameToken(env, bind.username);
  const misnamed = () => json({ error: "this key is registered under another username" }, 403);
  if (bind) {
    const held = await limiterCall(env, "names", "/name", { pubkey: bind.pubkey, now: Date.now() });
    if (held.name && held.name !== name) return misnamed();
  }
  const route = counters[0].key.slice(0, counters[0].key.indexOf(":"));
  const r = await limiterCall(env, route, "/take", { now: Date.now(), counters });
  if (!r.ok) return throttled("rate limited", r.retry_after);
  if (bind) {
    const named = await limiterCall(env, "names", "/bind", { pubkey: bind.pubkey, name, now: Date.now() });
    if (!named.ok) return misnamed();
  }
  return null;
}

async function routeBrake(env, request, route) {
  // The per-minute brake every route stands behind (births and stamps
  // behind their own per-/64 brake too): its own bucket per route and
  // network — the exact IPv4, the IPv6 /48 (one allocation, one bucket) —
  // and no KV, so what one network sends past it never reaches the stores.
  if (await brake(env.BRAKE_ROUTE, `${route}:${netKey(clientIp(request))}`)) return null;
  return throttled("rate limited (too many requests from your address)", BRAKE_RETRY_AFTER_S);
}

function netKey(ip) {
  return ip.includes(":") ? subnetOf(ip) : ip;
}

function clientIp(request) {
  return request.headers.get("CF-Connecting-IP") || "";
}

async function volumeToken(env, ip) {
  // The per-address axis of the daily caps: the exact IPv4, the IPv6 /48 —
  // one customer allocation (a /64 axis would hand one allocation 65 536
  // buckets). Peppered like every address the Worker keeps.
  return ip.includes(":") ? `6.${await subnetToken(env, ip)}` : `4.${await ipHashUuid(env, ip)}`;
}

async function readJson(request) {
  // A POST body as the Worker's callers send it — application/json, a JSON
  // object, at most MAX_BODY_BYTES — as {body}, or {refusal}.
  const type = (request.headers.get("Content-Type") || "").split(";")[0].trim().toLowerCase();
  if (type !== "application/json") {
    return { refusal: json({ error: "expected application/json" }, 415) };
  }
  if (!request.body) {
    return { refusal: json({ error: "empty body" }, 400) };
  }
  if (Number(request.headers.get("Content-Length")) > MAX_BODY_BYTES) {
    return { refusal: json({ error: "body too large" }, 413) };
  }
  const reader = request.body.getReader();
  const chunks = [];
  let size = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > MAX_BODY_BYTES) {
      await reader.cancel();
      return { refusal: json({ error: "body too large" }, 413) };
    }
    chunks.push(value);
  }
  const bytes = new Uint8Array(size);
  let at = 0;
  for (const c of chunks) { bytes.set(c, at); at += c.byteLength; }
  let body;
  try {
    body = JSON.parse(new TextDecoder().decode(bytes));
  } catch {
    return { refusal: json({ error: "invalid JSON" }, 400) };
  }
  if (!body || typeof body !== "object" || Array.isArray(body)) {
    return { refusal: json({ error: "expected a JSON object" }, 400) };
  }
  return { body };
}

function isText(value) {
  return typeof value === "string" && value.length > 0;
}

// -----------------------------------------------------------------------
// Master mailbox (Ф16) — store-and-forward for ONE recipient, E2E opaque
// -----------------------------------------------------------------------

function mailboxStub(env) {
  return env.MAILBOX.get(env.MAILBOX.idFromName(MASTER_PUBKEY));
}

async function mailboxCall(env, path, body) {
  return await objectCall(env.MAILBOX, MASTER_PUBKEY, path, body);
}

function tsFresh(ts) {
  const n = Number(ts);
  return Number.isFinite(n) && Math.abs(Date.now() / 1000 - n) <= MAIL_SIG_SKEW_S;
}

async function handleMailboxPut(request, env) {
  /**
   * Body: {to, from_public_key, encrypted, timestamp, message_uuid, signature}
   * — the exact chat wire payload plus a sender signature over
   *   mailbox:v1:{to}:{message_uuid}:{timestamp}:{sha256hex(encrypted)}
   * `to` must be the master; the sender must hold a certificate issued
   * here (no free keys); the mailbox object's daily caps — per sender, per
   * address, in all — and its row cap bound the storage. The Worker never
   * sees plaintext.
   */
  if (!env.MAILBOX) return json({ error: "mailbox not configured" }, 503);
  const braked = await routeBrake(env, request, "mailbox");
  if (braked) return braked;
  const { body, refusal: bad } = await readJson(request);
  if (bad) return bad;
  const { to, from_public_key, encrypted, timestamp, message_uuid, signature } = body;
  if (![to, from_public_key, encrypted, timestamp, message_uuid, signature].every(isText)) {
    return json({ error: "missing fields" }, 400);
  }
  if (to.toLowerCase() !== MASTER_PUBKEY) {
    return json({ error: "no mailbox for this recipient" }, 404);
  }
  const sender = from_public_key.toLowerCase();
  if (!isValidPubkeyHex(sender) || sender === MASTER_PUBKEY) {
    return json({ error: "invalid sender" }, 400);
  }
  if (typeof encrypted !== "string" || encrypted.length > MAIL_MAX_CHARS) {
    return json({ error: "message too large" }, 413);
  }
  if (!/^[0-9a-f-]{36}$/.test(message_uuid) || typeof timestamp !== "string" || timestamp.length > 40) {
    return json({ error: "invalid message" }, 400);
  }
  const digest = await sha256Hex(encrypted);
  const message = `mailbox:v1:${MASTER_PUBKEY}:${message_uuid}:${timestamp}:${digest}`;
  if (!await verifySignature(message, signature, sender)) {
    return json({ error: "invalid signature" }, 403);
  }
  if (!await env.RATE_LIMITS.get(`born:${sender}`, "json")) {
    return json({ error: "sender has no certificate" }, 403);
  }
  const r = await mailboxCall(env, "/put", {
    uuid: message_uuid, sender, addr: await volumeToken(env, clientIp(request)),
    encrypted, ts: timestamp, now: Date.now(),
  });
  return objectAnswer(r);
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

async function handleMailboxDrain(request, url, env) {
  /**
   * GET  /mailbox?public_key_hex&ts&signature&after=N  → {messages, more}
   *      signature over mailbox-drain:v1:{ts}:{after}
   * DELETE /mailbox?public_key_hex&ts&signature&upto=N → {deleted}
   *      signature over mailbox-ack:v1:{ts}:{upto}
   * The master imports what it drains (dedup by message_uuid on its side)
   * and acks by id; an unacked batch is served again next time.
   */
  if (!env.MAILBOX) return json({ error: "mailbox not configured" }, 503);
  if (request.method === "GET") {
    const after = String(parseInt(url.searchParams.get("after") || "0") || 0);
    const err = await verifyMasterRequest(url, "mailbox-drain", after);
    if (err) return json({ error: err }, 403);
    const r = await mailboxCall(env, `/drain?after=${after}`);
    return json(r.body, r.status);
  }
  const upto = String(parseInt(url.searchParams.get("upto") || "0") || 0);
  const err = await verifyMasterRequest(url, "mailbox-ack", upto);
  if (err) return json({ error: err }, 403);
  const r = await mailboxCall(env, "/ack", { upto: Number(upto) });
  return json(r.body, r.status);
}

async function handleMailboxWake(request, url, env) {
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
  if (!env.MAILBOX) return json({ error: "mailbox not configured" }, 503);
  const peerPort = url.searchParams.get("peer_port") || "";
  const err = await verifyMasterRequest(url, "mailbox-wake",
                                        peerPort === "" ? undefined : peerPort);
  if (err) return json({ error: err }, 403);
  // The hint is recorded on the SIGNED request itself (before the upgrade
  // gate): the signature is the authority, the upgrade only carries the
  // wake stream.
  const port = parseInt(peerPort);
  if (Number.isInteger(port) && port > 0 && port < 65536 && !writesPaused(env)) {
    const sigNonce = `wakesig:${await sha256Hex((url.searchParams.get("signature") || "").toLowerCase())}`;
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
    return json({ error: "websocket upgrade required" }, 426);
  }
  return await mailboxStub(env).fetch(new Request("https://mailbox/wake", request));
}

async function handleMasterHint(request, env) {
  /**
   * The master's last known peer address — a DISCOVERY tier of last
   * resort, never a trust statement: whoever dials it still faces the
   * full peer handshake (pinned pubkey, certificates, signatures), and
   * the address itself is public anyway through the master's own DHT
   * announces. Empty until the master's wake socket has connected once.
   */
  const refusal = await routeBrake(env, request, "master-hint");
  if (refusal) return refusal;
  const hint = await env.RATE_LIMITS.get("master:hint", "json");
  return json(hint || {});
}

async function directoryCall(env, path, body) {
  return await objectCall(env.NODE_DIRECTORY, "nodes", path, body);
}

async function handleNodeRegister(request, env) {
  /**
   * Body: {pubkey, port, capabilities: [..], ts, signature}
   *   signature over sautium-directory:v1:{port}:{caps sorted, comma}:{ts}
   * The ADDRESS is never taken from the client: the edge observed it
   * (CF-Connecting-IP), so a registration cannot point at somebody else —
   * and the signature is single-use (a limiter nonce), so a captured request
   * replayed from another host cannot re-point the entry either (the same
   * two guards the master hint uses). Requires a certificate issued here;
   * the master itself is refused — it has /master-hint, and clients keep
   * it as the LAST tier by design. One network holds at most
   * DIRECTORY_PER_NETWORK fresh entries; registrations are capped per /64
   * an hour, and per key, per address and in all a day (LIMITS.directory) —
   * the day's counted by the directory, of the registrations it stores; a
   * key not yet known fills only the newcomers' share of the day's ceiling.
   */
  const braked = await routeBrake(env, request, "node-register");
  if (braked) return braked;
  const { body, refusal: bad } = await readJson(request);
  if (bad) return bad;
  const { pubkey, port, capabilities, ts, signature } = body;
  const key = String(pubkey || "").toLowerCase();
  if (!isValidPubkeyHex(key) || !isText(signature)) {
    return json({ error: "missing fields" }, 400);
  }
  if (key === MASTER_PUBKEY) {
    return json({ error: "the master publishes /master-hint" }, 403);
  }
  const p = parseInt(port);
  if (!Number.isInteger(p) || p <= 0 || p >= 65536) {
    return json({ error: "invalid port" }, 400);
  }
  if (!Array.isArray(capabilities) || capabilities.length === 0
      || capabilities.length > DIRECTORY_CAPS.size
      || !capabilities.every((c) => DIRECTORY_CAPS.has(c))) {
    return json({ error: "invalid capabilities" }, 400);
  }
  if (!tsFresh(ts)) {
    return json({ error: "stale timestamp" }, 403);
  }
  const caps = [...new Set(capabilities)].sort();
  const message = `sautium-directory:v1:${p}:${caps.join(",")}:${Number(ts)}`;
  if (!await verifySignature(message, signature, key)) {
    return json({ error: "invalid signature" }, 403);
  }
  const ip = clientIp(request);
  if (!ip) return json({ error: "no source address" }, 400);
  const born = await env.RATE_LIMITS.get(`born:${key}`, "json");
  if (!born) {
    return json({ error: "node has no certificate" }, 403);
  }
  const nonce = `directory:sig:${signature.toLowerCase()}`;
  const taken = await limiterCall(env, "directory", "/take", { now: Date.now(), counters: [
    { key: nonce, limit: 1, window: 2 * MAIL_SIG_SKEW_S * 1000, amount: 1 },
    { key: `directory:hour:${await ipHashUuid(env, ip)}`, limit: DIRECTORY_REG_PER_IP_HOUR,
      window: HOUR_MS, amount: 1 },
  ] });
  if (!taken.ok) {
    return taken.refused === nonce
      ? json({ error: "signature already used" }, 403)
      : throttled("rate limited", taken.retry_after);
  }
  const r = await directoryCall(env, "/register", {
    pubkey: key, host: ip, fam: ip.includes(":") ? 6 : 4, port: p,
    caps: caps.join(","), addr: await volumeToken(env, ip), known: knownBirth(born), now: Date.now(),
  });
  return objectAnswer(r);
}

function objectAnswer(r) {
  // An object's answer as the caller's; a refusal for volume says when to
  // come back, as the Limiter's do.
  return r.body.retry_after
    ? json(r.body, r.status, { "Retry-After": String(r.body.retry_after) })
    : json(r.body, r.status);
}

async function handleNodeHints(request, url, env) {
  const refusal = await routeBrake(env, request, "node-hints");
  if (refusal) return refusal;
  const cap = url.searchParams.get("cap") || "";
  if (!DIRECTORY_CAPS.has(cap)) {
    return json({ error: "unknown capability" }, 400);
  }
  const r = await directoryCall(env, `/hints?cap=${cap}&now=${Date.now()}`);
  return json(r.body, r.status);
}

export class NodeDirectory {
  constructor(state, env) {
    this.state = state;
    this.sql = state.storage.sql;
    state.blockConcurrencyWhile(async () => this._init());
  }

  _init() {
    // net4/net6: the network of each address (subnetOf — IPv4 /24, IPv6 /48),
    // the axis the per-network cap and the distinct-network total count on.
    this.sql.exec(`CREATE TABLE IF NOT EXISTS nodes (
      pubkey TEXT PRIMARY KEY, host4 TEXT, host6 TEXT, port INTEGER NOT NULL,
      caps TEXT NOT NULL, ts INTEGER NOT NULL, net4 TEXT, net6 TEXT)`);
    // Columns added after the first deployment (SQLite: ADD COLUMN only). The
    // rows from before get theirs once, when the columns come — not a read of
    // every row at every start; a renewal writes them anyway.
    let added = false;
    for (const ddl of ["ALTER TABLE nodes ADD COLUMN net4 TEXT",
                       "ALTER TABLE nodes ADD COLUMN net6 TEXT"]) {
      try { this.sql.exec(ddl); added = true; } catch (e) { /* already there */ }
    }
    if (added) {
      for (const r of this.sql.exec(
          `SELECT pubkey, host4, host6 FROM nodes
            WHERE (host4 IS NOT NULL AND net4 IS NULL) OR (host6 IS NOT NULL AND net6 IS NULL)`).toArray()) {
        this.sql.exec("UPDATE nodes SET net4 = ?, net6 = ? WHERE pubkey = ?",
                      r.host4 ? subnetOf(r.host4) : null, r.host6 ? subnetOf(r.host6) : null, r.pubkey);
      }
    }
    this.sql.exec("CREATE INDEX IF NOT EXISTS nodes_ts ON nodes (ts)");
    this.sql.exec("CREATE INDEX IF NOT EXISTS nodes_net4_ts ON nodes (net4, ts)");
    this.sql.exec("CREATE INDEX IF NOT EXISTS nodes_net6_ts ON nodes (net6, ts)");
    // The day's registrations per key, per address and in all
    // (LIMITS.directory), counted here so a registration the network's cap
    // refuses spends none.
    counterTable(this.sql);
  }

  async fetch(request) {
    const url = new URL(request.url);
    if (request.method === "POST" && url.pathname === "/register") {
      const body = await request.json();
      const r = this.state.storage.transactionSync(() => this.register(body));
      return Response.json(r.body, { status: r.status });
    }
    if (request.method === "GET" && url.pathname === "/hints") {
      const cap = url.searchParams.get("cap") || "";
      const now = parseInt(url.searchParams.get("now")) || 0;
      const rows = this.sql.exec(
        `SELECT pubkey, host4, host6, port, caps FROM nodes
          WHERE ts > ? AND (',' || caps || ',') LIKE ?
          ORDER BY RANDOM() LIMIT ?`,
        now - DIRECTORY_TTL_MS, `%,${cap},%`, DIRECTORY_K).toArray();
      // `total`: how many distinct networks the fresh volunteers for this
      // capability sit in — never more than the reachable nodes, so clients
      // can take it as a floor of their network-size estimate, and entries
      // one network mints cannot raise it.
      const total = Number(this.sql.exec(
        `SELECT count(DISTINCT coalesce(net4, net6)) AS n FROM nodes
          WHERE ts > ? AND (',' || caps || ',') LIKE ?`,
        now - DIRECTORY_TTL_MS, `%,${cap},%`).one().n);
      return Response.json({ nodes: rows.map((r) => ({
        pubkey: r.pubkey, host: r.host4 || null, host6: r.host6 || null, port: Number(r.port),
      })), total });
    }
    if (request.method === "GET" && url.pathname === "/stats") {
      const now = parseInt(url.searchParams.get("now")) || 0;
      const fresh = (cap) => Number(this.sql.exec(
        `SELECT count(*) AS n FROM nodes WHERE ts > ? AND (',' || caps || ',') LIKE ?`,
        now - DIRECTORY_TTL_MS, `%,${cap},%`).one().n);
      return Response.json({
        nodes: Number(this.sql.exec("SELECT count(*) AS n FROM nodes WHERE ts > ?",
                                    now - DIRECTORY_TTL_MS).one().n),
        sync: fresh("sync"), mbdump: fresh("mbdump"),
        mbslices: fresh("mbslices"), relay: fresh("relay"),
        lbdump: fresh("lbdump"), lbslices: fresh("lbslices"),
      });
    }
    return new Response("not found", { status: 404 });
  }

  register({ pubkey, host, fam, port, caps, addr, known, now }) {
    if (typeof addr !== "string" || !addr) {
      return { status: 503, body: { error: "temporarily unavailable" } };   // an older Worker's call
    }
    const net = subnetOf(host);
    const crowd = Number(this.sql.exec(
      `SELECT count(*) AS n FROM nodes
        WHERE ${fam === 6 ? "net6" : "net4"} = ? AND ts > ? AND pubkey <> ?`,
      net, now - DIRECTORY_TTL_MS, pubkey).one().n);
    if (crowd >= DIRECTORY_PER_NETWORK) {
      return { status: 429, body: { error: "this network lists enough volunteers" } };
    }
    const taken = takeCounters(this.sql, now, [
      daily("directory", "key", pubkey), daily("directory", "addr", addr), daily("directory", "all"),
      ...(known === true ? [] : [daily("directory", "new")])]);
    if (!taken.ok) {
      return { status: 429, body: { error: "rate limited", retry_after: taken.retry_after } };
    }
    const prev = this.sql.exec("SELECT host4, host6 FROM nodes WHERE pubkey = ?", pubkey).toArray()[0] || {};
    const host4 = fam === 4 ? host : (prev.host4 || null);
    const host6 = fam === 6 ? host : (prev.host6 || null);
    this.sql.exec(
      `INSERT INTO nodes (pubkey, host4, host6, port, caps, ts, net4, net6) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
       ON CONFLICT(pubkey) DO UPDATE SET host4 = excluded.host4, host6 = excluded.host6,
         port = excluded.port, caps = excluded.caps, ts = excluded.ts,
         net4 = excluded.net4, net6 = excluded.net6`,
      pubkey, host4, host6, port, caps, now,
      host4 ? subnetOf(host4) : null, host6 ? subnetOf(host6) : null);
    this.sql.exec("DELETE FROM nodes WHERE ts < ?", now - 7 * 24 * 3600 * 1000);
    return { status: 200, body: { registered: true } };
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
    this.sql.exec("CREATE INDEX IF NOT EXISTS mail_expires ON mail (expires_at)");
    // Every drop, refused ones included, reads only by index: the mail held is
    // a count beside the rows, and the daily caps are counters, which drained
    // mail does not lower. Triggers keep the count, so any code that writes
    // `mail` keeps it exact — an older Worker deployed over this one too; an
    // older object's mail is counted once, here.
    this.sql.exec("CREATE TABLE IF NOT EXISTS held (n INTEGER NOT NULL)");
    if (!this.sql.exec("SELECT n FROM held").toArray().length) {
      this.sql.exec("INSERT INTO held (n) SELECT count(*) FROM mail");
    }
    this.sql.exec(`CREATE TRIGGER IF NOT EXISTS mail_held_in AFTER INSERT ON mail
                   BEGIN UPDATE held SET n = n + 1; END`);
    this.sql.exec(`CREATE TRIGGER IF NOT EXISTS mail_held_out AFTER DELETE ON mail
                   BEGIN UPDATE held SET n = n - 1; END`);
    counterTable(this.sql);
    // The indexes an older object's caps read.
    this.sql.exec("DROP INDEX IF EXISTS mail_sender_recv");
    this.sql.exec("DROP INDEX IF EXISTS mail_recv");
    if (typeof WebSocketRequestResponsePair !== "undefined" && this.state.setWebSocketAutoResponse) {
      // Keepalives answered by the runtime without waking the object.
      this.state.setWebSocketAutoResponse(new WebSocketRequestResponsePair("ping", "pong"));
    }
  }

  async fetch(request) {
    const url = new URL(request.url);
    if (request.method === "POST" && url.pathname === "/put") {
      const body = await request.json();
      const r = this.state.storage.transactionSync(() => this.put(body));
      if (r.body.stored) this._wake();
      return Response.json(r.body, { status: r.status });
    }
    if (request.method === "GET" && url.pathname === "/drain") {
      return Response.json(this.drain(parseInt(url.searchParams.get("after") || "0") || 0));
    }
    if (request.method === "POST" && url.pathname === "/ack") {
      const { upto } = await request.json();
      const deleted = this.state.storage.transactionSync(() => {
        const before = this._held();
        this.sql.exec("DELETE FROM mail WHERE id <= ?", Number(upto) || 0);
        return before - this._held();
      });
      return Response.json({ deleted });
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
      return Response.json({ pending: this._held(),
                             sockets: this.state.getWebSockets ? this.state.getWebSockets().length : 0 });
    }
    return new Response("not found", { status: 404 });
  }

  _held() {
    return Number(this.sql.exec("SELECT n FROM held").one().n);
  }

  put({ uuid, sender, addr, encrypted, ts, now }) {
    if (![uuid, sender, addr, encrypted, ts].every((v) => typeof v === "string" && v)) {
      return { status: 503, body: { error: "temporarily unavailable" } };   // an older Worker's call
    }
    now = Number(now) || Date.now();
    this.sql.exec("DELETE FROM mail WHERE expires_at <= ?", now);
    if (this.sql.exec("SELECT id FROM mail WHERE uuid = ?", uuid).toArray().length) {
      return { status: 200, body: { stored: false, duplicate: true } };
    }
    // Full, it refuses: the sender keeps the message and retries, where an
    // eviction dropped mail already accepted as delivered.
    if (this._held() >= MAIL_CAP) {
      return { status: 429, body: { error: "mailbox full" } };
    }
    const taken = takeCounters(this.sql, now, [
      { key: `mailbox:sender:${sender}`, limit: MAIL_PER_SENDER_DAY, window: DAY_MS, amount: 1 },
      { key: `mailbox:addr:${addr}`, limit: MAIL_PER_ADDR_DAY, window: DAY_MS, amount: 1 },
      { key: "mailbox:all:", limit: MAIL_PER_DAY, window: DAY_MS, amount: 1 },
    ]);
    if (!taken.ok) {
      const error = taken.refused.startsWith("mailbox:sender:") ? "sender cap reached"
        : taken.refused.startsWith("mailbox:addr:") ? "address cap reached" : "mailbox busy";
      return { status: 429, body: { error, retry_after: taken.retry_after } };
    }
    const id = Number(this.sql.exec(
      "INSERT INTO mail (uuid, sender, encrypted, ts, received_at, expires_at) VALUES (?, ?, ?, ?, ?, ?) RETURNING id",
      uuid, sender, encrypted, ts, now, now + MAIL_TTL_MS).one().id);
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

async function handleTimestamp(request, env) {
  /**
   * Notarize Merkle batch roots at the current date — the "when" a signed
   * enrichment record needs for authorship priority (see docs/design/
   * P2P-SYNC-INTEGRITY.md: The karma curve / Notary scaling). The Worker
   * signs roots, not records: a node submits one 32-byte root per batch.
   *
   * Body: {root}            one 64-hex root (the batch shape), or
   *       {roots: [...]}    up to STAMP_MAX_ROOTS (the re-stamp shape),
   *       + pubkey, ts, signature — the submitter's identity (Ф20): sig by the
   *         node key over
   *           sautium-timestamp-request:v{STAMP_REQUEST_VERSION}:{ts}:{roots joined by ","}
   *         with ts within MAIL_SIG_SKEW_S. Required, and the key must hold a
   *         certificate issued here: only a known identity writes `ts:` keys,
   *         at most LIMITS.stamp roots a day (new, or re-signed from an
   *         older format) per key, per address and in all.
   * Returns {root, date, ip_hash, sig, authority, v, not_before} for the batch
   * shape, {stamps: [...], v, not_before} for the re-stamp shape; sig is over
   *   sautium-timestamp:v{TIMESTAMP_VERSION}:{root}:{date}:{ip_hash}
   * by the master authority (BIRTH_SIGNING_KEY, domain-separated from birth
   * certs by the payload prefix). Idempotent per root: the KV record is the
   * transparency-log entry, a re-submitted root keeps its FIRST date and only
   * a record in an older format is re-signed. `not_before` (epoch seconds) is
   * the ledger's pace advice for this identity, which the client's notary
   * waits for. The identity reveals nothing new — root↔author is public in
   * every peer's signing_batches — and notarizing a hash still reveals
   * nothing about the records.
   */
  const { body, refusal: bad } = await readJson(request);
  if (bad) return bad;
  const single = body.roots === undefined;
  const raw = single ? [body.root] : body.roots;
  if (!Array.isArray(raw) || raw.length < 1 || raw.length > STAMP_MAX_ROOTS) {
    return json({ error: "invalid roots" }, 400);
  }
  const roots = raw.map((r) => String(r || "").toLowerCase());
  if (roots.some((r) => !/^[0-9a-f]{64}$/.test(r))) {
    return json({ error: "invalid root" }, 400);
  }
  if (new Set(roots).size !== roots.length) {
    return json({ error: "duplicate root" }, 400);
  }

  const ip = clientIp(request);
  if (!await brake(env.BRAKE_STAMP, `ts:${addrKey(ip)}`)) {
    return throttled("rate limited (too many requests from your address)",
                     BRAKE_RETRY_AFTER_S);
  }
  const braked = await routeBrake(env, request, "stamp");
  if (braked) return braked;

  if (body.pubkey === undefined || body.signature === undefined || body.ts === undefined) {
    return json({ error: "a signed request is required" }, 400);
  }
  const pubkey = String(body.pubkey).toLowerCase();
  if (!isValidPubkeyHex(pubkey)) {
    return json({ error: "invalid pubkey" }, 400);
  }
  if (!tsFresh(body.ts)) {
    return json({ error: "stale timestamp" }, 403);
  }
  const message = `sautium-timestamp-request:v${STAMP_REQUEST_VERSION}:${body.ts}:${roots.join(",")}`;
  if (!await verifySignature(message, String(body.signature), pubkey)) {
    return json({ error: "invalid signature" }, 403);
  }
  const born = await loadBirthRecord(env, pubkey);
  if (!born) {
    return json({ error: "no certificate for this key" }, 403);
  }

  // Every root asked about is a KV read: the lookups are counted before any
  // is made. Of those, a known root costs nothing more (idempotent
  // re-submission); a record in an older format is re-signed, a write the
  // stamp caps count like a new root. The budget charges new roots only,
  // and the ledger hears only of requests that bring one. A key not yet
  // known fills only the newcomers' share of each ceiling.
  const ipToken = await volumeToken(env, ip);
  const newcomer = !knownBirth(born);
  const looked = await admit(env, [
    daily("lookup", "key", pubkey, roots.length),
    daily("lookup", "addr", ipToken, roots.length),
    daily("lookup", "all", "", roots.length),
    ...(newcomer ? [daily("lookup", "new", "", roots.length)] : []),
  ]);
  if (looked) return looked;
  const records = await Promise.all(
    roots.map((r) => env.RATE_LIMITS.get(`ts:${r}`, "json")));
  const fresh = records.filter((r) => !r).length;
  const resigned = records.filter((r) => r && (r.v !== TIMESTAMP_VERSION
                                               || r.ipv !== IP_HASH_VERSION)).length;
  const ipHash = await ipHashUuid(env, ip);
  const cf = request.cf || {};
  const stampFacts = {
    pubkey,
    issued_at: Date.parse(born.issued_at),
    method: born.method || "",
    email_class: born.email_class || "",
    n_new: fresh,
    n_roots: roots.length,
    addr: ipHash,
    sub: await subnetToken(env, ip),
    asn: Number(cf.asn) || 0,
    now: Date.now(),
  };
  // Armed, the budget is a refusal, and no verdict is no stamp.
  const budgetRefusal = (verdict) => {
    if (!verdict) {
      return json({ error: "notary budget unavailable" }, 503,
                  { "Retry-After": String(BRAKE_RETRY_AFTER_S) });
    }
    if (verdict.allowed !== false) return null;
    const wait = Math.max(1, Math.ceil(verdict.not_before - Date.now() / 1000));
    return throttled("notary budget spent for this identity", wait, verdict.not_before);
  };
  if (fresh && STAMP_BUDGET_ARMED) {
    // Read before the daily caps are spent, so a spent budget spends none.
    const refusal = budgetRefusal(await ledgerCall(env, "/stamp-score", stampFacts));
    if (refusal) return refusal;
  }
  const written = fresh + resigned;
  if (written) {
    const refusal = await admit(env, [daily("stamp", "key", pubkey, written),
                                      daily("stamp", "addr", ipToken, written),
                                      daily("stamp", "all", "", written),
                                      ...(newcomer ? [daily("stamp", "new", "", written)] : [])]);
    if (refusal) return refusal;
  }
  // The ledger records — and charges — only a request that passed the caps.
  // Armed, this call is the budget's check and charge in one step: requests
  // in flight together can all pass the read above, and only one step that
  // both checks and charges keeps them inside the budget.
  const verdict = fresh ? await ledgerCall(env, "/stamp", stampFacts) : null;
  if (fresh && STAMP_BUDGET_ARMED) {
    const refusal = budgetRefusal(verdict);
    if (refusal) return refusal;
  }

  // Idempotent per root, but re-sign any record still in an older format so a
  // re-submitted root upgrades. The date is the FIRST-issuance moment —
  // preserved across a re-sign so authorship priority never regresses; only
  // ip_hash reflects whoever re-submits.
  const key = await birthSigningKey(env);
  const stamps = [];
  for (let i = 0; i < roots.length; i++) {
    const root = roots[i];
    let record = records[i];
    if (!record || record.v !== TIMESTAMP_VERSION
        || record.ipv !== IP_HASH_VERSION) {
      const date = record?.date || nowIsoSeconds();
      const sig = bytesToHex(new Uint8Array(await crypto.subtle.sign(
        "Ed25519", key,
        new TextEncoder().encode(
          `sautium-timestamp:v${TIMESTAMP_VERSION}:${root}:${date}:${ipHash}`))));
      record = { v: TIMESTAMP_VERSION, ipv: IP_HASH_VERSION, date,
                 ip_hash: ipHash, sig };
      await env.RATE_LIMITS.put(`ts:${root}`, JSON.stringify(record));
    }
    stamps.push({
      root,
      date: record.date,
      ip_hash: record.ip_hash,
      sig: record.sig,
      authority: TRUSTED_AUTHORITIES[0],
    });
  }
  const policy = { v: TIMESTAMP_VERSION, not_before: verdict?.not_before ?? null };
  return json(single ? { ...stamps[0], ...policy } : { stamps, ...policy });
}

// -----------------------------------------------------------------------
// Email verification & invites
// -----------------------------------------------------------------------

// The invite wire: one shape for the Worker and both node runtimes (MIRRORS
// desktop/p2p/invite_wire.py; the vectors both test suites assert pin the
// two). Codes travel in the short form a client builds (ownInviteName),
// keys in lower-case hex, `ts` in integer seconds; no field can hold ':'.
function sendText(ts, inviteCode, to, messageSha256) {
  return `sautium-invite-send:v1:${ts}:${inviteCode}:${to}:${messageSha256}`;
}

function acceptText(ts, inviteId, theirCode, myCode, pubkey) {
  return `sautium-invite-accept:v1:${ts}:${inviteId}:${theirCode}:${myCode}:${pubkey}`;
}

function addText(ts, theirCode, myCode, pubkey) {
  return `sautium-invite-add:v1:${ts}:${theirCode}:${myCode}:${pubkey}`;
}

function pickupText(ts, inviteCode, pubkey) {
  return `sautium-invite-pickup:v1:${ts}:${inviteCode}:${pubkey}`;
}

function mintInviteToken() {
  // 128 random bits; their one spelling is 32 lower-case hex.
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return bytesToHex(bytes);
}

async function inviteIdOf(token) {
  // An invite's id: the first 16 bytes of SHA-256 over its token, as a UUID
  // — the Worker's key for the invite, the inviter's record of it and the
  // name its acceptor signs. It gives no way back to the token.
  const h = await sha256Hex(`sautium-invite-id:v1:${token}`);
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20, 32)}`;
}

async function keyDigestHex(pubkey) {
  return bytesToHex(new Uint8Array(await crypto.subtle.digest("SHA-256", hexToBytes(pubkey)))).toUpperCase();
}

async function inviteDigest(pubkey) {
  // The 48 bits of a key's SHA-256 its invite code carries, in upper-case hex.
  return (await keyDigestHex(pubkey)).slice(0, 12);
}

async function longInviteCode(username, pubkey) {
  // The code a mail prints: the name and 128 bits of the key's SHA-256 in
  // eight groups of four, so a node binds the person to the key itself.
  // Its first three groups are the short code's.
  return `${username}#${(await keyDigestHex(pubkey)).slice(0, 32).match(/.{4}/g).join("-")}`;
}

async function inviteCaller(env, inviteCode, publicKeyHex, message, signature) {
  // The caller of an invite-family route: its own invite code, exactly as
  // its client builds it, a signature over the route's message, and a key
  // that holds the code's digest. → {pubkey, bind} (bind: the name the
  // limiter holds the key to), or {refusal}.
  const pubkey = publicKeyHex.toLowerCase();
  const username = await ownInviteName(inviteCode, pubkey);
  if (!username) {
    return { refusal: json({ error: "invite code doesn't match public key" }, 403) };
  }
  if (!await verifySignature(message, signature, pubkey)) {
    return { refusal: json({ error: "invalid signature" }, 403) };
  }
  const notOwner = await ownerRefusal(env, pubkey);
  if (notOwner) return { refusal: notOwner };
  return { pubkey, bind: { pubkey, username } };
}

async function ownerRefusal(env, pubkey) {
  // null when this key holds its code's digest — the earliest key born under
  // it — else the answer. A key that does not hold it yet but has a stored
  // birth claims now, under that birth's date, and holds the digest if it
  // was born first: a claim that never landed (a birth's that failed, one
  // the backfill could not list yet, KV's listing trailing its writes) is
  // made up for at the key's first use, whoever claimed meanwhile. Under
  // the owner's brake nothing is claimed.
  const digest = await inviteDigest(pubkey);
  const paused = writesPaused(env);
  const held = await limiterCall(env, "owners", "/owner", { digest, paused });
  if (!held.ready) return unavailable();
  if (held.owner === pubkey) return null;
  const born = await env.RATE_LIMITS.get(`born:${pubkey}`, "json");
  const issued = born ? Date.parse(born.issued_at || born.born_at) : NaN;
  if (!Number.isFinite(issued)) return json({ error: "no certificate for this key" }, 403);
  if (paused) return unavailable();
  const { owner } = await limiterCall(env, "owners", "/owner/claim", { digest, pubkey, issued });
  return owner === pubkey ? null : json({ error: "invite code doesn't match public key" }, 403);
}

async function handleVerification(request, env) {
  /**
   * Generate a verification code, remember its hash, email it.
   *
   * Body: {to, invite_code, public_key_hex, signature, from_username?}
   * Signature is over: "sendcode:{invite_code}:{to}"
   *
   * The code is generated HERE and only its hash is stored (the limiter,
   * 15 min, VERIFY_CODE_TRIES wrong tries). The client never learns the
   * expected value, so possession of the mailbox is proven to the Worker at
   * /register-email — not asserted by the client.
   */
  const braked = await routeBrake(env, request, "code");
  if (braked) return braked;
  const { body, refusal: bad } = await readJson(request);
  if (bad) return bad;
  const { to, invite_code, public_key_hex, signature, from_username } = body;
  if (![to, invite_code, public_key_hex, signature].every(isText)) {
    return json({ error: "missing fields" }, 400);
  }
  if (!isValidEmail(to)) {
    return json({ error: "invalid email" }, 400);
  }
  const caller = await inviteCaller(env, invite_code, public_key_hex,
                                    `sendcode:${invite_code}:${to}`, signature);
  if (caller.refusal) return caller.refusal;

  const refusal = await admit(env, [
    daily("code", "key", caller.pubkey),
    daily("code", "to", await emailToken(env, normalizeEmail(to))),
    daily("code", "addr", await volumeToken(env, clientIp(request))),
    daily("code", "all"),
  ], caller.bind);
  if (refusal) return refusal;

  const code = randomCode(6);
  await limiterCall(env, "code", "/code/put", {
    invite: invite_code, email: to, hash: await sha256Hex(code), now: Date.now(),
  });

  const html = verificationEmailHtml(code, from_username, invite_code);
  const subject = `Sautium — Verification code`;

  const result = await sendEmail(env, to, subject, html);
  if (!result.ok) {
    return json({ error: "email send failed" }, 502);
  }

  return json({ status: "sent" });
}

async function handleRegisterEmail(request, env) {
  /**
   * Register a verified email for an invite code.
   *
   * Body: {invite_code, email, public_key_hex, signature, code, birth_cert}
   * Signature is over: "register:{invite_code}:{email}"
   *
   * Requires:
   * - the verification code the Worker generated and emailed (proves mailbox
   *   ownership to the Worker, not to the client itself); every wrong try
   *   counts against the pending code;
   * - a valid identity certificate for the same pubkey (the KV record carries
   *   born_at, so a verified badge can cite identity age).
   *
   * Stored value: {email, pubkey, born_at, verified_at}. The identity's
   * certificate record is upgraded to method:email with the peppered
   * email_token + email_class (issued_at unchanged) and the new certificate is
   * returned as `birth_cert` so the client persists it in one round-trip.
   */
  const braked = await routeBrake(env, request, "register");
  if (braked) return braked;
  const { body, refusal: bad } = await readJson(request);
  if (bad) return bad;
  const { invite_code, email, public_key_hex, signature, code, birth_cert } = body;
  if (![invite_code, email, public_key_hex, signature, code].every(isText)
      || !birth_cert || typeof birth_cert !== "object") {
    return json({ error: "missing fields" }, 400);
  }
  const caller = await inviteCaller(env, invite_code, public_key_hex,
                                    `register:${invite_code}:${email}`, signature);
  if (caller.refusal) return caller.refusal;
  const pubkey = caller.pubkey;

  if (!await verifyBirthCertificate(birth_cert) || birth_cert.pubkey !== pubkey) {
    return json({ error: "invalid birth certificate" }, 403);
  }
  const record = await loadBirthRecord(env, pubkey);
  if (!record) {
    return json({ error: "certificate not issued here" }, 403);
  }

  const tries = await admit(env, [
    daily("register", "key", pubkey),
    daily("register", "addr", await volumeToken(env, clientIp(request))),
  ], caller.bind);
  if (tries) return tries;
  const check = await limiterCall(env, "code", "/code/check", {
    invite: invite_code, email, hash: await sha256Hex(code.trim().toUpperCase()), now: Date.now(),
  });
  if (!check.ok) {
    return json({ error: check.spent ? "too many wrong codes: request a new one"
                                     : "invalid or expired code" }, 403);
  }
  const full = await admit(env, [daily("register", "all")]);
  if (full) return full;

  const normalized = normalizeEmail(email);
  const token = await emailToken(env, normalized);
  // Succession: the mailbox moves to this key; whoever held it before is
  // named in the certificate as the predecessor (a password change makes a
  // new key — nodes carry witnessed age and bans across the link). The
  // holder is read first and the index moves only after the certificate
  // naming it is stored, so a retry after a failure in between still finds
  // the predecessor; each count is taken right after its own write, so a
  // retry counts nothing twice.
  const holder = await env.RATE_LIMITS.get(`mailbox:${token}`, "json");
  const previous = holder && holder.pubkey !== pubkey ? holder.pubkey : null;
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
    await ledgerCall(env, "/method", { pubkey, method: "email", now: Date.now() });
  }
  if (!holder || previous) {
    await env.RATE_LIMITS.put(`mailbox:${token}`, JSON.stringify({ pubkey, since: nowIsoSeconds() }));
    if (previous) await ledgerCall(env, "/count", { field: "succession", now: Date.now() });
  }

  await env.RATE_LIMITS.put(`verified:${kvKey(invite_code)}`, JSON.stringify({
    email,
    pubkey,
    born_at: upgraded.issued_at,
    verified_at: nowIsoSeconds(),
  }));
  await limiterCall(env, "code", "/code/drop", { invite: invite_code });

  return json({
    status: "registered", invite_code, email,
    birth_cert: certFromRecord(pubkey, upgraded),
  });
}

async function handleInvite(request, env) {
  /**
   * Mail an invite from a key that holds its code (a certificate issued
   * here) and a verified email for this very code: the mail names that
   * address as its sender and carries the invite string — the inviter's long
   * code and a single-use token, which lives in the mail alone. The invite
   * is kept under the token's hash (its id) with its inviter, for 30 days;
   * a mail that fails leaves none.
   *
   * Body: {to, invite_code, public_key_hex, ts, message?, signature}
   * Signature over sendText — ts within MAIL_SIG_SKEW_S, the SHA-256 of the
   * message (of "" when there is none). `message`: optional plain text, at
   * most INVITE_MESSAGE_MAX_CHARS.
   * → {status: "sent", verified_sender: true, invite_id}
   */
  const braked = await routeBrake(env, request, "invite");
  if (braked) return braked;
  const { body, refusal: bad } = await readJson(request);
  if (bad) return bad;
  const { to, invite_code, public_key_hex, ts, signature, message: userMessage } = body;
  if (![to, invite_code, public_key_hex, signature].every(isText) || !Number.isSafeInteger(ts)) {
    return json({ error: "missing fields" }, 400);
  }
  if (userMessage != null && typeof userMessage !== "string") {
    return json({ error: "invalid message" }, 400);
  }
  if (userMessage && [...userMessage].length > INVITE_MESSAGE_MAX_CHARS) {   // characters, as the backend counts
    return json({ error: "message too long" }, 400);
  }
  if (!isValidEmail(to)) {
    return json({ error: "invalid email" }, 400);
  }
  if (!tsFresh(ts)) {
    return json({ error: "stale timestamp" }, 403);
  }
  const caller = await inviteCaller(env, invite_code, public_key_hex,
                                    sendText(ts, invite_code, to, await sha256Hex(userMessage || "")),
                                    signature);
  if (caller.refusal) return caller.refusal;

  const verified = await getVerified(env, invite_code);
  if (!verified || verified.pubkey !== caller.pubkey) {
    return json({ error: "a verified email is required to send invites" }, 403);
  }

  const refusal = await admit(env, [
    daily("invite", "key", caller.pubkey),
    daily("invite", "from", await emailToken(env, normalizeEmail(verified.email))),
    daily("invite", "to", await emailToken(env, normalizeEmail(to))),
    daily("invite", "addr", await volumeToken(env, clientIp(request))),
    daily("invite", "all"),
  ], caller.bind);
  if (refusal) return refusal;

  const token = mintInviteToken();
  const inviteId = await inviteIdOf(token);
  const now = Date.now();
  await limiterCall(env, "invites", "/invite/put", {
    id: inviteId, inviter: caller.pubkey, inviter_code: invite_code, now,
  });
  const username = caller.bind.username;
  const inviteString = `${await longInviteCode(username, caller.pubkey)}#i-${token}`;
  const html = inviteEmailHtml(username, inviteString, verified.email, userMessage,
                               mailDate(now + INVITE_TTL_MS));
  const result = await sendEmail(env, to, `${username} invites you to Sautium`, html);
  if (!result.ok) {
    await limiterCall(env, "invites", "/invite/drop", { id: inviteId });
    return json({ error: "email send failed" }, 502);
  }

  return json({ status: "sent", verified_sender: true, invite_id: inviteId });
}

async function handleCheckEmail(request, url, env) {
  /**
   * Check if an email is already verified for an invite code.
   *
   * GET /check-email?invite_code=X&email=Y&public_key_hex=Z&signature=S
   * Signature over: "check-email:{invite_code}:{email}"
   *
   * Returns {verified: true} only if the stored email matches exactly.
   * Safe: requires signature (proves key ownership), doesn't leak emails.
   * When verified for this very pubkey, also returns (and, if needed,
   * upgrades to) the method:email identity certificate. A read: its only
   * writes are once-only repairs — a certificate record not yet of method
   * email with its domain token upgraded (and the ledger's count of methods
   * told), the mailbox's owner index filled in, a step of the owners'
   * backfill, a key's claim on its own code at its first use (ownerRefusal)
   * — so it takes no counters and binds no name; the brake alone stands in
   * front, and under the owner's brake none of them is made.
   */
  const braked = await routeBrake(env, request, "check");
  if (braked) return braked;
  const inviteCode = url.searchParams.get("invite_code");
  const email = url.searchParams.get("email");
  const publicKeyHex = url.searchParams.get("public_key_hex");
  const signature = url.searchParams.get("signature");
  if (![inviteCode, email, publicKeyHex, signature].every(isText)) {
    return json({ error: "missing fields" }, 400);
  }
  const caller = await inviteCaller(env, inviteCode, publicKeyHex,
                                    `check-email:${inviteCode}:${email}`, signature);
  if (caller.refusal) return caller.refusal;
  const pubkey = caller.pubkey;

  const stored = await getVerified(env, inviteCode);
  const verified = stored?.email === email;
  if (!verified || stored.pubkey !== pubkey) {
    return json({ verified });
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
      await ledgerCall(env, "/method", { pubkey, method: "email", now: Date.now() });
    }
  }
  if (record && record.email_token) {
    await claimMailbox(env, record.email_token, pubkey);   // owner index, no take-over
  }
  return json({
    verified: true,
    birth_cert: record ? certFromRecord(pubkey, record) : null,
  });
}

async function handleAcceptInvite(request, env) {
  /**
   * One route, two texts, and the token tells which. With `invite_token`
   * (acceptText): the invite the token names is spent for this key in one
   * step of the invites instance — open, or already this key's (its answer
   * again); unexpired; sent under `their_invite_code`; not by the caller —
   * and its inviter is mailed once, with no address of the acceptor's: the
   * step hands the right to send to one call at a time, a mail that does
   * not go gives it back for the acceptor's retry, and a mail that went is
   * never a failure of the answer. A sent mail whose outcome could not be
   * recorded — the store failing, or the call cut off before it recorded —
   * keeps the right until its lease runs out (MAIL_LEASE_MS), and a retry
   * after that mails again. Without one (addText): an add by plain
   * code. Nothing is kept, and the code's owner, if it has a verified
   * address, is mailed the adder's username and the code to add back.
   *
   * Body: {my_invite_code, their_invite_code, public_key_hex, ts, invite_token?, signature}
   * → {status: "accepted", invite_id, inviter: {invite_code, public_key_hex}, notified}
   *   {status: "added", notified} — `notified`: whether the inviter, or the
   *   code's owner, has been told by mail
   * A refused invite names its `reason` (INVITE_REFUSALS).
   */
  const braked = await routeBrake(env, request, "accept");
  if (braked) return braked;
  const { body, refusal: bad } = await readJson(request);
  if (bad) return bad;
  const { my_invite_code, their_invite_code, public_key_hex, ts, invite_token, signature } = body;
  if (![my_invite_code, their_invite_code, public_key_hex, signature].every(isText)
      || !Number.isSafeInteger(ts) || (invite_token != null && typeof invite_token !== "string")) {
    return json({ error: "missing fields" }, 400);
  }
  if (!tsFresh(ts)) {
    return json({ error: "stale timestamp" }, 403);
  }
  const theirs = canonicalInviteCode(their_invite_code);
  if (!theirs) {
    return json({ error: "invalid invite code" }, 400);
  }
  const tokened = invite_token != null;
  if (tokened && !/^[0-9a-f]{32}$/.test(invite_token)) return refusedInvite("bad_token");
  const pubkey = public_key_hex.toLowerCase();
  const inviteId = tokened ? await inviteIdOf(invite_token) : null;
  const caller = await inviteCaller(env, my_invite_code, public_key_hex,
                                    tokened ? acceptText(ts, inviteId, theirs, my_invite_code, pubkey)
                                            : addText(ts, theirs, my_invite_code, pubkey),
                                    signature);
  if (caller.refusal) return caller.refusal;
  if (!tokened && theirs === my_invite_code) {
    return json({ error: "invalid invite code" }, 400);
  }

  const refusal = await admit(env, [
    daily("accept", "key", caller.pubkey),
    daily("accept", "addr", await volumeToken(env, clientIp(request))),
    daily("accept", "all"),
  ], caller.bind);
  if (refusal) return refusal;

  const username = caller.bind.username;
  const longCode = await longInviteCode(username, caller.pubkey);
  if (!tokened) {
    // The code's owner hears of it at the address verified by the key that
    // holds the code: a record another key left under it is nobody's.
    const owner = await getVerified(env, theirs);
    const notified = !!owner?.email && await holdsCode(env, owner.pubkey, theirs) && await notify(
      env, request, caller.pubkey, owner.email, `${username} added your Sautium invite code`,
      codeAddEmailHtml(username, longCode));
    return json({ status: "added", notified });
  }

  const inviter = await getVerified(env, theirs);
  const taken = await limiterCall(env, "invites", "/invite/accept", {
    id: inviteId, acceptor: caller.pubkey, acceptor_code: my_invite_code, their_code: theirs,
    statement_ts: ts, signature: signature.toLowerCase(),
    mailable: inviter?.email ? inviter.pubkey : null, now: Date.now(),
  });
  if (!taken.ok) return refusedInvite(taken.reason);
  let notified = taken.notified;
  if (taken.mail) {
    let sent = false;
    try {
      sent = await notify(
        env, request, caller.pubkey, inviter.email, `${username} accepted your Sautium invite`,
        acceptNotificationEmailHtml(username, longCode));
    } finally {
      await recordMail(env, inviteId, taken.lease, sent);
    }
    notified = sent;
  }
  return json({
    status: "accepted", invite_id: inviteId,
    inviter: { invite_code: taken.inviter_code, public_key_hex: taken.inviter }, notified,
  });
}

// What a refused invite answers; the node words each reason.
const INVITE_REFUSALS = {
  bad_token: [400, "invalid invite token"],
  not_found: [404, "no such invite"],              // unknown, or expired
  used: [409, "invite already used"],              // accepted by another key
  not_this_code: [403, "the invite belongs to another code"],
  own: [403, "your own invite"],
};

async function recordMail(env, id, lease, sent) {
  // What became of the mail this call had the right to send: the inviter
  // told, or the right given back. A store failure here is the log's, not
  // the answer's — the acceptance is made, and a right never given back
  // runs out by itself (MAIL_LEASE_MS).
  try {
    await limiterCall(env, "invites", "/invite/mailed", { id, lease, sent });
  } catch (e) {
    if (!(e instanceof StoreUnavailable)) throw e;
    console.error("invite mail's outcome not recorded:", e.cause);
  }
}

async function holdsCode(env, pubkey, code) {
  // True when `pubkey` is the key that holds `code`'s digest.
  if (!isValidPubkeyHex(pubkey)) return false;
  const digest = await inviteDigest(pubkey);
  if (code.split("#")[1].replace(/-/g, "") !== digest) return false;
  const held = await limiterCall(env, "owners", "/owner", { digest, paused: writesPaused(env) });
  return held.ready === true && held.owner === pubkey;
}

function refusedInvite(reason) {
  const [status, error] = INVITE_REFUSALS[reason];
  return json({ error, reason }, status);
}

async function notify(env, request, pubkey, to, subject, html) {
  // An acceptance or code-add mail, under its own caps. → whether it went.
  const limited = await admit(env, [
    daily("notify", "key", pubkey),
    daily("notify", "to", await emailToken(env, normalizeEmail(to))),
    daily("notify", "addr", await volumeToken(env, clientIp(request))),
    daily("notify", "all"),
  ]);
  if (limited) return false;
  return (await sendEmail(env, to, subject, html)).ok;
}

async function handlePendingAccepts(request, env) {
  /**
   * The inviter's accepted invites, each with its acceptor's signed
   * statement, which the inviter's node verifies again: every accepted,
   * unexpired invite of this key, the newest acceptance first, at most
   * PICKUP_PAGE. A read — the inviter's node keeps which it has taken, so a
   * pickup lost on its way loses nothing.
   *
   * Body: {invite_code, public_key_hex, ts, signature}, the signature over
   * pickupText (ts within MAIL_SIG_SKEW_S).
   * → {accepts: [{invite_id, accepted_at, acceptor: {invite_code, public_key_hex, ts, signature}}]}
   */
  const braked = await routeBrake(env, request, "pending");
  if (braked) return braked;
  const { body, refusal: bad } = await readJson(request);
  if (bad) return bad;
  const { invite_code, public_key_hex, ts, signature } = body;
  if (![invite_code, public_key_hex, signature].every(isText) || !Number.isSafeInteger(ts)) {
    return json({ error: "missing fields" }, 400);
  }
  if (!tsFresh(ts)) {
    return json({ error: "stale timestamp" }, 403);
  }
  const caller = await inviteCaller(env, invite_code, public_key_hex,
                                    pickupText(ts, invite_code, public_key_hex.toLowerCase()), signature);
  if (caller.refusal) return caller.refusal;
  const refusal = await admit(env, [
    daily("pending", "key", caller.pubkey),
    daily("pending", "addr", await volumeToken(env, clientIp(request))),
    daily("pending", "all"),
  ], caller.bind);
  if (refusal) return refusal;

  const { accepts } = await limiterCall(env, "invites", "/invite/pickup", {
    inviter: caller.pubkey, now: Date.now(),
  });
  return json({ accepts: accepts.map((r) => ({
    invite_id: r.id,
    accepted_at: new Date(Number(r.accepted)).toISOString(),
    acceptor: { invite_code: r.acceptor_code, public_key_hex: r.acceptor,
                ts: Number(r.statement_ts), signature: r.signature },
  })) });
}

// -----------------------------------------------------------------------
// Crypto helpers
// -----------------------------------------------------------------------

async function verifySignature(message, signatureHex, publicKeyHex) {
  return await verifySignatureBytes(
    new TextEncoder().encode(message), signatureHex, publicKeyHex
  );
}

// The encodings of edwards25519's eight small-order points — y = 0, 1, p-1,
// the two of order 8, and y = p, p+1 written past the field — with the sign
// bit masked off. A signature under such a key proves nothing (anyone can
// make one), whatever a runtime's verifier does with them.
const SMALL_ORDER_KEYS = new Set([
  "0000000000000000000000000000000000000000000000000000000000000000",
  "0100000000000000000000000000000000000000000000000000000000000000",
  "26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc05",
  "c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac037a",
  "ecffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
  "edffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
  "eeffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
]);

function isSmallOrderKey(publicKeyHex) {
  const h = publicKeyHex.toLowerCase();
  const last = (parseInt(h.slice(62), 16) & 0x7f).toString(16).padStart(2, "0");
  return SMALL_ORDER_KEYS.has(h.slice(0, 62) + last);
}

async function verifySignatureBytes(messageBytes, signatureHex, publicKeyHex) {
  // One spelling per key and per signature — exact length, hex digits only,
  // either case — so a nonce taken over a signature's lower-case text is
  // single-use for the signature, not for one of its spellings; and no
  // small-order key.
  if (typeof publicKeyHex !== "string" || typeof signatureHex !== "string"
      || !/^[0-9a-fA-F]{64}$/.test(publicKeyHex) || !/^[0-9a-fA-F]{128}$/.test(signatureHex)
      || isSmallOrderKey(publicKeyHex)) {
    return false;
  }
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

async function ownInviteName(inviteCode, publicKeyHex) {
  /**
   * The caller's own invite code, exactly as every client builds it:
   * "username#XXXX-XXXX-XXXX", where XXXX-XXXX-XXXX = SHA256(public key)[:6]
   * in upper-case hex and username matches USERNAME_RE (enforced at account
   * creation too). → the username, or null for any other spelling: one key,
   * one code.
   */
  if (!isValidPubkeyHex(publicKeyHex)) return null;
  const parts = inviteCode.split("#");
  if (parts.length !== 2 || !USERNAME_RE.test(parts[0])) return null;
  const h = await inviteDigest(publicKeyHex);
  return parts[1] === `${h.slice(0, 4)}-${h.slice(4, 8)}-${h.slice(8)}` ? parts[0] : null;
}

function canonicalInviteCode(inviteCode) {
  /**
   * Somebody else's invite code as a person typed it → the form its owner's
   * client builds, or null. Whose key the digest names is not known here, so
   * only its shape is checked.
   */
  const parts = inviteCode.trim().split("#");
  if (parts.length !== 2 || !USERNAME_RE.test(parts[0])) return null;
  const h = parts[1].replace(/-/g, "").toUpperCase();
  if (!/^[0-9A-F]{12}$/.test(h)) return null;
  return `${parts[0]}#${h.slice(0, 4)}-${h.slice(4, 8)}-${h.slice(8)}`;
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
  // → the provider's Response, or {ok: false} when it could not be reached
  // (the route answers 502 either way; the reason goes to the log).
  try {
    const res = await fetch("https://api.resend.com/emails", {
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
    if (!res.ok) console.error("mail provider refused:", res.status);
    return res;
  } catch (e) {
    console.error("mail provider unreachable:", e);
    return { ok: false };
  }
}

// -----------------------------------------------------------------------
// Rate limiting
// -----------------------------------------------------------------------

async function brake(binding, key) {
  // Per-address emergency brake in a Rate Limiting binding: no KV write, a
  // 60 s window, counted per Cloudflare location — WAF-grade by design
  // (plan invariant 6), never the accounting. Absent binding (the test
  // harness, a dev deploy) = no brake.
  if (!binding) return true;
  const { success } = await binding.limit({ key });
  return success;
}

function throttled(error, retryAfterS, notBefore = null) {
  return json({ error, retry_after: retryAfterS, not_before: notBefore }, 429,
              { "Retry-After": String(retryAfterS) });
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
    <a href="https://sautium.net" style="color: #666;">Sautium</a> — a self-hosted music companion for the collection you own. Free for personal use.
  </p>
</body>
</html>`;
}

// The words of the three invite mails are drafts for the owner's words
// review; the invite string and the codes are the wire's.
const MONTHS = ["January", "February", "March", "April", "May", "June", "July",
                "August", "September", "October", "November", "December"];

function mailDate(ms) {
  const d = new Date(ms);
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]} ${d.getUTCFullYear()}`;
}

function codeBox(text) {
  // A code to paste. Wrapped inside its box on a narrow screen: the node's
  // parse drops whatever whitespace a mail client adds.
  return `<div style="background: #f0f0f0; padding: 12px 16px; border-radius: 8px; margin: 12px 0;">
    <code style="font-size: 16px; font-weight: bold; word-break: break-all;">${escapeHtml(text)}</code>
  </div>`;
}

const MAIL_FOOTER = `<hr style="border: none; border-top: 1px solid #eee; margin: 20px 0;">
  <p style="color: #999; font-size: 11px;">
    <a href="https://sautium.net" style="color: #666;">Sautium</a> — a self-hosted music companion for the collection you own. Free for personal use.
  </p>`;

function inviteEmailHtml(fromUsername, inviteString, verifiedEmail, message, untilDate) {
  const verifiedBadge =
    `<div style="background: #e8f5e9; border: 1px solid #a5d6a7; border-radius: 6px; padding: 8px 12px; margin: 12px 0; font-size: 13px;">
        &#9989; Verified sender: <strong>${escapeHtml(verifiedEmail)}</strong>
       </div>`;
  const name = escapeHtml(fromUsername);

  return `<!DOCTYPE html>
<html>
<body style="font-family: -apple-system, sans-serif; max-width: 480px; margin: 0 auto; padding: 20px;">
  <h2 style="color: #333;">You're invited to Sautium</h2>
  <p><strong>${name}</strong> invites you to connect on Sautium.</p>
  ${verifiedBadge}
  ${message ? `<blockquote style="border-left: 3px solid #ddd; padding-left: 12px; color: #555;">${escapeHtml(message)}</blockquote>` : ""}
  <p>To accept, paste this into Sautium → Friends → Add a friend:</p>
  ${codeBox(inviteString)}
  <p style="font-size: 13px; color: #666;">
    It works once, until ${escapeHtml(untilDate)}. Whoever uses it becomes ${name}'s friend, so keep it to yourself.
  </p>
  <h3 style="color: #333; margin-top: 24px;">What is Sautium?</h3>
  <ul style="color: #555;">
    <li>Search your own library by sound, mood or lyrics — on your machine</li>
    <li>Play from your phone through HQPlayer, a DLNA renderer or the host itself</li>
    <li>An assistant that knows your shelf (sign in with Codex or Claude Code)</li>
    <li>A collectors' network: signed audio analysis and catalogue facts between nodes — never your files</li>
    <li>End-to-end encrypted chat with friends; source-available, free for personal use</li>
  </ul>
  <p>
    <a href="https://sautium.net/download"
       style="display: inline-block; background: #E8B06F; color: #1B1714; padding: 10px 24px;
              border-radius: 6px; text-decoration: none; font-weight: bold;">
      Download Sautium
    </a>
  </p>
  ${MAIL_FOOTER}
</body>
</html>`;
}

function acceptNotificationEmailHtml(fromUsername, longCode) {
  // To the inviter: the invite it mailed was spent. The token tied the
  // acceptance to the mailbox the inviter chose, so the mail names no
  // address of the acceptor's.
  return `<!DOCTYPE html>
<html>
<body style="font-family: -apple-system, sans-serif; max-width: 480px; margin: 0 auto; padding: 20px;">
  <h2 style="color: #333;">Invite accepted!</h2>
  <p><strong>${escapeHtml(fromUsername)}</strong> accepted your invite. Your Sautium adds them by itself — at the latest when you open Friends.</p>
  <div style="background: #e8f5e9; border: 1px solid #a5d6a7; border-radius: 6px; padding: 8px 12px; margin: 12px 0; font-size: 13px;">
    &#9989; Accepted through the invite you sent
  </div>
  <p>Their code:</p>
  ${codeBox(longCode)}
  ${MAIL_FOOTER}
</body>
</html>`;
}

function codeAddEmailHtml(fromUsername, longCode) {
  // To a code's owner: someone added the plain code. The username and the
  // code to add back, nothing else of the adder's.
  return `<!DOCTYPE html>
<html>
<body style="font-family: -apple-system, sans-serif; max-width: 480px; margin: 0 auto; padding: 20px;">
  <h2 style="color: #333;">Your invite code was added</h2>
  <p><strong>${escapeHtml(fromUsername)}</strong> added your invite code. To connect, add theirs in Sautium → Friends → Add a friend:</p>
  ${codeBox(longCode)}
  ${MAIL_FOOTER}
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
  // timestamp string and the `uuid` column all stay as they are. Hashes
  // addrKey() (IP_HASH_VERSION 3): the exact IPv4, the /64 for IPv6.
  const pepper = env.IP_PEPPER;
  if (!pepper) throw new Error("IP_PEPPER is not configured");
  const key = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(pepper),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const mac = new Uint8Array(await crypto.subtle.sign(
    "HMAC", key, new TextEncoder().encode(`ip:${normalizeText(addrKey(ip))}`)));
  const h = mac.slice(0, 16);
  h[6] = (h[6] & 0x0f) | 0x40; // version 4 shape: this is keyed, not a uuid5
  h[8] = (h[8] & 0x3f) | 0x80; // RFC 4122 variant
  const x = bytesToHex(h);
  return `${x.slice(0, 8)}-${x.slice(8, 12)}-${x.slice(12, 16)}-${x.slice(16, 20)}-${x.slice(20)}`;
}

function isValidEmail(email) {
  // One plain address: no whitespace, no list, quoting or display-name
  // syntax — each of those would spell one mailbox several ways past its
  // per-mailbox caps.
  return email.length <= 254
    && /^[^\s@,;:<>()[\]\\"]{1,64}@[^\s@,;:<>()[\]\\"]+\.[^\s@,;:<>()[\]\\"]{2,}$/.test(email);
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

async function nameToken(env, username) {
  // A key's username as the limiter keeps it: equal names, equal tokens, and
  // no name in the table. Same construction as emailToken, its own prefix.
  const pepper = env.EMAIL_PEPPER;
  if (!pepper) throw new Error("EMAIL_PEPPER is not configured");
  const key = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(pepper),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const mac = new Uint8Array(await crypto.subtle.sign(
    "HMAC", key, new TextEncoder().encode(`name:${username}`)));
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

function json(data, status = 200, headers = {}) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}
