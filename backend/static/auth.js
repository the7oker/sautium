// Sautium request signing.
//
// Every privileged request to the backend carries:
//   X-Sautium-Ts   unix seconds
//   X-Sautium-Sig  hex(HMAC-SHA256(secret, METHOD\nPATH_AND_QUERY\nTS\nsha256_hex(body)))
//
// The signing key is a DEVICE TOKEN this browser earned once — by the
// account password or a pairing PIN shown on the host — and keeps in
// localStorage (see backend/device_auth.py). The page itself carries no
// key: it used to inline the shared secret, which handed it to every
// device that could load the page and to any rebinding origin.
//
// localStorage is bound to an origin, which is the point: a rebinding
// attacker on evil.com opens their own empty storage.
//
// This module:
//   • monkey-patches window.fetch so all in-app calls auto-sign,
//   • exports sseStream(path, onMessage, onError) — the EventSource
//     replacement that uses fetch+ReadableStream so we can attach
//     auth headers (EventSource API can't),
//   • exports Sautium.auth for the login screen (login / pair / forget).
//
// Whitelisted backend paths (see backend/auth_hmac.py) accept
// unsigned requests, so we sign blindly — the backend ignores
// signatures on those paths.

(function () {
  const TOKEN_KEY = "sautium.device_token";
  const enc = new TextEncoder();
  let _key = null;

  function storedToken() {
    try {
      return localStorage.getItem(TOKEN_KEY) || "";
    } catch {
      return "";               // private mode / storage disabled
    }
  }

  function setToken(tok) {
    try {
      if (tok) localStorage.setItem(TOKEN_KEY, tok);
      else localStorage.removeItem(TOKEN_KEY);
    } catch { /* nothing to do — auth degrades to "log in every load" */ }
    _key = null;               // force re-import on next signature
  }

  async function getKey() {
    if (_key) return _key;
    const tok = storedToken();
    if (!tok) return null;
    _key = await crypto.subtle.importKey(
      "raw",
      enc.encode(tok),
      { name: "HMAC", hash: "SHA-256" },
      false,
      ["sign"]
    );
    return _key;
  }

  function toHex(buf) {
    const arr = new Uint8Array(buf);
    let s = "";
    for (let i = 0; i < arr.length; i++) {
      s += arr[i].toString(16).padStart(2, "0");
    }
    return s;
  }

  async function sha256Hex(bytes) {
    const buf = await crypto.subtle.digest("SHA-256", bytes);
    return toHex(buf);
  }

  // Returns Uint8Array view of the body for hashing.
  async function bodyBytes(body) {
    if (body == null) return new Uint8Array(0);
    if (typeof body === "string") return enc.encode(body);
    if (body instanceof ArrayBuffer) return new Uint8Array(body);
    if (ArrayBuffer.isView(body)) {
      return new Uint8Array(body.buffer, body.byteOffset, body.byteLength);
    }
    if (body instanceof Blob) return new Uint8Array(await body.arrayBuffer());
    if (body instanceof URLSearchParams) return enc.encode(body.toString());
    // FormData and ReadableStream aren't used in our codebase. Fail loud
    // rather than silently send a broken signature.
    throw new Error("Sautium auth: unsupported body type " + typeof body);
  }

  async function signRequest(method, pathAndQuery, body) {
    const key = await getKey();
    if (!key) return null;     // not paired yet — send the request unsigned
    const ts = Math.floor(Date.now() / 1000).toString();
    const bodyHash = await sha256Hex(await bodyBytes(body));
    const canonical = `${method}\n${pathAndQuery}\n${ts}\n${bodyHash}`;
    const sigBuf = await crypto.subtle.sign("HMAC", key, enc.encode(canonical));
    return { ts, sig: toHex(sigBuf) };
  }

  // -- fetch override --------------------------------------------------------

  const _origFetch = window.fetch.bind(window);

  window.fetch = async function signedFetch(input, init) {
    init = init || {};
    const req = (typeof input === "string" || input instanceof URL)
      ? new Request(input, init)
      : input;

    // Only sign same-origin requests.
    let url;
    try {
      url = new URL(req.url);
    } catch {
      return _origFetch(input, init);
    }
    if (url.origin !== location.origin) {
      return _origFetch(input, init);
    }

    const pathAndQuery = url.pathname + (url.search || "");
    const method = req.method.toUpperCase();

    // For body: prefer init.body (Request object hides original body).
    // The pattern in our codebase is fetch(url, { method, body }) so
    // init.body is always present when body matters.
    const body = init.body != null ? init.body : "";
    const signed = await signRequest(method, pathAndQuery, body);

    const headers = new Headers(init.headers || {});
    if (signed) {
      headers.set("X-Sautium-Ts", signed.ts);
      headers.set("X-Sautium-Sig", signed.sig);
    }

    const resp = await _origFetch(input, { ...init, headers });
    // A 401 on a signed request means the token was revoked (epoch bumped
    // by a password change or "log out everywhere"). Drop it so the app
    // falls back to the login screen instead of looping on failures.
    if (resp.status === 401 && signed) {
      setToken("");
      window.dispatchEvent(new CustomEvent("sautium:auth-required"));
    }
    return resp;
  };

  // -- login surface ---------------------------------------------------------

  window.Sautium = window.Sautium || {};
  window.Sautium.auth = {
    hasToken: () => !!storedToken(),
    forget: () => setToken(""),
    status: async () => (await fetch("/api/auth/status")).json(),
    async login(username, password) {
      const r = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      });
      if (!r.ok) return false;
      setToken((await r.json()).token);
      return true;
    },
    async pair(code) {
      const r = await fetch("/api/auth/pair", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code }),
      });
      if (!r.ok) return false;
      setToken((await r.json()).token);
      return true;
    },
    async logoutEverywhere() {
      // The server hands back a fresh token so the browser that pressed the
      // button is not logged out by its own action.
      const r = await fetch("/api/auth/logout-all", { method: "POST" });
      if (!r.ok) return false;
      setToken((await r.json()).token);
      return true;
    },
  };

  // -- login gate ------------------------------------------------------------

  // Shown when this browser has no token: on first visit, after "log out
  // everywhere", or after a password change. Built here rather than in
  // app-shell because auth.js loads first — the app must not start issuing
  // 401s before the user has a way to sign in.
  function showLoginGate() {
    if (document.getElementById("auth-gate")) return;

    const overlay = document.createElement("div");
    overlay.id = "auth-gate";
    overlay.className = "confirm-overlay";
    overlay.innerHTML = `
      <div class="confirm-sheet">
        <h3 class="confirm-title">Sign in</h3>
        <p class="confirm-message" id="auth-gate-msg">Checking…</p>
        <div id="auth-gate-fields"></div>
        <div class="confirm-actions single">
          <button class="profile-btn primary" id="auth-gate-submit">Continue</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);

    const msg = overlay.querySelector("#auth-gate-msg");
    const fields = overlay.querySelector("#auth-gate-fields");
    const submit = overlay.querySelector("#auth-gate-submit");
    const input = (id, type, ph, value) =>
      `<input class="add-gear-input" id="${id}" type="${type}" placeholder="${ph}"
              value="${value || ""}" autocapitalize="off" autocorrect="off"
              spellcheck="false" style="width:100%;margin-bottom:calc(10*var(--px));">`;

    let mode = "pin";

    window.Sautium.auth.status().then((st) => {
      mode = st.password_login ? "password" : "pin";
      if (mode === "password") {
        msg.textContent = "Enter your Sautium account password.";
        fields.innerHTML = input("auth-user", "text", "username", st.username) +
                           input("auth-pass", "password", "password");
      } else {
        msg.textContent =
          "Open Sautium on the computer that runs it, press “Pair a device” " +
          "in Settings, and type the code shown there.";
        fields.innerHTML = input("auth-pin", "text", "XXXX-XXXX");
      }
      const first = fields.querySelector("input:not([value]), input[value='']")
                 || fields.querySelector("input");
      if (first) first.focus();
    }).catch(() => {
      msg.textContent = "Cannot reach the server.";
    });

    async function attempt() {
      submit.disabled = true;
      const prev = submit.textContent;
      submit.textContent = "Checking…";
      let ok = false;
      if (mode === "password") {
        ok = await window.Sautium.auth.login(
          overlay.querySelector("#auth-user").value.trim(),
          overlay.querySelector("#auth-pass").value);
      } else {
        ok = await window.Sautium.auth.pair(
          overlay.querySelector("#auth-pin").value.trim());
      }
      if (ok) { location.reload(); return; }
      submit.disabled = false;
      submit.textContent = prev;
      msg.textContent = mode === "password"
        ? "Wrong username or password."
        : "That code is wrong or has expired — get a new one on the host.";
    }

    submit.addEventListener("click", attempt);
    overlay.addEventListener("keydown", (e) => {
      if (e.key === "Enter") attempt();
    });
  }

  window.Sautium.auth.showLoginGate = showLoginGate;
  window.addEventListener("sautium:auth-required", showLoginGate);
  if (!storedToken()) {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", showLoginGate);
    } else {
      showLoginGate();
    }
  }

  // -- SSE replacement -------------------------------------------------------

  // Reads the SSE wire format from a ReadableStream reader and yields
  // raw `data:` payloads (joined with newlines if multi-line). Comment
  // lines (": ...") and `event:` / `id:` / `retry:` fields are
  // ignored — the backend never sets those for us right now.
  async function* parseSSE(reader) {
    const decoder = new TextDecoder();
    let buf = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) return;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const block = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        const dataLines = [];
        for (const line of block.split("\n")) {
          if (line.startsWith("data:")) {
            dataLines.push(line.slice(5).replace(/^ /, ""));
          }
        }
        if (dataLines.length) yield dataLines.join("\n");
      }
    }
  }

  // Drop-in replacement for `new EventSource(path)` when you need
  // request signing. Returns an AbortController — call .abort() to
  // close the stream. Callbacks mirror EventSource semantics so we
  // can swap call sites with minimal change.
  window.sseStream = function (path, onMessage, onError) {
    const ctrl = new AbortController();
    (async () => {
      let backoff = 1000;
      while (!ctrl.signal.aborted) {
        try {
          const resp = await fetch(path, {
            method: "GET",
            headers: { Accept: "text/event-stream" },
            signal: ctrl.signal,
            cache: "no-store",
          });
          if (!resp.ok) {
            if (onError) onError(new Error("SSE HTTP " + resp.status));
            await new Promise(r => setTimeout(r, backoff));
            backoff = Math.min(backoff * 2, 30000);
            continue;
          }
          backoff = 1000;
          const reader = resp.body.getReader();
          for await (const data of parseSSE(reader)) {
            try {
              onMessage({ data });
            } catch (e) {
              if (onError) onError(e);
            }
          }
          // Server closed the stream normally — reconnect after a beat.
          if (!ctrl.signal.aborted) await new Promise(r => setTimeout(r, 1000));
        } catch (e) {
          if (ctrl.signal.aborted) return;
          if (onError) onError(e);
          await new Promise(r => setTimeout(r, backoff));
          backoff = Math.min(backoff * 2, 30000);
        }
      }
    })();
    return ctrl;
  };
})();
