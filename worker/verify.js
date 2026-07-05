/**
 * Sautium Email Verification, Invite & Birth Certificate Worker (Cloudflare Workers)
 *
 * Trusted server that:
 * 1. Verifies email ownership (server-side code flow — the Worker generates
 *    and checks the code; clients never see the expected value)
 * 2. Stores verified email records in KV: {email, pubkey, born_at, verified_at}
 * 3. Sends signed invite emails with verified sender info
 * 4. Issues birth certificates — the network's identity-age anchor
 *    (see docs/design/P2P-SYNC-INTEGRITY.md: cert = initial trust weight,
 *    never immunity). Issuance is idempotent: the first request for a pubkey
 *    signs {pubkey, now} into KV; every later request returns the stored
 *    certificate — recreating an account on another device (Argon2id
 *    login+password → same keypair) never changes the birth date.
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
 *
 * Endpoints:
 *   POST /send-verification    — generate + send verification code to email
 *   POST /register-email       — store verified email (requires code + birth cert)
 *   POST /send-invite          — send signed invite email with verified sender
 *   GET  /check-email          — check if email already verified for invite code
 *   POST /accept-invite        — accept an invite (stores reciprocal + notifies sender)
 *   GET  /pending-accepts      — poll for accepted invites (one-time pickup)
 *   POST /birth-certificate    — issue (or return existing) birth certificate
 *   GET  /birth-certificate    — public read of an issued certificate
 *   GET  /health               — health check
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
const BIRTH_CERT_VERSION = 1;

// Enforced on account creation client-side; re-checked here because invite
// codes arrive from the network. Guarantees '#' appears exactly once in an
// invite code and keeps kvKey() substitution unambiguous.
const USERNAME_RE = /^[A-Za-z0-9_-]{3,32}$/;

export default {
  async fetch(request, env) {
    const corsHeaders = {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
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

      if (url.pathname === "/birth-certificate" && request.method === "POST") {
        return await handleBirthCertificate(request, env, corsHeaders);
      }

      if (url.pathname === "/birth-certificate" && request.method === "GET") {
        return await handleBirthCertificateGet(url, env, corsHeaders);
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

function birthPayload(pubkeyHex, bornAtIso) {
  return new TextEncoder().encode(
    `sautium-birth:v${BIRTH_CERT_VERSION}:${pubkeyHex}:${bornAtIso}`
  );
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
    if (!cert || cert.v !== BIRTH_CERT_VERSION) return false;
    if (!TRUSTED_AUTHORITIES.includes(cert.issuer)) return false;
    if (!isValidPubkeyHex(cert.pubkey)) return false;
    return await verifySignatureBytes(
      birthPayload(cert.pubkey, cert.born_at), cert.sig, cert.issuer
    );
  } catch {
    return false;
  }
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

  let record = await env.RATE_LIMITS.get(`born:${pubkey}`, "json");
  if (!record) {
    const bornAt = nowIsoSeconds();
    const key = await birthSigningKey(env);
    const sig = bytesToHex(new Uint8Array(
      await crypto.subtle.sign("Ed25519", key, birthPayload(pubkey, bornAt))
    ));
    record = { born_at: bornAt, sig };
    await env.RATE_LIMITS.put(`born:${pubkey}`, JSON.stringify(record));
  }

  return json({
    v: BIRTH_CERT_VERSION,
    pubkey,
    born_at: record.born_at,
    issuer: TRUSTED_AUTHORITIES[0],
    sig: record.sig,
  }, corsHeaders);
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

  const record = await env.RATE_LIMITS.get(`born:${pubkey}`, "json");
  if (!record) {
    return json({ error: "not issued" }, corsHeaders, 404);
  }

  return json({
    v: BIRTH_CERT_VERSION,
    pubkey,
    born_at: record.born_at,
    issuer: TRUSTED_AUTHORITIES[0],
    sig: record.sig,
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
   * - a valid birth certificate for the same pubkey (the KV record carries
   *   born_at, so a verified badge can cite identity age).
   *
   * Stored value: {email, pubkey, born_at, verified_at}.
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

  if (!await verifyBirthCertificate(birth_cert) ||
      birth_cert.pubkey !== public_key_hex.toLowerCase()) {
    return json({ error: "invalid birth certificate" }, corsHeaders, 403);
  }

  await env.RATE_LIMITS.put(`verified:${kvKey(invite_code)}`, JSON.stringify({
    email,
    pubkey: public_key_hex.toLowerCase(),
    born_at: birth_cert.born_at,
    verified_at: nowIsoSeconds(),
  }));
  await env.RATE_LIMITS.delete(codeKey);

  return json({ status: "registered", invite_code, email }, corsHeaders);
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
  return json({ verified: stored?.email === email }, corsHeaders);
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

function isValidEmail(email) {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email);
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
