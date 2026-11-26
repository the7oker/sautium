// Sautium request signing and the credential channel.
//
// Every privileged request to the backend carries:
//   X-Sautium-Ts           unix seconds
//   X-Sautium-Nonce        16 random bytes, hex, drawn for this request
//   X-Sautium-Body-Sha256  sha256_hex(body), the digest the signature covers
//   X-Sautium-Sig          hex(HMAC-SHA256(token, METHOD\nPATH_AND_QUERY\nTS\nNONCE\nsha256_hex(body)))
// The node accepts a signature once (auth_hmac.py): the nonce is what makes
// two identical presses in one second two requests. The declared digest lets
// it judge the signature before it reads a byte of the body.
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
//   • raises `sautium:wake` on window when the page comes back from the
//     background — the one signal everything that must re-read listens to,
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
  // The pairing code this page was opened with from the host (its button or
  // its QR), kept in memory for this load only: an account with no password
  // changes identity on its host's word, and this is that word.
  let hostCode = "";
  // The person said, at the gate, to set up the node that presents no
  // identity: the account being created may answer unsigned. The pin stays
  // as it is until the account made replaces it.
  let settingUp = false;
  // The node's account exists and its key is still being made (SEC-S5: the
  // status's `key`): its handshake is unsigned until then, and a browser signs
  // in all the same; the pin waits for the first signed answer.
  let unborn = false;
  const PIN_PENDING_KEY = "sautium.pin_pending";
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

  // A pin is a node's identity key, 32 bytes in hex, or nothing: an empty or
  // malformed value pins no node, and is never written.
  const KEY_FORM = /^[0-9a-f]{64}$/;

  function pinnedNode() {
    try {
      const pinned = localStorage.getItem(NODE_KEY) || "";
      return KEY_FORM.test(pinned) ? pinned : "";
    } catch {
      return "";
    }
  }

  function pinPending() {
    try { return localStorage.getItem(PIN_PENDING_KEY) === "1"; } catch { return false; }
  }

  function setPinPending(on) {
    try {
      if (on) localStorage.setItem(PIN_PENDING_KEY, "1");
      else localStorage.removeItem(PIN_PENDING_KEY);
    } catch { /* no storage — the next sign-in is a first sign-in again */ }
  }

  function pinNode(pubkey) {
    try {
      if (KEY_FORM.test(pubkey || "")) localStorage.setItem(NODE_KEY, pubkey);
      else localStorage.removeItem(NODE_KEY);
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
  // Returns {ts, nonce, bodyHash, sig, token} — the token rides with the
  // signature, so a signature stays attributable to the token that made it
  // even when storage changes in between.
  async function signRequest(method, pathAndQuery, body) {
    const token = storedToken();
    if (!token) return null;   // not paired yet — send the request unsigned
    const ts = Math.floor(Date.now() / 1000).toString();
    const nonce = toHex(crypto.getRandomValues(new Uint8Array(16)));
    const bodyHash = toHex(sha256(await bodyBytes(body)));
    const canonical = `${method}\n${pathAndQuery}\n${ts}\n${nonce}\n${bodyHash}`;
    const sig = toHex(hmacSha256(enc.encode(token), enc.encode(canonical)));
    return { ts, nonce, bodyHash, sig, token };
  }

  function signedHeaders(init, signed) {
    const headers = new Headers(init.headers || {});
    if (signed) {
      headers.set("X-Sautium-Ts", signed.ts);
      headers.set("X-Sautium-Nonce", signed.nonce);
      headers.set("X-Sautium-Body-Sha256", signed.bodyHash);
      headers.set("X-Sautium-Sig", signed.sig);
    }
    return headers;
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

    let resp = await _origFetch(input, { ...init, headers: signedHeaders(init, signed) });

    // Three 401s say nothing about the token itself, and each is repaired by
    // signing again, once (a second refusal is the caller's to read):
    //   stale-ts, for a read — the request sat in the browser past the replay
    //     window: a phone that leaves the browser holds what the page had just
    //     signed (a stream reconnecting as its socket died) and sends it when
    //     it comes back, minutes later. A change is not sent again: the tap
    //     was made then, and doing it now would surprise its maker — the
    //     refusal goes back to the caller (the transport's reporter says it;
    //     another caller shows its own refusal).
    //   bad-sig over a token that is no longer the stored one — the browser
    //     signed in (a pairing link, another tab) while this request was in
    //     flight, so what the server rejected is a key that has already been
    //     superseded here.
    //   replayed, for a read — the browser itself sent this very request
    //     again (a retry after a dropped connection); a change it made is not
    //     made twice.
    const authError = resp.status === 401 && signed
      ? resp.headers.get("X-Sautium-Auth-Error") : null;
    const superseded = authError === "bad-sig" &&
      storedToken() && storedToken() !== signed.token;
    const read = method === "GET" || method === "HEAD";

    if (((authError === "stale-ts" || authError === "replayed") && read) || superseded) {
      const fresh = await signRequest(method, pathAndQuery, body);
      if (fresh) {
        resp = await _origFetch(input, { ...init, headers: signedHeaders(init, fresh) });
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
  // `seen` is the key it answered with — "" for a node not set up yet,
  // which only an account being created meets.
  class NodeChanged extends Error {
    constructor(seen) {
      super("node identity changed");
      this.seen = seen;
    }
  }

  // An answer without an identity where only a node that has one can sign
  // this browser in (a password, a pairing code): refused, never pinned.
  class NodeUnset extends Error {
    constructor() {
      super("This node presents no identity — it is not set up yet.");
    }
  }

  // `creating`: the exchange creates the node's account, the one moment a
  // node answers with no identity of its own.
  async function handshake(creating = false) {
    const r = await _origFetch("/api/auth/handshake", { cache: "no-store" });
    if (!r.ok) throw new Error("handshake HTTP " + r.status);
    const hs = await r.json();
    const eph = unb64(hs.eph);
    const pinned = pinnedNode();
    const expires = new Uint8Array(8);
    new DataView(expires.buffer).setBigUint64(0, BigInt(hs.expires));
    const signed = new Uint8Array([...enc.encode("sautium-pair:v1"), ...eph, ...expires]);
    // The device token's word on this answer (device_auth.open_channel's
    // tag): only the node that issued the token makes it
    const token = storedToken();
    const vouched = !!token && typeof hs.tag === "string" && hs.tag.length > 0 &&
      toHex(hmacSha256(enc.encode(token),
                       new Uint8Array([...signed, ...enc.encode(hs.node_pubkey || "")]))) === hs.tag;
    if (hs.node_pubkey) {
      if (!KEY_FORM.test(hs.node_pubkey)) throw new Error("handshake identity malformed");
      if (!nacl.sign.detached.verify(signed, unhex(hs.sig), unhex(hs.node_pubkey))) {
        throw new Error("handshake signature invalid");
      }
      // A pinned identity the answer does not carry is the case the pin
      // exists for
      if (pinned && pinned !== hs.node_pubkey) throw new NodeChanged(hs.node_pubkey);
      // The first signed answer of a node this browser signed in to before
      // its key: pinned on the token's word, never on being first
      if (!pinned && pinPending()) {
        if (token && !vouched) throw new NodeChanged(hs.node_pubkey);
        pinNode(hs.node_pubkey);
        setPinPending(false);
      }
    } else if (!pinned && (vouched || (unborn && !token))) {
      // An account whose key is being made (or whose making stopped)
      // answers unsigned, as at its setup: a browser that holds its token
      // takes the answer on the token's word alone, whatever the page last
      // heard of the key; one with no token, on the status's word
      setPinPending(true);
    } else if (!pinned && unborn && token) {
      throw new NodeChanged("");
    } else if (!creating) {
      // A node with an account always signs: an answer without is no node
      // this browser can sign in to
      throw new NodeUnset();
    } else if (pinned && !settingUp) {
      throw new NodeChanged("");
    }
    return { eph, ephB64: hs.eph, nodePubkey: hs.node_pubkey };
  }

  // The routes of the first-run setup: the one moment a node answers with no
  // identity of its own (handshake's `creating`)
  const SETUP_PATHS = new Set(["/api/auth/create-account", "/api/auth/setup-estimate",
                               "/api/auth/setup-phrase"]);

  // What is sealed, padded to a whole number of BOX_BUCKET bytes (the
  // `_pad` member every reader ignores): a box's length would otherwise be
  // its payload's — a password's length, in every login's box. (The meter's
  // requests still follow the keystrokes in number: what that says is
  // accepted, SEC-S2 §15.) The node pads its replies the same way
  // (device_auth.padded).
  const BOX_BUCKET = 1024;
  function padded(payload) {
    const bare = enc.encode(JSON.stringify({ ...payload, _pad: "" })).length;
    return enc.encode(JSON.stringify({ ...payload, _pad: " ".repeat((BOX_BUCKET - bare % BOX_BUCKET) % BOX_BUCKET) }));
  }

  // POST `payload` sealed to a fresh handshake and open the sealed reply.
  // `viaSigned` sends it through the signing wrapper, for the routes that
  // want a signature on top of the box (logout-all, change-identity).
  // Resolves {ok:true, data, nodePubkey} or {ok:false, status, resp}. A box
  // the node could not open (`channel`: its handshake let go meanwhile — the
  // node keeps a bounded number) is sealed again once, to a fresh handshake.
  async function boxedPost(path, payload, viaSigned, again = true, method = "POST") {
    const hs = await handshake(SETUP_PATHS.has(path));
    const kp = nacl.box.keyPair();
    const nonce = nacl.randomBytes(nacl.box.nonceLength);
    const sealed = nacl.box(padded(payload), nonce, hs.eph, kp.secretKey);
    const r = await (viaSigned ? fetch : _origFetch)(path, {
      method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ eph: hs.ephB64, client: b64(kp.publicKey),
                             nonce: b64(nonce), box: b64(sealed) }),
    });
    if (again && r.status === 400 && r.headers.get("X-Sautium-Auth-Error") === "channel") {
      return boxedPost(path, payload, viaSigned, false, method);
    }
    if (!r.ok) return { ok: false, status: r.status, resp: r };
    const env = await r.json();
    const plain = nacl.box.open(unb64(env.box), unb64(env.nonce), hs.eph, kp.secretKey);
    if (!plain) throw new Error("reply box does not open");
    return { ok: true, data: JSON.parse(dec.decode(plain)), nodePubkey: hs.nodePubkey };
  }

  const BUSY = "The node is busy — try again in a moment.";
  // Where the node wrote its setup code: a launcher's node has its identity
  // folder there, a Docker node its log. The status says which (`launcher`);
  // the gate reads it before any form.
  let launcherHost = false;
  const setupCodePlace = () => launcherHost
    ? "On the computer that runs Sautium it is in setup_code.txt, in the identity folder that " +
      "Sautium's window names (a new one at each start)."
    : "It is on the last screen of the node's setup, and in the node's log (docker logs " +
      "sautium-backend; the node writes a new one at each start) or in setup_code.txt in its " +
      "identity folder — data/node_identity, or backend/data/node_identity under the WSL and Mac " +
      "compose files.";
  // What a new identity does to the records the lost one signed, in both
  // states of a node: started on this version before its identity went
  // (the records' cutover ran with it), or not (the next start runs the
  // cutover with the new key, desktop/node_identity, backend/db_migrate).
  const NEW_IDENTITY_COSTS =
    "A new identity makes the records this node signed with the one that is gone another " +
    "author's. If the node started on this version of Sautium while it still had its identity, " +
    "they stay as they are, under that identity's signatures, and what is analysed or changed " +
    "from now on is signed with the new one. If the identity went before that, the next start " +
    "deletes the features and instruments it signed, and those of its embeddings mixed with " +
    "unsigned parts; the embeddings it signed whole stay, as another author's. What is deleted " +
    "is analysed again where the music file is here (instruments only where “Recognise " +
    "instruments” is on, which a lite machine's default is not); a streamed track's on its " +
    "next full stream where one is still to be had (a provider of your own, or its one demo " +
    "listen not yet spent), and otherwise only if a pull from the network brings them back.";
  // Only a 401 says the password or the code was wrong; a 429 is the door
  // shut by wrong passwords, its wait named; anything else is the exchange
  // that did not finish, said as such.
  const UNFINISHED = "The sign-in did not finish — try again.";
  function shut(r) {
    const seconds = Number(r.resp.headers.get("Retry-After")) || 60;
    const wait = seconds < 120 ? `${seconds} s` : `${Math.ceil(seconds / 60)} min`;
    return `Too many wrong passwords — the next try is taken in ${wait}.`;
  }
  const failure = (r) => r.status === 401 ? false : r.status === 503 ? BUSY
    : r.status === 429 ? shut(r) : UNFINISHED;

  async function errorDetail(r, fallback) {
    try {
      // A refused shape's detail is the list of its fields, not words
      const detail = (await r.resp.json()).detail;
      return typeof detail === "string" && detail ? detail : fallback;
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
  // browser knows; the gate asks the user and retries. login and pair throw
  // NodeUnset when it answers with no identity. A busy node (503) is said
  // as such, never as a wrong password.
  window.Sautium = window.Sautium || {};
  window.Sautium.auth = {
    hasToken: () => !!storedToken(),
    forget: () => setToken(""),
    status: async () => {
      const st = await (await _origFetch("/api/auth/status")).json();
      launcherHost = st.launcher === true;
      unborn = !!st.key && st.key !== "ready";
      return st;
    },
    // The gate's answer to NodeChanged: this is the node from now on — or,
    // for a node not set up yet, set it up: the pin stays until the account
    // made replaces it.
    acceptNode: (pubkey) => { if (pubkey) pinNode(pubkey); else settingUp = true; },
    // Whether this page was opened from the host, with its pairing code
    hasHostCode: () => !!hostCode,
    async login(password) {
      const r = await boxedPost("/api/auth/login", { password });
      if (!r.ok) return failure(r);
      setToken(r.data.token);
      pinNode(r.nodePubkey);
      // The first sign-in of this password since the estimate exists: its
      // time, for the page that loads next to say once (app-shell.js)
      if (r.data.password) {
        try { sessionStorage.setItem(PASSWORD_TOLD_KEY, JSON.stringify(r.data.password)); } catch (_) { /* no storage: not said */ }
      }
      return true;
    },
    // `keep`: the person's word for a password one GPU would find within a
    // year; `email`: the one typed beside it — a word the estimate tries
    // first, and the account's address once its key is made. A password
    // under the door's edge resolves {weak: verdict}. The account exists at
    // once and its key follows (SEC-S5): this browser pins the node at its
    // first signed answer; whether the pair is the identity the node's
    // records name is asked when the key is made (askAnotherIdentity).
    async createAccount(username, password, setupCode, keep = false, email = "") {
      const r = await boxedPost("/api/auth/create-account",
                                { username, password, setup_code: setupCode, keep, email });
      if (r.ok && r.data.password) return { weak: r.data.password };
      if (r.ok) {
        setToken(r.data.token);
        // A node the gate was told to set up replaces the one this browser
        // knew: pinned at its key's first signed answer
        pinNode("");
        setPinPending(true);
        return true;
      }
      if (r.status === 409) return "This node already has an account — reload.";
      if (r.status === 403) return `That is not this node's setup code. ${setupCodePlace()}`;
      if (r.status === 429 && r.resp.headers.get("X-Sautium-Auth-Error") === "shut") {
        return `Too many wrong setup codes — the next try is taken in ${r.resp.headers.get("Retry-After")} s.`;
      }
      if (r.status === 422) return "Nickname: 3-32 Latin letters, digits, - or _.";
      return errorDetail(r, "Could not create the account.");
    },
    async pair(code) {
      // Whether the node's key is still being made: its answer is unsigned then
      await window.Sautium.auth.status();
      const r = await boxedPost("/api/auth/pair", { code });
      if (!r.ok) return failure(r);
      setToken(r.data.token);
      pinNode(r.nodePubkey);
      return true;
    },
    async logoutEverywhere() {
      // The server hands back a fresh token so the browser that pressed the
      // button is not logged out by its own action.
      let r;
      try {
        r = await boxedPost("/api/auth/logout-all", {}, true);
      } catch (e) {
        if (e instanceof NodeChanged || e instanceof NodeUnset) return false;
        throw e;
      }
      if (!r.ok) return false;
      setToken(r.data.token);
      return true;
    },
    async changeIdentity(username, password, currentPassword, keep = false) {
      // A new name or password is a new key, and the token is bound to the
      // key — every paired browser is out, this one takes the fresh token
      // from the same reply and pins the new key. An account with a password
      // has it typed again (`currentPassword`); one without carries the code
      // the host opened this page with. Returns the reply, or {ok:false, error}.
      let r;
      try {
        r = await boxedPost("/api/auth/change-identity",
                            { username, password, current_password: currentPassword || "",
                              pin: hostCode, keep }, true);
      } catch (e) {
        if (e instanceof NodeChanged || e instanceof NodeUnset) {
          return { ok: false, error: "The node answering is not the one this browser signed in to — reload the page." };
        }
        throw e;
      }
      if (!r.ok) {
        return { ok: false, error: r.status === 503 ? BUSY : r.status === 429 ? shut(r)
          : await errorDetail(r, `HTTP ${r.status}`) };
      }
      if (r.data.password) return { ok: false, weak: r.data.password };
      // A new password's key is made in the background: the old identity
      // stands, and this browser stays signed in to it, until it lands —
      // then every browser signs in again with the new password
      if (r.data.key === "deriving") return { ok: true, deriving: true, ...r.data };
      setToken(r.data.token);
      pinNode(r.data.public_key_hex);
      return { ok: true, ...r.data };
    },
    // A request of the app's own whose body rides the box, signed on top:
    // every route that takes a secret typed here (an AI key, a sign-in's
    // code, the email's code) or a change a device token alone does not make
    // (routers/auth.boxed). `password`, when given (an empty one too), is
    // the person's word for that change: the account's password, with the
    // code the host opened this page with beside it for an account that has
    // none (routers/auth.step_up). Resolves {ok:true, data} or
    // {ok:false, status, error, stepUp} — `stepUp`: the password or the code
    // was refused, and the sheet asks again.
    async boxed(path, payload, { method = "POST", password } = {}) {
      const body = password === undefined ? payload : { ...payload, password, pin: hostCode };
      let r;
      try {
        r = await boxedPost(path, body, true, true, method);
      } catch (e) {
        if (e instanceof NodeUnset) return { ok: false, status: 0, stepUp: false, error: e.message };
        if (e instanceof NodeChanged) {
          return { ok: false, status: 0, stepUp: false,
                   error: "The node answering is not the one this browser signed in to — reload the page." };
        }
        throw e;
      }
      if (r.ok) return { ok: true, data: r.data };
      return { ok: false, status: r.status,
               stepUp: r.status === 403 && r.resp.headers.get("X-Sautium-Auth-Error") === "step-up",
               // A 429 of the password door says so; another (the Worker's
               // brake on codes) is said in its own words
               error: r.status === 503 ? BUSY
                 : r.status === 429 && r.resp.headers.get("X-Sautium-Auth-Error") === "shut" ? shut(r)
                 : await errorDetail(r, `HTTP ${r.status}`) };
    },
  };

  // -- a new password ----------------------------------------------------------
  //
  // Every form that makes or changes the account's password (the first-run
  // setup here, Profile's change in app-shell.js) builds it alike: a phrase
  // the node draws offered first, the field with its show control, and under
  // it the time one GPU would take to find what is typed — the node's own
  // estimate, the one its door holds (desktop/password_strength), asked
  // through the box. One estimate is in flight at a time: a keystroke while
  // one is out marks the field changed, and the answer's arrival sends the
  // text as it stands then; no timer guesses when typing has stopped.

  // Every sentence here is the node's (desktop/password_strength: the
  // meter's line, a door's, the phrase's count, the note), so the launcher's
  // wizard and this page say the same words.
  const htmlText = (s) => String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const EYE = '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" ' +
    'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg>';

  // A password field with its show control. Paste is never blocked.
  // `attrs`: more attributes of the input, as markup.
  function passwordField(id, placeholder, autocomplete, attrs = "") {
    return `<div class="pw-field">
      <input class="add-gear-input" ${id ? `id="${id}"` : ""} ${attrs} type="password" placeholder="${htmlText(placeholder)}"
             autocomplete="${autocomplete}" autocapitalize="off" autocorrect="off" spellcheck="false">
      <button type="button" class="pw-show" aria-label="Show password" aria-pressed="false">${EYE}</button>
    </div>`;
  }

  // Every show control on the page, one listener: a tap shows its field's
  // text, the next hides it again.
  document.addEventListener("click", (e) => {
    const btn = e.target.closest && e.target.closest(".pw-show");
    if (!btn) return;
    const field = btn.parentElement.querySelector("input");
    const shown = field.type === "password";
    field.type = shown ? "text" : "password";
    btn.setAttribute("aria-pressed", String(shown));
    btn.setAttribute("aria-label", shown ? "Hide password" : "Show password");
  });


  // Build the new-password block into `root`. `signed`: a form of a
  // signed-in browser (the signed routes); otherwise the first-run setup's.
  // `inputs()`: the nickname and email typed beside it, which the estimate
  // tries first. `onUse(phrase)`: a confirmation field to fill as well.
  function newPassword(root, { signed, id, placeholder, inputs, onUse }) {
    root.innerHTML = `
      <p class="pw-offer" hidden>A phrase is drawn for you:
        <span class="pw-phrase"></span> — <span class="pw-phrase-time"></span>
        Keep it, write it down, or type your own.
        <button type="button" class="pw-link" data-pw-use>Use it</button>
        <button type="button" class="pw-link" data-pw-another>Another</button></p>
      ${passwordField(id, placeholder, "new-password")}
      <p class="pw-meter" aria-live="polite"></p>
      <label class="pw-keep" hidden><input type="checkbox" data-pw-keep> I understand, keep it</label>
      <p class="pw-what"></p>
      <p class="pw-note"></p>`;
    const field = root.querySelector("input.add-gear-input");
    const offer = root.querySelector(".pw-offer");
    const meter = root.querySelector(".pw-meter");
    const what = root.querySelector(".pw-what");
    const keepRow = root.querySelector(".pw-keep");
    const keepBox = root.querySelector("[data-pw-keep]");
    let drawn = null;               // {phrase, words}
    let inFlight = false;
    let changed = false;

    const say = (zone, html) => {
      meter.dataset.zone = zone;
      meter.innerHTML = html;
      keepRow.hidden = zone !== "word";
      if (zone !== "word") keepBox.checked = false;
      // Which identity the field makes (SEC-S5 §20): empty, a random key;
      // typed, one its nickname and password make again anywhere
      what.textContent = field.value ? PASSWORD_MAKES : randomIdentityWords();
    };
    const showOffer = (o) => {
      drawn = o;
      offer.querySelector(".pw-phrase").textContent = o.phrase;
      offer.querySelector(".pw-phrase-time").textContent = o.count;
      root.querySelector(".pw-note").textContent = o.note;
      offer.hidden = false;
    };
    let drawing = false;
    async function draw() {
      if (drawing) return;
      drawing = true;
      try {
        const r = await boxedPost(signed ? "/api/auth/phrase" : "/api/auth/setup-phrase", {}, signed);
        if (r.ok) showOffer(r.data);
      } catch (e) {
        // A node this browser has not accepted yet (NodeChanged) or one with
        // no identity at a signed form (NodeUnset): the offer stays hidden,
        // and the form's own submit asks the question
        if (!(e instanceof NodeChanged || e instanceof NodeUnset)) throw e;
      } finally {
        drawing = false;
      }
    }
    async function measure() {
      const text = field.value;
      // A node this page could not ask before (not accepted yet): its phrase
      // is drawn now that the person types
      if (!drawn) draw();
      if (!text) { say("", ""); return; }
      if (drawn && text === drawn.phrase) {
        say("clear", htmlText(drawn.meter));
        return;
      }
      inFlight = true;
      changed = false;
      let r = null;
      try {
        r = await boxedPost(signed ? "/api/auth/estimate" : "/api/auth/setup-estimate",
                            { password: text, ...inputs() }, signed);
      } catch (e) {
        if (!(e instanceof NodeChanged || e instanceof NodeUnset)) { inFlight = false; throw e; }
      }
      inFlight = false;
      if (changed) { measure(); return; }
      // No answer (busy, a node not accepted): no time rather than an old one
      if (!r || !r.ok) { say("", ""); return; }
      say(r.data.zone, htmlText(r.data.meter));
    }
    const ask = () => { if (inFlight) changed = true; else measure(); };
    field.addEventListener("input", ask);
    root.querySelector("[data-pw-another]").addEventListener("click", draw);
    root.querySelector("[data-pw-use]").addEventListener("click", () => {
      if (!drawn) return;
      field.value = drawn.phrase;
      field.type = "text";           // to be read and written down
      if (onUse) onUse(drawn.phrase);
      // As if typed: the meter and the form's own listeners follow
      field.dispatchEvent(new Event("input"));
    });
    draw();
    say("", "");
    return {
      field,
      keep: () => keepBox.checked,
      // The nickname or email beside it changed: the estimate tries them first
      refresh: ask,
      // A door's verdict: its words in the meter's place, its phrase offered
      said(v) {
        if (v.offer) showOffer(v.offer);
        say(v.zone, htmlText(v.door));
      },
    };
  }

  // What the new-password field makes, in its two states (SEC-S5 §20; draft
  // words for the owner's words review). Empty: the random identity, and
  // where its copy and its other doors are — the launcher's window, or a
  // Docker node's console. Typed: a mnemonic identity, and its price.
  const PASSWORD_MAKES =
    "Your nickname and this password make your identity again on any machine — and so does " +
    "anyone who guesses it. The time above is how long one GPU would need.";
  function randomIdentityWords() {
    return launcherHost
      ? "No password: your identity is a random key. Nobody can guess it or make it again — not " +
        "even you. It lives on this machine and in the identity file you export (Settings › Backup " +
        "& Restore); keep that file safe. Other browsers sign in with the pairing code this " +
        "computer shows."
      : "No password: your identity is a random key. Nobody can guess it or make it again — not " +
        "even you. It lives on this node and in the identity file its console exports " +
        "(./sautium identity export); keep that file safe. Other browsers sign in with the pairing " +
        "code its console shows (./sautium pair, or python -m pairing in its container).";
  }

  window.Sautium.auth.newPassword = newPassword;
  window.Sautium.auth.passwordField = passwordField;

  // -- login gate ------------------------------------------------------------

  // Shown when this browser has no token: on first visit, after "log out
  // everywhere", or after a password change. Built here rather than in
  // app-shell because auth.js loads first — the app must not start issuing
  // 401s before the user has a way to sign in.
  //
  // `changed` + `retry`: the boot found a pairing code in the URL but the
  // node answering is not the one this browser knows. The gate opens on the
  // question, and "Continue" pins the new node and redeems the code.
  // `notice`: why the code in the URL did not sign this browser in.
  function showLoginGate({ changed, retry, notice } = {}) {
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
    // The create form's new-password block (newPassword)
    let newPass = null;
    let unnamedKey = false;
    let older = false;
    let username = "";
    let emailRoad = false;
    // The setup code the address carried (#setup=, the QR on the setup's
    // last screen): the field is filled, nobody types it
    const setupFromUrl = takeFragmentSetup();

    function showForm() {
      title.textContent = mode === "create" ? "Set up Sautium"
        : mode === "restore" ? "Bring this node's identity back" : "Sign in";
      fields.hidden = false;
      formActions.hidden = mode === "none";
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
          "Choose a nickname, and a password if you want one. This is your identity on the " +
          "Sautium network, not just a local login — the nickname sits inside " +
          "your invite code, so Latin letters, digits, - or _.<br><br>" +
          "What the password field makes — with a password or without — is said under it.<br><br>" +
          "The setup code proves you run this node. " + setupCodePlace();
        // The email is optional everywhere. On a node that never earns its
        // identity by computing it is the way to one (draft words for the
        // owner's words review); on any other it skips the wait.
        fields.innerHTML = input("auth-user", "text", "nickname") +
                           '<div id="auth-newpass"></div>' +
                           input("auth-setup", "text", "setup code", setupFromUrl) +
                           input("auth-email", "email", "email (optional)") +
                           `<p class="confirm-message" style="margin-top:0;">${emailRoad
                             ? "This machine is too small to earn its network identity by computing. " +
                               "A verified email gives it one. Without it Sautium still plays and " +
                               "organises your music, but cannot reach Support or be reached from " +
                               "outside your network."
                             : "Optional. A verified email gives this node its network identity at " +
                               "once; without one it earns it by a calculation that takes a while."}</p>`;
        groupSetupCode(fields.querySelector("#auth-setup"));
        newPass = newPassword(fields.querySelector("#auth-newpass"), {
          signed: false, id: "auth-pass", placeholder: "password",
          inputs: () => ({ username: fields.querySelector("#auth-user").value.trim(),
                           email: (fields.querySelector("#auth-email") || { value: "" }).value.trim() }),
        });
        for (const beside of ["#auth-user", "#auth-email"]) {
          fields.querySelector(beside).addEventListener("input", newPass.refresh);
        }
        submit.textContent = "Create account";
      } else if (mode === "restore") {
        // No key, while the database says the node had one: the same
        // nickname and password derive the same key. The nickname the
        // identity folder still names, if it does, is filled in.
        msg.innerHTML =
          "This node has no identity, while its database says it had one: the key it signed " +
          "with is on record (or records it signed, or the key its network last ran on). " +
          "Type that identity's nickname and password: the same pair makes the same identity, " +
          "and the node signs as before. If you changed the nickname or password before, the " +
          "record of the identities it replaced comes back only with a copy of the node's " +
          "identity folder or a backup; without it, what they signed is another author's here. " +
          "If Sautium chose the password (an account made without one), or the identity had " +
          "no nickname and password at all (a new identity made in Sautium's window), only a " +
          "copy of the identity folder brings it back.<br><br>" +
          "The setup code proves you run this node. " + setupCodePlace();
        fields.innerHTML = input("auth-user", "text", "nickname",
                                 /^[A-Za-z0-9_-]{3,32}$/.test(username) ? username : "") +
                           passwordField("auth-pass", "password", "current-password") +
                           '<label class="pw-keep" hidden><input type="checkbox" data-pw-keep> I understand, keep it</label>' +
                           input("auth-setup", "text", "setup code", setupFromUrl);
        groupSetupCode(fields.querySelector("#auth-setup"));
        submit.textContent = "Bring it back";
      } else if (mode === "password") {
        // The username is shown, not asked for — a node has one account.
        msg.textContent = username
          ? `Signing in as ${username}. Enter the account password.`
          : "Enter the account password.";
        fields.innerHTML = passwordField("auth-pass", "password", "current-password");
      } else if (mode === "none") {
        // Nothing here can sign this browser in: a key with no account beside
        // it, or an identity made before SEC-S5, whose password nothing checks
        // (draft words for the owner's words review)
        msg.textContent = older
          ? "This node's identity was made by an older Sautium, and its password can no longer be " +
            "checked. Make this node's account anew: stop the node, move its identity folder aside, " +
            "start it again; the first visit sets it up, and the same nickname and password make a " +
            "new key."
          : "This node's identity folder holds a key without its account. To start over, stop the " +
            "node, empty that folder and start it again; the first visit then sets it up.";
        fields.innerHTML = "";
      } else {
        // Nothing to ask for but the PIN: this node's account was created
        // without a password anyone has seen. Both host affordances end
        // here — "Open Web UI" signs a browser on that machine in outright,
        // and the QR carries the same code to a device elsewhere.
        msg.textContent = launcherHost
          ? "This node has no account password. On the computer that runs " +
            "Sautium press “Open Web UI”, or scan the QR code shown there with this device."
          : "This node has no account password. On the machine that runs it, ./sautium pair " +
            "(or docker compose exec backend python -m pairing) shows a QR code to scan and " +
            "the link to open.";
        fields.innerHTML = input("auth-pin", "text", "the pairing link or code");
      }
      showForm();
    }

    // The known_hosts moment. The typed values stay in the hidden fields, so
    // "Continue" needs nothing typed again; "Stop" is a refusal that leaves
    // the browser signed out, and a later submit asks again. Nothing the
    // answer says about itself is quoted: a name is no identity. A node with
    // no identity is met only when an account is being created ("Set it up").
    function askNodeChanged(err, onAccept) {
      const unset = !err.seen;
      // The node answering with no identity is the one whose identity the
      // form brings back, as far as it can tell
      const restoring = unset && mode === "restore";
      title.textContent = restoring ? "This node's identity is missing"
        : unset ? "A node that is not set up" : "A different node";
      msg.textContent = restoring
        ? "This node presents no identity: its identity is missing, as the form says. This " +
          "browser signed in to a node at this address before. If that was this node, go on " +
          "and bring its identity back. If it was not, stop: something else is answering on " +
          "this network."
        : unset
        ? "This node presents no identity — it is not set up yet. This browser " +
          "signed in to another node at this address before. If you are setting " +
          "this node up again, set it up. If you are not, stop: something else is " +
          "answering on this network."
        : "The node answering at this address is not the one this browser " +
          "signed in to before. If you changed the account or set the node up " +
          "again, continue. If you did not, stop: something else is answering on " +
          "this network.";
      fields.hidden = true;
      formActions.hidden = true;
      nodeActions.hidden = false;
      overlay.querySelector("#auth-gate-accept").textContent =
        restoring ? "Bring it back" : unset ? "Set it up" : "Continue";
      overlay.querySelector("#auth-gate-stop").onclick = () => showForm();
      overlay.querySelector("#auth-gate-accept").onclick = () => {
        window.Sautium.auth.acceptNode(err.seen);
        showForm();
        onAccept();
      };
    }

    window.Sautium.auth.status().then((st) => {
      unnamedKey = !!st.unnamed_key;
      older = !!st.older_identity;
      mode = st.onboarding ? (st.identity_missing ? "restore" : "create") : st.password_login ? "password"
        : (unnamedKey || older) ? "none" : "pin";
      username = st.username || "";
      emailRoad = !!st.email_road;
      buildForm();
      // The create form says itself what a node not set up yet needs, and
      // no notice stands over what a node no browser can sign in to says
      if (notice && mode !== "create" && mode !== "restore" && mode !== "none") msg.textContent = notice;
      if (changed) askNodeChanged(changed, redeem);
    }).catch(() => {
      msg.textContent = "Cannot reach the server.";
    });

    // A pairing code from the URL, once the user has accepted the new node.
    async function redeem() {
      submit.disabled = true;
      let res = false;
      try {
        res = await retry();
      } catch (e) {
        if (e instanceof NodeUnset) { msg.textContent = e.message; return; }
        if (!(e instanceof NodeChanged)) throw e;
        askNodeChanged(e, redeem);
        return;
      } finally {
        submit.disabled = false;
      }
      if (res === true) { location.reload(); return; }
      msg.textContent = typeof res === "string" ? res : "That code is wrong or has expired — get a new one on the host.";
    }

    async function attempt() {
      if (submit.disabled || !nodeActions.hidden || mode === "none") return;   // Enter arrives here too
      submit.disabled = true;
      const prev = submit.textContent;
      submit.textContent = "Checking…";
      let ok = false, why = "";
      try {
        if (mode === "create" || mode === "restore") {
          const given = [overlay.querySelector("#auth-user").value.trim(),
                         overlay.querySelector("#auth-pass").value,
                         overlay.querySelector("#auth-setup").value.trim()];
          const emailField = overlay.querySelector("#auth-email");
          const email = emailField ? emailField.value.trim() : "";
          if (email && !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) {
            // Said before anything is made: the account cannot be un-made
            submit.disabled = false;
            submit.textContent = prev;
            msg.textContent = "That email does not look right. Correct it, or leave the field empty.";
            return;
          }
          if (mode === "restore" && !given[1]) {
            // No password brings an identity back: an empty one is a new random key
            submit.disabled = false;
            submit.textContent = prev;
            msg.textContent = "Type that identity's password: no password brings an identity back.";
            return;
          }
          // The person's word for a password under a year: the block's box
          // on the create form, the restore form's own once a door asked
          const keepBox = fields.querySelector("[data-pw-keep]");
          const keep = newPass ? newPass.keep() : !!(keepBox && keepBox.checked);
          let res = await window.Sautium.auth.createAccount(...given, keep, email);
          if (res && res.weak) {
            // Nothing was made: the door's words where the time is shown
            if (newPass) newPass.said(res.weak);
            else if (keepBox) keepBox.parentElement.hidden = res.weak.zone !== "word";
            res = res.weak.door + (res.weak.zone === "word" ? " Tick the box to keep it." : "");
          }
          ok = res === true;
          why = typeof res === "string" ? res : "Could not create the account.";
        } else if (mode === "password") {
          const res = await window.Sautium.auth.login(
            overlay.querySelector("#auth-pass").value);
          ok = res === true;
          why = typeof res === "string" ? res : "Wrong password.";
        } else {
          // A pasted link carries the code in its fragment
          const typed = overlay.querySelector("#auth-pin").value.trim();
          const linked = /(?:^|[#&])pair=([A-Za-z0-9-]+)/.exec(typed);
          const res = await window.Sautium.auth.pair(linked ? linked[1] : typed);
          ok = res === true;
          why = typeof res === "string" ? res : "That code is wrong or has expired — get a new one on the host.";
        }
      } catch (e) {
        submit.disabled = false;
        submit.textContent = prev;
        if (e instanceof NodeChanged) { askNodeChanged(e, attempt); return; }
        if (e instanceof NodeUnset) { msg.textContent = e.message; return; }
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
      // Enter on a button is that button's (the phrase's Use it, Another, a
      // show control): the form is sent from a field
      if (e.key === "Enter" && !(e.target.closest && e.target.closest("button"))) attempt();
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
  // The setup code rides the fragment the same way (the QR on the console
  // setup's last screen): read once, and gone from the address bar at once.
  function takeFragmentSetup() {
    const m = /(?:^|[#&])setup=([A-Za-z0-9-]+)/.exec(location.hash || "");
    if (!m) return "";
    const clean = (location.hash || "").replace(/(?:^|[#&])setup=[A-Za-z0-9-]+/, "");
    history.replaceState(null, "", location.pathname + location.search +
                         (clean && clean !== "#" ? clean : ""));
    return m[1];
  }

  // The setup code is typed by hand: its characters are grouped as they are
  // typed (XXXX-XXXX). Anything longer than a code is a pasted line, left as
  // it is — the node reads the code out of it.
  function groupSetupCode(field) {
    if (!field) return;
    const group = () => {
      const plain = field.value.toUpperCase().replace(/[^A-Z0-9]/g, "");
      if (plain.length <= 8 && /^[A-Z0-9\s-]*$/i.test(field.value)) {
        field.value = plain.length > 4 ? `${plain.slice(0, 4)}-${plain.slice(4)}` : plain;
      }
    };
    field.addEventListener("input", group);
    group();
  }

  // The key the setup made is not the identity this node's records name
  // (the status's and the identity event's "another_identity"), or the
  // key's making was stopped ("interrupted"): asked by the page then open
  // (app-shell.js), with the account's password — who the node is changes
  // only on the person's own word. `make`: the owner's answer to the first.
  window.Sautium.auth.NEW_IDENTITY_COSTS = NEW_IDENTITY_COSTS;
  window.Sautium.auth.decideNewIdentity = (make, password) =>
    window.Sautium.auth.boxed("/api/auth/new-identity", { make }, { password });
  window.Sautium.auth.resumeKey = (password) =>
    window.Sautium.auth.boxed("/api/auth/resume-key", {}, { password });
  const PASSWORD_TOLD_KEY = "sautium.password_told";
  window.Sautium.auth.takePasswordTold = () => {
    let told = null;
    try {
      told = JSON.parse(sessionStorage.getItem(PASSWORD_TOLD_KEY) || "null");
      sessionStorage.removeItem(PASSWORD_TOLD_KEY);
    } catch (_) { /* no storage: nothing to say */ }
    return told;
  };
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
      let notice = null;
      if (code) {
        let res = false;
        try {
          res = await window.Sautium.auth.pair(code);
        } catch (e) {
          if (e instanceof NodeUnset) {
            // Not a node this code can sign in to: spent, and said
            stripFragmentCode();
            showLoginGate({ notice: e.message });
            return;
          }
          if (!(e instanceof NodeChanged)) throw e;
          // The code stays in the URL until the gate redeems it.
          // Redeemed, the gate reloads with the code still in the URL: the
          // boot pairs with it again, keeps it as the host's and strips it
          showLoginGate({ changed: e, retry: () => window.Sautium.auth.pair(code) });
          return;
        }
        // A code the node took or refused is spent; one the node did not get
        // to (busy, an exchange that did not finish) stays in the URL for the
        // next load, and the gate says why
        if (typeof res !== "string") stripFragmentCode();
        if (res === true) { hostCode = code; return; }
        notice = typeof res === "string" ? res : null;
      }
      if (storedToken()) return;
      showLoginGate({ notice });
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

  // -- the page's own lifecycle ------------------------------------------------

  // A page the phone put away and brought back cannot trust anything it holds
  // from before: its streams' sockets may have died without a word (no FIN —
  // the phone suspended, the Wi-Fi slept, a NAT forgot), and what it shows is
  // what was true when it left. So its coming back is an event, raised once
  // per trip (`sautium:wake`, window): every stream re-opens on it with a
  // fresh signature (one that claims rather than reads opts out), and what
  // the page shows is read again from the node.
  // Away is any of the browser's words for it — hidden, frozen (Chrome),
  // hidden in the back/forward cache — and back is the page visible again
  // after one of them. A page frozen while still hidden comes back when it is
  // shown, not when it is thawed. No threshold on how long it was away: when
  // an OS kills a sleeping app's sockets is not the page's to guess, and a
  // re-open costs one request per stream.
  // A page loaded hidden (opened in a background tab) is away already
  let away = document.visibilityState !== "visible";
  const goneAway = () => { away = true; };
  const cameBack = (why) => {
    if (!away || document.hidden) return;
    away = false;
    window.dispatchEvent(new CustomEvent("sautium:wake", { detail: { why } }));
  };
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) goneAway();
    else cameBack("visible");
  });
  document.addEventListener("freeze", goneAway);
  document.addEventListener("resume", () => cameBack("resume"));
  window.addEventListener("pagehide", goneAway);
  window.addEventListener("pageshow", () => cameBack("pageshow"));

  // -- SSE replacement -------------------------------------------------------

  // Every backend SSE generator emits at least a keepalive comment every
  // 15-20s (asyncio.wait_for timeouts in the routers). Silence past two
  // full cadences therefore means the socket died without a FIN — the
  // phone slept, the AP roamed, NAT rebound — and the pending read()
  // would otherwise hang for however long the OS takes to notice, with
  // the UI frozen on stale data the whole time. It counts from the last
  // byte, so it is no answer for a page back from a short trip — the
  // re-open on `sautium:wake` is; this is for a socket that dies while the
  // page looks on.
  const SSE_IDLE_MS = 45000;

  // Reads the SSE wire format from a ReadableStream reader and yields
  // one { data, type } per event: the raw `data:` payload (joined with
  // newlines if multi-line) and the name its `event:` field gives it —
  // "message" without one, as EventSource has it. Comment lines (": ...")
  // and `id:` / `retry:` fields are ignored — the backend never sets
  // those for us right now.
  async function* parseSSE(reader, path) {
    const decoder = new TextDecoder();
    let buf = "";
    while (true) {
      let idleTimer;
      const idle = new Promise(r => { idleTimer = setTimeout(() => r("idle"), SSE_IDLE_MS); });
      let read;
      try {
        read = await Promise.race([reader.read(), idle]);
      } finally {
        clearTimeout(idleTimer);
      }
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
        let type = "message";
        for (const line of block.split("\n")) {
          if (line.startsWith("data:")) {
            dataLines.push(line.slice(5).replace(/^ /, ""));
          } else if (line.startsWith("event:")) {
            type = line.slice(6).trim() || "message";
          }
        }
        if (dataLines.length) yield { data: dataLines.join("\n"), type };
      }
    }
  }

  // One reconnect wait. The timer is the schedule; the browser's own
  // signals that the odds just changed end it early: `online`
  // (connectivity is back) and the page coming back (`sautium:wake` — a
  // phone woke: its retries ran on throttled timers while it slept, and the
  // wait armed last may have most of its 30 s left, strip up, music playing).
  // `navigator.onLine` stays true through most real drops (Wi-Fi/LTE
  // handoff, NAT timeout, a router blip), so neither event can replace
  // the schedule. Resolves with what ended the wait: "timer", "online"
  // or "wake" — a cut-short wait means the schedule should restart.
  window.awaitReconnectWindow = function (ms) {
    return new Promise(resolve => {
      const done = (why) => {
        clearTimeout(timer);
        window.removeEventListener("online", onOnline);
        window.removeEventListener("sautium:wake", onWake);
        resolve(why);
      };
      const onOnline = () => done("online");
      const onWake = () => done("wake");
      const timer = setTimeout(() => done("timer"), ms);
      window.addEventListener("online", onOnline);
      window.addEventListener("sautium:wake", onWake);
    });
  };

  // Drop-in replacement for `new EventSource(path)` when you need
  // request signing. Returns an AbortController — call .abort() to
  // close the stream. Callbacks mirror EventSource semantics so we
  // can swap call sites with minimal change — but for a named event
  // (`event: x`), which onMessage gets too, as { data, type: "x" }: the
  // one stream that carries any tells them apart by `type` (app-shell.js
  // libraryWake). Reconnects with a doubling
  // backoff (1 s … 30 s) that restarts from 1 s whenever a wake or
  // `online` cut a wait short: the network that just came back needs a
  // few quick tries, not one attempt and then the long tail.
  //
  // A page that comes back re-opens the stream at once (`sautium:wake`):
  // the connection it holds may be a dead socket the 45 s idle check would
  // find only after the page had shown its old state for that long, and a
  // fresh connection is signed now and opens with the node's current state.
  // The re-open is no error — onError hears nothing of it. A stream that
  // claims rather than reads opts out (`reopenOnWake: false`).
  window.sseStream = function (path, onMessage, onError, { reopenOnWake = true } = {}) {
    const ctrl = new AbortController();
    let conn = null;                 // this connection's own abort
    const reopen = () => { if (conn) conn.abort(); };
    if (reopenOnWake) window.addEventListener("sautium:wake", reopen);
    ctrl.signal.addEventListener("abort", () => {
      window.removeEventListener("sautium:wake", reopen);
      reopen();
    });
    (async () => {
      let backoff = 1000;
      const waitOut = async () => {
        const why = await window.awaitReconnectWindow(backoff);
        backoff = why === "timer" ? Math.min(backoff * 2, 30000) : 1000;
      };
      while (!ctrl.signal.aborted) {
        conn = new AbortController();
        const { signal } = conn;
        try {
          const resp = await fetch(path, {
            method: "GET",
            headers: { Accept: "text/event-stream" },
            signal,
            cache: "no-store",
          });
          if (!resp.ok) {
            if (onError) onError(new Error("SSE HTTP " + resp.status));
            await waitOut();
            continue;
          }
          backoff = 1000;
          const reader = resp.body.getReader();
          for await (const event of parseSSE(reader, path)) {
            try {
              onMessage(event);
            } catch (e) {
              if (onError) onError(e);
            }
          }
          // Server closed the stream normally — reconnect after a beat (a
          // wake or `online` cuts it short).
          if (!signal.aborted) await window.awaitReconnectWindow(1000);
        } catch (e) {
          if (ctrl.signal.aborted) return;
          if (signal.aborted) {
            backoff = 1000;
            continue;
          }
          if (onError) onError(e);
          await waitOut();
        }
      }
    })();
    return ctrl;
  };
})();
