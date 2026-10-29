// Buy list + portfolio tracker. Backend: features/buylist.py (/api/buylist).
// Other features: POST /api/buylist, then window.dispatchEvent(new Event("buylist:changed")).
// Read-only helper for other scripts: MTG.buylist.{items, has(rowKey), refresh()}.
(() => {
  const M = window.MTG;
  const {esc, usd, eur} = M;
  const STATUSES = ["planned", "ordered", "received", "listed", "sold"];
  const CONDS = ["M", "NM", "EX", "GD", "LP", "PL", "PO"];
  const VIA = {"": "Auto", tcgplayer: "TCGplayer", manapool: "Mana Pool", cardkingdom: "CK buylist", cardconduit: "CardConduit", offer: "Buyer offer", credit: "Store credit", local: "Local / cash"};
  const STORE_LABEL = {tcgplayer: "TCGplayer", manapool: "Mana Pool", cardkingdom: "Card Kingdom", cardconduit: "CardConduit", offer: "Buyer offer"};
  const FIN = {nonfoil: "", foil: "foil", etched: "etched"};

  let items = [], cardKeys = new Set(), sel = new Set(), tab, root;
  let filter = M.store.get("bl.filter", "all");
  let groupBy = M.store.get("bl.group", "");
  let sortBy = M.store.get("bl.sort", "added");

  const cardKey = i => i.card_key || i.key;
  const n2 = v => v == null || v === "" ? "" : Number(v).toFixed(2);
  const cls = v => v > 0.005 ? "pos" : v < -0.005 ? "neg" : "";
  const sum = (list, fn) => list.reduce((a, i) => a + (fn(i) || 0), 0);

  // ---------- data ----------
  async function refresh() {
    try { setItems((await M.api("/api/buylist")).items); }
    catch (e) { if (root) root.querySelector(".bl-body").innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
  }
  function setItems(list) {
    items = list || [];
    cardKeys = new Set(items.map(cardKey));
    const live = new Set(items.map(i => i.key));
    sel = new Set([...sel].filter(k => live.has(k)));
    if (tab) tab.setCount(items.filter(i => i.status !== "sold").length);
    render();
    M.rerender(); // refresh the stars on scanner results
  }
  function changed() {
    window.dispatchEvent(new CustomEvent("buylist:changed", {detail: {source: "buylist", items}}));
  }
  async function call(method, body) {
    try {
      const res = await M.api("/api/buylist", {method, body});
      setItems(res.items); changed();
      return res;
    } catch (e) { M.toast("Buy list: " + e.message); refresh(); return null; }
  }
  window.addEventListener("buylist:changed", e => { if (e.detail?.source !== "buylist") refresh(); });
  M.on("filters", () => render()); // cost model changed -> recompute profits
  M.on("meta", () => refresh());   // price data (re)loaded -> new current prices

  M.buylist = {get items() { return items; }, has: k => cardKeys.has(k), refresh};

  // ---------- economics (per copy, USD) ----------
  function calc(it, p = M.readFilters()) {
    const cur = it.eur ?? it.cardmarket_eur;
    const snap = it.cardmarket_eur ?? cur;
    // cost: actual price paid has no "buy above trend" markup; otherwise model it on trend
    const cost = it.actual_buy_eur != null ? M.landedCost(it.actual_buy_eur, {...p, buy_markup: 0})
      : M.landedCost(it.status === "planned" ? cur : snap, p);
    // current price per store (falls back to the snapshot if the printing left today's data)
    const prices = {tcgplayer: it.tcg ?? it.tcgplayer_usd, manapool: it.mp ?? it.manapool_usd,
                    cardkingdom: it.priced ? it.ck : it.cardkingdom_buy_usd,
                    cardconduit: it.priced ? it.cc : it.cardconduit_buy_usd};
    const via = it.sell_via || "";
    // A US buyer's offer (list checker): use the snapshotted offer and its net, not a live store price.
    if (via === "offer" || (!via && it.sold_on === "offer")) {
      const est = it.actual_sell_usd == null, net = est ? it.net_sale_usd ?? it.sell_price_usd : it.actual_sell_usd;
      const profit = net == null ? null : net - cost;
      return {cost, net, profit, roi: profit == null || !cost ? null : profit / cost * 100, store: "offer",
              market: it.sell_price_usd, est, drift: cur != null && snap != null ? cur - snap : 0, cur, snap};
    }
    const avail = Object.keys(prices).filter(s => prices[s]);
    let store;
    if (prices[via] !== undefined) store = via;
    else if (via === "credit" || via === "local") store = prices.tcgplayer ? "tcgplayer" : prices.manapool ? "manapool" : null;
    else if (prices[it.sold_on]) store = it.sold_on; // Auto: the store the card was picked for
    else store = avail.sort((a, b) => M.netSale(prices[b], b, p) - M.netSale(prices[a], a, p))[0] || null;
    const market = store ? prices[store] : null;
    let net = null, est = true;
    if (it.actual_sell_usd != null) {
      est = false;
      // CK pays cash with no fee; CardConduit still takes its service fee (netSale handles it)
      net = via === "credit" || via === "local" || store === "cardkingdom" ? it.actual_sell_usd
        : M.netSale(it.actual_sell_usd, store || "tcgplayer", {...p, sell_mode: "fees", sell_at: 100});
    } else if (market != null) {
      const mode = via === "credit" ? "credit" : via === "local" ? "market" : p.sell_mode;
      net = M.netSale(market, store, {...p, sell_mode: mode});
    }
    const profit = net == null ? null : net - cost;
    return {cost, net, profit, roi: profit == null || !cost ? null : profit / cost * 100, store, market, est,
            drift: cur != null && snap != null ? cur - snap : 0, cur, snap};
  }

  // ---------- star on every card ----------
  async function toggle(r) {
    const k = M.rowKey(r);
    const planned = items.find(i => cardKey(i) === k && i.status === "planned");
    if (planned) {
      if (await call("DELETE", {key: planned.key})) M.toast(`Removed ${r.name} from buy list`);
      return;
    }
    if (cardKeys.has(k) && !confirm(`${r.name} is already in your buy list (ordered or later). Plan another copy?`)) return;
    const res = await call("POST", {items: [{row: r, qty: 1}]});
    if (res) M.toast(`★ ${r.name} added to buy list`);
  }
  M.addCardAction({
    id: "buylist",
    icon: r => cardKeys.has(M.rowKey(r)) ? "★" : "☆",
    title: r => cardKeys.has(M.rowKey(r)) ? "Remove from buy list" : "Add to buy list",
    isActive: r => cardKeys.has(M.rowKey(r)),
    onClick: toggle,
  });

  // ---------- drawer: lots of this card + notes ----------
  M.addDrawerSection((r, el) => {
    const draw = () => {
      const lots = items.filter(i => cardKey(i) === M.rowKey(r));
      if (!lots.length) { el.innerHTML = ""; return; }
      el.innerHTML = `<h3>Buy list</h3>${lots.map(i => `
        <div class="panel" style="margin-bottom:8px;padding:10px">
          <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
            <b>${i.qty}×</b><span class="bl-st bl-${i.status}">${i.status}</span><span class="muted">${esc(i.condition)}</span>
            ${i.order_ref ? `<span class="muted">· ${esc(i.order_ref)}</span>` : ""}
            <span class="grow" style="flex:1"></span><span class="muted">added ${esc((i.created_at || "").slice(0, 10))} at ${eur(i.cardmarket_eur)}</span>
          </div>
          <label class="f" style="margin-top:6px">Notes<textarea data-k="${esc(i.key)}" rows="2" style="width:100%;display:block;margin-top:3px">${esc(i.notes)}</textarea></label>
        </div>`).join("")}`;
      el.querySelectorAll("textarea").forEach(t => t.onchange = () => call("PUT", {key: t.dataset.k, notes: t.value}));
    };
    draw();
  });

  // ---------- tab ----------
  const css = document.createElement("style");
  css.textContent = `
    .bl-body table.t td { vertical-align: middle; }
    .bl-body input[type=number] { width: 74px; padding: 3px 6px; }
    .bl-body input.qty { width: 54px; }
    .bl-body input.ref { width: 120px; padding: 3px 6px; }
    .bl-body select { padding: 3px 4px; }
    .bl-body tr.grp td { background: var(--panel-2); font-weight: 600; font-size: 12px; }
    .bl-body tr.bl-sel { background: color-mix(in srgb, var(--accent) 10%, transparent); }
    .bl-body .nm { max-width: 240px; overflow: hidden; text-overflow: ellipsis; display: inline-block; vertical-align: middle; }
    .bl-st { font-size: 11px; padding: 1px 8px; border-radius: 999px; background: var(--chip); }
    .bl-ordered { color: var(--c-cm); } .bl-received { color: var(--c-mp); } .bl-listed { color: var(--c-tcg); } .bl-sold { color: var(--pos); }
    .bl-bulk { background: var(--panel); border: 1px solid var(--accent); border-radius: 10px; padding: 8px 10px; }
    .bl-exports { margin-top: 14px; }
    .bl-seg { flex-wrap: wrap; }
    @media (max-width: 820px) { .bl-seg button { padding: 6px 8px; font-size: 12px; } }`;
  document.head.appendChild(css);

  tab = M.addTab({
    id: "buylist", label: "Buy list",
    render(sec) {
      root = sec;
      sec.innerHTML = `
        <div class="toolbar">
          <div class="seg bl-seg" data-r="status">${["all", ...STATUSES].map(s =>
            `<button data-v="${s}" class="${s === filter ? "on" : ""}">${s[0].toUpperCase() + s.slice(1)}<span class="muted" data-cnt="${s}"></span></button>`).join("")}</div>
          <span class="grow"></span>
          <label class="f" style="display:flex;align-items:center;gap:6px">Group
            <select data-r="group"><option value="">None</option><option value="order_ref">Order ref</option>
              <option value="status">Status</option><option value="set">Set</option></select></label>
          <label class="f" style="display:flex;align-items:center;gap:6px">Sort
            <select data-r="sort"><option value="added">Newest</option><option value="name">Name</option><option value="set">Set</option>
              <option value="status">Status</option><option value="profit">Profit</option><option value="drift">Price drift</option></select></label>
        </div>
        <div class="stats" data-r="stats"></div>
        <div class="bl-bulk toolbar" data-r="bulk" hidden></div>
        <div class="bl-body"></div>
        <div class="panel bl-exports">
          <div class="lbl">Export</div>
          <div class="btn-row">
            <button class="btn" data-x="cm" title="Paste into Cardmarket: Wants > a wants list > Add decklist">Cardmarket wants list</button>
            <button class="btn" data-x="tcg" title="TCGplayer Mass Entry: qty Name [SET]">TCGplayer Mass Entry</button>
            <button class="btn" data-x="mox" title="qty Name (SET) number *F* -- Moxfield, Mana Pool, Archidekt">Mana Pool / Moxfield</button>
            <button class="btn" data-x="csv">CSV (everything)</button>
          </div>
          <p class="note" style="margin-bottom:0">Exports use the selected rows if any; otherwise Cardmarket = planned items,
            TCGplayer = received + listed (in hand), Mana Pool / Moxfield = rows in the current view. Text exports are also copied to the clipboard.
            Cardmarket matches on <i>Name (Expansion)</i>; check its red "not added" list for set names that differ from Scryfall's, and set foil per want afterwards.</p>
          <p class="note" style="margin-bottom:0">Buy € = price you actually paid per copy (placeholder: Cardmarket trend when added). Sell $ = per-copy sale price;
            fees from the sidebar are deducted unless Via is CK buylist, Buyer offer, Store credit or Local (CardConduit deducts its service fee). Via Auto = the store the card was picked for in the scanner. Profit is per lot and follows the sidebar cost model.</p>
        </div>`;
      sec.querySelector("[data-r=group]").value = groupBy;
      sec.querySelector("[data-r=sort]").value = sortBy;
      wire(sec);
    },
    onShow() { render(); },
  });

  function viewList() {
    const list = items.filter(i => filter === "all" || i.status === filter);
    const c = new Map(list.map(i => [i.key, calc(i)]));
    const by = {
      added: (a, b) => (b.created_at || "").localeCompare(a.created_at || ""),
      name: (a, b) => a.name.localeCompare(b.name),
      set: (a, b) => a.set.localeCompare(b.set) || String(a.number).localeCompare(String(b.number), undefined, {numeric: true}),
      status: (a, b) => STATUSES.indexOf(a.status) - STATUSES.indexOf(b.status),
      profit: (a, b) => (c.get(b.key).profit ?? -1e9) * b.qty - (c.get(a.key).profit ?? -1e9) * a.qty,
      drift: (a, b) => c.get(b.key).drift - c.get(a.key).drift,
    }[sortBy] || (() => 0);
    list.sort(by);
    if (groupBy) {
      const g = i => groupBy === "status" ? STATUSES.indexOf(i.status) : String(i[groupBy] || "￿");
      list.sort((a, b) => { const x = g(a), y = g(b); return x < y ? -1 : x > y ? 1 : 0; }); // stable: keeps inner sort
    }
    return {list, c};
  }

  function render() {
    if (!root) return;
    const p = M.readFilters();
    // segment counts
    for (const s of ["all", ...STATUSES]) {
      const n = items.filter(i => s === "all" || i.status === s).length;
      root.querySelector(`[data-cnt="${s}"]`).textContent = n ? ` ${n}` : "";
    }
    renderStats(p);
    renderBulk();

    const body = root.querySelector(".bl-body");
    const af = document.activeElement, focus = af && body.contains(af) && af.dataset.f ? [af.closest("tr")?.dataset.k, af.dataset.f] : null;
    const {list, c} = viewList();
    if (!list.length) {
      body.innerHTML = `<div class="empty">${items.length ? "Nothing with this status." : "Your buy list is empty. Click ☆ on any card in the scanner to add it."}</div>`;
      return;
    }
    const allSel = list.every(i => sel.has(i.key));
    let html = `<div class="tablewrap"><table class="t"><thead><tr>
      <th><input type="checkbox" data-selall ${allSel ? "checked" : ""} aria-label="Select all"></th><th></th><th>Card</th><th>Set</th><th>Finish</th>
      <th class="n">Qty</th><th>Cond</th><th>Status</th><th class="n" title="Actual price paid per copy">Buy €</th>
      <th class="n" title="Cardmarket trend now (change since added)">Now €</th><th class="n" title="Actual / listed sale price per copy">Sell $</th>
      <th>Via</th><th>Order ref</th><th class="n" title="Per lot: expected (unsold) or realized (sold)">Profit</th><th></th></tr></thead><tbody>`;
    let lastG;
    for (const it of list) {
      if (groupBy) {
        const gv = groupBy === "status" ? it.status : (it[groupBy] || "");
        if (gv !== lastG) {
          lastG = gv;
          const gl = list.filter(i => (groupBy === "status" ? i.status : (i[groupBy] || "")) === gv);
          const gp = sum(gl, i => (c.get(i.key).profit ?? 0) * i.qty);
          const ge = sum(gl, i => (i.actual_buy_eur ?? c.get(i.key).cur ?? 0) * i.qty);
          html += `<tr class="grp"><td colspan="15">${esc(gv || (groupBy === "order_ref" ? "(no order ref)" : "–"))}
            <span class="muted"> · ${sum(gl, i => i.qty)} cop${sum(gl, i => i.qty) === 1 ? "y" : "ies"} · ${eur(ge)} ·</span>
            <span class="${cls(gp)}">${usd(gp)}</span></td></tr>`;
        }
      }
      const x = c.get(it.key);
      const lot = x.profit == null ? null : x.profit * it.qty;
      const sold = it.status === "sold";
      const driftHtml = Math.abs(x.drift) >= 0.01
        ? ` <span class="${it.status === "planned" ? (x.drift > 0 ? "neg" : "pos") : "muted"}" title="since added (${eur(x.snap)})">${x.drift > 0 ? "+" : ""}${x.drift.toFixed(2)}</span>` : "";
      html += `<tr data-k="${esc(it.key)}" class="${sel.has(it.key) ? "bl-sel" : ""}">
        <td><input type="checkbox" data-sel ${sel.has(it.key) ? "checked" : ""} aria-label="Select"></td>
        <td>${it.image_small ? `<img class="thumb" loading="lazy" src="${esc(it.image_small)}" data-big="${esc(it.image_normal || it.image_small)}" alt="">` : ""}</td>
        <td><a href="#" data-open class="nm" title="${esc(it.name)}${it.notes ? "\n\n" + esc(it.notes) : ""}">${esc(it.name)}</a>${it.finish !== "nonfoil" ? " ✦" : ""}${it.notes ? ' <span class="muted" title="' + esc(it.notes) + '">✎</span>' : ""}${!it.priced ? ' <span class="warn" title="Not in today\'s price data">!</span>' : ""}
          ${it.cardmarket_url && it.status === "planned" ? ` <a class="cm" target="_blank" rel="noopener" href="${esc(it.cardmarket_url)}" title="Open on Cardmarket">↗</a>` : ""}</td>
        <td title="${esc(it.set_name)}">${esc(it.set)} #${esc(it.number)}</td>
        <td>${FIN[it.finish] ?? esc(it.finish)}</td>
        <td class="n"><input class="qty" type="number" min="1" step="1" data-f="qty" value="${it.qty}"></td>
        <td><select data-f="condition">${CONDS.map(o => `<option ${o === it.condition ? "selected" : ""}>${o}</option>`).join("")}</select></td>
        <td><select data-f="status" class="bl-${it.status}">${STATUSES.map(o => `<option value="${o}" ${o === it.status ? "selected" : ""}>${o}</option>`).join("")}</select></td>
        <td class="n"><input type="number" min="0" step="0.01" inputmode="decimal" data-f="actual_buy_eur" value="${n2(it.actual_buy_eur)}" placeholder="${n2(it.cardmarket_eur)}"></td>
        <td class="n"><span class="cm">${eur(x.cur)}</span>${driftHtml}</td>
        <td class="n"><input type="number" min="0" step="0.01" inputmode="decimal" data-f="actual_sell_usd" value="${n2(it.actual_sell_usd)}" placeholder="${n2(x.market)}"></td>
        <td><select data-f="sell_via">${Object.entries(VIA).map(([v, l]) => `<option value="${v}" ${v === (it.sell_via || "") ? "selected" : ""}>${v ? l : `${l} (${STORE_LABEL[x.store] || "–"})`}</option>`).join("")}</select></td>
        <td><input type="text" class="ref" data-f="order_ref" value="${esc(it.order_ref)}" placeholder="seller / order #"></td>
        <td class="n ${cls(lot)}" title="${x.profit == null ? "No US price" : `${usd(x.profit)} per copy · cost ${usd(x.cost)} · net ${usd(x.net)}${x.store ? " via " + x.store : ""}${x.roi != null ? ` · ROI ${Math.round(x.roi)}%` : ""}`}">${lot == null ? "–" : usd(lot)}${sold && x.est ? ' <span class="muted">est.</span>' : ""}</td>
        <td><button class="btn sm" data-del title="Remove">✕</button></td>
      </tr>`;
    }
    body.innerHTML = html + "</tbody></table></div>";
    if (focus) body.querySelector(`tr[data-k="${CSS.escape(focus[0])}"] [data-f="${focus[1]}"]`)?.focus();
  }

  function renderStats(p) {
    const st = s => items.filter(i => i.status === s);
    const planned = st("planned"), bought = items.filter(i => i.status !== "planned");
    const unsold = items.filter(i => i.status !== "sold"), sold = st("sold");
    const c = new Map(items.map(i => [i.key, calc(i, p)]));
    const lotP = i => (c.get(i.key).profit ?? 0) * i.qty;
    const toSpend = sum(planned, i => (i.actual_buy_eur ?? i.eur ?? i.cardmarket_eur ?? 0) * i.qty);
    const spent = sum(bought, i => (i.actual_buy_eur ?? i.cardmarket_eur ?? 0) * i.qty);
    const exp = sum(unsold, lotP), real = sum(sold, lotP);
    const soldEst = sold.filter(i => i.actual_sell_usd == null).length;
    const drift = sum(planned, i => c.get(i.key).drift * i.qty);
    const worse = planned.filter(i => c.get(i.key).drift >= 0.01).length;
    const stat = (v, k, cl = "", title = "") => `<div class="stat" title="${esc(title)}"><div class="v ${cl}">${v}</div><div class="k">${k}</div></div>`;
    root.querySelector("[data-r=stats]").innerHTML = [
      stat(sum(items, i => i.qty).toLocaleString(), `Copies · ${items.length} lots`),
      stat(eur(toSpend), `To spend · ${sum(planned, i => i.qty)} planned`, "cm",
           `Landed ≈ ${usd(sum(planned, i => c.get(i.key).cost * i.qty))} with the sidebar cost model`),
      stat(eur(spent), "Spent · ordered+", "", `Landed ≈ ${usd(sum(bought, i => c.get(i.key).cost * i.qty))}`),
      stat(usd(exp), "Expected profit · unsold", cls(exp)),
      stat(usd(real), `Realized profit${soldEst ? ` · ${soldEst} est.` : ""}`, cls(real),
           soldEst ? `${soldEst} sold lot(s) have no sell price yet; estimated from market` : ""),
      stat(`${drift > 0 ? "+" : ""}${eur(drift)}`, worse ? `Price drift · ${worse} worse` : "Price drift · planned",
           drift > 0.005 ? "neg" : drift < -0.005 ? "pos" : "", "Cardmarket trend now vs when added, planned copies"),
    ].join("");
  }

  function renderBulk() {
    const b = root.querySelector("[data-r=bulk]");
    b.hidden = !sel.size;
    if (!sel.size) return;
    if (!b.dataset.built) {
      b.dataset.built = "1";
      b.innerHTML = `<b data-r="selcount"></b>
        <select data-b="status"><option value="">Move to…</option>${STATUSES.map(s => `<option>${s}</option>`).join("")}</select>
        <input type="text" class="ref" data-b="ref" placeholder="order ref" style="width:140px">
        <button class="btn sm" data-b="setref">Set order ref</button>
        <span class="grow"></span>
        <button class="btn sm" data-b="del">Remove</button>
        <button class="btn sm" data-b="clear">Clear selection</button>`;
    }
    b.querySelector("[data-r=selcount]").textContent = `${sel.size} selected`;
  }

  // ---------- events ----------
  function numVal(f, v) {
    if (f === "qty") return Math.max(1, parseInt(v, 10) || 1);
    if (f === "actual_buy_eur" || f === "actual_sell_usd") return v === "" ? null : parseFloat(v);
    return v;
  }
  function wire(sec) {
    sec.querySelector("[data-r=status]").addEventListener("click", e => {
      const v = e.target.closest("button")?.dataset.v; if (!v) return;
      filter = v; M.store.set("bl.filter", v);
      sec.querySelectorAll("[data-r=status] button").forEach(b => b.classList.toggle("on", b.dataset.v === v));
      render();
    });
    sec.querySelector("[data-r=group]").onchange = e => { groupBy = e.target.value; M.store.set("bl.group", groupBy); render(); };
    sec.querySelector("[data-r=sort]").onchange = e => { sortBy = e.target.value; M.store.set("bl.sort", sortBy); render(); };

    const body = sec.querySelector(".bl-body");
    body.addEventListener("change", e => {
      const t = e.target, k = t.closest("tr")?.dataset.k;
      if (t.matches("[data-selall]")) {
        viewList().list.forEach(i => t.checked ? sel.add(i.key) : sel.delete(i.key)); render(); return;
      }
      if (t.matches("[data-sel]")) { t.checked ? sel.add(k) : sel.delete(k); render(); return; }
      if (!k || !t.dataset.f) return;
      const v = numVal(t.dataset.f, t.value);
      if (typeof v === "number" && isNaN(v)) { M.toast("Not a number"); return; }
      call("PUT", {key: k, [t.dataset.f]: v});
    });
    body.addEventListener("click", e => {
      const k = e.target.closest("tr")?.dataset.k;
      if (!k) return;
      const it = items.find(i => i.key === k);
      if (e.target.closest("[data-del]")) {
        if (it.status !== "planned" && !confirm(`Remove ${it.name} (${it.status}) from your records?`)) return;
        call("DELETE", {key: k});
      } else if (e.target.closest("[data-open]")) {
        e.preventDefault(); openItem(it);
      }
    });
    body.addEventListener("keydown", e => { if (e.key === "Enter" && e.target.matches("input[data-f]")) e.target.blur(); });
    // big image on thumbnail hover (reuses the scanner's #hover element)
    body.addEventListener("mousemove", e => {
      const h = M.$("#hover"), t = e.target.closest("img.thumb");
      if (!h) return;
      if (!t) { h.style.display = "none"; return; }
      if (h.src !== t.dataset.big) h.src = t.dataset.big;
      h.style.display = "block";
      h.style.left = (e.clientX + 24) + "px"; h.style.top = Math.max(8, Math.min(e.clientY - 40, innerHeight - 360)) + "px";
    });
    body.addEventListener("mouseleave", () => { const h = M.$("#hover"); if (h) h.style.display = "none"; });

    const bulk = sec.querySelector("[data-r=bulk]");
    bulk.addEventListener("change", e => {
      if (e.target.dataset.b !== "status" || !e.target.value) return;
      const s = e.target.value; e.target.value = "";
      call("PUT", {keys: [...sel], status: s}).then(r => r && M.toast(`${r.updated} lot(s) → ${s}`));
    });
    bulk.addEventListener("click", e => {
      const b = e.target.dataset.b;
      if (b === "clear") { sel.clear(); render(); }
      else if (b === "setref") {
        const ref = bulk.querySelector("[data-b=ref]").value.trim();
        call("PUT", {keys: [...sel], order_ref: ref}).then(r => r && M.toast(`Order ref set on ${r.updated} lot(s)`));
      } else if (b === "del") {
        if (!confirm(`Remove ${sel.size} lot(s) from the buy list?`)) return;
        call("DELETE", {keys: [...sel]}).then(r => { if (r) { sel.clear(); render(); M.toast(`Removed ${r.removed}`); } });
      }
    });

    sec.querySelector(".bl-exports").addEventListener("click", e => {
      const x = e.target.closest("[data-x]")?.dataset.x; if (x) doExport(x);
    });
  }

  // Open the scanner's detail drawer with a scan-row-shaped object built from the item.
  function openItem(it) {
    const x = calc(it);
    M.openDrawer({
      ...it, rarity: it.rarity || "", reserved: false,
      cardmarket_eur: x.cur, tcgplayer_usd: it.tcg ?? it.tcgplayer_usd, manapool_usd: it.mp ?? it.manapool_usd,
      cardkingdom_buy_usd: it.priced ? it.ck : it.cardkingdom_buy_usd, ck_qty: it.ck_qty,
      cardconduit_buy_usd: it.priced ? it.cc : it.cardconduit_buy_usd,
      mp_qty: it.mp_qty, sold_on: x.store || "tcgplayer", landed_cost_usd: x.cost, net_sale_usd: x.net ?? 0,
      profit_usd: x.profit ?? 0, roi_pct: x.roi ?? 0,
      scryfall_url: it.scryfall_url || `https://scryfall.com/card/${it.set.toLowerCase()}/${it.number}`,
    });
  }

  // ---------- exports ----------
  function download(name, text, type = "text/plain;charset=utf-8") {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([text], {type}));
    a.download = name; document.body.appendChild(a); a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
  }
  async function copy(text) {
    try { await navigator.clipboard.writeText(text); return true; } catch { return false; }
  }
  function aggregate(list, fmt) { // merge identical lines, summing quantities
    const m = new Map();
    for (const i of list) { const l = fmt(i); m.set(l, (m.get(l) || 0) + i.qty); }
    return [...m];
  }
  const csvCell = v => { const s = v == null ? "" : String(v); return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s; };

  async function doExport(kind) {
    const day = new Date().toISOString().slice(0, 10);
    const pick = def => sel.size ? items.filter(i => sel.has(i.key)) : def;
    if (kind === "csv") {
      const p = M.readFilters();
      const cols = ["key", "card_key", "name", "set", "set_name", "number", "finish", "qty", "condition", "status", "order_ref",
        "cardmarket_eur", "tcgplayer_usd", "manapool_usd", "cardkingdom_buy_usd", "cardconduit_buy_usd", "sold_on", "sell_price_usd", "net_sale_usd", "actual_buy_eur", "actual_sell_usd", "sell_via",
        "eur", "tcg", "mp", "mp_qty", "ck", "ck_qty", "cc", "notes", "created_at", "updated_at",
        "cardmarket_url", "tcgplayer_url", "manapool_url", "cardkingdom_url", "cardconduit_url"];
      const extra = ["landed_cost_usd", "net_sale_usd", "profit_per_copy_usd", "profit_lot_usd"];
      const lines = [cols.concat(extra).join(",")];
      for (const i of pick(items)) {
        const x = calc(i, p), r2 = v => v == null ? "" : v.toFixed(2);
        lines.push(cols.map(k => csvCell(i[k])).concat([r2(x.cost), r2(x.net), r2(x.profit), r2(x.profit == null ? null : x.profit * i.qty)]).join(","));
      }
      download(`buylist-${day}.csv`, "﻿" + lines.join("\r\n") + "\r\n", "text/csv;charset=utf-8");
      M.toast(`Exported ${lines.length - 1} rows`);
      return;
    }
    let list, lines, file, what;
    if (kind === "cm") {
      list = pick(items.filter(i => i.status === "planned"));
      lines = aggregate(list, i => `${i.name} (${i.set_name})`).map(([l, q]) => `${q}x ${l}`);
      file = `cardmarket-wants-${day}.txt`; what = "Cardmarket wants";
    } else if (kind === "tcg") {
      list = pick(items.filter(i => i.status === "received" || i.status === "listed"));
      lines = aggregate(list, i => `${i.name} [${i.set}]`).map(([l, q]) => `${q} ${l}`);
      file = `tcgplayer-mass-entry-${day}.txt`; what = "TCGplayer Mass Entry";
    } else {
      list = pick(viewList().list.filter(i => filter !== "all" || i.status !== "sold"));
      lines = aggregate(list, i => `${i.name} (${i.set}) ${i.number}${i.finish === "foil" ? " *F*" : i.finish === "etched" ? " *E*" : ""}`)
        .map(([l, q]) => `${q} ${l}`);
      file = `manapool-moxfield-${day}.txt`; what = "Mana Pool / Moxfield list";
    }
    if (!lines.length) { M.toast("Nothing to export with the current selection / status"); return; }
    const text = lines.join("\n") + "\n";
    const ok = await copy(text);
    download(file, text);
    M.toast(`${what}: ${lines.length} line(s) downloaded${ok ? " and copied" : ""}`, 3000);
  }

  refresh();
})();
