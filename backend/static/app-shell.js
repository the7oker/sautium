/* =====================================================================
 * Sautium app shell — router + screen registry + mini-player adapter.
 * Phase 1, Step 1.1.
 *
 * Loaded BEFORE app.js so the shell exists when legacy bootstrap code
 * runs. The shell listens for `np-update` CustomEvents emitted by
 * app.js's updateNowPlaying() and renders the mini-player accordingly.
 * Other UI surfaces are owned by individual screen renderers.
 * ===================================================================== */

(function () {
  'use strict';

  /* ---------- Placeholder colour helpers ---------- */

  function hashName(name) {
    let h = 0;
    const s = String(name || '');
    for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0;
    return Math.abs(h);
  }

  function avatarPlaceholder(name) {
    if (!name) return { bg: 'var(--color-surface-hi)', initials: '?' };
    const hue = hashName(name) % 360;
    const initials =
      name.split(/\s+/).slice(0, 2)
        .map(w => (w[0] || '').toUpperCase()).join('') || '?';
    return { bg: `hsl(${hue}, 28%, 22%)`, initials };
  }

  function coverPlaceholderColors(seed) {
    const h = hashName(seed || 'untitled');
    const hue = h % 360;
    return {
      bg1: `hsl(${hue}, 22%, 18%)`,
      bg2: `hsl(${(hue + 30) % 360}, 18%, 12%)`,
    };
  }

  /* ---------- HTML escape ---------- */

  function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  /* ---------- Now-playing track highlight ----------
     Toggles `is-playing` on detail-screen track rows whose
     data-media-file-id matches the currently playing track.
     Wired to the `np-update` SSE event so highlight follows
     playback across detail-screen renders. */

  // Cached from the most recent np-update so `playlist-loaded` (which
  // carries no detail of its own) can re-run the highlight pass after a
  // detail screen renders with stale rows.
  let _lastNpMediaFileId = null;

  function updatePlayingHighlight() {
    const target = _lastNpMediaFileId != null ? String(_lastNpMediaFileId) : null;
    document.querySelectorAll('.detail-screen .track-row[data-media-file-id]')
      .forEach(row => {
        const match = target !== null
          && row.getAttribute('data-media-file-id') === target;
        row.classList.toggle('is-playing', match);
      });
  }

  /* ---------- Cover URL builder ----------
     Two cover sources, picked in priority order:
       1) cover_id (UUID, already resolved)  → /api/covers/<uuid>
       2) media_file_id (int, lazy resolution) → /api/covers/by-media/<int>
     The lazy endpoint extracts from disk / Last.fm on first request and
     caches the result in covers.id. Returns "" when neither is available
     so callers fall back to a gradient placeholder. */

  function coverUrl(item) {
    if (!item) return '';
    if (item.cover_id) return '/api/covers/' + encodeURIComponent(item.cover_id);
    if (item.media_file_id != null) {
      return '/api/covers/by-media/' + encodeURIComponent(item.media_file_id);
    }
    return '';
  }

  // Artist avatar layered HTML: an initials backdrop with an <img>
  // overlay on top. The <img> covers the initials when it loads
  // (artist photo from Last.fm); on 404/error it removes itself or
  // falls back to an album cover, revealing the initials beneath.
  // `initialsHtml` is the already-styled fallback element string.
  // `albumFallbackUrl` is optional (e.g. coverUrl(item)).
  function artistAvatarInner(artistId, initialsHtml, albumFallbackUrl) {
    if (!artistId) {
      if (albumFallbackUrl) {
        return `${initialsHtml}<img src="${albumFallbackUrl}" alt=""
          loading="lazy" onerror="this.remove()">`;
      }
      return initialsHtml;
    }
    const photoUrl = '/api/covers/by-artist/' + encodeURIComponent(artistId);
    const onError = albumFallbackUrl
      ? `this.onerror=null;this.src='${albumFallbackUrl}'`
      : `this.remove()`;
    return `${initialsHtml}<img src="${photoUrl}" alt=""
      loading="lazy" onerror="${onError}">`;
  }

  /* ---------- Lightweight markdown ----------
     Line-oriented parser sized for the prose the AI DJ emits in
     practice: headings, horizontal rules, bullet/numbered lists,
     pipe tables, inline code, **bold**, *italic*. No external
     library; escapeHtml runs first so angle brackets in the input
     can't break out of our generated HTML. Streaming-friendly —
     gets re-run on every delta, so partial markup like an
     unclosed `**` settles visually as soon as the closer arrives. */

  function applyInlineMd(s) {
    return s
      .replace(/`([^`\n]+)`/g, '<code>$1</code>')
      // 1. Triple `***X***` → bold-italic. Run first so the inner
      //    italic stars can't be stolen by the lazy bold pass below.
      .replace(/\*\*\*([^*\n]+?)\*\*\*/g, '<strong><em>$1</em></strong>')
      // 2. Italic before bold. The closing `*` has NO `(?!\*)` lookahead
      //    so it can sit immediately before the closing `**` of an
      //    enclosing bold — handles `**Peter — *Machines of Desire***`
      //    where the inner italic and outer bold share the trailing
      //    star run. Once italic has wrapped the inner span as <em>...</em>,
      //    the outer bold's content has no leftover stars and matches
      //    cleanly in step 3.
      .replace(/(^|[^*])\*(?!\s)([^*\n]+?)\*/g,
               (_, pre, body) => `${pre}<em>${body}</em>`)
      // 3. Bold last. By now any nested italic is already <em>…</em>.
      .replace(/\*\*([^*\n]+?)\*\*/g, '<strong>$1</strong>')
      // 4. Links. HTTPS only — disarms javascript:, file:, data: URIs
      //    that would otherwise execute when the user clicks. The
      //    href has already been HTML-escaped by mdToHtml, so we
      //    just need to wrap it.
      .replace(/\[([^\]]+)\]\(((?:https?:)?\/\/[^)\s]+)\)/g,
               (_, label, url) =>
                 `<a href="${url}" target="_blank" rel="noopener" style="color:var(--color-amber);">${label}</a>`);
  }

  function mdToHtml(text) {
    const lines = escapeHtml(text).split('\n');
    const out = [];
    let listMode = null;          // 'ol' | 'ul' | null
    let paragraphLines = [];      // raw lines, joined with <br>
    let tableLines = [];          // raw "| ... |" rows pending flush

    const flushParagraph = () => {
      if (paragraphLines.length) {
        out.push('<p>' + paragraphLines.join('<br>') + '</p>');
        paragraphLines = [];
      }
    };
    const closeList = () => {
      if (listMode) {
        out.push('</' + listMode + '>');
        listMode = null;
      }
    };

    const splitRow = (row) => {
      let r = row.trim();
      if (r.startsWith('|')) r = r.slice(1);
      if (r.endsWith('|')) r = r.slice(0, -1);
      return r.split('|').map(c => applyInlineMd(c.trim()));
    };
    const flushTable = () => {
      if (tableLines.length === 0) return;
      // A real markdown table needs a header, a `|---|---|` separator,
      // and at least one data row; otherwise treat the buffered lines
      // as plain prose (paragraph) so partial input mid-stream doesn't
      // collapse into a malformed `<table>`.
      const sep = (tableLines[1] || '').trim();
      const looksLikeTable =
        tableLines.length >= 2 &&
        /^\|?\s*:?[-]+:?(?:\s*\|\s*:?[-]+:?)*\s*\|?$/.test(sep);
      if (!looksLikeTable) {
        for (const tl of tableLines) {
          paragraphLines.push(applyInlineMd(tl));
        }
        tableLines = [];
        return;
      }
      const head = splitRow(tableLines[0]);
      const body = tableLines.slice(2).map(splitRow);
      let html = '<table><thead><tr>';
      for (const h of head) html += '<th>' + h + '</th>';
      html += '</tr></thead><tbody>';
      for (const row of body) {
        html += '<tr>';
        for (const c of row) html += '<td>' + c + '</td>';
        html += '</tr>';
      }
      html += '</tbody></table>';
      out.push(html);
      tableLines = [];
    };

    for (const rawLine of lines) {
      const line = rawLine;
      const trimmed = line.trim();

      // Table row — buffer until the run breaks. We don't flush mid-
      // table, so a freshly-arriving header + separator + first row
      // emit as one well-formed `<table>`.
      if (trimmed.startsWith('|') && trimmed.endsWith('|') && trimmed.length > 2) {
        flushParagraph();
        closeList();
        tableLines.push(trimmed);
        continue;
      }
      if (tableLines.length > 0) flushTable();

      if (!trimmed) {
        flushParagraph();
        closeList();
        continue;
      }

      if (/^(?:-{3,}|_{3,}|\*{3,})$/.test(trimmed)) {
        flushParagraph();
        closeList();
        out.push('<hr>');
        continue;
      }

      const heading = trimmed.match(/^(#{1,6})\s+(.*)$/);
      if (heading) {
        flushParagraph();
        closeList();
        const level = Math.min(heading[1].length, 6);
        out.push('<h' + level + '>' + applyInlineMd(heading[2]) + '</h' + level + '>');
        continue;
      }

      const olMatch = trimmed.match(/^\d+\.\s+(.*)$/);
      if (olMatch) {
        flushParagraph();
        if (listMode !== 'ol') {
          closeList();
          out.push('<ol>');
          listMode = 'ol';
        }
        out.push('<li>' + applyInlineMd(olMatch[1]) + '</li>');
        continue;
      }

      const ulMatch = trimmed.match(/^[-*+]\s+(.*)$/);
      if (ulMatch) {
        flushParagraph();
        if (listMode !== 'ul') {
          closeList();
          out.push('<ul>');
          listMode = 'ul';
        }
        out.push('<li>' + applyInlineMd(ulMatch[1]) + '</li>');
        continue;
      }

      closeList();
      paragraphLines.push(applyInlineMd(line));
    }

    flushParagraph();
    closeList();
    if (tableLines.length > 0) flushTable();
    flushParagraph();

    return out.join('');
  }

  /* ---------- SSE parser ----------
     The chat send endpoint streams text/event-stream over a fetch
     ReadableStream (EventSource doesn't support POST). One SSE
     message is "event: <name>\ndata: <json>\n\n"; multiple `data:`
     lines on the same message concatenate per the spec. */

  function parseSseMessage(raw) {
    let event = 'message';
    const dataLines = [];
    for (const line of raw.split('\n')) {
      if (line.startsWith('event:')) {
        event = line.slice(6).trim();
      } else if (line.startsWith('data:')) {
        // Spec: strip exactly one leading space if present.
        dataLines.push(line.slice(5).replace(/^ /, ''));
      }
    }
    if (dataLines.length === 0) return null;
    try {
      return { event, data: JSON.parse(dataLines.join('\n')) };
    } catch (e) {
      console.warn('parseSseMessage failed:', e, raw);
      return null;
    }
  }

  /* ---------- AI block helpers ----------
     Reuses the Discovery row/list renderers (renderArtistRow, etc.)
     so AI replies and search results share the same visual contract.
     Adding a chevron, divider or filter to Discovery automatically
     reflects in the chat without a second implementation. */

  function aiBlocksFromMessage(m) {
    if (Array.isArray(m.blocks_data) && m.blocks_data.length) {
      return m.blocks_data;
    }
    // Fallback for messages persisted before blocks_data existed: a
    // flat tracks_data list maps to one tracks block.
    const flat = m.tracks_data || m.tracks;
    if (Array.isArray(flat) && flat.length) {
      return [{ kind: 'tracks', items: flat }];
    }
    return [];
  }

  function renderAiBlock(b) {
    const items = b && b.items;
    if (!items || items.length === 0) return null;
    const wrap = document.createElement('div');
    wrap.className = 'ai-block ai-block-' + (b.kind || 'unknown');
    if (b.kind === 'artist') {
      wrap.innerHTML = renderArtistRow(items);
    } else if (b.kind === 'album') {
      wrap.innerHTML = renderAlbumRow(items);
    } else if (b.kind === 'tracks') {
      wrap.innerHTML = renderTrackList(items);
    } else {
      return null;
    }
    return wrap;
  }

  /* ---------- Router ---------- */

  const routes = {};
  let currentRoute = null;
  let _lastRenderedHash = '';
  // null = unknown / still loading — keep the FAB hidden so we don't
  // flash it on every page load only to immediately retract it once
  // /api/chat/providers comes back empty. 0 = no providers configured;
  // ≥1 = at least one provider works. The FAB shows only on ≥1.
  let _aiProviderCount = null;

  function registerScreen(name, render) {
    routes[name] = render;
  }

  function parseHash() {
    return (location.hash || '').replace(/^#/, '') || 'home';
  }

  function navigate(hash) {
    const target = '#' + hash;
    if (location.hash !== target) {
      location.hash = target;  // hashchange event will trigger render()
    } else {
      // Already on this hash. The shared renderers (Home, Discovery,
      // Friends) appendChild into root, so calling render() here
      // would duplicate the whole screen markup on every re-tap of
      // an active tab. Standard mobile behaviour: scroll back to
      // the top instead.
      window.scrollTo({ top: 0, behavior: 'smooth' });
    }
  }

  function navigateToEntity(kind, id) {
    if (!id) return;
    // Any open overlay sheet would obscure the destination screen, so
    // close them before changing the route. The user's intent on tapping
    // an artist/album tile is to *see* that page, not to keep the
    // overlay covering it.
    if (typeof ai !== 'undefined' && ai && ai.isOpen) ai.hide();
    if (typeof sheet !== 'undefined' && sheet && sheet.isOpen) sheet.hide();
    const tab = currentRoute || 'home';
    navigate(`${tab}/${kind}/${id}`);
  }

  function render() {
    const hash = parseHash();
    const segments = hash.split('/').filter(Boolean);
    const route = segments[0] || 'home';
    currentRoute = route;
    const app = document.getElementById('app');
    if (!app) return;
    // Wipe app only when navigating to a *different* hash. Same-hash
    // re-renders (poll ticks on Library, cancel action) skip the wipe
    // so the renderer can do an atomic innerHTML swap without an
    // intermediate blank flash. Cross-route navigation still wipes
    // because most screen renderers append (not overwrite) their
    // section into root.
    const sameHash = hash === _lastRenderedHash;
    _lastRenderedHash = hash;
    if (!sameHash) app.innerHTML = '';

    // Top-level peer-profile route — #profile/<16-hex-prefix>. Portable
    // URL: same prefix works on any node since it's the friend's public
    // key, not a local SERIAL id.
    if (route === 'profile' && segments[1]) {
      renderProfileOther(app, segments[1]);
      updateNavActive('friends');
      updateFabVisibility('friends');
      window.scrollTo(0, 0);
      return;
    }

    // Nested entity routes — #<tab>/artist/<uuid>, #<tab>/album/<uuid>
    if (segments.length >= 3) {
      const kind = segments[1];
      const id = segments.slice(2).join('/');
      if (kind === 'artist') {
        renderArtist(app, id);
        updateNavActive(route);
        updateFabVisibility(route);
        window.scrollTo(0, 0);
        return;
      }
      if (kind === 'album') {
        renderAlbum(app, id);
        updateNavActive(route);
        updateFabVisibility(route);
        window.scrollTo(0, 0);
        return;
      }
      if (kind === 'chat') {
        renderChatThread(app, id);
        updateNavActive(route);
        updateFabVisibility(route);
        window.scrollTo(0, 0);
        return;
      }
      if (kind === 'genre') {
        renderGenre(app, id);
        updateNavActive(route);
        updateFabVisibility(route);
        window.scrollTo(0, 0);
        return;
      }
    }

    const renderer = routes[route] || routes.home;
    if (renderer) renderer(app, hash);
    updateNavActive(route);
    updateFabVisibility(route);
    window.scrollTo(0, 0);
  }

  function updateNavActive(route) {
    document.querySelectorAll('.nav-tab').forEach(btn => {
      const r = btn.getAttribute('data-route');
      if (r === route) btn.setAttribute('aria-current', 'page');
      else btn.removeAttribute('aria-current');
    });
  }

  /* ---------- FAB visibility ---------- */

  // Routes where the AI FAB is suppressed even when no overlay is
  // open. Friends is conceptually about humans you talk to, the
  // AI assistant has nothing to add there and the floating button
  // just clutters the chat-icon column.
  const FAB_HIDDEN_ROUTES = new Set(['friends']);

  function updateFabVisibility(route) {
    const fab = document.getElementById('aiFab');
    if (!fab) return;
    const npOpen = sheet && sheet.isOpen;
    const aiOpen = ai && ai.isOpen;
    const drawerOpen = typeof moreDrawer !== 'undefined' && moreDrawer.isOpen;
    const r = route || currentRoute;
    const segs = parseHash().split('/').filter(Boolean);
    const inChat = segs.length >= 3 && segs[1] === 'chat';
    const aiNotReady = !(_aiProviderCount > 0);
    fab.hidden = !!(npOpen || aiOpen || drawerOpen
                    || FAB_HIDDEN_ROUTES.has(r) || inChat || aiNotReady);
  }

  async function refreshAiAvailability() {
    try {
      const r = await fetch('/api/chat/providers');
      if (r.ok) {
        const data = await r.json();
        _aiProviderCount = Array.isArray(data) ? data.length : 0;
      } else {
        _aiProviderCount = 0;
      }
    } catch (_) {
      _aiProviderCount = 0;
    }
    updateFabVisibility(currentRoute);
  }

  /* ---------- Now Playing sheet ----------
     Markup mirrors docs/design/reference/claude-design-bundle/project/
     Now Playing v4.html. Class names and DOM IDs map 1:1 to the
     reference's structure. Energy uses energy_db (dB scale) for a
     calibrated 5-dot mapping.
  */

  function qualityBadgeHTML(quality) {
    return quality === 'hi-res' ? 'Hi-Res'
      : quality === 'lossless' ? 'Lossless'
      : 'Lossy';
  }

  function energyLevelFromDb(db) {
    if (db == null) return 0;
    // Map -35dB..-5dB to 1..5 dots. Below -35 → 1, above -5 → 5.
    const lvl = Math.round((db + 35) / 6);
    return Math.max(1, Math.min(5, lvl));
  }

  function energyLevelFromRaw(rms) {
    if (rms == null) return 0;
    // Fallback when energy_db is missing. Approx log scale.
    const db = 20 * Math.log10(Math.max(rms, 1e-5));
    return energyLevelFromDb(db);
  }

  const sheet = {
    el: null,
    coverImg: null, coverFallback: null,
    title: null, artist: null, albumText: null, year: null,
    qBadge: null, keyPill: null, bpm: null, bpmNum: null,
    energy: null, energyDots: null,
    progressFill: null, progressHead: null,
    timeCurrent: null, timeTotal: null, radioBtn: null,
    playPause: null, playPauseIcon: null,
    prev: null, next: null, close: null, lyricsBtn: null,
    similar: null, similarList: null, similarCount: null,
    isOpen: false,
    lastTrackKey: null,
    lastDetailFetchedKey: null,
    inflightKey: null,
    lastDetail: null,

    init() {
      this.el = document.getElementById('npSheet');
      if (!this.el) return;
      this.coverImg = document.getElementById('npCoverImg');
      this.coverFallback = document.getElementById('npCoverFallback');
      this.title = document.getElementById('npTitleLine');
      this.artist = document.getElementById('npArtistLine');
      this.albumText = document.getElementById('npAlbumText');
      this.year = document.getElementById('npYearText');
      this.qBadge = document.getElementById('npQBadge');
      this.keyPill = document.getElementById('npKeyPill');
      this.bpm = document.getElementById('npBpm');
      this.bpmNum = document.getElementById('npBpmNum');
      this.energy = document.getElementById('npEnergy');
      this.energyDots = document.getElementById('npEnergyDots');
      this.progressFill = document.getElementById('npProgressFill');
      this.progressHead = document.getElementById('npProgressHead');
      this.timeCurrent = document.getElementById('npTimeCurrent');
      this.timeTotal = document.getElementById('npTimeTotal');
      this.radioBtn = document.getElementById('npRadioBtn');
      this.queueBtn = document.getElementById('npQueueBtn');
      this.playPause = document.getElementById('npPlayPauseBtn');
      this.playPauseIcon = document.getElementById('npPlayPauseIcon');
      this.prev = document.getElementById('npPrev');
      this.next = document.getElementById('npNextBtn');
      this.close = document.getElementById('npClose');
      this.lyricsBtn = document.getElementById('npLyricsBtn');
      this.similar = document.getElementById('npSimilar');
      this.similarList = document.getElementById('npSimilarList');
      this.similarCount = document.getElementById('npSimilarCount');

      this.close.addEventListener('click', () => this.hide());
      this.playPause.addEventListener('click', e => {
        e.stopPropagation();
        if (typeof window.togglePlayPause === 'function') window.togglePlayPause();
      });
      this.prev.addEventListener('click', e => {
        e.stopPropagation();
        if (typeof window.playerCmd === 'function') window.playerCmd('previous');
      });
      this.next.addEventListener('click', e => {
        e.stopPropagation();
        if (typeof window.playerCmd === 'function') window.playerCmd('next');
      });
      if (this.queueBtn) {
        this.queueBtn.addEventListener('click', e => {
          e.stopPropagation();
          queue.show();
        });
      }
      if (this.radioBtn) {
        this.radioBtn.addEventListener('click', async e => {
          e.stopPropagation();
          const isOn = this.radioBtn.getAttribute('data-state') === 'on';
          if (isOn) {
            try {
              await fetch('/api/player/radio/stop', { method: 'POST' });
            } catch (err) { console.warn('radio stop failed', err); }
            return;
          }
          // Off → On. Radio replaces the queue, so warn the user if
          // there's more than just the current track to lose.
          const seedId = (this.lastDetail && this.lastDetail.media_file_id)
                       || (this._npMfId || null);
          if (!seedId) return;
          const playlistLen = (window.currentPlaylist || []).length;
          if (playlistLen > 1) {
            const ok = await window.confirmDestructive({
              title: 'Start radio?',
              message: 'Your current queue will be replaced with this track plus similar ones.',
              confirmText: 'Start radio',
              cancelText: 'Cancel',
            });
            if (!ok) return;
          }
          try {
            await fetch('/api/player/radio/start', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ track_id: seedId }),
            });
          } catch (err) { console.warn('radio start failed', err); }
        });
      }
      // Tap artist / album text → open the corresponding detail screen.
      // IDs come from now-playing-detail (lastDetail.primary_artist.id,
      // lastDetail.album_id). Sheet is hidden after navigation so the
      // user lands on the destination cleanly.
      if (this.artist) {
        this.artist.addEventListener('click', e => {
          e.stopPropagation();
          const id = this.lastDetail
            && this.lastDetail.primary_artist
            && this.lastDetail.primary_artist.id;
          if (id) {
            this.hide();
            navigateToEntity('artist', id);
          }
        });
      }
      const albumLine = this.albumText && this.albumText.parentElement;
      if (albumLine) {
        albumLine.addEventListener('click', e => {
          e.stopPropagation();
          const id = this.lastDetail && this.lastDetail.album_id;
          if (id) {
            this.hide();
            navigateToEntity('album', id);
          }
        });
      }
      // Similar tracks: delegated listener — rows are re-rendered on
      // every track change. Tap on `+` opens the same inline
      // "Add to: [Next] [End]" confirm bar used on detail screens;
      // clicks inside the bar are handled by its own per-button
      // listeners attached by openQueueConfirm. Tap anywhere else
      // on the row plays the track.
      if (this.similarList) {
        this.similarList.addEventListener('click', (e) => {
          const row = e.target.closest('.np-sim-row');
          if (!row) return;
          if (e.target.closest('.track-confirm-bar')) return;
          if (e.target.closest('.np-sim-add')) {
            if (row.classList.contains('is-confirming')) {
              closeQueueConfirm(row);
            } else {
              openQueueConfirm(row);
            }
            return;
          }
          if (row.classList.contains('is-confirming')) return;
          const mfId = row.getAttribute('data-track-id');
          if (!mfId) return;
          if (typeof window.playTrack === 'function') {
            window.playTrack(parseInt(mfId, 10));
          }
        });
      }
      document.addEventListener('keydown', e => {
        if (e.key === 'Escape' && this.isOpen) this.hide();
      });
    },

    show() {
      if (!this.el) return;
      this.el.hidden = false;
      this.isOpen = true;
      if (this.lastDetail) this.renderDetail(this.lastDetail);
      const fab = document.getElementById('aiFab');
      if (fab) fab.hidden = true;
    },

    hide() {
      if (!this.el) return;
      this.el.hidden = true;
      this.isOpen = false;
      updateFabVisibility(currentRoute);
    },

    onStatus(data) {
      if (!data) return;

      const pct = data.progress_percent || 0;
      if (this.progressFill) {
        this.progressFill.style.width = pct + '%';
        // .progress-fill border-radius right corners flatten when not 100%
        this.progressFill.style.borderRadius = pct >= 99
          ? 'calc(4 * var(--px))'
          : 'calc(4 * var(--px)) 0 0 calc(4 * var(--px))';
      }
      if (this.progressHead) {
        this.progressHead.style.left = pct + '%';
      }
      if (this.timeCurrent) this.timeCurrent.textContent = data.position_formatted || '0:00';
      if (this.timeTotal) this.timeTotal.textContent = data.length_formatted || '0:00';
      if (this.playPauseIcon) {
        const playing = data.state === 'playing';
        this.playPauseIcon.setAttribute('d',
          playing
            ? 'M9 5h4v20H9V5zm8 0h4v20h-4V5z'   // pause bars
            : 'M9 5v20l16-10z');                 // play triangle
      }
      // Radio toggle visual reflects backend flag every tick. Stashed
      // mfId so the radio start handler can use the current seed
      // without waiting on a detail fetch.
      if (this.radioBtn) {
        this.radioBtn.setAttribute(
          'data-state', data.radio_mode ? 'on' : 'off',
        );
      }
      this._npMfId = data.media_file_id || this._npMfId;

      if (!data.song) return;

      const trackKey = (data.song || '') + '|' + (data.artist || '');
      if (trackKey !== this.lastTrackKey) {
        // Track changed — reset ALL visible state and require fresh detail.
        // Anything that doesn't get re-set by a subsequent renderDetail
        // would otherwise keep showing the previous track's values.
        this.lastTrackKey = trackKey;
        this.lastDetailFetchedKey = null;
        this.lastDetail = null;
        if (this.title) this.title.textContent = data.song || '—';
        if (this.artist) this.artist.textContent = data.artist || '';
        if (this.albumText) this.albumText.textContent = data.album || '';
        if (this.year) this.year.textContent = '';
        if (this.coverImg) {
          this.coverImg.hidden = true;
          this.coverImg.removeAttribute('src');
          // Cover URL is resolvable from the SSE payload alone — paint
          // the big cover immediately so opening the sheet during a
          // detail fetch doesn't flash a blank placeholder.
          const eagerUrl = coverUrl({
            cover_id: data.cover_id,
            media_file_id: data.media_file_id,
          });
          if (eagerUrl) {
            this.coverImg.src = eagerUrl;
            this.coverImg.onerror = () => { this.coverImg.hidden = true; };
            this.coverImg.hidden = false;
          }
        }
        if (this.coverFallback) {
          const c = coverPlaceholderColors(data.song || data.album || '');
          this.coverFallback.style.setProperty('--cover-bg-1', c.bg1);
          this.coverFallback.style.setProperty('--cover-bg-2', c.bg2);
        }
        // Hide all detail-derived feature elements so prior track values
        // never linger if the next renderDetail is delayed or fails.
        if (this.qBadge) {
          this.qBadge.className = 'np-q-badge';
          this.qBadge.innerHTML = '';
        }
        if (this.keyPill) { this.keyPill.hidden = true; this.keyPill.innerHTML = ''; }
        if (this.bpm) { this.bpm.hidden = true; }
        if (this.bpmNum) this.bpmNum.textContent = '';
        if (this.energy) { this.energy.hidden = true; }
        if (this.energyDots) this.energyDots.innerHTML = '';
        if (this.similar) { this.similar.hidden = true; }
        if (this.similarList) this.similarList.innerHTML = '';
      }

      // The SSE payload now carries media_file_id (resolved server-side
      // against the playlist cache), so detail fetch can proceed on the
      // first np-update for a track — no race with playlist-loaded.
      if (this.lastDetailFetchedKey !== trackKey && data.media_file_id) {
        this.tryFetchDetail(trackKey, data.media_file_id);
      }
    },

    async tryFetchDetail(trackKey, mfId) {
      if (this.inflightKey === trackKey) return; // already fetching
      if (!mfId) return;  // playlist not yet loaded — retry on next status
      this.inflightKey = trackKey;
      try {
        const resp = await fetch('/api/player/now-playing-detail?media_file_id=' + mfId);
        if (!resp.ok) return;
        const detail = await resp.json();
        // If track changed during fetch, drop this stale result.
        if (this.lastTrackKey !== trackKey) return;
        this.lastDetail = detail;
        this.lastDetailFetchedKey = trackKey;
        this.renderDetail(detail);
        this.fetchSimilar(mfId);
        // Share detail with other surfaces (mini-player needs cover_id
        // which is not in the SSE status payload).
        document.dispatchEvent(new CustomEvent('np-detail', { detail }));
      } catch (err) {
        console.warn('now-playing-detail failed:', err);
      } finally {
        if (this.inflightKey === trackKey) this.inflightKey = null;
      }
    },

    async fetchSimilar(mfId) {
      try {
        const params = new URLSearchParams({ track_id: String(mfId), limit: '7' });
        const resp = await fetch('/search/similar?' + params, { method: 'POST' });
        if (!resp.ok) return;
        const data = await resp.json();
        // /search/similar returns `results` (the legacy contract); keep
        // the `tracks` fallback in case the endpoint shape evolves.
        this.renderSimilar(data.results || data.tracks || []);
      } catch (err) {
        console.warn('similar fetch failed:', err);
      }
    },

    renderDetail(d) {
      if (!d) return;

      // Cover — prefer resolved cover_id; fall back to lazy by-media URL.
      const url = coverUrl(d);
      if (url && this.coverImg) {
        this.coverImg.src = url;
        this.coverImg.onerror = () => { this.coverImg.hidden = true; };
        this.coverImg.hidden = false;
      } else if (this.coverImg) {
        this.coverImg.hidden = true;
      }
      if (this.coverFallback) {
        const c = coverPlaceholderColors(d.title || d.album_title || '');
        this.coverFallback.style.setProperty('--cover-bg-1', c.bg1);
        this.coverFallback.style.setProperty('--cover-bg-2', c.bg2);
      }

      if (this.title) this.title.textContent = d.title || '—';
      if (this.artist) {
        this.artist.textContent = d.primary_artist ? d.primary_artist.name : '';
      }
      if (this.albumText) this.albumText.textContent = d.album_title || '';
      if (this.year) this.year.textContent = d.year ? '· ' + d.year : '';

      // Quality badge
      if (this.qBadge) {
        this.qBadge.className = 'np-q-badge';
        const qual = d.quality || 'lossy';
        const cls = qual === 'hi-res' ? 'is-hires'
          : qual === 'lossless' ? 'is-lossless'
          : 'is-lossy';
        this.qBadge.classList.add(cls);
        this.qBadge.innerHTML = qualityBadgeHTML(qual);
      }

      // Key pill: "F min" / "C maj"
      if (this.keyPill) {
        if (d.key) {
          const modeShort = d.mode === 'minor' ? 'min'
            : d.mode === 'major' ? 'maj' : '';
          this.keyPill.innerHTML = escapeHtml(d.key)
            + (modeShort ? ' <span class="np-key-mode">' + modeShort + '</span>' : '');
          this.keyPill.hidden = false;
        } else {
          this.keyPill.hidden = true;
        }
      }

      // BPM
      if (this.bpm) {
        if (d.bpm) {
          if (this.bpmNum) this.bpmNum.textContent = String(Math.round(d.bpm));
          this.bpm.hidden = false;
        } else {
          this.bpm.hidden = true;
        }
      }

      // Energy dots — prefer energy_db (dB), fallback to raw RMS
      if (this.energy && this.energyDots) {
        const lvl = (d.energy_db != null)
          ? energyLevelFromDb(Number(d.energy_db))
          : energyLevelFromRaw(Number(d.energy));
        if (lvl > 0) {
          let dots = '';
          for (let i = 0; i < 5; i++) {
            dots += `<span class="np-energy-dot${i < lvl ? ' on' : ''}"></span>`;
          }
          this.energyDots.innerHTML = dots;
          this.energy.hidden = false;
        } else {
          this.energy.hidden = true;
        }
      }
    },

    renderSimilar(tracks) {
      if (!this.similar || !this.similarList || !this.similarCount) return;
      if (!tracks || tracks.length === 0) {
        this.similar.hidden = true;
        return;
      }
      this.similarCount.textContent = String(tracks.length);
      this.similarList.innerHTML = tracks.map(t => {
        // /search/similar returns mf_rep.id as t.id — that's media_file_id.
        const url = coverUrl({cover_id: t.cover_id, media_file_id: t.id});
        const cover = url
          ? `<img src="${url}" alt="" loading="lazy" onerror="this.style.display='none'">`
          : `<div class="np-sim-art-fallback"></div>`;
        const score = (t.similarity != null)
          ? Number(t.similarity).toFixed(2)
          : '';
        const yearStr = t.year ? ' · ' + t.year : '';
        return `
          <div class="np-sim-row" data-track-id="${escapeHtml(String(t.id || ''))}">
            <div class="np-sim-art">${cover}</div>
            <div class="np-sim-info">
              <div class="np-sim-info-row">
                <div class="np-sim-info-left">
                  <div class="np-sim-track">${escapeHtml(t.title || t.song || '')}</div>
                  <div class="np-sim-artist">${escapeHtml((t.artist || '') + yearStr)}</div>
                </div>
                <span class="np-sim-score">${score}</span>
              </div>
            </div>
            <button class="np-sim-add" type="button" aria-label="Add to queue">
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none"
                   stroke="currentColor" stroke-width="1.7" stroke-linecap="round" aria-hidden="true">
                <path d="M12 5v14M5 12h14"/>
              </svg>
            </button>
          </div>`;
      }).join('');
      this.similar.hidden = false;
    },
  };

  /* ---------- Mini-player adapter ---------- */

  const mp = {
    el: null, cover: null, title: null, artist: null,
    playPauseIcon: null, playPause: null, next: null,
    lastSongKey: null,

    init() {
      this.el = document.getElementById('miniPlayer');
      this.cover = document.getElementById('mpCover');
      this.title = document.getElementById('mpTitle');
      this.artist = document.getElementById('mpArtist');
      this.playPause = document.getElementById('mpPlayPause');
      this.playPauseIcon = document.getElementById('mpPlayPauseIcon');
      this.next = document.getElementById('mpNext');

      if (!this.el) return;

      this.playPause.addEventListener('click', e => {
        e.stopPropagation();
        if (typeof window.togglePlayPause === 'function') {
          window.togglePlayPause();
        }
      });
      this.next.addEventListener('click', e => {
        e.stopPropagation();
        if (typeof window.playerCmd === 'function') {
          window.playerCmd('next');
        }
      });
      this.el.addEventListener('click', e => {
        // Tap on the bar (excluding inner buttons) → expand sheet.
        if (e.target.closest('.mp-action')) return;
        sheet.show();
      });
    },

    update(data) {
      if (!this.el) return;
      const playing = data && data.state === 'playing';
      const paused = data && data.state === 'paused';
      const stopped = data && data.state === 'stopped';
      const hasTrack = !!(data && data.song);
      const visible = hasTrack && (playing || paused || stopped);

      if (!visible) {
        this.el.hidden = true;
        document.body.classList.remove('has-miniplayer');
        return;
      }

      this.el.hidden = false;
      document.body.classList.add('has-miniplayer');

      this.title.textContent = data.song || '—';
      const artist = data.artist || '';
      const album = data.album || '';
      this.artist.textContent = album ? `${artist} · ${album}` : artist;

      if (this.playPauseIcon) {
        this.playPauseIcon.setAttribute('d',
          playing
            ? 'M6 5h4v14H6V5zm8 0h4v14h-4V5z'   // pause bars
            : 'M8 5v14l11-7z');                  // play triangle
      }

      // Reset cover to gradient placeholder only when the track changes,
      // then immediately try the cover_id from the SSE payload itself —
      // it's served by the playlist cache so we don't have to wait for
      // /now-playing-detail to settle. The np-detail listener still
      // runs and re-applies the URL, but in the common case the image
      // is already on screen by then. CSS declares background-size:cover
      // + background-position:center on .mp-cover, so the gradient and
      // the image both render correctly without per-call overrides.
      const songKey = (data.song || '') + '|' + (data.album || '');
      if (songKey !== this.lastSongKey) {
        this.lastSongKey = songKey;
        const c = coverPlaceholderColors(data.song || data.album || '');
        this.cover.style.backgroundImage =
          `linear-gradient(135deg, ${c.bg1}, ${c.bg2})`;
        if (data.cover_id || data.media_file_id) {
          this.setCover({
            cover_id: data.cover_id,
            media_file_id: data.media_file_id,
          });
        }
      }
    },

    setCover(detail) {
      if (!this.cover) return;
      const url = coverUrl(detail);
      if (!url) return;
      // mp-cover is a <div> with background-image. Preloading via Image()
      // lets us silently keep the gradient placeholder when the URL 404s
      // (sentinel cover) instead of replacing it with a broken image.
      const probe = new Image();
      probe.onload = () => { this.cover.style.backgroundImage = `url(${url})`; };
      probe.src = url;
    },
  };

  /* ---------- Queue sheet ----------
     Full-screen overlay that lists the current HQPlayer playlist.
     Opened by the queue button on Now Playing's transport row;
     stacks above the Now Playing sheet (z-index: 110 > 100).
     Reference: docs/design/reference/claude-design-bundle/project/
     Session 2 v3.html — Queue section. */

  const queue = {
    el: null, list: null, summary: null, empty: null, closeBtn: null,
    isOpen: false,
    // Queue keeps its own cache because `currentPlaylist` and
    // `_latest_status_cache` are module-level `let` bindings in
    // app.js — they are NOT on `window`, so we can't read them from
    // here. We mirror them via events: `playlist-loaded.detail.tracks`
    // and `np-update.detail.track_index`. Show() also refetches the
    // playlist directly to guarantee fresh data even if no event
    // has fired yet in this session.
    tracks: [],
    trackIndex: 0,

    init() {
      this.el = document.getElementById('queueSheet');
      if (!this.el) return;
      this.list = document.getElementById('queueList');
      this.summary = document.getElementById('queueSummary');
      this.empty = document.getElementById('queueEmpty');
      this.closeBtn = document.getElementById('queueCloseBtn');

      this.closeBtn.addEventListener('click', () => this.hide());
      document.addEventListener('keydown', e => {
        if (e.key === 'Escape' && this.isOpen) this.hide();
      });

      // `np-update` carries track_index, position, length on every
      // status tick (~1s). Re-render only when the index changes;
      // otherwise just update the current row's countdown so we
      // don't repaint the whole list every second.
      document.addEventListener('np-update', e => {
        const d = e.detail || {};
        const idx = d.track_index || 0;
        this._npPosition = d.position;
        this._npLength = d.length;
        const stateChanged = this._npState !== d.state;
        this._npState = d.state;
        if (idx !== this.trackIndex) {
          this.trackIndex = idx;
          if (this.isOpen) this.render();
        } else if (this.isOpen) {
          this.updateCurrentRemaining();
          this.renderSummary();
          if (stateChanged) this.updateGlyphState();
        }
      });
      // Mirror the playlist whenever app.js refetches it.
      document.addEventListener('playlist-loaded', e => {
        this.tracks = (e.detail && e.detail.tracks) || [];
        if (this.isOpen) this.render();
      });
    },

    async show() {
      if (!this.el) return;
      this.el.hidden = false;
      this.isOpen = true;
      // Direct fetch for two reasons: (1) the user may open the
      // sheet before any SSE / playlist-loaded event has fired in
      // this tab session, and (2) reflects any queue mutation not
      // yet seen by the SSE poller. Fast — backend serves a cached
      // playlist payload with no HQPlayer round-trip.
      try {
        const resp = await fetch('/api/player/playlist');
        if (resp.ok) {
          const data = await resp.json();
          this.tracks = data.tracks || [];
        }
      } catch (err) {
        console.warn('queue load failed', err);
      }
      this.render();
    },

    hide() {
      if (!this.el) return;
      this.el.hidden = true;
      this.isOpen = false;
    },

    render() {
      const tracks = this.tracks;
      const currentIdx = this.trackIndex;            // 1-based

      if (tracks.length === 0) {
        this.list.innerHTML = '';
        this.summary.innerHTML = '';
        this.empty.hidden = false;
        return;
      }
      this.empty.hidden = true;

      this.renderSummary();

      const dragHandleSvg = `
        <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"
             aria-hidden="true">
          <circle cx="9" cy="6" r="1.3"/><circle cx="15" cy="6" r="1.3"/>
          <circle cx="9" cy="12" r="1.3"/><circle cx="15" cy="12" r="1.3"/>
          <circle cx="9" cy="18" r="1.3"/><circle cx="15" cy="18" r="1.3"/>
        </svg>`;
      const removeSvg = `
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none"
             stroke="currentColor" stroke-width="1.8" stroke-linecap="round"
             aria-hidden="true">
          <circle cx="12" cy="12" r="9"/>
          <path d="M8 8l8 8M16 8l-8 8"/>
        </svg>`;

      this.list.innerHTML = tracks.map((t, idx) => {
        const oneIdx = idx + 1;          // HQPlayer is 1-based
        const isCurrent = oneIdx === currentIdx;
        // Every track is draggable, including the current one —
        // moving the current row is equivalent to "shift before-
        // tracks across to after-current". Backend rebuilds and
        // re-anchors playback to the current track's new slot.
        const isLocked = false;
        const c = coverPlaceholderColors(t.title || t.album || '');
        const url = coverUrl({cover_id: t.cover_id, media_file_id: t.id});
        const cover = url
          ? `<img src="${url}" alt="" loading="lazy" onerror="this.style.display='none'">`
          : '';
        // Non-current rows show full duration; the current row gets
        // a countdown ("-2:14") populated after innerHTML by
        // updateCurrentRemaining(). Empty fallback for tracks with
        // unknown duration (non-DB URIs).
        const dur = t.duration_seconds
          ? fmtDuration(t.duration_seconds) : '';
        const right = `<span class="q-dur">${
          isCurrent ? '' : escapeHtml(dur)
        }</span>`;
        // The current row replaces its remove-slot with the playing
        // glyph so duration stays aligned with non-current rows and
        // the now-playing indicator sits in the action column.
        const glyphCls = this._npState === 'playing'
          ? 'q-playing-glyph' : 'q-playing-glyph is-paused';
        const removeBtn = isCurrent
          ? `<span class="q-now-marker" aria-label="Now playing">
               <span class="${glyphCls}">
                 <span></span><span></span><span></span>
               </span>
             </span>`
          : `<button class="q-remove" type="button" aria-label="Remove"
                  data-action="remove" data-index="${oneIdx}">
            ${removeSvg}
          </button>`;
        return `
          <div class="q-row${isCurrent ? ' is-current' : ''}"
               data-index="${oneIdx}" data-mfid="${t.id || ''}">
            <span class="q-drag" ${isLocked ? '' : 'data-drag="1"'}>
              ${dragHandleSvg}
            </span>
            <div class="q-cover" style="--cover-bg-1: ${c.bg1}; --cover-bg-2: ${c.bg2};">${cover}</div>
            <div class="q-info">
              <div class="q-title">${escapeHtml(t.title || '')}</div>
              <div class="q-artist">${escapeHtml(t.artist || '')}</div>
            </div>
            ${right}
            ${removeBtn}
          </div>`;
      }).join('');

      // Tap row body → jump (via row click handler — drag handle and
      // remove button stop propagation to prevent accidental jumps).
      // Tap semantics: short tap on current = play/pause toggle; on
      // any other row = jump to that track. Long press (≥500 ms) on
      // any row = play it from the beginning. Drag-handle and
      // remove-button events are filtered out by the closest()
      // check so they keep their own behaviour.
      this.list.querySelectorAll('.q-row').forEach(row => {
        let pressTimer = null;
        let longFired = false;
        const cancelPress = () => {
          if (pressTimer) { clearTimeout(pressTimer); pressTimer = null; }
        };
        row.addEventListener('pointerdown', e => {
          if (e.target.closest('[data-drag], [data-action]')) return;
          longFired = false;
          cancelPress();
          pressTimer = setTimeout(() => {
            longFired = true;
            const idx = parseInt(row.dataset.index, 10);
            if (idx) this.jumpTo(idx);
          }, 500);
        });
        row.addEventListener('pointermove', cancelPress);
        row.addEventListener('pointerleave', cancelPress);
        row.addEventListener('pointercancel', cancelPress);
        row.addEventListener('pointerup', e => {
          if (e.target.closest('[data-drag], [data-action]')) return;
          cancelPress();
          if (longFired) return;
          const idx = parseInt(row.dataset.index, 10);
          if (!idx) return;
          if (idx === this.trackIndex) this.togglePlayPause();
          else this.jumpTo(idx);
        });
        row.addEventListener('contextmenu', e => e.preventDefault());
      });
      this.list.querySelectorAll('[data-action="remove"]').forEach(btn => {
        btn.addEventListener('click', e => {
          e.stopPropagation();
          const idx = parseInt(btn.dataset.index, 10);
          if (idx) this.removeAt(idx);
        });
      });
      // Drag handles → reorder. See attachDrag for the gesture
      // contract; only non-locked rows carry [data-drag].
      this.list.querySelectorAll('[data-drag="1"]').forEach(handle => {
        this.attachDrag(handle);
      });
      this.updateCurrentRemaining();
    },

    updateCurrentRemaining() {
      // Live countdown for the playing row, refreshed by np-update
      // (every ~1s) without a full re-render.
      if (!this.list) return;
      const el = this.list.querySelector('.q-row.is-current .q-dur');
      if (!el) return;
      const pos = Number(this._npPosition) || 0;
      const len = Number(this._npLength) || 0;
      if (len <= 0) { el.textContent = ''; return; }
      const remaining = Math.max(0, Math.round(len - pos));
      el.textContent = fmtDuration(remaining);
    },

    updateGlyphState() {
      // Hide the equaliser bars when audio is paused / stopped so the
      // marker matches the actual playback state without redrawing
      // the row. The .q-now-marker slot stays the same width, so the
      // duration column doesn't shift.
      if (!this.list) return;
      const g = this.list.querySelector('.q-row.is-current .q-playing-glyph');
      if (!g) return;
      g.classList.toggle('is-paused', this._npState !== 'playing');
    },

    async togglePlayPause() {
      const url = this._npState === 'playing'
        ? '/api/player/pause' : '/api/player/play';
      try {
        await fetch(url, { method: 'POST' });
      } catch (err) { console.warn('toggle play/pause failed', err); }
    },

    renderSummary() {
      // Time field is a queue-wide countdown: remaining seconds of
      // the current track plus full duration of every track after
      // it. Falls back to total of the whole list when nothing is
      // playing. Tracks count and album count don't tick — only
      // the time string changes between SSE pulses.
      if (!this.summary) return;
      const tracks = this.tracks;
      if (!tracks || tracks.length === 0) {
        this.summary.innerHTML = '';
        return;
      }
      let remaining;
      if (this.trackIndex >= 1 && this.trackIndex <= tracks.length) {
        const cz = this.trackIndex - 1;
        const curLen = Number(this._npLength) ||
          Number(tracks[cz] && tracks[cz].duration_seconds) || 0;
        const curPos = Number(this._npPosition) || 0;
        remaining = Math.max(0, curLen - curPos);
        for (let i = cz + 1; i < tracks.length; i++) {
          remaining += Number(tracks[i].duration_seconds) || 0;
        }
      } else {
        remaining = tracks.reduce(
          (s, t) => s + (Number(t.duration_seconds) || 0), 0);
      }
      const albumCount = new Set(
        tracks.map(t => t.album).filter(Boolean)).size;
      const parts = [
        `${tracks.length} tracks`,
        formatDurationSummary(remaining),
        albumCount > 0
          ? `${albumCount} ${albumCount === 1 ? 'album' : 'albums'}`
          : null,
      ].filter(Boolean);
      this.summary.innerHTML = parts
        .map((p, i) => i === 0
          ? `<span>${escapeHtml(p)}</span>`
          : `<span class="qs-sep">·</span><span>${escapeHtml(p)}</span>`)
        .join('');
    },

    async jumpTo(index) {
      try {
        await fetch('/api/player/jump', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({index}),
        });
        // The queue sheet stays open — user typically wants to keep
        // browsing. Status SSE will repaint the active row.
      } catch (err) {
        console.warn('jump failed', err);
      }
    },

    // Pointer-events drag — works on desktop + mobile.
    //
    // Gesture: pointerdown on a handle captures the pointer to that
    // handle (so subsequent moves keep firing on it even when the
    // pointer leaves the original element); pointermove offsets the
    // row via `top: <deltaY>px` (the row already has position:
    // relative, no transform-collapse worries); pointerup figures
    // out which row the dragged row's mid-point overlaps and asks
    // the backend to reorder. The current row never becomes a drop
    // target, and a drag never crosses the current row — backend
    // would reject either case and the visual hint would be wrong.
    attachDrag(handle) {
      const row = handle.closest('.q-row');
      if (!row) return;
      // Read anchor lazily on each pointerdown (not at attach time)
      // so a drag started after np-update fires sees the latest
      // current track index. attach time can predate the first SSE.
      handle.addEventListener('pointerdown', e => {
        if (e.button !== undefined && e.button !== 0) return;
        e.preventDefault();
        try { handle.setPointerCapture(e.pointerId); } catch (_) {}

        const anchor = this.trackIndex;
        const startY = e.clientY;
        const originOneIdx = parseInt(row.dataset.index, 10);
        const originIdx = originOneIdx - 1;
        // Origin segment relative to the currently playing track.
        // The drop-target filter below mirrors backend's seamless-
        // reorder conditions so the UI never offers a target that
        // would interrupt audio — drag visibly snaps back instead.
        //   origin = current → only drops in `before` allowed
        //                      (current shifts left via removes).
        //   origin = before  → only drops in `after`  allowed
        //                      (boundary-cross out of before).
        //   origin = after   → only drops in `after`  allowed
        //                      (pure tail reorder).
        const originSeg = anchor < 1 ? 'after'
          : originOneIdx < anchor ? 'before'
          : originOneIdx > anchor ? 'after'
          : 'current';
        let dragging = false;

        let hoverIdx = -1;       // 0-based of row under pointer (or -1)
        let hoverSide = 'above'; // 'above' | 'below' midpoint of hoverIdx

        const onMove = ev => {
          const dy = ev.clientY - startY;
          if (!dragging) {
            if (Math.abs(dy) < 4) return;
            dragging = true;
            row.classList.add('is-dragging');
          }
          row.style.top = dy + 'px';

          const candidates = this.list.querySelectorAll('.q-row');
          candidates.forEach(r => {
            r.classList.remove('is-drop-above', 'is-drop-below');
          });
          hoverIdx = -1;
          for (const r of candidates) {
            if (r === row) continue;
            const rIdx = parseInt(r.dataset.index, 10);
            const rSeg = anchor < 1 ? 'after'
              : rIdx < anchor ? 'before'
              : rIdx > anchor ? 'after'
              : 'current';
            const allowed =
              (originSeg === 'current' && rSeg === 'before') ||
              (originSeg === 'before'  && rSeg === 'after')  ||
              (originSeg === 'after'   && rSeg === 'after');
            if (!allowed) continue;
            const rect = r.getBoundingClientRect();
            if (ev.clientY >= rect.top && ev.clientY <= rect.bottom) {
              const mid = (rect.top + rect.bottom) / 2;
              hoverIdx = rIdx - 1;
              hoverSide = ev.clientY < mid ? 'above' : 'below';
              r.classList.add(
                hoverSide === 'above' ? 'is-drop-above' : 'is-drop-below');
              break;
            }
          }
        };

        const onUp = () => {
          handle.removeEventListener('pointermove', onMove);
          handle.removeEventListener('pointerup', onUp);
          handle.removeEventListener('pointercancel', onUp);
          try { handle.releasePointerCapture(e.pointerId); } catch (_) {}
          row.style.top = '';
          row.classList.remove('is-dragging');
          this.list.querySelectorAll('.is-drop-above, .is-drop-below')
            .forEach(r => r.classList.remove('is-drop-above', 'is-drop-below'));
          if (!dragging || hoverIdx < 0) return;
          // Insert-at-position: drop position relative to hoverIdx.
          // Above midpoint → insert before hoverIdx; below → after.
          // After removal, indices > originIdx shift down by 1.
          let insertIdx = hoverIdx + (hoverSide === 'below' ? 1 : 0);
          if (insertIdx > originIdx) insertIdx -= 1;
          if (insertIdx === originIdx) return;
          this.commitReorder(originIdx, insertIdx);
        };

        handle.addEventListener('pointermove', onMove);
        handle.addEventListener('pointerup', onUp);
        handle.addEventListener('pointercancel', onUp);
      });
    },

    async commitReorder(fromIdx, insertIdx) {
      // Plain splice: remove at fromIdx, insert at insertIdx (which
      // attachDrag already adjusted for the post-removal shift).
      // The current track may shift to a new slot — backend looks
      // it up in req.order and re-anchors via select_track + seek.
      const newTracks = this.tracks.slice();
      const [moved] = newTracks.splice(fromIdx, 1);
      newTracks.splice(insertIdx, 0, moved);
      this.tracks = newTracks;
      this.render();

      const order = newTracks.map(t => t.id).filter(Boolean);
      try {
        const resp = await fetch('/api/player/reorder', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({order}),
        });
        if (!resp.ok) {
          const err = await resp.json().catch(() => ({}));
          console.warn('reorder rejected:', err.detail || resp.status);
        }
      } catch (err) {
        console.warn('reorder failed', err);
      }
      // Either way, sync with HQP so any drift between optimistic
      // local state and authoritative playlist is corrected.
      if (typeof window.fetchPlaylist === 'function') window.fetchPlaylist();
    },

    async removeAt(index) {
      // Optimistic local update: drop the row + re-render so the
      // tap feels instant. Backend invalidates its playlist cache;
      // the next playlist-loaded event from the SSE poller will
      // overwrite our optimistic copy with the authoritative HQP
      // state.
      const optimistic = this.tracks.slice(0, index - 1)
        .concat(this.tracks.slice(index));
      this.tracks = optimistic;
      this.render();
      try {
        await fetch('/api/player/remove', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({index}),
        });
        // Force a refresh — the SSE poller picks up the change
        // within ~1s but tapping refresh removes the visible delay.
        if (typeof window.fetchPlaylist === 'function') window.fetchPlaylist();
      } catch (err) {
        console.warn('remove failed', err);
        // Revert optimistic change on failure.
        if (typeof window.fetchPlaylist === 'function') window.fetchPlaylist();
      }
    },
  };

  function formatDurationSummary(sec) {
    const s = Math.max(0, Math.floor(sec || 0));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const r = s % 60;
    if (h > 0) return `${h}:${m.toString().padStart(2, '0')}:${r.toString().padStart(2, '0')}`;
    return `${m}:${r.toString().padStart(2, '0')}`;
  }

  /* ---------- AI assistant sheet ----------
     Wires the FAB to the /api/chat backend (sessions, messages, track
     picks). Provider/model selection lives in Settings — here we use
     whatever the backend's `default_provider` returns. */

  const ai = {
    el: null, thread: null, input: null, form: null, sendBtn: null,
    viewChat: null, viewList: null,
    chatList: null, emptyWrap: null,
    chatTitle: null,
    backBtn: null, newBtn: null, emptyNewBtn: null,
    closeBtnChat: null, closeBtnList: null,
    sessions: [],
    isOpen: false,
    view: 'chat',            // 'chat' | 'list'
    activeSessionId: null,
    sending: false,

    init() {
      this.el = document.getElementById('aiSheet');
      if (!this.el) return;
      this.viewChat = document.getElementById('aiViewChat');
      this.viewList = document.getElementById('aiViewList');
      this.thread = document.getElementById('aiThread');
      this.input = document.getElementById('aiInput');
      this.form = document.getElementById('aiInputForm');
      this.sendBtn = document.getElementById('aiSendBtn');
      this.chatList = document.getElementById('aiChatList');
      this.emptyWrap = document.getElementById('aiEmpty');
      this.chatTitle = document.getElementById('aiChatTitle');
      this.backBtn = document.getElementById('aiBackBtn');
      this.newBtn = document.getElementById('aiNewBtn');
      this.emptyNewBtn = document.getElementById('aiEmptyNewBtn');
      this.closeBtnChat = document.getElementById('aiCloseBtnChat');
      this.closeBtnList = document.getElementById('aiCloseBtnList');

      this.backBtn.addEventListener('click', () => this.openListView());
      this.newBtn.addEventListener('click', () => this.newSession());
      this.emptyNewBtn.addEventListener('click', () => this.newSession());
      this.closeBtnChat.addEventListener('click', () => this.hide());
      this.closeBtnList.addEventListener('click', () => this.hide());
      this.form.addEventListener('submit', e => {
        e.preventDefault();
        this.send();
      });

      const fab = document.getElementById('aiFab');
      if (fab) fab.addEventListener('click', () => this.show());

      document.addEventListener('keydown', e => {
        if (e.key !== 'Escape' || !this.isOpen) return;
        // Inside the sheet: list → chat (so users with an active
        // session can get back without losing context). Then chat
        // → close. Plain × in either view exits the sheet entirely.
        if (this.view === 'list' && this.activeSessionId !== null) {
          this.openChatView();
        } else {
          this.hide();
        }
      });
    },

    async show() {
      if (!this.el) return;
      this.el.hidden = false;
      this.isOpen = true;
      const fab = document.getElementById('aiFab');
      if (fab) fab.hidden = true;
      if (this.activeSessionId === null) {
        await this.bootstrap();
      } else {
        await this.loadSessions();
        this.renderChatList();
        setTimeout(() => this.input && this.input.focus(), 50);
      }
    },

    hide() {
      if (!this.el) return;
      this.el.hidden = true;
      this.isOpen = false;
      updateFabVisibility(currentRoute);
    },

    async bootstrap() {
      try {
        await this.loadSessions();
        if (this.sessions.length > 0) {
          await this.switchToSession(this.sessions[0].id);
        } else {
          // Brand-new install / all chats deleted: drop into the
          // empty list view so the user sees the explicit "+ New"
          // CTA instead of a half-built chat scaffold.
          this.openListView();
        }
      } catch (err) {
        console.warn('AI bootstrap failed:', err);
        this.openListView();
      }
    },

    setView(view) {
      this.view = view;
      this.viewChat.hidden = view !== 'chat';
      this.viewList.hidden = view !== 'list';
    },

    openChatView() {
      this.setView('chat');
      setTimeout(() => this.input && this.input.focus(), 50);
    },

    openListView() {
      this.setView('list');
      this.renderChatList();
    },

    async loadSessions() {
      try {
        const resp = await fetch('/api/chat/sessions?limit=50');
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        this.sessions = await resp.json() || [];
      } catch (err) {
        console.warn('sessions load failed:', err);
        this.sessions = [];
      }
    },

    renderChatList() {
      this.chatList.innerHTML = '';
      if (this.sessions.length === 0) {
        this.chatList.hidden = true;
        this.emptyWrap.hidden = false;
        return;
      }
      this.chatList.hidden = false;
      this.emptyWrap.hidden = true;
      for (const s of this.sessions) {
        this.chatList.appendChild(this.buildChatRow(s));
      }
    },

    buildChatRow(session) {
      const row = document.createElement('div');
      row.className = 'ai-chat-row';
      if (session.id === this.activeSessionId) row.classList.add('is-active');
      row.dataset.sessionId = String(session.id);

      const title = (session.title || 'New chat').trim() || 'New chat';
      const preview = (session.preview || '').trim();
      const ts = formatRelativeTime(session.updated_at || session.created_at);
      const modelLabel = shortModelLabel(session.last_model);

      const main = document.createElement('div');
      main.className = 'ai-chat-row-main';
      const titleEl = document.createElement('div');
      titleEl.className = 'ai-chat-row-title';
      titleEl.textContent = title;
      main.appendChild(titleEl);
      if (preview) {
        const prevEl = document.createElement('div');
        prevEl.className = 'ai-chat-row-preview';
        prevEl.textContent = preview;
        main.appendChild(prevEl);
      }

      const meta = document.createElement('div');
      meta.className = 'ai-chat-row-meta';
      if (ts) {
        const tsEl = document.createElement('span');
        tsEl.className = 'ai-chat-row-ts';
        tsEl.textContent = ts;
        meta.appendChild(tsEl);
      }
      if (modelLabel) {
        const mdl = document.createElement('span');
        mdl.className = 'ai-chat-row-model';
        mdl.textContent = modelLabel;
        meta.appendChild(mdl);
      }

      const trash = document.createElement('button');
      trash.type = 'button';
      trash.className = 'ai-trash-btn';
      trash.setAttribute('aria-label', 'Delete chat');
      trash.innerHTML = `
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none"
             stroke="currentColor" stroke-width="1.6"
             stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <path d="M4 7h16M9 7V4h6v3M6 7l1 13a2 2 0 002 2h6a2 2 0 002-2l1-13M10 11v7M14 11v7"/>
        </svg>`;

      // Tap row body → open chat. Tap trash → swap row contents
      // for an inline confirm bar (Cancel restores the row, Delete
      // commits). Click on trash MUST stop propagation so it
      // doesn't also fire the row's open-chat handler.
      row.addEventListener('click', () => this.switchToSession(session.id));
      trash.addEventListener('click', e => {
        e.stopPropagation();
        this.confirmDeleteRow(row, session);
      });

      main.style.gridColumn = '1';
      meta.style.gridColumn = '2';
      trash.style.gridColumn = '3';
      row.appendChild(main);
      row.appendChild(meta);
      row.appendChild(trash);
      return row;
    },

    confirmDeleteRow(row, session) {
      // Snapshot the original markup so Cancel can restore it.
      const original = row.innerHTML;
      const wasActive = row.classList.contains('is-active');
      row.classList.remove('is-active');
      row.classList.add('is-confirming');
      row.innerHTML = `
        <div class="ai-confirm-bar">
          <span class="ai-confirm-ask">Delete this chat?</span>
          <button type="button" class="ai-confirm-btn"
                  data-action="cancel">Cancel</button>
          <button type="button" class="ai-confirm-btn is-danger"
                  data-action="delete">Delete</button>
        </div>
      `;
      const restore = () => {
        row.classList.remove('is-confirming');
        if (wasActive) row.classList.add('is-active');
        row.innerHTML = original;
        // Re-bind row + trash handlers (they were destroyed by
        // innerHTML replacement). Cheaper than rebuilding from
        // scratch since the visible markup is identical.
        row.addEventListener('click',
          () => this.switchToSession(session.id));
        const trash = row.querySelector('.ai-trash-btn');
        if (trash) trash.addEventListener('click', e => {
          e.stopPropagation();
          this.confirmDeleteRow(row, session);
        });
      };
      row.querySelector('[data-action="cancel"]').addEventListener('click', e => {
        e.stopPropagation();
        restore();
      });
      row.querySelector('[data-action="delete"]').addEventListener('click', e => {
        e.stopPropagation();
        this.deleteSession(session.id);
      });
    },

    async newSession() {
      try {
        const resp = await fetch('/api/chat/sessions', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({}),
        });
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        const session = await resp.json();
        this.activeSessionId = session.id;
        this.chatTitle.textContent = 'New chat';
        this.thread.innerHTML = '';
        this.renderEmpty('Ask the AI for recommendations, analysis or context.');
        // Sessions list cache is now stale; refresh on next list-view
        // open. Don't refetch eagerly — the user is in chat-view and
        // doesn't need the list rebuilt right now.
        this.sessions.unshift({
          id: session.id, title: null, updated_at: session.created_at,
          preview: null, last_model: null,
        });
        this.openChatView();
      } catch (err) {
        console.warn('new session failed:', err);
      }
    },

    async switchToSession(id) {
      this.activeSessionId = id;
      this.openChatView();
      this.thread.innerHTML = '<p class="ai-empty">Loading…</p>';
      // Title from cached metadata; updates after messages load if
      // backend just auto-derived a title.
      const cached = this.sessions.find(s => s.id === id);
      this.chatTitle.textContent =
        (cached && cached.title) || 'New chat';
      try {
        const resp = await fetch('/api/chat/sessions/' + id + '/messages');
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        const messages = await resp.json();
        this.thread.innerHTML = '';
        if (!messages || messages.length === 0) {
          this.renderEmpty('Ask the AI for recommendations, analysis or context.');
        } else {
          for (const m of messages) this.appendMessage(m);
          this.scrollToBottom();
        }
        setTimeout(() => this.input && this.input.focus(), 50);
      } catch (err) {
        console.warn('load messages failed:', err);
        this.renderEmpty('Could not load this chat.');
      }
    },

    renderEmpty(text) {
      this.thread.innerHTML =
        '<p class="ai-empty">' + escapeHtml(text) + '</p>';
    },

    appendMessage(m) {
      // Drop any "empty" placeholder before adding real content.
      const empty = this.thread.querySelector('.ai-empty');
      if (empty) empty.remove();

      const row = document.createElement('div');
      row.className = 'ai-msg-row' + (m.role === 'user' ? ' is-user' : '');
      if (m.role === 'user') {
        const bubble = document.createElement('div');
        bubble.className = 'ai-msg-user';
        bubble.textContent = m.content || '';
        row.appendChild(bubble);
      } else {
        const body = document.createElement('div');
        body.className = 'ai-msg-ai';
        if (m.model) {
          const tag = document.createElement('span');
          tag.className = 'ai-model-tag';
          tag.textContent = shortModelLabel(m.model) || m.model;
          body.appendChild(tag);
        }
        const prose = document.createElement('div');
        prose.className = 'ai-msg-prose';
        prose.innerHTML = mdToHtml(m.content || '');
        body.appendChild(prose);
        row.appendChild(body);

        // Structured blocks render as a sibling of the prose bubble so
        // they span the full thread width — matching Discovery exactly,
        // where shuffle-row / track-list use their own 20px padding
        // against the screen edges. Wrapping them inside the 92%-capped
        // .ai-msg-ai bubble would force them ~26px narrower than
        // Discovery's identical markup.
        const blocks = aiBlocksFromMessage(m);
        if (blocks.length > 0) {
          const blocksEl = document.createElement('div');
          blocksEl.className = 'ai-blocks';
          for (const b of blocks) {
            const blockEl = renderAiBlock(b);
            if (blockEl) blocksEl.appendChild(blockEl);
          }
          row.appendChild(blocksEl);
          // Click + queue handlers come from the same helper that wires
          // Discovery and detail screens.
          wireDetailHandlers(blocksEl);
        }
      }
      this.thread.appendChild(row);
    },

    scrollToBottom() {
      this.thread.scrollTop = this.thread.scrollHeight;
    },

    typingIndicator() {
      const row = document.createElement('div');
      row.className = 'ai-msg-row';
      row.id = 'aiTyping';
      row.innerHTML = `
        <div class="ai-msg-ai"><span class="ai-typing">
          <span class="ai-typing-dot"></span>
          <span class="ai-typing-dot"></span>
          <span class="ai-typing-dot"></span>
        </span></div>`;
      return row;
    },

    async send() {
      const text = (this.input.value || '').trim();
      if (!text || this.sending) return;
      if (this.activeSessionId === null) {
        await this.newSession();
        if (this.activeSessionId === null) return;
      }
      this.sending = true;
      this.sendBtn.disabled = true;
      this.input.value = '';

      // Optimistic user bubble + typing indicator. The indicator
      // disappears as soon as the first text delta arrives, so the
      // typing dots feel like the model "thinking" before any prose.
      this.appendMessage({ role: 'user', content: text });
      const typing = this.typingIndicator();
      this.thread.appendChild(typing);
      this.scrollToBottom();

      // Lazily-built scaffolding for the assistant bubble. We only
      // mount it on the first event from the server (meta with model,
      // first delta, etc.) so that an early validation error keeps
      // the typing indicator clean.
      let aiRow = null, aiBody = null, proseDiv = null, blocksDiv = null;
      let modelTag = null, proseText = '';
      let pendingModelLabel = '';   // remembered between meta and bubble creation

      const ensureAiRow = () => {
        if (aiRow) return;
        if (typing.parentNode) typing.remove();
        aiRow = document.createElement('div');
        aiRow.className = 'ai-msg-row';
        aiBody = document.createElement('div');
        aiBody.className = 'ai-msg-ai';
        proseDiv = document.createElement('div');
        proseDiv.className = 'ai-msg-prose';
        aiBody.appendChild(proseDiv);
        aiRow.appendChild(aiBody);
        this.thread.appendChild(aiRow);
        if (pendingModelLabel) applyModelLabel();
      };

      const applyModelLabel = () => {
        if (!aiBody || !pendingModelLabel) return;
        if (!modelTag) {
          modelTag = document.createElement('span');
          modelTag.className = 'ai-model-tag';
          aiBody.insertBefore(modelTag, proseDiv);
        }
        modelTag.textContent =
          pendingModelLabel.split(':').pop() || pendingModelLabel;
      };

      const onDelta = (chunk) => {
        if (!chunk) return;
        ensureAiRow();
        proseText += chunk;
        // Re-render the whole prose on each delta. Cheap for normal
        // response sizes (a few hundred tokens) and lets in-flight
        // markdown — `**bold` mid-stream — settle visually as soon
        // as the closing `**` arrives.
        proseDiv.innerHTML = mdToHtml(proseText);
        this.scrollToBottom();
      };

      const onBlocks = (blocks) => {
        if (!Array.isArray(blocks) || blocks.length === 0) return;
        ensureAiRow();
        if (!blocksDiv) {
          blocksDiv = document.createElement('div');
          blocksDiv.className = 'ai-blocks';
          aiRow.appendChild(blocksDiv);
        } else {
          blocksDiv.innerHTML = '';
        }
        for (const b of blocks) {
          const el = renderAiBlock(b);
          if (el) blocksDiv.appendChild(el);
        }
        wireDetailHandlers(blocksDiv);
        this.scrollToBottom();
      };

      const onModel = (modelStr) => {
        if (!modelStr) return;
        pendingModelLabel = modelStr;
        const label = modelStr.split(':').pop() || modelStr;
        // Place the tag in the typing indicator so the user sees which
        // model is "thinking" before any prose lands; once the first
        // delta arrives, ensureAiRow() will re-apply pendingModelLabel
        // to the real bubble.
        if (typing && typing.parentNode) {
          const wrap = typing.querySelector('.ai-msg-ai');
          if (wrap && !wrap.querySelector('.ai-model-tag')) {
            const tag = document.createElement('span');
            tag.className = 'ai-model-tag';
            tag.textContent = label;
            wrap.insertBefore(tag, wrap.firstChild);
          }
        }
        if (aiRow) applyModelLabel();
      };

      try {
        const resp = await fetch(
          '/api/chat/sessions/' + this.activeSessionId + '/messages',
          {
            method: 'POST',
            headers: {
              'Content-Type': 'application/json',
              'Accept': 'text/event-stream',
            },
            body: JSON.stringify({ message: text }),
          });
        if (!resp.ok) {
          // Pre-stream validation failure (no provider, missing
          // session). Body is plain JSON, not SSE.
          let detail = 'send failed';
          try { detail = (await resp.json()).detail || detail; } catch (_) {}
          throw new Error(detail);
        }

        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          // SSE messages are separated by a blank line (\n\n).
          let sep;
          while ((sep = buffer.indexOf('\n\n')) >= 0) {
            const raw = buffer.slice(0, sep);
            buffer = buffer.slice(sep + 2);
            const evt = parseSseMessage(raw);
            if (!evt) continue;

            if (evt.event === 'meta') {
              const modelStr = evt.data.provider
                ? `${evt.data.provider}:${evt.data.model || ''}`
                : (evt.data.model || '');
              if (modelStr) onModel(modelStr);
            } else if (evt.event === 'delta') {
              onDelta(evt.data.text || '');
            } else if (evt.event === 'blocks') {
              onBlocks(evt.data.blocks || []);
            } else if (evt.event === 'tool') {
              // Tool starts are informational. Future: show a small
              // inline "searching tracks…" pip; for now, no-op.
            } else if (evt.event === 'done') {
              // Authoritative model name from the provider — overrides
              // the optimistic one we set on `meta`. Both usually agree
              // but `done` is canonical (matches what's persisted).
              const modelStr = evt.data.provider
                ? `${evt.data.provider}:${evt.data.model || ''}`
                : (evt.data.model || '');
              onModel(modelStr);
              // Backend just refined the truncated-message fallback
              // title via Haiku for the first exchange in this
              // session. Update both the chat-view title bar and
              // the cached sessions[] entry so list-view picks it
              // up without an extra round-trip.
              if (evt.data.title) {
                this.chatTitle.textContent = evt.data.title;
                const cached = this.sessions.find(
                  s => s.id === this.activeSessionId);
                if (cached) cached.title = evt.data.title;
              }
              // Provider-side failure (Anthropic 402 / quota / rate
              // limit / OpenAI invalid_api_key…). The stream finished
              // cleanly but the assistant has nothing to say. Surface
              // the message instead of leaving the bubble empty —
              // attach the SDK-derived action link when we have one
              // so the user can fix it in one click.
              if (evt.data.provider_error) {
                const err = new Error(evt.data.provider_error);
                if (evt.data.provider_error_action) {
                  err.action = evt.data.provider_error_action;
                }
                throw err;
              }
            } else if (evt.event === 'error') {
              throw new Error(evt.data.message || 'AI error');
            }
          }
        }

        if (typing.parentNode) typing.remove();
        // Refresh sessions metadata so list-view picks up the new
        // preview / last_model. Title is already up-to-date from
        // the 'done' SSE event so we don't overwrite the title bar
        // here — re-loading would briefly flash the stale value.
        await this.loadSessions();
        this.scrollToBottom();
      } catch (err) {
        if (typing.parentNode) typing.remove();
        console.warn('send failed:', err);
        const action = err && err.action && err.action.url && err.action.label
          ? err.action
          : null;
        const actionHtml = action
          ? ` <a href="${escapeHtml(action.url)}" target="_blank" rel="noopener" style="color:var(--color-amber);text-decoration:underline;">${escapeHtml(action.label)} →</a>`
          : '';
        if (aiRow && proseDiv) {
          // Stream started before failure — append the error inline
          // so partial output stays visible.
          const errP = document.createElement('p');
          errP.style.color = 'var(--color-text-muted)';
          errP.innerHTML = '— ' + escapeHtml(String(err.message || err)) + actionHtml;
          proseDiv.appendChild(errP);
        } else {
          const errRow = document.createElement('div');
          errRow.className = 'ai-msg-row';
          errRow.innerHTML =
            '<div class="ai-msg-ai" style="color:var(--color-text-muted);">' +
            escapeHtml(String(err.message || err)) + actionHtml + '</div>';
          this.thread.appendChild(errRow);
        }
        this.scrollToBottom();
      } finally {
        this.sending = false;
        this.sendBtn.disabled = false;
        this.input.focus();
      }
    },

    async deleteSession(id) {
      try {
        await fetch('/api/chat/sessions/' + id, { method: 'DELETE' });
      } catch (err) { console.warn('delete failed:', err); }
      // Drop locally first so the row disappears even if the next
      // fetch hiccups. Stay in whatever view the user is currently
      // in — deleting from the list shouldn't yank them into a
      // different chat. If the active chat is the one deleted, just
      // forget it; next time the user picks a chat (or hits "+New")
      // we'll mount a new thread.
      this.sessions = this.sessions.filter(s => s.id !== id);
      if (id === this.activeSessionId) {
        this.activeSessionId = null;
        this.thread.innerHTML = '';
        this.chatTitle.textContent = 'AI';
      }
      this.renderChatList();
    },
  };

  // Friendly label for the model tag — strips the provider prefix
  // and the date suffix (claude-haiku-4-5-20251001 → Haiku 4.5) so
  // the chat-list row stays under the 32 px reserved column instead
  // of stealing space from the title.
  function shortModelLabel(raw) {
    if (!raw) return '';
    const m = String(raw).split(':').pop().trim();
    const apiMatch = m.match(/^claude-(haiku|sonnet|opus)-([\d-]+?)(?:-\d{8})?$/i);
    if (apiMatch) {
      const tier = apiMatch[1].charAt(0).toUpperCase() + apiMatch[1].slice(1);
      return `${tier} ${apiMatch[2].replace(/-/g, '.')}`;
    }
    if (/^(sonnet|haiku|opus)$/i.test(m)) {
      return m.charAt(0).toUpperCase() + m.slice(1);
    }
    if (/^gpt-/i.test(m)) return m;
    return m.replace(/-\d{8}$/, '');
  }

  // Compact relative time for chat-list rows. "now" / "5m" / "2h" /
  // "yesterday" / "apr 26" / locale date for older entries.
  function formatRelativeTime(ts) {
    if (!ts) return '';
    const d = new Date(ts);
    if (isNaN(d.getTime())) return '';
    const sec = Math.max(0, (Date.now() - d.getTime()) / 1000);
    if (sec < 45) return 'now';
    if (sec < 3600) return Math.round(sec / 60) + 'm';
    if (sec < 86400) return Math.round(sec / 3600) + 'h';
    if (sec < 172800) return 'yesterday';
    if (sec < 604800) return Math.round(sec / 86400) + 'd';
    if (sec < 31536000) {
      return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
        .toLowerCase();
    }
    return d.toLocaleDateString();
  }

  /* ---------- Screen renderers ---------- */

  function placeholderScreen(label) {
    return (root) => {
      const div = document.createElement('div');
      div.className = 'placeholder-screen';
      div.innerHTML = `
        <h2 class="placeholder-title">${escapeHtml(label)}</h2>
        <p class="placeholder-body">
          This screen is being rebuilt in the new design system.
          The current full version is still available below.
        </p>
        <a class="legacy-link" href="/static/legacy.html">Open legacy UI →</a>
      `;
      root.appendChild(div);
    };
  }

  /* ---------- Home (placeholder data; real wiring in Step 1.2) ---------- */

  function renderArtistTile(item) {
    const name = item.name || '';
    const tile = document.createElement('button');
    tile.type = 'button';
    tile.className = 'artist-tile';
    const ph = avatarPlaceholder(name);
    const initials = `<span class="artist-avatar-initials">${escapeHtml(ph.initials)}</span>`;
    tile.innerHTML = `
      <div class="artist-avatar" style="background: ${ph.bg};">${
        artistAvatarInner(item.id, initials)
      }</div>
      <div class="artist-name">${escapeHtml(name)}</div>
    `;
    if (item.id) {
      tile.addEventListener('click', () => navigateToEntity('artist', item.id));
    }
    return tile;
  }

  function renderAlbumTile(item) {
    const { id, title, artist, similarity } = item;
    const tile = document.createElement('button');
    tile.type = 'button';
    tile.className = 'album-tile';
    const c = coverPlaceholderColors(title || artist || 'x');
    const url = coverUrl(item);
    // onerror hides the <img>, revealing the parent's gradient + label.
    const cover = url
      ? `<img src="${url}" alt="" loading="lazy"
              onerror="this.style.display='none'"
              style="width:100%;height:100%;object-fit:cover;display:block;">`
      : `<div class="placeholder-badge">${escapeHtml(title || '')}</div>`;
    const sim = (similarity != null)
      ? `<div class="album-similarity">${similarity.toFixed(2)}</div>`
      : '';
    tile.innerHTML = `
      <div class="album-cover" style="--cover-bg-1: ${c.bg1}; --cover-bg-2: ${c.bg2};">
        ${cover}
      </div>
      <div class="album-title">${escapeHtml(title || '')}</div>
      <div class="album-artist">${escapeHtml(artist || '')}</div>
      ${sim}
    `;
    if (id) {
      tile.addEventListener('click', () => navigateToEntity('album', id));
    }
    return tile;
  }

  // Renders an empty Home section shell (title + horizontally-scrolling
  // row container) and returns the row so callers can append tiles as
  // their fetches resolve. Splitting creation from population lets the
  // three Home blocks render on-readiness instead of waiting for the
  // slowest endpoint, and lets attachInfiniteScroll bolt onto the row
  // without reaching into renderHome's local state.
  function createHomeSection(parent, title) {
    const sec = document.createElement('section');
    sec.className = 'home-section';
    sec.innerHTML = `
      <div class="home-section-head">
        <h2 class="home-section-title">${escapeHtml(title)}</h2>
      </div>
    `;
    const row = document.createElement('div');
    row.className = 'home-row';
    sec.appendChild(row);
    parent.appendChild(sec);
    return { section: sec, row };
  }

  function fillHomeRow(row, items, kind) {
    if (!items || items.length === 0) {
      const empty = document.createElement('div');
      empty.className = 'placeholder-body';
      empty.style.padding = 'var(--space-2)';
      empty.textContent = '—';
      row.appendChild(empty);
      return;
    }
    for (const item of items) {
      if (kind === 'artist') row.appendChild(renderArtistTile(item));
      else row.appendChild(renderAlbumTile(item));
    }
  }

  function appendTilesBeforeSentinel(row, items, kind, sentinel) {
    const frag = document.createDocumentFragment();
    for (const item of items) {
      if (kind === 'artist') frag.appendChild(renderArtistTile(item));
      else frag.appendChild(renderAlbumTile(item));
    }
    if (sentinel && sentinel.parentNode === row) row.insertBefore(frag, sentinel);
    else row.appendChild(frag);
  }

  // IntersectionObserver-based infinite scroll for horizontal rows.
  // Watches a 1px sentinel at the end of the row; when it enters the
  // viewport (with 200px rootMargin so we fetch *before* the user
  // hits the wall), pulls the next cursor page and appends tiles in
  // front of the sentinel. Failures don't auto-retry — the next
  // scroll-induced intersection re-fires the observer, which is
  // event-driven not polling, so we stay within the project rule.
  function attachInfiniteScroll(row, endpoint, initialCursor, kind) {
    if (!initialCursor) return { disconnect() {} };

    const sentinel = document.createElement('div');
    sentinel.className = 'home-row-sentinel';
    sentinel.setAttribute('aria-hidden', 'true');
    row.appendChild(sentinel);

    const state = { cursor: initialCursor, loading: false, exhausted: false };

    const fetchNext = async () => {
      if (state.loading || state.exhausted) return;
      state.loading = true;
      try {
        const params = new URLSearchParams({
          limit: '20',
          before: state.cursor.before,
          before_id: state.cursor.before_id,
        });
        const resp = await fetch(`${endpoint}?${params}`);
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        const data = await resp.json();
        const items = data.albums || data.artists || [];
        appendTilesBeforeSentinel(row, items, kind, sentinel);
        if (data.next_cursor) {
          state.cursor = data.next_cursor;
        } else {
          state.exhausted = true;
          sentinel.remove();
          observer.disconnect();
        }
      } catch (err) {
        console.warn('Infinite scroll fetch failed:', err);
      } finally {
        state.loading = false;
      }
    };

    const observer = new IntersectionObserver((entries) => {
      if (entries.some(e => e.isIntersecting)) fetchNext();
    }, { root: row, threshold: 0, rootMargin: '0px 200px 0px 0px' });
    observer.observe(sentinel);

    return { disconnect: () => observer.disconnect() };
  }

  // Three independent fetches, each section renders as its endpoint
  // resolves. Mirrors the Discovery "render on readiness" pattern so
  // a slow Favourite-artists aggregation doesn't block New in library
  // from appearing.
  async function renderHome(root) {
    const screen = document.createElement('div');
    screen.className = 'screen';
    screen.innerHTML = `
      <header class="screen-head">
        <h1 class="screen-title">Sautium<span class="dot">.</span></h1>
      </header>
    `;
    root.appendChild(screen);

    const favSec = createHomeSection(screen, 'Favourite artists');
    const newSec = createHomeSection(screen, 'New in library');
    const recSec = createHomeSection(screen, 'Recommendations');

    const sectionFailure = (section, label) => (err) => {
      console.warn(`Home/${label} failed:`, err);
      section.row.innerHTML = '';
      const empty = document.createElement('div');
      empty.className = 'placeholder-body';
      empty.style.padding = 'var(--space-2)';
      empty.textContent = '—';
      section.row.appendChild(empty);
    };

    fetch('/api/home/favourite-artists?limit=100')
      .then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(data => fillHomeRow(favSec.row, data.artists, 'artist'))
      .catch(sectionFailure(favSec, 'favourite-artists'));

    fetch('/api/home/new-in-library?limit=20')
      .then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(data => {
        fillHomeRow(newSec.row, data.albums, 'album');
        attachInfiniteScroll(newSec.row, '/api/home/new-in-library',
          data.next_cursor, 'album');
      })
      .catch(sectionFailure(newSec, 'new-in-library'));

    fetch('/api/home/recommendations?limit=20')
      .then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(data => fillHomeRow(recSec.row, data.albums, 'album'))
      .catch(sectionFailure(recSec, 'recommendations'));
  }

  /* ---------- Discovery screen (Step 1.5b) ----------
     Universal search: typing fires 5 independent endpoints in
     parallel, each renders its block as soon as it resolves so
     fast signals (trigram) are not blocked by slow ones (CLAP /
     BGE-M3 cold start). Reference markup is preserved for the
     search input + shuffle mosaic; mode chips and advanced-filters
     toggle are removed (advanced filters move to a dedicated
     surface in 1.5c). */

  const SVG_SEARCH = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/></svg>';
  const SVG_ADV_CHEVRON = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg>';

  // Per-block descriptors — endpoint, frontend block id, rendered title.
  const DISCOVERY_BLOCKS = [
    { id: 'artists', title: 'Artists',           endpoint: '/api/discovery/artists',  layout: 'artists' },
    { id: 'albums',  title: 'Albums',            endpoint: '/api/discovery/albums',   layout: 'albums'  },
    { id: 'titles',  title: 'Title matches',     endpoint: '/api/discovery/titles',   layout: 'tracks'  },
    { id: 'sound',   title: 'Closest in sound',  endpoint: '/api/discovery/sound',    layout: 'tracks'  },
    { id: 'lyrics',  title: 'Closest in lyrics', endpoint: '/api/discovery/lyrics',   layout: 'tracks'  },
  ];

  // Filter rows for the advanced panel (Step 1.5c-a).
  // Each row has a label and a chip set; selected value lives in the
  // shared `discoveryFilters` state. Chip values map 1:1 to the
  // /search/features query params we'll wire in 1.5c-b.
  const DISCOVERY_FILTER_ROWS = [
    { key: 'mode',      label: 'Mode',
      chips: [['any','Any'], ['major','Major'], ['minor','Minor']] },
    { key: 'vocalist',  label: 'Vocalist',
      chips: [['any','Any'], ['vocal','Vocal'], ['instrumental','Instrumental']] },
    { key: 'gender',    label: 'Gender',
      chips: [['any','Any'], ['female','Female'], ['male','Male'], ['mixed','Mixed']] },
    { key: 'danceable', label: 'Danceable',
      chips: [['any','Any'], ['yes','Yes'], ['no','No']] },
    { key: 'energy',    label: 'Energy',
      chips: [['any','Any'], ['low','Low'], ['mid','Mid'], ['high','High']] },
  ];

  // Multi-select chip rows. Empty array = no filter; tapping any
  // chip adds/removes it from the set. No explicit "Any" chip — the
  // absence of selection IS "any".
  const DISCOVERY_QUALITY_OPTIONS = [
    ['lossy',    'Lossy'],
    ['lossless', 'Lossless'],
    ['hi-res',   'Hi-Res'],
  ];

  // Broad instrument chips. Each label is a category that 1.5c-b will
  // map to a set of underlying AST/PaSST tags in SQL — e.g. "Drums"
  // → ('drum', 'drum kit', 'drum machine', 'percussion', 'bass drum',
  // 'snare drum', 'cymbal', 'hi-hat'). Curated here to cover common
  // user intent without exposing the raw 25+ tag taxonomy. Vocals
  // intentionally excluded — Vocalist filter handles that signal.
  const DISCOVERY_INSTRUMENTS = [
    'Piano', 'Guitar', 'Electric guitar', 'Bass', 'Drums',
    'Strings', 'Orchestra', 'Synth', 'Brass', 'Saxophone',
  ];

  // 12 chromatic keys; matches `audio_features.key` storage.
  const DISCOVERY_KEYS = [
    'C', 'C#', 'D', 'D#', 'E', 'F',
    'F#', 'G', 'G#', 'A', 'A#', 'B',
  ];

  function defaultDiscoveryFilters() {
    return {
      bpm_min: null, bpm_max: null,
      key: '',
      mode: 'any', vocalist: 'any', gender: 'any',
      danceable: 'any', energy: 'any',
      quality: [],       // multi-select: ['lossy','lossless','hi-res']
      instruments: [],   // multi-select
    };
  }

  // True iff at least one filter dimension is set to a non-default
  // value. Used to decide whether the Apply button (with empty
  // search box) should browse the filtered library or do nothing.
  function hasActiveFilters(f) {
    if (!f) return false;
    if (f.bpm_min != null || f.bpm_max != null) return true;
    if (f.key) return true;
    if (f.mode && f.mode !== 'any') return true;
    if (f.vocalist && f.vocalist !== 'any') return true;
    if (f.gender && f.gender !== 'any') return true;
    if (f.danceable && f.danceable !== 'any') return true;
    if (f.energy && f.energy !== 'any') return true;
    if (f.instruments && f.instruments.length) return true;
    if (f.quality && f.quality.length) return true;
    return false;
  }

  // Translate the screen-local filter object into URLSearchParams the
  // backend endpoints understand. Drops "any" / null / empty values
  // so the server sees only filters the user actually set; multi-
  // selects (instruments, quality) become repeated query params.
  function appendFilterParams(params, f) {
    if (!f) return;
    if (f.bpm_min != null) params.set('bpm_min', f.bpm_min);
    if (f.bpm_max != null) params.set('bpm_max', f.bpm_max);
    if (f.key) params.set('key', f.key);
    if (f.mode && f.mode !== 'any') params.set('mode', f.mode);
    if (f.vocalist && f.vocalist !== 'any') params.set('vocalist', f.vocalist);
    if (f.gender && f.gender !== 'any') params.set('gender', f.gender);
    if (f.danceable && f.danceable !== 'any') params.set('danceable', f.danceable);
    if (f.energy && f.energy !== 'any') params.set('energy', f.energy);
    (f.instruments || []).forEach(v => params.append('instruments', v));
    (f.quality || []).forEach(v => params.append('quality', v));
  }

  async function renderDiscovery(root) {
    const screen = document.createElement('div');
    screen.className = 'discovery-screen';
    // Filter state is owned by the screen element so it survives
    // re-renders of children but resets when the screen is rebuilt.
    screen._filters = defaultDiscoveryFilters();

    screen.innerHTML = `
      <header class="screen-head">
        <h1 class="screen-title">Discovery</h1>
      </header>

      <div class="search-wrap">
        ${SVG_SEARCH}
        <input type="search" id="discoverySearchInput"
               placeholder="Artist, album, track, lyrics, or a mood…"
               autocomplete="off" autocapitalize="off" spellcheck="false">
      </div>

      <button class="adv-row" type="button" id="discoveryAdvToggle"
              aria-expanded="false">
        ${SVG_ADV_CHEVRON}
        Advanced filters
      </button>

      <div class="filters-panel" id="discoveryFiltersPanel" hidden>
        <div class="filter-row">
          <span class="filter-label">BPM range</span>
          <div class="bpm-inputs">
            <input type="number" inputmode="numeric" min="40" max="240"
                   class="bpm-field" id="discoveryFilterBpmMin"
                   placeholder="40">
            <span class="bpm-dash">—</span>
            <input type="number" inputmode="numeric" min="40" max="240"
                   class="bpm-field" id="discoveryFilterBpmMax"
                   placeholder="240">
            <span class="bpm-unit">bpm</span>
          </div>
        </div>

        <div class="filter-row">
          <span class="filter-label">Key</span>
          <div class="filter-chips" data-filter-key="key">
            <span class="f-chip is-active" data-value="">Any</span>
            ${DISCOVERY_KEYS.map(k =>
              `<span class="f-chip" data-value="${escapeHtml(k)}">${escapeHtml(k)}</span>`
            ).join('')}
          </div>
        </div>

        ${DISCOVERY_FILTER_ROWS.map(row => `
          <div class="filter-row">
            <span class="filter-label">${escapeHtml(row.label)}</span>
            <div class="filter-chips" data-filter-key="${escapeHtml(row.key)}">
              ${row.chips.map(([v, label], i) =>
                `<span class="f-chip${i === 0 ? ' is-active' : ''}"
                       data-value="${escapeHtml(v)}">${escapeHtml(label)}</span>`
              ).join('')}
            </div>
          </div>
        `).join('')}

        <div class="filter-row">
          <span class="filter-label">Quality</span>
          <div class="filter-chips" data-filter-multi="quality">
            ${DISCOVERY_QUALITY_OPTIONS.map(([v, label]) =>
              `<span class="f-chip" data-value="${escapeHtml(v)}">${escapeHtml(label)}</span>`
            ).join('')}
          </div>
        </div>

        <div class="filter-row">
          <span class="filter-label">Instruments</span>
          <div class="filter-chips" data-filter-multi="instruments">
            ${DISCOVERY_INSTRUMENTS.map(name =>
              `<span class="f-chip" data-value="${escapeHtml(name.toLowerCase())}">${escapeHtml(name)}</span>`
            ).join('')}
          </div>
        </div>

        <div class="filter-actions">
          <button class="filter-reset" type="button"
                  id="discoveryFilterReset">Reset</button>
          <button class="filter-apply" type="button"
                  id="discoveryFilterApply">Apply filters</button>
        </div>
      </div>

      <section class="discovery-shuffle" id="discoveryShuffle">
        <div class="discovery-section-head"><h3>Shuffle your library</h3></div>
        <p class="section-sub">Recall forgotten favourites</p>
        <div class="shuffle-mosaic">
          <div class="shuffle-row" id="discoveryShuffleRow"></div>
          <div class="edge-fade"></div>
        </div>
      </section>

      <section class="discovery-results" id="discoveryResults" hidden>
        <div class="d-searching" id="dSearching" hidden>
          <p class="placeholder-body" style="padding: var(--space-4);">Searching…</p>
        </div>
        ${DISCOVERY_BLOCKS.map(b => `
          <div class="d-block" id="dBlock-${b.id}" hidden>
            <div class="discovery-section-head"><h3>${escapeHtml(b.title)}</h3></div>
            <div class="d-block-body" id="dBody-${b.id}"></div>
          </div>
        `).join('')}
        <div class="d-empty" id="dEmpty" hidden>
          <p class="placeholder-body" style="padding: var(--space-4);">No matches.</p>
        </div>
      </section>
    `;
    root.appendChild(screen);

    fetchShuffle(screen);
    wireDiscoverySearch(screen);
    wireDiscoveryFilters(screen);
  }

  // Minimum query length before fanning out the 5-block search.
  // Single-character queries are pure noise: trigram similarity is
  // ~0 against any real word and BGE-M3 has no semantic context to
  // work with. Below the floor we keep the shuffle mosaic visible.
  const DISCOVERY_MIN_QUERY_LEN = 2;

  function wireDiscoveryFilters(screen) {
    // Toggle: adv-row open/close → flip aria-expanded + chevron + panel.
    const toggle = screen.querySelector('#discoveryAdvToggle');
    const panel = screen.querySelector('#discoveryFiltersPanel');
    if (toggle && panel) {
      toggle.addEventListener('click', () => {
        const open = panel.hidden;
        panel.hidden = !open;
        toggle.setAttribute('aria-expanded', String(open));
        toggle.classList.toggle('is-open', open);
      });
    }

    // Single-select chip rows (Key, Mode, Vocalist, Gender, etc.):
    // tapping a chip activates it and updates screen._filters[key].
    panel.querySelectorAll('.filter-chips[data-filter-key]').forEach(group => {
      const key = group.getAttribute('data-filter-key');
      group.querySelectorAll('.f-chip').forEach(chip => {
        chip.addEventListener('click', () => {
          group.querySelectorAll('.f-chip')
            .forEach(c => c.classList.remove('is-active'));
          chip.classList.add('is-active');
          screen._filters[key] = chip.getAttribute('data-value');
        });
      });
    });

    // Multi-select chip rows (Instruments): chips toggle independently
    // and the array of selected values lives in screen._filters[key].
    panel.querySelectorAll('.filter-chips[data-filter-multi]').forEach(group => {
      const key = group.getAttribute('data-filter-multi');
      group.querySelectorAll('.f-chip').forEach(chip => {
        chip.addEventListener('click', () => {
          chip.classList.toggle('is-active');
          const v = chip.getAttribute('data-value');
          const current = screen._filters[key] || [];
          if (chip.classList.contains('is-active')) {
            if (!current.includes(v)) current.push(v);
          } else {
            const i = current.indexOf(v);
            if (i >= 0) current.splice(i, 1);
          }
          screen._filters[key] = current;
        });
      });
    });

    // Numeric BPM bounds — clear-on-empty is OK, the filter is "any".
    const bpmMin = panel.querySelector('#discoveryFilterBpmMin');
    const bpmMax = panel.querySelector('#discoveryFilterBpmMax');
    if (bpmMin) bpmMin.addEventListener('input', e => {
      const v = e.target.value.trim();
      screen._filters.bpm_min = v ? Number(v) : null;
    });
    if (bpmMax) bpmMax.addEventListener('input', e => {
      const v = e.target.value.trim();
      screen._filters.bpm_max = v ? Number(v) : null;
    });

    // Reset returns every chip / input to its default.
    const resetBtn = panel.querySelector('#discoveryFilterReset');
    if (resetBtn) resetBtn.addEventListener('click', () => {
      screen._filters = defaultDiscoveryFilters();
      panel.querySelectorAll('.filter-chips[data-filter-key]').forEach(g => {
        g.querySelectorAll('.f-chip').forEach(
          (c, i) => c.classList.toggle('is-active', i === 0));
      });
      panel.querySelectorAll('.filter-chips[data-filter-multi] .f-chip')
        .forEach(c => c.classList.remove('is-active'));
      if (bpmMin) bpmMin.value = '';
      if (bpmMax) bpmMax.value = '';
    });

    // Apply: 1.5c-a is UI-only — re-running the search with active
    // filters lands in 1.5c-b. For now just collapse the panel and
    // re-trigger any current query so the user gets feedback. (No
    // wiring yet means results are unaffected.)
    const applyBtn = panel.querySelector('#discoveryFilterApply');
    if (applyBtn) applyBtn.addEventListener('click', () => {
      panel.hidden = true;
      toggle.setAttribute('aria-expanded', 'false');
      toggle.classList.remove('is-open');
      triggerDiscoverySearch(screen);
    });
  }

  function wireDiscoverySearch(screen) {
    const input = screen.querySelector('#discoverySearchInput');
    if (!input) return;
    screen._activeQueryId = 0;
    screen._debounceTimer = null;

    input.addEventListener('input', () => {
      clearTimeout(screen._debounceTimer);
      screen._debounceTimer = setTimeout(() =>
        triggerDiscoverySearch(screen), 250);
    });
  }

  // Single entry point for "do a search now" — invoked by the
  // input-debounce timer (typing) and by Apply (filter commit).
  // Decides between three states based on what the user has set:
  //   query >= MIN_QUERY_LEN          → unified 5-block search
  //   no query, but filters active    → filter-only browse via /titles
  //   neither                         → fall back to the shuffle mosaic
  function triggerDiscoverySearch(screen) {
    const input = screen.querySelector('#discoverySearchInput');
    const q = ((input && input.value) || '').trim();
    const filters = screen._filters || {};
    screen._activeQueryId = (screen._activeQueryId || 0) + 1;
    const id = screen._activeQueryId;
    const getActive = () => screen._activeQueryId;

    if (q.length >= DISCOVERY_MIN_QUERY_LEN) {
      runUnifiedSearch(screen, q, id, getActive);
    } else if (hasActiveFilters(filters)) {
      runFilterOnlyBrowse(screen, id, getActive);
    } else {
      showShuffle(screen);
    }
  }

  // Browse mode: no text query, only filters. Fires /api/discovery/titles
  // with an empty `q` so the backend returns tracks ordered by play
  // count desc that satisfy the active filters. Renders into a
  // single results block — artists/albums/sound/lyrics blocks
  // require text to be meaningful and are skipped.
  function runFilterOnlyBrowse(screen, queryId, getActiveId) {
    const shuffle = screen.querySelector('#discoveryShuffle');
    const results = screen.querySelector('#discoveryResults');
    const empty = screen.querySelector('#dEmpty');
    const searching = screen.querySelector('#dSearching');
    if (!shuffle || !results || !empty || !searching) return;

    shuffle.hidden = true;
    results.hidden = false;
    empty.hidden = true;
    searching.hidden = false;

    DISCOVERY_BLOCKS.forEach(b => {
      const blk = screen.querySelector('#dBlock-' + b.id);
      const body = screen.querySelector('#dBody-' + b.id);
      if (blk) blk.hidden = true;
      if (body) body.innerHTML = '';
    });

    const params = new URLSearchParams({ limit: '20' });
    appendFilterParams(params, screen._filters);
    fetch('/api/discovery/titles?' + params)
      .then(r => r.ok ? r.json() : Promise.reject(r.status))
      .then(data => {
        if (queryId !== getActiveId()) return;
        searching.hidden = true;
        const titlesBlock = DISCOVERY_BLOCKS.find(b => b.id === 'titles');
        // Override the block header to reflect browse semantics.
        const blk = screen.querySelector('#dBlock-titles');
        const head = blk && blk.querySelector('.discovery-section-head h3');
        if (head) head.textContent = 'Tracks matching filters';
        renderDiscoveryBlock(screen, titlesBlock, data);
        if ((data.results || []).length === 0) empty.hidden = false;
      })
      .catch(err => {
        if (queryId !== getActiveId()) return;
        searching.hidden = true;
        console.warn('filter-only browse failed:', err);
        empty.hidden = false;
      });
  }

  function showShuffle(screen) {
    const results = screen.querySelector('#discoveryResults');
    const shuffle = screen.querySelector('#discoveryShuffle');
    if (results) {
      results.hidden = true;
      DISCOVERY_BLOCKS.forEach(b => {
        const blk = screen.querySelector('#dBlock-' + b.id);
        const body = screen.querySelector('#dBody-' + b.id);
        if (blk) blk.hidden = true;
        if (body) body.innerHTML = '';
      });
      const empty = screen.querySelector('#dEmpty');
      if (empty) empty.hidden = true;
    }
    if (shuffle) shuffle.hidden = false;
  }

  function runUnifiedSearch(screen, query, queryId, getActiveId) {
    const shuffle = screen.querySelector('#discoveryShuffle');
    const results = screen.querySelector('#discoveryResults');
    const empty = screen.querySelector('#dEmpty');
    const searching = screen.querySelector('#dSearching');
    if (!shuffle || !results || !empty || !searching) return;
    shuffle.hidden = true;
    results.hidden = false;
    empty.hidden = true;
    searching.hidden = false;

    DISCOVERY_BLOCKS.forEach(b => {
      const blk = screen.querySelector('#dBlock-' + b.id);
      const body = screen.querySelector('#dBody-' + b.id);
      if (!blk || !body) return;
      blk.hidden = true;
      body.innerHTML = '';
      // Restore default header — browse mode rewrites titles' header
      // to "Tracks matching filters", reset before each new search.
      const head = blk.querySelector('.discovery-section-head h3');
      if (head) head.textContent = b.title;
    });

    const completion = { remaining: DISCOVERY_BLOCKS.length, hadAnyResults: false };

    const filters = screen._filters || {};
    DISCOVERY_BLOCKS.forEach(b => {
      const params = new URLSearchParams({ q: query, limit: '10' });
      // Filters apply only to track-level blocks (titles/sound/lyrics);
      // artists/albums are entity-level signals that don't carry the
      // filter dimensions (BPM, key, instruments, etc.).
      if (b.layout === 'tracks') appendFilterParams(params, filters);
      fetch(b.endpoint + '?' + params)
        .then(r => r.ok ? r.json() : Promise.reject(r.status))
        .then(data => {
          if (queryId !== getActiveId()) return;  // stale; user typed again
          searching.hidden = true;
          renderDiscoveryBlock(screen, b, data);
          if ((data.results || []).length > 0) completion.hadAnyResults = true;
        })
        .catch(err => {
          if (queryId !== getActiveId()) return;
          console.warn('discovery block failed:', b.id, err);
        })
        .finally(() => {
          if (queryId !== getActiveId()) return;
          completion.remaining -= 1;
          if (completion.remaining === 0) {
            searching.hidden = true;
            if (!completion.hadAnyResults) empty.hidden = false;
          }
        });
    });
  }

  function renderDiscoveryBlock(screen, descriptor, data) {
    const blk = screen.querySelector('#dBlock-' + descriptor.id);
    const body = screen.querySelector('#dBody-' + descriptor.id);
    if (!blk || !body) return;

    if (data.status === 'loading') {
      blk.hidden = false;
      body.innerHTML = `
        <p class="d-loading-notice">
          ${descriptor.id === 'sound'
            ? 'Audio model warming up — this takes ~20-30s on a fresh start. Try again shortly.'
            : 'Search model warming up — try again in a moment.'}
        </p>`;
      return;
    }

    const items = data.results || [];
    if (items.length === 0) {
      blk.hidden = true;
      body.innerHTML = '';
      return;
    }

    if (descriptor.layout === 'artists') {
      body.innerHTML = renderArtistRow(items);
      body.querySelectorAll('[data-artist-id]').forEach(el => {
        el.addEventListener('click', () =>
          navigateToEntity('artist', el.getAttribute('data-artist-id')));
      });
    } else if (descriptor.layout === 'albums') {
      body.innerHTML = renderAlbumRow(items);
      body.querySelectorAll('[data-album-id]').forEach(el => {
        el.addEventListener('click', () =>
          navigateToEntity('album', el.getAttribute('data-album-id')));
      });
    } else {
      body.innerHTML = renderTrackList(items);
      wireDetailHandlers(body);
      updatePlayingHighlight();
    }

    blk.hidden = false;
  }

  function renderArtistRow(items) {
    return `<div class="shuffle-row d-artist-row">${
      items.map(a => {
        const ph = avatarPlaceholder(a.artist || a.name || '?');
        const url = coverUrl({cover_id: a.cover_id, media_file_id: a.media_file_id});
        const inner = url
          ? `<img src="${url}" alt="" loading="lazy" onerror="this.style.display='none'">`
          : `<span class="d-artist-initials">${escapeHtml(ph.initials)}</span>`;
        return `
          <button class="d-artist-tile" type="button"
                  data-artist-id="${escapeHtml(a.artist_id || '')}">
            <div class="d-artist-avatar"
                 style="background: ${ph.bg};">${inner}</div>
            <div class="d-artist-name">${escapeHtml(a.artist || a.name || '')}</div>
          </button>`;
      }).join('')
    }</div>`;
  }

  function renderAlbumRow(items) {
    return `<div class="shuffle-row d-album-row">${
      items.map(a => {
        const c = coverPlaceholderColors(a.album || a.title || a.album_id || '');
        const url = coverUrl({cover_id: a.cover_id, media_file_id: a.media_file_id});
        const cover = url
          ? `<img src="${url}" alt="" loading="lazy" onerror="this.style.display='none'">`
          : '';
        return `
          <button class="mosaic-tile" type="button"
                  data-album-id="${escapeHtml(a.album_id || '')}">
            <div class="mosaic-cover"
                 style="--cover-bg-1: ${c.bg1}; --cover-bg-2: ${c.bg2};">${cover}</div>
            <div class="mosaic-title">${escapeHtml(a.album || a.title || '')}</div>
            <div class="mosaic-artist">${escapeHtml(a.artist || '')}</div>
          </button>`;
      }).join('')
    }</div>`;
  }

  function renderTrackList(items) {
    return `<div class="track-list">${
      items.map(t => {
        const c = coverPlaceholderColors(t.title || t.album || '');
        const url = coverUrl({cover_id: t.cover_id, media_file_id: t.id});
        const inner = url
          ? `<img src="${url}" alt="" loading="lazy" onerror="this.style.display='none'">`
          : '';
        const sub = [(t.artist || ''), (t.album || '')].filter(Boolean).join(' — ');
        const sim = (t.similarity != null)
          ? Number(t.similarity).toFixed(2) : '';
        return `
          <button class="track-row result-row" type="button"
                  data-media-file-id="${escapeHtml(String(t.id || ''))}">
            <div class="result-art"
                 style="--cover-bg-1: ${c.bg1}; --cover-bg-2: ${c.bg2};">${inner}</div>
            <div class="track-info">
              <div class="track-title-line">${escapeHtml(t.title || '')}</div>
              <div class="track-artist-line">${escapeHtml(sub)}</div>
            </div>
            <span class="result-similarity">${sim}</span>
            <span class="track-add" aria-label="Add to queue">${SVG_PLUS}</span>
          </button>`;
      }).join('')
    }</div>`;
  }

  async function fetchShuffle(screen) {
    const row = screen.querySelector('#discoveryShuffleRow');
    try {
      const resp = await fetch('/api/discovery/shuffle?limit=14');
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      const data = await resp.json();
      const albums = data.albums || [];
      if (albums.length === 0) {
        row.innerHTML = '<p class="placeholder-body">No albums in library yet.</p>';
        return;
      }
      row.innerHTML = albums.map(a => {
        const c = coverPlaceholderColors(a.title || a.id || '');
        const url = coverUrl(a);
        const cover = url
          ? `<img src="${url}" alt="" loading="lazy"
                  onerror="this.style.display='none'">`
          : '';
        return `
          <button class="mosaic-tile" type="button"
                  data-album-id="${escapeHtml(a.id || '')}">
            <div class="mosaic-cover"
                 style="--cover-bg-1: ${c.bg1}; --cover-bg-2: ${c.bg2};">${cover}</div>
            <div class="mosaic-title">${escapeHtml(a.title || '')}</div>
            <div class="mosaic-artist">${escapeHtml(a.artist || '')}</div>
          </button>`;
      }).join('');
      row.querySelectorAll('[data-album-id]').forEach(el => {
        el.addEventListener('click', () => {
          const id = el.getAttribute('data-album-id');
          if (id) navigateToEntity('album', id);
        });
      });
    } catch (err) {
      console.warn('shuffle failed:', err);
      row.innerHTML = '<p class="placeholder-body">Could not load shuffle.</p>';
    }
  }

  /* ---------- Artist + Album detail screens (Step 1.4) ----------
     Rendered when the hash matches #<tab>/artist/<uuid> or
     #<tab>/album/<uuid>. Markup mirrors docs/design/reference/
     claude-design-bundle/project/Session 2 v3.html — sections
     "Artist Detail" and "Album Detail". */

  function fmtDuration(seconds) {
    const s = Math.max(0, Math.floor(Number(seconds || 0)));
    const m = Math.floor(s / 60);
    const r = s % 60;
    return `${m}:${r.toString().padStart(2, '0')}`;
  }

  function fmtDurationLong(seconds) {
    const s = Math.max(0, Math.floor(Number(seconds || 0)));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const r = s % 60;
    if (h > 0) return `${h}:${m.toString().padStart(2, '0')}:${r.toString().padStart(2, '0')}`;
    return `${m}:${r.toString().padStart(2, '0')}`;
  }

  function modeShort(m) {
    if (m === 'minor') return 'min';
    if (m === 'major') return 'maj';
    return '';
  }

  function stripHtml(html) {
    if (!html) return '';
    return String(html)
      .replace(/<br\s*\/?>/gi, '\n')
      .replace(/<\/p\s*>/gi, '\n')
      .replace(/<[^>]+>/g, '')          // strip remaining tags
      .replace(/&nbsp;/g, ' ')
      .replace(/&amp;/g, '&')
      .replace(/&lt;/g, '<')
      .replace(/&gt;/g, '>')
      .replace(/&quot;/g, '"')
      .replace(/&#39;/g, "'")
      .replace(/[ \t]+/g, ' ')          // collapse spaces/tabs but keep newlines
      .replace(/\n[ \t]+/g, '\n')
      .replace(/[ \t]+\n/g, '\n')
      .replace(/\n{3,}/g, '\n\n')       // cap blank lines
      .trim();
  }

  /** Trim Last.fm boilerplate after the user-facing prose. */
  function trimLastFmTail(text) {
    if (!text) return '';
    // Cut at common Last.fm tail markers.
    const markers = [
      'Read more on Last.fm',
      'User-contributed text is available',
      'This article uses material from the Wikipedia article',
    ];
    for (const m of markers) {
      const idx = text.indexOf(m);
      if (idx > 0) return text.slice(0, idx).trim();
    }
    return text.trim();
  }

  const SVG_BACK = '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 6l-6 6 6 6"/></svg>';
  const SVG_KEBAB = '<svg width="22" height="22" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><circle cx="12" cy="5" r="1.8"/><circle cx="12" cy="12" r="1.8"/><circle cx="12" cy="19" r="1.8"/></svg>';
  const SVG_PLUS = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg>';
  const SVG_PLAY = '<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M8 5.2v13.6a1 1 0 001.5.87l11-6.8a1 1 0 000-1.74l-11-6.8A1 1 0 008 5.2z"/></svg>';

  // Sort spec for the Artist screen's Albums block. Each entry maps
  // the user-facing label, the picker hint, the inline glyph that
  // prefixes the tile metric (release_year and a_z reuse the year
  // and don't get a glyph), and which sorts skip the glyph entirely.
  const ALBUMS_SORTS = [
    { id: 'release_year',   label: 'Release year',   hint: 'Newest first',                       glyph: null },
    { id: 'time_listened',  label: 'Time listened',  hint: 'My total time on each album',        glyph: 'clock' },
    { id: 'popularity',     label: 'Popularity',     hint: 'Last.fm scrobble count',             glyph: 'plays' },
    { id: 'recently_added', label: 'Recently added', hint: 'When it landed in your library',     glyph: 'plus' },
    { id: 'a_z',            label: 'A–Z',            hint: 'Alphabetical, leading articles stripped', glyph: null },
  ];
  const ALBUMS_SORT_BY_ID = Object.fromEntries(ALBUMS_SORTS.map(s => [s.id, s]));
  const ALBUMS_SORT_GLYPHS = {
    clock:
      '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor"'
      + ' stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">'
      + '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>',
    plays:
      '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor"'
      + ' stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">'
      + '<path d="M4 20V9M10 20V4M16 20v-7M22 20v-4"/></svg>',
    plus:
      '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor"'
      + ' stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">'
      + '<path d="M12 5v14M5 12h14"/></svg>',
  };
  const ALBUMS_SORT_TRIGGER_SVG =
    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none"'
    + ' stroke="currentColor" stroke-width="1.8" stroke-linecap="round"'
    + ' stroke-linejoin="round">'
    + '<path d="M4 7h12M4 12h8M4 17h4"/>'
    + '<path d="M18 8v12M14 16l4 4 4-4"/></svg>';
  const ALBUMS_SORT_CHEV_SVG =
    '<svg width="12" height="12" viewBox="0 0 24 24" fill="none"'
    + ' stroke="currentColor" stroke-width="2.2" stroke-linecap="round"'
    + ' stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg>';
  const ALBUMS_SORT_CHECK_SVG =
    '<svg width="16" height="16" viewBox="0 0 24 24" fill="none"'
    + ' stroke="currentColor" stroke-width="2.4" stroke-linecap="round"'
    + ' stroke-linejoin="round"><path d="M5 13l4 4L19 7"/></svg>';

  async function _fetchAlbumsSort() {
    try {
      const r = await fetch('/api/settings/albums-sort');
      if (r.ok) {
        const data = await r.json();
        if (ALBUMS_SORT_BY_ID[data.sort]) return data.sort;
      }
    } catch (_) {}
    return 'release_year';
  }

  async function renderArtist(root, artistId) {
    root.innerHTML = '';
    const screen = document.createElement('div');
    screen.className = 'detail-screen';
    root.appendChild(screen);

    const sort = await _fetchAlbumsSort();
    let d;
    try {
      const url = '/api/artists/' + encodeURIComponent(artistId)
                  + '?sort=' + encodeURIComponent(sort);
      const resp = await fetch(url);
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      d = await resp.json();
    } catch (err) {
      screen.innerHTML = `<div class="placeholder-screen">
        <p class="placeholder-body">Artist not found.</p>
        <button class="legacy-link" onclick="history.back()">← Back</button>
      </div>`;
      return;
    }

    const ph = avatarPlaceholder(d.name || '?');
    // Initials backdrop is always rendered; the lazy-resolved photo
    // overlays it when the request succeeds, otherwise <img> removes
    // itself on error and the initials fallback stays visible.
    const heroFallback = `<div class="artist-hero-fallback"
        style="--cover-bg-1: ${ph.bg}; --cover-bg-2: var(--color-foundation);">${
          escapeHtml(ph.initials)}</div>`;
    const heroImg = d.id
      ? `${heroFallback}<img src="/api/covers/by-artist/${
          encodeURIComponent(d.id)}" alt="" onerror="this.remove()">`
      : heroFallback;

    const tagsHtml = (d.tags || [])
      .map(t => t.genre_id
        ? `<button class="tag-chip" type="button"
                   data-genre-id="${escapeHtml(t.genre_id)}">${
                     escapeHtml(t.name)}</button>`
        : `<span class="tag-chip">${escapeHtml(t.name)}</span>`)
      .join('');

    const sortSpec = ALBUMS_SORT_BY_ID[d.albums_sort] || ALBUMS_SORT_BY_ID.release_year;
    const glyphSvg = sortSpec.glyph ? ALBUMS_SORT_GLYPHS[sortSpec.glyph] || '' : '';
    const glyphHtml = glyphSvg ? `<span class="ic">${glyphSvg}</span>` : '';
    // Walk the (already group-sorted by the backend) album list once
    // and inject a `<span class="group-gap">` between the last
    // is_primary tile and the first feat. tile. Keeps the visual
    // grouping signal that role_priority drives on the server side.
    const albumsList = d.albums || [];
    const albumsHtml = albumsList.map((a, i) => {
      const c = coverPlaceholderColors(a.title || a.id);
      const url = coverUrl(a);
      const inner = url
        ? `<img src="${url}" alt="" loading="lazy"
                onerror="this.style.display='none'">`
        : `<div class="placeholder-badge"
              style="--cover-bg-1: ${c.bg1}; --cover-bg-2: ${c.bg2};">${
                escapeHtml(a.title || '')}</div>`;
      // is_primary === false = artist appears only as featured /
      // collaborator on this album; mark with a small "feat." badge
      // so the discography is honest without splitting into sections.
      const featBadge = a.is_primary === false
        ? '<span class="album-tile-feat">feat.</span>' : '';
      const prev = i > 0 ? albumsList[i - 1] : null;
      const gap = prev && prev.is_primary !== false && a.is_primary === false
        ? '<span class="group-gap" aria-hidden="true"></span>'
        : '';
      const metricRaw = (a.metric || '').toString();
      const hasMetric = !!metricRaw;
      const metricLine = hasMetric
        ? `${glyphHtml}${escapeHtml(metricRaw)}`
        : `${glyphHtml}—`;
      return `
        ${gap}
        <button class="album-tile" type="button" data-album-id="${escapeHtml(a.id)}">
          <div class="album-cover"
               style="--cover-bg-1: ${c.bg1}; --cover-bg-2: ${c.bg2};">${inner}${featBadge}</div>
          <div class="album-tile-title">${escapeHtml(a.title || '')}</div>
          <div class="album-tile-year${hasMetric ? '' : ' unavailable'}">${metricLine}</div>
        </button>`;
    }).join('');

    const tracksHtml = (d.popular_tracks || []).map((t, i) => `
      <button class="track-row" type="button"
              data-media-file-id="${escapeHtml(String(t.media_file_id || ''))}">
        <span class="track-rank">${i + 1}</span>
        <div class="track-info">
          <div class="track-title-line">${escapeHtml(t.title || '')}</div>
          <div class="track-artist-line">${escapeHtml(t.album || '')}</div>
        </div>
        <span class="track-dur">${fmtDuration(t.duration)}</span>
        <span class="track-add" aria-label="Add to queue">${SVG_PLUS}</span>
      </button>
    `).join('');

    const similarHtml = (d.similar_artists || []).map(s => {
      const sph = avatarPlaceholder(s.name || '?');
      const initialsBlock = `<div class="similar-avatar-fallback"
              style="--cover-bg-1: ${sph.bg}; --cover-bg-2: var(--color-foundation);">${
                escapeHtml(sph.initials)}</div>`;
      const inner = artistAvatarInner(s.id, initialsBlock, coverUrl(s));
      return `
        <button class="similar-artist" type="button" data-artist-id="${escapeHtml(s.id)}">
          <div class="similar-avatar">${inner}</div>
          <div class="similar-name">${escapeHtml(s.name || '')}</div>
        </button>`;
    }).join('');

    const bioSummary = trimLastFmTail(stripHtml(d.bio_summary || ''));
    const bioFull = trimLastFmTail(stripHtml(d.bio || ''));
    const initialBio = bioSummary || bioFull;
    const hasMoreBio = bioFull && bioSummary && bioFull.length > bioSummary.length;
    const bioHtml = initialBio
      ? `<p class="bio"><span class="bio-text">${escapeHtml(initialBio)}</span>${
          hasMoreBio ? '<span class="see-more"> See more&nbsp;▾</span>' : ''
        }</p>`
      : '';

    screen.innerHTML = `
      <div class="artist-hero">
        ${heroImg}
        <div class="artist-hero-scrim-top"></div>
        <div class="artist-hero-scrim-bottom"></div>
        <div class="artist-hero-controls">
          <button class="icon-btn" type="button" data-action="back" aria-label="Back">${SVG_BACK}</button>
        </div>
        <h1 class="artist-hero-name">${escapeHtml(d.name || '')}</h1>
      </div>
      <div style="height: calc(14 * var(--px));"></div>
      ${tagsHtml ? `<div class="tag-row">${tagsHtml}</div>` : ''}
      ${bioHtml}
      ${albumsHtml ? `
        <div class="section-sep"></div>
        <div class="section-head">
          <h3>Albums</h3>
          <button class="sort-trigger" type="button"
                  data-action="albums-sort"
                  aria-haspopup="dialog" aria-expanded="false"
                  aria-label="Sort albums by ${escapeHtml(sortSpec.label)}">
            <span class="glyph">${ALBUMS_SORT_TRIGGER_SVG}</span>
            <span class="label">${escapeHtml(sortSpec.label)}</span>
            <span class="chev">${ALBUMS_SORT_CHEV_SVG}</span>
          </button>
        </div>
        <div class="h-scroll">${albumsHtml}</div>
      ` : ''}
      ${tracksHtml ? `
        <div class="section-sep"></div>
        <div class="section-head"><h3>Popular tracks</h3></div>
        <div class="track-list">${tracksHtml}</div>
      ` : ''}
      ${similarHtml ? `
        <div class="section-sep"></div>
        <div class="section-head">
          <h3>Similar artists</h3>
        </div>
        <div class="similar-row">${similarHtml}</div>
      ` : ''}
      <div style="height: calc(24 * var(--px));"></div>
    `;

    if (hasMoreBio) {
      const bioP = screen.querySelector('.bio');
      const textSpan = bioP && bioP.querySelector('.bio-text');
      const seeMore = bioP && bioP.querySelector('.see-more');
      if (textSpan && seeMore) {
        let expanded = false;
        seeMore.addEventListener('click', e => {
          e.stopPropagation();
          expanded = !expanded;
          textSpan.textContent = expanded ? bioFull : bioSummary;
          seeMore.innerHTML = expanded
            ? ' See less&nbsp;▴'
            : ' See more&nbsp;▾';
        });
      }
    }

    const sortBtn = screen.querySelector('[data-action="albums-sort"]');
    if (sortBtn) {
      sortBtn.addEventListener('click', () => {
        openAlbumsSortPicker(d.albums_sort || 'release_year', async (picked) => {
          if (!picked || picked === d.albums_sort) return;
          try {
            await fetch('/api/settings/albums-sort', {
              method: 'PUT',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ sort: picked }),
            });
          } catch (_) {}
          // Re-fetch the whole artist payload so the tiles get the
          // metric formatted server-side for the new sort.
          renderArtist(root, artistId);
        });
      });
    }

    wireDetailHandlers(screen);
    updatePlayingHighlight();
  }

  function openAlbumsSortPicker(currentSort, onPick) {
    const overlay = document.createElement('div');
    overlay.className = 'confirm-overlay';
    overlay.setAttribute('role', 'dialog');
    overlay.setAttribute('aria-modal', 'true');
    const rows = ALBUMS_SORTS.map(s => `
      <button class="sort-row${s.id === currentSort ? ' active' : ''}"
              type="button"
              role="radio"
              aria-checked="${s.id === currentSort ? 'true' : 'false'}"
              data-sort-id="${escapeHtml(s.id)}">
        <div>
          <div class="sort-row-label">${escapeHtml(s.label)}</div>
          <div class="sort-row-hint">${escapeHtml(s.hint)}</div>
        </div>
        <span class="sort-check">${ALBUMS_SORT_CHECK_SVG}</span>
      </button>
    `).join('');
    overlay.innerHTML = `
      <div class="confirm-sheet">
        <div class="sheet-handle" aria-hidden="true"></div>
        <h4 class="sheet-title">Sort albums by</h4>
        <div class="sort-list">${rows}</div>
      </div>
    `;
    document.body.appendChild(overlay);
    const close = (picked) => {
      overlay.remove();
      document.removeEventListener('keydown', onKey);
      if (typeof onPick === 'function') onPick(picked);
    };
    const onKey = (e) => { if (e.key === 'Escape') close(null); };
    document.addEventListener('keydown', onKey);
    overlay.addEventListener('click', e => {
      if (e.target === overlay) close(null);
    });
    overlay.querySelectorAll('[data-sort-id]').forEach(btn => {
      btn.addEventListener('click', () => close(btn.dataset.sortId));
    });
  }

  // Human-readable label for an album_variant entry. Uses tech specs when
  // they uniquely identify the rip and falls back to the last segments of
  // the directory_path (which is UNIQUE on album_variants) when two
  // variants share specs — e.g. The Wall's two separate 96/24 rips
  // disambiguate via "[TR24]" vs "[Vinyl]" path tails.
  function variantLabel(v) {
    const sr = v.sample_rate ? (v.sample_rate / 1000).toFixed(v.sample_rate % 1000 === 0 ? 0 : 1) + ' kHz' : null;
    const bd = v.bit_depth ? v.bit_depth + '-bit' : null;
    const fmt = v.file_format || (v.is_lossless ? 'Lossless' : null);
    const specs = [sr, bd, fmt].filter(Boolean).join(' · ');
    const tailParts = (v.directory_path || '').split(/[\\/]/).filter(Boolean).slice(-2);
    const tail = tailParts.join('/');
    return specs && tail ? `${specs} · ${tail}` : (specs || tail || ('Variant ' + v.variant_id));
  }

  function openVariantPicker(variants, currentVid, onPick) {
    const overlay = document.createElement('div');
    overlay.className = 'confirm-overlay';
    overlay.setAttribute('role', 'dialog');
    overlay.setAttribute('aria-modal', 'true');
    const rows = variants.map(v => {
      const active = v.variant_id === currentVid;
      return `
        <button class="sort-row${active ? ' active' : ''}"
                type="button"
                role="radio"
                aria-checked="${active ? 'true' : 'false'}"
                data-variant-id="${v.variant_id}">
          <div>
            <div class="sort-row-label">${escapeHtml(variantLabel(v))}</div>
          </div>
          <span class="sort-check">${ALBUMS_SORT_CHECK_SVG}</span>
        </button>
      `;
    }).join('');
    overlay.innerHTML = `
      <div class="confirm-sheet">
        <div class="sheet-handle" aria-hidden="true"></div>
        <h4 class="sheet-title">Pick variant</h4>
        <div class="sort-list">${rows}</div>
      </div>
    `;
    document.body.appendChild(overlay);
    const close = (picked) => {
      overlay.remove();
      document.removeEventListener('keydown', onKey);
      if (typeof onPick === 'function' && picked !== null) onPick(picked);
    };
    const onKey = (e) => { if (e.key === 'Escape') close(null); };
    document.addEventListener('keydown', onKey);
    overlay.addEventListener('click', e => {
      if (e.target === overlay) close(null);
    });
    overlay.querySelectorAll('[data-variant-id]').forEach(btn => {
      btn.addEventListener('click', () => close(parseInt(btn.dataset.variantId, 10)));
    });
  }

  async function renderAlbum(root, albumId) {
    root.innerHTML = '';
    const screen = document.createElement('div');
    screen.className = 'detail-screen';
    root.appendChild(screen);

    // Local screen state. selectedVariantId is null until the user picks
    // a specific rip; the server then falls back to its DISTINCT ON
    // default (analysis-source preferred). On variant change we re-fetch
    // and re-render the whole screen — header/cover usually doesn't move
    // since covers are identical across variants of the same album, and
    // a clean rebuild keeps the wiring logic in one place.
    let selectedVariantId = null;

    const loadAndRender = async () => {
      let d;
      try {
        const url = '/api/albums/' + encodeURIComponent(albumId)
          + (selectedVariantId != null ? '?variant_id=' + selectedVariantId : '');
        const resp = await fetch(url);
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        d = await resp.json();
      } catch (err) {
        screen.innerHTML = `<div class="placeholder-screen">
          <p class="placeholder-body">Album not found.</p>
          <button class="legacy-link" onclick="history.back()">← Back</button>
        </div>`;
        return;
      }

      const c = coverPlaceholderColors(d.title || d.id);
      const heroUrl = coverUrl(d);
      const heroImg = heroUrl
        ? `<img src="${heroUrl}" alt="" onerror="this.style.display='none'">`
        : `<div class="album-hero-fallback"
              style="--cover-bg-1: ${c.bg1}; --cover-bg-2: ${c.bg2};"></div>`;

      const qual = d.quality || 'lossy';
      const qualLabel = qual === 'hi-res' ? 'Hi-Res'
        : qual === 'lossless' ? 'Lossless' : 'Lossy';
      const qualClass = qual === 'hi-res' ? 'is-hires'
        : qual === 'lossless' ? 'is-lossless' : 'is-lossy';

      const genresHtml = (d.genres || [])
        .map(g => `<button class="tag-chip" type="button"
                           data-genre-id="${escapeHtml(g.id)}">${escapeHtml(g.name)}</button>`)
        .join('');

      const tracksList = d.tracks || [];
      const hasMultipleDiscs = tracksList.some(t => t.disc_number && t.disc_number > 1);
      let lastDisc = null;
      const trackParts = [];
      for (const t of tracksList) {
        if (hasMultipleDiscs && t.disc_number && t.disc_number !== lastDisc) {
          lastDisc = t.disc_number;
          trackParts.push(`<div class="disc-header">Disc ${t.disc_number}</div>`);
        }
        const sub = [
          t.key ? (t.key + (modeShort(t.mode) ? ' ' + modeShort(t.mode) : '')) : null,
          t.bpm ? Math.round(t.bpm) + ' bpm' : null,
        ].filter(Boolean).join(' · ');
        trackParts.push(`
          <button class="track-row" type="button"
                  data-media-file-id="${escapeHtml(String(t.media_file_id || ''))}">
            <span class="track-rank">${t.track_number || ''}</span>
            <div class="track-info">
              <div class="track-title-line">${escapeHtml(t.title || '')}</div>
              ${sub ? `<div class="track-sub">${escapeHtml(sub)}</div>` : ''}
            </div>
            <span class="track-dur">${fmtDuration(t.duration)}</span>
            <span class="track-add" aria-label="Add to queue">${SVG_PLUS}</span>
          </button>
        `);
      }
      const tracksHtml = trackParts.join('');

      const artistName = d.primary_artist ? d.primary_artist.name : '';
      const artistId = d.primary_artist ? d.primary_artist.id : '';
      const totalDuration = fmtDurationLong(d.total_duration);

      // Variant selector — render only when >1 variant. variants[] is
      // pre-sorted by the API (lossless/highest-resolution first), so
      // variants[0] is the natural default when no variant is pinned.
      const variants = d.variants || [];
      let variantHtml = '';
      if (variants.length > 1) {
        const activeVid = selectedVariantId ?? variants[0].variant_id;
        const active = variants.find(v => v.variant_id === activeVid) || variants[0];
        variantHtml = `
          <button class="album-variant-toggle" type="button" data-action="pick-variant">
            <span class="variant-label">${escapeHtml(variantLabel(active))}</span>
            <span class="variant-chevron">▾</span>
          </button>
        `;
      }

      screen.innerHTML = `
        <div class="album-hero">
          ${heroImg}
          <div class="album-hero-scrim"></div>
          <div class="album-hero-controls">
            <button class="icon-btn" type="button" data-action="back" aria-label="Back">${SVG_BACK}</button>
          </div>
        </div>
        <div class="album-meta-block">
          <h1 class="album-title-line">${escapeHtml(d.title || '')}</h1>
          ${artistName ? `<button class="album-artist-line"
                                  style="background:none;border:0;padding:0;cursor:pointer;text-align:left;"
                                  data-artist-id="${escapeHtml(artistId)}">${escapeHtml(artistName)}</button>` : ''}
          ${variantHtml}
          <div class="album-meta-row">
            ${d.year ? `<span class="am-year">${d.year}</span><span class="am-dot"></span>` : ''}
            <span class="am-dur" style="margin-left: 0;">${totalDuration}</span>
            <span class="am-hires ${qualClass}" style="margin-left: auto;">${qualLabel}</span>
          </div>
          ${genresHtml ? `<div class="tag-row" style="padding: calc(12 * var(--px)) 0 0;">${genresHtml}</div>` : ''}
        </div>
        <div class="album-actions">
          <button class="btn-primary" type="button" data-action="play-all">${SVG_PLAY} Play all</button>
          <button class="btn-secondary album-queue-btn" type="button" data-action="queue-album">
            <span class="btn-icon">${SVG_PLUS}</span><span class="btn-label">Queue</span>
          </button>
        </div>
        <div class="album-tracklist">${tracksHtml}</div>
        <div style="height: calc(24 * var(--px));"></div>
      `;

      const pickBtn = screen.querySelector('[data-action="pick-variant"]');
      if (pickBtn) {
        pickBtn.addEventListener('click', () => {
          const currentVid = selectedVariantId ?? variants[0].variant_id;
          openVariantPicker(variants, currentVid, (newVid) => {
            if (newVid !== currentVid) {
              selectedVariantId = newVid;
              loadAndRender();
            }
          });
        });
      }

      wireDetailHandlers(screen, { albumId, tracks: d.tracks });
      updatePlayingHighlight();
    };

    await loadAndRender();
  }

  function wireDetailHandlers(screen, ctx = {}) {
    // Back chevron
    screen.querySelectorAll('[data-action="back"]').forEach(btn => {
      btn.addEventListener('click', () => history.back());
    });
    // Artist tile / similar artist / album-artist link
    screen.querySelectorAll('[data-artist-id]').forEach(el => {
      el.addEventListener('click', e => {
        e.stopPropagation();
        const id = el.getAttribute('data-artist-id');
        if (id) navigateToEntity('artist', id);
      });
    });
    // Album tile
    screen.querySelectorAll('[data-album-id]').forEach(el => {
      el.addEventListener('click', e => {
        e.stopPropagation();
        const id = el.getAttribute('data-album-id');
        if (id) navigateToEntity('album', id);
      });
    });
    // Genre chip — drills into the genre detail screen.
    screen.querySelectorAll('[data-genre-id]').forEach(el => {
      el.addEventListener('click', e => {
        e.stopPropagation();
        const id = el.getAttribute('data-genre-id');
        if (id) navigateToEntity('genre', id);
      });
    });
    // Track row → play track
    screen.querySelectorAll('[data-media-file-id]').forEach(el => {
      el.addEventListener('click', e => {
        if (e.target.closest('.track-add')) return;
        e.stopPropagation();
        const mfId = el.getAttribute('data-media-file-id');
        if (mfId && typeof window.playTrack === 'function') {
          window.playTrack(parseInt(mfId, 10));
        }
      });
    });
    // Track-add button → toggles an inline "Add to: [Next] [End]"
    // confirmation, matching the chat-row delete pattern. The same
    // `+` glyph plays the role of × in open state via a 45° rotate.
    // End appends via /queue-tracks. Next does the same plus a
    // /reorder call to slot the new track right after the current
    // one — HQPlayer has no insert primitive, so the seamless-
    // rebuild reorder is the cleanest path.
    screen.querySelectorAll('.track-add').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const row = btn.closest('[data-media-file-id]');
        if (!row) return;
        if (row.classList.contains('is-confirming')) {
          closeQueueConfirm(row);
        } else {
          openQueueConfirm(row);
        }
      });
    });
    // Play all (album) — replaces queue with full album
    screen.querySelectorAll('[data-action="play-all"]').forEach(btn => {
      btn.addEventListener('click', async () => {
        if (!ctx.tracks || !ctx.tracks.length) return;
        const ids = ctx.tracks.map(t => t.media_file_id).filter(Boolean);
        try {
          await fetch('/api/player/play-tracks', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ track_ids: ids }),
          });
        } catch (err) { console.warn('play-tracks failed', err); }
      });
    });
    // Queue album — opens the same inline "Add to: [Next] [End]"
    // confirm as a track row, except scoped to the album as a whole.
    // The Queue button itself plays the open/close role: CSS rotates
    // it 45° in confirming state. Play all is hidden under the bar
    // so the bar can stretch into its column.
    screen.querySelectorAll('[data-action="queue-album"]').forEach(btn => {
      btn.addEventListener('click', () => {
        if (!ctx.tracks || !ctx.tracks.length) return;
        const ids = ctx.tracks.map(t => t.media_file_id).filter(Boolean);
        if (!ids.length) return;
        const wrap = btn.closest('.album-actions');
        if (!wrap) return;
        if (wrap.classList.contains('is-confirming')) {
          closeAlbumQueueConfirm(wrap);
        } else {
          openAlbumQueueConfirm(wrap, ids);
        }
      });
    });
  }

  /* Inline "Add to: [Next] [End]" confirmation row. Mirrors the
     chat-row delete pattern. The same `+` button toggles the bar
     open/closed; CSS rotates it 45° in open state so the glyph
     visually morphs into ×, but the DOM node is unchanged. Bar
     content sits as a sibling in the row's grid and the non-bar
     non-`+` children are hidden via CSS, so no innerHTML swap is
     needed — keeps the original `.track-add` listener alive. */
  function closeQueueConfirm(row) {
    row.classList.remove('is-confirming');
    const bar = row.querySelector('.track-confirm-bar');
    if (bar) bar.remove();
  }

  function openQueueConfirm(row) {
    // mfId attribute name differs across call sites: detail screens
    // use data-media-file-id, Now Playing similar list uses
    // data-track-id. Same with the add-button class name (.track-add
    // vs .np-sim-add) — both are tagged with aria-label "Add to
    // queue" so a single query covers both. Keeping one function
    // shared means UX changes only need editing in one place.
    const mfId = parseInt(
      row.getAttribute('data-media-file-id')
        || row.getAttribute('data-track-id'),
      10,
    );
    if (!mfId) return;
    row.classList.add('is-confirming');

    const bar = document.createElement('div');
    bar.className = 'track-confirm-bar';
    bar.innerHTML = `
      <span class="track-confirm-ask">Add to:</span>
      <button class="track-confirm-btn" type="button" data-confirm="next">Next</button>
      <button class="track-confirm-btn" type="button" data-confirm="end">End</button>
    `;
    // Slot the bar before the add button so the grid lays out as
    // [bar 1fr][+ 44px] — the + stays at the right edge in the
    // same column it occupies when idle.
    const addBtn = row.querySelector('.track-add, .np-sim-add');
    if (addBtn) row.insertBefore(bar, addBtn);
    else row.appendChild(bar);

    bar.querySelector('[data-confirm="end"]').addEventListener('click', async e => {
      e.stopPropagation();
      try {
        await fetch('/api/player/queue-tracks', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ track_ids: [mfId] }),
        });
      } catch (err) { console.warn('queue-tracks (end) failed', err); }
      closeQueueConfirm(row);
    });
    bar.querySelector('[data-confirm="next"]').addEventListener('click', async e => {
      e.stopPropagation();
      try {
        // 1. Pull current playing index + freshest playlist so we
        //    can build the new order from a consistent snapshot.
        //    HQPlayer's track_index is 1-based; convert to JS index.
        const [statusResp, plResp] = await Promise.all([
          fetch('/api/player/status'),
          fetch('/api/player/playlist'),
        ]);
        const status = statusResp.ok ? await statusResp.json() : null;
        const pl = plResp.ok ? await plResp.json() : null;
        const tracks = (pl && pl.tracks) || [];
        const currentIdx = status && status.track_index
          ? status.track_index - 1
          : -1;
        // 2. Append the new track so HQPlayer has it in the
        //    playlist (the protocol has no insert primitive).
        await fetch('/api/player/queue-tracks', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ track_ids: [mfId] }),
        });
        // 3. Reorder: splice the just-appended track into the slot
        //    right after the currently playing one. /reorder takes
        //    the FULL new order including the current slot at its
        //    original position. If we don't know the current index
        //    (stopped / no playlist), we leave it at the end — same
        //    fallback as a plain End.
        if (currentIdx >= 0 && currentIdx < tracks.length) {
          const newOrder = tracks.map(t => t.id);
          newOrder.push(mfId);
          newOrder.splice(currentIdx + 1, 0, newOrder.pop());
          await fetch('/api/player/reorder', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ order: newOrder }),
          });
        }
      } catch (err) { console.warn('queue-tracks (next) failed', err); }
      closeQueueConfirm(row);
    });
  }

  /* Album-wide "Add to: [Next] [End]" confirm. Reuses the
     `.track-confirm-bar` markup; CSS hides Play all under
     `.album-actions.is-confirming` and rotates the Queue button
     45° so the same glyph plays the role of × (open ↔ close). */
  function closeAlbumQueueConfirm(wrap) {
    wrap.classList.remove('is-confirming');
    const bar = wrap.querySelector('.track-confirm-bar');
    if (bar) bar.remove();
  }

  function openAlbumQueueConfirm(wrap, ids) {
    wrap.classList.add('is-confirming');

    const bar = document.createElement('div');
    bar.className = 'track-confirm-bar';
    bar.innerHTML = `
      <span class="track-confirm-ask">Add album to:</span>
      <button class="track-confirm-btn" type="button" data-confirm="next">Next</button>
      <button class="track-confirm-btn" type="button" data-confirm="end">End</button>
    `;
    const queueBtn = wrap.querySelector('.album-queue-btn');
    if (queueBtn) wrap.insertBefore(bar, queueBtn);
    else wrap.appendChild(bar);

    bar.querySelector('[data-confirm="end"]').addEventListener('click', async e => {
      e.stopPropagation();
      try {
        await fetch('/api/player/queue-tracks', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ track_ids: ids }),
        });
      } catch (err) { console.warn('queue-album (end) failed', err); }
      closeAlbumQueueConfirm(wrap);
    });
    bar.querySelector('[data-confirm="next"]').addEventListener('click', async e => {
      e.stopPropagation();
      try {
        // Same approach as single-track Next: snapshot current
        // index + playlist, append the whole album, then reorder
        // so the appended block lands right after the current slot.
        const [statusResp, plResp] = await Promise.all([
          fetch('/api/player/status'),
          fetch('/api/player/playlist'),
        ]);
        const status = statusResp.ok ? await statusResp.json() : null;
        const pl = plResp.ok ? await plResp.json() : null;
        const tracks = (pl && pl.tracks) || [];
        const currentIdx = status && status.track_index
          ? status.track_index - 1
          : -1;
        await fetch('/api/player/queue-tracks', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ track_ids: ids }),
        });
        if (currentIdx >= 0 && currentIdx < tracks.length) {
          // The new ids were just appended in `ids` order. Build the
          // full new order: existing playlist + appended block,
          // then splice the appended block out and re-insert it
          // immediately after the current slot.
          const fullOrder = tracks.map(t => t.id).concat(ids);
          const tail = fullOrder.splice(fullOrder.length - ids.length, ids.length);
          fullOrder.splice(currentIdx + 1, 0, ...tail);
          await fetch('/api/player/reorder', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ order: fullOrder }),
          });
        }
      } catch (err) { console.warn('queue-album (next) failed', err); }
      closeAlbumQueueConfirm(wrap);
    });
  }

  /* ---------- Genre screen ----------
     No claude-design reference for this one; layout follows the
     Artist screen's pattern (hero band → name → tags → bio → row of
     entity tiles). Hero is one representative album cover blurred
     to a soft gradient — solves the "what image represents a genre"
     question without forcing a specific album/artist into the role. */

  async function renderGenre(root, genreId) {
    root.innerHTML = '';
    const screen = document.createElement('div');
    screen.className = 'detail-screen';
    root.appendChild(screen);

    let d;
    try {
      const resp = await fetch('/api/genres/' + encodeURIComponent(genreId));
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      d = await resp.json();
    } catch (err) {
      screen.innerHTML = `<div class="placeholder-screen">
        <p class="placeholder-body">Genre not found.</p>
        <button class="legacy-link" onclick="history.back()">← Back</button>
      </div>`;
      return;
    }

    const c = coverPlaceholderColors(d.name || d.id);
    const heroFallback = `<div class="genre-hero-fallback"
        style="--cover-bg-1: ${c.bg1}; --cover-bg-2: ${c.bg2};"></div>`;
    // Hero shows the photo of the top-ranked artist for the genre —
    // gives the screen a recognisable face. Lazy-resolved via the
    // existing /api/covers/by-artist endpoint, so the first visit
    // may briefly land on the gradient fallback before the photo
    // arrives. <img onerror="this.remove()"> reveals the gradient
    // permanently for genres whose top artist has no Last.fm photo.
    const topArtist = (d.artists && d.artists[0]) || null;
    const heroImg = topArtist
      ? `${heroFallback}<img class="genre-hero-bg"
            src="/api/covers/by-artist/${encodeURIComponent(topArtist.id)}"
            alt="" onerror="this.remove()">`
      : heroFallback;

    const stats = [
      d.track_count ? `${d.track_count} tracks` : null,
      d.album_count ? `${d.album_count} albums` : null,
      d.artist_count ? `${d.artist_count} artists` : null,
    ].filter(Boolean).join(' · ');

    const desc = (d.description || '').trim();
    // Genre descriptions from Last.fm tend to carry a "Read more on
    // Last.fm" tail and HTML; trimLastFmTail + stripHtml are reused
    // from the artist page for consistency.
    const cleanDesc = trimLastFmTail(stripHtml(desc));

    const artistsHtml = (d.artists || []).map(a => {
      const ph = avatarPlaceholder(a.name || '?');
      const initials = `<span class="artist-avatar-initials">${
        escapeHtml(ph.initials)}</span>`;
      return `
        <button class="artist-tile" type="button"
                data-artist-id="${escapeHtml(a.id)}">
          <div class="artist-avatar" style="background: ${ph.bg};">${
            artistAvatarInner(a.id, initials)
          }</div>
          <div class="artist-name">${escapeHtml(a.name || '')}</div>
        </button>`;
    }).join('');

    // Popular tracks for this genre — same row layout as the artist
    // page, but the secondary line shows "<artist> · <album>" so the
    // user can tell different artists apart in a genre-scoped list.
    const tracksHtml = (d.popular_tracks || []).map((t, i) => {
      const second = [t.artist, t.album].filter(Boolean).join(' · ');
      return `
        <button class="track-row" type="button"
                data-media-file-id="${escapeHtml(String(t.media_file_id || ''))}">
          <span class="track-rank">${i + 1}</span>
          <div class="track-info">
            <div class="track-title-line">${escapeHtml(t.title || '')}</div>
            <div class="track-artist-line">${escapeHtml(second)}</div>
          </div>
          <span class="track-dur">${fmtDuration(t.duration)}</span>
          <span class="track-add" aria-label="Add to queue">${SVG_PLUS}</span>
        </button>`;
    }).join('');

    screen.innerHTML = `
      <div class="genre-hero">
        ${heroImg}
        <div class="genre-hero-scrim"></div>
        <div class="artist-hero-controls">
          <button class="icon-btn" type="button" data-action="back" aria-label="Back">${SVG_BACK}</button>
        </div>
        <h1 class="genre-hero-name">${escapeHtml(d.name || '')}</h1>
      </div>
      <div style="height: calc(14 * var(--px));"></div>
      ${stats ? `<div class="genre-stats">${escapeHtml(stats)}</div>` : ''}
      ${cleanDesc ? `<p class="bio">${escapeHtml(cleanDesc)}</p>` : ''}
      ${tracksHtml ? `
        <div class="section-sep"></div>
        <div class="section-head"><h3>Popular tracks</h3></div>
        <div class="track-list">${tracksHtml}</div>
      ` : ''}
      ${artistsHtml ? `
        <div class="section-sep"></div>
        <div class="section-head">
          <h3>Artists</h3>
        </div>
        <div class="artists-grid">${artistsHtml}</div>
      ` : ''}
    `;

    screen.querySelector('[data-action="back"]')?.addEventListener('click', () => {
      history.back();
    });
    screen.querySelectorAll('.artist-tile[data-artist-id]').forEach(el => {
      el.addEventListener('click', () =>
        navigateToEntity('artist', el.getAttribute('data-artist-id')));
    });
    // Same play / queue handlers the artist + album screens use, so
    // a click on a Popular-tracks row plays through HQPlayer and the
    // '+' icon queues without leaving the page.
    wireDetailHandlers(screen);
    updatePlayingHighlight();
  }

  /* ---------- Friends screen ----------
     Rebuild of the legacy Friends section against the new design.
     Reference: docs/design/reference/claude-design-bundle/project/
     Session 4.html — frame 1 (Friends root). Backend endpoints
     (/api/p2p/account, /api/p2p/friends, /api/p2p/friends/add,
     /api/p2p/invite-by-email) are unchanged from the legacy UI.
     Chat thread lives in a follow-up increment. */

  const SVG_COPY = `
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none"
         stroke="currentColor" stroke-width="1.8" stroke-linecap="round"
         stroke-linejoin="round" aria-hidden="true">
      <rect x="9" y="9" width="11" height="11" rx="1.5"/>
      <path d="M5 15V5a1 1 0 011-1h10"/>
    </svg>`;
  const SVG_CHAT = `
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none"
         stroke="currentColor" stroke-width="1.7" stroke-linecap="round"
         stroke-linejoin="round" aria-hidden="true">
      <path d="M21 12a8 8 0 01-11.7 7.1L4 21l1.9-5.3A8 8 0 1121 12z"/>
    </svg>`;

  function lastSeenLabel(iso) {
    if (!iso) return '';
    const t = new Date(iso).getTime();
    if (!t) return '';
    const sec = Math.max(0, Math.floor((Date.now() - t) / 1000));
    if (sec < 60) return 'just now';
    if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
    if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
    if (sec < 86400 * 2) return 'yesterday';
    if (sec < 86400 * 7) return `${Math.floor(sec / 86400)}d ago`;
    return new Date(iso).toLocaleDateString();
  }

  // Window for "online" presence. last_seen is bumped on incoming
  // chat messages and on handshake (initial add + re-resolve via
  // LAN/DHT). 15 min is a forgiving middle ground: a peer who's
  // idle but launcher-running stays online across one missed
  // re-handshake; a peer whose launcher actually quit drops to
  // offline within ~15 min instead of looking stuck-online forever.
  function isOnline(iso) {
    if (!iso) return false;
    const t = new Date(iso).getTime();
    return t && (Date.now() - t) < 15 * 60 * 1000;
  }

  // Stable URL handle — public_key_hex is the same on every device
  // for the same friend, while friends.id is a per-device SERIAL.
  // 16 hex chars = 64 bits of identity, comfortably collision-free
  // within any realistic contact list.
  function friendUrlKey(friend) {
    const k = friend && friend.public_key_hex;
    return (typeof k === 'string' && k && !k.startsWith('pending:'))
      ? k.slice(0, 16) : '';
  }

  function renderFriendRow(friend) {
    const name = friend.display_name || friend.username || friend.invite_code || '?';
    const online = isOnline(friend.last_seen);
    const status = online
      ? `<div class="friend-status online"><span class="status-dot"></span>online</div>`
      : friend.last_seen
        ? `<div class="friend-status">last seen ${escapeHtml(lastSeenLabel(friend.last_seen))}</div>`
        : `<div class="friend-status">offline</div>`;
    const ph = avatarPlaceholder(name);
    const unread = Number(friend.unread_count) || 0;
    const badge = unread > 0
      ? `<span class="unread-badge">${unread > 99 ? '99+' : unread}</span>` : '';
    const key = friendUrlKey(friend);
    return `
      <div class="friend-row"
           data-friend-key="${escapeHtml(key)}">
        <div class="avatar friend-avatar" style="background: ${ph.bg};">${
          escapeHtml(ph.initials)}</div>
        <div class="friend-meta">
          <div class="friend-name">${escapeHtml(name)}</div>
          ${status}
        </div>
        <button class="chat-icon-btn" type="button" aria-label="chat">
          ${SVG_CHAT}${badge}
        </button>
      </div>`;
  }

  async function renderFriends(root) {
    const screen = document.createElement('div');
    screen.className = 'screen friends-screen';
    screen.innerHTML = `
      <header class="screen-head">
        <h1 class="screen-title">Friends</h1>
      </header>
      <div class="group-label">My identity</div>
      <div class="identity-card">
        <div>
          <div class="label">My invite code</div>
          <div class="code" id="myInviteCode">…</div>
        </div>
        <button class="copy-btn" type="button"
                aria-label="copy invite code" data-action="copy-invite">
          ${SVG_COPY}
        </button>
      </div>

      <div class="group-label">Add a friend</div>
      <div class="invite-form">
        <form class="input-row" data-action="add-by-code">
          <input class="text-field code" type="text" autocomplete="off"
                 placeholder="Paste invite code"
                 name="code">
          <button class="btn btn-primary" type="submit">Add</button>
        </form>
        <form class="input-row" data-action="add-by-email">
          <input class="text-field" type="email" autocomplete="off"
                 placeholder="Or invite by email" name="email">
          <button class="btn btn-secondary" type="submit">Send</button>
        </form>
      </div>
      <div class="hint" id="friendsHint">
        Add by invite code or by email.
      </div>

      <div class="group-label">Friends</div>
      <div class="friends-list" id="friendsList">
        <div class="friends-empty">Loading…</div>
      </div>
    `;
    root.appendChild(screen);

    const codeEl = screen.querySelector('#myInviteCode');
    const listEl = screen.querySelector('#friendsList');
    const hintEl = screen.querySelector('#friendsHint');
    const emailRow = screen.querySelector('[data-action="add-by-email"]');
    const emailInput = emailRow.querySelector('input[name="email"]');
    const emailBtn = emailRow.querySelector('button');

    // The hint doubles as a transient feedback line ("Adding…",
    // "Friend added.") and reverts to a steady context-aware text
    // afterwards. We compute that default once email status is known
    // so the user sees an honest reason if email invites are off.
    let defaultHint = 'Add by invite code or by email.';
    function showHint(text, transient) {
      hintEl.textContent = text;
      if (transient) {
        setTimeout(() => { hintEl.textContent = defaultHint; }, 2500);
      }
    }

    // Account + email-verification status in parallel; either failing
    // shouldn't block the other.
    const [acctResp, emailResp] = await Promise.all([
      fetch('/api/p2p/account').catch(() => null),
      fetch('/api/p2p/email/status').catch(() => null),
    ]);

    try {
      const data = acctResp && acctResp.ok ? await acctResp.json() : {};
      codeEl.textContent = data.invite_code || '— not set —';
    } catch (_) { codeEl.textContent = 'unavailable'; }

    try {
      const status = emailResp && emailResp.ok ? await emailResp.json() : {};
      // Email invites work in both directions: with a verified sender
      // the recipient sees a ✅ Verified badge, otherwise ⚠️ Unverified.
      // We never block — the recipient still gets the invite + intro
      // text either way; verification just buys trust. The hint
      // explains the difference so the user can decide whether to
      // set up email first.
      if (status.verified) {
        defaultHint =
          'Send invite — they get a verified email with a link.';
      } else {
        defaultHint = status.email
          ? 'Email not verified — recipients see an ⚠️ unverified badge. Verify in Settings to remove it.'
          : 'Without your email recipients see an ⚠️ unverified badge. Set a verified email in Settings for trust.';
      }
      hintEl.textContent = defaultHint;
    } catch (_) {
      // Status check failed; leave the inputs enabled — sending may
      // still work, the worker is the source of truth for delivery.
    }

    // SSE live updates: PG NOTIFY sautium_chat fires on incoming
    // messages, friend additions, and presence (last_seen) bumps;
    // backend forwards via /api/p2p/chat/stream as periodic
    // heartbeats. We refetch on each event so the list reflects
    // online/offline transitions without a manual reload.
    //
    // window.sseStream (auth.js) — not native EventSource. The
    // stream endpoint is HMAC-protected and EventSource can't add
    // a signature header, so the project ships a fetch-based
    // drop-in that goes through the signed-fetch monkey-patch.
    // Returns an AbortController; .abort() closes the stream.
    let sseCtrl = null;
    if (typeof window.sseStream === 'function') {
      sseCtrl = window.sseStream(
        '/api/p2p/chat/stream',
        () => {
          if (!document.contains(listEl)) {
            if (sseCtrl) { sseCtrl.abort(); sseCtrl = null; }
            return;
          }
          refreshFriends();
        },
        () => { /* sseStream auto-reconnects; nothing to do here */ },
      );
    }
    const onHashChange = () => {
      if (!document.contains(listEl)) {
        if (sseCtrl) { sseCtrl.abort(); sseCtrl = null; }
        window.removeEventListener('hashchange', onHashChange);
      }
    };
    window.addEventListener('hashchange', onHashChange);

    // Friends list.
    async function refreshFriends() {
      try {
        const r = await fetch('/api/p2p/friends');
        const friends = await r.json();
        if (!friends || friends.length === 0) {
          listEl.innerHTML = `<div class="friends-empty">
            No friends yet. Share your invite code or send an email.
          </div>`;
          return;
        }
        listEl.innerHTML = friends.map(renderFriendRow).join('');
        // Whole row is the tap target — the chat icon is a visual
        // affordance, not a separate button. A click on the icon
        // bubbles up to the row, no separate handler needed.
        listEl.querySelectorAll('.friend-row').forEach(row => {
          row.addEventListener('click', () => {
            const key = row.dataset.friendKey;
            // Pending invites have no resolved public_key_hex yet —
            // there's no chat thread to open until the handshake
            // completes. Surface a hint instead of routing to a
            // dead URL.
            if (!key) {
              showHint(
                'Waiting for handshake — you can chat once both sides accept.',
                true,
              );
              return;
            }
            navigateToEntity('chat', key);
          });
        });
      } catch (err) {
        listEl.innerHTML = `<div class="friends-empty">
          Could not load friends.</div>`;
      }
    }
    refreshFriends();

    // Pull pending email-invite acceptances. The Worker holds a
    // 30-day record of recipients who clicked our email link; this
    // endpoint auto-creates a friend entry locally for each one
    // and wakes the chat SSE on success. We also kick a manual
    // refresh on the `added` list — SSE may have only just
    // connected when the wake fires, and missing that one
    // notification would leave the new friend hidden until the
    // next live update or a manual reload.
    fetch('/api/p2p/pending-accepts')
      .then(r => r.ok ? r.json() : null)
      .then(data => {
        if (data && Array.isArray(data.added) && data.added.length > 0) {
          refreshFriends();
        }
      })
      .catch(() => {});

    // Copy invite code.
    screen.querySelector('[data-action="copy-invite"]').addEventListener('click', async () => {
      const code = codeEl.textContent;
      if (!code || code.startsWith('—') || code === 'unavailable') return;
      try {
        await navigator.clipboard.writeText(code);
        showHint('Invite code copied to clipboard.', true);
      } catch (_) { /* user denied or insecure context — silent */ }
    });

    // Add by code.
    screen.querySelector('[data-action="add-by-code"]').addEventListener('submit', async e => {
      e.preventDefault();
      const input = e.target.querySelector('input[name="code"]');
      const code = (input.value || '').trim();
      if (!code) return;
      showHint('Adding…', false);
      try {
        const resp = await fetch('/api/p2p/friends/add', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({invite_code: code}),
        });
        if (!resp.ok) {
          const err = await resp.json().catch(() => ({}));
          showHint(err.detail || 'Could not add friend.', true);
          return;
        }
        input.value = '';
        showHint('Friend added.', true);
        refreshFriends();
      } catch (err) {
        showHint('Network error.', true);
      }
    });

    // Invite by email.
    emailRow.addEventListener('submit', async e => {
      e.preventDefault();
      if (emailInput.disabled) return;
      const email = (emailInput.value || '').trim();
      if (!email) return;
      showHint('Sending…', false);
      try {
        const resp = await fetch('/api/p2p/invite-by-email', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({to_email: email}),
        });
        if (!resp.ok) {
          const err = await resp.json().catch(() => ({}));
          showHint(err.detail || 'Could not send invite.', true);
          return;
        }
        emailInput.value = '';
        showHint('Invite sent.', true);
      } catch (err) {
        showHint('Network error.', true);
      }
    });
  }

  /* ---------- Chat thread ----------
     Reference: docs/design/reference/claude-design-bundle/project/
     Session 4.html — frame 2. Routed as #friends/chat/<friend_id>;
     reuses existing /api/p2p/friends/{id}/messages and
     /api/p2p/friends/{id}/send. Live updates via the same
     window.sseStream channel as the Friends list — incoming
     messages, send confirmations and presence bumps all wake us. */

  const SVG_BACK_THIN = `
    <svg width="24" height="24" viewBox="0 0 24 24" fill="none"
         stroke="currentColor" stroke-width="1.8" stroke-linecap="round"
         stroke-linejoin="round" aria-hidden="true">
      <path d="M15 6l-6 6 6 6"/>
    </svg>`;
  const SVG_KEBAB_DOT = `
    <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor"
         aria-hidden="true">
      <circle cx="12" cy="5"  r="1.7"/>
      <circle cx="12" cy="12" r="1.7"/>
      <circle cx="12" cy="19" r="1.7"/>
    </svg>`;
  const SVG_LOCK = `
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none"
         stroke="currentColor" stroke-width="1.8" stroke-linecap="round"
         stroke-linejoin="round" aria-hidden="true">
      <rect x="4" y="11" width="16" height="10" rx="2"/>
      <path d="M8 11V7a4 4 0 018 0v4"/>
    </svg>`;
  // Match the AI chat's up-arrow glyph (#aiSendBtn in index.html) so
  // both chat surfaces share the same send affordance.
  const SVG_SEND = `
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none"
         stroke="currentColor" stroke-width="2.4" stroke-linecap="round"
         stroke-linejoin="round" aria-hidden="true">
      <path d="M12 19V5M5 12l7-7 7 7"/>
    </svg>`;

  function tsStampLabel(d) {
    const now = new Date();
    const sameDay = d.toDateString() === now.toDateString();
    const yest = new Date(now); yest.setDate(now.getDate() - 1);
    const isYesterday = d.toDateString() === yest.toDateString();
    const hh = String(d.getHours()).padStart(2, '0');
    const mm = String(d.getMinutes()).padStart(2, '0');
    if (sameDay) return `TODAY · ${hh}:${mm}`;
    if (isYesterday) return `YESTERDAY · ${hh}:${mm}`;
    return `${d.toLocaleDateString().toUpperCase()} · ${hh}:${mm}`;
  }

  function renderThreadHTML(messages) {
    if (!messages || messages.length === 0) {
      return `<div class="thread-empty">No messages yet — say hi.</div>`;
    }
    const out = [];
    let lastTs = null;
    for (const m of messages) {
      const ts = m.timestamp ? new Date(m.timestamp) : null;
      // Insert a date stamp on first message and whenever the gap
      // between consecutive messages crosses ~30 minutes — matches
      // the reference's day/time grouping.
      if (ts && (!lastTs || (ts - lastTs) > 30 * 60 * 1000)) {
        out.push(`<div class="ts-stamp">${escapeHtml(tsStampLabel(ts))}</div>`);
      }
      lastTs = ts || lastTs;
      const side = m.direction === 'in' ? 'them' : 'me';
      out.push(`<div class="msg-row ${side}">
        <div class="bubble ${side}">${escapeHtml(m.content || '')}</div>
      </div>`);
    }
    return out.join('');
  }

  async function renderChatThread(root, friendKey) {
    // friendKey is the first 16 hex chars of public_key_hex —
    // see friendUrlKey(). Resolve to the per-device friends.id at
    // mount time so backend API calls stay unchanged.
    const key = String(friendKey || '').toLowerCase();
    if (!key) { navigate('friends'); return; }
    let friend = null;
    try {
      const r = await fetch('/api/p2p/friends');
      const friends = await r.json();
      friend = (friends || []).find(f => {
        const k = f.public_key_hex || '';
        return !k.startsWith('pending:')
          && k.toLowerCase().startsWith(key);
      });
    } catch (_) { friend = null; }
    if (!friend) {
      // Stale bookmark / wiped DB / unresolved peer — drop back to
      // the friends list instead of showing an empty chat shell.
      navigate('friends');
      return;
    }
    const fid = friend.id;

    const screen = document.createElement('div');
    screen.className = 'screen chat-screen';
    screen.innerHTML = `
      <div class="chat-header">
        <button class="icon-btn" type="button" data-action="back" aria-label="back">
          ${SVG_BACK_THIN}
        </button>
        <div class="avatar avatar-32 friend-avatar" id="chatAvatar"></div>
        <div class="chat-header-meta">
          <div class="chat-header-name" id="chatName">…</div>
          <div class="chat-header-status" id="chatStatus"></div>
        </div>
        <button class="icon-btn" type="button" aria-label="more">
          ${SVG_KEBAB_DOT}
        </button>
      </div>
      <div class="chat-body-wrap">
        <div class="thread" id="chatThread">
          <div class="e2ee-banner">
            ${SVG_LOCK}
            <span>messages are end-to-end encrypted via
              <span class="e2ee-tag">NaCl</span></span>
          </div>
        </div>
        <form class="chat-input-row" id="chatForm">
          <input class="chat-input" type="text" autocomplete="off"
                 placeholder="Message…" name="msg">
          <button class="send-btn" type="submit" aria-label="send">
            ${SVG_SEND}
          </button>
        </form>
      </div>
    `;
    root.appendChild(screen);

    const threadEl = screen.querySelector('#chatThread');
    const nameEl = screen.querySelector('#chatName');
    const statusEl = screen.querySelector('#chatStatus');
    const avatarEl = screen.querySelector('#chatAvatar');
    const formEl = screen.querySelector('#chatForm');
    const inputEl = formEl.querySelector('input[name="msg"]');

    screen.querySelector('[data-action="back"]').addEventListener('click', () => {
      navigate('friends');
    });

    // Friend identity (name, avatar, online dot). The initial fetch
    // above already populated `friend`; loadFriend re-pulls on SSE
    // events so status updates without a full screen rebuild.
    async function loadFriend() {
      try {
        const r = await fetch('/api/p2p/friends');
        const friends = await r.json();
        friend = (friends || []).find(f => f.id === fid);
      } catch (_) { /* keep stale friend object on error */ }
      if (!friend) {
        nameEl.textContent = 'Unknown friend';
        statusEl.textContent = '';
        return;
      }
      const dn = friend.display_name || friend.username || friend.invite_code;
      nameEl.textContent = dn;
      const ph = avatarPlaceholder(dn);
      avatarEl.style.background = ph.bg;
      avatarEl.textContent = ph.initials;
      if (isOnline(friend.last_seen)) {
        statusEl.innerHTML = `<span class="status-dot"></span>online`;
        statusEl.style.color = 'var(--color-positive)';
      } else if (friend.last_seen) {
        statusEl.textContent =
          'last seen ' + lastSeenLabel(friend.last_seen);
        statusEl.style.color = 'var(--color-text-muted)';
      } else {
        statusEl.textContent = 'offline';
        statusEl.style.color = 'var(--color-text-muted)';
      }
    }

    async function loadMessages() {
      try {
        const r = await fetch(`/api/p2p/friends/${fid}/messages`);
        const msgs = await r.json();
        // Preserve the e2ee banner — replace only the message body.
        const banner = threadEl.querySelector('.e2ee-banner');
        threadEl.innerHTML = '';
        if (banner) threadEl.appendChild(banner);
        threadEl.insertAdjacentHTML('beforeend', renderThreadHTML(msgs));
        threadEl.scrollTop = threadEl.scrollHeight;
      } catch (err) {
        console.warn('Could not load messages', err);
      }
    }

    async function markRead() {
      try {
        await fetch(`/api/p2p/friends/${fid}/messages/read`, {method: 'POST'});
      } catch (_) { /* best effort */ }
    }

    formEl.addEventListener('submit', async e => {
      e.preventDefault();
      const text = (inputEl.value || '').trim();
      if (!text) return;
      inputEl.value = '';
      try {
        await fetch(`/api/p2p/friends/${fid}/send`, {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({content: text}),
        });
        // SSE will refresh; refetch immediately for snappy feel.
        loadMessages();
      } catch (err) { console.warn('Send failed', err); }
    });

    await loadFriend();
    await loadMessages();
    markRead();

    // SSE wakes us on incoming messages, send confirmations, and
    // presence bumps — same channel as the Friends list.
    let sseCtrl = null;
    if (typeof window.sseStream === 'function') {
      sseCtrl = window.sseStream(
        '/api/p2p/chat/stream',
        () => {
          if (!document.contains(threadEl)) {
            if (sseCtrl) { sseCtrl.abort(); sseCtrl = null; }
            return;
          }
          loadMessages();
          loadFriend();          // status may have changed
          markRead();
        },
        () => { /* sseStream handles reconnect */ },
      );
    }
    const onHashChange = () => {
      if (!document.contains(threadEl)) {
        if (sseCtrl) { sseCtrl.abort(); sseCtrl = null; }
        window.removeEventListener('hashchange', onHashChange);
      }
    };
    window.addEventListener('hashchange', onHashChange);
  }

  /* ---------- More tab ----------
     The "More" tab opens a bottom-drawer overlay over whatever
     screen is currently active — the underlying tab content stays
     visible behind a scrim, the AI FAB is suppressed, and tapping
     a row dismisses the drawer and navigates to that subsection's
     full-screen route (e.g. #<tab>/hqplayer). The drawer itself is
     not a route; URL hash stays put while open. Hitting #more/...
     directly (deep link) still works because renderMore dispatches
     to the leaf screen below. */

  function renderMore(root, hash) {
    const segs = (hash || '').split('/').filter(Boolean);
    const sub = segs[1] || '';
    if (sub === 'hqplayer') return renderHqplayerSettings(root);
    if (sub === 'profile') return renderProfile(root);
    if (sub === 'library') return renderLibrary(root);
    if (sub === 'ai')      return renderAI(root);
    if (sub === 'sync')    return renderSync(root);
    // Bare #more — nothing to render here; the drawer is the UI.
    // Drop back to home so the page isn't blank if the user
    // bookmarked the route.
    navigate('home');
  }

  // Drawer overlay (More tab handler). Built lazily on first open,
  // reused across opens, content (the connection-status chip on
  // the HQPlayer row) refreshed every time. Closed by tapping the
  // scrim, the More tab again, the handle, or any other tab.
  const moreDrawer = {
    el: null,
    isOpen: false,
    init() {
      const overlay = document.createElement('div');
      overlay.className = 'more-overlay';
      overlay.hidden = true;
      overlay.innerHTML = this._html();
      document.body.appendChild(overlay);
      this.el = overlay;
      overlay.querySelector('.more-scrim')
        .addEventListener('click', () => this.close());
      overlay.querySelector('.drawer-handle')
        .addEventListener('click', () => this.close());
      overlay.querySelectorAll('[data-go]').forEach(btn => {
        btn.addEventListener('click', () => {
          this.close();
          navigate(btn.dataset.go);
        });
      });
      overlay.querySelectorAll('.more-row[disabled]').forEach(btn => {
        btn.addEventListener('click', e => e.preventDefault());
      });
    },
    open() {
      if (!this.el) this.init();
      this.el.hidden = false;
      this.isOpen = true;
      // Highlight the More tab visually so the user knows the
      // drawer is "the More state" without changing route.
      document.querySelectorAll('.nav-tab').forEach(b => {
        if (b.getAttribute('data-route') === 'more') {
          b.setAttribute('aria-current', 'page');
        }
      });
      updateFabVisibility(currentRoute);
      this._refreshHqpStatus();
    },
    close() {
      if (!this.el) return;
      this.el.hidden = true;
      this.isOpen = false;
      // Restore the actual route's tab highlight.
      updateNavActive(currentRoute);
      updateFabVisibility(currentRoute);
    },
    toggle() { this.isOpen ? this.close() : this.open(); },
    async _refreshHqpStatus() {
      const hint = this.el && this.el.querySelector('#hqpHint');
      if (!hint) return;
      hint.className = 'more-hint';
      hint.textContent = '…';
      try {
        const r = await fetch('/api/hqplayer/state');
        if (r.ok) {
          const s = await r.json();
          if (s.connected) {
            hint.classList.add('ok');
            hint.innerHTML = '<span class="more-status-dot"></span>Connected';
          } else {
            hint.classList.add('off');
            hint.innerHTML = '<span class="more-status-dot off"></span>Offline';
          }
        }
      } catch (_) { /* leave hint as "…" — non-critical */ }
    },
    _html() {
      const ICON_HQP = `
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none"
             stroke="currentColor" stroke-width="1.7" stroke-linecap="round"
             stroke-linejoin="round" aria-hidden="true">
          <rect x="3" y="6" width="18" height="12" rx="2"/>
          <path d="M7 10v4M11 9v6M15 11v2M19 10v4"/>
        </svg>`;
      const ICON_PROFILE = `
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none"
             stroke="currentColor" stroke-width="1.7" stroke-linecap="round"
             stroke-linejoin="round" aria-hidden="true">
          <circle cx="12" cy="9" r="3.5"/>
          <path d="M5 20a7 7 0 0114 0"/>
        </svg>`;
      const ICON_LIBRARY = `
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none"
             stroke="currentColor" stroke-width="1.7" stroke-linecap="round"
             stroke-linejoin="round" aria-hidden="true">
          <path d="M9 17V5l11-2v12"/>
          <circle cx="6" cy="17" r="3"/>
          <circle cx="17" cy="15" r="3"/>
        </svg>`;
      const ICON_AI = `
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none"
             stroke="currentColor" stroke-width="1.6" stroke-linecap="round"
             stroke-linejoin="round" aria-hidden="true">
          <path d="M12 3l1.6 4 4 1.6-4 1.6L12 14l-1.6-3.8-4-1.6 4-1.6z"/>
          <path d="M19 14l.8 2 2 .8-2 .8L19 20l-.8-2.4-2-.8 2-.8z"/>
        </svg>`;
      const ICON_SYNC = `
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none"
             stroke="currentColor" stroke-width="1.7" stroke-linecap="round"
             stroke-linejoin="round" aria-hidden="true">
          <path d="M21 12a9 9 0 11-3-6.7M21 4v5h-5"/>
          <path d="M3 12a9 9 0 003 6.7M3 20v-5h5"/>
        </svg>`;
      const CHEV = `
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none"
             stroke="currentColor" stroke-width="2" stroke-linecap="round"
             stroke-linejoin="round" aria-hidden="true">
          <path d="M9 6l6 6-6 6"/>
        </svg>`;
      return `
        <div class="more-scrim"></div>
        <div class="drawer">
          <div class="drawer-handle"></div>
          <div class="drawer-title-row"><h1 class="drawer-title">More</h1></div>
          <div class="more-list">
            <button class="more-row" type="button" data-go="more/hqplayer">
              <span class="more-icon">${ICON_HQP}</span>
              <span class="more-label">HQPlayer</span>
              <span class="more-hint" id="hqpHint">…</span>
              <span class="more-chev">${CHEV}</span>
            </button>
            <button class="more-row" type="button" data-go="more/profile">
              <span class="more-icon">${ICON_PROFILE}</span>
              <span class="more-label">Profile</span>
              <span class="more-hint"></span>
              <span class="more-chev">${CHEV}</span>
            </button>
            <button class="more-row" type="button" data-go="more/library">
              <span class="more-icon">${ICON_LIBRARY}</span>
              <span class="more-label">Library</span>
              <span class="more-hint"></span>
              <span class="more-chev">${CHEV}</span>
            </button>
            <button class="more-row" type="button" data-go="more/ai">
              <span class="more-icon">${ICON_AI}</span>
              <span class="more-label">AI assistant</span>
              <span class="more-hint"></span>
              <span class="more-chev">${CHEV}</span>
            </button>
            <button class="more-row" type="button" data-go="more/sync">
              <span class="more-icon">${ICON_SYNC}</span>
              <span class="more-label">Sync &amp; P2P</span>
              <span class="more-hint"></span>
              <span class="more-chev">${CHEV}</span>
            </button>
          </div>
          <div class="drawer-foot">SAUTIUM</div>
        </div>
      `;
    },
  };

  /* ---------- HQPlayer settings ----------
     One screen at #more/hqplayer that consolidates the settings
     audiophiles actually touch: Connection (read-only — edited in
     the launcher Wizard), Output (Mode/Rate/Bits), Volume (read,
     adjusted in Now Playing), Filter (with favourites + a modal
     full-list picker), Matrix profile (only when profiles exist),
     and Advanced (collapsed shaper). State is fetched on mount;
     manual refresh button picks up changes made via HQP Desktop. */

  function fmtRateLabel(hz) {
    if (!hz) return '—';
    // PCM range — straight kHz (44.1 kHz … 768 kHz). The audiophile
    // convention switches at the DSD line, where rates are read as
    // base × multiplier (DSD64 = 44.1k × 64) instead of raw MHz.
    if (hz <= 768000) {
      if (hz % 1000 === 0) return (hz / 1000) + ' kHz';
      return (hz / 1000).toFixed(1) + ' kHz';
    }
    // 32k base appears too — HQP exposes 32k × 64/128/256/512 for
    // DACs that prefer the lower base (2.048, 4.096, 8.192,
    // 16.384 MHz). 22.05k family covers some legacy / mastering
    // rates. Multiplier must be a clean power of two ≥ 64 for the
    // base × N naming to be honest; anything else falls through.
    const labels = {44100: '44.1k', 48000: '48k', 32000: '32k', 22050: '22.05k'};
    for (const base of [44100, 48000, 32000, 22050]) {
      if (hz % base === 0) {
        const mult = hz / base;
        if (mult >= 64 && (mult & (mult - 1)) === 0) {
          return `${labels[base]} × ${mult}`;
        }
      }
    }
    const mhz = hz / 1_000_000;
    return (mhz % 1 === 0 ? mhz.toFixed(0) : mhz.toFixed(2)) + ' MHz';
  }

  function fmtVolume(db) {
    if (db === 0) return '0 dB';
    const sign = db > 0 ? '+' : '';
    return sign + db.toFixed(1) + ' dB';
  }

  function modeNameFromState(s, modes) {
    if (!s || !modes) return '—';
    const m = modes.find(x => x.index === s.mode);
    return m ? m.name : '—';
  }

  function rateNameFromState(s, rates) {
    if (!s || !rates || !rates.length) return '—';
    const r = rates.find(x => x.index === s.rate);
    return r ? fmtRateLabel(r.rate) : '—';
  }

  function filterNameFromIndex(idx, filters) {
    if (!filters) return '—';
    const f = filters.find(x => x.index === idx);
    return f ? f.name : '—';
  }

  function shaperNameFromIndex(idx, shapers) {
    if (!shapers) return '—';
    const s = shapers.find(x => x.index === idx);
    return s ? s.name : '—';
  }

  // Filters are named by family-prefix (poly-sinc-*, minring-*, IIR,
  // FIR, etc). Group by the leading dotted/dashed token so the
  // picker sheet has a usable hierarchy.
  function filterFamily(name) {
    if (!name) return 'Other';
    const parts = name.split('-');
    if (parts.length >= 2) {
      // Two-token families: "poly-sinc", "minring-XX", "TPDF-…".
      const head = parts[0].toLowerCase();
      if (head === 'poly' && parts[1] === 'sinc') return 'poly-sinc';
      return parts[0];
    }
    return name;
  }

  async function renderHqplayerSettings(root) {
    root.innerHTML = '';
    const screen = document.createElement('div');
    screen.className = 'screen hqp-screen';
    root.appendChild(screen);
    screen.innerHTML = `
      <header class="hqp-head">
        <button class="icon-btn" type="button" data-action="back" aria-label="Back">${SVG_BACK}</button>
        <h1 class="hqp-title">HQPlayer</h1>
        <button class="icon-btn" type="button" data-action="refresh" aria-label="Refresh">
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none"
               stroke="currentColor" stroke-width="1.7" stroke-linecap="round"
               stroke-linejoin="round" aria-hidden="true">
            <path d="M21 12a9 9 0 11-3-6.7L21 8"/>
            <path d="M21 3v5h-5"/>
          </svg>
        </button>
      </header>
      <div class="hqp-body" id="hqpBody">
        <div class="hqp-loading">Loading…</div>
      </div>
    `;
    screen.querySelector('[data-action="back"]').addEventListener('click', () => {
      navigate('more');
    });
    screen.querySelector('[data-action="refresh"]').addEventListener('click', () => {
      load();
    });

    let lastState = null;

    async function load() {
      const body = screen.querySelector('#hqpBody');
      // First-time render shows the Loading placeholder; subsequent
      // refreshes (filter pick, refresh button) keep current content
      // visible while the fetch is in flight so the screen doesn't
      // flash to "Loading…" and back. Without this guard, every
      // small DSP change strobed the entire panel.
      if (!body.firstElementChild) {
        body.innerHTML = `<div class="hqp-loading">Loading…</div>`;
      }
      let s;
      try {
        // cache: 'no-store' so the connection state reflects what the
        // backend just resolved — without it Chrome serves a stale GET
        // after a PUT /api/settings/hqplayer reconfig, and the UI
        // keeps saying "connected" even when the socket is gone.
        const r = await fetch('/api/hqplayer/state', { cache: 'no-store' });
        if (!r.ok) throw new Error('HTTP ' + r.status);
        s = await r.json();
      } catch (err) {
        body.innerHTML = `<div class="hqp-error">Could not reach HQPlayer.</div>`;
        return;
      }
      lastState = s;
      renderBody(body, s);
    }

    function renderBody(body, s) {
      const st = s.state || {};
      const modeName = modeNameFromState(st, s.modes);
      const rateName = rateNameFromState(st, s.rates);
      const filterName = filterNameFromIndex(st.filter, s.filters);
      const shaperName = shaperNameFromIndex(st.shaper, s.shapers);
      const matrixActive = st.matrix_profile || '';
      const profiles = s.matrix_profiles || [];
      const favourites = s.favorite_filters || [];
      // HQP Desktop labels the same Shaper control differently
      // depending on output mode: "Dither" in PCM mode, "Modulator"
      // in SDM. Mirror the language so the screen feels native to
      // anyone coming from the Desktop UI.
      const modeUpper = (modeName || '').toUpperCase();
      const isPcm = modeUpper === 'PCM';
      const isSdm = modeUpper.includes('SDM') || modeUpper.includes('DSD');
      const shaperLabel = isPcm ? 'Dither' : isSdm ? 'Modulator' : 'Shaper';

      const connBlock = s.connected
        ? `<div class="hqp-conn ok is-clickable" data-action="edit-conn" role="button" tabindex="0">
             <span class="hqp-conn-dot"></span>
             <div class="hqp-conn-text">
               <div class="hqp-conn-host">${escapeHtml(s.host)}:${s.port}</div>
               <div class="hqp-conn-sub">${
                 escapeHtml((s.info && (s.info.product || '')) || 'Connected')
               }${s.info && s.info.version ? ' · ' + escapeHtml(s.info.version) : ''}</div>
             </div>
           </div>`
        : `<div class="hqp-conn err is-clickable" data-action="edit-conn" role="button" tabindex="0">
             <span class="hqp-conn-dot"></span>
             <div class="hqp-conn-text">
               <div class="hqp-conn-host">${escapeHtml(s.host)}:${s.port}</div>
               <div class="hqp-conn-sub">Disconnected — tap to change host or port</div>
             </div>
           </div>`;

      const modeOptions = (s.modes || []).map(m =>
        `<option value="${m.index}"${m.index === st.mode ? ' selected' : ''}>${
          escapeHtml(m.name)}</option>`).join('');

      // HQPlayer's Control Protocol exposes only the "auto / [source]"
      // rate slot via GetRates — the explicit rate menu shown in HQP
      // Desktop isn't reachable from here. When the list is just the
      // auto entry we show active_rate (the actual current output)
      // as a read-only readout instead of a one-option dropdown.
      const realRates = (s.rates || []).filter(r => r.rate > 0);
      const showRateDropdown = realRates.length > 1;
      const rateOptions = (s.rates || []).map(r =>
        `<option value="${r.index}"${r.index === st.rate ? ' selected' : ''}>${
          escapeHtml(r.rate ? fmtRateLabel(r.rate) : 'Auto')}</option>`).join('');
      const activeRateLabel = st.active_rate
        ? fmtRateLabel(st.active_rate)
        : '—';

      const shaperOptions = (s.shapers || []).map(sh =>
        `<option value="${sh.index}"${sh.index === st.shaper ? ' selected' : ''}>${
          escapeHtml(sh.name)}</option>`).join('');

      const matrixOptions = profiles.length
        ? profiles.map(p =>
            `<option value="${escapeHtml(p)}"${p === matrixActive ? ' selected' : ''}>${
              escapeHtml(p)}</option>`).join('')
        : '';

      // Active filter row + the favourites strip below it. Tap on the
      // active filter or the [All filters] button opens the modal.
      const favouritesStrip = favourites.length
        ? `<div class="hqp-fav-strip">${favourites.map(name => {
            const isCurrent = name === filterName;
            return `<button type="button" class="hqp-fav-chip${
              isCurrent ? ' is-current' : ''}" data-fav="${escapeHtml(name)}">
              <span class="hqp-fav-star">★</span>
              <span class="hqp-fav-name">${escapeHtml(name)}</span>
            </button>`;
          }).join('')}</div>`
        : '';

      // Bits aren't directly exposed by the Control Protocol — leave
      // the row labelled "Bits" empty for now; it's read-only and
      // rarely interesting once Mode/Rate are picked.
      body.innerHTML = `
        <section class="hqp-section">
          <div class="hqp-section-label">Connection</div>
          ${connBlock}
        </section>

        <section class="hqp-section">
          <div class="hqp-section-label">Output</div>
          <div class="hqp-row">
            <label class="hqp-row-label" for="hqpMode">Mode</label>
            <select class="hqp-select" id="hqpMode" data-knob="mode">${modeOptions}</select>
          </div>
          <div class="hqp-row">
            <span class="hqp-row-label">Rate</span>
            ${showRateDropdown
              ? `<select class="hqp-select" id="hqpRate" data-knob="rate">${rateOptions}</select>`
              : `<span class="hqp-row-value mono">${escapeHtml(activeRateLabel)}</span>`}
          </div>
          <div class="hqp-row hqp-volume-row">
            <span class="hqp-row-label">Volume</span>
            <div class="hqp-volume-ctrl">
              <button class="hqp-vol-btn" type="button" data-vol="-1" aria-label="Volume -1 dB">−</button>
              <span class="hqp-vol-value mono">${fmtVolume(Number(st.volume) || 0)}</span>
              <button class="hqp-vol-btn" type="button" data-vol="+1" aria-label="Volume +1 dB">+</button>
            </div>
          </div>
        </section>

        <section class="hqp-section">
          <div class="hqp-section-label">Filter</div>
          <button class="hqp-row hqp-row-tap" type="button" data-action="open-filter">
            <span class="hqp-row-label">Active</span>
            <span class="hqp-row-value">${escapeHtml(filterName)}</span>
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none"
                 stroke="currentColor" stroke-width="1.6"
                 stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <path d="M9 6l6 6-6 6"/>
            </svg>
          </button>
          ${favouritesStrip}
          <div class="hqp-row">
            <label class="hqp-row-label" for="hqpShaper">${escapeHtml(shaperLabel)}</label>
            <select class="hqp-select" id="hqpShaper" data-knob="shaper">${shaperOptions}</select>
          </div>
        </section>

        ${profiles.length ? `
        <section class="hqp-section">
          <div class="hqp-section-label">Matrix profile</div>
          <div class="hqp-row">
            <label class="hqp-row-label" for="hqpMatrix">Active</label>
            <select class="hqp-select" id="hqpMatrix" data-knob="matrix_profile">
              <option value=""${matrixActive ? '' : ' selected'}>(none)</option>
              ${matrixOptions}
            </select>
          </div>
        </section>
        ` : ''}
      `;

      // Wire dropdowns: each select calls /config with one knob.
      body.querySelectorAll('select[data-knob]').forEach(sel => {
        sel.addEventListener('change', async () => {
          const knob = sel.dataset.knob;
          const raw = sel.value;
          const payload = {};
          payload[knob] = (knob === 'matrix_profile') ? raw : parseInt(raw, 10);
          if (knob === 'matrix_profile' && raw === '') return;
          try {
            const r = await fetch('/api/hqplayer/config', {
              method: 'POST',
              headers: {'Content-Type': 'application/json'},
              body: JSON.stringify(payload),
            });
            const out = await r.json();
            if (!r.ok || !out.ok) {
              console.warn('hqp config rejected:', out);
            }
          } catch (err) { console.warn('hqp config failed', err); }
          // Reload to reconcile (server-side change may cascade —
          // e.g. mode flip changes filter availability).
          load();
        });
      });

      body.querySelectorAll('.hqp-fav-chip').forEach(btn => {
        btn.addEventListener('click', async () => {
          const name = btn.dataset.fav;
          const filt = (s.filters || []).find(f => f.name === name);
          if (!filt) return;
          try {
            await fetch('/api/hqplayer/config', {
              method: 'POST',
              headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({filter: filt.index, filter1x: filt.index}),
            });
          } catch (err) { console.warn('filter switch failed', err); }
          load();
        });
      });

      body.querySelectorAll('[data-vol]').forEach(btn => {
        btn.addEventListener('click', async () => {
          const delta = parseFloat(btn.dataset.vol);
          try {
            await fetch('/api/hqplayer/volume', {
              method: 'POST',
              headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({delta}),
            });
          } catch (err) { console.warn('volume nudge failed', err); }
          load();
        });
      });

      const connEl = body.querySelector('[data-action="edit-conn"]');
      if (connEl) {
        connEl.addEventListener('click', () => {
          openHqpConnectionEditor({host: s.host, port: s.port}, () => load());
        });
      }

      body.querySelector('[data-action="open-filter"]').addEventListener('click', () => {
        openFilterPicker(s, async (chosen) => {
          try {
            await fetch('/api/hqplayer/config', {
              method: 'POST',
              headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({filter: chosen.index, filter1x: chosen.index}),
            });
          } catch (err) { console.warn('filter switch failed', err); }
          load();
        });
      });
    }

    load();
  }

  // Modal sheet: full filter list, search box, sections (Favourites
  // first, then by family). Sized to fill most of the viewport so
  // the user can scan 77 entries comfortably.
  function openFilterPicker(state, onPick) {
    const sheet = document.createElement('div');
    sheet.className = 'hqp-sheet';
    sheet.innerHTML = `
      <div class="hqp-sheet-bar">
        <button class="icon-btn" type="button" data-action="close" aria-label="Close">
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none"
               stroke="currentColor" stroke-width="1.8" stroke-linecap="round"
               stroke-linejoin="round" aria-hidden="true">
            <path d="M6 6l12 12M18 6L6 18"/>
          </svg>
        </button>
        <h2 class="hqp-sheet-title">Filter</h2>
        <input class="hqp-sheet-search" type="search" placeholder="Search…">
      </div>
      <div class="hqp-sheet-body" id="hqpSheetBody"></div>
    `;
    document.body.appendChild(sheet);

    const filters = state.filters || [];
    const favs = new Set(state.favorite_filters || []);
    const currentIdx = (state.state && state.state.filter) ?? -1;
    const search = sheet.querySelector('.hqp-sheet-search');
    const body = sheet.querySelector('#hqpSheetBody');

    function paint() {
      const q = (search.value || '').trim().toLowerCase();
      const filtered = q
        ? filters.filter(f => f.name.toLowerCase().includes(q))
        : filters.slice();

      const groups = new Map();
      // Favourites group is synthesised from the filtered set so
      // search applies uniformly; appears first when non-empty.
      const favRows = filtered.filter(f => favs.has(f.name));
      if (favRows.length) groups.set('★ Favourites', favRows);
      for (const f of filtered) {
        if (favs.has(f.name)) continue;
        const fam = filterFamily(f.name);
        if (!groups.has(fam)) groups.set(fam, []);
        groups.get(fam).push(f);
      }

      if (!groups.size) {
        body.innerHTML = `<div class="hqp-sheet-empty">No filters match.</div>`;
        return;
      }

      body.innerHTML = Array.from(groups.entries()).map(([fam, rows]) => `
        <div class="hqp-sheet-group">
          <div class="hqp-sheet-group-label">${escapeHtml(fam)}</div>
          ${rows.map(r => `
            <div class="hqp-sheet-row${
              r.index === currentIdx ? ' is-current' : ''}" data-idx="${r.index}">
              <button class="hqp-sheet-row-name" type="button" data-pick="${r.index}">
                ${r.index === currentIdx ? '<span class="hqp-sheet-dot"></span>' : ''}
                ${escapeHtml(r.name)}
              </button>
              <button class="hqp-sheet-star${
                favs.has(r.name) ? ' is-on' : ''}" type="button"
                      data-fav="${escapeHtml(r.name)}" aria-label="Toggle favourite">
                ★
              </button>
            </div>
          `).join('')}
        </div>
      `).join('');

      body.querySelectorAll('[data-pick]').forEach(btn => {
        btn.addEventListener('click', () => {
          const idx = parseInt(btn.dataset.pick, 10);
          const f = filters.find(x => x.index === idx);
          close();
          if (f) onPick(f);
        });
      });
      body.querySelectorAll('[data-fav]').forEach(btn => {
        btn.addEventListener('click', async (e) => {
          e.stopPropagation();
          const name = btn.dataset.fav;
          const action = favs.has(name) ? 'remove' : 'add';
          if (action === 'add') favs.add(name); else favs.delete(name);
          btn.classList.toggle('is-on');
          try {
            await fetch('/api/hqplayer/favorites/filter', {
              method: 'POST',
              headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({name, action}),
            });
          } catch (err) {
            console.warn('favourite update failed', err);
          }
        });
      });
    }

    function close() {
      sheet.remove();
    }

    sheet.querySelector('[data-action="close"]').addEventListener('click', close);
    search.addEventListener('input', paint);
    paint();
  }

  /* =====================================================================
   * Profile + Audio chain (#more/profile)
   * Mirrors docs/design/reference/claude-design-bundle/project/
   * Profile - Gear sheet.html. Three sub-views: own profile under the
   * More tab, peer profile under #profile/<key>, gear-item sheet
   * (overlay opened from a gear row).
   * ===================================================================== */

  const PROFILE_ICONS = {
    back:  '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M15 6l-6 6 6 6"/></svg>',
    edit:  '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M14 4l6 6L8 22H2v-6L14 4z"/></svg>',
    chev:  '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 6l6 6-6 6"/></svg>',
    pin:   '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s-7-6.5-7-12a7 7 0 1114 0c0 5.5-7 12-7 12z"/><circle cx="12" cy="10" r="2.5"/></svg>',
    check: '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M5 13l4 4L19 7"/></svg>',
    close: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M6 6l12 12M18 6L6 18"/></svg>',
  };

  /* ISO 3166-1 alpha-2 country codes. Country names rendered via
     Intl.DisplayNames so we don't carry a localisation table in JS. */
  const COUNTRY_CODES = [
    'AD','AE','AF','AG','AI','AL','AM','AO','AR','AT','AU','AW','AZ',
    'BA','BB','BD','BE','BF','BG','BH','BI','BJ','BM','BN','BO','BR','BS','BT','BW','BY','BZ',
    'CA','CD','CF','CG','CH','CI','CL','CM','CN','CO','CR','CU','CV','CY','CZ',
    'DE','DJ','DK','DM','DO','DZ',
    'EC','EE','EG','ER','ES','ET',
    'FI','FJ','FM','FR',
    'GA','GB','GD','GE','GH','GM','GN','GQ','GR','GT','GW','GY',
    'HK','HN','HR','HT','HU',
    'ID','IE','IL','IN','IQ','IR','IS','IT',
    'JM','JO','JP',
    'KE','KG','KH','KI','KM','KN','KP','KR','KW','KZ',
    'LA','LB','LC','LI','LK','LR','LS','LT','LU','LV','LY',
    'MA','MC','MD','ME','MG','MH','MK','ML','MM','MN','MR','MT','MU','MV','MW','MX','MY','MZ',
    'NA','NE','NG','NI','NL','NO','NP','NR','NZ',
    'OM',
    'PA','PE','PG','PH','PK','PL','PT','PW','PY',
    'QA',
    'RO','RS','RU','RW',
    'SA','SB','SC','SD','SE','SG','SI','SK','SL','SM','SN','SO','SR','SS','ST','SV','SY','SZ',
    'TD','TG','TH','TJ','TL','TM','TN','TO','TR','TT','TV','TW','TZ',
    'UA','UG','US','UY','UZ',
    'VA','VC','VE','VN','VU',
    'WS',
    'YE',
    'ZA','ZM','ZW',
  ];
  const _countryDN = (() => {
    try { return new Intl.DisplayNames(['en'], { type: 'region' }); }
    catch (_) { return null; }
  })();
  function countryName(code) {
    if (!code) return '';
    if (_countryDN) {
      try { return _countryDN.of(code) || code; } catch (_) {}
    }
    return code;
  }
  // Sorted once at script init so the editor dropdown is alphabetical.
  const COUNTRY_OPTIONS = COUNTRY_CODES
    .map(c => ({ code: c, name: countryName(c) }))
    .sort((a, b) => a.name.localeCompare(b.name));

  const GEAR_CATEGORIES = [
    { id: 'headphones', label: 'Headphones' },
    { id: 'iems',       label: 'IEMs' },
    { id: 'dac',        label: 'DAC' },
    { id: 'amp',        label: 'Amp' },
    { id: 'player',     label: 'Player' },
    { id: 'streamer',   label: 'Streamer' },
    { id: 'power',      label: 'Power' },
    { id: 'cable',      label: 'Cable' },
  ];
  function categoryLabel(id) {
    const c = GEAR_CATEGORIES.find(x => x.id === id);
    return c ? c.label : id;
  }

  function escapeProfileHtml(s) {
    if (s == null) return '';
    return String(s).replace(/[&<>"']/g, c => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
  }

  function statusBadgeHTML(status) {
    const map = {
      own:              { cls: 'badge-own',  txt: 'Own' },
      want:             { cls: 'badge-want', txt: 'Want' },
      sell:             { cls: 'badge-sell', txt: 'Sell' },
      previously_owned: { cls: 'badge-prev', txt: 'Previously-owned' },
    };
    const m = map[status] || map.own;
    return `<span class="badge ${m.cls}">${m.txt}</span>`;
  }

  function humanizeSpecKey(key) {
    if (!key) return '';
    return key.replace(/_/g, ' ')
              .replace(/\b\w/g, c => c.toUpperCase())
              .replace(/\bOhm\b/i, 'Ω')
              .replace(/\bDb\b/i, 'dB')
              .replace(/\bKhz\b/i, 'kHz')
              .replace(/\bMhz\b/i, 'MHz')
              .replace(/\bUsd\b/i, '($)');
  }

  function researchChipHTML(gear) {
    if (gear.research_state === 'researching') {
      return '<span class="research-pending is-researching"><span class="pulse"></span>Researching…</span>';
    }
    if (gear.research_state === 'cached') {
      const specs = gear.specs || {};
      let parts = [];
      // Pick a single "signature spec" per category — what audiophiles
      // would actually want at a glance. price always trails.
      if (specs.architecture) parts.push(specs.architecture);
      else if (specs.topology) parts.push(specs.topology);
      else if (specs.driver_type) parts.push(specs.driver_type);
      else if (specs.type) parts.push(specs.type);
      if (specs.price_usd) parts.push('$' + specs.price_usd);
      if (parts.length === 0) parts = [categoryLabel(gear.category)];
      return `<span class="research-chip">${escapeProfileHtml(parts.join(' · '))}</span>`;
    }
    return '<span class="research-pending">Awaiting research</span>';
  }

  function emailRowHTML(account, emailStatus) {
    const email = (emailStatus && emailStatus.email) || (account && account.email) || '';
    if (!email) {
      return `
        <div class="form-row is-clickable" data-action="set-email" data-row="email">
          <span class="form-label">Email</span>
          <span class="form-actions">
            <span class="form-value action">Add</span>
            <span class="link-chev">${PROFILE_ICONS.chev}</span>
          </span>
        </div>`;
    }
    if (emailStatus === null) {
      return `
        <div class="form-row" data-row="email">
          <span class="form-label">Email</span>
          <span class="form-actions">
            <span class="form-value">${escapeProfileHtml(email)}</span>
            <span class="form-value muted">checking…</span>
          </span>
        </div>`;
    }
    if (emailStatus.verified) {
      return `
        <div class="form-row" data-row="email">
          <span class="form-label">Email</span>
          <span class="form-actions">
            <span class="form-value">${escapeProfileHtml(email)}</span>
            <span class="verified">${PROFILE_ICONS.check}verified</span>
          </span>
        </div>`;
    }
    return `
      <div class="form-row is-clickable" data-action="verify-email" data-row="email">
        <span class="form-label">Email</span>
        <span class="form-actions">
          <span class="form-value">${escapeProfileHtml(email)}</span>
          <span class="form-value action">Verify</span>
          <span class="link-chev">${PROFILE_ICONS.chev}</span>
        </span>
      </div>`;
  }

  async function _refreshEmailRow(root, account) {
    // Background fetch — the backend itself has a 15s timeout on the
    // Worker call (httpx Timeout(15.0)) so we don't add a redundant
    // client-side abort that would mark the request "canceled" while
    // the backend is still legitimately waiting on a cold-start Worker.
    let data = null;
    try {
      const r = await fetch('/api/p2p/email/status');
      if (r.ok) data = await r.json();
    } catch (_) {}

    const old = root.querySelector('[data-row="email"]');
    if (!old) return;
    const wrap = document.createElement('template');
    wrap.innerHTML = emailRowHTML(account, data).trim();
    const replacement = wrap.content.firstElementChild;
    old.replaceWith(replacement);
    const verifyRow = root.querySelector('[data-action="verify-email"]');
    if (verifyRow) verifyRow.addEventListener('click', () => openEmailVerifyFlow());
    const setRow = root.querySelector('[data-action="set-email"]');
    if (setRow) setRow.addEventListener('click', () => openSetEmailFlow());
  }

  async function renderProfile(root) {
    let profile = null, account = null, config = null, scrobbling = null;
    // emailStatus stays null at first paint — fetched in background
    // after render so a slow Worker can't hang the page. The verify
    // status seldom changes, so checking-in-the-background is the
    // honest pattern here.
    const emailStatus = null;
    try {
      const [pr, ar, cr, sr] = await Promise.all([
        fetch('/api/profile'),
        fetch('/api/p2p/account'),
        fetch('/config'),
        fetch('/api/profile/scrobbling'),
      ]);
      if (pr.ok) profile = await pr.json();
      if (ar.ok) account = await ar.json();
      if (cr.ok) config = await cr.json();
      if (sr.ok) scrobbling = await sr.json();
    } catch (_) { /* fall through */ }

    if (!profile) {
      root.innerHTML = '<section class="screen"><div class="screen-head"><h2 class="screen-title">Profile</h2></div><div class="placeholder">Не вдалося завантажити профіль.</div></section>';
      return;
    }

    const display = profile.display_name || '';
    const username = (account && account.username) ? account.username : '';
    const initials = display.trim()
      ? display.trim().split(/\s+/).map(w => w[0]).slice(0, 2).join('').toUpperCase()
      : username.trim()
        ? username.trim().charAt(0).toUpperCase()
        : '—';
    const inviteTail = (account && account.invite_code)
      ? '· ' + account.invite_code.split('#').pop()
      : '';

    const gear = profile.gear || [];
    const byCategory = {};
    for (const g of gear) (byCategory[g.category] || (byCategory[g.category] = [])).push(g);
    const cats = GEAR_CATEGORIES.filter(c => byCategory[c.id] && byCategory[c.id].length);

    const gearSection = cats.length === 0
      ? `<div class="gear-list" style="padding:calc(18*var(--px));text-align:center;color:var(--color-text-muted);">
           No gear yet. Tap <b style="color:var(--color-amber);">Add gear</b> to start your audio chain.
         </div>`
      : cats.map(cat => `
          <div class="cat-header">
            <span class="cat-name">${cat.label}</span>
            <span class="cat-count">${byCategory[cat.id].length}</span>
          </div>
          <div class="gear-list">
            ${byCategory[cat.id].map(g => `
              <button class="gear-row" data-gear-id="${g.id}">
                <div class="gear-info">
                  <div class="gear-line1">
                    <span class="gear-brand">${escapeProfileHtml(g.brand)}</span>
                    <span class="gear-model">${escapeProfileHtml(g.model)}</span>
                  </div>
                  <div class="gear-line2">
                    ${statusBadgeHTML(g.status)}
                    ${researchChipHTML(g)}
                  </div>
                </div>
                <span class="gear-chev">${PROFILE_ICONS.chev}</span>
              </button>
            `).join('')}
          </div>
        `).join('');

    const filled = ['display_name', 'city', 'bio'].filter(k => (profile[k] || '').trim().length > 0).length
      + (gear.length ? 1 : 0)
      + (profile.avatar_cover_id ? 1 : 0);
    const pct = Math.round((filled / 5) * 100);

    root.innerHTML = `
      <section class="screen screen-profile">
        <div class="profile-header">
          <button class="icon-btn" aria-label="back" data-back>${PROFILE_ICONS.back}</button>
          <h1>Profile</h1>
          <button class="icon-btn" aria-label="edit" data-edit-toggle>${PROFILE_ICONS.edit}</button>
        </div>

        <div class="identity-block">
          <button class="avatar-big" data-avatar-upload type="button" aria-label="upload avatar">
            ${profile.avatar_cover_id
              ? `<img src="/api/covers/${escapeProfileHtml(profile.avatar_cover_id)}" alt="">`
              : `<span>${initials}</span>`}
            <span class="edit-pen">${PROFILE_ICONS.edit}</span>
          </button>
          <input type="file" id="avatarFile" accept="image/*" hidden>
          <div>
            <h2 class="identity-name">${display ? escapeProfileHtml(display) : '<span style="color:var(--color-text-dim);">Set your name</span>'}</h2>
            ${(username || inviteTail) ? `
              <div class="identity-handles">
                <span class="handle">${escapeProfileHtml(username)}</span>
                ${inviteTail ? `<span class="invite">${escapeProfileHtml(inviteTail)}</span>` : ''}
              </div>
            ` : ''}
          </div>
          ${(profile.city || profile.country) ? `<div class="identity-city">${PROFILE_ICONS.pin}${escapeProfileHtml([profile.city, countryName(profile.country)].filter(Boolean).join(', '))}</div>` : ''}
          <p class="identity-bio ${profile.bio ? '' : 'placeholder'}">
            ${profile.bio ? escapeProfileHtml(profile.bio) : 'Add a short bio so other audiophiles know who you are.'}
          </p>
        </div>

        <div class="profile-group-label">Account</div>
        <div class="form-group">
          ${emailRowHTML(account, emailStatus)}
          <div class="form-row is-clickable" data-action="change-password">
            <span class="form-label">Password</span>
            <span class="form-actions">
              <span class="form-value action">Change</span>
              <span class="link-chev">${PROFILE_ICONS.chev}</span>
            </span>
          </div>
          ${(() => {
            const lfmUser = (config && config.lastfm_username) || '';
            const lfmOn   = !!(config && config.lastfm_authorized);
            const scrobOn = !!(scrobbling && scrobbling.enabled);
            if (lfmOn) {
              return `
                <div class="form-row">
                  <span class="form-label">Last.fm</span>
                  <span class="form-actions">
                    <span class="form-value">${escapeProfileHtml(lfmUser || 'connected')}</span>
                    <span class="verified">${PROFILE_ICONS.check}connected</span>
                  </span>
                </div>
                <div class="form-row">
                  <span class="form-label">Scrobbling</span>
                  <button class="toggle ${scrobOn ? 'on' : ''}" data-action="scrobble-toggle"><span class="knob"></span></button>
                </div>`;
            }
            return `
              <div class="form-row is-clickable" data-action="lastfm">
                <span class="form-label">Last.fm</span>
                <span class="form-actions">
                  <span class="form-value muted">Not connected</span>
                  <span class="link-chev">${PROFILE_ICONS.chev}</span>
                </span>
              </div>
              <div class="form-row">
                <span class="form-label">Scrobbling</span>
                <button class="toggle disabled" disabled><span class="knob"></span></button>
              </div>`;
          })()}
        </div>

        <div class="profile-section-head">
          <h3>My setup</h3>
          <button class="add-btn" data-add-gear><span class="plus">+</span>Add gear</button>
        </div>
        ${gearSection}

        <div class="profile-group-label">Sociability</div>
        <div class="sociability">
          <div class="soc-row">
            <div>
              <div class="soc-label">Open to meet other audiophiles</div>
              <div class="soc-hint">Phase 2 — discovery is not active yet.</div>
            </div>
            <button class="toggle ${profile.open_to_meet ? 'on' : ''} disabled" disabled><span class="knob"></span></button>
          </div>
          <div class="soc-progress">
            <div class="soc-prose">
              Profile is <span class="pct">${pct}%</span> complete — finish to unlock discovery when it launches.
            </div>
            <div class="soc-bar"><div class="fill" style="width:${pct}%"></div></div>
          </div>
        </div>
      </section>
    `;

    root.querySelector('[data-back]').addEventListener('click', () => {
      if (history.length > 1) history.back();
      else navigate('home');
    });
    root.querySelectorAll('[data-gear-id]').forEach(btn => {
      btn.addEventListener('click', () => {
        const item = gear.find(g => g.id === btn.dataset.gearId);
        if (item) gearSheet.open(item);
      });
    });
    root.querySelector('[data-add-gear]').addEventListener('click', () => addGearSheet.open());
    root.querySelector('[data-edit-toggle]').addEventListener('click', () => openInlineProfileEditor(profile));

    // No verify-email handler attached here — the email row starts in
    // "checking…" state with no clickable affordance, _refreshEmailRow
    // re-renders it (and wires the click handler if needed) when the
    // status fetch returns.
    _refreshEmailRow(root, account);

    const lfmRow = root.querySelector('[data-action="lastfm"]');
    if (lfmRow) lfmRow.addEventListener('click', () => openLastfmAuthFlow());

    const scrobBtn = root.querySelector('[data-action="scrobble-toggle"]');
    if (scrobBtn) {
      scrobBtn.addEventListener('click', async () => {
        const next = !scrobBtn.classList.contains('on');
        scrobBtn.classList.toggle('on', next); // optimistic
        try {
          const r = await fetch('/api/profile/scrobbling', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled: next }),
          });
          if (!r.ok) scrobBtn.classList.toggle('on', !next); // rollback
        } catch (_) {
          scrobBtn.classList.toggle('on', !next);
        }
      });
    }

    const fileInput = root.querySelector('#avatarFile');
    root.querySelector('[data-avatar-upload]').addEventListener('click', () => fileInput.click());
    fileInput.addEventListener('change', async (e) => {
      const file = e.target.files && e.target.files[0];
      if (!file) return;
      try {
        const ab = await file.arrayBuffer();
        const r = await fetch('/api/profile/avatar', {
          method: 'POST',
          headers: { 'Content-Type': file.type || 'application/octet-stream' },
          body: ab,
        });
        if (r.ok) render();
        else console.error('avatar upload failed', r.status, await r.text());
      } catch (err) {
        console.error('avatar upload error', err);
      }
      fileInput.value = '';
    });
  }

  /* Destructive-action confirm dialog. Replaces window.confirm so
     the prompt sits inside our visual language (warm dark sheet,
     terracotta primary). Returns a Promise<boolean>. */
  function confirmDestructive({ title, message, confirmText = 'Remove', cancelText = 'Cancel' }) {
    return new Promise(resolve => {
      const overlay = document.createElement('div');
      overlay.className = 'confirm-overlay';
      overlay.innerHTML = `
        <div class="confirm-sheet" role="dialog" aria-modal="true">
          <h2 class="confirm-title">${escapeProfileHtml(title)}</h2>
          <p class="confirm-message">${message}</p>
          <div class="confirm-actions">
            <button class="profile-btn secondary" data-cancel>${escapeProfileHtml(cancelText)}</button>
            <button class="profile-btn destructive" data-confirm>${escapeProfileHtml(confirmText)}</button>
          </div>
        </div>
      `;
      document.body.appendChild(overlay);
      const close = (result) => { overlay.remove(); document.removeEventListener('keydown', onKey); resolve(result); };
      const onKey = (e) => {
        if (e.key === 'Escape') close(false);
        if (e.key === 'Enter')  close(true);
      };
      document.addEventListener('keydown', onKey);
      overlay.addEventListener('click', e => { if (e.target === overlay) close(false); });
      overlay.querySelector('[data-cancel]').addEventListener('click', () => close(false));
      overlay.querySelector('[data-confirm]').addEventListener('click', () => close(true));
      setTimeout(() => overlay.querySelector('[data-confirm]').focus(), 50);
    });
  }

  /* Notification dialog — single-button HTML replacement for
     window.alert(). Sits inside the same .confirm-overlay /
     .confirm-sheet shell as confirmDestructive so we don't fork
     visual languages. `kind` controls the title accent
     (error → negative, success → positive, info → default).
     Returns a Promise<void> that resolves once the dialog closes.

     `message` is rendered as HTML, mirroring confirmDestructive —
     callers must escape any user-controlled data themselves
     (escapeProfileHtml is exported on window for that). */
  function notifyDialog({ title, message, kind = 'info', dismissText = 'OK' } = {}) {
    return new Promise(resolve => {
      const overlay = document.createElement('div');
      overlay.className = 'confirm-overlay';
      const titleHtml = title
        ? `<h2 class="confirm-title ${kind}">${escapeProfileHtml(title)}</h2>`
        : '';
      overlay.innerHTML = `
        <div class="confirm-sheet" role="dialog" aria-modal="true">
          ${titleHtml}
          <p class="confirm-message">${message || ''}</p>
          <div class="confirm-actions single">
            <button class="profile-btn primary" data-confirm>${escapeProfileHtml(dismissText)}</button>
          </div>
        </div>
      `;
      document.body.appendChild(overlay);
      const close = () => { overlay.remove(); document.removeEventListener('keydown', onKey); resolve(); };
      const onKey = (e) => {
        if (e.key === 'Escape' || e.key === 'Enter') close();
      };
      document.addEventListener('keydown', onKey);
      overlay.addEventListener('click', e => { if (e.target === overlay) close(); });
      overlay.querySelector('[data-confirm]').addEventListener('click', close);
      setTimeout(() => overlay.querySelector('[data-confirm]').focus(), 50);
    });
  }

  // Cross-file API — app.js (non-IIFE legacy bootstrap) and any future
  // module needs the same dialogs to avoid native alert/confirm.
  window.confirmDestructive = confirmDestructive;
  window.notifyDialog       = notifyDialog;
  window.escapeProfileHtml  = escapeProfileHtml;

  /* Email verification — two-step Worker-mediated flow.
     POST /api/p2p/email/send-code sends a 6-char code to the
     account's email; POST /api/p2p/email/verify-code redeems it
     and registers the email on the Cloudflare Worker so future
     invites can claim the ✅ Verified badge. */
  async function openEmailVerifyFlow() {
    let sentTo = '';
    try {
      const r = await fetch('/api/p2p/email/send-code', { method: 'POST' });
      if (!r.ok) {
        const txt = await _errorMessage(r);
        await notifyDialog({
          title: 'Could not send verification code',
          message: escapeProfileHtml(txt),
          kind: 'error',
        });
        return;
      }
      const data = await r.json();
      sentTo = data.email || '';
    } catch (err) {
      await notifyDialog({
        title: 'Could not send verification code',
        message: escapeProfileHtml(String(err)),
        kind: 'error',
      });
      return;
    }

    const overlay = document.createElement('div');
    overlay.className = 'add-gear-overlay';
    overlay.innerHTML = `
      <div class="add-gear-sheet">
        <div class="sheet-handle"></div>
        <div class="add-gear-head">
          <h2 class="add-gear-title">Verify email</h2>
          <button class="icon-btn" data-cancel aria-label="close">${PROFILE_ICONS.close}</button>
        </div>
        <div class="add-gear-row">
          <p style="margin:0;color:var(--color-text-muted);font-size:calc(13*var(--px));line-height:1.5;">
            We sent a 6-character code to <b style="color:var(--color-text);">${escapeProfileHtml(sentTo)}</b>. Enter it below to confirm the address.
          </p>
          <input class="add-gear-input" id="verifyCode" placeholder="ABC123" maxlength="6"
                 style="text-transform:uppercase;letter-spacing:0.2em;font-family:var(--font-mono);text-align:center;">
          <button class="profile-btn primary" data-confirm>Confirm</button>
          <div id="verifyMsg" style="font-size:calc(12*var(--px));color:var(--color-text-dim);min-height:calc(16*var(--px));"></div>
        </div>
      </div>
    `;
    document.body.appendChild(overlay);
    const close = () => overlay.remove();
    overlay.addEventListener('click', e => { if (e.target === overlay) close(); });
    overlay.querySelector('[data-cancel]').addEventListener('click', close);
    const codeInput = overlay.querySelector('#verifyCode');
    const msg = overlay.querySelector('#verifyMsg');
    setTimeout(() => codeInput.focus(), 100);

    const submit = async () => {
      const code = codeInput.value.trim().toUpperCase();
      if (code.length !== 6) {
        msg.style.color = 'var(--color-negative)';
        msg.textContent = 'Code must be 6 characters.';
        return;
      }
      msg.style.color = 'var(--color-text-muted)';
      msg.textContent = 'Verifying…';
      try {
        const r = await fetch('/api/p2p/email/verify-code', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ code }),
        });
        if (!r.ok) {
          msg.style.color = 'var(--color-negative)';
          msg.textContent = await r.text();
          return;
        }
        const data = await r.json();
        if (data.verified) {
          msg.style.color = 'var(--color-positive)';
          msg.textContent = 'Verified.';
          setTimeout(() => { close(); render(); }, 600);
        } else {
          msg.style.color = 'var(--color-negative)';
          msg.textContent = data.error || 'Invalid code.';
        }
      } catch (err) {
        msg.style.color = 'var(--color-negative)';
        msg.textContent = String(err);
      }
    };
    overlay.querySelector('[data-confirm]').addEventListener('click', submit);
    codeInput.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); submit(); } });
  }

  async function openHqpConnectionEditor(current, onSaved) {
    const overlay = document.createElement('div');
    overlay.className = 'add-gear-overlay';
    overlay.innerHTML = `
      <div class="add-gear-sheet">
        <div class="sheet-handle"></div>
        <div class="add-gear-head">
          <h2 class="add-gear-title">HQPlayer connection</h2>
          <button class="icon-btn" data-cancel aria-label="close">${PROFILE_ICONS.close}</button>
        </div>
        <div class="add-gear-row">
          <p style="margin:0;color:var(--color-text-muted);font-size:calc(13*var(--px));line-height:1.5;">
            Address of the HQPlayer Control Protocol endpoint. <b>localhost</b> when HQPlayer runs on the same machine, the LAN IP otherwise. Default port is 4321.
          </p>
          <label style="display:flex;flex-direction:column;gap:calc(4*var(--px));">
            <span style="color:var(--color-text-muted);font-size:calc(12*var(--px));">Host</span>
            <input class="add-gear-input" id="hqpHostInput" type="text" placeholder="localhost" maxlength="255" autocomplete="off" spellcheck="false" value="${escapeProfileHtml(current.host || '')}">
          </label>
          <label style="display:flex;flex-direction:column;gap:calc(4*var(--px));">
            <span style="color:var(--color-text-muted);font-size:calc(12*var(--px));">Port</span>
            <input class="add-gear-input" id="hqpPortInput" type="number" min="1" max="65535" placeholder="4321" value="${current.port || 4321}">
          </label>
          <p style="margin:0;color:var(--color-text-muted);font-size:calc(11.5*var(--px));line-height:1.5;">
            Scrobbling runs in a separate process — restart the Sautium launcher to apply the change there too.
          </p>
          <button class="profile-btn primary" data-confirm>Save</button>
          <div id="hqpConnMsg" style="font-size:calc(12*var(--px));color:var(--color-text-dim);min-height:calc(16*var(--px));"></div>
        </div>
      </div>
    `;
    document.body.appendChild(overlay);
    const close = () => overlay.remove();
    overlay.addEventListener('click', e => { if (e.target === overlay) close(); });
    overlay.querySelector('[data-cancel]').addEventListener('click', close);
    const hostInput = overlay.querySelector('#hqpHostInput');
    const portInput = overlay.querySelector('#hqpPortInput');
    const msg = overlay.querySelector('#hqpConnMsg');
    setTimeout(() => hostInput.focus(), 100);

    const submit = async () => {
      const host = hostInput.value.trim();
      const port = parseInt(portInput.value, 10);
      if (!host) {
        msg.style.color = 'var(--color-negative)';
        msg.textContent = 'Host is required.';
        return;
      }
      if (!port || port < 1 || port > 65535) {
        msg.style.color = 'var(--color-negative)';
        msg.textContent = 'Port must be 1–65535.';
        return;
      }
      msg.style.color = 'var(--color-text-muted)';
      msg.textContent = 'Saving…';
      try {
        const r = await fetch('/api/settings/hqplayer', {
          method: 'PUT',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({host, port}),
        });
        if (!r.ok) {
          msg.style.color = 'var(--color-negative)';
          msg.textContent = await r.text();
          return;
        }
        msg.style.color = 'var(--color-positive)';
        msg.textContent = 'Saved.';
        setTimeout(() => { close(); if (onSaved) onSaved(); }, 400);
      } catch (err) {
        msg.style.color = 'var(--color-negative)';
        msg.textContent = String(err);
      }
    };
    overlay.querySelector('[data-confirm]').addEventListener('click', submit);
    [hostInput, portInput].forEach(el => {
      el.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); submit(); } });
    });
  }

  async function openSetEmailFlow() {
    const overlay = document.createElement('div');
    overlay.className = 'add-gear-overlay';
    overlay.innerHTML = `
      <div class="add-gear-sheet">
        <div class="sheet-handle"></div>
        <div class="add-gear-head">
          <h2 class="add-gear-title">Add email</h2>
          <button class="icon-btn" data-cancel aria-label="close">${PROFILE_ICONS.close}</button>
        </div>
        <div class="add-gear-row">
          <p style="margin:0;color:var(--color-text-muted);font-size:calc(13*var(--px));line-height:1.5;">
            Used to receive invites from friends and as the reply-to on
            invites you send. Verification is a separate step — saving an
            address here doesn't send any email yet.
          </p>
          <input class="add-gear-input" id="setEmailInput" type="email" placeholder="you@example.com" maxlength="320" autocomplete="email">
          <button class="profile-btn primary" data-confirm>Save</button>
          <div id="setEmailMsg" style="font-size:calc(12*var(--px));color:var(--color-text-dim);min-height:calc(16*var(--px));"></div>
        </div>
      </div>
    `;
    document.body.appendChild(overlay);
    const close = () => overlay.remove();
    overlay.addEventListener('click', e => { if (e.target === overlay) close(); });
    overlay.querySelector('[data-cancel]').addEventListener('click', close);
    const emailInput = overlay.querySelector('#setEmailInput');
    const msg = overlay.querySelector('#setEmailMsg');
    setTimeout(() => emailInput.focus(), 100);

    const submit = async () => {
      const email = emailInput.value.trim();
      if (!email || !email.includes('@') || !email.split('@')[1].includes('.')) {
        msg.style.color = 'var(--color-negative)';
        msg.textContent = 'Enter a valid email address.';
        return;
      }
      msg.style.color = 'var(--color-text-muted)';
      msg.textContent = 'Saving…';
      try {
        const r = await fetch('/api/p2p/account/email', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ email }),
        });
        if (!r.ok) {
          msg.style.color = 'var(--color-negative)';
          msg.textContent = await r.text();
          return;
        }
        msg.style.color = 'var(--color-positive)';
        msg.textContent = 'Saved.';
        setTimeout(() => { close(); render(); }, 400);
      } catch (err) {
        msg.style.color = 'var(--color-negative)';
        msg.textContent = String(err);
      }
    };
    overlay.querySelector('[data-confirm]').addEventListener('click', submit);
    emailInput.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); submit(); } });
  }

  async function openLastfmAuthFlow() {
    const overlay = document.createElement('div');
    overlay.className = 'add-gear-overlay';
    overlay.innerHTML = `
      <div class="add-gear-sheet">
        <div class="sheet-handle"></div>
        <div class="add-gear-head">
          <h2 class="add-gear-title">Connect Last.fm</h2>
          <button class="icon-btn" data-cancel aria-label="close">${PROFILE_ICONS.close}</button>
        </div>
        <div class="add-gear-row">
          <p style="margin:0;color:var(--color-text-muted);font-size:calc(13*var(--px));line-height:1.5;">
            Sautium uses your Last.fm account to fetch artist bios, similar artists and tags, and to scrobble what you play. The next step opens last.fm in a new tab — authorise there, come back here, and tap <b style="color:var(--color-text);">Finish</b>.
          </p>
          <div id="lfmStartRow">
            <button class="profile-btn primary" data-start>Open Last.fm authorisation</button>
          </div>
          <div id="lfmFinishRow" style="display:none;">
            <a id="lfmReopen" target="_blank" rel="noopener" style="display:block;font-size:calc(12*var(--px));color:var(--color-accent);word-break:break-all;text-decoration:underline;margin-bottom:calc(10*var(--px));"></a>
            <button class="profile-btn primary" data-finish>Finish</button>
          </div>
          <div id="lfmMsg" style="font-size:calc(12*var(--px));color:var(--color-text-dim);min-height:calc(16*var(--px));"></div>
        </div>
      </div>
    `;
    document.body.appendChild(overlay);
    const close = () => overlay.remove();
    overlay.addEventListener('click', e => { if (e.target === overlay) close(); });
    overlay.querySelector('[data-cancel]').addEventListener('click', close);
    const msg = overlay.querySelector('#lfmMsg');
    const startRow = overlay.querySelector('#lfmStartRow');
    const finishRow = overlay.querySelector('#lfmFinishRow');
    const reopen = overlay.querySelector('#lfmReopen');

    overlay.querySelector('[data-start]').addEventListener('click', async () => {
      msg.style.color = 'var(--color-text-muted)';
      msg.textContent = 'Requesting authorisation URL…';
      try {
        const r = await fetch('/lastfm/auth/start', { method: 'POST' });
        if (!r.ok) {
          msg.style.color = 'var(--color-negative)';
          msg.textContent = 'Could not start: ' + await r.text();
          return;
        }
        const data = await r.json();
        if (!data.auth_url) {
          msg.style.color = 'var(--color-negative)';
          msg.textContent = 'Last.fm did not return an authorisation URL.';
          return;
        }
        window.open(data.auth_url, '_blank', 'noopener');
        reopen.href = data.auth_url;
        reopen.textContent = 'Reopen authorisation page';
        startRow.style.display = 'none';
        finishRow.style.display = '';
        msg.textContent = 'After clicking "Yes, allow access" on Last.fm, return here and tap Finish.';
      } catch (err) {
        msg.style.color = 'var(--color-negative)';
        msg.textContent = String(err);
      }
    });

    overlay.querySelector('[data-finish]').addEventListener('click', async () => {
      msg.style.color = 'var(--color-text-muted)';
      msg.textContent = 'Confirming…';
      try {
        const r = await fetch('/lastfm/auth/complete', { method: 'POST' });
        if (!r.ok) {
          let detail = await r.text();
          try { detail = JSON.parse(detail).detail || detail; } catch (_) {}
          msg.style.color = 'var(--color-negative)';
          msg.textContent = detail;
          return;
        }
        const data = await r.json();
        msg.style.color = 'var(--color-positive)';
        msg.textContent = data.username ? `Connected as ${data.username}.` : 'Connected.';
        setTimeout(() => { close(); render(); }, 700);
      } catch (err) {
        msg.style.color = 'var(--color-negative)';
        msg.textContent = String(err);
      }
    });
  }

  function openInlineProfileEditor(profile) {
    const overlay = document.createElement('div');
    overlay.className = 'add-gear-overlay';
    overlay.innerHTML = `
      <div class="add-gear-sheet">
        <div class="sheet-handle"></div>
        <div class="add-gear-head">
          <h2 class="add-gear-title">Edit profile</h2>
          <button class="icon-btn" data-cancel aria-label="close">${PROFILE_ICONS.close}</button>
        </div>
        <div class="add-gear-row">
          <input class="add-gear-input" id="editName" placeholder="Display name" maxlength="128" value="${escapeProfileHtml(profile.display_name || '')}">
          <select class="add-gear-input" id="editCountry">
            <option value="">Country (none)</option>
            ${COUNTRY_OPTIONS.map(c => `<option value="${c.code}" ${profile.country === c.code ? 'selected' : ''}>${escapeProfileHtml(c.name)} (${c.code})</option>`).join('')}
          </select>
          <input class="add-gear-input" id="editCity" placeholder="City" maxlength="128" value="${escapeProfileHtml(profile.city || '')}">
          <textarea class="add-gear-input" id="editBio" placeholder="A short bio" rows="3" maxlength="2000" style="height:auto;padding:calc(10*var(--px))calc(14*var(--px));resize:vertical;">${escapeProfileHtml(profile.bio || '')}</textarea>
          <button class="profile-btn primary" data-save>Save</button>
        </div>
      </div>
    `;
    document.body.appendChild(overlay);
    const close = () => overlay.remove();
    overlay.addEventListener('click', e => { if (e.target === overlay) close(); });
    overlay.querySelector('[data-cancel]').addEventListener('click', close);
    overlay.querySelector('[data-save]').addEventListener('click', async () => {
      const payload = {
        display_name: overlay.querySelector('#editName').value,
        city: overlay.querySelector('#editCity').value,
        country: overlay.querySelector('#editCountry').value || null,
        bio: overlay.querySelector('#editBio').value,
      };
      try {
        await fetch('/api/profile', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
      } catch (_) {}
      close();
      render();
    });
  }

  /* Add-gear sheet.
     Pure two-field form (Brand + Model) with the category strip
     on top. No catalog-of-models search — the user is adding their
     own gear, not browsing global inventory, and stale model
     suggestions ("AM5LE" while typing "AM20") are noise. Only
     gear_brands has live autocomplete because that's where
     duplicates actually collide (Holo vs Holo Audio, Audeze vs
     Audeze, Inc.). When a brand suggestion is tapped, the diff
     between what the user typed and the suggestion is stripped
     from the start of the model field — "Holo" → "Holo Audio"
     trims a leading "Audio " from "Audio Spring 3 KTE". */
  const addGearSheet = {
    el: null,
    isOpen: false,
    category: 'headphones',
    init() {
      const overlay = document.createElement('div');
      overlay.className = 'add-gear-overlay';
      overlay.hidden = true;
      overlay.innerHTML = `
        <div class="add-gear-sheet">
          <div class="sheet-handle"></div>
          <div class="add-gear-head">
            <h2 class="add-gear-title">Add gear</h2>
            <button class="icon-btn" data-close aria-label="close">${PROFILE_ICONS.close}</button>
          </div>
          <div class="add-gear-category-row" id="agCatRow">
            ${GEAR_CATEGORIES.map(c => `
              <button class="filter-chip ${c.id === 'headphones' ? 'active' : ''}" data-cat="${c.id}">${c.label}</button>
            `).join('')}
          </div>
          <div class="add-gear-row" style="overflow:visible;">
            <div style="position:relative;">
              <input class="add-gear-input" id="agBrand" placeholder="Brand" autocomplete="off">
              <div id="agBrandSuggest" style="position:absolute;top:calc(48*var(--px));left:0;right:0;background:var(--color-surface);border:1px solid var(--color-divider);border-radius:var(--radius-md);box-shadow:var(--shadow-2);z-index:5;max-height:calc(180*var(--px));overflow-y:auto;display:none;"></div>
            </div>
            <input class="add-gear-input" id="agModel" placeholder="Model">
            <div id="agMsg" style="font-size:calc(12*var(--px));color:var(--color-text-dim);min-height:calc(16*var(--px));"></div>
            <button class="profile-btn primary" data-add>Add</button>
          </div>
        </div>
      `;
      document.body.appendChild(overlay);
      this.el = overlay;
      overlay.addEventListener('click', e => { if (e.target === overlay) this.close(); });
      overlay.querySelector('[data-close]').addEventListener('click', () => this.close());
      overlay.querySelectorAll('[data-cat]').forEach(btn => {
        btn.addEventListener('click', () => {
          this.category = btn.dataset.cat;
          overlay.querySelectorAll('.filter-chip').forEach(b => b.classList.toggle('active', b === btn));
        });
      });

      const brandInput = overlay.querySelector('#agBrand');
      const modelInput = overlay.querySelector('#agModel');
      const suggest = overlay.querySelector('#agBrandSuggest');
      let timer = null;

      const refreshBrandSuggest = async () => {
        const q = brandInput.value.trim();
        if (!q) { suggest.style.display = 'none'; return; }
        try {
          const r = await fetch('/api/gear-models/brands/search?q=' + encodeURIComponent(q) + '&limit=8');
          if (!r.ok) throw new Error();
          const rows = await r.json();
          if (rows.length === 0 ||
              (rows.length === 1 && rows[0].name.toLowerCase() === q.toLowerCase())) {
            suggest.style.display = 'none';
            return;
          }
          suggest.innerHTML = rows.map(r => `
            <div data-brand-name="${escapeProfileHtml(r.name)}"
                 style="padding:calc(10*var(--px)) calc(14*var(--px));font-size:calc(13.5*var(--px));color:var(--color-text);cursor:pointer;border-bottom:1px solid var(--color-divider);">
              ${escapeProfileHtml(r.name)}
            </div>
          `).join('');
          suggest.style.display = 'block';
          suggest.querySelectorAll('[data-brand-name]').forEach(el => {
            el.addEventListener('click', () => {
              const newBrand = el.dataset.brandName;
              const oldBrand = brandInput.value.trim();
              brandInput.value = newBrand;
              // Smart strip: if the new (canonical) brand extends the
              // user's typed brand by some trailing word(s), and the
              // model field starts with those same word(s), drop them
              // — they originally belonged to the brand. "Holo" →
              // "Holo Audio" with model "Audio Spring 3 KTE" becomes
              // model "Spring 3 KTE".
              if (oldBrand && newBrand.toLowerCase().startsWith(oldBrand.toLowerCase())) {
                const extra = newBrand.slice(oldBrand.length).trim();
                if (extra) {
                  const escaped = extra.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
                  modelInput.value = modelInput.value.replace(
                    new RegExp('^\\s*' + escaped + '\\s*', 'i'), '',
                  );
                }
              }
              suggest.style.display = 'none';
              modelInput.focus();
            });
          });
        } catch (_) {
          suggest.style.display = 'none';
        }
      };
      brandInput.addEventListener('input', () => {
        clearTimeout(timer);
        timer = setTimeout(refreshBrandSuggest, 120);
      });
      brandInput.addEventListener('blur', () => {
        setTimeout(() => { suggest.style.display = 'none'; }, 150);
      });
      brandInput.addEventListener('focus', refreshBrandSuggest);

      overlay.querySelector('[data-add]').addEventListener('click', () => this._submit());
      modelInput.addEventListener('keydown', e => {
        if (e.key === 'Enter') { e.preventDefault(); this._submit(); }
      });
      brandInput.addEventListener('keydown', e => {
        if (e.key === 'Enter') { e.preventDefault(); modelInput.focus(); }
      });
    },
    open() {
      if (!this.el) this.init();
      this.el.hidden = false;
      this.isOpen = true;
      this.el.querySelector('#agBrand').value = '';
      this.el.querySelector('#agModel').value = '';
      this.el.querySelector('#agMsg').textContent = '';
      const suggest = this.el.querySelector('#agBrandSuggest');
      if (suggest) suggest.style.display = 'none';
      setTimeout(() => this.el.querySelector('#agBrand').focus(), 100);
    },
    close() {
      if (!this.el) return;
      this.el.hidden = true;
      this.isOpen = false;
    },
    async _submit() {
      const brand = this.el.querySelector('#agBrand').value.trim();
      const model = this.el.querySelector('#agModel').value.trim();
      const msg = this.el.querySelector('#agMsg');
      if (!brand || !model) {
        msg.style.color = 'var(--color-negative)';
        msg.textContent = 'Brand and model are required.';
        return;
      }
      msg.style.color = 'var(--color-text-muted)';
      msg.textContent = 'Adding…';
      try {
        const r = await fetch('/api/profile/gear', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ brand, model, category: this.category, status: 'own' }),
        });
        if (!r.ok) {
          msg.style.color = 'var(--color-negative)';
          msg.textContent = await r.text();
          return;
        }
        this.close();
        if (parseHash().startsWith('more/profile')) render();
      } catch (err) {
        msg.style.color = 'var(--color-negative)';
        msg.textContent = String(err);
      }
    },
  };

  const gearSheet = {
    el: null,
    isOpen: false,
    current: null,
    init() {
      const overlay = document.createElement('div');
      overlay.className = 'gear-overlay';
      overlay.hidden = true;
      document.body.appendChild(overlay);
      this.el = overlay;
      overlay.addEventListener('click', e => { if (e.target === overlay) this.close(); });
    },
    async open(gear) {
      if (!this.el) this.init();
      this.current = gear;
      this.render();
      this.el.hidden = false;
      this.isOpen = true;
      // Pull full detail (specs array, technologies, sentiment terms)
      // from the canonical gear model — list payload only carries a
      // compact spec map for the card chip.
      if (gear.gear_model_id) {
        try {
          const r = await fetch('/api/gear-models/' + gear.gear_model_id);
          if (r.ok) {
            const detail = await r.json();
            // Merge canonical detail first, then re-apply user_gear
            // fields — detail.id is gear_models.id (canonical), but
            // status / notes / DELETE path key off user_gear.id.
            this.current = Object.assign({}, detail, {
              id:             gear.id,
              gear_model_id:  gear.gear_model_id,
              status:         gear.status,
              notes:          gear.notes,
            });
            this.render();
          }
        } catch (_) {}
      }
    },
    close() {
      if (!this.el) return;
      this.el.hidden = true;
      this.isOpen = false;
      this.current = null;
    },
    render() {
      const g = this.current;
      if (!g) return;
      const isCached = g.research_state === 'cached';
      const isResearching = g.research_state === 'researching';
      const specs = g.specs || {};
      const sentiment = g.community_sentiment || null;

      const statusOpts = ['own', 'want', 'sell', 'previously_owned'];
      const segments = statusOpts.map(s => `
        <button class="seg-opt ${g.status === s ? 'active' : ''}" data-status="${s}">
          ${s === 'previously_owned' ? 'Past' : (s[0].toUpperCase() + s.slice(1))}
        </button>
      `).join('');

      let researchHTML;
      if (isCached && g.research_summary) {
        const updatedAgo = g.researched_at ? formatRelativeTime(g.researched_at) : 'recently';
        researchHTML = `
          <div class="research-card">
            <div class="research-state-row">
              <span class="research-state-label">Community summary</span>
            </div>
            <p class="research-prose">${escapeProfileHtml(g.research_summary)}</p>
            <div class="research-meta">
              <span>Updated ${escapeProfileHtml(updatedAgo)}</span>
              <button class="refresh cooldown" disabled>Refresh in ${g.refresh_cooldown_days || 7}d</button>
            </div>
          </div>`;
      } else if (isResearching) {
        researchHTML = `
          <div class="research-card">
            <div class="research-state-row">
              <span class="pulse"></span>
              <span class="research-state-label is-researching">Researching</span>
            </div>
            <p class="research-prose empty">Gathering specs and community sentiment from audiophile sources. This usually completes in a few minutes.</p>
            <p class="helper-prose">Sources scanned: Head-Fi, ASR, manufacturer docs. The card refreshes automatically when the summary is ready.</p>
          </div>`;
      } else {
        researchHTML = `
          <div class="research-card">
            <div class="research-state-row">
              <span class="research-state-label is-awaiting">Awaiting research · queued</span>
            </div>
            <p class="research-prose empty">We don't have a community summary for this model yet. It's been added to the research queue.</p>
            <p class="helper-prose">AI-on users trigger background research; results sync to everyone via P2P. You'll see specs, sentiment and a personalised take here once it lands.</p>
          </div>`;
      }

      // Specs come either as a list of {key, label, unit, value, ...}
      // from /api/gear-models/<id> (full detail) or as a flat dict
      // from the list payload (chip-friendly, no labels). Normalise.
      let specsList = [];
      if (Array.isArray(g.specs)) {
        specsList = g.specs;
      } else if (g.specs && typeof g.specs === 'object') {
        specsList = Object.entries(g.specs).map(([key, value]) => ({
          key, label: humanizeSpecKey(key), value, unit: null,
        }));
      }
      let specsHTML = '';
      if (isCached && specsList.length > 0) {
        const rows = specsList
          .filter(s => s.value != null && s.value !== '')
          .map(s => `
            <div>
              <div class="spec-key">${escapeProfileHtml(s.label || humanizeSpecKey(s.key))}</div>
              <div class="spec-val">${escapeProfileHtml(String(s.value))}${s.unit ? ' <span style="color:var(--color-text-dim);">' + escapeProfileHtml(s.unit) + '</span>' : ''}</div>
            </div>
          `).join('');
        if (rows) {
          specsHTML = `<div class="specs-grid"><div class="specs-title">Specs</div><div class="specs-rows">${rows}</div></div>`;
        }
      }

      let technologiesHTML = '';
      if (isCached && Array.isArray(g.technologies) && g.technologies.length > 0) {
        const items = g.technologies.map(t => `
          <div style="padding:calc(8*var(--px)) 0;border-top:1px solid var(--color-divider);">
            <div style="display:flex;justify-content:space-between;gap:calc(8*var(--px));">
              <div style="font-size:calc(13*var(--px));font-weight:600;color:var(--color-text);">${escapeProfileHtml(t.label)}</div>
              ${t.introduced_year ? `<div class="spec-val" style="font-size:calc(11*var(--px));">${t.introduced_year}</div>` : ''}
            </div>
            <div style="font-size:calc(12*var(--px));color:var(--color-text-muted);line-height:1.45;margin-top:calc(2*var(--px));">${escapeProfileHtml(t.description)}</div>
          </div>
        `).join('');
        technologiesHTML = `
          <div class="specs-grid" style="margin-top:calc(12*var(--px));">
            <div class="specs-title">Technologies</div>
            <div>${items}</div>
          </div>`;
      }

      // Sentiment comes either as a `sentiment` object (from detail
      // endpoint: {score, sample_size, praise[], criticism[]}) or as
      // flat aggregate columns on the gear (sentiment_score / _sample_size)
      // without term lists. Render both shapes.
      const sent = g.sentiment || {
        score: g.sentiment_score, sample_size: g.sentiment_sample_size,
        praise: [], criticism: [],
      };
      let sentimentHTML = '';
      if (isCached && (sent.score != null || (sent.praise || []).length || (sent.criticism || []).length)) {
        const praise = (sent.praise || []).map(p => `<span class="sent-pill praise">${escapeProfileHtml(p)}</span>`).join('');
        const crit   = (sent.criticism || []).map(p => `<span class="sent-pill crit">${escapeProfileHtml(p)}</span>`).join('');
        sentimentHTML = `
          <div class="sentiment-card">
            ${sent.score != null ? `
              <div class="sent-head">
                <span class="sent-score">${escapeProfileHtml(String(sent.score))}</span>
                <span class="sent-score-suffix">/ 10${sent.sample_size ? ' · n = ' + sent.sample_size : ''}</span>
              </div>` : ''}
            ${praise ? `<div class="sent-label">Praise</div><div class="sent-pills">${praise}</div>` : ''}
            ${crit ? `<div class="sent-pill-sec">Criticism</div><div class="sent-pills" style="margin-top:calc(6*var(--px));">${crit}</div>` : ''}
          </div>`;
      }

      const aiTakeHTML = isCached ? `
        <div class="ai-take">
          <div class="ai-take-head">
            <span class="ai-take-glyph">AI</span>
            <span class="ai-take-label">Your personalised take</span>
          </div>
          <p class="ai-take-prose">Ask AI to weigh in on how this fits your chain and your listening style.</p>
          <button class="ai-take-cta" data-ai-prompt>Ask AI ${PROFILE_ICONS.chev}</button>
        </div>` : '';

      this.el.innerHTML = `
        <div class="gear-sheet">
          <div class="sheet-handle"></div>
          <div class="gear-sheet-head">
            <div class="gear-sheet-title-block">
              <div class="gear-sheet-brand">${escapeProfileHtml(g.brand)}</div>
              <h2 class="gear-sheet-model">
                <span>${escapeProfileHtml(g.model)}</span>
                <button class="gear-sheet-rename" data-rename type="button" aria-label="edit brand and model">${PROFILE_ICONS.edit}</button>
              </h2>
              <div class="gear-sheet-meta-row">
                <span class="cat-tag">${categoryLabel(g.category)}</span>
                ${statusBadgeHTML(g.status)}
              </div>
            </div>
            <button class="icon-btn" data-close aria-label="close" style="margin-top:calc(4*var(--px));">${PROFILE_ICONS.close}</button>
          </div>
          <div class="status-segmented">${segments}</div>
          ${researchHTML}
          ${specsHTML}
          ${technologiesHTML}
          ${sentimentHTML}
          ${aiTakeHTML}
          <div class="notes-card">
            <div class="notes-title">My notes</div>
            <textarea class="notes-area${g.notes ? '' : ' placeholder'}" id="gearNotes" placeholder="Notes only you can see — pairings, dealer, serial, settings…">${escapeProfileHtml(g.notes || '')}</textarea>
          </div>
          <div style="display:flex;gap:calc(10*var(--px));padding:0 calc(16*var(--px)) calc(20*var(--px));">
            <button class="profile-btn secondary" data-delete>Remove from chain</button>
          </div>
        </div>
      `;

      this.el.querySelector('[data-close]').addEventListener('click', () => this.close());
      this.el.querySelector('[data-rename]').addEventListener('click', () => this.openRenameForm());
      this.el.querySelectorAll('[data-status]').forEach(btn => {
        btn.addEventListener('click', () => this.setStatus(btn.dataset.status));
      });
      const notes = this.el.querySelector('#gearNotes');
      let notesTimer = null;
      notes.addEventListener('input', () => {
        clearTimeout(notesTimer);
        notesTimer = setTimeout(() => this.saveNotes(notes.value), 600);
      });
      this.el.querySelector('[data-delete]').addEventListener('click', () => this.remove());
      const aiBtn = this.el.querySelector('[data-ai-prompt]');
      if (aiBtn) aiBtn.addEventListener('click', () => {
        this.close();
        if (typeof ai !== 'undefined' && ai.open) ai.open();
      });
    },
    openRenameForm() {
      const g = this.current;
      const overlay = document.createElement('div');
      overlay.className = 'add-gear-overlay';
      overlay.innerHTML = `
        <div class="add-gear-sheet">
          <div class="sheet-handle"></div>
          <div class="add-gear-head">
            <h2 class="add-gear-title">Edit brand &amp; model</h2>
            <button class="icon-btn" data-cancel aria-label="close">${PROFILE_ICONS.close}</button>
          </div>
          <div class="add-gear-row">
            <div style="position:relative;">
              <input class="add-gear-input" id="renameBrand" placeholder="Brand" value="${escapeProfileHtml(g.brand || '')}" autocomplete="off">
              <div id="renameBrandSuggest" style="position:absolute;top:calc(48*var(--px));left:0;right:0;background:var(--color-surface);border:1px solid var(--color-divider);border-radius:var(--radius-md);box-shadow:var(--shadow-2);z-index:5;max-height:calc(180*var(--px));overflow-y:auto;display:none;"></div>
            </div>
            <input class="add-gear-input" id="renameModel" placeholder="Model" value="${escapeProfileHtml(g.model || '')}">
            <div id="renameMsg" style="font-size:calc(12*var(--px));color:var(--color-text-dim);min-height:calc(16*var(--px));"></div>
            <button class="profile-btn primary" data-save>Save</button>
          </div>
        </div>
      `;
      document.body.appendChild(overlay);
      const close = () => overlay.remove();
      overlay.addEventListener('click', e => { if (e.target === overlay) close(); });
      overlay.querySelector('[data-cancel]').addEventListener('click', close);

      const brandInput = overlay.querySelector('#renameBrand');
      const modelInput = overlay.querySelector('#renameModel');
      const suggest = overlay.querySelector('#renameBrandSuggest');
      const msg = overlay.querySelector('#renameMsg');

      let t = null;
      const refresh = async () => {
        const q = brandInput.value.trim();
        if (!q) { suggest.style.display = 'none'; return; }
        try {
          const r = await fetch('/api/gear-models/brands/search?q=' + encodeURIComponent(q) + '&limit=8');
          if (!r.ok) throw new Error();
          const rows = await r.json();
          if (rows.length === 0 || (rows.length === 1 && rows[0].name.toLowerCase() === q.toLowerCase())) {
            suggest.style.display = 'none';
            return;
          }
          suggest.innerHTML = rows.map(r => `
            <div data-brand-name="${escapeProfileHtml(r.name)}"
                 style="padding:calc(10*var(--px)) calc(14*var(--px));font-size:calc(13.5*var(--px));color:var(--color-text);cursor:pointer;border-bottom:1px solid var(--color-divider);">
              ${escapeProfileHtml(r.name)}
            </div>
          `).join('');
          suggest.style.display = 'block';
          suggest.querySelectorAll('[data-brand-name]').forEach(el => {
            el.addEventListener('click', () => {
              brandInput.value = el.dataset.brandName;
              suggest.style.display = 'none';
              modelInput.focus();
            });
          });
        } catch (_) {
          suggest.style.display = 'none';
        }
      };
      brandInput.addEventListener('input', () => { clearTimeout(t); t = setTimeout(refresh, 120); });
      brandInput.addEventListener('blur', () => { setTimeout(() => { suggest.style.display = 'none'; }, 150); });
      brandInput.addEventListener('focus', refresh);

      const submit = async () => {
        const b = brandInput.value.trim();
        const m = modelInput.value.trim();
        if (!b || !m) {
          msg.style.color = 'var(--color-negative)';
          msg.textContent = 'Brand and model are required.';
          return;
        }
        msg.style.color = 'var(--color-text-muted)';
        msg.textContent = 'Saving…';
        try {
          const r = await fetch('/api/profile/gear/' + g.id + '/model', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ brand: b, model: m }),
          });
          if (!r.ok) {
            const txt = await r.text();
            msg.style.color = 'var(--color-negative)';
            msg.textContent = txt || 'Could not save.';
            return;
          }
          close();
          this.close();
          if (parseHash().startsWith('more/profile')) render();
        } catch (err) {
          msg.style.color = 'var(--color-negative)';
          msg.textContent = String(err);
        }
      };
      overlay.querySelector('[data-save]').addEventListener('click', submit);
      modelInput.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); submit(); } });
    },
    async setStatus(status) {
      try {
        await fetch('/api/profile/gear/' + this.current.id, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ status }),
        });
        this.current.status = status;
        this.render();
        if (parseHash().startsWith('more/profile')) render();
      } catch (_) {}
    },
    async saveNotes(notes) {
      try {
        await fetch('/api/profile/gear/' + this.current.id, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ notes }),
        });
        this.current.notes = notes;
      } catch (_) {}
    },
    async remove() {
      const label = `${this.current.brand} ${this.current.model}`;
      const ok = await confirmDestructive({
        title: 'Remove from chain?',
        message: `<b>${escapeProfileHtml(label)}</b> will be removed from your audio chain. The model stays in the shared catalog.`,
        confirmText: 'Remove',
      });
      if (!ok) return;
      try {
        const r = await fetch('/api/profile/gear/' + this.current.id, { method: 'DELETE' });
        if (!r.ok) {
          console.error('delete gear failed', r.status, await r.text());
          return;
        }
      } catch (err) {
        console.error('delete gear error', err);
        return;
      }
      this.close();
      if (parseHash().startsWith('more/profile')) render();
    },
  };

  async function renderProfileOther(root, pubkeyPrefix) {
    let profile = null;
    try {
      const r = await fetch('/api/profile/by-pubkey/' + encodeURIComponent(pubkeyPrefix));
      if (r.ok) profile = await r.json();
    } catch (_) {}

    if (!profile) {
      root.innerHTML = '<section class="screen"><div class="screen-head"><h2 class="screen-title">Profile</h2></div><div class="placeholder">Peer not found.</div></section>';
      return;
    }

    const display = profile.display_name || profile.username || '—';
    const initials = display.trim().split(/\s+/).map(w => w[0]).slice(0, 2).join('').toUpperCase() || '—';
    const handle = profile.username || '';
    const inviteTail = profile.invite_code ? '· ' + profile.invite_code.split('#').pop() : '';

    root.innerHTML = `
      <section class="screen screen-profile">
        <div class="profile-header">
          <button class="icon-btn" aria-label="back" data-back>${PROFILE_ICONS.back}</button>
          <h1>Profile</h1>
          <span></span>
        </div>

        <div class="identity-block">
          <div class="avatar-big">
            ${profile.avatar_cover_id
              ? `<img src="/api/covers/${escapeProfileHtml(profile.avatar_cover_id)}" alt="">`
              : initials}
          </div>
          <div>
            <h2 class="identity-name">${escapeProfileHtml(display)}</h2>
            ${(handle || inviteTail) ? `
              <div class="identity-handles">
                <span class="handle">${escapeProfileHtml(handle)}</span>
                ${inviteTail ? `<span class="invite">${escapeProfileHtml(inviteTail)}</span>` : ''}
              </div>` : ''}
          </div>
          ${(profile.city || profile.country) ? `<div class="identity-city">${PROFILE_ICONS.pin}${escapeProfileHtml([profile.city, countryName(profile.country)].filter(Boolean).join(', '))}</div>` : ''}
          ${profile.bio ? `<p class="identity-bio">${escapeProfileHtml(profile.bio)}</p>` : ''}
        </div>

        <div class="other-cta-row">
          <button class="profile-btn primary" data-action="message">Send message</button>
          <button class="profile-btn secondary" data-action="add-friend">Add as friend</button>
        </div>

        <p class="gear-visibility-note" style="margin-top:calc(28*var(--px));">Audio chain sharing is private for now (Phase 2).</p>
      </section>
    `;

    root.querySelector('[data-back]').addEventListener('click', () => {
      if (history.length > 1) history.back();
      else navigate('friends');
    });
    root.querySelector('[data-action="message"]').addEventListener('click', () => {
      if (profile.public_key_hex) navigate('friends/chat/' + profile.public_key_hex.slice(0, 16));
    });
  }

  /* =====================================================================
   * Settings (#more/settings)
   * Mirrors docs/design/reference/claude-design-bundle/project/
   * Settings.html. Four stacked sections (Library / AI assistant /
   * Sync & P2P / Audio output) with three rendering states driven by
   * the /api/settings payload: default, in-flight (scanning), empty
   * library (first-run).
   * ===================================================================== */

  const SETTINGS_ICONS = {
    refresh: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 11-3-6.7M21 4v5h-5"/></svg>',
    check:   '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M5 13l4 4L19 7"/></svg>',
    chev:    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg>',
    rightCh: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 6l6 6-6 6"/></svg>',
    vinyl:   '<svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M9 17V5l11-2v12"/><circle cx="6" cy="17" r="3"/><circle cx="17" cy="15" r="3"/></svg>',
  };

  const PROVIDER_OPTIONS = [
    { id: 'claude_code', label: 'Claude Code (subscription)' },
    { id: 'anthropic',   label: 'Claude API' },
    { id: 'openai',      label: 'OpenAI' },
  ];
  // IDs here must match what each provider's models() method returns —
  // _resolve_model on the backend bails to models[0] when the picked
  // id isn't in the list, which silently flipped Anthropic users from
  // Haiku to Sonnet. Long-term we should drive these from
  // /api/chat/providers so the UI is auto-synced.
  const MODEL_OPTIONS = {
    // Claude Code CLI accepts the short --model alias only.
    claude_code: [
      { id: 'sonnet', label: 'Sonnet' },
      { id: 'haiku',  label: 'Haiku' },
    ],
    anthropic: [
      { id: 'claude-sonnet-4-20250514',  label: 'Sonnet 4' },
      { id: 'claude-haiku-4-5-20251001', label: 'Haiku 4.5' },
    ],
    openai: [
      { id: 'gpt-4o',      label: 'GPT-4o' },
      { id: 'gpt-4o-mini', label: 'GPT-4o mini' },
    ],
  };
  const AUTO_SYNC_OPTIONS = [
    { id: 0,   label: 'Disabled' },
    { id: 15,  label: 'Every 15 min' },
    { id: 30,  label: 'Every 30 min' },
    { id: 60,  label: 'Every 1 hour' },
    { id: 240, label: 'Every 4 hours' },
  ];
  const ANNOUNCE_LIMIT_OPTIONS = [
    { id: 0,   label: 'All artists' },
    { id: 10,  label: '10 artists' },
    { id: 50,  label: '50 artists' },
    { id: 200, label: '200 artists' },
  ];
  const ROTATION_OPTIONS = [
    { id: 15, label: 'Every 15 min' },
    { id: 30, label: 'Every 30 min' },
    { id: 60, label: 'Every 1 hour' },
  ];

  function fmtPathForDisplay(p) {
    // Windows paths carry a drive-letter prefix (C:, E:, …). The
    // launcher stores them with forward slashes for Docker / config
    // portability ("E:/Music"); for human display we flip those to
    // backslashes ("E:\\Music"). POSIX paths (/Volumes/..., /home/...)
    // keep forward slashes regardless of where the UI is viewed.
    if (!p) return '';
    if (/^[A-Za-z]:[/\\]/.test(p)) return p.replace(/\//g, '\\');
    return p;
  }
  function fmtPathTruncatedFromStart(p, max = 27) {
    // Truncate from the head, not the tail — the meaningful part of
    // a music-library path is the folder name at the end. The full
    // string remains accessible via the title= tooltip in markup.
    const formatted = fmtPathForDisplay(p);
    if (formatted.length <= max) return formatted;
    return '…' + formatted.slice(-(max - 1));
  }

  function fmtBytes(n) {
    // Binary (1024-based) sizing matches Windows Explorer / macOS
    // Finder so the figure shown here lines up with whatever the
    // user sees in the OS file manager on their music folder.
    n = Number(n) || 0;
    const TB = 1024 ** 4, GB = 1024 ** 3, MB = 1024 ** 2, KB = 1024;
    if (n >= TB) return (n / TB).toFixed(2) + ' TB';
    if (n >= GB) return (n / GB).toFixed(1) + ' GB';
    if (n >= MB) return (n / MB).toFixed(0) + ' MB';
    if (n >= KB) return (n / KB).toFixed(0) + ' KB';
    return n + ' B';
  }
  function fmtNum(n) {
    return Number(n || 0).toLocaleString('en-US').replace(/,/g, ' ');
  }
  function fmtRelative(iso) {
    if (!iso) return '—';
    const t = new Date(iso).getTime();
    if (!t) return '—';
    const diff = Math.round((Date.now() - t) / 1000);
    if (diff < 60)   return 'moments ago';
    if (diff < 3600) return Math.round(diff / 60) + ' min ago';
    if (diff < 86400) return Math.round(diff / 3600) + ' hours ago';
    return Math.round(diff / 86400) + ' days ago';
  }
  function fmtAbs(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    if (isNaN(d)) return '';
    const yyyy = d.getFullYear();
    const mm = String(d.getMonth() + 1).padStart(2, '0');
    const dd = String(d.getDate()).padStart(2, '0');
    const hh = String(d.getHours()).padStart(2, '0');
    const mi = String(d.getMinutes()).padStart(2, '0');
    return `${yyyy}-${mm}-${dd} · ${hh}:${mi}`;
  }

  /* Picker sheet — generic single-select. Returns Promise<id|null>. */
  function openSettingsPicker({ title, options, currentId }) {
    return new Promise(resolve => {
      const overlay = document.createElement('div');
      overlay.className = 'add-gear-overlay';
      overlay.innerHTML = `
        <div class="add-gear-sheet">
          <div class="sheet-handle"></div>
          <div class="add-gear-head">
            <h2 class="add-gear-title">${escapeProfileHtml(title)}</h2>
            <button class="icon-btn" data-cancel aria-label="close">${PROFILE_ICONS.close}</button>
          </div>
          <div class="add-gear-results">
            ${options.map(o => `
              <div class="add-gear-result" data-pick="${escapeProfileHtml(String(o.id))}">
                <div>
                  <div class="gear-line1">
                    <span class="gear-model">${escapeProfileHtml(o.label)}</span>
                  </div>
                </div>
                ${String(o.id) === String(currentId) ? `<span style="color:var(--color-amber);">${SETTINGS_ICONS.check}</span>` : `<span class="gear-chev">${SETTINGS_ICONS.rightCh}</span>`}
              </div>
            `).join('')}
          </div>
        </div>
      `;
      document.body.appendChild(overlay);
      const close = (id) => { overlay.remove(); resolve(id); };
      overlay.addEventListener('click', e => { if (e.target === overlay) close(null); });
      overlay.querySelector('[data-cancel]').addEventListener('click', () => close(null));
      overlay.querySelectorAll('[data-pick]').forEach(el => {
        el.addEventListener('click', () => close(el.dataset.pick));
      });
    });
  }

  /* API-key entry modal. Provider-aware helper link. */
  function openApiKeyModal(provider, onSaved) {
    const isAnthropic = provider === 'anthropic';
    const placeholder = isAnthropic ? 'sk-ant-...' : 'sk-...';
    const helpLink = isAnthropic
      ? '<a href="https://console.anthropic.com/" target="_blank" rel="noopener" style="color:var(--color-amber);">console.anthropic.com</a>'
      : '<a href="https://platform.openai.com/api-keys" target="_blank" rel="noopener" style="color:var(--color-amber);">platform.openai.com</a>';

    const overlay = document.createElement('div');
    overlay.className = 'add-gear-overlay';
    overlay.innerHTML = `
      <div class="add-gear-sheet">
        <div class="sheet-handle"></div>
        <div class="add-gear-head">
          <h2 class="add-gear-title">Connect ${escapeProfileHtml(isAnthropic ? 'Claude' : 'OpenAI')} API key</h2>
          <button class="icon-btn" data-cancel aria-label="close">${PROFILE_ICONS.close}</button>
        </div>
        <div class="add-gear-row">
          <input class="add-gear-input" id="apiKeyInput" type="password" placeholder="${placeholder}" maxlength="280" autocomplete="off" spellcheck="false">
          <p style="margin:0;color:var(--color-text-muted);font-size:calc(12.5*var(--px));line-height:1.45;">
            Get a key at ${helpLink}. Stored locally; sent only to ${escapeProfileHtml(isAnthropic ? 'Anthropic' : 'OpenAI')}.
          </p>
          <div id="apiKeyMsg" style="font-size:calc(12*var(--px));color:var(--color-text-dim);min-height:calc(16*var(--px));"></div>
          <button class="btn btn-primary" data-save>Save</button>
        </div>
      </div>
    `;
    document.body.appendChild(overlay);
    const input = overlay.querySelector('#apiKeyInput');
    const msg = overlay.querySelector('#apiKeyMsg');
    setTimeout(() => input.focus(), 50);

    const close = () => overlay.remove();
    overlay.addEventListener('click', e => { if (e.target === overlay) close(); });
    overlay.querySelector('[data-cancel]').addEventListener('click', close);

    const submit = async () => {
      const key = input.value.trim();
      if (!key) {
        msg.style.color = 'var(--color-negative)';
        msg.textContent = 'Paste a key first.';
        return;
      }
      msg.style.color = 'var(--color-text-muted)';
      msg.textContent = 'Saving…';
      try {
        const r = await fetch('/api/settings/ai/key', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ api_key: key }),
        });
        if (!r.ok) {
          msg.style.color = 'var(--color-negative)';
          msg.textContent = (await r.text()) || 'Save failed.';
          return;
        }
        close();
        if (typeof onSaved === 'function') onSaved();
      } catch (err) {
        msg.style.color = 'var(--color-negative)';
        msg.textContent = String(err);
      }
    };
    overlay.querySelector('[data-save]').addEventListener('click', submit);
    input.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); submit(); } });
  }

  /* Library section renderers — full vs first-run-empty. */
  /* 2×2 stat cell builders for the Library screen. */
  function _statCell(label, value) {
    return `
      <div class="stat-cell">
        <span class="lbl">${escapeProfileHtml(label)}</span>
        <span class="val">${escapeProfileHtml(String(value))}</span>
      </div>`;
  }
  function _enrichRowValueHTML(done, total) {
    if (!total || total <= 0) {
      return { className: 'form-value muted', html: '—' };
    }
    // Floor, not round: 1827/1829 must read 99%, not 100%. 100% is
    // reserved for done === total — otherwise the UI lies about
    // completeness when there's a tiny gap left (e.g. 71 tracks
    // missing features but rounded up because the gap is < 0.5%).
    const pct = done >= total ? 100 : Math.floor((done * 100) / total);
    const cls = pct === 100 ? 'pct-full' : pct >= 80 ? 'pct-near' : 'pct-low';
    return {
      className: `form-value mono ${cls}`,
      html: `${fmtNum(done)} / ${fmtNum(total)}<span class="pct-suffix">· ${pct}%</span>`,
    };
  }
  function _enrichRow(key, label, done, total) {
    const v = _enrichRowValueHTML(done, total);
    return `
      <div class="form-row" data-enrich-row="${key}">
        <span class="form-label">${escapeProfileHtml(label)}</span>
        <span class="${v.className}">${v.html}</span>
      </div>`;
  }
  function _refreshEnrichRow(root, key, done, total) {
    const row = root.querySelector(`[data-enrich-row="${key}"]`);
    if (!row) return;
    const value = row.querySelector('.form-value');
    if (!value) return;
    const v = _enrichRowValueHTML(done, total);
    if (value.className !== v.className) value.className = v.className;
    if (value.innerHTML !== v.html) value.innerHTML = v.html;
  }

  function _renderLibraryFull(lib) {
    const scanRunning = !!(lib.scan && lib.scan.running);
    const enrichRunning = !!(lib.enrich && lib.enrich.running);
    const tracks = lib.total_tracks || 0;
    const artists = lib.total_artists || 0;
    const totalArtists = artists;
    const withBio = lib.artists_with_bio || 0;
    const bioPct = totalArtists > 0 ? Math.round((withBio / totalArtists) * 100) : 0;
    const totalTracks = tracks;
    const withAudio = lib.tracks_with_audio || 0;
    const audioPct = totalTracks > 0 ? Math.round((withAudio / totalTracks) * 100) : 0;
    const path = fmtPathForDisplay(lib.music_path || '/music');
    const lastScan = lib.last_scan_at;

    return `
      <div class="form-group">
        <div class="form-row stacked">
          <div class="row-stack">
            <span class="row-stack-label">Music path</span>
            <span class="path-value" title="${escapeProfileHtml(path)}">${escapeProfileHtml(path)}</span>
          </div>
          <div class="row-stack-sub">Configured in the launcher.</div>
        </div>
        <div class="form-row">
          <span class="form-label">Tracks</span>
          <span class="form-value mono">${fmtNum(tracks)}</span>
        </div>
        <div class="form-row">
          <span class="form-label">Artists</span>
          <span class="form-value mono">${fmtNum(artists)}</span>
        </div>
        <div class="form-row">
          <span class="form-label">Albums</span>
          <span class="form-value mono">${fmtNum(lib.total_albums || 0)}</span>
        </div>
        <div class="form-row">
          <span class="form-label">Storage</span>
          <span class="form-value mono">${escapeProfileHtml(fmtBytes(lib.total_size_bytes))}</span>
        </div>
        <div class="enrich-row">
          <div class="enrich-line">
            <span class="label">Artist bios</span>
            <span class="val">${fmtNum(withBio)} <span class="of">/ ${fmtNum(totalArtists)}</span></span>
          </div>
          <div class="enrich-bar"><div class="fill" style="width:${bioPct}%;"></div></div>
        </div>
        <div class="enrich-row">
          <div class="enrich-line">
            <span class="label">Tracks analysed</span>
            <span class="val">${fmtNum(withAudio)} <span class="of">/ ${fmtNum(totalTracks)}</span></span>
          </div>
          <div class="enrich-bar"><div class="fill" style="width:${audioPct}%;"></div></div>
        </div>
        <div class="form-row stacked">
          <div class="row-stack">
            <span class="row-stack-label">Last scan</span>
            <span class="row-stack-value">${scanRunning ? 'running…' : escapeProfileHtml(fmtRelative(lastScan))}</span>
          </div>
          ${(!scanRunning && lastScan) ? `<div class="row-stack-sub" style="font-family:var(--font-mono);letter-spacing:0.02em;">${escapeProfileHtml(fmtAbs(lastScan))}</div>` : ''}
        </div>
      </div>

      ${scanRunning ? `
        <div class="progress-strip">
          <div class="head">
            <span class="label">Scanning library</span>
            <span class="stats">${escapeProfileHtml((lib.scan.stats && lib.scan.stats.scanned) || lib.scan.progress || '…')}</span>
          </div>
          <div class="bar"><div class="fill" style="width:36%;"></div></div>
          <div class="cancel"><button data-cancel-scan>Cancel</button></div>
        </div>
        <div class="btn-row single" style="margin-top:calc(10*var(--px));">
          <button class="btn btn-secondary" ${enrichRunning ? 'disabled' : ''} data-action="enrich">Re-enrich missing</button>
        </div>
      ` : `
        <div class="btn-row">
          <button class="btn btn-primary" data-action="scan">Rescan library</button>
          <button class="btn btn-secondary" ${enrichRunning ? 'disabled' : ''} data-action="enrich">Re-enrich missing</button>
        </div>
      `}
    `;
  }
  function _renderLibraryEmpty(lib) {
    const path = fmtPathForDisplay(lib.music_path || '/music');
    return `
      <div class="form-group">
        <div class="form-row stacked">
          <div class="row-stack">
            <span class="row-stack-label">Music path</span>
            <span class="path-value" title="${escapeProfileHtml(path)}">${escapeProfileHtml(path)}</span>
          </div>
          <div class="row-stack-sub">Configured in the launcher.</div>
        </div>
      </div>
      <div class="empty-library">
        <div class="icon">${SETTINGS_ICONS.vinyl}</div>
        <p class="empty-library-msg">
          No tracks indexed yet. Run the first scan to discover your library — it usually takes a few minutes per 10k tracks.
        </p>
        <button class="empty-cta" data-action="scan">${SETTINGS_ICONS.refresh}Run first scan</button>
      </div>
    `;
  }

  function _renderAi(ai, claudeState) {
    const provider = ai.provider || '';
    const hasProvider = !!provider;
    const isClaudeCode = provider === 'claude_code';
    const providerLabel = hasProvider
      ? ((PROVIDER_OPTIONS.find(p => p.id === provider) || {}).label || provider)
      : 'Not selected';
    const model = ai.model || '';
    const modelLabel = ((MODEL_OPTIONS[provider] || []).find(m => m.id === model) || {}).label || model || '—';
    const auth = ai.auth_state || 'not_authenticated';
    const usage = ai.usage;

    let authRow;
    if (auth === 'oauth_signed_in') {
      const expires = ai.expires_in_days != null ? `<span style="color:var(--color-text-muted);font-size:calc(12*var(--px));">· expires in <span style="font-family:var(--font-mono);color:var(--color-blue);font-size:calc(11.5*var(--px));letter-spacing:0.02em;">${ai.expires_in_days} days</span></span>` : '';
      authRow = `
        <div class="form-row stacked">
          <div class="row-stack">
            <span class="row-stack-label">Authentication</span>
            <button class="btn-link" data-action="reauthorize">Reauthorize</button>
          </div>
          <div class="row-stack-value" style="display:flex;align-items:center;gap:calc(8*var(--px));flex-wrap:wrap;">
            <span style="color:var(--color-positive);font-family:var(--font-mono);font-size:calc(12*var(--px));letter-spacing:0.02em;"><span class="status-dot green"></span>Signed in</span>
            ${expires}
          </div>
        </div>`;
    } else if (auth === 'api_key_set') {
      authRow = `
        <div class="form-row stacked">
          <div class="row-stack">
            <span class="row-stack-label">Authentication</span>
            <button class="btn-link" data-action="replace-key">Replace</button>
          </div>
          <div class="row-stack-value" style="display:flex;align-items:center;gap:calc(8*var(--px));">
            <span style="color:var(--color-positive);font-family:var(--font-mono);font-size:calc(12*var(--px));letter-spacing:0.02em;"><span class="status-dot green"></span>API key set</span>
            <span style="font-family:var(--font-mono);color:var(--color-blue);font-size:calc(12*var(--px));letter-spacing:0.04em;">${escapeProfileHtml(ai.masked_key || '')}</span>
          </div>
        </div>`;
    } else {
      authRow = `
        <div class="form-row stacked">
          <div class="row-stack">
            <span class="row-stack-label">Authentication</span>
            <span></span>
          </div>
          <div class="row-stack-value" style="color:var(--color-text-muted);font-size:calc(12.5*var(--px));">Not signed in</div>
          <div class="btn-row single" style="margin:calc(10*var(--px)) 0 0;">
            <button class="btn btn-primary" data-action="sign-in">Sign in / set key</button>
          </div>
        </div>`;
    }

    const usageRow = (usage && usage.spent != null) ? `
      <div class="form-row stacked">
        <div class="row-stack">
          <span class="row-stack-label">Usage</span>
          <span style="font-family:var(--font-mono);font-size:calc(11*var(--px));color:var(--color-text-muted);letter-spacing:0.04em;text-transform:uppercase;">monthly</span>
        </div>
        <div class="row-stack-value" style="display:flex;align-items:baseline;gap:calc(6*var(--px));">
          <span style="font-family:var(--font-mono);color:var(--color-blue);font-size:calc(14*var(--px));font-weight:600;">$${escapeProfileHtml(String(usage.spent))}</span>
          <span style="color:var(--color-text-muted);font-size:calc(12.5*var(--px));">of <span style="font-family:var(--font-mono);color:var(--color-blue);">$${escapeProfileHtml(String(usage.limit))}</span> limit${usage.days_left != null ? ` · <span style="font-family:var(--font-mono);color:var(--color-blue);">${usage.days_left}d</span> left` : ''}</span>
        </div>
        ${usage.limit ? `<div class="enrich-bar" style="margin-top:calc(8*var(--px));"><div class="fill" style="width:${Math.round((usage.spent / usage.limit) * 100)}%;"></div></div>` : ''}
      </div>` : '';

    const isUnauth = auth === 'not_authenticated';
    const ccBlock = (isClaudeCode && claudeState) ? _renderClaudeCodeBlock(claudeState) : '';
    const ccReady = (isClaudeCode && claudeState && claudeState.state === 'ready');
    const ccBlocksAuth = (isClaudeCode && claudeState && claudeState.state !== 'ready');
    return `
      <div class="form-group">
        <div class="form-row">
          <span class="form-label">Provider</span>
          <button class="select-trigger ${hasProvider ? '' : 'muted'}" data-action="pick-provider">
            ${escapeProfileHtml(providerLabel)}
            <span class="chev">${SETTINGS_ICONS.chev}</span>
          </button>
        </div>
        <div class="form-row">
          <span class="form-label">Model</span>
          ${!hasProvider
            ? '<span class="form-value muted">Select provider first</span>'
            : ccBlocksAuth
              ? '<span class="form-value muted">Sign in to choose</span>'
              : (isClaudeCode && ccReady)
                ? `<button class="select-trigger" data-action="pick-model">
                     ${escapeProfileHtml(modelLabel)}
                     <span class="chev">${SETTINGS_ICONS.chev}</span>
                   </button>`
                : isUnauth
                  ? '<span class="form-value muted">Sign in to choose</span>'
                  : `<button class="select-trigger" data-action="pick-model">
                       ${escapeProfileHtml(modelLabel)}
                       <span class="chev">${SETTINGS_ICONS.chev}</span>
                     </button>`}
        </div>
        ${isClaudeCode ? ccBlock : (hasProvider ? authRow : '')}
        ${hasProvider && !isClaudeCode ? usageRow : ''}
      </div>
    `;
  }

  function _renderClaudeCodeBlock(cc) {
    const s = cc.state;
    const installing = cc.install && cc.install.running;
    const installErr = cc.install && cc.install.error;
    if (s === 'ready') {
      return `
        <div class="form-row stacked">
          <div class="row-stack">
            <span class="row-stack-label">Authentication</span>
            <button class="btn-link" data-action="cc-signin">Reauthorize</button>
          </div>
          <div class="row-stack-value" style="display:flex;align-items:center;gap:calc(8*var(--px));">
            <span style="color:var(--color-positive);font-family:var(--font-mono);font-size:calc(12*var(--px));letter-spacing:0.02em;"><span class="status-dot green"></span>Signed in via subscription</span>
          </div>
          <div class="row-stack-sub">No API key needed. Plays nicely with the Claude Code CLI.</div>
        </div>`;
    }
    if (s === 'host_unsupported') {
      return `
        <div class="form-row stacked">
          <div class="row-stack">
            <span class="row-stack-label">Setup</span>
            <span></span>
          </div>
          <div class="row-stack-value" style="color:var(--color-text-muted);font-size:calc(12.5*var(--px));line-height:1.5;">
            Claude Code installation needs native access to your machine. Open the <b>Sautium Desktop Launcher</b> → <b>Settings</b> → <b>AI provider</b> to install and sign in.
          </div>
        </div>`;
    }
    if (s === 'node_missing') {
      return `
        <div class="form-row stacked">
          <div class="row-stack">
            <span class="row-stack-label">Setup</span>
            <button class="btn-link" data-action="cc-refresh">Refresh</button>
          </div>
          <div class="row-stack-value" style="color:var(--color-text-muted);font-size:calc(12.5*var(--px));line-height:1.5;">
            Node.js 18+ is required for Claude Code. Re-run the Sautium installer (it bundles Node) or install Node.js manually, then tap Refresh.
          </div>
        </div>`;
    }
    if (s === 'claude_missing') {
      return `
        <div class="form-row stacked">
          <div class="row-stack">
            <span class="row-stack-label">Setup</span>
            <span></span>
          </div>
          <div class="row-stack-value" style="color:var(--color-text-muted);font-size:calc(12.5*var(--px));line-height:1.5;">
            Claude Code is not installed yet. Downloads ~5 MB via npm.
          </div>
          ${installErr ? `<div class="row-stack-sub" style="color:var(--color-negative);white-space:pre-wrap;">${escapeProfileHtml(installErr)}</div>` : ''}
          <div class="btn-row single" style="margin:calc(10*var(--px)) 0 0;">
            <button class="btn btn-primary" data-action="cc-install" ${installing ? 'disabled' : ''}>${installing ? 'Installing…' : 'Install Claude Code'}</button>
          </div>
        </div>`;
    }
    if (s === 'not_authed') {
      return `
        <div class="form-row stacked">
          <div class="row-stack">
            <span class="row-stack-label">Sign in</span>
            <button class="btn-link" data-action="cc-refresh">Refresh</button>
          </div>
          <div class="row-stack-value" style="color:var(--color-text-muted);font-size:calc(12.5*var(--px));line-height:1.6;">
            Sautium will open a terminal running <b>claude</b>. In it:
            <ol style="margin:calc(6*var(--px)) 0 0;padding-left:calc(18*var(--px));">
              <li>Pick a theme (first run only)</li>
              <li>Type <code style="font-family:var(--font-mono);color:var(--color-blue);">/login</code></li>
              <li>Choose <i>Claude account with subscription</i></li>
              <li>Authorize in the browser tab</li>
            </ol>
            Sautium detects the sign-in automatically.
          </div>
          <div class="btn-row single" style="margin:calc(10*var(--px)) 0 0;">
            <button class="btn btn-primary" data-action="cc-signin">Sign in to Claude</button>
          </div>
        </div>`;
    }
    return '';
  }

  function _bgEnrichStatusLine(s, enabled) {
    // Status under the Background enrichment toggle. Three states:
    //   - toggle off → nothing (the row above already says "off")
    //   - on + idle → "Idle · last run X min ago · 17 stats, 4 lyrics"
    //   - on + busy → "Working on track stats · 17 stats, 4 lyrics so far"
    if (!enabled || !s) return '';
    const totals = s.totals || {};
    const partsArr = [];
    if (totals.track_stats) partsArr.push(totals.track_stats + ' stats');
    if (totals.lyrics)      partsArr.push(totals.lyrics      + ' lyrics');
    if (totals.artists)     partsArr.push(totals.artists     + ' artists');
    if (totals.albums)      partsArr.push(totals.albums      + ' albums');
    if (totals.genres)      partsArr.push(totals.genres      + ' genres');
    const totalsLine = partsArr.length ? partsArr.join(', ') : 'no items yet';

    const stepLabels = {
      track_stats: 'fetching track stats',
      lyrics:      'fetching lyrics',
      artists:     'fetching artist info',
      albums:      'fetching album info',
      genres:      'fetching genre wikis',
      idle:        'idle',
      starting:    'starting',
      '':          'idle',
    };
    const step = stepLabels[s.current_step] || s.current_step;
    const isWorking = s.current_step && s.current_step !== 'idle' && s.current_step !== '';

    let primary;
    if (isWorking) {
      primary = 'Working · ' + step;
    } else if (s.last_run_at) {
      primary = 'Idle · last batch ' + fmtRelative(s.last_run_at);
    } else {
      primary = 'Idle · awaiting first batch';
    }

    return `
        <div class="form-row stacked">
          <div class="row-stack">
            <span class="row-stack-label" style="color:var(--color-text-muted);font-size:calc(12*var(--px));">${escapeProfileHtml(primary)}</span>
            <span></span>
          </div>
          <div class="row-stack-sub" style="color:var(--color-text-dim);font-size:calc(11.5*var(--px));">${escapeProfileHtml(totalsLine)}</div>
        </div>`;
  }

  function _renderSync(sync) {
    const on = !!sync.p2p_enabled;
    const interval = sync.auto_interval_min;
    const intervalLabel = (AUTO_SYNC_OPTIONS.find(o => o.id === (interval || 0)) || AUTO_SYNC_OPTIONS[0]).label;
    const limit = sync.announce_limit;
    const limitLabel = (ANNOUNCE_LIMIT_OPTIONS.find(o => o.id === (limit || 0)) || ANNOUNCE_LIMIT_OPTIONS[0]).label;
    const rotation = sync.announce_rotation_min || 30;
    const rotationLabel = (ROTATION_OPTIONS.find(o => o.id === rotation) || ROTATION_OPTIONS[1]).label;
    const bgEnrich = !!sync.background_enrichment;
    const bgStatusLine = _bgEnrichStatusLine(sync.background_status, bgEnrich);
    const showRotation = !!limit;  // hidden when "All"

    if (!on) {
      // Sharing OFF — consolidated explainer in place of the three
      // detail rows. Background enrichment toggle still visible
      // because it's not gated on P2P.
      return `
        <div class="form-group">
          <div class="form-row">
            <span class="form-label">P2P sharing</span>
            <button class="toggle ${on ? 'on' : ''}" data-action="toggle-p2p"><span class="knob"></span></button>
          </div>
          <div class="form-row stacked">
            <div class="row-stack">
              <span class="row-stack-label" style="color:var(--color-text-dim);">Auto-sync · Announce · Rotation</span>
              <span></span>
            </div>
            <div class="row-stack-sub">Turn P2P sharing on to configure sync and node-announce limits.</div>
          </div>
          <div class="form-row">
            <span class="form-label">Background enrichment</span>
            <button class="toggle ${bgEnrich ? 'on' : ''}" data-action="toggle-bg-enrich"><span class="knob"></span></button>
          </div>
          ${bgStatusLine}
        </div>
      `;
    }

    return `
      <div class="form-group">
        <div class="form-row">
          <span class="form-label">P2P sharing</span>
          <button class="toggle on" data-action="toggle-p2p"><span class="knob"></span></button>
        </div>
        <div class="form-row">
          <span class="form-label">Auto-sync interval</span>
          <button class="select-trigger" data-action="pick-interval">
            ${escapeProfileHtml(intervalLabel)}
            <span class="chev">${SETTINGS_ICONS.chev}</span>
          </button>
        </div>
        <div class="form-row">
          <span class="form-label">Announce limit</span>
          <button class="select-trigger" data-action="pick-limit">
            ${escapeProfileHtml(limitLabel)}
            <span class="chev">${SETTINGS_ICONS.chev}</span>
          </button>
        </div>
        ${showRotation ? `
        <div class="form-row">
          <span class="form-label">Announce rotation</span>
          <button class="select-trigger" data-action="pick-rotation">
            ${escapeProfileHtml(rotationLabel)}
            <span class="chev">${SETTINGS_ICONS.chev}</span>
          </button>
        </div>` : ''}
        <div class="form-row">
          <span class="form-label">Background enrichment</span>
          <button class="toggle ${bgEnrich ? 'on' : ''}" data-action="toggle-bg-enrich"><span class="knob"></span></button>
        </div>
        <div class="form-row stacked">
          <div class="row-stack">
            <span class="row-stack-label">Last sync</span>
            <span style="color:var(--color-text-muted);font-size:calc(12*var(--px));">${escapeProfileHtml(fmtRelative(sync.last_sync_at))}</span>
          </div>
          ${sync.last_items_received != null ? `<div class="row-stack-value" style="margin-top:calc(2*var(--px));">
            <span style="font-family:var(--font-mono);color:var(--color-blue);font-size:calc(12.5*var(--px));letter-spacing:0.02em;">${fmtNum(sync.last_items_received)} new ${sync.last_items_received === 1 ? 'item' : 'items'}</span>
          </div>` : ''}
        </div>
        <div class="form-row">
          <span class="form-label">Friends online</span>
          <span class="form-value mono">${fmtNum(sync.friends_online || 0)} <span style="color:var(--color-text-dim);">/ ${fmtNum(sync.friends_total || 0)}</span></span>
        </div>
      </div>
      <div class="btn-row single">
        <button class="btn btn-secondary" data-action="force-sync"${_syncInFlight ? ' disabled' : ''}>${_syncInFlight ? 'Syncing…' : 'Force sync now'}</button>
      </div>
    `;
  }

  // Module-scope state for the async Force sync flow. _syncInFlight
  // keeps the button in "Syncing…" state across re-renders triggered
  // by /api/settings/library/stream wake events (the same SSE channel
  // backed by sautium_sync_done). _syncBaselineLastAt snapshots the
  // last_sync_at value at trigger time so the next render that sees
  // a different last_sync_at recognises completion and shows the
  // "Synced · N items" toast — no polling, no setTimeout.
  let _syncInFlight = false;
  let _syncBaselineLastAt = null;

  function _renderHqpRow(ao, connected) {
    const host = ao.hqplayer_host || '—';
    const port = ao.hqplayer_port || '—';
    let statusBlock;
    if (connected === null || connected === undefined) {
      // unknown — initial render, waiting on /api/hqplayer/state
      statusBlock = `<span style="color:var(--color-text-muted);font-family:var(--font-mono);font-size:calc(12*var(--px));letter-spacing:0.02em;">checking…</span>
                     <span style="font-family:var(--font-mono);color:var(--color-blue);font-size:calc(11.5*var(--px));letter-spacing:0.02em;">${escapeProfileHtml(host)}:${escapeProfileHtml(String(port))}</span>`;
    } else if (connected) {
      statusBlock = `<span style="color:var(--color-positive);font-family:var(--font-mono);font-size:calc(12*var(--px));letter-spacing:0.02em;"><span class="status-dot green"></span>Connected</span>
                     <span style="font-family:var(--font-mono);color:var(--color-blue);font-size:calc(11.5*var(--px));letter-spacing:0.02em;">${escapeProfileHtml(host)}:${escapeProfileHtml(String(port))}</span>`;
    } else {
      statusBlock = `<span style="color:var(--color-negative);font-family:var(--font-mono);font-size:calc(12*var(--px));letter-spacing:0.02em;"><span class="status-dot red"></span>Disconnected</span>
                     <span style="color:var(--color-text-muted);font-size:calc(12*var(--px));">— check launcher</span>`;
    }
    return `
      <div class="form-row stacked" data-action="open-hqplayer" data-row="hqp" style="cursor:pointer;">
        <div class="row-stack">
          <span class="row-stack-label">HQPlayer</span>
          <span style="color:var(--color-text-dim);display:inline-flex;">${SETTINGS_ICONS.rightCh}</span>
        </div>
        <div class="row-stack-value" style="display:flex;align-items:center;gap:calc(8*var(--px));flex-wrap:wrap;">
          ${statusBlock}
        </div>
      </div>`;
  }
  function _renderAudioOutput(ao) {
    return `
      <div class="form-group">
        ${_renderHqpRow(ao, null)}
        <div class="form-row disabled stacked">
          <div class="row-stack">
            <span class="row-stack-label">Native output<span class="coming-soon-tag">Coming soon</span></span>
            <span></span>
          </div>
          <div class="row-stack-value">
            <span style="font-family:var(--font-mono);font-size:calc(11.5*var(--px));color:var(--color-text-dim);letter-spacing:0.04em;">WASAPI · ASIO · CoreAudio · NAA</span>
          </div>
        </div>
      </div>
    `;
  }
  async function _refreshHqpRow(root, ao) {
    let connected = null;
    try {
      const r = await fetch('/api/hqplayer/state');
      if (r.ok) {
        const data = await r.json();
        connected = !!data.connected;
      } else {
        connected = false;
      }
    } catch (_) {
      connected = false;
    }
    const old = root.querySelector('[data-row="hqp"]');
    if (!old) return;
    const wrap = document.createElement('template');
    wrap.innerHTML = _renderHqpRow(ao, connected).trim();
    const fresh = wrap.content.firstElementChild;
    fresh.addEventListener('click', () => navigate('more/hqplayer'));
    old.replaceWith(fresh);
  }

  /* Render entrypoint. */
  const _settingsHeader = (title) => `
    <div class="profile-header">
      <button class="icon-btn" aria-label="back" data-back>${PROFILE_ICONS.back}</button>
      <h1>${escapeProfileHtml(title)}</h1>
      <span></span>
    </div>`;

  function _wireBack(root) {
    root.querySelector('[data-back]').addEventListener('click', () => {
      if (history.length > 1) history.back();
      else navigate('home');
    });
  }

  /* ============ Library screen — #more/library ============ */
  async function renderLibrary(root) {
    let lib = null;
    try {
      const r = await fetch('/api/settings/library');
      if (r.ok) lib = await r.json();
    } catch (_) {}
    if (!lib) {
      root.innerHTML = `<section class="screen screen-settings">${_settingsHeader('Library')}<div class="placeholder">Не вдалося завантажити статистику.</div></section>`;
      _wireBack(root);
      return;
    }

    const scanProgress   = String((lib.scan   && lib.scan.progress)   || '');
    const enrichProgress = String((lib.enrich && lib.enrich.progress) || '');
    const _terminalRe    = /(complete|failed|cancelled)/i;
    // Mirror the polling-tick logic: if progress text already reads
    // as a terminal state, treat the worker as done even when the
    // running flag is still true (worker lingering between
    // "Scan complete" and the finally-block that resets the flag).
    const scanRunning   = !!(lib.scan   && lib.scan.running)   && !_terminalRe.test(scanProgress);
    const enrichRunning = !!(lib.enrich && lib.enrich.running) && !_terminalRe.test(enrichProgress);
    const isEmpty       = (lib.total_tracks || 0) === 0;
    // Launcher writes MUSIC_LIBRARY_PATH= (blank) into .env when the
    // user hasn't picked a folder yet; Pydantic then loads it as ''.
    // Treat that — and the Docker default '/music' when nothing was
    // bind-mounted — as "not configured" so we don't pretend a path
    // exists.
    const isPathSet     = !!(lib.music_path && lib.music_path !== '/music');
    const pathFull      = isPathSet ? fmtPathForDisplay(lib.music_path) : '';
    const path          = isPathSet ? fmtPathTruncatedFromStart(pathFull) : 'Not set';

    const scanCancelling   = scanRunning   && !!(lib.scan   && lib.scan.cancel_requested);
    const enrichCancelling = enrichRunning && !!(lib.enrich && lib.enrich.cancel_requested);

    const scanActions = scanRunning ? `
      <div class="btn-row single">
        <button class="btn btn-danger" data-cancel-scan ${scanCancelling ? 'disabled' : ''}>${scanCancelling ? 'Cancelling…' : 'Cancel scan'}</button>
      </div>
      <div class="action-progress" data-progress-for="scan">${escapeProfileHtml(scanCancelling ? 'Finishing the current step… cancel will take effect at the next checkpoint.' : (lib.scan.progress || 'Scanning…'))}</div>
    ` : `
      <div class="btn-row">
        <button class="btn btn-primary" data-action="scan">Scan for new</button>
        <button class="btn btn-secondary" data-action="scan-prune">Rescan</button>
      </div>
    `;

    const enrichActions = enrichRunning ? `
      <div class="btn-row single">
        <button class="btn btn-danger" data-cancel-enrich ${enrichCancelling ? 'disabled' : ''}>${enrichCancelling ? 'Cancelling…' : 'Cancel enrichment'}</button>
      </div>
      <div class="action-progress" data-progress-for="enrich">${escapeProfileHtml(enrichCancelling ? 'Finishing the current step… cancel will take effect at the next checkpoint.' : (lib.enrich.progress || 'Enriching…'))}</div>
    ` : `
      <div class="btn-row single">
        <button class="btn btn-secondary" data-action="enrich">Re-enrich missing</button>
      </div>
    `;

    const libraryStats = isEmpty ? '' : `
      <div class="profile-group-label">Library</div>
      <div class="form-group">
        <div class="stats-grid">
          ${_statCell('Tracks',  fmtNum(lib.total_tracks))}
          ${_statCell('Artists', fmtNum(lib.total_artists))}
          ${_statCell('Albums',  fmtNum(lib.total_albums))}
          ${_statCell('Genres',  fmtNum(lib.total_genres))}
        </div>
      </div>
      ${scanActions}

      <div class="profile-group-label">Enrichment</div>
      <div class="form-group">
        ${_enrichRow('embeddings', 'Embeddings', lib.embeddings_done, lib.total_tracks)}
        ${_enrichRow('features',   'Features',   lib.features_done,   lib.total_tracks)}
        ${_enrichRow('lastfm',     'Last.fm',    lib.lastfm_done,     lib.lastfm_total)}
        ${_enrichRow('lyrics',     'Lyrics',     lib.lyrics_done,     lib.total_tracks)}
      </div>
      ${enrichActions}
    `;

    const emptyState = !isEmpty ? '' : !isPathSet ? `
      <div class="empty-library">
        <div class="icon">${SETTINGS_ICONS.vinyl}</div>
        <p class="empty-library-msg">
          Music library path is not configured yet. Open the <b>Desktop Launcher</b>, set the folder that holds your music, and click <b>Scan library</b> there.
        </p>
      </div>` : `
      <div class="empty-library">
        <div class="icon">${SETTINGS_ICONS.vinyl}</div>
        <p class="empty-library-msg">
          No tracks indexed yet. Run the first scan to discover your library — it usually takes a few minutes per 10k tracks.
        </p>
        <button class="empty-cta" data-action="scan">${SETTINGS_ICONS.refresh}Run first scan</button>
      </div>`;

    // Action button rows now live inside libraryStats — under their
    // respective sections (scan under Library, enrich under
    // Enrichment) — so no end-of-screen action block is emitted.
    const actions = '';

    root.innerHTML = `
      <section class="screen screen-settings">
        ${_settingsHeader('Library')}

        <div class="form-group" style="margin-top:calc(14*var(--px));">
          <div class="form-row stacked">
            <div class="row-stack">
              <span class="row-stack-label">Music path</span>
              <span class="${isPathSet ? 'path-value' : 'form-value muted'}"${isPathSet ? ` title="${escapeProfileHtml(pathFull)}"` : ''}>${escapeProfileHtml(path)}</span>
            </div>
            <div class="row-stack-sub">Configured in the launcher.</div>
          </div>
          ${isEmpty ? '' : `
            <div class="form-row">
              <span class="form-label">Storage</span>
              <span class="form-value mono">${escapeProfileHtml(fmtBytes(lib.total_size_bytes))}</span>
            </div>
            <div class="form-row stacked">
              <div class="row-stack">
                <span class="row-stack-label">Last scan</span>
                <span class="row-stack-value">${scanRunning ? 'running…' : escapeProfileHtml(fmtRelative(lib.last_scan_at))}</span>
              </div>
              ${(!scanRunning && lib.last_scan_at)
                ? `<div class="row-stack-sub" style="font-family:var(--font-mono);letter-spacing:0.02em;">${escapeProfileHtml(fmtAbs(lib.last_scan_at))}</div>`
                : ''}
            </div>
          `}
        </div>

        ${libraryStats}
        ${emptyState}
        ${actions}
      </section>
    `;
    _wireBack(root);

    const onAction = (sel, fn) => root.querySelectorAll(sel).forEach(el => el.addEventListener('click', fn));
    onAction('[data-action="scan"]',       async () => { await fetch('/api/settings/library/scan',          { method: 'POST' }); render(); });
    onAction('[data-action="scan-prune"]', async () => { await fetch('/api/settings/library/scan?prune=true', { method: 'POST' }); render(); });
    onAction('[data-action="enrich"]',     async () => { await fetch('/api/settings/library/enrich',        { method: 'POST' }); render(); });
    onAction('[data-cancel-scan]',         async () => { await fetch('/api/settings/library/scan/cancel',   { method: 'POST' }); render(); });
    onAction('[data-cancel-enrich]',       async () => { await fetch('/api/settings/library/enrich/cancel', { method: 'POST' }); render(); });

    _subscribeLibraryStream(root);
  }

  /* Live Library updates over SSE. Replaces 1.5s polling per
     CLAUDE.md "Event-driven over polling" rule. Worker side wakes
     /api/settings/library/stream subscribers at meaningful
     checkpoints (start / progress callback / completion). Client
     re-fetches /api/settings/library on each wake and updates the
     same in-place targets that the old poll used. */
  let _libraryStreamCtrl = null;
  let _libraryStreamDebounce = null;
  function _subscribeLibraryStream(root) {
    if (_libraryStreamCtrl) { _libraryStreamCtrl.abort(); _libraryStreamCtrl = null; }
    if (_libraryStreamDebounce) { clearTimeout(_libraryStreamDebounce); _libraryStreamDebounce = null; }

    async function refresh() {
      if (!parseHash().startsWith('more/library')) {
        _libraryStreamCtrl && _libraryStreamCtrl.abort();
        _libraryStreamCtrl = null;
        return;
      }
      let lib;
      try {
        const r = await fetch('/api/settings/library');
        if (!r.ok) return;
        lib = await r.json();
      } catch (_) { return; }

      const scanProgress   = String((lib.scan   && lib.scan.progress)   || '');
      const enrichProgress = String((lib.enrich && lib.enrich.progress) || '');
      const terminalRe = /(complete|failed|cancelled)/i;
      const scanRunning   = !!(lib.scan   && lib.scan.running)   && !terminalRe.test(scanProgress);
      const enrichRunning = !!(lib.enrich && lib.enrich.running) && !terminalRe.test(enrichProgress);

      const scanLine = root.querySelector('[data-progress-for="scan"]');
      if (scanLine && scanProgress && scanLine.textContent !== scanProgress) {
        scanLine.textContent = scanProgress;
      }
      const enrichLine = root.querySelector('[data-progress-for="enrich"]');
      if (enrichLine && enrichProgress && enrichLine.textContent !== enrichProgress) {
        enrichLine.textContent = enrichProgress;
      }
      _refreshEnrichRow(root, 'embeddings', lib.embeddings_done, lib.total_tracks);
      _refreshEnrichRow(root, 'features',   lib.features_done,   lib.total_tracks);
      _refreshEnrichRow(root, 'lastfm',     lib.lastfm_done,     lib.lastfm_total);
      _refreshEnrichRow(root, 'lyrics',     lib.lyrics_done,     lib.total_tracks);

      // When workers finish (or hit terminal progress), full
      // re-render once to flip Cancel button back to normal row.
      const wasRunning = !!root.querySelector('[data-cancel-scan], [data-cancel-enrich]');
      if (wasRunning && !scanRunning && !enrichRunning) {
        if (parseHash().startsWith('more/library')) render();
      }
    }

    // Trailing-edge debounce on the SSE wake-event burst.
    // A single enrichment-run hits progress_cb a dozen times in
    // the first second (Phase 1 GPU init, Phase 2 text embeddings
    // start, lyrics embeddings start, enrichment embeddings start
    // …). Without coalescing the client would fire one
    // /api/settings/library fetch per wake; debouncing collapses
    // the burst into a single refresh while keeping the
    // "immediate" feel — the longest gap before a visible update
    // is ~200ms.
    const scheduleRefresh = () => {
      if (_libraryStreamDebounce) return;
      _libraryStreamDebounce = setTimeout(() => {
        _libraryStreamDebounce = null;
        refresh();
      }, 200);
    };

    _libraryStreamCtrl = window.sseStream('/api/settings/library/stream', () => {
      scheduleRefresh();
    }, (_err) => {
      // sseStream auto-reconnects with backoff; nothing to do here.
    });
  }

  /* ============ AI assistant screen — #more/ai ============ */
  // Wake-event SSE subscription replaces 2s polling
  // (CLAUDE.md "Event-driven over polling"). Server emits a wake
  // whenever the install worker thread mutates state; client re-fetches
  // /api/settings/ai/claude/state and re-renders in place.
  //
  // Two correctness guards:
  //  - idempotent: renderAI() re-runs on every wake, and re-invokes
  //    _subscribeClaudeStream(); without an early-return the abort/
  //    reopen pair would echo the server's initial ping into a
  //    self-sustaining refresh loop.
  //  - initial ping skipped: server's "data: {}" on connect is just an
  //    acknowledgement that the channel is live, not a state change —
  //    acting on it would re-render immediately after the renderAI()
  //    that opened the stream in the first place.
  let _claudeStreamCtrl = null;
  function _stopClaudeStream() {
    if (_claudeStreamCtrl) { _claudeStreamCtrl.abort(); _claudeStreamCtrl = null; }
  }
  function _subscribeClaudeStream() {
    if (_claudeStreamCtrl) return;
    if (typeof window.sseStream !== 'function') return;
    let primed = false;
    _claudeStreamCtrl = window.sseStream('/api/settings/ai/claude/stream', () => {
      if (!primed) { primed = true; return; }
      if (!parseHash().startsWith('more/ai')) {
        _stopClaudeStream();
        return;
      }
      renderAI(document.getElementById('app'));
    }, (_err) => {
      // sseStream auto-reconnects with backoff; nothing to do here.
    });
  }
  async function _fetchClaudeState() {
    try {
      const r = await fetch('/api/settings/ai/claude/state');
      if (r.ok) return await r.json();
    } catch (_) {}
    return null;
  }
  // Pull a human-readable message out of a FastAPI error response.
  // FastAPI's HTTPException renders as `{"detail": "..."}` — showing
  // the raw JSON in an alert is the worst-case fallback.
  async function _errorMessage(resp) {
    try {
      const txt = await resp.text();
      try {
        const obj = JSON.parse(txt);
        if (obj && typeof obj.detail === 'string') return obj.detail;
      } catch (_) {}
      return txt;
    } catch (_) {
      return `HTTP ${resp.status}`;
    }
  }
  async function renderAI(root) {
    let ai = null;
    try {
      const r = await fetch('/api/settings/ai');
      if (r.ok) ai = await r.json();
    } catch (_) {}
    if (!ai) {
      root.innerHTML = `<section class="screen screen-settings">${_settingsHeader('AI assistant')}<div class="placeholder">Не вдалося завантажити налаштування.</div></section>`;
      _wireBack(root);
      return;
    }

    let claudeState = null;
    if (ai.provider === 'claude_code') {
      claudeState = await _fetchClaudeState();
    }

    root.innerHTML = `
      <section class="screen screen-settings">
        ${_settingsHeader('AI assistant')}
        <div style="margin-top:calc(14*var(--px));">${_renderAi(ai, claudeState)}</div>
      </section>
    `;
    _wireBack(root);

    const onAction = (sel, fn) => root.querySelectorAll(sel).forEach(el => el.addEventListener('click', fn));
    onAction('[data-action="pick-provider"]', async () => {
      const id = await openSettingsPicker({ title: 'Provider', options: PROVIDER_OPTIONS, currentId: ai.provider });
      if (id && id !== ai.provider) {
        await fetch('/api/settings/ai/provider', { method: 'PUT', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ provider: id }) });
        refreshAiAvailability();
        render();
      }
    });
    onAction('[data-action="pick-model"]', async () => {
      const options = MODEL_OPTIONS[ai.provider] || [];
      const id = await openSettingsPicker({ title: 'Model', options, currentId: ai.model });
      if (id && id !== ai.model) {
        await fetch('/api/settings/ai/model', { method: 'PUT', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ model: id }) });
        render();
      }
    });
    const afterKeySaved = () => { refreshAiAvailability(); render(); };
    onAction('[data-action="sign-in"]',     () => openApiKeyModal(ai.provider || 'anthropic', afterKeySaved));
    onAction('[data-action="replace-key"]', () => openApiKeyModal(ai.provider || 'anthropic', afterKeySaved));
    onAction('[data-action="reauthorize"]', () => {
      // OAuth handshake lives in the chat module — route there for now.
      navigate('friends');
    });

    // Claude Code state actions
    onAction('[data-action="cc-refresh"]', () => render());
    const _ccError = async (title, r, err) => {
      const msg = err ? String(err) : await _errorMessage(r);
      await notifyDialog({ title, message: escapeProfileHtml(msg), kind: 'error' });
    };
    onAction('[data-action="cc-install"]', async () => {
      try {
        const r = await fetch('/api/settings/ai/claude/install', { method: 'POST' });
        if (!r.ok) { await _ccError('Claude Code install failed', r); return; }
      } catch (err) { await _ccError('Claude Code install failed', null, err); return; }
      // No client-side polling — server wakes us on install state
      // transitions over /api/settings/ai/claude/stream.
      render();
    });
    onAction('[data-action="cc-signin"]', async () => {
      try {
        const r = await fetch('/api/settings/ai/claude/signin', { method: 'POST' });
        if (!r.ok) { await _ccError('Claude Code sign-in failed', r); return; }
      } catch (err) { await _ccError('Claude Code sign-in failed', null, err); return; }
      render();
    });

    if (ai.provider === 'claude_code') _subscribeClaudeStream();
    refreshAiAvailability();
  }

  /* ============ Sync & P2P screen — #more/sync ============ */
  let _syncStreamCtrl = null;
  let _syncStreamDebounce = null;

  async function renderSync(root) {
    const sync = await _fetchSyncState();
    if (!sync) {
      root.innerHTML = `<section class="screen screen-settings">${_settingsHeader('Sync & P2P')}<div class="placeholder">Не вдалося завантажити налаштування.</div></section>`;
      _wireBack(root);
      return;
    }

    root.innerHTML = `
      <section class="screen screen-settings">
        ${_settingsHeader('Sync & P2P')}
        <div data-sync-content style="margin-top:calc(14*var(--px));">${_renderSync(sync)}</div>
      </section>
    `;
    _wireBack(root);
    _wireSyncActions(root, sync);
    _subscribeSyncStream(root);
  }

  async function _fetchSyncState() {
    try {
      const r = await fetch('/api/settings/sync');
      if (r.ok) return await r.json();
    } catch (_) {}
    return null;
  }

  function _wireSyncActions(root, sync) {
    const onAction = (sel, fn) => root.querySelectorAll(sel).forEach(el => el.addEventListener('click', fn));
    const putSync = async (body) => {
      await fetch('/api/settings/sync', { method: 'PUT', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) });
      render();
    };

    onAction('[data-action="toggle-p2p"]',       () => putSync({ p2p_enabled: !sync.p2p_enabled }));
    onAction('[data-action="toggle-bg-enrich"]', () => putSync({ background_enrichment: !sync.background_enrichment }));
    onAction('[data-action="pick-interval"]', async () => {
      const id = await openSettingsPicker({ title: 'Auto-sync interval', options: AUTO_SYNC_OPTIONS, currentId: sync.auto_interval_min || 0 });
      if (id != null) await putSync({ auto_interval_min: Number(id) });
    });
    onAction('[data-action="pick-limit"]', async () => {
      const id = await openSettingsPicker({ title: 'Announce limit', options: ANNOUNCE_LIMIT_OPTIONS, currentId: sync.announce_limit || 0 });
      if (id != null) await putSync({ announce_limit: Number(id) });
    });
    onAction('[data-action="pick-rotation"]', async () => {
      const id = await openSettingsPicker({ title: 'Announce rotation', options: ROTATION_OPTIONS, currentId: sync.announce_rotation_min || 30 });
      if (id != null) await putSync({ announce_rotation_min: Number(id) });
    });
    onAction('[data-action="force-sync"]', async () => {
      if (_syncInFlight) return;  // double-click guard
      _syncBaselineLastAt = sync.last_sync_at || null;
      _syncInFlight = true;
      // Targeted button update — keeps SSE subscription alive (re-
      // rendering the whole screen would re-subscribe and trigger
      // the server's initial ping, causing an SSE/fetch ping-pong).
      const btn = root.querySelector('[data-action="force-sync"]');
      if (btn) { btn.disabled = true; btn.textContent = 'Syncing…'; }
      try {
        await fetch('/api/settings/sync/force', { method: 'POST' });
      } catch (_) {
        _syncInFlight = false;
        if (btn) { btn.disabled = false; btn.textContent = 'Force sync now'; }
      }
      // Completion is reported via SSE wake (sautium_sync_done →
      // library/stream subscribers); _refreshSyncContents handles it.
    });
  }

  /* SSE-driven refresh — re-renders ONLY the [data-sync-content] block
     and re-wires its actions. Does NOT touch the SSE subscription, so
     the server's initial-ping → renderSync → re-subscribe loop is
     impossible. Mirrors Library's refresh() / sseStream pattern. */
  function _subscribeSyncStream(root) {
    if (_syncStreamCtrl) { _syncStreamCtrl.abort(); _syncStreamCtrl = null; }
    if (_syncStreamDebounce) { clearTimeout(_syncStreamDebounce); _syncStreamDebounce = null; }

    async function refresh() {
      if (!parseHash().startsWith('more/sync')) {
        _syncStreamCtrl && _syncStreamCtrl.abort();
        _syncStreamCtrl = null;
        return;
      }
      const sync = await _fetchSyncState();
      if (!sync) return;

      let justCompletedItems = null;
      if (_syncInFlight
          && sync.last_sync_at
          && sync.last_sync_at !== _syncBaselineLastAt) {
        _syncInFlight = false;
        justCompletedItems = sync.last_items_received;
      }

      const contentDiv = root.querySelector('[data-sync-content]');
      if (!contentDiv) return;
      contentDiv.innerHTML = _renderSync(sync);

      if (justCompletedItems != null) {
        const btnRow = root.querySelector('[data-action="force-sync"]')?.parentElement;
        if (btnRow) {
          const toast = document.createElement('div');
          toast.className = 'toast-inline';
          toast.innerHTML = `${SETTINGS_ICONS.check} Synced · <span class="mono">${fmtNum(justCompletedItems)}</span> new ${justCompletedItems === 1 ? 'item' : 'items'}`;
          btnRow.parentElement.insertBefore(toast, btnRow);
        }
      }
      _wireSyncActions(root, sync);
    }

    const scheduleRefresh = () => {
      if (_syncStreamDebounce) return;
      _syncStreamDebounce = setTimeout(() => {
        _syncStreamDebounce = null;
        refresh();
      }, 200);
    };

    _syncStreamCtrl = window.sseStream('/api/settings/library/stream', () => {
      scheduleRefresh();
    }, (_err) => {
      // sseStream auto-reconnects; nothing to do here.
    });
  }

  /* ---------- Wire it up ---------- */

  registerScreen('home', renderHome);
  registerScreen('discovery', renderDiscovery);
  registerScreen('friends', renderFriends);
  registerScreen('more', renderMore);

  function attachNavListeners() {
    document.querySelectorAll('.nav-tab').forEach(btn => {
      btn.addEventListener('click', () => {
        const route = btn.getAttribute('data-route');
        // More is a drawer overlay rather than a route — toggle it
        // in place. Other tabs dismiss the drawer (if open) and
        // navigate normally.
        if (route === 'more') {
          moreDrawer.toggle();
          return;
        }
        if (moreDrawer.isOpen) moreDrawer.close();
        if (route) navigate(route);
      });
    });
  }

  function init() {
    attachNavListeners();
    mp.init();
    sheet.init();
    ai.init();
    queue.init();
    moreDrawer.init();
    refreshAiAvailability();
    document.addEventListener('np-update', e => {
      const d = e.detail || {};
      _lastNpMediaFileId = d.media_file_id != null ? d.media_file_id : null;
      mp.update(d);
      sheet.onStatus(d);
      updatePlayingHighlight();
    });
    document.addEventListener('np-detail', e => {
      mp.setCover(e.detail);
    });
    // playlist-loaded races np-update on the first SSE tick of a new
    // playlist (the version bump arrives within the same payload that
    // also triggers fetchPlaylist). Re-run the highlight pass against
    // detail screens once the playlist DOM is reachable; tryFetchDetail
    // no longer needs a re-poke because media_file_id is in np-update.
    document.addEventListener('playlist-loaded', updatePlayingHighlight);
    window.addEventListener('hashchange', render);

    if (!location.hash) {
      // Setting hash to #home won't fire hashchange when current is empty,
      // so call render() explicitly after.
      history.replaceState(null, '', '#home');
    }
    render();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
