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
      this.el.addEventListener('click', () => {
        // Future: expand to Now Playing sheet. For now, no-op.
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
    document.addEventListener('np-update', e => mp.update(e.detail));
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
