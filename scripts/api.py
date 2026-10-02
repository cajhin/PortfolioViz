#!/usr/bin/env python3
"""What the page reads, out of the database: the api/ routes server.py answers.

    python3 scripts/api.py 'api/prices?id=IE00B4L5Y983'     # one route's body on stdout

A route answers in the shape of the file it replaced — a CSV with the same header, or the same
JSON — so the page parses it exactly as it parsed the file. The command line form is how
check_portfolio.js reads the same routes without a server, through this same code.

    api/config                      the global settings (was config.json)
    api/instruments                 the registry (was registry/instruments.csv)
    api/price-sources                                    (registry/price_sources.csv)
    api/sector-colors                                    (registry/sector_colors.csv)
    api/prices?id=ID                one instrument's closes (gen_prices/<id>-<slug>.csv)
    api/latest                      the newest close per instrument (gen_prices/_latest.csv)
    api/live                        live prices fresher than any close (gen_prices/_live.csv)
    api/profile?profile=P           a profile's own settings (private-profiles/<p>/profile.json)
    api/positions?profile=P         its positions (positions.csv), open and closed
    api/activities?profile=P        its activities (activities.csv)
    api/cash?profile=P              its cash bookings (cash.csv) — none for a Parqet profile
An unknown profile is a 404 on each of the four.
"""
import csv, io, json, sys, urllib.parse

import db


def to_csv(fields, rows):
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(fields)
    w.writerows([db.text(r.get(f)) for f in fields] for r in rows)
    return out.getvalue()


def get(path):
    """(status, content type, body) for one route — path as the page asks for it, query included."""
    url = urllib.parse.urlparse(path)
    route = url.path.lstrip("/")
    q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
    CSV, JSON = "text/csv; charset=utf-8", "application/json"
    if route == "api/config":
        return 200, JSON, json.dumps(db.settings(""), indent=2, ensure_ascii=False)
    if route == "api/instruments":
        return 200, CSV, to_csv(db.INSTRUMENT_FIELDS, db.instruments())
    if route == "api/price-sources":
        return 200, CSV, to_csv(db.SOURCE_FIELDS, db.sources().values())
    if route == "api/sector-colors":
        return 200, CSV, to_csv(db.SECTOR_COLOR_FIELDS, db.sector_colors())
    if route == "api/prices":
        rows = db.series(q.get("id", ""))
        return (200, CSV, to_csv(db.SERIES_FIELDS, rows)) if rows else (404, CSV, "")
    if route == "api/latest":
        return 200, CSV, to_csv(db.LATEST_FIELDS, db.latest().values())
    if route == "api/live":
        return 200, CSV, to_csv(db.LIVE_FIELDS, db.live())
    if route in ("api/profile", "api/positions", "api/activities", "api/cash"):
        name = q.get("profile", "")
        cfg = db.profile_config(name)
        if cfg is None:
            return 404, CSV, ""
        if route == "api/profile":
            return 200, JSON, json.dumps(cfg, indent=2, ensure_ascii=False)
        table = {"api/positions": "position", "api/activities": "activity", "api/cash": "cash"}[route]
        return 200, CSV, to_csv(db.PROFILE_TABLES[table], db.profile_rows(name, table))
    return 404, CSV, ""


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__.strip().split("\n\n")[1])
    status, _, body = get(sys.argv[1])
    sys.stdout.write(body)
    sys.exit(0 if status == 200 else 4)


if __name__ == "__main__":
    main()
