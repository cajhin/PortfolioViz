/* =============================================================================================
   portfolio.model.js — the data and the arithmetic behind the page.

   NOTHING IN THIS FILE TOUCHES THE DOM. That is the line between the two scripts, and it is what
   lets the model be exercised headlessly (see check_portfolio.js). If a change here needs an
   element, it belongs in portfolio.view.js instead.

   Loaded first; portfolio.view.js runs after and reads what is declared here. Both are classic
   scripts sharing one global scope — no modules, no imports, no build step.

   Sections, in order:

     files                paths of config.json, the registry and the CSVs the page reads
     state                every mutable global, and who is allowed to write it
     CSV                  parsing, and the "# key=value" header lines some files carry
     the trade log        TRADE_INDEX — one position's activities, the input to almost everything
     FIFO lots            the shared share-retirement walk every cost-basis figure is built on
     colour from name     a position's hue, derived from its own name
     XIRR                 cash flows out of the trade log, and the bisection solver over them
     price series         prices/*.csv, the "last close at or before" lookup, and pricePath
     volatility           sigma of daily log returns, annualised — rolling window and EWMA
     splits               undoing Parqet's post-split restatement of historical share counts
     trade vs. now        what the price has done since a trade, on today's split scale
     as of                the portfolio rebuilt as it stood on a past date, or between two
     benchmark            "what if this money had gone into the index instead" — four flavours
     dividends            attributing income to the lots that earned it
     display names        trimming legal boilerplate off a position's full name
     build                CSV rows → the position objects every renderer consumes
     ingest               config + registry + the Parqet export in, the whole model out

   Two rules the code holds to, worth keeping: a position is identified by portfolio *and*
   identifier (the same ISIN can live in two portfolios with separate histories), and money
   figures are always pre-tax unless the name says otherwise.

   Three input directories, by lifecycle — the distinction is worth preserving:
     parqet/       IMPORTED  regenerated wholesale by the refresh task; never hand-edited
     registry/     CURATED   what exists and where its prices come from; never overwritten
     prices/  DERIVED   reproducible from registry/price_sources.csv alone
   Every close in prices/ is already in the portfolio currency — update_prices.py
   converts on write and keeps the untouched quote alongside — so nothing here does FX.
   prices/ also outranks the export on price: anywhere _latest.csv is fresher than Parqet's own
   lastPriceDate, build() takes its close and recomputes the position's current value from it.
   ============================================================================================= */

/* ---------- files ---------- */
const CONFIG_PATH = 'config.json';   // tunable settings an agent maintains — see its own comments
const CSV_PATH = 'parqet/positions.csv';
const TRADES_PATH = 'parqet/activities.csv';
// registry/ is curated: what exists, and what each instrument is called. It is keyed by ISIN and
// is deliberately NOT derived from the Parqet export — an instrument may be listed here that no
// portfolio holds (a benchmark, a watchlist name) and still be charted.
const INSTRUMENTS_PATH = 'registry/instruments.csv';
const PRICE_SOURCES_PATH = 'registry/price_sources.csv';
// prices/ is derived: reproducible from registry/price_sources.csv by update_prices.py.
// _latest.csv is the freshest close per instrument, and it is the price of record for every
// position it covers — the Parqet export is a snapshot from whenever it was pulled, so its quotes
// are usually the older pair. build() takes the close and recomputes the position's value with it.
const LATEST_PATH = 'prices/_latest.csv';

/* ---------- state ----------
   Every mutable global on the page. Only ingest() and build() below, and the control handlers
   in portfolio.view.js, ever write them; everything else reads.

   ITEMS / CLOSED are the open and sold positions — the objects every renderer consumes.
   TRADES is the raw activity log, and the three Maps are lookup tables read straight off their
   CSVs. MODE / VIEW / AS_OF are what the three controls in the chart bar currently say; the
   view syncs MODE from the checkbox at startup, since a browser restores a checkbox's ticked
   state across a reload on its own and this script would otherwise disagree with the screen.
   AS_FROM is the other end of the same pick: with it set the map answers "what happened between
   these two dates" rather than "what has happened up to this date", and every basis figure is
   re-based onto that start date (see rebaseLots).
   TIMELINE_START / BENCH_LABEL start at sensible defaults and are overwritten by ingest() from
   config.json — kept as ordinary globals, not a nested CONFIG object, so every reader still just
   reads a plain name the way it does for everything else here. */
let ITEMS = [], CLOSED = [], TRADES = [], NAMES = new Map(), PRICES = new Map(), SECTORS = new Map(),
    INSTRUMENTS = new Map(),      // registry rows, keyed by ISIN
    SOURCES = new Map(),          // instrument id → its price source row, for the Source column
    BENCH = [], CCY = 'EUR',
    MODE = 'abs',                                      // 'abs' | 'rel' (vs. the benchmark)
    VIEW = new URLSearchParams(location.search).get('view') === 'pie' ? 'pie' : 'map',
    AS_OF = null,                                      // an ISO date, or null for "today"
    AS_FROM = null,                                    // an ISO date, or null for "beginning of time"
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

// The benchmark is an instrument like any other now, so its series has no dedicated path — it is
// resolved from config.json's benchmarkIsin against the registry. The view calls this before its
// second fetch round, since the file to ask for is not knowable until both of those are in hand.
// Parsing stays here rather than in the view: the model never reads the DOM, and the view never
// parses a CSV.
function benchSeriesPath(configText, instrumentsText) {
  let isin = '';
  try { isin = (configText ? JSON.parse(configText) : {}).benchmarkIsin || ''; } catch { /* none */ }
  if (!isin) return '';
  const row = (instrumentsText ? parseCSV(instrumentsText) : [])
    .find(r => r.id === isin || r.isin === isin);
  return row && row.slug ? `prices/${row.id}-${row.slug}.csv` : '';
}

// The money that has actually left the account and stayed out: everything paid in, less everything
// taken back out, over the whole activity log. Buys and costs count in; sales, dividends and
// interest count out — the same signs XIRR uses.
//
// Deliberately not the same thing as the "Invested" tile, which is the cost basis of what is held
// *now*. Sell something for more than it cost and buy the next thing with the proceeds, and that
// basis grows while the money put in has not moved: the tile counts the enlarged stake, this counts
// only the original one. The gap between them is profit that has been put back to work.
function netCapital(rows = TRADES) {
  return -rows.reduce((t, x) => t + (SIGN[x.type] ?? 0) * num(x.amountNet), 0);
}

/* ---------- price series ---------- */
const SERIES_CACHE = new Map();
// The series file for a position: "<isin>-<slug>", both straight off the registry row. The slug
// is carried there rather than derived from the display name, so renaming a position on screen
// can never silently point the chart at a different file (or at none).
const seriesSlug = d => {
  const inst = INSTRUMENTS.get(d.identifier) || INSTRUMENTS.get(d.name);
  if (!inst || !inst.slug) return '';
  return `${inst.id}-${inst.slug}`;
};

async function loadSeries(slug) {
  if (!slug) return null;
  if (SERIES_CACHE.has(slug)) return SERIES_CACHE.get(slug);
  let out = null;
  try {
    const r = await fetch(`prices/${slug}.csv`, { cache: 'no-store' });
    if (r.ok) {
      const file = splitMeta(await r.text());
      // raw/ccy are the quote before update_prices.py converted it — carried so a price can be
      // shown in the currency it actually trades in alongside the portfolio-currency figure.
      // Absent from a hand-maintained file, so every reader has to tolerate 0 and ''.
      const rows = parseCSV(file.body)
        .map(x => ({ date: x.date, close: num(x.close),
                     raw: num(x.close_raw), ccy: x.quote_currency || '' }))
        .filter(x => x.date && x.close > 0)
        .sort((a, b) => a.date < b.date ? -1 : 1);
      if (rows.length) out = { rows, meta: file.meta };
    }
  } catch { /* no series for this position */ }
  SERIES_CACHE.set(slug, out);
  return out;
}

// The synchronous half of loadSeries: whatever is already cached for this slug, or null. For a
// caller that cannot await — a pointerenter handler building a tooltip — and would rather draw
// nothing than block the hover.
const seriesIfLoaded = slug => SERIES_CACHE.get(slug) || null;

// This position's own price across a window, as a percentage from the window's first close — the
// figures behind the sparkline in the map's tooltip. Null when there is no cached series, or too
// little of it inside the window to draw a line.
//
// No anchoring here, deliberately, though every other reader of a prices series applies it: each
// point is divided by the same first close, so the factor that lines the series up with Parqet's
// own last price cancels out of the ratio. It would move every number and no part of the shape.
//
// Prices only — this is the instrument's path, not the holding's. What the position did over the
// range depends on when its lots were bought and is already the tile's own colour and figure;
// this is the line behind that, and it is the same line whether one share was held or a thousand.
// The slice of a position's cached series covering a window, or null when there is no series or
// too little of it inside the window.
//
// Both ends are the *same* rows the range arithmetic uses: seriesCloseAt is "last close at or
// before", so a window starting on a Sunday — or on a market holiday — is measured from the Friday
// before it. Slicing from the first row on-or-after the start instead would drop that very row,
// and with it any gap between it and the next session: anything drawn or quoted from it would be
// missing exactly the move the percentage beside it reports. Anchor and window must be one row.
function priceWindow(d, fromStr, toStr) {
  const series = seriesIfLoaded(seriesSlug(d));
  if (!series) return null;
  const all = series.rows;
  const lo = fromStr ? Math.max(0, lastIndexAtOrBefore(all, fromStr)) : 0;
  const hi = toStr ? lastIndexAtOrBefore(all, toStr) : all.length - 1;
  return (hi < 0 || hi - lo < 1) ? null : all.slice(lo, hi + 1);
}

function pricePath(d, fromStr, toStr) {
  const rows = priceWindow(d, fromStr, toStr);
  if (!rows || !(rows[0].close > 0)) return null;
  const base = rows[0].close;
  return rows.map(r => ({ date: r.date, pct: (r.close - base) / base * 100 }));
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

/* ---------- realised volatility ----------
   Trailing standard deviation of daily log returns, annualised — the conventional measure, and
   the one every other tool means by "volatility". Log returns rather than simple ones because
   they add over time, which is what makes the sqrt(periods) scaling below valid at all; over a
   single day the two barely differ, but the annualisation is where the choice would show.

   Computed on a position's own price rows, in the portfolio currency, so a foreign listing's
   volatility here includes its FX component — which is the honest figure for a portfolio that
   holds it in euros, and not the same number a US site would print for the same ticker.

   Two things this deliberately excludes from a window:

     · a return spanning a real gap in the history (Roche's RHO.DE has one running 2019-09 to
       2025-04). Whatever the price did over those years arrives as a single log return, and one
       such value inside a 21-day window is not "a volatile month", it is an artefact of the data.
       The detail chart already draws that stretch dashed for the same reason.
     · a zero or negative close, which has no log at all.

   Both simply drop out of the window, and a window left with too few real returns reports null
   rather than a figure built on four observations. */
const VOLA_GAP_DAYS = 20;                 // past a long holiday cluster; a real data gap
const VOLA_PERIODS = 252;                 // trading days in a year — the annualisation factor
const VOLA_MIN_FRAC = 0.6;                // a window needs this share of its days to count

// Rolling volatility at every row of `rows`, as an annualised percentage.
// Returns one { date, vola } per input row, vola null where the window is too sparse to mean
// anything — including, always, the first `window` rows of a series, which have no full window
// behind them. Prefix sums keep this O(n) rather than O(n·window): the detail chart recomputes
// it on every range button and every comparison-line swap.
//
// upWeight scales down a positive return before it is squared — 1 leaves ordinary volatility
// (up and down moves count the same) alone; the "discount up moves" checkbox passes 0.5, so a
// day the price rose contributes a quarter of its usual weight to variance (an already-halved
// return, squared) and a down day is untouched. That is not textbook semi-deviation — semi-
// deviation drops upside days to zero rather than merely discounting them — but the same idea
// softened, per the request: emphasise the downside without pretending an up day carries no
// information at all. The prefix-sum shortcut (Σr'² − (Σr')²/n) still holds unmodified since it
// only assumes r' IS the series being measured, not that it came straight from the price ratio.
function volatilitySeries(rows, window, periodsPerYear = VOLA_PERIODS, upWeight = 1) {
  const out = rows.map(r => ({ date: r.date, vola: null }));
  if (!rows || rows.length < 2 || window < 2) return out;
  // r[i] is the return arriving AT row i, so a window ending at i covers r[i-window+1 .. i]
  const n = rows.length;
  const sum = new Float64Array(n + 1), sumSq = new Float64Array(n + 1);
  const count = new Int32Array(n + 1);
  for (let i = 1; i < n; i++) {
    const a = rows[i - 1].close, b = rows[i].close;
    const days = (Date.parse(rows[i].date) - Date.parse(rows[i - 1].date)) / 864e5;
    const usable = a > 0 && b > 0 && days <= VOLA_GAP_DAYS;
    const raw = usable ? Math.log(b / a) : 0;
    const r = raw > 0 ? raw * upWeight : raw;
    sum[i + 1] = sum[i] + r;
    sumSq[i + 1] = sumSq[i] + r * r;
    count[i + 1] = count[i] + (usable ? 1 : 0);
  }
  const need = Math.max(2, Math.ceil(window * VOLA_MIN_FRAC));
  for (let i = window; i < n; i++) {
    const lo = i - window + 1;                         // first return in the window
    const k = count[i + 1] - count[lo];
    if (k < need) continue;
    const s1 = sum[i + 1] - sum[lo], s2 = sumSq[i + 1] - sumSq[lo];
    // sample variance, k-1: these are a sample of the return process, not the whole of it
    const varce = (s2 - s1 * s1 / k) / (k - 1);
    if (!(varce > 0)) continue;
    out[i].vola = Math.sqrt(varce) * Math.sqrt(periodsPerYear) * 100;
  }
  return out;
}

// A rolling window gives every return inside it equal weight and none outside it — a single
// one-day shock rides at full strength for `window` days, then drops out in one step, so a spike
// draws as a plateau with a cliff on both edges rather than as the one-day event it actually was.
// EWMA (RiskMetrics' own choice, and the standard alternative) never has an edge to fall off:
// each day's return is folded in and then decays geometrically forever after, so a spike shows up
// immediately and fades out smoothly instead of vanishing 21 days later for no new reason.
//
// λ=0.94 is RiskMetrics' own daily constant — a return's contribution to variance halves roughly
// every 11 trading days, deliberately close to the 21-day window's own persistence, so the two
// read as answering the same question at different smoothness. No `window` argument: unlike
// volatilitySeries, this has no edge to be sparse near — the very first valid return already
// seeds a value, at the cost of that first value being unreliably noisy (an estimate built on
// one observation, same as anywhere else in statistics with n=1).
const VOLA_LAMBDA = 0.94;
// upWeight: same "discount up moves" scaling as volatilitySeries above, applied before the return
// is folded into the recursion — see that function's comment for what it does and doesn't mean.
function volatilityEwma(rows, lambda = VOLA_LAMBDA, periodsPerYear = VOLA_PERIODS, upWeight = 1) {
  const out = rows.map(r => ({ date: r.date, vola: null }));
  if (!rows || rows.length < 2) return out;
  let variance = null;
  for (let i = 1; i < rows.length; i++) {
    const a = rows[i - 1].close, b = rows[i].close;
    const days = (Date.parse(rows[i].date) - Date.parse(rows[i - 1].date)) / 864e5;
    if (!(a > 0 && b > 0) || days > VOLA_GAP_DAYS) continue;   // same exclusions as the window above
    const raw = Math.log(b / a);
    const r = raw > 0 ? raw * upWeight : raw;
    variance = variance === null ? r * r : lambda * variance + (1 - lambda) * r * r;
    out[i].vola = Math.sqrt(variance) * Math.sqrt(periodsPerYear) * 100;
  }
  return out;
}

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
// prices prices come from an external provider that split-adjusts its whole history
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

/* ---------- as of: the portfolio between two past dates ---------- */
// Parqet's own last known price is the ground truth for what a position is worth "today", but the
// closes in prices/ come from a different provider whose level can sit a little apart from it.
// This is the multiplier that lines that series up with Parqet, so a reconstruction of today
// reproduces the live map exactly. Every reader of a prices series applies it.
function anchorFactor(d, series) {
  const anchorClose = seriesCloseAt(series.rows, d.lastPriceDate) ||
    series.rows[series.rows.length - 1].close;
  return (d.lastPrice > 0 && anchorClose > 0) ? d.lastPrice / anchorClose : 1;
}

// Lots re-based onto a range's start date: anything already held then is treated as though it had
// been bought that morning at that day's close, so the gain it had accumulated before the range
// belongs to the range before this one and is not booked into this one. Lots bought *inside* the
// range keep the price and the date they were really bought at, which is what makes a mid-range
// purchase count from its own cost rather than from a price it never traded at.
//
// This one rewrite is the whole of "measure the range and not the whole history": every figure
// downstream — cost basis, gain, return, the benchmark counterfactual, XIRR — is some walk over
// these lots, so re-basing them re-bases all of it at once.
//
// closeAtFrom must already be anchored (see anchorFactor) — it stands in for money paid. With no
// start date, or no close to re-base onto, the lots are handed back exactly as they came, and the
// caller falls back to measuring from what was actually paid.
function rebaseLots(lots, fromStr, closeAtFrom) {
  if (!fromStr || !(closeAtFrom > 0)) return lots;
  return lots.map(l => l.at.slice(0, 10) < fromStr
    ? { shares: l.shares, price: closeAtFrom, at: fromStr }
    : l);
}

// The portfolio as it stood on a past date: replay each position's trades up to that day (FIFO,
// same walk as the split/dividend helpers) to get shares actually held, price them from that
// position's own prices file, and value the remaining cost at what was actually paid. A position
// lacking a series that day is left out and named in the result, since it cannot be honestly
// reconstructed from what this page has on hand.
//
// With `fromStr` given the same replay answers a narrower question — what these holdings did
// *between* the two dates — by re-basing the surviving lots onto the start date first. Shares are
// still those held on `dateStr`; only what they are measured against moves.
const AS_OF_CACHE = new Map();
async function computeAsOf(dateStr, fromStr = null) {
  // re-picking "vs. World", or either end of the range, must not hit a stale cache
  const cacheKey = dateStr + '|' + (fromStr || '') + '|' + MODE;
  if (AS_OF_CACHE.has(cacheKey)) return AS_OF_CACHE.get(cacheKey);

  const source = [...ITEMS, ...CLOSED];

  // the lot replay is cheap and synchronous; do it first so only positions actually held on
  // this date ever trigger a fetch, then load every needed series in parallel (each cached
  // after the first request, so re-picking a date, or reopening the map later, is instant)
  const held = source.map(d => {
    const lots = survivingLots(splitAdjustedDeals(d), { until: dateStr });
    return { d, sharesAtD: lotShares(lots), heldLots: lots };
  }).filter(h => h.sharesAtD > 1e-9);                   // not yet bought, or already sold out, by then

  const seriesFor = new Map(await Promise.all(
    held.map(async h => [h.d, await loadSeries(seriesSlug(h.d))])));

  const out = [];
  const missing = new Set();
  for (const { d, sharesAtD, heldLots } of held) {
    const series = seriesFor.get(d);
    const closeAtD = series && seriesCloseAt(series.rows, dateStr);
    if (!series || closeAtD == null) { missing.add(d.label || d.name); continue; }
    const factor = anchorFactor(d, series);

    // Only a position that was already held on the start date needs a price there — one bought
    // inside the range is measured from what it cost, which needs no series that far back. When it
    // *is* needed and missing, the position drops out: measuring it from its original purchase
    // while every neighbour is measured from the start date would quietly mix two questions.
    const needsFrom = fromStr && heldLots.some(l => l.at.slice(0, 10) < fromStr);
    const closeAtFrom = needsFrom ? seriesCloseAt(series.rows, fromStr) : null;
    if (needsFrom && closeAtFrom == null) { missing.add(d.label || d.name); continue; }
    const lots = needsFrom ? rebaseLots(heldLots, fromStr, closeAtFrom * factor) : heldLots;

    const costAtD = lotCost(lots);
    // "Performance vs. MSCI World": the same lots' cost, grown in the index instead, up to this
    // date — same measure the live map uses, just bounded to dateStr rather than today (and, on a
    // range pick, starting from the start date's value rather than from the original purchase)
    const benchAtD = MODE === 'rel' ? benchValueOfLots(lots, dateStr) : NaN;

    const cur = sharesAtD * closeAtD * factor;
    // in "vs. World" mode, fall back to actual cost for any lot the benchmark couldn't price —
    // never silently drop a position just because the index comparison came up short
    const pur = Number.isFinite(benchAtD) ? benchAtD : costAtD;
    const gain = cur - pur;
    out.push({
      portfolio: d.portfolio, name: d.name, label: d.label, identifier: d.identifier,
      core: d.core, fund: d.fund, shares: sharesAtD,
      // purAbs is what these shares actually cost, before any re-basing or benchmark substitution
      // — the money that left the account. `pur` is what the range or the vs.-World mode measures
      // against, which on a range pick is a market value rather than a purchase, so only purAbs
      // divides into an average price paid.
      purAbs: lotCost(heldLots),
      cur, pur, gain, ret: pur > 0 ? gain / pur * 100 : 0,
      state: pur <= 0 || Math.abs(gain) < 0.005 ? 'flat' : (gain > 0 ? 'gain' : 'loss'),
      irr: asOfIrr(lots, cur, dateStr), divHeld: 0, firstActivity: d.firstActivity,
      lastPrice: closeAtD * factor, lastPriceDate: dateStr,
    });
  }

  const grand = out.reduce((t, x) => t + x.cur, 0);
  out.forEach(x => { x.share = grand > 0 ? x.cur / grand : 0; });
  out.sort((a, b) => b.cur - a.cur);

  const currentTotal = ITEMS.reduce((t, x) => t + x.cur, 0);   // today's true total
  const result = {
    items: out, asOfTotal: grand, currentTotal, from: fromStr,
    ratio: currentTotal > 0 ? grand / currentTotal : 1,
    missing: [...missing],
  };
  AS_OF_CACHE.set(cacheKey, result);
  return result;
}

// The same per-date value computeAsOf works out for one pick, walked across every day already on
// screen instead — what the value bar under the detail overlay chart reads. It anchors the series
// the same way computeAsOf() does, so this and the live map agree on the position's current value
// even though the prices close comes from a different provider. Synchronous — the series is
// already loaded by the time a chart draws.
function valueOverTime(d, series, displayRows) {
  const deals = splitAdjustedDeals(d);
  const factor = anchorFactor(d, series);
  return displayRows.map(r => {
    const lots = survivingLots(deals, { until: r.date });
    const shares = lotShares(lots), cost = lotCost(lots);
    const cur = shares * r.close * factor;
    return { date: r.date, cur, cost, ret: cost > 0 ? (cur - cost) / cost * 100 : 0 };
  });
}

// The whole portfolio as one series, day by day — every open or closed position's lots
// replayed at each date, priced from that position's own prices file and anchored to Parqet's own
// latest price the same way valueOverTime is. The calendar is the union of every held position's
// trading days, so a date only one position actually traded on still lands correctly.
//
// Two figures per row, because they answer different questions:
//   value — what the holdings were worth that day, in portfolio currency
//   close — a time-weighted return index, 100 on the first day anything was held
//
// `close` is the one that makes this comparable to a stock's own chart, and it is deliberately
// NOT the summed value: paying €100 into a savings plan lifts the value by €100, so charting that
// would read every contribution as a gain. The index instead asks only "what did yesterday's
// holdings do today":
//
//     r(t) = Σᵢ Sᵢ(t−1)·Pᵢ(t) / Σᵢ Sᵢ(t−1)·Pᵢ(t−1) − 1,     I(t) = I(t−1)·(1 + r(t))
//
// which is the standard chain-linked time-weighted return, written so that today's trades never
// enter the arithmetic at all — only *yesterday's* share counts appear, so a buy, a sell or a
// deposit is structurally incapable of moving it. Hold nothing but one instrument and this traces
// that instrument's own chart exactly, whatever the contributions were, which is the point.
//
// Price return: dividends are deliberately excluded. prices/ carries Yahoo's raw close, so every
// other line on the detail chart is a price return too — adding payouts back on this line alone
// would lift it above the rest by roughly the dividend yield, for a reason that is not performance.
//
// Named "close" rather than "index" so the row is shaped like any price series ({rows: [{date,
// close}]}) and can stand in for a real instrument's wherever one is expected. Computed once and
// cached module-wide: the FIFO replay is O(positions × dates), cheap once, wasteful to repeat on
// every range switch. Nothing it reads varies with MODE or the as-of pick, so there is no key.
let PORTFOLIO_SERIES_CACHE = null;
async function portfolioSeries() {
  if (PORTFOLIO_SERIES_CACHE) return PORTFOLIO_SERIES_CACHE;
  const source = [...ITEMS, ...CLOSED];
  const items = (await Promise.all(source.map(async d => {
    const series = await loadSeries(seriesSlug(d));
    if (!series || !series.rows.length) return null;
    return { deals: splitAdjustedDeals(d), series, factor: anchorFactor(d, series) };
  }))).filter(Boolean);

  const dateSet = new Set();
  items.forEach(({ series }) => series.rows.forEach(r => dateSet.add(r.date)));
  const dates = [...dateSet].sort();

  const rows = [];
  let prev = null, index = 100;
  for (const date of dates) {
    const held = items.map(({ deals, series, factor }) => {
      const px = seriesCloseAt(series.rows, date);
      return { shares: lotShares(survivingLots(deals, { until: date })),
               px: px == null ? null : px * factor };
    });
    const value = held.reduce((t, h) => t + (h.px == null ? 0 : h.shares * h.px), 0);

    if (prev) {
      // both sums are over *yesterday's* holdings, so a position first bought today contributes
      // to neither and its first day of return is tomorrow — the end-of-day flow convention. A
      // position priced on only one of the two days is skipped from both, keeping the ratio
      // consistent rather than comparing a partial basket against a whole one.
      let then = 0, now = 0;
      prev.forEach((p, i) => {
        if (p.shares <= 1e-9 || p.px == null || held[i].px == null) return;
        then += p.shares * p.px;
        now += p.shares * held[i].px;
      });
      if (then > 0) index *= now / then;
    }
    prev = held;
    // nothing held yet: no return to record and no value to plot. Once the first position is
    // bought the series runs unbroken, holding the index flat across any later stretch the
    // portfolio happens to be empty rather than restarting it.
    if (rows.length || value > 0) rows.push({ date, close: index, value });
  }

  PORTFOLIO_SERIES_CACHE = { rows };
  return PORTFOLIO_SERIES_CACHE;
}

// The realised side of the same as-of pick: every sell (and its dividends/taxes) booked on or
// before dateStr — or, on a range pick, strictly inside the range — split into "open" (some shares
// still held on the end date) and "closed" (fully sold by then) so renderClosed can draw the same
// open|closed bar it draws for today, just bounded to what had actually happened.
//
// Without a start date this touches no price file at all and walks the trades' own booked share
// counts, which need no split rescaling: only the amounts matter, and those net out correctly on
// whatever historical share scale they were booked at.
//
// A start date changes that, and it is the one thing here that costs a fetch. Shares still held on
// that date are re-based onto its close (same rule as computeAsOf, so the realised and unrealised
// halves of a range agree about where it begins), and pricing shares against the external series
// only lines up on today's split scale — hence the split-adjusted walk below. The raw list is kept
// alongside it, because "sold N shares at X" is a booked fact the tooltip prints next to its own
// split factor and must stay on the scale it was booked at.
const AS_OF_REALIZED_CACHE = new Map();
async function computeAsOfRealized(dateStr, fromStr = null) {
  const cacheKey = dateStr + '|' + (fromStr || '') + '|' + MODE;
  if (AS_OF_REALIZED_CACHE.has(cacheKey)) return AS_OF_REALIZED_CACHE.get(cacheKey);

  const source = [...ITEMS, ...CLOSED];
  // One anchored close per position, on the range's start date — fetched only for the positions
  // that actually held something then, so a range whose start predates the whole portfolio costs
  // no requests at all. A position with no series that far back simply stays out of the Map, and
  // rebaseLots leaves its lots at what was paid.
  const priceAtFrom = new Map();
  if (fromStr) await Promise.all(source.map(async d => {
    if (!survivingLots(splitAdjustedDeals(d), { until: fromStr }).length) return;
    const series = await loadSeries(seriesSlug(d));
    const close = series && seriesCloseAt(series.rows, fromStr);
    if (close != null) priceAtFrom.set(d, close * anchorFactor(d, series));
  }));

  const rows = [];
  for (const d of source) {
    const booked = dealsOf(d);
    // same array, same order, share counts on the price series' scale — indices line up
    const trades = fromStr ? splitAdjustedDeals(d) : booked;
    const lots = fromStr
      ? rebaseLots(survivingLots(trades, { until: fromStr }), fromStr, priceAtFrom.get(d))
      : [];
    let realized = 0, taxSell = 0, costSold = 0, soldShares = 0, grossProceeds = 0;
    let lastSell = '', sellCount = 0, activityCount = 0, benchProceeds = 0, benchKnown = true;
    for (let i = 0; i < trades.length; i++) {
      const t = trades[i], day = t.datetime.slice(0, 10);
      if (day > dateStr) break;                          // sorted — nothing after matters
      if (fromStr && day <= fromStr) continue;           // already folded into the re-based lots
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
      soldShares += num(booked[i].shares);               // as booked, never re-scaled
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
    });
  }

  const inRange = t => {
    const day = t.datetime.slice(0, 10);
    return day <= dateStr && (!fromStr || day > fromStr);
  };
  const result = {
    open: rows.filter(d => !d.sold), closed: rows.filter(d => d.sold),
    ...incomeAndTax(TRADES.filter(inRange)),
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
  if (!BENCH.length) return NaN;
  const deals = dealsOf(d);
  if (!deals.length) return NaN;
  const lots = survivingLots(deals);
  return lots.length ? benchValueOfLots(lots, BENCH[BENCH.length - 1].date) : NaN;
}

// What each sale actually made, measured against the index instead of against cost: proceeds minus
// what the sold lots' own money would have grown to in the benchmark between buying and selling.
function realisedAlpha(d) {
  if (!BENCH.length) return NaN;
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

/* ---------- a trade against today's price ---------- */
// The trades table's "Now %": where the price stands now against what this trade booked, as a
// percentage of the trade price. Two things have to be lined up before that subtraction means
// anything.
//
// The unit — `price` is what was paid per share on the day, so a trade made before a split is
// quoted on a scale today's price is not. The booked price is restated onto today's scale first,
// the same correction the realised-bar tooltip already prints next to a sale.
//
// The position — a trade is only comparable to the last price of the holding it belongs to, found
// by portfolio *and* identifier like everything else here. A closed position works too: Parqet
// stops quoting one at the sale, and prices/_latest.csv fills that in above.
//
// The sign is deliberately the same for both directions: the number answers "what has the price
// done since", which is one question however the trade went. Whether that counts as a *good*
// trade is the opposite verdict for a sale as for a buy, and that is the renderer's business.
//
// Keyed by the trade object itself, and built in one pass the first time it is asked for: the
// table wants this for every row, and re-deriving a position's split scale per row would be
// O(trades × trades). Nothing it reads changes after ingest(), so there is no key.
let TRADE_VS_NOW_CACHE = null;
function tradeVsNow(t) {
  if (!TRADE_VS_NOW_CACHE) {
    TRADE_VS_NOW_CACHE = new Map();
    for (const d of [...ITEMS, ...CLOSED]) {
      if (!(d.lastPrice > 0)) continue;
      // same array, same order — splitAdjustedDeals maps over dealsOf(d), so indices line up and
      // the ratio of the two share counts is exactly this trade's scale onto today
      const booked = dealsOf(d), adjusted = splitAdjustedDeals(d);
      booked.forEach((deal, i) => {
        const scale = num(adjusted[i].shares) / num(deal.shares);
        const paid = num(deal.price) / scale;
        if (paid > 0 && scale > 0) TRADE_VS_NOW_CACHE.set(deal, (d.lastPrice - paid) / paid * 100);
      });
    }
  }
  const v = TRADE_VS_NOW_CACHE.get(t);
  return v === undefined ? NaN : v;
}

/* ---------- display names ---------- */
// No fund flag comes out of Parqet — assetType is "security" for stocks and ETFs alike — so read it
// off the full name. Overridable later by a column in registry/instruments.csv if a fund hides it.
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
  if (!(d.pur > 0) || !(d.cur > 0) || !Number.isFinite(d.years)) return NaN;
  return ((d.cur / d.pur) ** (1 / d.years) - 1) * 100;
}

/* ---------- build ---------- */
const nameColor = name => `hsl(${nameHue(name).toFixed(1)}deg var(--core-s) var(--core-l))`;

function build(rows) {
  CCY = rows[0]?.currency || 'EUR';

  // Cash accounts are dropped here and exist nowhere downstream. They have no price series and no
  // dated balance history, so they could never be reconstructed for a past date — every as-of pick
  // already filtered them out, which left the same portfolio worth two different amounts depending
  // on whether a range was selected. The only cash equivalent held here is Berkshire, and that is
  // an ordinary security needing nothing special.
  const items = rows.filter(r => (r.assetType || '').toLowerCase() !== 'cash').map(r => {
    const cur = num(r.currentValue), pur = num(r.purchaseValue);
    const gain = cur - pur;
    const state = pur === 0 || Math.abs(gain) < 0.005 ? 'flat' : (gain > 0 ? 'gain' : 'loss');
    return {
      portfolio: r.portfolio || 'Portfolio', name: r.name, shares: num(r.shares),
      cur, pur, purAbs: pur, gain, ret: pur > 0 ? gain / pur * 100 : 0, state,
      rel: num(r.realizedGainNet),
      firstActivity: r.earliestActivityDate || '',
      identifier: r.identifier || '',
      lastPrice: num(r.lastPrice),
      lastPriceDate: r.lastPriceDate || '',
      sold: r.isSold === '1' || r.isSold === 'true',
      activityCount: num(r.activityCount),
    };
  });

  items.forEach(d => {
    // prices/ is the price of record wherever it is fresher than the export, which is the normal
    // case: positions.csv is a snapshot from whenever Parqet was last pulled, while
    // update_prices.py runs on its own schedule and usually carries several more sessions.
    //
    // Taking the quote means taking the value with it. This used to apply to sold positions only,
    // on the grounds that currentValue and lastPrice have to describe the same moment — true, and
    // the fix is to recompute the value rather than to keep the stale quote. Parqet's currentValue
    // is exactly shares × lastPrice (they agree to the cent on every open position in the export),
    // so shares × the fresh close is the same figure a few days later, and the two stay in step.
    // Leaving cur on the old quote while the series moved on is what made a range pick and the
    // live map disagree about what a position is worth *today*.
    //
    // Share counts need no adjustment: Parqet's `shares` is the holding as it stands now, and the
    // provider's closes are split-adjusted to that same current-day scale.
    const fresh = PRICES.get(d.identifier);
    if (fresh && fresh.price > 0 && (!d.lastPriceDate || fresh.asof > d.lastPriceDate)) {
      d.lastPrice = fresh.price;
      d.lastPriceDate = fresh.asof;
      d.priceSource = fresh.symbol;   // nothing renders this; it's here to inspect in devtools
                                      // when a hand-maintained price looks wrong
      // applyMode() recomputes gain/ret/state from cur below, but not before this loop finishes —
      // keep the object self-consistent in between rather than briefly reporting a gain that
      // belongs to the old quote.
      d.cur = d.shares * fresh.price;
      d.gain = d.cur - d.pur;
      d.ret = d.pur > 0 ? d.gain / d.pur * 100 : 0;
      d.state = !(d.pur > 0) || Math.abs(d.gain) < 0.005 ? 'flat' : (d.gain > 0 ? 'gain' : 'loss');
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
    d.benchAlt = benchAlternative(d);
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
    d.state = (!(d.pur > 0) || Math.abs(d.gain) < 0.005) ? 'flat' : (d.gain > 0 ? 'gain' : 'loss');
    d.core = nameColor(d.label);
  });
}
const barValue = d => (d.isTax || d.isDiv) ? d.relPre
  : (MODE === 'rel' ? (Number.isFinite(d.alpha) ? d.alpha : 0) : d.relPre);

function totals(rows) {
  const cur = rows.reduce((s, d) => s + d.cur, 0);
  const pur = rows.reduce((s, d) => s + d.pur, 0);
  return { cur, pur, gain: cur - pur, rel: rows.reduce((s, d) => s + (d.relPre ?? d.rel), 0) };
}

/* ---------- ingest ----------
   config.json plus the six CSVs in, the whole model out. Called by load() in portfolio.view.js,
   which renders what this leaves behind; nothing here touches the page. */
function ingest(configText, text, tradesText, instrumentsText, sourcesText, benchText, latestText) {
  // malformed or missing config.json keeps the built-in defaults rather than failing the page —
  // same "absent input degrades gracefully" rule every other file here follows
  let benchIsin = '';
  try {
    const cfg = configText ? JSON.parse(configText) : {};
    if (cfg.timelineStart) TIMELINE_START = cfg.timelineStart;
    if (cfg.benchmarkLabel) BENCH_LABEL = cfg.benchmarkLabel;
    if (cfg.benchmarkIsin) benchIsin = cfg.benchmarkIsin;
  } catch { /* keep defaults */ }

  // one registry row per instrument, indexed by ISIN. NAMES/SECTORS stay as they were so every
  // reader downstream is unchanged; only where they are filled from has moved. The by-name index
  // that used to sit here existed for cash alone, which carries no ISIN and is no longer tracked.
  INSTRUMENTS = new Map();
  NAMES = new Map();
  SECTORS = new Map();
  (instrumentsText ? parseCSV(instrumentsText) : []).forEach(r => {
    if (!r.id) return;
    INSTRUMENTS.set(r.id, r);
    if (r.isin) INSTRUMENTS.set(r.isin, r);
    if (r.display) NAMES.set(r.isin || r.name, r.display);
    if (r.isin && r.sector) SECTORS.set(r.isin, r.sector);
  });
  if (benchIsin && INSTRUMENTS.has(benchIsin)) BENCH_LABEL = INSTRUMENTS.get(benchIsin).display;

  // one row per instrument now — no priority to pick among, so this is a straight load
  SOURCES = new Map((sourcesText ? parseCSV(sourcesText) : [])
    .filter(r => r.id)
    .map(r => [r.id, r]));

  // the last close update_prices.py stored per instrument — same shape the hand-kept
  // parqet_prices.csv used to supply, now a by-product of the fetch instead of a chore
  PRICES = new Map((latestText ? parseCSV(latestText) : [])
    .filter(r => r.id && r.close !== '' && r.date)
    .map(r => [r.id, { price: num(r.close), asof: r.date, symbol: r.source }]));
  BENCH = (benchText ? parseCSV(splitMeta(benchText).body) : [])
    .map(r => ({ date: r.date, close: num(r.close) }))
    .filter(r => r.date && r.close > 0)
    .sort((a, b) => a.date < b.date ? -1 : 1);
  TRADES = tradesText ? parseCSV(tradesText) : [];
  indexTrades();
  const all = build(parseCSV(text));
  ITEMS = all.filter(d => !d.sold);
  CLOSED = all.filter(d => d.sold).sort((a, b) => b.rel - a.rel);
  if (!ITEMS.length) throw new Error('no rows with a positive value');
}
