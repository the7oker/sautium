/* Sautium — player layer.
 *
 * Slim replacement for the legacy app.js. Owns the four bits the new
 * shell (app-shell.js) actually consumes from the player domain:
 *
 *   * SSE subscription to /api/player/status/stream
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
  function connectStatusSSE() {
    if (_sseSource) _sseSource.abort();
    _sseSource = window.sseStream(
      '/api/player/status/stream',
      (event) => {
        let data;
        try { data = JSON.parse(event.data); }
        catch (e) { console.error('SSE parse error:', e); return; }
        handleStatusEvent(data);
      },
      () => {
        // Transport-level disconnect — sseStream will reconnect. Flip
        // the shared state and let subscribers paint the "disconnected"
        // affordance until the next message arrives.
        currentState = 'disconnected';
        document.dispatchEvent(new CustomEvent('np-update', {
          detail: { state: 'disconnected' },
        }));
      }
    );
  }

  // Phantom-preview change pings. A bare SSE: each message just means "preview
  // state changed" (a track started/finished buffering, or enrichment landed).
  // Re-broadcast as a DOM event; the open album page (app-shell.js) re-fetches
  // its OWN /api/albums/{id} for one consistent snapshot (features + buffering),
  // so there's no payload to keep in sync here — the page owns the re-read.
  let _previewSSE = null;
  function connectPreviewSSE() {
    if (_previewSSE) _previewSSE.abort();
    _previewSSE = window.sseStream(
      '/api/player/preview-events',
      () => window.dispatchEvent(new CustomEvent('sautium:preview-changed')),
      () => { /* sseStream auto-reconnects; nothing to repaint here */ }
    );
  }

  // SSE events are dispatched as soon as the message lands; if we made
  // the handler async directly, two rapid playlist mutations would race
  // their fetchPlaylist() calls. Chain each event onto a promise so the
  // playlist-aware step stays strictly serial.
  let _sseChain = Promise.resolve();
  function handleStatusEvent(data) {
    _sseChain = _sseChain
      .then(() => processStatusEvent(data))
      .catch((e) => console.error('SSE handler error:', e));
  }

  async function processStatusEvent(data) {
    currentState = data.state;
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
    try { await fetch('/api/player/' + cmd, { method: 'POST' }); }
    catch (e) { console.error('Player command failed:', e); }
  }

  function togglePlayPause() {
    // The renderer tab retries a blocked <audio>.play() inside THIS user
    // gesture first — autoplay policies accept it here where a directive
    // arriving over SSE (no gesture context) can be rejected.
    if (browserRenderer.active && currentState !== 'playing') {
      browserRenderer.resumeLocal();
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
    // Local queue tail (signed URLs + metadata) shipped with every load
    // directive. Mobile browsers freeze background tabs — SSE is dead while
    // the screen is off — so the NEXT track must start locally inside the
    // `ended` handler, from this list, and the backend is only notified.
    playlist: [],
    queueIndex: null,
    epoch: null,

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
      this.playlist = [];
      this.queueIndex = null;
      this.epoch = null;
    },

    resumeLocal() {
      // Called inside a real user gesture (play tap) — unlocks autoplay.
      if (this.audio && this.audio.src && (this.pendingPlay || this.audio.paused)) {
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
          // The response is the background-safe downlink (SSE may be dead
          // in a frozen tab): `advanced` returns the refreshed tail —
          // including radio refills — and `ended` past the local tail
          // returns the next load directive.
          if (resp.queue) this.playlist = resp.queue;
          if (resp.directive) this._onDirective(resp.directive);
        })
        .catch(() => {});
    },

    _wireAudio() {
      const a = this.audio;
      a.addEventListener('playing', () => { this.pendingPlay = false; this._post('playing'); });
      a.addEventListener('pause', () => { if (!a.ended) this._post('paused'); });
      a.addEventListener('ended', () => {
        if (!this._advanceLocal()) this._post('ended');
      });
      a.addEventListener('error', () => { if (a.src) this._post('error'); });
      let lastTimeupdate = 0;
      a.addEventListener('timeupdate', () => {
        const now = Date.now();
        if (!a.paused && now - lastTimeupdate >= 1000) {
          lastTimeupdate = now;
          this._post('timeupdate');
        }
      });
    },

    _onDirective(d) {
      const a = this.audio;
      switch (d.cmd) {
        case 'load':
          if (d.epoch !== undefined) this.epoch = d.epoch;
          this.playlist = d.queue || [];
          if (!d.play && d.queue_index !== undefined
              && d.queue_index === this.queueIndex && a.src) {
            // Channel reconnect re-prime of the slot we already hold —
            // reloading the src would cut playback / lose the position.
            this._mediaSession(d.meta || {});
            break;
          }
          this.queueIndex = (d.queue_index !== undefined) ? d.queue_index : null;
          a.src = d.url;
          this._mediaSession(d.meta || {});
          if (d.play) this._tryPlay();
          break;
        case 'queue':
          this.playlist = d.queue || [];
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
      // frozen tab; an SSE round-trip may not). Returns false at the true
      // end of the local tail — the plain `ended` POST lets the backend
      // decide (it may know more of the queue than the shipped tail).
      if (this.queueIndex === null || !this.playlist.length) return false;
      const i = this.playlist.findIndex((e) => e.queue_index === this.queueIndex);
      const next = i >= 0 ? this.playlist[i + 1] : null;
      if (!next) return false;
      this.queueIndex = next.queue_index;
      this.audio.src = next.url;
      this._mediaSession(next.meta || {});
      this._tryPlay();
      this._post('advanced');
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
        this._tryPlay();
        playerCmd('play');
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
      await fetch('/api/player/play-track', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ track_id: mediaFileId }),
      });
      // No client-side refetch — the backend bumps playlist_version on
      // the next SSE tick and processStatusEvent awaits fetchPlaylist()
      // before notifying subscribers.
    } catch (e) {
      console.error('Play track failed:', e);
    }
  }

  // --- Public API ------------------------------------------------------
  window.playerCmd = playerCmd;
  window.playTrack = playTrack;
  window.togglePlayPause = togglePlayPause;
  window.browserRenderer = browserRenderer;
  window.fetchPlaylist = fetchPlaylist;

  // --- Boot ------------------------------------------------------------
  function init() {
    connectStatusSSE();
    connectPreviewSSE();
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
