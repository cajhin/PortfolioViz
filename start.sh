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
import http.server, os, subprocess, sys

# The one non-static route this server answers: the page'"'"'s "Update prices" button POSTs here
# instead of you running update_prices.py by hand. Runs with no arguments — every instrument in
# the registry, incremental from whatever gen_prices/ already has — since a button has no way to
# ask which slug or --from date you meant; use the script directly from a terminal for that.
# Blocking, not streamed: the fetch is normally done well within the timeout below, and a button
# that just shows "Updating..." until the response lands is a lot less code than a progress feed.
class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

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

http.server.test(HandlerClass=QuietHandler, port=int(sys.argv[1]), bind="127.0.0.1")
' "$port"
