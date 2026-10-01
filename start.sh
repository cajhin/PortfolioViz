#!/bin/sh
# Serve this directory so portfolio.html can fetch the CSVs — fetch is blocked on file://.
#
#   ./start.sh          serve on 8000 and open the page
#   ./start.sh 8080     serve on another port
#   ./start.sh -n       don't open a browser
#
# Bound to 127.0.0.1 on purpose: private-profiles/ and gen_prices/ hold real position values, and this
# server has no access control at all. Ctrl-C to stop.

set -eu

port=8000
open_browser=1

for arg in "$@"; do
    case "$arg" in
        -n|--no-open) open_browser=0 ;;
        [0-9]*)       port=$arg ;;
        *)            echo "usage: $0 [port] [-n|--no-open]" >&2; exit 2 ;;
    esac
done

if [ "$port" -lt 1 ] || [ "$port" -gt 65535 ]; then
    echo "start.sh: $port is not a port number" >&2
    exit 2
fi

cd "$(dirname "$0")"

command -v python3 >/dev/null 2>&1 || { echo "start.sh: python3 not found" >&2; exit 1; }

if command -v lsof >/dev/null 2>&1 && lsof -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "start.sh: port $port is already in use — pass another one, e.g. $0 $((port + 1))" >&2
    exit 1
fi

url="http://localhost:$port/portfolio.html"
echo "Serving $(pwd) at $url"

if [ "$open_browser" -eq 1 ] && command -v open >/dev/null 2>&1; then
    # The server isn't listening yet; give it a moment before the browser asks.
    ( sleep 1; open "$url" ) &
fi

exec python3 -c '
import http.server, json, os, re, socket, subprocess, sys, urllib.parse, urllib.request

# The six non-static routes this server answers.
#
# POST /update-prices?profile=NAME — the page'"'"'s "Update prices" button, instead of you running
# update_prices.py by hand. Runs it with just --profile — the instruments that profile holds,
# watches or benchmarks against, incremental from whatever gen_prices/ already has — since a
# button has no way to ask which slug or --from date you meant; use the script directly from a
# terminal for that. Without ?profile= it updates the whole registry. Blocking, not
# streamed: the fetch is normally done well within the timeout below, and a button that just
# shows "Updating..." until the response lands is a lot less code than a progress feed.
#
# GET /live-index?symbol=... — the header'"'"'s live-index widget. A browser page cannot call
# Yahoo'"'"'s chart API directly (no CORS allowance there — the same reason update_prices.py runs
# server-side rather than from portfolio.view.js), so this fetches it here and hands back just
# the day'"'"'s 5-minute bars. Nothing is written to disk — the whole point of this route is that
# it has no memory between requests, unlike every other price this page ever shows.
#
# GET /profiles — the page'"'"'s profile picker: one {name, label} per private-profiles/<name>/
# that has a positions.csv, label from its profile.json when it has one. A static server cannot
# list a directory in any form a page could rely on, hence a route.
#
# POST /profiles {name, label} — the Config tab'"'"'s "Create new profile": creates private-profiles/<name>/
# with a profile.json and header-only positions.csv/activities.csv (the schema
# REFRESH_PARQET_DATA.md writes), ready for its first refresh. 409 if it already exists.
# POST /profile-label?profile=NAME {label} — the Config tab'"'"'s rename: rewrites only the label
# in that profile'"'"'s profile.json, every other key kept. The directory name never changes — it is
# what URLs, baselines and the refresh task refer to the profile by.
#
# POST /import-tr?profile=NAME&name=FILE.csv — the Config tab'"'"'s "Import Trade Republic file":
# the body is the export itself. Kept in private-profiles/<name>/exports/ (the record of what
# was imported), then import_tr.py merges it into that profile — rows already imported are
# skipped by their transaction_id, so uploading an overlapping or repeated export is harmless.
#
# GET /host — the machine this server runs on, so the page can tell the dev copy from the live one
# (see markDevHost). Not knowable from the browser: both are reached through localhost or a proxy.
#
PROFILE_NAME = re.compile(r"^[A-Za-z0-9_-]+$")   # a directory name, never a path

def list_profiles():
    root = os.path.join(os.getcwd(), "private-profiles")
    out = []
    for name in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        d = os.path.join(root, name)
        if not PROFILE_NAME.match(name) or not os.path.isfile(os.path.join(d, "positions.csv")):
            continue
        try:
            with open(os.path.join(d, "profile.json")) as fh:
                label = json.load(fh).get("label") or name
        except (OSError, ValueError):
            label = name
        out.append({"name": name, "label": label})
    return out

POSITIONS_HEADER = ("portfolio,name,identifier,assetType,isSold,shares,currency,currentValue,"
                    "purchaseValue,lastPriceDate,lastPrice,realizedGainNet,unrealizedGainNet,"
                    "earliestActivityDate,activityCount\n")
ACTIVITIES_HEADER = ("portfolio,name,identifier,type,datetime,shares,price,amount,amountNet,fee,tax,"
                     "realizedGains,realizedGainsNet,currency\n")

def set_profile_label(name, label):
    path = os.path.join(os.getcwd(), "private-profiles", name, "profile.json")
    with open(path) as fh:
        cfg = json.load(fh)
    cfg["label"] = label
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(cfg, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)

def create_profile(name, label):
    d = os.path.join(os.getcwd(), "private-profiles", name)
    os.makedirs(d)                      # FileExistsError if taken — the caller answers 409
    with open(os.path.join(d, "profile.json"), "w") as fh:
        json.dump({"label": label, "watchlist": []}, fh, indent=2)
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
            except ValueError:
                name, label = "", ""
            if not PROFILE_NAME.match(name):
                self.send_error(400, "bad profile name")
                return
            try:
                create_profile(name, label or name)
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
                [sys.executable, os.path.join(os.getcwd(), "update_prices.py")]
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

    def import_tr(self, query):
        profile = (query.get("profile") or [""])[0]
        if not PROFILE_NAME.match(profile) or not os.path.isfile(
                os.path.join(os.getcwd(), "private-profiles", profile, "profile.json")):
            self.send_error(400, "unknown profile")
            return
        size = int(self.headers.get("Content-Length") or 0)
        if not 0 < size <= 20 * 1024 * 1024:
            self.send_error(400, "empty or oversized upload")
            return
        # the uploaded name, reduced to something safe to put in a path
        name = re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.basename((query.get("name") or [""])[0])).lstrip(".")
        if not name.lower().endswith(".csv"):
            name = (name or "transactions") + ".csv"
        exports = os.path.join(os.getcwd(), "private-profiles", profile, "exports")
        existed = os.path.isdir(exports)
        os.makedirs(exports, exist_ok=True)
        path = os.path.join(exports, name)
        with open(path, "wb") as fh:
            fh.write(self.rfile.read(size))
        proc = subprocess.run([sys.executable, os.path.join(os.getcwd(), "import_tr.py"), profile, path],
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
            url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
                   + urllib.parse.quote(symbol, safe="") + "?interval=5m&range=1d")
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as r:
                raw = json.load(r)
            result = raw["chart"]["result"][0]
            quote = result["indicators"]["quote"][0]
            meta = result.get("meta") or {}
            body = json.dumps({
                "symbol": symbol,
                "times": result["timestamp"],
                "closes": quote["close"],
                "highs": quote["high"],
                "lows": quote["low"],
                "previousClose": meta.get("chartPreviousClose"),
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

http.server.test(HandlerClass=QuietHandler, port=int(sys.argv[1]), bind="127.0.0.1")
' "$port"
