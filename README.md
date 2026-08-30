Custom visualization for portfolio data pulled from Parqet and (currently) Yahoo.
Coded by (and for) Claude, I only made the design decisions.

To init:
1. enable Parqet Claude connector (either via Parqet or Claude web)
2. clone repo
3. config.json contains a few settings; adjust timeline start to your liking (careful, 30 years back will need to pull a lot of price history that is probably irrelevant to today's stock market)
4. run Claude in repo; tell it to REFRESH_PARQET_DATA.md; this pulls all port transactions into private-parqet/ (private*/ is in .gitignore)
5. run update_prices.py (or tell Claude); this pulls daily EOB ticks for 1.1.2019..today for all relevant stocks from Yahoo (thank you Y).
6. run start.sh for local webserver

Notes:
- you can tell Claude directly to analyze the known data (like 'calc the max downdraw for all my stocks' or 'how many successful trades did i do in 2025?')
- design and calculations uses both standard methods and my personal tweaks
- undocumented: in stock charts, you can click to set 0% line; click twice to go back to std; drag to select custom timeline
- work in progress and not battle tested; may contain calc errors. Contains inaccuracies (e.g. dividends are not reliably considered for gain calc; negligible with my tech stocks)

Open Questions? Ask Claude, it's really good with both coding and finance math :)
