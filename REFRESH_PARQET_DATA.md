# Refresh the Parqet data files

Task for an agent with the **Parqet MCP tools** and write access to `/Users/jjj/git/parqet`.

Goal: pull fresh data from Parqet, write it as two CSVs with **exactly the schema below**, and
import them into the portfolio with `scripts/import_parqet.py`. All data lives in the database
(`data/portfolio.db`); the page reads it from there. The two CSVs are only the import's input —
kept, timestamped, as the record of what was imported. Never write to the database any other way
than through the scripts named here.

**Which portfolio.** Refresh one portfolio at a time — the one the user names, else the
`defaultPortfolio` setting (`python3 scripts/db.py config`). Only a Parqet portfolio is yours to
refresh; a manual one is filled by Trade Republic imports and hand entries, and import_parqet.py
refuses it — stop and say so instead. List them with

```bash
python3 scripts/db.py query "SELECT name, label, source FROM portfolio"
python3 scripts/db.py config --portfolio <portfolio>     # its own settings
```

Its settings may carry `"parqetPortfolios": [...]`, the names of Parqet's own portfolios — its
depots — that belong to it; pull only those. Without that key, pull every one the Parqet login has. Never change another
portfolio. A new portfolio is made on the page's Config tab — create one only when the user asks.

---

## 1. Where the files go

Both files go into the portfolio's exports folder, named by the time of the pull — so earlier
imports are never overwritten:

```bash
cd /Users/jjj/git/portfolioviz
P=<portfolio>; TS=$(date +%Y%m%d-%H%M)
mkdir -p private-portfolios/$P/exports
# write private-portfolios/$P/exports/positions_$TS.csv and .../activities_$TS.csv (sections 3, 4)
```

If either name already exists, stop and report rather than clobbering it.

## 2. Pull fresh data

In this section "portfolio" is Parqet's word, as its tools use it: one depot.

Portfolio IDs are not hardcoded here — they are per-account and this file is committed. Call
`parqet_list_portfolios` first to get the current IDs (for `main`: Trade Republic, Comdirect,
Schwab), keep the ones this portfolio covers, and re-read them if any later call 404s.

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

## 3. Write `positions_<TS>.csv`

One row per position, **open and closed**, plus the cash accounts. Header, in order:

```
depot,name,identifier,assetType,isSold,shares,currency,currentValue,purchaseValue,
lastPriceDate,lastPrice,realizedGainNet,unrealizedGainNet,earliestActivityDate,activityCount
```

- `depot` — the display name of Parqet's portfolio (`Trade Republic`, `Comdirect`, `Schwab`), not
  the ID. Parqet calls each depot a portfolio; here a portfolio is the whole the page draws.
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

## 4. Write `activities_<TS>.csv`

One row per activity, all depots, sorted by `depot` then `datetime` ascending. Header:

```
depot,name,identifier,type,datetime,shares,price,amount,amountNet,fee,tax,
realizedGains,realizedGainsNet,currency
```

- `type` — `buy` | `sell` | `dividend` | `fees_taxes` (whatever the API returns).
- `name` — resolve from the positions file by `(depot, ISIN)`; every row must end up named.
- `datetime` — the API's ISO string, unchanged.
- `amountNet` — as delivered: buys = gross **+** fee, sells = gross **−** tax **−** fee,
  dividends after withholding. The page's XIRR runs on this column, so do not recompute it.
- `realizedGains` (gross) and `realizedGainsNet` (after tax and fees) — both, on sell rows;
  `0` elsewhere.
- `fees_taxes` rows are standalone tax bookings (German Vorabpauschale) with `amount = 0` and the
  amount in `amountNet`/`tax`.

Last run: 211 rows — 138 buys, 43 sells, 26 dividends, 4 fee/tax bookings.

## 5. Import

```bash
python3 scripts/import_parqet.py $P private-portfolios/$P/exports/positions_$TS.csv private-portfolios/$P/exports/activities_$TS.csv
```

It checks before it writes anything — every activity named, every traded `(depot, ISIN)` also a
position, every sale's net = gross − tax − fee — and refuses the import if one fails: fix the
files and run it again. On success it replaces the portfolio's positions and activities in one go,
prints the counts and headline figures, and lists any held instrument the registry lacks or that
has no price source (section 6).

## 6. Update the registry and the portfolio's watchlist

The registry (tables `instrument` and `price_source`) has one row per instrument, with an `id`
(equal to its ISIN for a security). It carries `display` (the short label the page prints), `slug`
(a short name scripts accept in place of the id), `sector` (the map's grouping) and `type`.
**After a refresh, register any position it does not yet cover** — that is the one
hand-maintenance step left:

```bash
python3 scripts/add_instrument.py --symbols <ISIN>                       # Yahoo's listings, best first
python3 scripts/add_instrument.py <ISIN> <symbol> "<name>" "<sector>"    # registers it, fetches its prices
python3 scripts/db.py upsert instrument id=<ISIN> display="<short label>"   # amend one column
```

The registry is shared by every portfolio, so it is the union of all their instruments — add rows,
never remove one because *this* portfolio no longer holds it.

An instrument needs no matching Parqet holding. That is how the benchmark is charted, and how a
watchlist name gets charted. The `benchmarkIsin` setting names the benchmark by ISIN (a portfolio's
own setting may override it). A watchlist name must also be in the portfolio's own `watchlist`
setting — the registry alone would put it in every portfolio's watchlist:

```bash
python3 scripts/db.py config --portfolio $P set watchlist '["US30303M1027", "NL0009805522"]'
```

## 7. Refresh the price series

Price series are driven by the registry's `price_source` table — **one row per instrument**, keyed
by its `id` — and fetched by `update_prices.py`. Nothing about *how* to fetch an instrument lives in
its series, and there is no hand-maintained price to update.

```bash
python3 scripts/update_prices.py --portfolio <p>    # just what this portfolio holds, watches, benchmarks
python3 scripts/update_prices.py                  # every instrument in the registry, all portfolios
python3 scripts/update_prices.py roche            # just this one (slug or id)
python3 scripts/update_prices.py roche --from 2019-01-01   # also backfill, from that date
```

**Run the bare form on every refresh**, closed positions included. The script has no idea which
positions are open or closed — it walks the registry — so a closed position's series keeps
extending in step with everything else. The freshest close per instrument (the view
`latest_close`) is what supplies the quote Parqet freezes once a position is sold.

Prices are converted to the `currency` setting **on write**, through the `fx_symbol` named in
the registry, with the untouched quote kept in `close_raw`. The browser does no FX at all.

### When a position has no series, or a bad one

Add or amend the one row for it in `price_source`
(`python3 scripts/db.py upsert price_source id=<ISIN> symbol=<symbol> ...`):

| column | meaning |
|---|---|
| `id` | the instrument, matching its `instrument` row |
| `source` | `yahoo` today; `manual` for something unquotable (an expired warrant) |
| `symbol` | the source's own ticker |
| `quote_currency` | what that listing quotes in — `GBp` is pence, and is handled as such |
| `fx_symbol` | the pair to convert through, e.g. `EURUSD=X`; blank when already in `currency` |

Resolve an unknown ISIN with `https://query1.finance.yahoo.com/v1/finance/search?q=<ISIN>`, and
**check the match by name** — that search once returned iShares *S&P SmallCap 600* for the MSCI
Japan Small Cap ISIN. Ask before fetching.

There is no fallback row and no priority: **one instrument, one source.** A thin listing that needs
backing by a liquid one is registered as a *second instrument* instead — its own row in
`instrument`, its own `id` and `slug`, its own row here — never a second row for the same
`id`. SK Hynix's home listing (`000660.KS`, Seoul) replaced its old Frankfurt line entirely rather
than sitting alongside it as a fallback, once it turned out 18% of the Frankfurt line's days had no
real trade behind them.

A gap the chosen source itself cannot fill — a stretch of trading days a thin listing genuinely has
no quote for — is not this script's problem to solve. The chart handles it: any run of missing
dates over ~20 calendar days draws as a dashed line, held flat at the last known price, with a
warning naming the range. Worth a look before spending time hunting a better symbol; the gap may
already be small enough that nobody would notice it unlabelled.

## 8. Check before you call it done

The import (section 5) already checked the three things that make it refuse. What is left:

```bash
cd /Users/jjj/git/portfolioviz
python3 scripts/db.py query "SELECT p.identifier FROM position p LEFT JOIN instrument i ON i.id = p.identifier
  WHERE p.portfolio = '$P' AND p.identifier <> '' AND i.id IS NULL"         # missing from the registry
python3 scripts/db.py query "SELECT DISTINCT p.identifier FROM position p JOIN instrument i ON i.id = p.identifier
  LEFT JOIN price_source s ON s.id = i.id WHERE p.portfolio = '$P' AND s.id IS NULL"   # no price source
```

Both must come back empty (`[]`), and the totals import_parqet.py printed must be in the same
ballpark as the previous import's. Then load `http://localhost:8765/portfolio.html?portfolio=<portfolio>` (serve the folder with
`./start.sh 8765 -n`) and confirm the map renders and the realised bar under it shows both
the open and closed groups — the page fetches with `cache: no-store`, so a plain reload is enough.

Report: the files you wrote, the new row counts, and the headline current value.
