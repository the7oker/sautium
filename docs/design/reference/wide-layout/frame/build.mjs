#!/usr/bin/env node
/* Frame-direction artboards for the tablet/desktop layout (Ф1 of the
 * wide-layout cycle). Generates the .dc.html artboards + canvas.json in
 * this directory from shared fragments, with every size and colour lifted
 * from backend/static/tokens.css and style.css (1 design px == 1 px above
 * the 360 lock). Also writes editor-free previews for headless screenshots
 * when --preview <dir> is given.
 *
 *   node build.mjs                 # artboards + canvas.json here
 *   node build.mjs --preview /tmp/x  # plus plain-HTML previews
 */

import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const previewDir = process.argv.includes('--preview')
  ? process.argv[process.argv.indexOf('--preview') + 1] : null;

const BAR_H = 72;

/* ---------- tokens + component classes (from tokens.css / style.css) ---------- */
const STYLE = `
  :root {
    --foundation: #1B1714; --surface: #2A2420; --surface-hi: #342D28; --surface-lo: #221D1A;
    --divider: #3A322C; --text: #EDE2D4; --muted: #A69B8E; --dim: #6E665C;
    --amber: #E8B06F; --amber-hover: #EFBE83; --amber-weak: rgba(232, 176, 111, 0.14);
    --blue: #4A7FA7; --hires: #D4AF5A; --scrim: rgba(14, 10, 8, 0.65);
    --shadow-1: 0 1px 0 0 rgba(14,10,8,.40), 0 2px 6px -1px rgba(14,10,8,.45);
    --shadow-2: 0 2px 1px 0 rgba(14,10,8,.35), 0 8px 20px -4px rgba(14,10,8,.55), 0 1px 0 0 rgba(237,226,212,.03) inset;
    --shadow-3: 0 4px 2px 0 rgba(14,10,8,.30), 0 20px 48px -12px rgba(14,10,8,.70), 0 1px 0 0 rgba(237,226,212,.04) inset;
  }
  body { margin: 0; background: var(--foundation); color: var(--text);
         font-family: "Inter Tight", ui-sans-serif, system-ui, sans-serif; font-size: 15px; line-height: 1.5;
         -webkit-font-smoothing: antialiased; font-feature-settings: "ss01", "cv11"; }
  a { color: var(--amber); } a:hover { color: var(--amber-hover); }
  * { box-sizing: border-box; }
  .mono { font-family: "JetBrains Mono", ui-monospace, Menlo, Consolas, monospace; }
  svg { display: block; }

  .frame { position: relative; overflow: hidden; background: var(--foundation); }

  .nav { position: absolute; top: 0; left: 0; display: flex; flex-direction: column; gap: 2px;
         padding: 24px 12px; border-right: 1px solid var(--divider); }
  .brand { font-size: 32px; font-weight: 700; letter-spacing: -0.02em; line-height: 1.15; padding: 0 12px 20px; }
  .brand .dot { color: var(--amber); }
  .nav-row { display: flex; align-items: center; gap: 14px; height: 48px; padding: 0 12px; border-radius: 8px;
             color: var(--muted); font-weight: 500; font-size: 15px; }
  .nav-row.active { color: var(--amber); background: var(--amber-weak); }
  .nav-row svg { width: 22px; height: 22px; flex-shrink: 0; }
  .nav-row.small { height: 40px; font-size: 13px; }
  .nav-sect { font-size: 11px; letter-spacing: 0.04em; text-transform: uppercase; color: var(--dim); padding: 20px 12px 6px; }

  .rail { position: absolute; top: 0; left: 0; width: 80px; display: flex; flex-direction: column;
          padding: 16px 0; border-right: 1px solid var(--divider); }
  .rail .mark { font-size: 24px; font-weight: 700; letter-spacing: -0.02em; text-align: center; padding-bottom: 12px; }
  .rail-tab { display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 2px;
              height: 56px; color: var(--muted); font-size: 11px; font-weight: 500; letter-spacing: 0.02em; }
  .rail-tab.active { color: var(--amber); }
  .rail-tab svg { width: 22px; height: 22px; }
  .rail .spacer { flex: 1; }

  .content { position: absolute; top: 0; overflow: hidden; padding: 24px 0 0 24px;
             display: flex; flex-direction: column; gap: 32px; }
  .sec { display: flex; flex-direction: column; gap: 12px; }
  .sec h2 { margin: 0; font-size: 20px; font-weight: 600; letter-spacing: -0.01em; }
  .row { display: flex; gap: 12px; overflow: hidden; }
  .artist { display: flex; flex-direction: column; align-items: center; gap: 8px; width: 96px; flex-shrink: 0; }
  .avatar { width: 80px; height: 80px; border-radius: 50%; background: var(--surface-hi); box-shadow: var(--shadow-1); }
  .artist .name { font-size: 13px; font-weight: 500; text-align: center; line-height: 1.25; }
  .album { display: flex; flex-direction: column; gap: 8px; width: 140px; flex-shrink: 0; }
  .cover { width: 100%; aspect-ratio: 1 / 1; border-radius: 4px; background: var(--surface-hi); box-shadow: var(--shadow-2); }
  .album .t { font-size: 15px; font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .album .s { font-size: 13px; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

  .bar { position: absolute; bottom: 0; right: 0; height: ${BAR_H}px; background: var(--surface);
         border-top: 1px solid var(--divider); display: grid; align-items: center; gap: 16px; padding: 0 16px; }
  .bar.mini { grid-template-columns: auto 1fr auto auto; }
  .bar.full { grid-template-columns: 1fr auto 1fr; }
  .bar .who { display: flex; align-items: center; gap: 12px; min-width: 0; }
  .bar .cov { width: 44px; height: 44px; border-radius: 4px; background: var(--surface-hi); flex-shrink: 0; }
  .bar .ti { font-size: 13px; font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .bar .ar { font-size: 11px; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .btn { width: 44px; height: 44px; border-radius: 50%; display: grid; place-items: center; color: var(--text); }
  .btn svg { width: 24px; height: 24px; }
  .play40 { width: 40px; height: 40px; border-radius: 50%; background: var(--amber); color: var(--foundation); display: grid; place-items: center; }
  .play40 svg { width: 22px; height: 22px; }
  .bar .centre { display: flex; flex-direction: column; align-items: center; gap: 6px; width: 520px; }
  .bar .keys { display: flex; align-items: center; gap: 12px; }
  .bar .line { display: flex; align-items: center; gap: 10px; width: 100%; }
  .bar .right { display: flex; justify-content: flex-end; gap: 4px; }
  .prog { height: 3px; background: var(--surface-hi); border-radius: 2px; position: relative; }
  .bar .line .prog { flex: 1; }
  .prog i { position: absolute; left: 0; top: 0; bottom: 0; width: 62%; background: var(--blue); border-radius: 2px; }
  .prog b { position: absolute; top: 50%; left: 62%; width: 14px; height: 14px; margin: -7px 0 0 -7px; border-radius: 50%;
            background: var(--blue); box-shadow: 0 0 0 4px rgba(74,127,167,.25); }
  .tm { font-family: "JetBrains Mono", ui-monospace, monospace; font-size: 11px; color: var(--blue); }
  .tm.total { color: var(--muted); }

  .panel { position: absolute; top: 0; right: 0; border-left: 1px solid var(--divider); background: var(--foundation); overflow: hidden; }
  .head { display: flex; justify-content: space-between; align-items: center; height: 48px; padding: 0 12px 0 16px;
          font-size: 11px; letter-spacing: 0.04em; text-transform: uppercase; color: var(--muted); }
  .head svg { width: 24px; height: 24px; color: var(--text); }
  .npcover { width: 100%; aspect-ratio: 1 / 1; background: #3A2F27; }
  .npbody { padding: 16px 20px 0; }
  .npt { font-size: 19px; font-weight: 600; letter-spacing: -0.015em; line-height: 1.25; margin: 0 0 4px; }
  .npa { font-size: 15px; font-weight: 500; margin: 0 0 2px; }
  .npal { font-size: 13px; color: var(--muted); margin: 0; }
  .meta { display: flex; gap: 8px; align-items: center; margin-top: 10px; padding-top: 10px; border-top: 1px solid var(--divider); }
  .pill { font-family: "JetBrains Mono", ui-monospace, monospace; font-size: 11px; letter-spacing: 0.06em; padding: 4px 8px;
          border: 1px solid var(--blue); border-radius: 4px; color: var(--blue); }
  .pill.hires { border-color: var(--hires); color: var(--hires); }
  .bpm { font-family: "JetBrains Mono", ui-monospace, monospace; font-size: 13px; color: var(--blue); }
  .bpm small { font-family: "Inter Tight", sans-serif; font-size: 11px; letter-spacing: 0.04em; color: var(--muted); margin-left: 4px; }
  .energy { margin-left: auto; display: flex; align-items: center; gap: 8px; font-family: "JetBrains Mono", ui-monospace, monospace;
            font-size: 11px; letter-spacing: 0.06em; color: var(--muted); }
  .dots { display: flex; gap: 4px; }
  .dots i { width: 6px; height: 6px; border-radius: 50%; background: var(--surface-hi); }
  .dots i.on { background: var(--blue); }
  .npprog { margin-top: 16px; display: flex; flex-direction: column; gap: 6px; }
  .npprog .times { display: flex; justify-content: space-between; }
  .transport { display: flex; align-items: center; justify-content: space-between; padding: 0 4px; margin-top: 18px; }
  .t56 { width: 56px; height: 56px; border-radius: 50%; display: grid; place-items: center; color: var(--text); }
  .t56 svg { width: 28px; height: 28px; }
  .t68 { width: 68px; height: 68px; border-radius: 50%; background: var(--amber); color: var(--foundation); display: grid; place-items: center; }
  .t68 svg { width: 32px; height: 32px; }
  .simhead { display: flex; justify-content: space-between; align-items: baseline; margin: 24px 0 12px; padding-bottom: 8px;
             border-bottom: 1px solid var(--divider); font-size: 15px; font-weight: 600; letter-spacing: 0.06em; }
  .simhead span { font-family: "JetBrains Mono", ui-monospace, monospace; font-size: 11px; font-weight: 400; color: var(--muted); letter-spacing: 0.06em; }
  .sim { display: grid; grid-template-columns: 44px 1fr auto 44px; gap: 12px; align-items: center; padding: 8px 0; }
  .sim .c { width: 44px; height: 44px; border-radius: 4px; background: var(--surface-hi); }
  .sim .t { font-size: 15px; font-weight: 500; }
  .sim .s { font-size: 13px; color: var(--muted); }
  .sim .score { font-family: "JetBrains Mono", ui-monospace, monospace; font-size: 13px; color: var(--blue); }
  .sim .add { width: 44px; height: 44px; display: grid; place-items: center; color: var(--text); }

  .scrim { position: absolute; inset: 0; background: var(--scrim); }
  .card { position: absolute; left: 50%; top: 50%; transform: translate(-50%, -50%); width: 560px; background: var(--foundation);
          border-radius: 14px; box-shadow: var(--shadow-3); overflow: hidden; }
  .card .top { display: flex; gap: 20px; padding: 20px; }
  .card .npcover { width: 200px; flex-shrink: 0; border-radius: 8px; }
  .card .npbody { padding: 0; min-width: 0; }
  .card .below { padding: 0 20px 20px; }
  .card .transport { margin-top: 12px; }

  .fab { position: absolute; right: 16px; width: 56px; height: 56px; border-radius: 50%; background: var(--amber); color: var(--foundation);
         font-weight: 700; font-size: 13px; display: grid; place-items: center; box-shadow: var(--shadow-2); }
`;

/* ---------- icons (stroke, 24 grid; nav ones copied from index.html) ---------- */
const I = {
  home: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 11l9-8 9 8v10a1 1 0 0 1-1 1h-5v-7H10v7H4a1 1 0 0 1-1-1V11z" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/></svg>',
  discovery: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="6" fill="none" stroke="currentColor" stroke-width="2"/><line x1="16" y1="16" x2="21" y2="21" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
  friends: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="9" cy="8" r="3.5" fill="none" stroke="currentColor" stroke-width="2"/><path d="M3 20c0-3 2.5-5 6-5s6 2 6 5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><circle cx="17" cy="9" r="2.5" fill="none" stroke="currentColor" stroke-width="2"/><path d="M14 17c0-2 1.7-3.5 4-3.5s4 1.5 4 3.5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
  more: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="6" r="1.6" fill="currentColor"/><circle cx="12" cy="12" r="1.6" fill="currentColor"/><circle cx="12" cy="18" r="1.6" fill="currentColor"/></svg>',
  ai: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8L12 3z" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/><path d="M18.5 15.5l.8 2.2 2.2.8-2.2.8-.8 2.2-.8-2.2-2.2-.8 2.2-.8.8-2.2z" fill="currentColor"/></svg>',
  play: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5v14l11-7z" fill="currentColor"/></svg>',
  next: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 18l8.5-6L6 6v12zm10-12v12h2V6h-2z" fill="currentColor"/></svg>',
  prev: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M18 18l-8.5-6L18 6v12zM8 18V6H6v12h2z" fill="currentColor"/></svg>',
  queue: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M4 12h16M4 17h10" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
  radio: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="9" width="18" height="11" rx="2" fill="none" stroke="currentColor" stroke-width="2"/><path d="M7 9l10-5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><circle cx="8" cy="14.5" r="2" fill="none" stroke="currentColor" stroke-width="2"/><path d="M13 13h5M13 16h5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
  chevron: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 9l6 6 6-6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  close: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
  plus: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
  hqp: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 12h3l2-6 3 12 3-9 2 3h5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  output: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 9v6h4l5 4V5L8 9H4z" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/><path d="M16 9a4 4 0 0 1 0 6M18.5 6.5a8 8 0 0 1 0 11" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
  profile: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="8" r="4" fill="none" stroke="currentColor" stroke-width="2"/><path d="M4 21c0-4 3.5-6.5 8-6.5s8 2.5 8 6.5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
  gear: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h10M18 7h2M4 17h4M12 17h8" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><circle cx="16" cy="7" r="2.5" fill="none" stroke="currentColor" stroke-width="2"/><circle cx="10" cy="17" r="2.5" fill="none" stroke="currentColor" stroke-width="2"/></svg>',
  advisor: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="2"/><path d="M15.5 8.5l-2 5-5 2 2-5 5-2z" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/></svg>',
  library: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 6h6l2 2h10v10a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V6z" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/></svg>',
  phantoms: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 18a4 4 0 0 1-.5-8A6 6 0 0 1 18 9a4 4 0 0 1 0 9H7z" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/></svg>',
  sync: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 12a8 8 0 0 1 14-5.3M20 12a8 8 0 0 1-14 5.3" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><path d="M18 3v4h-4M6 21v-4h4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
};

/* ---------- content ---------- */
const ARTISTS = ['Hidden Orchestra', 'Klaus Schulze', 'Vangelis', 'Portico Quartet', 'David Gilmour',
  'Ludovico Einaudi', 'Tangerine Dream', 'Jon Hopkins', 'Gai Barone', 'Mamanet', 'The Alan Parsons Project'];
const RECS = [['Immunity', 'Jon Hopkins'], ['Moonlake', 'Klaus Schulze'], ['Phaedra', 'Tangerine Dream'],
  ['Archipelago', 'Hidden Orchestra'], ['Blade Runner', 'Vangelis'], ['Isla', 'Portico Quartet'],
  ['Singularity', 'Jon Hopkins'], ['Mirage', 'Klaus Schulze'], ['Rubycon', 'Tangerine Dream']];
const NEW = [['После Stereo', 'Mamanet'], ['Легко', 'Mamanet'], ['Patterns', 'Gai Barone'], ['Echoes', 'Ludovico Einaudi'],
  ['Remember That Night', 'David Gilmour'], ['Night Walks', 'Hidden Orchestra'], ['Art of Chill', 'Portico Quartet'],
  ['Elements', 'Ludovico Einaudi'], ['Underwater', 'Ludovico Einaudi']];
const HISTORY = [['The Turn Of A Friendly Card', 'The Alan Parsons Project'], ['Portico Quartet', 'Portico Quartet'],
  ['Live at the Royal Albert Hall', 'David Gilmour'], ['Echoes', 'Ludovico Einaudi'], ['Immunity', 'Jon Hopkins'],
  ['Moonlake', 'Klaus Schulze'], ['Phaedra', 'Tangerine Dream'], ['Archipelago', 'Hidden Orchestra'], ['Isla', 'Portico Quartet']];

const SETTINGS = [['hqp', 'HQPlayer'], ['output', 'Audio output'], ['profile', 'Profile'], ['gear', 'My gear'],
  ['advisor', 'Gear advisor'], ['library', 'Library'], ['phantoms', 'Streaming library'], ['ai', 'AI assistant'], ['sync', 'Sync']];

const artistRow = n => `<div class="row">${ARTISTS.slice(0, n).map(a =>
  `<div class="artist"><div class="avatar"></div><div class="name">${a}</div></div>`).join('')}</div>`;
const albumRow = (list, n) => `<div class="row">${list.slice(0, n).map(([t, s]) =>
  `<div class="album"><div class="cover"></div><div class="t">${t}</div><div class="s">${s}</div></div>`).join('')}</div>`;

function home({ artists, albums, brandInContent }) {
  return `
    ${brandInContent ? '<h1 class="brand" style="padding: 0 0 8px">Sautium<span class="dot">.</span></h1>' : ''}
    <section class="sec"><h2>Favourite artists</h2>${artistRow(artists)}</section>
    <section class="sec"><h2>Recommendations</h2>${albumRow(RECS, albums)}</section>
    <section class="sec"><h2>New in my collection</h2>${albumRow(NEW, albums)}</section>
    <section class="sec"><h2>Listening history</h2>${albumRow(HISTORY, albums)}</section>`;
}

function sidebar(width, { withAi }) {
  return `
  <nav class="nav" style="width: ${width}; bottom: ${BAR_H}px;">
    <div class="brand">Sautium<span class="dot">.</span></div>
    <div class="nav-row active">${I.home}<span>Home</span></div>
    <div class="nav-row">${I.discovery}<span>Discovery</span></div>
    <div class="nav-row">${I.friends}<span>Friends</span></div>
    ${withAi ? `<div class="nav-row" style="color: var(--amber);">${I.ai}<span>Ask AI</span></div>` : ''}
    <div class="nav-sect">Settings</div>
    ${SETTINGS.map(([ic, label]) => `<div class="nav-row small">${I[ic]}<span>${label}</span></div>`).join('\n    ')}
  </nav>`;
}

function rail({ withAi }) {
  return `
  <nav class="rail" style="bottom: ${BAR_H}px;">
    <div class="mark">S<span style="color: var(--amber);">.</span></div>
    <div class="rail-tab active">${I.home}<span>Home</span></div>
    <div class="rail-tab">${I.discovery}<span>Discovery</span></div>
    <div class="rail-tab">${I.friends}<span>Friends</span></div>
    <div class="rail-tab">${I.more}<span>More</span></div>
    <div class="spacer"></div>
    ${withAi ? `<div class="rail-tab" style="color: var(--amber);">${I.ai}<span>AI</span></div>` : ''}
  </nav>`;
}

function miniBar(left) {
  return `
  <div class="bar mini" style="left: ${left};">
    <div class="cov"></div>
    <div><div class="ti">High Hopes</div><div class="ar">David Gilmour · Remember That Night: Live at the Royal Albert Hall</div></div>
    <div class="btn">${I.play}</div>
    <div class="btn">${I.next}</div>
  </div>`;
}

function fullBar(left) {
  return `
  <div class="bar full" style="left: ${left};">
    <div class="who"><div class="cov"></div><div style="min-width: 0;"><div class="ti">High Hopes</div><div class="ar">David Gilmour · Remember That Night: Live at the Royal Albert Hall · 2007</div></div></div>
    <div class="centre">
      <div class="keys"><div class="btn">${I.prev}</div><div class="play40">${I.play}</div><div class="btn">${I.next}</div></div>
      <div class="line"><span class="tm">05:51</span><div class="prog"><i></i><b></b></div><span class="tm total">09:18</span></div>
    </div>
    <div class="right"><div class="btn">${I.queue}</div><div class="btn">${I.radio}</div></div>
  </div>`;
}

const npMeta = `
      <div class="meta">
        <span class="pill hires">HI-RES</span><span class="pill">D# MAJ</span>
        <span class="bpm">78<small>BPM</small></span>
        <span class="energy">ENERGY <span class="dots"><i class="on"></i><i class="on"></i><i></i><i></i><i></i></span></span>
      </div>`;
const npProgress = `
      <div class="npprog"><div class="prog"><i></i><b></b></div><div class="times"><span class="tm">05:51</span><span class="tm total">09:18</span></div></div>`;
const npTransport = `
      <div class="transport">
        <div class="t56">${I.queue}</div><div class="t56">${I.prev}</div><div class="t68">${I.play}</div><div class="t56">${I.next}</div><div class="t56">${I.radio}</div>
      </div>`;
const npSimilar = (n = 3) => `
      <div class="simhead">SIMILAR TRACKS <span>CLAP · 7 MATCHES</span></div>
      ${[['Coming Back To Life', '0.94'], ['Shine On You Crazy Diamond', '0.93'], ['Wish You Were Here', '0.91'], ['On an Island', '0.89']].slice(0, n)
        .map(([t, s]) => `<div class="sim"><div class="c"></div><div><div class="t">${t}</div><div class="s">David Gilmour · 2007</div></div><span class="score">${s}</span><div class="add">${I.plus}</div></div>`).join('\n      ')}`;

function panel(width) {
  return `
  <aside class="panel" style="width: ${width}; bottom: ${BAR_H}px;">
    <div class="head"><span>Now playing</span>${I.close}</div>
    <div class="npcover"></div>
    <div class="npbody">
      <h3 class="npt">High Hopes</h3>
      <p class="npa">David Gilmour</p>
      <p class="npal">Remember That Night: Live at the Royal Albert Hall · 2007</p>
      ${npMeta}
      ${npProgress}
      ${npTransport}
      ${npSimilar(3)}
    </div>
  </aside>`;
}

// The mobile Now Playing screen, verbatim, as a permanent panel: cover at the
// top (no chevron — nothing to close), the body scrolls under the fold.
function panelMobile(width) {
  return `
  <aside class="panel" style="width: ${width}; bottom: 0;">
    <div class="npcover"></div>
    <div class="npbody">
      <h3 class="npt">High Hopes</h3>
      <p class="npa">David Gilmour</p>
      <p class="npal">Remember That Night: Live at the Royal Albert Hall · 2007</p>
      ${npMeta}
      ${npProgress}
      ${npTransport}
      ${npSimilar(2)}
    </div>
  </aside>`;
}

function card() {
  return `
  <div class="scrim"></div>
  <div class="card">
    <div class="head"><span>Now playing</span>${I.close}</div>
    <div class="top">
      <div class="npcover"></div>
      <div class="npbody">
        <h3 class="npt">High Hopes</h3>
        <p class="npa">David Gilmour</p>
        <p class="npal">Remember That Night: Live at the Royal Albert Hall · 2007</p>
        ${npMeta}
      </div>
    </div>
    <div class="below">
      ${npProgress}
      ${npTransport}
      ${npSimilar(3)}
    </div>
  </div>`;
}

const fab = (bottom, right = 16) => `<div class="fab" style="bottom: ${bottom}px; right: ${right}px;">AI</div>`;

/* ---------- artboards ---------- */
const boards = {
  'Main.dc.html': {
    title: 'A · Sidebar + docked Now Playing', w: 1440, h: 900,
    props: { navW: { editor: 'int', default: 240, min: 200, max: 320, unit: 'px', section: 'Frame' },
             panelW: { editor: 'int', default: 380, min: 320, max: 440, unit: 'px', section: 'Frame' } },
    body: v => `
  ${sidebar(v.navW, { withAi: true })}
  <main class="content" style="left: ${v.navW}; right: ${v.panelW}; bottom: ${BAR_H}px;">${home({ artists: 7, albums: 5, brandInContent: false })}</main>
  ${miniBar(v.navW)}
  ${panel(v.panelW)}`,
  },
  'DirectionB.dc.html': {
    title: 'B · Sidebar + player bar + Now Playing card', w: 1440, h: 900,
    props: { navW: { editor: 'int', default: 240, min: 200, max: 320, unit: 'px', section: 'Frame' } },
    body: v => `
  ${sidebar(v.navW, { withAi: true })}
  <main class="content" style="left: ${v.navW}; right: 0; bottom: ${BAR_H}px;">${home({ artists: 10, albums: 7, brandInContent: false })}</main>
  ${fullBar(v.navW)}
  ${card()}`,
  },
  'DirectionC.dc.html': {
    title: 'C · Rail + docked Now Playing', w: 1440, h: 900,
    props: { panelW: { editor: 'int', default: 380, min: 320, max: 440, unit: 'px', section: 'Frame' } },
    body: v => `
  ${rail({ withAi: true })}
  <main class="content" style="left: 80px; right: ${v.panelW}; bottom: ${BAR_H}px;">${home({ artists: 8, albums: 6, brandInContent: true })}</main>
  ${miniBar('80px')}
  ${panel(v.panelW)}`,
  },
  'TabletPortrait.dc.html': {
    title: 'Tablet portrait 768 · rail + mini-player', w: 768, h: 1024,
    body: () => `
  ${rail({ withAi: false })}
  <main class="content" style="left: 80px; right: 0; bottom: ${BAR_H}px;">${home({ artists: 7, albums: 5, brandInContent: true })}</main>
  ${miniBar('80px')}
  ${fab(BAR_H + 16)}`,
  },
  'TabletLandscape.dc.html': {
    title: 'Tablet landscape 1024 · rail + Now Playing docked, no mini-player', w: 1024, h: 768,
    body: () => `
  ${rail({ withAi: false })}
  <main class="content" style="left: 80px; right: 360px; bottom: 0;">${home({ artists: 6, albums: 4, brandInContent: true })}</main>
  ${fab(16, 360 + 16)}
  ${panelMobile('360px')}`,
  },
};

const FONTS = '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter+Tight:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap">';

function dcHtml(name, b) {
  const holes = Object.fromEntries(Object.keys(b.props || {}).map(k => [k, `{{${k}}}px`]));
  const props = { ...(b.props || {}), $preview: { width: b.w, height: b.h } };
  const script = b.props ? `
<script data-dc-script data-props='${JSON.stringify(props).replace(/'/g, '&#39;')}'>
class Component extends DCLogic {
  renderVals() {
    return { ${Object.entries(b.props).map(([k, p]) => `${k}: this.props.${k} ?? ${p.default}`).join(', ')} };
  }
}
</script>` : '';
  return `<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <script src="./support.js"></script>
</head>
<body>
<x-dc>
<helmet>
  ${FONTS}
  <style>${STYLE}</style>
</helmet>
<div class="frame" style="width: ${b.w}px; height: ${b.h}px;">${b.body(holes)}
</div>
</x-dc>${script}
</body>
</html>
`;
}

function previewHtml(b) {
  const values = Object.fromEntries(Object.entries(b.props || {}).map(([k, p]) => [k, `${p.default}px`]));
  return `<!doctype html>
<html><head><meta charset="utf-8">${FONTS}<style>${STYLE}</style></head>
<body><div class="frame" style="width: ${b.w}px; height: ${b.h}px;">${b.body(values)}
</div></body></html>
`;
}

for (const [name, b] of Object.entries(boards)) {
  writeFileSync(join(here, name), dcHtml(name, b));
  if (previewDir) {
    mkdirSync(previewDir, { recursive: true });
    writeFileSync(join(previewDir, name.replace('.dc.html', '.html')), previewHtml(b));
  }
}

const GAP_X = 80, GAP_Y = 120;
const canvas = {
  pages: [
    { id: 'tablet', name: 'Tablet' },
    { id: 'desktop', name: 'Desktop (later)' },
  ],
  artboards: [
    { file: 'TabletPortrait.dc.html', title: boards['TabletPortrait.dc.html'].title, page: 'tablet', x: 0, y: 0, w: 768, h: 1024 },
    { file: 'TabletLandscape.dc.html', title: boards['TabletLandscape.dc.html'].title, page: 'tablet', x: 768 + GAP_X, y: 0, w: 1024, h: 768 },
    { file: 'Main.dc.html', title: boards['Main.dc.html'].title, page: 'desktop', x: 0, y: 0, w: 1440, h: 900 },
    { file: 'DirectionB.dc.html', title: boards['DirectionB.dc.html'].title, page: 'desktop', x: 1440 + GAP_X, y: 0, w: 1440, h: 900 },
    { file: 'DirectionC.dc.html', title: boards['DirectionC.dc.html'].title, page: 'desktop', x: 2 * (1440 + GAP_X), y: 0, w: 1440, h: 900 },
  ],
  annotations: [
    { id: 'tablet-brief', page: 'tablet', x: 0, y: -220, w: 560, text:
      'Tablet first (2026-09-11). Same DOM and tokens as the phone app; only the frame changes. Base sizes: 768×1024 portrait, 1024×768 landscape (the smallest iPad — wider iPads get more room, never more chrome). Shelves run under the right edge exactly as on the phone: a cut tile is the scroll affordance, not a gutter.' },
    { id: 'portrait-note', page: 'tablet', x: 620, y: -220, w: 380, text:
      'Portrait: rail (80) + Home + mini-player bar at the bottom + AI FAB. Tapping the mini-player opens the mobile Now Playing sheet as a centred card over a scrim.' },
    { id: 'landscape-note', page: 'tablet', x: 1020, y: -220, w: 420, text:
      'Landscape: rail (80) + content + the mobile Now Playing screen docked at its native 360px — always on, so no mini-player. The Queue opens in the same slot over it, as on the phone. The FAB sits left of the panel. Content gets 584px at 1024 (more on wider iPads).' },
    { id: 'desktop-brief', page: 'desktop', x: 0, y: -200, w: 560, text:
      'Desktop directions — parked. Kept for the later desktop cycle; nothing here is being built now. A: sidebar + docked NP · B: sidebar + full player bar + NP card · C: rail + docked NP.' },
  ],
  launch: { view: 'canvas', page: 'tablet' },
};
writeFileSync(join(here, 'canvas.json'), JSON.stringify(canvas, null, 2) + '\n');
console.log(`wrote ${Object.keys(boards).length} artboards + canvas.json${previewDir ? ` + previews in ${previewDir}` : ''}`);
