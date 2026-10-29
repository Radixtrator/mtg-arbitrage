"""
CardConduit buylist prices, pulled one card at a time.

Every candidate starts with an estimated CardConduit offer (mtg_arb.cc_estimate). Pulling a card asks
CardConduit's price-check search for that printing (1-2 requests) and replaces the estimate for all its
finishes. Pulled prices are kept in data/cardconduit.json and used for PULL_TTL_DAYS days.

  POST /api/cardconduit/pull {"id": scryfall id}
       -> {"prices": {finish: usd | null}, "pulled_at"}   null = CardConduit isn't buying that finish
"""
import datetime
import json
import threading
import time

import mtg_arb

PULL_TTL_DAYS = 3
_lock = threading.Lock()
_last_request = 0.0


def _path(state):
    return state.data_dir / "cardconduit.json"


def _read(state):
    try:
        with open(_path(state), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def _fresh(pulled):
    """Drop pulls older than PULL_TTL_DAYS."""
    cutoff = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=PULL_TTL_DAYS)).isoformat()
    return {k: v for k, v in pulled.items() if v.get("pulled_at", "") >= cutoff}


def _apply(cands, pulled):
    for c in cands:
        hit = pulled.get(c["id"])
        if hit:
            c["cc"] = hit["prices"].get(c["finish"])
            c["cc_est"] = False
            c["cc_pulled_at"] = hit["pulled_at"]


def on_load(state):
    _apply(state.cands, _fresh(_read(state)))


def pull(state, query, body):
    global _last_request
    sid = (body or {}).get("id", "")
    with state.lock:
        cands = [c for c in state.cands if c["id"] == sid]
    if not cands:
        return {"error": "unknown card"}, 404
    with _lock:  # one CardConduit lookup at a time, at most one per second
        time.sleep(max(0.0, _last_request + 1 - time.time()))
        _last_request = time.time()
        found = mtg_arb.fetch_cardconduit(cands[0]["name"], cands[0]["set"], sid)
        entry = {"prices": {c["finish"]: found.get(c["finish"]) for c in cands},
                 "pulled_at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")}
        pulled = _fresh(_read(state))
        pulled[sid] = entry
        tmp = _path(state).with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(pulled, f)
        tmp.replace(_path(state))
    _apply(cands, {sid: entry})
    return entry


ROUTES = {("POST", "/api/cardconduit/pull"): pull}
