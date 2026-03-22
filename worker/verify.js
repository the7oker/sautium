/**
 * Sautium Email Verification Worker (Cloudflare Workers)
 *
 * Sends verification codes via Resend API for P2P friend discovery.
 * Rate limited: 5 emails per IP per hour, 10 per recipient per hour.
 *
 * Deploy: wrangler deploy
 * Secrets: wrangler secret put RESEND_API_KEY
 *
 * Endpoints:
 *   POST /send-verification  — send verification code email
 *   POST /send-invite        — send invite email to non-user
 *   GET  /health             — health check
 */

const RATE_LIMIT_PER_IP = 5;
const RATE_LIMIT_PER_RECIPIENT = 10;
const RATE_LIMIT_WINDOW = 3600; // 1 hour in seconds
const FROM_EMAIL = "noreply@sautium.net";
const FROM_NAME = "Sautium";

export default {
  async fetch(request, env) {
    // CORS headers
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

      if (url.pathname === "/send-invite" && request.method === "POST") {
        return await handleInvite(request, env, corsHeaders);
      }

      return json({ error: "not found" }, corsHeaders, 404);
    } catch (e) {
      return json({ error: e.message }, corsHeaders, 500);
    }
  },
};

async function handleVerification(request, env, corsHeaders) {
  const body = await request.json();
  const { to, code, from_username, invite_code } = body;

  if (!to || !code) {
    return json({ error: "missing 'to' or 'code'" }, corsHeaders, 400);
  }

  // Validate email format
  if (!to.match(/^[^\s@]+@[^\s@]+\.[^\s@]+$/)) {
    return json({ error: "invalid email" }, corsHeaders, 400);
  }

  // Rate limiting
  const ip = request.headers.get("CF-Connecting-IP") || "unknown";
  const rateLimitResult = await checkRateLimit(env, ip, to);
  if (rateLimitResult) {
    return json({ error: rateLimitResult }, corsHeaders, 429);
  }

  // Send email via Resend
  const html = verificationEmailHtml(code, from_username, invite_code);
  const subject = `Sautium — Verification code from ${from_username || "a user"}`;

  const result = await sendEmail(env, to, subject, html);
  if (!result.ok) {
    return json({ error: "email send failed" }, corsHeaders, 502);
  }

  return json({ status: "sent" }, corsHeaders);
}

async function handleInvite(request, env, corsHeaders) {
  const body = await request.json();
  const { to, from_username, invite_code, message } = body;

  if (!to || !invite_code) {
    return json({ error: "missing 'to' or 'invite_code'" }, corsHeaders, 400);
  }

  if (!to.match(/^[^\s@]+@[^\s@]+\.[^\s@]+$/)) {
    return json({ error: "invalid email" }, corsHeaders, 400);
  }

  const ip = request.headers.get("CF-Connecting-IP") || "unknown";
  const rateLimitResult = await checkRateLimit(env, ip, to);
  if (rateLimitResult) {
    return json({ error: rateLimitResult }, corsHeaders, 429);
  }

  const html = inviteEmailHtml(from_username, invite_code, message);
  const subject = `${from_username || "Someone"} invites you to Sautium`;

  const result = await sendEmail(env, to, subject, html);
  if (!result.ok) {
    return json({ error: "email send failed" }, corsHeaders, 502);
  }

  return json({ status: "sent" }, corsHeaders);
}

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

async function checkRateLimit(env, ip, recipient) {
  // Using KV for rate limiting (Cloudflare Workers KV)
  if (!env.RATE_LIMITS) return null; // KV not configured — skip

  const ipKey = `ip:${ip}`;
  const recipientKey = `to:${recipient}`;

  const ipCount = parseInt(await env.RATE_LIMITS.get(ipKey)) || 0;
  if (ipCount >= RATE_LIMIT_PER_IP) {
    return "rate limited (too many requests from your IP)";
  }

  const recipientCount = parseInt(await env.RATE_LIMITS.get(recipientKey)) || 0;
  if (recipientCount >= RATE_LIMIT_PER_RECIPIENT) {
    return "rate limited (too many emails to this address)";
  }

  // Increment counters with TTL
  await env.RATE_LIMITS.put(ipKey, String(ipCount + 1), {
    expirationTtl: RATE_LIMIT_WINDOW,
  });
  await env.RATE_LIMITS.put(recipientKey, String(recipientCount + 1), {
    expirationTtl: RATE_LIMIT_WINDOW,
  });

  return null;
}

function verificationEmailHtml(code, fromUsername, inviteCode) {
  return `
<!DOCTYPE html>
<html>
<body style="font-family: -apple-system, sans-serif; max-width: 480px; margin: 0 auto; padding: 20px;">
  <h2 style="color: #333;">Sautium — Email Verification</h2>
  <p><strong>${fromUsername || "A user"}</strong> wants to add you as a friend on Sautium.</p>
  <p>Enter this code in the app to verify your email:</p>
  <div style="background: #f0f0f0; padding: 20px; text-align: center; border-radius: 8px; margin: 20px 0;">
    <span style="font-size: 32px; font-weight: bold; letter-spacing: 4px; font-family: monospace;">${escapeHtml(code)}</span>
  </div>
  ${inviteCode ? `<p style="color: #666; font-size: 13px;">Their invite code: <code>${escapeHtml(inviteCode)}</code></p>` : ""}
  <p style="color: #999; font-size: 12px;">If you didn't expect this, ignore this email.</p>
  <hr style="border: none; border-top: 1px solid #eee; margin: 20px 0;">
  <p style="color: #999; font-size: 11px;">
    <a href="https://sautium.net" style="color: #666;">Sautium</a> — AI-powered music library for FLAC collectors.
  </p>
</body>
</html>`;
}

function inviteEmailHtml(fromUsername, inviteCode, message) {
  return `
<!DOCTYPE html>
<html>
<body style="font-family: -apple-system, sans-serif; max-width: 480px; margin: 0 auto; padding: 20px;">
  <h2 style="color: #333;">You're invited to Sautium</h2>
  <p><strong>${escapeHtml(fromUsername || "Someone")}</strong> invites you to join Sautium —
     an AI-powered music library for FLAC collectors.</p>
  ${message ? `<blockquote style="border-left: 3px solid #ddd; padding-left: 12px; color: #555;">${escapeHtml(message)}</blockquote>` : ""}
  <p>Their invite code: <code style="background: #f0f0f0; padding: 2px 6px; border-radius: 3px;">${escapeHtml(inviteCode)}</code></p>
  <h3>What is Sautium?</h3>
  <ul>
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
