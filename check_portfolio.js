#!/usr/bin/env node
/*
 * Regression check for portfolio.html. No dependencies — plain `node check_portfolio.js`.
 *
 *   node check_portfolio.js --save     record the current output as the baseline
 *   node check_portfolio.js            re-run and diff against that baseline
 *
 * How it works: the scripts portfolio.html loads are concatenated in page order and run in a vm
 * context against a mini DOM defined below and the real CSVs in parqet/ and data_series/. Add a
 * <script src> to the page and it is picked up here automatically. It then dumps
 *
 *   - every computed field of every open and closed position, in both modes,
 *   - four as-of reconstructions (the past-date picker), and
 *   - the HTML of every tooltip, header tile and legend the renderers produce,
 *     in both modes and with "Show €" both on and off,
 *
 * into check_baseline.json. The clock is frozen, so two runs of unchanged code produce byte-
 * identical output and any diff is a real behaviour change.
 *
 * Use it around a refactor: --save on the code you trust, then a bare run after the edit. A clean
 * run proves the numbers and the rendered text are untouched; a diff shows exactly what moved.
 * It cannot see layout, colour or anything that needs a real browser — check those by eye.
 *
 * The baseline is data, not truth: when a change is *meant* to alter output, read the diff, agree
 * with it, then --save over it.
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = __dirname;
const PAGE = path.join(ROOT, 'portfolio.html');
const BASELINE = path.join(ROOT, 'check_baseline.json');
const SAVE = process.argv.includes('--save');

/* ---------- the page's scripts, plus a few hooks into their scope ----------
   Concatenated in the order portfolio.html loads them, which is also the order they depend on.
   `let`/`const` at the top level of a vm script are not reachable from outside it, so the hooks
   have to be appended to the source itself, where they close over everything. */
const SCRIPTS = [...fs.readFileSync(PAGE, 'utf8').matchAll(/<script src="([^"]+)"><\/script>/g)]
  .map(m => m[1]);
if (!SCRIPTS.length) { console.error(`no <script src> tags found in ${PAGE}`); process.exit(2); }
const src = SCRIPTS.map(f => fs.readFileSync(path.join(ROOT, f), 'utf8')).join('\n;\n') + `
;globalThis.__hooks = {
  items: () => ITEMS, closed: () => CLOSED, pf: () => PF,
  totals, computeAsOf, computeAsOfRealized, openDetail, detail: () => DETAIL,
  renderPie, renderMap, renderClosed, renderMeta, renderTrades,
  setMode: m => { MODE = m; applyMode(ITEMS); applyMode(CLOSED); },
  setShowMoney: v => { SHOW_MONEY = v; },
  account: () => ({ divTotal: DIV_TOTAL, taxTotal: TAX_TOTAL, taxSplit: TAX_SPLIT,
                    trades: TRADES.length, bench: BENCH.length, names: NAMES.size,
                    sectors: SECTORS.size, indexed: TRADE_INDEX.size }),
};`;

/* ---------- a DOM just real enough ----------
   The renderers only ever create nodes, set attributes, hang listeners and measure. None of that
   needs layout, so ~50 lines stand in for the browser — and because the listeners are kept, the
   tooltips can be fired and read back. */
const BY_ID = new Map();
class El {
  constructor(tag = 'div', id = '') {
    this.tagName = String(tag).toUpperCase();
    this.id = id; this.children = []; this._on = {}; this.attrs = {}; this.dataset = {};
    this.innerHTML = ''; this.textContent = ''; this.className = '';
    this.hidden = false; this.checked = true; this.disabled = false; this.open = false;
    this.value = ''; this.min = '2019-01-01'; this.max = ''; this.tabIndex = 0; this.files = [];
    this.offsetWidth = 180; this.offsetHeight = 90;
    this.style = { setProperty() {}, removeProperty() {} };
    this.classList = { add() {}, remove() {}, toggle() {}, contains: () => false };
    this.parentElement = BY_ID.get('__wrap') || null;
  }
  setAttribute(k, v) { this.attrs[k] = v; }
  getAttribute(k) { return this.attrs[k] ?? null; }
  appendChild(c) { this.children.push(c); return c; }
  replaceChildren(...c) { this.children = c; }
  addEventListener(t, f) { (this._on[t] = this._on[t] || []).push(f); }
  removeEventListener() {}
  querySelector() { return this._input || (this._input = byId('showMoney')); }
  querySelectorAll() { return []; }
  closest() { return BY_ID.get('__wrap'); }
  getBoundingClientRect() { return { left: 0, top: 0, right: 900, bottom: 560, width: 900, height: 560 }; }
  showModal() { this.open = true; }
  close() { this.open = false; }
  fire(type, ev = { clientX: 400, clientY: 300 }) {
    (this._on[type] || []).forEach(f => f(ev));
    if (typeof this['on' + type] === 'function') this['on' + type](ev);
  }
}
const byId = id => BY_ID.get(id) || (BY_ID.set(id, new El('div', id)), BY_ID.get(id));
byId('__wrap');
const TBODIES = new Map();                                   // document.querySelector('#x tbody')
const tbody = sel => TBODIES.get(sel) || (TBODIES.set(sel, new El('tbody')), TBODIES.get(sel));

const FROZEN = Date.parse('2026-08-22T12:00:00Z');           // IRR's last flow is "today"
class FixedDate extends Date {
  constructor(...a) { super(...(a.length ? a : [FROZEN])); }
  static now() { return FROZEN; }
}

const sandbox = {
  console, Intl, Math, JSON, Promise, Map, Set, Number, String, Array, Object, URLSearchParams,
  setTimeout, clearTimeout, Date: FixedDate,
  innerWidth: 1400, innerHeight: 900,
  location: { search: '' },
  matchMedia: () => ({ matches: false, addEventListener() {} }),
  document: {
    getElementById: byId,
    createElement: t => new El(t),
    createElementNS: (_, t) => new El(t),
    querySelector: tbody,
    querySelectorAll: () => [],
    addEventListener() {},
    documentElement: { dataset: {} },
    body: new El('body'),
  },
  fetch: async p => {
    const f = path.join(ROOT, p);
    return fs.existsSync(f)
      ? { ok: true, status: 200, text: async () => fs.readFileSync(f, 'utf8') }
      : { ok: false, status: 404, text: async () => '' };
  },
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(src, sandbox, { filename: SCRIPTS.join(' + ') });

/* ---------- what gets recorded ---------- */
const r = v => typeof v === 'number'
  ? (Number.isFinite(v) ? Math.round(v * 1e4) / 1e4 : String(v)) : v;
const FIELDS = ['portfolio', 'label', 'identifier', 'shares', 'cur', 'pur', 'purAbs', 'gain', 'ret',
  'rel', 'relPre', 'state', 'cash', 'fund', 'invested', 'split', 'irr', 'irrExact', 'benchIrr',
  'benchAlt', 'alpha', 'divHeld', 'divSold', 'since', 'sinceKnown', 'vsBench', 'heldValue',
  'benchValue', 'soldShares', 'grossProceeds', 'sellCount', 'taxSell', 'share', 'core', 'years'];
const snap = d => Object.fromEntries(FIELDS.map(k => [k, r(d[k])]).concat([['flows', d.flows.length]]));

// hover every node the renderers hung on a position and keep what the tooltip showed
function hoverAll(rows, tag, out) {
  const tip = byId('tip');
  rows.forEach(d => (d.nodes || []).forEach((n, i) => {
    tip.innerHTML = '';
    n.fire('pointerenter');
    if (tip.innerHTML) out[`${tag} ${d.label || d.name} #${i}`] = tip.innerHTML;
    n.fire('pointerleave');
  }));
}

(async () => {
  await new Promise(res => setTimeout(res, 600));            // let the fetch chain settle
  const h = sandbox.__hooks;
  if (!h.items().length) { console.error('no positions loaded — are the CSVs in parqet/ ?'); process.exit(2); }
  const out = {};

  for (const mode of ['abs', 'rel']) {
    h.setMode(mode);
    out[`${mode}/model`] = {
      account: h.account(), pf: h.pf().map(p => p.name),
      totals: h.totals(h.items()), closedTotals: h.totals(h.closed()),
      items: h.items().map(snap), closed: h.closed().map(snap),
    };
    for (const day of ['2026-08-14', '2025-06-16', '2023-03-15', '2021-11-15']) {
      const s = await h.computeAsOf(day), q = await h.computeAsOfRealized(day);
      out[`${mode}/asOf ${day}`] = {
        n: s.items.length, total: r(s.asOfTotal), ratio: r(s.ratio), missing: [...s.missing].sort(),
        items: s.items.map(x => [x.label, r(x.cur), r(x.pur), r(x.ret), r(x.irr)]),
        open: q.open.length, closed: q.closed.length,
        realizedTotal: r(q.realizedTotal), divTotal: r(q.divTotal), taxTotal: r(q.taxTotal),
      };
    }
    for (const money of [true, false]) {
      h.setShowMoney(money);
      const tag = `${mode}/${money ? 'eur' : 'masked'}`;
      const tips = {};
      h.renderMap(h.items(), null);   hoverAll(h.items(), 'map', tips);
      h.renderPie(h.items());         hoverAll(h.items(), 'pie', tips);
      h.renderClosed();               hoverAll([...h.closed(), ...h.items()], 'bar', tips);
      h.renderMeta(h.items(), h.closed());
      const curTip = byId('curTip'); curTip.innerHTML = '';
      byId('tileCur').fire('pointerenter');
      out[`${tag}/chrome`] = {
        tiles: ['tCur', 'tPur', 'tGain', 'tRel', 'tN', 'kPur', 'kGain'].map(id => byId(id).textContent),
        closedNet: byId('closedNet').textContent,
        dataStamp: byId('dataStamp').textContent,
        byPortfolio: curTip.innerHTML,
        legendItems: byId('legend').children.length,
      };
      out[`${tag}/tips`] = tips;
    }
    h.setShowMoney(true);

    // the modal price chart: its own code path, and the only one that reads a position's
    // data_series file for drawing rather than for the as-of replay
    const detail = {};
    for (const d of h.items().slice(0, 6).concat(h.closed().slice(0, 2))) {
      await h.openDetail(d);
      const body = byId('dtBody');
      const svg = body.children.find(c => c.tagName === 'SVG');
      detail[d.label] = {
        title: byId('dtTitle').textContent,
        sub: byId('dtSub').textContent,
        drawn: !!svg,
        label: svg ? svg.getAttribute('aria-label') : body.innerHTML,
        nodes: svg ? svg.children.length : 0,
        anchor: h.detail() && h.detail().alignDate,
      };
    }
    out[`${mode}/detail`] = detail;
  }

  const text = JSON.stringify(out, null, 1);
  if (SAVE || !fs.existsSync(BASELINE)) {
    fs.writeFileSync(BASELINE, text);
    console.log(`baseline written: ${BASELINE} (${(text.length / 1024).toFixed(0)} KB)`);
    return;
  }
  // compared key by key on re-stringified values, so a baseline that has been reformatted (by a
  // pretty-printer, or by hand) still reads as unchanged when nothing actually moved
  const a = JSON.parse(fs.readFileSync(BASELINE, 'utf8'));
  const keys = [...new Set([...Object.keys(a), ...Object.keys(out)])];
  const changed = keys.filter(k => JSON.stringify(a[k]) !== JSON.stringify(out[k]));
  if (!changed.length) { console.log('✓ no change against the baseline'); return; }

  let shown = 0;
  console.log('CHANGED against the baseline:\n');
  for (const k of changed) {
    const x = JSON.stringify(a[k], null, 1), y = JSON.stringify(out[k], null, 1);
    console.log(`  ${k}`);
    if (shown++ < 3) {
      const xl = (x || '').split('\n'), yl = (y || '').split('\n');
      for (let i = 0, n = 0; i < Math.max(xl.length, yl.length) && n < 12; i++) {
        if (xl[i] === yl[i]) continue;
        console.log(`      - ${(xl[i] || '').trim()}\n      + ${(yl[i] || '').trim()}`);
        n++;
      }
    }
  }
  console.log('\nIf every change above is intended, re-run with --save to accept it.');
  process.exit(1);
})();
