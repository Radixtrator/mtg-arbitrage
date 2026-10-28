// Trip planner tab: pick the most profitable set of cards for one Cardmarket order / luggage trip.
// Backend: POST /api/plan (features/planner.py). Optional: /api/buylist (buy list feature).
(() => {
  const M = window.MTG;
  if (!M) return;
  const {esc, usd, eur} = M;
  const SKEY = "planner";
  const DEF = {budget_eur: 500, max_cards: 100, max_per_card: 1, max_per_name: 2,
               max_share_per_card: "", objective: "profit", exclude_buylist: true};
  const STORE = {tcgplayer: ["TCGplayer", "tcg"], manapool: ["Mana Pool", "mp"], cardkingdom: ["Card Kingdom", "ck"]};
  const MODE = {fees: "after fees", market: "market value", credit: "store credit"};
  let opts = {...DEF, ...M.store.get(SKEY, {})};
  let plan = null, busy = false, root;

  const css = document.createElement("style");
  css.textContent = `
    .pl-form { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 10px 12px; align-items: end; }
    .pl-form .seg { width: 100%; display: flex; } .pl-form .seg button { flex: 1; }
    .pl-check { display: inline-flex; align-items: center; gap: 6px; font-size: 13px; cursor: pointer; }
    .pl-bar { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin-top: 12px; }
    .pl-head { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin: 16px 0 10px; }
    .pl-head .grow { flex: 1; }
    .pl-chart { margin-bottom: 14px; }
    .pl-chart svg { display: block; width: 100%; height: auto; max-height: 220px; }
    .pl-chart text { font-size: 11px; fill: var(--muted); font-variant-numeric: tabular-nums; }
    .pl-chart text.v { fill: var(--text); font-weight: 600; }
    .pl-chart .k { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; margin-bottom: 6px; }
    .pl-summary { margin: 10px 0 0; }
  `;
  document.head.appendChild(css);

  function save() { M.store.set(SKEY, opts); }
  const num = v => (v === "" || v == null || isNaN(+v)) ? "" : +v;

  function filterSummary() {
    const p = M.readFilters();
    const [label] = STORE[p.storefront] || [p.storefront === "best" ? "best storefront" : p.storefront];
    const bits = [`sell on ${label}`, MODE[p.sell_mode] || p.sell_mode];
    if (p.min_roi !== "") bits.push(`min ROI ${p.min_roi}%`);
    if (p.min_profit !== "") bits.push(`min profit ${usd(p.min_profit)}`);
    if (p.max_usd) bits.push(`US price ${usd(p.min_usd || 0)}–${usd(p.max_usd)}`);
    else if (p.min_usd !== "") bits.push(`US price ≥ ${usd(p.min_usd)}`);
    if (p.sets) bits.push(`sets: ${p.sets.split(",").length}`);
    if (p.q) bits.push(`name contains “${p.q}”`);
    if (p.buy_markup !== "") bits.push(`buy +${p.buy_markup}% over trend`);
    return bits.map(esc).join(" · ");
  }

  function render(sec) {
    root = sec;
    sec.innerHTML = `
      <div class="panel">
        <div class="pl-form">
          <label class="f">Budget €<input type="number" id="plBudget" min="0" step="50"></label>
          <label class="f">Max cards in luggage<input type="number" id="plCards" min="1" step="10"></label>
          <label class="f">Max copies per printing<input type="number" id="plPerCard" min="1" step="1"></label>
          <label class="f">Max copies per name<input type="number" id="plPerName" min="1" step="1"></label>
          <label class="f" title="No single card name may take more than this share of the budget">Max share per card %<input type="number" id="plShare" min="0" max="100" step="5" placeholder="no limit"></label>
          <div><div class="lbl">Optimize for</div>
            <div class="seg" id="plObj"><button data-v="profit">Profit</button><button data-v="roi" title="Best return per euro; may leave budget unspent">ROI</button></div></div>
        </div>
        <p class="note pl-summary">Uses your current scanner filters and sell mode: <span id="plFilters"></span></p>
        <div class="pl-bar">
          <button class="btn primary" id="plBuild">Build plan</button>
          <label class="pl-check"><input type="checkbox" id="plExcl"> Skip cards already in my buy list</label>
        </div>
      </div>
      <div id="plOut"><div class="empty">Set a budget and press “Build plan”.</div></div>`;
    const bind = (id, k) => {
      const el = sec.querySelector(id);
      el.value = opts[k] ?? "";
      el.addEventListener("input", () => { opts[k] = el.value; save(); });
    };
    bind("#plBudget", "budget_eur"); bind("#plCards", "max_cards"); bind("#plPerCard", "max_per_card");
    bind("#plPerName", "max_per_name"); bind("#plShare", "max_share_per_card");
    const ex = sec.querySelector("#plExcl");
    ex.checked = !!opts.exclude_buylist;
    ex.onchange = () => { opts.exclude_buylist = ex.checked; save(); };
    const seg = sec.querySelector("#plObj");
    const syncSeg = () => seg.querySelectorAll("button").forEach(b => b.classList.toggle("on", b.dataset.v === opts.objective));
    seg.onclick = e => { const v = e.target.dataset.v; if (v) { opts.objective = v; save(); syncSeg(); } };
    syncSeg();
    sec.querySelector("#plBuild").onclick = build;
    sec.querySelector("#plOut").addEventListener("click", onOutClick);
    updateSummary();
  }

  function updateSummary() {
    const el = root && root.querySelector("#plFilters");
    if (el) { try { el.innerHTML = filterSummary(); } catch { el.textContent = ""; } }
  }

  async function buylistKeys() {
    try {
      const res = await M.api("/api/buylist");
      return (res.items || []).filter(i => i.status !== "sold").map(i => i.card_key || i.key).filter(Boolean);
    } catch (e) {
      M.toast("Buy list not available, so nothing is skipped");
      return [];
    }
  }

  async function build() {
    if (busy) return;
    const b = root.querySelector("#plBuild");
    busy = true; b.disabled = true; b.textContent = "Planning…";
    try {
      const body = {
        params: M.readFilters(),
        budget_eur: num(opts.budget_eur) || 0,
        max_cards: num(opts.max_cards) || 1,
        max_per_card: num(opts.max_per_card) || 1,
        max_per_name: num(opts.max_per_name) || 1,
        max_share_per_card: num(opts.max_share_per_card) || null,
        objective: opts.objective,
        exclude_keys: opts.exclude_buylist ? await buylistKeys() : [],
      };
      plan = await M.api("/api/plan", {method: "POST", body});
      plan.excluded = body.exclude_keys.length;
      show();
    } catch (e) {
      M.toast("Plan failed: " + e.message);
    } finally {
      busy = false; b.disabled = false; b.textContent = "Build plan";
    }
  }

  function chart(curve) {
    if (!curve || !curve.length) return "";
    const W = 640, H = 200, padL = 8, padR = 8, top = 22, bottom = 36;
    const max = Math.max(...curve.map(c => c.profit_usd), 1);
    const slot = (W - padL - padR) / curve.length, bw = Math.min(64, slot * 0.62);
    const y = v => top + (H - top - bottom) * (1 - v / max);
    const bars = curve.map((c, i) => {
      const x = padL + slot * i + (slot - bw) / 2, yy = y(c.profit_usd), cur = c.pct === 100;
      return `<rect x="${x.toFixed(1)}" y="${yy.toFixed(1)}" width="${bw.toFixed(1)}" height="${Math.max(0, H - bottom - yy).toFixed(1)}"
                rx="3" fill="var(--accent)" opacity="${cur ? 1 : 0.4}"><title>${esc(eur(c.budget_eur))} budget → ${esc(usd(c.profit_usd))} profit, ${c.cards} cards, ${esc(eur(c.spend_eur))} spent</title></rect>
        <text class="v" x="${(x + bw / 2).toFixed(1)}" y="${(yy - 6).toFixed(1)}" text-anchor="middle">${esc(usd(Math.round(c.profit_usd)).replace(/\.00$/, ""))}</text>
        <text x="${(x + bw / 2).toFixed(1)}" y="${H - bottom + 15}" text-anchor="middle">${esc(eur(c.budget_eur).replace(/\.00$/, ""))}</text>
        <text x="${(x + bw / 2).toFixed(1)}" y="${H - bottom + 29}" text-anchor="middle">${c.pct}%${cur ? " (now)" : ""}</text>`;
    }).join("");
    const pts = curve.map((c, i) => `${(padL + slot * i + slot / 2).toFixed(1)},${y(c.profit_usd).toFixed(1)}`).join(" ");
    // marginal profit per extra 100 EUR beyond the current budget
    const i100 = curve.findIndex(c => c.pct === 100), nxt = curve[i100 + 1], cur = curve[i100];
    let note = "";
    if (cur && nxt && nxt.budget_eur > cur.budget_eur) {
      const m = (nxt.profit_usd - cur.profit_usd) / (nxt.budget_eur - cur.budget_eur) * 100;
      const avg = cur.spend_eur ? cur.profit_usd / cur.spend_eur * 100 : 0;
      note = `<p class="note" style="margin:4px 0 0">Each extra €100 of budget adds about ${esc(usd(m))} profit (your plan averages ${esc(usd(avg))} per €100 spent).</p>`;
    }
    return `<div class="panel pl-chart"><div class="k">Profit vs budget (same card limit)</div>
      <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Expected profit at different budgets">
        <line x1="${padL}" x2="${W - padR}" y1="${H - bottom}" y2="${H - bottom}" stroke="var(--border)"/>
        ${bars}
        <polyline points="${pts}" fill="none" stroke="var(--accent)" stroke-width="1.5" stroke-dasharray="3 3" opacity=".8"/>
      </svg>${note}</div>`;
  }

  function show() {
    const out = root.querySelector("#plOut");
    const p = plan;
    if (!p.items.length) {
      out.innerHTML = `<div class="empty">No cards fit. ${p.candidates ? "Try a bigger budget or more cards." : "No cards match your scanner filters."}</div>`;
      return;
    }
    const t = p.totals, s = p.solver || {};
    const lim = {cards: "the card limit", budget: "the budget", candidates: "the cards available under your filters"}[t.limited_by] || t.limited_by;
    const vsGreedy = s.greedy_profit_usd != null && t.profit_usd - s.greedy_profit_usd > 0.005
      ? ` That is ${usd(t.profit_usd - s.greedy_profit_usd)} more than picking the most profitable cards first (${usd(s.greedy_profit_usd)}).` : "";
    const gap = s.gap_pct != null ? ` It is within ${s.gap_pct}% of the best possible plan.` : "";
    out.innerHTML = `
      <div class="pl-head"><b>Plan</b><span class="grow"></span>
        <button class="btn" id="plAdd">Add all to buy list</button>
        <button class="btn" id="plCsv">Export CSV</button>
        <button class="btn" id="plCopy">Copy Cardmarket list</button></div>
      <div class="stats">
        <div class="stat"><div class="v">${eur(t.spend_eur)}</div><div class="k">Cardmarket spend</div></div>
        <div class="stat"><div class="v">${usd(t.landed_usd)}</div><div class="k">Landed cost</div></div>
        <div class="stat"><div class="v">${usd(t.net_usd)}</div><div class="k">Expected net</div></div>
        <div class="stat"><div class="v pos">${usd(t.profit_usd)}</div><div class="k">Profit</div></div>
        <div class="stat"><div class="v">${Math.round(t.roi_pct)}%</div><div class="k">ROI</div></div>
        <div class="stat"><div class="v">${t.cards}</div><div class="k">Cards (${t.distinct} distinct)</div></div>
      </div>
      <p class="note">Limited by ${esc(lim)}; ${esc(eur(t.budget_left_eur))} of the budget is left. Chosen from ${p.eligible.toLocaleString()} candidates${p.excluded ? `, skipping ${p.excluded} already in your buy list` : ""}. Spend includes your ${p.buy_markup_pct}% markup over the trend price.${esc(vsGreedy)}${esc(gap)}</p>
      ${chart(p.curve)}
      <div class="tablewrap"><table class="t"><thead><tr>
        <th></th><th>Card</th><th>Set</th><th>Finish</th><th class="n">Qty</th><th class="n">CM €</th>
        <th class="n">US $</th><th>Sells on</th><th class="n">Profit</th><th class="n">ROI</th>
      </tr></thead><tbody>${p.items.map((r, i) => {
        const [lbl, cls] = STORE[r.sold_on] || [r.sold_on, ""];
        return `<tr data-pi="${i}">
          <td>${r.image_small ? `<img class="thumb" loading="lazy" src="${esc(r.image_small)}" alt="">` : ""}</td>
          <td>${esc(r.name)}${r.finish !== "nonfoil" ? " ✦" : ""}</td>
          <td title="${esc(r.set_name)}">${esc(r.set)} #${esc(r.number)}</td><td>${esc(r.finish)}</td>
          <td class="n">${r.qty}</td><td class="n">${eur(r.cardmarket_eur)}</td>
          <td class="n">${usd(r.sell_price_usd)}</td><td class="${cls}">${esc(lbl)}</td>
          <td class="n pos" title="${esc(usd(r.profit_usd))} each">${usd(r.profit_usd * r.qty)}</td>
          <td class="n">${Math.round(r.roi_pct)}%</td></tr>`;
      }).join("")}</tbody></table></div>
      <p class="note">${esc(p.algorithm)}${s.ms != null ? ` · ${s.ms} ms` : ""}</p>`;
  }

  function onOutClick(e) {
    if (!plan) return;
    const id = e.target.id;
    if (id === "plAdd") return addAll(e.target);
    if (id === "plCsv") return exportCsv();
    if (id === "plCopy") return copyList();
    const tr = e.target.closest("tr[data-pi]");
    if (tr) M.openDrawer(plan.items[+tr.dataset.pi]);
  }

  async function addAll(btn) {
    btn.disabled = true;
    try {
      const items = plan.items.map(r => {
        const row = {...r}; delete row.qty; delete row.spend_eur;
        return {row, qty: r.qty};
      });
      const res = await M.api("/api/buylist", {method: "POST", body: {items}});
      window.dispatchEvent(new CustomEvent("buylist:changed"));
      M.toast(`Added ${res.added ?? plan.totals.cards} cards to the buy list`);
    } catch (e) {
      M.toast("Could not add to buy list: " + e.message);
    } finally { btn.disabled = false; }
  }

  function exportCsv() {
    const cols = ["qty", "name", "set", "set_name", "number", "finish", "cardmarket_eur", "spend_eur", "sold_on",
                  "sell_price_usd", "landed_cost_usd", "net_sale_usd", "profit_usd", "roi_pct", "cardmarket_url"];
    const q = v => { const s = String(v ?? ""); return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s; };
    const lines = [cols.join(","), ...plan.items.map(r => cols.map(c => q(r[c])).join(","))];
    const blob = new Blob([lines.join("\n") + "\n"], {type: "text/csv"});
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `trip-plan-${new Date().toISOString().slice(0, 10)}.csv`;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }

  async function copyList() {
    const text = plan.items.map(r => `${r.qty} ${r.name} (${r.set_name})`).join("\n");
    try { await navigator.clipboard.writeText(text); }
    catch {
      const ta = document.createElement("textarea"); ta.value = text; document.body.appendChild(ta);
      ta.select(); try { document.execCommand("copy"); } catch {} ta.remove();
    }
    M.toast(`Copied ${plan.items.length} lines for Cardmarket`);
  }

  M.addTab({id: "planner", label: "Trip planner", render, onShow: updateSummary});
  M.on("filters", updateSummary);
  M.on("meta", updateSummary);
})();
