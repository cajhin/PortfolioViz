#!/bin/sh
# Serve this directory so portfolio.html can fetch the CSVs — fetch is blocked on file://.
#
#   ./start.sh          serve on 8000 and open the page
#   ./start.sh 8080     serve on another port
#   ./start.sh -n       don't open a browser
#
# Bound to 127.0.0.1 on purpose: parqet/ and prices/ hold real position values, and this
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
import http.server, json, os, subprocess, sys, urllib.parse, urllib.request

# The two non-static routes this server answers.
#
# POST /update-prices — the page'"'"'s "Update prices" button, instead of you running
# update_prices.py by hand. Runs with no arguments — every instrument in the registry,
# incremental from whatever gen_prices/ already has — since a button has no way to ask which
# slug or --from date you meant; use the script directly from a terminal for that. Blocking, not
# streamed: the fetch is normally done well within the timeout below, and a button that just
# shows "Updating..." until the response lands is a lot less code than a progress feed.
#
# GET /live-index?symbol=... — the header'"'"'s live-index widget. A browser page cannot call
# Yahoo'"'"'s chart API directly (no CORS allowance there — the same reason update_prices.py runs
# server-side rather than from portfolio.view.js), so this fetches it here and hands back just
# the day'"'"'s 5-minute bars. Nothing is written to disk — the whole point of this route is that
# it has no memory between requests, unlike every other price this page ever shows.
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
        if self.path != "/update-prices":
            self.send_error(404)
            return
        try:
            proc = subprocess.run(
                [sys.executable, os.path.join(os.getcwd(), "update_prices.py")],
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

    def do_GET(self):
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
