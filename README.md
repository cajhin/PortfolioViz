Custom visualization for portfolio data pulled from Parqet and (currently) Yahoo.
Coded by (and for) Claude, I only made the design decisions.

To init:
1. enable Parqet Claude connector (either via Parqet or Claude web)
2. clone repo
3. config.json contains a few settings; adjust timeline start to your liking (default 1.1.2019; careful, 30 years back will need to pull a lot of price history that is probably irrelevant to today's stock market)
4. run Claude in repo; tell it to REFRESH_PARQET_DATA.md; this pulls all port transactions into private-profiles/main/ (private*/ is in .gitignore). For more profiles, use "Create new profile" on the Config tab: it creates an empty private-profiles/<name>/ with a profile.json (label, watchlist, optional benchmark); then fill it from Parqet (have Claude refresh that profile) or make it manual and import a Trade Republic transaction export on the same tab (re-importing an overlapping export only adds what is new), or enter buys and sells by hand to track a virtual demo portfolio
5. run start.sh for local webserver
6. click [Update]; this pulls all missing daily EOB ticks for 1.1.2019..today for the current profile's stocks from Yahoo (thank you Y).

Notes:
- you can tell Claude directly to analyze the known data (like 'calc the max downdraw for all my stocks' or 'how many successful trades did i do in 2025?')
- design and calculations uses both standard methods and my personal tweaks (e.g. Volatility calc discounts upward moves by 50%, standard is either 0% or 100%)
- undocumented: in stock charts, you can click to set 0% line to a specific date, e.g. your buy date; click twice to go back to std; drag to select custom timeline
- work in progress and not battle tested; may contain calc errors. Contains inaccuracies (e.g. dividends are not reliably considered for gain calc; negligible with my tech stocks)

Open Questions? Ask Claude, it's really good with both coding and finance math :)
