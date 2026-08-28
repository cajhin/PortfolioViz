#!/usr/bin/env node
/*
 * Regression check for portfolio.html. No dependencies — plain `node check_portfolio.js`.
 *
 *   node check_portfolio.js --save     record the current output as the baseline
 *   node check_portfolio.js            re-run and diff against that baseline
 *
 * How it works: the scripts portfolio.html loads are concatenated in page order and run in a vm
 * context against a mini DOM defined below and the real CSVs in parqet/ and prices/. Add a
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
  renderPie, renderMap, renderClosed, renderMeta, renderTrades, renderWatch, renderHeaderTotals,
  setMode: m => { MODE = m; applyMode(ITEMS); applyMode(CLOSED); },
  setShowMoney: v => { SHOW_MONEY = v; TRADES_DRAWN_FOR = null; WATCH_DRAWN_FOR = null; },
  // the range pick lives in three globals at once — the two dates and the view they only apply
  // to — so it is set and cleared as one thing rather than three
  setRange: (from, to) => { VIEW = to || from ? 'map' : VIEW; AS_FROM = from; AS_OF = to; },
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
  remove() {}
  focus() {}
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
  // window-level, for the page's uncaught-error reporter
  addEventListener() {}, removeEventListener() {},
  location: { search: '' },
  matchMedia: () => ({ matches: false, addEventListener() {} }),
  document: {
    getElementById: byId,
    createElement: t => new El(t),
    createElementNS: (_, t) => new El(t),
    querySelector: tbody,
    querySelectorAll: () => [],
    addEventListener() {},
    removeEventListener() {},          // the stock picker's close path tears its listener down
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

// hover every node a renderer hung on a position and keep what the tooltip showed. `nodes` is the
// Map that renderer returned — position → its elements *in that chart* — so hovering the map reads
// the map's tiles even for a position the realised bar also drew.
function hoverAll(nodes, rows, tag, out) {
  const tip = byId('tip');
  rows.forEach(d => (nodes.get(d) || []).forEach((n, i) => {
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
    const reconstruct = async (from, day) => {
      const s = await h.computeAsOf(day, from), q = await h.computeAsOfRealized(day, from);
      return {
        n: s.items.length, total: r(s.asOfTotal), ratio: r(s.ratio), missing: [...s.missing].sort(),
        items: s.items.map(x => [x.label, r(x.cur), r(x.pur), r(x.ret), r(x.irr)]),
        open: q.open.length, closed: q.closed.length,
        realizedTotal: r(q.realizedTotal), divTotal: r(q.divTotal), taxTotal: r(q.taxTotal),
      };
    };
    for (const day of ['2026-08-14', '2025-06-16', '2023-03-15', '2021-11-15']) {
      out[`${mode}/asOf ${day}`] = await reconstruct(null, day);
    }
    // the same reconstruction bounded at both ends: every basis figure re-based onto `from`, and
    // only the sales booked inside the window counted as realised
    const RANGES = [['2025-01-02', '2026-08-14'], ['2023-03-15', '2025-06-16'],
                    ['2021-11-15', '2023-03-15']];
    for (const [from, day] of RANGES) {
      out[`${mode}/range ${from}..${day}`] = await reconstruct(from, day);
    }
    for (const money of [true, false]) {
      h.setShowMoney(money);
      const tag = `${mode}/${money ? 'eur' : 'masked'}`;
      const tips = {};
      hoverAll(h.renderMap(h.items(), null), h.items(), 'map', tips);
      hoverAll(h.renderPie(h.items()), h.items(), 'pie', tips);
      hoverAll(h.renderClosed(), [...h.closed(), ...h.items()], 'bar', tips);
      h.renderMeta(h.items(), h.closed());
      const curTip = byId('curTip'); curTip.innerHTML = '';
      byId('tileCur').fire('pointerenter');
      out[`${tag}/chrome`] = {
        tiles: ['tCur', 'tPur', 'tGain', 'tRel', 'tN', 'kCur', 'kPur', 'kGain'].map(id => byId(id).textContent),
        closedNet: byId('closedNet').textContent,
        dataStamp: byId('dataStamp').textContent,
        byPortfolio: curTip.innerHTML,
        legendItems: byId('legend').children.length,
      };
      out[`${tag}/tips`] = tips;

      // the trades table: its own render path, and the only place a trade is measured against
      // today's price (Now %) rather than against a position's cost
      h.renderTrades();
      const trRows = tbody('#tblTrades tbody').children;
      // the watchlist: registry instruments with no holding behind them, and the only view whose
      // "last close" comes from prices/_latest.csv rather than from a position
      h.renderWatch();
      const wRows = tbody('#tblWatch tbody').children;
      out[`${tag}/watch`] = {
        n: wRows.length,
        head: byId('watchHead').textContent,
        rows: wRows.map(tr => tr.innerHTML.replace(/style="[^"]*"/g, 'style=…')),
      };

      out[`${tag}/trades`] = {
        n: trRows.length,
        head: byId('tradesHead').textContent,
        first: trRows.slice(0, 12).map(tr => tr.innerHTML),
      };
    }
    h.setShowMoney(true);

    // the chrome a range pick rewrites: the tile keys and the tooltip rows that say what a basis
    // figure is measured from, plus the note under the map that spells the window out
    {
      const [from, day] = RANGES[0];
      h.setRange(from, day);
      const s = await h.computeAsOf(day, from), q = await h.computeAsOfRealized(day, from);
      const tips = {};
      hoverAll(h.renderMap(s.items, { ...s, date: day }), s.items, 'map', tips);
      h.renderHeaderTotals(s.items, [], { realizedTotal: q.realizedTotal });
      h.renderClosed({ open: q.open, closed: q.closed, divTotal: q.divTotal,
                       divRows: q.divRows, taxTotal: q.taxTotal, taxSplit: q.taxSplit });
      out[`${mode}/range chrome`] = {
        tiles: ['tCur', 'tPur', 'tGain', 'tRel', 'tN', 'kCur', 'kPur', 'kGain', 'kRel'].map(id => byId(id).textContent),
        note: byId('asOfNote').innerHTML,
        closedNet: byId('closedNet').textContent,
        dataStamp: byId('dataStamp').textContent,
        tips,
      };
      h.setRange(null, null);
    }

    // the modal price chart: its own code path, and the only one that reads a position's
    // prices file for drawing rather than for the as-of replay
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

    // the detail chart's timeframe buttons, driven through the real click handler — they share
    // RANGE_PRESETS with the map's range picker, so a button added there must land here too
    {
      const d0 = h.items()[0];
      await h.openDetail(d0);
      const spans = {};
      for (const range of ['all', '5y', '3y', '1y', 'ytd', '6m', '3m', '1m', '1w', '1d', 'buy']) {
        byId('dtRange').fire('click', { target: { closest: () => ({ dataset: { range } }) } });
        await new Promise(res => setTimeout(res, 30));
        const svg = byId('dtBody').children.find(c => c.tagName === 'SVG');
        spans[range] = svg ? svg.getAttribute('aria-label').replace(/^.*?, /, '') : 'not drawn';
      }
      out[`${mode}/detail ranges`] = { position: d0.label, spans };
    }

    // "[All]" in the + compare picker: driven through the real control, since the whole point of
    // it is the click path — open the picker, find the row, click it, and see what the chart is
    // left holding. The aria-label names every line, so it is the record of what got drawn.
    {
      await h.openDetail(h.items()[0]);
      byId('dtAddCompare').fire('click');
      const menu = byId('dtBody').children.find(c => c.className === 'dtpick');
      const allRow = menu && menu.children.find(r => r.textContent === '[All]');
      if (allRow) allRow.fire('click');
      await new Promise(res => setTimeout(res, 400));      // the onPick handler is async
      const svg = byId('dtBody').children.find(c => c.tagName === 'SVG');
      out[`${mode}/compare all`] = {
        offered: !!allRow,
        extras: h.detail() ? h.detail().extras.length : 0,
        lines: svg ? svg.getAttribute('aria-label') : null,
        nodes: svg ? svg.children.length : 0,
      };
    }
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
