#!/usr/bin/env python3
"""
MTG price arbitrage scanner: Cardmarket (EU) -> TCGplayer / Mana Pool (US).

Price sources, all keyed by Scryfall card id so matching is exact
(same set, collector number and finish) -- no fuzzy name matching:
  Scryfall bulk data  prices.eur / eur_foil  -> Cardmarket trend price (EUR)
                      prices.usd / usd_foil  -> TCGplayer market price (USD)
  Mana Pool           /api/v1/prices/singles -> Mana Pool market price (USD)
  Card Kingdom        /api/v2/pricelist      -> Card Kingdom cash buylist offer (USD)
  Cardmarket          daily price guide      -> fills Cardmarket prices Scryfall is missing
  CardConduit         estimated from the above; the real offer is pulled per card on request

Stdlib only. Run `python3 mtg_arb.py --help` for options, or `python3 app.py`
for the web UI.
"""
import argparse
import csv
import datetime
import gzip
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

UA = {"User-Agent": "mtg-arb/0.1 (+https://github.com/Radixtrator/mtg-arbitrage)", "Accept": "application/json"}
CACHE = Path(__file__).resolve().parent / ".cache"
MANAPOOL_URL = "https://manapool.com/api/v1/prices/singles"
CARDKINGDOM_URL = "https://api.cardkingdom.com/api/v2/pricelist"
CARDMARKET_GUIDE_URL = "https://downloads.s3.cardmarket.com/productCatalog/priceGuide/price_guide_1.json"
CARDMARKET_PRODUCTS_URL = "https://downloads.s3.cardmarket.com/productCatalog/productList/products_singles_1.json"

FINISHES = {
    "nonfoil": ("eur", "usd"),
    "foil": ("eur_foil", "usd_foil"),
    "etched": ("eur_etched", "usd_etched"),
}
# Mana Pool price fields to try per finish, best first: market, cheapest NM, cheapest any.
MANAPOOL_FIELDS = {
    "nonfoil": ("price_market", "price_cents_nm", "price_cents"),
    "foil": ("price_market_foil", "price_cents_nm_foil", "price_cents_foil"),
    "etched": ("price_cents_nm_etched", "price_cents_etched"),
}
STORES = {"tcgplayer": "tcg", "manapool": "mp", "cardkingdom": "ck", "cardconduit": "cc"}
# CardConduit service tiers: (fee % of the buy price, fee per card in USD)
CC_SERVICES = {"sorted": (2.0, 0.0), "curated": (5.0, 0.0), "standard": (10.0, 0.03)}
CARDCONDUIT_URL = "https://cardconduit.com/buylist"
# CardConduit pays at least Card Kingdom's buylist price on almost every card (sampled: p10 = 1.00x CK). Where CK
# isn't buying, 45% of the lower US market price undershoots on most cards (median 0.79x) and rarely overshoots.
CC_EST_SHARE = 0.45
CARDCONDUIT_PAGE = "https://cardconduit.com/card-price-check"
LANGS = {"en": "English", "ja": "Japanese"}
# Scryfall promo_types that mean a stamp on the card face, and how the UI names them
STAMPS = {"datestamped": "date", "stamped": "promo"}

# Defaults shared by the CLI and the web UI.
DEFAULTS = {
    "min_usd": 5.0,
    "max_usd": None,
    "min_profit": 2.0,
    "min_roi": 15.0,
    "max_ratio": 8.0,
    "finishes": ["nonfoil", "foil", "etched"],
    "sets": None,
    "rarities": None,
    "langs": ["en", "ja"],  # card languages to include (Scryfall only prices Japanese-only printings separately)
    "stamps": "",  # promo stamps: "" any, "hide" unstamped only, "only" stamped only
    "q": "",
    "min_mp_qty": None,  # liquidity: min copies listed on Mana Pool (all finishes)
    "max_edhrec": None,  # demand: only cards ranked this high or better on EDHREC
    "max_disagree": None,  # drop cards whose TCGplayer and Mana Pool prices differ by more than this factor
    "buy_markup": 5.0,
    "per_card_cost": 0.75,
    "duty": 0.0,
    "storefront": "tcgplayer",  # "tcgplayer", "manapool", "cardkingdom" / "cardconduit" (buylists), or "best"
    "cc_service": "sorted",  # CardConduit service tier, see CC_SERVICES
    "sell_mode": "fees",  # "fees": sell after fees; "market": raw market value; "credit": store credit
    "sell_at": 95.0,
    "tcg_fee": 13.25,
    "tcg_fixed": 0.30,
    "mp_fee": 5.0,
    "mp_fee_cap": 50.0,
    "mp_processing": 2.9,
    "mp_fixed": 0.30,
    "credit_pct": 60.0,  # store credit as % of the US market price
    "ck_credit_bonus": 30.0,  # Card Kingdom pays this much more when you take store credit
    "sort": "profit",
}

# Request params that are lists (comma-separated in URLs) or plain strings; everything else in DEFAULTS is a number.
LIST_PARAMS = {"finishes", "sets", "rarities", "langs"}
STR_PARAMS = {"q", "sort", "sell_mode", "storefront", "stamps", "cc_service"}

SORT_KEYS = {"profit": "profit_usd", "roi": "roi_pct", "ratio": "ratio",
             "sell": "sell_price_usd", "cm": "cardmarket_eur", "name": "name"}


def http_json(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def download(url, path):
    print(f"Downloading {url} ...", file=sys.stderr)
    tmp = path.with_suffix(".part")
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)
    tmp.rename(path)


def get_bulk_file(refresh=False):
    """Download Scryfall's default_cards JSONL (once per Scryfall update)."""
    CACHE.mkdir(exist_ok=True)
    meta = http_json("https://api.scryfall.com/bulk-data")
    entry = next(d for d in meta["data"] if d["type"] == "default_cards")
    url = entry.get("jsonl_download_uri") or entry["download_uri"]
    path = CACHE / url.rsplit("/", 1)[-1]
    if path.exists() and not refresh:
        return path
    for old in CACHE.glob("default-cards-*"):
        old.unlink()
    download(url, path)
    return path


def get_manapool_prices(refresh=False):
    """Mana Pool singles prices (cached per UTC day) as {scryfall_id: {finish: usd, "url": ...}}."""
    CACHE.mkdir(exist_ok=True)
    path = CACHE / f"manapool-{datetime.datetime.now(datetime.UTC):%Y%m%d}.json"
    if not path.exists() or refresh:
        for old in CACHE.glob("manapool-*"):
            old.unlink()
        download(MANAPOOL_URL, path)
    out = {}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    for c in data["data"]:
        if not c.get("scryfall_id"):
            continue
        entry = {"url": c.get("url", ""), "qty": c.get("available_quantity") or 0}
        for fin, fields in MANAPOOL_FIELDS.items():
            cents = next((c[k] for k in fields if c.get(k)), None)
            entry[fin] = cents / 100 if cents else None
        out[c["scryfall_id"]] = entry
    return out, data.get("meta", {}).get("as_of", "")


def get_cardkingdom_buylist(refresh=False):
    """Card Kingdom cash buylist (cached per UTC day) as {scryfall_id: [offer, ...]}, each offer
    {"finish", "price", "qty", "url", "numbers"}. Pick one per printing+finish with ck_offer().

    Only entries CK is currently buying (qty_buying > 0) are kept."""
    CACHE.mkdir(exist_ok=True)
    path = CACHE / f"cardkingdom-{datetime.datetime.now(datetime.UTC):%Y%m%d}.json"
    if not path.exists() or refresh:
        for old in CACHE.glob("cardkingdom-*"):
            old.unlink()
        download(CARDKINGDOM_URL, path)
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    base = data.get("meta", {}).get("base_url", "https://www.cardkingdom.com/")
    out = {}
    for c in data["data"]:
        price = float(c.get("price_buy") or 0)
        if not c.get("scryfall_id") or price <= 0 or not c.get("qty_buying"):
            continue
        if c.get("is_foil") == "true":
            fin = "etched" if re.search(r"\betched\b", c.get("variation", ""), re.I) else "foil"
        else:
            fin = "nonfoil"
        out.setdefault(c["scryfall_id"], []).append({
            "finish": fin, "price": price, "qty": c.get("qty_buying", 0), "url": base + c.get("url", ""),
            "numbers": ck_numbers(c.get("variation") or ""),
        })
    return out, data.get("meta", {}).get("created_at", "")


def ck_numbers(variation):
    """Collector numbers CK writes at the start of a variation: "0096 - Raised Foil", "1928 - Squidward ...",
    "003 // 002". Bare years like "2018 Lunar New Year" don't count."""
    m = re.match(r"\s*(\d{1,4}(?:\s*//\s*\d{1,4})*)\s*(?:-|$)", variation)
    return {int(n) for n in re.findall(r"\d+", m.group(1))} if m else set()


def ck_offer(offers, fin, number):
    """CK's offer for one printing+finish, or None.

    CK sometimes tags a variant (a raised foil, a Secret Lair reprint) with the regular printing's Scryfall id;
    drop offers whose own collector number disagrees. If several remain, take the lowest, to stay conservative."""
    n = re.match(r"\d+", number or "")
    n = int(n.group()) if n else None
    ok = [o for o in offers if o["finish"] == fin and not (o["numbers"] and n is not None and n not in o["numbers"])]
    return min(ok, key=lambda o: o["price"]) if ok else None


def get_cardmarket_guide(refresh=False):
    """Cardmarket's own daily downloads (cached per UTC day) as {idProduct: {finish: trend EUR, "exp": idExpansion}}.

    The price guide fills Cardmarket prices Scryfall lacks, e.g. Secret Lair "★" foils that Scryfall lists as
    their own printing but Cardmarket sells as the foil of the regular product. The product list gives each
    product's expansion, which the Cardmarket links need (see cardmarket_link)."""
    CACHE.mkdir(exist_ok=True)
    day = f"{datetime.datetime.now(datetime.UTC):%Y%m%d}"
    files = {}
    for kind, url in (("cmguide", CARDMARKET_GUIDE_URL), ("cmproducts", CARDMARKET_PRODUCTS_URL)):
        path = files[kind] = CACHE / f"{kind}-{day}.json"
        if not path.exists() or refresh:
            for old in CACHE.glob(f"{kind}-*"):
                old.unlink()
            download(url, path)
    out = {}
    with open(files["cmproducts"], encoding="utf-8") as f:
        for p in json.load(f).get("products", []):
            out[p["idProduct"]] = {"exp": p.get("idExpansion")}
    with open(files["cmguide"], encoding="utf-8") as f:
        data = json.load(f)
    for g in data.get("priceGuides", []):
        out.setdefault(g["idProduct"], {}).update(nonfoil=g.get("trend") or None, foil=g.get("trend-foil") or None)
    return out, data.get("createdAt", "")


def iter_cards(path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as f:
        if path.name.endswith(".jsonl") or path.name.endswith(".jsonl.gz"):
            for line in f:
                if line.strip():
                    yield json.loads(line)
        else:
            yield from json.load(f)


def get_fx():
    try:
        data = http_json("https://api.frankfurter.dev/v1/latest?from=EUR&to=USD")
        return float(data["rates"]["USD"])
    except Exception as e:  # noqa: BLE001
        sys.exit(f"Could not fetch EUR->USD rate ({e}); pass --fx manually.")


def card_images(c):
    imgs = c.get("image_uris") or (c.get("card_faces") or [{}])[0].get("image_uris") or {}
    return imgs.get("small", ""), imgs.get("normal", "")


def load_candidates(path, manapool=None, cardkingdom=None, cmguide=None):
    """Every English paper printing+finish with a EUR price and at least one US price.

    EUR comes from Scryfall, else from Cardmarket's price guide via the printing's cardmarket_id.
    Printings Scryfall doesn't link to Cardmarket at all, like Secret Lair "★" foils (#1596★), get the
    guide's foil price of their same-numbered sibling (#1596) when that sibling has no foil of its own:
    Cardmarket sells them as one product."""
    manapool = manapool or {}
    cardkingdom = cardkingdom or {}
    cmguide = cmguide or {}
    out, orphans, products = [], [], {}

    def guide_eur(pid, fin):
        return (cmguide.get(pid) or {}).get("foil" if fin == "etched" else fin) or 0

    def add(c, fin, eur, pid, cm_note=""):
        uk = FINISHES[fin][1]
        tcg = float((c.get("prices") or {}).get(uk) or 0) or None
        mp = manapool.get(c["id"], {})
        ck = ck_offer(cardkingdom.get(c["id"], []), fin, c.get("collector_number")) or {}
        mpp = mp.get(fin)
        ckp = ck.get("price")
        if eur <= 0 or not (tcg or mpp or ckp):
            return
        uris = c.get("purchase_uris") or {}
        small, normal = card_images(c)
        out.append({
            "id": c["id"],
            "name": c["name"],
            "set": c["set"].upper(),
            "set_name": c.get("set_name", ""),
            "number": c.get("collector_number", ""),
            "finish": fin,
            "lang": c.get("lang", "en"),
            "stamp": next((STAMPS[t] for t in STAMPS if t in (c.get("promo_types") or [])), ""),
            "rarity": c.get("rarity", ""),
            "released_at": c.get("released_at", ""),
            "eur": eur,
            "tcg": tcg,
            # TCGplayer market for every finish of this printing, so the UI can warn about foil/non-foil mix-ups
            "tcg_finishes": {f: float(v) for f, (_, k) in FINISHES.items() if (v := (c.get("prices") or {}).get(k))},
            "mp": mpp,
            "mp_qty": mp.get("qty", 0),
            "ck": ckp,
            "ck_qty": ck.get("qty", 0),
            "cc": cc_estimate(ckp, tcg, mpp),  # CardConduit offer: estimated until pulled (features/cardconduit.py)
            "cc_est": True,
            "edhrec_rank": c.get("edhrec_rank"),
            "reserved": bool(c.get("reserved")),
            "image_small": small,
            "image_normal": normal,
            "cardmarket_url": cardmarket_link(c["name"], (cmguide.get(pid) or {}).get("exp")),
            "cm_note": cm_note,
            "tcgplayer_id": tcgplayer_id(c, fin),
            "tcgplayer_url": tcgplayer_link(tcgplayer_id(c, fin), fin, c.get("lang", "en")) or uris.get("tcgplayer", ""),
            "manapool_url": mp.get("url", ""),
            "cardkingdom_url": ck.get("url", ""),
            "cardconduit_url": CARDCONDUIT_PAGE,
            "scryfall_url": c.get("scryfall_uri", ""),
        })

    for c in iter_cards(path):
        if c.get("digital") or c.get("lang", "en") not in LANGS:
            continue
        prices = c.get("prices") or {}
        finishes = set(c.get("finishes") or [])
        number = c.get("collector_number", "")
        pid = c.get("cardmarket_id")
        if pid:
            products[(c["set"], number)] = (pid, finishes)
        for fin, (ek, _) in FINISHES.items():
            eur = float(prices.get(ek) or 0)
            if eur > 0:
                add(c, fin, eur, pid)
            elif fin in finishes and pid and guide_eur(pid, fin):
                add(c, fin, guide_eur(pid, fin), pid, "Cardmarket price guide")
            elif fin in finishes and not pid and fin != "nonfoil" and re.search(r"[^0-9A-Za-z]$", number):
                orphans.append((c, fin))
    for c, fin in orphans:
        base = re.sub(r"[^0-9A-Za-z]+$", "", c.get("collector_number", ""))
        sib = products.get((c["set"], base))
        if sib and not sib[1] & {"foil", "etched"}:
            add(c, fin, guide_eur(sib[0], fin), sib[0], f"Cardmarket sells this as the foil of #{base}")
    return out


def tcgplayer_id(c, fin):
    """TCGplayer product id for this finish (etched printings are separate products)."""
    return c.get("tcgplayer_etched_id") if fin == "etched" else c.get("tcgplayer_id")


def tcgplayer_link(pid, fin, lang="en"):
    """Product page pre-filtered to this finish and language, so foil and non-foil listings aren't mixed."""
    if not pid:
        return ""
    printing = {"nonfoil": "&Printing=Normal", "foil": "&Printing=Foil"}.get(fin, "")
    return f"https://www.tcgplayer.com/product/{pid}?Language={LANGS.get(lang, 'English')}{printing}"


def cardmarket_link(name, exp=None):
    """Cardmarket search for this card name, limited to its expansion when known.

    Not the product page: Cloudflare hard-blocks Cardmarket's Products?idProduct= URLs (the ones Scryfall
    hands out), while search pages only get the normal human check."""
    front = name.split(" // ")[0]
    q = {"searchString": front}
    if exp:
        q["idExpansion"] = exp
    if front == name:
        q["exactMatch"] = "on"
    return "https://www.cardmarket.com/en/Magic/Products/Search?" + urllib.parse.urlencode(q)


def cc_estimate(ck, tcg, mp):
    """Estimated CardConduit buylist offer (USD): Card Kingdom's offer, else a share of the lower US market price."""
    if ck:
        return ck
    market = min([p for p in (tcg, mp) if p] or [0])
    return round(market * CC_EST_SHARE, 2) if market else None


def fetch_cardconduit(name, set_code, scryfall_id):
    """CardConduit's current NM buylist offer for one printing, as {finish: usd}; finishes it isn't buying are absent.

    Uses the same search the site's price-check page uses: card name + set, 50 rows a page."""
    out = {}
    for page in range(1, 6):
        q = {"cardname": name.split(" // ")[0], "set_code": set_code.upper(), "price_gte": "", "price_lte": "",
             "sort": "name-asc", "finish": "", "page": page}
        req = urllib.request.Request(f"{CARDCONDUIT_URL}?{urllib.parse.urlencode(q)}", headers={**UA, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.load(r)
        for d in data.get("data", []):
            if (d.get("card") or {}).get("scryfall_uuid") == scryfall_id and d.get("condition", "nm") == "nm":
                fin = "etched" if d.get("is_etched") else "foil" if d.get("is_foil") else "nonfoil"
                out[fin] = float(d["amount"])
        if page >= data.get("meta", {}).get("last_page", 1):
            break
    return out


def net_sale(price, store, p):
    """What you actually end up with (USD) from one copy at `price` on `store`."""
    if store == "cardkingdom":  # `price` is already CK's cash buylist offer; no fees
        return price * (1 + p["ck_credit_bonus"] / 100) if p["sell_mode"] == "credit" else price
    if store == "cardconduit":  # cash buylist offer minus the service tier's fee; no store-credit option
        if p["sell_mode"] == "market":
            return price
        pct, per_card = CC_SERVICES.get(p["cc_service"], CC_SERVICES["sorted"])
        return price * (1 - pct / 100) - per_card
    if p["sell_mode"] == "market":
        return price
    if p["sell_mode"] == "credit":
        return price * p["credit_pct"] / 100
    sale = price * p["sell_at"] / 100
    if store == "manapool":
        fee = min(sale * p["mp_fee"] / 100, p["mp_fee_cap"])
        return sale - fee - sale * p["mp_processing"] / 100 - p["mp_fixed"]
    return sale * (1 - p["tcg_fee"] / 100) - p["tcg_fixed"]


def landed_cost(eur, fx, p):
    return eur * fx * (1 + p["buy_markup"] / 100) * (1 + p["duty"] / 100) + p["per_card_cost"]


def score(cands, fx, params):
    """Apply the cost model and filters; return rows sorted best-first."""
    p = {**DEFAULTS, **{k: v for k, v in params.items() if v is not None}}
    finishes = set(p["finishes"])
    sets = {s.upper() for s in p["sets"]} if p["sets"] else None
    rarities = set(p["rarities"]) if p["rarities"] else None
    langs = set(p["langs"] or LANGS)
    q = p["q"].lower().strip()
    stores = list(STORES) if p["storefront"] == "best" else [p["storefront"]]

    rows = []
    for c in cands:
        if c["finish"] not in finishes:
            continue
        if sets and c["set"] not in sets:
            continue
        if rarities and c["rarity"] not in rarities:
            continue
        if c["lang"] not in langs:
            continue
        if (p["stamps"] == "hide" and c["stamp"]) or (p["stamps"] == "only" and not c["stamp"]):
            continue
        if q and q not in c["name"].lower():
            continue
        if p["min_mp_qty"] and c["mp_qty"] < p["min_mp_qty"]:
            continue
        if p["max_edhrec"] and not (c["edhrec_rank"] and c["edhrec_rank"] <= p["max_edhrec"]):
            continue
        # Pick the storefront that nets the most (only one when a store is fixed).
        best = None
        for store in stores:
            price = c[STORES[store]]
            if price:
                net = net_sale(price, store, p)
                if best is None or net > best[2]:
                    best = (store, price, net)
        if best is None:
            continue
        store, price, net = best
        disagree = max(c["tcg"], c["mp"]) / min(c["tcg"], c["mp"]) if c["tcg"] and c["mp"] else None
        if p["max_disagree"] and disagree and disagree > p["max_disagree"]:
            continue
        if price < p["min_usd"] or (p["max_usd"] and price > p["max_usd"]):
            continue
        eur = c["eur"]
        ratio = price / (eur * fx)
        if ratio > p["max_ratio"]:  # almost always bad data / mismatched listing
            continue
        landed = landed_cost(eur, fx, p)
        profit = net - landed
        roi = profit / landed if landed else 0.0
        if profit < p["min_profit"] or roi * 100 < p["min_roi"]:
            continue
        rows.append({
            **{k: v for k, v in c.items() if k not in ("eur", "tcg", "mp", "ck", "cc", "cc_est")},
            "cardmarket_eur": round(eur, 2),
            "cardmarket_usd": round(eur * fx, 2),
            "tcgplayer_usd": round(c["tcg"], 2) if c["tcg"] else None,
            "manapool_usd": round(c["mp"], 2) if c["mp"] else None,
            "cardkingdom_buy_usd": round(c["ck"], 2) if c["ck"] else None,
            "cardconduit_buy_usd": round(c["cc"], 2) if c["cc"] else None,
            "cardconduit_estimated": c["cc_est"],
            "sold_on": store,
            "us_disagree": round(disagree, 2) if disagree else None,
            "sell_price_usd": round(price, 2),
            "ratio": round(ratio, 2),
            "landed_cost_usd": round(landed, 2),
            "net_sale_usd": round(net, 2),
            "profit_usd": round(profit, 2),
            "roi_pct": round(roi * 100, 1),
        })

    key = SORT_KEYS.get(p["sort"], "profit_usd")
    rows.sort(key=lambda r: r[key], reverse=key != "name")
    return rows


def print_table(rows, fx, p, top):
    sell = {"market": "raw market value, no fees",
            "credit": f"store credit at {p['credit_pct']}% of market",
            "fees": f"after fees, sell at {p['sell_at']}% of market"}[p["sell_mode"]]
    print(f"\nEUR->USD {fx:.4f}  duty {p['duty']}%  per-card cost ${p['per_card_cost']}  "
          f"store: {p['storefront']}  ({sell})")
    print(f"{len(rows)} opportunities found; showing top {min(top, len(rows))} by {p['sort']}\n")
    hdr = (f"{'#':>3}  {'Card':<36} {'Set':<6} {'Fin':<7} {'CM €':>8} {'TCG $':>8} {'MP $':>8} {'CK buy':>8} {'CC est':>8} "
           f"{'Cost $':>8} {'Profit $':>9} {'ROI':>6}  Via")
    print(hdr)
    print("-" * len(hdr))
    money = lambda v: f"{v:>8.2f}" if v else f"{'-':>8}"  # noqa: E731
    for i, r in enumerate(rows[:top], 1):
        tag = (" [JP]" if r["lang"] == "ja" else "") + (" [stamp]" if r["stamp"] else "")
        name = r["name"] if len(r["name"]) + len(tag) <= 36 else r["name"][:33 - len(tag)] + "..."
        name += tag
        print(f"{i:>3}  {name:<36} {r['set']:<6} {r['finish']:<7} "
              f"{r['cardmarket_eur']:>8.2f} {money(r['tcgplayer_usd'])} {money(r['manapool_usd'])} {money(r['cardkingdom_buy_usd'])} {money(r['cardconduit_buy_usd'])} "
              f"{r['landed_cost_usd']:>8.2f} {r['profit_usd']:>9.2f} {r['roi_pct']:>5.0f}%  {r['sold_on']}")


def main():
    d = DEFAULTS
    p = argparse.ArgumentParser(
        description="Find MTG cards cheaper on Cardmarket (EU) than on TCGplayer / Mana Pool (US).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    g = p.add_argument_group("filters")
    g.add_argument("--min-usd", type=float, default=d["min_usd"], help="ignore cards whose US price is below this")
    g.add_argument("--max-usd", type=float, help="ignore cards whose US price is above this")
    g.add_argument("--min-profit", type=float, default=d["min_profit"], help="minimum net profit per copy (USD)")
    g.add_argument("--min-roi", type=float, default=d["min_roi"], help="minimum ROI per copy (%%)")
    g.add_argument("--max-ratio", type=float, default=d["max_ratio"], help="drop rows where US/CM price ratio exceeds this (bad data)")
    g.add_argument("--finish", choices=["nonfoil", "foil", "etched", "all"], default="all")
    g.add_argument("--set", nargs="+", help="only these set codes, e.g. --set mh3 otj")
    g.add_argument("--rarity", nargs="+", choices=["common", "uncommon", "rare", "mythic", "special", "bonus"])
    g.add_argument("--name", default="", help="card name contains this text")
    g.add_argument("--lang", nargs="+", choices=list(LANGS), default=d["langs"], help="card languages (default: en ja)")
    g.add_argument("--stamps", choices=["", "hide", "only"], default=d["stamps"],
                   help="promo-stamped printings (prerelease date stamp, promo pack / event stamp): hide them, or only show them")
    g.add_argument("--min-mp-qty", type=int, help="liquidity: min copies listed on Mana Pool")
    g.add_argument("--max-edhrec", type=int, help="demand: only cards with EDHREC rank <= this")
    g.add_argument("--max-disagree", type=float, help="drop cards whose TCGplayer and Mana Pool prices differ by more than this factor, e.g. 1.5")
    c = p.add_argument_group("cost model")
    c.add_argument("--fx", type=float, help="EUR->USD rate (default: live ECB rate via frankfurter.dev)")
    c.add_argument("--buy-markup", type=float, default=d["buy_markup"],
                   help="%% paid above Cardmarket trend (trend is an average; cheapest NM copies may differ)")
    c.add_argument("--per-card-cost", type=float, default=d["per_card_cost"],
                   help="flat USD per card: amortized Cardmarket shipping, sleeves, your shipping to buyer")
    c.add_argument("--duty", type=float, default=d["duty"], help="import duty on the purchase price (%%)")
    s = p.add_argument_group("selling")
    s.add_argument("--store", choices=[*STORES, "best"], default=d["storefront"],
                   help="where you sell (cardkingdom = their buylist); 'best' picks whichever nets more per card")
    s.add_argument("--mode", choices=["fees", "market", "credit"], default=d["sell_mode"],
                   help="fees: sell after storefront fees; market: raw market value; credit: store credit")
    s.add_argument("--sell-at", type=float, default=d["sell_at"], help="sell price as %% of market (fees mode)")
    s.add_argument("--tcg-fee", type=float, default=d["tcg_fee"], help="TCGplayer commission + processing (%%)")
    s.add_argument("--tcg-fixed", type=float, default=d["tcg_fixed"], help="TCGplayer fixed fee per sale (USD)")
    s.add_argument("--mp-fee", type=float, default=d["mp_fee"], help="Mana Pool marketplace fee (%%)")
    s.add_argument("--mp-fee-cap", type=float, default=d["mp_fee_cap"], help="Mana Pool marketplace fee cap per item (USD)")
    s.add_argument("--mp-processing", type=float, default=d["mp_processing"], help="Mana Pool card processing (%%)")
    s.add_argument("--mp-fixed", type=float, default=d["mp_fixed"], help="Mana Pool fixed fee per sale (USD)")
    s.add_argument("--credit-pct", type=float, default=d["credit_pct"],
                   help="store credit offered as %% of US market (credit mode)")
    s.add_argument("--ck-credit-bonus", type=float, default=d["ck_credit_bonus"],
                   help="extra %% Card Kingdom pays in store credit vs cash (credit mode)")
    s.add_argument("--cc-service", choices=list(CC_SERVICES), default=d["cc_service"],
                   help="CardConduit service tier (fee): sorted 2%%, curated 5%%, standard 10%% + $0.03")
    o = p.add_argument_group("output")
    o.add_argument("--sort", choices=list(SORT_KEYS), default=d["sort"])
    o.add_argument("--top", type=int, default=50, help="rows to print")
    o.add_argument("--csv", help="write all results to this CSV file")
    o.add_argument("--file", help="use a local Scryfall bulk file instead of downloading")
    o.add_argument("--refresh", action="store_true", help="force re-download of price data")
    a = p.parse_args()

    fx = a.fx or get_fx()
    path = Path(a.file) if a.file else get_bulk_file(a.refresh)
    manapool, _ = get_manapool_prices(a.refresh)
    cardkingdom, _ = get_cardkingdom_buylist(a.refresh)
    cmguide, _ = get_cardmarket_guide(a.refresh)
    params = {
        "min_usd": a.min_usd, "max_usd": a.max_usd, "min_profit": a.min_profit,
        "min_roi": a.min_roi, "max_ratio": a.max_ratio,
        "finishes": list(FINISHES) if a.finish == "all" else [a.finish],
        "sets": a.set, "rarities": a.rarity, "q": a.name, "langs": a.lang, "stamps": a.stamps,
        "min_mp_qty": a.min_mp_qty, "max_edhrec": a.max_edhrec, "max_disagree": a.max_disagree,
        "buy_markup": a.buy_markup, "per_card_cost": a.per_card_cost, "duty": a.duty,
        "storefront": a.store, "sell_mode": a.mode, "sell_at": a.sell_at,
        "tcg_fee": a.tcg_fee, "tcg_fixed": a.tcg_fixed,
        "mp_fee": a.mp_fee, "mp_fee_cap": a.mp_fee_cap, "mp_processing": a.mp_processing, "mp_fixed": a.mp_fixed,
        "credit_pct": a.credit_pct, "ck_credit_bonus": a.ck_credit_bonus, "cc_service": a.cc_service, "sort": a.sort,
    }
    rows = score(load_candidates(path, manapool, cardkingdom, cmguide), fx, params)
    print_table(rows, fx, {**DEFAULTS, **params}, a.top)
    if a.csv and rows:
        with open(a.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        print(f"\nWrote {len(rows)} rows to {os.path.abspath(a.csv)}")


if __name__ == "__main__":
    main()
