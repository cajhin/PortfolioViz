# Refresh the Parqet data files

Task for an agent with the **Parqet MCP tools** and write access to `/Users/jjj/git/parqet`.

Goal: archive the two current CSVs under their download timestamp, then pull fresh data from Parqet
and write new ones with **exactly the same schema**. `portfolio.html` reads
`parqet/parqet_all_port.csv` and `parqet/parqet_trades.csv` — do not edit the HTML, and do not
rename those two working filenames or move them out of `parqet/`.

---

## 1. Archive the current files

The "dl-timestamp" is the file's own modification time — that is when the data was downloaded.

```bash
cd /Users/jjj/git/parqet/parqet
for f in parqet_all_port parqet_trades; do
  [ -f "$f.csv" ] || continue
  ts=$(date -r "$f.csv" +%Y%m%d-%H%M)      # macOS/BSD date
  mv -n "$f.csv" "${f}_${ts}.csv"
  echo "archived ${f}_${ts}.csv"
done
```

`mv -n` never overwrites: if that archive name already exists, stop and report rather than clobbering
it. Archived files stay in this directory; the page ignores anything but the two working names.

## 2. Pull fresh data

Portfolio IDs (confirm with `parqet_list_portfolios` — re-read them if any call 404s):

| Portfolio | ID |
|---|---|
| Trade Republic | `6927025189ee83d59e7e2327` |
| Comdirect | `692705df48dadacc8cdfd393` |
| Schwab | `6a16f9f33e9d674fc51dfed0` |

Three calls give everything:

1. **Positions incl. closed ones** — `parqet_query_portfolio` with `view: "holdings"`,
   all three IDs in `portfolioIds`, `assetType: "security"`, `includeSold: true`,
   `limit: 500`, `sortBy: "name"`.
2. **Cash accounts** — the same call with `assetType: "cash"` (Verrechnungskonto, Girokonto).
3. **Activities** — `parqet_get_activities` **once per portfolio**, with `assetType: "security"`
   and `limit: 500`. Cash activities (deposits/withdrawals) are deliberately excluded.

Gotchas that will bite you:

- **The combined holdings response carries no `portfolioId` per holding.** Either call
  `parqet_query_portfolio` once per portfolio, or attribute by the ISINs seen in that portfolio's
  activity list. Two portfolios can hold the same ISIN (POET Technologies is open in Trade Republic
  and closed in Comdirect) — never map an ISIN to a portfolio globally.
- **The Trade Republic activity response is too large to return inline.** The tool writes it to a
  file under `~/.claude/projects/-Users-jjj-git-parqet/<session>/tool-results/` and tells you the
  path; read it with `jq`/python, not by pasting it into context.
- Prices move during the day. Pull the positions and the activities in one sitting so the two files
  agree, and note that `currentValue` is a snapshot.

## 3. Write `parqet/parqet_all_port.csv`

One row per position, **open and closed**, plus the cash accounts. Header, in order:

```
portfolio,name,identifier,assetType,isSold,shares,currency,currentValue,purchaseValue,
lastPriceDate,lastPrice,realizedGainNet,unrealizedGainNet,earliestActivityDate,activityCount
```

- `portfolio` — display name (`Trade Republic`, `Comdirect`, `Schwab`), not the ID.
- `identifier` — ISIN; empty for cash rows.
- `isSold` — `1` for a position closed out (`isSold: true`), else `0`. Closed rows carry
  `shares`, `currentValue`, `purchaseValue` = 0 and keep their `realizedGainNet`.
- `unrealizedGainNet` — `currentValue − purchaseValue` for open rows, `0` for closed ones.
- `lastPrice` / `lastPriceDate` — the holding's `currentPrice` and the date of its `quote.datetime`.
  For an open position that is today's quote; for a closed one Parqet freezes it at the sale, which is
  fine — the page labels the figure with this date. Cash rows: price `1`, date = the snapshot date.
- `realizedGainNet` — straight from the API. It is **net of tax and fees**; the page adds sale tax
  back from the trades file to show pre-tax figures, so do not adjust it here.
- Cash rows: keep Parqet's `purchaseValue` (cumulative deposits) as-is. The page recognises
  `assetType == "cash"` and leaves it out of invested/gain figures.
- Numbers: plain decimals, `.` separator, no thousands separator, no currency symbol.

## 4. Write `parqet/parqet_trades.csv`

One row per activity, all portfolios, sorted by `portfolio` then `datetime` ascending. Header:

```
portfolio,name,identifier,type,datetime,shares,price,amount,amountNet,fee,tax,
realizedGains,realizedGainsNet,currency
```

- `type` — `buy` | `sell` | `dividend` | `fees_taxes` (whatever the API returns).
- `name` — resolve from the positions file by `(portfolio, ISIN)`; every row must end up named.
- `datetime` — the API's ISO string, unchanged.
- `amountNet` — as delivered: buys = gross **+** fee, sells = gross **−** tax **−** fee,
  dividends after withholding. The page's XIRR runs on this column, so do not recompute it.
- `realizedGains` (gross) and `realizedGainsNet` (after tax and fees) — both, on sell rows;
  `0` elsewhere.
- `fees_taxes` rows are standalone tax bookings (German Vorabpauschale) with `amount = 0` and the
  amount in `amountNet`/`tax`.

Last run: 211 rows — 138 buys, 43 sells, 26 dividends, 4 fee/tax bookings.

## 5. Update `registry/instruments.csv`

One row per instrument, keyed by ISIN (cash gets a `CASH:<portfolio>` id, since it has none). It
carries `display` (the short label the page prints), `slug` (which fixes the series filename, so
renaming the label can never point the chart at the wrong file), `sector` (the map's grouping) and
`type`. **After a refresh, add a row for any position it does not yet cover** — that is the one
hand-maintenance step left, and it replaces the old `parqet_names.csv` and `parqet_sectors.csv`.

An instrument needs no matching Parqet holding. That is how the benchmark is charted, and how a
watchlist name would be. `config.json` names the benchmark by ISIN (`benchmarkIsin`).

## 6. Refresh the price series

Price series are driven by `registry/price_sources.csv` — one row per instrument per source,
ordered by `priority` — and fetched by `update_data_series.py`. Nothing about *how* to fetch an
instrument lives in the fetched file, and there is no hand-maintained price file left to update.

```bash
python3 update_data_series.py                  # every instrument in the registry
python3 update_data_series.py roche            # just this one (slug or ISIN)
python3 update_data_series.py roche --from 2019-01-01   # also backfill, from that date
```

**Run the bare form on every refresh**, closed positions included. The script has no idea which
positions are open or closed — it walks the registry — so a closed position's series keeps
extending in step with everything else. It also writes `data_series/_latest.csv`, the freshest
close per instrument, which is what supplies the quote Parqet freezes once a position is sold.
That file used to be `parqet/parqet_prices.csv` and used to be maintained by hand; it is now a
by-product of the fetch. Do not recreate it.

Prices are converted to `config.json`'s `currency` **on write**, through the `fx_symbol` named in
the registry, with the untouched quote kept in `close_raw`. The browser does no FX at all.

### When a position has no series, or a bad one

Add or amend a row in `registry/price_sources.csv`:

| column | meaning |
|---|---|
| `isin` | the instrument, matching `registry/instruments.csv` |
| `priority` | 1 is the truth; a higher number is consulted only for dates the lower one lacks |
| `source` | `yahoo` today; `manual` for something unquotable (an expired warrant) |
| `symbol` | the source's own ticker |
| `quote_currency` | what that listing quotes in — `GBp` is pence, and is handled as such |
| `fx_symbol` | the pair to convert through, e.g. `EURUSD=X`; blank when already in `currency` |

Resolve an unknown ISIN with `https://query1.finance.yahoo.com/v1/finance/search?q=<ISIN>`, and
**check the match by name** — that search once returned iShares *S&P SmallCap 600* for the MSCI
Japan Small Cap ISIN. Ask before fetching.

A thin listing is worth a second row rather than a shrug: Roche's `RHO.DE` both gapped for five
years and carried stale quotes, so `RO.SW` (CHF, liquid) is its priority 1 and `RHO.DE` the
fallback. The chart marks any stretch it still cannot fill with a dashed line and a warning.

## 7. Check before you call it done

```bash
cd /Users/jjj/git/parqet
python3 - <<'PY'
import csv, collections
pos = list(csv.DictReader(open('parqet/parqet_all_port.csv')))
tr  = list(csv.DictReader(open('parqet/parqet_trades.csv')))
f = lambda r, k: float(r[k] or 0)
print('positions', len(pos), '| closed', sum(1 for p in pos if p['isSold'] == '1'),
      '| cash', sum(1 for p in pos if p['assetType'] == 'cash'))
print('trades', len(tr), collections.Counter(r['type'] for r in tr))
print('unnamed trades:', sum(1 for r in tr if not r['name']))
print('trades with no matching position:',
      {(r['portfolio'], r['identifier']) for r in tr}
      - {(p['portfolio'], p['identifier']) for p in pos})
bad = [r for r in tr if r['type'] == 'sell'
       and abs((f(r,'amount') - f(r,'tax') - f(r,'fee')) - f(r,'amountNet')) > 0.02]
print('sells where gross-tax-fee != net:', len(bad))
print('total tax', round(sum(f(r,'tax') for r in tr), 2),
      '| current value', round(sum(f(p,'currentValue') for p in pos), 2))

inst = {r['id'] for r in csv.DictReader(open('registry/instruments.csv'))}
src  = {r['isin'] for r in csv.DictReader(open('registry/price_sources.csv'))}
held = {p['identifier'] for p in pos if p['identifier']}
print('positions missing from registry/instruments.csv:', held - inst or 'none')
print('instruments with no price source:', (held & inst) - src or 'none')
PY
```

All six must hold: no unnamed trades, no trade whose `(portfolio, ISIN)` is missing from the
positions file, no sell failing the net-amount identity, nothing missing from the registry, every
held instrument carrying a price source, and totals in the same ballpark as the archived files. Then load `http://localhost:8765/portfolio.html` (serve the folder with
`python3 -m http.server 8765`) and confirm the map renders and the realised bar under it shows both
the open and closed groups — the page fetches with `cache: no-store`, so a plain reload is enough.

Report: the archive names you created, the new row counts, and the headline current value.
