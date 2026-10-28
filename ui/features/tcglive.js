// Live TCGplayer price points (market + median listing, both finishes) in the card drawer.
// Backend: features/tcglive.py
(() => {
  const M = window.MTG;
  if (!M) return;
  const {esc, usd, api} = M;
  const PRINTING = {nonfoil: "Normal", foil: "Foil", etched: "Foil"};  // etched products list as "Foil"
  const LABEL = {Normal: "Non-foil", Foil: "Foil"};

  const css = document.createElement("style");
  css.textContent = `
    .tl-t { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; font-size: 13px; margin: 4px 0 8px; }
    .tl-t th { font-size: 11px; color: var(--muted); text-transform: uppercase; font-weight: 500; text-align: right; padding: 2px 6px; }
    .tl-t th:first-child, .tl-t td:first-child { text-align: left; }
    .tl-t td { text-align: right; padding: 3px 6px; border-top: 1px solid var(--border); }
    .tl-t tr.tl-this td { font-weight: 600; }
    .tl-t .tl-tag { font-size: 11px; font-weight: 500; color: var(--muted); margin-left: 4px; }
    .tl-warn { font-size: 12px; color: var(--warn); margin: 4px 0; }
  `;
  document.head.appendChild(css);

  const productId = r => r.tcgplayer_id || (String(r.tcgplayer_url || "").match(/tcgplayer\.com\/product\/(\d+)/) || [])[1];

  M.addDrawerSection(async (r, box) => {
    const pid = productId(r);
    if (!pid) return;
    const printing = PRINTING[r.finish];
    box.innerHTML = `<h3>TCGplayer right now</h3><div class="note">Loading…</div>`;
    let res;
    try { res = await api(`/api/tcglive?product=${pid}`); }
    catch (e) { box.querySelector(".note").textContent = "Live TCGplayer prices unavailable: " + e.message; return; }

    const pts = res.printings || {};
    const order = ["Normal", "Foil"].filter(k => pts[k] && (pts[k].market != null || pts[k].listed_median != null));
    if (!order.length) { box.querySelector(".note").textContent = "TCGplayer has no prices for this product."; return; }
    const me = pts[printing] || {};

    const notes = [];
    const {market, listed_median: median} = me;
    if (market && median && median > market * 1.5)
      notes.push(`Current listings sit ${Math.round((median / market - 1) * 100)}% above the market price — few recent sales, so expect a slow sale.`);
    if (market && median && median < market * 0.8)
      notes.push(`Current listings are ${Math.round((1 - median / market) * 100)}% below the market price — the market price is probably stale.`);
    const other = pts[printing === "Foil" ? "Normal" : "Foil"];
    const ref = market || median, oref = other && (other.market || other.listed_median);
    if (ref && oref && Math.max(ref, oref) / Math.min(ref, oref) >= 2)
      notes.push(`${LABEL[printing]} and ${LABEL[printing === "Foil" ? "Normal" : "Foil"].toLowerCase()} prices differ ${(Math.max(ref, oref) / Math.min(ref, oref)).toFixed(1)}× — only compare ${LABEL[printing].toLowerCase()} listings.`);

    let profitLine = "";
    if (median && r.sold_on !== "offer") {
      const pr = M.netSale(median, "tcgplayer") - r.landed_cost_usd;
      profitLine = `<div class="note">Selling at the median listing (${usd(median)}) under your current fee settings:
        <b class="${pr >= 0 ? "pos" : "neg"}">${pr < 0 ? "−" : "+"}${usd(Math.abs(pr))}</b> per copy.</div>`;
    }

    box.innerHTML = `<h3>TCGplayer right now</h3>
      <table class="tl-t"><thead><tr><th></th><th>Market</th><th>Median listing</th></tr></thead><tbody>
        ${order.map(k => `<tr class="${k === printing ? "tl-this" : ""}"><td>${LABEL[k]}${k === printing ? `<span class="tl-tag">this card</span>` : ""}</td>
          <td>${usd(pts[k].market)}</td><td>${usd(pts[k].listed_median)}</td></tr>`).join("")}
      </tbody></table>
      ${notes.map(n => `<div class="tl-warn">⚠ ${esc(n)}</div>`).join("")}
      ${profitLine}
      ${r.tcgplayer_url ? `<div class="btn-row" style="margin-top:6px"><a class="btn sm" target="_blank" rel="noopener" href="${esc(r.tcgplayer_url)}">${esc(LABEL[printing])} listings &amp; recent sales on TCGplayer ↗</a></div>` : ""}`;
  });
})();
