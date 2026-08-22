/* =============================================================================================
   portfolio.view.js — everything that reads or writes the page.

   Runs after portfolio.model.js and reads its state and helpers directly (one shared global
   scope; no modules). The model never calls into here — the arrow points one way.

   Sections, in order:

     formatting           money and percent formatters, and the "Show €" mask
     geometry             SVG element helper, polar points, pie wedge paths
     pie                  renderPie — area-true wedges, one per position
     shared chrome        the tooltip fragments both charts use, and tooltip placement
     tables + headline    the positions/closed/trades tables and the stat tiles
     map                  the sector treemap, its colour ramp, and the realised bar under it
     detail overlay       the per-position price chart in the modal
     views + controls     show(), the segmented control, the as-of date picker, the key handler
     startup              MODE sync, load(), and the fetch that starts it all

   AREA IS VALUE everywhere it appears — the pie's radii, the map's tiles, the realised bar's
   widths. When the as-of map shrinks, it shrinks by the square root of the value ratio for the
   same reason. Keep that true of anything new — and note that chrome costs area: any gap, inset
   or label strip you carve out of a tile has to be paid for out of the layout (squarifyNet), not
   out of the tile, or the smallest positions quietly stop being drawn to scale.
   ============================================================================================= */

/* ---------- formatting ---------- */
let SHOW_MONEY = true;              // the "Show €" switch; off replaces every amount with xxxx

// Intl.NumberFormat is expensive to construct and these run once per table cell and tooltip
// row — build each shape once per currency and hand the same formatter back every time.
const FMT_SHAPES = {
  money0:  () => ({ style: 'currency', currency: CCY, maximumFractionDigits: 0 }),
  money2:  () => ({ style: 'currency', currency: CCY, minimumFractionDigits: 2, maximumFractionDigits: 2 }),
  plain2:  () => ({ minimumFractionDigits: 2, maximumFractionDigits: 2 }),
  percent: () => ({ style: 'percent', minimumFractionDigits: 1 }),
};
const FMT_CACHE = new Map();
function fmt(shape) {
  const key = CCY + ':' + shape;
  let f = FMT_CACHE.get(key);
  if (!f) FMT_CACHE.set(key, f = new Intl.NumberFormat('de-DE', FMT_SHAPES[shape]()));
  return f;
}
const ccySymbol = () => fmt('money0').formatToParts(0).find(part => part.type === 'currency').value;
const masked = () => `xxxx ${ccySymbol()}`;
const fmtMoney = v => SHOW_MONEY ? fmt('money0').format(v) : masked();
const fmtMoney2 = v => SHOW_MONEY ? fmt('money2').format(v) : masked();
const fmtMoneyPre = v => SHOW_MONEY ? ccySymbol() + fmt('plain2').format(v) : masked();
const fmtShare = v => fmt('percent').format(v);
const fmtPct = v => (v >= 0 ? '+' : '') + v.toFixed(1) + '%';
const fmtPP = v => (v >= 0 ? '+' : '') + v.toFixed(1) + ' pp';

/* ---------- geometry ---------- */
const TAU = Math.PI * 2;
const pt = (cx, cy, r, a) => [cx + r * Math.cos(a - Math.PI / 2), cy + r * Math.sin(a - Math.PI / 2)];
function wedge(cx, cy, r0, r1, a0, a1) {
  const large = a1 - a0 > Math.PI ? 1 : 0;
  const [x1, y1] = pt(cx, cy, r1, a0), [x2, y2] = pt(cx, cy, r1, a1);
  if (r0 <= 0.01) return `M ${cx} ${cy} L ${x1} ${y1} A ${r1} ${r1} 0 ${large} 1 ${x2} ${y2} Z`;
  const [x3, y3] = pt(cx, cy, r0, a1), [x4, y4] = pt(cx, cy, r0, a0);
  return `M ${x1} ${y1} A ${r1} ${r1} 0 ${large} 1 ${x2} ${y2} L ${x3} ${y3} A ${r0} ${r0} 0 ${large} 0 ${x4} ${y4} Z`;
}
const el = (n, attrs) => {
  const e = document.createElementNS('http://www.w3.org/2000/svg', n);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  return e;
};

/* ---------- pie ---------- */
const LABEL_MIN_SHARE = 0.003;   // label wedges worth at least 0.3% of the total…
const LABEL_GAP = 14;            // …as long as they still fit this far apart

function renderPie(items) {
  const svg = document.getElementById('pie');
  const H = 560, cx = 450, cy = 280;
  const total = items.reduce((s, d) => s + d.cur, 0);

  // R is the radius whose wedge area equals a position's CURRENT value (angle is proportional to it).
  // A loser's red band reaches past R, out to the radius that is area-true to its purchase value.
  const ratioOut = d => d.state === 'loss' ? Math.sqrt(d.pur / d.cur) : 1;
  const ratioEnd = d => Math.sqrt(ratioOut(d) ** 2 + Math.abs(d.rel) / d.cur);
  const grow = Math.max(1, ...items.map(ratioEnd));
  const R = Math.min(190, 228 / grow);
  const GAP = 0.75 / R;                              // ~0.75px surface gap between wedges

  let a = 0;
  items.forEach(d => {
    d.a0 = a; a += d.cur / total * TAU; d.a1 = a;
    d.mid = (d.a0 + d.a1) / 2;
    const rPur = R * Math.sqrt(d.pur / d.cur);       // area-true radius for the purchase value
    d.rIn  = d.state === 'gain' ? rPur : (d.state === 'loss' ? R : 0);
    d.rOut = d.state === 'loss' ? rPur : R;
    // realized gain/loss: an area-true band outside everything else
    d.rRel = Math.abs(d.rel) > 0.005 ? Math.sqrt(d.rOut ** 2 + Math.abs(d.rel) * R ** 2 / d.cur) : d.rOut;
  });

  const g = el('g', {});
  const labels = el('g', {});

  items.forEach(d => {
    const a0 = d.a0 + GAP / 2, a1 = Math.max(d.a1 - GAP / 2, d.a0 + GAP / 2 + 0.0005);
    const stroke = { stroke: 'var(--surface-1)', 'stroke-width': 0.75, 'stroke-linejoin': 'round' };
    d.nodes = [];

    // outer band — the gain (inside the rim) or the loss (protruding past it)
    if (d.rOut - d.rIn > 0.5) {
      const outer = el('path', {
        d: wedge(cx, cy, d.rIn, d.rOut, a0, a1),
        fill: d.state === 'loss' ? 'var(--loss-light)' : 'var(--gain-light)', ...stroke,
      });
      g.appendChild(outer); d.nodes.push(outer);
    }
    // inner disc — purchase value (gainers) or current value (losers and cash)
    const inner = el('path', {
      d: wedge(cx, cy, 0, Math.max(d.rIn, 0.5), a0, a1),
      fill: d.core, ...stroke,
    });
    g.appendChild(inner); d.nodes.push(inner);

    // realized result — outermost band, green when positive, hatched red when negative
    if (d.rRel - d.rOut > 0.5) {
      const rel = el('path', {
        d: wedge(cx, cy, d.rOut, d.rRel, a0, a1),
        fill: d.rel > 0 ? 'var(--realized)' : 'var(--loss-deep)', ...stroke,
      });
      g.appendChild(rel); d.nodes.push(rel);
    }
  });

  // outside labels for the significant wedges, de-collided per side
  const right = [], left = [];
  items.filter(d => d.cur / total >= LABEL_MIN_SHARE).forEach(d => {
    d.rLead = Math.max(R, d.rOut, d.rRel);
    const [, ey] = pt(cx, cy, R + 14, d.mid);
    (Math.cos(d.mid - Math.PI / 2) >= 0 ? right : left).push({ d, ey });
  });
  // keep the largest wedges when a side has more labels than the column can hold
  const capacity = Math.floor((H - 40) / LABEL_GAP);
  [right, left].forEach(arr => {
    if (arr.length > capacity) {
      const keep = new Set([...arr].sort((p, q) => q.d.cur - p.d.cur).slice(0, capacity).map(p => p.d));
      arr.splice(0, arr.length, ...arr.filter(p => keep.has(p.d)));
    }
  });
  const place = (arr, dir) => {
    arr.sort((p, q) => p.ey - q.ey);
    const minGap = LABEL_GAP;
    for (let i = 1; i < arr.length; i++)
      if (arr[i].ey - arr[i - 1].ey < minGap) arr[i].ey = arr[i - 1].ey + minGap;
    const overflow = arr.length ? arr[arr.length - 1].ey - (H - 20) : 0;
    if (overflow > 0) arr.forEach(p => p.ey -= overflow);
    const top = arr.length ? 20 - arr[0].ey : 0;
    if (top > 0) arr.forEach(p => p.ey += top);
    arr.forEach(p => {
      const tx = dir > 0 ? cx + 238 : cx - 238;
      const [sx, sy] = pt(cx, cy, p.d.rLead + 2, p.d.mid);
      const [bx, by] = pt(cx, cy, p.d.rLead + 16, p.d.mid);
      labels.appendChild(el('path', { d: `M ${sx} ${sy} L ${bx} ${by} L ${tx} ${p.ey}`, class: 'leader' }));
      const anchor = dir > 0 ? 'start' : 'end';
      const t1 = el('text', { x: tx + (dir > 0 ? 6 : -6), y: p.ey + 3, class: 'lbl', 'text-anchor': anchor });
      t1.textContent = p.d.label.length > 34 ? p.d.label.slice(0, 33) + '…' : p.d.label;
      labels.appendChild(t1);
    });
  };
  place(right, 1); place(left, -1);

  // portfolio boundaries — a thin radial line where one portfolio's arc meets the next
  const dividers = el('g', {
    stroke: 'var(--rim)', 'stroke-width': 0.8, 'stroke-linecap': 'round', 'pointer-events': 'none',
  });
  items.forEach((d, i) => {
    const prev = items[(i - 1 + items.length) % items.length];
    if (prev.portfolio === d.portfolio) return;
    const rEnd = Math.max(R, d.rRel, prev.rRel) + 3;
    const [x, y] = pt(cx, cy, rEnd, d.a0);
    dividers.appendChild(el('line', { x1: cx, y1: cy, x2: x, y2: y }));
  });

  // the rim itself — the radius at which a wedge's area is its current value
  const rim = el('circle', {
    cx, cy, r: R, fill: 'none', stroke: 'var(--rim)', 'stroke-width': 0.8,
    'pointer-events': 'none',
  });

  svg.setAttribute('aria-label', 'Pie of all positions; the band inside the rim is the gain, ' +
    'a band beyond it a loss, and the outermost bands realised results');
  svg.replaceChildren(g, dividers, rim, labels);

  attachTip(items);

  legendPie();
}

/* ---------- shared chrome: tooltip fragments and placement ----------
   The map/pie tiles and the realised bar describe the same positions from two angles, so the
   rows they have in common are built here once. Each returns HTML, or '' when the fact doesn't
   apply to this position — callers just concatenate and never test for themselves. */
const posNeg = v => v >= 0 ? 'pos' : 'neg';
const tipRow = (label, value, cls) =>
  `<div class="r"><span>${label}</span><b${cls ? ` class="${cls}"` : ''}>${value}</b></div>`;
const tipHead = (d, withPrice) =>
  `<div class="t">${d.label || d.name}` +
  (withPrice && d.lastPrice > 0 ? ` <span class="quote">${fmtMoneyPre(d.lastPrice)}` +
    `${d.lastPriceDate ? ' · ' + d.lastPriceDate : ''}</span>` : '') + '</div>' +
  (d.label && d.label !== d.name ? `<div class="full">${d.name}</div>` : '');
// when the position was sold out of, and at what price
const tipSaleRows = d =>
  (d.soldShares > 0 && d.lastSell
    ? tipRow('Sold at', d.lastSell.slice(0, 10) +
        (d.sellCount > 1 ? ` (last of ${d.sellCount})` : '')) : '') +
  (d.sellPrice > 0
    ? tipRow('Sell price', fmtMoney2(d.sellPrice) +
        (d.split !== 1 ? ` → ${fmtMoney2(d.sellPrice / d.split)} split-adj.` : '')) : '');
// in vs.-World mode the position's own IRR only means something next to the index's
const tipIrrRow = (d, cagrLabel) => {
  if (!Number.isFinite(d.irr)) return '';
  if (MODE === 'rel' && Number.isFinite(d.benchIrr))
    return tipRow('XIRR vs. World', fmtPP(d.irr - d.benchIrr), posNeg(d.irr - d.benchIrr));
  return tipRow(d.irrExact ? 'XIRR' : cagrLabel, fmtPct(d.irr), posNeg(d.irr));
};

// Keep a popover inside the viewport: prefer down-right of the pointer, flip to the other side
// when it would overflow, and clamp so it can never render past an edge.
function placeTip(tip, wrapEl, e) {
  const r = wrapEl.getBoundingClientRect();
  const w = tip.offsetWidth, h = tip.offsetHeight, PAD = 8, OFF = 14;
  let x = e.clientX + OFF;
  if (x + w > innerWidth - PAD) x = e.clientX - OFF - w;
  x = Math.min(Math.max(x, PAD), Math.max(PAD, innerWidth - w - PAD));
  let y = e.clientY + OFF;
  if (y + h > innerHeight - PAD) y = e.clientY - OFF - h;
  y = Math.min(Math.max(y, PAD), Math.max(PAD, innerHeight - h - PAD));
  tip.style.left = (x - r.left) + 'px';
  tip.style.top = (y - r.top) + 'px';
}

function attachTip(items) {
  const svg = document.getElementById('pie');
  const tip = document.getElementById('tip');
  // #tip lives inside #chartBody, but #chartBody isn't positioned — its containing block is
  // really .chartwrap, one level up, past the whole chart-bar header row. Using #chartBody's own
  // rect here (as this did) discounts that header's height from every placeTip() calculation,
  // rendering the tooltip that much too high — close enough to slide over the cursor that started
  // it. .closest('.chartwrap') is the same fix renderClosed() already needed for this same tip.
  const wrapEl = svg.closest('.chartwrap');
  items.forEach(d => d.nodes.forEach(n => {
    n.style.cursor = 'default';
    n.addEventListener('pointerenter', e => {
      tip.innerHTML =
        tipHead(d, !d.cash) +
        `<div class="pf">${d.portfolio}</div>` +
        tipRow('Share', fmtShare(d.share)) +
        (d.cash ? '' :
          tipRow(MODE === 'rel' ? `Same money in ${BENCH_LABEL}` : 'Purchase value', fmtMoney2(d.pur))) +
        tipRow('Current value', fmtMoney2(d.cur)) +
        (d.divHeld > 0 ? tipRow('Dividends', fmtMoney2(d.divHeld), 'income') : '') +
        (d.state === 'flat' ? '' :
          tipRow(MODE === 'rel' ? 'Ahead by' : 'Unrealised',
                 `${fmtMoney2(d.gain)} (${fmtPct(d.ret)})`, posNeg(d.gain))) +
        tipIrrRow(d, 'Annualised (CAGR)') +
        (Math.abs(d.relPre) > 0.005
          ? tipSaleRows(d) +
            tipRow('Realised (pre-tax)', fmtMoney2(d.relPre), d.relPre >= 0 ? 'realized' : 'neg')
          : '');
      tip.classList.add('on');
      placeTip(tip, wrapEl, e);
      items.forEach(o => o.nodes.forEach(m => m.style.opacity = o === d ? 1 : 0.35));
    });
    n.addEventListener('pointermove', e => placeTip(tip, wrapEl, e));
    n.addEventListener('pointerleave', () => {
      tip.classList.remove('on');
      items.forEach(o => o.nodes.forEach(m => m.style.opacity = 1));
    });
  }));
}

function legendPie() {
  const legend = document.getElementById('legend');
  const ramp = text => {
    const div = document.createElement('div'); div.className = 'item';
    div.innerHTML = `<span class="swatch" style="background:linear-gradient(90deg,` +
      `hsl(${HUE_FROM}deg var(--core-s) var(--core-l)),hsl(${HUE_TO}deg var(--core-s) var(--core-l)))"></span>` +
      `<span>${text}</span>`;
    return div;
  };
  const mk = (bg, core, text) => {
    const div = document.createElement('div'); div.className = 'item';
    div.innerHTML = `<span class="swatch" style="background:${bg}"><i style="background:${core}"></i></span><span>${text}</span>`;
    return div;
  };
  legend.replaceChildren(
    ramp('Core = invested, hue by name (aa → zz)'),
    mk('var(--gain-light)', 'var(--gain-light)', 'Unrealised gain — light green band inside the rim'),
    mk('var(--loss-light)', 'var(--loss-light)', 'Unrealised loss — light red band beyond the rim'),
    mk('var(--realized)', 'var(--realized)', 'Realised gain — green band outside'),
    mk('var(--loss-deep)', 'var(--loss-deep)', 'Realised loss — dark red band outside'),
    mk('var(--flat)', 'var(--flat)', 'Cash — flat'),
    mk('var(--rim)', 'var(--rim)', 'Black radial line — portfolio boundary'),
  );
}

/* ---------- table + headline figures ---------- */
// Where a position's prices actually come from, compact enough for a table cell: the provider's
// initial and its own symbol, e.g. "Y-HY9H.F". The exchange suffix is kept — two listings of one
// instrument differ only by it (XNAS.DE vs IE00BMFKG444.SG), and that is exactly what this column
// exists to disambiguate. Cash, and anything with no quotable source, gets a dash.
const SOURCE_LETTER = { yahoo: 'Y', manual: 'M' };
function sourceTag(d) {
  const s = SOURCES.get(d.identifier);
  if (!s) return '–';
  const letter = SOURCE_LETTER[s.source] || (s.source || '?').slice(0, 1).toUpperCase();
  return s.symbol ? `${letter}-${s.symbol}` : letter;
}
function renderMeta(items, closed = []) {
  const tb = document.querySelector('#tbl tbody');
  const trs = [];
  PF.forEach(p => {
    const rows = items.filter(d => d.portfolio === p.name);
    rows.forEach(d => {
      const cls = d.state === 'flat' ? '' : d.gain > 0 ? 'pos' : 'neg';
      const tr = document.createElement('tr');
      tr.innerHTML =
        `<td>${p.name}</td>` +
        `<td class="src">${sourceTag(d)}</td>` +
        `<td class="posname"><span class="dot" style="background:${d.core}"></span>${d.label}</td>` +
        `<td>${d.shares.toLocaleString('de-DE')}</td>` +
        `<td>${d.cash ? '–' : fmtMoney2(d.pur)}</td><td>${fmtMoney2(d.cur)}</td>` +
        `<td class="${cls}">${d.state === 'flat' ? '–' : fmtMoney2(d.gain)}</td>` +
        `<td class="${cls}">${d.state === 'flat' ? '–' : fmtPct(d.ret)}</td>` +
        `<td class="${d.relPre > 0 ? 'realized' : d.relPre < 0 ? 'neg' : ''}">` +
        `${Math.abs(d.relPre) > 0.005 ? fmtMoney2(d.relPre) : '–'}</td>`;
      // cash has no price series, so nothing to chart — every real position opens it on a click
      // scoped to its own name cell, not the whole row, so the surrounding figures stay plain text
      if (!d.cash) tr.querySelector('.posname').addEventListener('click', () => openDetail(d));
      trs.push(tr);
    });
    const s = totals(rows);
    const tr = document.createElement('tr'); tr.className = 'sub';
    tr.innerHTML = `<td colspan="4">${p.name} — total</td>` +
      `<td>${fmtMoney2(s.pur)}</td><td>${fmtMoney2(s.cur)}</td>` +
      `<td class="${s.gain >= 0 ? 'pos' : 'neg'}">${fmtMoney2(s.gain)}</td>` +
      `<td class="${s.gain >= 0 ? 'pos' : 'neg'}">${s.pur > 0 ? fmtPct(s.gain / s.pur * 100) : '–'}</td>` +
      `<td class="${s.rel > 0 ? 'realized' : s.rel < 0 ? 'neg' : ''}">${fmtMoney2(s.rel)}</td>`;
    trs.push(tr);
  });
  tb.replaceChildren(...trs);
  renderClosedPositions();

  renderHeaderTotals(ITEMS, CLOSED);

  document.getElementById('loader').hidden = true;
  document.getElementById('app').hidden = false;
}

// A flat list — deliberately not grouped by portfolio like the open-positions table above, since
// a closed position's own portfolio is already a column and there's nothing to subtotal that the
// realised bar doesn't already show. CLOSED is pre-sorted by realised gain (build()), kept as-is.
function renderClosedPositions() {
  const tb = document.querySelector('#tblClosedPositions tbody');
  tb.replaceChildren(...CLOSED.map(d => {
    const ret = d.invested > 0 ? d.relPre / d.invested * 100 : NaN;
    const tr = document.createElement('tr');
    tr.innerHTML =
      `<td>${d.portfolio}</td>` +
      `<td class="src">${sourceTag(d)}</td>` +
      `<td class="posname"><span class="dot" style="background:${d.core}"></span>${d.label}</td>` +
      `<td>${fmtMoney2(d.invested)}</td>` +
      `<td class="${Number.isFinite(ret) ? (ret >= 0 ? 'pos' : 'neg') : ''}">${Number.isFinite(ret) ? fmtPct(ret) : '–'}</td>` +
      `<td class="${d.relPre >= 0 ? 'realized' : 'neg'}">${fmtMoney2(d.relPre)}</td>` +
      `<td class="${Number.isFinite(d.irr) ? (d.irr >= 0 ? 'pos' : 'neg') : ''}">${Number.isFinite(d.irr) ? fmtPct(d.irr) : '–'}</td>`;
    tr.querySelector('.posname').addEventListener('click', () => openDetail(d));
    return tr;
  }));
}

const TYPE_LABEL = { buy: 'Buy', sell: 'Sell', dividend: 'Dividend', fees_taxes: 'Fees/Taxes' };
// Built against the raw trade log, which never changes after load() — so an as-of pick and the
// MODE toggle have nothing to keep in sync here, and the table is drawn once. "Show €" is the
// one exception: it decides whether the amounts are masked, so it is what the guard tracks.
let TRADES_DRAWN_FOR = null;
function renderTrades() {
  if (TRADES_DRAWN_FOR === SHOW_MONEY) return;
  TRADES_DRAWN_FOR = SHOW_MONEY;
  const rows = [...TRADES].sort((a, b) => a.datetime < b.datetime ? 1 : -1);   // newest first
  const feesTotal = TRADES.reduce((t, r) => t + num(r.fee), 0);
  document.getElementById('tradesHead').textContent =
    `${rows.length} Trades (${fmtMoney(feesTotal)} Fees)`;
  const tb = document.querySelector('#tblTrades tbody');
  tb.replaceChildren(...rows.map(t => {
    const tr = document.createElement('tr');
    const cls = t.type === 'sell' ? 'pos' : t.type === 'dividend' ? 'income' : '';
    tr.innerHTML =
      `<td>${t.datetime.slice(0, 10)}</td><td>${t.portfolio}</td>` +
      `<td>${NAMES.get(t.name) || tidyName(t.name)}</td>` +
      `<td>${TYPE_LABEL[t.type] || t.type}</td>` +
      `<td>${num(t.shares) ? num(t.shares).toLocaleString('de-DE') : '–'}</td>` +
      `<td>${num(t.price) ? fmtMoney2(num(t.price)) : '–'}</td>` +
      `<td class="${cls}">${fmtMoney2(num(t.amount))}</td>` +
      `<td class="${cls}">${fmtMoney2(num(t.amountNet))}</td>` +
      `<td>${num(t.fee) > 0.005 ? fmtMoney2(num(t.fee)) : '–'}</td>` +
      `<td>${num(t.tax) > 0.005 ? fmtMoney2(num(t.tax)) : '–'}</td>`;
    return tr;
  }));
}

// Every registry instrument with no matching holding, open or closed — the point of a row here
// with nothing to trade against it. INSTRUMENTS carries each row under both its id and its ISIN
// (equal, for a security), so de-dupe by object identity before filtering, not the map's own size.
let WATCH_DRAWN_FOR = null;
function renderWatch() {
  if (WATCH_DRAWN_FOR === SHOW_MONEY) return;
  WATCH_DRAWN_FOR = SHOW_MONEY;
  const held = new Set([...ITEMS, ...CLOSED].filter(d => !d.cash).map(d => d.identifier));
  const rows = [...new Set(INSTRUMENTS.values())]
    .filter(inst => inst.type !== 'cash' && !held.has(inst.id) && !held.has(inst.isin))
    .sort((a, b) => (a.display || a.name).localeCompare(b.display || b.name));
  document.getElementById('watchHead').textContent =
    `${rows.length} tracked, never held`;
  const tb = document.querySelector('#tblWatch tbody');
  tb.replaceChildren(...rows.map(inst => {
    // enough of a position-shaped object for openDetail()/sourceTag() to work on: no trades will
    // ever match tradeKey(d), so the value bar is correctly all-zero rather than wrong
    const d = { identifier: inst.id, name: inst.name, label: inst.display || inst.name,
               portfolio: 'Watchlist', cash: false };
    const last = PRICES.get(inst.id);
    const tr = document.createElement('tr');
    tr.style.cursor = 'pointer';
    tr.addEventListener('click', () => openDetail(d));
    tr.innerHTML =
      `<td class="src">${sourceTag(d)}</td>` +
      `<td><span class="dot" style="background:${nameColor(d.label)}"></span>${d.label}</td>` +
      `<td>${inst.sector || '–'}</td>` +
      `<td>${last ? fmtMoney2(last.price) : '–'}</td>`;
    return tr;
  }));
}

// The stat-tile row + per-portfolio breakdown, split out of renderMeta so an as-of pick can
// refresh just this part with reconstructed figures without touching the (always-live) table.
// `opts.realizedTotal`, when given, replaces the live rel/relPre sum for the "Realised pre-tax"
// tile — as-of items carry no relPre of their own, that figure comes from computeAsOfRealized.
function renderHeaderTotals(items, closed = [], opts = {}) {
  document.getElementById('kPur').textContent =
    MODE === 'rel' ? `Same money in ${BENCH_LABEL}` : 'Invested (ex cash)';
  document.getElementById('kGain').textContent =
    MODE === 'rel' ? `Ahead of ${BENCH_LABEL}` : 'Unrealised gain';
  const all = totals(items);
  document.getElementById('tCur').textContent = fmtMoney2(all.cur);
  document.getElementById('tPur').textContent = fmtMoney2(all.pur);
  const gEl = document.getElementById('tGain');
  gEl.textContent = fmtMoney2(all.gain) + (all.pur > 0 ? ' (' + fmtPct(all.gain / all.pur * 100) + ')' : '');
  gEl.className = 'v ' + (all.gain >= 0 ? 'pos' : 'neg');
  const rel = Number.isFinite(opts.realizedTotal)
    ? opts.realizedTotal
    : all.rel + closed.reduce((t, d) => t + (d.relPre ?? d.rel), 0);
  const rEl = document.getElementById('tRel');
  rEl.textContent = fmtMoney2(rel);
  rEl.className = 'v ' + (rel >= 0 ? 'realized' : 'neg');
  document.getElementById('tN').textContent = items.filter(d => !d.cash).length;

  // freshness always comes from the live data, never from an as-of pick's synthetic date
  const asOf = ITEMS.reduce((t, d) => d.lastPriceDate > t ? d.lastPriceDate : t, '');
  const deDate = s => new Date(s).toLocaleDateString('de-DE', { day: '2-digit', month: '2-digit', year: 'numeric' });
  const toggle = document.createElement('label');
  toggle.className = 'modeswitch';
  toggle.innerHTML = `<input type="checkbox" id="showMoney"${SHOW_MONEY ? ' checked' : ''}><span>Show €</span>`;
  toggle.querySelector('input').addEventListener('change', e => {
    SHOW_MONEY = e.target.checked;
    redrawEverything();
  });
  attachCurTip(items);
  document.getElementById('showEuroSlot').replaceChildren(toggle);

  const stamp = document.getElementById('dataStamp');
  if (opts.asOfDateStr) {
    stamp.textContent = `Showing ${deDate(opts.asOfDateStr)}` +
      `${asOf ? ' · latest data ' + deDate(asOf) : ''}`;
  } else {
    stamp.textContent = asOf ? `Last data update: ${deDate(asOf)}` : '';
  }
}

// The per-portfolio breakdown, now a hover popup over the Current value tile instead of its own
// always-visible row — reuses the same #tip element and placeTip() the map/bar tooltips use.
// Reassigning .onpointer* (rather than addEventListener) is deliberate: this tile is a static
// DOM node re-rendered against fresh `items` on every header refresh, so a plain addEventListener
// would stack a new listener — closing over stale `items` — on every as-of pick.
function attachCurTip(items) {
  const tile = document.getElementById('tileCur');
  // its own tip node, a child of totalsCard — #tip lives inside .chartwrap instead, so
  // placeTip()'s math (relative to totalsCard) and the browser's actual containing block
  // (whichever positioned ancestor #tip is really inside) would disagree and the popup would
  // land wherever chartwrap happens to sit on the page, not near the cursor
  const tip = document.getElementById('curTip');
  const wrapEl = document.getElementById('totalsCard');
  const rows = PF.map(p => totals(items.filter(d => d.portfolio === p.name)))
    .map((s, i) => ({ name: PF[i].name, cur: s.cur, gain: s.gain, pur: s.pur }));
  tile.onpointerenter = e => {
    tip.innerHTML = `<div class="t">Current value by portfolio</div>` +
      rows.map(r => `<div class="r"><span>${r.name}</span>` +
        `<b class="${r.gain >= 0 ? 'pos' : 'neg'}">${fmtMoney(r.cur)}` +
        `${r.pur > 0 ? ' · ' + fmtPct(r.gain / r.pur * 100) : ''}</b></div>`).join('');
    tip.classList.add('on');
    placeTip(tip, wrapEl, e);
  };
  tile.onpointermove = e => placeTip(tip, wrapEl, e);
  tile.onpointerleave = () => tip.classList.remove('on');
}

// Every figure on the page, redrawn. show() alone is not enough: it only touches the view that
// is currently on screen, while the two tables are drawn once and left alone (their numbers
// depend on neither the view nor the as-of date). A change that reaches all of them — flipping
// "Show €" or the vs.-World mode — has to come through here or the hidden frames keep showing
// the old formatting until something else happens to rebuild them.
function redrawEverything() {
  TRADES_DRAWN_FOR = null;
  WATCH_DRAWN_FOR = null;
  renderMeta(ITEMS, CLOSED);
  show(VIEW);
}

// Picks live vs. as-of totals for the header tiles and the realised bar together, so the two
// never disagree about which date they're showing. The pie view has no as-of rendering of its
// own, so it always falls back to live totals even if a date is still picked underneath.
async function refreshHeader() {
  if (AS_OF && VIEW === 'map') {
    const [snap, real] = await Promise.all([computeAsOf(AS_OF), computeAsOfRealized(AS_OF)]);
    renderHeaderTotals(snap.items, [], { realizedTotal: real.realizedTotal, asOfDateStr: AS_OF });
    renderClosed({ open: real.open, closed: real.closed, divTotal: real.divTotal,
                   divRows: real.divRows, taxTotal: real.taxTotal, taxSplit: real.taxSplit });
  } else {
    renderHeaderTotals(ITEMS, CLOSED);
    renderClosed();
  }
}

/* ---------- map (treemap) ---------- */
const isDark = () => {
  const t = document.documentElement.dataset.theme;
  return t ? t === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches;
};
// three stops per direction: the tile body walks zero → mid, the bar at its foot mid → full
const MAP_STOPS = {
  light: {
    zero: [201, 200, 193],
    up:   { mid: [111, 191, 135], full: [ 16, 102,  45] },
    down: { mid: [217, 141, 140], full: [161,  31,  31] },
  },
  dark: {
    zero: [74, 73, 68],
    up:   { mid: [ 43, 122,  65], full: [ 70, 192, 106] },
    down: { mid: [163,  80,  80], full: [224, 103, 103] },
  },
};
// gradeColor runs once per tile, per foot bar and 21 times for the legend ramp; re-reading the
// media query each time was the bulk of that work. show() clears this so a theme flip re-reads.
let STOPS = null;
const mapStops = () => STOPS || (STOPS = MAP_STOPS[isDark() ? 'dark' : 'light']);
const rgb = a => `rgb(${a.map(Math.round).join(',')})`;
const lerp = (a, b, t) => a.map((v, i) => v + (b[i] - v) * t);

// one grade for both layers: the tile body takes the total return, the bar the annualised one,
// so a position whose IRR trails its total return gets a lighter bar than its body.
const GRADE_UP = 300;      // % where the ramp saturates on the way up
const GRADE_DOWN = 100;    // …and down, the floor a position can reach
const GRADE_HALF = 20;     // % that lands halfway up the unnormalised curve — sets the bend

// Saturating (hyperbolic) grade: t ∝ v / (v + GRADE_HALF), normalised so the cap lands on 1.
// It spends its contrast where the returns actually live: 10% → 20% moves the colour as far
// as 50% → 150% does, instead of a quarter as far like a square-root or linear ramp.
function grade(v, cap) {
  const x = Math.min(Math.abs(v), cap);
  return (x / (x + GRADE_HALF)) / (cap / (cap + GRADE_HALF));
}
function gradeColor(v, cash) {
  const st = mapStops();
  if (cash || !Number.isFinite(v)) return rgb(st.zero);
  const arm = v >= 0 ? st.up : st.down;
  return rgb(lerp(st.zero, arm.full, grade(v, v >= 0 ? GRADE_UP : GRADE_DOWN)));
}
const barColor = d => MODE === 'rel'
  ? gradeColor(d.ret, d.cash)
  : gradeColor(Number.isFinite(d.irr) ? d.irr : d.ret, d.cash);

// the bar is the result measured against the tile it sits in — the current value.
// +100% return → half the tile; −50% return → the loss equals the current value, so the whole tile.
function barShare(d) {
  if (d.cash || d.state === 'flat' || d.cur <= 0) return 0;
  return Math.min(1, Math.abs(d.gain) / d.cur);
}
const luminance = rgb => {
  const parts = String(rgb).match(/\d+/g);
  if (!parts || parts.length < 3) return 0.5;        // a token colour, not an rgb() string
  const [r, g, b] = parts.map(v => {
    const c = v / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
};

// The map's sector: from the registry's own sector column (ISIN → sector), cash gets its
// own bucket, anything the file doesn't cover — a new position, before it's been triaged — falls
// to "Other" rather than breaking the grouping.
const sectorOf = d => d.cash ? 'Cash' : (SECTORS.get(d.identifier) || 'Other');

// Fixed order so a sector's colour and position among its peers stay put across sessions — only
// the sectors that actually appear are drawn, but their relative order never depends on which
// ones happen to be present today. Anything not listed here (a sector added to the CSV later)
// still gets a colour, just hashed rather than hand-picked.
const SECTOR_ORDER = ['Optical', 'Hyperscaler', 'Semiconductors', 'Cybersecurity', 'AI Supply',
  'Other', 'Cash'];
function sectorHue(name) {
  const i = SECTOR_ORDER.indexOf(name);
  if (i >= 0) return (i * 137.508) % 360;                 // golden-angle spacing, well separated
  let h = 0;
  for (let i2 = 0; i2 < name.length; i2++) h = (h * 31 + name.charCodeAt(i2)) >>> 0;
  return h % 360;
}
function sectorRank(name) {
  const i = SECTOR_ORDER.indexOf(name);
  return i >= 0 ? i : SECTOR_ORDER.length + name.charCodeAt(0);
}

// squarified treemap: lay `vals` (descending) into rect {x,y,w,h}
function squarify(vals, rect) {
  const out = [];
  let { x, y, w, h } = rect;
  let rest = vals.slice();
  const sum = a => a.reduce((t, v) => t + v.value, 0);
  const worst = (row, len, scale) => {
    const s = sum(row) * scale;
    if (!s || !len) return Infinity;
    const mx = Math.max(...row.map(v => v.value)) * scale;
    const mn = Math.min(...row.map(v => v.value)) * scale;
    return Math.max(len * len * mx / (s * s), (s * s) / (len * len * mn));
  };
  while (rest.length) {
    const vertical = w >= h;
    const len = vertical ? h : w;
    const total = sum(rest);
    const scale = (w * h) / (total || 1);
    const row = [];
    while (rest.length) {
      const next = [...row, rest[0]];
      if (row.length && worst(next, len, scale) > worst(row, len, scale)) break;
      row.push(rest.shift());
    }
    const rowSum = sum(row) * scale;
    const thick = rowSum / (len || 1);
    let off = 0;
    row.forEach(v => {
      const side = (v.value * scale) / (thick || 1);
      out.push(vertical
        ? { item: v.item, x, y: y + off, w: thick, h: side }
        : { item: v.item, x: x + off, y, w: side, h: thick });
      off += side;
    });
    if (vertical) { x += thick; w -= thick; } else { y += thick; h -= thick; }
  }
  return out;
}

// Every tile pays for chrome out of its own area: the 1-unit gap that separates it from its
// neighbour. That toll is a fixed number of viewBox units per tile, not a share of it, so it
// falls hardest on the smallest — which breaks the one rule this page has if left unpaid.
// Measured against the real portfolio before this compensated for it, tiles ran from 46% to
// 104% of the area their value called for, and a position in a small sector could gain 10%
// while its tile barely moved.
//
// So the layout pays the toll from a separate pocket: lay out, measure what each tile will
// actually lose to chrome, hand it that much room on top of its value, and lay out again.
// `overhead` reports the units one laid-out rect loses. The toll depends on a rect's perimeter,
// which stops moving as soon as the areas are close, so this reaches its fixed point in two
// passes and the third is only insurance — every tile then draws its exact share of the value.
function squarifyNet(vals, rect, overhead) {
  let laid = squarify(vals, rect);
  const total = vals.reduce((t, v) => t + v.value, 0);
  const room = rect.w * rect.h;
  if (!(total > 0) || !(room > 0)) return laid;
  for (let pass = 0; pass < 3; pass++) {
    const toll = new Map(laid.map(t => [t.item, Math.max(0, overhead(t))]));
    const spare = room - [...toll.values()].reduce((t, v) => t + v, 0);
    if (spare <= 0) break;                 // nothing left to share out; keep the plain layout
    const k = spare / total;
    // the input order is a fixed key, never value, so re-weighting never reshuffles the tiles
    laid = squarify(vals.map(v => ({ item: v.item, value: k * v.value + toll.get(v.item) })), rect);
  }
  return laid;
}

// what one position tile loses to the 1-unit gap on two of its edges, and what is left of it
const tileToll = t => t.w + t.h - 1;
const tileArea = t => Math.max(0, t.w - 1) * Math.max(0, t.h - 1);

// The area a sector rect really hands to its positions, once every tile's own gap inside it is
// paid — the sector frame itself costs nothing (no inset, no head strip: positions run flush to
// its edge). This, not the sector rect, is the figure that has to come out proportional to the
// sector's value: a short wide sector spends far more of itself on tile gaps than a square one
// does, so measuring the rect instead of the net would still leave the tiles in the flattest
// sector under their due.
function sectorNetArea(sr, members) {
  if (sr.w <= 0 || sr.h <= 0) return 0;
  return layTiles(sr, members).reduce((t, x) => t + tileArea(x), 0);
}
const layTiles = (inner, members) =>
  squarifyNet(members.map(d => ({ value: d.cur, item: d })), inner, tileToll);

function renderMap(items, asOf) {
  const svg = document.getElementById('pie');
  const W = 900, H = 560, PAD = 2;
  const note = document.getElementById('asOfNote');
  let mapScale = 1;                      // set below when the as-of map is shrunk; 1 otherwise

  if (asOf) {
    // No floor here: a floor would clamp every ratio below it to the same width, so two real
    // past values that both happen to sit under the floor render pixel-identical — exactly the
    // "gain doesn't move the box" bug this invariant exists to prevent. sqrt(ratio) all the way
    // down keeps distinct values visibly distinct, even when both are small.
    mapScale = Math.min(1, Math.sqrt(Math.max(0, asOf.ratio)));
    svg.style.width = (mapScale * 100) + '%';
    svg.style.setProperty('--map-scale', mapScale);
    note.hidden = false;
    note.innerHTML = `As of <b>${asOf.date}</b>: <b>${fmtMoney2(asOf.asOfTotal)}</b>` +
      ` (${(asOf.ratio * 100).toFixed(0)}% of today's ${fmtMoney(asOf.currentTotal)}) — ` +
      `area scaled to match, since area is value throughout this page. Labels stay full size.` +
      (asOf.missing.length
        ? ` ${asOf.missing.length} held then but omitted for lack of price history: ${asOf.missing.join(', ')}.`
        : '');
  } else {
    svg.style.width = '100%';
    svg.style.setProperty('--map-scale', 1);
    note.hidden = true;
  }

  const g = el('g', {});
  items.forEach(d => { d.nodes = []; });
  const nudge = 1 / mapScale;

  // two-level treemap: sectors first, each position's own tile squarified inside its sector's
  // slice. Both levels sort by a fixed key (sector order, then label) rather than by current
  // value, so a day's price move resizes tiles without reshuffling who sits next to whom.
  const bySector = new Map();
  items.filter(d => d.cur > 0).forEach(d => {
    const s = sectorOf(d);
    (bySector.get(s) || bySector.set(s, []).get(s)).push(d);
  });
  // settle the member order here, once: sectorNetArea below lays the same tiles out to measure
  // what a sector rect is worth, and it has to reach the layout the drawing will actually use
  bySector.forEach(members => members.sort((a, b) => a.label.localeCompare(b.label)));
  const sectorNames = [...bySector.keys()].sort((a, b) => sectorRank(a) - sectorRank(b));
  const sectorRects = squarifyNet(
    sectorNames.map(s => ({ value: bySector.get(s).reduce((t, d) => t + d.cur, 0), item: s })),
    { x: PAD, y: PAD, w: W - 2 * PAD, h: H - 2 * PAD },
    sr => sr.w * sr.h - sectorNetArea(sr, bySector.get(sr.item))
  );

  sectorRects.forEach(sr => {
    const hue = sectorHue(sr.item);
    // outline only, in the same neutral --rim token the pie's own rim uses (black on light,
    // white on dark) — never the sector's hue, so two sectors sharing an edge draw the same
    // line on top of each other and it reads as one divider, not two colours meeting.
    // No half-pixel crisp-line offset here (unlike the position tiles): the svg is stretched to
    // whatever width its container gives it, so 1 viewBox unit is rarely 1 device pixel, and that
    // offset — tuned for an exact 1:1 map — only made the line's rendered weight swim between
    // roughly 1 and 3px as the window resized. A plain 2px stroke on the rect's true edges is
    // heavy enough to anti-alias consistently at any scale instead.
    //
    // *nudge, though, same as the label offsets below: an as-of pick shrinks the whole svg by
    // mapScale, and a bare stroke-width shrinks right along with it — the border on a small,
    // long-ago snapshot would visibly thin out as its value dropped, when the line is chrome, not
    // data, and should read the same width regardless of what the map happens to be showing.
    const bg = el('rect', {
      x: sr.x, y: sr.y, width: sr.w, height: sr.h,
      fill: `hsl(${hue.toFixed(1)}deg 55% 55% / 0.22)`,
      stroke: 'var(--rim)', 'stroke-width': 2 * nudge,
      class: 'sectorbg',
    });
    g.appendChild(bg);

    if (sr.w <= 0 || sr.h <= 0) return;

    // positions run flush to the sector frame — no inset, no head strip; the sector's own name
    // is an overlay on the top-left tile instead (below), so it never takes layout space of its own
    let firstTile = null;
    layTiles(sr, bySector.get(sr.item)).forEach((t, i) => {
      const d = t.item;
      const w = Math.max(0, t.w - 1), h = Math.max(0, t.h - 1);
      const fill = gradeColor(d.state === 'flat' ? NaN : d.ret, d.cash);
      if (i === 0) firstTile = t;   // top-left tile: the sector label overlays it, below
      // no stroke on either layer — the 1px layout gap is the separator (showing the sector's
      // own background colour through), so the bar can never look wider than the tile it sits in
      const rect = el('rect', { x: t.x + 0.5, y: t.y + 0.5, width: w, height: h, fill,
                                class: 'maprect' });
      rect.addEventListener('click', () => openDetail(d));
      g.appendChild(rect); d.nodes.push(rect);

      // income band at the head of the tile: dividends the held shares paid, against tile value
      if (d.divHeld > 0 && d.cur > 0 && h > 6) {
        const ih = Math.max(1.5, Math.min(h / 3, h * d.divHeld / d.cur));
        const band = el('rect', {
          x: t.x + 0.5, y: t.y + 0.5, width: w, height: ih, fill: 'var(--income)',
        });
        g.appendChild(band); d.nodes.push(band);
      }

      const share = barShare(d);            // also decides the label's headroom, further down
      if (share > 0 && h > 4) {
        const bh = Math.max(1.5, h * share);
        const bar = el('rect', {
          x: t.x + 0.5, y: t.y + 0.5 + (h - bh), width: w, height: bh,
          fill: barColor(d),
        });
        g.appendChild(bar); d.nodes.push(bar);
      }

      if (w > 4 && h > 7) {
        const dark = d.fund || d.cash;          // funds and cash in black ink, everything else white
        const cx = t.x + w / 2;
        const free = h * (1 - share);                       // headroom above the bar
        const cy = t.y + (free > 26 ? free / 2 : h / 2);
        // 4.9 viewBox units/char is calibrated at mapScale 1; the label's own font-size is
        // compensated to stay a constant physical size regardless of mapScale (see .maplbl), so a
        // shrunk as-of map fits fewer characters per viewBox unit of tile width, not the same
        // count — without the factor here, labels overflowed their tiles once the map shrank.
        // Floored at 1: even a sliver of a tile still gets its first character rather than nothing.
        const chars = Math.max(1, Math.floor(w * mapScale / 4.9));
        const name = el('text', {
          x: cx, y: h > 28 ? cy - nudge : cy + 3 * nudge, 'text-anchor': 'middle',
          class: 'maplbl' + (dark ? ' dark' : ''), 'pointer-events': 'none',
        });
        name.textContent = d.label.length > chars ? d.label.slice(0, chars) : d.label;
        g.appendChild(name);
        if (h > 28) {
          const sub = el('text', {
            x: cx, y: cy + 10 * nudge, 'text-anchor': 'middle',
            class: 'maplbl mapsub' + (dark ? ' dark' : ''), 'pointer-events': 'none',
          });
          sub.textContent = d.cash ? fmtMoney(d.cur) : fmtPct(d.ret);
          g.appendChild(sub);
        }
      }
    });

    // sector name, overlaid on the top-left tile after it (and everything on it) is drawn, so it
    // sits on top rather than sharing the tile's own space
    if (firstTile && firstTile.w > 30 && firstTile.h > 16) {
      const lbl = el('text', {
        x: firstTile.x + 4 * nudge, y: firstTile.y + 8 * nudge,
        class: 'sectorlbl',
      });
      lbl.textContent = sr.item;
      g.appendChild(lbl);
    }
  });

  svg.setAttribute('aria-label', 'Treemap of all positions grouped by sector, area by current value, colour by return');
  svg.replaceChildren(g);
  attachTip(items);
  legendMap();
}

// paint the legend through the same curve, so the swatch shows where the contrast sits
function gradientStops() {
  const out = [];
  for (let i = 0; i <= 20; i++) {
    const pos = i / 20;
    const v = pos <= 0.5
      ? -GRADE_DOWN * (1 - pos * 2)
      : GRADE_UP * (pos - 0.5) * 2;
    out.push(`${gradeColor(v, false)} ${(pos * 100).toFixed(0)}%`);
  }
  return out.join(',');
}

// one stacked bar over every realised result in the account, pre-tax. Two main blocks — gains and
// losses — each split into the positions still open and the ones closed out; tax closes the losses.
function renderClosed(over = null) {
  const wrap = document.getElementById('closedWrap');
  const svg = document.getElementById('closed');
  const has = d => Math.abs(barValue(d)) > 0.005 && !d.cash;
  const closed = (over ? over.closed : CLOSED).filter(has);
  const open = (over ? over.open : ITEMS).filter(has);
  const divTotal = over ? over.divTotal : DIV_TOTAL;
  const taxTotal = over ? over.taxTotal : TAX_TOTAL;
  const divRows = over ? over.divRows : DIV_ROWS;
  const taxSplit = over ? over.taxSplit : TAX_SPLIT;
  if (!closed.length && !open.length) { wrap.hidden = true; return; }
  wrap.hidden = false;

  // y is the bar's own top padding, inside the svg, below the "Realized gains" header that sits
  // just above it in the HTML — trimmed from 14 so the header reads closer to what it labels. H
  // shrinks by the same amount, not just y, so the labels below the bar (unchanged relative to y)
  // keep the same margin under them they always had, instead of the removed space reappearing
  // at the bottom.
  const W = 900, H = 88, y = 6, h = 30, STRIP_Y = 3, STRIP_H = 6, DIV_Y = 8;
  const pick = (rows, sign) => rows.filter(d => Math.sign(barValue(d)) === sign)
                                   .sort((a, b) => Math.abs(barValue(b)) - Math.abs(barValue(a)));
  const divRow = divTotal > 0.005
    ? { name: 'Dividends', label: 'Dividends', portfolio: 'all portfolios', relPre: divTotal,
        isDiv: true, sold: true, flows: [], nodes: [] }
    : null;
  const taxRow = taxTotal > 0.005
    ? { name: 'Taxes', label: 'Taxes', portfolio: 'all portfolios', relPre: -taxTotal, isTax: true, flows: [], nodes: [] }
    : null;
  const sub = (label, rows) => ({ label: `${label} · ${rows.length}`, rows });

  const closedGains = pick(closed, 1);
  if (divRow) closedGains.push(divRow);              // booked like a position sold at a profit
  // "gains"/"losses" is a total-return read; in vs.-World mode the bar is grouped by alpha
  // instead (barValue() switches to d.alpha there), so the labels need to say what the split
  // actually means now — beat the benchmark or trailed it, not merely ended up positive
  const mains = [
    { label: MODE === 'rel' ? 'overperform' : 'gains', subs: [sub('open', pick(open, 1)), sub('closed', closedGains)] },
    { label: MODE === 'rel' ? 'underperform' : 'losses', subs: [
      sub('open', pick(open, -1)), sub('closed', pick(closed, -1)),
      ...(taxRow ? [{ label: 'tax', rows: [taxRow] }] : []),
    ] },
  ].map(m => ({ ...m, subs: m.subs.filter(sg => sg.rows.length) })).filter(m => m.subs.length);

  const ordered = mains.flatMap(m => m.subs.flatMap(sg => sg.rows));
  const magnitude = ordered.reduce((t, d) => t + Math.abs(barValue(d)), 0) || 1;
  // real gaps between the blocks, so the dividers read as structure rather than hairlines
  const GAP_SUB = 5, GAP_MAIN = 14;
  const subBreaks = mains.reduce((t, m) => t + m.subs.length - 1, 0);
  const mainBreaks = mains.length - 1;
  const scale = (W - subBreaks * GAP_SUB - mainBreaks * GAP_MAIN) / magnitude;

  const g = el('g', {});
  const marks = el('g', { 'pointer-events': 'none' });
  const rule = (x, y1, y2, w) => marks.appendChild(el('line',
    { x1: x, y1, x2: x, y2, stroke: 'var(--rim)', 'stroke-width': w }));

  let x = 0;
  mains.forEach((m, mi) => {
    const mx0 = x;
    m.subs.forEach((sg, si) => {
      const sx0 = x;
      sg.rows.forEach(d => {
        const w = Math.max(0.6, Math.abs(barValue(d)) * scale);
        // shade by the realised result on the money that went in, so the colour always
        // agrees with the side of the bar the segment is on
        const pct = d.invested > 0 ? barValue(d) / d.invested * 100 : (barValue(d) >= 0 ? 30 : -30);
        const fill = d.isTax ? gradeColor(-60, false)
          : d.isDiv ? 'var(--income)' : gradeColor(pct, false);
        const rect = el('rect', { x, y, width: Math.max(0, w - 0.75), height: h, fill });
        g.appendChild(rect);
        d.nodes = [rect];

        // what the shares did after the sale, read from your side — a rise since selling is a
        // loss to you, so the grade is inverted. Closed positions get it too, now that
        // prices/_latest.csv supplies the price Parqet stopped publishing at the sale.
        if (!d.isTax && !d.isDiv && d.sinceKnown && d.grossProceeds > 0) {
          // absolute: did the price fall after the sale? benchmark: did selling and holding the
          // index beat holding on? Both are graded so that green means the sale was right.
          const pct = MODE === 'rel' && Number.isFinite(d.vsBench)
            ? -d.vsBench / d.grossProceeds * 100
            : d.since / d.grossProceeds * 100;
          const strip = el('rect', {
            x, y: y + h + STRIP_Y, width: Math.max(0, w - 0.75), height: STRIP_H,
            fill: gradeColor(MODE === 'rel' ? pct : -pct, false),
          });
          g.appendChild(strip); d.nodes.push(strip);
        }
        if (w > 46) {
          const dark = luminance(fill) > 0.55;
          const chars = Math.floor(w / 4.6);
          const cx = x + w / 2 - 0.4;
          const name = el('text', { x: cx, y: y + 13, 'text-anchor': 'middle',
                                    class: 'closedlbl' + (dark ? ' dark' : '') });
          const lbl = d.label || d.name;
          name.textContent = lbl.length > chars ? lbl.slice(0, Math.max(1, chars - 1)) + '…' : lbl;
          const val = el('text', { x: cx, y: y + 24, 'text-anchor': 'middle',
                                   class: 'closedlbl' + (dark ? ' dark' : '') });
          val.textContent = fmtMoney(barValue(d));
          marks.appendChild(name); marks.appendChild(val);
        }
        x += w;
      });
      const xEnd = x;

      if (si < m.subs.length - 1) {                                       // open | closed | tax
        rule(x + GAP_SUB / 2 - 0.4, y - DIV_Y - 2, y + h + STRIP_Y + STRIP_H + 2, 1.4);
        x += GAP_SUB;
      }
      const cap = el('text', { x: (sx0 + xEnd) / 2, y: y + h + 23, 'text-anchor': 'middle', class: 'mapgrp' });
      cap.textContent = sg.label;
      marks.appendChild(cap);
    });
    const mxEnd = x;
    if (mi < mains.length - 1) {                                          // gains | losses
      rule(x + GAP_MAIN / 2 - 0.4, y - DIV_Y - 5, y + h + 44, 2.2);
      x += GAP_MAIN;
    }
    const cap = el('text', { x: (mx0 + mxEnd) / 2, y: y + h + 40, 'text-anchor': 'middle', class: 'mainlbl' });
    cap.textContent = m.label;
    marks.appendChild(cap);
  });

  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.replaceChildren(g, marks);

  const sum = (rows, sign) => rows.reduce((t, d) => t + (Math.sign(barValue(d)) === sign ? barValue(d) : 0), 0);
  const wins = sum(closed, 1) + sum(open, 1) + divTotal;
  const loss = sum(closed, -1) + sum(open, -1);
  const netEl = document.getElementById('closedNet');
  netEl.textContent = `Realized gains pre-tax ${fmtMoney(wins)} · dividends ${fmtMoney(divTotal)}` +
    ` · losses ${fmtMoney(loss)} · taxes ${fmtMoney(-taxTotal)}` +
    ` · net ${fmtMoney(wins + loss - taxTotal)}`;
  netEl.className = wins + loss - taxTotal >= 0 ? 'realized' : 'neg';

  attachTipClosed(ordered, { divTotal, divRows, taxTotal, taxSplit });
}

function attachTipClosed(rows, ctx = null) {
  const divTotal = ctx ? ctx.divTotal : DIV_TOTAL;
  const divRows = ctx ? ctx.divRows : DIV_ROWS;
  const taxTotal = ctx ? ctx.taxTotal : TAX_TOTAL;
  const taxSplit = ctx ? ctx.taxSplit : TAX_SPLIT;
  const tip = document.getElementById('tip');
  const wrapEl = document.getElementById('closed').closest('.chartwrap');
  // dividends/taxes are synthetic rows (all portfolios, no single position behind them) — every
  // other row here is a real position, open or closed, and opens the same chart a map tile does
  const clickable = d => !d.isDiv && !d.isTax;
  rows.forEach(d => d.nodes.forEach(n => {
    n.style.cursor = clickable(d) ? 'pointer' : 'default';
    n.addEventListener('pointerenter', e => {
      if (d.isDiv) {
        const byName = new Map();
        divRows.forEach(r => byName.set(r.name, (byName.get(r.name) || 0) + num(r.amountNet)));
        const top = [...byName.entries()].sort((a, b) => b[1] - a[1]);
        tip.innerHTML = `<div class="t">Dividends</div>` +
          `<div class="pf">all portfolios · net of withholding</div>` +
          `<div class="r"><span>Total · ${divRows.length} payments</span>` +
          `<b class="income">${fmtMoney2(divTotal)}</b></div>` +
          top.slice(0, 8).map(([name, amt]) =>
            `<div class="r"><span>${NAMES.get(name) || tidyName(name)}</span>` +
            `<b class="income">${fmtMoney2(amt)}</b></div>`).join('') +
          (top.length > 8 ? `<div class="r"><span>+ ${top.length - 8} smaller</span><b></b></div>` : '');
      } else if (d.isTax) {
        tip.innerHTML = `<div class="t">Taxes</div><div class="pf">all portfolios</div>` +
          `<div class="r"><span>On sales</span><b class="neg">${fmtMoney2(-taxSplit.sell)}</b></div>` +
          `<div class="r"><span>On dividends</span><b class="neg">${fmtMoney2(-taxSplit.dividend)}</b></div>` +
          `<div class="r"><span>Advance lump sum</span><b class="neg">${fmtMoney2(-taxSplit.other)}</b></div>` +
          `<div class="r"><span>Total</span><b class="neg">${fmtMoney2(-taxTotal)}</b></div>`;
      } else {
        tip.innerHTML =
          tipHead(d, true) +
          `<div class="pf">${d.portfolio} · ${d.sold ? 'closed' : 'still open'}</div>` +
          tipSaleRows(d) +
          tipRow('Realised (pre-tax)', fmtMoney2(d.relPre), d.relPre >= 0 ? 'realized' : 'neg') +
          (Number.isFinite(d.alpha)
            ? tipRow(`beat ${BENCH_LABEL} by`, fmtMoney2(d.alpha), d.alpha >= 0 ? 'realized' : 'neg') : '') +
          (d.invested > 0
            ? tipRow(`on cost ${fmtMoney(d.invested)}`,
                     fmtPct(d.relPre / d.invested * 100), posNeg(d.relPre)) : '') +
          (d.divSold > 0 ? tipRow('Dividends (sold shares)', fmtMoney2(d.divSold), 'income') : '') +
          (d.taxSell > 0.005 ? tipRow('Tax paid', fmtMoney2(-d.taxSell), 'neg') : '') +
          tipIrrRow(d, 'Annualised') +
          tipRow('Trades', d.flows.length || d.activityCount || '–') +
          `<div class="sep">What if held${d.lastSell ? ' since ' + d.lastSell.slice(0, 10) : ''}</div>` +
          (d.sinceKnown
            ? tipRow('Until today', fmtMoney2(d.since) +
                (d.grossProceeds > 0 ? ` (${fmtPct(d.since / d.grossProceeds * 100)})` : ''),
                posNeg(d.since)) +
              (Number.isFinite(d.vsBench)
                ? tipRow('vs. World',
                    `${fmtMoney2(d.vsBench)} (${fmtPct((d.heldValue / d.benchValue - 1) * 100)})`,
                    posNeg(d.vsBench)) : '') +
              (d.split !== 1
                ? tipRow('split since sale',
                    d.split >= 1 ? `${d.split}:1` : `1:${Math.round(1 / d.split)}`, 'mut') : '')
            : tipRow('no data', '–', 'mut'));
      }
      tip.classList.add('on');
      placeTip(tip, wrapEl, e);
      rows.forEach(o => o.nodes.forEach(m => m.style.opacity = o === d ? 1 : 0.35));
    });
    n.addEventListener('pointermove', e => placeTip(tip, wrapEl, e));
    if (clickable(d)) n.addEventListener('click', () => openDetail(d));
    n.addEventListener('pointerleave', () => {
      tip.classList.remove('on');
      rows.forEach(o => o.nodes.forEach(m => m.style.opacity = 1));
    });
  }));
}

function legendMap() {
  const legend = document.getElementById('legend');
  const item = html => {
    const div = document.createElement('div'); div.className = 'item'; div.innerHTML = html; return div;
  };
  legend.replaceChildren(
    item(`<span>−${GRADE_DOWN}%</span>` +
      `<span class="swatch" style="width:150px;background:linear-gradient(90deg,${gradientStops()})"></span>` +
      `<span>+${GRADE_UP}% — one ramp: ${MODE === 'rel'
        ? `body and foot bar = performance against ${BENCH_LABEL}`
        : 'body = total return, foot bar = annualised'}</span>`),
    item(`<span class="swatch" style="background:var(--income)"></span>` +
      `<span>Gold = dividends — a band on the tile, one block in the bar</span>`),
    item(`<span>Tile area = current value · bar height = gain or loss ÷ current value</span>`),
  );
}

/* ---------- detail overlay ---------- */
// Both lines rebased to 100 at the first date shown, so shape is comparable regardless of price.
// Clicking a trade marker re-anchors the benchmark to that date instead, so the two lines meet there.

// Every open or closed, non-cash position — the pool clicking the benchmark's own label can pick
// a replacement from. Deduped by identifier: the same ISIN can appear twice (open in one
// portfolio, closed in another — POET Technologies does), and it should only offer once.
function benchmarkCandidates() {
  const seen = new Map();
  [...ITEMS, ...CLOSED].forEach(d => {
    if (!d.cash && !seen.has(d.identifier)) seen.set(d.identifier, d);
  });
  return [...seen.values()].sort((a, b) => a.label.localeCompare(b.label));
}

const RANGE_FROM = {
  all: (d, series) => series.rows[0].date,
  '5y': () => shiftYears(-5),
  '1y': () => shiftYears(-1),
  ytd: () => new Date().getFullYear() + '-01-01',
  buy: d => d.firstActivity || '',
};
function shiftYears(n) {
  const t = new Date();
  t.setFullYear(t.getFullYear() + n);
  return t.toISOString().slice(0, 10);
}

// custom, when given, is a { from, to } drag-selected window that overrides the range buttons
// entirely — both ends explicit, unlike a button's range which always runs through to today.
// altBench, when given, swaps out the real MSCI World series for another position's own — same
// { series, label } shape loadSeries()/a position both already carry, so a picked stock slots in
// with no separate code path.
function drawDetail(d, series, alignDate, range, custom, altBench) {
  const W = 840, H = 300, T = 12, B = 22;
  let from, to;
  if (custom) {
    ({ from, to } = custom);
  } else {
    const pick = (RANGE_FROM[range] || RANGE_FROM.buy)(d, series);
    from = pick && pick > series.rows[0].date ? pick : series.rows[0].date;
    to = series.rows[series.rows.length - 1].date;
  }
  let rows = series.rows.filter(r => r.date >= from && r.date <= to);
  if (rows.length < 2) return null;
  const benchLabel = altBench ? altBench.label : BENCH_LABEL;
  const bench = (altBench ? altBench.series.rows : BENCH).filter(r => r.date >= rows[0].date);

  // The x-axis spaces points by index, not calendar time — normally harmless, since real trading
  // days are already close to evenly spaced. It breaks badly across a genuine gap in the price
  // history (a thinly-traded listing Yahoo has no data for over some stretch — Roche's RHO.DE
  // has one, 2019-09 to 2025-04): the two rows on either side land on ADJACENT x positions no
  // matter how many years actually separate them, so whatever real price move happened during
  // the gap draws as a single implausible jump. And that jump doesn't stay confined to this
  // line — the benchmark is looked up AT these same dates, so it visibly "jumps" too, even
  // though its own data has no gap at all.
  //
  // Rather than get the x-axis right in general (a bigger change, and every other line here is
  // already fine with index spacing), patch the one case that's actually broken: bridge a gap
  // with synthetic weekly points holding the last known price flat, so the x-axis allocates that
  // stretch roughly the width real trading days would have, and a still-continuous benchmark or
  // alt-benchmark reads its own real values there instead of two years apart read as one step.
  const GAP_DAYS = 20;                 // past a long holiday cluster; a real data gap, not a weekend
  const WEEK = 7 * 864e5;
  const bridged = [rows[0]];
  for (let i = 1; i < rows.length; i++) {
    const prevT = Date.parse(rows[i - 1].date), curT = Date.parse(rows[i].date);
    if ((curT - prevT) / 864e5 > GAP_DAYS) {
      for (let t = prevT + WEEK; t < curT; t += WEEK)
        bridged.push({ date: new Date(t).toISOString().slice(0, 10), close: rows[i - 1].close, filled: true });
    }
    bridged.push(rows[i]);
  }
  rows = bridged;
  const gapRuns = [];                  // [{ from, to }] — every bridged stretch, for the warning
  rows.forEach((r, i) => {
    if (!r.filled) return;
    if (!rows[i - 1] || !rows[i - 1].filled) gapRuns.push({ from: rows[i - 1].date, to: null });
    gapRuns[gapRuns.length - 1].to = rows[i + 1] ? rows[i + 1].date : r.date;
  });

  const dates = rows.map(r => r.date);
  const xOf = date => {                                  // nearest trading day at or before
    const i = lastIndexAtOrBefore(rows, date);
    return i >= 0 ? i : null;
  };
  const at = lastAtOrBefore;

  // both lines are read as % against the tie point, so they are 0 there and cross by construction
  const anchor = (alignDate && xOf(alignDate) !== null) ? dates[xOf(alignDate)] : dates[0];
  const stockAnchor = rows[xOf(anchor)];
  const benchAnchor = at(bench, anchor);
  const stockPct = r => (r.close / stockAnchor.close - 1) * 100;
  const benchPct = b => benchAnchor ? (b.close / benchAnchor.close - 1) * 100 : NaN;

  const benchVals = dates.map(dt => {
    const b = at(bench, dt);
    return b ? benchPct(b) : null;
  });
  const stockVals = rows.map(stockPct);                  // kept: the hover crosshair reads back into it
  const values = [...stockVals, ...benchVals.filter(Number.isFinite), 0];
  const lo = Math.min(...values), hi = Math.max(...values);
  // No padding on either edge: each bound is the actual all-time low/high for the window shown —
  // the most either line, stock or benchmark, ever fell or rose — so the chart never implies more
  // room than the data has evidence for, above a peak or below a trough alike. Guard against the
  // one degenerate case a flat pair of bounds would divide by zero on: a dead-flat line.
  const yLo = lo, yHi = hi > lo ? hi : lo + 1;
  const y = v => T + (H - T - B) * (1 - (v - yLo) / (yHi - yLo));

  // the left margin is whatever the widest y-axis label needs, so a big swing (a multi-bagger's
  // "+10000%") never runs past the left edge — same sizing rule as the right margin below
  const axisTexts = [0, 1, 2, 3, 4].map(i => {
    const v = yLo + (yHi - yLo) * i / 4;
    return `${v >= 0 ? '+' : ''}${Math.round(v)}%`;
  });
  const L = Math.max(34, 10 + Math.max(...axisTexts.map(t => t.length)) * 5.6);

  // the right margin is whatever the end labels need, so they can never be clipped
  const lastStock = stockVals[stockVals.length - 1];
  const lastBench = benchVals.filter(Number.isFinite).slice(-1)[0];
  const keys = [{ text: `${d.label} ${fmtPct(lastStock)}`, v: lastStock, colour: 'var(--series-1)' }];
  if (Number.isFinite(lastBench))
    keys.push({ text: `${benchLabel} ${fmtPct(lastBench)}`, v: lastBench,
                colour: 'var(--text-secondary)', isBench: true });
  const R = Math.min(210, 14 + Math.max(...keys.map(k => k.text.length)) * 5.6);
  const fits = Math.floor((R - 14) / 5.6);              // a very long name gets clipped, not the label
  keys.forEach(k => {
    if (k.text.length > fits) k.text = k.text.slice(0, Math.max(4, fits - 1)) + '…';
  });
  const x = i => L + (W - L - R) * (i / (dates.length - 1));
  const benchPts = benchVals.map((v, i) => Number.isFinite(v) ? [x(i), v] : null).filter(Boolean);

  const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img',
    'aria-label': `${d.label} against the benchmark, in per cent from ${anchor}` });

  for (let i = 0; i <= 4; i++) {
    const v = yLo + (yHi - yLo) * i / 4;
    svg.appendChild(el('line', { x1: L, y1: y(v), x2: W - R, y2: y(v), class: 'dtgrid' }));
    const t = el('text', { x: L - 6, y: y(v) + 3, class: 'dtaxis', 'text-anchor': 'end' });
    t.textContent = axisTexts[i];
    svg.appendChild(t);
  }
  svg.appendChild(el('line', { x1: L, y1: y(0), x2: W - R, y2: y(0), class: 'dtzero' }));
  // year labels, plus small ticks along the bottom marking quarters — April, July, October, never
  // January, since that boundary already has the year label above to carry it. Zoomed under a
  // year, quarters would land three or fewer per chart — too sparse to read anything from — so
  // every month gets its own tick and a single-letter label instead, except where that letter
  // would land right on top of a year label already claiming the same spot.
  const spanDays = (Date.parse(dates[dates.length - 1]) - Date.parse(dates[0])) / 864e5;
  const monthMode = spanDays < 365;
  const MONTH_LETTER = 'JFMAMJJASOND';
  // a range starting mid-year (a drag selection, a "1y"/"5y" button) shouldn't claim that year
  // with a label at the left edge — it isn't really where that year begins, just where the view
  // happens to cut in. Only a range that genuinely starts in January gets to show it up front;
  // priming lastYear with the real starting year everywhere else skips that first, false label.
  let lastYear = (+dates[0].slice(5, 7) === 1) ? '' : dates[0].slice(0, 4);
  // same idea one level down: a range that starts on, say, the 20th of April isn't where April
  // begins either, so priming lastMonth the same way keeps that leading tick from claiming a
  // month (or quarter) it only partly covers. <=3 allows for a weekend or holiday pushing the
  // first trading day of a genuine month-start forward a little, same slack every other month
  // boundary in this walk already gets for free by only checking when the month value changes.
  const startDay = +dates[0].slice(8, 10);
  let lastMonth = startDay <= 3 ? '' : String(+dates[0].slice(5, 7));
  dates.forEach((dt, i) => {
    const yr = dt.slice(0, 4), mi = +dt.slice(5, 7);      // mi: 1..12
    const newYear = yr !== lastYear;
    if (newYear) {
      lastYear = yr;
      const t = el('text', { x: x(i), y: H - 6, class: 'dtaxis', 'text-anchor': 'middle' });
      t.textContent = yr;
      svg.appendChild(t);
      // in quarter mode January never qualifies as a quarter (only April/July/October do), so
      // without this the year boundary itself would be the one tick-less label on the axis;
      // month mode already draws January's own tick below, so skip it here to avoid a double line
      if (!monthMode) svg.appendChild(el('line',
        { x1: x(i), y1: H - B, x2: x(i), y2: H - B + 5, class: 'dtqtick' }));
    }
    if (String(mi) === lastMonth) return;
    lastMonth = String(mi);
    if (!monthMode && mi !== 4 && mi !== 7 && mi !== 10) return;
    const tx = x(i);
    svg.appendChild(el('line', { x1: tx, y1: H - B, x2: tx, y2: H - B + 5, class: 'dtqtick' }));
    if (monthMode && !newYear) {
      const t = el('text', { x: tx, y: H - 6, class: 'dtaxis', 'text-anchor': 'middle' });
      t.textContent = MONTH_LETTER[mi - 1];
      svg.appendChild(t);
    }
  });

  const path = (pts, stroke, dashed) => svg.appendChild(el('path', {
    d: pts.map((p, i) => `${i ? 'L' : 'M'} ${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join(' '),
    class: 'dtline' + (dashed ? ' dtline-fill' : ''), stroke,
  }));
  if (benchPts.length > 1) path(benchPts.map(p => [p[0], y(p[1])]), 'var(--flat)');
  // the stock's own path breaks into solid/dashed runs at each bridged gap: an edge is "filled"
  // if either point it connects is a synthetic one, so the dashing starts and ends exactly on the
  // last real point either side, and the two styles always meet rather than leaving a visible seam
  const stockPts = rows.map((r, i) => [x(i), y(stockPct(r)), !!r.filled]);
  let run = [stockPts[0]], runFill = null;
  for (let i = 1; i < stockPts.length; i++) {
    const edgeFill = stockPts[i][2] || stockPts[i - 1][2];
    if (runFill === null) runFill = edgeFill;
    if (edgeFill !== runFill) {
      path(run, 'var(--series-1)', runFill);
      run = [stockPts[i - 1]];
      runFill = edgeFill;
    }
    run.push(stockPts[i]);
  }
  path(run, 'var(--series-1)', runFill);

  if (anchor !== dates[0]) {                              // show what the lines were tied to
    const i = xOf(anchor);
    svg.appendChild(el('line', { x1: x(i), y1: T, x2: x(i), y2: H - B, class: 'dtanchor' }));
  }

  // one marker per day, not per trade: a savings-plan top-up routinely books as two or three
  // separate orders on the same day (see Alphabet C, 2026-02-05 — one whole share, one fractional
  // remainder), and those would otherwise stack as identical, individually-unclickable circles at
  // the exact same point. Group first, so the marker underneath is always the thing you can hit.
  const byDay = new Map();
  dealsOf(d).filter(t => t.datetime.slice(0, 10) >= from).forEach(t => {
    const day = t.datetime.slice(0, 10);
    (byDay.get(day) || byDay.set(day, []).get(day)).push(t);
  });
  const markNodes = [...byDay.entries()].map(([day, trades]) => {
    const i = xOf(day);
    if (i === null) return null;
    const type = trades.every(t => t.type === trades[0].type) ? trades[0].type : 'mixed';
    const node = el('circle', {
      cx: x(i), cy: y(stockPct(rows[i])), r: 4.2, class: 'dtmark ' + type,
    });
    svg.appendChild(node);
    return { node, trades, day, close: rows[i].close, aligned: anchor === dates[i] };
  }).filter(Boolean);

  // place the end labels at their line, then push apart if they would collide, keeping both inside
  const MIN_GAP = 13;
  keys.forEach(k => { k.y = y(k.v); });
  keys.sort((a, b) => a.y - b.y);
  for (let i = 1; i < keys.length; i++)
    if (keys[i].y - keys[i - 1].y < MIN_GAP) keys[i].y = keys[i - 1].y + MIN_GAP;
  const overflow = keys[keys.length - 1].y - (H - B - 2);
  if (overflow > 0) keys.forEach(k => { k.y -= overflow; });
  const top = (T + 8) - keys[0].y;
  if (top > 0) keys.forEach(k => { k.y += top; });
  let benchKeyNode = null;
  keys.forEach(k => {
    if (Math.abs(k.y - y(k.v)) > 1.5)                    // moved: draw a leader to its line
      svg.appendChild(el('line', { x1: W - R - 1, y1: y(k.v), x2: W - R + 4, y2: k.y - 3,
                                   class: 'dtgrid' }));
    const t = el('text', { x: W - R + 6, y: k.y, class: 'dtkey', fill: k.colour });
    t.textContent = k.text;
    svg.appendChild(t);
    if (k.isBench) benchKeyNode = t;
  });

  // value bar: what the position was actually worth (euros, not per cent) on every day shown,
  // one column per date, coloured through the same red/green ramp the map's tiles use — same x
  // positions as the line chart above it (L/R/x carry over unchanged) so the two stay aligned
  // when the range buttons change which days are in view.
  const VALH = 200;
  const vals = valueOverTime(d, series, rows);
  const ath = Math.max(0, ...vals.map(v => v.cur));
  const barW = (W - L - R) / Math.max(1, dates.length - 1);
  const valueSvg = el('svg', { viewBox: `0 0 ${W} ${VALH}`, role: 'img', class: 'dtvalue',
    'aria-label': `${d.label}'s value over the same period, area by day` });
  vals.forEach((v, i) => {
    const h = ath > 0 ? Math.max(0, v.cur / ath) * VALH : 0;
    const fill = gradeColor(Math.abs(v.ret) < 0.005 ? NaN : v.ret, false);
    valueSvg.appendChild(el('rect', {
      x: x(i) - barW / 2, y: VALH - h, width: barW, height: h, fill,
    }));
  });
  // ATH is a property of the whole window and stays put; CLS and INV describe one day, so the
  // crosshair retargets them at whatever day it is over (see valueAt() in renderDetail)
  let clsLabel = null, invLabel = null;
  if (ath > 0) {
    const athLabel = el('text', { x: L, y: 12, class: 'dtaxis', 'text-anchor': 'start' });
    athLabel.textContent = `ATH ${fmtMoney(ath)}`;
    valueSvg.appendChild(athLabel);
    clsLabel = el('text', { x: L, y: 24, class: 'dtaxis', 'text-anchor': 'start' });
    valueSvg.appendChild(clsLabel);
    invLabel = el('text', { x: L, y: 36, class: 'dtaxis', 'text-anchor': 'start' });
    valueSvg.appendChild(invLabel);
  }

  // dates/stockVals/benchVals/x plus the margins: everything renderDetail needs to turn a pointer
  // position back into "which day is this" for the hover crosshair, without redoing this geometry
  return { svg, valueSvg, from: rows[0].date, anchor, stock: lastStock, bench: lastBench,
           benchLabel, benchKeyNode, marks: markNodes, dates, stockVals, benchVals, gapRuns,
           vals, clsLabel, invLabel,
           filled: rows.map(r => !!r.filled), x, L, R, T, B, H, W };
}

let DETAIL = null;                                    // { d, series, alignDate }

function renderDetail() {
  const body = document.getElementById('dtBody');
  const { d, series, alignDate, range, customRange, altBench } = DETAIL;
  document.querySelectorAll('#dtRange button').forEach(b =>
    b.classList.toggle('on', b.dataset.range === range));
  let drawn = null;
  try {
    drawn = series && drawDetail(d, series, alignDate, range, customRange, altBench);
  } catch (err) {
    body.innerHTML = `<div class="empty">chart error: ${(err && err.message) || err}</div>`;
    return;
  }
  if (!drawn) {
    const slug = seriesSlug(d);
    body.innerHTML = `<div class="empty">no data — add <code>prices/${slug || '…'}.csv</code>` +
      ` and run <code>python3 update_prices.py ${slug || '…'} --from ${TIMELINE_START}</code></div>`;
    return;
  }
  const sub = document.getElementById('dtSub');
  // built once so hovering can restore exactly this on pointerleave, instead of re-deriving it
  const subAt = (stock, bench, day, isFilled) =>
    `${d.portfolio} · 0 % at ${drawn.anchor}` +
    ` · ${d.label} ${fmtPct(stock)}` +
    (Number.isFinite(bench) ? ` · ${drawn.benchLabel} ${fmtPct(bench)}` : '') +
    (day ? ` — ${day}` : '') +
    (isFilled ? ' (no data — held flat)' : '');
  const defaultSub = subAt(drawn.stock, drawn.bench, null);
  sub.textContent = defaultSub;

  // no price history for a stretch (a thinly-traded listing Yahoo has gaps for) — the dashed
  // the dashed run on the chart already shows what this covers; just say when
  const warn = document.getElementById('dtWarn');
  warn.hidden = !drawn.gapRuns.length;
  if (drawn.gapRuns.length) {
    warn.textContent = `⚠ no price data ` +
      drawn.gapRuns.map(g => `${g.from} → ${g.to}`).join(', ');
  }

  const tip = document.createElement('div');
  tip.className = 'dt-tip';
  const crosshair = el('line', { x1: 0, y1: drawn.T, x2: 0, y2: drawn.H - drawn.B, class: 'dtcrosshair' });
  drawn.svg.appendChild(crosshair);
  body.replaceChildren(drawn.svg, drawn.valueSvg, tip);

  // clicking the benchmark's own end-label swaps it for any other position's price history — a
  // plain <select>, floated near the label like the trade tooltip is, rather than a bespoke
  // dropdown for what's fundamentally one choice from a list. Built on click, not on every
  // render, so it costs nothing when nobody uses it.
  if (drawn.benchKeyNode) {
    drawn.benchKeyNode.style.cursor = 'pointer';
    drawn.benchKeyNode.addEventListener('click', e => {
      e.stopPropagation();   // else this also reaches the svg's own click handler underneath it
      const candidates = benchmarkCandidates();
      const select = document.createElement('select');
      select.className = 'dtbenchpick';
      select.innerHTML = `<option value="">${BENCH_LABEL} (reset)</option>` +
        candidates.map(c => `<option value="${c.identifier}"` +
          `${altBench && altBench.identifier === c.identifier ? ' selected' : ''}>${c.label}</option>`
        ).join('');
      const box = drawn.benchKeyNode.getBoundingClientRect(), host = body.getBoundingClientRect();
      select.style.left = Math.max(4, box.left - host.left - 100) + 'px';
      select.style.top = (box.top - host.top - 4) + 'px';
      body.appendChild(select);
      select.focus();
      select.addEventListener('blur', () => select.remove(), { once: true });
      select.addEventListener('change', async () => {
        const val = select.value;
        select.remove();
        if (!val) { DETAIL.altBench = null; renderDetail(); return; }
        const cand = candidates.find(c => c.identifier === val);
        const s = await loadSeries(seriesSlug(cand));
        if (!s) {
          sub.textContent = `${defaultSub} — no price history for ${cand.label}`;
          return;
        }
        DETAIL.altBench = { label: cand.label, identifier: cand.identifier, series: s };
        renderDetail();
      });
    });
  }

  // hover anywhere over the plot: a vertical line at the nearest trading day, and the subheader
  // swapped to that day's numbers instead of today's — pointermove fires on the svg as a whole, so
  // this reads even while the cursor sits over a trade marker or one of the lines themselves.
  // hoverDay tracks what's currently under the crosshair so a plain click can tie the lines there
  // too, the same re-anchor a trade marker's own click already does — click is meaningless without
  // a day under it, so it's a no-op wherever pointermove last cleared this back to null.
  const { dates, stockVals, benchVals, filled, x, L, R } = drawn;
  // CLS and INV follow the crosshair; passing null puts them back on the last day in the window,
  // which is what they read when nothing is hovered
  const valueAt = i => {
    if (!drawn.clsLabel) return;
    const v = drawn.vals[i == null ? drawn.vals.length - 1 : i];
    if (!v) return;
    drawn.clsLabel.textContent = `CLS ${fmtMoney(v.cur)}`;
    drawn.invLabel.textContent = `INV ${fmtMoney(v.cost)}`;
  };
  valueAt(null);
  let hoverDay = null;
  // drag-to-zoom: pointerdown marks where a possible drag starts, pointermove past a day's width
  // turns it into one (dragMoved), pointerup on a real drag sets the custom range and redraws.
  // click still does the old "tie the lines here" — dragMoved gates which one a release means,
  // since a plain click also fires pointerdown/pointerup a few pixels apart in the same spot.
  let dragStartI = null, dragMoved = false;
  const selectBand = el('rect', { x: 0, y: drawn.T, width: 0, height: drawn.H - drawn.B - drawn.T,
                                  class: 'dtselect' });
  drawn.svg.appendChild(selectBand);
  const idxAt = vx => Math.max(0, Math.min(dates.length - 1,
    Math.round((vx - L) / (drawn.W - L - R) * (dates.length - 1))));
  const vxOf = e => {
    const rect = drawn.svg.getBoundingClientRect();
    return (e.clientX - rect.left) / rect.width * drawn.W;
  };
  drawn.svg.addEventListener('pointerdown', e => {
    const vx = vxOf(e);
    if (vx < L || vx > drawn.W - R) return;
    dragStartI = idxAt(vx);
    dragMoved = false;
    drawn.svg.setPointerCapture?.(e.pointerId);
  });
  drawn.svg.addEventListener('pointermove', e => {
    const vx = vxOf(e);
    if (dragStartI !== null) {
      const i = idxAt(vx);
      hoverDay = dates[i];   // so a click that turns out not to be a drag still has a day to use
      if (i !== dragStartI) dragMoved = true;
      const a = Math.min(dragStartI, i), b = Math.max(dragStartI, i);
      selectBand.setAttribute('x', x(a));
      selectBand.setAttribute('width', Math.max(0, x(b) - x(a)));
      selectBand.classList.add('on');
      crosshair.classList.remove('on');
      valueAt(null);
      return;
    }
    if (vx < L || vx > drawn.W - R) {
      hoverDay = null;
      crosshair.classList.remove('on');
      sub.textContent = defaultSub;
      valueAt(null);
      return;
    }
    const i = idxAt(vx);
    hoverDay = dates[i];
    crosshair.setAttribute('x1', x(i)); crosshair.setAttribute('x2', x(i));
    crosshair.classList.add('on');
    sub.textContent = subAt(stockVals[i], benchVals[i], dates[i], filled[i]);
    valueAt(i);
  });
  drawn.svg.addEventListener('pointerup', e => {
    if (dragStartI === null) return;
    const i = idxAt(vxOf(e));
    const a = Math.min(dragStartI, i), b = Math.max(dragStartI, i);
    const madeSelection = dragMoved && b > a;
    dragStartI = null;
    selectBand.classList.remove('on');
    drawn.svg.releasePointerCapture?.(e.pointerId);
    if (madeSelection) {
      DETAIL.customRange = { from: dates[a], to: dates[b] };
      renderDetail();
    }
  });
  drawn.svg.addEventListener('pointerleave', () => {
    if (dragStartI !== null) return;   // pointer capture keeps a drag going past the edge
    hoverDay = null;
    crosshair.classList.remove('on');
    sub.textContent = defaultSub;
    valueAt(null);
  });
  drawn.svg.addEventListener('click', () => {
    if (dragMoved) { dragMoved = false; return; }   // that click was the tail end of a real drag
    if (!hoverDay) return;
    DETAIL.alignDate = (DETAIL.alignDate === hoverDay) ? null : hoverDay;   // click again to reset
    renderDetail();
  });

  drawn.marks.forEach(m => {
    if (m.aligned) m.node.classList.add('on');
    m.node.addEventListener('pointerenter', () => {
      const line = t => `${num(t.shares).toLocaleString('de-DE')} × ${fmtMoney2(num(t.price))}` +
        ` = ${fmtMoney2(num(t.amount))}`;
      let head;
      if (m.trades.length === 1) {
        const t = m.trades[0];
        head = `<b>${t.type === 'buy' ? 'Buy' : 'Sell'}</b> ${m.day}<br>${line(t)}<br>`;
      } else {
        // the average is what one order at this size and price would have looked like — same
        // shares and amount as the individual trades sum to, just undivided — with each real
        // trade listed below it in grey so the split itself is still visible, not hidden by it
        const shares = m.trades.reduce((s, t) => s + num(t.shares), 0);
        const amount = m.trades.reduce((s, t) => s + num(t.amount), 0);
        head = `<b>${m.trades.length} trades</b> on ${m.day}<br>` +
          `avg ${line({ shares, amount, price: amount / shares })}<br>` +
          m.trades.map(t => `<span class="mut">${line(t)}</span>`).join('<br>') + '<br>';
      }
      tip.innerHTML = head +
        `<span class="mut">close ${fmtMoney2(m.close)} · click to tie the lines here</span>`;
      const box = m.node.getBoundingClientRect(), host = body.getBoundingClientRect();
      tip.style.left = Math.max(4, Math.min(box.left - host.left - 60, host.width - 200)) + 'px';
      tip.style.top = (box.top - host.top - 8) + 'px';
      tip.classList.add('on');
    });
    m.node.addEventListener('pointerleave', () => tip.classList.remove('on'));
    m.node.addEventListener('click', e => {
      e.stopPropagation();   // else this bubbles to the svg's own click handler and immediately
                              // un-toggles what this just set — a marker always sits exactly on a
                              // trading day, so the two handlers would otherwise agree and fight
      DETAIL.alignDate = (DETAIL.alignDate === m.day) ? null : m.day;   // click again to reset
      renderDetail();
    });
  });
}

async function openDetail(d) {
  const dlg = document.getElementById('detail');
  document.getElementById('dtTitle').textContent = d.label || d.name;
  document.getElementById('dtSub').textContent = `${d.portfolio} · ${d.name}`;
  const body = document.getElementById('dtBody');
  body.innerHTML = '<div class="empty">loading…</div>';
  if (!dlg.open) dlg.showModal();

  DETAIL = { d, series: await loadSeries(seriesSlug(d)), alignDate: null,
             range: (DETAIL && DETAIL.range) || 'buy', customRange: null, altBench: null };
  renderDetail();
}

document.getElementById('dtRange').addEventListener('click', e => {
  const btn = e.target.closest('button[data-range]');
  if (!btn || !DETAIL) return;
  DETAIL.range = btn.dataset.range;
  DETAIL.customRange = null;   // a preset button is the escape hatch out of a drag-selected zoom
  renderDetail();
});
document.getElementById('dtClose').addEventListener('click',
  () => document.getElementById('detail').close());
document.getElementById('detail').addEventListener('click', e => {
  if (e.target.id === 'detail') e.target.close();      // click the backdrop
});

/* ---------- views + controls ---------- */
let SHOW_TOKEN = 0;
const SEG_BUTTONS = { map: 'btnMap', pie: 'btnPie', positions: 'btnPositions', trades: 'btnTrades',
                      watch: 'btnWatch' };
async function show(view) {
  VIEW = view;
  STOPS = null;                          // re-read the theme once, not once per tile
  for (const [v, id] of Object.entries(SEG_BUTTONS)) {
    const btn = document.getElementById(id);
    btn.classList.toggle('on', view === v);
    btn.setAttribute('aria-pressed', view === v);
  }
  const isChart = view === 'map' || view === 'pie';
  document.getElementById('chartBody').hidden = !isChart;
  document.getElementById('keyPie').hidden = view !== 'pie';
  document.getElementById('keyMap').hidden = view !== 'map';
  document.getElementById('asOfWrap').hidden = view !== 'map';
  document.getElementById('positionsCard').hidden = view !== 'positions';
  document.getElementById('closedPositionsCard').hidden = view !== 'positions';
  document.getElementById('tradesCard').hidden = view !== 'trades';
  document.getElementById('watchCard').hidden = view !== 'watch';
  document.getElementById('tip').classList.remove('on');
  // as-of reconstruction only exists for the map — disable the actual controls (not just the
  // hidden wrapper) so they can't be triggered by a stray focus/keypress on the other frames
  ['asOfDate', 'asOfDayBack', 'asOfDayFwd', 'asOfClear'].forEach(id => {
    document.getElementById(id).disabled = view !== 'map';
  });
  document.getElementById('asOfStep').tabIndex = view === 'map' ? 0 : -1;
  if (view === 'pie') document.getElementById('closedWrap').hidden = true;
  const renderToken = ++SHOW_TOKEN;
  try {
    if (view === 'trades') {
      renderTrades();
    } else if (view === 'watch') {
      renderWatch();
    } else if (view === 'positions') {
      // the table's own numbers are always live — an as-of pick only ever affects map/pie
    } else if (view === 'pie') {
      renderPie(ITEMS);
    } else if (AS_OF) {
      const snap = await computeAsOf(AS_OF);
      if (renderToken !== SHOW_TOKEN) return;           // a newer date/view was picked meanwhile
      renderMap(snap.items, { ...snap, date: AS_OF });
    } else {
      renderMap(ITEMS);
    }
    await refreshHeader();
    if (renderToken !== SHOW_TOKEN) return;              // a newer date/view raced us here too
  } catch (err) {
    document.getElementById('err').textContent =
      'chart error: ' + (err && err.message ? err.message + ' | ' : '') + (err && err.stack || err);
    document.getElementById('loader').hidden = false;
  }
}

document.getElementById('relMode').addEventListener('change', e => {
  MODE = e.target.checked ? 'rel' : 'abs';
  applyMode(ITEMS); applyMode(CLOSED);
  redrawEverything();
});
document.getElementById('btnPie').addEventListener('click', () => show('pie'));
document.getElementById('btnMap').addEventListener('click', () => show('map'));
document.getElementById('btnPositions').addEventListener('click', () => show('positions'));
document.getElementById('btnTrades').addEventListener('click', () => show('trades'));
document.getElementById('btnWatch').addEventListener('click', () => show('watch'));
const TODAY = new Date().toISOString().slice(0, 10);
const isWeekend = dateStr => [0, 6].includes(new Date(dateStr + 'T00:00:00Z').getUTCDay());
document.getElementById('asOfDate').max = TODAY;
document.getElementById('asOfDate').value = TODAY;   // "cleared" reads as today, not blank
document.getElementById('asOfDate').addEventListener('change', e => {
  // picking today's date is the same as clearing — no point re-deriving what's already live
  AS_OF = (e.target.value && e.target.value !== TODAY) ? e.target.value : null;
  document.getElementById('asOfClear').hidden = !AS_OF;
  if (VIEW === 'map') show('map');
});
document.getElementById('asOfClear').addEventListener('click', () => {
  AS_OF = null;
  document.getElementById('asOfDate').value = TODAY;
  document.getElementById('asOfClear').hidden = true;
  if (VIEW === 'map') show('map');
});

// Scrubbing: Date's own setDate/setMonth carry overflow for us (Jan 31 + 1 day = Feb 1, no
// manual day/month-length bookkeeping needed). The value updates immediately on every step for
// a responsive field; the actual re-render is debounced, so holding a key doesn't fire a burst
// of chart rebuilds while scrubbing fast.
// read at call time, never cached: this runs at parse time, before config.json's fetch (in the
// startup chain, far below) has resolved TIMELINE_START to its real value
let asOfRenderTimer = null;
function shiftAsOf(days, months) {
  const base = new Date((AS_OF || TODAY) + 'T00:00:00Z');
  if (months) {
    // setMonth alone overflows FORWARD past the target month when the current day doesn't
    // exist there (Mar 31 − 1mo would land on Mar 3, not Feb) — clamp to that month's last day
    // instead, which is what stepping "a month back" actually means
    const day = base.getUTCDate();
    base.setUTCDate(1);
    base.setUTCMonth(base.getUTCMonth() + months);
    const lastDay = new Date(Date.UTC(base.getUTCFullYear(), base.getUTCMonth() + 1, 0)).getUTCDate();
    base.setUTCDate(Math.min(day, lastDay));
  }
  if (days) base.setUTCDate(base.getUTCDate() + days);

  // a market has no data on a weekend; land on the nearest workday in the direction we were
  // already stepping, so "back a day" from Monday reaches Friday rather than bouncing to Sunday
  const dir = Math.sign(days) || Math.sign(months) || 1;
  while (isWeekend(base.toISOString().slice(0, 10))) base.setUTCDate(base.getUTCDate() + dir);

  let iso = base.toISOString().slice(0, 10);
  if (iso > TODAY) iso = TODAY;
  if (iso < TIMELINE_START) iso = TIMELINE_START;
  document.getElementById('asOfDate').value = iso;
  AS_OF = iso === TODAY ? null : iso;
  document.getElementById('asOfClear').hidden = !AS_OF;
  clearTimeout(asOfRenderTimer);
  asOfRenderTimer = setTimeout(() => { if (VIEW === 'map') show('map'); }, 150);
}
document.getElementById('asOfDayBack').addEventListener('click', () => shiftAsOf(-1, 0));
document.getElementById('asOfDayFwd').addEventListener('click', () => shiftAsOf(1, 0));
const ASOF_KEYS = {
  ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1],
  PageUp: [0, -12], PageDown: [0, 12],
};
// on document, not the scrub wrapper: works whether that control is focused, some unrelated
// button is, or nothing is focused at all. The native date input keeps its own arrow-key segment
// spinning — anything else editable (were one ever added) gets the same courtesy.
document.addEventListener('keydown', e => {
  if (VIEW !== 'map') return;
  const tag = (e.target && e.target.tagName || '').toLowerCase();
  if (tag === 'input' || tag === 'textarea' || tag === 'select' || (e.target && e.target.isContentEditable)) return;
  const step = ASOF_KEYS[e.key];
  if (!step) return;
  e.preventDefault();
  shiftAsOf(step[0], step[1]);
});
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => { if (VIEW === 'map') show('map'); });

/* ---------- startup ---------- */
// A browser restores a checkbox's ticked state across a reload on its own, independent of this
// script re-running — read it back before anything renders, so MODE starts in sync with what the
// page actually shows rather than at the model's 'abs' default under an already-ticked box.
MODE = document.getElementById('relMode').checked ? 'rel' : 'abs';

// ingest() builds the model; this draws it. Split so the model can be exercised without a DOM.
function load(...texts) {
  ingest(...texts);
  // config.json only ever changes two labels a browser has no other way to pick up early: the
  // date picker's own native floor, and the mode-switch text baked into the static HTML
  document.getElementById('asOfDate').min = TIMELINE_START;
  document.getElementById('relModeLabel').textContent = `Performance vs. ${BENCH_LABEL}`;
  renderMeta(ITEMS, CLOSED);
  show(VIEW);
}

// Two rounds, because the benchmark's own file is not knowable until config.json (which names the
// benchmark by ISIN) and the registry (which maps that ISIN to a file) are both in hand. Only the
// positions CSV is required; every other text may come back empty and ingest() copes.
const get = (path, required) => !path ? Promise.resolve('')
  : fetch(path, { cache: 'no-store' })
      .then(r => r.ok ? r.text() : (required ? Promise.reject(new Error(r.status)) : ''))
      .catch(err => { if (required) throw err; return ''; });

Promise.all([get(CONFIG_PATH), get(INSTRUMENTS_PATH), get(PRICE_SOURCES_PATH)])
  .then(([configText, instrumentsText, sourcesText]) => Promise.all([
    configText, get(CSV_PATH, true), get(TRADES_PATH), instrumentsText, sourcesText,
    get(benchSeriesPath(configText, instrumentsText)), get(LATEST_PATH),
  ]))
  .then(texts => load(...texts))                       // ingest()'s argument order
  .catch(err => {
    document.getElementById('loader').hidden = false;
    if (err && err.message !== '404') document.getElementById('err').textContent = String(err && err.stack || err);
  });

// The fallback when the positions CSV can't be fetched: pick that one file by hand. config.json
// and the other five CSVs are simply absent — ingest() treats each missing text as empty (an
// empty table, or the built-in default settings), so the page comes up with no trades, names,
// benchmark or sectors, and the figures that need them are left out.
document.getElementById('file').addEventListener('change', e => {
  const f = e.target.files[0]; if (!f) return;
  f.text().then(t => load('', t))
    .catch(err => document.getElementById('err').textContent = String(err));
});
