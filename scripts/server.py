#!/usr/bin/env python3
"""The page's server: the repo's files, plus the routes the page needs a backend for.

    python3 scripts/server.py [PORT]      # 8000 by default; start.sh runs this

Serves the page's own files (web/) on 127.0.0.1 only: the data behind the api/ routes holds real
position values. Nothing else in the repo is served — not the database, not the import files.
Most routes run one of the other scripts and hand back what it printed; the logic lives there.

Every route but the page's own files (which hold no data) and logging in wants a login — a
session cookie, see USERS below — and a user sees and changes only the portfolios granted to them.
That is for convenience and to keep people apart, not hardened security: a script run on this
machine needs no login at all.
"""
import functools, http.cookies, http.server, json, os, py_compile, re, socket, sqlite3, subprocess, sys
import tempfile, threading, urllib.parse, urllib.request

import api
import db

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SCRIPTS)                  # the repo — this file lives in scripts/
WEB = os.path.join(ROOT, "web")                  # the static half: the page and its css and scripts

# The non-static routes this server answers.
#
# POST /login {user, password}, POST /register {user, password}, POST /logout — login.html's.
# Register is open to anyone who reaches the page: the new user sees nothing until they create a
# portfolio (or are granted one with scripts/manage-users.py). Both answer with the session cookie.
# GET /me — {user, admin, restart}: who is logged in, whether they are an admin (the global
# `admins` setting, a list of user names — never sent to the page with the other settings), and,
# for an admin, whether this server's code changed on disk since it started (see RESTART).
# POST /restart — an admin's "Restart server": the server replaces itself (same process, port and
# terminal) with a fresh copy of its code, after checking that code compiles; else 500, and the
# running one stays.
#
# POST /update-prices?portfolio=ID — the page's "Update prices" button, instead of you running
# update_prices.py by hand. Runs it with just --portfolio — the instruments that portfolio holds,
# watches or benchmarks against, incremental from whatever the database already has — since a
# button has no way to ask which slug or --from date you meant; use the script directly from a
# terminal for that. Only for a portfolio the user may open: the whole registry at once is the
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
# GET /portfolios — the page's portfolio picker: one {id, label, type, write, ai} per portfolio the
# user may open.
#
# POST /portfolios {label, type} → {id} — the Config tab's "Create new portfolio", type "normal" or
# "game": an empty portfolio with an empty watchlist under the next id (pid17), granted to its
# creator — a game with GAME_CASH booked (manage-portfolios.py create).
# POST /portfolio-ai?portfolio=ID {allow} — the Config tab's "Allow AI (API) write access".
# POST /portfolio-label?portfolio=ID {label} — the Config tab's rename: only the label changes. The
# id never does — it is what URLs, scripts, tokens and baselines refer to the portfolio by.
# POST /portfolio-delete?portfolio=ID — the Config tab's Delete, for a portfolio the user may change:
# manage-portfolios.py purge, with no backup. The default portfolio is refused there.
#
# POST /import-tr?portfolio=ID&name=FILE.csv — the Config tab's "Import Trade Republic file":
# the body is the export itself. import_tr.py merges it into that portfolio's ledger — rows already
# imported are skipped by their transaction_id, so uploading an overlapping or repeated export is
# harmless. The file itself is not kept: it waits in a temporary folder for the import and is gone
# after it — the export stays on the user's disk, its rows in the database.
# Normal portfolios only — any other gets a 403 before anything is written.
#
# POST /import-csv?portfolio=ID — the Transactions tab's "Import CSV (Portfolioviz format)": the
# body is the pasted text, which import_csv.py checks whole (any problem: nothing imported, every
# problem listed), skipping duplicates. Its rows land unverified. With &action=accept they are
# accepted, with &action=discard all rows not yet accepted are deleted again — the rollback.
# Normal portfolios only, like the import above.
#
# POST /manual-tx?portfolio=ID {date, depot, isin, type, shares, price, fee} — the Config tab's
# "Add transaction", for building a virtual demo portfolio by hand (depot "Manual" if none named);
# with &edit=ID, rewrites that row, whatever its origin (same body plus tax, instrument ignored);
# with &delete=ID, removes one. A body of {date, depot, type: deposit|withdrawal|interest, amount}
# books cash instead. Both run manual_tx.py, which validates, writes the ledger and rebuilds the
# portfolio.
# Normal portfolios only, like the import.
#
# GET /quote?instrument=ISIN and POST /trade?portfolio=ID {side: buy|sell, isin, shares | eur |
# all} — a game's "trade now" on the Transactions tab: trade.py quote, and trade.py buy or sell —
# its JSON as it is, the rules (trading-rules.md) and the refusals all trade.py's own. Games only.
#
# GET /tokens?portfolio=ID, POST /tokens {label, portfolio} → {id, token}, POST /token-revoke {id}
# — the Config tab's API tokens: the user's own for that portfolio, each with the token itself, for
# its Copy button. See TOKENS.
#
# GET /instrument-search?find=NAME | ?symbols=ISIN and POST /instrument {isin, symbol, name,
# sector} — the Transactions tab's "new instrument" dialog: instruments by name (with their
# ISIN), Yahoo's listings of the one picked, then registering it (the registry's instrument and
# price_source rows) and fetching its prices. All run add_instrument.py; /instrument answers
# {instrument, log} as JSON, or the reason as text. Registering takes a user who may change at
# least one portfolio — the registry is shared, but it only ever grows.
#
# GET /api/... — every piece of data the page reads, out of data/portfolio.db: settings, registry,
# prices, the portfolio's own. scripts/api.py lists them; each answers in the shape of the file it
# replaced. A portfolio's own routes answer only for a portfolio the user may open — any other is a
# 404, the same as one that does not exist. api/config's defaultPortfolio is the user's: the global
# one if they may open it, else the first they may.
#
# GET /host — the machine this server runs on, so the page can tell the dev copy from the live one
# (see markDevHost). Not knowable from the browser: both are reached through localhost or a proxy.
#

PORTFOLIO_ID = re.compile(r"^[A-Za-z0-9_-]+$")     # pid17 — or an older id, not renumbered yet

# RESTART. The routes that run a script get its current code every time; this server's own code —
# itself and what it imports from scripts/ (api.py, db.py) — is what it read when it started. A
# change to one of those needs a restart, which an admin can do from the page (/restart).
LOADED = {f: os.path.getmtime(f) for f in
          {os.path.abspath(getattr(m, "__file__", None) or "") for m in list(sys.modules.values())}
          | {os.path.abspath(__file__)}
          if os.path.dirname(f) == SCRIPTS and f.endswith(".py")}


def code_changed():
    return any(not os.path.exists(f) or os.path.getmtime(f) != t for f, t in LOADED.items())


def is_admin(user):
    return user in (db.settings("").get("admins") or [])


def restart():
    """This process, replaced by a fresh one of the same command line — the listening socket closes
    with it (it is not inherited), and the new one binds the same port again."""
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(sys.executable, [sys.executable] + sys.argv)

# A portfolio's type: normal (imports, hand entries and edits on the page, of any transaction) or
# game (no past to edit — only trades now, under trading-rules.md, by trade.py from the page or an
# agent). The page's imports and edits are refused for a game, so its rules cannot be bent. A game
# made on the page starts with GAME_CASH, booked by manage-portfolios.py.
PAGE_TYPES = ("normal", "game")
GAME_CASH = 10000

def portfolio_type(pid):
    p = db.portfolio(pid) if PORTFOLIO_ID.match(pid) else None
    return p["type"] if p else None

def list_portfolios(user):
    return [{"id": p["id"], "label": p["label"], "type": p["type"], "write": bool(p["can_write"]),
             "ai": bool(p["ai_write"])} for p in db.user_portfolios(user)]

# TOKENS. A script or an AI logs in with one of its user's API tokens (Authorization: Bearer …) in
# place of the session cookie. A token is bound to the portfolio it was made for: every route acts on
# that one — ?portfolio= may be left out, and any other is refused, for reading too. It writes only
# while that portfolio allows AI write access, and only through TOKEN_WRITES — never accepting its
# own CSV import (that is the user's check of it), and never anything about portfolios, users,
# tokens or the server.
TOKEN_WRITES = ("/manual-tx", "/import-csv", "/import-tr", "/trade", "/instrument", "/update-prices")

def script_env():
    """The environment a script runs in: never an agent's binding (TRADE_PORTFOLIO), which would
    make trade.py ask for AI write access of the page's own trades."""
    return {k: v for k, v in os.environ.items() if k != "TRADE_PORTFOLIO"}

# USERS. A login is a random token in an HttpOnly cookie; the database keeps its hash (db.py's
# access section) for SESSION_DAYS. SameSite=Strict keeps other sites from posting with it, and
# every POST from a browser must come from this page's own origin as well.
USER_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
COOKIE = "pv_session"
PORTFOLIO_ROUTES = ("/api/portfolio", "/api/positions", "/api/activities", "/api/cash")

class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def finish(self):
        # every request is a thread of its own, and its database connection must not outlive it
        try:
            super().finish()
        finally:
            db.close()

    # The page's own files carry only Last-Modified, which lets a browser guess they are still fresh
    # and reuse an old portfolio.html with a new portfolio.view.js — whose elements it then lacks.
    # "no-cache" makes it ask every time (an unchanged file is a cheap 304), unless a route has said
    # otherwise.
    def send_header(self, keyword, value):
        if keyword.lower() == "cache-control":
            self._cache_control = True
        super().send_header(keyword, value)

    def end_headers(self):
        if not getattr(self, "_cache_control", False):
            super().send_header("Cache-Control", "no-cache")
        self._cache_control = False
        super().end_headers()

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
        """Who is logged in — by session cookie, or by API token (self.via_token, bound to
        self.token_portfolio) — or None, after answering, so the caller just returns. A token's
        portfolio goes into the request's ?portfolio= when it names none; naming another is refused."""
        auth = self.headers.get("Authorization") or ""
        self.via_token, self.token_portfolio = auth.startswith("Bearer "), None
        if not self.via_token:
            user = db.session_user(self.session_token())
            if not user:
                self.send_text(401, "log in first")
            return user
        found = db.token_login(auth[7:].strip())
        if not found:
            self.send_text(401, "unknown API token, or one made before tokens were bound to a portfolio")
            return None
        user, self.token_portfolio = found
        url = urllib.parse.urlparse(self.path)
        asked = (urllib.parse.parse_qs(url.query).get("portfolio") or [""])[0]
        if asked and asked != self.token_portfolio:
            self.send_text(403, f"this token is for portfolio {self.token_portfolio} only")
            return None
        if not asked:
            self.path = url._replace(query="&".join(filter(None, [
                url.query, "portfolio=" + urllib.parse.quote(self.token_portfolio)]))).geturl()
        return user

    def may_open(self, portfolio):
        return (PORTFOLIO_ID.match(portfolio or "") and db.access(self.user, portfolio) is not None
                and (not self.via_token or portfolio == self.token_portfolio))

    def may_change(self, portfolio):
        """Granted with write access — and, for an API token, AI write access allowed on it."""
        return (PORTFOLIO_ID.match(portfolio or "") and db.access(self.user, portfolio) is True
                and (not self.via_token or bool((db.portfolio(portfolio) or {}).get("ai_write"))))

    def refuse_change(self, portfolio, what="change"):
        if not self.may_open(portfolio):
            self.send_error(404, f"no portfolio {portfolio} for you")
        elif self.via_token and db.access(self.user, portfolio):
            self.send_error(403, f"{portfolio} does not allow AI (API) write access")
        else:
            self.send_error(403, f"you may not {what} {portfolio}")

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
        parsed = urllib.parse.urlparse(self.path)          # a token may have added its ?portfolio=
        if self.via_token and parsed.path not in TOKEN_WRITES:
            self.send_error(403, "not with an API token - log in on the page")
            return
        if parsed.path == "/portfolios":
            req = self.read_json()
            label, type_ = " ".join(str(req.get("label") or "").split()), str(req.get("type") or "")
            if not label:
                self.send_error(400, "give the portfolio a name to show")
                return
            if type_ not in PAGE_TYPES:
                self.send_error(400, "type must be normal or game")
                return
            if type_ == "game":
                # the opening cash and its id are manage-portfolios.py's
                out = self.run_json(["manage-portfolios.py", "create", "--label", label, "--cash", str(GAME_CASH)])
                if not out.get("ok"):
                    self.send_text(400, out.get("error") or "failed")
                    return
                pid = out["portfolio"]
                db.grant(self.user, pid)
            else:
                with db.tx():
                    pid = db.create_portfolio(label, type_)
                    db.grant(self.user, pid)
            body = json.dumps({"id": pid}).encode("utf-8")
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/portfolio-label":
            portfolio = (urllib.parse.parse_qs(parsed.query).get("portfolio") or [""])[0]
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                label = str(req.get("label") or "").strip()
            except ValueError:
                label = ""
            if not PORTFOLIO_ID.match(portfolio) or not label:
                self.send_error(400, "bad portfolio or empty label")
                return
            if not self.may_change(portfolio):
                self.send_error(404 if not self.may_open(portfolio) else 403, f"you may not rename {portfolio}")
                return
            if not db.set_label(portfolio, label):
                self.send_error(404, f"no portfolio {portfolio}")
                return
            self.send_response(204)
            self.end_headers()
            return
        if parsed.path == "/portfolio-delete":
            self.delete_portfolio((urllib.parse.parse_qs(parsed.query).get("portfolio") or [""])[0])
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
        if parsed.path == "/import-csv":
            self.import_csv(urllib.parse.parse_qs(parsed.query))
            return
        if parsed.path == "/restart":
            self.restart()
            return
        if parsed.path == "/portfolio-ai":
            self.portfolio_ai((urllib.parse.parse_qs(parsed.query).get("portfolio") or [""])[0])
            return
        if parsed.path == "/trade":
            self.trade((urllib.parse.parse_qs(parsed.query).get("portfolio") or [""])[0])
            return
        if parsed.path in ("/tokens", "/token-revoke"):
            self.tokens_post(parsed.path)
            return
        if parsed.path != "/update-prices":
            self.send_error(404)
            return
        portfolio = (urllib.parse.parse_qs(parsed.query).get("portfolio") or [""])[0]
        if not self.may_open(portfolio):
            self.send_error(404, f"no portfolio {portfolio} for you")
            return
        try:
            proc = subprocess.run(
                [sys.executable, os.path.join(SCRIPTS, "update_prices.py"), "--portfolio", portfolio],
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
        """api.get, within what the user may see: their own portfolios, and their default."""
        url = urllib.parse.urlparse(self.path)
        if url.path in PORTFOLIO_ROUTES:
            name = (urllib.parse.parse_qs(url.query).get("portfolio") or [""])[0]
            if not self.may_open(name):
                mine = [p["id"] for p in db.user_portfolios(self.user) if self.may_open(p["id"])]
                return 404, "text/plain; charset=utf-8", (
                    f"No portfolio {name!r} for you — you have {', '.join(mine)}." if mine else
                    "You have no portfolio yet — create one, or ask for one to be granted to you.")
        status, ctype, text = api.get(self.path)
        if url.path == "/api/config" and status == 200:
            cfg = json.loads(text)
            cfg.pop("admins", None)                 # who the admins are is not the page's business
            mine = [p["id"] for p in db.user_portfolios(self.user) if self.may_open(p["id"])]
            if cfg.get("defaultPortfolio") not in mine:
                cfg.pop("defaultPortfolio", None)
                if mine:
                    cfg["defaultPortfolio"] = mine[0]
            text = json.dumps(cfg, indent=2, ensure_ascii=False)
        return status, ctype, text

    def send_text(self, status, text):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def delete_portfolio(self, portfolio):
        if not self.may_change(portfolio):
            self.send_text(404 if not self.may_open(portfolio) else 403, f"you may not delete {portfolio}")
            return
        proc = subprocess.run([sys.executable, os.path.join(SCRIPTS, "manage-portfolios.py"), "purge", portfolio,
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
        if not any(p["can_write"] and (not self.via_token or (p["ai_write"] and p["id"] == self.token_portfolio))
                   for p in db.user_portfolios(self.user)):
            self.send_error(403, "you may change no portfolio, so not the registry either")
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
        portfolio = (query.get("portfolio") or [""])[0]
        if not self.may_change(portfolio):
            self.refuse_change(portfolio)
            return
        if portfolio_type(portfolio) != "normal":
            self.send_error(403, "not a normal portfolio")
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
            if tx.get("type") in ("deposit", "withdrawal", "interest"):
                cash = [str(tx.get("date", "")), str(tx["type"]), str(tx.get("amount", ""))]
                args = ["edit-cash", edit] + cash if edit else ["cash"] + cash
            else:
                fields = [str(tx.get(k, "")) for k in ("date", "isin", "type", "shares", "price")] \
                         + [str(tx.get("fee") or 0)]
                # an edit keeps its instrument: the id names the row, the rest is what it becomes
                args = ["edit", edit] + fields[:1] + fields[2:] if edit else ["add"] + fields
                if tx.get("tax"):
                    args += ["--tax", str(tx["tax"])]
            # a new row books in the depot named (else "Manual"); an edit moves to it, if named
            if tx.get("depot"):
                args += ["--depot", str(tx["depot"])]
        proc = subprocess.run([sys.executable, os.path.join(SCRIPTS, "manual_tx.py"), portfolio] + args,
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
        portfolio = (query.get("portfolio") or [""])[0]
        if not self.may_open(portfolio) or not db.portfolio(portfolio):
            self.send_error(400, "unknown portfolio")
            return
        if not self.may_change(portfolio):
            self.refuse_change(portfolio)
            return
        if portfolio_type(portfolio) != "normal":
            # ASCII only: the reason goes into the status line, which is latin-1
            self.send_error(403, f"{portfolio} is not a normal portfolio - no imports")
            return
        # the uploaded name, reduced to something safe to put in a path
        name = re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.basename((query.get("name") or [""])[0])).lstrip(".")
        if not name.lower().endswith(".csv"):
            name = (name or "transactions") + ".csv"
        with tempfile.TemporaryDirectory(prefix="portfolioviz-import-") as tmp:
            path = os.path.join(tmp, name)      # its own name, so import_tr.py's report shows it
            with open(path, "wb") as fh:
                fh.write(data)
            proc = subprocess.run([sys.executable, os.path.join(SCRIPTS, "import_tr.py"), portfolio, path],
                                  capture_output=True, text=True, timeout=120)
        body = (proc.stdout + proc.stderr).encode("utf-8")
        self.send_response(200 if proc.returncode == 0 else 500)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def run_json(self, args, timeout=120):
        """One of the JSON-answering scripts (trade.py, manage-portfolios.py) run, its answer as a dict."""
        proc = subprocess.run([sys.executable, os.path.join(SCRIPTS, args[0])] + args[1:], env=script_env(),
                              capture_output=True, text=True, timeout=timeout)
        try:
            return json.loads(proc.stdout)
        except ValueError:
            return {"ok": False, "error": (proc.stdout + proc.stderr).strip()[-500:] or "failed"}

    def read_json(self):
        try:
            return json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            return {}

    def portfolio_ai(self, portfolio):
        req = self.read_json()
        if not self.may_change(portfolio):
            self.refuse_change(portfolio)
            return
        db.set_ai_write(portfolio, bool(req.get("allow")))
        self.send_json({"portfolio": portfolio, "ai": bool(req.get("allow"))})

    def trade(self, portfolio):
        req = self.read_json()
        if not self.may_change(portfolio):
            self.refuse_change(portfolio, "trade")
            return
        if portfolio_type(portfolio) != "game":
            self.send_error(403, "only a game is traded - a normal portfolio takes transactions")
            return
        side, isin = str(req.get("side") or ""), str(req.get("isin") or "")
        if side not in ("buy", "sell") or not isin:
            self.send_error(400, "give side (buy or sell) and isin")
            return
        args = ["trade.py", side, portfolio, isin]
        for key in ("shares", "eur"):
            if req.get(key) not in (None, ""):
                args += ["--" + key, str(req[key])]
        if side == "sell" and req.get("all"):
            args.append("--all")
        args += ["--reason", "traded on the page" if not self.via_token else "traded through the API"]
        if req.get("id"):
            args += ["--id", str(req["id"])]
        out = self.run_json(args)
        self.send_text(200 if out.get("ok") else 400, json.dumps(out, ensure_ascii=False))

    def tokens_post(self, path):
        req = self.read_json()
        if path == "/tokens":
            label = " ".join(str(req.get("label") or "").split())[:60]
            portfolio = str(req.get("portfolio") or "")
            if not label:
                self.send_error(400, "give the token a label - what will use it")
                return
            if not self.may_change(portfolio):
                self.refuse_change(portfolio, "make a token for")
                return
            token = db.new_api_token(self.user, label, portfolio)
            self.send_json({"token": token, "label": label, "portfolio": portfolio})
            return
        if not db.revoke_api_token(self.user, str(req.get("id") or "")):
            self.send_error(404, "no such token")
            return
        self.send_json({"revoked": req.get("id")})

    def restart(self):
        if not is_admin(self.user):
            self.send_error(403, "admins only")
            return
        try:
            for f in LOADED:
                py_compile.compile(f, doraise=True)
        except py_compile.PyCompileError as e:
            self.send_text(500, f"not restarted - the code does not compile:\n{e.msg}")
            return
        self.send_text(200, "restarting")
        threading.Timer(0.3, restart).start()      # once this answer is on its way

    def import_csv(self, query):
        size = int(self.headers.get("Content-Length") or 0)
        if size > 5 * 1024 * 1024:
            self.send_error(400, "oversized paste")
            return
        data = self.rfile.read(size)                # before any refusal, as in import_tr
        portfolio = (query.get("portfolio") or [""])[0]
        if not self.may_change(portfolio):
            self.refuse_change(portfolio)
            return
        if portfolio_type(portfolio) != "normal":
            self.send_error(403, f"{portfolio} is not a normal portfolio - no imports")
            return
        action = (query.get("action") or [""])[0]
        if action not in ("", "accept", "discard"):
            self.send_error(400, "action must be accept or discard")
            return
        if action == "accept" and self.via_token:
            self.send_error(403, "accepting an import is for its user to do, on the page")
            return
        proc = subprocess.run([sys.executable, os.path.join(SCRIPTS, "import_csv.py"), portfolio, action or "-"],
                              input="" if action else data.decode("utf-8", "replace"),
                              capture_output=True, text=True, timeout=120)
        self.send_text(200 if proc.returncode == 0 else 400, proc.stdout + proc.stderr)

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
        if not (path.startswith("/api/") or path in ("/me", "/portfolios", "/host", "/instrument-search",
                                                    "/live-index", "/quote", "/tokens")):
            super().do_GET()       # the page's own files: no data in them, and login.html is one
            return
        self.user = self.login_user()
        if not self.user:
            return
        if path == "/me":
            admin = is_admin(self.user)
            self.send_json({"user": self.user, "admin": admin, "restart": admin and code_changed()})
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
        if path == "/tokens":
            if self.via_token:
                self.send_error(403, "not with an API token - log in on the page")
                return
            portfolio = (urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("portfolio") or [""])[0]
            if not self.may_open(portfolio):
                self.send_error(404, f"no portfolio {portfolio} for you")
                return
            self.send_json(db.api_tokens(self.user, portfolio))
            return
        if path == "/quote":
            what = (urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("instrument") or [""])[0]
            out = self.run_json(["trade.py", "quote", what], timeout=60) if what else {"ok": False, "error": "missing ?instrument="}
            self.send_text(200 if out.get("ok") else 400, json.dumps(out, ensure_ascii=False))
            return
        if path in ("/portfolios", "/host"):
            self.send_json([p for p in list_portfolios(self.user) if self.may_open(p["id"])] if path == "/portfolios"
                           else {"host": socket.gethostname()})
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
