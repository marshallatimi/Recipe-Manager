from flask import Flask, request, jsonify, send_from_directory, g, Response
from recipe_scrapers import scrape_me
import sqlite3
import json
import re
import os
import sys
import subprocess
import csv
import io
import base64
import mimetypes
import uuid
import threading
import tempfile
import time
import urllib.request
import ssl

# App version – overwritten by CI at build time via _version.py
try:
    from _version import __version__ as APP_VERSION
except ImportError:
    APP_VERSION = "dev"

GITHUB_REPO = "marshallatimi/Macleay-Recipe-Manager"

# ── Path setup (works both in development and as a PyInstaller .exe) ──────────
# BASE_DIR  = where the bundled files live (read-only when frozen)
# DATA_DIR  = where we write user data (db, uploads) – always writable
if getattr(sys, "frozen", False):
    BASE_DIR = sys._MEIPASS                          # type: ignore[attr-defined]
    # Respect the data directory set by launcher.py; fall back to exe dir
    DATA_DIR = os.environ.get("RECIPE_DATA_DIR") or os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    DATA_DIR = os.environ.get("RECIPE_DATA_DIR") or BASE_DIR

COOKBOOKS_DIR        = os.path.join(DATA_DIR, "cookbooks")
DEFAULT_COOKBOOK_NAME = "My Cookbook"
DB_PATH              = os.path.join(COOKBOOKS_DIR, DEFAULT_COOKBOOK_NAME + ".cookbook")
UPLOADS_DIR          = os.path.join(DATA_DIR, "static", "uploads")

_active_db    = {"path": DB_PATH}
SETTINGS_PATH          = os.path.join(DATA_DIR, "settings.json")
SHOPPING_SETTINGS_PATH = os.path.join(DATA_DIR, "shopping_settings.json")

def active_db_path():
    return _active_db["path"]

# ── In-memory settings cache (invalidated on every write) ─────────────────────
_settings_cache: dict = {"data": None}

def load_settings():
    if _settings_cache["data"] is not None:
        return dict(_settings_cache["data"])
    try:
        with open(SETTINGS_PATH) as f:
            _settings_cache["data"] = json.load(f)
            return dict(_settings_cache["data"])
    except Exception:
        return {}

def save_settings_to_file(data):
    _settings_cache["data"] = dict(data)
    with open(SETTINGS_PATH, "w") as f:
        json.dump(data, f, indent=2)

def add_recent_file(path):
    s = load_settings()
    recent = s.get("recentFiles", [])
    path = os.path.normpath(path)
    if path in recent:
        recent.remove(path)
    recent.insert(0, path)
    s["recentFiles"] = recent[:10]
    save_settings_to_file(s)


_cookbooks_cache: dict = {"data": None, "ts": 0.0}
_COOKBOOKS_CACHE_TTL = 8  # seconds

def _invalidate_cookbooks_cache():
    _cookbooks_cache["data"] = None
    _cookbooks_cache["ts"] = 0.0

def get_cookbooks_list():
    """Return all .cookbook files in COOKBOOKS_DIR plus any linked external ones.
    Result is cached for _COOKBOOKS_CACHE_TTL seconds to avoid opening every file
    on every request (especially costly for external cookbooks on network drives)."""
    now = time.monotonic()
    if _cookbooks_cache["data"] is not None and (now - _cookbooks_cache["ts"]) < _COOKBOOKS_CACHE_TTL:
        # Update isActive flag in-place (active cookbook can change without full rebuild)
        active = os.path.normpath(_active_db["path"])
        for b in _cookbooks_cache["data"]:
            b["isActive"] = os.path.normpath(b["path"]) == active
        return list(_cookbooks_cache["data"])

    os.makedirs(COOKBOOKS_DIR, exist_ok=True)
    seen_paths = set()
    books = []

    def _add_book(path, linked=False):
        norm = os.path.normpath(path)
        if norm in seen_paths:
            return
        seen_paths.add(norm)
        if not os.path.exists(path):
            return  # Don't create empty SQLite files by connecting to missing paths
        name = os.path.splitext(os.path.basename(path))[0]
        try:
            c = sqlite3.connect(path)
            count = c.execute("SELECT COUNT(*) FROM recipes").fetchone()[0]
            c.close()
        except Exception:
            count = 0
        books.append({
            "name":        name,
            "path":        path,
            "isDefault":   False,
            "isActive":    os.path.normpath(path) == os.path.normpath(_active_db["path"]),
            "recipeCount": count,
            "linked":      linked,
        })

    for fname in sorted(os.listdir(COOKBOOKS_DIR)):
        if fname.endswith(".cookbook"):
            _add_book(os.path.join(COOKBOOKS_DIR, fname))

    s = load_settings()
    linked_valid = []
    settings_changed = False
    for lpath in s.get("linkedCookbooks", []):
        if os.path.exists(lpath):
            _add_book(lpath, linked=True)
            linked_valid.append(lpath)
        else:
            settings_changed = True  # stale entry — prune it
    if settings_changed:
        s["linkedCookbooks"] = linked_valid
        save_settings_to_file(s)

    books.sort(key=lambda b: b["name"].lower())
    _cookbooks_cache["data"] = books
    _cookbooks_cache["ts"]   = now
    return list(books)


def startup():
    """
    Called once when the server starts.
    - Ensures cookbooks/ folder exists.
    - Migrates old recipes.db → My Cookbook.cookbook if needed.
    - Restores the last-used cookbook from settings.
    - Initialises the active cookbook's schema.
    """
    os.makedirs(COOKBOOKS_DIR, exist_ok=True)
    default_path = os.path.join(COOKBOOKS_DIR, DEFAULT_COOKBOOK_NAME + ".cookbook")

    # One-time migration: copy old recipes.db into the cookbooks folder
    old_db = os.path.join(DATA_DIR, "recipes.db")
    if os.path.exists(old_db) and not os.path.exists(default_path):
        import shutil
        shutil.copy2(old_db, default_path)

    # Create default cookbook if it still doesn't exist
    if not os.path.exists(default_path):
        _active_db["path"] = default_path
        init_db()

    # Restore last-used cookbook
    s = load_settings()
    last = s.get("activeCookbook")
    if last and os.path.exists(last):
        _active_db["path"] = last
    else:
        _active_db["path"] = default_path

    # Make sure the active cookbook has up-to-date schema
    init_db()

    # Auto-backup: take a daily backup of the active cookbook
    try:
        import datetime, shutil
        backup_dir = os.path.join(DATA_DIR, "backups")
        os.makedirs(backup_dir, exist_ok=True)
        today = datetime.date.today().strftime("%Y-%m-%d")
        cb_name = os.path.splitext(os.path.basename(_active_db["path"]))[0]
        today_backup = os.path.join(backup_dir, f"{cb_name}_{today}.cookbook")
        if not os.path.exists(today_backup):
            shutil.copy2(_active_db["path"], today_backup)
            # Keep last 30 backups
            all_backups = sorted([
                os.path.join(backup_dir, f) for f in os.listdir(backup_dir)
                if f.endswith(".cookbook")
            ], key=os.path.getmtime)
            for old in all_backups[:-30]:
                try: os.remove(old)
                except OSError: pass
    except Exception:
        pass


app = Flask(__name__, static_folder=os.path.join(BASE_DIR, "static"))


# ── Database ──────────────────────────────────────────────────────────────────

def get_db():
    db = getattr(g, "_database", None)
    if db is None:
        db = g._database = sqlite3.connect(active_db_path())
        db.row_factory = sqlite3.Row
        # Use classic rollback journal (not WAL) so cloud-synced drives get a
        # single self-contained file with no .shm/.wal sidecars.
        db.execute("PRAGMA journal_mode=DELETE")
        db.execute("PRAGMA synchronous=FULL")
    return db


@app.teardown_appcontext
def close_db(exception):
    db = getattr(g, "_database", None)
    if db is not None:
        db.close()


def init_db():
    with sqlite3.connect(active_db_path()) as conn:
        # Single-file mode — no WAL sidecars (.shm/.wal) so cloud drives sync cleanly
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS recipes (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                title              TEXT    NOT NULL,
                servings           TEXT,
                servings_num       REAL,
                ingredients        TEXT    DEFAULT '[]',
                instructions       TEXT    DEFAULT '[]',
                ingredient_groups  TEXT    DEFAULT NULL,
                instruction_groups TEXT    DEFAULT NULL,
                image              TEXT,
                total_time         TEXT,
                site_name          TEXT,
                source_url         TEXT,
                category           TEXT    DEFAULT NULL,
                categories         TEXT    DEFAULT NULL,
                notes              TEXT    DEFAULT NULL,
                view_count         INTEGER DEFAULT 0,
                created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at         TIMESTAMP DEFAULT NULL,
                base_recipe        TEXT    DEFAULT NULL,
                scale_by_batch     INTEGER DEFAULT 0
            )
        """)
        for col in ["ingredient_groups", "instruction_groups", "category TEXT DEFAULT NULL",
                    "view_count INTEGER DEFAULT 0", "categories TEXT DEFAULT NULL",
                    "notes TEXT DEFAULT NULL", "updated_at TIMESTAMP DEFAULT NULL",
                    "base_recipe TEXT DEFAULT NULL", "scale_by_batch INTEGER DEFAULT 0"]:
            try:
                conn.execute(f"ALTER TABLE recipes ADD COLUMN {col}")
            except Exception:
                pass
        for col in ["categories TEXT DEFAULT NULL", "default_servings REAL DEFAULT NULL",
                    "notes TEXT DEFAULT NULL"]:
            try:
                conn.execute(f"ALTER TABLE meals ADD COLUMN {col}")
            except Exception:
                pass
        conn.execute("""
            CREATE TABLE IF NOT EXISTS meals (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                name             TEXT NOT NULL,
                category         TEXT DEFAULT NULL,
                categories       TEXT DEFAULT NULL,
                default_servings REAL DEFAULT NULL,
                created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        try:
            conn.execute("ALTER TABLE meals ADD COLUMN category TEXT DEFAULT NULL")
        except Exception:
            pass
        conn.execute("""
            CREATE TABLE IF NOT EXISTS meal_recipes (
                meal_id    INTEGER NOT NULL,
                recipe_id  INTEGER NOT NULL,
                sort_order INTEGER DEFAULT 0,
                servings   REAL DEFAULT NULL,
                PRIMARY KEY (meal_id, recipe_id)
            )
        """)
        try:
            conn.execute("ALTER TABLE meal_recipes ADD COLUMN servings REAL DEFAULT NULL")
        except Exception:
            pass
        # Indexes for faster list queries
        for idx_sql in [
            "CREATE INDEX IF NOT EXISTS idx_recipes_created ON recipes(created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_recipes_title ON recipes(title COLLATE NOCASE)",
            "CREATE INDEX IF NOT EXISTS idx_recipes_updated ON recipes(updated_at DESC)",
        ]:
            try:
                conn.execute(idx_sql)
            except Exception:
                pass
        conn.execute("""
            CREATE TABLE IF NOT EXISTS group_meals (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                name             TEXT NOT NULL,
                default_servings REAL DEFAULT NULL,
                created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        for col in ["default_servings REAL DEFAULT NULL"]:
            try: conn.execute(f"ALTER TABLE group_meals ADD COLUMN {col}")
            except Exception: pass
        # group_meal_members – must allow the same meal multiple times in one group.
        # Older DBs had PRIMARY KEY (group_id, meal_id); migrate to autoincrement row_id.
        has_gmm = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='group_meal_members'"
        ).fetchone()[0] > 0

        if has_gmm:
            # Table exists – check whether it already has the row_id column
            has_row_id = False
            try:
                conn.execute("SELECT row_id FROM group_meal_members LIMIT 1")
                has_row_id = True
            except Exception:
                pass

            if not has_row_id:
                # Old composite-PK schema: rename → create new → copy → drop old
                conn.execute("ALTER TABLE group_meal_members RENAME TO group_meal_members_old")
                conn.execute("""
                    CREATE TABLE group_meal_members (
                        row_id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        group_id        INTEGER NOT NULL,
                        meal_id         INTEGER NOT NULL,
                        servings        REAL    DEFAULT NULL,
                        sort_order      INTEGER DEFAULT 0,
                        recipe_servings TEXT    DEFAULT NULL
                    )
                """)
                try:
                    conn.execute("""
                        INSERT INTO group_meal_members
                               (group_id, meal_id, servings, sort_order, recipe_servings)
                        SELECT  group_id, meal_id, servings, sort_order, recipe_servings
                        FROM    group_meal_members_old
                    """)
                except Exception:
                    try:
                        conn.execute("""
                            INSERT INTO group_meal_members (group_id, meal_id)
                            SELECT group_id, meal_id FROM group_meal_members_old
                        """)
                    except Exception:
                        pass
                conn.execute("DROP TABLE group_meal_members_old")
            else:
                # Already migrated – just add any missing optional columns
                for col in ["servings REAL DEFAULT NULL", "recipe_servings TEXT DEFAULT NULL"]:
                    try:
                        conn.execute(f"ALTER TABLE group_meal_members ADD COLUMN {col}")
                    except Exception:
                        pass
        else:
            # Brand-new installation – create with row_id from the start
            conn.execute("""
                CREATE TABLE group_meal_members (
                    row_id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id        INTEGER NOT NULL,
                    meal_id         INTEGER NOT NULL,
                    servings        REAL    DEFAULT NULL,
                    sort_order      INTEGER DEFAULT 0,
                    recipe_servings TEXT    DEFAULT NULL
                )
            """)

        conn.commit()


def _parse_categories(d):
    """Return a clean list of up to 5 categories from a row dict."""
    if d.get("categories"):
        try:
            cats = json.loads(d["categories"])
            if isinstance(cats, list):
                return [str(c).strip() for c in cats if str(c).strip()][:5]
        except Exception:
            pass
    # Fall back to single category field
    return [d["category"]] if d.get("category") else []


def _categories_payload(data):
    """Extract categories list from request data, keeping category in sync."""
    cats = data.get("categories")
    if isinstance(cats, list):
        cats = [str(c).strip() for c in cats if str(c).strip()][:5]
    elif isinstance(cats, str) and cats:
        cats = [c.strip() for c in cats.split(",") if c.strip()][:5]
    else:
        # Fall back to single category field
        single = (data.get("category") or "").strip()
        cats = [single] if single else []
    category = cats[0] if cats else None
    return cats, category


def row_to_dict(row):
    d = dict(row)
    flat_ings = json.loads(d.get("ingredients") or "[]")
    flat_steps = json.loads(d.get("instructions") or "[]")
    d["ingredients"] = flat_ings
    d["instructions"] = flat_steps
    d["ingredient_groups"] = (
        json.loads(d["ingredient_groups"])
        if d.get("ingredient_groups")
        else [{"purpose": None, "ingredients": flat_ings}]
    )
    d["instruction_groups"] = (
        json.loads(d["instruction_groups"])
        if d.get("instruction_groups")
        else [{"purpose": None, "steps": flat_steps}]
    )
    cats = _parse_categories(d)
    d["categories"] = cats
    d["category"]   = cats[0] if cats else None
    return d


def flatten_groups(groups, key):
    return [item for g in (groups or []) for item in g.get(key, [])]


# Checkbox/square Unicode characters that some recipe sites prepend to ingredients
_CHECKBOX_CHARS = re.compile(r'^[\u25A1\u25A2\u25FB\u25FC\u2610\u2611\u2612\u2713\u2714\s]+')

def clean_ingredient(text):
    """Strip leading checkbox symbols and whitespace from ingredient strings."""
    return _CHECKBOX_CHARS.sub('', text).strip() if text else text


# ── Scraper helpers ───────────────────────────────────────────────────────────

def safe_call(fn):
    try:
        result = fn()
        return result if result else None
    except Exception:
        return None


def safe_list_call(fn):
    try:
        result = fn()
        return result if isinstance(result, list) else []
    except Exception:
        return []


def get_ingredient_groups(scraper):
    try:
        groups = scraper.ingredient_groups()
        if groups:
            result = [{"purpose": g.purpose, "ingredients": [clean_ingredient(i) for i in g.ingredients]} for g in groups]
            if any(g["purpose"] for g in result) or len(result) > 1:
                return result
    except Exception:
        pass
    return [{"purpose": None, "ingredients": [clean_ingredient(i) for i in safe_list_call(scraper.ingredients)]}]


def get_instruction_groups(scraper):
    steps = []
    try:
        steps = scraper.instructions_list()
        if not isinstance(steps, list):
            steps = []
    except Exception:
        pass
    if not steps:
        try:
            text = scraper.instructions()
            if text:
                steps = [s.strip() for s in text.split("\n") if s.strip()]
        except Exception:
            pass
    return parse_instruction_groups(steps)


def parse_instruction_groups(steps):
    groups, current_purpose, current_steps = [], None, []
    for step in steps:
        clean = step.strip()
        if not clean:
            continue
        if is_section_header(clean):
            if current_steps or current_purpose is not None:
                groups.append({"purpose": current_purpose, "steps": current_steps})
            current_purpose = clean.rstrip(":").strip()
            current_steps = []
        else:
            current_steps.append(clean)
    groups.append({"purpose": current_purpose, "steps": current_steps})
    return groups


def is_section_header(text):
    if len(text) > 80:
        return False
    if text.endswith(":") and "." not in text and "!" not in text and "?" not in text:
        return True
    return False


def parse_servings_num(s):
    if not s:
        return None
    m = re.search(r"\d+(?:\.\d+)?", str(s))
    return float(m.group()) if m else None


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


def _parse_iso_duration(val):
    """Convert ISO 8601 duration like PT1H30M to human string."""
    m = re.match(r'^PT(?:(\d+)H)?(?:(\d+)M)?', str(val))
    if not m:
        return None
    parts = []
    if m.group(1): parts.append(f"{m.group(1)} hr")
    if m.group(2): parts.append(f"{m.group(2)} min")
    return ' '.join(parts) or None


def _fetch_url_html(url):
    """Fetch a URL and return the HTML as a string, or None on failure."""
    try:
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(req, context=ctx, timeout=12) as resp:
            return resp.read().decode('utf-8', errors='replace')
    except Exception:
        return None


def _extract_jsonld_recipe(html, url):
    """Extract a recipe from JSON-LD schema.org/Recipe data in pre-fetched HTML."""
    # Find all JSON-LD blocks
    for raw in re.finditer(
            r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html, re.DOTALL | re.IGNORECASE):
        try:
            data = json.loads(raw.group(1))
        except Exception:
            continue
        # data may be a dict or a list; also handle @graph
        items = []
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = data.get('@graph', [data])
        for item in items:
            if not isinstance(item, dict):
                continue
            t = item.get('@type', '')
            types = t if isinstance(t, list) else [t]
            if not any('recipe' in str(x).lower() for x in types):
                continue
            # Parse ingredients
            raw_ings = item.get('recipeIngredient', [])
            if not isinstance(raw_ings, list):
                raw_ings = []
            ingredients = [clean_ingredient(str(i)) for i in raw_ings if i]
            ingredient_groups = [{"purpose": None, "ingredients": ingredients}]
            # Parse instructions
            raw_steps_val = item.get('recipeInstructions', [])
            steps = []
            if isinstance(raw_steps_val, str):
                steps = [s.strip() for s in raw_steps_val.split('\n') if s.strip()]
            elif isinstance(raw_steps_val, list):
                for s in raw_steps_val:
                    if isinstance(s, str):
                        steps.append(s.strip())
                    elif isinstance(s, dict):
                        if s.get('@type') == 'HowToSection':
                            for sub in s.get('itemListElement', []):
                                text = sub.get('text', '') if isinstance(sub, dict) else str(sub)
                                if text.strip(): steps.append(text.strip())
                        else:
                            text = s.get('text', '') or s.get('name', '')
                            if text.strip(): steps.append(text.strip())
            instruction_groups = parse_instruction_groups(steps)
            # Servings
            srv = item.get('recipeYield', '') or ''
            if isinstance(srv, list): srv = srv[0] if srv else ''
            servings = str(srv).strip()
            # Time
            total_time = None
            for tf in ('totalTime', 'cookTime', 'prepTime'):
                tv = item.get(tf)
                if tv:
                    total_time = _parse_iso_duration(tv)
                    if total_time: break
            # Image
            img_field = item.get('image')
            image = None
            if isinstance(img_field, str): image = img_field
            elif isinstance(img_field, list) and img_field:
                first = img_field[0]
                image = first.get('url', '') if isinstance(first, dict) else str(first)
            elif isinstance(img_field, dict): image = img_field.get('url', '')
            # Site name
            try:
                from urllib.parse import urlparse as _up
                domain = _up(url).netloc.replace('www.', '')
            except Exception:
                domain = ''
            title = str(item.get('name', '') or '').strip()
            if not title and not ingredients:
                continue
            return {
                "title": title,
                "servings": servings,
                "servings_num": parse_servings_num(servings),
                "ingredients": ingredients,
                "instructions": flatten_groups(instruction_groups, "steps"),
                "ingredient_groups": ingredient_groups,
                "instruction_groups": instruction_groups,
                "image": image,
                "total_time": total_time,
                "site_name": domain,
                "source_url": url,
            }
    return None


def _scrape_jsonld_fallback(url):
    """Fetch a URL and extract a recipe from JSON-LD schema.org/Recipe data."""
    html = _fetch_url_html(url)
    if not html:
        return None
    return _extract_jsonld_recipe(html, url)


def _extract_html_generic(html, url):
    """Last-resort: extract recipe-like content from raw HTML using heuristics.
    Looks for ingredients in <ul>/<ol> lists and instructions in <ol> lists,
    guided by class/id names containing 'ingredient', 'instruction', 'direction', 'step'.
    Collects ALL matching sections (not just the first) to handle multi-section recipes."""

    def _strip_tags(s):
        return re.sub(r'<[^>]+>', ' ', s).strip()

    def _decode_entities(s):
        s = re.sub(r'&amp;', '&', s)
        s = re.sub(r'&lt;', '<', s)
        s = re.sub(r'&gt;', '>', s)
        s = re.sub(r'&nbsp;', ' ', s)
        s = re.sub(r'&#(\d+);', lambda m: chr(int(m.group(1))), s)
        return s

    # Remove script/style/nav/footer noise
    html_clean = re.sub(
        r'<(script|style|nav|footer|header|aside|noscript)[^>]*>.*?</\1>',
        '', html, flags=re.DOTALL | re.IGNORECASE)

    def _text(s):
        return re.sub(r'\s+', ' ', _decode_entities(_strip_tags(s))).strip()

    def _li_items(block):
        items = re.findall(r'<li[^>]*>(.*?)</li>', block, re.DOTALL | re.IGNORECASE)
        return [_text(i) for i in items if _text(i)]

    def _p_items(block):
        items = re.findall(r'<p[^>]*>(.*?)</p>', block, re.DOTALL | re.IGNORECASE)
        return [_text(i) for i in items if _text(i)]

    # Title: first <h1>
    title = None
    h1 = re.search(r'<h1[^>]*>(.*?)</h1>', html_clean, re.DOTALL | re.IGNORECASE)
    if h1:
        title = _text(h1.group(1))
    if not title:
        t = re.search(r'<title[^>]*>(.*?)</title>', html_clean, re.DOTALL | re.IGNORECASE)
        if t:
            title = _text(t.group(1))

    # Ingredients: collect ALL <ul>/<ol>/<div>/<section> with "ingredient" in class/id
    ingredients = []
    ing_blocks = re.findall(
        r'<(?:ul|ol|div|section)([^>]*(?:class|id)=["\'][^"\']*ingredient[^"\']*["\'][^>]*)>(.*?)</(?:ul|ol|div|section)>',
        html_clean, re.DOTALL | re.IGNORECASE)
    for _, block in ing_blocks:
        items = _li_items(block) or _p_items(block)
        ingredients.extend(items)
    # Remove duplicates while preserving order
    seen = set(); ingredients = [x for x in ingredients if not (x in seen or seen.add(x))]

    # Instructions: collect ALL matching sections
    steps = []
    instr_pattern = r'instruction|direction|step|method|preparation'
    instr_blocks = re.findall(
        r'<(?:ul|ol|div|section)([^>]*(?:class|id)=["\'][^"\']*(?:' + instr_pattern + r')[^"\']*["\'][^>]*)>(.*?)</(?:ul|ol|div|section)>',
        html_clean, re.DOTALL | re.IGNORECASE)
    for _, block in instr_blocks:
        items = _li_items(block) or _p_items(block)
        steps.extend(items)
    seen2 = set(); steps = [x for x in steps if not (x in seen2 or seen2.add(x))]

    # Servings
    servings = None
    srv_match = re.search(
        r'(?:serves?|servings?|yield[s]?|makes?)[^\d<]*(\d+(?:\s*[-\u2013]\s*\d+)?(?:\s+\w+)?)',
        html_clean, re.IGNORECASE)
    if srv_match:
        servings = srv_match.group(1).strip()

    if not title and not ingredients:
        return None

    try:
        from urllib.parse import urlparse as _up
        domain = _up(url).netloc.replace('www.', '')
    except Exception:
        domain = ''

    ig = [{"purpose": None, "ingredients": ingredients}]
    instruction_groups = parse_instruction_groups(steps)
    return {
        "title": title or "",
        "servings": servings or "",
        "servings_num": parse_servings_num(servings or ""),
        "ingredients": ingredients,
        "instructions": flatten_groups(instruction_groups, "steps"),
        "ingredient_groups": ig,
        "instruction_groups": instruction_groups,
        "image": None,
        "total_time": None,
        "site_name": domain,
        "source_url": url,
    }


@app.route("/scrape", methods=["POST"])
def scrape():
    data = request.get_json()
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    # Try the library first
    library_error = None
    try:
        scraper = scrape_me(url)
        servings = safe_call(scraper.yields)
        ingredient_groups = get_ingredient_groups(scraper)
        instruction_groups = get_instruction_groups(scraper)
        recipe = {
            "title": safe_call(scraper.title),
            "servings": servings,
            "servings_num": parse_servings_num(servings),
            "ingredients": flatten_groups(ingredient_groups, "ingredients"),
            "instructions": flatten_groups(instruction_groups, "steps"),
            "ingredient_groups": ingredient_groups,
            "instruction_groups": instruction_groups,
            "image": safe_call(scraper.image),
            "total_time": safe_call(scraper.total_time),
            "site_name": safe_call(scraper.site_name),
            "source_url": url,
        }
        if recipe["title"] or recipe["ingredients"]:
            return jsonify(recipe)
        # Library returned empty — fall through to JSON-LD
    except Exception as e:
        library_error = str(e)

    # Fallback: fetch page HTML once and try JSON-LD then generic HTML parsing
    try:
        raw_html = _fetch_url_html(url)
        if raw_html:
            # 2nd fallback: JSON-LD schema.org/Recipe
            jsonld = _extract_jsonld_recipe(raw_html, url)
            if jsonld and (jsonld.get("title") or jsonld.get("ingredients")):
                return jsonify(jsonld)
            # 3rd fallback: generic HTML heuristics (class/id names, <h1>, <ul>/<ol>)
            generic = _extract_html_generic(raw_html, url)
            if generic and (generic.get("title") or generic.get("ingredients")):
                return jsonify(generic)
    except Exception:
        pass

    msg = library_error or "Could not extract a recipe from this page."
    return jsonify({"error": f"Failed to scrape recipe: {msg}"}), 500


@app.route("/recipes", methods=["GET"])
def list_recipes():
    # Exclude the image column — base64 images can be hundreds of KB each.
    # The full image is fetched individually when a recipe is opened.
    rows = get_db().execute(
        "SELECT id,title,servings,servings_num,"
        "ingredient_groups,instruction_groups,total_time,site_name,source_url,"
        "category,categories,notes,view_count,created_at,updated_at,base_recipe,scale_by_batch"
        " FROM recipes ORDER BY created_at DESC"
    ).fetchall()
    return jsonify([row_to_dict(r) for r in rows])


def parse_text_recipe(text):
    """Parse a plain-text recipe file into a recipe dict.
    Recognises labelled sections (Ingredients:, Instructions:, etc.)
    and falls back to heuristics for unlabelled text."""
    text = _normalize_fractions(text)
    lines = [l.rstrip() for l in text.splitlines()]

    # Strip BOM / leading blank lines
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines:
        return None

    title = lines[0].strip()

    # Keyword section headers
    SECTION = re.compile(
        r'^(?P<key>ingredients?|directions?|instructions?|steps?|method'
        r'|servings?|serves?|category|time|prep|cook|source|url|notes?'
        r'|description)\s*:?\s*$',
        re.IGNORECASE,
    )
    INLINE = re.compile(
        r'^(?P<key>servings?|serves?|category|time|prep|cook|source|url|notes?)\s*:\s*(?P<val>.+)$',
        re.IGNORECASE,
    )

    ingredients, instructions = [], []
    meta = {}
    current = None

    for line in lines[1:]:
        stripped = line.strip()
        m_sec = SECTION.match(stripped)
        m_inl = INLINE.match(stripped)

        if m_sec:
            key = m_sec.group("key").lower().rstrip("s")
            if key in ("ingredient",):
                current = "ing"
            elif key in ("direction", "instruction", "step", "method"):
                current = "ins"
            elif key in ("serving", "serve"):
                current = "serving"
            elif key == "category":
                current = "category"
            else:
                current = None
            continue

        if m_inl:
            key = m_inl.group("key").lower().rstrip("s")
            val = m_inl.group("val").strip()
            if key in ("serving", "serve"):
                meta["servings"] = val
            elif key == "category":
                meta["category"] = val
            elif key in ("time", "prep", "cook"):
                meta["total_time"] = val
            elif key in ("source", "url"):
                meta["source_url"] = val
            current = None
            continue

        if not stripped:
            continue

        if current == "ing":
            ingredients.append(stripped)
        elif current == "ins":
            instructions.append(stripped)
        # else: ignore (preamble / notes)

    # Fallback: if no sections found, try a blank-line split heuristic
    if not ingredients and not instructions:
        blocks = []
        block = []
        for line in lines[1:]:
            if line.strip():
                block.append(line.strip())
            elif block:
                blocks.append(block)
                block = []
        if block:
            blocks.append(block)
        # Heuristic: block with many short lines = ingredients
        for b in blocks:
            avg_len = sum(len(l) for l in b) / max(len(b), 1)
            if avg_len < 50 and not ingredients:
                ingredients = b
            else:
                instructions.extend(b)

    if not title:
        return None

    ig = [{"purpose": None, "ingredients": ingredients}]
    sg = [{"purpose": None, "steps": instructions}]
    return {
        "title":              title,
        "servings":           meta.get("servings"),
        "servings_num":       parse_servings_num(meta.get("servings", "")),
        "total_time":         meta.get("total_time"),
        "source_url":         meta.get("source_url"),
        "site_name":          None,
        "category":           meta.get("category"),
        "image":              None,
        "ingredients":        ingredients,
        "instructions":       instructions,
        "ingredient_groups":  ig,
        "instruction_groups": sg,
    }


def _insert_recipes_into_db(db, recipes, merge=False):
    """Bulk-insert (or merge-update) a list of recipe dicts into an open SQLite connection.

    When merge=True, recipes whose title already exists in the DB are updated
    (refreshing content fields and filling any NULL notes) instead of being
    inserted as duplicates.  New recipes are still inserted normally.
    Returns (inserted, updated) counts.
    """
    inserted = updated = 0
    for r in recipes:
        ig = r.get("ingredient_groups")
        sg = r.get("instruction_groups")
        cats = r.get("categories") or ([r["category"]] if r.get("category") else [])
        category = cats[0] if cats else None
        title = r.get("title", "Untitled")

        if merge:
            # Look for an existing recipe with the same title (case-insensitive)
            row = db.execute(
                "SELECT id, notes FROM recipes WHERE LOWER(title)=LOWER(?) LIMIT 1",
                (title,)
            ).fetchone()
            if row:
                existing_id, existing_notes = row["id"], row["notes"]
                # Update content fields; only overwrite notes when currently blank
                db.execute(
                    """UPDATE recipes SET
                       servings=?, servings_num=?, ingredients=?, instructions=?,
                       ingredient_groups=?, instruction_groups=?,
                       notes=COALESCE(NULLIF(?, ''), notes),
                       updated_at=CURRENT_TIMESTAMP
                       WHERE id=?""",
                    (r.get("servings"), r.get("servings_num"),
                     json.dumps(r.get("ingredients", [])),
                     json.dumps(r.get("instructions", [])),
                     json.dumps(ig) if ig else None,
                     json.dumps(sg) if sg else None,
                     r.get("notes") or None,
                     existing_id),
                )
                updated += 1
                continue

        db.execute(
            """INSERT INTO recipes
               (title,servings,servings_num,ingredients,instructions,
                ingredient_groups,instruction_groups,image,total_time,
                site_name,source_url,category,categories,notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (title,
             r.get("servings"), r.get("servings_num"),
             json.dumps(r.get("ingredients", [])),
             json.dumps(r.get("instructions", [])),
             json.dumps(ig) if ig else None,
             json.dumps(sg) if sg else None,
             r.get("image"), r.get("total_time"),
             r.get("site_name"), r.get("source_url"),
             category, json.dumps(cats) if cats else None,
             r.get("notes") or None),
        )
        inserted += 1
    return inserted, updated


@app.route("/recipes/import-peek", methods=["POST"])
def import_recipes_peek():
    """Preview a .cookbook or .csv file — returns type and recipe count, no changes made."""
    import tempfile
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "No file uploaded"}), 400
    ext = os.path.splitext(f.filename or "")[1].lower()
    if ext not in (".cookbook", ".csv"):
        return jsonify({"error": "Unsupported for peek"}), 400
    tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False, dir=DATA_DIR)
    f.save(tmp.name)
    tmp.close()
    try:
        if ext == ".cookbook":
            try:
                conn2 = sqlite3.connect(tmp.name)
                count = conn2.execute("SELECT COUNT(*) FROM recipes").fetchone()[0]
                conn2.close()
            except Exception:
                count = 0
            return jsonify({"type": "cookbook", "recipeCount": count})
        else:
            csv_type, recipes = detect_and_parse_csv(tmp.name)
            display_type = "csv_rm" if csv_type == "rm" else "csv"
            return jsonify({"type": display_type, "recipeCount": len(recipes)})
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass


def _parse_macleay_pdf_page(page_text: str) -> str:
    """Parse one page of text extracted from a Macleay Recipe Manager printed PDF.
    Returns the page reformatted as a .txt-style recipe block that the text importer
    understands (Title / Servings: / Ingredients: / Instructions: sections).
    """
    import re

    # ── 1. Filter browser-injected chrome lines ────────────────────────────────
    raw_lines = [l.strip() for l in page_text.split('\n') if l.strip()]
    lines = []
    for ln in raw_lines:
        if re.match(r'file://', ln, re.I):                          # browser footer URL
            continue
        if re.match(r'^\d{1,2}/\d{1,2}/\d{2,4},?\s+\d{1,2}:\d{2}', ln):  # date/time header
            continue
        if re.match(r'^(?:page\s+)?\d+\s*/\s*\d+$', ln, re.I):    # page number "1/1"
            continue
        if re.match(r'^Per Serving\s*:', ln, re.I):                 # nutrition
            continue
        if re.match(r'^Tot(?:al)?\s+Fat', ln, re.I):
            continue
        if re.search(r'mg Sodium.*mg Cholesterol|mg Cholesterol.*mg Sodium', ln, re.I):
            continue
        lines.append(ln)

    if not lines:
        return ""

    # ── 2. Extract header: title / yield / note / optional source ─────────────
    title = lines[0]
    servings = ""
    note = ""
    i = 1

    while i < len(lines):
        line = lines[i]
        if line.lower().startswith("yield:"):
            servings = line[6:].strip()
            i += 1
        elif line.lower().startswith("note:"):
            note = line[5:].strip()
            i += 1
        elif i == 1 and len(line) < 60 and not re.match(r'^\d', line):
            i += 1  # skip site/source name
        else:
            break

    # ── 3. Split remaining lines: ingredients vs instructions ─────────────────
    # Instructions are prose — long lines or lines with cooking verbs and sentence endings.
    COOKING_VERBS = re.compile(
        r'\b(heat|bake|cook|mix|stir|add|place|combine|preheat|prepare|remove|drain|'
        r'pour|let|allow|cover|serve|spread|cut|slice|chop|melt|whisk|fold|transfer|'
        r'refrigerate|freeze|season|bring|simmer|boil|reduce|strain|blend|process|'
        r'pulse|knead|roll|press|shape|form|coat|dip|layer|arrange|top|sprinkle|'
        r'garnish|drizzle|brush|rub|marinate|rest|cool|chill|grease|uncover|roast|'
        r'fry|saute|sauté|grill|broil|steam|microwave)\b', re.I
    )

    ingredients = []
    instruction_lines = []
    in_instructions = False

    for ln in lines[i:]:
        if not in_instructions:
            is_prose = (
                len(ln) > 60 or
                (len(ln) > 25 and re.search(r'\.\s+[A-Z]', ln)) or
                (ingredients and len(ln) > 30 and COOKING_VERBS.search(ln))
            )
            if is_prose:
                in_instructions = True
                instruction_lines.append(ln)
            else:
                ingredients.append(ln)
        else:
            instruction_lines.append(ln)

    # ── 4. Format output ───────────────────────────────────────────────────────
    out = title + "\n"
    if servings:
        out += f"Servings: {servings}\n"
    if note:
        out += f"Note: {note}\n"
    out += "\nIngredients:\n"
    for ing in ingredients:
        out += ing + "\n"
    out += "\nInstructions:\n"
    if instruction_lines:
        instruction_text = " ".join(instruction_lines)
        steps = re.split(r'(?<=[\.\!\?])\s+(?=[A-Z\d])', instruction_text)
        for step in steps:
            if step.strip():
                out += step.strip() + "\n"
    return out.strip()


@app.route("/recipes/import-pdf", methods=["POST"])
def import_pdf_text():
    """Upload a PDF printed from Macleay Recipe Manager, parse each page as a recipe,
    and return the combined text in .txt import format."""
    import tempfile
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "No file uploaded"}), 400
    if not (f.filename or "").lower().endswith(".pdf"):
        return jsonify({"error": "Not a PDF file"}), 400
    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False, dir=DATA_DIR)
    f.save(tmp.name)
    tmp.close()
    try:
        try:
            import pypdf
            reader = pypdf.PdfReader(tmp.name)
            recipe_blocks = []
            for page in reader.pages:
                t = page.extract_text()
                if t and t.strip():
                    parsed = _parse_macleay_pdf_page(t.strip())
                    if parsed:
                        recipe_blocks.append(parsed)
        except ImportError:
            return jsonify({"error": "pypdf not installed — PDF import not available"}), 500
        except Exception as e:
            return jsonify({"error": f"Could not read PDF: {e}"}), 400
        if not recipe_blocks:
            return jsonify({"error": "No text found in this PDF. It may be a scanned image."}), 400
        # Join multiple recipes with a clear separator that the text importer recognises
        combined = "\n\n---\n\n".join(recipe_blocks)
        return jsonify({"text": combined})
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass


@app.route("/recipes/import-file", methods=["POST"])
def import_recipes_to_current():
    """Upload a .txt / .cookbook / .csv and add its recipes to the active cookbook."""
    import tempfile
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "No file uploaded"}), 400

    ext = os.path.splitext(f.filename or "")[1].lower()
    if ext not in (".txt", ".cookbook", ".csv"):
        return jsonify({"error": "Unsupported file type. Use .txt, .cookbook, or .csv"}), 400

    tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False, dir=DATA_DIR)
    f.save(tmp.name)
    tmp.close()

    try:
        recipes = []
        if ext == ".txt":
            text = open(tmp.name, encoding="utf-8-sig", errors="replace").read()
            r = parse_text_recipe(text)
            if r:
                recipes = [r]
        elif ext == ".cookbook":
            conn2 = sqlite3.connect(tmp.name)
            conn2.row_factory = sqlite3.Row
            rows = conn2.execute("SELECT * FROM recipes").fetchall()
            conn2.close()
            recipes = [row_to_dict(r) for r in rows]
        elif ext == ".csv":
            _csv_type, recipes = detect_and_parse_csv(tmp.name)

        if not recipes:
            return jsonify({"error": "No recipes found in the file."}), 422

        merge = request.form.get("merge") == "1"
        db = get_db()
        inserted, updated = _insert_recipes_into_db(db, recipes, merge=merge)
        db.commit()
        return jsonify({"ok": True, "imported": inserted + updated,
                        "inserted": inserted, "updated": updated}), 201
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass


@app.route("/recipes", methods=["POST"])
def save_recipe():
    data = request.get_json()
    ig = data.get("ingredient_groups")
    sg = data.get("instruction_groups")
    cats, category = _categories_payload(data)
    db = get_db()
    try:
        cur = db.execute(
            """INSERT INTO recipes
               (title, servings, servings_num, ingredients, instructions,
                ingredient_groups, instruction_groups, image, total_time, site_name, source_url, category, categories,
                base_recipe, scale_by_batch)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                data.get("title", "Untitled"),
                data.get("servings"),
                data.get("servings_num"),
                json.dumps(flatten_groups(ig, "ingredients") if ig else data.get("ingredients", [])),
                json.dumps(flatten_groups(sg, "steps") if sg else data.get("instructions", [])),
                json.dumps(ig) if ig else None,
                json.dumps(sg) if sg else None,
                data.get("image"),
                data.get("total_time"),
                data.get("site_name"),
                data.get("source_url"),
                category,
                json.dumps(cats) if cats else None,
                data.get("base_recipe") or None,
                1 if data.get("scale_by_batch") else 0,
            ),
        )
        db.commit()
    except Exception as e:
        return jsonify({"error": f"Database error: {e}"}), 500
    row = db.execute("SELECT * FROM recipes WHERE id=?", (cur.lastrowid,)).fetchone()
    return jsonify(row_to_dict(row)), 201


@app.route("/recipes/<int:rid>", methods=["GET"])
def get_recipe(rid):
    row = get_db().execute("SELECT * FROM recipes WHERE id=?", (rid,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    return jsonify(row_to_dict(row))


@app.route("/recipes/<int:rid>", methods=["PUT"])
def update_recipe(rid):
    data = request.get_json()
    ig = data.get("ingredient_groups")
    sg = data.get("instruction_groups")
    cats, category = _categories_payload(data)
    db = get_db()
    db.execute(
        """UPDATE recipes
           SET title=?, servings=?, servings_num=?, ingredients=?, instructions=?,
               ingredient_groups=?, instruction_groups=?, image=?, total_time=?, site_name=?,
               source_url=?, category=?, categories=?, notes=?, base_recipe=?, scale_by_batch=?,
               updated_at=CURRENT_TIMESTAMP
           WHERE id=?""",
        (
            data.get("title"),
            data.get("servings"),
            data.get("servings_num"),
            json.dumps(flatten_groups(ig, "ingredients") if ig else data.get("ingredients", [])),
            json.dumps(flatten_groups(sg, "steps") if sg else data.get("instructions", [])),
            json.dumps(ig) if ig else None,
            json.dumps(sg) if sg else None,
            data.get("image"),
            data.get("total_time"),
            data.get("site_name"),
            data.get("source_url"),
            category,
            json.dumps(cats) if cats else None,
            data.get("notes") or None,
            data.get("base_recipe") or None,
            1 if data.get("scale_by_batch") else 0,
            rid,
        ),
    )
    db.commit()
    row = db.execute("SELECT * FROM recipes WHERE id=?", (rid,)).fetchone()
    return jsonify(row_to_dict(row))


@app.route("/static/uploads/<path:filename>")
def serve_upload(filename):
    """Serve uploaded images from the writable DATA_DIR (works when frozen)."""
    return send_from_directory(UPLOADS_DIR, filename)


@app.route("/recipes/<int:rid>/image", methods=["POST"])
def update_image(rid):
    db = get_db()
    if "file" in request.files:
        f = request.files["file"]
        if f and f.filename:
            ext = os.path.splitext(f.filename)[1].lower()
            os.makedirs(UPLOADS_DIR, exist_ok=True)
            filename = f"{rid}{ext}"
            f.save(os.path.join(UPLOADS_DIR, filename))
            url = f"/static/uploads/{filename}"
            db.execute("UPDATE recipes SET image=? WHERE id=?", (url, rid))
            db.commit()
            return jsonify({"image": url})
    data = request.get_json(silent=True) or {}
    url = data.get("url", "").strip()
    if url:
        db.execute("UPDATE recipes SET image=? WHERE id=?", (url, rid))
        db.commit()
        return jsonify({"image": url})
    return jsonify({"error": "No image provided"}), 400


@app.route("/recipes/<int:rid>/image", methods=["DELETE"])
def delete_image(rid):
    db = get_db()
    # Also remove the physical file if it's a local upload
    row = db.execute("SELECT image FROM recipes WHERE id=?", (rid,)).fetchone()
    if row and row["image"] and row["image"].startswith("/static/uploads/"):
        try:
            os.remove(os.path.join(UPLOADS_DIR, os.path.basename(row["image"])))
        except OSError:
            pass
    db.execute("UPDATE recipes SET image=NULL WHERE id=?", (rid,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/recipes/<int:rid>/view", methods=["POST"])
def increment_view(rid):
    db = get_db()
    db.execute("UPDATE recipes SET view_count = view_count + 1 WHERE id=?", (rid,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/recipes/<int:rid>", methods=["DELETE"])
def delete_recipe(rid):
    db = get_db()
    db.execute("DELETE FROM recipes WHERE id=?", (rid,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/meals", methods=["GET"])
def list_meals():
    db = get_db()
    meals = db.execute("SELECT * FROM meals ORDER BY created_at DESC").fetchall()
    if not meals:
        return jsonify([])
    # Batch-fetch all meal_recipes in one query instead of N+1
    meal_ids = [m["id"] for m in meals]
    placeholders = ",".join("?" * len(meal_ids))
    all_recipes = db.execute(
        f"""SELECT mr.meal_id, r.id, r.title, r.servings, r.servings_num, r.image,
                   r.scale_by_batch, mr.servings AS recipe_servings
            FROM meal_recipes mr JOIN recipes r ON r.id = mr.recipe_id
            WHERE mr.meal_id IN ({placeholders}) ORDER BY mr.meal_id, mr.sort_order""",
        meal_ids,
    ).fetchall()
    recipes_by_meal: dict = {}
    for r in all_recipes:
        mid = r["meal_id"]
        recipes_by_meal.setdefault(mid, []).append(dict(r))
    result = []
    for m in meals:
        md = dict(m)
        md["categories"] = _parse_categories(md)
        md["category"]   = md["categories"][0] if md["categories"] else None
        result.append({**md, "recipes": recipes_by_meal.get(m["id"], [])})
    return jsonify(result)


@app.route("/meals", methods=["POST"])
def create_meal():
    data = request.get_json()
    db = get_db()
    cur = db.execute("INSERT INTO meals (name) VALUES (?)", (data.get("name", "New Meal"),))
    db.commit()
    meal_id = cur.lastrowid
    return jsonify({"id": meal_id, "name": data.get("name", "New Meal"), "recipes": []}), 201


@app.route("/meals/<int:mid>", methods=["PUT"])
def update_meal(mid):
    data = request.get_json()
    db = get_db()
    cats, category = _categories_payload(data)
    ds = data.get("default_servings")
    db.execute("UPDATE meals SET name=?, category=?, categories=?, default_servings=?, notes=? WHERE id=?",
               (data.get("name"), category, json.dumps(cats) if cats else None, ds,
                data.get("notes") or None, mid))
    db.commit()
    return jsonify({"ok": True})


@app.route("/meals/<int:mid>", methods=["DELETE"])
def delete_meal(mid):
    db = get_db()
    db.execute("DELETE FROM meal_recipes WHERE meal_id=?", (mid,))
    db.execute("DELETE FROM meals WHERE id=?", (mid,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/meals/<int:mid>/copy", methods=["POST"])
def copy_meal(mid):
    db = get_db()
    original = db.execute("SELECT * FROM meals WHERE id=?", (mid,)).fetchone()
    if not original:
        return jsonify({"error": "Meal not found"}), 404
    orig = dict(original)
    cur = db.execute(
        "INSERT INTO meals (name, category, categories, default_servings, notes) VALUES (?,?,?,?,?)",
        (orig["name"] + " (copy)", orig.get("category"), orig.get("categories"),
         orig.get("default_servings"), orig.get("notes"))
    )
    new_id = cur.lastrowid
    # Copy all recipe associations
    recipes = db.execute("SELECT * FROM meal_recipes WHERE meal_id=?", (mid,)).fetchall()
    for r in recipes:
        db.execute(
            "INSERT INTO meal_recipes (meal_id, recipe_id, sort_order, servings) VALUES (?,?,?,?)",
            (new_id, r["recipe_id"], r["sort_order"], r["servings"])
        )
    db.commit()
    # Return the new meal
    meal_row = db.execute("SELECT * FROM meals WHERE id=?", (new_id,)).fetchone()
    meal_dict = dict(meal_row)
    meal_dict["categories"] = _parse_categories(meal_dict)
    meal_dict["recipes"] = [dict(r) for r in db.execute(
        """SELECT r.id, r.title, r.servings, r.servings_num, r.image, r.scale_by_batch,
                  mr.servings AS recipe_servings
           FROM meal_recipes mr JOIN recipes r ON r.id = mr.recipe_id
           WHERE mr.meal_id = ? ORDER BY mr.sort_order""", (new_id,)
    ).fetchall()]
    return jsonify(meal_dict), 201


@app.route("/meals/<int:mid>/recipes", methods=["POST"])
def add_recipe_to_meal(mid):
    data = request.get_json()
    rid = data.get("recipe_id")
    db = get_db()
    try:
        db.execute("INSERT OR IGNORE INTO meal_recipes (meal_id, recipe_id) VALUES (?,?)", (mid, rid))
        db.commit()
    except Exception:
        pass
    return jsonify({"ok": True})


@app.route("/meals/<int:mid>/recipes/<int:rid>", methods=["DELETE"])
def remove_recipe_from_meal(mid, rid):
    db = get_db()
    db.execute("DELETE FROM meal_recipes WHERE meal_id=? AND recipe_id=?", (mid, rid))
    db.commit()
    return jsonify({"ok": True})


@app.route("/meals/<int:mid>/recipes/<int:rid>/servings", methods=["PUT"])
def set_meal_recipe_servings(mid, rid):
    data = request.get_json()
    srv = data.get("servings")
    db = get_db()
    db.execute("UPDATE meal_recipes SET servings=? WHERE meal_id=? AND recipe_id=?", (srv, mid, rid))
    db.commit()
    return jsonify({"ok": True})


@app.route("/group-meals", methods=["GET"])
def list_group_meals():
    db = get_db()
    groups = db.execute("SELECT * FROM group_meals ORDER BY created_at DESC").fetchall()
    if not groups:
        return jsonify([])
    # Batch-fetch all group_meal_members in one query instead of N+1
    group_ids = [g["id"] for g in groups]
    placeholders = ",".join("?" * len(group_ids))
    all_members = db.execute(
        f"""SELECT gm.group_id, gm.row_id AS slot_id, m.id, m.name,
                   gm.servings, gm.recipe_servings
            FROM group_meal_members gm
            JOIN meals m ON m.id = gm.meal_id
            WHERE gm.group_id IN ({placeholders})
            ORDER BY gm.group_id, gm.sort_order, gm.row_id""",
        group_ids,
    ).fetchall()
    members_by_group: dict = {}
    for ml in all_members:
        gid = ml["group_id"]
        md  = dict(ml)
        try:
            md["recipe_servings"] = json.loads(md["recipe_servings"]) if md["recipe_servings"] else {}
        except Exception:
            md["recipe_servings"] = {}
        members_by_group.setdefault(gid, []).append(md)
    result = []
    for g in groups:
        result.append({**dict(g), "meals": members_by_group.get(g["id"], [])})
    return jsonify(result)


@app.route("/group-meals", methods=["POST"])
def create_group_meal():
    data = request.get_json()
    db = get_db()
    cur = db.execute("INSERT INTO group_meals (name) VALUES (?)", (data.get("name", "New Group"),))
    db.commit()
    return jsonify({"id": cur.lastrowid, "name": data.get("name", "New Group"),
                    "default_servings": None, "meals": []}), 201


@app.route("/group-meals/<int:gid>", methods=["PUT"])
def update_group_meal(gid):
    data = request.get_json()
    db = get_db()
    ds = data.get("default_servings")
    ds = float(ds) if ds not in (None, "", "null") else None
    db.execute("UPDATE group_meals SET name=?, default_servings=? WHERE id=?",
               (data.get("name"), ds, gid))
    db.commit()
    return jsonify({"ok": True})


@app.route("/group-meals/<int:gid>", methods=["DELETE"])
def delete_group_meal(gid):
    db = get_db()
    db.execute("DELETE FROM group_meal_members WHERE group_id=?", (gid,))
    db.execute("DELETE FROM group_meals WHERE id=?", (gid,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/group-meals/<int:gid>/meals", methods=["POST"])
def add_meal_to_group(gid):
    data = request.get_json()
    mid = data.get("meal_id")
    db = get_db()
    cur = db.execute(
        "INSERT INTO group_meal_members (group_id, meal_id) VALUES (?,?)", (gid, mid)
    )
    db.commit()
    return jsonify({"ok": True, "slot_id": cur.lastrowid})


@app.route("/group-meals/<int:gid>/slots/<int:slot_id>", methods=["PATCH"])
def patch_group_meal_member(gid, slot_id):
    data = request.get_json()
    db = get_db()
    srv = data.get("servings")
    srv = float(srv) if srv not in (None, "", "null") else None
    rs = data.get("recipe_servings")
    rs_json = json.dumps(rs) if isinstance(rs, dict) else None
    if rs_json is not None:
        db.execute("UPDATE group_meal_members SET servings=?, recipe_servings=? WHERE row_id=? AND group_id=?",
                   (srv, rs_json, slot_id, gid))
    else:
        db.execute("UPDATE group_meal_members SET servings=? WHERE row_id=? AND group_id=?",
                   (srv, slot_id, gid))
    db.commit()
    return jsonify({"ok": True})


@app.route("/group-meals/<int:gid>/slots/<int:slot_id>", methods=["DELETE"])
def remove_meal_from_group(gid, slot_id):
    db = get_db()
    db.execute("DELETE FROM group_meal_members WHERE row_id=? AND group_id=?", (slot_id, gid))
    db.commit()
    return jsonify({"ok": True})


@app.route("/group-meals/<int:gid>/slots/reorder", methods=["PATCH"])
def reorder_group_meal_slots(gid):
    """Accepts {"order": [slot_id1, slot_id2, ...]} and updates sort_order."""
    data = request.get_json()
    order = data.get("order", [])
    db = get_db()
    for i, slot_id in enumerate(order):
        db.execute(
            "UPDATE group_meal_members SET sort_order=? WHERE row_id=? AND group_id=?",
            (i, slot_id, gid)
        )
    db.commit()
    return jsonify({"ok": True})


@app.route("/file/current")
def file_current():
    path = active_db_path()
    name = os.path.splitext(os.path.basename(path))[0]
    s    = load_settings()
    return jsonify({"path": path, "name": name, "recentFiles": s.get("recentFiles", [])})

@app.route("/file/new", methods=["POST"])
def file_new():
    data = request.get_json()
    path = data.get("path")
    if not path:
        return jsonify({"error": "No path provided"}), 400
    _active_db["path"] = path
    init_db()
    add_recent_file(path)
    return jsonify({"ok": True, "path": path})

@app.route("/file/open", methods=["POST"])
def file_open_route():
    data = request.get_json()
    path = data.get("path")
    if not path:
        return jsonify({"error": "No path provided"}), 400
    if not os.path.exists(path):
        return jsonify({"error": "File not found"}), 404
    _active_db["path"] = path
    add_recent_file(path)
    return jsonify({"ok": True, "path": path})

@app.route("/file/save-as", methods=["POST"])
def file_save_as():
    import shutil
    data = request.get_json()
    new_path = data.get("path")
    if not new_path:
        return jsonify({"error": "No path provided"}), 400
    shutil.copy2(active_db_path(), new_path)
    _active_db["path"] = new_path
    add_recent_file(new_path)
    return jsonify({"ok": True, "path": new_path})

@app.route("/settings", methods=["GET"])
def get_settings_route():
    return jsonify(load_settings())

@app.route("/settings", methods=["POST"])
def post_settings_route():
    data = request.get_json()
    s = load_settings()
    s.update(data)
    save_settings_to_file(s)
    return jsonify({"ok": True})


# ── Auto-Update ───────────────────────────────────────────────────────────────

def _version_gt(a: str, b: str) -> bool:
    """Return True if version string a is newer than b."""
    try:
        av = [int(x) for x in a.strip().lstrip("v").split(".")]
        bv = [int(x) for x in b.strip().lstrip("v").split(".")]
        return av > bv
    except Exception:
        return False


@app.route("/api/version")
def api_version():
    return jsonify({"version": APP_VERSION})


def _urlopen_with_ssl_fallback(req, timeout=15):
    """Try urlopen with verified SSL; fall back to unverified if cert chain fails.
    urllib wraps SSLError inside URLError, so we check the reason attribute.
    This handles laptops where the PyInstaller bundle's cert store is incomplete."""
    import urllib.error
    try:
        ctx = ssl.create_default_context()
        return urllib.request.urlopen(req, timeout=timeout, context=ctx)
    except urllib.error.URLError as e:
        # Only retry on SSL certificate errors, not timeouts or network issues
        reason = getattr(e, "reason", e)
        if isinstance(reason, ssl.SSLError) or "certificate" in str(e).lower() or "ssl" in str(e).lower():
            ctx = ssl._create_unverified_context()
            return urllib.request.urlopen(req, timeout=timeout, context=ctx)
        raise


@app.route("/api/check-update")
def api_check_update():
    try:
        url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
        req = urllib.request.Request(url, headers={"User-Agent": f"RecipeManager/{APP_VERSION}"})
        with _urlopen_with_ssl_fallback(req, timeout=15) as resp:
            data = json.loads(resp.read())

        latest_tag = data.get("tag_name", "").lstrip("v")
        current    = APP_VERSION.lstrip("v")
        available  = _version_gt(latest_tag, current)

        # Find the installer asset URL
        installer_url = None
        portable_url  = None
        for asset in data.get("assets", []):
            name = asset.get("name", "")
            if "Setup" in name and name.endswith(".exe"):
                installer_url = asset["browser_download_url"]
            elif name == "RecipeManager.exe":
                portable_url  = asset["browser_download_url"]

        return jsonify({
            "current":          current,
            "latest":           latest_tag,
            "update_available": available,
            "installer_url":    installer_url,
            "portable_url":     portable_url,
            "release_url":      data.get("html_url"),
            "release_notes":    data.get("body", ""),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/download-update")
def api_download_update():
    """
    Stream download progress as SSE (text/event-stream).
    Query param: url = download URL for the installer exe.
    Events: {"pct": 0-100, "bytes": N}  then {"done": true, "path": "/tmp/..."}
    or     {"error": "message"} on failure.
    """
    download_url = request.args.get("url", "")
    if not download_url:
        return jsonify({"error": "No URL provided"}), 400

    def generate():
        try:
            req = urllib.request.Request(
                download_url,
                headers={"User-Agent": f"RecipeManager/{APP_VERSION}"},
            )
            with _urlopen_with_ssl_fallback(req, timeout=120) as resp:
                total     = int(resp.headers.get("Content-Length") or 0)
                tmp_path  = os.path.join(tempfile.gettempdir(), "MacleayRecipeManager-Update.exe")
                downloaded = 0
                with open(tmp_path, "wb") as f:
                    while True:
                        chunk = resp.read(131072)  # 128 KB chunks
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)
                        pct = int(downloaded * 100 / total) if total else 0
                        yield f"data: {json.dumps({'pct': pct, 'bytes': downloaded, 'total': total})}\n\n"

            yield f"data: {json.dumps({'done': True, 'path': tmp_path})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/run-installer", methods=["POST"])
def api_run_installer():
    """Launch the downloaded installer then exit the app.

    We use a small batch-script intermediary so the installer only starts
    *after* this process has fully exited and PyInstaller has finished
    cleaning up its _MEI temp directory.  Launching the installer directly
    from a PyInstaller one-file exe causes a 'Failed to remove temporary
    directory' warning because the installer holds DLL handles open.
    """
    path = (request.get_json() or {}).get("path", "")
    if not path or not os.path.exists(path):
        return jsonify({"error": "Installer file not found"}), 400
    try:
        # Use a VBScript launched via wscript.exe — this has zero window flash,
        # unlike cmd.exe or PowerShell which show briefly even with CREATE_NO_WINDOW.
        # WScript.Shell.Run with window-style 0 runs child processes completely hidden.
        vbs_path = os.path.join(tempfile.gettempdir(), "_recipe_update.vbs")
        # Escape double-quotes in the path for VBScript string concatenation
        safe_path = path.replace('"', '""')
        with open(vbs_path, "w", encoding="utf-8") as f:
            f.write('Set sh = CreateObject("WScript.Shell")\r\n')
            f.write('Set fso = CreateObject("Scripting.FileSystemObject")\r\n')
            # Kill the running app and wait for it to fully exit before touching files.
            # Longer pre-installer delay prevents "Failed to load Python DLL" on slow
            # machines where the old process holds the DLL handle a bit longer.
            f.write('WScript.Sleep 2000\r\n')
            f.write('sh.Run "taskkill /f /im RecipeManager.exe", 0, True\r\n')
            f.write('WScript.Sleep 4000\r\n')
            # Run installer with /VERYSILENT — suppresses ALL UI including the
            # "Launch app now" finish-page checkbox, so the installer never
            # launches the app itself.  bWaitOnReturn=True blocks until fully done.
            # Window style 0 = completely hidden (no flash).
            f.write(f'sh.Run Chr(34) & "{safe_path}" & Chr(34) & " /VERYSILENT /SUPPRESSMSGBOXES", 0, True\r\n')
            # Longer post-installer wait (10 s) lets Windows finish all file I/O,
            # release any remaining DLL handles, and lets the new exe's PyInstaller
            # temp-dir extraction complete before any import is attempted.
            # This fixes "Python DLL load failure" on first launch after update
            # and the "PDF export fails once then works after restart" issue.
            f.write('WScript.Sleep 10000\r\n')
            # Resolve the install path — check %ProgramFiles% first, then
            # %ProgramW6432% (always the 64-bit Program Files even in WoW64/32-bit
            # wscript.exe, which is where Inno Setup actually installs on 64-bit OS).
            f.write('Dim exePath\r\n')
            f.write('exePath = sh.ExpandEnvironmentStrings("%ProgramFiles%") & "\\Macleay Recipe Manager\\RecipeManager.exe"\r\n')
            f.write('If NOT fso.FileExists(exePath) Then\r\n')
            f.write('  exePath = sh.ExpandEnvironmentStrings("%ProgramW6432%") & "\\Macleay Recipe Manager\\RecipeManager.exe"\r\n')
            f.write('End If\r\n')
            f.write('If NOT fso.FileExists(exePath) Then\r\n')
            f.write('  exePath = sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\\Programs\\Macleay Recipe Manager\\RecipeManager.exe"\r\n')
            f.write('End If\r\n')
            # Use Shell.Application.ShellExecute — this opens the exe via the
            # Explorer shell mechanism (same as double-clicking), giving it a clean
            # environment rather than inheriting wscript's elevated/constrained one.
            f.write('If fso.FileExists(exePath) Then\r\n')
            f.write('  Dim shell2\r\n')
            f.write('  Set shell2 = CreateObject("Shell.Application")\r\n')
            f.write('  shell2.ShellExecute exePath, "", "", "open", 1\r\n')
            f.write('End If\r\n')
            f.write(f'fso.DeleteFile "{vbs_path}", True\r\n')

        subprocess.Popen(
            ["wscript", "//nologo", vbs_path],
            creationflags=0x08000000,  # CREATE_NO_WINDOW
            close_fds=True,
        )
        # Exit after the response reaches the browser
        threading.Timer(1.0, lambda: os._exit(0)).start()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Cookbooks ──────────────────────────────────────────────────────────────

@app.route("/cookbooks", methods=["GET"])
def list_cookbooks():
    return jsonify(get_cookbooks_list())


@app.route("/cookbooks", methods=["POST"])
def create_cookbook():
    data   = request.get_json()
    name   = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Name required"}), 400
    # Sanitise filename
    safe = re.sub(r'[\\/:*?"<>|]', "", name).strip()
    if not safe:
        return jsonify({"error": "Invalid name"}), 400
    path = os.path.join(COOKBOOKS_DIR, safe + ".cookbook")
    if os.path.exists(path):
        return jsonify({"error": "A cookbook with that name already exists"}), 409
    _active_db["path"] = path
    init_db()
    # Save as active
    s = load_settings()
    s["activeCookbook"] = path
    save_settings_to_file(s)
    _invalidate_cookbooks_cache()
    return jsonify({"ok": True, "name": safe, "path": path}), 201


@app.route("/cookbooks/switch", methods=["POST"])
def switch_cookbook():
    data = request.get_json()
    path = data.get("path", "").strip()
    if not path or not os.path.exists(path):
        return jsonify({"error": "Cookbook not found"}), 404
    _active_db["path"] = path
    # Run schema migration on the newly-activated cookbook so all columns
    # (including base_recipe, updated_at, etc.) exist before any queries.
    init_db()
    s = load_settings()
    s["activeCookbook"] = path
    save_settings_to_file(s)
    return jsonify({"ok": True})


@app.route("/cookbooks/rename", methods=["POST"])
def rename_cookbook():
    data     = request.get_json()
    old_name = (data.get("oldName") or "").strip()
    new_name = (data.get("newName") or "").strip()
    if not new_name:
        return jsonify({"error": "Name required"}), 400
    safe_new = re.sub(r'[\\/:*?"<>|]', "", new_name).strip()
    if not safe_new:
        return jsonify({"error": "Name contains only invalid characters"}), 400
    old_path = os.path.join(COOKBOOKS_DIR, old_name + ".cookbook")
    new_path = os.path.join(COOKBOOKS_DIR, safe_new + ".cookbook")
    if not os.path.exists(old_path):
        return jsonify({"error": "Original cookbook not found"}), 404
    if os.path.exists(new_path):
        return jsonify({"error": "A cookbook with that name already exists"}), 409
    os.rename(old_path, new_path)
    if os.path.normpath(_active_db["path"]) == os.path.normpath(old_path):
        _active_db["path"] = new_path
        s = load_settings()
        s["activeCookbook"] = new_path
        save_settings_to_file(s)
    _invalidate_cookbooks_cache()
    return jsonify({"ok": True, "newName": safe_new, "newPath": new_path})


@app.route("/cookbooks/delete", methods=["POST"])
def delete_cookbook():
    data = request.get_json()
    name = (data.get("name") or "").strip()
    path = os.path.join(COOKBOOKS_DIR, name + ".cookbook")
    if not os.path.exists(path):
        return jsonify({"error": "Cookbook not found"}), 404
    # Must always keep at least one cookbook
    all_books = [f for f in os.listdir(COOKBOOKS_DIR) if f.endswith(".cookbook")]
    if len(all_books) <= 1:
        return jsonify({"error": "Cannot delete your only cookbook"}), 403
    # If deleting the active one, switch to first other available
    if os.path.normpath(_active_db["path"]) == os.path.normpath(path):
        for other_fname in sorted(all_books):
            other_path = os.path.join(COOKBOOKS_DIR, other_fname)
            if os.path.normpath(other_path) != os.path.normpath(path):
                _active_db["path"] = other_path
                s = load_settings()
                s["activeCookbook"] = other_path
                save_settings_to_file(s)
                break
    # Close any open Flask g-level connection to this file before deleting
    db = getattr(g, "_database", None)
    if db is not None:
        try:
            db.close()
        except Exception:
            pass
        g._database = None
    # Force Python GC so any other lingering sqlite3 handles are released
    import gc
    gc.collect()
    os.remove(path)
    _invalidate_cookbooks_cache()
    return jsonify({"ok": True})


# ── Linked (external) cookbooks ───────────────────────────────────────────────

@app.route("/cookbooks/link", methods=["POST"])
def link_cookbook():
    """Add an external .cookbook file path to the linked list in settings."""
    data  = request.get_json() or {}
    lpath = (data.get("path") or "").strip()
    if not lpath:
        return jsonify({"error": "No path provided"}), 400
    if not os.path.exists(lpath):
        return jsonify({"error": "File not found"}), 404
    # Validate it's a real cookbook
    try:
        c = sqlite3.connect(lpath)
        c.execute("SELECT COUNT(*) FROM recipes")
        c.close()
    except Exception:
        return jsonify({"error": "Not a valid cookbook file"}), 400
    # Run schema migration on it so it has all current columns
    old = _active_db["path"]
    _active_db["path"] = lpath
    try:
        init_db()
    finally:
        _active_db["path"] = old
    s = load_settings()
    linked = s.get("linkedCookbooks", [])
    norm = os.path.normpath(lpath)
    if not any(os.path.normpath(p) == norm for p in linked):
        linked.append(lpath)
        s["linkedCookbooks"] = linked
        save_settings_to_file(s)
    _invalidate_cookbooks_cache()
    return jsonify({"ok": True})


@app.route("/cookbooks/unlink", methods=["POST"])
def unlink_cookbook():
    """Remove an external cookbook path from the linked list."""
    data  = request.get_json() or {}
    lpath = (data.get("path") or "").strip()
    s     = load_settings()
    norm  = os.path.normpath(lpath)
    linked = [p for p in s.get("linkedCookbooks", [])
              if os.path.normpath(p) != norm]
    s["linkedCookbooks"] = linked
    save_settings_to_file(s)
    _invalidate_cookbooks_cache()
    return jsonify({"ok": True})


# ── Shopping settings ─────────────────────────────────────────────────────────

@app.route("/shopping/settings", methods=["GET"])
def get_shopping_settings():
    try:
        with open(SHOPPING_SETTINGS_PATH, encoding="utf-8") as f:
            return jsonify(json.load(f))
    except Exception:
        return jsonify({"ingredient_categories": {}})


@app.route("/shopping/settings", methods=["POST"])
def save_shopping_settings():
    data = request.get_json() or {}
    with open(SHOPPING_SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return jsonify({"ok": True})


@app.route("/shopping/rename-ingredient", methods=["POST"])
def rename_shopping_ingredient():
    """Rename an ingredient key across ALL recipes in the active cookbook.
    Replaces just the name portion, preserving quantity and unit prefix."""
    data = request.get_json() or {}
    old_key = (data.get("old_key") or "").strip().lower()
    new_name = (data.get("new_name") or "").strip()
    if not old_key or not new_name:
        return jsonify({"error": "old_key and new_name required"}), 400

    # Regex to parse qty+unit prefix from an ingredient string
    NP = r'(?:(?:\d+\s+)?\d+\/\d+|\d+(?:\.\d+)?)'
    UL = (r'tsps?|teaspoons?|tbsps?|tbls?|tablespoons?|fl\.?\s*ozs?|cups?|pints?|pts?|'
          r'quarts?|qts?|gallons?|gals?|ozs?|ounces?|lbs?|pounds?|kgs?|kilograms?|'
          r'grams?|g(?=\b)|mls?|l(?=\b)')
    ING_RE = re.compile(rf'^({NP})(?:\s+({UL}))?\s+(.+)$', re.IGNORECASE)

    def _name_key(text):
        """Normalize ingredient name for comparison (mirrors JS ingNameKey)."""
        # Strip qty+unit prefix
        m = ING_RE.match(text.strip())
        name = m.group(3).strip() if m else text.strip()
        key = name.lower()
        key = re.sub(r'\(.*?\)', '', key)
        key = re.sub(r',.*$', '', key)
        key = re.sub(r'^(large|medium|small|extra-large|xl|big)\s+', '', key)
        key = re.sub(r'\s+', ' ', key).strip()
        key = re.sub(r'ies$', 'y', key)
        key = re.sub(r'([^s])s$', r'\1', key)
        return key

    def _rename_ing(text, new_name):
        """Replace the name part of an ingredient string, keeping qty+unit."""
        m = ING_RE.match(text.strip())
        if m:
            qty = m.group(1)
            unit = (' ' + m.group(2).strip()) if m.group(2) else ''
            return f"{qty}{unit} {new_name}"
        return new_name

    db = get_db()
    rows = db.execute("SELECT id, ingredient_groups FROM recipes").fetchall()
    updated = 0
    for row in rows:
        rid = row[0]
        try:
            igs = json.loads(row[1] or "[]")
        except Exception:
            continue
        changed = False
        for g in igs:
            new_ings = []
            for ing in g.get("ingredients", []):
                if _name_key(ing) == old_key:
                    new_ings.append(_rename_ing(ing, new_name))
                    changed = True
                else:
                    new_ings.append(ing)
            g["ingredients"] = new_ings
        if changed:
            flat = [i for g in igs for i in g.get("ingredients", [])]
            db.execute(
                "UPDATE recipes SET ingredient_groups=?, ingredients=? WHERE id=?",
                (json.dumps(igs), json.dumps(flat), rid)
            )
            updated += 1
    db.commit()
    return jsonify({"ok": True, "updated_recipes": updated})


@app.route("/shopping/ingredients", methods=["GET"])
def list_shopping_ingredients():
    """Return all unique ingredient strings from all recipes in the active cookbook."""
    db = get_db()
    rows = db.execute("SELECT ingredient_groups, ingredients FROM recipes").fetchall()
    all_ings = set()
    for row in rows:
        try:
            groups = json.loads(row[0] or "[]")
            for grp in groups:
                for ing in grp.get("ingredients", []):
                    if ing and ing.strip():
                        all_ings.add(ing.strip())
        except Exception:
            pass
        try:
            plain = json.loads(row[1] or "[]")
            for ing in plain:
                if ing and ing.strip():
                    all_ings.add(ing.strip())
        except Exception:
            pass
    return jsonify(sorted(all_ings))


# ── Backup ────────────────────────────────────────────────────────────────────

def _backup_cookbook_name(fname):
    """Extract original cookbook name from a backup filename.
    Format: {cb_name}_{YYYY-MM-DD} or {cb_name}_{YYYY-MM-DD_HH-MM-SS}"""
    stem = fname[:-len('.cookbook')] if fname.endswith('.cookbook') else fname
    m = re.match(r'^(.+?)_(\d{4}-\d{2}-\d{2}(?:_\d{2}-\d{2}-\d{2})?)$', stem)
    return m.group(1) if m else stem


@app.route("/backup/list", methods=["GET"])
def list_backups():
    backup_dir = os.path.join(DATA_DIR, "backups")
    os.makedirs(backup_dir, exist_ok=True)
    files = []
    for fname in sorted(os.listdir(backup_dir), reverse=True):
        if fname.endswith(".cookbook"):
            path = os.path.join(backup_dir, fname)
            cb_name = _backup_cookbook_name(fname)
            files.append({
                "filename": fname,
                "path": path,
                "size": os.path.getsize(path),
                "modified": os.path.getmtime(path),
                "cookbook_name": cb_name,
            })
    return jsonify(files[:60])  # Return last 60 (2 per cookbook × 30 days)


@app.route("/backup/create", methods=["POST"])
def create_backup():
    """Create a timestamped backup of EVERY known cookbook."""
    import shutil, datetime
    backup_dir = os.path.join(DATA_DIR, "backups")
    os.makedirs(backup_dir, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    created = []
    # Collect all known cookbook paths
    all_books = get_cookbooks_list()
    paths_to_backup = [b["path"] for b in all_books if os.path.exists(b["path"])]
    if not paths_to_backup:
        paths_to_backup = [active_db_path()]
    for cb_path in paths_to_backup:
        cb_name = os.path.splitext(os.path.basename(cb_path))[0]
        backup_name = f"{cb_name}_{ts}.cookbook"
        backup_path = os.path.join(backup_dir, backup_name)
        shutil.copy2(cb_path, backup_path)
        created.append({"filename": backup_name, "path": backup_path, "cookbook_name": cb_name})
    # Clean up: keep last 30 per cookbook
    all_files = sorted([
        os.path.join(backup_dir, f) for f in os.listdir(backup_dir)
        if f.endswith(".cookbook")
    ], key=os.path.getmtime)
    # Group by cookbook name and prune each
    from collections import defaultdict
    by_cb: dict = defaultdict(list)
    for fp in all_files:
        by_cb[_backup_cookbook_name(os.path.basename(fp))].append(fp)
    for cb_files in by_cb.values():
        for old in sorted(cb_files, key=os.path.getmtime)[:-30]:
            try: os.remove(old)
            except OSError: pass
    return jsonify({"ok": True, "created": created,
                    "filename": created[0]["filename"] if created else ""})


@app.route("/backup/restore", methods=["POST"])
def restore_backup():
    import shutil
    data = request.get_json()
    backup_path = data.get("path", "")
    if not backup_path:
        return jsonify({"error": "No path provided"}), 400
    # Validate path is inside the expected backups directory to prevent traversal
    backups_dir = os.path.join(DATA_DIR, "backups")
    abs_backup  = os.path.abspath(backup_path)
    abs_backups = os.path.abspath(backups_dir)
    if not abs_backup.startswith(abs_backups + os.sep) and abs_backup != abs_backups:
        return jsonify({"error": "Invalid backup path"}), 400
    if not os.path.exists(abs_backup):
        return jsonify({"error": "Backup file not found"}), 404
    backup_path = abs_backup
    # Validate it's a valid SQLite cookbook
    try:
        conn = sqlite3.connect(backup_path)
        conn.execute("SELECT COUNT(*) FROM recipes")
        conn.close()
    except Exception:
        return jsonify({"error": "Invalid backup file"}), 400
    # Determine which cookbook this backup belongs to (by name in filename)
    cb_name = _backup_cookbook_name(os.path.basename(backup_path))
    target_path = os.path.join(COOKBOOKS_DIR, cb_name + ".cookbook")
    # If the target doesn't exist in the default cookbooks dir, check linked cookbooks
    if not os.path.exists(target_path):
        s = load_settings()
        for lpath in s.get("linkedCookbooks", []):
            lname = os.path.splitext(os.path.basename(lpath))[0]
            if lname.lower() == cb_name.lower() and os.path.exists(lpath):
                target_path = lpath
                break
    # If still not found, refuse — don't silently overwrite an unrelated cookbook
    if not os.path.exists(target_path):
        return jsonify({"error": f"No matching cookbook '{cb_name}' found to restore into. The original cookbook file may have been moved or deleted."}), 404
    shutil.copy2(backup_path, target_path)
    _invalidate_cookbooks_cache()
    return jsonify({"ok": True, "restored_to": cb_name})


@app.route("/backups/open-folder", methods=["POST"])
def open_backups_folder():
    """Open the backups folder in the system file manager."""
    backups_dir = os.path.join(DATA_DIR, "backups")
    os.makedirs(backups_dir, exist_ok=True)
    try:
        if sys.platform == "win32":
            os.startfile(backups_dir)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", backups_dir])
        else:
            subprocess.Popen(["xdg-open", backups_dir])
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True})


# ── CSV helpers ────────────────────────────────────────────────────────────────

def _image_to_exportable(image_val):
    """Convert a stored image value to a portable form for CSV export.
    Local uploads are embedded as base64 data-URIs so the CSV is self-contained."""
    if not image_val:
        return ""
    if image_val.startswith("data:"):
        return image_val                    # already a data-URI
    if image_val.startswith("/static/uploads/"):
        filename = image_val[len("/static/uploads/"):]
        filepath = os.path.join(UPLOADS_DIR, filename)
        if os.path.exists(filepath):
            mime = mimetypes.guess_type(filepath)[0] or "image/jpeg"
            with open(filepath, "rb") as fh:
                b64 = base64.b64encode(fh.read()).decode()
            return f"data:{mime};base64,{b64}"
    return image_val                        # external URL — keep as-is


def _image_from_import(image_val):
    """Restore an exported image value back to a storable form.
    Base64 data-URIs are decoded and saved as local uploads."""
    if not image_val:
        return None
    if image_val.startswith("data:"):
        try:
            header, b64data = image_val.split(",", 1)
            ext = header.split(";")[0].split("/")[-1]
            if ext not in ("jpeg", "jpg", "png", "gif", "webp"):
                ext = "jpg"
            filename = f"imported_{uuid.uuid4().hex[:12]}.{ext}"
            os.makedirs(UPLOADS_DIR, exist_ok=True)
            with open(os.path.join(UPLOADS_DIR, filename), "wb") as fh:
                fh.write(base64.b64decode(b64data))
            return f"/static/uploads/{filename}"
        except Exception:
            return None
    return image_val                        # external URL — keep as-is


# ── Macleay Recipe Manager CSV (lossless round-trip) ──────────────────────────
# First row is a header containing "rm_version" in column 0.
# This distinguishes it from AccuChef CSVs (which start with a blank row).
_RM_CSV_HEADER = [
    "rm_version", "title", "categories", "category", "servings", "servings_num",
    "total_time", "source_url", "site_name", "image",
    "ingredient_groups", "instruction_groups",
]


def export_cookbook_csv(cookbook_path):
    """Export all recipes to a Recipe Manager CSV (full fidelity, lossless)."""
    conn = sqlite3.connect(cookbook_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM recipes ORDER BY title COLLATE NOCASE").fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    writer.writerow(_RM_CSV_HEADER)

    for row in rows:
        r = row_to_dict(row)
        cats = r.get("categories") or ([r["category"]] if r.get("category") else [])
        writer.writerow([
            "1",                                                        # rm_version
            r.get("title") or "",
            json.dumps(cats, ensure_ascii=False),                       # categories (JSON array)
            cats[0] if cats else "",                                    # category (first, legacy)
            r.get("servings") or "",
            r.get("servings_num") if r.get("servings_num") is not None else "",
            r.get("total_time") or "",
            r.get("source_url") or "",
            r.get("site_name") or "",
            _image_to_exportable(r.get("image") or ""),
            json.dumps(r.get("ingredient_groups") or [], ensure_ascii=False),
            json.dumps(r.get("instruction_groups") or [], ensure_ascii=False),
        ])

    return output.getvalue()


# ── Unicode fraction normalisation ────────────────────────────
_UNICODE_FRACTIONS = {
    '\u00bd': '1/2', '\u00bc': '1/4', '\u00be': '3/4',
    '\u2153': '1/3', '\u2154': '2/3',
    '\u215b': '1/8', '\u215c': '3/8', '\u215d': '5/8', '\u215e': '7/8',
    '\u2159': '1/6', '\u215a': '5/6',
    '\u2155': '1/5', '\u2156': '2/5', '\u2157': '3/5', '\u2158': '4/5',
}

def _normalize_fractions(text):
    """Replace Unicode fraction characters with ASCII equivalents."""
    if not text:
        return text
    for uc, asc in _UNICODE_FRACTIONS.items():
        text = text.replace(uc, asc)
    return text


def _read_csv_text(csv_path):
    """Read a CSV file, trying UTF-8-BOM first then Windows-1252 fallback.
    Returns the full file text as a string with fractions normalised."""
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = open(csv_path, newline="", encoding=enc).read()
            return _normalize_fractions(text)
        except (UnicodeDecodeError, LookupError):
            continue
    # Last resort — replace undecodable bytes
    text = open(csv_path, newline="", encoding="utf-8-sig", errors="replace").read()
    return _normalize_fractions(text)


def parse_rm_csv(csv_path):
    """Parse a Recipe Manager CSV export — full fidelity."""
    recipes = []
    with io.StringIO(_read_csv_text(csv_path)) as f:
        reader = csv.DictReader(f)
        for row in reader:
            title = (row.get("title") or "").strip()
            if not title:
                continue
            try:
                ig = json.loads(row.get("ingredient_groups") or "[]")
            except Exception:
                ig = [{"purpose": None, "ingredients": []}]
            try:
                sg = json.loads(row.get("instruction_groups") or "[]")
            except Exception:
                sg = [{"purpose": None, "steps": []}]
            image = _image_from_import(row.get("image") or "")
            srv_num_raw = (row.get("servings_num") or "").strip()
            try:
                srv_num = float(srv_num_raw) if srv_num_raw else None
            except ValueError:
                srv_num = None
            flat_ings  = [i for g in ig for i in g.get("ingredients", [])]
            flat_steps = [s for g in sg for s in g.get("steps", [])]
            # Parse categories — prefer JSON array column, fall back to single category
            cats_raw = (row.get("categories") or "").strip()
            try:
                cats = json.loads(cats_raw) if cats_raw else []
                if not isinstance(cats, list):
                    cats = [str(cats)] if cats else []
            except Exception:
                cats = [cats_raw] if cats_raw else []
            if not cats:
                single = (row.get("category") or "").strip()
                cats = [single] if single else []
            cats = [c for c in cats if c][:5]
            recipes.append({
                "title":              title,
                "categories":         cats,
                "category":           cats[0] if cats else None,
                "servings":           (row.get("servings") or "").strip() or None,
                "servings_num":       srv_num,
                "total_time":         (row.get("total_time") or "").strip() or None,
                "source_url":         (row.get("source_url") or "").strip() or None,
                "site_name":          (row.get("site_name") or "").strip() or None,
                "image":              image,
                "ingredients":        flat_ings,
                "instructions":       flat_steps,
                "ingredient_groups":  ig,
                "instruction_groups": sg,
            })
    return recipes


def parse_accuchef_csv(csv_path):
    """Parse an AccuChef exported CSV (no header row, 63 fixed columns)."""
    recipes = []
    with io.StringIO(_read_csv_text(csv_path)) as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) < 9:
                continue
            title = row[0].strip()
            if not title:
                continue
            category      = row[1].strip() or None
            servings_str  = row[3].strip()
            servings_unit = row[4].strip()
            time_str      = row[5].strip()
            notes_raw     = row[6].strip() if len(row) > 6 else ""
            servings = f"{servings_str} {servings_unit}".strip() if servings_unit else servings_str
            instructions_raw = row[-1].strip()
            ingredient_cols  = row[7:-1]
            raw_ings = [c.strip() for c in ingredient_cols if c.strip() and c.strip() != " "]
            ing_groups    = []
            current_group = {"purpose": None, "ingredients": []}
            for ing in raw_ings:
                if re.match(r'^-{3,}', ing):
                    if current_group["ingredients"] or current_group["purpose"]:
                        ing_groups.append(current_group)
                    purpose = re.sub(r'^[-=\s]+|[-=\s]+$', '', ing).strip() or None
                    current_group = {"purpose": purpose, "ingredients": []}
                else:
                    # Strip leading single-dash prefix (AccuChef sub-item style "- 1 cup Salt")
                    clean_ing = re.sub(r'^-\s+', '', ing).strip() or ing
                    current_group["ingredients"].append(clean_ing)
            if current_group["ingredients"] or not ing_groups:
                ing_groups.append(current_group)
            step_groups = [{"purpose": None, "steps": [instructions_raw]}] if instructions_raw else [{"purpose": None, "steps": []}]
            flat_ings  = [i for g in ing_groups for i in g["ingredients"]]
            flat_steps = [s for g in step_groups for s in g["steps"]]
            total_time = time_str if time_str and time_str not in (":", "00:00", ":00") else None
            recipes.append({
                "title":              title,
                "servings":           servings,
                "servings_num":       parse_servings_num(servings_str),
                "ingredients":        flat_ings,
                "instructions":       flat_steps,
                "ingredient_groups":  ing_groups,
                "instruction_groups": step_groups,
                "image":              None,
                "total_time":         total_time,
                "site_name":          "AccuChef Import",
                "source_url":         None,
                "category":           category,
                "notes":              notes_raw or None,
            })
    return recipes


def detect_and_parse_csv(csv_path):
    """Auto-detect RM vs AccuChef CSV and return (type_str, recipes)."""
    with io.StringIO(_read_csv_text(csv_path)) as f:
        reader = csv.reader(f)
        for row in reader:
            if not any(c.strip() for c in row):
                continue        # skip blank rows
            if row and row[0].strip().lower() == "rm_version":
                return "rm", parse_rm_csv(csv_path)
            else:
                return "accuchef", parse_accuchef_csv(csv_path)
    return "accuchef", []


# ── Open folder & Import ───────────────────────────────────────────────────────

@app.route("/open-folder", methods=["POST"])
def open_folder():
    """Open the cookbooks folder in the OS file explorer."""
    os.makedirs(COOKBOOKS_DIR, exist_ok=True)
    try:
        if sys.platform == "win32":
            os.startfile(COOKBOOKS_DIR)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", COOKBOOKS_DIR])
        else:
            subprocess.Popen(["xdg-open", COOKBOOKS_DIR])
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/cookbooks/upload-temp", methods=["POST"])
def upload_temp_cookbook():
    """
    Accept a file upload (.cookbook or .RWZ), save to a temp path,
    peek at it, and return info — used by the HTML file-input fallback.
    """
    import tempfile
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    f   = request.files["file"]
    ext = os.path.splitext(f.filename or "")[1].lower()
    if ext not in (".cookbook", ".csv"):
        return jsonify({"error": "Unsupported file type. Use .cookbook or .csv"}), 400
    tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False, dir=DATA_DIR)
    f.save(tmp.name)
    tmp.close()
    suggested = os.path.splitext(f.filename)[0]
    if ext == ".cookbook":
        try:
            with sqlite3.connect(tmp.name) as c:
                count = c.execute("SELECT COUNT(*) FROM recipes").fetchone()[0]
        except Exception:
            count = 0
        file_type = "cookbook"
    else:
        csv_type, recipes = detect_and_parse_csv(tmp.name)
        count     = len(recipes)
        file_type = "csv_rm" if csv_type == "rm" else "csv"
    return jsonify({"tempPath": tmp.name, "type": file_type,
                    "recipeCount": count, "suggestedName": suggested})


@app.route("/cookbooks/peek", methods=["POST"])
def peek_cookbook():
    """
    Inspect a file before importing — returns recipe count and detected type
    without making any changes.
    """
    data = request.get_json()
    path = (data.get("path") or "").strip()
    if not path or not os.path.exists(path):
        return jsonify({"error": "File not found"}), 404
    ext = os.path.splitext(path)[1].lower()
    if ext == ".cookbook":
        try:
            c = sqlite3.connect(path)
            count = c.execute("SELECT COUNT(*) FROM recipes").fetchone()[0]
            c.close()
        except Exception:
            count = 0
        return jsonify({"type": "cookbook", "recipeCount": count,
                        "suggestedName": os.path.splitext(os.path.basename(path))[0]})
    elif ext == ".csv":
        csv_type, recipes = detect_and_parse_csv(path)
        display_type = "csv_rm" if csv_type == "rm" else "csv"
        return jsonify({"type": display_type, "recipeCount": len(recipes),
                        "suggestedName": os.path.splitext(os.path.basename(path))[0]})
    else:
        return jsonify({"error": "Unsupported file type. Use .cookbook or .csv"}), 400


@app.route("/cookbooks/import", methods=["POST"])
def import_cookbook():
    """Import a .cookbook or AccuChef .RWZ file into the cookbooks folder."""
    data     = request.get_json()
    src_path = (data.get("path") or "").strip()
    name     = (data.get("name") or "").strip()
    if not src_path or not os.path.exists(src_path):
        return jsonify({"error": "Source file not found"}), 404
    if not name:
        return jsonify({"error": "Cookbook name required"}), 400
    safe      = re.sub(r'[\\/:*?"<>|]', "", name).strip()
    dest_path = os.path.join(COOKBOOKS_DIR, safe + ".cookbook")
    if os.path.exists(dest_path):
        return jsonify({"error": "A cookbook with that name already exists"}), 409
    ext = os.path.splitext(src_path)[1].lower()
    if ext == ".cookbook":
        import shutil
        shutil.copy2(src_path, dest_path)
        try:
            with sqlite3.connect(dest_path) as c:
                count = c.execute("SELECT COUNT(*) FROM recipes").fetchone()[0]
        except Exception:
            count = 0
        _invalidate_cookbooks_cache()
        return jsonify({"ok": True, "name": safe, "path": dest_path, "recipeCount": count})
    elif ext == ".csv":
        _csv_type, recipes = detect_and_parse_csv(src_path)
        if not recipes:
            return jsonify({"error": "No recipes could be read from this CSV file."}), 422
        # Create the new cookbook and populate it
        old_path = _active_db["path"]
        _active_db["path"] = dest_path
        init_db()
        _active_db["path"] = old_path          # restore — caller must switch explicitly
        conn = sqlite3.connect(dest_path)
        conn.row_factory = sqlite3.Row
        try:
            _insert_recipes_into_db(conn, recipes)
            conn.commit()
        finally:
            conn.close()
        _invalidate_cookbooks_cache()
        return jsonify({"ok": True, "name": safe, "path": dest_path,
                        "recipeCount": len(recipes)})
    else:
        return jsonify({"error": "Unsupported file type"}), 400


@app.route("/cookbooks/export", methods=["POST"])
def export_cookbook_route():
    """Export a cookbook's recipes to AccuChef-compatible CSV, saved to Downloads."""
    import pathlib
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Cookbook name required"}), 400
    cb_path = os.path.join(COOKBOOKS_DIR, name + ".cookbook")
    if not os.path.exists(cb_path):
        return jsonify({"error": "Cookbook not found"}), 404
    try:
        csv_content = export_cookbook_csv(cb_path)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    # Save to user's Downloads folder (or Desktop as fallback)
    safe_name = re.sub(r'[^\w\s\-]', '', name).strip() or "cookbook"
    downloads = pathlib.Path.home() / "Downloads"
    if not downloads.exists():
        downloads = pathlib.Path.home() / "Desktop"
    out_path = downloads / (safe_name + ".csv")
    try:
        out_path.write_text(csv_content, encoding="utf-8-sig")
    except Exception as e:
        return jsonify({"error": f"Could not write file: {e}"}), 500
    return jsonify({"ok": True, "path": str(out_path)})


if __name__ == "__main__":
    os.makedirs(os.path.join(BASE_DIR, "static"), exist_ok=True)
    startup()
    app.run(debug=True, port=5000)
