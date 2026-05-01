// Sautium request signing.
//
// Every privileged request to the backend carries:
//   X-Sautium-Ts   unix seconds
//   X-Sautium-Sig  hex(HMAC-SHA256(secret, METHOD\nPATH_AND_QUERY\nTS\nsha256_hex(body)))
//
// The secret is inlined into the HTML by the backend (see
// backend/main.py @app.get("/")). It lives in window.__SAUTIUM_SECRET
// and never leaves this origin (browsers forbid cross-origin reads
// of HTML responses without explicit CORS).
//
// This module:
//   • monkey-patches window.fetch so all in-app calls auto-sign,
//   • exports sseStream(path, onMessage, onError) — the EventSource
//     replacement that uses fetch+ReadableStream so we can attach
//     auth headers (EventSource API can't).
//
// Whitelisted backend paths (see backend/auth_hmac.py) accept
// unsigned requests, so we sign blindly — the backend ignores
// signatures on those paths.

(function () {
  const SECRET_RAW = window.__SAUTIUM_SECRET;
  if (!SECRET_RAW) {
    console.error("Sautium: window.__SAUTIUM_SECRET missing — auth disabled");
    return;
  }

  const enc = new TextEncoder();
  let _key = null;

  async function getKey() {
    if (_key) return _key;
    _key = await crypto.subtle.importKey(
      "raw",
      enc.encode(SECRET_RAW),
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
    const ts = Math.floor(Date.now() / 1000).toString();
    const bodyHash = await sha256Hex(await bodyBytes(body));
    const canonical = `${method}\n${pathAndQuery}\n${ts}\n${bodyHash}`;
    const key = await getKey();
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
    const { ts, sig } = await signRequest(method, pathAndQuery, body);

    const headers = new Headers(init.headers || {});
    headers.set("X-Sautium-Ts", ts);
    headers.set("X-Sautium-Sig", sig);

    return _origFetch(input, { ...init, headers });
  };

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
