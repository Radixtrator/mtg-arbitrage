"""
Trip planner: choose which cards (and how many copies) to buy on Cardmarket for one trip.

POST /api/plan  body:
  {"params": <scanner filters, as MTG.readFilters() returns them>,
   "budget_eur": 500, "max_cards": 100, "max_per_card": 1, "max_per_name": 2,
   "max_share_per_card": 25,            # optional: % of budget one card name may take
   "min_mp_qty": null,                  # optional extra liquidity filter
   "exclude_keys": ["<id>:<finish>"],   # optional: printings already owned / planned
   "objective": "profit" | "roi"}

The problem is a bounded knapsack with a second (cardinality) constraint and group caps:
  max  sum p_i q_i
  s.t. sum c_i q_i <= budget        (c_i = Cardmarket EUR incl. buy markup)
       sum q_i     <= max_cards
       q_i <= max_per_card;  sum over a name <= max_per_name;  name spend <= share * budget

Solver ("profit" objective):
  1. Surrogate-ratio sweep: the two resources are merged into one weight
     w_i = theta * c_i / budget + (1 - theta) / max_cards, and items are filled greedily by
     p_i / w_i (respecting every cap) for a grid of theta in [0, 1], then refined around the best
     theta.  theta=0 is plain greedy-by-profit, theta=1 is greedy-by-ROI, so the result is never
     worse than either.
  2. Local search: 1-for-1 swaps (replace a copy with a more profitable one that still fits),
     followed by a re-fill of any freed budget/slots, until no swap improves.
  3. A Lagrangian upper bound (both constraints dualized, name caps kept) gives a certificate of
     how far the plan can be from optimal ("gap").
"roi" objective: greedy by ROI (capital efficiency), leftover budget is left unspent.
"""
import bisect
import math
import time

import mtg_arb

LIST_PARAMS = {"finishes", "sets", "rarities"}
STR_PARAMS = {"q", "sort", "sell_mode", "storefront"}
EPS = 1e-9
CURVE = (25, 50, 75, 100, 150, 200)


# ---------------------------------------------------------------- input normalization
def norm_params(raw):
    """Same typing as app.parse_params, but from a JSON dict (values may be str, number or list)."""
    p = {}
    for k, v in (raw or {}).items():
        if k in LIST_PARAMS:
            if isinstance(v, str):
                v = [x for x in v.split(",") if x]
            p[k] = list(v) if v else None
        elif k in STR_PARAMS:
            p[k] = "" if v is None else str(v)
        elif k in mtg_arb.DEFAULTS or k == "fx":
            if v is None or (isinstance(v, str) and v.strip() == ""):
                p[k] = None
            else:
                p[k] = float(v)
    return p


def _num(body, key, default, lo=None, cast=float):
    v = body.get(key, default)
    if v is None or (isinstance(v, str) and v.strip() == ""):
        v = default
    if v is None:
        return None
    v = cast(float(v))
    if lo is not None and v < lo:
        raise ValueError(f"{key} must be >= {lo}")
    return v


# ---------------------------------------------------------------- solver
class Problem:
    """Arrays describing one planning instance (index i = one printing+finish row)."""

    def __init__(self, rows, cost_mult, budget, max_cards, max_per_card, max_per_name, share_pct):
        self.budget, self.K = budget, max_cards
        self.name_cap = max_per_name
        self.share_cap = budget * share_pct / 100 if share_pct else math.inf
        rows = [r for r in rows if r["profit_usd"] > 0]
        cost = [r["cardmarket_eur"] * cost_mult for r in rows]
        keep = [i for i, c in enumerate(cost) if 0 < c <= budget + EPS and c <= self.share_cap + EPS]
        self.rows = [rows[i] for i in keep]
        self.c = [cost[i] for i in keep]
        self.p = [r["profit_usd"] for r in self.rows]
        self.n = len(self.rows)
        gid = {}
        self.g = [gid.setdefault(r["name"], len(gid)) for r in self.rows]
        self.ngroups = len(gid)
        self.u = [max(0, min(max_per_card, max_per_name,
                             int((self.share_cap + EPS) // c) if self.share_cap < math.inf else max_per_card))
                  for c in self.c]
        self.min_cost = min(self.c) if self.c else 0

    # -- a plan is (qty list, aggregates) --
    def empty(self):
        return {"q": [0] * self.n, "spend": 0.0, "cnt": 0, "gcnt": [0] * self.ngroups,
                "gspend": [0.0] * self.ngroups, "profit": 0.0}

    def copy(self, s):
        return {"q": s["q"][:], "spend": s["spend"], "cnt": s["cnt"], "gcnt": s["gcnt"][:],
                "gspend": s["gspend"][:], "profit": s["profit"]}

    def room(self, s, i):
        """How many more copies of i fit into plan s."""
        c, g = self.c[i], self.g[i]
        k = min(self.u[i] - s["q"][i], self.name_cap - s["gcnt"][g], self.K - s["cnt"])
        if k <= 0:
            return 0
        k = min(k, int((self.budget - s["spend"] + EPS) // c))
        if self.share_cap < math.inf:
            k = min(k, int((self.share_cap - s["gspend"][g] + EPS) // c))
        return max(k, 0)

    def add(self, s, i, k):
        g = self.g[i]
        s["q"][i] += k
        s["spend"] += self.c[i] * k
        s["cnt"] += k
        s["gcnt"][g] += k
        s["gspend"][g] += self.c[i] * k
        s["profit"] += self.p[i] * k

    def fill(self, s, order):
        """Greedy: walk `order`, taking as many copies of each item as still fit."""
        for i in order:
            if s["cnt"] >= self.K or self.budget - s["spend"] < self.min_cost - EPS:
                break
            k = self.room(s, i)
            if k:
                self.add(s, i, k)
        return s

    def order_for(self, theta):
        B, K, c, p = self.budget, self.K, self.c, self.p
        a, b = theta / B, (1 - theta) / K
        return sorted(range(self.n), key=lambda i: -p[i] / (a * c[i] + b))

    def sweep(self, thetas):
        best = None
        for t in thetas:
            s = self.fill(self.empty(), self.order_for(t))
            if best is None or s["profit"] > best[1]["profit"] + EPS:
                best = (t, s)
        return best

    def improve(self, s, order, deadline):
        """1-for-1 swaps toward higher profit, then re-fill. Returns number of swaps."""
        byp = sorted(range(self.n), key=lambda i: -self.p[i])
        neg_p = [-self.p[i] for i in byp]
        swaps = 0
        while time.perf_counter() < deadline:
            improved = False
            sel = sorted((i for i in range(self.n) if s["q"][i]), key=lambda i: self.p[i])
            for j in sel:
                if not s["q"][j]:
                    continue
                pj, cj, gj = self.p[j], self.c[j], self.g[j]
                limit = self.budget - s["spend"] + cj + EPS
                stop = bisect.bisect_left(neg_p, -pj - 1e-6)  # only strictly better items
                for idx in range(stop):
                    i = byp[idx]
                    if i == j or self.c[i] > limit or s["q"][i] >= self.u[i]:
                        continue
                    gi = self.g[i]
                    if gi != gj and s["gcnt"][gi] >= self.name_cap:
                        continue
                    if self.share_cap < math.inf:
                        gs = s["gspend"][gi] - (cj if gi == gj else 0) + self.c[i]
                        if gs > self.share_cap + EPS:
                            continue
                    self.add(s, j, -1)
                    self.add(s, i, 1)
                    swaps += 1
                    improved = True
                    break
                if time.perf_counter() > deadline:
                    break
            self.fill(s, order)
            if not improved:
                break
        return swaps

    def lagrangian_bound(self, mu, lam):
        """Valid upper bound: mu*B + lam*K + best relaxed value with caps (mu, lam >= 0)."""
        per_group = {}
        for i in range(self.n):
            r = self.p[i] - mu * self.c[i] - lam
            if r > 0:
                per_group.setdefault(self.g[i], []).append((r, self.u[i]))
        total = mu * self.budget + lam * self.K
        cap = self.name_cap
        for items in per_group.values():
            if len(items) > 1:
                items.sort(reverse=True)
            left = cap
            for r, u in items:
                k = min(u, left)
                total += r * k
                left -= k
                if not left:
                    break
        return total

    def bound(self, theta, ub_hint):
        """Minimize the Lagrangian along direction (theta/B, (1-theta)/K) by golden-section search."""
        a, b = theta / self.budget, (1 - theta) / self.K
        f = lambda t: self.lagrangian_bound(t * a, t * b)  # noqa: E731
        lo, hi = 0.0, max(1.0, max(self.p, default=0) * 2 / max(a * self.min_cost + b, EPS))
        best = min(ub_hint, f(lo))
        gr = (math.sqrt(5) - 1) / 2
        x1, x2 = hi - gr * (hi - lo), lo + gr * (hi - lo)
        f1, f2 = f(x1), f(x2)
        for _ in range(28):
            if f1 < f2:
                hi, x2, f2 = x2, x1, f1
                x1 = hi - gr * (hi - lo)
                f1 = f(x1)
            else:
                lo, x1, f1 = x1, x2, f2
                x2 = lo + gr * (hi - lo)
                f2 = f(x2)
        return min(best, f1, f2)


def solve(prob, objective="profit", time_limit=0.6, want_bound=True):
    t0 = time.perf_counter()
    if not prob.n:
        return prob.empty(), {"algorithm": "empty", "greedy_profit_usd": 0.0}
    greedy = prob.fill(prob.empty(), prob.order_for(0.0))  # plain greedy by profit
    if objective == "roi":
        s = prob.fill(prob.empty(), prob.order_for(1.0))
        return s, {"algorithm": "greedy by ROI (profit per euro)", "greedy_profit_usd": round(greedy["profit"], 2),
                   "ms": round((time.perf_counter() - t0) * 1000, 1)}
    theta, s = prob.sweep([i / 10 for i in range(11)])
    fine = [theta + d / 100 for d in range(-9, 10) if d and 0 <= theta + d / 100 <= 1]
    t2, s2 = prob.sweep(fine)
    if s2["profit"] > s["profit"] + EPS:
        theta, s = t2, s2
    order = prob.order_for(theta)
    swaps = prob.improve(s, order, t0 + time_limit)
    info = {"algorithm": f"surrogate-ratio sweep (theta={theta:.2f}) + {swaps} swap(s)",
            "theta": round(theta, 2), "swaps": swaps, "greedy_profit_usd": round(greedy["profit"], 2)}
    if want_bound:
        ub = min(prob.bound(t, math.inf) for t in {theta, 0.0, 1.0, 0.5})
        info["upper_bound_usd"] = round(ub, 2)
        info["gap_pct"] = round(max(0.0, (ub - s["profit"]) / ub * 100), 2) if ub > 0 else 0.0
    info["ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return s, info


# ---------------------------------------------------------------- endpoint
def plan(state, query, body):
    body = body or {}
    try:
        params = norm_params(body.get("params"))
        budget = _num(body, "budget_eur", 500, lo=0)
        max_cards = _num(body, "max_cards", 100, lo=0, cast=int)
        per_card = _num(body, "max_per_card", 1, lo=1, cast=int)
        per_name = _num(body, "max_per_name", 2, lo=1, cast=int)
        share = _num(body, "max_share_per_card", None, lo=0)
        min_mp = _num(body, "min_mp_qty", None, lo=0)
    except (TypeError, ValueError) as e:
        return {"error": f"bad input: {e}"}, 400
    if share is not None and (share <= 0 or share >= 100):
        share = None
    objective = body.get("objective") if body.get("objective") in ("profit", "roi") else "profit"
    # buy-list lot keys may carry a "#2" suffix; the printing key is the part before it
    exclude = {str(k).split("#", 1)[0] for k in (body.get("exclude_keys") or [])}

    rows = state.scan(params)
    if exclude:
        rows = [r for r in rows if f"{r['id']}:{r['finish']}" not in exclude]
    if min_mp:
        rows = [r for r in rows if (r.get("mp_qty") or 0) >= min_mp]
    p = {**mtg_arb.DEFAULTS, **{k: v for k, v in params.items() if v is not None}}
    mult = 1 + p["buy_markup"] / 100

    prob = Problem(rows, mult, budget, max_cards, per_card, per_name, share)
    s, info = solve(prob, objective)

    items = []
    for i in range(prob.n):
        if s["q"][i]:
            items.append({**prob.rows[i], "qty": s["q"][i], "spend_eur": round(prob.c[i] * s["q"][i], 2)})
    items.sort(key=lambda r: (-r["profit_usd"] * r["qty"], r["name"]))
    landed = sum(r["landed_cost_usd"] * r["qty"] for r in items)
    net = sum(r["net_sale_usd"] * r["qty"] for r in items)
    profit = sum(r["profit_usd"] * r["qty"] for r in items)
    totals = {
        "spend_eur": round(s["spend"], 2), "landed_usd": round(landed, 2), "net_usd": round(net, 2),
        "profit_usd": round(profit, 2), "roi_pct": round(profit / landed * 100, 1) if landed else 0.0,
        "cards": s["cnt"], "distinct": len(items),
        "budget_left_eur": round(budget - s["spend"], 2),
    }
    left = budget - s["spend"]
    totals["limited_by"] = ("cards" if s["cnt"] >= max_cards else
                            "budget" if prob.n and left < prob.min_cost else "candidates")

    curve = []
    for pct in CURVE:
        if pct == 100:
            cs = s
        else:
            b = budget * pct / 100
            sub = Problem(prob.rows, mult, b, max_cards, per_card, per_name, share)
            cs, _ = solve(sub, objective, time_limit=0.12, want_bound=False)
        curve.append({"pct": pct, "budget_eur": round(budget * pct / 100, 2),
                      "profit_usd": round(cs["profit"], 2), "spend_eur": round(cs["spend"], 2),
                      "cards": cs["cnt"]})

    return {"items": items, "totals": totals, "candidates": len(rows), "eligible": prob.n,
            "algorithm": info.pop("algorithm"), "solver": info, "curve": curve,
            "objective": objective, "buy_markup_pct": p["buy_markup"]}


ROUTES = {("POST", "/api/plan"): plan}
