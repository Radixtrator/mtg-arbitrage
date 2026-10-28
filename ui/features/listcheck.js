// "Check a list" tab: paste any card list and see which printings are worth importing.
// Backend: POST /api/listcheck (features/listcheck.py).
(() => {
  const M = window.MTG;
  if (!M) return;
  const {esc, usd, eur, store, api, toast} = M;
  const KEY_TEXT = "listcheck.text", KEY_ONLY = "listcheck.only", KEY_SORT = "listcheck.sort";
  const VIA = {tcgplayer: ["TCG", "tcg", "TCGplayer market"], manapool: ["MP", "mp", "Mana Pool market"],
               cardkingdom: ["CK", "ck", "Card Kingdom buylist"], offer: ["Offer", "lc-offer", "Price offered in your list"]};
  const PLACEHOLDER = `4 Lightning Bolt
1 Ragavan, Nimble Pilferer (MH2) 138 *F*
2x Fable of the Mirror-Breaker
Thoughtseize @ $9.50
Sideboard
1 Force of Will [ALL]`;

  const css = document.createElement("style");
  css.textContent = `
    .lc-input textarea { width: 100%; min-height: 170px; resize: vertical; font: 13px/1.45 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
    .lc-bar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-top: 10px; }
    .lc-bar .grow { flex: 1; }
    .lc-bar label.chk { display: inline-flex; align-items: center; gap: 5px; font-size: 13px; cursor: pointer; user-select: none; }
    .lc-help { margin: 8px 0 0; }
    .lc-help code { background: var(--chip); border-radius: 4px; padding: 0 4px; font-size: 11.5px; }
    table.t tr.lc-dim > td { opacity: .5; }
    table.t tr.lc-dim:hover > td { opacity: .8; }
    table.t td.lc-in { max-width: 260px; overflow: hidden; text-overflow: ellipsis; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; color: var(--muted); }
    table.t td.lc-pr { max-width: 300px; overflow: hidden; text-overflow: ellipsis; }
    table.t tr.lc-sub > td { background: var(--panel-2); padding: 8px 10px 12px 44px; }
    table.t tr.lc-sub table.t { font-size: 12.5px; }
    .lc-x { border: 0; background: none; cursor: pointer; color: var(--muted); padding: 0 4px; font-size: 13px; }
    .lc-x:disabled { visibility: hidden; }
    .lc-sug { border: 1px solid var(--border); cursor: pointer; margin: 0 3px 0 0; }
    .lc-sug:hover { border-color: var(--accent); }
    .lc-offer { color: var(--accent); }
    .lc-tag { font-size: 11px; color: var(--warn); }
    .lc-stats .stat .v.small { font-size: 14px; }
  `;
  document.head.appendChild(css);

  let res = null, busy = false, dirty = false, open = new Set(), view = [];
  let onlyProfitable = store.get(KEY_ONLY, false), sortBy = store.get(KEY_SORT, "input");
  let sec, tab;

  tab = M.addTab({
    id: "listcheck", label: "Check a list",
    render(el) {
      sec = el;
      el.innerHTML = `
        <div class="panel lc-input">
          <label class="lbl" for="lcText" style="display:block">Paste a card list</label>
          <textarea id="lcText" spellcheck="false" placeholder="${esc(PLACEHOLDER)}"></textarea>
          <p class="note lc-help">One card per line: <code>4 Lightning Bolt</code>, <code>4x Name</code>, <code>Name (SET) 123</code>,
            <code>Name [SET]</code>, Moxfield/Arena <code>1 Name (SET) 123 *F*</code>, Cardmarket lines, or a CSV with a
            <code>Name</code> column (plus optional quantity / set / number / foil / offer). End a line with
            <code>@ $12.50</code> (or a tab + price) to use what a US buyer offers instead of market price.
            Section headers and <code>//</code> comments are ignored. Uses your sidebar cost model and storefront; min filters are ignored.</p>
          <div class="lc-bar">
            <button class="btn primary" id="lcRun">Check list</button>
            <button class="btn" id="lcClear">Clear</button>
            <span class="grow"></span>
            <label class="chk"><input type="checkbox" id="lcOnly">Only profitable</label>
            <label class="f" style="display:inline-flex;align-items:center;gap:6px">Order
              <select id="lcSort"><option value="input">As pasted</option><option value="profit">Total profit</option>
                <option value="each">Profit / copy</option><option value="roi">ROI</option></select></label>
          </div>
        </div>
        <div id="lcOut" style="margin-top:14px"></div>`;
      const ta = el.querySelector("#lcText");
      ta.value = store.get(KEY_TEXT, "");
      let t;
      ta.addEventListener("input", () => { clearTimeout(t); t = setTimeout(() => store.set(KEY_TEXT, ta.value), 300); });
      ta.addEventListener("keydown", e => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); run(); } });
      el.querySelector("#lcRun").onclick = run;
      el.querySelector("#lcClear").onclick = () => { ta.value = ""; store.set(KEY_TEXT, ""); res = null; open.clear(); draw(); ta.focus(); };
      const only = el.querySelector("#lcOnly");
      only.checked = onlyProfitable;
      only.onchange = () => { onlyProfitable = only.checked; store.set(KEY_ONLY, onlyProfitable); draw(); };
      const so = el.querySelector("#lcSort");
      so.value = sortBy;
      so.onchange = () => { sortBy = so.value; store.set(KEY_SORT, sortBy); draw(); };
      el.querySelector("#lcOut").addEventListener("click", onClick);
      el.querySelector("#lcOut").addEventListener("mousemove", onHover);
      el.querySelector("#lcOut").addEventListener("mouseleave", () => { const h = M.$("#hover"); if (h) h.style.display = "none"; });
      draw();
    },
    onShow() { if (dirty && res) run(); },
  });

  // Cost model / storefront changed in the sidebar: re-run if we have results.
  let lastParams = "", rerunT;
  M.on("filters", p => {
    if (!res || JSON.stringify(p) === lastParams) return;
    if (sec && !sec.hidden) { clearTimeout(rerunT); rerunT = setTimeout(run, 400); } else dirty = true;
  });

  async function run() {
    const ta = sec.querySelector("#lcText");
    const text = ta.value;
    store.set(KEY_TEXT, text);
    if (!text.trim()) { toast("Paste a card list first"); ta.focus(); return; }
    if (busy) return;
    busy = true; dirty = false;
    const b = sec.querySelector("#lcRun"); b.disabled = true; b.textContent = "Checking…";
    try {
      const params = M.readFilters();
      res = await api("/api/listcheck", {method: "POST", body: {text, params}});
      lastParams = JSON.stringify(params);
      open.clear();
      draw();
    } catch (e) {
      toast("List check failed: " + e.message);
    } finally {
      busy = false; b.disabled = false; b.textContent = "Check list";
    }
  }

  const fin = f => f === "nonfoil" ? "" : ` <span class="lc-tag">${esc(f)}</span>`;
  function sellCell(r) {
    const [lbl, cls, title] = VIA[r.sold_on] || [r.sold_on, "", ""];
    return `<span class="${cls}" title="${esc(title)}">${usd(r.sell_price_usd)} <small>${esc(lbl)}</small></span>`;
  }
  const pcls = (r, ok) => r.profit_usd > 0 ? (ok ? "pos" : "") : "neg";

  function visible() {
    let ls = res.lines.map((l, i) => ({...l, _i: i}));
    if (onlyProfitable) ls = ls.filter(l => l.profitable);
    const k = {profit: l => l.total_profit_usd, each: l => l.best?.profit_usd, roi: l => l.best?.roi_pct}[sortBy];
    if (k) ls.sort((a, b) => (k(b) ?? -1e9) - (k(a) ?? -1e9));
    return ls;
  }

  function draw() {
    const out = sec.querySelector("#lcOut");
    if (tab) tab.setCount(res ? res.totals.profitable : 0);
    if (!res) { out.innerHTML = `<div class="empty">Paste a want list, decklist or buylist above and press <b>Check list</b> (Ctrl+Enter).</div>`; return; }
    const T = res.totals, P = T.profitable_only, A = T.all;
    view = visible();
    out.innerHTML = `
      <div class="stats lc-stats">
        <div class="stat"><div class="v">${T.found}<span class="muted" style="font-size:13px"> / ${T.lines}</span></div><div class="k">Cards found</div></div>
        <div class="stat"><div class="v pos">${T.profitable}</div><div class="k" title="Meets your min profit $${res.thresholds.min_profit} and min ROI ${res.thresholds.min_roi}%">Worth importing</div></div>
        <div class="stat"><div class="v">${eur(P.spend_eur)}</div><div class="k">Spend (${P.copies} profitable copies)</div></div>
        <div class="stat"><div class="v pos">${usd(P.profit_usd)}</div><div class="k">Profit, profitable only</div></div>
        <div class="stat"><div class="v ${A.profit_usd >= 0 ? "" : "neg"}">${usd(A.profit_usd)}</div><div class="k" title="${A.copies} copies, ${eur(A.spend_eur)}">Profit, whole list</div></div>
      </div>
      <div class="btn-row" style="margin-bottom:10px">
        <button class="btn" id="lcAdd" ${T.profitable ? "" : "disabled"}>Add profitable to buy list</button>
        <button class="btn" id="lcCsv">Export CSV</button>
        ${res.truncated ? `<span class="note warn">List truncated to the first ${res.lines.length} lines.</span>` : ""}
      </div>
      ${view.length ? `<div class="tablewrap"><table class="t"><thead><tr>
        <th></th><th></th><th>Input</th><th class="n">Qty</th><th>Matched printing</th>
        <th class="n">CM €</th><th class="n">US $</th><th class="n">Profit / copy</th><th class="n">ROI</th><th class="n">Total</th>
      </tr></thead><tbody>${view.map(rowHtml).join("")}</tbody></table></div>`
      : `<div class="empty">${onlyProfitable ? "No profitable cards in this list." : "No card lines found in the text."}</div>`}`;
    out.querySelector("#lcAdd").onclick = addToBuylist;
    out.querySelector("#lcCsv").onclick = exportCsv;
  }

  function rowHtml(l) {
    const i = l._i, r = l.best;
    const exp = `<button class="lc-x" data-exp="${i}" ${l.alternatives.length ? "" : "disabled"} aria-label="Other printings">${open.has(i) ? "▾" : "▸"}</button>`;
    const inp = `<td class="lc-in" title="${esc(l.input)}">${esc(l.input)}</td>`;
    if (!r) {
      const why = l.status === "no_price" ? `<b>${esc(l.matched)}</b> <span class="muted">— no priced printing</span>`
        : `<span class="neg">Not found</span> ${l.suggestions.length ? `<span class="muted">· did you mean</span> ` +
           l.suggestions.map(s => `<button class="chip lc-sug" data-sug="${esc(s)}" data-li="${i}">${esc(s)}</button>`).join("") : ""}`;
      return `<tr class="lc-dim"><td></td><td></td>${inp}<td class="n">${l.qty}</td><td colspan="6" style="white-space:normal">${why}</td></tr>`;
    }
    const notes = l.notes.length ? ` <span class="warn" title="${esc(l.notes.join("; "))}">⚠</span>` : "";
    let h = `<tr data-li="${i}" class="${l.profitable ? "" : "lc-dim"}">
      <td>${exp}</td>
      <td>${r.image_small ? `<img class="thumb" loading="lazy" src="${esc(r.image_small)}" data-big="${esc(r.image_normal)}" alt="">` : ""}</td>
      ${inp}<td class="n">${l.qty}</td>
      <td class="lc-pr" title="${esc(`${r.name} — ${r.set_name} #${r.number} (${l.printings} printings considered)`)}">${esc(r.name)}
        <span class="muted">${esc(r.set)} #${esc(r.number)}</span>${fin(r.finish)}${notes}</td>
      <td class="n cm">${eur(r.cardmarket_eur)}</td>
      <td class="n">${sellCell(r)}</td>
      <td class="n ${pcls(r, l.profitable)}">${usd(r.profit_usd)}</td>
      <td class="n">${Math.round(r.roi_pct)}%</td>
      <td class="n ${pcls(r, l.profitable)}"><b>${usd(l.total_profit_usd)}</b></td></tr>`;
    if (open.has(i)) {
      h += `<tr class="lc-sub"><td colspan="10"><div class="note" style="margin-bottom:6px">Other printings of ${esc(r.name)} (top ${l.alternatives.length} of ${l.printings - 1})</div>
        <div class="tablewrap"><table class="t"><thead><tr><th></th><th>Set</th><th>Finish</th><th class="n">CM €</th><th class="n">US $</th>
        <th class="n">Profit / copy</th><th class="n">ROI</th></tr></thead><tbody>
        ${l.alternatives.map((a, j) => `<tr data-li="${i}" data-alt="${j}">
          <td>${a.image_small ? `<img class="thumb" loading="lazy" src="${esc(a.image_small)}" data-big="${esc(a.image_normal)}" alt="">` : ""}</td>
          <td title="${esc(a.set_name)}">${esc(a.set)} #${esc(a.number)}${a.suspect ? ` <span class="warn" title="Price ratio ${a.ratio}× / US disagreement beyond your bad-data limits">⚠</span>` : ""}</td><td>${esc(a.finish)}</td>
          <td class="n cm">${eur(a.cardmarket_eur)}</td><td class="n">${sellCell(a)}</td>
          <td class="n ${a.profit_usd > 0 ? "pos" : "neg"}">${usd(a.profit_usd)}</td><td class="n">${Math.round(a.roi_pct)}%</td></tr>`).join("")}
        </tbody></table></div></td></tr>`;
    }
    return h;
  }

  function onClick(e) {
    const sug = e.target.closest("[data-sug]");
    if (sug) { e.stopPropagation(); applySuggestion(res.lines[+sug.dataset.li], sug.dataset.sug); return; }
    const x = e.target.closest("[data-exp]");
    if (x) { e.stopPropagation(); const i = +x.dataset.exp; open.has(i) ? open.delete(i) : open.add(i); draw(); return; }
    const tr = e.target.closest("tr[data-li]");
    if (!tr) return;
    const l = res.lines[+tr.dataset.li];
    const row = tr.dataset.alt != null ? l.alternatives[+tr.dataset.alt] : l.best;
    if (row) M.openDrawer(row);
  }

  function applySuggestion(l, name) {
    const ta = sec.querySelector("#lcText");
    const lines = ta.value.replace(/\r\n?/g, "\n").split("\n");
    const n = l.line - 1;
    let s = lines[n];
    if (s == null || s.trim() !== l.input.trim()) {  // text was edited since: find the line again
      const k = lines.findIndex(x => x.trim() === l.input.trim());
      if (k < 0) { toast("That line is no longer in the list"); return; }
      return applySuggestion({...l, line: k + 1}, name);
    }
    const at = s.indexOf(l.query) >= 0 ? s.indexOf(l.query) : s.toLowerCase().indexOf(l.query.toLowerCase());
    // CSV cells holding a comma need quotes
    const csvish = /^[^\t]*,/.test(s) && !/^\s*\d+x?\s/.test(s) && name.includes(",") ? `"${name}"` : name;
    lines[n] = at >= 0 ? s.slice(0, at) + csvish + s.slice(at + l.query.length) : `${l.qty} ${name}`;
    ta.value = lines.join("\n");
    store.set(KEY_TEXT, ta.value);
    run();
  }

  function onHover(e) {
    const h = M.$("#hover");
    if (!h) return;
    const t = e.target.closest("img.thumb");
    if (!t) { h.style.display = "none"; return; }
    if (h.src !== t.dataset.big) h.src = t.dataset.big;
    h.style.display = "block";
    const y = Math.min(e.clientY - 40, innerHeight - 360);
    h.style.left = Math.min(e.clientX + 24, innerWidth - 260) + "px"; h.style.top = Math.max(8, y) + "px";
  }

  async function addToBuylist() {
    const items = res.lines.filter(l => l.profitable && l.best).map(l => ({row: l.best, qty: l.qty}));
    if (!items.length) { toast("No profitable cards to add"); return; }
    let r;
    try {
      r = await fetch("/api/buylist", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({items})});
    } catch (e) { toast("Could not reach the server"); return; }
    if (r.status === 404) { toast("Buy list feature is not installed"); return; }
    const data = await r.json().catch(() => ({}));
    if (!r.ok) { toast("Buy list: " + (data.error || r.statusText)); return; }
    window.dispatchEvent(new CustomEvent("buylist:changed"));
    const copies = items.reduce((s, x) => s + x.qty, 0);
    toast(`Added ${items.length} card${items.length === 1 ? "" : "s"} (${copies} cop${copies === 1 ? "y" : "ies"}) to the buy list`);
  }

  function exportCsv() {
    const cols = ["input", "qty", "status", "card", "set", "set_name", "number", "finish", "cardmarket_eur", "sell_usd", "sold_on",
                  "landed_cost_usd", "net_sale_usd", "profit_per_copy_usd", "roi_pct", "total_profit_usd", "profitable",
                  "printings", "notes", "suggestions", "cardmarket_url"];
    const q = v => { const s = v == null ? "" : String(v); return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s; };
    const lines = (onlyProfitable ? res.lines.filter(l => l.profitable) : res.lines).map(l => {
      const r = l.best || {};
      return [l.input, l.qty, l.status, l.matched, r.set, r.set_name, r.number, r.finish, r.cardmarket_eur, r.sell_price_usd,
              r.sold_on, r.landed_cost_usd, r.net_sale_usd, r.profit_usd, r.roi_pct, l.best ? l.total_profit_usd : "",
              l.best ? (l.profitable ? "yes" : "no") : "", l.printings, l.notes.join("; "), l.suggestions.join(" | "),
              r.cardmarket_url].map(q).join(",");
    });
    const blob = new Blob(["﻿" + [cols.join(","), ...lines].join("\r\n")], {type: "text/csv;charset=utf-8"});
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `list-check-${new Date().toISOString().slice(0, 10)}.csv`;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }
})();
