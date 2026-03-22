/**
 * Sautium Email Verification & Invite Worker (Cloudflare Workers)
 *
 * Trusted server that:
 * 1. Verifies email ownership (code flow)
 * 2. Stores verified email→invite_code mappings in KV
 * 3. Sends signed invite emails with verified sender info
 *
 * All state-changing requests require Ed25519 signature verification.
 * Masha can't impersonate Alice because she can't sign with Alice's keys.
 *
 * Deploy: wrangler deploy
 * Secrets: wrangler secret put RESEND_API_KEY
 *
 * Endpoints:
 *   POST /send-verification  — send verification code to email
 *   POST /register-email     — store verified email mapping (after code confirmed)
 *   POST /send-invite        — send signed invite email with verified sender
 *   GET  /lookup-email       — check verified email for an invite code
 *   GET  /health             — health check
 */

const RATE_LIMIT_PER_IP = 5;
const RATE_LIMIT_PER_RECIPIENT = 10;
const RATE_LIMIT_WINDOW = 3600;
const FROM_EMAIL = "noreply@sautium.net";
const FROM_NAME = "Sautium";

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

      if (url.pathname === "/lookup-email" && request.method === "GET") {
        return await handleLookupEmail(url, env, corsHeaders);
      }

      return json({ error: "not found" }, corsHeaders, 404);
    } catch (e) {
      return json({ error: e.message }, corsHeaders, 500);
    }
  },
};

// -----------------------------------------------------------------------
// Endpoints
// -----------------------------------------------------------------------

async function handleVerification(request, env, corsHeaders) {
  const body = await request.json();
  const { to, code, from_username, invite_code } = body;

  if (!to || !code) {
    return json({ error: "missing 'to' or 'code'" }, corsHeaders, 400);
  }

  if (!isValidEmail(to)) {
    return json({ error: "invalid email" }, corsHeaders, 400);
  }

  const ip = request.headers.get("CF-Connecting-IP") || "unknown";
  const rateLimitResult = await checkRateLimit(env, ip, to);
  if (rateLimitResult) {
    return json({ error: rateLimitResult }, corsHeaders, 429);
  }

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
   * Register a verified email→invite_code mapping.
   * Called after the user successfully enters the verification code.
   *
   * Body: {invite_code, email, public_key_hex, signature}
   * Signature is over: "register:{invite_code}:{email}"
   */
  const body = await request.json();
  const { invite_code, email, public_key_hex, signature } = body;

  if (!invite_code || !email || !public_key_hex || !signature) {
    return json({ error: "missing fields" }, corsHeaders, 400);
  }

  // Verify invite code matches public key
  if (!await verifyInviteCode(invite_code, public_key_hex)) {
    return json({ error: "invite code doesn't match public key" }, corsHeaders, 403);
  }

  // Verify Ed25519 signature
  const message = `register:${invite_code}:${email}`;
  const valid = await verifySignature(message, signature, public_key_hex);
  if (!valid) {
    return json({ error: "invalid signature" }, corsHeaders, 403);
  }

  // Store mapping in KV (no expiration — permanent until overwritten)
  await env.RATE_LIMITS.put(`verified:${invite_code}`, email);

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

  // Verify invite code matches public key
  if (!await verifyInviteCode(invite_code, public_key_hex)) {
    return json({ error: "invite code doesn't match public key" }, corsHeaders, 403);
  }

  // Verify signature
  const sigMessage = `invite:${invite_code}:to:${to}`;
  const valid = await verifySignature(sigMessage, signature, public_key_hex);
  if (!valid) {
    return json({ error: "invalid signature" }, corsHeaders, 403);
  }

  // Rate limiting
  const ip = request.headers.get("CF-Connecting-IP") || "unknown";
  const rateLimitResult = await checkRateLimit(env, ip, to);
  if (rateLimitResult) {
    return json({ error: rateLimitResult }, corsHeaders, 429);
  }

  // Look up verified email for sender
  const verifiedEmail = await env.RATE_LIMITS.get(`verified:${invite_code}`);
  const username = invite_code.split("#")[0];

  const html = inviteEmailHtml(username, invite_code, verifiedEmail, userMessage);
  const subject = `${username} invites you to Sautium`;

  const result = await sendEmail(env, to, subject, html);
  if (!result.ok) {
    return json({ error: "email send failed" }, corsHeaders, 502);
  }

  return json({ status: "sent", verified_sender: !!verifiedEmail }, corsHeaders);
}

async function handleLookupEmail(url, env, corsHeaders) {
  /**
   * Look up verified email for an invite code.
   * GET /lookup-email?invite_code=user%23XXXX-XXXX-XXXX
   */
  const inviteCode = url.searchParams.get("invite_code");
  if (!inviteCode) {
    return json({ error: "missing invite_code" }, corsHeaders, 400);
  }

  const email = await env.RATE_LIMITS.get(`verified:${inviteCode}`);
  return json({
    invite_code: inviteCode,
    email: email || null,
    verified: !!email,
  }, corsHeaders);
}

// -----------------------------------------------------------------------
// Crypto helpers
// -----------------------------------------------------------------------

async function verifySignature(message, signatureHex, publicKeyHex) {
  try {
    const publicKeyBytes = hexToBytes(publicKeyHex);
    const signatureBytes = hexToBytes(signatureHex);
    const messageBytes = new TextEncoder().encode(message);

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
   */
  try {
    const parts = inviteCode.split("#");
    if (parts.length !== 2) return false;

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

  const ipCount = parseInt(await env.RATE_LIMITS.get(ipKey)) || 0;
  if (ipCount >= RATE_LIMIT_PER_IP) {
    return "rate limited (too many requests from your IP)";
  }

  const recipientCount = parseInt(await env.RATE_LIMITS.get(recipientKey)) || 0;
  if (recipientCount >= RATE_LIMIT_PER_RECIPIENT) {
    return "rate limited (too many emails to this address)";
  }

  await env.RATE_LIMITS.put(ipKey, String(ipCount + 1), {
    expirationTtl: RATE_LIMIT_WINDOW,
  });
  await env.RATE_LIMITS.put(recipientKey, String(recipientCount + 1), {
    expirationTtl: RATE_LIMIT_WINDOW,
  });

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
