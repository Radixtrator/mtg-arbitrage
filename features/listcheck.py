"""
"Check a list" feature: paste any card list (want list, decklist, Cardmarket wants/export,
a US store's buylist, a CSV) and see which cards/printings are worth importing.

POST /api/listcheck  {"text": "...", "params": <readFilters() output or parsed params>}
  -> {"lines": [...], "totals": {...}, "fx": ..., "thresholds": {...}}

Every printing (and finish) of each listed card is scored with mtg_arb.score using the
user's cost model and storefront / sell mode, but with the min filters relaxed so that
unprofitable cards still show. An optional "@ $12.50" (or tab-separated / CSV price column)
is treated as the price a US buyer offers and replaces the market price.
"""
import csv
import difflib
import io
import re
import threading
import unicodedata
from urllib.parse import parse_qs

import mtg_arb

MAX_LINES = 3000
MAX_SUGGEST_LINES = 60  # difflib is slow-ish; only suggest for the first N unknown names

# ---------------------------------------------------------------- params

_LIST = mtg_arb.LIST_PARAMS
_STR = mtg_arb.STR_PARAMS


def normalize_params(raw):
    """Same normalization as app.parse_params, but accepts a dict (strings or typed values)."""
    if isinstance(raw, str):
        raw = {k: v[-1] for k, v in parse_qs(raw).items()}
    p = {}
    for k, v in (raw or {}).items():
        if k in _LIST:
            if isinstance(v, str):
                v = [x for x in v.split(",") if x]
            p[k] = list(v) if v else None
        elif k in _STR:
            p[k] = str(v) if v is not None else None
        elif k in mtg_arb.DEFAULTS or k == "fx":
            if v is None or (isinstance(v, str) and v.strip() == ""):
                p[k] = None
            else:
                p[k] = float(v)
    return p


# ---------------------------------------------------------------- name index

_QUOTES = str.maketrans({"‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'", "`": "'",
                         "´": "'", "“": '"', "”": '"', "„": '"', "″": '"',
                         "–": "-", "—": "-", "−": "-", " ": " "})
_LIGS = {"æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE", "ß": "ss", "ø": "o", "Ø": "O"}


def norm_name(s):
    s = s.translate(_QUOTES)
    for a, b in _LIGS.items():
        s = s.replace(a, b)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower()
    s = re.sub(r"\s*/{1,3}\s*", " // ", s)  # "Fire/Ice", "Fire /// Ice" -> "fire // ice"
    return re.sub(r"\s+", " ", s).strip()


def _loose(k):
    return re.sub(r"[^a-z0-9/]+", "", k)


class Index:
    def __init__(self, cands):
        self.by_name = {}   # full display name -> [cand, ...]
        self.keys = {}      # normalized key -> full display name
        for c in cands:
            self.by_name.setdefault(c["name"], []).append(c)
        for name in self.by_name:
            self.keys.setdefault(norm_name(name), name)
        self.full_keys = list(self.keys)
        for name in self.by_name:  # faces of double-faced / split / adventure cards
            if "//" in name:
                for face in name.split("//"):
                    self.keys.setdefault(norm_name(face), name)
        self.loose = {}     # alphanumerics only ("sheoldred the apocalypse") -> full display name
        for k, name in self.keys.items():
            self.loose.setdefault(_loose(k), name)
        self.sets = {}      # lower set code / set name -> SET CODE
        for c in cands:
            self.sets.setdefault(c["set"].lower(), c["set"])
            if c["set_name"]:
                self.sets.setdefault(norm_name(c["set_name"]), c["set"])

    def lookup(self, name):
        k = norm_name(name)
        if k in self.keys:
            return self.keys[k]
        for alt in (re.sub(r"^a-", "", k),               # Alchemy rebalanced prefix
                    re.sub(r"\s*\(v\.?\s*\d+\)$", "", k),  # Cardmarket "(V.2)" versions
                    k.rstrip(" .,;:")):
            if alt in self.keys:
                return self.keys[alt]
        return self.loose.get(_loose(k))

    def set_code(self, s):
        if not s:
            return None
        s = s.strip()
        return self.sets.get(s.lower()) or self.sets.get(norm_name(s))

    def suggest(self, name, n=3):
        k = norm_name(name)
        hits = difflib.get_close_matches(k, self.full_keys, n=n, cutoff=0.6)
        return [self.keys[h] for h in hits]


_cache = {"id": None, "index": None}
_lock = threading.Lock()


def get_index(state):
    with state.lock:
        cands = state.cands
    with _lock:
        if _cache["id"] != id(cands):
            _cache["index"], _cache["id"] = Index(cands), id(cands)
        return _cache["index"]


def on_load(state):
    with _lock:
        _cache["id"] = None  # rebuilt lazily on the next request


# ---------------------------------------------------------------- parsing

HEADERS = {
    "deck", "decklist", "main", "maindeck", "mainboard", "main deck", "sideboard", "side", "sb", "commander",
    "commanders", "companion", "maybeboard", "maybe", "considering", "tokens", "token", "creatures", "creature",
    "lands", "land", "instants", "instant", "sorceries", "sorcery", "artifacts", "artifact", "enchantments",
    "enchantment", "planeswalkers", "planeswalker", "battles", "battle", "spells", "other", "others", "about",
    "wants", "want list", "wantlist", "wishlist", "buylist", "cards", "singles", "attractions", "stickers",
    "non-creature spells", "noncreature spells", "ramp", "removal", "draw", "card draw", "board wipes",
}
FOIL_RE = re.compile(r"\s*(?:\*F\*|\*foil\*|[(\[]\s*foil\s*[)\]]|-\s*foil\b|\bfoil\s*$)\s*", re.I)
ETCHED_RE = re.compile(r"\s*(?:\*E\*|\*etched\*|[(\[]\s*(?:foil\s+)?etched(?:\s+foil)?\s*[)\]]|-\s*etched\b)\s*", re.I)
NONFOIL_RE = re.compile(r"\s*(?:[(\[]\s*non-?foil\s*[)\]]|-\s*non-?foil\b)\s*", re.I)
PRICE_RE = re.compile(r"(?:^|\s)(?:@\s*\$?|\$)\s*(\d+(?:[.,]\d{1,2})?)\s*(?:usd|\$)?\s*$", re.I)
PRICE_CELL_RE = re.compile(r"^\s*(?:@\s*)?(?:us)?\$?\s*(\d+(?:[.,]\d{1,2})?)\s*(?:usd|\$)?\s*$", re.I)
EURO_RE = re.compile(r"(?:€\s*\d+(?:[.,]\d+)?|\d+(?:[.,]\d+)?\s*(?:€|eur\b))", re.I)
CONDITIONS = {"m", "mt", "mint", "nm", "near mint", "ex", "excellent", "gd", "good", "lp", "light played",
              "lightly played", "pl", "played", "mp", "moderately played", "hp", "heavily played", "po", "poor",
              "dmg", "damaged", "sp"}
LANGS = {"english", "en", "eng", "german", "de", "french", "fr", "italian", "it", "spanish", "es", "japanese",
         "jp", "ja", "portuguese", "pt", "russian", "ru", "korean", "ko", "chinese", "zh", "s-chinese",
         "t-chinese", "simplified chinese", "traditional chinese"}


def _price(s):
    m = PRICE_CELL_RE.match(s or "")
    return float(m.group(1).replace(",", ".")) if m else None


def _is_header(line):
    s = re.sub(r"\s*\(\d+\)\s*$", "", line.strip().rstrip(":").strip()).strip().rstrip(":").lower()
    s = re.sub(r"\s+\d+$", "", s)  # "Creatures 12"
    return s in HEADERS


def parse_line(raw, index):
    """One free-text line -> spec dict, or None to skip."""
    line = raw.strip().lstrip("﻿")
    if not line or line.startswith(("#", "//", "--", ";")) or not re.search(r"[^\W\d_]", line):
        return None
    if _is_header(line):
        return None
    line = re.sub(r"^(?:[-*•·]\s+|SB:\s*)", "", line, flags=re.I)
    spec = {"qty": 1, "set": None, "number": None, "finish": None, "price": None}

    # US offer price: trailing "@ $12.50" / "$12.50" or a tab-separated price cell.
    if "\t" in line:
        cells = [c.strip() for c in line.split("\t") if c.strip()]
        if len(cells) > 1 and _price(cells[-1]) is not None and not EURO_RE.search(cells[-1]):
            spec["price"] = _price(cells.pop())
        line = " ".join(cells)
    m = PRICE_RE.search(line)
    if m and spec["price"] is None:
        spec["price"] = float(m.group(1).replace(",", "."))
        line = line[:m.start()].strip()

    # quantity: "4 ", "4x ", "4 x ", "x4 " prefix or " x4" suffix
    m = re.match(r"^(\d+)\s*[xX×]?\s+(?=\S)", line) or re.match(r"^[xX×]\s*(\d+)\s+", line)
    if m:
        spec["qty"] = max(1, int(m.group(1)))
        line = line[m.end():]
    else:
        m = re.search(r"\s+(?:[xX×]\s*(\d+)|(\d+)\s*[xX×])$", line)
        if m:
            spec["qty"] = max(1, int(m.group(1) or m.group(2)))
            line = line[:m.start()]

    # finish markers
    for rx, fin in ((ETCHED_RE, "etched"), (NONFOIL_RE, "nonfoil")):
        if rx.search(line):
            spec["finish"] = fin
            line = rx.sub(" ", line).strip()
    if spec["finish"] is None and FOIL_RE.search(line) and index.lookup(line) is None:  # "Foil" is a card name
        spec["finish"] = "foil"
        line = FOIL_RE.sub(" ", line).strip()

    # Cardmarket-style tails: "Name (Expansion) - 146 - Near Mint - English - 0,25 €"
    if " - " in line and index.lookup(line) is None:
        parts = [p.strip() for p in line.split(" - ")]
        head, rest = parts[0], []
        for p in parts[1:]:
            pl = p.lower()
            if EURO_RE.search(p) or pl in CONDITIONS or pl in LANGS or pl in ("signed", "altered", "playset"):
                continue
            if re.fullmatch(r"#?[\w★†]+", p) and re.search(r"\d", p) and not spec["number"]:
                spec["number"] = p.lstrip("#")
                continue
            rest.append(p)
        line = " - ".join([head] + rest)
    line = EURO_RE.sub("", line).strip()

    name = line.strip()
    if index.lookup(name) is None:
        # "(SET) 123", "[SET] 123", "(Set Name)", "[SET]", "| SET", or trailing collector number after a set
        m = re.search(r"\s*[(\[]([^()\[\]]+)[)\]]\s*(?:#?([\w★†\-]+))?\s*$", name)
        if m:
            code = index.set_code(m.group(1))
            if code or re.fullmatch(r"[A-Za-z0-9]{2,6}", m.group(1).strip()):
                spec["set"] = code or m.group(1).strip().upper()
                if m.group(2):
                    spec["number"] = m.group(2)
                name = name[:m.start()].strip()
            elif re.fullmatch(r"v\.?\s*\d+", m.group(1).strip(), re.I):
                name = name[:m.start()].strip()
        else:
            m = re.search(r"\s*\|\s*([A-Za-z0-9]{2,6})\s*(?:\|\s*([\w★†\-]+))?\s*$", name)
            if m:
                spec["set"], spec["number"] = m.group(1).upper(), m.group(2)
                name = name[:m.start()].strip()
    spec["name"] = name.strip(" ,;")
    return spec if spec["name"] else None


CSV_COLS = {
    "name": ("name", "card name", "card", "cardname", "english name", "product name", "product", "card_name", "title"),
    "qty": ("quantity", "qty", "count", "amount", "copies", "#", "number of copies", "want", "wants"),
    "set": ("set code", "set", "set_code", "setcode", "edition", "expansion", "exp. name", "exp.", "set name",
            "edition code", "expansion code", "set_name"),
    "number": ("collector number", "collector_number", "collectornumber", "number", "cn", "card number", "nr", "no"),
    "foil": ("foil", "foil?", "is foil", "isfoil", "printing", "finish", "foiling", "treatment"),
    "price": ("offer", "offer price", "buy price", "buylist", "buylist price", "buy price usd", "cash", "cash price",
              "price usd", "usd", "price ($)", "price $", "us price", "offer usd", "we pay", "paying"),
}


def _csv_header(line):
    for delim in (",", ";", "\t"):
        if delim in line:
            cols = [c.strip().strip('"').lower() for c in next(csv.reader([line], delimiter=delim))]
            if any(c in CSV_COLS["name"] for c in cols):
                return delim, cols
    return None


def parse_csv(text, delim, header):
    cm_export = any(c in header for c in ("idproduct", "idarticle", "exp. name", "exp."))
    col = {}
    for key, names in CSV_COLS.items():
        for n in names:
            if n in header:
                col[key] = header.index(n)
                break
    if "price" not in col and not cm_export:
        for i, h in enumerate(header):
            if h.startswith("price") and "eur" not in h and "€" not in h:
                col["price"] = i
                break
    out = []
    rows = csv.reader(io.StringIO(text), delimiter=delim)
    next(rows, None)
    for n, cells in enumerate(rows, 2):
        get = lambda k: cells[col[k]].strip() if k in col and col[k] < len(cells) else ""  # noqa: E731
        name = get("name")
        if not name:
            continue
        q = re.sub(r"[^\d]", "", get("qty"))
        fv = get("foil").lower()
        finish = None
        if "foil" in col:
            finish = ("etched" if "etched" in fv else
                      "nonfoil" if fv in ("", "0", "no", "false", "n", "normal", "nonfoil", "non-foil", "regular") else
                      "foil")
        price = _price(get("price")) if "€" not in get("price") else None
        out.append({"line": n, "input": ",".join(cells) if delim == "," else delim.join(cells),
                    "qty": max(1, int(q)) if q else 1, "name": name, "set": get("set") or None,
                    "number": get("number") or None, "finish": finish, "price": price})
    return out


def parse_text(text, index):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    first_i = next((i for i, ln in enumerate(lines) if ln.strip()), 0)
    hdr = _csv_header(lines[first_i]) if lines else None
    if hdr:
        out = parse_csv("\n".join(lines[first_i:]), *hdr)
        for spec in out:
            spec["line"] += first_i
        return out
    out, prev = [], ""
    for n, raw in enumerate(lines, 1):
        if prev.strip().lower() == "about" and raw.strip().lower().startswith("name "):  # Arena deck name
            prev = raw
            continue
        prev = raw
        spec = parse_line(raw, index)
        if spec:
            spec.update(line=n, input=raw.strip())
            out.append(spec)
    return out


# ---------------------------------------------------------------- scoring

def _relaxed(params, finishes, offer):
    p = dict(params)
    p.update(min_usd=0.0, max_usd=None, min_profit=-1e9, min_roi=-1e9, max_ratio=1e9, sets=None, rarities=None,
             q="", min_mp_qty=None, max_edhrec=None, max_disagree=None, finishes=list(finishes), sort="profit")
    p.pop("fx", None)
    if offer is not None:
        p.update(storefront="tcgplayer", sell_mode="market")
    return p


def _score(cands, fx, params, finishes, offer):
    if offer is None:
        return mtg_arb.score(cands, fx, _relaxed(params, finishes, offer))
    orig = {(c["id"], c["finish"]): c for c in cands}
    fake = [{**c, "tcg": offer, "mp": None, "ck": None} for c in cands]
    rows = mtg_arb.score(fake, fx, _relaxed(params, finishes, offer))
    for r in rows:
        c = orig[(r["id"], r["finish"])]
        r.update(sold_on="offer", offer_usd=offer,
                 tcgplayer_usd=round(c["tcg"], 2) if c["tcg"] else None,
                 manapool_usd=round(c["mp"], 2) if c["mp"] else None,
                 cardkingdom_buy_usd=round(c["ck"], 2) if c.get("ck") else None, us_disagree=None)
    return rows


def evaluate(spec, index, fx, params, user_finishes, is_ok, is_suspect):
    res = {"line": spec["line"], "input": spec["input"], "qty": spec["qty"], "query": spec["name"],
           "set": spec["set"], "number": spec["number"], "finish": spec["finish"], "offer_usd": spec["price"],
           "notes": [], "status": "not_found", "matched": None, "suggestions": [], "best": None,
           "alternatives": [], "printings": 0, "profitable": False, "total_profit_usd": 0.0}
    name = index.lookup(spec["name"])
    if not name:
        return res
    res.update(status="ok", matched=name)
    cands = index.by_name[name]

    # Which finishes: explicit marker > (set given => nonfoil, Moxfield convention) > user's finish filter
    if spec["finish"]:
        finishes = [spec["finish"]]
    elif spec["set"]:
        finishes = ["nonfoil"]
    else:
        finishes = user_finishes
    rows = _score(cands, fx, params, finishes, spec["price"])
    if not rows and finishes != list(mtg_arb.FINISHES):
        rows = _score(cands, fx, params, list(mtg_arb.FINISHES), spec["price"])
        if rows:
            res["notes"].append(f"no priced {'/'.join(finishes)} printing; showing other finishes")
    if not rows:
        res.update(status="no_price")
        res["notes"].append("no printing with both a Cardmarket and a US price")
        return res
    res["printings"] = len(rows)
    for r in rows:
        r["suspect"] = is_suspect(r)

    chosen = rows
    if spec["set"]:
        code = index.set_code(spec["set"]) or spec["set"].upper()
        in_set = [r for r in rows if r["set"] == code]
        if spec["number"] and in_set:
            num = spec["number"].lstrip("0").lower() or "0"
            exact = [r for r in in_set if (r["number"].lstrip("0").lower() or "0") == num]
            if exact:
                in_set = exact
            else:
                res["notes"].append(f"#{spec['number']} not priced in {code}; best in set shown")
        if in_set:
            chosen = in_set
        else:
            res["notes"].append(f"{spec['set']} printing not priced; best printing shown")
    # Prefer a printing that passes the user's bad-data guards (max price ratio / max disagreement).
    best = next((r for r in chosen if not r["suspect"]), chosen[0])
    if best["suspect"]:
        res["notes"].append("price ratio / US price disagreement exceeds your bad-data limits: check before buying")
    res["best"] = best
    res["alternatives"] = [r for r in rows if r is not best][:5]
    res["profitable"] = is_ok(best)
    res["total_profit_usd"] = round(best["profit_usd"] * spec["qty"], 2)
    return res


def listcheck(state, query, body):
    body = body or {}
    text = body.get("text") or ""
    if not text.strip():
        return {"error": "paste a card list first"}, 400
    try:
        params = normalize_params(body.get("params") or {})
    except (TypeError, ValueError) as e:
        return {"error": f"bad params: {e}"}, 400
    with state.lock:
        fx = params.get("fx") or state.fx
    index = get_index(state)
    p = {**mtg_arb.DEFAULTS, **{k: v for k, v in params.items() if v is not None}}
    user_finishes = [f for f in (p["finishes"] or []) if f in mtg_arb.FINISHES] or list(mtg_arb.FINISHES)
    min_profit = max(p["min_profit"] or 0.0, 0.0)
    min_roi = p["min_roi"] or 0.0
    max_ratio, max_dis = p["max_ratio"], p.get("max_disagree")
    is_suspect = lambda r: bool((max_ratio and r["ratio"] > max_ratio) or  # noqa: E731
                                (max_dis and r.get("us_disagree") and r["us_disagree"] > max_dis))
    is_ok = lambda r: not r["suspect"] and r["profit_usd"] > 0 and r["profit_usd"] >= min_profit and r["roi_pct"] >= min_roi  # noqa: E731

    specs = parse_text(text, index)
    truncated = len(specs) > MAX_LINES
    specs = specs[:MAX_LINES]
    lines, suggested = [], 0
    for spec in specs:
        res = evaluate(spec, index, fx, params, user_finishes, is_ok, is_suspect)
        if res["status"] == "not_found" and suggested < MAX_SUGGEST_LINES:
            res["suggestions"] = index.suggest(spec["name"])
            suggested += 1
        lines.append(res)

    def tot(sel):
        return {
            "lines": len(sel),
            "copies": sum(r["qty"] for r in sel),
            "spend_eur": round(sum(r["best"]["cardmarket_eur"] * r["qty"] for r in sel), 2),
            "cost_usd": round(sum(r["best"]["landed_cost_usd"] * r["qty"] for r in sel), 2),
            "profit_usd": round(sum(r["total_profit_usd"] for r in sel), 2),
        }
    priced = [r for r in lines if r["best"]]
    good = [r for r in priced if r["profitable"]]
    return {
        "fx": fx,
        "thresholds": {"min_profit": min_profit, "min_roi": min_roi, "max_ratio": max_ratio, "max_disagree": max_dis},
        "truncated": truncated,
        "totals": {
            "lines": len(lines),
            "found": sum(r["status"] != "not_found" for r in lines),
            "priced": len(priced),
            "not_found": sum(r["status"] == "not_found" for r in lines),
            "profitable": len(good),
            "all": tot(priced),
            "profitable_only": tot(good),
        },
        "lines": lines,
    }


ROUTES = {("POST", "/api/listcheck"): listcheck}
