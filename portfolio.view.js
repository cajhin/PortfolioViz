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
     views + controls     show(), the segmented control, the From/As-of range pickers, the keys
     startup              MODE sync, load(), and the fetch that starts it all

   AREA IS VALUE everywhere it appears — the pie's radii, the map's tiles, the realised bar's
   widths. When the as-of map shrinks, it shrinks by the square root of the value ratio for the
   same reason. Keep that true of anything new — and note that chrome costs area: any gap, inset
   or label strip you carve out of a tile has to be paid for out of the layout (squarifyNet), not
   out of the tile, or the smallest positions quietly stop being drawn to scale.
   ============================================================================================= */

/* ---------- errors ----------
   The page has one visible error slot, #err, and until now only show()'s own try/catch ever
   reached it. Anything thrown inside an event listener — a tooltip's pointerenter, a button's
   click — went to the console and nowhere else, which makes a broken hover indistinguishable from
   a hover that simply does nothing. Route uncaught errors and rejected promises here as well, one
   line per distinct message so a handler that fires on every mouse move cannot flood it. */
const SEEN_ERRORS = new Set();
function reportError(where, err) {
  const msg = `${where}: ${(err && err.message) || err}`;
  console.error(where, err);
  if (SEEN_ERRORS.has(msg)) return;
  SEEN_ERRORS.add(msg);
  const slot = document.getElementById('runtimeErr');
  if (!slot) return;
  slot.textContent = [...SEEN_ERRORS].join(' · ');
  slot.hidden = false;
}
addEventListener('error', e => reportError('uncaught', e.error || e.message));
addEventListener('unhandledrejection', e => reportError('unhandled rejection', e.reason));

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
// The symbol for a quote currency that isn't the portfolio's.
//
// Only an all-uppercase code is handed to Intl. Not every code in registry/price_sources.csv is
// ISO 4217: a London listing quotes in GBp — pence — and Intl does not reject that, it matches
// GBP case-insensitively and hands back "£". Printing a pence figure behind a pound sign would be
// wrong by a factor of a hundred, so a minor-unit code stands in for its own symbol instead. The
// same holds for ZAc, ILA and the rest of that family.
const FOREIGN_SYMBOLS = new Map();
function foreignSymbol(ccy) {
  if (!FOREIGN_SYMBOLS.has(ccy)) {
    let sym = ccy;
    if (ccy === ccy.toUpperCase()) {
      try {
        sym = new Intl.NumberFormat('de-DE', { style: 'currency', currency: ccy })
          .formatToParts(0).find(part => part.type === 'currency').value;
      } catch { /* not a currency Intl knows; the code itself reads fine */ }
    }
    FOREIGN_SYMBOLS.set(ccy, sym);
  }
  return FOREIGN_SYMBOLS.get(ccy);
}
const masked = () => `xxxx ${ccySymbol()}`;
const fmtMoney = v => SHOW_MONEY ? fmt('money0').format(v) : masked();
const fmtMoney2 = v => SHOW_MONEY ? fmt('money2').format(v) : masked();
const fmtMoneyPre = v => SHOW_MONEY ? ccySymbol() + fmt('plain2').format(v) : masked();
const fmtShare = v => fmt('percent').format(v);
const fmtPct = v => (v >= 0 ? '+' : '') + v.toFixed(1) + '%';
const fmtPP = v => (v >= 0 ? '+' : '') + v.toFixed(1) + ' pp';
const deDate = s => new Date(s).toLocaleDateString('de-DE', { day: '2-digit', month: '2-digit', year: 'numeric' });
// One close, in the portfolio currency, with the quote it was converted from beside it — the only
// place on the page the untouched number is visible. Omitted where there is nothing to add: an
// instrument that already trades in the portfolio currency, a hand-maintained file with no raw
// column, or "Show €" switched off, where revealing the figure in brackets would defeat the mask.
function fmtClose(row) {
  const main = fmtMoneyPre(row.close);
  if (!SHOW_MONEY || !(row.raw > 0) || !row.ccy || row.ccy === CCY) return main;
  return `${main} (${foreignSymbol(row.ccy)}${fmt('plain2').format(row.raw)})`;
}

// A range pick re-bases every basis figure onto its start date (see rebaseLots), so the words for
// those figures have to move with it: "invested" is no longer what the number means once the
// comparison starts mid-history. Only the map reconstructs — the pie and the two tables are always
// live — so this reads null everywhere else and the wording falls back to the lifetime one.
const rangeFrom = () => (VIEW === 'map' && AS_FROM) ? AS_FROM : null;
const rangeAt = () => (VIEW === 'map' && AS_OF) ? AS_OF : null;
const rangeTo = () => rangeAt() || TODAY;
const purLabel = short => MODE === 'rel' ? `Same money in ${BENCH_LABEL}`
  : rangeFrom() ? `Value on ${deDate(rangeFrom())}`
  : (short ? 'Purchase value' : 'Invested');
const gainLabel = () => MODE === 'rel' ? `Ahead of ${BENCH_LABEL}`
  : rangeFrom() ? `Gain since ${deDate(rangeFrom())}` : 'Unrealised gain';
// The other end of the same pair, so the two read together: "Value on 02.01.2025 → Value on
// 14.08.2026". Only where an end date was actually picked, though. Left alone, the figures are
// Parqet's own latest quotes — dated by the header's "last data update", which is a few days
// behind today — so stamping today's date on them would be a small lie for a small gain.
const curLabel = () => rangeAt() ? `Value on ${deDate(rangeAt())}` : 'Current value';

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

  const nodes = nodeMap();
  items.forEach(d => {
    const a0 = d.a0 + GAP / 2, a1 = Math.max(d.a1 - GAP / 2, d.a0 + GAP / 2 + 0.0005);
    const stroke = { stroke: 'var(--surface-1)', 'stroke-width': 0.75, 'stroke-linejoin': 'round' };

    // outer band — the gain (inside the rim) or the loss (protruding past it)
    if (d.rOut - d.rIn > 0.5) {
      const outer = el('path', {
        d: wedge(cx, cy, d.rIn, d.rOut, a0, a1),
        fill: d.state === 'loss' ? 'var(--loss-light)' : 'var(--gain-light)', ...stroke,
      });
      g.appendChild(addNode(nodes, d, outer));
    }
    // inner disc — purchase value (gainers) or current value (losers)
    const inner = el('path', {
      d: wedge(cx, cy, 0, Math.max(d.rIn, 0.5), a0, a1),
      fill: d.core, ...stroke,
    });
    g.appendChild(addNode(nodes, d, inner));

    // realized result — outermost band, green when positive, hatched red when negative
    if (d.rRel - d.rOut > 0.5) {
      const rel = el('path', {
        d: wedge(cx, cy, d.rOut, d.rRel, a0, a1),
        fill: d.rel > 0 ? 'var(--realized)' : 'var(--loss-deep)', ...stroke,
      });
      g.appendChild(addNode(nodes, d, rel));
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

  attachTip(items, nodes);

  legendPie();
  return nodes;
}

/* ---------- shared chrome: tooltip fragments and placement ----------
   The map/pie tiles and the realised bar describe the same positions from two angles, so the
   rows they have in common are built here once. Each returns HTML, or '' when the fact doesn't
   apply to this position — callers just concatenate and never test for themselves. */
// Every chart hangs its own SVG elements off the positions it draws, and more than one chart can
// be showing the same position object at once — the map and the realised bar both draw a position
// you have sold part of. A `nodes` slot on the position itself cannot hold both: whichever chart
// drew last would win, and the loser's hover would then dim elements belonging to a different
// chart while its own tiles sat untouched. Each renderer keeps its own Map instead, position →
// its elements *in that chart*, and hands it to the matching attach function.
const nodeMap = () => new Map();
const addNode = (map, d, n) => { (map.get(d) || map.set(d, []).get(d)).push(n); return n; };
const nodesOf = (map, d) => map.get(d) || [];

const posNeg = v => v >= 0 ? 'pos' : 'neg';
const tipRow = (label, value, cls) =>
  `<div class="r"><span>${label}</span><b${cls ? ` class="${cls}"` : ''}>${value}</b></div>`;
// `withPrice` puts the current quote and its date beside the name. The map and pie tooltips no
// longer want it: they carry a Purchase price / End price pair of their own further down, and the
// header quote was the same number a second time. The realised bar has no such pair, so its
// tooltip is the one place the quote still earns its space.
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

// A sparkline of the position's own price across the range now selected, drawn into the map and
// pie tooltips. Built per hover like every other line of that tooltip — the work is a filter and
// a divide over a few hundred already-parsed rows, which costs less than the string concatenation
// around it, and it is what lets the graph follow the range picker without re-rendering the map.
//
// Returns '' rather than a placeholder when there is nothing to draw: an instrument with no price
// file, or a series that simply has not arrived yet. A tooltip one row shorter is a better answer
// than an empty box where a graph should be.
// What a share cost and what it is worth: the volume-weighted price actually paid for the shares
// still held, against the close at the end of the window the tooltip describes — the same close
// the sparkline below ends on, so the figure and the line agree about which window is on screen.
//
// The two come from different places and can appear independently. The paid price is purAbs over
// the share count, both of which describe the shares held *now* (or on the range's end date), so
// it follows partial sales without a walk of its own; it carries no foreign quote because the
// trade log books in the portfolio currency and there is no original number to show. The end
// price needs a cached series and is blank until one arrives, exactly as the graph is.
function priceRows(d) {
  const rows = priceWindow(d, rangeFrom(), rangeTo());
  const paid = (d.purAbs > 0 && d.shares > 0) ? d.purAbs / d.shares : NaN;
  return (Number.isFinite(paid) ? tipRow('Purchase price', fmtMoneyPre(paid)) : '') +
         // "To" italicised to name the picker it comes from — this is the close on whatever the
         // chart bar's `to` field says, not simply the latest one on file
         (rows ? tipRow('<em>To</em> price', fmtClose(rows[rows.length - 1])) : '');
}

const SPARK = { w: 208, h: 46, pad: 3, max: 90 };
function sparkline(d) {
  try {
    return sparkSvg(d);
  } catch (err) {
    // Decoration must never take the tooltip's figures down with it. This runs inside the
    // pointerenter handler that builds tip.innerHTML, and anything thrown here would skip the
    // classList.add('on') below it — the whole popup would vanish over a graph that failed.
    reportError('sparkline', err);
    return '';
  }
}
function sparkSvg(d) {
  // rangeFrom/rangeTo, not AS_FROM/AS_OF: the pie shares this tooltip and does not reconstruct,
  // so its figures are live and its graph has to be too, or the two would describe different days
  const path = pricePath(d, rangeFrom(), rangeTo());
  if (!path) { warmSeries(d); return ''; }        // not loaded yet — have it ready for next time

  // x is keyed to the position in the *whole* window, not to the drawn point's place in the
  // strided list — so a trade marker and the line agree about where a date sits even though only
  // every nth close is actually plotted.
  const { w, h, pad } = SPARK;
  const lo = Math.min(...path.map(p => p.pct)), hi = Math.max(...path.map(p => p.pct));
  const span = hi - lo || 1;                       // a dead-flat line sits on the middle, not at 0/0
  const x = i => pad + i * (w - 2 * pad) / Math.max(1, path.length - 1);
  const y = v => h - pad - (v - lo) / span * (h - 2 * pad);

  // stride to at most SPARK.max points: seven years of daily closes is ~1800 of them, and past a
  // couple of hundred they land closer together than the line is wide. The last point is kept
  // whatever the stride lands on, so the line always ends where the figures above it say it does.
  const step = Math.ceil(path.length / SPARK.max);
  const keep = path.map((p, i) => i).filter(i => i % step === 0);
  if (keep[keep.length - 1] !== path.length - 1) keep.push(path.length - 1);
  const dAttr = keep.map((i, n) => `${n ? 'L' : 'M'}${x(i).toFixed(1)} ${y(path[i].pct).toFixed(1)}`).join(' ');

  const last = path[path.length - 1].pct;
  // the zero line only where zero is actually in view — otherwise it would sit on an edge and
  // read as the axis rather than as "where this started"
  const zero = lo <= 0 && hi >= 0
    ? `<line x1="0" y1="${y(0).toFixed(1)}" x2="${w}" y2="${y(0).toFixed(1)}" class="sparkzero"/>` : '';
  return `<div class="sparkwrap"><svg class="spark ${last >= 0 ? 'pos' : 'neg'}" ` +
    `viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" aria-hidden="true">` +
    `${zero}<path d="${dAttr}"/>${sparkMarks(d, path, x, y)}</svg></div>`;
}

// The buys and sells that happened inside the window, as dots on the line. One dot per date
// rather than per trade — a savings plan can book several on one day, and they would stack into
// an unreadable blob — coloured by what that day held: green bought, red sold, neutral for a day
// that did both. Each sits on the close the price path shows for its own date (the last close at
// or before it, so a trade booked on a holiday lands on the session the line actually draws).
function sparkMarks(d, path, x, y) {
  const first = path[0].date, last = path[path.length - 1].date;
  const byDate = new Map();
  for (const t of dealsOf(d)) {
    const day = t.datetime.slice(0, 10);
    if (day < first || day > last) continue;
    const at = byDate.get(day) || byDate.set(day, { buy: false, sell: false }).get(day);
    at[t.type === 'sell' ? 'sell' : 'buy'] = true;
  }
  if (!byDate.size) return '';
  return [...byDate].map(([day, kind]) => {
    const i = lastIndexAtOrBefore(path, day);
    if (i < 0) return '';
    const cls = kind.buy && kind.sell ? 'both' : kind.sell ? 'sell' : 'buy';
    return `<circle class="sparkmark ${cls}" cx="${x(i).toFixed(1)}" cy="${y(path[i].pct).toFixed(1)}" r="2.2"/>`;
  }).join('');
}

// Pull a position's price file into the series cache without waiting for it. The tooltip cannot
// await, so the first hover on a cold cache draws no graph and asks for the file; by the second
// it is there. renderMap also calls this for everything on screen, which in practice means the
// first hover already has it.
function warmSeries(d) {
  loadSeries(seriesSlug(d));
}

function attachTip(items, nodes) {
  const svg = document.getElementById('pie');
  const tip = document.getElementById('tip');
  // #tip lives inside #chartBody, but #chartBody isn't positioned — its containing block is
  // really .chartwrap, one level up, past the whole chart-bar header row. Using #chartBody's own
  // rect here (as this did) discounts that header's height from every placeTip() calculation,
  // rendering the tooltip that much too high — close enough to slide over the cursor that started
  // it. .closest('.chartwrap') is the same fix renderClosed() already needed for this same tip.
  const wrapEl = svg.closest('.chartwrap');
  items.forEach(d => nodesOf(nodes, d).forEach(n => {
    n.style.cursor = 'default';
    n.addEventListener('pointerenter', e => {
      tip.innerHTML =
        tipHead(d, false) +
        `<div class="pf">${d.portfolio}</div>` +
        tipRow('Share', fmtShare(d.share)) +
        tipRow(purLabel(true), fmtMoney2(d.pur)) +
        tipRow(curLabel(), fmtMoney2(d.cur)) +
        priceRows(d) +
        (d.divHeld > 0 ? tipRow('Dividends', fmtMoney2(d.divHeld), 'income') : '') +
        (d.state === 'flat' ? '' :
          tipRow(MODE === 'rel' ? 'Ahead by' : rangeFrom() ? 'Over the range' : 'Unrealised',
                 `${fmtMoney2(d.gain)} (${fmtPct(d.ret)})`, posNeg(d.gain))) +
        tipIrrRow(d, 'Annualised (CAGR)') +
        (Math.abs(d.relPre) > 0.005
          ? tipSaleRows(d) +
            tipRow('Realised (pre-tax)', fmtMoney2(d.relPre), d.relPre >= 0 ? 'realized' : 'neg')
          : '') +
        sparkline(d);
      tip.classList.add('on');
      placeTip(tip, wrapEl, e);
      items.forEach(o => nodesOf(nodes, o).forEach(m => m.style.opacity = o === d ? 1 : 0.35));
    });
    n.addEventListener('pointermove', e => placeTip(tip, wrapEl, e));
    n.addEventListener('pointerleave', () => {
      tip.classList.remove('on');
      items.forEach(o => nodesOf(nodes, o).forEach(m => m.style.opacity = 1));
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
    mk('var(--rim)', 'var(--rim)', 'Black radial line — portfolio boundary'),
  );
}

/* ---------- table + headline figures ---------- */
// Where a position's prices actually come from, compact enough for a table cell: the provider's
// initial and its own symbol, e.g. "Y-HY9H.F". The exchange suffix is kept — two listings of one
// instrument differ only by it (XNAS.DE vs IE00BMFKG444.SG), and that is exactly what this column
// exists to disambiguate. Anything with no quotable source gets a dash.
const SOURCE_LETTER = { yahoo: 'Y', manual: 'M' };
function sourceTag(d) {
  const s = SOURCES.get(d.identifier);
  if (!s) return '–';
  const letter = SOURCE_LETTER[s.source] || (s.source || '?').slice(0, 1).toUpperCase();
  return s.symbol ? `${letter}-${s.symbol}` : letter;
}
/* ---------- sortable tables ----------
   Click a header to sort, click again to reverse. One implementation for all four tables, working
   on the rendered <tr> nodes rather than on the data behind them. The tables hold different objects
   — positions, closed positions, trades, registry rows — so a comparator per column per table would
   be four times the code and four chances for a column and its comparator to drift apart. What a
   cell says is what it sorts by.

   Subtotal rows are not sorted and not moved. In the positions table they delimit the portfolio
   groups, so rows are sorted *within* each group and the subtotal stays pinned at the end of its
   own — the grouping the table is built around survives. A table without them is simply one group.

   The sort outlives a re-render: every renderer re-fills its tbody and then calls applySort, so
   flipping "Show €" or picking a date doesn't silently drop the order back to the default. */
const SORTS = new Map();                  // table id → { col, dir } while a sort is in force

// A cell's text as something orderable, or null for "no value" — a dash is the absence of a figure,
// not a small one, so those rows sit at the bottom whichever direction is asked for.
//
// Two number conventions land here and both have to parse: German (1.924,90) out of Intl, and plain
// dot-decimals (+150.1%) out of toFixed. Whichever separator appears *last* is the decimal one; with
// only dots present, a run of three-digit groups is thousands and anything else is a decimal point.
function cellValue(td) {
  const t = (td ? td.textContent : '').trim();
  if (!t || t === '\u2013' || t === '-') return null;
  if (/^\d{4}-\d{2}-\d{2}/.test(t)) return t;             // ISO dates already sort lexically
  const num = t.replace(/[^\d.,+-]/g, '');
  if (/\d/.test(num)) {
    const comma = num.lastIndexOf(','), dot = num.lastIndexOf('.');
    const plain = comma > dot ? num.replace(/\./g, '').replace(',', '.')
      : (comma < 0 && /^[+-]?\d{1,3}(\.\d{3})+$/.test(num)) ? num.replace(/\./g, '')
      : num.replace(/,/g, '');
    const v = parseFloat(plain);
    if (Number.isFinite(v)) return v;
  }
  return t.toLowerCase();
}

const isSubRow = tr => tr.className === 'sub' || (tr.classList && tr.classList.contains('sub'));

function rowSorter(col, dir) {
  return (r1, r2) => {
    const a = cellValue(r1.children[col]), b = cellValue(r2.children[col]);
    if (a === null || b === null) return a === b ? 0 : (a === null ? 1 : -1);
    if (typeof a === 'number' && typeof b === 'number') return (a - b) * dir;
    return String(a).localeCompare(String(b), 'de') * dir;
  };
}

function applySort(id) {
  const st = SORTS.get(id);
  const tbody = document.querySelector(`#${id} tbody`);
  if (!tbody) return;
  markSortHeaders(id, st);
  if (!st) return;
  const out = [], group = [];
  const flush = tail => {
    group.sort(rowSorter(st.col, st.dir));
    out.push(...group, ...(tail ? [tail] : []));
    group.length = 0;
  };
  for (const tr of [...tbody.children]) isSubRow(tr) ? flush(tr) : group.push(tr);
  flush(null);
  tbody.replaceChildren(...out);
}

function markSortHeaders(id, st) {
  const ths = document.querySelectorAll(`#${id} thead th`);
  ths.forEach((th, i) => {
    th.classList.toggle('sorted', !!st && st.col === i);
    th.classList.toggle('desc', !!st && st.col === i && st.dir < 0);
  });
}

// Wired once at startup. The first click on a column picks the direction that column is most often
// wanted in — biggest first for figures, A→Z for names — decided from the data rather than from a
// hand-kept list of which column is which; a second click reverses whatever that was.
function makeSortable(id) {
  document.querySelectorAll(`#${id} thead th`).forEach((th, i) => {
    th.classList.add('sortable');
    th.addEventListener('click', () => {
      const st = SORTS.get(id);
      let dir;
      if (st && st.col === i) dir = -st.dir;
      else {
        const first = [...document.querySelector(`#${id} tbody`).children]
          .filter(tr => !isSubRow(tr)).map(tr => cellValue(tr.children[i]))
          .find(v => v !== null);
        dir = typeof first === 'number' ? -1 : 1;
      }
      SORTS.set(id, { col: i, dir });
      applySort(id);
    });
  });
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
        `<td>${fmtMoney2(d.pur)}</td><td>${fmtMoney2(d.cur)}</td>` +
        `<td class="${cls}">${d.state === 'flat' ? '–' : fmtMoney2(d.gain)}</td>` +
        `<td class="${cls}">${d.state === 'flat' ? '–' : fmtPct(d.ret)}</td>` +
        `<td class="${d.relPre > 0 ? 'realized' : d.relPre < 0 ? 'neg' : ''}">` +
        `${Math.abs(d.relPre) > 0.005 ? fmtMoney2(d.relPre) : '–'}</td>`;
      // scoped to its own name cell, not the whole row, so the surrounding figures stay plain text
      tr.querySelector('.posname').addEventListener('click', () => openDetail(d));
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
  applySort('tbl');
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
  applySort('tblClosedPositions');
}

const TYPE_LABEL = { buy: 'Buy', sell: 'Sell', dividend: 'Dividend', fees_taxes: 'Fees/Taxes' };

// The "Now %" cell. The figure is what the price has done since the trade and reads the same way
// for every row; the colour is the verdict on the trade, which is the opposite reading for a sale.
// Buying is vindicated by a price that went up, selling by one that came down — so a sale showing
// +20% is red: those are shares you no longer hold and would rather have. Anything with no price
// of its own (a dividend, a fee) has nothing to compare and stays blank.
function nowCell(t) {
  const vs = tradeVsNow(t);
  if (!Number.isFinite(vs)) return '<td>–</td>';
  const ahead = t.type === 'sell' ? -vs : vs;
  const verdict = t.type === 'sell'
    ? `Price has ${vs >= 0 ? 'risen' : 'fallen'} ${Math.abs(vs).toFixed(1)}% since this sale — selling ${ahead >= 0 ? 'beat' : 'lost to'} holding on`
    : `Price is ${Math.abs(vs).toFixed(1)}% ${vs >= 0 ? 'above' : 'below'} what this buy paid`;
  return `<td class="${ahead >= 0 ? 'pos' : 'neg'}" title="${verdict}">${fmtPct(vs)}</td>`;
}
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
      nowCell(t) +
      `<td class="${cls}">${fmtMoney2(num(t.amount))}</td>` +
      `<td class="${cls}">${fmtMoney2(num(t.amountNet))}</td>` +
      `<td>${num(t.fee) > 0.005 ? fmtMoney2(num(t.fee)) : '–'}</td>` +
      `<td>${num(t.tax) > 0.005 ? fmtMoney2(num(t.tax)) : '–'}</td>`;
    return tr;
  }));
  applySort('tblTrades');
}

// Every registry instrument with no matching holding, open or closed — the point of a row here
// with nothing to trade against it. INSTRUMENTS carries each row under both its id and its ISIN
// (equal, for a security), so de-dupe by object identity before filtering, not the map's own size.
let WATCH_DRAWN_FOR = null;
function renderWatch() {
  if (WATCH_DRAWN_FOR === SHOW_MONEY) return;
  WATCH_DRAWN_FOR = SHOW_MONEY;
  const held = new Set([...ITEMS, ...CLOSED].map(d => d.identifier));
  const rows = [...new Set(INSTRUMENTS.values())]
    .filter(inst => !held.has(inst.id) && !held.has(inst.isin))
    .sort((a, b) => (a.display || a.name).localeCompare(b.display || b.name));
  document.getElementById('watchHead').textContent =
    `${rows.length} tracked, never held`;
  const tb = document.querySelector('#tblWatch tbody');
  tb.replaceChildren(...rows.map(inst => {
    // enough of a position-shaped object for openDetail()/sourceTag() to work on: no trades will
    // ever match tradeKey(d), so the value bar is correctly all-zero rather than wrong
    const d = { identifier: inst.id, name: inst.name, label: inst.display || inst.name,
               portfolio: 'Watchlist' };
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
  applySort('tblWatch');
}

// The stat-tile row + per-portfolio breakdown, split out of renderMeta so an as-of pick can
// refresh just this part with reconstructed figures without touching the (always-live) table.
// `opts.realizedTotal`, when given, replaces the live rel/relPre sum for the "Realised pre-tax"
// tile — as-of items carry no relPre of their own, that figure comes from computeAsOfRealized.
function renderHeaderTotals(items, closed = [], opts = {}) {
  document.getElementById('kCur').textContent = curLabel();
  document.getElementById('kPur').textContent = purLabel(false);
  document.getElementById('kGain').textContent = gainLabel();
  // a range counts only the sales booked inside it, so the tile is no longer a lifetime total
  document.getElementById('kRel').textContent =
    rangeFrom() ? 'Realised pre-tax (in range)' : 'Realised pre-tax (incl. closed)';
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
  document.getElementById('tN').textContent = items.length;

  // freshness always comes from the live data, never from an as-of pick's synthetic date
  const asOf = ITEMS.reduce((t, d) => d.lastPriceDate > t ? d.lastPriceDate : t, '');
  const toggle = document.createElement('label');
  toggle.className = 'modeswitch';
  toggle.innerHTML = `<input type="checkbox" id="showMoney"${SHOW_MONEY ? ' checked' : ''}><span>Show €</span>`;
  toggle.querySelector('input').addEventListener('change', e => {
    SHOW_MONEY = e.target.checked;
    redrawEverything();
  });
  attachCurTip(items);
  attachPurTip(all);
  document.getElementById('showEuroSlot').replaceChildren(toggle);

  // Only ever the freshness of the data — which range is on screen is the chart bar's own two
  // date fields and the note under the map to say, and saying it a third time up here left the
  // one fact this line exists for competing for the space.
  document.getElementById('dataStamp').textContent =
    asOf ? `Last data update: ${deDate(asOf)}` : '';
}

// The per-portfolio breakdown, now a hover popup over the Current value tile instead of its own
// always-visible row — reuses the same #tip element and placeTip() the map/bar tooltips use.
// Reassigning .onpointer* (rather than addEventListener) is deliberate: this tile is a static
// DOM node re-rendered against fresh `items` on every header refresh, so a plain addEventListener
// would stack a new listener — closing over stale `items` — on every as-of pick.
// The Invested tile explains itself on hover: the tile is the cost basis of what is held now, and
// the money actually put in to get there is a different, smaller number once anything has been sold
// at a profit and the proceeds redeployed. Cost is that money; Gain is what the tile has grown by
// beyond it, which is realised profit still at work.
//
// Only where the tile really is showing invested capital. In "vs. World" mode it holds the
// benchmark counterfactual, and on a range pick a market value on the start date — differencing
// either against a lifetime cash total would produce a figure that means nothing, so the popup
// says what the tile is instead of inventing one.
function attachPurTip(all) {
  const tile = document.getElementById('tilePur');
  const tip = document.getElementById('curTip');
  const wrapEl = document.getElementById('totalsCard');
  tile.onpointerenter = e => {
    // Cost is bounded to whatever date the tiles are showing, so that on a past pick it is the
    // money put in *by then* rather than by today — otherwise the value would be historic and the
    // capital current, and Total Gain would count contributions the snapshot never held.
    const end = rangeAt();
    const cost = netCapital(end ? TRADES.filter(t => t.datetime.slice(0, 10) <= end) : TRADES);
    // Invested only means invested in plain mode; the portfolio's value is what it is either way,
    // so Total Gain survives a mode the middle two rows cannot.
    const plain = MODE !== 'rel' && !rangeFrom();
    const gain = all.pur - cost, total = all.cur - cost;
    const pct = (v) => cost > 0 ? fmtPct(v / cost * 100) : '–';
    tip.innerHTML = `<div class="t">Invested vs. money put in</div>` +
      tipRow('Cost', fmtMoney2(cost)) +
      (plain
        ? tipRow('Invested', fmtMoney2(all.pur)) +
          tipRow('Gain', fmtMoney2(gain), posNeg(gain)) +
          tipRow('% Gain', pct(gain), posNeg(gain))
        : '') +
      tipRow('Total Gain', fmtMoney2(total), posNeg(total)) +
      tipRow('% Total Gain', pct(total), posNeg(total)) +
      `<div class="full">Cost is everything paid in less everything taken back out. ` +
      (plain
        ? `Invested is the cost basis of what is held now — bigger by whatever realised profit has ` +
          `been put back to work. `
        : `The tile itself is showing “${purLabel(false)}”, not invested capital, so it is left out ` +
          `of the comparison. `) +
      `Total Gain is the whole portfolio measured against that money.</div>`;
    tip.classList.add('on');
    placeTip(tip, wrapEl, e);
  };
  tile.onpointermove = e => placeTip(tip, wrapEl, e);
  tile.onpointerleave = () => tip.classList.remove('on');
}

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
    tip.innerHTML = `<div class="t">${curLabel()} by portfolio</div>` +
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
// never disagree about which dates they're showing. A start date on its own is enough to take
// this path: "since March" still needs the whole reconstruction even though the end is today.
// The pie view has no as-of rendering of its own, so it always falls back to live totals even if
// a date is still picked underneath.
async function refreshHeader() {
  if ((AS_OF || AS_FROM) && VIEW === 'map') {
    const to = AS_OF || TODAY;
    const [snap, real] = await Promise.all([computeAsOf(to, AS_FROM), computeAsOfRealized(to, AS_FROM)]);
    renderHeaderTotals(snap.items, [], { realizedTotal: real.realizedTotal });
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
function gradeColor(v) {
  const st = mapStops();
  if (!Number.isFinite(v)) return rgb(st.zero);
  const arm = v >= 0 ? st.up : st.down;
  return rgb(lerp(st.zero, arm.full, grade(v, v >= 0 ? GRADE_UP : GRADE_DOWN)));
}
const barColor = d => MODE === 'rel'
  ? gradeColor(d.ret)
  : gradeColor(Number.isFinite(d.irr) ? d.irr : d.ret);

// the bar is the result measured against the tile it sits in — the current value.
// +100% return → half the tile; −50% return → the loss equals the current value, so the whole tile.
function barShare(d) {
  if (d.state === 'flat' || d.cur <= 0) return 0;
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

// The map's sector: from the registry's own sector column (ISIN → sector). Anything the file
// doesn't cover — a new position, before it's been triaged — falls to "Other" rather than
// breaking the grouping.
const sectorOf = d => SECTORS.get(d.identifier) || 'Other';

// Fixed order so a sector's colour and position among its peers stay put across sessions — only
// the sectors that actually appear are drawn, but their relative order never depends on which
// ones happen to be present today. Anything not listed here (a sector added to the CSV later)
// still gets a colour, just hashed rather than hand-picked.
// "Cash" here is a *sector*, not the asset type that was removed — the bucket for holdings kept
// as cash equivalents rather than for a bank balance. It sits last, after Other.
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
    note.innerHTML =
      (asOf.from ? `From <b>${asOf.from}</b> to <b>${asOf.date}</b>: ` : `As of <b>${asOf.date}</b>: `) +
      `<b>${fmtMoney2(asOf.asOfTotal)}</b>` +
      ` (${(asOf.ratio * 100).toFixed(0)}% of today's ${fmtMoney(asOf.currentTotal)}) — ` +
      `area scaled to match, since area is value throughout this page. Labels stay full size.` +
      (asOf.from
        ? ` Colour is the gain <em>over the range</em>: anything already held on ${asOf.from} counts` +
          ` from what it was worth that day, anything bought since from what it cost.`
        : '') +
      (asOf.missing.length
        ? ` ${asOf.missing.length} ${asOf.from ? 'omitted for lack of price history at one end of the range' : 'held then but omitted for lack of price history'}: ${asOf.missing.join(', ')}.`
        : '');
  } else {
    svg.style.width = '100%';
    svg.style.setProperty('--map-scale', 1);
    note.hidden = true;
  }

  const g = el('g', {});
  const nodes = nodeMap();
  items.forEach(warmSeries);                             // for the tooltip's sparkline
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
      const fill = gradeColor(d.state === 'flat' ? NaN : d.ret);
      if (i === 0) firstTile = t;   // top-left tile: the sector label overlays it, below
      // no stroke on either layer — the 1px layout gap is the separator (showing the sector's
      // own background colour through), so the bar can never look wider than the tile it sits in
      // The body is the whole tile and the only thing here that takes a pointer: it carries the
      // click, the pointer cursor and — through attachTip — the tooltip. Everything drawn over it
      // below opts out with pointer-events, so the hit target is the tile as you see it rather
      // than whatever fraction of it the overlays happen to leave uncovered. Those overlays are
      // still in `nodes`, because dimming a tile has to dim all of it.
      const rect = el('rect', { x: t.x + 0.5, y: t.y + 0.5, width: w, height: h, fill,
                                class: 'maprect' });
      rect.addEventListener('click', () => openDetail(d));
      g.appendChild(addNode(nodes, d, rect));

      // income band at the head of the tile: dividends the held shares paid, against tile value
      if (d.divHeld > 0 && d.cur > 0 && h > 6) {
        const ih = Math.max(1.5, Math.min(h / 3, h * d.divHeld / d.cur));
        const band = el('rect', {
          x: t.x + 0.5, y: t.y + 0.5, width: w, height: ih, fill: 'var(--income)',
          'pointer-events': 'none',
        });
        g.appendChild(addNode(nodes, d, band));
      }

      const share = barShare(d);            // also decides the label's headroom, further down
      if (share > 0 && h > 4) {
        const bh = Math.max(1.5, h * share);
        const bar = el('rect', {
          x: t.x + 0.5, y: t.y + 0.5 + (h - bh), width: w, height: bh,
          fill: barColor(d), 'pointer-events': 'none',
        });
        g.appendChild(addNode(nodes, d, bar));
      }

      if (w > 4 && h > 7) {
        const dark = d.fund;                    // funds in black ink, everything else white
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
          sub.textContent = fmtPct(d.ret);
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
  attachTip(items, nodes);
  legendMap();
  return nodes;
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
  const has = d => Math.abs(barValue(d)) > 0.005;
  const closed = (over ? over.closed : CLOSED).filter(has);
  const open = (over ? over.open : ITEMS).filter(has);
  const divTotal = over ? over.divTotal : DIV_TOTAL;
  const taxTotal = over ? over.taxTotal : TAX_TOTAL;
  const divRows = over ? over.divRows : DIV_ROWS;
  const taxSplit = over ? over.taxSplit : TAX_SPLIT;
  const nodes = nodeMap();
  if (!closed.length && !open.length) { wrap.hidden = true; return nodes; }
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
        isDiv: true, sold: true, flows: [] }
    : null;
  const taxRow = taxTotal > 0.005
    ? { name: 'Taxes', label: 'Taxes', portfolio: 'all portfolios', relPre: -taxTotal, isTax: true, flows: [] }
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
        g.appendChild(addNode(nodes, d, rect));

        // what the shares did after the sale, read from your side — a rise since selling is a
        // loss to you, so the grade is inverted. Closed positions get it too, now that
        // gen_prices/_latest.csv supplies the price Parqet stopped publishing at the sale.
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
          g.appendChild(addNode(nodes, d, strip));
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

  attachTipClosed(ordered, nodes, { divTotal, divRows, taxTotal, taxSplit });
  return nodes;
}

function attachTipClosed(rows, nodes, ctx = null) {
  const divTotal = ctx ? ctx.divTotal : DIV_TOTAL;
  const divRows = ctx ? ctx.divRows : DIV_ROWS;
  const taxTotal = ctx ? ctx.taxTotal : TAX_TOTAL;
  const taxSplit = ctx ? ctx.taxSplit : TAX_SPLIT;
  const tip = document.getElementById('tip');
  const wrapEl = document.getElementById('closed').closest('.chartwrap');
  // dividends/taxes are synthetic rows (all portfolios, no single position behind them) — every
  // other row here is a real position, open or closed, and opens the same chart a map tile does
  const clickable = d => !d.isDiv && !d.isTax;
  rows.forEach(d => nodesOf(nodes, d).forEach(n => {
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
      rows.forEach(o => nodesOf(nodes, o).forEach(m => m.style.opacity = o === d ? 1 : 0.35));
    });
    n.addEventListener('pointermove', e => placeTip(tip, wrapEl, e));
    if (clickable(d)) n.addEventListener('click', () => openDetail(d));
    n.addEventListener('pointerleave', () => {
      tip.classList.remove('on');
      rows.forEach(o => nodesOf(nodes, o).forEach(m => m.style.opacity = 1));
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

// Every open or closed position — the pool clicking the benchmark's own label can pick
// a replacement from. Deduped by identifier: the same ISIN can appear twice (open in one
// portfolio, closed in another — POET Technologies does), and it should only offer once.
function benchmarkCandidates() {
  const seen = new Map();
  [...ITEMS, ...CLOSED].forEach(d => {
    if (!seen.has(d.identifier)) seen.set(d.identifier, d);
  });
  // every registry instrument tracked but never held gets a shot too — the same pool renderWatch()
  // lists under "Watch": comparing against a name you don't own is exactly what that tab is for
  [...new Set(INSTRUMENTS.values())]
    .filter(inst => !seen.has(inst.id) && !seen.has(inst.isin))
    .forEach(inst => seen.set(inst.id, { identifier: inst.id, label: inst.display || inst.name }));
  return [...seen.values()].sort((a, b) => a.label.localeCompare(b.label));
}

// The one picker every clickable comparison-line label (and the compare control in the footer)
// opens: a custom floated list, not a native <select> — a native dropdown only ever paints a
// handful of rows and auto-scrolls the *page* underneath it to reveal more as you move through a
// long list, which reads as slow with dozens of candidates. Every row is a plain <div> built up
// front instead, so the whole list is on screen (or one ordinary, instant div-scroll away) the
// moment it opens. onPick(value) fires with the chosen identifier, "__reset", or "__hide" — never
// at all if the picker is dismissed by clicking elsewhere.
function openStockPicker(anchorEl, { resetLabel, showHide, showAll, currentIdentifier } = {}, onPick) {
  const candidates = benchmarkCandidates();
  const body = document.getElementById('dtBody');
  const box = anchorEl.getBoundingClientRect();

  const menu = document.createElement('div');
  menu.className = 'dtpick';
  // fixed, not absolute: a <dialog> defaults to overflow:auto, and since this popup is out of
  // flow it never grows the dialog's own fit-content box — it just gets clipped to a sliver by
  // the dialog's small scrollport, with its OWN internal scrollbar standing in for the dialog's.
  // Fixed positioning is placed against the viewport directly and isn't clipped by an ancestor's
  // overflow at all, which is the standard way a floating menu escapes a scrolling container.
  menu.style.left = Math.max(4, box.left - 100) + 'px';
  const MARGIN = 8;
  const spaceBelow = innerHeight - box.bottom - MARGIN, spaceAbove = box.top - MARGIN;
  // open on whichever side has more room, and size the list to exactly what that side has —
  // no fixed cap, so a tall window shows the whole thing and a short one still uses all it's got
  if (spaceBelow >= spaceAbove) {
    menu.style.top = (box.bottom + 4) + 'px';
    menu.style.maxHeight = Math.max(60, spaceBelow - 4) + 'px';
  } else {
    menu.style.bottom = (innerHeight - box.top + 4) + 'px';
    menu.style.maxHeight = Math.max(60, spaceAbove - 4) + 'px';
  }

  const close = () => { menu.remove(); document.removeEventListener('mousedown', onOutside, true); };
  const row = (value, text, current) => {
    const r = document.createElement('div');
    r.className = 'dtpick-opt' + (current ? ' on' : '');
    r.textContent = text;
    r.addEventListener('click', () => { close(); onPick(value); });
    return r;
  };
  const rows = [];
  if (resetLabel) rows.push(row('__reset', `${resetLabel} (reset)`, false));
  if (showHide) rows.push(row('__hide', '[hide this]', false));
  rows.push(row(PORTFOLIO_ID, '[Portfolio]', currentIdentifier === PORTFOLIO_ID));
  // only where a line is being *added*: the label pickers swap one line for another, and "all"
  // has no meaning as a replacement for a single line
  if (showAll) rows.push(row(ALL_ID, '[All]', false));
  candidates.forEach(c => rows.push(row(c.identifier, c.label, currentIdentifier === c.identifier)));
  menu.replaceChildren(...rows);
  body.appendChild(menu);

  // deferred a tick so the very click that opened this menu — still bubbling toward the document
  // — doesn't immediately close it again
  const onOutside = e => { if (!menu.contains(e.target)) close(); };
  setTimeout(() => document.addEventListener('mousedown', onOutside, true), 0);
}

// The detail chart's timeframe buttons are the map's range picker, same windows under the same
// names, built straight off RANGE_PRESETS so the two cannot drift apart as one gains a button.
// Two differences, both inherent to a single-position chart:
//   • measured off the series' own last day rather than off today, so a listing whose prices stop
//     early still shows its last month instead of an empty window;
//   • "since buy", which only means anything when there is one position to have bought.
// drawDetail clamps whatever comes back to the series' first row, so a window reaching further
// back than the data simply shows all of it.
// A function, not a lookup table built up front: RANGE_PRESETS is declared with the controls far
// below, so anything evaluated here at load time would read it before it exists. Resolved per call
// instead, which costs a find over ten entries once per redraw.
function rangeStartFor(range, d, series) {
  const p = RANGE_PRESETS.find(q => q.key === range);
  if (!p) return d.firstActivity || '';               // 'buy', and anything unrecognised
  return p.from ? p.from(series.rows[series.rows.length - 1].date) : series.rows[0].date;
}

// custom, when given, is a { from, to } drag-selected window that overrides the range buttons
// entirely — both ends explicit, unlike a button's range which always runs through to today.
// One entry per comparison line beyond the position's own — the real benchmark by default (see
// DEFAULT_EXTRAS in renderDetail), plus whatever the "+" button or a swap has added. isDefaultBench
// stays on the fast in-memory BENCH/BENCH_LABEL path (no fetch, and it tracks a mid-session config
// change); anything picked by hand carries its own fetched series instead.
const EXTRA_COLOURS = ['var(--flat)', 'var(--series-2)', 'var(--series-3)', 'var(--series-4)'];

// the sentinel identifier for "the whole portfolio" as a comparison line — distinct from any real
// instrument's identifier, which is always an ISIN, so it can never collide with one
const PORTFOLIO_ID = '__portfolio';
const ALL_ID = '__all';           // "everything at once" — offered by the + compare picker only

// the "discount up vola 50%" checkbox — a view preference like SHOW_MONEY, not part of DETAIL
// itself, so it survives closing and reopening the dialog on a different position
let VOLA_DOWNSIDE = false;

function drawDetail(d, series, alignDate, range, custom, extras) {
  const W = 840, H = 300, T = 12, B = 22;
  let from, to;
  if (custom) {
    ({ from, to } = custom);
  } else {
    const pick = rangeStartFor(range, d, series);
    from = pick && pick > series.rows[0].date ? pick : series.rows[0].date;
    to = series.rows[series.rows.length - 1].date;
  }
  // The same rows the map's range picker measures from — "last close at or before" each end, not
  // "first close on or after" the start. Otherwise a window opening on a weekend or a holiday
  // begins one session late here and on time there, and "3M" quietly means two different windows
  // depending on which chart is asking. It is also the row the percentages are relative to.
  const iFrom = Math.max(0, lastIndexAtOrBefore(series.rows, from));
  const iTo = lastIndexAtOrBefore(series.rows, to);
  let rows = iTo > iFrom ? series.rows.slice(iFrom, iTo + 1) : [];
  if (rows.length < 2) return null;
  // colour is keyed by what a line IS, not by its position in the list: the neutral grey stays
  // reserved for the real benchmark specifically, so a plain stock never inherits "the benchmark's
  // colour" just because it happens to end up first after the actual benchmark gets hidden
  let extraColourAt = 0;
  const secondaries = (extras || []).map((entry, i) => ({
    label: entry.isDefaultBench ? BENCH_LABEL : entry.label,
    identifier: entry.isDefaultBench ? null : entry.identifier,
    secRows: (entry.isDefaultBench ? BENCH : entry.series.rows).filter(r => r.date >= rows[0].date),
    colour: entry.isDefaultBench ? EXTRA_COLOURS[0]
      : EXTRA_COLOURS[1 + (extraColourAt++ % (EXTRA_COLOURS.length - 1))],
    entryIndex: i,
  }));

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

  // every line is read as % against the tie point, so they are all 0 there and cross by construction
  const anchor = (alignDate && xOf(alignDate) !== null) ? dates[xOf(alignDate)] : dates[0];
  const stockAnchor = rows[xOf(anchor)];
  const stockPct = r => (r.close / stockAnchor.close - 1) * 100;
  const stockVals = rows.map(stockPct);                  // kept: the hover crosshair reads back into it

  secondaries.forEach(s => {
    const anchorRow = at(s.secRows, anchor);
    // a positive close, not just any row — the portfolio-total line (secRows can legitimately be
    // 0 before the first position was ever bought) divides by zero exactly like a missing row
    // would, so both need the same fallback below
    if (anchorRow && anchorRow.close > 0) {
      // has real, usable data back to the global tie point — read it against that, exactly like
      // every other line, so they all cross at 0% there by construction
      s.vals = dates.map(dt => {
        const r = at(s.secRows, dt);
        return r ? (r.close / anchorRow.close - 1) * 100 : null;
      });
    } else {
      // it didn't exist yet at the tie point (an IPO, a later start than the chart's range, or —
      // for the portfolio total — before the first position was bought at all) — there's nothing
      // usable there to compare it against, so instead of resetting it to a false 0% on its first
      // day, pick it up wherever the primary stock's own line already is that day: "what if this
      // had been bought instead, right when it became available" is the fair comparison, and
      // understates nothing the way a fresh 0% start would
      const first = s.secRows.find(r => r.close > 0);
      const startI = first && xOf(first.date);
      const startFrac = (startI != null) ? stockVals[startI] / 100 : 0;
      s.vals = dates.map((dt, i) => {
        if (!first || dt < first.date) return null;
        const r = at(s.secRows, dt);
        if (!r || !(r.close > 0)) return null;
        return ((1 + startFrac) * (r.close / first.close) - 1) * 100;
      });
    }
    s.last = s.vals.filter(Number.isFinite).slice(-1)[0];
  });

  // Volatility rides this chart's y-axis on a scale of its own. The two lines are in annualised
  // per cent of sigma, not per cent of price change, and reading them against the same 0% would
  // mean nothing — hence the second pair of axis labels further down, and the deliberately
  // background-ish dashing (see .dtvola) that keeps them from reading as another price line.
  //
  // Computed over series.rows — the WHOLE history, not the window on screen — so the trailing
  // window behind the left edge is made of real days. Computing it on the visible slice instead
  // would leave the annual line blank for the first year of every range, and "1y" would show no
  // annual volatility at all.
  // The 1-year rolling window alongside a vanilla EWMA (λ=0.94, RiskMetrics' own constant) — the
  // 1-month window was dropped after comparing the two: it rides a one-day spike at full strength
  // for 21 days and then drops it in a single step, drawing as a plateau with a cliff on both
  // edges, where EWMA (no window, no edge to fall off) fades the same spike out smoothly instead.
  const VOLA_SPECS = [
    { label: 'σ 1y', kind: 'window', window: 252, colour: 'var(--vola-1)', cls: '' },
    { label: 'σ ewma', kind: 'ewma', colour: 'var(--vola-3)', cls: ' dtvola-e' },
  ];
  const upWeight = VOLA_DOWNSIDE ? 0.5 : 1;
  const volas = VOLA_SPECS.map(spec => {
    const full = spec.kind === 'ewma' ? volatilityEwma(series.rows, undefined, undefined, upWeight)
                                       : volatilitySeries(series.rows, spec.window, undefined, upWeight);
    // read at the visible dates under the same "last trading day at or before" rule every other
    // line here follows, so a bridged gap holds the last real sigma flat rather than breaking
    const vals = dates.map(dt => {
      const r = at(full, dt);
      return r && Number.isFinite(r.vola) ? r.vola : null;
    });
    return { ...spec, vals, last: vals.filter(Number.isFinite).slice(-1)[0] };
  });
  const volaVals = volas.flatMap(v => v.vals.filter(Number.isFinite));
  const vLo = volaVals.length ? Math.min(...volaVals) : 0;
  const vHi = volaVals.length ? Math.max(...volaVals) : 0;

  const values = [...stockVals, ...secondaries.flatMap(s => s.vals.filter(Number.isFinite)), 0];
  const lo = Math.min(...values), hi = Math.max(...values);
  // No padding on either edge: each bound is the actual all-time low/high for the window shown —
  // the most either line, stock or benchmark, ever fell or rose — so the chart never implies more
  // room than the data has evidence for, above a peak or below a trough alike. Guard against the
  // one degenerate case a flat pair of bounds would divide by zero on: a dead-flat line.
  const yLo = lo, yHi = hi > lo ? hi : lo + 1;
  const y = v => T + (H - T - B) * (1 - (v - yLo) / (yHi - yLo));

  // Both sigma lines share ONE FIXED scale, 0-200% annualised, stretched across the chart's whole
  // height — fixed rather than fit to this window's own min/max, so a line's height means the same
  // thing on every chart: 40% sigma looks the same whether it's the calmest name in the portfolio
  // or the wildest one, which a fit-to-data scale would never give you (and would also divide by
  // zero on a dead-flat window). 200% covers everything held here short of the handful of small-
  // caps that spike past it (POET has touched 350%) — those don't get their own scale, they get a
  // flat spike drawn just above the top instead. That's real information — "this went off the top
  // of an already generous scale" — not a chart that quietly re-stretches every time one name has
  // a rough month.
  const VOLA_LO = 0, VOLA_HI = 200, VOLA_CLAMP = 210;   // CLAMP is deliberately off-scale — see above
  const volaToChart = v =>
    yLo + ((v > VOLA_HI ? VOLA_CLAMP : v) - VOLA_LO) / (VOLA_HI - VOLA_LO) * (yHi - yLo);
  const yVola = v => y(volaToChart(v));
  // One decimal or none, decided once for the whole chart rather than per label: a bond fund
  // living between 2% and 11% would otherwise print "σ 11%" against "σ 2.0%" at the other end of
  // the same axis and read as two different scales. The decimal comes out for a low or a narrow
  // range — both cases where whole points would round the whole spread away.
  const volaDp = (vHi < 10 || vHi - vLo < 5) ? 1 : 0;
  const fmtVola = v => `${v.toFixed(volaDp)}%`;

  // the left margin is whatever the widest y-axis label needs, so a big swing (a multi-bagger's
  // "+10000%") never runs past the left edge — same sizing rule as the right margin below
  const axisTexts = [0, 1, 2, 3, 4].map(i => {
    const v = yLo + (yHi - yLo) * i / 4;
    return `${v >= 0 ? '+' : ''}${Math.round(v)}%`;
  });
  const L = Math.max(34, 10 + Math.max(...axisTexts.map(t => t.length)) * 5.6);

  // the right margin is whatever the end labels need, so they can never be clipped
  const lastStock = stockVals[stockVals.length - 1];
  const keys = [{ text: `${d.label} ${fmtPct(lastStock)}`, v: lastStock, colour: 'var(--series-1)' }];
  secondaries.forEach(s => {
    if (Number.isFinite(s.last))
      keys.push({ text: `${s.label} ${fmtPct(s.last)}`, v: s.last, colour: s.colour,
                  entryIndex: s.entryIndex });
  });
  // the sigma lines are named the same way, but placed through their own scale: v is handed over
  // already in chart space, so the collision walk below treats them as just two more labels and
  // the leader lines land on the right place without knowing anything about the second unit
  volas.forEach(v => {
    if (Number.isFinite(v.last))
      keys.push({ text: `${v.label} ${fmtVola(v.last)}`, v: volaToChart(v.last), colour: v.colour });
  });
  const R = Math.min(210, 14 + Math.max(...keys.map(k => k.text.length)) * 5.6);
  const fits = Math.floor((R - 14) / 5.6);              // a very long name gets clipped, not the label
  keys.forEach(k => {
    if (k.text.length > fits) k.text = k.text.slice(0, Math.max(4, fits - 1)) + '…';
  });
  const x = i => L + (W - L - R) * (i / (dates.length - 1));
  secondaries.forEach(s => {
    s.pts = s.vals.map((v, i) => Number.isFinite(v) ? [x(i), v] : null).filter(Boolean);
  });

  const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img',
    'aria-label': `${d.label} against ${secondaries.map(s => s.label).join(', ') || 'nothing'},` +
                  ` in per cent from ${anchor}` +
                  (volaVals.length ? `, plus annualised volatility (${volas.map(v => v.label).join(', ')}),` +
                                     ` ranging ${Math.round(vLo)}% to ${Math.round(vHi)}%` : '') });

  for (let i = 0; i <= 4; i++) {
    const v = yLo + (yHi - yLo) * i / 4;
    svg.appendChild(el('line', { x1: L, y1: y(v), x2: W - R, y2: y(v), class: 'dtgrid' }));
    const t = el('text', { x: L - 6, y: y(v) + 3, class: 'dtaxis', 'text-anchor': 'end' });
    t.textContent = axisTexts[i];
    svg.appendChild(t);
  }
  svg.appendChild(el('line', { x1: L, y1: y(0), x2: W - R, y2: y(0), class: 'dtzero' }));

  // The sigma scale's two ends, on the same left axis as the % labels but stepped inward and in
  // the annual line's colour, so it reads at a glance that this axis carries a second unit and
  // which lines own it. These name the FIXED scale (0%/200%), not this window's own min/max — the
  // whole point of fixing the scale is that this axis reads the same on every chart, unlike the %
  // bounds above which move with the data.
  if (volaVals.length) {
    [[VOLA_HI, T + 15], [VOLA_LO, H - B - 7]].forEach(([v, ty]) => {
      const t = el('text', { x: L - 6, y: ty, class: 'dtvolaaxis', 'text-anchor': 'end',
                             fill: 'var(--vola-1)' });
      t.textContent = `σ ${fmtVola(v)}`;
      svg.appendChild(t);
    });
  }
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
  // sigma first, so it sits behind every price line rather than over them. A stretch with no full
  // trailing window behind it (the start of a short history) breaks the line instead of bridging
  // it — the same refusal to draw what isn't there that the dashed gap runs stand for.
  const volaPath = (pts, v) => svg.appendChild(el('path', {
    d: pts.map((p, i) => `${i ? 'L' : 'M'} ${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join(' '),
    class: 'dtvola' + v.cls, stroke: v.colour,
  }));
  volas.forEach(v => {
    let run = [];
    v.vals.forEach((val, i) => {
      if (Number.isFinite(val)) { run.push([x(i), yVola(val)]); return; }
      if (run.length > 1) volaPath(run, v);
      run = [];
    });
    if (run.length > 1) volaPath(run, v);
  });
  secondaries.forEach(s => { if (s.pts.length > 1) path(s.pts.map(p => [p[0], y(p[1])]), s.colour); });
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

  // Place the end labels at their line, then push apart if they would collide, keeping both
  // inside. MIN_GAP tracks .dtkey's font size — enough to clear the glyphs, and no more.
  //
  // It is the *preferred* gap, not a floor: "[All]" puts every tracked name on the chart at once,
  // far more labels than the height can seat at that spacing, and pushing them apart regardless
  // would run the surplus off the bottom where the viewBox clips it away entirely. Compressing to
  // whatever the chart actually has keeps every line named, which matters more than the breathing
  // room does — with few enough labels to fit, this is exactly MIN_GAP and nothing changes.
  const MIN_GAP = 9;
  const top0 = T + 8, bottom0 = H - B - 2;
  const gap = keys.length > 1 ? Math.min(MIN_GAP, (bottom0 - top0) / (keys.length - 1)) : MIN_GAP;
  keys.forEach(k => { k.y = y(k.v); });
  keys.sort((a, b) => a.y - b.y);
  for (let i = 1; i < keys.length; i++)
    if (keys[i].y - keys[i - 1].y < gap) keys[i].y = keys[i - 1].y + gap;
  const overflow = keys[keys.length - 1].y - bottom0;
  if (overflow > 0) keys.forEach(k => { k.y -= overflow; });
  const top = top0 - keys[0].y;
  if (top > 0) keys.forEach(k => { k.y += top; });
  keys.forEach(k => {
    if (Math.abs(k.y - y(k.v)) > 1.5)                    // moved: draw a leader to its line
      svg.appendChild(el('line', { x1: W - R - 1, y1: y(k.v), x2: W - R + 4, y2: k.y - 3,
                                   class: 'dtgrid' }));
    const t = el('text', { x: W - R + 6, y: k.y, class: 'dtkey', fill: k.colour });
    t.textContent = k.text;
    svg.appendChild(t);
    // fed back onto the matching secondaries[] entry, not just kept on the (sorted-by-y, so
    // reordered) keys array — renderDetail wires the click there, keyed by entryIndex
    if (k.entryIndex != null) secondaries[k.entryIndex].node = t;
  });

  // value bar: what the position was actually worth (euros, not per cent) on every day shown,
  // one column per date, coloured through the same red/green ramp the map's tiles use — same x
  // positions as the line chart above it (L/R/x carry over unchanged) so the two stay aligned
  // when the range buttons change which days are in view.
  const VALH = 100;   // half of the price chart's own height — the viewBox's aspect ratio, not a
                       // CSS cap, is what keeps this in horizontal sync with it (see .dtvalue)
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

  // dates/stockVals/secondaries/x plus the margins: everything renderDetail needs to turn a
  // pointer position back into "which day is this" for the hover crosshair, and each secondary's
  // .node back into "which slot was clicked", without redoing this geometry
  return { svg, valueSvg, from: rows[0].date, anchor, stock: lastStock, secondaries, volas,
           marks: markNodes, dates, stockVals, gapRuns, vals, clsLabel, invLabel,
           filled: rows.map(r => !!r.filled), x, L, R, T, B, H, W };
}

let DETAIL = null;                                    // { d, series, alignDate }

function renderDetail() {
  const body = document.getElementById('dtBody');
  const { d, series, alignDate, range, customRange, extras } = DETAIL;
  // a drag selection isn't any preset's range, even though DETAIL.range still names whichever
  // button was on before the drag (it's the escape hatch back out — see the drag handler below) —
  // so leave every button off while a custom window is showing, rather than a stale preset lit
  // for a range it no longer matches.
  document.querySelectorAll('#dtRange button').forEach(b =>
    b.classList.toggle('on', !customRange && b.dataset.range === range));
  let drawn = null;
  try {
    drawn = series && drawDetail(d, series, alignDate, range, customRange, extras);
  } catch (err) {
    body.innerHTML = `<div class="empty">chart error: ${(err && err.message) || err}</div>`;
    return;
  }
  if (!drawn) {
    const slug = seriesSlug(d);
    body.innerHTML = `<div class="empty">no data — add <code>gen_prices/${slug || '…'}.csv</code>` +
      ` and run <code>python3 update_prices.py ${slug || '…'} --from ${TIMELINE_START}</code></div>`;
    return;
  }
  const sub = document.getElementById('dtSub');
  const vals = document.getElementById('dtVals');
  // built once so hovering can restore exactly this on pointerleave, instead of re-deriving it
  const subAt = (day, isFilled) =>
    `${d.portfolio} · 0 % at ${drawn.anchor}` +
    (day ? ` — ${day}` : '') +
    (isFilled ? ' (no data — held flat)' : '');
  // every line on the chart reports here at the crosshair, sigma included — unsigned, since it is
  // a spread and not a change, which is also what tells the two units apart in one line of text
  const valsAt = (stock, secVals, volaVals) =>
    `${d.label} ${fmtPct(stock)}` +
    drawn.secondaries.map((s, i) =>
      Number.isFinite(secVals[i]) ? ` · ${s.label} ${fmtPct(secVals[i])}` : '').join('') +
    drawn.volas.map((v, i) =>
      Number.isFinite(volaVals[i]) ? ` · ${v.label} ${volaVals[i].toFixed(1)}%` : '').join('');
  const defaultSub = subAt(null, false);
  const defaultVals = valsAt(drawn.stock, drawn.secondaries.map(s => s.last),
                             drawn.volas.map(v => v.last));
  sub.textContent = defaultSub;
  vals.textContent = defaultVals;

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

  // clicking any comparison line's own end-label swaps it for another stock's price history, or
  // removes it — a plain <select>, floated near the label like the trade tooltip is, rather than
  // a bespoke dropdown for what's fundamentally one choice from a list. Built on click, not on
  // every render, so it costs nothing when nobody uses it.
  drawn.secondaries.forEach(s => {
    if (!s.node) return;
    s.node.style.cursor = 'pointer';
    s.node.addEventListener('click', e => {
      e.stopPropagation();   // else this also reaches the svg's own click handler underneath it
      // "was this ever the benchmark slot" has to survive a swap, or resetting back only works
      // once — isBenchSlot is set the first time and then just carried forward on every rewrite
      const cur = extras[s.entryIndex];
      const isBenchSlot = !!(cur && (cur.isDefaultBench || cur.isBenchSlot));
      openStockPicker(s.node, {
        resetLabel: isBenchSlot ? BENCH_LABEL : null,
        showHide: true,
        currentIdentifier: s.identifier,
      }, async val => {
        if (val === '__hide') { extras.splice(s.entryIndex, 1); renderDetail(); return; }
        if (val === '__reset') { extras[s.entryIndex] = { isDefaultBench: true }; renderDetail(); return; }
        if (val === PORTFOLIO_ID) {
          extras[s.entryIndex] = { label: 'Portfolio', identifier: PORTFOLIO_ID,
                                   series: await portfolioSeries(), isBenchSlot };
          renderDetail();
          return;
        }
        const cand = benchmarkCandidates().find(c => c.identifier === val);
        const ser = await loadSeries(seriesSlug(cand));
        if (!ser) { vals.textContent = `${defaultVals} — no price history for ${cand.label}`; return; }
        extras[s.entryIndex] = { label: cand.label, identifier: cand.identifier, series: ser, isBenchSlot };
        renderDetail();
      });
    });
  });

  // hover anywhere over the plot: a vertical line at the nearest trading day, and the subheader
  // swapped to that day's numbers instead of today's — pointermove fires on the svg as a whole, so
  // this reads even while the cursor sits over a trade marker or one of the lines themselves.
  // hoverDay tracks what's currently under the crosshair so a plain click can tie the lines there
  // too, the same re-anchor a trade marker's own click already does — click is meaningless without
  // a day under it, so it's a no-op wherever pointermove last cleared this back to null.
  const { dates, stockVals, secondaries, volas, filled, x, L, R } = drawn;
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
      vals.textContent = defaultVals;
      valueAt(null);
      return;
    }
    const i = idxAt(vx);
    hoverDay = dates[i];
    crosshair.setAttribute('x1', x(i)); crosshair.setAttribute('x2', x(i));
    crosshair.classList.add('on');
    sub.textContent = subAt(dates[i], filled[i]);
    vals.textContent = valsAt(stockVals[i], secondaries.map(s => s.vals[i]), volas.map(v => v.vals[i]));
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
    vals.textContent = defaultVals;
    valueAt(null);
  });
  // Click a day to tie every line to 0% there; click the marked day again to let go of it.
  //
  // Two things make that harder than it sounds, and both are why this reads the event rather than
  // comparing dates. The day comes from the click's own coordinates, not from whatever the last
  // pointermove left in hoverDay: setting the anchor re-renders, rebuilding this closure with
  // hoverDay back at null, so a second click without moving the pointer used to read null and
  // return. And the second click is matched by *position*, not by date, because the axis labels
  // change width when the anchor moves — L and R with them — which slides every day a pixel or two
  // sideways under a stationary cursor. Comparing dates would find the neighbouring day and
  // silently re-anchor, which is exactly what it looks like when clicking twice does nothing.
  // Two tests, because each covers where the other fails. Same index catches a sparse window,
  // where a month of trading days sits tens of pixels apart and a click never lands exactly on a
  // tick. Same pixel catches a dense one, where years of days are sub-pixel apart and the axis
  // shift alone is enough to move the index by one under a stationary cursor. Either counts as
  // "the day already marked"; on a sparse window the neighbouring day is far enough away in
  // pixels, and on a dense one it is not separately clickable to begin with.
  const ANCHOR_HIT = 5;                  // viewBox units, ~0.6% of the plot
  drawn.svg.addEventListener('click', e => {
    if (dragMoved) { dragMoved = false; return; }   // that click was the tail end of a real drag
    const vx = vxOf(e);
    if (vx < L || vx > drawn.W - R) return;
    const i = idxAt(vx);
    // only when an anchor is actually set: with none, drawn.anchor is just the first day in the
    // window, and treating a click there as "clear" would swallow it instead of anchoring
    const anchorI = DETAIL.alignDate ? dates.indexOf(drawn.anchor) : -1;
    const onAnchor = anchorI >= 0 && (i === anchorI || Math.abs(vx - x(anchorI)) <= ANCHOR_HIT);
    DETAIL.alignDate = onAnchor ? null : dates[i];
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
  document.getElementById('dtVals').textContent = '';
  const body = document.getElementById('dtBody');
  body.innerHTML = '<div class="empty">loading…</div>';
  if (!dlg.open) dlg.showModal();

  // extras always starts with just the real benchmark — a fresh chart shows what it always has,
  // and isDefaultBench keeps it on the fast in-memory BENCH path rather than a fetch
  DETAIL = { d, series: await loadSeries(seriesSlug(d)), alignDate: null,
             range: (DETAIL && DETAIL.range) || 'buy', customRange: null,
             extras: [{ isDefaultBench: true }] };
  renderDetail();
}

document.getElementById('dtRange').addEventListener('click', e => {
  const btn = e.target.closest('button[data-range]');
  if (!btn || !DETAIL) return;
  DETAIL.range = btn.dataset.range;
  DETAIL.customRange = null;   // a preset button is the escape hatch out of a drag-selected zoom
  renderDetail();
});
document.getElementById('volaDownside').addEventListener('change', e => {
  VOLA_DOWNSIDE = e.target.checked;
  if (DETAIL) renderDetail();
});
const addCompareBtn = document.getElementById('dtAddCompare');
addCompareBtn.addEventListener('click', () => {
  if (!DETAIL) return;
  openStockPicker(addCompareBtn, { showAll: true }, async val => {
    if (val === PORTFOLIO_ID) {
      DETAIL.extras.push({ label: 'Portfolio', identifier: PORTFOLIO_ID, series: await portfolioSeries() });
      renderDetail();
      return;
    }
    if (val === ALL_ID) {
      // Every comparison there is, drawn at once. It *replaces* the set rather than adding to it,
      // since picking it twice would otherwise draw everything a second time — and it leaves out
      // the position being viewed, which is already the chart's own line. Anything with no series
      // to load is simply absent; the chart is open and there is nowhere to report it but the
      // console. Every load is cached, so a second [All] on another position is instant.
      const cands = benchmarkCandidates().filter(c => c.identifier !== DETAIL.d.identifier);
      const [pf, ...sers] = await Promise.all(
        [portfolioSeries(), ...cands.map(c => loadSeries(seriesSlug(c)))]);
      DETAIL.extras = [{ label: 'Portfolio', identifier: PORTFOLIO_ID, series: pf }].concat(
        cands.map((c, i) => sers[i] && { label: c.label, identifier: c.identifier, series: sers[i] })
             .filter(Boolean));
      renderDetail();
      return;
    }
    const cand = benchmarkCandidates().find(c => c.identifier === val);
    if (!cand) return;
    const ser = await loadSeries(seriesSlug(cand));
    if (!ser) return;   // the chart is already open; nowhere to report "no data" but the console
    DETAIL.extras.push({ label: cand.label, identifier: cand.identifier, series: ser });
    renderDetail();
  });
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
  ['asOfDate', 'asFromDate', 'asOfDayBack', 'asOfDayFwd', 'asOfClear',
   ...RANGE_PRESETS.map(p => p.id)].forEach(id => {
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
    } else if (AS_OF || AS_FROM) {
      const to = AS_OF || TODAY;
      const snap = await computeAsOf(to, AS_FROM);
      if (renderToken !== SHOW_TOKEN) return;           // a newer date/view was picked meanwhile
      renderMap(snap.items, { ...snap, date: to });
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

// The two pickers are one range, so neither is written on its own: every handler sets AS_FROM /
// AS_OF and then calls this, which is the single place that decides what the two fields, their
// bounds and their clear buttons should say. That keeps the impossible states off the screen —
// a start after the end, or a ✕ offering to clear something already clear.
//
// Both ends read as a real date when nothing is picked, rather than one of them going blank: the
// range starts life at config.json's timelineStart → today, which is the widest window the page
// has data for. Null is still how "at the default" is stored — AS_FROM null means "at or before
// timelineStart", AS_OF null means "today" — so the cheap whole-history path is what runs until
// the user actually narrows the window.
//
// The quick-range buttons are shorthand for the start field, not a mode beside it, so nothing
// remembers which one was pressed: whichever preset's date matches what the field now holds is
// the one that lights up, and a hand-picked date that matches none of them lights none. That way
// typing a date, scrubbing the end, and clicking a button all leave the group telling the truth.
function syncAsOfControls() {
  if (AS_SPAN) AS_FROM = presetStart(AS_SPAN);    // a span re-measures itself off the end
  const from = document.getElementById('asFromDate'), to = document.getElementById('asOfDate');
  from.value = AS_FROM || TIMELINE_START;              // read now: load() has resolved it by here
  to.value = AS_OF || TODAY;
  from.min = TIMELINE_START;
  from.max = prevWeekday(AS_OF || TODAY);              // a start *at* the end is not a window
  // only a hand-picked start floors the end; a span's start gets out of the way by following it
  to.min = (!AS_SPAN && AS_FROM) ? AS_FROM : TIMELINE_START;
  to.max = TODAY;
  RANGE_PRESETS.forEach(p => {
    const btn = document.getElementById(p.id);
    btn.classList.toggle('on', p === AS_SPAN);
    btn.setAttribute('aria-pressed', p === AS_SPAN);
    btn.title = `${p.title} — from ${presetStart(p) || TIMELINE_START}`;
  });
  const unit = rangeUnit();
  document.getElementById('asOfDayBack').setAttribute('aria-label', `Previous ${unit}`);
  document.getElementById('asOfDayFwd').setAttribute('aria-label', `Next ${unit}`);
  document.getElementById('asOfStep').title =
    `Slide the whole range by one ${unit} · ← → a day · ↓ ↑ a month · PgDn/PgUp a year ` +
    `(back on ← ↓ PgDn, forward on → ↑ PgUp)`;
  document.getElementById('asOfClear').hidden = !AS_OF;
}

// `iso` moved by whole months and then days, in UTC. setUTCMonth alone overflows FORWARD past the
// target month when the current day doesn't exist there (Mar 31 − 1mo would land on Mar 3, not
// Feb) — clamping to that month's last day instead is what "a month back" actually means.
function shiftDate(iso, months, days) {
  const d = new Date(iso + 'T00:00:00Z');
  if (months) {
    const day = d.getUTCDate();
    d.setUTCDate(1);
    d.setUTCMonth(d.getUTCMonth() + months);
    const lastDay = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + 1, 0)).getUTCDate();
    d.setUTCDate(Math.min(day, lastDay));
  }
  if (days) d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}
// A market has no close on a weekend, so a start date landing on one would silently be read as
// the previous Friday anyway. Say so in the field rather than letting the two disagree.
const weekdayAtOrBefore = iso => isWeekend(iso) ? prevWeekday(iso) : iso;
const weekdayAtOrAfter = iso => isWeekend(iso) ? nextWeekday(iso) : iso;
// One weekday back — the newest start that still leaves a window with a day in it — and its
// mirror. Which of the two a stray weekend uses depends on where the edit was heading: snapping
// backwards is right for a date arrived at going back, and pins the field for one going forward.
const prevWeekday = iso => weekdayAtOrBefore(shiftDate(iso, 0, -1));
const nextWeekday = iso => weekdayAtOrAfter(shiftDate(iso, 0, 1));
const weekdaySnap = (iso, forward) => forward ? weekdayAtOrAfter(iso) : weekdayAtOrBefore(iso);

// The quick-range buttons, in the order they sit on the bar. Each is a span measured back from
// whatever the *end* currently says, not from today, so moving the end and clicking again
// re-measures the same window from there.
// One row per button on the bar, in the order they sit there. Three fields, because a range and
// the step that slides it are not always the same thing:
//   from  places the start, given wherever the end now is — null for "the whole history"
//   step  the [months, days] one ◀ ▶ click slides the window by
//   unit  what that step is called, for the buttons' tooltip and aria-labels
// For a fixed span the two agree, and one click lands on the neighbouring window. "Year to date"
// is the one that needs them apart: its start is a date on the calendar rather than a distance
// back, and stepping it by a year is what gives you the same stretch of the year before.
const RANGE_PRESETS = [
  { id: 'rngAll', key: 'all', unit: 'day', title: 'The whole history',
    from: null, step: [0, 1] },
  { id: 'rng5y', key: '5y', unit: '5 years', title: 'Five years back from the end date',
    from: to => shiftDate(to, -60, 0), step: [60, 0] },
  { id: 'rng3y', key: '3y', unit: '3 years', title: 'Three years back from the end date',
    from: to => shiftDate(to, -36, 0), step: [36, 0] },
  { id: 'rngY', key: '1y', unit: 'year', title: 'One year back from the end date',
    from: to => shiftDate(to, -12, 0), step: [12, 0] },
  // 1 January is never a trading day, so the basis lands on the previous year's last close —
  // which is what year-to-date should measure from, or the first session of January is missed
  { id: 'rngYtd', key: 'ytd', unit: 'year', title: 'From 1 January of the end date\'s year',
    from: to => to.slice(0, 4) + '-01-01', step: [12, 0] },
  { id: 'rng6m', key: '6m', unit: '6 months', title: 'Six months back from the end date',
    from: to => shiftDate(to, -6, 0), step: [6, 0] },
  { id: 'rng3m', key: '3m', unit: '3 months', title: 'Three months back from the end date',
    from: to => shiftDate(to, -3, 0), step: [3, 0] },
  { id: 'rngM', key: '1m', unit: 'month', title: 'One month back from the end date',
    from: to => shiftDate(to, -1, 0), step: [1, 0] },
  { id: 'rngW', key: '1w', unit: 'week', title: 'One week back from the end date',
    from: to => shiftDate(to, 0, -7), step: [0, 7] },
  { id: 'rngD', key: '1d', unit: 'day', title: 'One day back from the end date',
    from: to => shiftDate(to, 0, -1), step: [0, 1] },
];
const DAY = [0, 1];                  // a hand-picked window has no named width; it slides by a day
const rangeStep = () => (AS_SPAN && AS_SPAN.step) || DAY;
const rangeUnit = () => (AS_SPAN && AS_SPAN.unit) || 'day';

// Which quick range is in force, or null when the start was picked by hand. A preset is a
// *relationship* to the end date rather than a one-off assignment — move the end and a week still
// means a week, re-measured from wherever the end now sits — so it has to outlive the click that
// set it. Picking a date in the From field drops it, unless that date happens to be exactly what
// some preset would have set, in which case it is adopted and the highlight tells the truth.
let AS_SPAN = RANGE_PRESETS[0];      // "All": the whole history, which is also the page's default

// What a preset would set the start to, given where the end is now. Null means the whole history —
// which is what "All" is, and equally what a span reaching back past timelineStart collapses to.
function presetStart(p) {
  if (!p || !p.from) return null;
  const to = AS_OF || TODAY;
  const at = clampDate(weekdayAtOrBefore(p.from(to)), TIMELINE_START, prevWeekday(to));
  return at > TIMELINE_START ? at : null;
}

// A native date field's segment spinners wrap inside the segment and ignore min/max entirely:
// stepping the day down from the 1st lands on the 31st of the same month, which can easily fall
// outside the window the picker allows. So an out-of-bounds value is clamped, never rejected —
// rejecting it would leave the field showing whatever the default renders as, which is the far end
// of the range, and a step of one day would look like a jump of seven years.
const clampDate = (v, lo, hi) => !v ? null : v < lo ? lo : v > hi ? hi : v;

document.getElementById('asFromDate').addEventListener('change', e => {
  // A start on a Saturday or Sunday is measured from a weekday close regardless — every figure
  // here reads the last close at or before it — so snap the field to a real trading day rather
  // than showing a date no market ever traded on. (A market *holiday* still reads back the same
  // way, but the calendar to detect one is per-exchange and this page has no single answer, so
  // those dates stay as typed.)
  //
  // Which way it snaps has to follow the edit. A native date field's segment spinners report no
  // direction, so infer it from the date the field was already showing: stepping the day up from a
  // Friday means Monday, stepping down means Thursday. Always snapping backwards pins the field —
  // every press forward lands on Saturday and bounces straight back to the Friday it came from,
  // so the date can never be walked past a weekend at all.
  const forward = e.target.value > (AS_FROM || TIMELINE_START);
  const snapped = weekdaySnap(clampDate(e.target.value, TIMELINE_START, TODAY) || '', forward);
  // clamped down to timelineStart is exactly what "the whole history" means, so it reads as null
  const v = clampDate(snapped, TIMELINE_START, prevWeekday(AS_OF || TODAY));
  AS_FROM = (v && v > TIMELINE_START) ? v : null;
  // a hand-picked date that lands exactly where a preset would is that preset, so the buttons
  // never show a window as unnamed when it has a perfectly good name
  AS_SPAN = RANGE_PRESETS.find(q => presetStart(q) === AS_FROM) || null;
  syncAsOfControls();
  if (VIEW === 'map') show('map');
});
RANGE_PRESETS.forEach(p => document.getElementById(p.id).addEventListener('click', () => {
  AS_SPAN = p;                                         // syncAsOfControls derives AS_FROM from it
  syncAsOfControls();
  if (VIEW === 'map') show('map');
}));
document.getElementById('asOfDate').addEventListener('change', e => {
  // same clamp, same reason — and picking today is the same as clearing, no point re-deriving
  // what is already live. The floor is timelineStart rather than the start date: letting the end
  // land on or before the start is how the range is deliberately given up, handled just below.
  const v = clampDate(e.target.value, TIMELINE_START, TODAY);
  AS_OF = (v && v !== TODAY) ? v : null;
  if (AS_FROM && AS_FROM >= (AS_OF || TODAY)) AS_FROM = null;   // the end moved past the start
  syncAsOfControls();
  if (VIEW === 'map') show('map');
});
document.getElementById('asOfClear').addEventListener('click', () => {
  AS_OF = null;
  syncAsOfControls();
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
  const to0 = AS_OF || TODAY;
  const base = new Date(shiftDate(to0, months, days) + 'T00:00:00Z');

  // a market has no data on a weekend; land on the nearest workday in the direction we were
  // already stepping, so "back a day" from Monday reaches Friday rather than bouncing to Sunday
  const dir = Math.sign(days) || Math.sign(months) || 1;
  while (isWeekend(base.toISOString().slice(0, 10))) base.setUTCDate(base.getUTCDate() + dir);

  let iso = base.toISOString().slice(0, 10);
  if (iso > TODAY) iso = TODAY;
  if (iso < TIMELINE_START) iso = TIMELINE_START;
  // The whole window slides, not just its end. A span's start re-derives itself off the end in
  // syncAsOfControls, so only a hand-picked one is carried here — and carrying it by the gap in
  // whole days, rather than by re-applying the step, holds the width exactly through whatever
  // clamping the end just went through. It is deliberately not snapped to a weekday the way a
  // span's start is: a span recomputes from the end each time and so cannot drift, while a
  // repeatedly snapped gap would widen the window a little on every press.
  let from = AS_FROM;
  if (!AS_SPAN && AS_FROM) {
    const gap = (Date.parse(to0) - Date.parse(AS_FROM)) / 864e5;
    from = clampDate(shiftDate(iso, 0, -gap), TIMELINE_START, prevWeekday(iso));
    if (!(from > TIMELINE_START)) from = null;
    if (from && from >= iso) return;                   // no room left to slide into
  }
  AS_OF = iso === TODAY ? null : iso;
  if (!AS_SPAN) AS_FROM = from;
  syncAsOfControls();
  clearTimeout(asOfRenderTimer);
  asOfRenderTimer = setTimeout(() => { if (VIEW === 'map') show('map'); }, 150);
}
// ◀ ▶ slide by the width of the range now showing, so one click lands on the neighbouring
// window with no overlap. The arrow keys keep their own fixed steps — they are how you move by a
// day while a year is selected — and go through the same slide.
const slideRange = dir => { const [months, days] = rangeStep(); shiftAsOf(days * dir, months * dir); };
document.getElementById('asOfDayBack').addEventListener('click', () => slideRange(-1));
document.getElementById('asOfDayFwd').addEventListener('click', () => slideRange(1));
// [days, months] per key. One direction rule across both axes: left and down go back, right and
// up go forward — up moves the range towards today the way it raises a value anywhere else.
const ASOF_KEYS = {
  ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowDown: [0, -1], ArrowUp: [0, 1],
  PageDown: [0, -12], PageUp: [0, 12],
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
  // headers are static markup, so this is wired once rather than after every render
  ['tbl', 'tblClosedPositions', 'tblTrades', 'tblWatch'].forEach(makeSortable);
  // config.json only ever changes one thing a browser has no other way to pick up early: the date
  // pickers' own native floor — which syncAsOfControls writes, along with the rest of their state,
  // from the range as it now stands. (The mode switch's own text is fixed; see portfolio.html.)
  syncAsOfControls();
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
