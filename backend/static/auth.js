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
  let _keyToken = "";          // the token _key was imported from

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
    _keyToken = "";
  }

  // Storage is read on every signature, and the imported key is a cache OF
  // that read rather than a copy that outlives it. localStorage belongs to
  // the origin, not to this tab: a second tab redeeming a pairing link
  // replaces the token underneath us, and a key kept from before that point
  // signs requests the server correctly rejects — which was then read as
  // "the token is dead" and logged every tab out, the freshly paired one
  // included.
  //
  // Returns {key, token} — the pair, never the key alone, so a signature
  // stays attributable to the token that made it even when a concurrent
  // request re-imports the cache in between.
  async function getKey() {
    const tok = storedToken();
    if (!tok) {
      _key = null;
      _keyToken = "";
      return null;
    }
    if (_key && _keyToken === tok) return { key: _key, token: tok };
    const key = await crypto.subtle.importKey(
      "raw",
      enc.encode(tok),
      { name: "HMAC", hash: "SHA-256" },
      false,
      ["sign"]
    );
    _key = key;
    _keyToken = tok;
    return { key, token: tok };
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
    const signer = await getKey();
    if (!signer) return null;  // not paired yet — send the request unsigned
    const ts = Math.floor(Date.now() / 1000).toString();
    const bodyHash = await sha256Hex(await bodyBytes(body));
    const canonical = `${method}\n${pathAndQuery}\n${ts}\n${bodyHash}`;
    const sigBuf = await crypto.subtle.sign("HMAC", signer.key,
                                            enc.encode(canonical));
    // The token rides back with the signature: a 401 has to be attributable
    // to the key that actually produced it, not to whatever is in storage by
    // the time the answer lands.
    return { ts, sig: toHex(sigBuf), token: signer.token };
  }

  // -- fetch override --------------------------------------------------------

  const _origFetch = window.fetch.bind(window);

  // App traffic waits until the boot below has settled what this browser can
  // sign with. The app starts fetching the instant it loads, so without this
  // the first screens raced the pairing round-trip: they went out unsigned,
  // or signed with a token that had already died, and the repair was a full
  // page reload once the real token landed. One awaited promise is what that
  // reload was standing in for.
  let _authSettled;
  const _authReady = new Promise((resolve) => { _authSettled = resolve; });

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

    await _authReady;

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

    let resp = await _origFetch(input, { ...init, headers });

    // Two 401s say nothing about the token itself, and both are repaired by
    // signing again:
    //   stale-ts — the request sat in the network queue past the replay
    //     window; a frozen phone tab flushes minutes-old signatures on wake.
    //   bad-sig over a token that is no longer the stored one — the browser
    //     signed in (a pairing link, another tab) while this request was in
    //     flight, so what the server rejected is a key that has already been
    //     superseded here.
    const authError = resp.status === 401 && signed
      ? resp.headers.get("X-Sautium-Auth-Error") : null;
    const superseded = authError === "bad-sig" &&
      storedToken() && storedToken() !== signed.token;

    if (authError === "stale-ts" || superseded) {
      const fresh = await signRequest(method, pathAndQuery, body);
      if (fresh) {
        const retryHeaders = new Headers(init.headers || {});
        retryHeaders.set("X-Sautium-Ts", fresh.ts);
        retryHeaders.set("X-Sautium-Sig", fresh.sig);
        resp = await _origFetch(input, { ...init, headers: retryHeaders });
      }
    } else if (authError === "bad-sig") {
      // The token the browser still holds is the one the server refused, so
      // it is genuinely dead (epoch bumped by a password change or "log out
      // everywhere", or a different node answering on this address now).
      // Any other 401 is the route's own verdict (e.g. an expired media URL)
      // and says nothing about the token.
      setToken("");
      window.dispatchEvent(new CustomEvent("sautium:auth-required"));
    }
    return resp;
  };

  // -- login surface ---------------------------------------------------------

  // status / login / pair / create-account go out through the ORIGINAL fetch.
  // They are whitelisted server-side (a client with no token cannot sign), and
  // the wrapper would make them wait on a boot that is waiting on them.
  window.Sautium = window.Sautium || {};
  window.Sautium.auth = {
    hasToken: () => !!storedToken(),
    forget: () => setToken(""),
    status: async () => (await _origFetch("/api/auth/status")).json(),
    async login(password) {
      const r = await _origFetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password }),
      });
      if (!r.ok) return false;
      setToken((await r.json()).token);
      return true;
    },
    async createAccount(username, password) {
      const r = await _origFetch("/api/auth/create-account", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      });
      if (r.ok) { setToken((await r.json()).token); return true; }
      if (r.status === 409) return "This node already has an account — reload.";
      if (r.status === 422) return "Name: 3-32 letters, digits, - or _. Password: 8+ characters.";
      try {
        return (await r.json()).detail || "Could not create the account.";
      } catch { return "Could not create the account."; }
    },
    async pair(code) {
      const r = await _origFetch("/api/auth/pair", {
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
      mode = st.onboarding ? "create" : (st.password_login ? "password" : "pin");
      if (mode === "create") {
        // No identity at all — a fresh node. What is created here is the P2P
        // account (username+password -> Argon2id -> Ed25519), not a local
        // login, so it also switches on sync, chat and analysis signing.
        overlay.querySelector(".confirm-title").textContent = "Set up Sautium";
        msg.innerHTML =
          "Choose an account name and password. This is your identity on the " +
          "Sautium network, not just a local login.<br><br>" +
          "<b>The password is never stored</b> — it derives your keys. If you " +
          "lose it you get a new identity, and your invite code and friends " +
          "with it.";
        fields.innerHTML = input("auth-user", "text", "account name") +
                           input("auth-pass", "password", "password (8+ characters)");
        submit.textContent = "Create account";
      } else if (mode === "password") {
        // The username is shown, not asked for — a node has one account.
        msg.textContent = st.username
          ? `Signing in as ${st.username}. Enter the account password.`
          : "Enter the account password.";
        fields.innerHTML = input("auth-pass", "password", "password");
      } else {
        // Nothing to ask for but the PIN: this node's account was created
        // without a password anyone has seen. Both host affordances end
        // here — "Open Web UI" signs a browser on that machine in outright,
        // and the code beside the QR is what a device elsewhere types.
        msg.textContent =
          "This node has no account password. On the computer that runs " +
          "Sautium press “Open Web UI”, or type the pairing code shown " +
          "under the QR there.";
        fields.innerHTML = input("auth-pin", "text", "XXXX-XXXX");
      }
      const first = fields.querySelector("input");
      if (first) first.focus();
    }).catch(() => {
      msg.textContent = "Cannot reach the server.";
    });

    async function attempt() {
      submit.disabled = true;
      const prev = submit.textContent;
      submit.textContent = "Checking…";
      let ok = false, why = "";
      if (mode === "create") {
        const res = await window.Sautium.auth.createAccount(
          overlay.querySelector("#auth-user").value.trim(),
          overlay.querySelector("#auth-pass").value);
        ok = res === true;
        why = typeof res === "string" ? res : "Could not create the account.";
      } else if (mode === "password") {
        ok = await window.Sautium.auth.login(
          overlay.querySelector("#auth-pass").value);
        why = "Wrong password.";
      } else {
        ok = await window.Sautium.auth.pair(
          overlay.querySelector("#auth-pin").value.trim());
        why = "That code is wrong or has expired — get a new one on the host.";
      }
      if (ok) { location.reload(); return; }
      submit.disabled = false;
      submit.textContent = prev;
      msg.textContent = why;
    }

    submit.addEventListener("click", attempt);
    overlay.addEventListener("keydown", (e) => {
      if (e.key === "Enter") attempt();
    });
  }

  window.Sautium.auth.showLoginGate = showLoginGate;
  window.addEventListener("sautium:auth-required", showLoginGate);

  // The token belongs to the origin, so signing in or out is news for every
  // other tab of it — and the tab that learns it by failing a request has
  // already shown the user a broken screen. `storage` fires in the tabs that
  // did NOT make the change, which is exactly the audience.
  window.addEventListener("storage", (e) => {
    if (e.key !== null && e.key !== TOKEN_KEY) return;   // null = clear()
    if (!storedToken()) {
      showLoginGate();
    } else if (document.getElementById("auth-gate")) {
      location.reload();       // signed in elsewhere — this tab can go on
    }
  });

  // A pairing code can arrive in the URL fragment — that is how the launcher's
  // "Open Web UI" button and its QR sign a device in without anyone reading a
  // code aloud. The fragment never reaches the server (so the one-time code
  // stays out of access logs and Referer), and it is stripped from the address
  // bar the moment it is redeemed.
  async function redeemFragmentCode() {
    const m = /(?:^|[#&])pair=([A-Za-z0-9-]+)/.exec(location.hash || "");
    if (!m) return false;
    const ok = await window.Sautium.auth.pair(m[1]);
    const clean = (location.hash || "").replace(/(?:^|[#&])pair=[A-Za-z0-9-]+/, "");
    history.replaceState(null, "", location.pathname + location.search +
                         (clean && clean !== "#" ? clean : ""));
    return ok;
  }

  async function bootAuth() {
    try {
      // The fragment outranks localStorage. It is a credential the host minted
      // seconds ago for this exact click, while the stored token is a cache
      // that can be dead — a bumped epoch, a recreated identity, another node
      // that once answered on this port. Reading storage first turned the
      // deliberate act ("sign this browser in") into a no-op in precisely the
      // case it exists for: the page went on signing with the dead token, ate
      // a 401, and raised the password dialog the button is there to avoid.
      if (await redeemFragmentCode()) return;
      if (storedToken()) return;
      showLoginGate();
    } finally {
      // Every exit, gate included — a latch nobody releases hangs the app.
      _authSettled();
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bootAuth);
  } else {
    bootAuth();
  }

  // -- SSE replacement -------------------------------------------------------

  // Every backend SSE generator emits at least a keepalive comment every
  // 15-20s (asyncio.wait_for timeouts in the routers). Silence past two
  // full cadences therefore means the socket died without a FIN — the
  // phone slept, the AP roamed, NAT rebound — and the pending read()
  // would otherwise hang for however long the OS takes to notice, with
  // the UI frozen on stale data the whole time. A frozen tab freezes
  // this timer too; on resume it fires immediately, which is exactly
  // the moment to declare the pre-sleep socket dead.
  const SSE_IDLE_MS = 45000;

  // Reads the SSE wire format from a ReadableStream reader and yields
  // raw `data:` payloads (joined with newlines if multi-line). Comment
  // lines (": ...") and `event:` / `id:` / `retry:` fields are
  // ignored — the backend never sets those for us right now.
  async function* parseSSE(reader, path) {
    const decoder = new TextDecoder();
    let buf = "";
    while (true) {
      let idleTimer;
      const idle = new Promise(r => { idleTimer = setTimeout(() => r("idle"), SSE_IDLE_MS); });
      const read = await Promise.race([reader.read(), idle]);
      clearTimeout(idleTimer);
      if (read === "idle") {
        console.warn("SSE idle >" + SSE_IDLE_MS / 1000 + "s, reconnecting:", path);
        // Resolves the pending read() with done:true — the stream ends
        // cleanly and sseStream's loop reconnects after its usual beat.
        // A rejection here is the already-dead body objecting; nothing
        // to act on.
        reader.cancel().catch(() => {});
        return;
      }
      const { value, done } = read;
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
          for await (const data of parseSSE(reader, path)) {
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
