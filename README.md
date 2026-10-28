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
| [Cardmarket price guide + product list](https://www.cardmarket.com/en/Magic/Data/Price-Guide) | Cardmarket trend € for printings Scryfall has no price for, and each product's set for the Cardmarket links | daily cache |
| [Frankfurter](https://frankfurter.dev) | EUR→USD rate (ECB reference rate) | each start |

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
  - **TCGplayer by finish:** the TCGplayer market price of every finish of the printing, taken from the daily Scryfall data. It warns when foil and non-foil prices differ a lot, and links to TCGplayer's listings filtered to the right finish. The app never contacts TCGplayer directly, because their terms ban automated price collection.
  - The TCGplayer link opens filtered to English and the card's finish.
  - The Cardmarket link opens a search for the card in its set. Cardmarket's firewall blocks direct product links like `Products?idProduct=…`. The search pages only show the normal "verify you are human" check.
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
| `PUID` / `PGID` | `99` / `100` | Owner of the files in the volumes (Unraid's `nobody:users`) |

Every push to `main` builds `ghcr.io/radixtrator/mtg-arbitrage:latest` with GitHub Actions, for amd64 and arm64.

**On Unraid (web UI, no terminal):**
1. Install the template, once: in the Unraid terminal run
   ```bash
   wget -O /boot/config/plugins/dockerMan/templates-user/my-mtg-arbitrage.xml https://raw.githubusercontent.com/Radixtrator/mtg-arbitrage/main/unraid/mtg-arbitrage.xml
   ```
   Alternatively, skip this step and fill in the fields below by hand under **Add Container**.
2. **Docker → Add Container → Template:** pick `mtg-arbitrage`. Set a **Password**, then click **Apply**.
   - It stores data in `/mnt/user/appdata/mtg-arbitrage/` (`cache` and `data`). Those folders are created and owned by `nobody:users` automatically.
   - To keep your buy list and price history, copy your local `data/` folder into `appdata/mtg-arbitrage/data` before the first start.
   - The first start downloads about 350 MB of price data, which takes a minute or two. Then click the container icon → **WebUI**.
3. **Updates:** after a push, wait about 3 minutes for the build. The Docker tab then shows *update ready*; click **Apply update**. To update automatically, use the *Auto Update Applications* plugin from Community Apps.

**With Docker Compose instead:** `docker-compose.yml` uses the same image. Set `MTG_PASSWORD` in it, then run `docker compose up -d` (or `docker compose pull && docker compose up -d` to update). To build from a checkout, swap `image:` for `build: .`.

**On your domain:** proxy `mtg.yourdomain.com` to `http://<unraid-ip>:8765` with HTTPS (Nginx Proxy Manager, SWAG, Traefik or a Cloudflare Tunnel).
- Basic auth is only safe over HTTPS.
- If the proxy already handles login (Authelia, Authentik or Cloudflare Access), you can leave `MTG_PASSWORD` empty. Either way, keep some login in front of it: Cardmarket's terms don't allow showing their prices publicly.

## Project layout
```
mtg_arb.py           data loading, cost model, scoring, CLI
app.py               local web server and plugin loader
ui/index.html        scanner UI and the window.MTG plugin API
features/*.py        server plugins: ROUTES + on_load (buylist, history, planner, listcheck)
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

## Data sources, terms and credits
This is a personal tool: run it yourself, for yourself. Every data source has its own terms, and you are bound by them when you run it.
- **Downloads:** each source is fetched at most once a day, with a User-Agent that names this repo.
- **Sharing:** never redistribute the downloaded data or make an instance publicly reachable. Keep hosted instances behind `MTG_PASSWORD`. `data/` and `.cache/` are gitignored for this reason.
- **Prices:** these are daily estimates with no guarantee. Check the store before buying.

| Source | Credit / notes |
|---|---|
| [Scryfall](https://scryfall.com) | Card data, images and the Cardmarket/TCGplayer prices. Used under the [API guidelines](https://scryfall.com/docs/api). Not affiliated with or endorsed by Scryfall. |
| [MTGJSON](https://mtgjson.com) | Price history backfill. MIT licensed. |
| [Cardmarket](https://www.cardmarket.com/en/Magic/Data/Price-Guide) | Price data © Cardmarket. Uses the public daily price guide. If you resell regularly, Cardmarket's terms may require a commercial account. |
| [Mana Pool](https://manapool.com/api/docs/v1) | Public prices endpoint. |
| [Card Kingdom](https://www.cardkingdom.com) | The buylist pricelist is not officially documented, and Card Kingdom's terms restrict automated data gathering. Use at your own discretion. |
| [Frankfurter](https://frankfurter.dev) | EUR→USD. Source: European Central Bank reference rates. |

MTG Arbitrage is unofficial Fan Content permitted under the [Fan Content Policy](https://company.wizards.com/en/legal/fancontentpolicy). Not approved/endorsed by Wizards. Portions of the materials used are property of Wizards of the Coast. ©Wizards of the Coast LLC.

## License
[MIT](LICENSE)
