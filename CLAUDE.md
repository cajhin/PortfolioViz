# Working in this repo

One page that draws a portfolio out of CSVs exported from Parqet. No build step, no dependencies,
no framework — served from a local directory and opened in a browser.

```
portfolio.html          markup only; loads the css and the two scripts, in that order
portfolio.css
portfolio.model.js      data and arithmetic — never touches the DOM
portfolio.view.js       everything that reads or writes the page
check_portfolio.js      regression check for both scripts (see below)
update_data_series.py   fetches a position's daily closes into data_series/
REFRESH_PARQET_DATA.md  how to pull fresh CSVs from Parqet — a task for an agent with the MCP tools
parqet/*.csv            positions, trades, names, prices, sectors — gitignored, regenerate them
data_series/*.csv       one file of daily closes per position, plus benchmark.csv — gitignored
```

The two scripts are classic `<script>` tags sharing one global scope — no modules, no imports.
`portfolio.model.js` loads first and declares the state; `portfolio.view.js` reads it. The
dependency arrow points one way: **the model never calls into the view.** Keep it that way — it
is what lets the model be exercised without a browser.

## Running it

`fetch` is blocked on `file://`, so the page must be served:

```bash
python3 -m http.server 8000        # then open http://localhost:8000/portfolio.html
```

## Editing the page

Each script opens with a map of its sections and the invariant it holds to — read that first, and
you will usually only need to open one of the two.

**Check your change against the baseline.** The page has no tests in the usual sense, but
`check_portfolio.js` runs both scripts headlessly against the real CSVs and records every computed
figure and every rendered tooltip. Around any edit:

```bash
node check_portfolio.js --save     # before: record what the code does today
node check_portfolio.js            # after: diff against it
```

A clean run means the numbers and the rendered text are untouched — worth having after any
refactor, since much of the arithmetic here (FIFO lots, XIRR, split detection, the as-of replay)
is easy to break in ways that still render fine. When a change is *meant* to move a number, read
the diff, agree with it, then `--save` over the baseline.

It cannot see layout, colour, or anything needing a real browser. Check those by eye.

## Data

The CSVs and `check_baseline.json` hold real position values and are gitignored — never commit
them, and don't paste figures from them into commit messages or issues.
