# Trading rules for demo accounts

The rules `trade.py` enforces on every demo account. They are the same for every account, so
results can be compared fairly.

## Time and price

- **Live only.** No trade, deposit or withdrawal can be dated: everything happens now.
- **Where trades are priced.** A buy or sell executes at once at a live price:
  1. **gettex** (Munich), weekdays 08:00–22:00 German time, if it has a quote under 15 minutes old.
     gettex quotes European and US stocks and most ETFs in euros. **A buy pays the ask, a sale gets
     the bid** — the gap between them (the spread) is a real cost, as at any broker: around 0.02% for
     a large US stock, 0.5% for a mid-sized German one, several percent for thinly traded names.
  2. Otherwise the instrument's **home exchange**, but only during its regular session — e.g.
     09:30–16:00 New York time for NVIDIA, 09:00–15:30 Tokyo time for Lasertec — and only with a price
     under 30 minutes old (some exchanges reach us about 15 minutes late; that is allowed for).
     It gives a last price but no bid/ask, so **a buy pays that price +1% and a sale gets it −1%**,
     in place of a spread.
  3. Otherwise **no trade**: the answer says why, and when the instrument can next be traded. A last
     close is never used — the instrument goes on moving elsewhere while it stands still.
- **`trade.py quote <instrument> [<instrument> ...]`** shows what a buy and a sale would get right
  now, where, and the spread — or why no trade is possible.
- **Currency.** All prices, fees and taxes are in euros. gettex quotes in euros; a home-exchange
  price in another currency is converted at that day's rate.
- **Valuation.** Positions are valued at their home exchange's latest price. While that exchange is
  closed and gettex is open with a fresh quote, at gettex's **bid** instead — what the position could
  be sold for right now. `trade.py status` says which, per position (`price_source`).

## Costs

| | |
|---|---|
| **Fee, every buy and every sell** | €10 |
| **Tax, every sale with a gain** | 20% of the gain |
| **Spread, on gettex** | buy at the ask, sell at the bid (see Time and price) |
| **Spread, at the home exchange** | 1% per trade: buy at the price +1%, sell at −1% |

- The fee and the spread are what make trading in and out on every move a losing game. The home
  exchange's 1% is more than gettex's spread for most stocks, so gettex is usually the cheaper venue.
- **Gain** = sale value − the sale's fee − what the sold shares cost. Cost is taken oldest-bought first
  (FIFO) and includes the fees paid when buying them.
- A sale at a loss pays no tax. Losses are **not** carried forward against later gains.

## Cash

- Cash can never go below zero. A buy (fee included) or a withdrawal larger than the cash held is refused.
- `buy --eur X` spends exactly X, fee included: `--eur 2000` buys €1,990 of shares and pays the €10 fee.
- `buy --shares N` buys N shares and adds the fee on top.
- A sale's fee and tax come off its proceeds.
- Shares are fractional (six decimals).

## Examples

| Trade | Value | Fee | Tax | Cash change |
|---|---|---|---|---|
| `buy --eur 2000` | 1,990.00 | 10.00 | – | −2,000.00 |
| `buy --shares 10` at €150 | 1,500.00 | 10.00 | – | −1,510.00 |
| sell 15 shares for €3,043.63, which cost €1,753.00 | 3,043.63 | 10.00 | 256.13 (20% of 1,280.63) | +2,777.50 |
| sell for €980.20, at a loss | 980.20 | 10.00 | 0 | +970.20 |

Value is shares × the price the trade got — the ask for a buy, the bid for a sale — so the spread
is already in it.

## Comparing accounts

`trade.py status` reports the account's **result**:
investments at their latest price, plus cash, minus net deposits. Fees and taxes are already in it.
`result_pct` is that result over net deposits — the figure to compare accounts by.

## Limits

- No dividends, no interest on cash, no short selling, no leverage.
- An instrument can be traded only if it has a Yahoo price series. An unknown ISIN is registered on
  its first buy.
