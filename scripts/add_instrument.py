#!/usr/bin/env python3
"""Register a new instrument and fetch its prices — so a hand-entered transaction can use it.

    python3 scripts/add_instrument.py <ISIN> <yahoo symbol> <name> [<sector> [<note>]]
    python3 scripts/add_instrument.py --find <name or ISIN>       # instruments by name, with ISIN (JSON)
    python3 scripts/add_instrument.py --symbols <ISIN>            # Yahoo listings of an ISIN, best first (JSON)

The page's "New instrument" dialog runs the two look-ups in turn: --find turns a name ("Porsche")
into candidates with their ISINs — onvista's search, since Yahoo's carries no ISIN — and, once
one is picked, --symbols asks Yahoo which symbols list that ISIN, best guess first: XETRA, then
another euro exchange, then a home listing without suffix (AAPL), then the rest.

Adds one row to the registry's instrument table and one to price_source (both curated — see
CLAUDE.md), then runs update_prices.py for it from the timelineStart setting. The quote currency
is not asked for: it is read off Yahoo's own answer for the symbol, and the FX pair to convert
through follows from it. If Yahoo returns no prices for the symbol, both rows are taken out again
and nothing is left behind.

Check a --symbols match by name before using it: Yahoo's ISIN search once returned iShares
S&P SmallCap 600 for the MSCI Japan Small Cap ISIN (see REFRESH_PARQET_DATA.md).
"""
import json, os, re, subprocess, sys, urllib.parse, urllib.request

import db

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
UA = {"User-Agent": "Mozilla/5.0"}


def yahoo(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=15) as r:
        return json.load(r)


FINDABLE = {"STOCK": "Equity", "FUND": "Fund", "ETF": "ETF", "ETC": "ETC", "INDEX": "Index"}
EURO_SUFFIXES = (".DE", ".F", ".AS", ".PA", ".MI", ".MC", ".BR", ".VI", ".LS", ".HE", ".IR")


def find(q):
    """Instruments matching a name (or an ISIN) — with their ISIN, which is what the registry is
    keyed by. Bonds, certificates and the like are left out: a demo position wants a quoted price."""
    raw = yahoo("https://api.onvista.de/api/v1/instruments/query?" + urllib.parse.urlencode({"searchValue": q}))
    return [{"isin": x["isin"], "name": x.get("name") or "", "type": FINDABLE[x.get("entityType")]}
            for x in raw.get("list", []) if x.get("isin") and x.get("entityType") in FINDABLE][:10]


def symbols(isin):
    """Yahoo's listings of an ISIN, best guess first, each with Yahoo's sector where it has one."""
    raw = yahoo("https://query1.finance.yahoo.com/v1/finance/search?"
                + urllib.parse.urlencode({"q": isin, "quotesCount": 10, "newsCount": 0}))
    out = [{"symbol": x["symbol"], "name": x.get("longname") or x.get("shortname") or "",
            "exchange": x.get("exchDisp") or x.get("exchange") or "", "type": x.get("quoteType") or "",
            "sector": x.get("sectorDisp") or x.get("sector") or ""}
           for x in raw.get("quotes", []) if x.get("symbol")]

    def rank(x):
        sym = x["symbol"]
        if x["exchange"] == "XETRA" or sym.endswith(".DE"):
            return 0
        if sym.endswith(EURO_SUFFIXES):
            return 1
        return 2 if "." not in sym else 3
    return sorted(out, key=rank)


def quote_currency(symbol):
    raw = yahoo("https://query1.finance.yahoo.com/v8/finance/chart/"
                + urllib.parse.quote(symbol, safe="") + "?interval=1d&range=5d")
    result = (raw.get("chart") or {}).get("result")
    if not result:
        raise LookupError(f"Yahoo knows no symbol {symbol!r}")
    return result[0].get("meta", {}).get("currency") or ""


def add(isin, symbol, name, sector="", note="added from the page"):
    isin, symbol, name = isin.strip().upper(), symbol.strip(), name.strip()
    if not ISIN.match(isin):
        sys.exit(f"{isin!r} is not an ISIN (two letters, nine letters or digits, one check digit)")
    if not symbol or not name:
        sys.exit("a Yahoo symbol and a name are both needed")
    instruments = db.instruments()
    if any(isin in (r["id"], r["isin"]) for r in instruments):
        sys.exit(f"{isin} is already in the registry")
    try:
        ccy = quote_currency(symbol)
    except (LookupError, OSError, ValueError) as err:
        sys.exit(f"{symbol}: {err}")
    home = db.portfolio_currency()
    fx = "" if ccy in ("", home) else f"{home}{'GBP' if ccy == 'GBp' else ccy.upper()}=X"

    # the slug is the short name update_prices.py and trade.py take — lowercase letters and digits
    base = re.sub(r"[^a-z0-9]", "", name.lower())[:20] or isin.lower()
    slugs = {r["slug"] for r in instruments}
    slug, n = base, 2
    while slug in slugs:
        slug, n = f"{base}{n}", n + 1

    with db.tx():
        db.upsert("instrument", {"id": isin, "isin": isin, "slug": slug, "name": name, "display": name,
                                 "type": "security", "currency": home, "sector": sector or "Other",
                                 "note": note or "added from the page"})
        db.upsert("price_source", {"id": isin, "source": "yahoo", "symbol": symbol,
                                   "quote_currency": ccy or home, "fx_symbol": fx, "note": ""})
    start = db.timeline_start()
    proc = subprocess.run([sys.executable, os.path.join(SCRIPTS, "update_prices.py"), isin, "--from", start],
                          capture_output=True, text=True, timeout=180)
    print((proc.stdout + proc.stderr).strip())
    if not db.series_dates(isin):
        db.delete("instrument", isin)            # its price_source row goes with it
        sys.exit(f"no prices came back for {symbol} — {isin} not added")
    print(json.dumps({"id": isin, "isin": isin, "slug": slug, "name": name, "display": name,
                      "sector": sector or "Other", "symbol": symbol, "quote_currency": ccy or home}))


def main():
    args = sys.argv[1:]
    if len(args) == 2 and args[0] == "--find":
        print(json.dumps(find(args[1])))
    elif len(args) == 2 and args[0] == "--symbols":
        print(json.dumps(symbols(args[1])))
    elif len(args) in (3, 4, 5):
        add(*args)
    else:
        sys.exit(__doc__.strip().split("\n\n")[1])


if __name__ == "__main__":
    main()
