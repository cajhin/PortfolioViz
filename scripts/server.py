#!/usr/bin/env python3
"""The page's server: the repo's files, plus the routes the page needs a backend for.

    python3 scripts/server.py [PORT]      # 8000 by default; start.sh runs this

Serves the repo directory (this file's folder's parent) on 127.0.0.1 only: private-profiles/ and
gen_prices/ hold real position values, and there is no access control at all. Most routes run one
of the other scripts and hand back what it printed; the logic lives there.
"""
import http.server, json, os, re, socket, subprocess, sys, urllib.parse, urllib.request

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SCRIPTS)                  # the repo — this file lives in scripts/

# The nine non-static routes this server answers.
#
# POST /update-prices?profile=NAME — the page's "Update prices" button, instead of you running
# update_prices.py by hand. Runs it with just --profile — the instruments that profile holds,
# watches or benchmarks against, incremental from whatever gen_prices/ already has — since a
# button has no way to ask which slug or --from date you meant; use the script directly from a
# terminal for that. Without ?profile= it updates the whole registry. Blocking, not
# streamed: the fetch is normally done well within the timeout below, and a button that just
# shows "Updating..." until the response lands is a lot less code than a progress feed.
#
# GET /live-index?symbol=... — the header's live-index widget. A browser page cannot call
# Yahoo's chart API directly (no CORS allowance there — the same reason update_prices.py runs
# server-side rather than from portfolio.view.js), so this fetches it here and hands back just
# the day's 5-minute bars. Nothing is written to disk — the whole point of this route is that
# it has no memory between requests, unlike every other price this page ever shows. Pre- and
# post-market bars are included where Yahoo has them (US stocks and ETFs; not indices).
#
# GET /profiles — the page's profile picker: one {name, label, source} per private-profiles/<name>/
# that has a positions.csv, label from its profile.json when it has one. A static server cannot
# list a directory in any form a page could rely on, hence a route.
#
# POST /profiles {name, label, source} — the Config tab's "Create new profile", source "parqet"
# or "manual" (see SOURCES below): creates private-profiles/<name>/
# with a profile.json and header-only positions.csv/activities.csv (the schema
# REFRESH_PARQET_DATA.md writes), ready for its first refresh. 409 if it already exists.
# POST /profile-label?profile=NAME {label} — the Config tab's rename: rewrites only the label
# in that profile's profile.json, every other key kept. The directory name never changes — it is
# what URLs, baselines and the refresh task refer to the profile by.
#
# POST /import-tr?profile=NAME&name=FILE.csv — the Config tab's "Import Trade Republic file":
# the body is the export itself. Kept in private-profiles/<name>/exports/ (the record of what
# was imported), then import_tr.py merges it into that profile — rows already imported are
# skipped by their transaction_id, so uploading an overlapping or repeated export is harmless.
# Manual profiles only — a Parqet one gets a 403 before anything is written.
#
# POST /manual-tx?profile=NAME {date, isin, type, shares, price, fee} — the Config tab's "Add
# transaction", for building a virtual demo portfolio by hand; with &edit=ID, rewrites that
# hand-entered row (same body, instrument ignored); with &delete=ID, removes one. A body of
# {date, type: deposit|withdrawal, amount} books cash instead. Both run manual_tx.py, which validates, writes manual_ledger.csv and rebuilds the profile.
# Manual profiles only, like the import.
#
# GET /instrument-search?find=NAME | ?symbols=ISIN and POST /instrument {isin, symbol, name,
# sector} — the Transactions tab's "new instrument" dialog: instruments by name (with their
# ISIN), Yahoo's listings of the one picked, then registering it (registry/instruments.csv +
# price_sources.csv) and fetching its prices. All run add_instrument.py; /instrument answers
# {instrument, log} as JSON, or the reason as text.
#
# GET /host — the machine this server runs on, so the page can tell the dev copy from the live one
# (see markDevHost). Not knowable from the browser: both are reached through localhost or a proxy.
#

PROFILE_NAME = re.compile(r"^[A-Za-z0-9_-]+$")   # a directory name, never a path

# A profile is controlled by Parqet (REFRESH_PARQET_DATA.md writes its CSVs) or manually (imports
# from the Config tab). profile.json says which; anything else, or nothing, counts as Parqet — the
# side that refuses manual imports, so a slip can never overwrite a Parqet export.
SOURCES = ("parqet", "manual")

def read_profile(name):
    try:
        with open(os.path.join(ROOT, "private-profiles", name, "profile.json")) as fh:
            cfg = json.load(fh)
    except (OSError, ValueError):
        cfg = {}
    return {"name": name, "label": cfg.get("label") or name,
            "source": "manual" if cfg.get("source") == "manual" else "parqet"}

def list_profiles():
    root = os.path.join(ROOT, "private-profiles")
    return [read_profile(name)
            for name in (sorted(os.listdir(root)) if os.path.isdir(root) else [])
            if PROFILE_NAME.match(name) and os.path.isfile(os.path.join(root, name, "positions.csv"))]

POSITIONS_HEADER = ("portfolio,name,identifier,assetType,isSold,shares,currency,currentValue,"
                    "purchaseValue,lastPriceDate,lastPrice,realizedGainNet,unrealizedGainNet,"
                    "earliestActivityDate,activityCount\n")
ACTIVITIES_HEADER = ("portfolio,name,identifier,type,datetime,shares,price,amount,amountNet,fee,tax,"
                     "realizedGains,realizedGainsNet,currency\n")

def set_profile_label(name, label):
    path = os.path.join(ROOT, "private-profiles", name, "profile.json")
    with open(path) as fh:
        cfg = json.load(fh)
    cfg["label"] = label
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(cfg, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)

def create_profile(name, label, source):
    d = os.path.join(ROOT, "private-profiles", name)
    os.makedirs(d)                      # FileExistsError if taken — the caller answers 409
    with open(os.path.join(d, "profile.json"), "w") as fh:
        json.dump({"label": label, "source": source, "watchlist": []}, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    for f, head in (("positions.csv", POSITIONS_HEADER), ("activities.csv", ACTIVITIES_HEADER)):
        with open(os.path.join(d, f), "w") as fh:
            fh.write(head)

class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def handle_error(self, request, client_address):
        # A client that vanished mid-response (tab closed, laptop slept/resumed) shows up here
        # as a broken pipe or reset connection — cosmetic, not a bug in this server. Anything
        # else still gets the normal traceback.
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/profiles":
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                name, label = str(req.get("name") or ""), str(req.get("label") or "").strip()
                source = str(req.get("source") or "")
            except ValueError:
                name, label, source = "", "", ""
            if not PROFILE_NAME.match(name):
                self.send_error(400, "bad profile name")
                return
            if source not in SOURCES:
                self.send_error(400, "source must be parqet or manual")
                return
            try:
                create_profile(name, label or name, source)
            except FileExistsError:
                self.send_error(409, f"profile {name} already exists")
                return
            body = json.dumps({"name": name}).encode("utf-8")
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/profile-label":
            profile = (urllib.parse.parse_qs(parsed.query).get("profile") or [""])[0]
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                label = str(req.get("label") or "").strip()
            except ValueError:
                label = ""
            if not PROFILE_NAME.match(profile) or not label:
                self.send_error(400, "bad profile or empty name")
                return
            try:
                set_profile_label(profile, label)
            except (OSError, ValueError):
                self.send_error(404, f"no profile.json for {profile}")
                return
            self.send_response(204)
            self.end_headers()
            return
        if parsed.path == "/instrument":
            self.add_instrument()
            return
        if parsed.path == "/manual-tx":
            self.manual_tx(urllib.parse.parse_qs(parsed.query))
            return
        if parsed.path == "/import-tr":
            self.import_tr(urllib.parse.parse_qs(parsed.query))
            return
        if parsed.path != "/update-prices":
            self.send_error(404)
            return
        profile = (urllib.parse.parse_qs(parsed.query).get("profile") or [""])[0]
        if profile and not PROFILE_NAME.match(profile):
            self.send_error(400, "bad profile name")
            return
        try:
            proc = subprocess.run(
                [sys.executable, os.path.join(SCRIPTS, "update_prices.py")]
                + (["--profile", profile] if profile else []),
                capture_output=True, text=True, timeout=300)
            body = (proc.stdout + proc.stderr).encode("utf-8")
            status = 200 if proc.returncode == 0 else 500
        except subprocess.TimeoutExpired as e:
            out = (e.stdout or "") + (e.stderr or "")
            body = f"timed out after {e.timeout:.0f}s\n{out}".encode("utf-8")
            status = 504
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_text(self, status, text):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def add_instrument(self):
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            args = [str(req.get(k) or "") for k in ("isin", "symbol", "name", "sector")]
        except ValueError:
            self.send_error(400, "body must be JSON")
            return
        proc = subprocess.run([sys.executable, os.path.join(SCRIPTS, "add_instrument.py")] + args,
                              capture_output=True, text=True, timeout=240)
        out = (proc.stdout + proc.stderr).strip()
        if proc.returncode != 0:
            self.send_text(400, out)
            return
        # the script's last line is the new registry row, as JSON; everything above it is the log
        log, _, last = out.rpartition("\n")
        body = json.dumps({"instrument": json.loads(last), "log": log}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def manual_tx(self, query):
        data = self.rfile.read(int(self.headers.get("Content-Length") or 0))   # before any refusal
        profile = (query.get("profile") or [""])[0]
        if not PROFILE_NAME.match(profile) or read_profile(profile)["source"] != "manual":
            self.send_error(403, "not a manual profile")
            return
        delete = (query.get("delete") or [""])[0]
        edit = (query.get("edit") or [""])[0]
        if delete:
            args = ["delete", delete]
        else:
            try:
                tx = json.loads(data or b"{}")
            except ValueError:
                self.send_error(400, "body must be JSON")
                return
            if tx.get("type") in ("deposit", "withdrawal"):
                cash = [str(tx.get("date", "")), str(tx["type"]), str(tx.get("amount", ""))]
                args = ["edit-cash", edit] + cash if edit else ["cash"] + cash
            else:
                fields = [str(tx.get(k, "")) for k in ("date", "isin", "type", "shares", "price")] \
                         + [str(tx.get("fee") or 0)]
                # an edit keeps its instrument: the id names the row, the rest is what it becomes
                args = ["edit", edit] + fields[:1] + fields[2:] if edit else ["add"] + fields
        proc = subprocess.run([sys.executable, os.path.join(SCRIPTS, "manual_tx.py"), profile] + args,
                              capture_output=True, text=True, timeout=60)
        self.send_text(200 if proc.returncode == 0 else 400, proc.stdout + proc.stderr)

    def import_tr(self, query):
        size = int(self.headers.get("Content-Length") or 0)
        if not 0 < size <= 20 * 1024 * 1024:
            self.send_error(400, "empty or oversized upload")
            return
        # read before any refusal: answering mid-upload closes the connection under the client,
        # which then reports a reset instead of the reason
        data = self.rfile.read(size)
        profile = (query.get("profile") or [""])[0]
        if not PROFILE_NAME.match(profile) or not os.path.isfile(
                os.path.join(ROOT, "private-profiles", profile, "profile.json")):
            self.send_error(400, "unknown profile")
            return
        if read_profile(profile)["source"] != "manual":
            # ASCII only: the reason goes into the status line, which is latin-1
            self.send_error(403, f"{profile} is controlled by Parqet - no manual imports")
            return
        # the uploaded name, reduced to something safe to put in a path
        name = re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.basename((query.get("name") or [""])[0])).lstrip(".")
        if not name.lower().endswith(".csv"):
            name = (name or "transactions") + ".csv"
        exports = os.path.join(ROOT, "private-profiles", profile, "exports")
        existed = os.path.isdir(exports)
        os.makedirs(exports, exist_ok=True)
        path = os.path.join(exports, name)
        with open(path, "wb") as fh:
            fh.write(data)
        proc = subprocess.run([sys.executable, os.path.join(SCRIPTS, "import_tr.py"), profile, path],
                              capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            # refused (not a TR export, or a Parqet-fed profile): keep no record of it
            os.remove(path)
            if not existed:
                os.rmdir(exports)
        body = (proc.stdout + proc.stderr).encode("utf-8")
        self.send_response(200 if proc.returncode == 0 else 500)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # The bare directory means the page, not a listing of it. Relative, so it resolves
        # correctly behind a proxy that mounts this server under a sub-path.
        if urllib.parse.urlparse(self.path).path == "/":
            self.send_response(302)
            self.send_header("Location", "portfolio.html")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if urllib.parse.urlparse(self.path).path == "/instrument-search":
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            kind = "find" if "find" in query else "symbols"
            q = (query.get(kind) or [""])[0]
            proc = subprocess.run([sys.executable, os.path.join(SCRIPTS, "add_instrument.py"),
                                   "--" + kind, q], capture_output=True, text=True, timeout=30)
            if proc.returncode != 0:
                self.send_text(502, (proc.stdout + proc.stderr).strip())
                return
            body = proc.stdout.strip().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if urllib.parse.urlparse(self.path).path in ("/profiles", "/host"):
            body = json.dumps(list_profiles() if self.path.startswith("/profiles")
                              else {"host": socket.gethostname()}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if urllib.parse.urlparse(self.path).path != "/live-index":
            super().do_GET()
            return
        symbol = (urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("symbol") or [""])[0]
        if not symbol:
            self.send_error(400, "missing ?symbol=")
            return
        try:
            # with pre/post-market bars first; a symbol that has none (an index) gets no bars at all
            # that way before its open, so it is asked again for its last regular session instead
            for prepost in ("true", "false"):
                url = ("https://query1.finance.yahoo.com/v8/finance/chart/" + urllib.parse.quote(symbol, safe="")
                       + "?interval=5m&range=1d&includePrePost=" + prepost)
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=10) as r:
                    raw = json.load(r)
                result = raw["chart"]["result"][0]
                if result.get("timestamp"):
                    break
            quote = result["indicators"]["quote"][0]
            meta = result.get("meta") or {}
            # Yahoo pairs pre-market bars with the close from the session BEFORE the last one; when
            # the last regular trade predates every bar here, that trade is the previous close
            prev = meta.get("chartPreviousClose")
            if result.get("timestamp") and (meta.get("regularMarketTime") or 0) < result["timestamp"][0]:
                prev = meta.get("regularMarketPrice") or prev
            body = json.dumps({
                "symbol": symbol,
                "times": result["timestamp"],
                "closes": quote["close"],
                "highs": quote["high"],
                "lows": quote["low"],
                "previousClose": prev,
                # "USD", "EUR", etc. An index (^NDX and friends) has no real currency to quote in,
                # but Yahoo still fills this in with something regardless; the page decides for
                # itself whether the symbol is an index at all (see renderLiveIndex, isIndex)
                # rather than trust that.
                "currency": meta.get("currency"),
            }).encode("utf-8")
            status = 200
        except Exception as e:
            body = json.dumps({"error": str(e)}).encode("utf-8")
            status = 502
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

if __name__ == "__main__":
    os.chdir(ROOT)                  # what the static half serves
    http.server.test(HandlerClass=QuietHandler, port=int(sys.argv[1]) if len(sys.argv) > 1 else 8000,
                     bind="127.0.0.1")
