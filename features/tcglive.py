"""
Live TCGplayer price points for one product, shown in the card drawer.

GET /api/tcglive?product=<tcgplayer product id>
    -> {"product", "fetched_at", "printings": {"Normal": {"market", "listed_median"}, "Foil": {...}}}
    market = TCGplayer market price (recent sales); listed_median = median of current listings.

Uses TCGplayer's public price-points endpoint (the one behind the product page's price guide).
Individual listings and latest sales are not available: TCGplayer refuses those requests from
anything but its own site, so the drawer links to the filtered product page instead.
Results are cached in memory for 10 minutes per product.
"""
import json
import threading
import time
import urllib.error
import urllib.request

import mtg_arb

TTL = 600
_cache = {}
_lock = threading.Lock()


def _fetch(pid):
    req = urllib.request.Request(f"https://mpapi.tcgplayer.com/v2/product/{pid}/pricepoints", headers=mtg_arb.UA)
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.load(r)
    return {p["printingType"]: {"market": p.get("marketPrice"), "listed_median": p.get("listedMedianPrice")}
            for p in data if p.get("printingType")}


def get_live(state, query, body):
    pid = query.get("product", "")
    if not pid.isdigit():
        return {"error": "product must be a TCGplayer product id"}, 400
    with _lock:
        hit = _cache.get(pid)
    if hit and time.time() - hit["fetched_at"] < TTL:
        return hit
    try:
        res = {"product": int(pid), "fetched_at": time.time(), "printings": _fetch(pid)}
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        return {"error": f"TCGplayer unavailable: {e}"}, 502
    with _lock:
        _cache[pid] = res
    return res


ROUTES = {("GET", "/api/tcglive"): get_live}
