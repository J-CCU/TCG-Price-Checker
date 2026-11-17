# TCG Price Checker

A desktop app for Shopify stores that sell trading cards. It lists every Shopify listing with its stock at
each store location, compares sealed product (boxes, packs, tins, and so on) and TCG singles (Magic, Pokemon,
One Piece and other games) with TCGplayer prices, and flags listings whose price has drifted away from the
market, with a suggested new price.

TCGplayer prices come from [tcgcsv.com](https://tcgcsv.com), which republishes TCGplayer's catalogue and
prices once a day. The app only reads from Shopify and tcgcsv, with one exception: "Update Shopify
prices..." changes the prices of listings you select, and only after you check each new price and confirm.
Every change is logged in `reports/price_changes.csv`. (Full Shopify reads use a bulk operation,
`bulkOperationRunQuery`. Shopify files it under mutations, but it only runs a read query and builds a file
of the results to download; it changes nothing. The app never cancels a bulk operation; if Shopify won't start one, the app reads page by page instead.)

## Quick start

Prebuilt apps aren't included right now; build one from source (see [Building the app](#building-the-app))
or run the script with Python.

1. Get a `config.yaml` with your store's Shopify login (see [config.yaml](#configyaml-shopify-login)).
2. Run `python3 tcg_price_checker.py` (see [How to run](#how-to-run)), or open a `TCG Price Checker.exe`
   (Windows) or `TCG Price Checker.app` (Mac) you built yourself (see [Building the app](#building-the-app)).
3. When asked, choose your `config.yaml`. The first check starts automatically.
4. Click "Need update" to see every URGENT and DRIFTING row (the suggested new price is in the details under the table), and
   check the CHECK MATCH rows. "Open printable report" opens the HTML report (share it with the store
   managers: it shows every store's stock); the same rows are in `reports/price_report_<date>.csv` and
   the rows that need a new price in `reports/need_update_<date>.csv`.

## Features

- Reads every Shopify product variant through the Admin GraphQL API (or from a Shopify product export
  CSV) and lists all of them: active products, plus drafts if "Also check draft products" is on. Archived
  products are never listed.
- Each listing is one of three kinds:
  - Sealed: product types Box, Pack, Tin by default (Settings). Matched automatically to a TCGplayer sealed
    product by title words, with safeguards so a "1st Edition", Japanese, Bundle or Pack listing does not
    auto-match a different kind of product. Unchanged from earlier versions.
  - Singles: listings tagged `MTG Singles`, `Pokemon Singles`, `One Piece Singles` (and other games, see
    `singles_tags`), plus product type `Singles` titles that end in a set code. Matched to a TCGplayer card,
    see [How singles are matched](#how-singles-are-matched).
  - Other: everything TCGplayer can't price for us (sports cards, graded slabs, accessories, merchandise,
    sealed product with an ignore word). Listed with status `NO PRICE SOURCE` and never matched.
- Status for each listing:
  - `URGENT` / `DRIFTING`: our price is at least the set percentage away from TCGplayer
  - `CHECK MATCH`: the automatic match is uncertain (or the price gap is suspiciously large), please confirm
  - `OK`: within the drifting threshold
  - `SELF-PRICED`: marked "we price this ourselves", or the title contains a self-priced word
  - `NO PRICE`: matched, but TCGplayer has no price for it
  - `NO MATCH`: no TCGplayer product found (shown under the "Not on TCGplayer" filter)
  - `NO PRICE SOURCE`: a kind of product TCGplayer doesn't price (see Other above)
  - `NO DATA`: the Shopify listing has no price
- "Need update": every URGENT and DRIFTING row, with the suggested new price (the TCGplayer price; for
  singles adjusted for condition). In the window, the HTML report, and `need_update_<date>.csv`.
- Stock: one column per store / warehouse plus their total, in the window and the reports, so every store
  can see what the others hold. Columns are set in Settings (`stock_columns`): by default Warehouse (Cash
  Cards Unlimited Warehouse + Relics Awakened), Frank and Son, Janss and Woodland Hills. Shopify locations
  in no column (e.g. Spoils and Loot via Shopify Collective) aren't counted; a listing stocked only there
  counts as out of stock. The details under the table show any stock that isn't counted.
- Update Shopify prices: select one or more rows, check or type each new price (it starts at the
  suggested price), then send them to Shopify after a confirmation that lists every change.
- Columns can be dragged to a new place; the order is remembered. Right-click a heading to reset it.
- Out-of-stock listings (stock 0 or blank) are hidden by default in the window and the reports; "Show out
  of stock" (or `--show-zero`) brings them back. The report says how many were left out.
- Flags: presale, fast-moving (presale or set released recently), TCGplayer price below our cost.
- Cost and margin from Shopify's "Cost per item": our margin, and the margin if priced at TCGplayer.
- Separate drift / urgent thresholds for presale items.
- Choice of TCGplayer Market Price or lowest listing as the comparison price.
- In the window: confirm a match, choose a different match (search TCGplayer), enter a live price typed
  from TCGplayer's site, mark "we price this ourselves", hide a product, undo a choice, view and remove
  all saved choices.
- Filter, search, sort columns, export the visible list to CSV, open a printable HTML report.
- Opens instantly with the previous results, then checks again in the background. The first check of
  the day reads every Shopify product with one bulk operation (about 20 seconds for 17,000 variants);
  later checks only fetch variants and products changed since (about a second). See [Speed](#speed).
- Remembers window size, filter, product kind, "Show out of stock", sort and text size.

### How singles are matched

Single titles follow TCGplayer's own naming, for example `Zapdos ex - 202/165 - SV: Scarlet & Violet 151 (MEW)`,
`Enduring Curiosity (Extended Art) - Duskmourn: House of Horror (DSK)` or
`Otama (Full Art) - Premium Booster - The Best - (PRB-01)`.

1. Game: from the listing's tags (`singles_tags`, e.g. `MTG Singles` = Magic).
2. Set: the set code in brackets is looked up among TCGplayer's set abbreviations for that game (`MEW`,
   `DSK`, `PRB-01`). Several sets can share a code (Pokemon promos are all `PR`); the set name in the
   title picks between them. Without a known code, the set name itself is used.
3. Card: the card part of the title (`Zapdos ex - 202/165`, `Enduring Curiosity (Extended Art)`) is looked
   up by name in that set. Collector numbers (`202/165`, `(164 Secret Full Art)`, `OP05-041`) must agree,
   and treatment words in brackets (Extended Art, Borderless, Full Art, Showcase, Secret...) must match.
4. Finish: the variant (`Near Mint / English / Foil`, `Lightly Played Foil`, `Reverse Holo`) picks the
   TCGplayer subtype: Normal, Foil, Holofoil, Reverse Holofoil, 1st Edition / Unlimited (Holofoil).
   With no finish named, the plain printing is used (or the only one there is).
5. Condition: tcgcsv prices are Near Mint. The variant's condition (Near Mint, Lightly Played,
   Moderately Played, Heavily Played, Damaged) scales the price by the `condition_pct` table
   (default 100 / 85 / 70 / 55 / 40 %, editable in Settings).

A single becomes CHECK MATCH instead of matching automatically when the card is ambiguous (several cards
fit equally well, e.g. a vintage card with no edition named, or "Foil" on a card with both holo and reverse
holo), when it is not in English (non-English language in the variant or title), when it was only found
in a different game than its tag (e.g. a Japanese Pokemon set), or when its price is very far from ours.
"Match is correct" / "Choose a different match..." work for singles too; a confirmed single remembers the
card and finish for that exact title and variant (condition).

## Requirements

- Built app: nothing to install to run it. Build it yourself (see [Building the app](#building-the-app)); a
  Mac build runs only on Macs with the same chip type as the one that built it.
- From source: Python 3.10 or newer with Tkinter (included with the python.org installers). Only the
  standard library is needed. PyYAML is used to read `config.yaml` if installed, but is optional; without
  it, a built-in reader handles simple `key: "value"` lines.
- Internet access to `tcgcsv.com` and the store's `*.myshopify.com` domain.
- A `config.yaml` with a `shopify:` section (see [Configuration](#configuration)), or a Shopify product export CSV for
  command-line runs.

## How to run

### Built app

1. Build the app (see [Building the app](#building-the-app)) and put it in its own folder. Its data (`products.json`, `cache/`, `reports/`) is created next to it,
   so moving the folder moves everything.
2. Open it:
   - Windows: double-click `TCG Price Checker.exe`.
   - Mac: double-click `TCG Price Checker.app`. On a Mac other than the one that built it, the first time:
     right-click > Open > Open.
3. The first time, it asks you to choose your `config.yaml` (the Shopify login file). It remembers the
   choice; change it any time in Settings. The file is never searched for automatically.
4. Prices are checked automatically on start. Press "Check prices now" to check again.

### From source

```
py tcg_price_checker.py            # Windows (or double-click the .py file)
python3 tcg_price_checker.py       # Mac
```

Command line, one run without the window (writes the HTML and CSV reports and exits):

```
py tcg_price_checker.py --cli [--credentials PATH] [--shopify-csv EXPORT.csv] [--config PATH] [--out DIR] [--show-zero] [--timings]
python3 tcg_price_checker.py --cli [--credentials PATH] [--shopify-csv EXPORT.csv] [--config PATH] [--out DIR] [--show-zero] [--timings]
```

| Option | Default | Meaning |
| --- | --- | --- |
| `--cli` | | Run once in the console instead of opening the window. |
| `--credentials PATH` | the file chosen in the window | `config.yaml` with a `shopify:` section. |
| `--shopify-csv PATH` | | Read a Shopify product export CSV instead of calling the Shopify API. |
| `--config PATH` | `products.json` in the data folder | Saved choices and settings file. |
| `--out DIR` | `reports/` in the data folder | Where reports are written. |
| `--show-zero` | off (the `show_zero_stock` setting) | Include out-of-stock listings in the reports. |
| `--no-singles` | | Don't price-check singles this run (they are listed as NO PRICE SOURCE). Not saved. |
| `--cached-shopify` | | Reuse the Shopify products saved by the last check (`cache/shopify_products.json`) instead of reading Shopify. |
| `--no-bulk` | | Read Shopify page by page instead of with a bulk operation. Not saved. |
| `--timings` | | Print how long each step took. |
| `--build-app` | | Build the stand-alone app (see [Building the app](#building-the-app)). |

## Using the window

| Control | What it does |
| --- | --- |
| Check prices now | Re-read Shopify and tcgcsv and recompute (also Cmd/Ctrl+R or F5). |
| Open printable report | Write and open the HTML report in your browser. |
| Saved choices | List every saved match / choice; remove one to undo it. |
| Settings | Thresholds, comparison price, product types, word lists, stock columns, login file, text size. |
| Export this list... | Save the rows currently shown (current filter, kind, stock toggle and search) as CSV, with every location's stock. Choose "Need update" first to export just the rows whose price should change. |
| Help | Explains statuses, symbols and shortcuts. |
| Filter buttons | All, Need update (Urgent + Drifting), Urgent, Drifting, Check match, OK, Presale, Fast-moving, Below cost, We price, Not on TCGplayer, No price, No price source, No data. Each shows its count. |
| Show | Everything, Sealed, Singles or Other (no price source). |
| Show out of stock | Off by default: listings with stock 0 or blank are hidden. The status line says how many. |

Table columns: Status, Flags, Shopify product (title and variant), Our price, Cost, Our margin, TCG market
(or TCG lowest), Margin at TCG, Difference, $ diff, Stock (total of the stock columns), one column per
stock column (see `stock_columns`), TCGplayer product, Match. Drag a heading to move a column; right-click
a heading for "Reset column order". The order is saved when the window closes. On a narrow window the less important columns hide first
(Match, Margin at TCG, Our margin, $ diff, Cost, Flags, then the location columns); the details under the
table always show everything, including the suggested new price and each stock column.

Long lists are drawn in batches so the window stays usable, and the search waits for a pause in typing.
| Row actions (buttons or right-click) | Open on TCGplayer, Match is correct, Choose a different match..., Enter live price..., Update Shopify prices..., We price this ourselves, Hide this product, Undo my choice. |

Select several rows with Cmd/Ctrl+click or Shift+click. The other actions use the last row clicked.

### Updating prices in Shopify

1. Select the rows to change (for example in "Need update"), then click "Update Shopify prices...".
2. Each listing starts at its suggested price (else the TCGplayer price). Type a different price, or
   "Keep our prices" / "Use suggested prices" for all. The Change column shows the % change, and warns when
   a price is below cost or 50%+ away. Rows left at our price are skipped.
3. "Send to Shopify..." shows the full list of changes and asks again before anything is sent. The price
   changes everywhere the listing is sold (online and POS).
4. The list updates straight away. Every attempt, changed or failed, is added to
   `reports/price_changes.csv` (time, product, variant ID, old and new price, result).

This needs the Shopify app to have the `write_products` permission. Rows from a Shopify CSV export
(`--shopify-csv`) can't be updated.

Double-click a row (or press Enter) to open it on TCGplayer. Symbols: star = presale, lightning =
fast-moving, warning sign = TCGplayer price below our cost, pencil = price typed in with "Enter live
price...". A typed live price is used until a newer daily snapshot has a price for that product.

Shortcuts: Cmd/Ctrl+F search, Esc clears the search, Cmd/Ctrl + / - / 0 change text size.

## Configuration

### config.yaml (Shopify login)

Only the top-level `shopify:` section is read. Use placeholders like these; never commit a real file.

```yaml
shopify:
  store_url: "your-store.myshopify.com"
  client_id: "YOUR_CLIENT_ID"
  client_secret: "YOUR_CLIENT_SECRET"
  # alternatives:
  # admin_api_token: "YOUR_ADMIN_API_TOKEN"
  # app_automation_token: "YOUR_APP_TOKEN"
```

| Key | Purpose |
| --- | --- |
| `store_url` | Required. Accepts `my-store`, `my-store.myshopify.com`, a full `https://` URL, or `admin.shopify.com/store/my-store`. |
| `admin_api_token` | Admin API access token. Used first if present. |
| `client_id`, `client_secret` | Used if there is no `admin_api_token`: exchanged for a 24-hour Admin API token (client-credentials grant, for an app installed on your own store). |
| `app_automation_token` | Used last, or if the client-credentials exchange fails. |

Shopify access scopes the app needs:

| Scope | Used for | Without it |
| --- | --- | --- |
| `read_products` | Products, variants, prices, tags (required). | The check can't run. |
| `read_inventory` | Cost per item, and stock per location. | Costs left blank; total stock only. |
| `read_locations` | Location names for the per-location stock columns. | Total stock only. |

When Shopify refuses per-location stock or cost, the check carries on without it and says so once in the
status line (and in the report). Only read scopes are needed; the app sends no changes.

### Environment variables

| Variable | Purpose |
| --- | --- |
| `SHOPIFY_STORE` + `SHOPIFY_TOKEN` | If both are set, they are used instead of `config.yaml` (store domain and Admin API token). |
| `SHOPIFY_API_VERSION` | Shopify Admin API version. Default `2026-07`. |

### products.json (saved choices and settings)

Created automatically in the data folder and updated by the window. Before each save, the previous
version is copied to `products.json.bak`. If `products.json` becomes unreadable, it is renamed to
`products.damaged.json` and the backup is used. On first run it starts with a few built-in matches.

Top-level keys:

| Key | Contents |
| --- | --- |
| `settings` | See the table below. |
| `overrides` | Saved choices per Shopify title (and optional `variant`): `tcg` (list of `category` / `group` / `product` TCGplayer IDs, plus `finish` for a single, e.g. `Foil`; several are averaged), `ignore` (hidden), `self_priced`, `presale`, `note`. These always win over automatic matching. |
| `live_prices` | Prices typed in from TCGplayer: `shopify_title`, `variant`, `price`, `entered` (UTC time). |

`settings` keys (most are editable in Settings):

| Key | Default | Purpose |
| --- | --- | --- |
| `drift_pct` | `3` | DRIFTING when our price is at least this % from TCGplayer. |
| `urgent_pct` | `10` | URGENT when at least this % away. |
| `presale_drift_pct` | `3` | Drifting threshold for presale items. |
| `presale_urgent_pct` | `10` | Urgent threshold for presale items. |
| `tcg_price_field` | `marketPrice` | `marketPrice` (Market Price) or `lowPrice` (lowest listing). |
| `sealed_types` | `Box`, `Pack`, `Tin` | Shopify product types matched as sealed product. |
| `check_singles` | `true` | Price-check TCG singles. Off: singles are listed as NO PRICE SOURCE. |
| `condition_pct` | `NM 100`, `LP 85`, `MP 70`, `HP 55`, `DMG 40` | % of TCGplayer's Near Mint price a single in that condition should sell for. |
| `singles_tags` | `MTG Singles`, `Pokemon Singles`, `One Piece Singles`, ... | Shopify tag -> tcgcsv category id (game) for singles (file only). |
| `no_price_source_tags` | `Sports Singles`, `baseball`, `football`, ... | Listings with any of these tags are NO PRICE SOURCE (file only). |
| `stock_columns` | Warehouse, Frank and Son, Janss, Woodland Hills | Stock columns in order: `name` and the Shopify `locations` it adds up. Locations in no column aren't counted. Empty list: one column per Shopify location. |
| `show_zero_stock` | `false` | Show out-of-stock listings in the window and reports ("Show out of stock"). |
| `shopify_bulk` | `true` | Read every Shopify product with one bulk operation; `false` pages through instead (file only). |
| `include_drafts` | `false` | Also check draft products. Archived products are never checked. |
| `ignore_words` | sports-card brands etc. | Never match products whose title contains any of these (whole words); they are listed as NO PRICE SOURCE. |
| `self_priced_words` | `damaged` | Treat as SELF-PRICED when the title contains any of these. |
| `new_release_days` | `30` | Sets released within this many days are flagged fast-moving. |
| `auto_match_score` | `0.75` | Minimum score for an automatic match (file only). |
| `check_match_score` | `0.35` | Below this score a listing is NO MATCH (file only). |
| `auto_match_max_diff_pct` | `75` | An automatic match this far from our price becomes CHECK MATCH (file only). |
| `tcg_categories` | list of tcgcsv category IDs | TCGplayer games to match against (file only). |
| `credentials_file` | | Path to the chosen `config.yaml`. |
| `text_size`, `window` | | Text size and window position, filter, sort and column order. |

Edit `products.json` only while the app is closed.

## Outputs

All in the data folder (see [Data folder location](#data-folder-location)).

### reports/

- `price_report_YYYY-MM-DD.html`: printable report with summary counts, a search box, filter buttons
  (All, Need update, each status, Sealed, Singles, Presale, below cost, fast-moving) and one row per
  listing (status, product with condition and finish, our price, cost and margin, TCGplayer price, low and
  margin, difference, total stock, one column per stock column, matched
  TCGplayer product with link). It notes how many out-of-stock listings were left out. Supports dark mode.
- `price_report_YYYY-MM-DD.csv`: same rows, UTF-8 with BOM.
- `need_update_YYYY-MM-DD.csv`: only the URGENT and DRIFTING rows, same columns.

CSV columns:

| Column | Meaning |
| --- | --- |
| `tag` | Status (URGENT, DRIFTING, CHECK MATCH, OK, SELF-PRICED, NO PRICE, NO MATCH, NO DATA). |
| `presale` | Presale item. |
| `release` | TCGplayer release date for presale items. |
| `channel` | `POS` if the title contains `(POS)`, else `Online`. |
| `kind` | `sealed`, `single` or `other` (no price source). |
| `shopify_title`, `variant`, `type` | Shopify product title, variant, product type. |
| `condition`, `finish` | Singles: condition code (NM, LP, MP, HP, DMG) and TCGplayer subtype used. |
| `our_price`, `suggested_price` | Shopify price; suggested new price (URGENT / DRIFTING rows). |
| `cost`, `margin_pct` | Cost per item, our margin %. |
| `tcg_price`, `tcg_margin_pct`, `below_cost` | TCGplayer comparison price, margin at that price, whether it is below our cost. |
| `diff_amt`, `diff_pct` | Our price minus TCGplayer, in dollars and %. |
| `tcg_low` | TCGplayer lowest listing. |
| `match`, `match_score` | Confirmed / Automatic / Needs check / Not found, and match score. |
| `tcg_name`, `tcg_set` | Matched TCGplayer product(s) and set(s). |
| `source`, `price_as_of` | Where the price came from, and its date if older than today's snapshot. |
| `snapshot_price`, `live_entered` | Snapshot price, and when a live price was typed in. |
| `fast_moving`, `status`, `stock`, `note` | Fast-moving flag, Shopify status, total available stock in the stock columns, saved note. |
| `stock: <column>` | Available stock in each stock column (see `stock_columns`; blank if per-location stock couldn't be read). |

- `price_changes.csv`: every price sent with "Update Shopify prices..." (`when`, `shopify_title`, `variant`,
  `variant_id`, `old_price`, `new_price`, `result`).

Reports are rewritten after every check (one set per day). Out-of-stock listings are left out unless "Show
out of stock" is ticked (or `--show-zero` is given). "Export this list..." writes the same columns to a
file you choose (default name `tcg_prices_<filter>_<date>.csv`).

### cache/

| File | Contents |
| --- | --- |
| `tcg_index.json` | TCGplayer sets and sealed-product catalogue for the chosen games; only changed sets are re-downloaded. |
| `cards/` | Single-card lists, one file per TCGplayer set we stock singles from; re-downloaded only when the set changes. |
| `prices/` | TCGplayer prices per set for the current daily snapshot, so a second check the same day downloads nothing. |
| `last_prices.json` | Last price seen per product (and per card finish), so a product with no current listings shows its last price. |
| `match_memo.json` | Automatic sealed matches, reused while the sealed catalogue and match settings are unchanged. |
| `shopify_products.json` | Shopify variants (with stock per location) from the last check, for incremental updates. |
| `last_check.json` | Last results, shown immediately when the app opens. |

Deleting `cache/` is safe; it is rebuilt (the next check takes longer).

### error_log.txt

Written in the data folder if an error happens inside the window.

## Data folder location

`products.json`, `cache/`, `reports/` and `error_log.txt` live in the folder next to the script, the
`.exe`, or the `.app`. If that folder cannot be written to, or the app is in `/Applications` or
`~/Applications`, or macOS is running it from a read-only quarantine copy (for example straight from
Downloads), this folder is used instead:

- Mac: `~/Library/Application Support/TCG Price Checker`
- Windows: `%APPDATA%\TCG Price Checker`
- Other: `~/.tcg_price_checker`

## Building the app

Build once per computer type, with Python 3.10+ from python.org:

```
py tcg_price_checker.py --build-app        # Windows -> TCG Price Checker.exe
python3 tcg_price_checker.py --build-app   # Mac     -> TCG Price Checker.app
```

It creates a throwaway virtual environment in a temp folder, installs PyInstaller and Pillow from PyPI,
draws the app icon in code, and runs PyInstaller (`--windowed`; `--onefile` on Windows; bundle identifier
`com.tcgpricechecker.app` on Mac). The finished app is placed next to the script and the temp folder is
removed. A Mac app only runs on Macs with the same chip type as the one that built it.

To ship the Mac app, zip `TCG Price Checker.app`.

## Speed

Measured with the store's real data (16,944 variants: 7,833 singles, 3,102 boxes, 1,848 graded slabs...) on
a Mac, and with that catalogue copied 10 times (169,440 variants):

| Step | Before | Now | Now, 10x catalogue |
| --- | --- | --- | --- |
| Full Shopify read (first check of the day) | 73 s (paging, no per-location stock) | 21 s (bulk operation, with per-location stock) | bulk operations scale with the catalogue; paging would take about 12 min (extrapolated) |
| Later Shopify reads the same day | a few s | about 1 s | depends on how much changed |
| Open the window with the last results | 0.07 s | 0.08 s | about 0.4 s |
| Match (first check of the day / later checks) | 0.8 s for 1,249 sealed listings | 1.6 s / 0.2 s for all 16,944 variants | 6.7 s / 2.0 s |
| TCGplayer prices, second check the same day | downloaded again | read from `cache/prices/` (0.1 s) | 0.4 s |
| Draw the list | 0.03 s (1,249 rows) | 0.02 s first screen, 0.1 s all 6,496 rows | 0.05 s first screen, 0.8 s all 64,960 rows in the background |

How: Shopify bulk operation for full reads, paged reads of only changed variants later in the day;
parallel (16 at a time) tcgcsv downloads cached on disk per set and per daily snapshot; card lookups by
set code, name and number from dictionaries instead of scanning; automatic sealed matches remembered
between checks; one-pass filter counts; batched drawing of long lists; all heavy work off the window's
main thread.

## Troubleshooting

| Message / symptom | Fix |
| --- | --- |
| "No Shopify login file chosen yet" / "can't be found" | Open Settings > Choose file... and pick your `config.yaml`. |
| "has no filled-in 'shopify:' section" / "needs a store_url" / "needs client_id and client_secret" | Fill in the `shopify:` section (see [Configuration](#configuration)). |
| "Shopify didn't accept the login" | Check the credentials and that the Shopify app is installed on the store with permission to read products. |
| "Shopify login failed: ..." | The `client_id` / `client_secret` exchange failed. Check both values, or add an `admin_api_token`. |
| "Shopify API error: ..." | Shopify rejected the product query; the message names the cause, often a missing permission. |
| Cost and margin columns are blank | The Shopify app lacks inventory read access, or "Cost per item" is not filled in. |
| "showing total stock only" in the status line | The Shopify app lacks `read_inventory` / `read_locations`; add them to get per-location stock. |
| A listing is missing | It may be out of stock: tick "Show out of stock". Stock at locations in no stock column doesn't count. Also check "Show" is "Everything". |
| "Update Shopify prices" fails with a permission message | The Shopify app needs the `write_products` permission. |
| "Update Shopify prices" says the rows can't be changed | They came from a CSV export or a check by an older version; press Check prices now first. |
| A single is CHECK MATCH | The card, finish or edition is ambiguous, or it isn't English. Confirm it, or choose the right card. |
| A single is NO MATCH | The set code or card name isn't on TCGplayer as written (e.g. Chinese cards, stamped promos). Use "Choose a different match...". |
| A single is NO PRICE SOURCE | Its tags don't name a game in `singles_tags`, or it has a sports / graded tag. |
| "A secure connection couldn't be checked" (Mac) | Run "Install Certificates.command" from Applications > Python 3.x. |
| "Couldn't connect to ..." | Check the internet connection, then Check prices now. |
| "... says it's busy right now" / "... is having problems right now (error 5xx)" | Wait a few minutes and try again. |
| Status bar time shown as a warning | The TCGplayer snapshot is over 30 hours old; tcgcsv may not have updated yet. |
| A product is matched to the wrong TCGplayer item | Select it > Choose a different match..., or Hide this product. |
| A product is missing from the list | Check it has stock (or tick "Show out of stock"), it is active (or drafts are included), and it was not hidden (see Saved choices). |
| A product deleted in Shopify still appears | Later checks in a day only fetch changed products. It drops out at the first check of the next day, or delete `cache/shopify_products.json` and check again. |
| Mac says the app cannot be opened | Right-click > Open > Open the first time. A Mac build only runs on the chip type it was built on; build your own on that Mac. |
| Settings or choices lost after an edit | A damaged `products.json` is renamed to `products.damaged.json` and `products.json.bak` is used. |
| Any other error in the window | Details are in `error_log.txt` in the data folder. |

## File guide

| Path | Notes |
| --- | --- |
| `tcg_price_checker.py` | The whole app: matching (sealed and singles), Shopify and tcgcsv access, window, reports, build. |
| `products.json`, `products.json.bak` | Saved choices and settings (created at runtime, not in the repo). |
| `cache/`, `reports/`, `error_log.txt` | Created at runtime. |
| `config.yaml` | Your Shopify login file. Keep it out of git; it can live anywhere. |
