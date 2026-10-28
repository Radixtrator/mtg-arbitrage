// Price history + trends: drawer chart and a "Trends" tab. Backend: features/history.py
(() => {
  const M = window.MTG;
  if (!M) return;
  const {esc, usd, eur, api} = M;
  const SVGNS = "http://www.w3.org/2000/svg";
  const SERIES = [
    {k: "eur_usd", label: "Cardmarket (USD)", color: "var(--c-cm)"},
    {k: "tcg", label: "TCGplayer", color: "var(--c-tcg)"},
    {k: "mp", label: "Mana Pool", color: "var(--c-mp)"},
    {k: "ck", label: "CK buylist", color: "var(--c-ck, #0c8599)"},
  ];
  const STORE = {tcgplayer: "TCGplayer", manapool: "Mana Pool", cardkingdom: "Card Kingdom buylist"};
  const pct = v => v == null ? "–" : v > 999 ? "+999%+" : `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toFixed(Math.abs(v) < 10 ? 1 : 0)}%`;
  const signed = v => v == null ? "–" : (v < 0 ? "−" : "+") + usd(Math.abs(v));
  const money = v => v == null ? "–" : v < 0 ? "−" + usd(-v) : usd(v);
  const cls = (v, goodUp = true) => v == null || Math.abs(v) < 1e-9 ? "" : (v > 0) === goodUp ? "pos" : "neg";
  const shortDate = d => new Date(d + "T00:00:00Z").toLocaleDateString(undefined, {month: "short", day: "numeric", timeZone: "UTC"});
  const filterQS = extra => {
    const p = M.readFilters();
    for (const k of Object.keys(p)) if (p[k] === "" || p[k] == null) delete p[k];
    return new URLSearchParams({...p, ...extra}).toString();
  };

  // A few CSS rules this feature needs (scoped with .hx-)
  const css = document.createElement("style");
  css.textContent = `
    .hx-chart { position: relative; margin-top: 4px; }
    .hx-chart svg { display: block; width: 100%; height: auto; overflow: visible; }
    .hx-chart text { fill: var(--muted); font-size: 10.5px; font-variant-numeric: tabular-nums; }
    .hx-tip { position: absolute; pointer-events: none; background: var(--panel); border: 1px solid var(--border);
      border-radius: 8px; padding: 6px 8px; font-size: 12px; box-shadow: var(--shadow); white-space: nowrap;
      font-variant-numeric: tabular-nums; z-index: 2; }
    .hx-tip b { display: block; margin-bottom: 2px; }
    .hx-legend { display: flex; flex-wrap: wrap; gap: 4px 12px; font-size: 12px; margin: 2px 0 6px; }
    .hx-legend span::before { content: ""; display: inline-block; width: 10px; height: 3px; border-radius: 2px;
      background: currentColor; vertical-align: middle; margin-right: 5px; }
    .hx-legend .lbl2 { color: var(--text); }
    .hx-chip { background: var(--chip); border-radius: 999px; padding: 0 6px; font-size: 11px; margin-left: 4px; color: var(--pos); font-weight: 600; }
    .hx-spark { display: block; }
    .hx-sub { font-size: 11px; color: var(--muted); }
    .hx-bf { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-bottom: 14px; }
    .hx-bf progress { width: 160px; }
    @media (max-width: 820px) { .hx-hide-sm { display: none; } }
  `;
  document.head.appendChild(css);

  function el(tag, attrs = {}, parent) {
    const e = document.createElementNS(SVGNS, tag);
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
    if (parent) parent.appendChild(e);
    return e;
  }
  function niceTicks(lo, hi, n = 4) {
    if (hi <= lo) { hi = lo + 1; lo = Math.max(0, lo - 1); }
    const raw = (hi - lo) / n, mag = 10 ** Math.floor(Math.log10(raw));
    const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => s >= raw);
    const start = Math.floor(lo / step) * step, out = [];
    for (let v = start; v <= hi + step * 0.5; v += step) out.push(+v.toFixed(10));
    return out;
  }
  const fmtAxis = v => v >= 10000 ? "$" + Math.round(v / 1000) + "k" : v >= 1000 ? "$" + (v / 1000).toFixed(1) + "k" : "$" + (v >= 100 || v === Math.round(v) ? Math.round(v) : v.toFixed(2));
  // SVG path through points, breaking the line at gaps (null)
  function pathFor(vals, x, y) {
    let d = "", pen = false;
    vals.forEach((v, i) => {
      if (v == null) { pen = false; return; }
      d += `${pen ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`;
      pen = true;
    });
    return d;
  }

  // ---------------------------------------------------------------- drawer chart
  function drawChart(box, series) {
    const W = Math.max(280, Math.round(box.clientWidth || 500)), H = 210;
    const pad = {l: 46, r: 10, t: 8, b: 22};
    const shown = SERIES.filter(s => series.some(p => p[s.k] != null));
    const all = series.flatMap(p => shown.map(s => p[s.k])).filter(v => v != null);
    let lo = Math.min(...all), hi = Math.max(...all);
    const span = hi - lo || hi * 0.1 || 1;
    lo = Math.max(0, lo - span * 0.08); hi = hi + span * 0.08;
    const ticks = niceTicks(lo, hi);
    lo = Math.min(lo, ticks[0]); hi = Math.max(hi, ticks[ticks.length - 1]);
    const n = series.length;
    const x = i => pad.l + (n === 1 ? 0 : i * (W - pad.l - pad.r) / (n - 1));
    const y = v => pad.t + (1 - (v - lo) / (hi - lo)) * (H - pad.t - pad.b);

    box.innerHTML = `<div class="hx-legend">${shown.map(s => `<span style="color:${s.color}"><span class="lbl2">${s.label}</span></span>`).join("")}</div>`;
    const wrap = document.createElement("div"); wrap.className = "hx-chart"; box.appendChild(wrap);
    const svg = el("svg", {viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Price history chart"}, wrap);
    for (const t of ticks) {
      if (t < lo - 1e-9 || t > hi + 1e-9) continue;
      el("line", {x1: pad.l, x2: W - pad.r, y1: y(t), y2: y(t), stroke: "var(--border)", "stroke-width": 1}, svg);
      el("text", {x: pad.l - 6, y: y(t) + 3.5, "text-anchor": "end"}, svg).textContent = fmtAxis(t);
    }
    const xl = n > 2 ? [0, Math.floor((n - 1) / 2), n - 1] : [0, n - 1];
    xl.forEach((i, j) => el("text", {x: x(i), y: H - 6, "text-anchor": j === 0 ? "start" : j === xl.length - 1 ? "end" : "middle"}, svg)
      .textContent = shortDate(series[i].date));
    for (const s of shown) {
      const vals = series.map(p => p[s.k]);
      el("path", {d: pathFor(vals, x, y), fill: "none", stroke: s.color, "stroke-width": 1.8, "stroke-linejoin": "round", "stroke-linecap": "round"}, svg);
      vals.forEach((v, i) => {  // isolated points would be invisible as a path
        if (v != null && vals[i - 1] == null && vals[i + 1] == null) el("circle", {cx: x(i), cy: y(v), r: 2, fill: s.color}, svg);
      });
    }
    const guide = el("line", {y1: pad.t, y2: H - pad.b, stroke: "var(--muted)", "stroke-dasharray": "3 3", visibility: "hidden"}, svg);
    const dots = shown.map(s => el("circle", {r: 3.5, fill: s.color, stroke: "var(--panel)", "stroke-width": 1.5, visibility: "hidden"}, svg));
    const tip = document.createElement("div"); tip.className = "hx-tip"; tip.hidden = true; wrap.appendChild(tip);

    const move = ev => {
      const r = svg.getBoundingClientRect();
      const px = ((ev.touches ? ev.touches[0].clientX : ev.clientX) - r.left) * W / r.width;
      const i = Math.max(0, Math.min(n - 1, Math.round((px - pad.l) / ((W - pad.l - pad.r) / Math.max(1, n - 1)))));
      const p = series[i];
      guide.setAttribute("x1", x(i)); guide.setAttribute("x2", x(i)); guide.setAttribute("visibility", "visible");
      shown.forEach((s, j) => {
        const v = p[s.k];
        dots[j].setAttribute("visibility", v == null ? "hidden" : "visible");
        if (v != null) { dots[j].setAttribute("cx", x(i)); dots[j].setAttribute("cy", y(v)); }
      });
      tip.innerHTML = `<b>${esc(p.date)}</b>` + shown.map(s => `<div><span style="color:${s.color}">●</span> ${s.label}: ${
        s.k === "eur_usd" ? `${usd(p.eur_usd)} <span class="muted">(${eur(p.eur)})</span>` : usd(p[s.k])}</div>`).join("") +
        (p.profit != null ? `<div class="${p.ok ? "pos" : "muted"}">Profit ${usd(p.profit)}${p.ok ? " ✓" : ""}</div>` : "");
      tip.hidden = false;
      const left = x(i) * r.width / W, tw = tip.offsetWidth;
      tip.style.left = Math.max(0, Math.min(r.width - tw, left > r.width / 2 ? left - tw - 12 : left + 12)) + "px";
      tip.style.top = "4px";
    };
    const leave = () => { tip.hidden = true; guide.setAttribute("visibility", "hidden"); dots.forEach(d => d.setAttribute("visibility", "hidden")); };
    svg.addEventListener("mousemove", move);
    svg.addEventListener("touchmove", move, {passive: true});
    svg.addEventListener("touchstart", move, {passive: true});
    svg.addEventListener("mouseleave", leave);
    svg.addEventListener("touchend", leave);
  }

  M.addDrawerSection(async (r, box) => {
    box.innerHTML = `<h3>Price history</h3><div class="note">Loading history…</div>`;
    const store = r.sold_on || "tcgplayer";
    let res;
    try { res = await api(`/api/history?${filterQS({id: r.id, finish: r.finish, store})}`); }
    catch (e) { box.querySelector(".note").textContent = "History unavailable: " + e.message; return; }
    const series = res.series || [];
    if (series.length < 2) {
      box.innerHTML = `<h3>Price history</h3><div class="note">History starts today — check back tomorrow${
        series.length ? "" : " (no snapshot for this printing yet)"}. Or backfill ~90 days from MTGJSON in the Trends tab.</div>`;
      return;
    }
    box.innerHTML = `<h3>Price history</h3><div class="hx-box"></div><div class="note hx-line"></div>`;
    drawChart(box.querySelector(".hx-box"), series);
    const s = res.summary;
    const span = Math.round((Date.parse(s.last) - Date.parse(s.first)) / 864e5) + 1;
    box.querySelector(".hx-line").innerHTML =
      `Spread has existed <b class="${s.days_ok / s.days >= 0.7 ? "pos" : s.days_ok / s.days < 0.3 ? "neg" : ""}">${s.days_ok} of the last ${s.days} days</b>` +
      ` with data${span > s.days ? ` (${span}-day span)` : ""} — profitable selling on ${esc(STORE[s.store] || s.store)} under your current filters.`;
  });

  // ---------------------------------------------------------------- sparkline
  function sparkline(vals) {
    const W = 96, H = 26, p = 2;
    const nums = vals.filter(v => v != null);
    if (nums.length < 2) return `<span class="hx-sub">—</span>`;
    let lo = Math.min(...nums), hi = Math.max(...nums);
    if (hi - lo < Math.max(1e-6, Math.abs(hi) * 0.01)) { lo -= Math.abs(lo) * 0.05 + 0.5; hi += Math.abs(hi) * 0.05 + 0.5; }
    const n = vals.length;
    const x = i => p + i * (W - 2 * p) / (n - 1), y = v => p + (1 - (v - lo) / (hi - lo)) * (H - 2 * p);
    const first = nums[0], last = nums[nums.length - 1];
    const color = last >= first ? "var(--pos)" : "var(--neg)";
    const zero = lo < 0 && hi > 0 ? `<line x1="0" x2="${W}" y1="${y(0).toFixed(1)}" y2="${y(0).toFixed(1)}" stroke="var(--border)" stroke-dasharray="2 2"/>` : "";
    return `<svg class="hx-spark" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" aria-hidden="true">${zero}
      <path d="${pathFor(vals, x, y)}" fill="none" stroke="${color}" stroke-width="1.5" stroke-linejoin="round"/>
      <circle cx="${x(n - 1).toFixed(1)}" cy="${y(last).toFixed(1)}" r="2.2" fill="${color}"/></svg>`;
  }

  // ---------------------------------------------------------------- Trends tab
  const ORDERS = [["widened", "Spread widened"], ["cm_drop", "Cardmarket dropping"], ["consistent", "Most consistent"]];
  const DAYS = [3, 7, 14, 30, 90];
  const PAGE = 100;
  const st = Object.assign({order: "widened", days: 7}, M.store.get("trends", {}));
  let sec, rows = [], limit = PAGE, dirty = true, seq = 0, timer, polling = null;

  function renderShell(s) {
    sec = s;
    s.innerHTML = `
      <div class="toolbar">
        <div class="seg" id="hxOrder">${ORDERS.map(([v, l]) => `<button data-v="${v}">${l}</button>`).join("")}</div>
        <span class="grow"></span>
        <label class="f" style="display:flex;align-items:center;gap:6px">Compare with
          <div class="seg" id="hxDays">${DAYS.map(d => `<button data-v="${d}">${d}d</button>`).join("")}</div></label>
      </div>
      <div class="stats">
        <div class="stat"><div class="v" id="hxTotal">–</div><div class="k">Opportunities now</div></div>
        <div class="stat"><div class="v" id="hxNew">–</div><div class="k">New since then</div></div>
        <div class="stat"><div class="v" id="hxThen">–</div><div class="k">Compared with</div></div>
        <div class="stat"><div class="v" id="hxHist">–</div><div class="k">Days of history</div></div>
      </div>
      <div class="hx-bf panel" id="hxBf" hidden></div>
      <p class="note" style="margin-top:0">Spread = US price on the store you sell to − Cardmarket price in USD at that day's ECB rate.
        "Profitable days" counts days the card passed your current min profit / ROI filters.</p>
      <div id="hxResults"><div class="empty">Loading…</div></div>
      <div class="more"><button class="btn" id="hxMore" hidden>Show more</button></div>`;
    s.querySelector("#hxOrder").addEventListener("click", e => {
      const v = e.target.closest("button")?.dataset.v; if (!v) return;
      st.order = v; save(); query();
    });
    s.querySelector("#hxDays").addEventListener("click", e => {
      const v = e.target.closest("button")?.dataset.v; if (!v) return;
      st.days = +v; save(); query();
    });
    s.querySelector("#hxMore").onclick = () => { limit += PAGE; query(); };
    const res = s.querySelector("#hxResults");
    res.addEventListener("click", e => {
      const tr = e.target.closest("tr[data-i]");
      if (tr) M.openDrawer(rows[+tr.dataset.i]);
    });
    const hover = document.getElementById("hover");
    res.addEventListener("mousemove", e => {
      const t = e.target.closest("img.thumb");
      if (!hover) return;
      if (!t) { hover.style.display = "none"; return; }
      if (hover.src !== t.dataset.big) hover.src = t.dataset.big;
      hover.style.display = "block";
      hover.style.left = (e.clientX + 24) + "px";
      hover.style.top = Math.max(8, Math.min(e.clientY - 40, innerHeight - 360)) + "px";
    });
    res.addEventListener("mouseleave", () => { if (hover) hover.style.display = "none"; });
    syncSegs();
    refreshStatus();
  }
  function save() { M.store.set("trends", {order: st.order, days: st.days}); syncSegs(); }
  function syncSegs() {
    sec.querySelectorAll("#hxOrder button").forEach(b => b.classList.toggle("on", b.dataset.v === st.order));
    sec.querySelectorAll("#hxDays button").forEach(b => b.classList.toggle("on", +b.dataset.v === st.days));
  }
  const visible = () => sec && !sec.hidden;

  async function query() {
    if (!sec) return;
    dirty = false;
    const my = ++seq;
    const box = sec.querySelector("#hxResults");
    if (!rows.length) box.innerHTML = `<div class="empty">Loading…</div>`;
    let res;
    try { res = await api(`/api/history/movers?${filterQS({days: st.days, limit, order: st.order})}`); }
    catch (e) { if (my === seq) box.innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
    if (my !== seq) return;
    rows = res.rows;
    sec.querySelector("#hxTotal").textContent = res.total.toLocaleString();
    sec.querySelector("#hxNew").textContent = res.then_date ? res.new_count.toLocaleString() : "–";
    sec.querySelector("#hxThen").textContent = res.then_date ? shortDate(res.then_date) : "–";
    sec.querySelector("#hxHist").textContent = res.dates.length;
    sec.querySelector("#hxMore").hidden = rows.length >= res.total;
    if (!res.then_date) {
      box.innerHTML = `<div class="empty">History starts today — check back tomorrow, or backfill ~90 days from MTGJSON above.</div>`;
      return;
    }
    if (!rows.length) { box.innerHTML = `<div class="empty">No cards match these filters.</div>`; return; }
    const daysLbl = `${res.dates.length}d`;
    box.innerHTML = `<div class="tablewrap"><table class="t"><thead><tr>
      <th></th><th>Card</th><th class="hx-hide-sm">Set</th><th class="hx-hide-sm">Via</th>
      <th class="n">CM €</th><th class="n">US $</th>
      <th class="n" title="Spread on ${esc(res.then_date)}">Spread then</th><th class="n">Spread now</th><th class="n">Δ spread</th>
      <th class="n" title="Days the card was profitable under your filters, out of days with data">Profitable</th>
      <th>Spread, ${daysLbl}</th><th class="n">Profit $</th>
      </tr></thead><tbody>${rows.map((r, i) => `<tr data-i="${i}">
        <td>${r.image_small ? `<img class="thumb" loading="lazy" src="${esc(r.image_small)}" data-big="${esc(r.image_normal)}" alt="">` : ""}</td>
        <td>${esc(r.name)}${r.finish !== "nonfoil" ? " ✦" : ""}${r.new ? `<span class="hx-chip" title="Not profitable (or not tracked) on ${esc(res.then_date)}">new</span>` : ""}
          <div class="hx-sub">${esc(r.finish)}</div></td>
        <td class="hx-hide-sm" title="${esc(r.set_name)}">${esc(r.set)} #${esc(r.number)}</td>
        <td class="hx-hide-sm">${esc(STORE[r.sold_on] || r.sold_on)}</td>
        <td class="n"><span class="cm">${eur(r.cardmarket_eur)}</span><div class="hx-sub ${cls(r.cm_change_pct, false)}">${pct(r.cm_change_pct)}</div></td>
        <td class="n">${usd(r.sell_price_usd)}<div class="hx-sub ${cls(r.us_change_pct)}">${pct(r.us_change_pct)}</div></td>
        <td class="n">${r.spread_then == null ? `<span class="muted">–</span>` : money(r.spread_then)}</td>
        <td class="n">${money(r.spread_now)}</td>
        <td class="n ${cls(r.spread_change)}">${signed(r.spread_change)}</td>
        <td class="n">${r.days_ok}/${r.days_tracked}</td>
        <td>${sparkline(r.spark)}</td>
        <td class="n pos">${usd(r.profit_usd)}</td>
      </tr>`).join("")}</tbody></table></div>`;
  }

  // ---- backfill status / button
  async function refreshStatus() {
    if (!sec) return;
    let s;
    try { s = await api("/api/history/status"); } catch { return; }
    const box = sec.querySelector("#hxBf");
    const b = s.backfill;
    const has = s.sources && s.sources.mtgjson;
    box.hidden = false;
    if (b.running) {
      const pctDone = b.total ? Math.min(100, Math.round(b.done / b.total * 100)) : null;
      box.innerHTML = `<b>Backfilling from MTGJSON…</b>
        ${pctDone != null ? `<progress max="100" value="${pctDone}"></progress>` : `<progress></progress>`}
        <span class="note">${esc(b.msg)}</span>`;
      if (!polling) polling = setInterval(refreshStatus, 2000);
      return;
    }
    if (polling) {
      clearInterval(polling); polling = null;
      if (b.error) M.toast("Backfill failed: " + b.error, 4000);
      else { M.toast(b.msg || "Backfill done"); query(); }
    }
    const info = `${s.dates} day${s.dates === 1 ? "" : "s"} of history${s.first ? ` (${shortDate(s.first)} – ${shortDate(s.last)})` : ""} · ${s.db_mb} MB`;
    box.innerHTML = `<span class="note">${esc(info)}${b.error ? ` · <span class="neg">last backfill failed: ${esc(b.error)}</span>` : ""}</span>
      <span class="grow" style="flex:1"></span>
      <button class="btn sm" id="hxBfBtn" title="Downloads ~65 MB from mtgjson.com (cached in .cache/mtgjson), ~1 min, ~300 MB on disk">
        ${has ? "Re-run MTGJSON backfill" : "Backfill 90 days from MTGJSON"}</button>`;
    box.querySelector("#hxBfBtn").onclick = async () => {
      try { await api("/api/history/backfill", {method: "POST", body: {days: 90}}); }
      catch (e) { M.toast("Backfill: " + e.message); }
      refreshStatus();
    };
  }

  M.addTab({
    id: "trends", label: "Trends", render: renderShell,
    onShow() { if (dirty) { limit = PAGE; query(); } refreshStatus(); },
  });
  M.on("filters", () => {
    dirty = true;
    if (visible()) { clearTimeout(timer); timer = setTimeout(() => { limit = PAGE; query(); }, 300); }
  });
})();
