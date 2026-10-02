#!/usr/bin/env python3
"""The page's server: the repo's files, plus the routes the page needs a backend for.

    python3 scripts/server.py [PORT]      # 8000 by default; start.sh runs this

Serves the page's own files (web/) on 127.0.0.1 only: the data behind the api/ routes holds real
position values. Nothing else in the repo is served — not the database, not the import files.
Most routes run one of the other scripts and hand back what it printed; the logic lives there.

Every route but the page's own files (which hold no data) and logging in wants a login — a
session cookie, see USERS below — and a user sees and changes only the profiles granted to them.
That is for convenience and to keep people apart, not hardened security: a script run on this
machine needs no login at all.
"""
import functools, http.cookies, http.server, json, os, re, socket, sqlite3, subprocess, sys
import urllib.parse, urllib.request

import api
import db

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SCRIPTS)                  # the repo — this file lives in scripts/
WEB = os.path.join(ROOT, "web")                  # the static half: the page and its css and scripts

# The non-static routes this server answers.
#
# POST /login {user, password}, POST /register {user, password}, POST /logout — login.html's.
# Register is open to anyone who reaches the page: the new user sees nothing until they create a
# profile (or are granted one with scripts/manage-users.py). Both answer with the session cookie.
# GET /me — {user}, who is logged in.
#
# POST /update-prices?profile=NAME — the page's "Update prices" button, instead of you running
# update_prices.py by hand. Runs it with just --profile — the instruments that profile holds,
# watches or benchmarks against, incremental from whatever the database already has — since a
# button has no way to ask which slug or --from date you meant; use the script directly from a
# terminal for that. Only for a profile the user may open: the whole registry at once is the
# command line's. Blocking, not
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
# GET /profiles — the page's profile picker: one {name, label, source, write} per profile the
# user may open.
#
# POST /profiles {name, label, source} — the Config tab's "Create new profile", source "parqet"
# or "manual" (see SOURCES below): an empty profile with an empty watchlist, ready for its first
# refresh or import, granted to its creator. 409 if it already exists.
# POST /profile-label?profile=NAME {label} — the Config tab's rename: only the label changes. The
# name never does — it is what URLs, baselines and the refresh task refer to the profile by.
# POST /profile-delete?profile=NAME — the Config tab's Delete, for a profile the user may change:
# manage-accounts.py purge, with no backup. The default profile is refused there.
#
# POST /import-tr?profile=NAME&name=FILE.csv — the Config tab's "Import Trade Republic file":
# the body is the export itself. Kept in private-profiles/<name>/exports/ (the record of what
# was imported — the only files a profile still has), then import_tr.py merges it into that profile — rows already imported are
# skipped by their transaction_id, so uploading an overlapping or repeated export is harmless.
# Manual profiles only — a Parqet one gets a 403 before anything is written.
#
# POST /manual-tx?profile=NAME {date, isin, type, shares, price, fee} — the Config tab's "Add
# transaction", for building a virtual demo portfolio by hand; with &edit=ID, rewrites that
# hand-entered row (same body, instrument ignored); with &delete=ID, removes one. A body of
# {date, type: deposit|withdrawal, amount} books cash instead. Both run manual_tx.py, which
# validates, writes the ledger and rebuilds the profile.
# Manual profiles only, like the import.
#
# GET /instrument-search?find=NAME | ?symbols=ISIN and POST /instrument {isin, symbol, name,
# sector} — the Transactions tab's "new instrument" dialog: instruments by name (with their
# ISIN), Yahoo's listings of the one picked, then registering it (the registry's instrument and
# price_source rows) and fetching its prices. All run add_instrument.py; /instrument answers
# {instrument, log} as JSON, or the reason as text. Registering takes a user who may change at
# least one profile — the registry is shared, but it only ever grows.
#
# GET /api/... — every piece of data the page reads, out of data/portfolio.db: settings, registry,
# prices, the profile's own. scripts/api.py lists them; each answers in the shape of the file it
# replaced. A profile's own routes answer only for a profile the user may open — any other is a
# 404, the same as one that does not exist. api/config's defaultProfile is the user's: the global
# one if they may open it, else the first they may.
#
# GET /host — the machine this server runs on, so the page can tell the dev copy from the live one
# (see markDevHost). Not knowable from the browser: both are reached through localhost or a proxy.
#

PROFILE_NAME = re.compile(r"^[A-Za-z0-9_-]+$")   # a directory name, never a path

# A profile is controlled by Parqet (REFRESH_PARQET_DATA.md imports its positions and activities)
# or manually (imports from the Config tab). Its source says which; a manual import into a Parqet
# one is refused, so a slip can never overwrite a Parqet export.
SOURCES = ("parqet", "manual")

def is_manual(name):
    p = db.profile(name) if PROFILE_NAME.match(name) else None
    return bool(p) and p["source"] == "manual"

def list_profiles(user):
    return [{"name": p["name"], "label": p["label"], "source": p["source"], "write": bool(p["can_write"])}
            for p in db.user_profiles(user)]

# USERS. A login is a random token in an HttpOnly cookie; the database keeps its hash (db.py's
# access section) for SESSION_DAYS. SameSite=Strict keeps other sites from posting with it, and
# every POST from a browser must come from this page's own origin as well.
USER_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
COOKIE = "pv_session"
PROFILE_ROUTES = ("/api/profile", "/api/positions", "/api/activities", "/api/cash")

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

    # ---------- login ----------

    def session_token(self):
        cookie = http.cookies.SimpleCookie(self.headers.get("Cookie") or "")
        return cookie[COOKIE].value if COOKIE in cookie else ""

    def login_user(self):
        """Who is logged in, or None — after answering 401, so the caller just returns."""
        user = db.session_user(self.session_token())
        if not user:
            self.send_text(401, "log in first")
        return user

    def may_open(self, profile):
        return PROFILE_NAME.match(profile or "") and db.access(self.user, profile) is not None

    def may_change(self, profile):
        return PROFILE_NAME.match(profile or "") and db.access(self.user, profile) is True

    def same_origin(self):
        """A POST a browser sends says where from; none at all (curl) has no cookie to abuse."""
        origin = self.headers.get("Origin")
        host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or ""
        return not origin or urllib.parse.urlparse(origin).netloc == host

    def credentials(self):
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            return str(req.get("user") or "").strip(), str(req.get("password") or "")
        except ValueError:
            return "", ""

    def start_session(self, status, user):
        secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
        body = json.dumps({"user": user}).encode("utf-8")
        self.send_response(status)
        self.send_header("Set-Cookie", f"{COOKIE}={db.new_session(user)}; HttpOnly; SameSite=Strict; "
                                       f"Path=/; Max-Age={db.SESSION_DAYS * 86400}{secure}")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def login(self):
        user, pw = self.credentials()
        row = db.user(user) if USER_NAME.match(user) else None
        if not row or not db.check_password(pw, row["pw_hash"]):
            self.send_text(403, "wrong user name or password")
            return
        self.start_session(200, user)

    def register(self):
        user, pw = self.credentials()
        if not USER_NAME.match(user):
            self.send_text(400, "a user name is letters, digits, '.', '-' and '_', at most 40")
            return
        try:
            db.create_user(user, pw)
        except sqlite3.IntegrityError:
            self.send_text(409, f"the user name {user} is taken")
            return
        self.start_session(201, user)

    def logout(self):
        if self.session_token():
            db.end_session(self.session_token())
        self.send_response(204)
        self.send_header("Set-Cookie", f"{COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0")
        self.end_headers()

    # ---------- routes ----------

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if not self.same_origin():
            self.send_error(403, "cross-origin request")
            return
        if parsed.path in ("/login", "/register", "/logout"):
            {"/login": self.login, "/register": self.register, "/logout": self.logout}[parsed.path]()
            return
        self.user = self.login_user()
        if not self.user:
            return
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
                with db.tx():
                    db.create_profile(name, label or name, source)
                    db.grant(self.user, name)
            except sqlite3.IntegrityError:
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
            if not self.may_change(profile):
                self.send_error(404 if not self.may_open(profile) else 403, f"you may not rename {profile}")
                return
            if not db.set_label(profile, label):
                self.send_error(404, f"no profile {profile}")
                return
            self.send_response(204)
            self.end_headers()
            return
        if parsed.path == "/profile-delete":
            self.delete_profile((urllib.parse.parse_qs(parsed.query).get("profile") or [""])[0])
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
        if not self.may_open(profile):
            self.send_error(404, f"no profile {profile} for you")
            return
        try:
            proc = subprocess.run(
                [sys.executable, os.path.join(SCRIPTS, "update_prices.py"), "--profile", profile],
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

    def send_json(self, value):
        body = json.dumps(value).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def api_get(self):
        """api.get, within what the user may see: their own profiles, and their default."""
        url = urllib.parse.urlparse(self.path)
        if url.path in PROFILE_ROUTES:
            name = (urllib.parse.parse_qs(url.query).get("profile") or [""])[0]
            if not self.may_open(name):
                mine = [p["name"] for p in db.user_profiles(self.user)]
                return 404, "text/plain; charset=utf-8", (
                    f"No profile {name!r} for you — you have {', '.join(mine)}." if mine else
                    "You have no profile yet — create one, or ask for one to be granted to you.")
        status, ctype, text = api.get(self.path)
        if url.path == "/api/config" and status == 200:
            cfg = json.loads(text)
            mine = [p["name"] for p in db.user_profiles(self.user)]
            if cfg.get("defaultProfile") not in mine:
                cfg.pop("defaultProfile", None)
                if mine:
                    cfg["defaultProfile"] = mine[0]
            text = json.dumps(cfg, indent=2, ensure_ascii=False)
        return status, ctype, text

    def send_text(self, status, text):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def delete_profile(self, profile):
        if not self.may_change(profile):
            self.send_text(404 if not self.may_open(profile) else 403, f"you may not delete {profile}")
            return
        proc = subprocess.run([sys.executable, os.path.join(SCRIPTS, "manage-accounts.py"), "purge", profile,
                               "--yes", "--no-backup"],
                              capture_output=True, text=True, timeout=120)
        try:
            out = json.loads(proc.stdout)
        except ValueError:
            out = {"ok": False, "error": (proc.stdout + proc.stderr).strip()}
        if not out.get("ok"):
            self.send_text(400, out.get("error") or "failed")
            return
        self.send_json(out)

    def add_instrument(self):
        data = self.rfile.read(int(self.headers.get("Content-Length") or 0))   # before any refusal
        if not any(p["can_write"] for p in db.user_profiles(self.user)):
            self.send_error(403, "you may change no profile, so not the registry either")
            return
        try:
            req = json.loads(data or b"{}")
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
        if not self.may_change(profile):
            self.send_error(404 if not self.may_open(profile) else 403, f"you may not change {profile}")
            return
        if not is_manual(profile):
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
        if not self.may_open(profile) or not db.profile(profile):
            self.send_error(400, "unknown profile")
            return
        if not self.may_change(profile):
            self.send_error(403, f"you may not change {profile}")
            return
        if not is_manual(profile):
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
        path = urllib.parse.urlparse(self.path).path
        if not (path.startswith("/api/") or path in ("/me", "/profiles", "/host", "/instrument-search",
                                                    "/live-index")):
            super().do_GET()       # the page's own files: no data in them, and login.html is one
            return
        self.user = self.login_user()
        if not self.user:
            return
        if path == "/me":
            self.send_json({"user": self.user})
            return
        if path.startswith("/api/"):
            status, ctype, text = self.api_get()
            body = text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
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
        if path in ("/profiles", "/host"):
            self.send_json(list_profiles(self.user) if path == "/profiles" else {"host": socket.gethostname()})
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
    http.server.test(HandlerClass=functools.partial(QuietHandler, directory=WEB),
                     port=int(sys.argv[1]) if len(sys.argv) > 1 else 8000, bind="127.0.0.1")
