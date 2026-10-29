"""
Buy list + portfolio tracker. Persists to data/buylist.json.

  GET    /api/buylist[?status=planned]   -> {"items": [...]}  (each enriched with current prices)
  POST   /api/buylist  {"items": [{"row": <scan row>, "qty": 1}, ...]}  -> {"items": [...], "added": n}
         (a row may also be {"id": ..., "finish": ...} alone; missing fields are filled from the price data)
  PUT    /api/buylist  {"key": "...", <fields>}  or  {"keys": [...], <fields>}  -> {"items": [...]}
  DELETE /api/buylist  {"key": "..."}  or  {"keys": [...]}  -> {"items": [...], "removed": n}

Keys: an item's `card_key` is always "<id>:<finish>". Its `key` is the same string for the
first lot of a card; if the card is added again while no lot is still "planned" (e.g. the first
copies are already ordered), the new lot gets key "<id>:<finish>#2", "#3", ... so keys stay unique.

Current prices added on GET (from state.cands, None if the printing dropped out of the data):
  eur (Cardmarket trend), tcg (TCGplayer market), mp (Mana Pool), mp_qty, ck (Card Kingdom cash
  buylist), ck_qty, cc (CardConduit buylist), priced (bool), plus cardkingdom_url / cardconduit_url /
  image_normal / scryfall_url / rarity.
Snapshot when added: cardmarket_eur, tcgplayer_usd, manapool_usd, cardkingdom_buy_usd, cardconduit_buy_usd,
sold_on, sell_price_usd and net_sale_usd (for sold_on="offer" rows from the list checker these are the
buyer's offer and its net, used for expected profit instead of a live store price).
sell_via (editable, "" = follow the sidebar storefront): tcgplayer | manapool | cardkingdom | cardconduit | credit | local.
"""
import json
import os
import threading
import time

STATUSES = ["planned", "ordered", "received", "listed", "sold"]
CONDITIONS = ["M", "NM", "EX", "GD", "LP", "PL", "PO"]
CARD_FIELDS = ["id", "name", "set", "set_name", "number", "finish", "image_small",
               "cardmarket_url", "tcgplayer_url", "manapool_url", "cardkingdom_url", "cardconduit_url"]
EDITABLE = {"qty", "condition", "status", "actual_buy_eur", "actual_sell_usd", "order_ref",
            "notes", "sold_on", "sell_via", "cardmarket_eur", "tcgplayer_usd", "manapool_usd",
            "cardkingdom_buy_usd", "cardconduit_buy_usd", "sell_price_usd", "net_sale_usd"}
SOLD_ON = ("tcgplayer", "manapool", "cardkingdom", "cardconduit", "offer")
SELL_VIA = ("", "tcgplayer", "manapool", "cardkingdom", "cardconduit", "offer", "credit", "local")  # "" = follow sidebar

_lock = threading.RLock()
_index = {}  # "<id>:<finish>" -> candidate dict from state.cands
_FILE = None


def on_load(state):
    global _index
    _index = {f"{c['id']}:{c['finish']}": c for c in state.cands}


def _path(state):
    global _FILE
    if _FILE is None:
        os.makedirs(state.data_dir, exist_ok=True)
        _FILE = os.path.join(state.data_dir, "buylist.json")
    return _FILE


def _read(state):
    p = _path(state)
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f).get("items", [])
    except FileNotFoundError:
        return []
    except ValueError:
        # Keep the corrupt file around instead of silently overwriting the user's data.
        os.replace(p, p + f".corrupt-{int(time.time())}")
        return []


def _write(state, items):
    p = _path(state)
    tmp = f"{p}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "items": items}, f, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _num(v, cast=float):
    if v is None or v == "":
        return None
    v = cast(v)
    if v != v or v < 0:  # NaN / negative
        raise ValueError("must be a non-negative number")
    return v


def _enrich(item):
    c = _index.get(item.get("card_key") or f"{item['id']}:{item['finish']}")
    out = dict(item)
    out.update({
        "eur": c["eur"] if c else None,
        "tcg": c["tcg"] if c else None,
        "mp": c["mp"] if c else None,
        "mp_qty": c["mp_qty"] if c else None,
        "ck": c.get("ck") if c else None,
        "ck_qty": c.get("ck_qty") if c else None,
        "cc": c.get("cc") if c else None,
        "priced": bool(c),
        "cardkingdom_url": (c or {}).get("cardkingdom_url") or item.get("cardkingdom_url", ""),
        "cardconduit_url": (c or {}).get("cardconduit_url") or item.get("cardconduit_url", ""),
        "cardmarket_url": (c or {}).get("cardmarket_url") or item.get("cardmarket_url", ""),
        "tcgplayer_url": (c or {}).get("tcgplayer_url") or item.get("tcgplayer_url", ""),
        "image_normal": (c or {}).get("image_normal", ""),
        "scryfall_url": (c or {}).get("scryfall_url", ""),
        "rarity": (c or {}).get("rarity", ""),
    })
    return out


def _listing(items, status=None):
    return {"items": [_enrich(i) for i in items if not status or i.get("status") == status]}


def _new_item(row, qty, existing_keys):
    card_key = f"{row['id']}:{row['finish']}"
    c = _index.get(card_key, {})
    pick = lambda *ks: next((row[k] for k in ks if row.get(k) not in (None, "")), None)  # noqa: E731
    item = {k: row.get(k) if row.get(k) not in (None, "") else c.get(k, "") for k in CARD_FIELDS}
    key, n = card_key, 1
    while key in existing_keys:
        n += 1
        key = f"{card_key}#{n}"
    ts = _now()
    item.update({
        "key": key,
        "card_key": card_key,
        "qty": qty,
        "condition": row.get("condition") or "NM",
        "status": "planned",
        # price snapshot at the moment the card was added
        "cardmarket_eur": pick("cardmarket_eur", "eur") or c.get("eur"),
        "tcgplayer_usd": pick("tcgplayer_usd", "tcg") or c.get("tcg"),
        "manapool_usd": pick("manapool_usd", "mp") or c.get("mp"),
        "cardkingdom_buy_usd": pick("cardkingdom_buy_usd", "ck") or c.get("ck"),
        "cardconduit_buy_usd": pick("cardconduit_buy_usd", "cc") or c.get("cc"),
        "sold_on": row.get("sold_on") if row.get("sold_on") in SOLD_ON
                   else ("tcgplayer" if (row.get("tcgplayer_usd") or c.get("tcg")) else "manapool"),
        "sell_price_usd": pick("sell_price_usd"),
        "net_sale_usd": pick("net_sale_usd"),
        "sell_via": "",
        "actual_buy_eur": None,
        "actual_sell_usd": None,
        "order_ref": "",
        "notes": row.get("notes") or "",
        "created_at": ts,
        "updated_at": ts,
    })
    return item


def get_items(state, query, body):
    with _lock:
        items = _read(state)
    status = query.get("status")
    if status and status not in STATUSES:
        return {"error": f"status must be one of {STATUSES}"}, 400
    return _listing(items, status)


def add_items(state, query, body):
    reqs = (body or {}).get("items")
    if not isinstance(reqs, list) or not reqs:
        return {"error": 'expected {"items": [{"row": {...}, "qty": 1}, ...]}'}, 400
    with _lock:
        items = _read(state)
        added = 0
        for r in reqs:
            row = (r or {}).get("row") or {}
            if not row.get("id") or row.get("finish") not in ("nonfoil", "foil", "etched"):
                return {"error": "each row needs an id and a finish (nonfoil/foil/etched)"}, 400
            try:
                qty = _num(r.get("qty", 1), int) or 1
            except (TypeError, ValueError):
                return {"error": "qty must be a positive integer"}, 400
            card_key = f"{row['id']}:{row['finish']}"
            hit = next((i for i in items if i.get("card_key", i["key"]) == card_key and i["status"] == "planned"), None)
            if hit:
                hit["qty"] += qty
                hit["updated_at"] = _now()
            else:
                items.append(_new_item(row, qty, {i["key"] for i in items}))
            added += qty
        _write(state, items)
    out = _listing(items)
    out["added"] = added
    return out


def _clean(field, v):
    if field == "qty":
        v = _num(v, int)
        if not v:
            raise ValueError("qty must be >= 1")
        return v
    if field == "status":
        if v not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        return v
    if field == "condition":
        if v not in CONDITIONS:
            raise ValueError(f"condition must be one of {CONDITIONS}")
        return v
    if field in ("actual_buy_eur", "actual_sell_usd", "cardmarket_eur", "tcgplayer_usd", "manapool_usd",
                 "cardkingdom_buy_usd", "cardconduit_buy_usd", "sell_price_usd", "net_sale_usd"):
        v = _num(v)
        return round(v, 2) if v is not None else None
    if field == "sold_on":
        if v not in SOLD_ON:
            raise ValueError(f"sold_on must be one of {SOLD_ON}")
        return v
    if field == "sell_via":
        v = v or ""
        if v not in SELL_VIA:
            raise ValueError(f"sell_via must be one of {SELL_VIA}")
        return v
    return str(v or "")[:500]


def update_items(state, query, body):
    body = body or {}
    keys = body.get("keys") or ([body["key"]] if body.get("key") else [])
    if not keys:
        return {"error": 'expected {"key": "..."} or {"keys": [...]}'}, 400
    try:
        changes = {k: _clean(k, v) for k, v in body.items() if k in EDITABLE}
    except (TypeError, ValueError) as e:
        return {"error": str(e)}, 400
    if not changes:
        return {"error": f"nothing to update; editable fields: {sorted(EDITABLE)}"}, 400
    with _lock:
        items = _read(state)
        hit = [i for i in items if i["key"] in keys]
        if not hit:
            return {"error": "no such key"}, 404
        ts = _now()
        for i in hit:
            if "status" in changes and changes["status"] != i["status"]:
                i.setdefault("status_dates", {})[changes["status"]] = ts
            i.update(changes)
            i["updated_at"] = ts
        _write(state, items)
    out = _listing(items)
    out["updated"] = len(hit)
    return out


def delete_items(state, query, body):
    body = body or {}
    keys = set(body.get("keys") or ([body["key"]] if body.get("key") else []))
    if not keys:
        return {"error": 'expected {"key": "..."} or {"keys": [...]}'}, 400
    with _lock:
        items = _read(state)
        kept = [i for i in items if i["key"] not in keys]
        removed = len(items) - len(kept)
        if removed:
            _write(state, kept)
    out = _listing(kept)
    out["removed"] = removed
    return out


ROUTES = {
    ("GET", "/api/buylist"): get_items,
    ("POST", "/api/buylist"): add_items,
    ("PUT", "/api/buylist"): update_items,
    ("DELETE", "/api/buylist"): delete_items,
}
