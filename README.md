# TCG Price Checker

A desktop app for Shopify stores that sell sealed trading card product. It compares every sealed listing
(boxes, packs, tins, and so on) with TCGplayer prices and flags listings whose price has drifted away from
the market.

TCGplayer prices come from [tcgcsv.com](https://tcgcsv.com), which republishes TCGplayer's catalogue and
prices once a day. The app is read-only: it only reads from Shopify and tcgcsv and never changes anything
in the store.

## Quick start

1. Get a `config.yaml` with your store's Shopify login (see [config.yaml](#configyaml-shopify-login)).
2. Open `TCG Price Checker.exe` (Windows) or `TCG Price Checker.app` (Mac), or run
   `python3 tcg_price_checker.py` (see [How to run](#how-to-run)).
3. When asked, choose your `config.yaml`. The first check starts automatically.
4. Work through the URGENT and CHECK MATCH rows. "Open printable report" opens the HTML report; the same
   rows are in `reports/price_report_<date>.csv`.

## Features

- Reads every Shopify product variant through the Admin GraphQL API (or from a Shopify product export
  CSV) and keeps only sealed product types (Box, Pack, Tin by default). Singles are never checked.
- Automatically matches each listing to a TCGplayer sealed product by title words, with safeguards so a
  "1st Edition", Japanese, Bundle or Pack listing does not auto-match a different kind of product.
- Status for each listing:
  - `URGENT` / `DRIFTING`: our price is at least the set percentage away from TCGplayer
  - `CHECK MATCH`: the automatic match is uncertain (or the price gap is suspiciously large), please confirm
  - `OK`: within the drifting threshold
  - `SELF-PRICED`: marked "we price this ourselves", or the title contains a self-priced word
  - `NO PRICE`: matched, but TCGplayer has no price for it
  - `NO MATCH`: no TCGplayer product found (shown under the "Not on TCGplayer" filter)
  - `NO DATA`: the Shopify listing has no price
- Flags: presale, fast-moving (presale or set released recently), TCGplayer price below our cost.
- Cost and margin from Shopify's "Cost per item": our margin, and the margin if priced at TCGplayer.
- Separate drift / urgent thresholds for presale items.
- Choice of TCGplayer Market Price or lowest listing as the comparison price.
- In the window: confirm a match, choose a different match (search TCGplayer), enter a live price typed
  from TCGplayer's site, mark "we price this ourselves", hide a product, undo a choice, view and remove
  all saved choices.
- Filter, search, sort columns, export the visible list to CSV, open a printable HTML report.
- Opens instantly with the previous results, then checks again in the background. The first check of
  the day reads every Shopify product (about a minute); later checks only fetch products changed since.
- Remembers window size, filter, sort and text size.

## Requirements

- Prebuilt app: nothing to install.
  - Windows: `TCG Price Checker.exe`
  - Mac: unzip `TCG Price Checker.app.zip` to get `TCG Price Checker.app`. The included build is for
    Apple Silicon Macs; on an Intel Mac, run from source or build it yourself (see [Building the app](#building-the-app)).
- From source: Python 3.10 or newer with Tkinter (included with the python.org installers). Only the
  standard library is needed. PyYAML is used to read `config.yaml` if installed, but is optional; without
  it, a built-in reader handles simple `key: "value"` lines.
- Internet access to `tcgcsv.com` and the store's `*.myshopify.com` domain.
- A `config.yaml` with a `shopify:` section (see [Configuration](#configuration)), or a Shopify product export CSV for
  command-line runs.

## How to run

### Prebuilt app

1. Put the app in its own folder. Its data (`products.json`, `cache/`, `reports/`) is created next to it,
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
py tcg_price_checker.py --cli [--credentials PATH] [--shopify-csv EXPORT.csv] [--config PATH] [--out DIR]
python3 tcg_price_checker.py --cli [--credentials PATH] [--shopify-csv EXPORT.csv] [--config PATH] [--out DIR]
```

| Option | Default | Meaning |
| --- | --- | --- |
| `--cli` | | Run once in the console instead of opening the window. |
| `--credentials PATH` | the file chosen in the window | `config.yaml` with a `shopify:` section. |
| `--shopify-csv PATH` | | Read a Shopify product export CSV instead of calling the Shopify API. |
| `--config PATH` | `products.json` in the data folder | Saved choices and settings file. |
| `--out DIR` | `reports/` in the data folder | Where reports are written. |
| `--build-app` | | Build the stand-alone app (see [Building the app](#building-the-app)). |

## Using the window

| Control | What it does |
| --- | --- |
| Check prices now | Re-read Shopify and tcgcsv and recompute (also Cmd/Ctrl+R or F5). |
| Open printable report | Write and open the HTML report in your browser. |
| Saved choices | List every saved match / choice; remove one to undo it. |
| Settings | Thresholds, comparison price, product types, word lists, login file, text size. |
| Export this list... | Save the rows currently shown (current filter and search) as CSV. |
| Help | Explains statuses, symbols and shortcuts. |
| Filter buttons | All, Urgent, Drifting, Check match, OK, Presale, Fast-moving, Below cost, We price, Not on TCGplayer, No price, No data. |
| Row actions (buttons or right-click) | Open on TCGplayer, Match is correct, Choose a different match..., Enter live price..., We price this ourselves, Hide this product, Undo my choice. |

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

The Shopify app needs permission to read products. Cost and margin also need inventory read access
(`read_inventory`); without it, the check carries on with costs left blank.

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
| `overrides` | Saved choices per Shopify title (and optional `variant`): `tcg` (list of `category` / `group` / `product` TCGplayer IDs; several are averaged), `ignore` (hidden), `self_priced`, `presale`, `note`. These always win over automatic matching. |
| `live_prices` | Prices typed in from TCGplayer: `shopify_title`, `variant`, `price`, `entered` (UTC time). |

`settings` keys (most are editable in Settings):

| Key | Default | Purpose |
| --- | --- | --- |
| `drift_pct` | `3` | DRIFTING when our price is at least this % from TCGplayer. |
| `urgent_pct` | `10` | URGENT when at least this % away. |
| `presale_drift_pct` | `3` | Drifting threshold for presale items. |
| `presale_urgent_pct` | `10` | Urgent threshold for presale items. |
| `tcg_price_field` | `marketPrice` | `marketPrice` (Market Price) or `lowPrice` (lowest listing). |
| `sealed_types` | `Box`, `Pack`, `Tin` | Shopify product types to check. |
| `include_drafts` | `false` | Also check products that are not active (drafts, and also archived products). |
| `ignore_words` | sports-card brands etc. | Skip products whose title contains any of these (whole words). |
| `self_priced_words` | `damaged` | Treat as SELF-PRICED when the title contains any of these. |
| `new_release_days` | `30` | Sets released within this many days are flagged fast-moving. |
| `auto_match_score` | `0.75` | Minimum score for an automatic match (file only). |
| `check_match_score` | `0.35` | Below this score a listing is NO MATCH (file only). |
| `auto_match_max_diff_pct` | `75` | An automatic match this far from our price becomes CHECK MATCH (file only). |
| `tcg_categories` | list of tcgcsv category IDs | TCGplayer games to match against (file only). |
| `credentials_file` | | Path to the chosen `config.yaml`. |
| `text_size`, `window` | | Text size and window position, filter and sort. |

Edit `products.json` only while the app is closed.

## Outputs

All in the data folder (see [Data folder location](#data-folder-location)).

### reports/

- `price_report_YYYY-MM-DD.html`: printable report with summary counts, filter buttons and one row per
  listing (status, product, our price, cost and margin, TCGplayer price, low and margin, difference,
  matched TCGplayer product with link). Supports dark mode.
- `price_report_YYYY-MM-DD.csv`: same rows, UTF-8 with BOM. Columns:

| Column | Meaning |
| --- | --- |
| `tag` | Status (URGENT, DRIFTING, CHECK MATCH, OK, SELF-PRICED, NO PRICE, NO MATCH, NO DATA). |
| `presale` | Presale item. |
| `release` | TCGplayer release date for presale items. |
| `channel` | `POS` if the title contains `(POS)`, else `Online`. |
| `shopify_title`, `variant`, `type` | Shopify product title, variant, product type. |
| `our_price`, `cost`, `margin_pct` | Shopify price, cost per item, our margin %. |
| `tcg_price`, `tcg_margin_pct`, `below_cost` | TCGplayer comparison price, margin at that price, whether it is below our cost. |
| `diff_amt`, `diff_pct` | Our price minus TCGplayer, in dollars and %. |
| `tcg_low` | TCGplayer lowest listing. |
| `match`, `match_score` | Confirmed / Automatic / Needs check / Not found, and match score. |
| `tcg_name`, `tcg_set` | Matched TCGplayer product(s) and set(s). |
| `source`, `price_as_of` | Where the price came from, and its date if older than today's snapshot. |
| `snapshot_price`, `live_entered` | Snapshot price, and when a live price was typed in. |
| `fast_moving`, `status`, `stock`, `note` | Fast-moving flag, Shopify status, inventory, saved note. |

Reports are rewritten after every check (one pair per day). "Export this list..." writes the same
columns to a file you choose (default name `tcg_prices_<filter>_<date>.csv`).

### cache/

| File | Contents |
| --- | --- |
| `tcg_index.json` | TCGplayer sealed-product catalogue for the chosen games; only changed sets are re-downloaded. |
| `last_prices.json` | Last price seen per product, so a product with no current listings shows its last price. |
| `shopify_products.json` | Shopify products from the last check, for incremental updates. |
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

To ship the Mac app, zip `TCG Price Checker.app` (as in `TCG Price Checker.app.zip`).

## Troubleshooting

| Message / symptom | Fix |
| --- | --- |
| "No Shopify login file chosen yet" / "can't be found" | Open Settings > Choose file... and pick your `config.yaml`. |
| "has no filled-in 'shopify:' section" / "needs a store_url" / "needs client_id and client_secret" | Fill in the `shopify:` section (see [Configuration](#configuration)). |
| "Shopify didn't accept the login" | Check the credentials and that the Shopify app is installed on the store with permission to read products. |
| "Shopify login failed: ..." | The `client_id` / `client_secret` exchange failed. Check both values, or add an `admin_api_token`. |
| "Shopify API error: ..." | Shopify rejected the product query; the message names the cause, often a missing permission. |
| Cost and margin columns are blank | The Shopify app lacks inventory read access, or "Cost per item" is not filled in. |
| "A secure connection couldn't be checked" (Mac) | Run "Install Certificates.command" from Applications > Python 3.x. |
| "Couldn't connect to ..." | Check the internet connection, then Check prices now. |
| "... says it's busy right now" / "... is having problems right now (error 5xx)" | Wait a few minutes and try again. |
| Status bar time shown as a warning | The TCGplayer snapshot is over 30 hours old; tcgcsv may not have updated yet. |
| A product is matched to the wrong TCGplayer item | Select it > Choose a different match..., or Hide this product. |
| A product is missing from the list | Check its product type is ticked in Settings, it is active (or drafts are included), its title has no ignore word, and it was not hidden (see Saved choices). |
| A product deleted in Shopify still appears | Later checks in a day only fetch changed products. It drops out at the first check of the next day, or delete `cache/shopify_products.json` and check again. |
| Mac says the app cannot be opened | Right-click > Open > Open the first time. On an Intel Mac, the included Apple Silicon build will not run; build your own. |
| Settings or choices lost after an edit | A damaged `products.json` is renamed to `products.damaged.json` and `products.json.bak` is used. |
| Any other error in the window | Details are in `error_log.txt` in the data folder. |

## File guide

| Path | Notes |
| --- | --- |
| `tcg_price_checker.py` | The whole app: matching, Shopify and tcgcsv access, window, reports, build. |
| `TCG Price Checker.exe` | Prebuilt Windows app. |
| `TCG Price Checker.app.zip` | Prebuilt Mac app (zipped). |
| `products.json`, `products.json.bak` | Saved choices and settings (created at runtime, not in the repo). |
| `cache/`, `reports/`, `error_log.txt` | Created at runtime. |
| `config.yaml` | Your Shopify login file. Keep it out of git; it can live anywhere. |
