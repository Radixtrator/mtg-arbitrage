// CardConduit in the card drawer: shows the estimated (or pulled) buylist offer and a button that pulls
// CardConduit's current price for this card. Backend: features/cardconduit.py
(() => {
  const M = window.MTG;
  if (!M) return;
  const {esc, usd, api} = M;
  const when = iso => new Date(iso).toLocaleString(undefined, {month: "short", day: "numeric", hour: "numeric", minute: "2-digit"});

  M.addDrawerSection((r, box) => {
    const render = () => {
      const price = r.cardconduit_buy_usd;
      const basis = r.cardkingdom_buy_usd ? "= Card Kingdom's buylist offer; CardConduit usually pays at least that"
        : "rough: 45% of the lower US market price, since Card Kingdom isn't buying it";
      const net = price != null ? M.netSale(price, "cardconduit") : null;
      const profit = net != null ? net - r.landed_cost_usd : null;
      box.innerHTML = `<h3>CardConduit buylist</h3>
        <div class="note">${
          r.cardconduit_estimated
            ? price != null ? `Estimated <b>~${usd(price)}</b> NM (${basis}).` : "No estimate: no US price to base it on."
            : price != null ? `CardConduit pays <b>${usd(price)}</b> NM (pulled ${esc(when(r.cc_pulled_at))}).`
                            : `CardConduit isn't buying this finish right now (checked ${esc(when(r.cc_pulled_at))}).`}
          ${net != null ? `After your service fee: ${usd(net)}, <b class="${profit >= 0 ? "pos" : "neg"}">${profit < 0 ? "−" : "+"}${usd(Math.abs(profit))}</b> per copy.` : ""}</div>
        <div class="btn-row" style="margin-top:6px">
          <button class="btn sm" data-pull>${r.cardconduit_estimated ? "Pull CardConduit price" : "Pull again"}</button>
          <a class="btn sm" target="_blank" rel="noopener" href="${esc(r.cardconduit_url || "https://cardconduit.com/card-price-check")}">CardConduit ↗</a>
        </div>`;
      box.querySelector("[data-pull]").onclick = pull;
    };
    const pull = async () => {
      const b = box.querySelector("[data-pull]");
      b.disabled = true; b.textContent = "Pulling…";
      try {
        const res = await api("/api/cardconduit/pull", {method: "POST", body: {id: r.id}});
        r.cardconduit_buy_usd = res.prices[r.finish] ?? null;
        r.cardconduit_estimated = false;
        r.cc_pulled_at = res.pulled_at;
        render();
        M.scan();  // re-score: the pulled price can change which store wins and the profit
      } catch (e) {
        M.toast("CardConduit lookup failed: " + e.message);
        b.disabled = false; b.textContent = "Pull CardConduit price";
      }
    };
    render();
  });
})();
