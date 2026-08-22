/* =============================================================================================
   portfolio.model.js — the data and the arithmetic behind the page.

   NOTHING IN THIS FILE TOUCHES THE DOM. That is the line between the two scripts, and it is what
   lets the model be exercised headlessly (see check_portfolio.js). If a change here needs an
   element, it belongs in portfolio.view.js instead.

   Loaded first; portfolio.view.js runs after and reads what is declared here. Both are classic
   scripts sharing one global scope — no modules, no imports, no build step.

   Sections, in order:

     files                paths of config.json and the six CSVs the page reads
     state                every mutable global, and who is allowed to write it
     CSV                  parsing, and the "# key=value" header lines some files carry
     the trade log        TRADE_INDEX — one position's activities, the input to almost everything
     FIFO lots            the shared share-retirement walk every cost-basis figure is built on
     colour from name     a position's hue, derived from its own name
     XIRR                 cash flows out of the trade log, and the bisection solver over them
     price series         data_series/*.csv, and the "last close at or before" lookup
     splits               undoing Parqet's post-split restatement of historical share counts
     as of                the portfolio rebuilt as it stood on a past date
     benchmark            "what if this money had gone into the index instead" — four flavours
     dividends            attributing income to the lots that earned it
     display names        trimming legal boilerplate off a position's full name
     build                CSV rows → the position objects every renderer consumes
     ingest               all six files in, the whole model out

   Two rules the code holds to, worth keeping: a position is identified by portfolio *and*
   identifier (the same ISIN can live in two portfolios with separate histories), and money
   figures are always pre-tax unless the name says otherwise.
   ============================================================================================= */

/* ---------- files ---------- */
const CONFIG_PATH = 'config.json';   // tunable settings an agent maintains — see its own comments
const CSV_PATH = 'parqet/parqet_all_port.csv';
const TRADES_PATH = 'parqet/parqet_trades.csv';
const NAMES_PATH = 'parqet/parqet_names.csv';   // ISIN (or exact name) → short display name
const BENCH_PATH = 'data_series/benchmark.csv';   // daily closes of the benchmark fund
const PRICES_PATH = 'parqet/parqet_prices.csv';     // fresh prices where Parqet has none (closed positions)
const SECTORS_PATH = 'parqet/parqet_sectors.csv';   // hand-maintained ISIN → sector, for the map's grouping

/* ---------- state ----------
   Every mutable global on the page. Only ingest() and build() below, and the control handlers
   in portfolio.view.js, ever write them; everything else reads.

   ITEMS / CLOSED are the open and sold positions — the objects every renderer consumes.
   TRADES is the raw activity log, and the three Maps are lookup tables read straight off their
   CSVs. MODE / VIEW / AS_OF are what the three controls in the chart bar currently say; the
   view syncs MODE from the checkbox at startup, since a browser restores a checkbox's ticked
   state across a reload on its own and this script would otherwise disagree with the screen.
   TIMELINE_START / BENCH_LABEL start at sensible defaults and are overwritten by ingest() from
   config.json — kept as ordinary globals, not a nested CONFIG object, so every reader still just
   reads a plain name the way it does for everything else here. */
let ITEMS = [], CLOSED = [], TRADES = [], NAMES = new Map(), PRICES = new Map(), SECTORS = new Map(),
    BENCH = [], CCY = 'EUR',
    MODE = 'abs',                                      // 'abs' | 'rel' (vs. the benchmark)
    VIEW = new URLSearchParams(location.search).get('view') === 'pie' ? 'pie' : 'map',
    AS_OF = null,                                      // an ISO date, or null for "today"
    PF = [],                                           // [{ name }] — portfolios in draw order
    TAX_TOTAL = 0, DIV_TOTAL = 0, DIV_ROWS = [], TAX_SPLIT = { sell: 0, dividend: 0, other: 0 },
    TIMELINE_START = '2019-01-01', BENCH_LABEL = 'MSCI World';

/* ---------- CSV ---------- */
function parseCSV(text) {
  const rows = []; let row = [], field = '', q = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (q) {
      if (c === '"') { if (text[i+1] === '"') { field += '"'; i++; } else q = false; }
      else field += c;
    } else if (c === '"') q = true;
    else if (c === ',') { row.push(field); field = ''; }
    else if (c === '\n') { row.push(field); field = ''; rows.push(row); row = []; }
    else if (c !== '\r') field += c;
  }
  if (field.length || row.length) { row.push(field); rows.push(row); }
  const head = rows.shift().map(h => h.trim());
  return rows.filter(r => r.some(v => v.trim() !== ''))
             .map(r => Object.fromEntries(head.map((h, i) => [h, (r[i] ?? '').trim()])));
}
const num = v => { const n = parseFloat(v); return Number.isFinite(n) ? n : 0; };

// "# key=value" lines at the top of a file hold constants that would otherwise repeat on every row
function splitMeta(text) {
  const meta = {};
  const body = (text || '').split('\n').filter(line => {
    const m = line.match(/^\s*#\s*([\w-]+)\s*=\s*(.*)$/);
    if (m) { meta[m[1]] = m[2].trim(); return false; }
    return true;
  }).join('\n');
  return { meta, body };
}

/* ---------- the trade log, per position ----------
   Nearly every figure on this page is a replay of one position's own trades, and before this
   index each of the dozen or so helpers that need them re-scanned and re-sorted the whole log
   for every position — quadratic in a file that only grows. Built once by load(); a position is
   keyed by portfolio *and* identifier, never identifier alone, since the same ISIN can be held
   in two portfolios (and closed in one of them) with entirely separate histories. */
let TRADE_INDEX = new Map();
const tradeKey = d => d.portfolio + '|' + d.identifier;
function indexTrades() {
  TRADE_INDEX = new Map();
  for (const t of TRADES) {
    if (!t.identifier) continue;
    const k = tradeKey(t);
    let rows = TRADE_INDEX.get(k);
    if (!rows) TRADE_INDEX.set(k, rows = []);
    rows.push(t);
  }
  for (const rows of TRADE_INDEX.values()) rows.sort((a, b) => a.datetime < b.datetime ? -1 : 1);
}
const NO_TRADES = [];
// oldest first. The array is shared — filter or map it, never sort or splice it in place.
const tradesOf = d => TRADE_INDEX.get(tradeKey(d)) || NO_TRADES;
const isDeal = t => t.type === 'buy' || t.type === 'sell';
// the buys and sells alone — a fresh array, safe to rewrite (the split rescaling does)
const dealsOf = d => tradesOf(d).filter(isDeal);

// Account-wide dividend and tax figures over whatever slice of the log is handed in — the whole
// thing for the live view, everything up to a date for an as-of pick. One pass: these used to be
// six separate filter+reduce sweeps written out twice.
function incomeAndTax(rows) {
  const divRows = [], taxSplit = { sell: 0, dividend: 0, other: 0 };
  let divTotal = 0, taxTotal = 0;
  for (const t of rows) {
    const tax = num(t.tax);
    taxTotal += tax;
    taxSplit[t.type === 'sell' || t.type === 'dividend' ? t.type : 'other'] += tax;
    if (t.type === 'dividend') { divRows.push(t); divTotal += num(t.amountNet); }
  }
  return { divTotal, divRows, taxTotal, taxSplit };
}

/* ---------- FIFO lots ----------
   Shares leave a position oldest-first, and every cost-basis figure here — what a sale realised,
   what is still held, what the same money would have made in the index — is some walk over the
   surviving lots. These two helpers are that walk; the callers differ only in what they record
   as each lot is retired. */
// Retire `shares` from the front of `lots`, reporting each slice to `onTake(lot, taken)`.
function fifoTake(lots, shares, onTake) {
  let sh = shares;
  while (sh > 1e-9 && lots.length) {
    const take = Math.min(sh, lots[0].shares);
    if (onTake) onTake(lots[0], take);
    lots[0].shares -= take; sh -= take;
    if (lots[0].shares <= 1e-9) lots.shift();
  }
}
// The buy lots still held after every sale up to `until` has been retired. `priceOf` says what a
// lot's unit cost means: net amount per share for anything money-based, the booked trade price
// where the pre-split scale is the point (splitFactor).
const NET_PER_SHARE = t => num(t.amountNet) / num(t.shares);
function survivingLots(deals, { until = '9999-99-99', priceOf = NET_PER_SHARE } = {}) {
  const lots = [];
  for (const t of deals) {
    if (t.datetime.slice(0, 10) > until) break;        // sorted — nothing after matters
    const sh = num(t.shares);
    if (!sh) continue;
    if (t.type === 'buy') lots.push({ shares: sh, price: priceOf(t), at: t.datetime });
    else fifoTake(lots, sh);
  }
  return lots;
}
const lotShares = lots => lots.reduce((t, l) => t + l.shares, 0);
const lotCost = lots => lots.reduce((t, l) => t + l.shares * l.price, 0);

/* ---------- colour from name ---------- */
const HUE_FROM = 190, HUE_TO = 280;   // cyan → violet, keyed by the first two letters of the name

function nameHue(name) {
  const letters = (name || '').toLowerCase().replace(/[^a-z]/g, '') + 'aa';
  const a = letters.charCodeAt(0) - 97, b = letters.charCodeAt(1) - 97;
  const t = Math.min(1, Math.max(0, (a * 26 + b) / (26 * 26 - 1)));
  return HUE_FROM + t * (HUE_TO - HUE_FROM);
}

/* ---------- XIRR from the trade list ---------- */
const YEAR_MS = 365.2425 * 864e5;
const SIGN = { buy: -1, transfer_in: -1, fees_taxes: -1, sell: +1, dividend: +1, interest: +1, transfer_out: +1 };

// cash flows for one position: money out on buys and costs, money in on sells and dividends,
// plus what the position is worth today as a final inflow while it is still open.
function cashFlows(d) {
  const flows = tradesOf(d)
    .map(t => ({ t: Date.parse(t.datetime), v: (SIGN[t.type] ?? 0) * num(t.amountNet) }))
    .filter(f => Number.isFinite(f.t) && f.v);
  if (!flows.length) return [];
  if (!d.sold && d.cur > 0) flows.push({ t: Date.now(), v: d.cur });
  return flows.sort((a, b) => a.t - b.t);
}

// annualised money-weighted return: the rate that discounts every flow back to zero.
// Bisection — slow but unconditionally stable, and there are only a few dozen positions.
function xirr(flows) {
  if (flows.length < 2) return NaN;
  const t0 = flows[0].t;
  const npv = r => flows.reduce((s, f) => s + f.v / (1 + r) ** ((f.t - t0) / YEAR_MS), 0);
  let lo = -0.9999, hi = 100;
  let flo = npv(lo), fhi = npv(hi);
  if (!Number.isFinite(flo) || !Number.isFinite(fhi) || flo * fhi > 0) return NaN;
  // stop once the bracket is far below what the page shows (one decimal of a per-cent) rather
  // than running a fixed count past the point where doubles have anything left to converge
  for (let i = 0; i < 100 && hi - lo > 1e-9; i++) {
    const mid = (lo + hi) / 2, fm = npv(mid);
    if (flo * fm <= 0) { hi = mid; fhi = fm; } else { lo = mid; flo = fm; }
  }
  return ((lo + hi) / 2) * 100;
}

/* ---------- price series ---------- */
const SERIES_CACHE = new Map();
const seriesSlug = d => (NAMES.get('series:' + d.identifier) ||
  (d.label || d.name || '').toLowerCase().replace(/[^a-z0-9]+/g, ''));

async function loadSeries(slug) {
  if (!slug) return null;
  if (SERIES_CACHE.has(slug)) return SERIES_CACHE.get(slug);
  let out = null;
  try {
    const r = await fetch(`data_series/${slug}.csv`, { cache: 'no-store' });
    if (r.ok) {
      const file = splitMeta(await r.text());
      const rows = parseCSV(file.body)
        .map(x => ({ date: x.date, close: num(x.close) }))
        .filter(x => x.date && x.close > 0)
        .sort((a, b) => a.date < b.date ? -1 : 1);
      if (rows.length) out = { rows, meta: file.meta };
    }
  } catch { /* no series for this position */ }
  SERIES_CACHE.set(slug, out);
  return out;
}

/* ---------- "nearest trading day" lookup ----------
   Markets are shut on weekends and holidays, so every date this page is asked about has to fall
   back to the last close at or before it. One binary search over any date-sorted {date, …} array
   serves the position series, the benchmark and the detail chart alike. */
function lastIndexAtOrBefore(rows, date) {
  let lo = 0, hi = rows.length - 1, found = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (rows[mid].date <= date) { found = mid; lo = mid + 1; } else hi = mid - 1;
  }
  return found;                                        // -1 when the array starts after `date`
}
const lastAtOrBefore = (rows, date) => rows[lastIndexAtOrBefore(rows, date)] || null;
const seriesCloseAt = (rows, date) => (lastAtOrBefore(rows, date) || {}).close ?? null;

/* ---------- splits ---------- */
// A split mid-way through a *closed* position's own trade history — buys at the old scale,
// sells at the new one (State Street SPDR ACWI IMI did this: ~€185–232 buys through 2025, then
// both sells in 2026 at ~€10.3, a 25:1 split with nothing in between to flag it). splitFactor()
// only looks at positions still open, so this needs its own detector.
//
// The ratio comes from share-count reconciliation, not price: a fully closed position's total
// bought and total sold shares must be equal once both are on the same scale, so any clean
// multiple between them (2266.7 sold vs 90.7 bought here — exactly 25.0) is the split ratio,
// full stop. Comparing prices instead (the first version of this) got fooled: the ~10-month gap
// between the last buy and the first sell let real market movement masquerade as part of the
// split, snapping to 20 instead of the true 25. Boundary — which trades need rescaling — is
// still found from price, but only to place a already-known-correct ratio, not to derive it.
function detectMidHistorySplit(trades) {
  const buys = trades.filter(t => t.type === 'buy'), sells = trades.filter(t => t.type === 'sell');
  const bought = buys.reduce((s, t) => s + num(t.shares), 0);
  const sold = sells.reduce((s, t) => s + num(t.shares), 0);
  if (!buys.length || !sells.length || !bought || !sold) return null;
  const ratio = sold / bought;
  const snap = SPLIT_RATIOS.reduce((best, c) => Math.abs(ratio - c) < Math.abs(ratio - best) ? c : best, 1);
  if (snap === 1 || Math.abs(ratio - snap) / snap > 0.08) return null;

  const preAvg = buys.reduce((s, t) => s + num(t.price), 0) / buys.length;
  const postAvg = preAvg / snap;
  const at = trades.findIndex(t => Math.abs(num(t.price) - postAvg) < Math.abs(num(t.price) - preAvg));
  return { at: at === -1 ? trades.length : at, ratio: snap };
}

// This position's buys and sells with every share count restated onto today's scale.
// data_series prices come from an external provider that split-adjusts its whole history
// uniformly, so share counts fed against them must be on that same current-day scale for every
// date, not whatever scale they were actually traded at. d.split already carries the ratio for an
// open position (detected from today's Parqet-restated cost basis); a closed one needs its own
// scan, since there is no "today" position left to compare against.
function splitAdjustedDeals(d) {
  const deals = dealsOf(d);
  if (!d.sold && d.split !== 1)
    return deals.map(t => ({ ...t, shares: num(t.shares) * d.split }));
  if (d.sold && deals.length > 1) {
    const split = detectMidHistorySplit(deals);
    if (split) return deals.map((t, i) =>
      i < split.at ? { ...t, shares: num(t.shares) * split.ratio } : t);
  }
  return deals;
}

// What the same lots' money would be worth had it gone into the benchmark instead, valued as of
// dateStr rather than today — the as-of counterpart of benchAlternative(), which is always
// "now". Same FIFO lots computeAsOf already built, just priced through the index at this date.
function benchValueOfLots(lots, dateStr) {
  const atDate = benchClose(dateStr);
  if (!Number.isFinite(atDate)) return NaN;
  let total = 0;
  for (const lot of lots) {
    const buyClose = benchClose(lot.at);
    if (!Number.isFinite(buyClose) || buyClose <= 0) return NaN;
    total += lot.shares * lot.price * atDate / buyClose;
  }
  return total;
}

// The as-of counterpart of the live map's per-position IRR: same bisection solver, cash flows
// built from these lots' buy dates instead of the full trade history, valued at dateStr instead
// of today. Without this the foot-bar had nothing but d.ret to shade itself with — the same
// number the tile body already uses — so bar and tile rendered as the same colour and the bar
// was only findable by hovering for the tooltip's numbers, not by looking at it.
function asOfIrr(lots, cur, dateStr) {
  if (!lots.length || !(cur > 0)) return NaN;
  const flows = lots.map(l => ({ t: Date.parse(l.at), v: -(l.shares * l.price) }));
  flows.push({ t: Date.parse(dateStr + 'T00:00:00Z'), v: cur });
  flows.sort((a, b) => a.t - b.t);
  return xirr(flows);
}

/* ---------- as of: the portfolio on a past date ---------- */
// The portfolio as it stood on a past date: replay each position's trades up to that day (FIFO,
// same walk as the split/dividend helpers) to get shares actually held, price them from that
// position's own data_series file, and value the remaining cost at what was actually paid. Cash
// and any position lacking a series that day are left out and named in the result, since neither
// can be honestly reconstructed from what this page has on hand.
const AS_OF_CACHE = new Map();
async function computeAsOf(dateStr) {
  const cacheKey = dateStr + '|' + MODE;                // re-picking "vs. World" must not hit a stale cache
  if (AS_OF_CACHE.has(cacheKey)) return AS_OF_CACHE.get(cacheKey);

  const source = [...ITEMS, ...CLOSED].filter(d => !d.cash);

  // the lot replay is cheap and synchronous; do it first so only positions actually held on
  // this date ever trigger a fetch, then load every needed series in parallel (each cached
  // after the first request, so re-picking a date, or reopening the map later, is instant)
  const held = source.map(d => {
    const lots = survivingLots(splitAdjustedDeals(d), { until: dateStr });
    const sharesAtD = lotShares(lots);
    const costAtD = lotCost(lots);
    // "Performance vs. MSCI World": the same lots' cost, grown in the index instead, up to this
    // date — same measure the live map uses, just bounded to dateStr rather than today
    const benchAtD = MODE === 'rel' && sharesAtD > 1e-9 ? benchValueOfLots(lots, dateStr) : NaN;
    return { d, sharesAtD, costAtD, benchAtD, lots };
  }).filter(h => h.sharesAtD > 1e-9);                   // not yet bought, or already sold out, by then

  const seriesFor = new Map(await Promise.all(
    held.map(async h => [h.d, await loadSeries(seriesSlug(h.d))])));

  const out = [];
  const missing = new Set();
  for (const { d, sharesAtD, costAtD, benchAtD, lots } of held) {
    const series = seriesFor.get(d);
    const closeAtD = series && seriesCloseAt(series.rows, dateStr);
    if (!series || closeAtD == null) { missing.add(d.label || d.name); continue; }

    // anchor to Parqet's own last known price, so "today" reproduces the live map exactly
    const anchorClose = seriesCloseAt(series.rows, d.lastPriceDate) ||
      series.rows[series.rows.length - 1].close;
    const factor = (d.lastPrice > 0 && anchorClose > 0) ? d.lastPrice / anchorClose : 1;

    const cur = sharesAtD * closeAtD * factor;
    // in "vs. World" mode, fall back to actual cost for any lot the benchmark couldn't price —
    // never silently drop a position just because the index comparison came up short
    const pur = Number.isFinite(benchAtD) ? benchAtD : costAtD;
    const gain = cur - pur;
    out.push({
      portfolio: d.portfolio, name: d.name, label: d.label, identifier: d.identifier,
      core: d.core, cash: false, fund: d.fund, shares: sharesAtD,
      cur, pur, gain, ret: pur > 0 ? gain / pur * 100 : 0,
      state: pur <= 0 || Math.abs(gain) < 0.005 ? 'flat' : (gain > 0 ? 'gain' : 'loss'),
      irr: asOfIrr(lots, cur, dateStr), divHeld: 0, firstActivity: d.firstActivity,
      lastPrice: closeAtD * factor, lastPriceDate: dateStr,
    });
  }

  const grand = out.reduce((t, x) => t + x.cur, 0);
  out.forEach(x => { x.share = grand > 0 ? x.cur / grand : 0; });
  out.sort((a, b) => b.cur - a.cur);

  const currentTotal = ITEMS.reduce((t, x) => t + x.cur, 0);   // today's true total, incl. cash
  const result = {
    items: out, asOfTotal: grand, currentTotal,
    ratio: currentTotal > 0 ? grand / currentTotal : 1,
    missing: [...missing],
  };
  AS_OF_CACHE.set(cacheKey, result);
  return result;
}

// The realised side of the same as-of pick: every sell (and its dividends/taxes) booked on or
// before dateStr, split into "open" (some shares still held on that date) and "closed" (fully
// sold by then) so renderClosed can draw the same open|closed bar it draws for today, just
// bounded to what had actually happened by dateStr. Unlike computeAsOf, this needs no split
// rescaling: it never compares against the external price series, only trades' own booked
// amounts, and those net out correctly on their own historical share scale regardless of any
// later split.
const AS_OF_REALIZED_CACHE = new Map();
async function computeAsOfRealized(dateStr) {
  const cacheKey = dateStr + '|' + MODE;
  if (AS_OF_REALIZED_CACHE.has(cacheKey)) return AS_OF_REALIZED_CACHE.get(cacheKey);

  const source = [...ITEMS, ...CLOSED].filter(d => !d.cash);
  const rows = [];
  for (const d of source) {
    const trades = dealsOf(d);
    const lots = [];
    let realized = 0, taxSell = 0, costSold = 0, soldShares = 0, grossProceeds = 0;
    let lastSell = '', sellCount = 0, activityCount = 0, benchProceeds = 0, benchKnown = true;
    for (const t of trades) {
      if (t.datetime.slice(0, 10) > dateStr) break;      // sorted — nothing after matters
      const sh0 = num(t.shares);
      if (!sh0) continue;
      activityCount++;
      if (t.type === 'buy') { lots.push({ shares: sh0, price: num(t.amountNet) / sh0, at: t.datetime }); continue; }
      let costOut = 0;
      fifoTake(lots, sh0, (lot, take) => {
        costOut += take * lot.price;
        if (MODE === 'rel') {
          const bBuy = benchClose(lot.at), bSell = benchClose(t.datetime);
          if (Number.isFinite(bBuy) && bBuy > 0 && Number.isFinite(bSell)) {
            benchProceeds += take * lot.price * bSell / bBuy;
          } else benchKnown = false;
        }
      });
      costSold += costOut;
      realized += (num(t.amount) - num(t.fee)) - costOut;
      taxSell += num(t.tax);
      soldShares += sh0;
      grossProceeds += num(t.amount);
      sellCount++;
      if (t.datetime > lastSell) lastSell = t.datetime;
    }
    if (!sellCount) continue;                            // nothing realised on this position yet

    rows.push({
      portfolio: d.portfolio, name: d.name, label: d.label, identifier: d.identifier,
      relPre: realized, invested: costSold,
      alpha: (MODE === 'rel' && benchKnown) ? grossProceeds - benchProceeds : NaN,
      sold: lotShares(lots) <= 1e-9,
      soldShares, grossProceeds, lastSell, sellCount,
      sellPrice: soldShares > 0 ? grossProceeds / soldShares : NaN,
      split: d.split ?? 1, taxSell, divSold: 0, sinceKnown: false, flows: [], activityCount,
      cash: false,
    });
  }

  const result = {
    open: rows.filter(d => !d.sold), closed: rows.filter(d => d.sold),
    ...incomeAndTax(TRADES.filter(t => t.datetime.slice(0, 10) <= dateStr)),
    realizedTotal: rows.reduce((t, d) => t + d.relPre, 0),
  };
  AS_OF_REALIZED_CACHE.set(cacheKey, result);
  return result;
}

/* ---------- benchmark ---------- */
// The counterfactual: money taken out of a position on its sale date, put into the benchmark fund
// instead, and left there. Anything a sale "saved" only counts if it beat that.
const benchClose = iso => {
  if (!BENCH.length || !iso) return NaN;
  // a buy older than the index file itself is priced at its first close rather than dropped —
  // the comparison is then optimistic by however much the index moved before then, but present
  return (lastAtOrBefore(BENCH, iso.slice(0, 10)) || BENCH[0]).close;
};
const benchNow = () => BENCH.length ? BENCH[BENCH.length - 1].close : NaN;
// BENCH_LABEL itself lives in the state block above, since ingest() overwrites it from config.json

// The counterfactual for a position you still hold: only the lots that survived, each mirrored in
// the benchmark from its own buy date. Realised results are the bar's business, so sold lots leave
// here — every euro is counted exactly once across the two views.
// TODO: dividends and fee/tax bookings are ignored entirely; they belong to neither side yet.
function benchAlternative(d) {
  if (!BENCH.length || d.cash) return NaN;
  const deals = dealsOf(d);
  if (!deals.length) return NaN;
  const lots = survivingLots(deals);
  return lots.length ? benchValueOfLots(lots, BENCH[BENCH.length - 1].date) : NaN;
}

// What each sale actually made, measured against the index instead of against cost: proceeds minus
// what the sold lots' own money would have grown to in the benchmark between buying and selling.
function realisedAlpha(d) {
  if (!BENCH.length || d.cash) return NaN;
  const lots = [];
  let alpha = 0, sold = false, unpriced = false;
  for (const t of dealsOf(d)) {
    const sh = num(t.shares);
    if (!sh) continue;
    if (t.type === 'buy') { lots.push({ shares: sh, price: NET_PER_SHARE(t), at: t.datetime }); continue; }
    sold = true;
    const sellIdx = benchClose(t.datetime);
    let indexed = 0;
    fifoTake(lots, sh, (lot, take) => {
      const buyIdx = benchClose(lot.at);
      if (!Number.isFinite(buyIdx) || buyIdx <= 0 || !Number.isFinite(sellIdx)) unpriced = true;
      else indexed += take * lot.price * sellIdx / buyIdx;
    });
    if (unpriced) return NaN;                                // no honest comparison to be made
    alpha += num(t.amount) - indexed;                        // gross proceeds, pre-tax
  }
  return sold ? alpha : NaN;
}

// Same schedule, same euros, but in the benchmark: units bought and sold on each flow date, valued
// today. Feeding that terminal value back through XIRR gives the index's money-weighted return for
// this position's timing — the thing a position's own IRR should be judged against.
function benchIrrOf(flows) {
  const now = benchNow();
  if (!Number.isFinite(now) || flows.length < 2) return NaN;
  const body = flows.slice(0, -1);                       // drop the terminal value
  let units = 0;
  for (const f of body) {
    const at = benchClose(new Date(f.t).toISOString());
    if (!Number.isFinite(at) || at <= 0) return NaN;
    units += -f.v / at;                                  // outflow buys units, inflow sells them
  }
  return xirr([...body, { t: flows[flows.length - 1].t, v: units * now }]);
}

// what the sale proceeds would be worth today had each tranche gone into the benchmark that day
function benchValue(sells) {
  const now = benchNow();
  if (!Number.isFinite(now)) return NaN;
  let total = 0;
  for (const t of sells) {
    const at = benchClose(t.datetime);
    if (!Number.isFinite(at) || at <= 0) return NaN;
    total += num(t.amount) * now / at;
  }
  return total;
}

/* ---------- dividends ---------- */
// A dividend belongs to the shares that earned it. Walk the trades in order, spreading each payment
// over the lots held that day; when a lot is later sold, its share of the income goes with it.
// Anything below this share of the base is noise and is dropped silently.
const DIV_FLOOR = 0.005;
function splitDividends(d) {
  const rows = tradesOf(d).filter(t => isDeal(t) || t.type === 'dividend');
  const lots = [];
  let sold = 0;
  for (const t of rows) {
    if (t.type === 'dividend') {
      const held = lotShares(lots);
      if (held <= 0) { sold += num(t.amountNet); continue; }
      lots.forEach(l => { l.div += num(t.amountNet) * l.shares / held; });
      continue;
    }
    const sh = num(t.shares);
    if (!sh) continue;
    if (t.type === 'buy') { lots.push({ shares: sh, div: 0 }); continue; }
    fifoTake(lots, sh, (lot, take) => {                // the lot's income leaves with its shares
      const moved = lot.div * take / lot.shares;
      sold += moved;
      lot.div -= moved;
    });
  }
  return { held: lots.reduce((sum, l) => sum + l.div, 0), sold };
}

/* ---------- share splits ---------- */
// Parqet restates a holding's shares and cost basis after a split but leaves the historical
// activity rows at pre-split prices, so any maths mixing trade share counts with today's price
// is off by the split ratio. Recover it: walk the trades FIFO, take the average price of the lots
// still held, and compare with the holding's restated basis. Cash amounts (and therefore the
// realised figures and the XIRR) are unaffected — only share counts need this.
const SPLIT_RATIOS = [1, 2, 3, 4, 5, 6, 8, 10, 20, 25, 50, 100, 1 / 2, 1 / 3, 1 / 4, 1 / 5, 1 / 10];
function splitFactor(d) {
  if (d.sold || !(d.shares > 0) || !(d.pur > 0)) return 1;
  const deals = dealsOf(d);
  if (!deals.some(t => t.type === 'sell')) return 1;

  // priced off the booked trade price, not the net amount: it is precisely the pre-split scale
  // that has to survive here, for comparison against Parqet's restated basis below
  const lots = survivingLots(deals, { priceOf: t => num(t.price) });
  const remShares = lotShares(lots);
  if (!remShares) return 1;
  const basisPre = lotCost(lots) / remShares;
  const ratio = basisPre / (d.pur / d.shares);
  const snap = SPLIT_RATIOS.reduce((best, c) => Math.abs(ratio - c) < Math.abs(ratio - best) ? c : best, 1);
  return Math.abs(ratio - snap) / snap < 0.08 ? snap : 1;   // unrecognised → leave it alone
}

/* ---------- display names ---------- */
// No fund flag comes out of Parqet — assetType is "security" for stocks and ETFs alike — so read it
// off the full name. Overridable later by a column in parqet_names.csv if a fund ever hides it.
const FUND_RE = /\b(UCITS|ETF|ETC|ETN|Fonds|Fund|Fd|Index|Ind\.?\s?Fd|SICAV|Investmentfonds)\b/i;
const isFund = d => FUND_RE.test(d.name || '');

/* ---------- display names ---------- */
// Anything the mapping file doesn't cover gets a generic tidy: drop legal and fund boilerplate.
const BOILERPLATE = [
  /\s*[-–,]?\s*(USD|EUR|GBP|CHF)?\s*(ACC|DIS)\b.*$/i,
  /\b(UCITS|ETF|Fd|Fund|Index|Registered\s+Shares|Reg\.?\s*Shs?|Inhaber|Anteile|o\.?N\.?)\b.*$/i,
  /[,\s]+(Inc|Corp|Corporation|Company|Co|Ltd|PLC|N\.V|S\.A|AG|SE|ADR)\.?$/i,
  /\s+(Holdings?|Technologies|Technology|Group|Therapeutics|Power|Energy)$/i,
];
function tidyName(name) {
  let out = (name || '').trim();
  BOILERPLATE.forEach(re => { const cut = out.replace(re, '').trim(); if (cut.length > 2) out = cut; });
  return out.replace(/[\s,\-–]+$/, '') || name;
}
const displayName = d => NAMES.get(d.identifier) || NAMES.get(d.name) || tidyName(d.name);

const MIN_YEARS = 1 / 12;    // a position held for days would annualise to nonsense
function holdingYears(iso) {
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return NaN;
  return Math.max(MIN_YEARS, (Date.now() - t) / (365.2425 * 864e5));
}
// annualised return on the money still in the position: (current / purchase) ^ (1/years) − 1.
// Parqet gives no per-activity cash flows, so this is a CAGR from the first activity date,
// not a true XIRR — for a position bought in several tranches it understates the real IRR.
function annualised(d) {
  if (d.cash || !(d.pur > 0) || !(d.cur > 0) || !Number.isFinite(d.years)) return NaN;
  return ((d.cur / d.pur) ** (1 / d.years) - 1) * 100;
}

/* ---------- build ---------- */
const nameColor = name => `hsl(${nameHue(name).toFixed(1)}deg var(--core-s) var(--core-l))`;

function build(rows) {
  CCY = rows[0]?.currency || 'EUR';

  const items = rows.map(r => {
    const cur = num(r.currentValue), pur = num(r.purchaseValue);
    const gain = cur - pur;
    const cash = (r.assetType || '').toLowerCase() === 'cash';
    const state = cash || pur === 0 || Math.abs(gain) < 0.005 ? 'flat' : (gain > 0 ? 'gain' : 'loss');
    return {
      portfolio: r.portfolio || 'Portfolio', name: r.name, shares: num(r.shares),
      cur, pur, purAbs: pur, gain, ret: pur > 0 ? gain / pur * 100 : 0, cash, state,
      rel: cash ? 0 : num(r.realizedGainNet),
      firstActivity: r.earliestActivityDate || '',
      identifier: r.identifier || '',
      lastPrice: num(r.lastPrice),
      lastPriceDate: r.lastPriceDate || '',
      sold: r.isSold === '1' || r.isSold === 'true',
      activityCount: num(r.activityCount),
    };
  });

  items.forEach(d => {
    const fresh = PRICES.get(d.identifier);           // fills the gap Parqet leaves on closed positions
    if (fresh && (!d.lastPriceDate || fresh.asof > d.lastPriceDate)) {
      d.lastPrice = fresh.price;
      d.lastPriceDate = fresh.asof;
      d.priceSource = fresh.symbol;   // nothing renders this; it's here to inspect in devtools
                                      // when a hand-maintained price looks wrong
    }
    d.label = displayName(d);
    d.fund = isFund(d);
    d.years = holdingYears(d.firstActivity);
    d.flows = cashFlows(d);
    d.invested = d.flows.reduce((t, f) => f.v < 0 ? t - f.v : t, 0);   // money actually put in
    // Parqet's realised gain is after tax and fees; add the sale tax back for a pre-tax figure
    const mine = tradesOf(d);
    d.taxSell = mine.filter(t => t.type === 'sell').reduce((t, r) => t + num(r.tax), 0);
    d.relPre = d.rel + d.taxSell;

    // what the shares sold would be worth at the last price we know, against what they fetched
    const sells = mine.filter(t => t.type === 'sell');
    d.soldShares = sells.reduce((t, r) => t + num(r.shares), 0);
    d.grossProceeds = sells.reduce((t, r) => t + num(r.amount), 0);
    d.lastSell = sells.reduce((t, r) => r.datetime > t ? r.datetime : t, '');
    d.sellCount = sells.length;
    d.sellPrice = d.soldShares > 0 ? d.grossProceeds / d.soldShares : NaN;   // volume-weighted, as booked
    // expired warrants book a sell at zero — no meaningful comparison there
    d.split = splitFactor(d);                          // 4 ⇒ the sold shares became 4× as many
    d.since = (d.soldShares > 0 && d.lastPrice > 0 && d.grossProceeds > 0)
      ? d.soldShares * d.split * d.lastPrice - d.grossProceeds : NaN;
    // Parqet stops quoting a position once it is closed: if the price is no younger than the
    // last sale there is nothing to compare against, and the figure would be noise
    d.benchAlt = d.cash ? NaN : benchAlternative(d);
    d.alpha = realisedAlpha(d);
    const divs = splitDividends(d);
    d.divHeld = divs.held > DIV_FLOOR * (d.purAbs || Infinity) ? divs.held : 0;
    d.divSold = divs.sold > DIV_FLOOR * (d.grossProceeds || Infinity) ? divs.sold : 0;
    d.benchValue = sells.length ? benchValue(sells) : NaN;
    d.heldValue = (d.soldShares > 0 && d.lastPrice > 0) ? d.soldShares * d.split * d.lastPrice : NaN;
    d.vsBench = (Number.isFinite(d.benchValue) && Number.isFinite(d.heldValue))
      ? d.heldValue - d.benchValue : NaN;
    d.sinceKnown = Number.isFinite(d.since) &&
      (!d.sold || (d.lastPriceDate && d.lastPriceDate > (d.lastSell || '').slice(0, 10)));
    const x = xirr(d.flows);
    d.irr = Number.isFinite(x) ? x : annualised(d);   // fall back to a CAGR without trades
    d.irrExact = Number.isFinite(x);
    d.benchIrr = benchIrrOf(d.flows);
  });

  ({ divTotal: DIV_TOTAL, divRows: DIV_ROWS, taxTotal: TAX_TOTAL, taxSplit: TAX_SPLIT } =
    incomeAndTax(TRADES));

  const grand = items.reduce((t, d) => t + d.cur, 0);
  items.forEach(d => { d.share = grand > 0 ? d.cur / grand : 0; });

  // portfolios in draw order: biggest by live value first, and that order is the pie's arc order
  const byPf = new Map();
  items.filter(d => !d.sold).forEach(d => byPf.set(d.portfolio, (byPf.get(d.portfolio) || 0) + d.cur));
  PF = [...byPf.entries()].sort((a, b) => b[1] - a[1]).map(([name]) => ({ name }));
  const rank = new Map(PF.map((p, i) => [p.name, i]));

  applyMode(items);                                    // sets .core, .pur, .gain, .ret, .state

  // group by portfolio (contiguous arcs), largest position first inside each group
  return items.sort((a, b) =>
    (rank.get(a.portfolio) - rank.get(b.portfolio)) || (b.cur - a.cur));
}

// absolute mode measures against what was paid; relative mode against the benchmark mirror
function applyMode(items) {
  items.forEach(d => {
    const base = MODE === 'rel' ? d.benchAlt : d.purAbs;
    d.pur = Number.isFinite(base) ? base : d.purAbs;
    d.gain = d.cur + (d.divHeld || 0) - d.pur;      // income the shares paid out counts as return
    d.ret = d.pur > 0 ? d.gain / d.pur * 100 : 0;
    d.state = (d.cash || !(d.pur > 0) || Math.abs(d.gain) < 0.005) ? 'flat'
            : (d.gain > 0 ? 'gain' : 'loss');
    d.core = d.cash ? 'var(--flat)' : nameColor(d.label);
  });
}
const barValue = d => (d.isTax || d.isDiv) ? d.relPre
  : (MODE === 'rel' ? (Number.isFinite(d.alpha) ? d.alpha : 0) : d.relPre);

// cash sits outside invested/gain: Parqet reports its "purchase value" as cumulative deposits
function totals(rows) {
  const cur = rows.reduce((s, d) => s + d.cur, 0);
  const inv = rows.filter(d => !d.cash);
  const pur = inv.reduce((s, d) => s + d.pur, 0);
  return { cur, pur, gain: inv.reduce((s, d) => s + d.cur, 0) - pur,
           rel: inv.reduce((s, d) => s + (d.relPre ?? d.rel), 0) };
}

/* ---------- ingest ----------
   config.json plus the six CSVs in, the whole model out. Called by load() in portfolio.view.js,
   which renders what this leaves behind; nothing here touches the page. */
function ingest(configText, text, tradesText, namesText, benchText, pricesText, sectorsText) {
  // malformed or missing config.json keeps the built-in defaults rather than failing the page —
  // same "absent input degrades gracefully" rule every other file here follows
  try {
    const cfg = configText ? JSON.parse(configText) : {};
    if (cfg.timelineStart) TIMELINE_START = cfg.timelineStart;
    if (cfg.benchmarkLabel) BENCH_LABEL = cfg.benchmarkLabel;
  } catch { /* keep defaults */ }
  PRICES = new Map((pricesText ? parseCSV(pricesText) : [])
    .filter(r => r.identifier && r.price !== '' && r.asof)
    .map(r => [r.identifier, { price: num(r.price), asof: r.asof, symbol: r.symbol }]));
  BENCH = (benchText ? parseCSV(splitMeta(benchText).body) : [])
    .map(r => ({ date: r.date, close: num(r.close) }))
    .filter(r => r.date && r.close > 0)
    .sort((a, b) => a.date < b.date ? -1 : 1);
  TRADES = tradesText ? parseCSV(tradesText) : [];
  indexTrades();
  NAMES = new Map((namesText ? parseCSV(namesText) : [])
    .filter(r => r.display)
    .map(r => [r.identifier || r.name, r.display]));
  SECTORS = new Map((sectorsText ? parseCSV(sectorsText) : [])
    .filter(r => r.identifier && r.sector)
    .map(r => [r.identifier, r.sector]));
  const all = build(parseCSV(text));
  ITEMS = all.filter(d => !d.sold);
  CLOSED = all.filter(d => d.sold).sort((a, b) => b.rel - a.rel);
  if (!ITEMS.length) throw new Error('no rows with a positive value');
}
