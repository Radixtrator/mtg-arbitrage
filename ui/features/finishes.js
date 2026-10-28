// TCGplayer market price for every finish of the opened printing, from the daily Scryfall data
// (no extra requests). Warns when finishes differ a lot, so foil and non-foil listings aren't mixed up.
(() => {
  const M = window.MTG;
  if (!M) return;
  const {esc, usd} = M;
  const LABEL = {nonfoil: "Non-foil", foil: "Foil", etched: "Etched"};

  const css = document.createElement("style");
  css.textContent = `
    .fn-t { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; font-size: 13px; margin: 4px 0 8px; }
    .fn-t th { font-size: 11px; color: var(--muted); text-transform: uppercase; font-weight: 500; text-align: right; padding: 2px 6px; }
    .fn-t th:first-child, .fn-t td:first-child { text-align: left; }
    .fn-t td { text-align: right; padding: 3px 6px; border-top: 1px solid var(--border); }
    .fn-t tr.fn-this td { font-weight: 600; }
    .fn-t .fn-tag { font-size: 11px; font-weight: 500; color: var(--muted); margin-left: 4px; }
    .fn-warn { font-size: 12px; color: var(--warn); margin: 4px 0; }
  `;
  document.head.appendChild(css);

  M.addDrawerSection((r, box) => {
    const prices = r.tcg_finishes || {};
    const fins = Object.keys(LABEL).filter(f => prices[f]);
    if (!fins.length && !r.tcgplayer_url) return;

    const me = prices[r.finish];
    const notes = fins.filter(f => f !== r.finish && me).map(f => {
      const x = Math.max(me, prices[f]) / Math.min(me, prices[f]);
      return x >= 2 ? `${LABEL[r.finish]} and ${LABEL[f].toLowerCase()} prices differ ${x.toFixed(1)}× — only compare ${LABEL[r.finish].toLowerCase()} listings.` : "";
    }).filter(Boolean);

    box.innerHTML = `<h3>TCGplayer by finish</h3>
      ${fins.length > 1 ? `<table class="fn-t"><thead><tr><th></th><th>Market</th></tr></thead><tbody>
        ${fins.map(f => `<tr class="${f === r.finish ? "fn-this" : ""}"><td>${LABEL[f]}${f === r.finish ? `<span class="fn-tag">this card</span>` : ""}</td>
          <td>${usd(prices[f])}</td></tr>`).join("")}
      </tbody></table>` : `<div class="note">TCGplayer lists only the ${LABEL[r.finish].toLowerCase()} version of this printing.</div>`}
      ${notes.map(n => `<div class="fn-warn">⚠ ${esc(n)}</div>`).join("")}
      ${r.tcgplayer_url ? `<div class="btn-row" style="margin-top:6px"><a class="btn sm" target="_blank" rel="noopener" href="${esc(r.tcgplayer_url)}">${esc(LABEL[r.finish])} listings &amp; recent sales on TCGplayer ↗</a></div>` : ""}`;
  });
})();
