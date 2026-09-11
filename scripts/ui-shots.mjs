#!/usr/bin/env node
/* scripts/ui-shots.mjs — Sautium UI screenshots at several viewport widths.
 *
 * Drives a headless Chrome over CDP with nothing but Node 22 (built-in
 * WebSocket) and writes one PNG per route × viewport plus a contact sheet.
 * Written for the WSL setup: Chrome comes from puppeteer's download cache,
 * its runtime libraries from a `dpkg -x` directory (no sudo), the device
 * token from the running backend container.
 *
 *   node scripts/ui-shots.mjs --out tmp/ui-shots/baseline --widths 360x800
 *   node scripts/ui-shots.mjs --ids artist=<uuid>,album=<uuid>,genre=<uuid>,session=<uuid>
 *   node scripts/ui-shots.mjs --compare tmp/ui-shots/baseline tmp/ui-shots/after
 *
 * Options (defaults in DEFAULTS):
 *   --url       backend origin
 *   --out       output directory (tmp/ is git-ignored)
 *   --widths    comma list of WxH viewports
 *   --routes    comma list of hash routes; a `+np`, `+queue`, `+ai` or
 *               `+more` suffix opens that overlay on top of the route
 *   --ids       entity ids, rendered as home/<kind>/<id>
 *   --artboards directory of reference PNGs named <slug>@<width>.png,
 *               shown beside the shots in the contact sheet when present
 *   --chrome    Chrome binary (default: newest under ~/.cache/puppeteer/chrome)
 *   --libs      directory holding usr/lib/x86_64-linux-gnu (LD_LIBRARY_PATH)
 *   --token     device token (else $SAUTIUM_TOKEN, else `docker exec`)
 *   --quiet     ms of DOM silence that counts as settled (default 500;
 *               raise it for a build that does not publish
 *               window.sautiumRendered)
 *
 * Never waits for network idle: the SSE stream keeps a connection open
 * forever. A route counts as rendered when fonts are ready and the DOM has
 * been quiet for 500 ms (mini-player progress ticks excluded), capped at
 * 8 s. Screens are captured full-page, overlays at viewport size.
 */

import { execFileSync, spawn } from 'node:child_process';
import { inflateSync } from 'node:zlib';
import { existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { homedir, tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const DEFAULTS = {
  url: 'https://localhost:8800',
  out: 'tmp/ui-shots',
  widths: '360x800,834x1194,1194x834,1280x800,1440x900',
  routes: [
    'home', 'home+np', 'home+queue', 'home+ai', 'home+more',
    'discovery', 'friends',
    'more/library', 'more/output', 'more/profile', 'more/hqplayer', 'more/ai',
    'more/sync', 'more/phantoms', 'more/gear-system', 'more/gear-advisor',
  ].join(','),
  ids: '',
  artboards: '',
  chrome: '',
  libs: '',
  token: process.env.SAUTIUM_TOKEN || '',
  quiet: '500',
};

const OVERLAYS = {
  np: {
    needs: `!document.getElementById('miniPlayer').hidden`,
    open: `document.getElementById('miniPlayer').click()`,
    close: `document.getElementById('npClose').click()`,
  },
  queue: {
    needs: `!document.getElementById('miniPlayer').hidden`,
    open: `document.getElementById('miniPlayer').click(); document.getElementById('npQueueBtn').click()`,
    close: `document.getElementById('queueCloseBtn').click(); document.getElementById('npClose').click()`,
  },
  ai: {
    needs: `!document.getElementById('aiFab').hidden`,
    open: `document.getElementById('aiFab').click()`,
    close: `[...document.querySelectorAll('#aiCloseBtnChat, #aiCloseBtnList')].find(b => b.offsetParent)?.click()`,
  },
  more: {
    needs: 'true',
    open: `document.querySelector('.nav-tab[data-route="more"]').click()`,
    close: `document.querySelector('.more-scrim')?.click()`,
  },
};

const TOKEN_KEY = 'sautium.device_token';

const SETTLE = quiet => `new Promise(resolve => {
  const QUIET = ${quiet}, CAP = 8000, started = performance.now();
  const ignore = '#miniPlayer, #npProgressTrack, #npTimeCurrent, #npTimeTotal';
  let timer = null;
  const finish = () => {
    observer.disconnect(); clearTimeout(timer); clearTimeout(cap);
    resolve(Math.round(performance.now() - started));
  };
  const arm = () => { clearTimeout(timer); timer = setTimeout(finish, QUIET); };
  const observer = new MutationObserver(records => {
    const owner = r => (r.target.nodeType === 1 ? r.target : r.target.parentElement);
    if (records.every(r => owner(r)?.closest(ignore))) return;
    arm();
  });
  observer.observe(document.documentElement,
    { subtree: true, childList: true, attributes: true, characterData: true });
  const cap = setTimeout(finish, CAP);
  document.fonts.ready.then(arm);
})`;

// Cover art arrives without a DOM mutation (<img> decode, CSS background
// fetch), so the quiet DOM says nothing about it. Wait for every <img> and
// every background-image URL in the document before capturing.
const IMAGES_READY = `new Promise(resolve => {
  const urls = new Set();
  for (const el of document.querySelectorAll('*')) {
    const bg = getComputedStyle(el).backgroundImage;
    const m = bg && bg.match(/url\\("?([^")]+)"?\\)/);
    if (m) urls.add(m[1]);
  }
  for (const i of document.images) if (i.loading === 'lazy') i.loading = 'eager';
  const waits = [...document.images].filter(i => !i.complete)
    .map(i => new Promise(r => { i.addEventListener('load', r, { once: true }); i.addEventListener('error', r, { once: true }); }));
  for (const u of urls) waits.push(new Promise(r => { const i = new Image(); i.onload = i.onerror = r; i.src = u; }));
  const cap = setTimeout(resolve, 10000);
  Promise.all(waits).then(() => { clearTimeout(cap); resolve(); });
})`;

// Navigate by hash and wait for the app's own "route painted" promise
// (window.sautiumRendered, set by render() on hashchange) when the build
// publishes one; older builds fall back to the quiet-DOM settle alone.
// The hashchange listener is registered before the hash moves, and the
// app's listener (registered at boot) runs first, so the promise read
// afterwards is the new route's.
const GO_TO = hash => `new Promise(resolve => {
  const target = ${JSON.stringify('#' + hash)};
  // A renderer whose promise never settles must not hang the run: after
  // 15 s the quiet-DOM settle that follows is the only signal left.
  const capped = p => Promise.race([p.then(() => 'rendered'), new Promise(r => setTimeout(() => r('timeout'), 15000))]);
  const done = () => resolve(window.sautiumRendered ? capped(window.sautiumRendered) : 'quiet');
  if (location.hash === target) return done();
  addEventListener('hashchange', done, { once: true });
  location.hash = target;
})`;

const TWO_FRAMES = 'new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))';

function parseArgs(argv) {
  const opts = { ...DEFAULTS, compare: null };
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === '--compare') { opts.compare = [argv[++i], argv[++i]]; continue; }
    const key = arg.startsWith('--') ? arg.slice(2) : null;
    if (!key || !(key in DEFAULTS)) throw new Error(`unknown option ${arg}`);
    opts[key] = argv[++i];
  }
  return opts;
}

function findChrome() {
  const root = join(homedir(), '.cache', 'puppeteer', 'chrome');
  const version = name => name.split('-')[1].split('.').map(Number);
  const builds = existsSync(root)
    ? readdirSync(root).filter(d => d.startsWith('linux-'))
        .sort((a, b) => version(a).reduce((acc, n, i) => acc || n - version(b)[i], 0))
    : [];
  if (!builds.length) throw new Error(`no Chrome under ${root}; pass --chrome`);
  return join(root, builds.at(-1), 'chrome-linux64', 'chrome');
}

function deriveToken() {
  const py = 'from auth_hmac import secret_path; from device_auth import current_token; '
    + 'print(current_token(secret_path().read_bytes().strip()))';
  return execFileSync('docker', ['exec', 'sautium-backend', 'python3', '-c', py], { encoding: 'utf8' }).trim();
}

function launchChrome(chromePath, libs) {
  const userData = mkdtempSync(join(tmpdir(), 'sautium-ui-shots-'));
  const env = { ...process.env };
  if (libs) {
    env.LD_LIBRARY_PATH = [join(resolve(libs), 'usr/lib/x86_64-linux-gnu'), env.LD_LIBRARY_PATH]
      .filter(Boolean).join(':');
  }
  const proc = spawn(chromePath, [
    '--headless=new', '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage',
    '--ignore-certificate-errors', '--hide-scrollbars', '--no-first-run',
    '--no-default-browser-check', `--user-data-dir=${userData}`,
    '--remote-debugging-port=0', '--window-size=1440,900', 'about:blank',
  ], { env, stdio: ['ignore', 'ignore', 'pipe'] });
  return new Promise((resolvePromise, reject) => {
    let stderr = '';
    proc.stderr.on('data', chunk => {
      stderr += chunk;
      const match = stderr.match(/DevTools listening on (ws:\/\/\S+)/);
      if (match) resolvePromise({ proc, wsUrl: match[1], userData });
    });
    proc.on('exit', code => reject(new Error(`Chrome exited with ${code}:\n${stderr}`)));
  });
}

class Cdp {
  constructor(ws) {
    this.ws = ws;
    this.seq = 0;
    this.pending = new Map();
    this.listeners = new Map();
    ws.addEventListener('message', ev => this._onMessage(JSON.parse(ev.data)));
  }

  static async connect(url) {
    const ws = new WebSocket(url);
    await new Promise((resolvePromise, reject) => {
      ws.addEventListener('open', resolvePromise, { once: true });
      ws.addEventListener('error', reject, { once: true });
    });
    return new Cdp(ws);
  }

  _onMessage(msg) {
    if (msg.id !== undefined) {
      const call = this.pending.get(msg.id);
      this.pending.delete(msg.id);
      if (msg.error) call.reject(new Error(`${call.method}: ${msg.error.message}`));
      else call.resolve(msg.result);
      return;
    }
    const key = `${msg.sessionId || ''}:${msg.method}`;
    for (const fn of this.listeners.get(key) || []) fn(msg.params);
  }

  send(method, params = {}, sessionId) {
    const id = ++this.seq;
    const msg = { id, method, params };
    if (sessionId) msg.sessionId = sessionId;
    return new Promise((resolvePromise, reject) => {
      this.pending.set(id, { method, resolve: resolvePromise, reject });
      this.ws.send(JSON.stringify(msg));
    });
  }

  on(method, sessionId, fn) {
    const key = `${sessionId || ''}:${method}`;
    if (!this.listeners.has(key)) this.listeners.set(key, []);
    this.listeners.get(key).push(fn);
  }

  once(method, sessionId) {
    const key = `${sessionId || ''}:${method}`;
    if (!this.listeners.has(key)) this.listeners.set(key, []);
    const list = this.listeners.get(key);
    return new Promise(resolvePromise => {
      const fn = params => { list.splice(list.indexOf(fn), 1); resolvePromise(params); };
      list.push(fn);
    });
  }

  close() { this.ws.close(); }
}

class Page {
  constructor(cdp, sessionId) {
    this.cdp = cdp;
    this.sessionId = sessionId;
    this.errors = [];
    cdp.on('Runtime.exceptionThrown', sessionId, ({ exceptionDetails: d }) => {
      const text = d.exception?.description || d.text;
      this.errors.push(`${text}${d.url ? ` (${d.url}:${d.lineNumber})` : ''}`);
    });
    cdp.on('Runtime.consoleAPICalled', sessionId, ({ type, args }) => {
      if (type !== 'error') return;
      this.errors.push(`console.error: ${args.map(a => a.value ?? a.description ?? '').join(' ')}`);
    });
  }

  takeErrors() { const list = this.errors; this.errors = []; return list; }

  send(method, params) { return this.cdp.send(method, params, this.sessionId); }

  async eval(expression, awaitPromise = false) {
    const { result, exceptionDetails } = await this.send('Runtime.evaluate',
      { expression, awaitPromise, returnByValue: true });
    if (exceptionDetails) {
      throw new Error(exceptionDetails.exception?.description || exceptionDetails.text);
    }
    return result.value;
  }

  settle() { return this.eval(SETTLE(this.quiet), true); }

  async navigate(url) {
    const loaded = this.cdp.once('Page.loadEventFired', this.sessionId);
    await this.send('Page.navigate', { url });
    await loaded;
  }

  async reload() {
    const loaded = this.cdp.once('Page.loadEventFired', this.sessionId);
    await this.send('Page.reload');
    await loaded;
  }

  metrics({ width, height }) {
    return this.send('Emulation.setDeviceMetricsOverride',
      { width, height, deviceScaleFactor: width < 768 ? 2 : 1, mobile: width < 768 });
  }

  async setViewport(vp) {
    await this.metrics(vp);
    await this.send('Emulation.setTouchEmulationEnabled', { enabled: vp.width < 768 });
  }

  async shoot(file, vp, fullPage) {
    let height = vp.height;
    if (fullPage) {
      const scrollHeight = await this.eval('document.documentElement.scrollHeight');
      height = Math.min(Math.max(scrollHeight, vp.height), 6000);
    }
    if (height !== vp.height) await this.metrics({ width: vp.width, height });
    await this.eval(IMAGES_READY, true);
    await this.eval(TWO_FRAMES, true);
    const { data } = await this.send('Page.captureScreenshot', { format: 'png' });
    writeFileSync(file, Buffer.from(data, 'base64'));
    if (height !== vp.height) await this.metrics(vp);
  }
}

const slugOf = route => route.replace(/\//g, '_');

async function shootRoute(page, route, vp, out) {
  const [base, overlay] = route.split('+');
  const file = join(out, `${slugOf(route)}@${vp.width}x${vp.height}.png`);
  await page.eval(GO_TO(base), true);
  let ms = await page.settle();
  if (overlay) {
    const ov = OVERLAYS[overlay];
    if (!ov) throw new Error(`unknown overlay +${overlay} in ${route}`);
    if (!(await page.eval(ov.needs))) return { route, vp, note: `skipped — ${overlay} not available` };
    await page.eval(ov.open);
    ms += await page.settle();
  }
  await page.shoot(file, vp, !overlay);
  if (overlay) {
    await page.eval(OVERLAYS[overlay].close);
    await page.settle();
  }
  return { route, vp, ms, errors: page.takeErrors() };
}

function writeContactSheet(out, routes, widths, artboards) {
  const cell = (dir, file) => (existsSync(join(dir, file))
    ? `<td><a href="${file}"><img src="${file}" loading="lazy"></a></td>`
    : '<td class="missing">—</td>');
  const head = widths.map(vp => `<th>${vp.width}×${vp.height}</th>`).join('');
  const rows = routes.map(route => {
    const slug = slugOf(route);
    const shots = widths.map(vp => cell(out, `${slug}@${vp.width}x${vp.height}.png`)).join('');
    const refs = artboards
      ? widths.map(vp => {
          const file = `${slug}@${vp.width}.png`;
          return existsSync(join(artboards, file))
            ? `<td class="ref"><img src="${join(resolve(artboards), file)}" loading="lazy"></td>`
            : '<td class="ref missing">—</td>';
        }).join('')
      : '';
    return `<tr><th>${route}</th>${shots}${refs}</tr>`;
  }).join('\n');
  const refHead = artboards ? widths.map(vp => `<th class="ref">artboard ${vp.width}</th>`).join('') : '';
  writeFileSync(join(out, 'index.html'), `<!doctype html>
<meta charset="utf-8">
<title>Sautium UI shots</title>
<style>
  body { margin: 16px; background: #111; color: #ddd; font: 13px system-ui, sans-serif; }
  table { border-collapse: collapse; }
  th, td { border: 1px solid #333; padding: 6px; vertical-align: top; text-align: left; }
  th.ref, td.ref { background: #1a1a1a; }
  img { display: block; max-width: 480px; max-height: 720px; object-fit: contain; object-position: top; }
  .missing { color: #666; text-align: center; }
</style>
<table><tr><th>route</th>${head}${refHead}</tr>
${rows}
</table>
`);
}

// Minimal PNG reader for Chrome's own output (8-bit RGB/RGBA, no interlace).
function decodePng(buf) {
  let pos = 8, width = 0, height = 0, channels = 0;
  const idat = [];
  while (pos < buf.length) {
    const len = buf.readUInt32BE(pos), type = buf.toString('ascii', pos + 4, pos + 8);
    const data = buf.subarray(pos + 8, pos + 8 + len);
    if (type === 'IHDR') {
      width = data.readUInt32BE(0); height = data.readUInt32BE(4);
      const depth = data[8], colour = data[9];
      if (depth !== 8 || data[12] !== 0) throw new Error(`unsupported PNG (depth ${depth}, interlace ${data[12]})`);
      channels = { 0: 1, 2: 3, 4: 2, 6: 4 }[colour];
    } else if (type === 'IDAT') idat.push(data);
    pos += 12 + len;
  }
  const raw = inflateSync(Buffer.concat(idat));
  const stride = width * channels, px = Buffer.alloc(stride * height);
  for (let y = 0; y < height; y++) {
    const filter = raw[y * (stride + 1)], src = y * (stride + 1) + 1, dst = y * stride;
    for (let x = 0; x < stride; x++) {
      const a = x >= channels ? px[dst + x - channels] : 0;
      const b = y > 0 ? px[dst - stride + x] : 0;
      const c = (x >= channels && y > 0) ? px[dst - stride + x - channels] : 0;
      let v = raw[src + x];
      if (filter === 1) v += a;
      else if (filter === 2) v += b;
      else if (filter === 3) v += (a + b) >> 1;
      else if (filter === 4) { const p = a + b - c, pa = Math.abs(p - a), pb = Math.abs(p - b), pc = Math.abs(p - c); v += (pa <= pb && pa <= pc) ? a : (pb <= pc ? b : c); }
      px[dst + x] = v & 255;
    }
  }
  return { width, height, channels, px };
}

function pixelDiff(a, b) {
  if (a.width !== b.width || a.height !== b.height) return { size: `${a.width}x${a.height} vs ${b.width}x${b.height}` };
  const ch = Math.min(a.channels, b.channels);
  let count = 0, x0 = Infinity, y0 = Infinity, x1 = -1, y1 = -1;
  for (let y = 0; y < a.height; y++) {
    for (let x = 0; x < a.width; x++) {
      const ia = (y * a.width + x) * a.channels, ib = (y * b.width + x) * b.channels;
      let differs = false;
      for (let c = 0; c < ch; c++) if (a.px[ia + c] !== b.px[ib + c]) { differs = true; break; }
      if (!differs) continue;
      count++;
      if (x < x0) x0 = x; if (x > x1) x1 = x; if (y < y0) y0 = y; if (y > y1) y1 = y;
    }
  }
  return { count, box: count ? `x ${x0}–${x1}, y ${y0}–${y1}` : '' };
}

function compare(dirA, dirB) {
  const files = readdirSync(dirA).filter(f => f.endsWith('.png')).sort();
  let differing = 0;
  for (const file of files) {
    const other = join(dirB, file);
    if (!existsSync(other)) { console.log(`MISSING  ${file}`); differing++; continue; }
    const bufA = readFileSync(join(dirA, file)), bufB = readFileSync(other);
    if (bufA.equals(bufB)) { console.log(`same     ${file}`); continue; }
    const d = pixelDiff(decodePng(bufA), decodePng(bufB));
    if (d.size) { console.log(`DIFF     ${file}  size ${d.size}`); differing++; continue; }
    if (!d.count) { console.log(`same     ${file}  (bytes differ, pixels equal)`); continue; }
    differing++;
    console.log(`DIFF     ${file}  ${d.count} px  ${d.box}`);
  }
  console.log(`${files.length} compared, ${differing} differ`);
  process.exitCode = differing ? 1 : 0;
}

async function main() {
  const opts = parseArgs(process.argv.slice(2));
  if (opts.compare) return compare(...opts.compare);

  const widths = opts.widths.split(',').map(s => {
    const [width, height] = s.split('x').map(Number);
    return { width, height };
  });
  const routes = opts.routes.split(',').filter(Boolean);
  for (const pair of opts.ids.split(',').filter(Boolean)) {
    const [kind, id] = pair.split('=');
    routes.push(`home/${kind}/${id}`);
  }
  const token = opts.token || deriveToken();
  const chromePath = opts.chrome || findChrome();
  const out = resolve(opts.out);
  mkdirSync(out, { recursive: true });

  const { proc, wsUrl, userData } = await launchChrome(chromePath, opts.libs);
  const cdp = await Cdp.connect(wsUrl);
  const results = [];
  try {
    const { targetId } = await cdp.send('Target.createTarget', { url: 'about:blank' });
    const { sessionId } = await cdp.send('Target.attachToTarget', { targetId, flatten: true });
    const page = new Page(cdp, sessionId);
    page.quiet = Number(opts.quiet);
    await page.send('Page.enable');
    await page.send('Runtime.enable');
    for (const vp of widths) {
      await page.setViewport(vp);
      await page.navigate(`${opts.url}/`);
      await page.eval(`localStorage.setItem(${JSON.stringify(TOKEN_KEY)}, ${JSON.stringify(token)})`);
      await page.reload();
      await page.settle();
      page.takeErrors();
      for (const route of routes) {
        const result = await shootRoute(page, route, vp, out);
        results.push(result);
        const errs = result.errors?.length ? `  !! ${result.errors.length} js error(s)` : '';
        console.log(`${vp.width}x${vp.height}  ${route.padEnd(28)} ${result.note || `${result.ms} ms`}${errs}`);
      }
    }
  } finally {
    cdp.close();
    const exited = new Promise(resolvePromise => proc.once('exit', resolvePromise));
    proc.kill();
    await exited;
    try {
      rmSync(userData, { recursive: true, force: true, maxRetries: 50, retryDelay: 200 });
    } catch (err) {
      // Chrome's renderers can outlive the browser process by a moment;
      // a leftover profile in the temp dir is not worth failing the run.
      console.warn(`profile dir not removed: ${userData} (${err.code})`);
    }
  }
  writeContactSheet(out, routes, widths, opts.artboards);
  const errorLog = results.filter(r => r.errors?.length)
    .map(r => `## ${r.route} @ ${r.vp.width}x${r.vp.height}\n${r.errors.join('\n')}\n`).join('\n');
  writeFileSync(join(out, 'errors.log'), errorLog);
  console.log(`${results.filter(r => !r.note).length} shots in ${out} — open ${join(out, 'index.html')}`
    + (errorLog ? `\nJS errors were thrown — see ${join(out, 'errors.log')}` : '\nno JS errors'));
}

// Importable as a library for ad-hoc probes (the CDP plumbing, Chrome
// launch, token derivation); runs the CLI only when executed directly.
export { Cdp, Page, launchChrome, findChrome, deriveToken, GO_TO, IMAGES_READY, TWO_FRAMES, decodePng, pixelDiff };

if (import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch(err => { console.error(err.stack || err); process.exit(1); });
}
