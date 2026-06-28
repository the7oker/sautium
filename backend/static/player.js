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
  async function playerCmd(cmd) {
    try { await fetch('/api/player/' + cmd, { method: 'POST' }); }
    catch (e) { console.error('Player command failed:', e); }
  }

  function togglePlayPause() {
    return playerCmd(currentState === 'playing' ? 'pause' : 'play');
  }

  async function playTrack(mediaFileId) {
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
