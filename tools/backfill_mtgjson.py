#!/usr/bin/env python3
"""
Backfill the price-history database (data/history.sqlite) from MTGJSON.

MTGJSON publishes ~90 days of daily retail prices per printing:
  AllPrices.json.xz       (~48 MB compressed) keyed by MTGJSON uuid:
                          paper.cardmarket.retail.{normal,foil,etched}  EUR
                          paper.tcgplayer.retail.{...}                  USD
                          paper.cardkingdom.buylist.{...}               USD (cash buylist)
                          (paper.manapool.retail is skipped: it runs ~17% below the Mana Pool
                          market price the app uses, which would fake a price jump)
  csv/cardIdentifiers.csv.gz (~16 MB) maps MTGJSON uuid -> scryfallId
The JSON is never loaded whole: it is decompressed as a stream and each card's
object is decoded on its own, so memory stays around a few hundred MB.
Historical EUR->USD rates come from frankfurter.app (ECB; weekends carry forward).

Rows are written with INSERT OR IGNORE, so real daily snapshots taken by the app
(which also include Mana Pool prices) always win over backfilled ones.
Backfilled rows have mp = mp_qty = NULL.

Storage is compact because 90 days x ~145k printings is ~12M rows: a card key
integer, day number and integer cents (~25 bytes/row instead of ~110). The `prices`
view gives the readable shape: prices(date, id, finish, eur, tcg, mp, mp_qty, ck).

Stdlib only.  Usage:  python3 tools/backfill_mtgjson.py [--days 90] [--all]
"""
import argparse
import csv
import datetime
import gzip
import json
import lzma
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / ".cache" / "mtgjson"
DB = ROOT / "data" / "history.sqlite"
BASE = "https://mtgjson.com/api/v5/"
FILES = {"prices": "AllPrices.json.xz", "ids": "csv/cardIdentifiers.csv.gz"}
FINISH = {"normal": "nonfoil", "foil": "foil", "etched": "etched"}
UA = {"User-Agent": "mtg-arb/0.1"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
  k INTEGER PRIMARY KEY, id TEXT NOT NULL, finish TEXT NOT NULL, UNIQUE (id, finish)
);
-- d: days since 1970-01-01 (UTC); prices in integer cents. PK (k, d) is the (id, finish) index.
CREATE TABLE IF NOT EXISTS price_rows (
  k INTEGER NOT NULL, d INTEGER NOT NULL,
  eur INTEGER, tcg INTEGER, mp INTEGER, mp_qty INTEGER, ck INTEGER,
  PRIMARY KEY (k, d)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS days (
  date TEXT PRIMARY KEY, fx REAL, source TEXT, rows INTEGER, taken_at TEXT
);
CREATE VIEW IF NOT EXISTS prices AS
  SELECT date(p.d * 86400, 'unixepoch') AS date, c.id, c.finish,
         p.eur / 100.0 AS eur, p.tcg / 100.0 AS tcg, p.mp / 100.0 AS mp, p.mp_qty, p.ck / 100.0 AS ck
  FROM price_rows p JOIN cards c USING (k);
"""
EPOCH = datetime.date(1970, 1, 1).toordinal()


def day_num(iso):
    return datetime.date.fromisoformat(iso).toordinal() - EPOCH


def day_iso(n):
    return datetime.date.fromordinal(n + EPOCH).isoformat()


def cents(v):
    return None if v is None else round(v * 100)


def card_keys(db, pairs):
    """{(id, finish): k}, creating keys for pairs not seen before."""
    keys = {(i, f): k for k, i, f in db.execute("SELECT k, id, finish FROM cards")}
    new = sorted({p for p in pairs if p not in keys})
    if new:
        with db:
            db.executemany("INSERT OR IGNORE INTO cards (id, finish) VALUES (?, ?)", new)
        keys = {(i, f): k for k, i, f in db.execute("SELECT k, id, finish FROM cards")}
    return keys


def connect(path=DB):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=60, check_same_thread=False)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("PRAGMA cache_size=-200000")  # ~200 MB page cache for big inserts
    db.executescript(SCHEMA)
    return db


def _fetch(url, path, log):
    log(f"Downloading {url}")
    tmp = path.with_suffix(path.suffix + ".part")
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=300) as r, open(tmp, "wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)
    tmp.rename(path)


def fetch_files(log, refresh=False):
    """Download the MTGJSON files into .cache/mtgjson/ (re-used for 20 hours)."""
    CACHE.mkdir(parents=True, exist_ok=True)
    out = {}
    for key, name in FILES.items():
        path = CACHE / name.rsplit("/", 1)[-1]
        if refresh or not path.exists() or time.time() - path.stat().st_mtime > 20 * 3600:
            _fetch(BASE + name, path, log)
        out[key] = path
    return out


def load_id_map(path):
    """MTGJSON uuid -> Scryfall id."""
    out = {}
    with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if row.get("scryfallId"):
                out[row["uuid"]] = row["scryfallId"]
    return out


def fx_history(start, end, fallback):
    """{date: EUR->USD} for every calendar day in [start, end], carrying the last ECB rate forward."""
    rates = {}
    try:
        url = f"https://api.frankfurter.app/{start}..{end}?from=EUR&to=USD"
        req = urllib.request.Request(url, headers={**UA, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            rates = {d: v["USD"] for d, v in json.load(r)["rates"].items()}
    except Exception as e:  # noqa: BLE001
        print(f"FX history unavailable ({e}); using {fallback}", file=sys.stderr)
    out, last = {}, None
    d, stop = datetime.date.fromisoformat(start), datetime.date.fromisoformat(end)
    first = rates[min(rates)] if rates else fallback
    while d <= stop:
        k = d.isoformat()
        last = rates.get(k, last) or first
        out[k] = last
        d += datetime.timedelta(days=1)
    return out


def iter_price_objects(path, chunk=1 << 22):
    """Yield (uuid, obj) from AllPrices.json.xz's "data" object without loading the whole file."""
    dec = json.JSONDecoder()
    raw = dec.raw_decode
    with lzma.open(path, "rt", encoding="utf-8") as f:
        buf, pos, eof = "", 0, False

        def more():
            nonlocal buf, pos, eof
            data = f.read(chunk)
            if not data:
                eof = True
            buf = buf[pos:] + data
            pos = 0

        while '"data"' not in buf:
            more()
            if eof:
                raise ValueError("no data object in AllPrices")
        pos = buf.index('"data"') + 6
        while True:  # skip to the opening brace of "data"
            i = buf.find("{", pos)
            if i >= 0:
                pos = i + 1
                break
            more()
        while True:
            # skip whitespace and commas between entries
            while True:
                while pos < len(buf) and buf[pos] in " \t\r\n,":
                    pos += 1
                if pos < len(buf) or eof:
                    break
                more()
            if pos >= len(buf) or buf[pos] == "}":
                return
            try:
                key, p = raw(buf, pos)
                while True:
                    while p < len(buf) and buf[p] in " \t\r\n":
                        p += 1
                    if p < len(buf):
                        break
                    raise ValueError("need more")
                if buf[p] != ":":
                    raise ValueError(f"expected ':' at {p}")
                p += 1
                while p < len(buf) and buf[p] in " \t\r\n":
                    p += 1
                obj, p = raw(buf, p)
            except ValueError:
                if eof:
                    raise
                more()
                continue
            pos = p
            yield key, obj


def backfill(days=90, wanted=None, fx_fallback=None, db_path=DB, progress=None, refresh=False):
    """Fill `prices` with MTGJSON retail history. `wanted`: optional set of (scryfall_id, finish)
    to keep (the app passes its candidates so the DB only holds cards it can show).
    `progress(stage, done, total, msg)` is called periodically. Returns a summary dict."""
    t0 = time.time()
    log = (lambda m: progress("download", 0, 0, m)) if progress else (lambda m: print(m, file=sys.stderr))
    files = fetch_files(log, refresh)
    if progress:
        progress("ids", 0, 0, "Reading MTGJSON → Scryfall id map")
    ids = load_id_map(files["ids"])
    wanted_ids = {k[0] for k in wanted} if wanted else None
    cutoff = (datetime.datetime.now(datetime.UTC).date() - datetime.timedelta(days=days)).isoformat()

    db = connect(db_path)
    keys = card_keys(db, wanted) if wanted else {}
    seen_dates = {}
    dnum = {}
    rows_out = 0
    batch = []
    sql = "INSERT OR IGNORE INTO price_rows (k, d, eur, tcg, mp, mp_qty, ck) VALUES (?,?,?,?,NULL,NULL,?)"

    def flush():
        nonlocal batch, rows_out
        if batch:
            batch.sort()
            with db:
                db.executemany(sql, batch)
            rows_out += len(batch)
            batch = []

    n = 0
    total = len(ids)  # rough: AllPrices has about one entry per uuid
    done_sids = set()
    for uuid, obj in iter_price_objects(files["prices"]):
        n += 1
        if progress and n % 2000 == 0:
            progress("parse", n, total, f"Parsed {n:,} printings, {rows_out + len(batch):,} rows")
        sid = ids.get(uuid)
        if not sid or (wanted_ids is not None and sid not in wanted_ids) or sid in done_sids:
            continue  # done_sids: double-faced cards have one uuid per face, same prices
        paper = obj.get("paper") or {}
        cm = ((paper.get("cardmarket") or {}).get("retail")) or {}
        tcg = ((paper.get("tcgplayer") or {}).get("retail")) or {}
        ckb = ((paper.get("cardkingdom") or {}).get("buylist")) or {}
        if not cm:
            continue
        done_sids.add(sid)
        for mk, fin in FINISH.items():
            ce = cm.get(mk)
            if not ce or (wanted is not None and (sid, fin) not in wanted):
                continue
            k = keys.get((sid, fin))
            if k is None:  # only when wanted is None (--all)
                db.execute("INSERT OR IGNORE INTO cards (id, finish) VALUES (?, ?)", (sid, fin))
                k = keys[(sid, fin)] = db.execute("SELECT k FROM cards WHERE id = ? AND finish = ?",
                                                  (sid, fin)).fetchone()[0]
            tu, ku = tcg.get(mk) or {}, ckb.get(mk) or {}
            for d, e in ce.items():
                if d < cutoff or not e:
                    continue
                if d not in dnum:
                    dnum[d] = day_num(d)
                batch.append((k, dnum[d], cents(e), cents(tu.get(d)), cents(ku.get(d))))
                seen_dates[d] = seen_dates.get(d, 0) + 1
        if len(batch) > 200_000:
            flush()
    flush()

    if seen_dates:
        fx = fx_history(min(seen_dates), max(seen_dates), fx_fallback or 1.1)
        now = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
        with db:
            db.executemany("INSERT OR IGNORE INTO days (date, fx, source, rows, taken_at) VALUES (?,?,?,?,?)",
                           [(d, fx[d], "mtgjson", c, now) for d, c in seen_dates.items()])
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db.close()
    res = {"rows": rows_out, "dates": len(seen_dates), "first": min(seen_dates, default=None),
           "last": max(seen_dates, default=None), "printings": n, "seconds": round(time.time() - t0, 1)}
    if progress:
        progress("done", n, n, f"Backfilled {rows_out:,} rows over {len(seen_dates)} days")
    return res


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=90, help="how far back to fill")
    ap.add_argument("--all", action="store_true",
                    help="keep every printing (default: only cards already in the DB's latest snapshot)")
    ap.add_argument("--refresh", action="store_true", help="re-download the MTGJSON files")
    ap.add_argument("--db", type=Path, default=DB)
    a = ap.parse_args()
    wanted = None
    if not a.all and a.db.exists():
        db = connect(a.db)
        last = db.execute("SELECT max(date) FROM days WHERE source = 'snapshot'").fetchone()[0]
        if last:
            wanted = set(db.execute("SELECT c.id, c.finish FROM price_rows p JOIN cards c USING (k) "
                                    "WHERE p.d = ?", (day_num(last),)))
            print(f"Limiting to {len(wanted):,} printings from the {last} snapshot", file=sys.stderr)
        db.close()

    def prog(stage, done, total, msg):
        print(f"[{stage}] {msg}", file=sys.stderr)

    print(json.dumps(backfill(a.days, wanted, db_path=a.db, progress=prog, refresh=a.refresh)))


if __name__ == "__main__":
    main()
