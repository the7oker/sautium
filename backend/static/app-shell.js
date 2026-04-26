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

  function render() {
    const hash = parseHash();
    const route = hash.split('/')[0] || 'home';
    currentRoute = route;
    const app = document.getElementById('app');
    if (!app) return;
    const renderer = routes[route] || routes.home;
    app.innerHTML = '';
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
    // Phase 1.1: visible on every root surface. AI sheet / overlays
    // not implemented yet — when they are, hide the FAB while open.
    fab.hidden = false;
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

      // Cover
      if (d.cover_id && this.coverImg) {
        this.coverImg.src = '/api/covers/' + d.cover_id;
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
        const cover = t.cover_id
          ? `<img src="/api/covers/${t.cover_id}" alt="">`
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

      const c = coverPlaceholderColors(data.song || data.album || '');
      this.cover.style.backgroundImage =
        `linear-gradient(135deg, ${c.bg1}, ${c.bg2})`;
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

  function renderArtistTile(name) {
    const tile = document.createElement('button');
    tile.type = 'button';
    tile.className = 'artist-tile';
    const ph = avatarPlaceholder(name);
    tile.innerHTML = `
      <div class="artist-avatar" style="background: ${ph.bg};">${escapeHtml(ph.initials)}</div>
      <div class="artist-name">${escapeHtml(name)}</div>
    `;
    return tile;
  }

  function renderAlbumTile({ title, artist, similarity }) {
    const tile = document.createElement('button');
    tile.type = 'button';
    tile.className = 'album-tile';
    const c = coverPlaceholderColors(title || artist || 'x');
    const sim = (similarity != null)
      ? `<div class="album-similarity">${similarity.toFixed(2)}</div>`
      : '';
    tile.innerHTML = `
      <div class="album-cover" style="--cover-bg-1: ${c.bg1}; --cover-bg-2: ${c.bg2};">
        <div class="placeholder-badge">${escapeHtml(title || '')}</div>
      </div>
      <div class="album-title">${escapeHtml(title || '')}</div>
      <div class="album-artist">${escapeHtml(artist || '')}</div>
      ${sim}
    `;
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
        if (kind === 'artist') row.appendChild(renderArtistTile(item.name));
        else row.appendChild(renderAlbumTile({
          title: item.title,
          artist: item.artist,
          similarity: item.similarity,
        }));
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

  /* ---------- Wire it up ---------- */

  registerScreen('home', renderHome);
  registerScreen('discovery', placeholderScreen('Discovery'));
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
    document.addEventListener('np-update', e => {
      mp.update(e.detail);
      sheet.onStatus(e.detail);
    });
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
