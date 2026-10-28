"""
Price history + trends. Persists to data/history.sqlite.

On every (re)load of price data, today's (UTC) prices for every candidate are written to
the `prices` table in a background thread (one snapshot per date; a same-day refresh
replaces it). Past dates can be backfilled from MTGJSON (tools/backfill_mtgjson.py).

  GET  /api/history?id=..&finish=..[&store=tcgplayer&<scan filters>]
       -> {"series": [{"date","eur","eur_usd","tcg","mp","ck","fx","profit","ok"}...],
           "summary": {"days", "days_ok", "first", "last", "store"}}
       profit/ok: that day's profit and whether it passed min_profit/min_roi under the given
       filters' cost model, selling on `store` (default: storefront filter, or tcgplayer).
  GET  /api/history/movers?days=7&limit=50&order=widened|cm_drop|consistent&<scan filters>
       -> {"rows": [scan row + spread_now, spread_then, spread_change, cm_change_pct,
                    us_change_pct, days_tracked, days_ok, new, spark: [spread...]], "total", "new_count",
           "then_date", "dates", "window_days"}
       spread = US price on the row's store - Cardmarket price in USD (that day's FX).
  GET  /api/history/status   -> {"dates", "first", "last", "rows", "snapshot": {...}, "backfill": {...}}
  POST /api/history/backfill {"days": 90}  -> starts the MTGJSON backfill in the background

Schema (see tools/backfill_mtgjson.py): cards(k, id, finish), price_rows(k, d, cents...) WITHOUT
ROWID with PK (k, d) as the per-card index, days(date, fx, source, rows, taken_at), and a
readable view prices(date, id, finish, eur, tcg, mp, mp_qty, ck).
"""
import datetime
import sqlite3
import sys
import threading
import time
import traceback
from pathlib import Path
from urllib.parse import urlencode

import mtg_arb

ROOT = Path(__file__).resolve().parent.parent
_tools = str(ROOT / "tools")
if _tools not in sys.path:
    sys.path.insert(0, _tools)
import backfill_mtgjson as bf  # noqa: E402

STORE_COL = {"tcgplayer": "tcg", "manapool": "mp", "cardkingdom": "ck"}
_write_lock = threading.Lock()
_snap = {"running": False, "date": None, "rows": 0, "seconds": None, "error": None}
_bf = {"running": False, "stage": None, "done": 0, "total": 0, "msg": "", "result": None, "error": None}
_db_path = None


def _path(state):
    global _db_path
    _db_path = Path(state.data_dir) / "history.sqlite"
    return _db_path


def _connect(state=None):
    return bf.connect(_path(state) if state else _db_path)


def _today():
    return datetime.datetime.now(datetime.UTC).date().isoformat()


# ---------------------------------------------------------------- snapshot

def _snapshot(state, cands, fx):
    t0 = time.time()
    date = _today()
    _snap.update(running=True, date=date, error=None)
    try:
        d = bf.day_num(date)
        c_ = bf.cents
        with _write_lock:
            db = _connect(state)
            try:
                keys = bf.card_keys(db, [(c["id"], c["finish"]) for c in cands])
                rows = sorted((keys[(c["id"], c["finish"])], d, c_(c["eur"]), c_(c.get("tcg")), c_(c.get("mp")),
                               c.get("mp_qty"), c_(c.get("ck"))) for c in cands)
                with db:
                    db.executemany("INSERT OR REPLACE INTO price_rows (k, d, eur, tcg, mp, mp_qty, ck) "
                                   "VALUES (?,?,?,?,?,?,?)", rows)
                    db.execute("INSERT OR REPLACE INTO days (date, fx, source, rows, taken_at) VALUES (?,?,?,?,?)",
                               (date, fx, "snapshot", len(rows),
                                datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")))
            finally:
                db.close()
        _snap.update(rows=len(rows), seconds=round(time.time() - t0, 2))
        print(f"[history] snapshot {date}: {len(rows):,} rows in {time.time() - t0:.2f}s")
    except Exception as e:  # noqa: BLE001
        _snap["error"] = str(e)
        traceback.print_exc()
    finally:
        _snap["running"] = False


def on_load(state):
    _path(state)
    threading.Thread(target=_snapshot, args=(state, list(state.cands), state.fx),
                     name="history-snapshot", daemon=True).start()


# ---------------------------------------------------------------- helpers

def _params(query):
    """Scan params from the query dict, parsed exactly like /api/scan."""
    qs = urlencode({k: v for k, v in query.items()
                    if k not in ("id", "finish", "store", "days", "limit", "offset", "order")})
    main = sys.modules.get("__main__")
    parse = getattr(main, "parse_params", None)
    if parse is None:
        import app
        parse = app.parse_params
    return parse(qs)


def _model(params):
    return {**mtg_arb.DEFAULTS, **{k: v for k, v in params.items() if v is not None}}


def _day_profit(eur, price, store, fx, p):
    if not eur or not price or not fx:
        return None, False
    landed = mtg_arb.landed_cost(eur, fx, p)
    profit = mtg_arb.net_sale(price, store, p) - landed
    ok = profit >= p["min_profit"] and (profit / landed * 100 if landed else 0) >= p["min_roi"]
    return round(profit, 2), ok


def _fx_by_date(db):
    return dict(db.execute("SELECT date, fx FROM days"))


def _r(v):
    return None if v is None else round(v, 2)


# ---------------------------------------------------------------- endpoints

def get_history(state, query, body):
    cid, finish = query.get("id"), query.get("finish", "nonfoil")
    if not cid:
        return {"error": "id is required"}, 400
    p = _model(_params(query))
    store = query.get("store") or (p["storefront"] if p["storefront"] in STORE_COL else "tcgplayer")
    col = STORE_COL.get(store, "tcg")
    db = _connect(state)
    try:
        fx = _fx_by_date(db)
        rows = db.execute("SELECT p.d, p.eur, p.tcg, p.mp, p.ck FROM cards c JOIN price_rows p USING (k) "
                          "WHERE c.id = ? AND c.finish = ? ORDER BY p.d", (cid, finish)).fetchall()
    finally:
        db.close()
    series, ok_days = [], 0
    for d, e, t, m, k in rows:
        date, e, t, m, k = bf.day_iso(d), *(None if v is None else v / 100 for v in (e, t, m, k))
        f = fx.get(date) or state.fx
        price = {"tcg": t, "mp": m, "ck": k}[col]
        profit, ok = _day_profit(e, price, store, f, p)
        ok_days += ok
        series.append({"date": date, "eur": e, "eur_usd": _r(e * f) if e else None, "tcg": t, "mp": m, "ck": k,
                       "fx": f, "profit": profit, "ok": ok})
    return {"series": series, "summary": {"days": len(series), "days_ok": ok_days, "store": store,
                                          "first": series[0]["date"] if series else None,
                                          "last": series[-1]["date"] if series else None}}


def get_movers(state, query, body):
    days = max(1, int(float(query.get("days") or 7)))
    limit = max(1, min(int(float(query.get("limit") or 50)), 500))
    order = query.get("order") or "widened"
    params = _params(query)
    p = _model(params)
    fx_now = params.get("fx") or state.fx
    opps = state.scan(params)
    today = _today()
    cutoff = (datetime.date.fromisoformat(today) - datetime.timedelta(days=days)).isoformat()

    db = _connect(state)
    try:
        fx = _fx_by_date(db)
        past = sorted(d for d in fx if d < today)
        window = [d for d in past if d >= cutoff]
        before = [d for d in past if d <= cutoff]
        then_date = before[-1] if before else (window[0] if window else None)
        if then_date and then_date not in window:
            window = [then_date] + window
        hist = {}
        if window and opps:
            db.execute("CREATE TEMP TABLE want (id TEXT, finish TEXT, PRIMARY KEY (id, finish)) WITHOUT ROWID")
            db.executemany("INSERT OR IGNORE INTO want VALUES (?, ?)", [(r["id"], r["finish"]) for r in opps])
            cur = db.execute("SELECT c.id, c.finish, p.d, p.eur, p.tcg, p.mp, p.ck FROM want w "
                             "JOIN cards c ON c.id = w.id AND c.finish = w.finish "
                             "JOIN price_rows p ON p.k = c.k AND p.d >= ? AND p.d < ?",
                             (bf.day_num(window[0]), bf.day_num(today)))
            iso = {bf.day_num(x): x for x in window}
            for cid, fin, d, e, t, m, k in cur:
                hist.setdefault((cid, fin), {})[iso.get(d) or bf.day_iso(d)] = (
                    e and e / 100, t and t / 100, m and m / 100, k and k / 100)
    finally:
        db.close()

    idx = {"tcgplayer": 1, "manapool": 2, "cardkingdom": 3}
    out = []
    for r in opps:
        h = hist.get((r["id"], r["finish"]), {})
        store = r["sold_on"]
        i = idx.get(store, 1)
        spark, ok_days = [], 0
        for d in window:
            v = h.get(d)
            if not v or not v[0] or not v[i]:
                spark.append(None)
                continue
            f = fx.get(d) or fx_now
            spark.append(round(v[i] - v[0] * f, 2))
            ok_days += _day_profit(v[0], v[i], store, f, p)[1]
        spread_now = round(r["sell_price_usd"] - r["cardmarket_eur"] * fx_now, 2)
        spark.append(spread_now)
        then = h.get(then_date) if then_date else None
        spread_then = cm_chg = us_chg = None
        new = True
        if then and then[0]:
            cm_chg = round((r["cardmarket_eur"] / then[0] - 1) * 100, 1)
            if then[i]:
                spread_then = round(then[i] - then[0] * (fx.get(then_date) or fx_now), 2)
                us_chg = round((r["sell_price_usd"] / then[i] - 1) * 100, 1)
                new = not _day_profit(then[0], then[i], store, fx.get(then_date) or fx_now, p)[1]
        out.append({**r, "spread_now": spread_now, "spread_then": spread_then,
                    "spread_change": None if spread_then is None else round(spread_now - spread_then, 2),
                    "cm_change_pct": cm_chg, "us_change_pct": us_chg,
                    "days_tracked": sum(1 for d in window if d in h) + 1, "days_ok": ok_days + 1,
                    "new": new, "spark": spark})

    low = float("-inf")
    if order == "cm_drop":
        out.sort(key=lambda x: (x["cm_change_pct"] is None, x["cm_change_pct"] or 0, -x["profit_usd"]))
    elif order == "consistent":
        out.sort(key=lambda x: (-x["days_ok"], -x["profit_usd"]))
    else:
        out.sort(key=lambda x: (x["spread_change"] if x["spread_change"] is not None else low, x["profit_usd"]),
                 reverse=True)
    return {"rows": out[:limit], "total": len(out), "new_count": sum(x["new"] for x in out) if then_date else 0,
            "then_date": then_date, "today": today,
            "dates": window + [today], "window_days": days, "fx": fx_now}


def get_status(state, query, body):
    db = _connect(state)
    try:
        first, last, n, rows = db.execute("SELECT min(date), max(date), count(*), sum(rows) FROM days").fetchone()
        sources = dict(db.execute("SELECT source, count(*) FROM days GROUP BY source"))
    finally:
        db.close()
    size = _db_path.stat().st_size if _db_path and _db_path.exists() else 0
    return {"dates": n, "first": first, "last": last, "rows": rows or 0, "sources": sources,
            "db_mb": round(size / 1e6, 1), "snapshot": dict(_snap), "backfill": dict(_bf)}


def _run_backfill(state, days):
    def prog(stage, done, total, msg):
        _bf.update(stage=stage, done=done, total=total, msg=msg)
    try:
        wanted = {(c["id"], c["finish"]) for c in state.cands}
        with _write_lock:
            _bf["result"] = bf.backfill(days, wanted, fx_fallback=state.fx, db_path=_db_path, progress=prog)
        print(f"[history] backfill done: {_bf['result']}")
    except Exception as e:  # noqa: BLE001
        _bf.update(error=str(e), stage="error", msg=f"Backfill failed: {e}")
        traceback.print_exc()
    finally:
        _bf["running"] = False


def post_backfill(state, query, body):
    if _bf["running"]:
        return {"ok": False, "error": "backfill already running", "backfill": dict(_bf)}, 409
    days = int((body or {}).get("days") or query.get("days") or 90)
    _path(state)
    _bf.update(running=True, stage="start", done=0, total=0, msg="Starting…", result=None, error=None)
    threading.Thread(target=_run_backfill, args=(state, days), name="history-backfill", daemon=True).start()
    return {"ok": True, "backfill": dict(_bf)}


ROUTES = {
    ("GET", "/api/history"): get_history,
    ("GET", "/api/history/movers"): get_movers,
    ("GET", "/api/history/status"): get_status,
    ("POST", "/api/history/backfill"): post_backfill,
}
