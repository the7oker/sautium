// Sautium request signing and the credential channel.
//
// Every privileged request to the backend carries:
//   X-Sautium-Ts   unix seconds
//   X-Sautium-Sig  hex(HMAC-SHA256(token, METHOD\nPATH_AND_QUERY\nTS\nsha256_hex(body)))
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
// The page rides plain HTTP on the LAN (PROGRESS.md "HTTP on the LAN"), so
// the transport hides nothing, and two things follow. HMAC comes from
// sha256.js rather than crypto.subtle, which browsers withhold from an http
// origin. And every exchange that carries a credential — password or PIN
// in, token out — is boxed end to end (tweetnacl, vendor/) to a
// per-exchange server key that the node's identity key signs; the browser
// pins that identity on its first sign-in and, when a different one answers
// later, asks before going on (SSH's known_hosts, as a dialog).
//
// This module:
//   • monkey-patches window.fetch so all in-app calls auto-sign,
//   • exports sseStream(path, onMessage, onError) — the EventSource
//     replacement that uses fetch+ReadableStream so we can attach
//     auth headers (EventSource API can't), and awaitReconnectWindow(ms),
//     the backoff wait every stream reconnect sits out,
//   • exports Sautium.auth for the login screen (login / pair / forget).
//
// Whitelisted backend paths (see backend/auth_hmac.py) accept
// unsigned requests, so we sign blindly — the backend ignores
// signatures on those paths.

(function () {
  const TOKEN_KEY = "sautium.device_token";
  // The identity key of the node this browser signed in to (hex).
  const NODE_KEY = "sautium.node_pubkey";
  const enc = new TextEncoder();
  const dec = new TextDecoder();
  const { sha256, hmacSha256, toHex } = window.Sautium.hash;

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
  }

  function pinnedNode() {
    try {
      return localStorage.getItem(NODE_KEY) || "";
    } catch {
      return "";
    }
  }

  function pinNode(pubkey) {
    try {
      localStorage.setItem(NODE_KEY, pubkey);
    } catch { /* no storage — the next sign-in is a first sign-in again */ }
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

  // Storage is read on every signature. localStorage belongs to the origin,
  // not to this tab: a second tab redeeming a pairing link replaces the
  // token underneath us, and a signature made with the old one is one the
  // server correctly rejects — which was then read as "the token is dead"
  // and logged every tab out, the freshly paired one included.
  //
  // Returns {ts, sig, token} — the token rides with the signature, so a
  // signature stays attributable to the token that made it even when
  // storage changes in between.
  async function signRequest(method, pathAndQuery, body) {
    const token = storedToken();
    if (!token) return null;   // not paired yet — send the request unsigned
    const ts = Math.floor(Date.now() / 1000).toString();
    const bodyHash = toHex(sha256(await bodyBytes(body)));
    const canonical = `${method}\n${pathAndQuery}\n${ts}\n${bodyHash}`;
    const sig = toHex(hmacSha256(enc.encode(token), enc.encode(canonical)));
    return { ts, sig, token };
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

  // -- credential channel ----------------------------------------------------

  // One exchange: GET /handshake hands out the server half of a box key,
  // signed by the node's identity; the request that answers it is sealed to
  // that key and consumes it, and the reply comes back sealed to ours.
  // Nothing about the transport is relied on. See device_auth.py.

  const b64 = (bytes) => btoa(String.fromCharCode.apply(null, bytes));
  const unb64 = (s) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0));
  const unhex = (h) => Uint8Array.from(h.match(/../g) || [], (b) => parseInt(b, 16));

  // The node answering on this address is not the one this browser signed
  // in to. Raised from the handshake; the gate turns it into a question.
  class NodeChanged extends Error {
    constructor(seen) {
      super("node identity changed");
      this.seen = seen;
    }
  }

  async function handshake() {
    const r = await _origFetch("/api/auth/handshake", { cache: "no-store" });
    if (!r.ok) throw new Error("handshake HTTP " + r.status);
    const hs = await r.json();
    const eph = unb64(hs.eph);
    if (hs.node_pubkey) {
      const expires = new Uint8Array(8);
      new DataView(expires.buffer).setBigUint64(0, BigInt(hs.expires));
      const signed = new Uint8Array([
        ...enc.encode("sautium-pair:v1"), ...eph, ...expires]);
      if (!nacl.sign.detached.verify(signed, unhex(hs.sig), unhex(hs.node_pubkey))) {
        throw new Error("handshake signature invalid");
      }
    }
    // A pinned identity the answer does not carry — a different key, or no
    // key where a signed one is expected — is the case the pin exists for.
    const pinned = pinnedNode();
    if (pinned && pinned !== hs.node_pubkey) throw new NodeChanged(hs.node_pubkey);
    return { eph, ephB64: hs.eph, nodePubkey: hs.node_pubkey };
  }

  // POST `payload` sealed to a fresh handshake and open the sealed reply.
  // `viaSigned` sends it through the signing wrapper, for the routes that
  // want a signature on top of the box (logout-all, change-identity).
  // Resolves {ok:true, data, nodePubkey} or {ok:false, status, resp}.
  async function boxedPost(path, payload, viaSigned) {
    const hs = await handshake();
    const kp = nacl.box.keyPair();
    const nonce = nacl.randomBytes(nacl.box.nonceLength);
    const sealed = nacl.box(enc.encode(JSON.stringify(payload)), nonce,
                            hs.eph, kp.secretKey);
    const r = await (viaSigned ? fetch : _origFetch)(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ eph: hs.ephB64, client: b64(kp.publicKey),
                             nonce: b64(nonce), box: b64(sealed) }),
    });
    if (!r.ok) return { ok: false, status: r.status, resp: r };
    const env = await r.json();
    const plain = nacl.box.open(unb64(env.box), unb64(env.nonce), hs.eph, kp.secretKey);
    if (!plain) throw new Error("reply box does not open");
    return { ok: true, data: JSON.parse(dec.decode(plain)), nodePubkey: hs.nodePubkey };
  }

  async function errorDetail(r, fallback) {
    try {
      return (await r.resp.json()).detail || fallback;
    } catch {
      return fallback;
    }
  }

  // -- login surface ---------------------------------------------------------

  // status / login / pair / create-account go out through the ORIGINAL fetch.
  // They are whitelisted server-side (a client with no token cannot sign), and
  // the wrapper would make them wait on a boot that is waiting on them.
  //
  // login / pair / createAccount resolve false (or a message) on a refusal
  // and throw NodeChanged when the node answering is not the one this
  // browser knows; the gate asks the user and retries.
  window.Sautium = window.Sautium || {};
  window.Sautium.auth = {
    hasToken: () => !!storedToken(),
    forget: () => setToken(""),
    status: async () => (await _origFetch("/api/auth/status")).json(),
    // The gate's answer to NodeChanged: this is the node from now on.
    acceptNode: (pubkey) => pinNode(pubkey),
    async login(password) {
      const r = await boxedPost("/api/auth/login", { password });
      if (!r.ok) return false;
      setToken(r.data.token);
      pinNode(r.nodePubkey);
      return true;
    },
    async createAccount(username, password) {
      const r = await boxedPost("/api/auth/create-account", { username, password });
      if (r.ok) {
        setToken(r.data.token);
        pinNode(r.data.public_key_hex);
        return true;
      }
      if (r.status === 409) return "This node already has an account — reload.";
      if (r.status === 422) return "Nickname: 3-32 Latin letters, digits, - or _. Password: 8+ characters.";
      return errorDetail(r, "Could not create the account.");
    },
    async pair(code) {
      const r = await boxedPost("/api/auth/pair", { code });
      if (!r.ok) return false;
      setToken(r.data.token);
      pinNode(r.nodePubkey);
      return true;
    },
    async logoutEverywhere() {
      // The server hands back a fresh token so the browser that pressed the
      // button is not logged out by its own action.
      const r = await boxedPost("/api/auth/logout-all", {}, true);
      if (!r.ok) return false;
      setToken(r.data.token);
      return true;
    },
    async changeIdentity(username, password) {
      // A new name or password is a new key, and the token is bound to the
      // key — every paired browser is out, this one takes the fresh token
      // from the same reply and pins the new key. Returns the reply, or
      // {ok:false, error}.
      const r = await boxedPost("/api/auth/change-identity", { username, password }, true);
      if (!r.ok) return { ok: false, error: await errorDetail(r, `HTTP ${r.status}`) };
      setToken(r.data.token);
      pinNode(r.data.public_key_hex);
      return { ok: true, ...r.data };
    },
  };

  // -- login gate ------------------------------------------------------------

  // Shown when this browser has no token: on first visit, after "log out
  // everywhere", or after a password change. Built here rather than in
  // app-shell because auth.js loads first — the app must not start issuing
  // 401s before the user has a way to sign in.
  //
  // `changed` + `retry`: the boot found a pairing code in the URL but the
  // node answering is not the one this browser knows. The gate opens on the
  // question, and "Continue" pins the new node and redeems the code.
  function showLoginGate({ changed, retry } = {}) {
    if (document.getElementById("auth-gate")) return;

    const overlay = document.createElement("dialog");
    overlay.id = "auth-gate";
    overlay.className = "confirm-overlay";
    overlay.innerHTML = `
      <div class="confirm-sheet">
        <h3 class="confirm-title">Sign in</h3>
        <p class="confirm-message" id="auth-gate-msg">Checking…</p>
        <div id="auth-gate-fields"></div>
        <div class="confirm-actions single" id="auth-gate-form-actions">
          <button class="profile-btn primary" type="button" id="auth-gate-submit">Continue</button>
        </div>
        <div class="confirm-actions" id="auth-gate-node-actions" hidden>
          <button class="profile-btn secondary" type="button" id="auth-gate-stop">Stop</button>
          <button class="profile-btn primary" type="button" id="auth-gate-accept">Continue</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);
    // showModal() raises the gate into the browser's top layer, above any
    // sheet that happened to be open when the token died — a z-index could
    // not promise that. The gate has no dismiss path (nothing behind it is
    // usable without a token), so Escape has to be refused twice over:
    // preventDefault covers the cancelable close request, and the re-open
    // covers the one Chrome grants outright because the dialog was raised
    // without user activation. The only way out is a successful sign-in,
    // which reloads the page.
    overlay.addEventListener("cancel", (e) => e.preventDefault());
    overlay.addEventListener("close", () => overlay.showModal());
    overlay.showModal();

    const title = overlay.querySelector(".confirm-title");
    const msg = overlay.querySelector("#auth-gate-msg");
    const fields = overlay.querySelector("#auth-gate-fields");
    const formActions = overlay.querySelector("#auth-gate-form-actions");
    const nodeActions = overlay.querySelector("#auth-gate-node-actions");
    const submit = overlay.querySelector("#auth-gate-submit");
    const input = (id, type, ph, value) =>
      `<input class="add-gear-input" id="${id}" type="${type}" placeholder="${ph}"
              value="${value || ""}" autocapitalize="off" autocorrect="off"
              spellcheck="false" style="width:100%;margin-bottom:calc(10*var(--px));">`;

    let mode = "pin";
    let username = "";
    const escapeText = (s) => s.replace(/[&<>"']/g,
      (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

    function showForm() {
      title.textContent = mode === "create" ? "Set up Sautium" : "Sign in";
      fields.hidden = false;
      formActions.hidden = false;
      nodeActions.hidden = true;
      const first = fields.querySelector("input");
      if (first) first.focus();
    }

    function buildForm() {
      if (mode === "create") {
        // No identity at all — a fresh node. What is created here is the P2P
        // account (username+password -> Argon2id -> Ed25519), not a local
        // login, so it also switches on sync, chat and analysis signing.
        msg.innerHTML =
          "Choose a nickname and password. This is your identity on the " +
          "Sautium network, not just a local login — the nickname sits inside " +
          "your invite code, so Latin letters, digits, - or _.<br><br>" +
          "<b>The password is never stored</b> — it derives your keys. If you " +
          "lose it you get a new identity, and your invite code and friends " +
          "with it.";
        fields.innerHTML = input("auth-user", "text", "nickname") +
                           input("auth-pass", "password", "password (8+ characters)");
        submit.textContent = "Create account";
      } else if (mode === "password") {
        // The username is shown, not asked for — a node has one account.
        msg.textContent = username
          ? `Signing in as ${username}. Enter the account password.`
          : "Enter the account password.";
        fields.innerHTML = input("auth-pass", "password", "password");
      } else {
        // Nothing to ask for but the PIN: this node's account was created
        // without a password anyone has seen. Both host affordances end
        // here — "Open Web UI" signs a browser on that machine in outright,
        // and the QR carries the same code to a device elsewhere.
        msg.textContent =
          "This node has no account password. On the computer that runs " +
          "Sautium press “Open Web UI”, or scan the QR code shown " +
          "there with this device.";
        fields.innerHTML = input("auth-pin", "text", "XXXX-XXXX");
      }
      showForm();
    }

    // The known_hosts moment. The typed values stay in the hidden fields, so
    // "Continue" needs nothing typed again; "Stop" is a refusal that leaves
    // the browser signed out, and a later submit asks again.
    function askNodeChanged(err, onAccept) {
      title.textContent = "A different node";
      msg.innerHTML =
        "The node answering at this address is not the one this browser " +
        "signed in to before" +
        (username ? ` — it calls itself <b>${escapeText(username)}</b>.` : ".") +
        " If you changed the account or set the node up again, continue. " +
        "If you did not, stop: something else is answering on this network.";
      fields.hidden = true;
      formActions.hidden = true;
      nodeActions.hidden = false;
      overlay.querySelector("#auth-gate-stop").onclick = () => showForm();
      overlay.querySelector("#auth-gate-accept").onclick = () => {
        window.Sautium.auth.acceptNode(err.seen);
        showForm();
        onAccept();
      };
    }

    window.Sautium.auth.status().then((st) => {
      mode = st.onboarding ? "create" : (st.password_login ? "password" : "pin");
      username = st.username || "";
      buildForm();
      if (changed) askNodeChanged(changed, redeem);
    }).catch(() => {
      msg.textContent = "Cannot reach the server.";
    });

    // A pairing code from the URL, once the user has accepted the new node.
    async function redeem() {
      submit.disabled = true;
      let ok = false;
      try {
        ok = await retry();
      } catch (e) {
        if (!(e instanceof NodeChanged)) throw e;
        askNodeChanged(e, redeem);
        return;
      } finally {
        submit.disabled = false;
      }
      if (ok) { location.reload(); return; }
      msg.textContent = "That code is wrong or has expired — get a new one on the host.";
    }

    async function attempt() {
      if (submit.disabled || !nodeActions.hidden) return;   // Enter arrives here too
      submit.disabled = true;
      const prev = submit.textContent;
      submit.textContent = "Checking…";
      let ok = false, why = "";
      try {
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
      } catch (e) {
        submit.disabled = false;
        submit.textContent = prev;
        if (e instanceof NodeChanged) { askNodeChanged(e, attempt); return; }
        msg.textContent = "Cannot reach the server.";
        throw e;
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
  window.addEventListener("sautium:auth-required", () => showLoginGate());

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
  function fragmentCode() {
    const m = /(?:^|[#&])pair=([A-Za-z0-9-]+)/.exec(location.hash || "");
    return m ? m[1] : "";
  }

  function stripFragmentCode() {
    const clean = (location.hash || "").replace(/(?:^|[#&])pair=[A-Za-z0-9-]+/, "");
    history.replaceState(null, "", location.pathname + location.search +
                         (clean && clean !== "#" ? clean : ""));
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
      const code = fragmentCode();
      if (code) {
        let ok = false;
        try {
          ok = await window.Sautium.auth.pair(code);
        } catch (e) {
          if (!(e instanceof NodeChanged)) throw e;
          // The code stays in the URL until the gate redeems it.
          showLoginGate({ changed: e, retry: async () => {
            const done = await window.Sautium.auth.pair(code);
            if (done) stripFragmentCode();
            return done;
          } });
          return;
        }
        stripFragmentCode();
        if (ok) return;
      }
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

  // One reconnect wait. The timer is the schedule; the browser's own
  // signals that the odds just changed end it early: `online`
  // (connectivity is back) and the tab becoming visible (a phone woke —
  // its retries ran on throttled timers while it slept, and the wait
  // armed last may have most of its 30 s left, strip up, music playing).
  // `navigator.onLine` stays true through most real drops (Wi-Fi/LTE
  // handoff, NAT timeout, a router blip), so neither event can replace
  // the schedule. Resolves with what ended the wait: "timer", "online"
  // or "visible" — a cut-short wait means the schedule should restart.
  window.awaitReconnectWindow = function (ms) {
    return new Promise(resolve => {
      const done = (why) => {
        clearTimeout(timer);
        window.removeEventListener("online", onOnline);
        document.removeEventListener("visibilitychange", onVisible);
        resolve(why);
      };
      const onOnline = () => done("online");
      const onVisible = () => { if (!document.hidden) done("visible"); };
      const timer = setTimeout(() => done("timer"), ms);
      window.addEventListener("online", onOnline);
      document.addEventListener("visibilitychange", onVisible);
    });
  };

  // Drop-in replacement for `new EventSource(path)` when you need
  // request signing. Returns an AbortController — call .abort() to
  // close the stream. Callbacks mirror EventSource semantics so we
  // can swap call sites with minimal change. Reconnects with a doubling
  // backoff (1 s … 30 s) that restarts from 1 s whenever a wake or
  // `online` cut a wait short: the network that just came back needs a
  // few quick tries, not one attempt and then the long tail.
  window.sseStream = function (path, onMessage, onError) {
    const ctrl = new AbortController();
    (async () => {
      let backoff = 1000;
      const waitOut = async () => {
        const why = await window.awaitReconnectWindow(backoff);
        backoff = why === "timer" ? Math.min(backoff * 2, 30000) : 1000;
      };
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
            await waitOut();
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
          await waitOut();
        }
      }
    })();
    return ctrl;
  };
})();
