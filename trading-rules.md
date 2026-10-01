# Trading rules for demo accounts

The rules `trade.py` enforces on every demo account. They are the same for every account, so
results can be compared fairly.

## Time

- **Live only.** No trade, deposit or withdrawal can be dated: everything happens now.
- **Price.** A buy or sell executes at once at the latest price, fetched fresh just before.
  `trade.py quote <instrument>` shows that price, and whether a trade would be taken now (`tradable_now`).
- **Market hours.** Trades are taken only while the instrument's own exchange is in its regular
  session — e.g. 09:30–16:00 New York time for NVIDIA, 09:00–17:30 Amsterdam time for ASML. Outside
  it the last price is stale while the instrument goes on trading elsewhere, so a trade is refused,
  with the session hours in the answer.
- **Fresh prices only.** A trade is refused if the latest price is more than 30 minutes old. Some
  exchanges reach Yahoo about 15 minutes late; that is allowed for, and nothing more.
- **Currency.** All prices, fees and taxes are in euros. Instruments quoted in another currency are
  converted at that day's rate.

## Costs

| | |
|---|---|
| **Fee, every buy and every sell** | €10 + 1% of the trade's value |
| **Tax, every sale with a gain** | 20% of the gain |

- The fee exists to make trading in and out on every move a losing game.
- **Gain** = sale value − the sale's fee − what the sold shares cost. Cost is taken oldest-bought first
  (FIFO) and includes the fees paid when buying them.
- A sale at a loss pays no tax. Losses are **not** carried forward against later gains.

## Cash

- Cash can never go below zero. A buy (fee included) or a withdrawal larger than the cash held is refused.
- `buy --eur X` spends exactly X, fee included: `--eur 2000` buys €1,970.30 of shares and pays a €29.70 fee.
- `buy --shares N` buys N shares and adds the fee on top.
- A sale's fee and tax come off its proceeds.
- Shares are fractional (six decimals).

## Examples

| Trade | Value | Fee | Tax | Cash change |
|---|---|---|---|---|
| `buy --eur 2000` | 1,970.30 | 29.70 | – | −2,000.00 |
| `buy --shares 10` at €150 | 1,500.00 | 25.00 | – | −1,525.00 |
| sell 15 shares for €3,043.63, which cost €1,753.00 | 3,043.63 | 40.44 | 250.04 (20% of 1,250.19) | +2,753.15 |
| sell for €980.20, at a loss | 980.20 | 19.80 | 0 | +960.40 |

## Comparing accounts

`trade.py status` reports the account's **result**:
investments at their latest price, plus cash, minus net deposits. Fees and taxes are already in it.
`result_pct` is that result over net deposits — the figure to compare accounts by.

## Limits

- No dividends, no interest on cash, no short selling, no leverage.
- An instrument can be traded only if it has a Yahoo price series. An unknown ISIN is registered on
  its first buy.
