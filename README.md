# MTG Arbitrage

Finds Magic cards that are cheap on **Cardmarket (EU)** and sell for more in the **US** on TCGplayer or Mana Pool, or to Card Kingdom's buylist. It accounts for fees, shipping and your own cost model.

It's built for this workflow: order on Cardmarket to an address in Germany, carry the cards to the US in your luggage, then sell or trade them there.

## Quick start
```bash
python3 app.py
```
Open http://localhost:8765. The first start downloads about 200 MB of price data into `.cache/`. Later starts within the same day are fast. There's also a command-line version:
```bash
python3 mtg_arb.py --store best --max-usd 150 --csv out.csv
python3 mtg_arb.py --help
```
It uses only the Python standard library: there's nothing to install and no API keys.

## Data sources
All sources are matched on the **Scryfall card ID**, which identifies one exact printing and finish. No fuzzy name matching is involved.

| Source | What it gives | Refreshed |
|---|---|---|
| [Scryfall bulk data](https://scryfall.com/docs/api/bulk-data) | Cardmarket trend € and TCGplayer market $, plus images and metadata | daily |
| [Mana Pool prices](https://manapool.com/api/v1/prices/singles) | Mana Pool market $ and number of listings | daily cache |
| [Card Kingdom pricelist](https://api.cardkingdom.com/api/v2/pricelist) | Card Kingdom's **buylist** cash offer | daily cache |
| [Cardmarket price guide](https://downloads.s3.cardmarket.com/productCatalog/priceGuide/price_guide_1.json) | Cardmarket trend € for printings Scryfall has no price for | daily cache |
| [frankfurter.app](https://www.frankfurter.app) | EUR→USD rate (ECB) | each start |

Some printings exist as two cards on Scryfall but one product on Cardmarket. Examples are Secret Lair rainbow foils (`#1596★`), 7th–9th Edition foils and Warhammer 40K surge foils. Their Cardmarket price is the foil price of the regular product, and the card panel says so.

Card images come straight from Scryfall's image CDN. The card detail panel also fetches live prices from the Scryfall API, staying under their limit of 10 requests per second.

## How profit is calculated (per copy)
```
landed = CM€ × fx × (1 + buy markup) × (1 + duty) + per-card cost
net    = depends on where you sell (below)
profit = net − landed
```
| Sell on | After fees | Market | Store credit |
|---|---|---|---|
| TCGplayer | price × sell-at% × (1 − 13.25%) − $0.30 | market price | market × credit% |
| Mana Pool | price × sell-at% − min(5%, $50) − 2.9% − $0.30 | market price | market × credit% |
| Card Kingdom buylist | cash offer (no fees) | cash offer | cash × (1 + 30% bonus) |
| Best | whichever of the above nets the most for that card | | |

All of these numbers can be changed in the sidebar or with CLI flags. Import duty defaults to 0%, because the cards travel in your luggage.

## Tabs
- **Scanner:** every opportunity, in a grid or table, with card images.
  - Click a card to see live Scryfall prices, a price history chart and links to each store.
  - **TCGplayer right now:** the live market price and median listing for both foil and non-foil, and the profit if you sell at the median listing.
    - It warns when the two finishes differ a lot, or when listings are far from the market price.
    - Individual listings and recent sales can't be fetched, because TCGplayer blocks outside requests for them. The button opens TCGplayer's page filtered to the right finish instead.
  - Store links open filtered to English and the card's finish.
- **Buy list:** star cards from any tab and track each lot through *planned → ordered → received → listed → sold*.
  - You can edit quantity, condition, actual buy and sell prices, and the order reference.
  - The summary shows € to spend, expected profit, realized profit on sold lots, and price drift since you added each card.
  - **Exports:**
    - **Cardmarket wants list:** works only for the set names Cardmarket recognizes; foils must be marked by hand after import.
    - **TCGplayer Mass Entry**
    - **Mana Pool / Moxfield**
    - **CSV**
- **Trip planner:** give it a € budget, how many cards fit in your luggage, and per-card and per-name limits.
  - It picks the set of cards with the most expected profit under your current filters. This is an optimizer, not simple greedy.
  - It shows a budget curve (how profit changes at 25%–200% of your budget) and can add the whole plan to the buy list.
- **Check a list:** paste a want list, decklist, Moxfield or Arena export, Cardmarket export or CSV.
  - It shows the best printing to import for each card.
  - Typos get suggestions, and `@ $12.50` at the end of a line uses a buyer's offer as the sale price.
- **Trends:** a daily price snapshot saved to `data/history.sqlite`.
  - Shows which spreads are widening, which Cardmarket prices are dropping, and which deals have lasted the longest.
  - The **Backfill** button loads about 90 days of past Cardmarket, TCGplayer and Card Kingdom prices from [MTGJSON](https://mtgjson.com) (about 64 MB download, 30–40 s, about 300 MB on disk).
  - Use it to tell a lasting price gap from a one-day blip.

User data (buy list and history) lives in `data/` and the price downloads in `.cache/`. Both folders are gitignored.

## Filters
- Finish, rarity, sets and card name
- Minimum and maximum US price, minimum profit, minimum ROI
- **Max price ratio:** drops obviously bad data
- **Liquidity:** minimum number of Mana Pool listings
- **Demand:** EDHREC rank cutoff
- **TCG vs Mana Pool disagreement:** drops cards where the two US price sources disagree by more than a set factor. That usually means one of the prices is stale or based on very few sales.

## Hosting (Docker / Unraid)
On a server, the app re-downloads prices every day at `MTG_REFRESH_HOUR` UTC (default 10:00, just after Scryfall publishes). It needs about 450 MB of RAM and about 550 MB of disk.

| Variable | Default | Purpose |
|---|---|---|
| `MTG_PASSWORD` | *(none)* | Turns on basic auth, with any username and this password. Set it whenever the app is reachable from outside. |
| `MTG_HOST` / `MTG_PORT` | `0.0.0.0` / `8765` in Docker | Address and port to listen on |
| `MTG_REFRESH_HOUR` | `10` | UTC hour for the daily refresh (`-1` turns it off) |

**On Unraid:**
1. Copy this folder to the server, for example to `/mnt/user/appdata/mtg-arbitrage/src`, with `git clone` or over the SMB share.
   - Optionally copy your existing `data/` folder to `/mnt/user/appdata/mtg-arbitrage/data` to keep your buy list and price history.
2. In the Unraid terminal, create the storage folders and give them to Unraid's default user:
   ```bash
   mkdir -p /mnt/user/appdata/mtg-arbitrage/{cache,data} && chown -R 99:100 /mnt/user/appdata/mtg-arbitrage
   ```
3. Set a real `MTG_PASSWORD` in `docker-compose.yml`, then start it:
   ```bash
   cd /mnt/user/appdata/mtg-arbitrage/src && docker compose up -d --build
   ```
   The Compose Manager plugin from Community Apps can run the same file from the web UI.
   - The first start downloads about 320 MB of price data, which takes a minute or two.
   - After changing the code, run `docker compose up -d --build` again.
4. Point a subdomain at it through your reverse proxy (Nginx Proxy Manager, SWAG, Traefik or a Cloudflare Tunnel).
   - Proxy `mtg.yourdomain.com` to `http://<unraid-ip>:8765` and turn on HTTPS. Basic auth is only safe over HTTPS.
   - If the proxy already handles login (Authelia, Authentik or Cloudflare Access), you can remove `MTG_PASSWORD`.

## Project layout
```
mtg_arb.py           data loading, cost model, scoring, CLI
app.py               local web server and plugin loader
ui/index.html        scanner UI and the window.MTG plugin API
features/*.py        server plugins: ROUTES + on_load (buylist, history, planner, listcheck, tcglive)
ui/features/*.js     matching UI plugins (tabs, card actions, drawer sections)
tools/backfill_mtgjson.py   price-history backfill (also runs standalone)
```
To add a feature, drop a module in `features/` and a script in `ui/features/`. `app.py` loads them automatically. The contract is documented in the `app.py` docstring and the `window.MTG` comments in `index.html`.

## Caveats
- **Reference prices:** Cardmarket "trend" is an average, not the cheapest listing. Always check the actual offers in your condition and language.
- **Grading:** Cardmarket grades condition more strictly than US sites, which works in your favor, but compare like with like.
- **Thin markets:** "Market" prices for rare foils and promos can come from very few sales. The disagreement filter and the Card Kingdom buylist price are good sanity checks.
- **Card Kingdom:** its buylist offers are capped (`qty_buying`) and change daily.
- **US customs:** carrying cards in for resale is technically commercial goods. Declare them as required.
