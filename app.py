#!/usr/bin/env python3
"""
Local web UI for mtg_arb. Run `python3 app.py` and open http://localhost:8765

Loads Scryfall + Mana Pool + Card Kingdom data once into memory, then serves:
  GET  /               the UI (ui/index.html)
  GET  /ui/<path>      static UI files (feature scripts live in ui/features/*.js)
  GET  /api/meta       fx rate, data files, set list, defaults, feature scripts
  GET  /api/scan       filtered + scored rows (query params mirror mtg_arb.DEFAULTS)
  POST /api/refresh    re-download Scryfall + Mana Pool data and the FX rate
Card images and live per-card prices are fetched by the browser straight from Scryfall.

Feature plugins: every module in features/ may define
  ROUTES = {("GET", "/api/foo"): handler, ...}   handler(state, query, body) -> obj | (obj, status)
      query: dict of str -> str (last value wins); body: parsed JSON for POST/PUT/DELETE, else None
  on_load(state)                                  called after every (re)load of price data
`state` is the State class below: state.fx, state.cands, state.sets, state.scan(params), ...

Hosting (see README "Hosting"): every option also reads an environment variable --
  MTG_HOST (127.0.0.1; use 0.0.0.0 in Docker), MTG_PORT (8765),
  MTG_REFRESH_HOUR (UTC hour for the daily re-download, default 10; -1 disables),
  MTG_PASSWORD (if set, HTTP basic auth with any username and this password).
"""
import argparse
import base64
import datetime
import hmac
import importlib
import json
import mimetypes
import os
import pkgutil
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import mtg_arb

ROOT = Path(__file__).resolve().parent
UI = ROOT / "ui"
DATA = ROOT / "data"  # user data that features persist (buy list, history, ...)
LIST_PARAMS = mtg_arb.LIST_PARAMS
STR_PARAMS = mtg_arb.STR_PARAMS


class State:
    lock = threading.Lock()
    fx = None
    path = None
    mp_as_of = ""
    ck_as_of = ""
    cm_as_of = ""
    cands = []
    sets = []
    data_dir = DATA

    @classmethod
    def load(cls, refresh=False, fx=None):
        path = mtg_arb.get_bulk_file(refresh)
        rate = fx or mtg_arb.get_fx()
        manapool, mp_as_of = mtg_arb.get_manapool_prices(refresh)
        cardkingdom, ck_as_of = mtg_arb.get_cardkingdom_buylist(refresh)
        cmguide, cm_as_of = mtg_arb.get_cardmarket_guide(refresh)
        cands = mtg_arb.load_candidates(path, manapool, cardkingdom, cmguide)
        names = {}
        for c in cands:
            names.setdefault(c["set"], (c["set_name"], c["released_at"]))
        sets = sorted(({"code": k, "name": n, "released": r} for k, (n, r) in names.items()),
                      key=lambda s: s["released"], reverse=True)
        with cls.lock:
            cls.fx, cls.path, cls.cands, cls.sets = rate, path, cands, sets
            cls.mp_as_of, cls.ck_as_of, cls.cm_as_of = mp_as_of, ck_as_of, cm_as_of
        print(f"Loaded {len(cands)} priced printings from {path.name}, EUR->USD {rate}")
        for mod in FEATURES:
            if hasattr(mod, "on_load"):
                try:
                    mod.on_load(cls)
                except Exception:  # noqa: BLE001 -- one broken feature must not kill the app
                    traceback.print_exc()

    @classmethod
    def scan(cls, params):
        """Score all candidates with the given params dict (see mtg_arb.DEFAULTS)."""
        params = dict(params)
        fx = params.pop("fx", None) or cls.fx
        with cls.lock:
            cands = cls.cands
        return mtg_arb.score(cands, fx, params)


def load_features():
    import features
    mods = []
    for info in pkgutil.iter_modules(features.__path__):
        try:
            mods.append(importlib.import_module(f"features.{info.name}"))
        except Exception:  # noqa: BLE001
            traceback.print_exc()
    return mods


FEATURES = []
ROUTES = {}
PASSWORD = os.environ.get("MTG_PASSWORD", "")


def daily_refresh(hour):
    """Re-download all price data once a day at `hour`:00 UTC (Scryfall publishes around 09:00 UTC)."""
    while True:
        now = datetime.datetime.now(datetime.UTC)
        nxt = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if nxt <= now:
            nxt += datetime.timedelta(days=1)
        time.sleep((nxt - now).total_seconds())
        try:
            State.load(refresh=True)
        except Exception:  # noqa: BLE001 -- keep serving yesterday's data and try again tomorrow
            traceback.print_exc()


def parse_params(qs):
    p = {}
    for k, v in parse_qs(qs).items():
        v = v[-1]
        if k in LIST_PARAMS:
            p[k] = [x for x in v.split(",") if x] or None
        elif k in STR_PARAMS:
            p[k] = v
        elif k in mtg_arb.DEFAULTS or k == "fx":
            p[k] = float(v) if v != "" else None
    return p


class Handler(BaseHTTPRequestHandler):
    def parse_request(self):
        """Gate every request behind basic auth when MTG_PASSWORD is set."""
        if not super().parse_request():
            return False
        if not PASSWORD:
            return True
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                _, _, pw = base64.b64decode(auth[6:]).decode().partition(":")
            except ValueError:
                pw = ""
            if hmac.compare_digest(pw.encode(), PASSWORD.encode()):
                return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="MTG Arbitrage"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, ctype=None):
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype or mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def route(self, method):
        url = urlparse(self.path)
        handler = ROUTES.get((method, url.path))
        if not handler:
            return False
        body = None
        if method != "GET":
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            try:
                body = json.loads(raw) if raw else {}
            except ValueError:
                self.send_json({"error": "invalid JSON body"}, 400)
                return True
        query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        try:
            res = handler(State, query, body)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self.send_json({"error": str(e)}, 500)
            return True
        obj, code = res if isinstance(res, tuple) else (res, 200)
        self.send_json(obj, code)
        return True

    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            self.send_file(UI / "index.html", "text/html; charset=utf-8")
        elif url.path.startswith("/ui/"):
            f = (UI / url.path[4:]).resolve()
            if f.is_file() and UI in f.parents:
                self.send_file(f)
            else:
                self.send_error(404)
        elif url.path == "/api/meta":
            with State.lock:
                self.send_json({
                    "fx": State.fx, "data_file": State.path.name, "mp_as_of": State.mp_as_of, "ck_as_of": State.ck_as_of, "cm_as_of": State.cm_as_of,
                    "count": len(State.cands), "sets": State.sets, "defaults": mtg_arb.DEFAULTS,
                    "features": sorted(f"/ui/features/{p.name}" for p in (UI / "features").glob("*.js")),
                })
        elif url.path == "/api/scan":
            try:
                params = parse_params(url.query)
            except ValueError as e:
                return self.send_json({"error": str(e)}, 400)
            qs = parse_qs(url.query)
            offset = int(qs.get("offset", ["0"])[0])
            limit = min(int(qs.get("limit", ["120"])[0]), 5000)
            fx = params.get("fx") or State.fx
            rows = State.scan(params)
            self.send_json({
                "fx": fx,
                "total": len(rows),
                "total_profit": round(sum(r["profit_usd"] for r in rows), 2),
                "total_cost": round(sum(r["landed_cost_usd"] for r in rows), 2),
                "rows": rows[offset:offset + limit],
            })
        elif not self.route("GET"):
            self.send_error(404)

    def do_POST(self):
        if urlparse(self.path).path == "/api/refresh":
            State.load(refresh=True)
            self.send_json({"ok": True, "data_file": State.path.name, "fx": State.fx})
        elif not self.route("POST"):
            self.send_error(404)

    def do_PUT(self):
        if not self.route("PUT"):
            self.send_error(404)

    def do_DELETE(self):
        if not self.route("DELETE"):
            self.send_error(404)

    def log_message(self, fmt, *args):
        pass


def main():
    ap = argparse.ArgumentParser(description="Web UI for the MTG Cardmarket -> US scanner")
    env = os.environ.get
    ap.add_argument("--host", default=env("MTG_HOST", "127.0.0.1"), help="0.0.0.0 to accept other machines")
    ap.add_argument("--port", type=int, default=int(env("MTG_PORT", "8765")))
    ap.add_argument("--refresh-hour", type=int, default=int(env("MTG_REFRESH_HOUR", "10")),
                    help="UTC hour to re-download prices every day (-1: never)")
    ap.add_argument("--fx", type=float, help="fixed EUR->USD rate instead of the live one")
    a = ap.parse_args()
    DATA.mkdir(exist_ok=True)
    FEATURES.extend(load_features())
    for mod in FEATURES:
        ROUTES.update(getattr(mod, "ROUTES", {}))
    State.load(fx=a.fx)
    if 0 <= a.refresh_hour <= 23:
        threading.Thread(target=daily_refresh, args=(a.refresh_hour,), name="daily-refresh", daemon=True).start()
    server = ThreadingHTTPServer((a.host, a.port), Handler)
    if a.host not in ("127.0.0.1", "localhost") and not PASSWORD:
        print("WARNING: listening on all interfaces without MTG_PASSWORD -- anyone who can reach this port can use it")
    print(f"Open http://localhost:{a.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
