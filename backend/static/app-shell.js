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

  function updatePlayingHighlight() {
    const mfId = (typeof window._getCurrentMediaFileId === 'function')
      ? window._getCurrentMediaFileId()
      : null;
    const target = mfId != null ? String(mfId) : null;
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

  /* ---------- Lightweight markdown ----------
     Just enough to render the prose tone the AI emits without a
     library: **bold**, *italic* and paragraph/line breaks. Anything
     else (links, lists, code) renders as plain text — we add it only
     if a real chat message starts using it.
     escapeHtml runs first so injected angle-brackets stay inert. */

  function mdToHtml(text) {
    const escaped = escapeHtml(text);
    let html = escaped
      .replace(/\*\*([^*\n]+?)\*\*/g, '<strong>$1</strong>')
      .replace(/(^|[^*])\*(?!\s)([^*\n]+?)\*(?!\*)/g,
               (_, pre, body) => `${pre}<em>${body}</em>`);
    // Paragraph splits on blank lines; soft <br> for single newlines.
    const paragraphs = html.split(/\n{2,}/).map(p =>
      `<p>${p.replace(/\n/g, '<br>')}</p>`);
    return paragraphs.join('');
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
      render();
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
    app.innerHTML = '';

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

  function updateFabVisibility(/* route */) {
    const fab = document.getElementById('aiFab');
    if (!fab) return;
    // Hide while overlay sheets are open (Now Playing or AI assistant)
    // — they have their own dismiss controls and the FAB on top
    // would obscure them.
    const npOpen = sheet && sheet.isOpen;
    const aiOpen = ai && ai.isOpen;
    fab.hidden = !!(npOpen || aiOpen);
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
    timeCurrent: null, timeTotal: null, repeatBtn: null,
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
      this.repeatBtn = document.getElementById('npRepeatBtn');
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

      // Retry detail fetch on every status update until we successfully
      // pull it. _getCurrentMediaFileId depends on currentPlaylist which
      // app.js loads asynchronously, so the first attempt typically
      // fails right after page load.
      if (this.lastDetailFetchedKey !== trackKey) {
        this.tryFetchDetail(trackKey);
      }
    },

    async tryFetchDetail(trackKey) {
      if (this.inflightKey === trackKey) return; // already fetching
      const mfId = (typeof window._getCurrentMediaFileId === 'function')
        ? window._getCurrentMediaFileId()
        : null;
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

      // Reset cover to gradient placeholder only when the track changes.
      // The np-detail listener will swap to the real cover image once
      // the sheet's tryFetchDetail completes. CSS already declares
      // background-size:cover + background-position:center on .mp-cover,
      // so the gradient and the image both render correctly without
      // per-call inline overrides.
      const songKey = (data.song || '') + '|' + (data.album || '');
      if (songKey !== this.lastSongKey) {
        this.lastSongKey = songKey;
        const c = coverPlaceholderColors(data.song || data.album || '');
        this.cover.style.backgroundImage =
          `linear-gradient(135deg, ${c.bg1}, ${c.bg2})`;
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

  /* ---------- AI assistant sheet ----------
     Wires the FAB to the /api/chat backend (sessions, messages, track
     picks). Provider/model selection lives in Settings — here we use
     whatever the backend's `default_provider` returns. */

  const ai = {
    el: null, thread: null, input: null, form: null,
    closeBtn: null, sendBtn: null,
    pills: null, newPill: null,
    sessions: [],            // cached metadata for pill rendering
    isOpen: false,
    activeSessionId: null,
    sending: false,
    pressTimer: null,

    init() {
      this.el = document.getElementById('aiSheet');
      if (!this.el) return;
      this.thread = document.getElementById('aiThread');
      this.input = document.getElementById('aiInput');
      this.form = document.getElementById('aiInputForm');
      this.closeBtn = document.getElementById('aiCloseBtn');
      this.sendBtn = document.getElementById('aiSendBtn');
      this.pills = document.getElementById('aiPills');
      this.newPill = document.getElementById('aiNewPill');

      this.closeBtn.addEventListener('click', () => this.hide());
      this.newPill.addEventListener('click', () => this.newSession());
      this.form.addEventListener('submit', e => {
        e.preventDefault();
        this.send();
      });

      // FAB → open
      const fab = document.getElementById('aiFab');
      if (fab) fab.addEventListener('click', () => this.show());

      document.addEventListener('keydown', e => {
        if (e.key === 'Escape' && this.isOpen) this.hide();
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
        await this.refreshPills();
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
        await this.refreshPills();
        if (this.sessions.length > 0) {
          const top = this.sessions[0];
          await this.switchToSession(top.id);
        } else {
          await this.newSession();
        }
      } catch (err) {
        console.warn('AI bootstrap failed:', err);
        this.renderEmpty('Could not load chats. Try refreshing.');
      }
    },

    async refreshPills() {
      try {
        const resp = await fetch('/api/chat/sessions?limit=50');
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        this.sessions = await resp.json() || [];
      } catch (err) {
        console.warn('sessions load failed:', err);
        this.sessions = [];
      }
      this.renderPills();
    },

    renderPills() {
      // Keep "+ New" pill (always first), append session pills after.
      // Existing session pills are removed and rebuilt — cheap for
      // up to ~50 entries.
      const existing = this.pills.querySelectorAll('.ai-pill:not(.ai-pill-new)');
      existing.forEach(el => el.remove());
      for (const s of this.sessions) {
        const pill = document.createElement('button');
        pill.type = 'button';
        pill.className = 'ai-pill';
        if (s.id === this.activeSessionId) pill.classList.add('is-active');
        const label = (s.title || 'New chat').trim() || 'New chat';
        pill.innerHTML = '<span></span>';
        pill.querySelector('span').textContent = label;
        pill.addEventListener('click', () => this.switchToSession(s.id));
        this.attachLongPress(pill, s);
        this.pills.appendChild(pill);
      }
    },

    // Long-press (700ms) prompts to delete the session. Works on
    // mouse + touch. Cleared on pointerup / leave so a quick tap
    // falls through to the normal click handler (switch session).
    attachLongPress(pill, session) {
      const start = () => {
        clearTimeout(this.pressTimer);
        this.pressTimer = setTimeout(() => {
          this.pressTimer = null;
          if (confirm(`Delete chat "${session.title || 'New chat'}"?`)) {
            this.deleteSession(session.id);
          }
        }, 700);
      };
      const cancel = () => {
        if (this.pressTimer) {
          clearTimeout(this.pressTimer);
          this.pressTimer = null;
        }
      };
      pill.addEventListener('pointerdown', start);
      pill.addEventListener('pointerup', cancel);
      pill.addEventListener('pointerleave', cancel);
      pill.addEventListener('pointercancel', cancel);
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
        this.thread.innerHTML = '';
        this.renderEmpty('Ask the AI for recommendations, analysis or context.');
        await this.refreshPills();
        setTimeout(() => this.input && this.input.focus(), 50);
      } catch (err) {
        console.warn('new session failed:', err);
      }
    },

    async switchToSession(id) {
      this.activeSessionId = id;
      this.thread.innerHTML = '<p class="ai-empty">Loading…</p>';
      this.renderPills();  // update active highlight immediately
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
          tag.textContent = String(m.model).split(':').pop() || m.model;
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

      // Optimistic user bubble + typing indicator.
      this.appendMessage({ role: 'user', content: text });
      const typing = this.typingIndicator();
      this.thread.appendChild(typing);
      this.scrollToBottom();

      try {
        const resp = await fetch(
          '/api/chat/sessions/' + this.activeSessionId + '/messages',
          {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ message: text }),
          });
        const data = await resp.json();
        if (typing.parentNode) typing.remove();
        if (!resp.ok) throw new Error(data.detail || 'send failed');
        const am = data.assistant_msg || {};
        // Backend returns hydrated blocks at the response root and
        // assistant_msg.blocks_data mirrors them — prefer the root
        // version because it is the canonical, richer payload.
        am.blocks_data = data.blocks || am.blocks_data || [];
        am.tracks_data = data.tracks || am.tracks_data || [];
        am.model = data.provider
          ? `${data.provider}:${data.model}`
          : (am.model || data.model || '');
        this.appendMessage(am);
        // Backend may have just auto-set a title from the first
        // message — refresh the pill row so the active pill picks
        // it up. Also bumps it to the front by `updated_at`.
        await this.refreshPills();
        this.scrollToBottom();
      } catch (err) {
        if (typing.parentNode) typing.remove();
        console.warn('send failed:', err);
        const errRow = document.createElement('div');
        errRow.className = 'ai-msg-row';
        errRow.innerHTML =
          '<div class="ai-msg-ai" style="color:var(--color-text-muted);">' +
          escapeHtml(String(err.message || err)) + '</div>';
        this.thread.appendChild(errRow);
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
      // Drop locally first so the pill disappears even if the next
      // refresh hiccups; then re-bootstrap if we killed the active
      // session, else just refresh the pill row.
      this.sessions = this.sessions.filter(s => s.id !== id);
      if (id === this.activeSessionId) {
        this.activeSessionId = null;
        this.thread.innerHTML = '';
        await this.bootstrap();
      } else {
        this.renderPills();
      }
    },
  };

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
    tile.innerHTML = `
      <div class="artist-avatar" style="background: ${ph.bg};">${escapeHtml(ph.initials)}</div>
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

  function renderHomeSection(parent, title, items, kind) {
    const sec = document.createElement('section');
    sec.className = 'home-section';
    sec.innerHTML = `
      <div class="home-section-head">
        <h2 class="home-section-title">${escapeHtml(title)}</h2>
        <button class="see-all" type="button">See all</button>
      </div>
    `;
    const row = document.createElement('div');
    row.className = 'home-row';

    if (!items || items.length === 0) {
      const empty = document.createElement('div');
      empty.className = 'placeholder-body';
      empty.style.padding = 'var(--space-2)';
      empty.textContent = '—';
      row.appendChild(empty);
    } else {
      for (const item of items) {
        if (kind === 'artist') row.appendChild(renderArtistTile(item));
        else row.appendChild(renderAlbumTile(item));
      }
    }
    sec.appendChild(row);
    parent.appendChild(sec);
  }

  async function renderHome(root) {
    const screen = document.createElement('div');
    screen.className = 'screen';
    screen.innerHTML = `
      <header class="screen-head">
        <h1 class="screen-title">Sautium<span class="dot">.</span></h1>
      </header>
    `;
    root.appendChild(screen);

    let feed;
    try {
      const resp = await fetch('/api/home/feed?limit=8');
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      feed = await resp.json();
    } catch (err) {
      console.error('Home feed failed:', err);
      const errBox = document.createElement('div');
      errBox.className = 'placeholder-body';
      errBox.style.padding = 'var(--space-4)';
      errBox.textContent = 'Could not load Home feed. Try refreshing.';
      screen.appendChild(errBox);
      return;
    }

    renderHomeSection(screen, 'Favourite artists',
      feed.favourite_artists, 'artist');
    renderHomeSection(screen, 'New in library',
      feed.new_in_library, 'album');
    renderHomeSection(screen, 'Recommendations',
      feed.recommendations, 'album');
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

  async function renderArtist(root, artistId) {
    root.innerHTML = '';
    const screen = document.createElement('div');
    screen.className = 'detail-screen';
    root.appendChild(screen);

    let d;
    try {
      const resp = await fetch('/api/artists/' + encodeURIComponent(artistId));
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
    const heroImg = d.photo_url
      ? `<img src="${escapeHtml(d.photo_url)}" alt="">`
      : `<div class="artist-hero-fallback"
            style="--cover-bg-1: ${ph.bg}; --cover-bg-2: var(--color-foundation);">${
              escapeHtml(ph.initials)}</div>`;

    const tagsHtml = (d.tags || [])
      .map(t => `<span class="tag-chip">${escapeHtml(t.name)}</span>`)
      .join('');

    const albumsHtml = (d.albums || []).map(a => {
      const c = coverPlaceholderColors(a.title || a.id);
      const url = coverUrl(a);
      const inner = url
        ? `<img src="${url}" alt="" loading="lazy"
                onerror="this.style.display='none'">`
        : `<div class="placeholder-badge"
              style="--cover-bg-1: ${c.bg1}; --cover-bg-2: ${c.bg2};">${
                escapeHtml(a.title || '')}</div>`;
      return `
        <button class="album-tile" type="button" data-album-id="${escapeHtml(a.id)}">
          <div class="album-cover"
               style="--cover-bg-1: ${c.bg1}; --cover-bg-2: ${c.bg2};">${inner}</div>
          <div class="album-tile-title">${escapeHtml(a.title || '')}</div>
          <div class="album-tile-year">${a.year || ''}</div>
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
      const url = coverUrl(s);
      const inner = url
        ? `<img src="${url}" alt="" loading="lazy"
                onerror="this.style.display='none'">`
        : `<div class="similar-avatar-fallback"
              style="--cover-bg-1: ${sph.bg}; --cover-bg-2: var(--color-foundation);">${
                escapeHtml(sph.initials)}</div>`;
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
          <button class="icon-btn" type="button" aria-label="More">${SVG_KEBAB}</button>
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
          <button class="see-all" type="button">See all ›</button>
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
          <button class="see-all" type="button">See all ›</button>
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

    wireDetailHandlers(screen);
    updatePlayingHighlight();
  }

  async function renderAlbum(root, albumId) {
    root.innerHTML = '';
    const screen = document.createElement('div');
    screen.className = 'detail-screen';
    root.appendChild(screen);

    let d;
    try {
      const resp = await fetch('/api/albums/' + encodeURIComponent(albumId));
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

    const tracksHtml = (d.tracks || []).map(t => {
      const sub = [
        t.key ? (t.key + (modeShort(t.mode) ? ' ' + modeShort(t.mode) : '')) : null,
        t.bpm ? Math.round(t.bpm) + ' bpm' : null,
      ].filter(Boolean).join(' · ');
      return `
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
      `;
    }).join('');

    const artistName = d.primary_artist ? d.primary_artist.name : '';
    const artistId = d.primary_artist ? d.primary_artist.id : '';
    const totalDuration = fmtDurationLong(d.total_duration);

    screen.innerHTML = `
      <div class="album-hero">
        ${heroImg}
        <div class="album-hero-scrim"></div>
        <div class="album-hero-controls">
          <button class="icon-btn" type="button" data-action="back" aria-label="Back">${SVG_BACK}</button>
          <button class="icon-btn" type="button" aria-label="More">${SVG_KEBAB}</button>
        </div>
      </div>
      <div class="album-meta-block">
        <h1 class="album-title-line">${escapeHtml(d.title || '')}</h1>
        ${artistName ? `<button class="album-artist-line"
                                style="background:none;border:0;padding:0;cursor:pointer;text-align:left;"
                                data-artist-id="${escapeHtml(artistId)}">${escapeHtml(artistName)}</button>` : ''}
        <div class="album-meta-row">
          ${d.year ? `<span class="am-year">${d.year}</span><span class="am-dot"></span>` : ''}
          <span class="am-dur" style="margin-left: 0;">${totalDuration}</span>
          <span class="am-hires ${qualClass}" style="margin-left: auto;">${qualLabel}</span>
        </div>
        ${genresHtml ? `<div class="tag-row" style="padding: calc(12 * var(--px)) 0 0;">${genresHtml}</div>` : ''}
      </div>
      <div class="album-actions">
        <button class="btn-primary" type="button" data-action="play-all">${SVG_PLAY} Play all</button>
        <button class="btn-secondary" type="button" data-action="queue-album">${SVG_PLUS} Queue</button>
      </div>
      <div class="album-tracklist">${tracksHtml}</div>
      <div style="height: calc(24 * var(--px));"></div>
    `;

    wireDetailHandlers(screen, { albumId, tracks: d.tracks });
    updatePlayingHighlight();
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
    // Track-add button → append exactly this track to the queue.
    // /queue-next is Radio Mode (similar tracks); /queue-tracks is
    // literal append. No client-side fetchPlaylist refresh — backend
    // bumps playlist_version, the SSE handler in app.js awaits a
    // fresh fetchPlaylist() before notifying subscribers.
    screen.querySelectorAll('.track-add').forEach(btn => {
      btn.addEventListener('click', async (e) => {
        e.stopPropagation();
        const row = btn.closest('[data-media-file-id]');
        if (!row) return;
        const mfId = row.getAttribute('data-media-file-id');
        if (!mfId) return;
        try {
          await fetch('/api/player/queue-tracks', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ track_ids: [parseInt(mfId, 10)] }),
          });
        } catch (err) { console.warn('queue-tracks failed', err); }
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
    // Queue album — append all tracks in one request.
    screen.querySelectorAll('[data-action="queue-album"]').forEach(btn => {
      btn.addEventListener('click', async () => {
        if (!ctx.tracks || !ctx.tracks.length) return;
        const ids = ctx.tracks.map(t => t.media_file_id).filter(Boolean);
        if (!ids.length) return;
        try {
          await fetch('/api/player/queue-tracks', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ track_ids: ids }),
          });
        } catch (err) { console.warn('queue-tracks failed', err); }
        // No client-side refetch — see comment on .track-add above.
      });
    });
  }

  /* ---------- Wire it up ---------- */

  registerScreen('home', renderHome);
  registerScreen('discovery', renderDiscovery);
  registerScreen('friends', placeholderScreen('Friends'));
  registerScreen('more', placeholderScreen('More'));

  function attachNavListeners() {
    document.querySelectorAll('.nav-tab').forEach(btn => {
      btn.addEventListener('click', () => {
        const route = btn.getAttribute('data-route');
        if (route) navigate(route);
      });
    });
  }

  function init() {
    attachNavListeners();
    mp.init();
    sheet.init();
    ai.init();
    document.addEventListener('np-update', e => {
      mp.update(e.detail);
      sheet.onStatus(e.detail);
      updatePlayingHighlight();
    });
    document.addEventListener('np-detail', e => {
      mp.setCover(e.detail);
    });
    // currentPlaylist resolves async; once it lands, the previous
    // np-update events that bailed (no mfId yet) need a re-run.
    document.addEventListener('playlist-loaded', updatePlayingHighlight);
    // When the legacy playlist load resolves, kick a detail fetch in
    // case SSE already fired before playlist was ready (in which case
    // the previous tryFetchDetail bailed because mfId was null).
    document.addEventListener('playlist-loaded', () => {
      if (sheet.lastTrackKey && sheet.lastDetailFetchedKey !== sheet.lastTrackKey) {
        sheet.tryFetchDetail(sheet.lastTrackKey);
      }
    });
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
