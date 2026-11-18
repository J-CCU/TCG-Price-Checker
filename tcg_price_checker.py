#!/usr/bin/env python3
"""TCG Price Checker - compares a Shopify shop's sealed product and TCG singles with TCGplayer prices.

Easiest: use the app ("TCG Price Checker.exe" on Windows, "TCG Price Checker.app" on Mac). It needs nothing
installed and can be moved anywhere; its saved choices, cache and reports live in the folder next to it.

Making the app (once per computer type, needs Python 3.10+ from python.org):
  * Mac:      open Terminal and run   python3 tcg_price_checker.py --build-app
              (Macs have no "python" command, only "python3", after installing Python from python.org)
  * Windows:  py tcg_price_checker.py --build-app
The app appears next to this file. Copying a Mac app to another Mac: the first time, right-click it > Open.

Running the script directly instead also works: double-click on Windows, or python3 tcg_price_checker.py on a Mac.

It opens showing the last check straight away, then checks again in the background. The first check of the
day reads every Shopify product with one bulk operation; later ones only fetch what changed (a few seconds).

  * Every active Shopify listing is shown. Sealed product (types Box / Pack / Tin by default) is matched to a
    TCGplayer sealed product; TCG singles (MTG / Pokemon / One Piece... by tag) to the TCGplayer card by set
    code, name, number, treatment and finish, priced by condition. Everything TCGplayer can't price (sports
    cards, graded slabs, accessories) is listed as NO PRICE SOURCE. Prices come from tcgcsv.com, TCGplayer's
    prices republished once a day.
  * Status: URGENT / DRIFTING = our price is that far from TCGplayer (percentages set in Settings; "Need
    update" shows both, with the suggested new price), CHECK MATCH = please confirm the TCGplayer product,
    plus cost, margin, presale and fast-moving flags, and stock per store / warehouse.
    "Update Shopify prices…" changes selected prices in Shopify, only after the operator confirms.
  * Choices people make in the window (confirmed matches, hidden products, typed-in live prices, settings)
    are saved in products.json next to this file. cache/ and reports/ are created here too.
  * Shopify login comes from the shopify: section of a config.yaml (store_url + client_id/client_secret,
    or admin_api_token). You choose the file the first time (and can change it in Settings); it's never
    searched for automatically, and no passwords are stored in this script or the app.

Read-only: it only reads from Shopify and tcgcsv, and never changes anything in the shop.

Command line (optional):  py tcg_price_checker.py --cli [--shopify-csv export.csv] [--show-zero] [--timings]
"""
import argparse
import concurrent.futures as cf
import csv
import datetime as dt
import html
import json
import math
import os
import queue
import re
import socket
import ssl
import sys
import threading
import time
import traceback
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

def app_folder():
    """Where products.json, cache/ and reports/ live: next to the app, so moving the folder moves everything.

    Works for the plain script, a packaged Windows .exe and a Mac .app (the folder holding the .app).
    Falls back to the user's app-data folder when that place can't be written to (e.g. Program Files, or a
    Mac app run straight from Downloads, which macOS starts from a hidden read-only copy).
    """
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).resolve()
        folder = exe.parents[3] if ".app/Contents/MacOS" in exe.as_posix() else exe.parent
    else:
        folder = Path(__file__).resolve().parent
    try:
        if "AppTranslocation" in folder.as_posix():
            raise OSError("read-only copy")
        if folder.as_posix().rstrip("/") in ("/Applications", str(Path.home() / "Applications")):
            raise OSError("installed in Applications: keep data in the user's app-data folder instead")
        probe = folder / ".write_test"
        probe.write_text("ok")
        probe.unlink()
        return folder
    except OSError:
        if sys.platform == "darwin":
            fallback = Path.home() / "Library" / "Application Support" / "TCG Price Checker"
        elif sys.platform == "win32":
            fallback = Path(os.environ.get("APPDATA", Path.home())) / "TCG Price Checker"
        else:
            fallback = Path.home() / ".tcg_price_checker"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


HERE = app_folder()
TCGCSV = "https://tcgcsv.com/tcgplayer"
USER_AGENT = "shop-price-check/2.0"
SHOPIFY_API_VERSION = os.environ.get("SHOPIFY_API_VERSION", "2026-07")
PRESALE_WORDS = ("presale", "pre-sale", "preorder", "pre-order")
SINGLE_CARD_FIELDS = {"Rarity", "Number"}
POS_MARK = re.compile(r"\(\s*POS\s*\)", re.I)  # shopfront marker; can appear anywhere in a title
TAG_ORDER = {"URGENT": 0, "DRIFTING": 1, "CHECK MATCH": 2, "OK": 3, "SELF-PRICED": 4, "NO PRICE": 5,
             "NO MATCH": 6, "NO PRICE SOURCE": 7, "NO DATA": 8}
NEED_UPDATE = ("URGENT", "DRIFTING")  # rows whose price should change
INDEX_VERSION = 1
SINGLES_VERSION = 1
# card conditions as Shopify variants spell them -> our short codes (tcgcsv prices are Near Mint)
CONDITIONS = {"NM": "Near Mint", "LP": "Lightly Played", "MP": "Moderately Played", "HP": "Heavily Played",
              "DMG": "Damaged"}
FIELD_NAMES = {"marketPrice": "TCGplayer Market Price", "lowPrice": "TCGplayer lowest listing"}
RARE_WORD_WEIGHT = 5.5  # word weight (idf) above which a word names something specific

DEFAULT_SETTINGS = {
    "drift_pct": 3,
    "urgent_pct": 10,
    "presale_drift_pct": 3,
    "presale_urgent_pct": 10,
    "tcg_price_field": "marketPrice",
    "sealed_types": ["Box", "Pack", "Tin"],
    "include_drafts": False,
    "ignore_words": ["panini", "topps", "upper deck", "bowman", "donruss", "leaf", "keepsake", "hobby box",
                     "heat seeker", "breaker edition"],
    "self_priced_words": ["damaged"],
    "auto_match_score": 0.75,
    "check_match_score": 0.35,
    # an automatic match this far from our price is more likely the wrong product: ask a person
    "auto_match_max_diff_pct": 75,
    # sets released this recently (and presale items) are flagged fast-moving: tcgcsv is a daily snapshot
    "new_release_days": 30,
    # TCGplayer games to match against (tcgcsv category ids)
    "tcg_categories": [1, 2, 3, 85, 68, 79, 86, 90, 21, 66, 71, 89, 76, 91, 93, 62, 63, 80, 81],
    # singles: price-check TCG single cards too (matched to TCGplayer cards by set code, name and number)
    "check_singles": True,
    # % of the Near Mint price a single in this condition should sell for
    "condition_pct": {"NM": 100, "LP": 85, "MP": 70, "HP": 55, "DMG": 40},
    # Shopify tag -> TCGplayer game (tcgcsv category id) for singles
    "singles_tags": {"MTG Singles": 1, "Magic Singles": 1, "Pokemon Singles": 3, "Pokemon Japan Singles": 85,
                     "One Piece Singles": 68, "Yu-Gi-Oh Singles": 2, "YuGiOh Singles": 2, "Lorcana Singles": 71,
                     "Star Wars Unlimited Singles": 79, "Flesh and Blood Singles": 62, "Digimon Singles": 63,
                     "Dragon Ball Singles": 80, "Union Arena Singles": 81, "Gundam Singles": 86,
                     "Riftbound Singles": 89},
    # singles with any of these tags (or title words) have no TCGplayer price: listed as NO PRICE SOURCE
    "no_price_source_tags": ["Sports Singles", "baseball", "football", "basketball", "hockey", "soccer", "ufc",
                             "wrestling", "racing", "golf", "Graded", "PSA", "BGS", "CGC"],
    # out-of-stock (0 or blank) listings are hidden in the window and reports unless this is on
    "show_zero_stock": False,
    # read every Shopify product with one bulk operation (much faster than paging); off = page through
    "shopify_bulk": True,
    # stock columns, in order: each adds up one or more Shopify locations. Locations not listed are left out
    # (also from the Stock total). An empty list = one column per Shopify location.
    "stock_columns": [
        {"name": "Warehouse", "locations": ["Cash Cards Unlimited Warehouse", "Relics Awakened"]},
        {"name": "Frank and Son", "locations": ["Frank and Son"]},
        {"name": "Janss", "locations": ["Janss Marketplace Storefront"]},
        {"name": "Woodland Hills", "locations": ["Woodland Hills Storefront"]},
    ],
}

# Matches already checked by hand; used to start products.json the first time this runs somewhere new.
ASCENDED_HEROES_EX_BOXES = [{"category": 3, "group": 24541, "product": p} for p in (672733, 672734, 672735)]
DEFAULT_OVERRIDES = [
    {"shopify_title": "Pokemon Mega Evolution - Ascended Heroes ex Box (Damaged Package)",
     "tcg": ASCENDED_HEROES_EX_BOXES,
     "note": "Damaged package - priced in-house; TCGplayer sealed ex Box average shown for reference only"},
    {"shopify_title": "Pokemon Mega Evolution - Ascended Heroes ex Box", "tcg": ASCENDED_HEROES_EX_BOXES,
     "note": "Average of Meganium / Emboar / Feraligatr ex Box (we ship whichever is needed)"},
    {"shopify_title": "Riftbound: League of Legends TCG - Showdown Decks: Evelynn vs Seraphine (RAD)",
     "tcg": [{"category": 89, "group": 24819, "product": 711368}]},
    {"shopify_title": "Yu-Gi-Oh! - Beyond the Brave Booster Pack [1st Edition]",
     "tcg": [{"category": 2, "group": 24815, "product": 711264}]},
    {"shopify_title": "Magic: The Gathering - Reality Fracture Collector Booster Pack",
     "tcg": [{"category": 1, "group": 24684, "product": 692982}]},
    {"shopify_title": "CookieRun Braverse TCG: Festival Arena Booster Pack",
     "tcg": [{"category": 90, "group": 24886, "product": 718118}]},
]


# ---------------------------------------------------------------- helpers

def _ssl_context():
    """Normal certificate checking. Python from python.org on a Mac has no certificates until its
    'Install Certificates' step is run, so also trust the Mac's own list (/etc/ssl/cert.pem)."""
    ctx = ssl.create_default_context()
    if sys.platform == "darwin" and Path("/etc/ssl/cert.pem").exists():
        ctx.load_verify_locations("/etc/ssl/cert.pem")
    return ctx


SSL_CONTEXT = _ssl_context()


def http_open(req, timeout=60):
    return urllib.request.urlopen(req, timeout=timeout, context=SSL_CONTEXT)


def http_json(url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with http_open(req) as resp:
        return json.load(resp)


def normalize_title(title):
    """Drop our internal markers so POS and online listings match the same entry."""
    t = POS_MARK.sub(" ", title or "")
    return re.sub(r"\s+", " ", t).strip().lower()


def is_pos(title):
    return bool(POS_MARK.search(title or ""))


def is_presale_text(text):
    text = (text or "").lower()
    return any(w in text for w in PRESALE_WORDS)


def to_float(value):
    try:
        return float(str(value).replace("$", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def to_int(value):
    f = to_float(value)
    return None if f is None else int(f)


def is_sealed(product):
    return not any(e.get("name") in SINGLE_CARD_FIELDS for e in product.get("extendedData") or [])


def money(v):
    return "" if v is None else f"{'-' if v < 0 else ''}${abs(v):,.2f}"


def pct(v):
    return "" if v is None else f"{v:+.1f}%"


def load_config(path):
    path = Path(path)
    config = None
    if path.exists():
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, UnicodeDecodeError):
            # damaged (e.g. edited by hand): keep it aside and carry on from the automatic backup
            path.replace(path.with_name("products.damaged.json"))
            try:
                config = json.loads(path.with_suffix(".json.bak").read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeDecodeError):
                config = None
    if config is None:  # first run on this computer (or nothing usable to recover)
        config = {"overrides": json.loads(json.dumps(DEFAULT_OVERRIDES))}
    config["settings"] = {**DEFAULT_SETTINGS, **config.get("settings", {})}
    for key in ("condition_pct",):  # tables: keep defaults for entries the file doesn't mention
        config["settings"][key] = {**DEFAULT_SETTINGS[key], **(config["settings"].get(key) or {})}
    config["overrides"] = config.get("overrides", config.pop("products", []))
    return config


def save_config(config, path):
    path = Path(path)
    if path.exists():
        path.with_suffix(".json.bak").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    path.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------- word matching

STOPWORDS = {"the", "of", "and", "a", "an", "tcg", "ccg", "card", "cards", "game", "trading", "official", "for",
             "with", "x"}
PHRASES = [  # applied to both sides so different wording lines up
    (r"\byu gi oh\b", "yugioh"),
    (r"\bbooster display\b", "booster box"),
    (r"\bbooster pack\b", "booster"),
    (r"\betb\b", "elite trainer box"),
    (r"\bjapan\b", "japanese"),
    (r"\bfirst edition\b", "1st edition"),
    (r"\bsecond edition\b", "2nd edition"),
    (r"\bvol\b", "volume"),
    (r"\bpokemon center\b", "pokemoncenter"),
    (r"\b(bundle|deck|tin|pack|kit|blister|collection|chest|case)s\b", r"\1"),
    (r"\bboxes\b", "box"),
]
# Our own notes in titles: (POS), (Damaged...), **Tear in Plastic**, (LiveStream Only), presale markers
SHOPIFY_NOISE = re.compile(r"\((?:pos|sealed|damaged[^)]*|[^)]*\bonly)\)|\*\*.*?\*\*|\*[^*]*damaged[^*]*\*|\bpre-?sale\b|\bpre-?order\b",
                           re.I)
# Words that make a different product: a "1st Edition" or Japanese listing must not auto-match one without them.
QUALIFIERS = {"japanese", "korean", "chinese", "german", "french", "italian", "spanish", "portuguese", "russian",
              "1st", "2nd", "unlimited", "sleeved",
              # product types: a "Booster Bundle" must not auto-match a "Booster Pack"
              "bundle", "box", "case", "tin", "deck", "blister", "collection", "kit", "chest", "pack"}
# Pokemon set codes on TCGplayer ("SV10: Destined Rivals") -> the series name we use in titles
POKEMON_SERIES = {"sv": "scarlet violet", "swsh": "sword shield", "me": "mega evolution", "sm": "sun moon",
                  "bw": "black white", "xy": "xy", "dp": "diamond pearl", "hgss": "heartgold soulsilver"}


def words(text):
    t = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    t = t.replace("&", " and ")
    t = re.sub(r"['’`]", "", t)
    t = re.sub(r"[^a-z0-9]+", " ", t)
    for pat, rep in PHRASES:
        t = re.sub(pat, rep, t)
    return {w for w in t.split() if w not in STOPWORDS}


# ---------------------------------------------------------------- tcgcsv sealed index

def release_date(group):
    """A set's real release date, or "" when tcgcsv only has a fetch timestamp (those aren't midnight)."""
    published = group.get("publishedOn") or ""
    return published[:10] if published[10:19] == "T00:00:00" else ""


FIELD_INDEX = {"marketPrice": 0, "lowPrice": 1}  # position in a cached price entry [market, low]
FETCH_THREADS = 16  # parallel downloads from tcgcsv (it's a static file server)


def card_key(text):
    """A card or set name for lookups: lower case, accents and punctuation dropped."""
    t = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    t = re.sub(r"['’`.]", "", t.replace("&", " and "))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", t).split())


def code_key(code):
    """Set code for lookups: 'PRB-01' / 'prb01' / 'OP 05' all compare equal."""
    return re.sub(r"[^a-z0-9]", "", (code or "").lower())


def number_key(text):
    """Collector number for comparing: '202/165' -> '202', '008' -> '8', 'OP05-041' -> 'op05-041'."""
    t = re.sub(r"[^a-z0-9-]", "", (text or "").split("/")[0].lower().lstrip("#")).strip("-")
    return str(int(t)) if t.isdigit() else t


def same_number(ours, theirs):
    """Collector numbers agree: equal, or one is just the number part of the other ('118' vs 'OP11-118')."""
    if not ours or not theirs or ours == theirs:
        return True
    tail = lambda n: (re.search(r"(\d+)[a-z]?$", n) or [None, None])[1]
    return (ours.isdigit() or theirs.isdigit()) and tail(ours) is not None and \
        int(tail(ours)) == int(tail(theirs) or -1)


NUMBER_SEGMENT = re.compile(r"^#?(?:[A-Za-z]{1,4}\d*-?)?\d+[A-Za-z]?(?:/[A-Za-z]*\d+)?$")
PAREN = re.compile(r"\(([^()]*)\)")


def split_card_name(name):
    """'Mewtwo EX (164 Secret Full Art)' -> ('mewtwo ex', '164', {'secret', 'full', 'art'});
    'Zapdos ex - 202/165' -> ('zapdos ex', '202', set()). Works for TCGplayer names and our titles."""
    number, treat = "", set()
    for inner in PAREN.findall(name):
        m = re.match(r"\s*#?(\d+[a-z]?)\b\s*(.*)$", inner, re.I)
        if m:
            number = number or number_key(m.group(1))
            inner = m.group(2)
        treat |= set(card_key(inner).split())
    rest = PAREN.sub(" ", name)
    parts = [p.strip() for p in rest.split(" - ")]
    keep = []
    for p in parts:
        if NUMBER_SEGMENT.match(p) and keep:
            number = number or number_key(p)
        elif p:
            keep.append(p)
    return card_key(" - ".join(keep)), number, treat


class TcgIndex:
    """Every TCGplayer product in the chosen games, cached on disk and refreshed when sets change.

    Sealed products of every set are kept in tcg_index.json (for matching sealed listings). Single cards are
    only downloaded for the sets we actually stock singles from, one small file per set in cache/cards/.
    Prices are cached per set in cache/prices/ for the current daily snapshot, so a second check the same
    day downloads nothing, and only sets whose file is missing or older are fetched.
    """

    def __init__(self, cache_dir):
        self.cache_dir = Path(cache_dir)
        self.path = self.cache_dir / "tcg_index.json"
        self.cards_dir = self.cache_dir / "cards"
        self.prices_dir = self.cache_dir / "prices"
        self.data = {"version": INDEX_VERSION, "categories": {}, "groups": {}}
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if loaded.get("version") == INDEX_VERSION:
                    self.data = loaded
            except ValueError:
                pass
        self._prices = {}
        self._built = False
        self._cards = {}       # "cat/group" -> lookup tables for that set's single cards
        self._codes = None     # (cat, set code) -> ["cat/group", ...]
        self._stamp = ""       # fingerprint of the sealed catalogue (see sealed_stamp)
        # last price seen per product (and per card finish): {"cat/group/id[/finish]": {"marketPrice": [price, date]}}
        self.history_path = self.cache_dir / "last_prices.json"
        try:
            saved = json.loads(self.history_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        if "prices" not in saved:  # older file: just the prices
            saved = {"prices": saved}
        self.history = saved["prices"]
        self.snapshot = ""       # tcgcsv's last-updated time for the snapshot in use, set by PriceChecker.run
        self.snapshot_date = ""

    def category_list(self):
        return http_json(f"{TCGCSV}/categories")["results"]

    @staticmethod
    def _sealed_entry(cid, g, products):
        sealed = []
        for p in products:
            if is_sealed(p):
                pre = p.get("presaleInfo") or {}
                sealed.append({"id": p["productId"], "name": p["name"], "url": p.get("url"),
                               "presale": bool(pre.get("isPresale")),
                               "release": (pre.get("releasedOn") or "")[:10]})
        return {"category": cid, "group": g["groupId"], "name": g["name"], "abbr": g.get("abbreviation") or "",
                "modifiedOn": g.get("modifiedOn"), "published": release_date(g), "products": sealed}

    @staticmethod
    def _card_rows(products):
        """Single cards of a set, compact: [id, name, number, rarity]."""
        rows = []
        for p in products:
            if is_sealed(p):
                continue
            ext = {e.get("name"): e.get("value") for e in p.get("extendedData") or []}
            rows.append([p["productId"], p["name"], ext.get("Number") or "", ext.get("Rarity") or ""])
        return rows

    def _write_cards(self, key, modified, rows):
        try:
            self.cards_dir.mkdir(parents=True, exist_ok=True)
            (self.cards_dir / f"{key.replace('/', '_')}.json").write_text(
                json.dumps({"version": SINGLES_VERSION, "modifiedOn": modified, "cards": rows}), encoding="utf-8")
        except OSError:
            pass
        self._cards.pop(key, None)

    def refresh(self, category_ids, progress=None):
        """Fetch new/changed sets. First run downloads everything (about a minute)."""
        cats = {c["categoryId"]: c for c in self.category_list()}
        wanted = [cid for cid in category_ids if cid in cats]

        def group_list(cid):
            try:
                return cid, http_json(f"{TCGCSV}/{cid}/groups")["results"]
            except Exception:  # category with no groups yet (tcgcsv returns a 404 XML page)
                return cid, []

        todo = []
        with cf.ThreadPoolExecutor(FETCH_THREADS) as ex:
            lists = list(ex.map(group_list, wanted))
        for cid, groups in lists:
            self.data["categories"][str(cid)] = {"name": cats[cid]["name"],
                                                 "display": cats[cid].get("displayName") or ""}
            for g in groups:
                key = f"{cid}/{g['groupId']}"
                cached = self.data["groups"].get(key)
                if not cached or cached.get("modifiedOn") != g.get("modifiedOn"):
                    todo.append((cid, g))
                else:
                    cached["published"] = release_date(g)
                    cached["abbr"] = g.get("abbreviation") or ""

        def fetch(job):
            cid, g = job
            try:
                products = http_json(f"{TCGCSV}/{cid}/{g['groupId']}/products")["results"]
            except Exception:  # (as before: a set tcgcsv can't serve is kept with no products)
                products = []
            return job, products

        changed = 0
        with cf.ThreadPoolExecutor(FETCH_THREADS) as ex:
            for i, ((cid, g), products) in enumerate(ex.map(fetch, todo), 1):
                key = f"{cid}/{g['groupId']}"
                self.data["groups"][key] = self._sealed_entry(cid, g, products)
                changed += 1
                # we already have this set's cards: keep them current from the same download
                if products and (self.cards_dir / f"{key.replace('/', '_')}.json").exists():
                    self._write_cards(key, g.get("modifiedOn"), self._card_rows(products))
                if progress and (i % 25 == 0 or i == len(todo)):
                    progress(f"Updating TCGplayer catalogue: {i} of {len(todo)} sets")
        # always save: release dates may have changed even when no set needed downloading
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data), encoding="utf-8")
        self._built = False
        self._codes = None
        self._stamp = ""
        return changed

    def _build(self):
        """Word lists + inverted index for matching."""
        self.products = []
        for g in self.data["groups"].values():
            cat = self.data["categories"].get(str(g["category"]), {})
            set_words = words(g["name"])
            context = set_words | words(cat.get("name", "")) | words(cat.get("display", ""))
            if g["abbr"]:
                context.add(g["abbr"].lower())
            code = re.match(r"([A-Za-z]+)\d*\s*:", g["name"])
            if code and code.group(1).lower() in POKEMON_SERIES:
                context |= words(POKEMON_SERIES[code.group(1).lower()])
            for p in g["products"]:
                name = words(p["name"])
                # Words that only repeat the set name ("Universes Beyond: ...") shouldn't count as extras.
                own = (name - set_words) or name
                self.products.append({**p, "category": g["category"], "group": g["group"], "set": g["name"],
                                      "set_released": g.get("published", ""),
                                      "game": cat.get("display") or cat.get("name", ""),
                                      "name_words": own, "all_words": name | context})
        df = {}
        self.postings = {}
        for i, p in enumerate(self.products):
            for w in p["all_words"]:
                df[w] = df.get(w, 0) + 1
                self.postings.setdefault(w, []).append(i)
        n = max(len(self.products), 1)
        self.idf = {w: math.log(n / c) + 1 for w, c in df.items()}
        self._built = True

    def sealed_stamp(self):
        """Fingerprint of the sealed catalogue: saved automatic matches stay valid while it's unchanged."""
        if self._stamp:
            return self._stamp
        import hashlib
        h = hashlib.sha1()
        for key in sorted(self.data["groups"]):
            g = self.data["groups"][key]
            h.update(f"{key}|{g['name']}|{g['abbr']}|{g.get('published', '')}|".encode())
            for p in g["products"]:
                h.update(f"{p['id']}:{p['name']}:{p.get('presale')}:{p.get('release')};".encode())
        self._stamp = h.hexdigest()
        return self._stamp

    def weight(self, w):
        return self.idf.get(w, 1.0)  # words TCGplayer never uses are probably our own noise

    def score(self, query, p):
        if not query:
            return 0.0
        wq = sum(self.weight(w) for w in query)
        wn = sum(self.weight(w) for w in p["name_words"]) or 1
        recall = sum(self.weight(w) for w in query & p["all_words"]) / wq          # how much of our title is explained
        precision = sum(self.weight(w) for w in p["name_words"] & query) / wn       # extra words like "Case" or "Display" hurt
        return recall * precision

    def candidates(self, text, limit=10):
        """Best TCGplayer sealed products for a Shopify title, best first: [(score, product), ...]."""
        if not self._built:
            self._build()
        query = words(SHOPIFY_NOISE.sub(" ", text))
        seeds = sorted((w for w in query if w in self.postings), key=self.weight, reverse=True)[:3]
        pool = set()
        for w in seeds:
            pool.update(self.postings[w])
        scored = sorted(((self.score(query, self.products[i]), self.products[i]) for i in pool),
                        key=lambda sp: sp[0], reverse=True)
        return scored[:limit]

    def product(self, category, group, product_id):
        g = self.data["groups"].get(f"{category}/{group}")
        if not g:
            return None
        found = next(({**p, "category": category, "group": group, "set": g["name"],
                       "set_released": g.get("published", "")} for p in g["products"] if p["id"] == product_id), None)
        if found is None:  # a single card
            card = self.cards(f"{category}/{group}", download=False)["by_id"].get(product_id)
            if card:
                found = self.card_ref(card)
        return found

    # ---------------------------------------------------------------- single cards
    def groups_for_code(self, category, code):
        """Sets in a game with this set code (TCGplayer's abbreviation), e.g. (1, 'DSK')."""
        if self._codes is None:
            self._codes, self._set_names = {}, {}
            for key, g in self.data["groups"].items():
                if g.get("abbr"):
                    self._codes.setdefault((g["category"], code_key(g["abbr"])), []).append(key)
                self._set_names.setdefault((g["category"], card_key(g["name"])), []).append(key)
        return self._codes.get((category, code_key(code)), [])

    def groups_for_set_name(self, category, name):
        self.groups_for_code(category, "")
        return self._set_names.get((category, card_key(name)), [])

    def ensure_cards(self, keys, progress=None):
        """Make sure the single-card lists of these sets are on disk and current (download the rest)."""
        todo = []
        for key in set(keys):
            g = self.data["groups"].get(key)
            if not g or key in self._cards:
                continue
            path = self.cards_dir / f"{key.replace('/', '_')}.json"
            try:
                saved = json.loads(path.read_text(encoding="utf-8"))
                if saved.get("version") == SINGLES_VERSION and saved.get("modifiedOn") == g.get("modifiedOn"):
                    self._cards[key] = self._card_tables(key, saved["cards"])
                    continue
            except (OSError, ValueError):
                pass
            todo.append(key)

        def fetch(key):
            cid, gid = key.split("/")
            try:
                return key, http_json(f"{TCGCSV}/{cid}/{gid}/products")["results"]
            except Exception:
                return key, None

        with cf.ThreadPoolExecutor(FETCH_THREADS) as ex:
            for i, (key, products) in enumerate(ex.map(fetch, todo), 1):
                if products is not None:
                    rows = self._card_rows(products)
                    self._write_cards(key, self.data["groups"][key].get("modifiedOn"), rows)
                    self._cards[key] = self._card_tables(key, rows)
                if progress and (i % 25 == 0 or i == len(todo)):
                    progress(f"Downloading TCGplayer card lists: {i} of {len(todo)} sets")
        return len(todo)

    def _card_tables(self, key, rows):
        g = self.data["groups"].get(key, {})
        exact, base, by_id = {}, {}, {}
        for pid, name, number, rarity in rows:
            b, num, treat = split_card_name(name)
            card = {"id": pid, "name": name, "number": number, "rarity": rarity, "num": number_key(number) or num,
                    "treat": treat, "base": b, "category": g.get("category"), "group": g.get("group"),
                    "set": g.get("name", ""), "set_released": g.get("published", "")}
            exact.setdefault(card_key(name), []).append(card)
            base.setdefault(b, []).append(card)
            by_id[pid] = card
        return {"exact": exact, "base": base, "by_id": by_id}

    def cards(self, key, download=True):
        if key not in self._cards:
            if download:
                self.ensure_cards([key])
            else:  # only what's on disk
                path = self.cards_dir / f"{key.replace('/', '_')}.json"
                try:
                    self._cards[key] = self._card_tables(key, json.loads(path.read_text(encoding="utf-8"))["cards"])
                except (OSError, ValueError, KeyError):
                    return {"exact": {}, "base": {}, "by_id": {}}
        return self._cards.get(key) or {"exact": {}, "base": {}, "by_id": {}}

    def card_ref(self, card):
        cat = self.data["categories"].get(str(card["category"]), {})
        return {"id": card["id"], "name": card["name"], "category": card["category"], "group": card["group"],
                "game": cat.get("display") or cat.get("name", ""),
                "set": card["set"], "set_released": card.get("set_released", ""), "number": card["number"],
                "url": f"https://www.tcgplayer.com/product/{card['id']}", "single": True}

    def search_cards(self, text, categories=None, limit=40):
        """Single cards (in the sets downloaded so far) whose name fits the text, best first."""
        q = set(card_key(SHOPIFY_NOISE.sub(" ", text)).split())
        if not q:
            return []
        if not self._cards and self.cards_dir.is_dir():  # (window opened on the last results: read from disk)
            for path in self.cards_dir.glob("*_*.json"):
                self.cards(path.stem.replace("_", "/"), download=False)
        scored = []
        for key, tables in self._cards.items():
            if categories and int(key.split("/")[0]) not in categories:
                continue
            set_words = set(card_key(self.data["groups"].get(key, {}).get("name", "")).split())
            set_words.add(code_key(self.data["groups"].get(key, {}).get("abbr", "")))
            for card in tables["by_id"].values():
                cw = set(card_key(card["name"]).split()) | {card["num"]}
                hit = q & cw
                if not hit:
                    continue
                score = len(hit) / len(q | cw) + 0.5 * len(q & set_words) / len(q)
                scored.append((min(score, 1.0), self.card_ref(card)))
        scored.sort(key=lambda sp: sp[0], reverse=True)
        return scored[:limit]

    # ---------------------------------------------------------------- prices
    def load_prices(self, keys, progress=None, remember=()):
        """Prices for the given (category, group) pairs. Sets already downloaded for today's snapshot come
        from cache/prices/; the rest are fetched in parallel. remember: (cat, group, id, finish) of single
        cards whose last-seen price should be kept."""
        todo = []
        for k in set(keys):
            if k in self._prices:
                continue
            path = self.prices_dir / f"{k[0]}_{k[1]}.json"
            if self.snapshot:
                try:
                    saved = json.loads(path.read_text(encoding="utf-8"))
                    if saved.get("snapshot") == self.snapshot:
                        self._prices[k] = saved["prices"]
                        continue
                except (OSError, ValueError, KeyError):
                    pass
            todo.append(k)

        def fetch(key):
            try:
                rows = http_json(f"{TCGCSV}/{key[0]}/{key[1]}/prices")["results"]
            except Exception:
                return key, None
            by_id = {}
            for r in rows:
                by_id.setdefault(str(r["productId"]), {})[r.get("subTypeName") or "Normal"] = [
                    r.get("marketPrice"), r.get("lowPrice")]
            return key, by_id

        failed, fetched = [], []
        with cf.ThreadPoolExecutor(FETCH_THREADS) as ex:
            for i, (key, value) in enumerate(ex.map(fetch, todo), 1):
                if value is None:
                    failed.append(key)
                    continue
                self._prices[key] = value
                fetched.append(key)
                if self.snapshot:
                    try:
                        self.prices_dir.mkdir(parents=True, exist_ok=True)
                        (self.prices_dir / f"{key[0]}_{key[1]}.json").write_text(
                            json.dumps({"snapshot": self.snapshot, "prices": value}), encoding="utf-8")
                    except OSError:
                        pass
                if progress and (i % 25 == 0 or i == len(todo)):
                    progress(f"Loading TCGplayer prices: {i} of {len(todo)} sets")
        if todo and len(failed) > len(todo) // 2:
            raise ConnectionError("Couldn't download TCGplayer prices from tcgcsv.com")
        if self.snapshot_date:
            self._remember(fetched, remember)

    @staticmethod
    def _row(rows, finish=None):
        """[market, low] for a finish; sealed product is "Normal" (fall back to whatever subtype exists)."""
        if not rows:
            return None
        if finish:
            return rows.get(finish)
        return rows.get("Normal") or next(iter(rows.values()))

    def finishes(self, category, group, product_id):
        """TCGplayer subtypes (Normal, Foil, Holofoil, ...) that have a price row for this product today."""
        return list((self._prices.get((category, group)) or {}).get(str(product_id), {}))

    def _remember(self, keys, cards=()):
        changed = False
        for key in keys:  # sealed products: as before, every one in the set
            g = self.data["groups"].get(f"{key[0]}/{key[1]}", {})
            prices = self._prices.get(key, {})
            for p in g.get("products", []):
                row = self._row(prices.get(str(p["id"])))
                if row:
                    entry = self.history.setdefault(f"{key[0]}/{key[1]}/{p['id']}", {})
                    for f, i in FIELD_INDEX.items():
                        if row[i] is not None:
                            entry[f] = [row[i], self.snapshot_date]
                            changed = True
        for c, g, pid, finish in cards:  # single cards: only the ones we stock, per finish
            row = self._row((self._prices.get((c, g)) or {}).get(str(pid)), finish)
            if row:
                entry = self.history.setdefault(f"{c}/{g}/{pid}/{finish}", {})
                for f, i in FIELD_INDEX.items():
                    if row[i] is not None and entry.get(f) != [row[i], self.snapshot_date]:
                        entry[f] = [row[i], self.snapshot_date]
                        changed = True
        if not changed:
            return
        try:
            self.history_path.parent.mkdir(parents=True, exist_ok=True)
            self.history_path.write_text(json.dumps({"snapshot": self.snapshot, "prices": self.history}),
                                         encoding="utf-8")
        except OSError:
            pass

    def price(self, category, group, product_id, field, finish=None):
        """Returns (price, as_of, cheapest listing). Only the chosen field is used (Market Price by default).
        finish: the TCGplayer subtype for single cards (None for sealed product).

        as_of is "" for today's snapshot, or the date of the last snapshot that had a price: tcgcsv leaves
        out products with no listings, but TCGplayer still shows their last Market Price.
        """
        seen = self.history.get(f"{category}/{group}/{product_id}" + (f"/{finish}" if finish else ""), {})
        row = self._row((self._prices.get((category, group)) or {}).get(str(product_id)), finish)
        i = FIELD_INDEX.get(field, 0)
        if row and row[i] is not None:
            return row[i], "", row[1]
        low = row[1] if row else None
        if seen.get(field):
            return seen[field][0], seen[field][1], low
        return None, "", low

    @staticmethod
    def last_updated():
        req = urllib.request.Request("https://tcgcsv.com/last-updated.txt", headers={"User-Agent": USER_AGENT})
        with http_open(req, timeout=30) as resp:
            return resp.read().decode().strip()


# ---------------------------------------------------------------- shopify

def shop_domain(store_url):
    """Accept 'my-store', 'my-store.myshopify.com', 'https://...', or 'admin.shopify.com/store/my-store'."""
    store = re.sub(r"^https?://", "", store_url.strip()).rstrip("/")
    m = re.match(r"admin\.shopify\.com/store/([^/]+)", store)
    if m:
        store = m.group(1)
    store = store.split("/")[0]
    return store if "." in store else store + ".myshopify.com"


class SetupError(Exception):
    """Something the user needs to fix (credentials, files)."""


def read_yaml_section(text, section):
    """The simple `key: "value"` lines under one top-level section of a YAML file.

    Uses PyYAML when it's installed; otherwise this small reader is enough for the config.yaml layout,
    so nobody has to install anything.
    """
    try:
        import yaml
        return (yaml.safe_load(text) or {}).get(section) or {}
    except ImportError:
        pass
    values, inside = {}, False
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line[0].isspace():  # a new top-level section
            inside = line.split(":", 1)[0].strip() == section
            continue
        if not inside or line.startswith(" " * 4):  # skip nested sub-sections
            continue
        key, _, value = line.strip().partition(":")
        value = value.strip()
        if value and value[0] in "\"'":
            value = value[1:].split(value[0], 1)[0]
        else:
            value = value.split(" #", 1)[0].strip()
        values[key.strip()] = value
    return values


def check_credentials_file(path):
    """None if this looks like a usable Shopify login file, otherwise what's wrong in plain words."""
    if not path:
        return "No Shopify login file chosen yet. Open Settings and choose your config.yaml."
    path = Path(path)
    if not path.is_file():
        return f"The Shopify login file can't be found:\n{path}\n\nOpen Settings and choose it again."
    try:
        cfg = read_yaml_section(path.read_text(encoding="utf-8"), "shopify")
    except (OSError, UnicodeDecodeError, ValueError) as e:
        return f"Couldn't read {path.name}: {e}"
    cfg = {k: str(v).strip() for k, v in (cfg or {}).items() if v}
    if not cfg:
        return f"{path.name} has no filled-in 'shopify:' section."
    if not cfg.get("store_url"):
        return f"{path.name}: the shopify: section needs a store_url."
    if not (cfg.get("admin_api_token") or cfg.get("app_automation_token")
            or (cfg.get("client_id") and cfg.get("client_secret"))):
        return f"{path.name}: the shopify: section needs client_id and client_secret (or an admin_api_token)."
    return None


def friendly_error(exc, stage=""):
    """Turn a network/login failure into something a person can act on."""
    where = {"shopify": "Shopify", "tcgplayer": "TCGplayer's price data (tcgcsv.com)"}.get(stage, "the internet")
    if isinstance(exc, SetupError):
        return str(exc)
    if isinstance(exc, urllib.error.HTTPError):
        if stage == "shopify" and exc.code in (401, 403):
            return ("Shopify didn't accept the login.\n\nCheck the client_id / client_secret in your config.yaml, "
                    "and that the Shopify app is installed on the store with permission to read products.")
        if exc.code == 429:
            return f"{where} says it's busy right now. Wait a minute and press Check prices now again."
        if exc.code >= 500:
            return f"{where} is having problems right now (error {exc.code}). Try again in a few minutes."
        return f"{where} answered with error {exc.code} ({exc.reason})."
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, ssl.SSLError) or "CERTIFICATE" in str(exc).upper():
        return ("A secure connection couldn't be checked. On a Mac, open Applications > Python 3.x and run "
                "'Install Certificates.command', then try again.")
    if isinstance(exc, (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError)):
        return (f"Couldn't connect to {where}.\n\nCheck that this computer is online, then press "
                "Check prices now again.")
    return f"Something went wrong: {exc}"


def shopify_credentials(config_path):
    """Return (store, token) from env vars, else the shopify: section of the shared config.yaml.

    Token order: admin_api_token, then a client-credentials exchange with client_id/client_secret,
    then app_automation_token. Returns None when nothing usable is configured.
    """
    if os.environ.get("SHOPIFY_STORE") and os.environ.get("SHOPIFY_TOKEN"):
        return shop_domain(os.environ["SHOPIFY_STORE"]), os.environ["SHOPIFY_TOKEN"]
    if not config_path or not Path(config_path).exists():
        return None
    cfg = read_yaml_section(Path(config_path).read_text(encoding="utf-8"), "shopify")
    cfg = {k: str(v).strip() for k, v in cfg.items() if v}
    if not cfg.get("store_url"):
        return None
    store = shop_domain(cfg["store_url"])
    if cfg.get("admin_api_token"):
        return store, cfg["admin_api_token"]
    if cfg.get("client_id") and cfg.get("client_secret"):
        # Dev Dashboard apps installed on our own store can swap id/secret for a 24h Admin API token.
        body = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": cfg["client_id"],
                                       "client_secret": cfg["client_secret"]}).encode()
        try:
            data = http_json(f"https://{store}/admin/oauth/access_token", data=body,
                             headers={"Content-Type": "application/x-www-form-urlencoded"})
            return store, data["access_token"]
        except Exception as e:
            if not cfg.get("app_automation_token"):
                raise SetupError(f"Shopify login failed: {e}")
            print(f"Client-credentials login failed ({e}); using app_automation_token", file=sys.stderr)
    if cfg.get("app_automation_token"):
        return store, cfg["app_automation_token"]
    return None


class ShopifyReader:
    """Read-only access to the shop's products through the Admin GraphQL API.

    Only queries are sent. A full read uses one bulk operation (bulkOperationRunQuery: Shopify builds a
    file of every variant, which is then downloaded), far faster than paging for a big catalogue. Reads
    of what changed since the last check page through productVariants instead. Bulk operations are never
    cancelled: if Shopify won't start one, paging is used.

    Stock per location needs the read_inventory and read_locations permissions, cost per item needs
    read_inventory. Without them the read carries on with those left blank, and self.notes says so.
    """

    COST = "unitCost { amount }"
    LEVEL = 'location { name } quantities(names: ["available"]) { quantity }'
    PAGE_LEVELS = "inventoryLevels(first: 20) { nodes { " + LEVEL + " } }"
    BULK_LEVELS = "inventoryLevels { edges { node { " + LEVEL + " } } }"
    PRODUCT = "product { id title status tags productType }"

    def __init__(self, store, token, progress=None, use_bulk=True):
        self.url = f"https://{store}/admin/api/{SHOPIFY_API_VERSION}/graphql.json"
        self.token = token
        self.progress = progress or (lambda msg: None)
        self.use_bulk = use_bulk
        self.cost = True        # can read "Cost per item"
        self.levels = True      # can read stock per location
        self.notes = []
        self.how = ""           # "bulk" / "paging", for timings

    # ---------------------------------------------------------------- plumbing
    def gql(self, query, variables=None):
        """Run one GraphQL query; retries when throttled. Returns (data, errors)."""
        body = json.dumps({"query": query, "variables": variables or {}}).encode()
        for attempt in range(8):
            data = http_json(self.url, data=body, headers={"X-Shopify-Access-Token": self.token,
                                                           "Content-Type": "application/json"})
            errors = data.get("errors")
            if errors and "THROTTLED" in json.dumps(errors):
                time.sleep(2)
                continue
            return data.get("data"), errors
        raise SetupError(f"Shopify API error: {errors}")

    def _denied(self, errors):
        """Drop a field Shopify won't let us read; True if there was one to drop."""
        text = json.dumps(errors).lower()
        if not any(w in text for w in ("access_denied", "access denied", "read_inventory", "read_locations")):
            return False
        if self.levels:
            self.levels = False
            self.notes.append("Shopify didn't allow reading stock per location (the app needs the read_inventory "
                              "and read_locations permissions): showing total stock only")
            return True
        if self.cost:
            self.cost = False
            self.notes.append("Shopify didn't allow reading cost per item (needs read_inventory): costs left blank")
            return True
        return False

    def _variant_fields(self, bulk):
        inner = " ".join(f for f, on in ((self.COST, self.cost),
                                         (self.BULK_LEVELS if bulk else self.PAGE_LEVELS, self.levels)) if on)
        item = f"inventoryItem {{ {inner} }}" if inner else ""
        return f"id title price compareAtPrice sku inventoryQuantity {self.PRODUCT} {item}"

    def _record(self, v, levels):
        p = v.get("product") or {}
        cost = ((v.get("inventoryItem") or {}).get("unitCost") or {}).get("amount")
        stock_at = None
        if self.levels and levels is not None:
            stock_at = {}
            for lv in levels:
                name = (lv.get("location") or {}).get("name") or "?"
                qty = sum(q.get("quantity") or 0 for q in lv.get("quantities") or [])
                stock_at[name] = stock_at.get(name, 0) + qty
        return {
            "product_id": p.get("id"),
            "variant_id": v.get("id"),
            "title": p.get("title") or "",
            "variant": "" if v.get("title") == "Default Title" else (v.get("title") or ""),
            "price": to_float(v.get("price")),
            "compare_at": to_float(v.get("compareAtPrice")),
            "cost": to_float(cost),
            "stock": v.get("inventoryQuantity"),
            "locations": stock_at,
            "status": p.get("status", ""),
            "type": p.get("productType") or "",
            "tags": ", ".join(p.get("tags") or []),
        }

    # ---------------------------------------------------------------- full / changed reads
    def variants(self, changed_since=None):
        """Every variant (or only those changed since a time). Tries a bulk operation first."""
        q = f"updated_at:>'{changed_since}'" if changed_since else None
        if self.use_bulk:
            if q:  # a few changes: paging is quicker than starting a bulk operation
                items = self._paged(q, give_up_after=2000)
                if items is not None:
                    return items
            items = self._bulk(q)
            if items is not None:
                return items
        return self._paged(q)

    def _paged(self, q, give_up_after=None):
        """Page through the variants. None if there are more than give_up_after (a bulk read is quicker)."""
        self.how = "paging"
        out, cursor, size = [], None, 80
        while True:
            query = ("query($cursor: String, $size: Int!, $q: String) { productVariants(first: $size, after: $cursor, "
                     "query: $q) { pageInfo { hasNextPage endCursor } nodes { "
                     + self._variant_fields(bulk=False) + " } } }")
            data, errors = self.gql(query, {"cursor": cursor, "size": size, "q": q})
            if errors:
                if "MAX_COST_EXCEEDED" in json.dumps(errors) and size > 10:
                    size //= 2
                    continue
                if self._denied(errors):
                    continue
                raise SetupError(f"Shopify API error: {errors}")
            page = data["productVariants"]
            for v in page["nodes"]:
                levels = ((v.get("inventoryItem") or {}).get("inventoryLevels") or {}).get("nodes")
                out.append(self._record(v, levels))
            self.progress(f"Reading Shopify products: {len(out)} variants")
            if not page["pageInfo"]["hasNextPage"]:
                return out
            if give_up_after and len(out) >= give_up_after:
                return None
            cursor = page["pageInfo"]["endCursor"]

    def _bulk(self, q):
        """One bulk operation for every variant. None when Shopify won't run one (then we page instead)."""
        self.how = "bulk"
        while True:
            root = f'productVariants(query: {json.dumps(q)})' if q else "productVariants"
            inner = "{ " + root + " { edges { node { " + self._variant_fields(bulk=True) + " } } } }"
            data, errors = self.gql("mutation($q: String!) { bulkOperationRunQuery(query: $q) { "
                                    "bulkOperation { id status } userErrors { field message } } }", {"q": inner})
            if errors:
                if self._denied(errors):
                    continue
                return None
            result = data["bulkOperationRunQuery"]
            if result.get("userErrors"):
                text = json.dumps(result["userErrors"])
                if self._denied(result["userErrors"]):
                    continue
                return None  # e.g. another bulk operation already running: page through instead
            op = result["bulkOperation"]
            break
        started, wait = time.time(), 0.5
        while True:
            time.sleep(wait)
            wait = min(wait * 1.5, 3)
            data, errors = self.gql("query($id: ID!) { node(id: $id) { ... on BulkOperation { id status errorCode "
                                    "objectCount url partialDataUrl } } }", {"id": op["id"]})
            node = (data or {}).get("node") or {}
            status = node.get("status")
            self.progress(f"Shopify is preparing the product list: {node.get('objectCount') or 0} rows")
            if status == "COMPLETED":
                break
            if status in ("FAILED", "CANCELED", "CANCELING", "EXPIRED"):
                if node.get("errorCode") == "ACCESS_DENIED" and self._denied([{"extensions": {"code": "ACCESS_DENIED"}}]):
                    return self._bulk(q)
                return None
            if time.time() - started > 900:
                return None  # leave it running (never cancel); page instead
        if not node.get("url"):
            return []  # nothing matched
        self.progress("Downloading the Shopify product list…")
        req = urllib.request.Request(node["url"], headers={"User-Agent": USER_AGENT})
        variants, levels = [], {}
        with http_open(req, timeout=600) as resp:
            for line in resp:
                if not line.strip():
                    continue
                obj = json.loads(line)
                parent = obj.get("__parentId")
                if parent:
                    levels.setdefault(parent, []).append(obj)
                else:
                    variants.append(obj)
        return [self._record(v, levels.get(v["id"], []) if self.levels else None) for v in variants]

    def changed_products(self, changed_since):
        """Products whose title / status / tags / type changed: {product id: fields}."""
        out, cursor = {}, None
        while True:
            data, errors = self.gql(
                "query($cursor: String, $q: String) { products(first: 250, after: $cursor, query: $q) { "
                "pageInfo { hasNextPage endCursor } nodes { id title status tags productType } } }",
                {"cursor": cursor, "q": f"updated_at:>'{changed_since}'"})
            if errors:
                raise SetupError(f"Shopify API error: {errors}")
            page = data["products"]
            for p in page["nodes"]:
                out[p["id"]] = {"title": p["title"], "status": p.get("status", ""), "type": p.get("productType") or "",
                                "tags": ", ".join(p.get("tags") or [])}
            if not page["pageInfo"]["hasNextPage"]:
                return out
            cursor = page["pageInfo"]["endCursor"]


PRICE_UPDATE = ("mutation($productId: ID!, $variants: [ProductVariantsBulkInput!]!) { "
                "productVariantsBulkUpdate(productId: $productId, variants: $variants) { "
                "productVariants { id price } userErrors { field message } } }")


def push_prices(store, token, changes, progress=None):
    """Change our prices in Shopify. The only thing in this program that writes to Shopify, and it's only
    run after a person confirms the list in the window. changes: [(product_id, variant_id, new price)].
    Returns {variant_id: (True, price) or (False, reason)}. Needs the write_products permission."""
    progress = progress or (lambda msg: None)
    reader = ShopifyReader(store, token)
    by_product = {}
    for product_id, variant_id, price in changes:
        by_product.setdefault(product_id, []).append((variant_id, price))
    results = {}
    for n, (product_id, variants) in enumerate(by_product.items(), 1):
        progress(f"Updating Shopify prices: {n} of {len(by_product)} products")
        data, errors = reader.gql(PRICE_UPDATE, {"productId": product_id, "variants": [
            {"id": vid, "price": f"{price:.2f}"} for vid, price in variants]})
        if errors:
            text = json.dumps(errors)
            reason = ("the Shopify app isn't allowed to change products (it needs the write_products permission)"
                      if "access" in text.lower() else f"Shopify error: {text[:200]}")
            results.update({vid: (False, reason) for vid, _ in variants})
            continue
        result = data["productVariantsBulkUpdate"]
        if result.get("userErrors"):
            reason = "; ".join(e.get("message", "") for e in result["userErrors"])
            results.update({vid: (False, reason) for vid, _ in variants})
            continue
        saved = {v["id"]: to_float(v["price"]) for v in result.get("productVariants") or []}
        for vid, price in variants:
            results[vid] = (True, saved.get(vid, price))
    return results


def shopify_from_api(store, token, progress=None, changed_since=None):
    """Every variant of every product in our shop (or only those changed since a time)."""
    return ShopifyReader(store, token, progress).variants(changed_since)


def shopify_from_csv(path):
    """Read Shopify's product export (variant rows inherit the title via Handle)."""
    first_by_handle = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            row = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
            handle = row.get("handle", "")
            if handle and row.get("title"):
                first_by_handle[handle] = row
            parent = first_by_handle.get(handle, row)
            title = row.get("title") or parent.get("title", "")
            price = to_float(row.get("variant price") or row.get("price"))
            if not title or price is None:
                continue
            variant = " / ".join(v for v in (row.get("option1 value"), row.get("option2 value")) if v and v != "Default Title")
            yield {
                "title": title,
                "variant": variant,
                "price": price,
                "compare_at": to_float(row.get("variant compare at price") or row.get("compare at price")),
                "cost": to_float(row.get("cost per item")),
                "stock": to_int(row.get("variant inventory qty")),
                "locations": None,
                "status": (parent.get("status") or "active").upper(),
                "type": parent.get("type") or parent.get("product type") or "",
                "tags": parent.get("tags", ""),
            }


def status_ok(item, settings):
    """Active (or draft, when drafts are wanted). Archived products are never listed."""
    allowed = ("ACTIVE", "", "DRAFT") if settings.get("include_drafts") else ("ACTIVE", "")
    return item.get("status", "").upper() in allowed


def is_tracked(item, settings):
    """Sealed product we sell: right product type, active (unless drafts wanted), not on the ignore list."""
    if item.get("type", "").lower() not in {t.lower() for t in settings["sealed_types"]}:
        return False
    if not status_ok(item, settings):
        return False
    return not has_any_word(item["title"], settings.get("ignore_words", []))


_WORD_RES = {}


def has_any_word(text, phrases):
    """Whole-word match, so "leaf" skips Leaf sports cards but not Leafeon."""
    key = tuple(p.strip().lower() for p in phrases if p.strip())
    if not key:
        return False
    rx = _WORD_RES.get(key)
    if rx is None:  # one compiled pattern per word list: this runs for every listing
        rx = _WORD_RES[key] = re.compile("|".join(rf"(?<![a-z0-9]){re.escape(p)}(?![a-z0-9])" for p in key))
    return bool(rx.search((text or "").lower()))


def tag_list(item):
    return [t.strip().lower() for t in (item.get("tags") or "").split(",") if t.strip()]


def item_kind(item, settings):
    """('sealed', None), ('single', tcgcsv category id or None) or ('other', None).

    other = something TCGplayer can't price for us (sports cards, graded slabs, accessories, merch...):
    it's listed with status NO PRICE SOURCE and never matched.
    """
    title, ptype = item.get("title") or "", (item.get("type") or "").lower()
    if has_any_word(title, settings.get("ignore_words", [])):
        return "other", None
    if ptype in {t.lower() for t in settings["sealed_types"]}:
        return "sealed", None
    if not settings.get("check_singles", True) or "graded" in ptype or "slab" in ptype:
        return "other", None
    tags = tag_list(item)
    blocked = {t.lower() for t in settings.get("no_price_source_tags", [])}
    if blocked & set(tags):
        return "other", None
    games = {k.lower(): v for k, v in (settings.get("singles_tags") or {}).items()}
    game = next((games[t] for t in tags if t in games), None)
    if game is not None:
        return "single", game
    if ptype == "singles" and PAREN.search(title):  # a TCG single without a game tag: try every game
        return "single", None
    return "other", None


LANGUAGES = ("japanese", "korean", "chinese", "german", "french", "italian", "spanish", "portuguese", "russian")


def single_details(item):
    """Condition, finish wanted, edition and language from a single's variant (and title)."""
    v = re.sub(r"\s+", " ", (item.get("variant") or "").lower())
    t = (item.get("title") or "").lower()
    cond = "NM"
    for code, pattern in (("DMG", r"\bdamaged\b|\bdmg\b"), ("HP", r"\bheavily\b|\bhp\b"),
                          ("MP", r"\bmoderately\b|\bmp\b"), ("LP", r"\blightly\b|\blp\b"),
                          ("NM", r"\bnear[ -]?mint\b|\bnm\b")):
        if re.search(pattern, v):
            cond = code
            break
    both = f"{v} {t}"
    if re.search(r"\bnon[- ]?foil\b", v):
        finish = "normal"
    elif "reverse" in v or re.search(r"\)\s*-?\s*reverse\b|\breverse holo", t):
        finish = "reverse"
    elif re.search(r"\b(foil|holo|holofoil)\b", v) or re.search(r"\b(holofoil|holo)\s*$", t):
        finish = "foil"
    elif re.search(r"\bnormal\b", v):
        finish = "normal"
    else:
        finish = None
    edition = "1st" if re.search(r"\b1st\b|first edition", both) else ("unlimited" if "unlimited" in both else None)
    language = next((lang for lang in LANGUAGES if re.search(rf"\b{lang}\b", both)), "english")
    return {"condition": cond, "finish": finish, "edition": edition, "language": language}


def resolve_finish(available, wanted, edition):
    """Pick the TCGplayer subtype (Normal / Foil / Holofoil / Reverse Holofoil / 1st Edition ...) for a single.
    Returns (subtype, sure)."""
    if not available:
        return None, False
    opts, sure = list(available), True
    first = [a for a in opts if a.startswith("1st Edition")]
    unlimited = [a for a in opts if a.startswith("Unlimited")]
    if first or unlimited:
        if edition == "1st" and first:
            opts = first
        elif edition == "unlimited" and unlimited:
            opts = unlimited
        elif first and unlimited:  # not said which edition: Unlimited is the usual one, but please check
            opts, sure = unlimited, False
        else:
            opts = first or unlimited
    foil = lambda a: "foil" in a.lower() or "holo" in a.lower()
    if wanted == "reverse":
        pick = [a for a in opts if "reverse" in a.lower()]
    elif wanted == "foil":
        pick = [a for a in opts if foil(a) and "reverse" not in a.lower()] or \
               [a for a in opts if "reverse" in a.lower()]
        if any("reverse" in a.lower() for a in opts) and any(foil(a) and "reverse" not in a.lower() for a in opts):
            sure = False  # "Foil" could be the holo or the reverse holo
    elif wanted == "normal":
        pick = [a for a in opts if not foil(a)]
    else:  # nothing said: the plain printing, else the only (non-reverse) one
        pick = ([a for a in opts if not foil(a)] or [a for a in opts if "reverse" not in a.lower()] or opts)
    if not pick:
        return opts[0], False
    return pick[0], sure


def match_single(index, item, game, settings):
    """Match a TCG single to a TCGplayer card: set code -> set, then card name, number and treatment.
    Returns (card or None, how, score) with how 'auto', 'check' or 'none'."""
    title = re.sub(r"\s+", " ", POS_MARK.sub(" ", item["title"])).strip()
    title = re.sub(r"\)-\s", ") - ", title)  # "Wrenn and Six (Borderless)- Double Masters 2022"
    every = list(settings.get("tcg_categories", []))
    if game is None:
        return _match_single(index, title, every)
    found = _match_single(index, title, [game])
    if found[0] is None:  # e.g. a Japanese Pokemon set tagged "Pokemon Singles": look in the other games
        other = _match_single(index, title, [c for c in every if c != game])
        if other[0] is not None:
            return other[0], "check", other[2]
    return found


def _match_single(index, title, cats):
    keys, before = [], title
    for m in reversed(list(PAREN.finditer(title))):  # the set code: the last (...) that is one
        for c in cats:
            keys = index.groups_for_code(c, m.group(1))
            if keys:
                break
        if keys:
            before = title[:m.start()].rstrip(" -")
            break
    segs = [s.strip() for s in before.split(" - ") if s.strip()]
    if not keys:  # no known code: the set name as the last part(s) of the title (without a trailing code)
        bare = re.sub(r"\s*\([^()]{1,12}\)\s*$", "", title)
        segs = [s.strip() for s in bare.split(" - ") if s.strip()]
        for k in range(len(segs) - 1, 0, -1):
            for c in cats:
                keys = index.groups_for_set_name(c, " - ".join(segs[k:]))
                if keys:
                    break
            if keys:
                segs = segs[:k]
                break
    if not keys or not segs:
        return None, "none", None
    best = []  # (score, set fit, card)
    title_number = split_card_name(" - ".join(segs))[1]  # a collector number anywhere in the card part counts
    for key in keys:
        tables = index.cards(key)
        set_words = set(card_key(index.data["groups"][key]["name"]).split())
        for k in range(len(segs), 0, -1):
            text = " - ".join(segs[:k])
            rest = set(card_key(" - ".join(segs[k:])).split())
            set_fit = len(rest & set_words) / len(rest | set_words) if rest else 0.5
            b, num, treat = split_card_name(text)
            num = num or title_number
            exact = tables["exact"].get(card_key(text), [])
            for card in exact:
                if not same_number(num, card["num"]):
                    continue
                best.append((1.0 if len(exact) == 1 or num else 0.7, set_fit, card))
            if exact:
                break
            for card in tables["base"].get(b, []):
                if not same_number(num, card["num"]):
                    continue
                score = 0.6 + (0.25 if num and card["num"] else 0)
                score += 0.15 if treat == card["treat"] else -0.1 * len(treat ^ card["treat"])
                best.append((round(score, 3), set_fit, card))
    if not best:
        return None, "none", None
    best.sort(key=lambda x: (x[0], x[1]), reverse=True)
    score, fit, card = best[0]
    rivals = [x for x in best[1:] if x[0] == score and x[2]["id"] != card["id"] and abs(x[1] - fit) < 1e-9]
    confident = score >= 0.85 and not rivals
    return card, ("auto" if confident else "check"), min(score, 1.0)


# ---------------------------------------------------------------- comparison

def classify(diff_pct, presale, settings):
    drift = settings["presale_drift_pct"] if presale else settings["drift_pct"]
    urgent = settings["presale_urgent_pct"] if presale else settings["urgent_pct"]
    if abs(diff_pct) >= urgent:
        return "URGENT"
    if abs(diff_pct) >= drift:
        return "DRIFTING"
    return "OK"


def parse_time(text):
    """tcgcsv's '2026-10-27T20:05:13+0000' or our own isoformat -> aware datetime (None if blank)."""
    if not text:
        return None
    text = re.sub(r"([+-]\d\d)(\d\d)$", r"\1:\2", text.strip())
    t = dt.datetime.fromisoformat(text)
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def find_live_price(config, title, variant, tcg_updated):
    """A price someone typed in from TCGplayer's site, if it's newer than the tcgcsv snapshot.
    Pass tcg_updated="" when the snapshot has no price for the product, so the typed price keeps counting."""
    key, variant = normalize_title(title), (variant or "").lower()
    snapshot = parse_time(tcg_updated)
    for e in config.get("live_prices", []):
        if normalize_title(e["shopify_title"]) == key and (e.get("variant") or "").lower() == variant:
            if snapshot is None or parse_time(e["entered"]) > snapshot:
                return e
    return None


def set_live_price(config, title, variant, price):
    """Save (or with price None, clear) a hand-entered TCGplayer price for one listing."""
    key, variant = normalize_title(title), variant or ""
    config["live_prices"] = [
        e for e in config.get("live_prices", [])
        if not (normalize_title(e["shopify_title"]) == key and (e.get("variant") or "") == variant)]
    if price is not None:
        config["live_prices"].append({"shopify_title": POS_MARK.sub("", title).strip(), "variant": variant,
                                      "price": price, "entered": dt.datetime.now(dt.timezone.utc).isoformat()})


def margin(price, cost):
    """Gross margin as % of price, or None when either is unknown."""
    return (price - cost) / price * 100 if price and cost else None


def find_override(config, title, variant):
    """Saved match for this listing: title+variant first, then title only. (POS) is ignored."""
    key = normalize_title(title)
    title_only = None
    for o in config["overrides"]:
        if normalize_title(o["shopify_title"]) != key:
            continue
        if o.get("variant"):
            if o["variant"].lower() == (variant or "").lower():
                return o
        elif title_only is None:
            title_only = o
    return title_only


def set_override(config, title, variant, **fields):
    """Save (or with no fields, remove) the override for exactly this title/variant."""
    key, variant = normalize_title(title), variant or ""
    config["overrides"] = [o for o in config["overrides"]
                           if not (normalize_title(o["shopify_title"]) == key and (o.get("variant") or "") == variant)]
    if fields:
        entry = {"shopify_title": POS_MARK.sub("", title).strip()}
        if variant:
            entry["variant"] = variant
        entry.update(fields)
        config["overrides"].append(entry)


def override_lookup(config):
    """find_override for many listings at once: {normalized title: {variant (lower) or "": override}}."""
    table = {}
    for o in config["overrides"]:
        slot = table.setdefault(normalize_title(o["shopify_title"]), {})
        slot.setdefault((o.get("variant") or "").lower(), o)
    return lambda title, variant: (lambda slot: slot.get((variant or "").lower()) or slot.get(""))(
        table.get(normalize_title(title), {})) if table else None


def match_listing(config, index, item, override=None, memo=None):
    """Returns (refs, how, score, alternatives). how: 'saved', 'auto', 'check', 'none', 'ignored'.
    memo: automatic matches remembered from earlier checks (valid while the TCGplayer catalogue is unchanged)."""
    settings = config["settings"]
    if override is None:
        override = find_override(config, item["title"], item["variant"])
    if override:
        if override.get("ignore"):
            return [], "ignored", None, []
        refs = [index.product(t["category"], t["group"], t["product"]) for t in override.get("tcg", [])]
        refs = [dict(r, finish=t["finish"]) if t.get("finish") else r
                for r, t in zip(refs, override.get("tcg", [])) if r]
        if refs or override.get("self_priced"):
            return refs, "saved", None, []
    text = f"{item['title']} {item['variant']}"
    if memo is not None and text in memo:
        ids, how, score = memo[text]
        refs = [index.product(*i) for i in ids]
        if all(refs):
            return refs, how, score, []
    cands = index.candidates(text, limit=5)
    if not cands or cands[0][0] < settings["check_match_score"]:
        result = [], "none", cands[0][0] if cands else None, cands
    else:
        best_score, best = cands[0]
        runner_up = cands[1][0] if len(cands) > 1 else 0
        query = words(SHOPIFY_NOISE.sub(" ", text))
        same_kind = query & QUALIFIERS <= best["all_words"]
        # a rare word of ours (e.g. "Nightfall") missing from their product usually means a different product
        # (set codes and years like "OP-14" or "2026" are ours, so words with digits don't count)
        missing_rare = any(index.weight(w) >= RARE_WORD_WEIGHT and not re.search(r"\d", w)
                           for w in query - best["all_words"])
        confident = (best_score >= settings["auto_match_score"] and best_score - runner_up >= 0.05
                     and same_kind and not missing_rare)
        result = [best], ("auto" if confident else "check"), best_score, cands
    if memo is not None:
        memo[text] = ([(r["category"], r["group"], r["id"]) for r in result[0]], result[1], result[2])
    return result


def sort_rows(rows):
    rows.sort(key=lambda r: (TAG_ORDER[r["tag"]], not r["presale"], -abs(r["diff_pct"] or 0), r["shopify_title"]))


def has_stock(row):
    s = row.get("stock")
    return isinstance(s, (int, float)) and s > 0


def group_stock(row, columns):
    """Fill row["locations"] (stock per column) and row["stock"] from the raw per-location stock in
    row["locations_raw"] and the stock_columns setting. Locations in no column don't count."""
    raw = row.get("locations_raw")
    if raw is None or not columns:
        row["locations"] = None if raw is None else dict(raw)
        row["stock"] = row.get("stock_all", row.get("stock"))
        return
    by_name = {name.lower(): qty for name, qty in raw.items()}
    row["locations"] = {c["name"]: sum(by_name.get(n.lower(), 0) for n in c.get("locations", ())) for c in columns}
    row["stock"] = sum(row["locations"].values())


def untracked_stock(row, columns):
    """{location: qty} with stock at Shopify locations that aren't in any stock column."""
    if not columns:
        return {}
    known = {n.lower() for c in columns for n in c.get("locations", ())}
    return {n: q for n, q in (row.get("locations_raw") or {}).items() if q and n.lower() not in known}


def build_rows(config, shopify_items, index, progress=None, tcg_updated="", memo=None, timings=None):
    """One row per listing: every active (and, if wanted, draft) Shopify variant.
    Sealed product and TCG singles are compared with TCGplayer; everything else is NO PRICE SOURCE."""
    settings = config["settings"]
    field = settings.get("tcg_price_field", "marketPrice")
    new_since = (dt.date.today() - dt.timedelta(days=settings.get("new_release_days", 30))).isoformat()
    timings = timings if timings is not None else {}
    clock = time.perf_counter()
    lookup = override_lookup(config)
    live_by = {}
    for e in config.get("live_prices", []):
        live_by.setdefault((normalize_title(e["shopify_title"]), (e.get("variant") or "").lower()), []).append(e)
    cond_pct = settings.get("condition_pct") or DEFAULT_SETTINGS["condition_pct"]

    listings = []
    for item in shopify_items:
        if status_ok(item, settings):
            kind, game = item_kind(item, settings)
            listings.append((item, kind, game, lookup(item["title"], item["variant"])))

    # single cards: download the card lists of every set we stock singles from (only changed sets), in parallel
    want = set()
    for item, kind, game, o in listings:
        if kind != "single":
            continue
        for t in (o or {}).get("tcg", []):
            want.add(f"{t['category']}/{t['group']}")
        cats = [game] if game is not None else settings.get("tcg_categories", [])
        for m in PAREN.finditer(item["title"]):
            for c in cats:
                want.update(index.groups_for_code(c, m.group(1)))
    if want:
        index.ensure_cards(want, progress)
    timings["card lists"] = time.perf_counter() - clock

    clock = time.perf_counter()
    matched = []
    sealed_memo = None if memo is None else memo.setdefault("sealed", {})
    single_memo = {}  # the same card in several conditions is matched once
    for n, (item, kind, game, override) in enumerate(listings, 1):
        if kind == "other":
            if override and override.get("ignore"):
                continue
            matched.append((item, kind, override, [], "nosource", None, None))
            continue
        if kind == "single":
            if override and override.get("ignore"):
                continue
            how = None
            if override and override.get("tcg"):  # a confirmed / chosen card
                refs, how, score, _ = match_listing(config, index, item, override)
                how = how if refs else None
            if how is None:
                key = (item["title"], game)
                if key not in single_memo:
                    single_memo[key] = match_single(index, item, game, settings)
                card, how, score = single_memo[key]
                refs = [index.card_ref(card)] if card else []
            info = single_details(item)
            if refs and refs[0]["category"] == 85 and game != 85:
                info["language"] = "japanese"  # found in TCGplayer's Pokemon Japan sets
            if (how != "saved" and info["language"] != "english" and refs
                    and not (game == 85 and info["language"] == "japanese")):
                how = "check"  # TCGplayer prices are for English cards: don't guess
            matched.append((item, kind, override, refs, how, score, info))
        else:
            refs, how, score, _ = match_listing(config, index, item, override or {}, sealed_memo)
            if how == "ignored":
                continue
            matched.append((item, kind, override, refs, how, score, None))
        if progress and n % 2000 == 0:
            progress(f"Matching products: {n} of {len(listings)}")
    timings["match"] = time.perf_counter() - clock

    clock = time.perf_counter()
    index.load_prices({(r["category"], r["group"]) for m in matched for r in m[3]}, progress, remember=())
    timings["prices"] = time.perf_counter() - clock

    clock = time.perf_counter()
    rows, remember = [], []
    for item, kind, override, refs, how, score, info in matched:
        override = override or {}
        condition = finish_note = ""
        factor = 1.0
        finishes, unsure = [], False
        if kind == "single":
            condition = info["condition"]
            factor = float(cond_pct.get(condition, 100)) / 100
            for r in refs:
                f = r.get("finish")
                if not f:
                    f, sure = resolve_finish(index.finishes(r["category"], r["group"], r["id"]),
                                             info["finish"], info["edition"])
                    unsure = unsure or not sure
                finishes.append(f)
                if f:
                    remember.append((r["category"], r["group"], r["id"], f))
            if unsure and how == "auto":
                how = "check"
        else:
            finishes = [None] * len(refs)
        prices = [index.price(r["category"], r["group"], r["id"], field, f) for r, f in zip(refs, finishes)]
        values = [p[0] for p in prices if p[0] is not None]
        snapshot_price = market = sum(values) / len(values) * factor if values else None
        as_of = max((p[1] for p in prices if p[0] is not None and p[1]), default="")  # "" = today's snapshot
        # a typed price is replaced once a newer snapshot has today's price; otherwise it keeps counting
        live = None
        entries = live_by.get((normalize_title(item["title"]), (item["variant"] or "").lower()))
        if entries:
            snapshot = parse_time(tcg_updated if values and not as_of else "")
            live = next((e for e in entries if snapshot is None or parse_time(e["entered"]) > snapshot), None)
        if live:
            market = live["price"]
        ours = item["price"]
        cost = item.get("cost") or None  # "Cost per item"; 0 means it was never filled in
        diff = diff_amt = None
        if ours and market:
            diff_amt = ours - market
            diff = diff_amt / market * 100
        presale = (bool(override.get("presale")) or any(r.get("presale") for r in refs)
                   or is_presale_text(item["tags"]) or is_presale_text(item["title"]))
        self_priced = bool(override.get("self_priced")) or has_any_word(item["title"], settings.get("self_priced_words", []))

        if how == "nosource":
            tag = "NO PRICE SOURCE"
        elif not ours:
            tag = "NO DATA"
        elif self_priced:
            tag = "SELF-PRICED"
        elif how == "none":
            tag = "NO MATCH"
        elif how == "check" or (how == "auto" and diff is not None
                                and abs(diff) >= settings.get("auto_match_max_diff_pct", 75)):
            tag = "CHECK MATCH"
        elif market is None:
            tag = "NO PRICE"
        else:
            tag = classify(diff, presale, settings)

        source = ""
        if live:
            source = "price typed in from TCGplayer's site"
        elif values:
            source = (FIELD_NAMES.get(field, field) + (f" (average of {len(values)})" if len(values) > 1 else "")
                      + (f", last seen {as_of} (no listings since)" if as_of else " from the daily snapshot"))
            if kind == "single":
                source += (f", {'/'.join(f for f in finishes if f)}"
                           + (f", × {factor * 100:g}% for {CONDITIONS[condition]}" if condition != "NM" else ""))
        note = override.get("note", "")
        if kind == "single" and info["language"] != "english" and refs:
            note = (note + " · " if note else "") + (
                "Japanese set (TCGplayer's Pokemon Japan prices): please confirm the match"
                if refs[0]["category"] == 85 else
                f"{info['language'].title()} card: TCGplayer's price is for the English card, please check")
        rows.append({
            "shopify_title": item["title"],
            "variant": item["variant"],
            "kind": kind,
            "channel": "POS" if is_pos(item["title"]) else "Online",
            "status": item["status"],
            "type": item.get("type", ""),
            "stock": item["stock"],
            "stock_all": item["stock"],
            "locations_raw": item.get("locations"),
            "locations": None,  # per stock column, filled in by group_stock below
            "product_id": item.get("product_id"),
            "variant_id": item.get("variant_id"),
            "our_price": ours,
            "compare_at": item["compare_at"],
            "cost": cost,
            "margin_pct": margin(ours, cost),            # profit at our price, % of price
            "tcg_margin_pct": margin(market, cost),      # profit if we priced at TCGplayer
            "below_cost": bool(cost and market is not None and market < cost),
            "tcg_refs": [{"category": r["category"], "group": r["group"], "product": r["id"],
                          **({"finish": f} if f else {})} for r, f in zip(refs, finishes)],
            "tcg_name": " | ".join(r["name"] for r in refs),
            "tcg_set": " | ".join(dict.fromkeys(r["set"] for r in refs)),
            "tcg_urls": [r["url"] for r in refs if r.get("url")],
            "tcg_price": market,
            "tcg_low": (lambda lows: min(lows) * factor if lows else None)([p[2] for p in prices if p[2] is not None]),
            "source": source,
            "price_as_of": "" if live else as_of,
            "match": {"saved": "Confirmed", "auto": "Automatic", "check": "Needs check", "none": "Not found",
                      "nosource": "No price source"}[how],
            "match_score": score,
            "condition": condition,
            "finish": " | ".join(f for f in finishes if f),
            "diff_amt": diff_amt,
            "diff_pct": diff,
            # what our price should become: the TCGplayer price (condition-adjusted for singles)
            "suggested_price": round(market, 2) if tag in NEED_UPDATE and market else None,
            "presale": presale,
            "release": next((r["release"] for r in refs if r.get("release")), ""),
            # presale / just released: TCGplayer prices can move a lot between daily snapshots
            "fast_moving": presale or any((r.get("set_released") or "") >= new_since for r in refs),
            "snapshot_price": snapshot_price,
            "live_entered": live["entered"] if live else "",
            "tag": tag,
            "note": note,
        })
    if remember and index.snapshot_date:
        index._remember([], remember)
    columns = config["settings"].get("stock_columns") or []
    for r in rows:
        group_stock(r, columns)
    sort_rows(rows)
    timings["rows"] = time.perf_counter() - clock
    return rows


class PriceChecker:
    """Holds one session's data so the GUI can re-run matching without downloading again."""

    def __init__(self, config_path=HERE / "products.json", credentials_path=None, out_dir=HERE / "reports",
                 cache_dir=HERE / "cache"):
        self.config_path = Path(config_path)
        self.out_dir = Path(out_dir)
        self.config = load_config(self.config_path)
        saved = self.config["settings"].get("credentials_file")
        chosen = credentials_path or saved  # picked by hand in the window (or --credentials); never guessed
        self.credentials_path = Path(chosen) if chosen else None
        self.index = TcgIndex(cache_dir)
        self.cache_dir = Path(cache_dir)
        self.shopify_items = None
        self.rows = []
        self.tcg_updated = ""
        self.checked_at = ""   # when the rows were last worked out (shown when reopening the app)
        self.stage = ""        # "shopify" / "tcgplayer" while running, for error messages
        self.notes = []        # things to say once in the status line (e.g. no per-location stock)
        self.timings = {}      # seconds per step of the last check (--timings)
        self.memo = None       # automatic sealed matches from earlier checks (cache/match_memo.json)

    def save(self):
        save_config(self.config, self.config_path)

    def run(self, progress=print, shopify_csv=None, use_cache=False):
        """use_cache: reuse the Shopify products saved by the last check instead of asking Shopify."""
        self.timings = {}
        clock = time.perf_counter()
        self.stage = "shopify"
        progress("Connecting to Shopify…")
        if shopify_csv:
            self.shopify_items = list(shopify_from_csv(shopify_csv))
        elif use_cache:
            saved = json.loads((self.cache_dir / "shopify_products.json").read_text(encoding="utf-8"))
            self.shopify_items, self.notes = saved["items"], saved.get("notes", [])
        else:
            problem = check_credentials_file(self.credentials_path)
            if problem and not os.environ.get("SHOPIFY_TOKEN"):
                raise SetupError(problem)
            creds = shopify_credentials(self.credentials_path)
            if not creds:
                raise SetupError(f"No Shopify login found. Fill in store_url and client_id/client_secret in the "
                                 f"shopify: section of {self.credentials_path}")
            self.shopify_items = self._read_shopify(creds, progress)
        self.timings["shopify"] = time.perf_counter() - clock
        clock = time.perf_counter()
        self.stage = "tcgplayer"
        progress("Checking TCGplayer catalogue for new sets…")
        self.index.refresh(self.config["settings"]["tcg_categories"], progress)
        self.index._prices = {}  # look again: a new daily snapshot may be out
        self.tcg_updated = self.index.last_updated()
        self.index.snapshot = self.tcg_updated
        self.index.snapshot_date = self.tcg_updated[:10]
        self.timings["tcgplayer catalogue"] = time.perf_counter() - clock
        rows = self.recompute(progress)
        self.stage = ""
        self.save_last_check()
        return rows

    def _read_shopify(self, creds, progress):
        """All products the first time each day (one bulk operation); after that only variants and products
        changed since the last check (Shopify marks a variant as changed when its price or stock changes)."""
        path = self.cache_dir / "shopify_products.json"
        now = dt.datetime.now(dt.timezone.utc)
        reader = ShopifyReader(*creds, progress=progress, use_bulk=self.config["settings"].get("shopify_bulk", True))
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            full_at, fetched_at = parse_time(saved["full_at"]), parse_time(saved["fetched_at"])
            fresh = (saved.get("store") == creds[0] and full_at.astimezone().date() == now.astimezone().date()
                     and all(i.get("variant_id") for i in saved["items"]))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            saved, fresh = None, False
        if fresh:
            since = (fetched_at - dt.timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
            reader.levels = saved.get("levels", True)
            changed = reader.variants(changed_since=since)
            products = reader.changed_products(since)
            ids = {i["variant_id"] for i in changed}
            items = [i for i in saved["items"] if i["variant_id"] not in ids] + changed
            for i in items:
                if i["product_id"] in products:
                    i.update(products[i["product_id"]])
            progress(f"Shopify: {len(ids)} variants and {len(products)} products changed since the last check")
            full_at = saved["full_at"]
            notes = saved.get("notes", []) + [n for n in reader.notes if n not in saved.get("notes", [])]
        else:
            items = reader.variants()
            full_at = now.isoformat()
            notes = reader.notes
        self.notes = notes
        self.timings["shopify read"] = reader.how or "cached"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"store": creds[0], "full_at": full_at, "fetched_at": now.isoformat(),
                                        "levels": reader.levels, "notes": notes, "items": items}), encoding="utf-8")
        except OSError:
            pass
        return items

    def save_last_check(self):
        """Keep the latest results so the app can show them the moment it opens next time."""
        self.checked_at = dt.datetime.now(dt.timezone.utc).isoformat()
        try:
            (self.cache_dir / "last_check.json").write_text(json.dumps(
                {"checked_at": self.checked_at, "tcg_updated": self.tcg_updated, "notes": self.notes,
                 "rows": self.rows}), encoding="utf-8")
        except OSError:
            pass

    def load_last_check(self):
        """Show the previous results straight away (and allow choices) while a fresh check runs."""
        try:
            last = json.loads((self.cache_dir / "last_check.json").read_text(encoding="utf-8"))
            items = json.loads((self.cache_dir / "shopify_products.json").read_text(encoding="utf-8"))["items"]
        except (OSError, ValueError, KeyError):
            return False
        self.rows, self.tcg_updated, self.checked_at = last["rows"], last["tcg_updated"], last["checked_at"]
        self.notes = last.get("notes", [])
        for r in self.rows:  # results saved by an older version of the app
            r.setdefault("kind", "sealed")
            r.setdefault("locations", None)
            if "locations_raw" not in r:  # saved before stock columns: "locations" held the raw locations
                r["locations_raw"], r["stock_all"] = r["locations"], r.get("stock")
            r.setdefault("product_id", None)
            r.setdefault("variant_id", None)
            r.setdefault("suggested_price", None)
            r.setdefault("condition", "")
            r.setdefault("finish", "")
        self.regroup_stock()
        self.shopify_items = items
        self.index.snapshot = self.tcg_updated
        self.index.snapshot_date = self.tcg_updated[:10]
        return bool(self.rows)

    def snapshot_age(self):
        """('Oct 27, 4:05 PM', hours old) for the tcgcsv snapshot in local time, or (None, None)."""
        t = parse_time(self.tcg_updated)
        if not t:
            return None, None
        local = t.astimezone()
        hours = (dt.datetime.now(dt.timezone.utc) - t).total_seconds() / 3600
        return local.strftime("%b %d, %I:%M %p").replace(" 0", " "), hours

    def _match_memo(self):
        """Automatic sealed matches from earlier checks, kept while the sealed catalogue and the match
        settings are unchanged (so only new or changed listings are matched again)."""
        s = self.config["settings"]
        stamp = "|".join(str(x) for x in (self.index.sealed_stamp(), s["auto_match_score"], s["check_match_score"]))
        if self.memo is None or self.memo.get("stamp") != stamp:
            try:
                memo = json.loads((self.cache_dir / "match_memo.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                memo = {}
            self.memo = memo if memo.get("stamp") == stamp else {"stamp": stamp, "sealed": {}}
            for v in self.memo["sealed"].values():
                v[0] = [tuple(i) for i in v[0]]
        return self.memo

    def _save_memo(self):
        try:
            (self.cache_dir / "match_memo.json").write_text(json.dumps(self.memo), encoding="utf-8")
        except (OSError, TypeError):
            pass

    def recompute(self, progress=None):
        memo = self._match_memo()
        before = len(memo["sealed"])
        self.rows = build_rows(self.config, self.shopify_items or [], self.index, progress, self.tcg_updated,
                               memo=memo, timings=self.timings)
        if len(memo["sealed"]) != before:
            self._save_memo()
        return self.rows

    def recompute_title(self, title):
        """Re-match just the listings with this title (after a saved choice changes)."""
        key = normalize_title(title)
        items = [i for i in self.shopify_items or [] if normalize_title(i["title"]) == key]
        self.rows = [r for r in self.rows if normalize_title(r["shopify_title"]) != key]
        self.rows += build_rows(self.config, items, self.index, tcg_updated=self.tcg_updated, memo=self.memo)
        sort_rows(self.rows)

    def search_tcg(self, text, limit=40, single=False, categories=None):
        """TCGplayer products for the match picker, with today's price: [(score, product, price)].
        single: search single cards (in the sets downloaded so far) instead of sealed product."""
        if single:
            cands = self.index.search_cards(text, categories, limit=limit)
        else:
            cands = self.index.candidates(text, limit=limit)
        self.index.load_prices([(p["category"], p["group"]) for _, p in cands])
        field = self.config["settings"].get("tcg_price_field", "marketPrice")
        out = []
        for s, p in cands:
            finish = None
            if single:
                finish, _ = resolve_finish(self.index.finishes(p["category"], p["group"], p["id"]), None, None)
            out.append((s, p, self.index.price(p["category"], p["group"], p["id"], field, finish)[0]))
        return out

    def product_types(self):
        """Product types in our shop with listing counts, most common first."""
        counts = {}
        for i in self.shopify_items or []:
            counts[i.get("type") or ""] = counts.get(i.get("type") or "", 0) + 1
        return sorted(counts.items(), key=lambda kv: -kv[1])

    def locations(self):
        """The stock columns (from the stock_columns setting), or, when that's empty, every Shopify
        location that holds stock of anything listed, in name order."""
        columns = self.config["settings"].get("stock_columns") or []
        if columns:
            return [c["name"] for c in columns]
        names = set()
        for r in self.rows:
            names.update(r.get("locations") or ())
        return sorted(names, key=str.lower)

    def shopify_locations(self):
        """Every Shopify location seen in the last check, in name order (for Settings)."""
        names = set()
        for r in self.rows:
            names.update(r.get("locations_raw") or ())
        return sorted(names, key=str.lower)

    def regroup_stock(self):
        """Re-add the stock per column after the stock_columns setting changed (no Shopify read needed)."""
        columns = self.config["settings"].get("stock_columns") or []
        for r in self.rows:
            group_stock(r, columns)

    def push_prices(self, rows_and_prices, progress=print):
        """Send confirmed new prices to Shopify, log every attempt to reports/price_changes.csv, and update
        the list. rows_and_prices: [(row, new price)]. Returns (changed rows, [(row, reason)] that failed)."""
        creds = shopify_credentials(self.credentials_path)
        if not creds:
            raise SetupError("No Shopify login found. Choose your config.yaml in Settings first.")
        self.stage = "shopify"
        results = push_prices(*creds, [(r["product_id"], r["variant_id"], p) for r, p in rows_and_prices], progress)
        stamp = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        log = self.out_dir / "price_changes.csv"
        self.out_dir.mkdir(parents=True, exist_ok=True)
        new = not log.exists()
        with open(log, "a", newline="", encoding="utf-8-sig" if new else "utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["when", "shopify_title", "variant", "variant_id", "old_price", "new_price", "result"])
            for r, p in rows_and_prices:
                ok, info = results.get(r["variant_id"], (False, "not sent"))
                w.writerow([stamp, r["shopify_title"], r["variant"], r["variant_id"], r["our_price"], f"{p:.2f}",
                            "changed" if ok else f"failed: {info}"])
        done = {vid: info for vid, (ok, info) in results.items() if ok}
        self.apply_price_changes(done)
        failed = [(r, results.get(r["variant_id"], (False, "not sent"))[1]) for r, _ in rows_and_prices
                  if not results.get(r["variant_id"], (False,))[0]]
        return [r for r, _ in rows_and_prices if r["variant_id"] in done], failed

    def apply_price_changes(self, changes):
        """After prices were changed in Shopify: {variant_id: new price}. Updates the saved Shopify products
        and re-works out the affected rows, so the list is right without a fresh check."""
        titles = set()
        for item in self.shopify_items or []:
            if item.get("variant_id") in changes:
                item["price"] = changes[item["variant_id"]]
                titles.add(item["title"])
        for title in titles:
            self.recompute_title(title)
        return titles

    def write_reports(self, show_zero=None):
        """HTML + CSV report, plus need_update CSV (URGENT and DRIFTING rows with the suggested price).
        Out-of-stock rows are left out unless show_zero (default: the show_zero_stock setting)."""
        if show_zero is None:
            show_zero = self.config["settings"].get("show_zero_stock", False)
        report_date = dt.date.today().isoformat()
        self.out_dir.mkdir(parents=True, exist_ok=True)
        html_path = self.out_dir / f"price_report_{report_date}.html"
        csv_path = self.out_dir / f"price_report_{report_date}.csv"
        need_path = self.out_dir / f"need_update_{report_date}.csv"
        rows = self.rows if show_zero else [r for r in self.rows if has_stock(r)]
        hidden = len(self.rows) - len(rows)
        locations = self.locations()
        write_html(rows, html_path, self.config["settings"], report_date, self.tcg_updated,
                   locations=locations, hidden=hidden, notes=self.notes)
        write_csv(rows, csv_path, locations)
        write_csv([r for r in rows if r["tag"] in NEED_UPDATE], need_path, locations)
        return html_path, csv_path


# ---------------------------------------------------------------- output

CSV_COLUMNS = ["tag", "presale", "release", "channel", "kind", "shopify_title", "variant", "type", "condition",
               "finish", "our_price", "suggested_price", "cost", "margin_pct", "tcg_price", "tcg_margin_pct",
               "below_cost", "diff_amt", "diff_pct", "tcg_low", "match", "match_score", "tcg_name", "tcg_set", "source",
               "price_as_of", "snapshot_price", "live_entered", "fast_moving", "status", "stock", "note"]


def write_csv(rows, path, locations=()):
    """diff_amt / diff_pct = our price minus TCGplayer. One "stock: <location>" column per Shopify location."""
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(CSV_COLUMNS + [f"stock: {name}" for name in locations])
        for r in rows:
            at = r.get("locations") or {}
            w.writerow([round(v, 2) if isinstance(v, float) else v for v in (r.get(c) for c in CSV_COLUMNS)]
                       + [at.get(name, 0) if r.get("locations") is not None else "" for name in locations])


def tag_class(tag):
    return re.sub(r"[^a-z]", "", tag.lower())


def stock_text(v):
    return "" if v in (None, "") else str(v)


def write_html(rows, path, settings, report_date, tcg_updated, locations=(), hidden=0, notes=()):
    counts = {t: sum(r["tag"] == t for r in rows) for t in TAG_ORDER}
    presale_count = sum(r["presale"] for r in rows)
    need_count = sum(r["tag"] in NEED_UPDATE for r in rows)
    body = []
    for r in rows:
        at = r.get("locations")
        stock_cells = "".join(
            f'<td class="num{" zero" if not (at or {}).get(name) else ""}">{stock_text((at or {}).get(name, 0) if at is not None else "")}</td>'
            for name in locations)
        extra = " · ".join(x for x in (r.get("condition") and CONDITIONS.get(r["condition"], r["condition"]),
                                        r.get("finish")) if x)
        if r["diff_pct"] is None:
            action = ""
        elif r["diff_pct"] > 0:
            action = "We're above TCGplayer"
        else:
            action = "We're below TCGplayer"
        links = " ".join(f'<a href="{html.escape(u)}" target="_blank">TCGplayer</a>' for u in r["tcg_urls"][:1])
        diff_cls = "" if r["diff_pct"] is None else ("up" if r["diff_pct"] > 0 else "down")
        body.append(f"""<tr class="row-{tag_class(r['tag'])} row-{r.get('kind', 'sealed')}{' row-needupdate' if r['tag'] in NEED_UPDATE else ''}{' row-presale' if r['presale'] else ''}{' row-belowcost' if r['below_cost'] else ''}{' row-fast' if r['fast_moving'] else ''}">
  <td><span class="tag t-{tag_class(r['tag'])}">{r['tag']}</span>{' <span class="tag t-presale">PRESALE</span>' if r['presale'] else ''}{' <span class="tag t-fast" title="Price can move a lot between daily snapshots - check live">⚡ FAST-MOVING</span>' if r['fast_moving'] else ''}</td>
  <td><div class="title">{html.escape(r['shopify_title'])}{(' — ' + html.escape(r['variant'])) if r['variant'] else ''}</div>
      <div class="sub">{r['channel']} · {html.escape(r['type'])}{(' · ' + html.escape(extra)) if extra else ''}</div>
      {f'<div class="sub">{html.escape(r["note"])}</div>' if r['note'] else ''}</td>
  <td class="num">{money(r['our_price'])}{f'<div class="sub">was {money(r["compare_at"])}</div>' if r['compare_at'] else ''}</td>
  <td class="num">{money(r['cost'])}{f'<div class="sub">margin {r["margin_pct"]:.0f}%</div>' if r['margin_pct'] is not None else ''}</td>
  <td class="num">{money(r['tcg_price'])}{f'<div class="sub">typed in (snapshot {money(r["snapshot_price"])})</div>' if r['live_entered'] else ''}{f'<div class="sub">low {money(r["tcg_low"])}</div>' if r['tcg_low'] is not None else ''}{f'<div class="sub{" neg" if r["tcg_margin_pct"] < 0 else ""}">margin {r["tcg_margin_pct"]:.0f}%{" · below cost" if r["below_cost"] else ""}</div>' if r['tcg_margin_pct'] is not None else ''}</td>
  <td class="num {diff_cls}"><b>{pct(r['diff_pct'])}</b><div class="sub">{('+' if (r['diff_amt'] or 0) > 0 else '') + money(r['diff_amt']) if r['diff_amt'] is not None else ''}</div><div class="sub">{action}</div></td>
  <td class="num"><b>{stock_text(r['stock'])}</b></td>{stock_cells}
  <td><div>{html.escape(r['tcg_name'] or '—')}</div><div class="sub">{html.escape(r['tcg_set'])}</div>
      <div class="sub">{r['match']}{(' · releases ' + r['release']) if r['presale'] and r['release'] else ''} {links}</div></td>
</tr>""")
    cards = "".join(f'<div class="card"><b class="c-{tag_class(t)}">{counts[t]}</b>{t.title()}</div>'
                    for t in TAG_ORDER if counts[t])
    buttons = "".join(f'<button data-f="{tag_class(t)}">{t.title()} ({counts[t]})</button>' for t in TAG_ORDER if counts[t])
    kinds = {k: sum(r.get("kind", "sealed") == k for r in rows) for k in ("sealed", "single")}
    loc_heads = "".join(f'<th class="num">{html.escape(name)}</th>' for name in locations)
    hidden_note = (f" · {hidden} out-of-stock listings not shown (tick 'Show out of stock' in the app, or run with "
                   f"--show-zero, to include them)" if hidden else "")
    notes_html = "".join(f'<div class="meta warn">{html.escape(n)}</div>' for n in notes)
    page = f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Price Check {report_date}</title>
<style>
:root{{--bg:#f7f7f5;--card:#fff;--fg:#1d1d1b;--muted:#6b6b66;--line:#e4e4df;--red:#c62828;--redbg:#fdecea;--amber:#a15c00;--amberbg:#fff4e0;--green:#2e7d32;--greenbg:#e8f5e9;--blue:#1e4fb8;--bluebg:#e8eefc;--grey:#777;--greybg:#eee}}
@media (prefers-color-scheme:dark){{:root{{--bg:#161615;--card:#1f1f1d;--fg:#ececea;--muted:#9a9a94;--line:#33332f;--red:#ff7b72;--redbg:#3a1a18;--amber:#f0b45a;--amberbg:#3a2a10;--green:#7ccf80;--greenbg:#16301a;--blue:#8fb0ff;--bluebg:#1a2540;--grey:#aaa;--greybg:#2a2a28}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:14px/1.4 system-ui,-apple-system,Segoe UI,sans-serif}}
main{{max-width:1300px;margin:0 auto;padding:24px 16px}}
h1{{font-size:22px;margin:0 0 4px}} .meta{{color:var(--muted);margin-bottom:16px}}
.cards{{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:16px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 16px;min-width:110px}}
.card b{{display:block;font-size:22px}}
.zero{{color:var(--muted)}} .suggest{{color:var(--fg)}} .warn{{color:var(--amber)}}
.search{{margin:0 0 10px;padding:6px 10px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--fg);width:min(420px,100%)}}
.c-urgent{{color:var(--red)}} .c-drifting{{color:var(--amber)}} .c-ok{{color:var(--green)}} .c-checkmatch{{color:var(--blue)}}
.filters{{margin-bottom:10px;display:flex;gap:6px;flex-wrap:wrap}}
.filters button{{border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:16px;padding:4px 12px;cursor:pointer}}
.filters button.on{{background:var(--fg);color:var(--bg)}}
.wrap{{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:8px}}
table{{border-collapse:collapse;width:100%;min-width:900px}}
th,td{{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}}
th{{font-size:12px;color:var(--muted);font-weight:600;position:sticky;top:0;background:var(--card)}}
.num{{text-align:right;white-space:nowrap}} .sub{{color:var(--muted);font-size:12px}} .title{{font-weight:500}}
.up{{color:var(--red)}} .down{{color:var(--amber)}} .sub.neg{{color:var(--red);font-weight:600}}
.tag{{display:inline-block;font-size:11px;font-weight:700;padding:2px 7px;border-radius:4px;margin:0 4px 4px 0;white-space:nowrap}}
.t-urgent{{background:var(--redbg);color:var(--red)}} .t-drifting{{background:var(--amberbg);color:var(--amber)}}
.t-ok{{background:var(--greenbg);color:var(--green)}} .t-nodata,.t-selfpriced,.t-nomatch,.t-noprice{{background:var(--greybg);color:var(--grey)}}
.t-presale,.t-fast{{background:var(--bluebg);color:var(--blue)}} .t-checkmatch{{background:var(--bluebg);color:var(--blue);outline:1px dashed var(--blue)}}
a{{color:var(--blue)}}
</style></head><body><main>
<h1>Shopify vs TCGplayer — {report_date}</h1>
<div class="meta">{len(rows)} listings{hidden_note} · TCGplayer prices are tcgcsv's daily snapshot from {html.escape(tcg_updated)} (UTC) - ⚡ items may have moved since · Diff = our price vs TCGplayer ·
Drifting ≥ {settings['drift_pct']}% / Urgent ≥ {settings['urgent_pct']}% (presale: {settings['presale_drift_pct']}% / {settings['presale_urgent_pct']}%) ·
Singles: TCGplayer Near Mint price × condition ({", ".join(f"{k} {v:g}%" for k, v in (settings.get("condition_pct") or {}).items())}) · Stock = Shopify available quantity, per location</div>{notes_html}
<div class="cards">{cards}<div class="card"><b style="color:var(--blue)">{presale_count}</b>Presale</div></div>
<div class="filters"><button class="on" data-f="all">All ({len(rows)})</button><button data-f="needupdate">Need update ({need_count})</button>{buttons}<button data-f="sealed">Sealed ({kinds['sealed']})</button><button data-f="single">Singles ({kinds['single']})</button><button data-f="presale">Presale</button><button data-f="belowcost">TCGplayer below our cost</button><button data-f="fast">⚡ Fast-moving</button></div>
<input class="search" type="search" placeholder="Search products…" aria-label="Search products">
<div class="wrap"><table>
<thead><tr><th>Tag</th><th>Shopify product</th><th class="num">Our price</th><th class="num">Cost</th><th class="num">TCGplayer</th><th class="num">Us vs TCG</th><th class="num">Stock</th>{loc_heads}<th>TCGplayer match</th></tr></thead>
<tbody>{''.join(body)}</tbody></table></div>
</main>
<script>
let f='all';const q=document.querySelector('.search');const rows=[...document.querySelectorAll('tbody tr')];
function apply(){{const n=q.value.trim().toLowerCase();
  rows.forEach(tr=>{{tr.style.display=((f==='all'||tr.classList.contains('row-'+f))&&(!n||tr.textContent.toLowerCase().includes(n)))?'':'none';}});}}
document.querySelectorAll('.filters button').forEach(b=>b.onclick=()=>{{
  document.querySelectorAll('.filters button').forEach(x=>x.classList.toggle('on',x===b));f=b.dataset.f;apply();}});
q.oninput=apply;
</script></body></html>"""
    path.write_text(page, encoding="utf-8")


# ================================================================ window

import tkinter as tk  # noqa: E402
from tkinter import filedialog, messagebox, simpledialog, ttk  # noqa: E402

FILTERS = [("All", None), ("Need update", "need_update"), ("Urgent", "URGENT"), ("Drifting", "DRIFTING"),
           ("Check match", "CHECK MATCH"), ("OK", "OK"), ("Presale", "presale"), ("⚡ Fast-moving", "fast_moving"),
           ("Below cost", "below_cost"), ("We price", "SELF-PRICED"), ("Not on TCGplayer", "NO MATCH"),
           ("No price", "NO PRICE"), ("No price source", "NO PRICE SOURCE"), ("No data", "NO DATA")]
KINDS = {"Everything": None, "Sealed": "sealed", "Singles": "single", "Other": "other"}
ROW_COLORS = {"URGENT": "#fbe3e1", "DRIFTING": "#fff1d6", "CHECK MATCH": "#e3eafc", "OK": "#ffffff",
              "SELF-PRICED": "#f1f1ef", "NO MATCH": "#f1f1ef", "NO PRICE": "#f1f1ef", "NO PRICE SOURCE": "#f1f1ef",
              "NO DATA": "#f1f1ef"}
FIRST_BATCH = 1500   # rows drawn straight away; the rest are added in the background so the window stays usable
BATCH = 4000
COLUMNS = [  # id, heading, width (characters), anchor, sort key
    ("tag", "Status", 12, "w", lambda r: TAG_ORDER[r["tag"]]),
    ("flags", "Flags", 6, "center", lambda r: (not r["presale"], not r["fast_moving"])),
    ("product", "Shopify product", 44, "w", lambda r: r["shopify_title"].lower()),
    ("ours", "Our price", 9, "e", lambda r: r["our_price"] or 0),
    ("cost", "Cost", 9, "e", lambda r: r["cost"] or 0),
    ("margin", "Our margin", 9, "e", lambda r: r["margin_pct"] if r["margin_pct"] is not None else -1e9),
    ("tcg", "TCG market", 10, "e", lambda r: r["tcg_price"] or 0),
    ("tcgmargin", "Margin at TCG", 11, "e", lambda r: r["tcg_margin_pct"] if r["tcg_margin_pct"] is not None else -1e9),
    ("diff", "Difference", 9, "e", lambda r: abs(r["diff_pct"] or 0)),
    ("amt", "$ diff", 9, "e", lambda r: abs(r["diff_amt"] or 0)),
    ("stock", "Stock", 6, "e", lambda r: r["stock"] if isinstance(r["stock"], (int, float)) else -1e9),
    ("match", "TCGplayer product", 32, "w", lambda r: r["tcg_name"].lower()),
    ("how", "Match", 10, "w", lambda r: r["match"]),
]
FLEX_COLUMNS = {"product": 0.6, "match": 0.4}  # these two share whatever width is left
FLEX_MIN_CHARS = {"product": 24, "match": 16}
# on narrow windows these columns hide, first to last, so product names stay readable (details stay below)
HIDE_WHEN_NARROW = ["how", "tcgmargin", "margin", "amt", "cost", "flags", "LOCATIONS"]


def short_names(names, size=9):
    """Short column headings for location names ('Woodland Hills Storefront' -> 'Woodland'), kept unique."""
    out = {}
    for name in names:
        first = (name.split() or [name])[0][:size]
        out[name] = first if sum((n.split() or [n])[0][:size] == first for n in names) == 1 else name[:size]
    return out
HINT = "Double-click a row to open it on TCGplayer · right-click for more options · Help explains the symbols"


def help_text(s):
    n = lambda v: f"{v:g}"  # 5.0 -> "5"
    return (f"Status: URGENT = {n(s['urgent_pct'])}%+ away from TCGplayer · DRIFTING = {n(s['drift_pct'])}%+ away "
            f"(presale items: {n(s['presale_urgent_pct'])}% / {n(s['presale_drift_pct'])}%) · CHECK MATCH = please "
            "confirm the TCGplayer product is right. Change these numbers in Settings.\n"
            "Need update = every URGENT and DRIFTING row; the suggested new price is the TCGplayer price (for singles "
            "adjusted for condition). NO PRICE SOURCE = TCGplayer doesn't price it (sports cards, graded "
            "slabs, accessories...). Stock = total available, then one column per store / warehouse (set in Settings). "
            "Out-of-stock listings are hidden unless 'Show out of stock' is ticked. Drag a column heading to move it.\n"
            "Select several rows (⌘/Ctrl-click or Shift-click) and press 'Update Shopify prices…' to change our prices "
            "in Shopify; nothing is sent until you confirm.\n"
            "Margin = profit as % of the price, using Shopify's cost per item. ⚠ = TCGplayer's price is below our cost.\n"
            "TCGplayer prices are a once-a-day snapshot. ★ = presale · ⚡ = fast-moving (presale or new set), check it live "
            "· ✎ = price typed in with 'Enter live price…'.\n"
            "Double-click a row (or press Enter) to open it on TCGplayer. Right-click for more options.\n\n"
            + ("Shortcuts: ⌘F search · ⌘R check prices again · ⌘+ / ⌘− / ⌘0 text size · Esc clears the search"
               if IS_MAC else
               "Shortcuts: Ctrl+F search · F5 check prices again · Ctrl+ / Ctrl− / Ctrl+0 text size · "
               "Esc clears the search"))


IS_MAC = sys.platform == "darwin"
MULTI_CLICK = "⌘-click" if IS_MAC else "Ctrl+click"
ZOOM_KEY = "Command" if IS_MAC else "Control"
TEXT_SIZES = {"Smaller": -2, "Normal": 0, "Larger": 2, "Largest": 4}  # steps added to the system font size


def fit_window(win, width, height, share_w=0.9, share_h=0.85):
    """Open at a comfortable size for this screen (never bigger than it), centred."""
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    w, h = min(width, int(sw * share_w)), min(height, int(sh * share_h))
    win.geometry(f"{w}x{h}+{max((sw - w) // 2, 0)}+{max((sh - h) // 3, 0)}")


def pixels(value):
    """A size read back from Tk as a number. Depending on the computer Tk may hand back an int, a string
    such as "12" or "12.0", or its own object type, so convert via text."""
    try:
        return int(float(str(value)))
    except ValueError:
        return 0


class Flow(ttk.Frame):
    """A row of buttons that wraps onto another line when the window is too narrow for one."""

    def __init__(self, master, gap=6, **kw):
        super().__init__(master, **kw)
        self.gap, self.items = gap, []
        self.bind("<Configure>", lambda e: self.reflow())

    def add(self, widget, space_before=0):
        self.items.append((widget, space_before))
        return widget

    def reflow(self):
        width = self.winfo_width() if self.winfo_width() > 1 else 10_000
        x = y = row_h = 0
        for widget, space in self.items:
            w, h = widget.winfo_reqwidth(), widget.winfo_reqheight()
            if x and x + space + w > width:  # start a new line
                x, y, row_h = 0, y + row_h + self.gap // 2, 0
            elif x:
                x += space
            widget.place(x=x, y=y)
            x += w + self.gap
            row_h = max(row_h, h)
        if pixels(self.cget("height")) != y + row_h:
            self.configure(height=y + row_h)
            self.event_generate("<<FlowChanged>>")


class ScrollArea(ttk.Frame):
    """Holds a frame (.inner) that is stretched to the visible size when it fits and scrolls when the
    window is too small for it. Scrollbars only appear when they are needed."""

    def __init__(self, master, width=None, height=None, **kw):
        super().__init__(master, **kw)
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0,
                                background=ttk.Style().lookup("TFrame", "background") or "white")
        if width:
            self.canvas.configure(width=width)
        if height:
            self.canvas.configure(height=height)
        self.ysb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.xsb = ttk.Scrollbar(self, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=self.ysb.set, xscrollcommand=self.xsb.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.inner = ttk.Frame(self.canvas)
        self._win = self.canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self._pending = False
        self._need = {"x": False, "y": False}
        self.canvas.bind("<Configure>", lambda e: self.refit())
        self.inner.bind("<Configure>", lambda e: self.refit())
        top = self.winfo_toplevel()
        top.bind("<<FlowChanged>>", lambda e: self.refit(), add="+")
        top.bind("<MouseWheel>", self._wheel, add="+")
        top.bind("<Shift-MouseWheel>", lambda e: self._wheel(e, "x"), add="+")
        top.bind("<Button-4>", lambda e: self._scroll(e, -1, "y"), add="+")
        top.bind("<Button-5>", lambda e: self._scroll(e, 1, "y"), add="+")

    def refit(self):
        """Call after the contents change size (wrapped text, added rows)."""
        if not self._pending:
            self._pending = True
            self.after_idle(self._fit)

    def _fit(self):
        self._pending = False
        if not self.winfo_exists():
            return
        cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        if cw <= 1 or ch <= 1:
            return
        need_w, need_h = self.inner.winfo_reqwidth(), self.inner.winfo_reqheight()
        w, h = max(cw, need_w), max(ch, need_h)
        self.canvas.itemconfigure(self._win, width=w, height=h)
        self.canvas.configure(scrollregion=(0, 0, w, h))
        for axis, bar, needed, place in (("y", self.ysb, need_h > ch, dict(row=0, column=1, sticky="ns")),
                                         ("x", self.xsb, need_w > cw, dict(row=1, column=0, sticky="ew"))):
            if needed != self._need[axis]:
                self._need[axis] = needed
                if needed:
                    bar.grid(**place)
                else:
                    bar.grid_remove()
                    (self.canvas.yview_moveto if axis == "y" else self.canvas.xview_moveto)(0)

    def _scroll(self, event, units, axis):
        # lists and text boxes scroll themselves; only scroll here for the rest of this area
        if not units or not self._need[axis] or isinstance(event.widget, (tk.Text, ttk.Treeview)):
            return
        if not str(event.widget).startswith(str(self)):
            return
        (self.canvas.yview_scroll if axis == "y" else self.canvas.xview_scroll)(units, "units")

    def _wheel(self, event, axis="y"):
        d = event.delta
        self._scroll(event, -d if abs(d) < 120 else -int(d / 120), axis)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("TCG Price Checker")
        try:
            import base64
            self._icon = tk.PhotoImage(data=base64.b64encode(icon_png(64)))
            self.iconphoto(True, self._icon)
        except tk.TclError:
            pass
        self.minsize(700, 450)
        self.checker = PriceChecker()
        saved = self.checker.config["settings"].get("window", {})
        if not self._restore_geometry(saved.get("geometry")):
            fit_window(self, 1500, 900)
        if saved.get("maximized"):
            self.after(50, self._maximize)
        self.events = queue.Queue()
        self.busy = False
        self.filter = tk.StringVar(value=saved.get("filter") if saved.get("filter") in dict(FILTERS) else "All")
        self.search = tk.StringVar()
        self.sort_key, self.sort_reverse = (saved.get("sort") or [None, False])
        self.kind = tk.StringVar(value=saved.get("kind") if saved.get("kind") in KINDS else "Everything")
        self.show_zero = tk.BooleanVar(value=bool(self.checker.config["settings"].get("show_zero_stock")))
        self.columns = list(COLUMNS)   # plus one stock column per Shopify location, added once they're known
        self.locations = []
        self.loc_ids = {}      # "loc0" -> stock column name
        self.col_order = [c[0] for c in COLUMNS]  # column ids in the order shown (headings can be dragged)
        self._drag = None
        self._order_set = False
        self._fill_job = self._search_job = None
        self.visible = []
        self._setup_colors()
        self._setup_fonts()
        self._build_ui()
        self.apply_text_size()
        self._bind_keys()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self._poll_job = self.after(100, self._poll)
        if self.checker.load_last_check():  # previous results straight away; a fresh check follows
            self.refresh_table()
            self.show_status(refreshing=True)
        self.after(300, self.start_check)

    # ------------------------------------------------------------------ window state, colours, keys
    def _restore_geometry(self, geometry):
        """Reopen where it was last time, unless that's off this screen (e.g. a different monitor)."""
        try:
            size, x, y = geometry.replace("-", "+-").split("+")[0], *map(int, geometry.replace("-", "+-").split("+")[1:3])
            w, h = map(int, size.split("x"))
        except (AttributeError, ValueError):
            return False
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        if w > sw or h > sh or x < -50 or y < -50 or x + 100 > sw or y + 100 > sh:
            return False
        self.geometry(geometry)
        return True

    def _maximize(self):
        try:
            self.state("zoomed")
        except tk.TclError:
            pass

    def on_close(self):
        """Remember the window, filter and sort for next time."""
        try:
            maximized = self.state() == "zoomed"
        except tk.TclError:
            maximized = False
        settings = self.checker.config["settings"]
        settings["window"] = {"geometry": settings.get("window", {}).get("geometry") if maximized else self.geometry(),
                              "maximized": maximized, "filter": self.filter.get(), "kind": self.kind.get(),
                              "sort": [self.sort_key, self.sort_reverse],
                              "column_order": self._order_tokens()}
        settings["show_zero_stock"] = bool(self.show_zero.get())
        try:
            self.checker.save()
        except OSError:
            pass
        self.destroy()

    def report_callback_exception(self, exc, value, tb):
        """Any error inside the window: save the details next to the app and say so, instead of failing silently."""
        details = "".join(traceback.format_exception(exc, value, tb))
        print(details, file=sys.stderr)
        log = Path(HERE) / "error_log.txt"
        try:
            with open(log, "a", encoding="utf-8") as f:
                f.write(f"--- {dt.datetime.now():%Y-%m-%d %H:%M:%S} · {sys.platform} · "
                        f"Tk {self.tk.call('info', 'patchlevel')}\n{details}\n")
        except OSError:
            pass
        now = time.time()
        if now - getattr(self, "_last_error_box", 0) > 10:  # one message, not a flood
            self._last_error_box = now
            messagebox.showerror("TCG Price Checker", f"Something didn't work: {value}\n\nThe details were saved to:\n"
                                 f"{log}\n\nPlease send that file to whoever looks after this app.", parent=self)

    def destroy(self):
        try:
            self.after_cancel(self._poll_job)  # stop the background-progress timer cleanly
        except (AttributeError, tk.TclError):
            pass
        super().destroy()

    def _setup_colors(self):
        """Grey helper text: fixed greys on Windows; the Mac's own label colours so dark mode stays readable."""
        def system(name, fallback):
            try:
                self.winfo_rgb(name)
                return name
            except tk.TclError:
                return fallback
        if IS_MAC:  # mid-grey fallbacks stay readable in both light and dark mode
            self.colors = {"text": system("systemLabelColor", system("systemTextColor", "#8a8a8a")),
                           "muted": system("systemSecondaryLabelColor", "#8a8a8a"),
                           "faint": system("systemTertiaryLabelColor", "#8a8a8a")}
        else:
            self.colors = {"text": "#333", "muted": "#555", "faint": "#777"}
        self.colors["warn"] = "#b45309"

    def _bind_keys(self):
        cmd = ZOOM_KEY  # Command on a Mac, Control elsewhere
        for keys, step in (("plus", 1), ("equal", 1), ("minus", -1), ("0", None)):
            self.bind_all(f"<{cmd}-{keys}>", lambda e, s=step: self.zoom(s))
        self.bind_all(f"<{cmd}-f>", lambda e: (self.search_entry.focus_set(), self.search_entry.select_range(0, "end")))
        self.bind_all(f"<{cmd}-r>", lambda e: self.start_check())
        self.bind_all("<F5>", lambda e: self.start_check())
        self.search_entry.bind("<Escape>", lambda e: (self.search.set(""), self.tree.focus_set()))
        self.tree.bind("<Return>", lambda e: self.open_tcgplayer())

    # ------------------------------------------------------------------ fonts and sizing
    def _setup_fonts(self):
        """Use the computer's own interface font (Segoe UI on Windows, San Francisco on a Mac) at its own
        size, so the app looks native; the Text size setting adds or removes a few points from there."""
        from tkinter import font as tkfont
        self.named = [tkfont.nametofont(n) for n in ("TkDefaultFont", "TkTextFont", "TkHeadingFont")]
        self.base_size = (abs(pixels(self.named[0].actual("size"))) or 13) + (1 if sys.platform == "win32" else 0)
        family = self.named[0].actual("family")
        self.fonts = {"body": self.named[0],
                      "bold": tkfont.Font(self, family=family, weight="bold"),
                      "big": tkfont.Font(self, family=family, weight="bold"),
                      "title": tkfont.Font(self, family=family, weight="bold")}

    def apply_text_size(self):
        step = int(self.checker.config["settings"].get("text_size", 0))
        size = max(self.base_size + step, 7)
        for f in self.named:
            f.configure(size=size)
        self.fonts["bold"].configure(size=size)
        self.fonts["big"].configure(size=size + 1)
        self.fonts["title"].configure(size=size + 6)
        style = ttk.Style(self)
        style.configure("Big.TButton", font=self.fonts["big"], padding=(14, 6))
        style.configure("Treeview", rowheight=self.fonts["body"].metrics("linespace") + 8, font=self.fonts["body"])
        style.configure("Treeview.Heading", font=self.fonts["bold"])
        self.update_idletasks()
        for flow in self.flows:
            flow.reflow()
        self._fit_columns()
        self._wrap_text()

    def show_help(self):
        messagebox.showinfo("How to read the list", help_text(self.checker.config["settings"]), parent=self)

    def zoom(self, step):
        s = self.checker.config["settings"]
        s["text_size"] = 0 if step is None else max(-4, min(8, int(s.get("text_size", 0)) + step))
        self.checker.save()
        self.apply_text_size()

    def _fit_columns(self, event=None):
        """Fixed columns get what their text needs; the two product-name columns share the rest of the width."""
        avail = self.tree.winfo_width() - 4
        if avail < 50:
            return
        digit = self.fonts["body"].measure("0")
        fixed = {cid: max(chars * digit, self.fonts["bold"].measure(heading) + 18)
                 for cid, heading, chars, _, _ in self.columns if cid not in FLEX_COLUMNS}
        flex_min = sum(FLEX_MIN_CHARS.values()) * digit
        hidden = []
        loc_ids = [c[0] for c in self.columns if c[0].startswith("loc")]
        for group in HIDE_WHEN_NARROW:
            if sum(w for c, w in fixed.items() if c not in hidden) + flex_min <= avail:
                break
            hidden.extend(loc_ids if group == "LOCATIONS" else [group])
        wanted = tuple(c for c in self.col_order if c not in hidden)
        if tuple(self.tk.splitlist(self.tree["displaycolumns"])) != wanted:
            self.tree["displaycolumns"] = wanted
        left = avail - sum(w for c, w in fixed.items() if c not in hidden)
        widths = dict(fixed)
        for cid, share in FLEX_COLUMNS.items():
            widths[cid] = max(int(left * share), FLEX_MIN_CHARS[cid] * digit)  # below this: sideways scrollbar
        for cid, w in widths.items():
            self.tree.column(cid, width=w)

    def _wrap_text(self, event=None):
        width = max(self.winfo_width() - 40, 300)
        self.detail.configure(wraplength=width)
        self.help.configure(wraplength=width)
        self._size_detail()

    def _size_detail(self):
        needed = self.detail.winfo_reqheight() if self.detail.cget("text") else 1
        cap = max(int(self.winfo_height() * 0.2), self.fonts["body"].metrics("linespace") * 2)
        height = min(needed, cap)
        if pixels(self.detail_box.cget("height")) != height:
            self.detail_box.configure(height=height)

    # ------------------------------------------------------------------ layout
    def _build_ui(self):
        self.flows = []

        top = Flow(self)
        top.pack(fill="x", padx=12, pady=(10, 4))
        top.add(ttk.Label(top, text="TCG Price Checker", font=self.fonts["title"]))
        self.run_btn = top.add(ttk.Button(top, text="▶  Check prices now", style="Big.TButton",
                                          command=self.start_check), space_before=14)
        top.add(ttk.Button(top, text="Open printable report", command=self.open_report))
        top.add(ttk.Button(top, text="Saved choices", command=self.open_saved))
        top.add(ttk.Button(top, text="Settings", command=self.open_settings))
        top.add(ttk.Button(top, text="Export this list…", command=self.export_list))
        top.add(ttk.Button(top, text="Help", command=self.show_help))
        self.flows.append(top)

        bar = self.filter_bar = Flow(self, gap=4)
        bar.pack(fill="x", padx=12, pady=4)
        self.filter_buttons = {}
        for label, _ in FILTERS:
            self.filter_buttons[label] = bar.add(ttk.Radiobutton(
                bar, text=label, value=label, variable=self.filter, style="Toolbutton", command=self.refresh_table))
        search = ttk.Frame(bar)
        ttk.Label(search, text="Search:").pack(side="left", padx=(0, 4))
        self.search_entry = ttk.Entry(search, textvariable=self.search, width=28)
        self.search_entry.pack(side="left")
        bar.add(search, space_before=14)
        kind = ttk.Frame(bar)
        ttk.Label(kind, text="Show:").pack(side="left", padx=(0, 4))
        box = ttk.Combobox(kind, textvariable=self.kind, values=list(KINDS), state="readonly", width=11)
        box.pack(side="left")
        box.bind("<<ComboboxSelected>>", lambda e: self.refresh_table())
        bar.add(kind, space_before=10)
        bar.add(ttk.Checkbutton(bar, text="Show out of stock", variable=self.show_zero,
                                command=self._toggle_zero), space_before=10)
        # wait for a pause in typing before redrawing: with a big catalogue every keystroke would redraw
        self.search.trace_add("write", lambda *_: self._search_soon())
        self.flows.append(bar)

        # bottom rows are packed before the table so they stay visible when the window is made small
        statusbar = ttk.Frame(self)
        statusbar.pack(side="bottom", fill="x", padx=12, pady=(0, 8))
        self.status = ttk.Label(statusbar, text="", foreground=self.colors["muted"])
        self.status.pack(side="left")
        self.progress = ttk.Progressbar(statusbar, mode="indeterminate", length=160)
        self.progress.pack(side="right")
        self.help = ttk.Label(self, text=HINT, foreground=self.colors["faint"], justify="left")
        self.help.pack(side="bottom", fill="x", padx=12, pady=(2, 6))
        # details of the selected row: a box whose height is capped so the table always keeps most of the room
        self.detail_box = ttk.Frame(self, height=1)
        self.detail_box.pack(side="bottom", fill="x", padx=12, pady=2)
        self.detail_box.pack_propagate(False)
        self.detail = ttk.Label(self.detail_box, text="", foreground=self.colors["text"], justify="left", anchor="nw")
        self.detail.pack(fill="both", expand=True)
        actions = Flow(self)
        actions.pack(side="bottom", fill="x", padx=12, pady=(4, 2))
        self.action_buttons = [actions.add(ttk.Button(actions, text=text, command=cmd)) for text, cmd in (
            ("Open on TCGplayer", self.open_tcgplayer),
            ("✓ Match is correct", self.confirm_match),
            ("Choose a different match…", self.change_match),
            ("Enter live price…", self.enter_live_price),
            ("Update Shopify prices…", self.update_shopify_prices),
            ("We price this ourselves", self.toggle_self_priced),
            ("Hide this product", self.hide_product),
            ("Undo my choice", self.reset_choice))]
        for b in self.action_buttons:
            b.state(["disabled"])
        self.flows.append(actions)
        self.bind("<Configure>", self._wrap_text, add="+")

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, padx=12, pady=4)
        self.tree = ttk.Treeview(body, columns=[c[0] for c in self.columns], show="headings", selectmode="extended")
        self._setup_columns()
        self.tree.bind("<Configure>", self._fit_columns, add="+")
        for tag, color in ROW_COLORS.items():
            self.tree.tag_configure(tag, background=color, foreground="#1d1d1b")
        ysb = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        xsb = ttk.Scrollbar(body, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        ysb.grid(row=0, column=1, sticky="ns")
        xsb.grid(row=1, column=0, sticky="ew")
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)
        self.tree.bind("<Double-1>", lambda e: self.open_tcgplayer())
        for button in (("<Button-2>", "<Control-Button-1>") if IS_MAC else ("<Button-3>",)):
            self.tree.bind(button, self._context_menu)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._update_detail())
        self.tree.bind("<ButtonPress-1>", self._drag_start, add="+")
        self.tree.bind("<B1-Motion>", self._drag_move, add="+")
        self.tree.bind("<ButtonRelease-1>", self._drag_end, add="+")

        self.menu = tk.Menu(self, tearoff=0)
        for b in self.action_buttons:
            self.menu.add_command(label=b.cget("text"), command=b.cget("command"))
        self.heading_menu = tk.Menu(self, tearoff=0)
        self.heading_menu.add_command(label="Reset column order", command=self.reset_column_order)

    # ------------------------------------------------------------------ background work
    def run_in_background(self, label, work, done=None):
        if self.busy:
            return
        self.busy = True
        self.run_btn.state(["disabled"])
        self._update_detail()
        self.progress.start(12)
        self.status.config(text=label)
        self.config(cursor="watch")

        def worker():
            try:
                result = work(lambda msg: self.events.put(("progress", msg)))
                self.events.put(("done", (done, result)))
            except SetupError as e:
                self.events.put(("error", str(e)))
            except Exception as e:  # network trouble etc. - explain it, keep details for whoever maintains this
                log = Path(HERE) / "error_log.txt"
                try:
                    log.write_text(traceback.format_exc(), encoding="utf-8")
                except OSError:
                    pass
                self.events.put(("error", friendly_error(e, self.checker.stage)))

        threading.Thread(target=worker, daemon=True).start()

    def _poll(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "progress":
                    self.status.config(text=payload)
                    continue
                self.busy = False
                self.run_btn.state(["!disabled"])
                self._update_detail()
                self.progress.stop()
                self.config(cursor="")
                if kind == "error":
                    self.status.config(text="Stopped - see message" + (" · showing the previous results"
                                                                        if self.checker.rows else ""))
                    messagebox.showerror("TCG Price Checker", payload, parent=self)
                else:
                    done, result = payload
                    if done:
                        done(result)
        except queue.Empty:
            pass
        self._poll_job = self.after(100, self._poll)

    def choose_login_file(self, parent, intro=None):
        """File picker for config.yaml; keeps asking until the file is usable or the person cancels.
        Returns the chosen path (already checked) or None."""
        if intro:
            messagebox.showinfo("TCG Price Checker", intro, parent=parent)
        while True:
            current = self.checker.credentials_path
            path = filedialog.askopenfilename(
                parent=parent, title="Choose your Shopify login file (config.yaml)",
                initialdir=str(current.parent) if current and current.parent.is_dir() else str(Path.home()),
                filetypes=[("YAML", "*.yaml *.yml"), ("All files", "*.*")])
            if not path:
                return None
            problem = check_credentials_file(path)
            if not problem:
                return path
            if not messagebox.askretrycancel("Not a Shopify login file", problem, parent=parent):
                return None

    def use_login_file(self, path):
        self.checker.credentials_path = Path(path)
        self.checker.config["settings"]["credentials_file"] = str(path)
        self.checker.save()

    def start_check(self):
        if not os.environ.get("SHOPIFY_TOKEN") and check_credentials_file(self.checker.credentials_path):
            # first run on this computer (or the file moved): ask for the Shopify login file, then remember it
            path = self.choose_login_file(self, intro=(
                "Please choose your config.yaml file. It holds the Shopify login "
                "(the shopify: section with store_url and client_id / client_secret).\n\n"
                "It's remembered, and you can change it any time in Settings."))
            if not path:
                self.status.config(text="No login file chosen. Choose it in Settings, then press Check prices now.")
                return
            self.use_login_file(path)

        def work(progress):
            self.checker.run(progress)
            return self.checker.write_reports()

        def done(paths):
            self.refresh_table()
            self.show_status()

        self.run_in_background("Starting…", work, done)

    def show_status(self, refreshing=False):
        when, hours = self.checker.snapshot_age()
        age = f"TCGplayer prices from {when} ({hours:.0f} hours old)" if when else "TCGplayer snapshot time unknown"
        checked = parse_time(self.checker.checked_at)
        checked = checked.astimezone().strftime("%b %d, %I:%M %p").replace(" 0", " ") if checked else ""
        rows = self.checker.rows
        hidden = 0 if self.show_zero.get() else sum(not has_stock(r) for r in rows)
        lead = (f"Showing the last check ({checked}) · checking for changes…" if refreshing
                else f"Checked {len(rows)} listings" + (f" ({hidden} out of stock hidden)" if hidden else ""))
        notes = "".join(f" · {n}" for n in self.checker.notes)
        self.status.config(text=f"{lead} · {age}{notes}",
                           foreground=self.colors["warn"] if (hours and hours > 30) or notes else self.colors["muted"])

    # ------------------------------------------------------------------ table
    def _setup_columns(self):
        """(Re)build the table's columns: the fixed ones plus one stock column per Shopify location."""
        tokens = self._order_tokens() if self._order_set else None  # keep a dragged order when columns change
        self._order_set = True
        self.locations = self.checker.locations()
        self.columns = list(COLUMNS)
        at = next(i for i, c in enumerate(self.columns) if c[0] == "stock") + 1
        grouped = bool(self.checker.config["settings"].get("stock_columns"))
        short = {n: n for n in self.locations} if grouped else short_names(self.locations)
        self.loc_ids = {}
        for i, name in enumerate(self.locations):  # full names are in the details below the table
            self.loc_ids[f"loc{i}"] = name
            self.columns.insert(at + i, (f"loc{i}", short[name], 6, "e",
                                         lambda r, n=name: (r.get("locations") or {}).get(n, 0)))
        self.tree["displaycolumns"] = ("#all",)
        self.tree["columns"] = [c[0] for c in self.columns]
        self._apply_saved_order(tokens)
        for cid, heading, _, anchor, _ in self.columns:
            self.tree.heading(cid, text=heading, command=lambda c=cid: self.sort_by(c))
            self.tree.column(cid, minwidth=30, anchor=anchor, stretch=False)
        if self.sort_key and self.sort_key not in self.tree["columns"]:
            self.sort_key = None
        self._fit_columns()

    def _row_text(self, r):
        product = r["shopify_title"] + (f"  —  {r['variant']}" if r["variant"] else "")
        amt = "" if r["diff_amt"] is None else ("+" if r["diff_amt"] > 0 else "−") + money(abs(r["diff_amt"]))
        margin = lambda m: "" if m is None else f"{m:.0f}%"
        tcg_margin = margin(r["tcg_margin_pct"]) + ("  ⚠" if r["below_cost"] else "")
        flags = ("★" if r["presale"] else "") + ("⚡" if r["fast_moving"] else "") + ("✎" if r["live_entered"] else "")
        at = r.get("locations")
        locs = tuple("" if at is None else at.get(n, 0) for n in self.locations)
        return (r["tag"], flags, product, money(r["our_price"]), money(r["cost"]),
                margin(r["margin_pct"]), money(r["tcg_price"]), tcg_margin, pct(r["diff_pct"]), amt,
                stock_text(r["stock"])) + locs + (r["tcg_name"] or "—", r["match"])

    def _matches_filter(self, r, code):
        if code is None:
            return True
        if code == "need_update":
            return r["tag"] in NEED_UPDATE
        if code in ("presale", "below_cost", "fast_moving"):
            return r[code]
        return r["tag"] == code

    def _search_soon(self):
        if self._search_job:
            self.after_cancel(self._search_job)
        self._search_job = self.after(250, self.refresh_table)

    def _toggle_zero(self):
        self.checker.config["settings"]["show_zero_stock"] = bool(self.show_zero.get())
        self.refresh_table()
        if not self.busy:
            self.show_status()

    def refresh_table(self, fallback_index=None):
        """Redraw the list. If the selected row is gone, select the row now at fallback_index."""
        self._search_job = None
        if self._fill_job:
            self.after_cancel(self._fill_job)
            self._fill_job = None
        if self.checker.locations() != self.locations:
            self._setup_columns()
        lowest = self.checker.config["settings"].get("tcg_price_field") == "lowPrice"
        for cid, heading, *_ in self.columns:
            if cid == "tcg":
                heading = "TCG lowest" if lowest else "TCG market"
            arrow = (" ▼" if self.sort_reverse else " ▲") if cid == self.sort_key else ""
            self.tree.heading(cid, text=heading + arrow)
        kind = KINDS.get(self.kind.get())
        show_zero = self.show_zero.get()
        base = [r for r in self.checker.rows
                if (show_zero or has_stock(r)) and (kind is None or r.get("kind", "sealed") == kind)]
        # one pass for the counts on the filter buttons
        counts = dict.fromkeys((label for label, _ in FILTERS), 0)
        counts["All"] = len(base)
        by_code = {code: label for label, code in FILTERS if code}
        for r in base:
            label = by_code.get(r["tag"])
            if label:
                counts[label] += 1
            if r["tag"] in NEED_UPDATE:
                counts["Need update"] += 1
            for flag in ("presale", "below_cost", "fast_moving"):
                if r[flag]:
                    counts[by_code[flag]] += 1
        for label, b in self.filter_buttons.items():
            b.config(text=f"{label} ({counts[label]})" if self.checker.rows else label)
        self.after_idle(self.filter_bar.reflow)  # the counts change the buttons' widths
        code = dict(FILTERS)[self.filter.get()]
        needle = self.search.get().strip().lower()
        visible = [r for r in base if self._matches_filter(r, code)
                   and (not needle or needle in f"{r['shopify_title']} {r['variant']} {r['tcg_name']}".lower())]
        if self.sort_key:
            key = next(c[4] for c in self.columns if c[0] == self.sort_key)
            visible.sort(key=key, reverse=self.sort_reverse)
        selected = self._selected()
        want = (selected["shopify_title"], selected["variant"]) if selected else None
        self.tree.delete(*self.tree.get_children())
        self.visible = visible
        found = None
        if want:
            found = next((i for i, r in enumerate(visible) if (r["shopify_title"], r["variant"]) == want), None)
        first = max(FIRST_BATCH, (found or 0) + 50)
        self._insert_rows(0, min(first, len(visible)))
        if found is not None:
            self.tree.selection_set(str(found))
            self.tree.see(str(found))
        elif fallback_index is not None and visible:
            i = str(min(fallback_index, len(visible) - 1))
            self.tree.selection_set(i)
            self.tree.see(i)
        if len(visible) > first:
            self._fill_job = self.after(1, self._fill_more, first)
        self._update_detail()

    def _insert_rows(self, start, end):
        insert, text = self.tree.insert, self._row_text
        for i in range(start, end):
            r = self.visible[i]
            insert("", "end", iid=str(i), values=text(r), tags=(r["tag"],))

    def _fill_more(self, start):
        """Add the rest of a long list a batch at a time, between other window events."""
        end = min(start + BATCH, len(self.visible))
        self._insert_rows(start, end)
        self._fill_job = self.after(1, self._fill_more, end) if end < len(self.visible) else None

    def sort_by(self, cid):
        self.sort_reverse = (not self.sort_reverse if self.sort_key == cid
                             else cid in ("diff", "amt", "ours", "tcg", "cost"))
        self.sort_key = cid
        self.refresh_table()

    def _selected(self):
        """The row the details are shown for: the last one clicked, if it's selected, else the first selected."""
        sel = self.tree.selection()
        if not sel:
            return None
        iid = self.tree.focus() if self.tree.focus() in sel else sel[0]
        return self.visible[int(iid)] if int(iid) < len(self.visible) else None

    def _selected_rows(self):
        return [self.visible[int(i)] for i in self.tree.selection() if int(i) < len(self.visible)]

    def _update_detail(self):
        r = self._selected()
        for i, b in enumerate(self.action_buttons):
            # while a check runs you can still look things up, but choices wait until it's finished
            usable = r and (i == 0 or not self.busy)
            b.state(["!disabled"] if usable else ["disabled"])
        if not r:
            self.detail.config(text="")
            self._size_detail()
            return
        parts = [f"{r['shopify_title']}{' — ' + r['variant'] if r['variant'] else ''}  ({r['channel']}, {r['type']})"]
        at = r.get("locations")
        stock = f"Stock {stock_text(r['stock']) or '—'}"
        if at is not None:
            stock += "  ·  " + "  ·  ".join(f"{n}: {at.get(n, 0)}" for n in self.locations)
        other = untracked_stock(r, self.checker.config["settings"].get("stock_columns"))
        if other:
            stock += "  ·  not counted: " + ", ".join(f"{n} {q}" for n, q in other.items())
        n = len(self.tree.selection())
        if n > 1:
            stock += f"  ·  ({n} rows selected)"
        parts.append(stock)
        if r.get("suggested_price"):
            parts.append(f"Suggested new price: {money(r['suggested_price'])} (TCGplayer"
                         + (f", {CONDITIONS.get(r['condition'], r['condition'])}" if r.get("condition") else "") + ")")
        if r.get("tag") == "NO PRICE SOURCE":
            parts.append("TCGplayer doesn't price this kind of product (sports cards, graded slabs, accessories, "
                         "merchandise...), so it isn't compared.")
        if r["tcg_name"]:
            parts.append(f"TCGplayer: {r['tcg_name']}  ·  set: {r['tcg_set']}  ·  match: {r['match']}"
                         + (f"  ·  {r['finish']}" if r.get("finish") else "")
                         + (f"  ·  condition {CONDITIONS.get(r['condition'], r['condition'])}" if r.get("condition") else ""))
            parts.append(f"Compared against: {r['source'] or 'no TCGplayer price'}"
                         + (f"  ·  cheapest listing at snapshot time {money(r['tcg_low'])}" if r["tcg_low"] is not None else ""))
        if r["live_entered"]:
            entered = parse_time(r["live_entered"]).astimezone().strftime("%b %d, %I:%M %p").replace(" 0", " ")
            parts.append(f"✎ TCGplayer price typed in {entered}: {money(r['tcg_price'])}  "
                         f"(daily snapshot: {money(r['snapshot_price']) or 'no price'})")
        elif r["tag"] == "NO PRICE":
            parts.append("TCGplayer has no listings for this today, so the daily snapshot has no Market Price for it. "
                         "Open it on TCGplayer and use 'Enter live price…' to type in the Market Price shown there. "
                         "It's kept until the snapshot has a price again.")
        elif r["fast_moving"]:
            parts.append("⚡ Fast-moving: the daily snapshot may be out of date. Use 'Enter live price…' to check it.")
        if r["cost"]:
            parts.append(f"Cost per item {money(r['cost'])}  ·  profit at our price {money(r['our_price'] - r['cost'])}"
                         + (f"  ·  profit at TCGplayer price {money(r['tcg_price'] - r['cost'])}" if r["tcg_price"] else "")
                         + ("  ·  ⚠ TCGplayer is below our cost" if r["below_cost"] else ""))
        else:
            parts.append("No cost per item in Shopify")
        if r["presale"] and r["release"]:
            parts.append(f"Presale · releases {r['release']}")
        if r["note"]:
            parts.append(r["note"])
        self.detail.config(text="\n".join(parts))
        self._size_detail()

    def _context_menu(self, event):
        if self.tree.identify_region(event.x, event.y) == "heading":
            self.heading_menu.tk_popup(event.x_root, event.y_root)
            return
        row = self.tree.identify_row(event.y)
        if row:
            if row not in self.tree.selection():  # right-click inside a multi-row selection keeps it
                self.tree.selection_set(row)
            self.tree.focus(row)
            for i in range(1, len(self.action_buttons)):  # same rule as the buttons: no changes mid-check
                self.menu.entryconfigure(i, state="disabled" if self.busy else "normal")
            self.menu.tk_popup(event.x_root, event.y_root)

    # ------------------------------------------------------------------ column order (drag a heading)
    def _order_tokens(self):
        """The column order to save: location columns by name, so they keep their place if columns change."""
        return [f"loc:{self.loc_ids[c]}" if c in self.loc_ids else c for c in self.col_order]

    def _apply_saved_order(self, tokens=None):
        if tokens is None:
            tokens = self.checker.config["settings"].get("window", {}).get("column_order") or []
        by_token = {f"loc:{n}": c for c, n in self.loc_ids.items()}
        ids = [c[0] for c in self.columns]
        order = []
        for t in tokens:
            cid = by_token.get(t, t)
            if cid in ids and cid not in order:
                order.append(cid)
        for i, cid in enumerate(ids):  # columns not in the saved order go after the column before them
            if cid not in order:
                before = ids[i - 1] if i else None
                order.insert(order.index(before) + 1 if before in order else len(order), cid)
        self.col_order = order

    def reset_column_order(self):
        self._apply_saved_order([])
        self._fit_columns()

    def _heading_at(self, x):
        """Column id under x in the heading row, or None."""
        col = self.tree.identify_column(x)  # "#3" = third column shown
        shown = self.tree["displaycolumns"]
        shown = self.col_order if shown in ("#all", ("#all",)) else self.tk.splitlist(shown)
        try:
            return shown[int(col[1:]) - 1]
        except (ValueError, IndexError):
            return None

    def _drag_start(self, event):
        self._drag = None
        if self.tree.identify_region(event.x, event.y) == "heading":
            self._drag = {"col": self._heading_at(event.x), "x": event.x, "moved": False}

    def _drag_move(self, event):
        if self._drag and abs(event.x - self._drag["x"]) > 8:
            self._drag["moved"] = True
            self.tree.configure(cursor="sb_h_double_arrow")

    def _drag_end(self, event):
        drag, self._drag = self._drag, None
        if not drag or not drag["moved"]:
            return
        self.tree.configure(cursor="")
        target = self._heading_at(event.x)
        src = drag["col"]
        if not src or not target or target == src:
            return
        order = [c for c in self.col_order if c != src]
        i = order.index(target)
        order.insert(i + 1 if self.col_order.index(src) < self.col_order.index(target) else i, src)
        self.col_order = order
        self._fit_columns()

    # ------------------------------------------------------------------ row actions
    def open_tcgplayer(self):
        r = self._selected()
        if r and r["tcg_urls"]:
            for url in r["tcg_urls"][:3]:
                webbrowser.open(url)
        elif r:
            self.change_match()

    def _save_choice(self, r, **fields):
        sel = self.tree.selection()
        set_override(self.checker.config, r["shopify_title"], r["variant"], **fields)
        self.checker.save()
        self.checker.recompute_title(r["shopify_title"])
        # if the row drops out of this list (e.g. confirmed in "Check match"), move on to the next one
        self.refresh_table(fallback_index=int(sel[0]) if sel else None)

    def confirm_match(self):
        r = self._selected()
        if not r:
            return
        if not r["tcg_refs"]:
            messagebox.showinfo("TCG Price Checker", "This product has no TCGplayer match yet.\n"
                                "Use 'Choose a different match…' to pick one.", parent=self)
            return
        self._save_choice(r, tcg=r["tcg_refs"])

    def change_match(self):
        r = self._selected()
        if r:
            MatchPicker(self, r)

    def enter_live_price(self):
        """Type in the price TCGplayer shows right now; it's used until tcgcsv's next daily snapshot."""
        r = self._selected()
        if not r:
            return
        if not r["tcg_refs"]:
            messagebox.showinfo("Enter live price", "Choose the TCGplayer match first.", parent=self)
            return
        for url in r["tcg_urls"][:1]:
            webbrowser.open(url)
        current = f"{r['tcg_price']:.2f}" if r["tcg_price"] else ""
        text = simpledialog.askstring(
            "Enter live price",
            f"{r['shopify_title']}{' — ' + r['variant'] if r['variant'] else ''}\n\n"
            f"Today's snapshot price: {money(r['snapshot_price']) or 'none'}\n"
            "TCGplayer is opening in your browser.\n"
            + ("Type the 'As low as' price at the top of the page (the cheapest listing).\n"
               if self.checker.config["settings"].get("tcg_price_field") == "lowPrice" else
               "Scroll down to 'Price Points' and type the Market Price.\n"
               "(Not the big price at the top: that's just the cheapest listing.)\n")
            + "It's used until the next daily snapshot. Leave empty to go back to the snapshot.",
            initialvalue=current, parent=self)
        if text is None:
            return
        text = text.strip().replace("$", "").replace(",", "")
        try:
            price = float(text) if text else None
        except ValueError:
            messagebox.showerror("Enter live price", f"'{text}' isn't a price.", parent=self)
            return
        set_live_price(self.checker.config, r["shopify_title"], r["variant"], price)
        self.checker.save()
        sel = self.tree.selection()
        self.checker.recompute_title(r["shopify_title"])
        self.refresh_table(fallback_index=int(sel[0]) if sel else None)

    def update_shopify_prices(self):
        """Change our price in Shopify for the selected rows, after the person checks every new price."""
        rows = self._selected_rows()
        if not rows or self.busy:
            return
        missing = [r for r in rows if not r.get("variant_id")]
        rows = [r for r in rows if r.get("variant_id")]
        if not rows:
            messagebox.showinfo("Update Shopify prices", "These rows came from a Shopify file export or an older "
                                "check, so they can't be changed from here. Press Check prices now, then try again.",
                                parent=self)
            return
        PriceUpdate(self, rows, skipped=len(missing))

    def toggle_self_priced(self):
        r = self._selected()
        if not r:
            return
        current = find_override(self.checker.config, r["shopify_title"], r["variant"]) or {}
        fields = {k: v for k, v in current.items() if k not in ("shopify_title", "variant")}
        if r["tag"] == "SELF-PRICED" and current.get("self_priced"):
            fields.pop("self_priced", None)
        else:
            fields["self_priced"] = True
            fields.setdefault("tcg", r["tcg_refs"])
        self._save_choice(r, **fields)

    def hide_product(self):
        r = self._selected()
        if r and messagebox.askyesno("Hide product", f"Stop checking this product?\n\n{r['shopify_title']}"
                                     "\n\nYou can bring it back from 'Saved choices'.", parent=self):
            self._save_choice(r, ignore=True)

    def reset_choice(self):
        r = self._selected()
        if r:
            self._save_choice(r)

    # ------------------------------------------------------------------ windows
    def export_list(self):
        """Save exactly what's on screen (current filter and search) as a spreadsheet file."""
        if not self.visible:
            messagebox.showinfo("Export", "There's nothing in the list to export.", parent=self)
            return
        name = re.sub(r"[^a-z0-9]+", "_", self.filter.get().lower()).strip("_") or "all"
        path = filedialog.asksaveasfilename(
            parent=self, title="Export this list", defaultextension=".csv",
            initialfile=f"tcg_prices_{name}_{dt.date.today().isoformat()}.csv",
            filetypes=[("Spreadsheet (CSV)", "*.csv")])
        if not path:
            return
        try:
            write_csv(self.visible, path, self.locations)
        except OSError as e:
            messagebox.showerror("Export", f"Couldn't save the file:\n{e}", parent=self)
            return
        self.status.config(text=f"Exported {len(self.visible)} rows to {Path(path).name}")

    def open_report(self):
        if not self.checker.rows:
            messagebox.showinfo("TCG Price Checker", "Run a price check first.", parent=self)
            return
        html_path, _ = self.checker.write_reports()
        webbrowser.open(html_path.as_uri())

    def open_saved(self):
        SavedChoices(self)

    def open_settings(self):
        Settings(self)


def target_price(r):
    """The price to move a listing to: the suggested price, else the TCGplayer price, else our price."""
    for v in (r.get("suggested_price"), r.get("tcg_price"), r.get("our_price")):
        if v:
            return round(v, 2)
    return None


class PriceUpdate(tk.Toplevel):
    """Review and send new prices for the selected listings. Nothing goes to Shopify until Send is pressed
    and the summary is confirmed."""
    MAX_ROWS = 300

    def __init__(self, app, rows, skipped=0):
        super().__init__(app)
        self.app = app
        self.title("Update Shopify prices")
        self.transient(app)
        if len(rows) > self.MAX_ROWS:
            messagebox.showinfo("Update Shopify prices", f"{len(rows)} rows are selected; only the first "
                                f"{self.MAX_ROWS} are shown. Do the rest in another batch.", parent=app)
            rows = rows[:self.MAX_ROWS]
        self.rows = rows
        f = ttk.Frame(self, padding=14)
        f.pack(fill="both", expand=True)
        intro = (f"{len(rows)} listing{'s' if len(rows) != 1 else ''} selected. Check the new price for each one; "
                 "it starts at the suggested price (TCGplayer). Clear a box or leave our price to skip that listing.\n"
                 "Nothing changes in Shopify until you press Send and confirm.")
        if skipped:
            intro += f"\n{skipped} selected row(s) can't be changed from here (no Shopify link); run a fresh check first."
        self.minsize(420, 300)
        intro_label = ttk.Label(f, text=intro, justify="left", wraplength=760)
        intro_label.pack(anchor="w", fill="x", pady=(0, 8))
        f.bind("<Configure>", lambda e: intro_label.configure(wraplength=max(200, e.width - 30)), add="+")

        # buttons are packed before the list so they stay visible when the window is made small
        buttons = Flow(f)
        buttons.pack(side="bottom", fill="x", pady=(12, 0))
        buttons.add(ttk.Button(buttons, text="Send to Shopify…", command=self.send))
        buttons.add(ttk.Button(buttons, text="Cancel", command=self.destroy))
        buttons.add(ttk.Button(buttons, text="Use suggested prices", command=self.fill_suggested), space_before=18)
        buttons.add(ttk.Button(buttons, text="Keep our prices", command=self.fill_ours))

        area = ScrollArea(f, width=820, height=min(32 * (len(rows) + 1), 420))
        area.pack(fill="both", expand=True)
        grid = area.inner
        for c, head in enumerate(("Product", "Our price", "TCGplayer", "Cost", "New price", "Change")):
            ttk.Label(grid, text=head, font=app.fonts["bold"]).grid(row=0, column=c, sticky="w", padx=6, pady=(0, 4))
        self.entries, self.changes = [], []
        for i, r in enumerate(rows, 1):
            name = r["shopify_title"] + (f" — {r['variant']}" if r["variant"] else "")
            ttk.Label(grid, text=name[:60] + ("…" if len(name) > 60 else "")).grid(row=i, column=0, sticky="w", padx=6)
            for c, v in ((1, r["our_price"]), (2, r["tcg_price"]), (3, r["cost"])):
                ttk.Label(grid, text=money(v) or "—").grid(row=i, column=c, sticky="e", padx=6)
            var = tk.StringVar(value=f"{target_price(r):.2f}" if target_price(r) else "")
            ttk.Entry(grid, textvariable=var, width=9, justify="right").grid(row=i, column=4, padx=6, pady=1)
            change = ttk.Label(grid, text="", width=16)
            change.grid(row=i, column=5, sticky="w", padx=6)
            var.trace_add("write", lambda *_, r=r, v=var, l=change: self._show_change(r, v, l))
            self._show_change(r, var, change)
            self.entries.append(var)
        self.bind("<Escape>", lambda e: self.destroy())
        self.update_idletasks()  # measure the finished list, then open big enough to show it (if the screen allows)
        fit_window(self, grid.winfo_reqwidth() + 60, self.winfo_reqheight() + 20, 0.85, 0.85)

    @staticmethod
    def _parse(text):
        text = text.strip().replace("$", "").replace(",", "")
        return float(text) if text else None

    def _show_change(self, r, var, label):
        try:
            new = self._parse(var.get())
        except ValueError:
            label.config(text="not a price", foreground=self.app.colors["warn"])
            return
        if new is None or r["our_price"] is None or round(new, 2) == round(r["our_price"], 2):
            label.config(text="no change", foreground=self.app.colors["muted"])
            return
        pct_change = (new - r["our_price"]) / r["our_price"] * 100 if r["our_price"] else 0
        warn = " ⚠ below cost" if r["cost"] and new < r["cost"] else ""
        label.config(text=f"{pct_change:+.1f}%{warn}",
                     foreground=self.app.colors["warn"] if warn or abs(pct_change) >= 50 else self.app.colors["text"])

    def fill_suggested(self):
        for r, var in zip(self.rows, self.entries):
            p = target_price(r)
            var.set(f"{p:.2f}" if p else "")

    def fill_ours(self):
        for r, var in zip(self.rows, self.entries):
            var.set(f"{r['our_price']:.2f}" if r["our_price"] else "")

    def send(self):
        changes, bad = [], []
        for r, var in zip(self.rows, self.entries):
            try:
                new = self._parse(var.get())
            except ValueError:
                bad.append(r)
                continue
            if new is None or (r["our_price"] is not None and round(new, 2) == round(r["our_price"], 2)):
                continue
            if new <= 0:
                bad.append(r)
                continue
            changes.append((r, round(new, 2)))
        if bad:
            messagebox.showerror("Update Shopify prices", f"{len(bad)} price(s) aren't valid (must be a number above "
                                 f"0), e.g. {bad[0]['shopify_title']}.", parent=self)
            return
        if not changes:
            messagebox.showinfo("Update Shopify prices", "None of the prices are different from ours.", parent=self)
            return
        lines = [f"{r['shopify_title'][:50]}{' — ' + r['variant'] if r['variant'] else ''}: "
                 f"{money(r['our_price'])} → {money(p)}" for r, p in changes[:15]]
        if len(changes) > 15:
            lines.append(f"…and {len(changes) - 15} more")
        below = sum(1 for r, p in changes if r["cost"] and p < r["cost"])
        warn = f"\n\n⚠ {below} of these would be below our cost." if below else ""
        if not messagebox.askyesno(
                "Send to Shopify?", f"Change {len(changes)} price{'s' if len(changes) != 1 else ''} in Shopify? "
                "This changes the live price customers see (online and in the stores' POS).\n\n"
                + "\n".join(lines) + warn, icon="warning", default="no", parent=self):
            return
        self.destroy()
        app = self.app

        def done(result):
            changed, failed = result
            app.refresh_table()
            app.show_status()
            msg = f"Changed {len(changed)} price{'s' if len(changed) != 1 else ''} in Shopify."
            if failed:
                msg += f"\n\n{len(failed)} not changed:\n" + "\n".join(
                    f"• {r['shopify_title'][:50]}: {why}" for r, why in failed[:10])
            msg += "\n\nEvery change is logged in reports/price_changes.csv."
            (messagebox.showwarning if failed else messagebox.showinfo)("Update Shopify prices", msg, parent=app)

        app.run_in_background("Sending prices to Shopify…",
                               lambda progress: app.checker.push_prices(changes, progress), done)


class MatchPicker(tk.Toplevel):
    """Search TCGplayer sealed products and pick the right one (or several to average)."""

    def __init__(self, app, row):
        super().__init__(app)
        self.app, self.row = app, row
        self.single = row.get("kind") == "single"
        self.title("Choose the TCGplayer card" if self.single else "Choose the TCGplayer product")
        fit_window(self, 1100, 620, 0.8, 0.75)
        self.transient(app)
        ttk.Label(self, text=row["shopify_title"] + (f" — {row['variant']}" if row["variant"] else ""),
                  font=app.fonts["big"], padding=(12, 10, 12, 2)).pack(fill="x")
        ttk.Label(self, text=f"Pick the matching TCGplayer product. Select several ({MULTI_CLICK}) to use their "
                  "average price.", padding=(12, 0, 12, 6), foreground=app.colors["muted"]).pack(fill="x")
        bar = ttk.Frame(self, padding=(12, 0))
        bar.pack(fill="x")
        start = (row["shopify_title"] if self.single
                 else SHOPIFY_NOISE.sub(" ", f"{row['shopify_title']} {row['variant']}").strip())
        self.query = tk.StringVar(value=start)
        entry = ttk.Entry(bar, textvariable=self.query)
        entry.pack(side="left", fill="x", expand=True)
        entry.bind("<Return>", lambda e: self.search())
        ttk.Button(bar, text="Search", command=self.search).pack(side="left", padx=6)

        frame = ttk.Frame(self, padding=(12, 6))
        frame.pack(fill="both", expand=True)
        digit = app.fonts["body"].measure("0")
        cols = [("name", "TCGplayer product", 38, True), ("set", "Set", 22, True), ("game", "Game", 14, True),
                ("price", "Price", 9, False), ("score", "Fit", 6, False)]
        self.tree = ttk.Treeview(frame, columns=[c[0] for c in cols], show="headings", selectmode="extended")
        for cid, text, chars, stretch in cols:
            self.tree.heading(cid, text=text)
            self.tree.column(cid, width=chars * digit, stretch=stretch, anchor="e" if cid in ("price", "score") else "w")
        ysb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ysb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        ysb.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda e: self.use())

        buttons = ttk.Frame(self, padding=(12, 4, 12, 12))
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Use selected", style="Big.TButton", command=self.use).pack(side="left")
        ttk.Button(buttons, text="Open selected on TCGplayer", command=self.open_selected).pack(side="left", padx=6)
        ttk.Button(buttons, text="Not on TCGplayer - we price it ourselves", command=self.self_priced).pack(side="left", padx=6)
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right")
        self.results = []
        self.search()

    def search(self):
        self.config(cursor="watch")
        self.update_idletasks()
        try:
            self.results = self.app.checker.search_tcg(self.query.get(), single=self.single)
        finally:
            self.config(cursor="")
        self.tree.delete(*self.tree.get_children())
        current = {(t["category"], t["group"], t["product"]) for t in self.row["tcg_refs"]}
        for i, (score, p, price) in enumerate(self.results):
            self.tree.insert("", "end", iid=str(i), values=(p["name"], p["set"], p["game"], money(price), f"{score:.0%}"))
            if (p["category"], p["group"], p["id"]) in current:
                self.tree.selection_add(str(i))

    def _picked(self):
        return [self.results[int(i)][1] for i in self.tree.selection()]

    def use(self):
        picked = self._picked()
        if not picked:
            messagebox.showinfo("Choose", "Select at least one product.", parent=self)
            return
        self.app._save_choice(self.row, tcg=[{"category": p["category"], "group": p["group"], "product": p["id"]}
                                             for p in picked])
        self.destroy()

    def self_priced(self):
        self.app._save_choice(self.row, self_priced=True)
        self.destroy()

    def open_selected(self):
        for p in self._picked()[:3]:
            if p.get("url"):
                webbrowser.open(p["url"])


class SavedChoices(tk.Toplevel):
    """Every match/choice someone has saved, with a way to undo it."""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Saved choices")
        fit_window(self, 1100, 560, 0.8, 0.75)
        self.transient(app)
        ttk.Label(self, text="Choices saved by people. These always win over automatic matching.",
                  padding=(12, 10, 12, 6)).pack(fill="x")
        frame = ttk.Frame(self, padding=(12, 0))
        frame.pack(fill="both", expand=True)
        digit = app.fonts["body"].measure("0")
        cols = [("product", "Shopify product", 44), ("choice", "Choice", 40)]
        self.tree = ttk.Treeview(frame, columns=[c[0] for c in cols], show="headings")
        for cid, text, chars in cols:
            self.tree.heading(cid, text=text)
            self.tree.column(cid, width=chars * digit, stretch=True)
        ysb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ysb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        ysb.pack(side="right", fill="y")
        buttons = ttk.Frame(self, padding=12)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Remove selected choice", command=self.remove).pack(side="left")
        ttk.Button(buttons, text="Close", command=self.destroy).pack(side="right")
        self.fill()

    def describe(self, o):
        if o.get("ignore"):
            return "Hidden from the check"
        names = []
        for t in o.get("tcg", []):
            p = self.app.checker.index.product(t["category"], t["group"], t["product"])
            names.append(p["name"] if p else f"TCGplayer #{t['product']}")
        text = " + ".join(names) if names else ""
        if len(names) > 1:
            text = "Average of: " + text
        if o.get("self_priced"):
            text = "We price this ourselves" + (f" (reference: {text})" if text else "")
        return text

    def fill(self):
        self.tree.delete(*self.tree.get_children())
        for i, o in enumerate(self.app.checker.config["overrides"]):
            product = o["shopify_title"] + (f" — {o['variant']}" if o.get("variant") else "")
            self.tree.insert("", "end", iid=str(i), values=(product, self.describe(o)))

    def remove(self):
        sel = self.tree.selection()
        if not sel:
            return
        o = self.app.checker.config["overrides"][int(sel[0])]
        set_override(self.app.checker.config, o["shopify_title"], o.get("variant", ""))
        self.app.checker.save()
        if self.app.checker.shopify_items:
            self.app.checker.recompute_title(o["shopify_title"])
            self.app.refresh_table()
        self.fill()


class Settings(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Settings")
        self.transient(app)
        self.minsize(520, 320)
        s = app.checker.config["settings"]
        # Save / Cancel are packed first so they stay visible; everything else scrolls if the window is small
        buttons = ttk.Frame(self, padding=(16, 8, 16, 14))
        buttons.pack(side="bottom", fill="x")
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="Save", command=self.save).pack(side="right", padx=6)
        ttk.Separator(self).pack(side="bottom", fill="x")
        self.area = ScrollArea(self)
        self.area.pack(fill="both", expand=True)
        f = ttk.Frame(self.area.inner, padding=16)
        f.pack(fill="both", expand=True)
        f.columnconfigure(1, weight=1)
        self.wraps = []  # (label, space kept free beside it); wrap widths follow the window width
        self.area.canvas.bind("<Configure>", self._rewrap, add="+")
        self.vars = {}
        r = 0
        ttk.Label(f, text="Text size").grid(row=r, column=0, sticky="w", pady=3)
        current = int(s.get("text_size", 0))
        names = list(TEXT_SIZES)
        self.text_size = tk.StringVar(value=next((n for n, v in TEXT_SIZES.items() if v == current),
                                                 f"Custom ({current:+d})"))
        ttk.Combobox(f, textvariable=self.text_size, values=names, state="readonly", width=10).grid(
            row=r, column=1, sticky="w")
        r += 1
        ttk.Label(f, text=f"(or {'⌘' if IS_MAC else 'Ctrl'} + / − in the main window)",
                  foreground=app.colors["faint"]).grid(row=r, column=1, columnspan=2, sticky="w", pady=(0, 3))
        r += 1
        for key, label in [("drift_pct", "Drifting when our price is at least this % away"),
                           ("urgent_pct", "Urgent when our price is at least this % away"),
                           ("presale_drift_pct", "Presale: drifting at %"),
                           ("presale_urgent_pct", "Presale: urgent at %")]:
            self._wrap(ttk.Label(f, text=label), 330).grid(row=r, column=0, sticky="w", pady=3)
            v = tk.StringVar(value=f"{s[key]:g}")  # 3.0 -> "3"
            ttk.Spinbox(f, from_=0, to=100, increment=1, textvariable=v, width=6).grid(row=r, column=1, sticky="w")
            self.vars[key] = v
            r += 1

        self._wrap(ttk.Label(f, text="Compare our price against TCGplayer's"), 330).grid(row=r, column=0, sticky="w", pady=3)
        self.field = tk.StringVar(value=s.get("tcg_price_field", "marketPrice"))
        fr = ttk.Frame(f)
        fr.grid(row=r, column=1, columnspan=2, sticky="w")
        ttk.Radiobutton(fr, text="Market price", value="marketPrice", variable=self.field).pack(side="left")
        ttk.Radiobutton(fr, text="Lowest listing", value="lowPrice", variable=self.field).pack(side="left", padx=10)
        r += 1
        self._wrap(ttk.Label(f, text="Flag as fast-moving when the set came out within this many days"), 330).grid(
            row=r, column=0, sticky="w", pady=3)
        self.vars["new_release_days"] = tk.StringVar(value=str(s.get("new_release_days", 30)))
        ttk.Spinbox(f, from_=0, to=365, increment=1, textvariable=self.vars["new_release_days"], width=6).grid(row=r, column=1, sticky="w")
        r += 1

        ttk.Separator(f).grid(row=r, column=0, columnspan=3, sticky="ew", pady=10)
        r += 1
        self._wrap(ttk.Label(f, text="Sealed product types (matched to TCGplayer sealed products):"), 40).grid(
            row=r, column=0, columnspan=3, sticky="w")
        r += 1
        types_frame = Flow(f, gap=12)
        types_frame.grid(row=r, column=0, columnspan=3, sticky="we")
        known = app.checker.product_types() or [(t, None) for t in s["sealed_types"]]
        chosen = {t.lower() for t in s["sealed_types"]}
        self.type_vars = {}
        for i, (t, n) in enumerate(t for t in known if t[0] and t[0].lower() != "singles"):
            v = tk.BooleanVar(value=t.lower() in chosen)
            types_frame.add(ttk.Checkbutton(types_frame, text=f"{t} ({n})" if n else t, variable=v))
            self.type_vars[t] = v
        r += 1
        self.drafts = tk.BooleanVar(value=s.get("include_drafts", False))
        ttk.Checkbutton(f, text="Also check draft (not published) products", variable=self.drafts).grid(row=r, column=0, columnspan=3, sticky="w", pady=(6, 0))
        r += 1
        self.singles = tk.BooleanVar(value=s.get("check_singles", True))
        singles = Flow(f)
        singles.grid(row=r, column=0, columnspan=3, sticky="we")
        singles.add(ttk.Checkbutton(singles, text="Price-check TCG singles too", variable=self.singles))
        singles.add(self._wrap(ttk.Label(singles, text="(MTG, Pokemon, One Piece... by their Shopify tags)"), 40))
        r += 1
        self._wrap(ttk.Label(f, text="Singles: % of TCGplayer's Near Mint price for each condition:"), 40).grid(
            row=r, column=0, columnspan=3, sticky="w", pady=(6, 0))
        r += 1
        cond = Flow(f, gap=14)
        cond.grid(row=r, column=0, columnspan=3, sticky="we")
        self.cond_vars = {}
        for code in CONDITIONS:
            pair = ttk.Frame(cond)
            ttk.Label(pair, text=code, foreground=app.colors["muted"]).pack(side="left", padx=(0, 4))
            v = tk.StringVar(value=f"{float(s['condition_pct'].get(code, 100)):g}")
            ttk.Spinbox(pair, from_=0, to=200, increment=5, textvariable=v, width=5).pack(side="left")
            cond.add(pair)
            self.cond_vars[code] = v
        r += 1

        ttk.Separator(f).grid(row=r, column=0, columnspan=3, sticky="ew", pady=10)
        r += 1
        for key, label in [("ignore_words", "Skip products whose title contains (comma separated):"),
                           ("self_priced_words", "We price it ourselves when the title contains:")]:
            self._wrap(ttk.Label(f, text=label), 40).grid(row=r, column=0, columnspan=3, sticky="w")
            v = tk.StringVar(value=", ".join(w.strip() for w in s[key]))
            ttk.Entry(f, textvariable=v, width=30).grid(row=r + 1, column=0, columnspan=3, sticky="we", pady=(0, 6))
            self.vars[key] = v
            r += 2

        self._wrap(ttk.Label(f, text="Stock columns, in order: one per line, as  Column name: Shopify location, "
                                     "other location"), 40).grid(row=r, column=0, columnspan=3, sticky="w")
        r += 1
        self.stock_text = tk.Text(f, width=30, height=5, wrap="word", font=app.fonts["body"])
        self.stock_text.insert("1.0", "\n".join(f"{c['name']}: {', '.join(c.get('locations', []))}"
                                                 for c in s.get("stock_columns") or []))
        self.stock_text.grid(row=r, column=0, columnspan=3, sticky="we")
        r += 1
        seen = app.checker.shopify_locations()
        self._wrap(ttk.Label(f, text=("Shopify locations: " + ", ".join(seen) if seen else
                                      "Shopify locations show here after a check")
                             + ". Locations in no column aren't counted. Leave empty for one column per location.",
                             foreground=app.colors["faint"], justify="left"), 40).grid(
            row=r, column=0, columnspan=3, sticky="w", pady=(2, 6))
        r += 1

        ttk.Label(f, text="Shopify login file (config.yaml):").grid(row=r, column=0, columnspan=3, sticky="w")
        self.cred = tk.StringVar(value=str(app.checker.credentials_path or "(not chosen yet)"))
        ttk.Entry(f, textvariable=self.cred, width=30, state="readonly").grid(row=r + 1, column=0, columnspan=2, sticky="we")
        ttk.Button(f, text="Choose file…", command=self.browse).grid(row=r + 1, column=2, padx=6)
        r += 2
        self.bind("<Escape>", lambda e: self.destroy())
        fit_window(self, 900, 1000, 0.8, 0.85)

    def _wrap(self, label, reserve, fixed=None):
        """Let a label's text wrap: to (window width - reserve), or to a fixed width."""
        if fixed:
            label.configure(wraplength=fixed)
        else:
            self.wraps.append((label, reserve))
        return label

    def _rewrap(self, _event=None):
        # follow the visible width (not the contents' width), so long text never forces sideways scrolling
        visible = self.area.canvas.winfo_width()
        changed = False
        for label, reserve in self.wraps:
            width = max(160, visible - reserve)
            if pixels(label.cget("wraplength")) != width:
                label.configure(wraplength=width)
                changed = True
        if changed:
            self.area.refit()

    def browse(self):
        path = self.app.choose_login_file(self)
        if path:
            self.cred.set(path)

    def save(self):
        s = self.app.checker.config["settings"]
        try:
            for key in ("drift_pct", "urgent_pct", "presale_drift_pct", "presale_urgent_pct"):
                s[key] = float(self.vars[key].get())
            s["new_release_days"] = int(float(self.vars["new_release_days"].get()))
            s["condition_pct"] = {code: float(v.get()) for code, v in self.cond_vars.items()}
        except ValueError:
            messagebox.showerror("Settings", "The percentages must be numbers.", parent=self)
            return
        columns = []
        for line in self.stock_text.get("1.0", "end").splitlines():
            if not line.strip():
                continue
            name, _, locs = line.partition(":")
            locs = [l.strip() for l in locs.split(",") if l.strip()] if _ else [name.strip()]
            if not name.strip() or any(c["name"] == name.strip() for c in columns):
                messagebox.showerror("Settings", f"Stock columns: each line needs its own name ('{line.strip()}').",
                                     parent=self)
                return
            columns.append({"name": name.strip(), "locations": locs})
        s["stock_columns"] = columns
        s["check_singles"] = self.singles.get()
        if self.text_size.get() in TEXT_SIZES:
            s["text_size"] = TEXT_SIZES[self.text_size.get()]
        s["tcg_price_field"] = self.field.get()
        s["sealed_types"] = [t for t, v in self.type_vars.items() if v.get()]
        s["include_drafts"] = self.drafts.get()
        for key in ("ignore_words", "self_priced_words"):
            s[key] = [w.strip() for w in self.vars[key].get().split(",") if w.strip()]
        if not check_credentials_file(self.cred.get()):
            self.app.checker.credentials_path = Path(self.cred.get())
            s["credentials_file"] = self.cred.get()
        self.app.checker.save()
        self.app.apply_text_size()
        self.app.checker.regroup_stock()
        self.app.refresh_table()
        self.destroy()
        if self.app.checker.shopify_items:
            self.app.run_in_background("Applying settings…", lambda progress: self.app.checker.recompute(progress),
                                       lambda _: self.app.refresh_table())


# ================================================================ entry point

def cli_main(argv):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cli", action="store_true", help="run once in the console instead of opening the window")
    ap.add_argument("--config", default=HERE / "products.json", type=Path)
    ap.add_argument("--credentials", type=Path,
                    help="config.yaml with a shopify: section (default: the file chosen in the window)")
    ap.add_argument("--shopify-csv", type=Path, help="Shopify product export instead of the API")
    ap.add_argument("--out", default=HERE / "reports", type=Path)
    ap.add_argument("--show-zero", action="store_true", help="include out-of-stock listings in the reports")
    ap.add_argument("--no-singles", action="store_true", help="don't price-check singles this run")
    ap.add_argument("--cached-shopify", action="store_true",
                    help="reuse the Shopify products saved by the last check instead of reading Shopify")
    ap.add_argument("--no-bulk", action="store_true", help="page through Shopify instead of one bulk operation")
    ap.add_argument("--timings", action="store_true", help="print how long each step took")
    args = ap.parse_args(argv)
    if sys.stdout:  # (a packaged windowed app has no console)
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows consoles default to cp1252

    checker = PriceChecker(args.config, args.credentials, args.out)
    kept = {k: checker.config["settings"][k] for k in ("check_singles", "shopify_bulk")}
    if args.no_singles:
        checker.config["settings"]["check_singles"] = False
    if args.no_bulk:
        checker.config["settings"]["shopify_bulk"] = False
    started = time.perf_counter()
    try:
        rows = checker.run(progress=print, shopify_csv=args.shopify_csv, use_cache=args.cached_shopify)
    except SetupError as e:
        sys.exit(str(e))
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, ssl.SSLError) as e:
        sys.exit(friendly_error(e, checker.stage))  # network / HTTP trouble: plain words, not a traceback
    checker.config["settings"].update(kept)  # one-off flags aren't saved in products.json
    checker.save()
    clock = time.perf_counter()
    html_path, csv_path = checker.write_reports(show_zero=args.show_zero or None)
    checker.timings["reports"] = time.perf_counter() - clock
    shown = rows if args.show_zero or checker.config["settings"].get("show_zero_stock") else [r for r in rows if has_stock(r)]
    counts = {t: sum(r["tag"] == t for r in shown) for t in TAG_ORDER}
    print("\n" + " · ".join(f"{t.title()}: {n}" for t, n in counts.items() if n))
    if len(shown) < len(rows):
        print(f"{len(rows) - len(shown)} out-of-stock listings left out of the reports (--show-zero includes them)")
    for note in checker.notes:
        print(f"Note: {note}")
    print(f"Report: {html_path}\nCSV:    {csv_path}\nNeed update: {html_path.with_name(html_path.name.replace('price_report_', 'need_update_')).with_suffix('.csv')}")
    if args.timings:
        checker.timings["total"] = time.perf_counter() - started
        print("Timings: " + " · ".join(f"{k} {v:.2f}s" if isinstance(v, float) else f"{k} {v}"
                                       for k, v in checker.timings.items()))


# ================================================================ app icon (drawn in code: no image files needed)

def icon_png(size=64):
    """PNG bytes of the app icon: two trading cards, the front one with a little price chart."""
    import struct
    import zlib

    def rounded(x, y, x0, y0, x1, y1, r):
        cx = min(max(x, x0 + r), x1 - r)
        cy = min(max(y, y0 + r), y1 - r)
        return x0 <= x <= x1 and y0 <= y <= y1 and (x - cx) ** 2 + (y - cy) ** 2 <= r * r

    rows = []
    for py_ in range(size):
        row = bytearray([0])  # PNG filter byte
        for px in range(size):
            x, y = px / size, py_ / size
            color = (0, 0, 0, 0)
            if rounded(x, y, 0.10, 0.06, 0.62, 0.78, 0.07):                       # back card
                color = (245, 158, 11, 255)
            if rounded(x, y, 0.32, 0.20, 0.90, 0.95, 0.07):                       # front card
                color = (30, 79, 184, 255)
                if rounded(x, y, 0.37, 0.25, 0.85, 0.90, 0.05):
                    color = (255, 255, 255, 255)
                    for bx, top, fill in ((0.43, 0.62, (220, 38, 38)), (0.56, 0.48, (245, 158, 11)),
                                          (0.69, 0.36, (22, 163, 74))):           # chart bars
                        if bx <= x <= bx + 0.09 and top <= y <= 0.82:
                            color = (*fill, 255)
            row += bytes(color)
        rows.append(bytes(row))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b""))


def write_icon(path):
    """Save the icon for building the app: .ico (Windows) or .png (Mac, PyInstaller converts it)."""
    import struct
    path = Path(path)
    if path.suffix.lower() != ".ico":
        path.write_bytes(icon_png(512))
        return
    images = [(s, icon_png(s)) for s in (16, 32, 48, 256)]
    offset = 6 + 16 * len(images)
    header = struct.pack("<HHH", 0, 1, len(images))
    for s, png in images:
        header += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(png), offset)
        offset += len(png)
    path.write_bytes(header + b"".join(png for _, png in images))


def build_app():
    """Package this script as a stand-alone app for the computer it's run on (Python not needed afterwards).

    Windows -> "TCG Price Checker.exe", Mac -> "TCG Price Checker.app", both placed next to this script.
    Uses a throwaway build folder with PyInstaller, so nothing is installed into your own Python.
    """
    import shutil
    import subprocess
    import tempfile
    import venv

    name = "TCG Price Checker"
    script = Path(__file__).resolve()
    work = Path(tempfile.mkdtemp(prefix="tcg_build_"))
    print(f"Building {name} in {work} ... (takes a few minutes the first time)")
    try:
        venv.create(work / "venv", with_pip=True)
        py = work / "venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        subprocess.run([str(py), "-m", "pip", "install", "--quiet", "pyinstaller", "pillow"], check=True)
        icon = work / ("icon.ico" if sys.platform == "win32" else "icon.png")
        write_icon(icon)
        args = [str(py), "-m", "PyInstaller", "--noconfirm", "--windowed", "--name", name, "--icon", str(icon),
                "--distpath", str(work / "dist"), "--workpath", str(work / "build"), "--specpath", str(work)]
        if sys.platform == "darwin":
            args += ["--osx-bundle-identifier", "com.tcgpricechecker.app"]
        else:
            args += ["--onefile"]
        subprocess.run(args + [str(script)], check=True)
        built = work / "dist" / (f"{name}.app" if sys.platform == "darwin" else f"{name}.exe")
        target = script.parent / built.name
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
        shutil.move(str(built), str(target))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print(f"\nDone: {target}\nMove it anywhere. Its saved choices, cache and reports stay in the folder next to it.")


def use_sharp_text():
    """Tell Windows this app handles high-resolution screens itself; otherwise Windows enlarges and blurs it."""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass


def hide_own_console():
    """On Windows, double-clicking a .py file opens a black console window next to ours: hide it.
    Leaves the console alone when it belongs to a terminal someone typed the command into."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        if kernel32.GetConsoleProcessList((ctypes.c_uint * 2)(), 2) == 1:  # only us in this console
            ctypes.windll.user32.ShowWindow(kernel32.GetConsoleWindow(), 0)
    except Exception:
        pass


if __name__ == "__main__":
    if sys.argv[1:2] == ["--write-icon"]:  # used when building the app
        write_icon(sys.argv[2])
    elif "--build-app" in sys.argv[1:]:
        build_app()
    elif "--cli" in sys.argv[1:]:
        cli_main(sys.argv[1:])
    else:
        hide_own_console()
        use_sharp_text()
        App().mainloop()
