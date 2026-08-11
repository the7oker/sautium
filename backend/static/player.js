/* Sautium — player layer.
 *
 * Slim replacement for the legacy app.js. Owns the four bits the new
 * shell (app-shell.js) actually consumes from the player domain:
 *
 *   * SSE subscription to /api/events (single multiplexed stream)
 *     → dispatches `np-update` CustomEvents that the mini-player,
 *       Now Playing sheet, queue and chat track-highlight all listen
 *       for.
 *
 *   * `fetchPlaylist()`
 *     → fetches /api/player/playlist, stores it in `window.currentPlaylist`
 *       and dispatches `playlist-loaded`. The queue overlay and any
 *       "expand the row that just started playing" handler rely on
 *       the cached list rather than re-fetching per event.
 *
 *   * Transport commands
 *     → `window.playerCmd(cmd)` and `window.playTrack(mfId)`. The
 *       shell calls these directly when wiring prev/next and
 *       track-row interactions.
 *
 *   * Serial SSE chain
 *     → `playlist_version` changes await `fetchPlaylist()` before any
 *       subscriber sees the np-update, so `currentPlaylist` is always
 *       consistent with the song HQPlayer reports.
 */

(() => {
  // --- State -----------------------------------------------------------
  let currentState = 'disconnected';
  let lastPlaylistVersion = null;
  let _sseSource = null;

  // How long the status stream may be down before the UI admits it.
  // A transport error here says nothing about playback — the music keeps
  // playing on the renderer; only our window into it dropped. Painting
  // "disconnected" on the first hiccup made every Wi-Fi blip and phone
  // wake hide the mini-player and the queue/album highlight for a beat
  // (mp.update treats the state as "nothing playing"). sseStream
  // reconnects on its own and the server pushes current status on every
  // connect, so a healthy recovery repaints within ~1-3s; 10s covers
  // three escalating attempts before we concede the link is really down.
  const DISCONNECT_GRACE_MS = 10000;
  let _disconnectPaint = null;

  // Exposed for legacy reads in some code paths (the shell prefers the
  // detail on np-update; this is a fallback for synchronous callers).
  window.currentPlaylist = [];

  // Latest player status object as delivered by the SSE stream. Screens
  // that mount mid-stream (e.g. the HQPlayer settings screen reading
  // process_speed) read this synchronously for an initial value, then
  // stay current via the np-update event. Same fallback role as
  // currentPlaylist above.
  window.currentStatus = null;

  // --- SSE -------------------------------------------------------------
  // ONE stream per tab. /api/events multiplexes every push channel as
  // typed messages {t, d}: 'status' (player state), 'preview' (phantom
  // preview changed — the open screen re-fetches its own snapshot),
  // 'research' (gear research transition). Browsers cap an HTTP/1.1
  // origin at 6 connections for the whole browser; the previous three
  // parallel streams per tab meant two tabs starved every other fetch
  // into (pending) forever. Never open another standalone SSE here —
  // new event kinds ride this channel.
  function connectEventsSSE() {
    if (_sseSource) _sseSource.abort();
    _sseSource = window.sseStream(
      '/api/events',
      (event) => {
        let msg;
        try { msg = JSON.parse(event.data); }
        catch (e) { console.error('SSE parse error:', e); return; }
        if (msg.t === 'status') {
          handleStatusEvent(msg.d);
        } else if (msg.t === 'preview') {
          // Payload-free ping — the open album page re-fetches its OWN
          // /api/albums/{id} for one consistent snapshot, so there's no
          // second source to keep in sync here.
          window.dispatchEvent(new CustomEvent('sautium:preview-changed'));
        } else if (msg.t === 'research') {
          window.dispatchEvent(new CustomEvent('sautium:research-changed'));
        }
      },
      () => {
        // Transport-level disconnect — sseStream will reconnect. Keep
        // showing the last known state for the grace window; repeated
        // errors during backoff keep the original deadline so the paint
        // lands at the earliest honest moment, not backoff-times later.
        if (_disconnectPaint) return;
        console.debug('events stream down; painting disconnected in',
          DISCONNECT_GRACE_MS / 1000 + 's');
        _disconnectPaint = setTimeout(() => {
          _disconnectPaint = null;
          console.debug('events stream still down; painting disconnected');
          currentState = 'disconnected';
          document.dispatchEvent(new CustomEvent('np-update', {
            detail: { state: 'disconnected' },
          }));
        }, DISCONNECT_GRACE_MS);
      }
    );
  }

  // SSE events are dispatched as soon as the message lands; if we made
  // the handler async directly, two rapid playlist mutations would race
  // their fetchPlaylist() calls. Chain each event onto a promise so the
  // playlist-aware step stays strictly serial.
  let _sseChain = Promise.resolve();
  function handleStatusEvent(data) {
    // A real message means the stream is back — the pending
    // "disconnected" paint is no longer true.
    if (_disconnectPaint) { clearTimeout(_disconnectPaint); _disconnectPaint = null; }
    _sseChain = _sseChain
      .then(() => processStatusEvent(data))
      .catch((e) => console.error('SSE handler error:', e));
  }

  // Stale-tab reload: the SSE status carries the frontend build stamp; when
  // the server's frontend is newer than the one this page loaded with, the
  // tab reloads itself — long-lived SPA tabs otherwise keep running pre-fix
  // code forever. Deferred while this tab is actively rendering audio (a
  // reload would kill the <audio>); the check re-fires on every event, so
  // it lands at the next pause/stop.
  function maybeReloadForUpdate(build) {
    if (window.__SAUTIUM_BUILD === build) return;
    if (browserRenderer.active && browserRenderer.playingNow) return;
    if (sessionStorage.getItem('sautiumReloadedFor') === String(build)) return;
    sessionStorage.setItem('sautiumReloadedFor', String(build));
    location.reload();
  }

  async function processStatusEvent(data) {
    currentState = data.state;
    if (data.ui_build) maybeReloadForUpdate(data.ui_build);
    // process_speed is HQPlayer's realtime DSP processing factor (0.0 when
    // unknown). Carried straight through on the status object so any screen
    // can read it off window.currentStatus or the np-update detail.
    window.currentStatus = data;
    // Browser-output renderer lifecycle: THIS tab renders audio only while
    // it holds the sessionStorage claim AND the backend output is browser.
    // Reloads re-attach (same tab claim); switching outputs detaches.
    const outType = data.output && data.output.type;
    if (sessionStorage.getItem('sautiumBrowserRenderer') === '1') {
      if (outType === 'browser' && !browserRenderer.active) {
        browserRenderer.attach();
      } else if (outType && outType !== 'browser' && browserRenderer.active) {
        browserRenderer.detach();
      }
    }
    if (data.playlist_version !== undefined &&
        data.playlist_version !== lastPlaylistVersion) {
      // Update marker first so a duplicate event doesn't trigger a
      // second refetch while this one is in flight.
      lastPlaylistVersion = data.playlist_version;
      try { await fetchPlaylist(); }
      catch (e) { console.warn('fetchPlaylist failed during SSE handling:', e); }
    }
    document.dispatchEvent(new CustomEvent('np-update', { detail: data }));
  }

  // --- Playlist --------------------------------------------------------
  async function fetchPlaylist() {
    try {
      const resp = await fetch('/api/player/playlist');
      if (!resp.ok) return;
      const data = await resp.json();
      window.currentPlaylist = data.tracks || [];
      document.dispatchEvent(new CustomEvent('playlist-loaded', { detail: data }));
    } catch (e) {
      console.warn('fetchPlaylist error:', e);
    }
  }

  // --- Transport -------------------------------------------------------
  // Orphaned browser output: the output is "this device" but NO tab renders
  // it (the previous renderer closed). A play gesture from ANY tab claims
  // the renderer role — the tap doubles as the autoplay unlock — instead of
  // playing into the void. When a renderer IS attached elsewhere, commands
  // act as a remote control (Connect semantics) and nothing is claimed.
  function maybeClaimRenderer() {
    const out = window.currentStatus && window.currentStatus.output;
    if (out && out.type === 'browser' && out.renderer_attached === false
        && !browserRenderer.active) {
      browserRenderer.attach();
    }
  }

  async function playerCmd(cmd) {
    if (cmd === 'play') maybeClaimRenderer();
    try {
      const resp = await fetch('/api/player/' + cmd, { method: 'POST' });
      // Play intents against an unreachable output (dozing renderer,
      // closed HQPlayer) 503 — surface the one dialog with a way to the
      // Output picker instead of dying in the console. Volume/pause/stop
      // stay quiet: nagging on every tick helps nobody.
      if (!resp.ok && resp.status === 503
          && (cmd === 'play' || cmd === 'next' || cmd === 'previous')
          && window.reportOutputUnavailable) {
        const err = await resp.json().catch(() => ({}));
        window.reportOutputUnavailable(err.detail || '');
      }
    } catch (e) { console.error('Player command failed:', e); }
  }

  function togglePlayPause() {
    // The renderer tab retries a blocked <audio>.play() inside THIS user
    // gesture first — autoplay policies accept it here where a directive
    // arriving over SSE (no gesture context) can be rejected. When the
    // local resume handles it, DON'T also send /play: the backend may
    // still think it's stopped (post-error) and would answer with a load
    // directive that reloads the track from zero, discarding the position
    // the recovery just preserved. The `playing` event syncs the state.
    if (browserRenderer.active && currentState !== 'playing') {
      if (browserRenderer.resumeLocal()) return Promise.resolve();
    }
    return playerCmd(currentState === 'playing' ? 'pause' : 'play');
  }

  // ---- Browser output renderer ("this device") ---------------------------
  // The tab that selects the browser output becomes the audio renderer: a
  // hidden <audio> plays short-lived signed same-origin media URLs;
  // transport directives arrive on a per-tab SSE command channel; element
  // events are POSTed back and become the backend's status feed.
  // MediaSession mirrors metadata to the lock screen. Backend counterpart:
  // playback/browser_backend.py.
  const browserRenderer = {
    tab: null,
    ctrl: null,
    audio: null,
    pendingPlay: false,
    playingNow: false,
    // Local queue tail (signed URLs + metadata) shipped with every load
    // directive. Mobile browsers freeze background tabs — SSE is dead while
    // the screen is off — so the NEXT track must start locally inside the
    // `ended` handler, from this list, and the backend is only notified.
    playlist: [],
    queueIndex: null,
    currentMediaId: null,
    epoch: null,
    // Blob insurance (queue_index → {u: objectURL, id: media identity};
    // current + lookahead). Entries are validated by media id — queue
    // indexes shift on replace/reorder, and a stale blob must never
    // impersonate a different track.
    // A dozing phone parks the tab's network the moment no media is
    // actively streaming (measured live: event POSTs flow ~1/s during
    // streaming playback and park within seconds of blob-only playback).
    // So playback always STREAMS — the network-active media session is
    // what keeps the doze latch away — while fully fetched blobs stand by
    // for an instant same-position swap whenever the stream starves.
    blobs: new Map(),
    fetching: new Map(),   // queue_index → AbortController
    // True once the stream demonstrably starved (doze latch closed). While
    // parked AND hidden, boundaries go blob-first — deterministic audio
    // with no reliance on timers a frozen tab may never fire. Any
    // completed response (fetch, event POST, visibility return) unparks.
    networkParked: false,
    _watchdog: null,

    get active() { return !!this.ctrl; },

    attach() {
      if (this.ctrl) return;
      this.tab = sessionStorage.getItem('sautiumBrowserTabId');
      if (!this.tab) {
        this.tab = (crypto.randomUUID && crypto.randomUUID())
          || (Date.now().toString(36) + Math.random().toString(36).slice(2));
        sessionStorage.setItem('sautiumBrowserTabId', this.tab);
      }
      sessionStorage.setItem('sautiumBrowserRenderer', '1');
      if (!this.audio) {
        this.audio = new Audio();
        this.audio.preload = 'auto';
        this._wireAudio();
      }
      this.ctrl = window.sseStream(
        '/api/player/browser/channel?tab=' + encodeURIComponent(this.tab),
        (msg) => {
          try { this._onDirective(JSON.parse(msg.data)); }
          catch (e) { console.warn('browser renderer directive error:', e); }
        },
        () => {});
    },

    detach() {
      sessionStorage.removeItem('sautiumBrowserRenderer');
      if (this.ctrl) { this.ctrl.abort(); this.ctrl = null; }
      if (this.audio) {
        this.audio.pause();
        this.audio.removeAttribute('src');
        this.audio.load();
      }
      this.pendingPlay = false;
      this.playingNow = false;
      this.playlist = [];
      this.queueIndex = null;
      this.currentMediaId = null;
      this.epoch = null;
      clearTimeout(this._watchdog);
      this._clearBlobs();
    },

    resumeLocal() {
      // Called inside a real user gesture (play tap) — unlocks autoplay.
      // Returns true only when playback was genuinely handled locally;
      // false falls through to the server path.
      if (!this.audio || !this.audio.src) return false;
      if (this.audio.error) {
        // Dead pipeline (network died mid-stream): a held blob revives it
        // at the same position; without one the server reload is the floor.
        return this._swapToBlob({ resume: true });
      }
      if (this.pendingPlay || this.audio.paused) {
        this._tryPlay();
        return true;
      }
      return false;
    },

    _post(event) {
      if (!this.tab) return;
      fetch('/api/player/browser/event', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          tab: this.tab,
          event,
          position: this.audio ? this.audio.currentTime : null,
          duration: (this.audio && isFinite(this.audio.duration))
            ? this.audio.duration : null,
          // Every event carries the rendered slot + directive epoch: the
          // backend resyncs its index from these (a lost `advanced` POST
          // self-heals) and drops events that raced a newer load.
          queue_index: this.queueIndex,
          epoch: this.epoch,
        }),
      })
        .then((r) => (r.ok ? r.json() : null))
        .then((resp) => {
          if (!resp) return;
          this.networkParked = false;   // a completed response = alive
          // The response is the background-safe downlink (SSE may be dead
          // in a frozen tab): `advanced` returns the refreshed tail —
          // including radio refills — and `ended` past the local tail
          // returns the next load directive.
          if (resp.queue) {
            this.playlist = resp.queue;
            this._relocate();
          }
          if (resp.directive) this._onDirective(resp.directive);
        })
        .catch(() => {});
    },

    _wireAudio() {
      const a = this.audio;
      a.addEventListener('playing', () => {
        this.pendingPlay = false;
        this.playingNow = true;
        this._post('playing');
        this._prefetchNext();
      });
      a.addEventListener('pause', () => {
        this.playingNow = false;
        if (!a.ended) this._post('paused');
      });
      a.addEventListener('ended', () => {
        this.playingNow = false;
        if (!this._advanceLocal()) this._post('ended');
      });
      a.addEventListener('error', () => {
        if (!a.src) return;
        // A parked network killed the streaming src mid-track — the fully
        // fetched blob takes over at the same position instead of dying.
        if (this._swapToBlob({ resume: this.playingNow || this.pendingPlay })) {
          this.networkParked = true;
          return;
        }
        this._post('error');
      });
      const starving = () => {
        // currentTime > 2 filters the routine waiting at every track start
        // (no data for the first ~100ms) from a drained mid-track readahead
        // — track starts are guarded by the boundary watchdog instead.
        if (!a.paused && a.readyState < 3 && a.currentTime > 2) {
          if (this._swapToBlob({ resume: true })) this.networkParked = true;
        }
      };
      a.addEventListener('waiting', starving);
      a.addEventListener('stalled', starving);
      document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible') {
          this.networkParked = false;
          this._prefetchNext();   // top the runway back up
        }
      });
      let lastTimeupdate = 0;
      a.addEventListener('timeupdate', () => {
        const now = Date.now();
        if (!a.paused && now - lastTimeupdate >= 1000) {
          lastTimeupdate = now;
          this._post('timeupdate');
        }
      });
    },

    // -- blob double-buffer ------------------------------------------------

    _heldBlob(index, mediaId) {
      const e = this.blobs.get(index);
      return (e && e.id === mediaId) ? e.u : null;
    },

    _startCurrent(url, play, startPos) {
      const a = this.audio;
      clearTimeout(this._watchdog);
      const held = this._heldBlob(this.queueIndex, this.currentMediaId);
      // Foreground: stream-first (instant start, and the download itself
      // nudges the doze away). Hidden: ALWAYS blob-first when held — the
      // doze latch closes silently (on a LAN the whole file lands before
      // any starvation signal, so no flag ever flips), boundary stream
      // fetches park, frozen timers never fire, and the >2s waiting
      // threshold blocks the start-of-track swap: the runway would sit
      // unused. Deterministic local playback is the only background mode
      // that survives without a single working network primitive.
      if (held && (this.networkParked || document.visibilityState === 'hidden')) {
        a.src = held;
      } else {
        a.src = url;
        if (!held) {
          this._fetchBlob(this.queueIndex, url, this.currentMediaId);
        }
        if (play) {
          // Boundary watchdog: if the stream produced no data within the
          // pre-latch window (timers still fire there), fall to the blob.
          this._watchdog = setTimeout(() => {
            if (a.readyState < 3 && !a.paused && this._swapToBlob({ resume: true })) {
              this.networkParked = true;
            }
          }, 2000);
        }
      }
      if (startPos > 0) {
        // Resume point from a re-prime (page reload mid-track): seek once
        // the fresh element knows its duration.
        const src = a.src;
        a.addEventListener('loadedmetadata', () => {
          if (a.src === src) a.currentTime = startPos;
        }, { once: true });
      }
      this._evictBlobs();
      if (play) this._tryPlay();
    },

    _fetchBlob(index, url, mediaId, onDone) {
      if (index === null || !url
          || this._heldBlob(index, mediaId) || this.fetching.has(index)) {
        if (onDone) onDone();
        return;
      }
      const ctrl = new AbortController();
      this.fetching.set(index, ctrl);
      fetch(url, { signal: ctrl.signal })
        .then((r) => (r.ok ? r.blob() : null))
        .then((blob) => {
          this.fetching.delete(index);
          if (!blob) { if (onDone) onDone(); return; }
          this.networkParked = false;   // a completed response = alive
          const old = this.blobs.get(index);
          if (old) URL.revokeObjectURL(old.u);
          this.blobs.set(index, { u: URL.createObjectURL(blob), id: mediaId });
          this._evictBlobs();
          // The current track may already be starving on its cut-off
          // streaming src — take over the moment the bytes are local.
          if (index === this.queueIndex) {
            const a = this.audio;
            if (a && (a.error || (!a.paused && a.readyState < 3 && !a.ended))) {
              this._swapToBlob({ resume: this.playingNow || this.pendingPlay });
            }
          }
          if (onDone) onDone();
        })
        .catch(() => {
          this.fetching.delete(index);
          if (onDone) onDone();
        });
    },

    _swapToBlob(opts) {
      const a = this.audio;
      const blobUrl = this._heldBlob(this.queueIndex, this.currentMediaId);
      if (!a || !blobUrl || a.src === blobUrl) return false;
      const pos = a.currentTime || 0;
      a.src = blobUrl;
      if (pos > 0) {
        a.addEventListener('loadedmetadata', () => {
          if (a.src === blobUrl) a.currentTime = pos;
        }, { once: true });
      }
      if (opts && opts.resume) this._tryPlay();
      return true;
    },

    _relocate() {
      // Queue mutations renumber the slots (removing a track before the
      // playing one shifts everything down) — re-find OUR track in the
      // fresh tail by media identity, closest slot wins.
      if (this.queueIndex === null || !this.currentMediaId) return;
      const at = this.playlist.find((e) => e.queue_index === this.queueIndex);
      if (at && at.media === this.currentMediaId) return;
      let best = null;
      for (const e of this.playlist) {
        if (e.media === this.currentMediaId
            && (best === null
                || Math.abs(e.queue_index - this.queueIndex)
                   < Math.abs(best - this.queueIndex))) {
          best = e.queue_index;
        }
      }
      if (best !== null) this.queueIndex = best;
    },

    _entryAfter(index) {
      if (index === null || !this.playlist.length) return null;
      const i = this.playlist.findIndex((e) => e.queue_index === index);
      return (i >= 0 && this.playlist[i + 1]) || null;
    },

    _nextEntry() { return this._entryAfter(this.queueIndex); },

    // How deep the blob insurance reaches past the current track. Once the
    // doze latch closes, no further fetch leaves the tab — held blobs are
    // the whole remaining runway (measured on a LAN: the streaming src
    // downloads whole in seconds, so "actively streaming" keeps the doze
    // away only briefly; depth 8 ≈ 35-45 min of pocket radio). Blobs are
    // disk-backed in the browser's blob storage, not resident RAM.
    _PREFETCH_AHEAD: 8,

    _lookahead() {
      const ahead = [];
      let idx = this.queueIndex;
      for (let k = 0; k < this._PREFETCH_AHEAD; k++) {
        const nxt = this._entryAfter(idx);
        if (!nxt) break;
        ahead.push(nxt);
        idx = nxt.queue_index;
      }
      return ahead;
    },

    _prefetchNext() {
      // Sequential chain, nearest first — parallel fetches of 8 FLACs
      // would congest the very Wi-Fi link the audible stream rides on.
      const targets = this._lookahead().filter(
        (e) => !this._heldBlob(e.queue_index, e.media)
               && !this.fetching.has(e.queue_index));
      const runNext = () => {
        const e = targets.shift();
        if (e) this._fetchBlob(e.queue_index, e.url, e.media, runNext);
      };
      runNext();
      this._evictBlobs();
    },

    _evictBlobs() {
      const keep = new Set([this.queueIndex]);
      for (const e of this._lookahead()) keep.add(e.queue_index);
      for (const [i, e] of [...this.blobs]) {
        if (!keep.has(i) && (!this.audio || this.audio.src !== e.u)) {
          URL.revokeObjectURL(e.u);
          this.blobs.delete(i);
        }
      }
      for (const [i, c] of [...this.fetching]) {
        if (!keep.has(i)) {
          c.abort();
          this.fetching.delete(i);
        }
      }
    },

    _clearBlobs() {
      for (const c of this.fetching.values()) c.abort();
      this.fetching.clear();
      for (const e of this.blobs.values()) URL.revokeObjectURL(e.u);
      this.blobs.clear();
    },

    _onDirective(d) {
      const a = this.audio;
      switch (d.cmd) {
        case 'load':
          if (d.epoch !== undefined) this.epoch = d.epoch;
          this.playlist = d.queue || [];
          if (!d.play && d.queue_index !== undefined
              && d.queue_index === this.queueIndex
              && d.media === this.currentMediaId && a.src) {
            // Channel reconnect re-prime of the SAME track we already hold
            // (index + media identity — an index alone can point at a
            // different track after a replace) — reloading the src would
            // cut playback / lose the position.
            this._mediaSession(d.meta || {});
            this._prefetchNext();
            break;
          }
          this.queueIndex = (d.queue_index !== undefined) ? d.queue_index : null;
          this.currentMediaId = (d.media !== undefined) ? d.media : null;
          this._startCurrent(d.url, !!d.play, d.position || 0);
          this._mediaSession(d.meta || {});
          break;
        case 'queue':
          this.playlist = d.queue || [];
          this._relocate();
          this._prefetchNext();
          break;
        case 'play':   this._tryPlay(); break;
        case 'pause':  a.pause(); break;
        case 'seek':   a.currentTime = d.position || 0; break;
        case 'volume': a.volume = Math.max(0, Math.min(1, (d.level || 0) / 100)); break;
        case 'stop':
          a.pause();
          a.removeAttribute('src');
          a.load();
          this.queueIndex = null;
          this.currentMediaId = null;
          this.playingNow = false;
          clearTimeout(this._watchdog);
          this._clearBlobs();
          break;
        case 'released':
          // Another tab took over as the renderer.
          this.detach();
          break;
      }
    },

    _advanceLocal() {
      // Background-safe track advance: swap to the next tail entry right
      // inside the `ended` handler (media playback may continue in a
      // frozen tab; an SSE round-trip may not). The next track normally
      // plays from its prefetched blob — no network at the boundary.
      // Returns false at the true end of the local tail — the plain
      // `ended` POST lets the backend decide (it may know more of the
      // queue than the shipped tail).
      const next = this._nextEntry();
      if (!next) return false;
      this.queueIndex = next.queue_index;
      this.currentMediaId = (next.media !== undefined) ? next.media : null;
      this._startCurrent(next.url, true, 0);
      this._mediaSession(next.meta || {});
      this._post('advanced');
      // Bias the prefetch of the track after next into THIS instant: a
      // dozing phone slams the network shut moments after audio goes
      // silent, but requests fired right at the boundary demonstrably
      // slip through. Re-attempted on `playing` anyway (idempotent).
      this._prefetchNext();
      return true;
    },

    _tryPlay() {
      const p = this.audio.play();
      if (p && p.catch) {
        p.catch(() => {
          // Autoplay policy rejected a directive outside a user gesture —
          // the next play tap goes through resumeLocal (gesture context).
          this.pendingPlay = true;
          this._post('paused');
        });
      }
    },

    _mediaSession(meta) {
      if (!('mediaSession' in navigator)) return;
      const artwork = [];
      if (meta.cover_id) {
        artwork.push({ src: '/api/covers/' + meta.cover_id,
                       sizes: '500x500', type: 'image/webp' });
      } else if (meta.cover_url) {
        artwork.push({ src: meta.cover_url });
      }
      navigator.mediaSession.metadata = new MediaMetadata({
        title: meta.title || '',
        artist: meta.artist || '',
        album: meta.album || '',
        artwork,
      });
      navigator.mediaSession.setActionHandler('play', () => {
        // Same rule as togglePlayPause: a successful local resume must not
        // be followed by /play — a stopped backend answers that with a
        // from-zero reload.
        if (!this.resumeLocal()) playerCmd('play');
      });
      navigator.mediaSession.setActionHandler('pause', () => {
        this.audio.pause();
        playerCmd('pause');
      });
      navigator.mediaSession.setActionHandler('previoustrack', () => playerCmd('previous'));
      navigator.mediaSession.setActionHandler('nexttrack', () => playerCmd('next'));
    },
  };

  async function playTrack(mediaFileId) {
    maybeClaimRenderer();
    try {
      const resp = await fetch('/api/player/play-track', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ track_id: mediaFileId }),
      });
      // No client-side refetch on success — the backend bumps
      // playlist_version on the next SSE tick and processStatusEvent
      // awaits fetchPlaylist() before notifying subscribers.
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        if (resp.status === 503 && window.reportOutputUnavailable) {
          window.reportOutputUnavailable(err.detail || '');
        } else if (window.notifyDialog) {
          const esc = window.escapeProfileHtml || ((s) => s);
          window.notifyDialog({
            title: 'Playback unavailable',
            message: esc(err.detail || 'Could not play the track.'),
            kind: 'error',
          });
        }
      }
    } catch (e) {
      console.error('Play track failed:', e);
    }
  }

  // --- Public API ------------------------------------------------------
  window.playerCmd = playerCmd;
  window.playTrack = playTrack;
  window.togglePlayPause = togglePlayPause;
  // Every play-intent gesture in the UI must claim the orphaned browser
  // output (the tap doubles as the autoplay unlock) — screens call this
  // before their play-ish fetches (sessions, albums, radio, jump).
  window.maybeClaimRenderer = maybeClaimRenderer;
  window.browserRenderer = browserRenderer;
  window.fetchPlaylist = fetchPlaylist;

  // --- Boot ------------------------------------------------------------
  function init() {
    connectEventsSSE();
    fetchPlaylist();
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  window.addEventListener('beforeunload', () => {
    if (_sseSource) _sseSource.abort();
    if (_previewSSE) _previewSSE.abort();
  });
})();
