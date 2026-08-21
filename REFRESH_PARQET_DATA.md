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

`data_series/benchmark.csv` holds daily closes of the benchmark fund. Its constants sit once in
`# key=value` comment lines above the header, and the rows are just `date,close`:

```
# symbol=EUNL.DE
# name=iShares Core MSCI World UCITS ETF (IE00B4L5Y983)
# currency=EUR
date,close
2019-08-20,51.086
```

The fund is iShares Core MSCI World, Xetra ticker EUNL.DE — the same ISIN as the Comdirect holding.

**Update it incrementally**, don't refetch seven years:

```bash
python3 update_data_series.py                              # update every series in data_series/
python3 update_data_series.py benchmark                    # just this one series
python3 update_data_series.py benchmark --from 2019-01-01  # also backfill, from that date
```

Clicking a tile in the map opens a detail overlay that charts that position against the benchmark,
both indexed to 100 at the position's first activity. It looks for `data_series/<slug>.csv`, where the
slug is the display name lowercased with non-alphanumerics stripped (`Amazon` → `amazon`); if the file
is missing it says so and names the command that would create it.

`update_data_series.py` works for any series under `data_series/`: it takes the file's stem as its
argument and reads the Yahoo symbol from the file's own header. To add another series — a second
benchmark, an index, a currency pair — create `data_series/<name>.csv` with just the `# symbol=`,
`# name=`, `# currency=` lines and a `date,close` header, then run the script against it.

The script reads `# symbol=` from the file, asks Yahoo only for the days from the last stored close
onward (or from `--from`, to extend the series backwards), and merges — refetching the overlap day on purpose, since the newest row may have been an
intraday value when it was written. It prints how many rows were added and corrected. The page reads
the last row as "today" for the benchmark comparison, so run it whenever the positions are refreshed.

`parqet/parqet_prices.csv` (`identifier,name,price,currency,asof,symbol,source`) carries a fresh price for
each **closed** position, since Parqet freezes their quotes at the sale and the page needs a current
one to answer "what if I had held on". Also from Yahoo: resolve the ISIN with
`https://query1.finance.yahoo.com/v1/finance/search?q=<ISIN>`, then quote the symbol with the chart
endpoint, preferring a EUR listing and converting USD/GBp with `EURUSD=X` / `EURGBP=X` when there
isn't one. Check every match by name — the ISIN search returned iShares **S&P SmallCap 600** for the
MSCI Japan Small Cap ISIN — and sanity-check each price against the frozen one in
`parqet/parqet_all_port.csv`. Expired warrants get `price 0` and a note. Ask before fetching.

There is also a hand-maintained file, `parqet/parqet_names.csv` (`identifier,name,display,note`),
mapping ISIN — or the exact name, for cash rows — to the short label the page prints. It is not
regenerated here: after a refresh, add a line for any position it does not yet cover.

Last run: 49 rows — 32 open (30 securities + 2 cash) and 17 closed.

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

## 5. Check before you call it done

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
PY
```

All four must hold: no unnamed trades, no trade whose `(portfolio, ISIN)` is missing from the
positions file, no sell failing the net-amount identity, and totals in the same ballpark as the
archived files. Then load `http://localhost:8765/portfolio.html` (serve the folder with
`python3 -m http.server 8765`) and confirm the map renders and the realised bar under it shows both
the open and closed groups — the page fetches with `cache: no-store`, so a plain reload is enough.

Report: the archive names you created, the new row counts, and the headline current value.
