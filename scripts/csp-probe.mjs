#!/usr/bin/env node
/* scripts/csp-probe.mjs — what a browser does with the node's
 * Content-Security-Policy: loads from another origin planted on the
 * signed-in page, one of every kind the policy governs.
 *
 * A third party listens on another loopback address and counts what it is
 * asked. The loads are planted on the node's page as its own script would
 * make them, then once more with DevTools bypassing the policy: the control,
 * which shows each load would have reached the third party.
 *
 *   node scripts/csp-probe.mjs --url http://localhost:8800 --token "$TOKEN"
 *
 * Under an enforced policy each planted load must be refused before it
 * leaves the browser; under report-only each must load and be reported.
 * Either way the control must reach the third party and the browser's
 * report the node's report route. Exit status 0 when the page did what its
 * header says. Options as scripts/ui-shots.mjs (--url, --token, --chrome,
 * --libs).
 */

import { rmSync } from 'node:fs';
import { createServer } from 'node:http';
import { pathToFileURL } from 'node:url';
import { Cdp, Page, launchChrome, findChrome, deriveToken, CSP_WATCH } from './ui-shots.mjs';

const TOKEN_KEY = 'sautium.device_token';
const PNG = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==', 'base64');

// One load of each kind, by the directive that governs it: the expression
// plants it and resolves to what the page saw — 'load', 'error', or 'sent'
// where the page is told nothing.
const KINDS = [
  { kind: 'img-src', file: 'image.png',
    plant: u => `new Promise(r => { const i = new Image(); i.onload = () => r('load'); i.onerror = () => r('error'); i.src = '${u}'; document.body.append(i); })` },
  { kind: 'img-src (CSS)', file: 'background.png',
    plant: u => `(document.body.append(Object.assign(document.createElement('div'), { style: 'width:8px;height:8px;background-image:url(${u})' })), 'sent')` },
  { kind: 'connect-src', file: 'fetch',
    plant: u => `fetch('${u}').then(() => 'load', () => 'error')` },
  { kind: 'script-src', file: 'script.js',
    plant: u => `new Promise(r => { const s = document.createElement('script'); s.onload = () => r('load'); s.onerror = () => r('error'); s.src = '${u}'; document.head.append(s); })` },
  { kind: 'style-src', file: 'sheet.css',
    plant: u => `new Promise(r => { const l = document.createElement('link'); l.rel = 'stylesheet'; l.onload = () => r('load'); l.onerror = () => r('error'); l.href = '${u}'; document.head.append(l); })` },
  { kind: 'font-src', file: 'font.woff2',
    plant: u => `new FontFace('csp-probe', 'url(${u})').load().then(() => 'load', () => 'error')` },
  { kind: 'media-src', file: 'audio.wav',
    plant: u => `(Object.assign(new Audio(), { src: '${u}' }).load(), 'sent')` },
  { kind: 'frame-src', file: 'frame.html',
    plant: u => `(document.body.append(Object.assign(document.createElement('iframe'), { src: '${u}', hidden: true })), 'sent')` },
];

function parseArgs(argv) {
  const opts = { url: 'http://localhost:8800', token: process.env.SAUTIUM_TOKEN || '', chrome: '', libs: '' };
  for (let i = 0; i < argv.length; i++) {
    const key = argv[i].startsWith('--') ? argv[i].slice(2) : null;
    if (!key || !(key in opts)) throw new Error(`unknown option ${argv[i]}`);
    opts[key] = argv[++i];
  }
  return opts;
}

// The third party: answers anything, counts every path it is asked for.
async function thirdParty() {
  const asked = new Map();
  const server = createServer((req, res) => {
    const path = req.url.split('?')[0];
    asked.set(path, (asked.get(path) || 0) + 1);
    res.writeHead(200, { 'Access-Control-Allow-Origin': '*',
      'Content-Type': path.endsWith('.png') ? 'image/png' : 'text/plain' });
    res.end(path.endsWith('.png') ? PNG : '');
  });
  for (const host of ['127.0.0.2', '127.0.0.1']) {
    try {
      await new Promise((ok, fail) => { server.once('error', fail); server.listen(0, host, ok); });
      return { origin: `http://${host}:${server.address().port}`, asked, server };
    } catch { /* not a loopback address of this machine: the next one */ }
  }
  throw new Error('no loopback address to listen on');
}

// Each kind once, under /<phase>/; what the page saw, by kind.
async function plant(page, origin, phase) {
  const seen = {};
  for (const { kind, file, plant: expr } of KINDS) {
    seen[kind] = await page.eval(`Promise.race([${expr(`${origin}/${phase}/${file}`)}, `
      + `new Promise(r => setTimeout(() => r('no answer'), 5000))])`, true);
  }
  await new Promise(r => setTimeout(r, 2000));     // what was sent has reached the third party
  return seen;
}

async function signedIn(page, url, token) {
  await page.navigate(`${url}/`);
  await page.eval(`localStorage.setItem(${JSON.stringify(TOKEN_KEY)}, ${JSON.stringify(token)})`);
  await page.reload();
  await page.settle();
}

async function main() {
  const opts = parseArgs(process.argv.slice(2));
  const shell = await fetch(`${opts.url}/`);
  const enforced = shell.headers.get('content-security-policy');
  const reportOnly = shell.headers.get('content-security-policy-report-only');
  if (!enforced && !reportOnly) throw new Error(`${opts.url}/ carries no Content-Security-Policy`);
  const mode = enforced ? 'enforce' : 'report';
  console.log(`${enforced ? 'Content-Security-Policy' : 'Content-Security-Policy-Report-Only'}: ${enforced || reportOnly}`);

  const token = opts.token || deriveToken();
  const third = await thirdParty();
  console.log(`third party at ${third.origin}`);
  const { proc, wsUrl, userData } = await launchChrome(opts.chrome || findChrome(), opts.libs);
  const cdp = await Cdp.connect(wsUrl);
  const failures = [];
  try {
    const { targetId } = await cdp.send('Target.createTarget', { url: 'about:blank' });
    const { sessionId } = await cdp.send('Target.attachToTarget', { targetId, flatten: true });
    const page = new Page(cdp, sessionId);
    page.quiet = 500;
    const reportRequests = new Set(), reportAnswers = [];
    cdp.on('Network.requestWillBeSent', sessionId, ({ requestId, request }) => {
      if (request.url.startsWith(`${opts.url}/api/csp-report`)) reportRequests.add(requestId);
    });
    cdp.on('Network.responseReceived', sessionId, ({ requestId, response }) => {
      if (reportRequests.has(requestId)) reportAnswers.push(response.status);
    });
    await page.send('Page.enable');
    await page.send('Runtime.enable');
    await page.send('Network.enable');
    await page.send('Page.addScriptToEvaluateOnNewDocument', { source: CSP_WATCH });
    await signedIn(page, opts.url, token);

    const planted = await plant(page, third.origin, 'planted');
    const violations = (await page.eval('(window.__cspViolations || []).splice(0)')).map(v => v.split(' '));
    await page.send('Page.setBypassCSP', { enabled: true });
    await page.reload();
    await page.settle();
    await plant(page, third.origin, 'control');
    await page.send('Page.setBypassCSP', { enabled: false });

    console.log(`\n${'kind'.padEnd(14)} ${'page saw'.padEnd(10)} ${'third party'.padEnd(12)} ${'violation'.padEnd(24)} control: third party`);
    for (const { kind, file } of KINDS) {
      const url = `${third.origin}/planted/${file}`;
      const asked = third.asked.get(`/planted/${file}`) || 0;
      const control = third.asked.get(`/control/${file}`) || 0;
      // [disposition, effective directive, blocked URI, source]: script-src
      // and style-src are reported as their -elem forms
      const v = violations.find(([, directive, blocked]) => directive.startsWith(kind.split(' ')[0])
        && (blocked === url || blocked === third.origin));
      console.log(`${kind.padEnd(14)} ${planted[kind].padEnd(10)} ${String(asked).padEnd(12)} `
        + `${(v ? `${v[0]} ${v[1]}` : 'none').padEnd(24)} ${control}`);
      if (!v || v[0] !== mode) failures.push(`${kind}: no violation with disposition ${mode}`);
      if (mode === 'enforce' && asked) failures.push(`${kind}: the third party was asked ${asked} time(s)`);
      if (mode === 'enforce' && planted[kind] === 'load') failures.push(`${kind}: the page saw it load`);
      if (mode === 'report' && !asked) failures.push(`${kind}: report-only, and still nothing reached the third party`);
      if (!control) failures.push(`${kind}: the control never reached the third party — the probe would see nothing`);
    }
    const answered = reportAnswers.filter(s => s === 204).length;
    console.log(`\nreports posted to ${opts.url}/api/csp-report: ${reportRequests.size}, answered 204: ${answered}`);
    if (!answered) failures.push('no report reached the node');
  } finally {
    cdp.close();
    const exited = new Promise(r => proc.once('exit', r));
    proc.kill();
    await exited;
    third.server.close();
    try {
      rmSync(userData, { recursive: true, force: true, maxRetries: 50, retryDelay: 200 });
    } catch (err) {
      console.warn(`profile dir not removed: ${userData} (${err.code})`);
    }
  }
  console.log(failures.length ? `\nFAILED\n- ${failures.join('\n- ')}`
    : `\nPASSED: every planted load ${mode === 'enforce' ? 'refused' : 'loaded and reported'}; `
      + 'the control reached the third party');
  process.exitCode = failures.length ? 1 : 0;
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch(err => { console.error(err.stack || err); process.exit(1); });
}
