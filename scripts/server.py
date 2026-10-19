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

import agents
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
# POST /default-portfolio {portfolio} — the Config tab's "Make default": the user's own default
# portfolio, which api/config's defaultPortfolio then names (see default_portfolio).
# GET /me — {user, admin, restart, publicUrl, default}: who is logged in, their default portfolio, whether they are an admin (the global
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
# GET /portfolios — the page's portfolio picker: one {id, label, type, write, ai, public} per
# portfolio granted to the user.
# POST /watchlist?portfolio=ID {add: ISIN} | {remove: ISIN} — the Watch tab's add and remove: the
# portfolio's own `watchlist` setting. Only a registered instrument can be added (it needs prices).
# GET /public-portfolios — every other user's public portfolio: {id, label, type, owners} — the Watch
# tab's list. A public portfolio is open to every logged-in user, read only: may_open, not may_change.
# POST /portfolio-public?portfolio=ID {public} — the Config tab's "Public".
#
# POST /portfolios {label, type} → {id} — the Config tab's "Create new portfolio", type "normal" or
# "game": an empty portfolio with an empty watchlist under the next id (pid17), granted to its
# creator — a game with GAME_CASH booked (manage-portfolios.py create).
# POST /portfolio-ai?portfolio=ID {level} — the Config tab's "Allow AI (API) access": 0 none,
# 1 read-only, 2 read-write — what every token of the portfolio may do.
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
# GET /export-csv?portfolio=ID — the Transactions tab's "Export PViz CSV": every transaction in the
# format the import reads (import_csv.py export), to replicate the portfolio in another.
# POST /import-csv?portfolio=ID — the Transactions tab's "Import PViz CSV": the
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
# GET /tokens?portfolio=ID, POST /tokens {label, portfolio} → {id, token}, POST /token-revoke {id} —
# the Config tab's API tokens: the user's own for that portfolio, each with the token itself, for its
# Copy button. See TOKENS.
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
# GET /agents.md (also /llms.txt), /agents/csv, /agents/game-rules, /agents/game-agent — instructions
# for agents using a portfolio over HTTP from elsewhere (scripts/agents.py): public, like login.html, so an agent can
# read how to get a token before it has one. Every refusal a token gets points there. The address
# they give is public_url(): the global `publicUrl` setting, which an admin sets to where others
# reach this server (db.py config set publicUrl '"https://…"'), else where the request came in.
# GET /instrument?isin=ISIN — {registered, id, name, display}: whether that one instrument is in the
# registry — never the registry whole, which names what every other portfolio holds.
# GET /game-status — a game's cash and positions valued live (trade.py status).
# GET /chart?instrument=X[&instrument=Y…] — price history in figures (trade.py chart), to research by.
# GET /notes — an agent's notes on the portfolio: {brief, strategy, memory, journal (the newest 50)};
# GET /notes/strategy?history=1 its versions (no text), ?version=N one; GET /notes/journal?limit=N.
# POST /notes/memory {text}, /notes/strategy {text, reason}, /notes/journal {text} — the agent's,
# open to a token allowed to write; POST /notes/brief {text} — the owner's, on the page only.
# See notes.py.
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
             "ai": p["ai_access"], "public": bool(p["public"])} for p in db.user_portfolios(user)]

# TOKENS. A script or an AI logs in with one of its user's API tokens (Authorization: Bearer …) in
# place of the session cookie. A token is bound to the portfolio it was made for: every route acts on
# that one — ?portfolio= may be left out, and any other is refused. It works only while that
# portfolio allows AI (API) access, reading included; it writes only if its own "allow writing" is
# ticked, and only through TOKEN_WRITES — never accepting its own CSV import (that is the user's
# check of it), and never anything about portfolios, users, tokens or the server.
TOKEN_WRITES = ("/manual-tx", "/import-csv", "/import-tr", "/trade", "/instrument", "/update-prices",
                "/watchlist", "/notes/memory", "/notes/strategy", "/notes/journal")

def script_env():
    """The environment a script runs in: never an agent's binding (TRADE_PORTFOLIO), which would
    make trade.py ask for AI access of the page's own trades."""
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
            self.send_text(401, "unknown API token, or one made before tokens were bound to a portfolio - "
                                "ask your user for a new one; see agents.md")
            return None
        user, self.token_portfolio, _ = found
        level = (db.portfolio(self.token_portfolio) or {}).get("ai_access") or db.AI_NONE
        self.token_write = level >= db.AI_WRITE
        if level == db.AI_NONE:
            self.send_text(403, f"portfolio {self.token_portfolio} does not allow AI (API) access - ask its "
                                "owner to allow it on the Config tab; see agents.md")
            return None
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
        """Granted — or public, to anyone logged in on the page; a token only ever its own."""
        if not PORTFOLIO_ID.match(portfolio or ""):
            return False
        if self.via_token:
            return portfolio == self.token_portfolio and db.access(self.user, portfolio) is not None
        return db.access(self.user, portfolio) is not None or bool((db.portfolio(portfolio) or {}).get("public"))

    def may_change(self, portfolio):
        """Granted with write access — and, for an API token, the portfolio's AI access read-write."""
        return (self.may_open(portfolio) and db.access(self.user, portfolio) is True
                and (not self.via_token or self.token_write))

    def refuse_change(self, portfolio, what="change"):
        if not self.may_open(portfolio):
            self.send_error(404, f"no portfolio {portfolio} for you")
        elif self.via_token and db.access(self.user, portfolio):
            self.send_error(403, "this portfolio allows AI read-only access - ask its owner to allow read-write "
                                 "on the Config tab (see agents.md)")
        else:
            self.send_error(403, f"you may not {what} {portfolio}")

    def public_url(self):
        """Where others reach this server: the `publicUrl` setting, else where this request came in —
        through a proxy, as the proxy says — which from this machine is only "localhost"."""
        set_ = str(db.settings("").get("publicUrl") or "").rstrip("/")
        if set_:
            return set_
        proto = self.headers.get("X-Forwarded-Proto") or "http"
        host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or "localhost"
        return f"{proto}://{host}{(self.headers.get('X-Forwarded-Prefix') or '').rstrip('/')}"

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
            self.send_error(403, "not with an API token - this is for its user, on the page (see agents.md)")
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
        if parsed.path.startswith("/notes/"):
            self.notes_post(parsed.path[len("/notes/"):], (urllib.parse.parse_qs(parsed.query).get("portfolio") or [""])[0])
            return
        if parsed.path == "/watchlist":
            self.watchlist((urllib.parse.parse_qs(parsed.query).get("portfolio") or [""])[0])
            return
        if parsed.path == "/default-portfolio":
            portfolio = str(self.read_json().get("portfolio") or "")
            if not PORTFOLIO_ID.match(portfolio) or db.access(self.user, portfolio) is None:
                self.send_error(404, f"no portfolio {portfolio} of yours")
                return
            db.set_user_default(self.user, portfolio)
            self.send_json({"default": portfolio})
            return
        if parsed.path == "/portfolio-public":
            portfolio = (urllib.parse.parse_qs(parsed.query).get("portfolio") or [""])[0]
            req = self.read_json()
            if not self.may_change(portfolio):
                self.refuse_change(portfolio)
                return
            db.set_public(portfolio, bool(req.get("public")))
            self.send_json({"portfolio": portfolio, "public": bool(req.get("public"))})
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
        # its own users only: someone viewing a public portfolio does not fetch prices for it
        if not self.may_open(portfolio) or db.access(self.user, portfolio) is None:
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
            cfg.pop("defaultPortfolio", None)
            mine = self.default_portfolio(cfg)
            if mine:
                cfg["defaultPortfolio"] = mine
            text = json.dumps(cfg, indent=2, ensure_ascii=False)
        return status, ctype, text

    def default_portfolio(self, cfg=None):
        """The portfolio a page with no ?portfolio= opens: the user's own default, else the global
        one, else the first granted — whichever the user may open; a token, its own."""
        if self.via_token:
            return self.token_portfolio
        mine = [p["id"] for p in db.user_portfolios(self.user)]
        own = (db.user(self.user) or {}).get("default_portfolio")
        glob = (cfg if cfg is not None else db.settings("")).get("defaultPortfolio")
        return next((p for p in (own, glob) if p in mine), mine[0] if mine else None)

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
        if not any(p["can_write"] and (not self.via_token or (self.token_write and p["id"] == self.token_portfolio))
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
        if self.via_token:
            args.append("--api")        # an api-… id, unverified; only unverified rows changed
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
            proc = subprocess.run([sys.executable, os.path.join(SCRIPTS, "import_tr.py"), portfolio, path]
                                  + (["--api"] if self.via_token else []),     # unverified, as any token write
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

    def notes_post(self, what, portfolio):
        req = self.read_json()
        if what not in ("memory", "strategy", "journal", "brief"):
            self.send_error(404)
            return
        if not self.may_change(portfolio):
            self.refuse_change(portfolio)
            return
        text = str(req.get("text") or "")
        if not text.strip() and what != "brief":            # a brief may be cleared
            self.send_error(400, "give the text")
            return
        if len(text) > db.NOTE_MAX:
            self.send_error(400, f"{len(text)} characters - at most {db.NOTE_MAX}")
            return
        if what == "strategy":
            reason = " ".join(str(req.get("reason") or "").split())
            if not reason:
                self.send_error(400, "a strategy version needs a reason: what changed, and why")
                return
            self.send_json({"strategy": db.add_strategy(portfolio, text, reason[:500])})
        elif what == "journal":
            db.add_journal(portfolio, text)
            self.send_json({"journal": "added"})
        else:
            db.set_note(portfolio, what, text)
            self.send_json({what: "saved"})

    def watchlist(self, portfolio):
        req = self.read_json()
        if not self.may_change(portfolio):
            self.refuse_change(portfolio)
            return
        add, remove = (str(req.get(k) or "").strip().upper() for k in ("add", "remove"))
        if not (add or remove):
            self.send_error(400, "give add or remove, an ISIN")
            return
        if add and not any(i["id"] == add for i in db.instruments()):
            self.send_error(400, f"{add} is not registered - register it first (+ New instrument)")
            return
        listed = [x for x in (db.settings(portfolio).get("watchlist") or []) if x != remove]
        if add and add not in listed:
            listed.append(add)
        db.set_setting("watchlist", listed, portfolio)
        self.send_json({"portfolio": portfolio, "watchlist": listed})

    def portfolio_ai(self, portfolio):
        req = self.read_json()
        if not self.may_change(portfolio):
            self.refuse_change(portfolio)
            return
        level = req.get("level")
        if level not in (db.AI_NONE, db.AI_READ, db.AI_WRITE):
            self.send_error(400, "level must be 0 (none), 1 (read-only) or 2 (read-write)")
            return
        db.set_ai_access(portfolio, level)
        self.send_json({"portfolio": portfolio, "ai": level})

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
        # the agent's own reason, kept with the trade; else where it came from
        reason = " ".join(str(req.get("reason") or "").split())[:1000]
        args += ["--reason", reason or ("traded on the page" if not self.via_token else "traded through the API")]
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
        docs = {"/agents.md": "md", "/llms.txt": "md", "/agents/csv": "csv", "/agents/game-rules": "rules",
                "/agents/game-agent": "agent"}
        if path in docs:                    # public: how an agent gets in is no secret
            text = {"md": lambda: agents.entry(self.public_url()), "csv": agents.csv_spec, "rules": agents.rules,
                    "agent": lambda: agents.game_agent(self.public_url())}[docs[path]]()
            body = text.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/markdown; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if not (path.startswith("/api/") or path in ("/me", "/portfolios", "/host", "/instrument-search",
                                                    "/live-index", "/quote", "/tokens", "/export-csv",
                                                    "/game-status", "/instrument", "/public-portfolios",
                                                    "/chart", "/notes", "/notes/strategy", "/notes/journal")):
            super().do_GET()       # the page's own files: no data in them, and login.html is one
            return
        self.user = self.login_user()
        if not self.user:
            return
        if path == "/me":
            admin = is_admin(self.user)
            self.send_json({"user": self.user, "admin": admin, "restart": admin and code_changed(),
                            "publicUrl": self.public_url(), "default": self.default_portfolio()})
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
        if path.startswith("/notes"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            portfolio = (q.get("portfolio") or [""])[0]
            if not self.may_open(portfolio):
                self.send_error(404, f"no portfolio {portfolio} for you")
                return
            if path == "/notes":
                self.send_json({"brief": db.note(portfolio, "brief"), "strategy": db.strategy(portfolio),
                                "memory": db.note(portfolio, "memory"), "journal": db.journal(portfolio)})
            elif path == "/notes/journal":
                limit = (q.get("limit") or ["50"])[0]
                self.send_json(db.journal(portfolio, max(1, min(int(limit) if limit.isdigit() else 50, db.JOURNAL_KEEP))))
            elif (q.get("version") or [""])[0].isdigit():
                found = db.strategy(portfolio, int(q["version"][0]))
                self.send_json(found) if found else self.send_error(404, "no such version")
            else:
                self.send_json(db.strategy_history(portfolio) if q.get("history") else db.strategy(portfolio))
            return
        if path == "/public-portfolios":
            self.send_json([] if self.via_token else
                           [{k: p[k] for k in ("id", "label", "type", "owners")} for p in db.public_portfolios(self.user)])
            return
        if path == "/instrument":
            isin = (urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("isin") or [""])[0].strip().upper()
            inst = next((i for i in db.instruments() if i["id"] == isin), None)
            self.send_json({"isin": isin, "registered": bool(inst),
                            **({k: inst[k] for k in ("id", "name", "display")} if inst else {})})
            return
        if path == "/game-status":
            portfolio = (urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("portfolio") or [""])[0]
            if not self.may_open(portfolio):
                self.send_error(404, f"no portfolio {portfolio} for you")
                return
            if portfolio_type(portfolio) != "game":
                self.send_error(400, "not a game - see api/positions and api/cash")
                return
            out = self.run_json(["trade.py", "status", portfolio], timeout=60)
            self.send_text(200 if out.get("ok") else 400, json.dumps(out, ensure_ascii=False))
            return
        if path == "/export-csv":
            portfolio = (urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("portfolio") or [""])[0]
            if not self.may_open(portfolio):
                self.send_error(404, f"no portfolio {portfolio} for you")
                return
            proc = subprocess.run([sys.executable, os.path.join(SCRIPTS, "import_csv.py"), portfolio, "export"],
                                  capture_output=True, text=True, timeout=60)
            self.send_text(200 if proc.returncode == 0 else 500, proc.stdout if proc.returncode == 0 else proc.stderr)
            return
        if path in ("/quote", "/chart"):
            # one instrument or several, as trade.py takes them; anything with a name, a ticker or an ISIN
            what = [w for w in urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("instrument", []) if w][:20]
            out = (self.run_json(["trade.py", path[1:]] + what, timeout=120) if what
                   else {"ok": False, "error": "missing ?instrument="})
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
