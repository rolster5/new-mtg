#!/usr/bin/env python3
"""
Local Magic: The Gathering card browser & search
Uses the SQLite database built from Scryfall default-cards.
"""

import gzip
import json
import math
import shutil
import sqlite3
import urllib.request
from pathlib import Path

import streamlit as st

APP_DIR = Path(__file__).parent
DB_PATH = APP_DIR / "mtg_cards.db"
DB_GZ_PATH = APP_DIR / "mtg_cards.db.gz"
CARDS_PER_PAGE = 48

# Optional: direct URL to mtg_cards.db.gz (GitHub Release, Hugging Face, etc.)
# Leave empty to only use a local file. Example:
# DB_DOWNLOAD_URL = "https://github.com/YOUR_USER/YOUR_REPO/releases/download/v1/mtg_cards.db.gz"
DB_DOWNLOAD_URL = ""

st.set_page_config(
    page_title="MTG Card Browser",
    page_icon="🃏",
    layout="wide",
    initial_sidebar_state="expanded",
)


def _is_sqlite_file(path: Path) -> bool:
    """True if file starts with the SQLite magic header."""
    try:
        with open(path, "rb") as f:
            return f.read(16).startswith(b"SQLite format 3")
    except Exception:
        return False


def _is_gzip_file(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(2) == b"\x1f\x8b"
    except Exception:
        return False


def _install_from_path(src: Path) -> bool:
    """Copy or decompress src into DB_PATH."""
    if _is_sqlite_file(src):
        if src.resolve() != DB_PATH.resolve():
            shutil.copy2(src, DB_PATH)
        return _is_sqlite_file(DB_PATH)

    if _is_gzip_file(src):
        with gzip.open(src, "rb") as f_in, open(DB_PATH, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)
        return _is_sqlite_file(DB_PATH)

    return False


def ensure_database() -> bool:
    """Make sure mtg_cards.db exists. Returns True on success."""
    if DB_PATH.exists() and _is_sqlite_file(DB_PATH) and DB_PATH.stat().st_size > 1_000_000:
        return True

    # 1) Local uncompressed db already present but failed size check above
    if DB_PATH.exists() and _is_sqlite_file(DB_PATH):
        return True

    # 2) Local .gz next to the app
    if DB_GZ_PATH.exists():
        with st.spinner("Unpacking card database..."):
            if _install_from_path(DB_GZ_PATH):
                return True

    # 3) Download from configured URL (accepts .db or .gz)
    if DB_DOWNLOAD_URL:
        with st.spinner("Downloading card database (one-time)..."):
            tmp = APP_DIR / "_db_download.tmp"
            try:
                urllib.request.urlretrieve(DB_DOWNLOAD_URL, tmp)
                if not _install_from_path(tmp):
                    # Show a helpful hint about what we actually got
                    head = tmp.read_bytes()[:20] if tmp.exists() else b""
                    st.error(
                        "Downloaded file is not a SQLite database or gzip archive. "
                        f"First bytes: {head!r}"
                    )
                    return False
                try:
                    tmp.unlink(missing_ok=True)
                    if DB_GZ_PATH.exists():
                        DB_GZ_PATH.unlink(missing_ok=True)
                except Exception:
                    pass
                return True
            except Exception as e:
                st.error(f"Failed to download database: {e}")
                return False
            finally:
                try:
                    tmp.unlink(missing_ok=True)
                except Exception:
                    pass

    st.error(
        "Card database not found.\n\n"
        "Place `mtg_cards.db` or `mtg_cards.db.gz` next to `app.py`, "
        "or set `DB_DOWNLOAD_URL` in app.py to a hosted `.db` / `.gz` file."
    )
    return False


if not ensure_database():
    st.stop()

# ---------- Custom CSS ----------
st.markdown("""
<style>
    /* Wider sidebar so long set names fit */
    section[data-testid="stSidebar"] {
        width: 420px !important;
        min-width: 420px !important;
    }
    section[data-testid="stSidebar"] > div {
        width: 420px !important;
    }

    /* Don't truncate multiselect option labels */
    div[data-baseweb="select"] span,
    div[data-baseweb="select"] div {
        white-space: normal !important;
        overflow: visible !important;
        text-overflow: unset !important;
    }
    ul[data-testid="stSelectboxVirtualDropdown"] li,
    div[role="listbox"] li,
    div[role="option"] {
        white-space: normal !important;
        line-height: 1.3 !important;
        padding-top: 0.4rem !important;
        padding-bottom: 0.4rem !important;
    }

    .stImage img {
        border-radius: 8px;
        box-shadow: 0 4px 12px rgba(0,0,0,0.3);
    }
    div[data-testid="stExpander"] {
        border: 1px solid #333;
        border-radius: 8px;
    }
</style>
""", unsafe_allow_html=True)


@st.cache_resource
def get_connection():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


# Common suffixes / variants that belong to a parent expansion.
# Longer / more specific suffixes must come first.
_SET_SUFFIXES = [
    " Beginner Box Front Cards",
    " Jumpstart Front Cards",
    " Eternal Art Series",
    " Eternal Tokens",
    " Commander Tokens",
    " Art Series",
    " Commander",
    " Eternal",
    " Jumpstart",
    " Minigames",
    " Promos",
    " Substitute Cards",
    " Tokens",
    " Front Cards",
    " Extras",
]


def derive_family(set_name: str) -> str:
    """Group related printings under one family name."""
    if not set_name:
        return "Unknown"

    name = set_name.strip()

    # Alchemy is one big family
    if name.lower().startswith("alchemy"):
        return "Alchemy"

    # "Something: Subtitle" — keep the full name as family if it's a UB / special product
    # but strip common trailing variants first
    base = name
    lower = name.lower()
    for suffix in _SET_SUFFIXES:
        if lower.endswith(suffix.lower()):
            base = name[: -len(suffix)].strip()
            break

    # Normalize whitespace / trailing punctuation
    base = base.replace("  ", " ").strip(" :-")
    if not base:
        base = name

    # Title-case so "Avatar: the Last Airbender" and
    # "Avatar: The Last Airbender" collapse into one family
    return base.title()


@st.cache_data
def get_all_sets():
    """Return list of dicts with set_code, set_name, cnt, family."""
    conn = get_connection()
    cur = conn.execute("""
        SELECT set_code, set_name, COUNT(*) as cnt
        FROM cards
        GROUP BY set_code
        ORDER BY set_name COLLATE NOCASE
    """)
    rows = []
    for row in cur.fetchall():
        family = derive_family(row[1] or "")
        rows.append({
            "set_code": row[0],
            "set_name": row[1],
            "cnt": row[2],
            "family": family,
        })
    # Sort by family then set name so related sets sit together
    rows.sort(key=lambda r: (r["family"].lower(), r["set_name"].lower()))
    return rows


@st.cache_data
def get_families():
    """Return list of (family_name, total_cards, set_codes) sorted by name."""
    sets = get_all_sets()
    families: dict[str, dict] = {}
    for s in sets:
        fam = s["family"]
        if fam not in families:
            families[fam] = {"name": fam, "cnt": 0, "codes": []}
        families[fam]["cnt"] += s["cnt"]
        families[fam]["codes"].append(s["set_code"])
    result = sorted(families.values(), key=lambda f: f["name"].lower())
    return result


def _build_where(
    query: str,
    color_filter: list,
    rarity_filter: list,
    type_terms: list[str],
    cmc_min: float | None,
    cmc_max: float | None,
    selected_sets: list[str],
    set_mode: str,
) -> tuple[str, list]:
    """Shared WHERE clause builder for search and random."""
    clauses = []
    params = []

    if query.strip():
        tokens = [t for t in query.strip().split() if t]
        if tokens:
            safe = [t.replace('"', '""') for t in tokens]
            if len(safe) == 1:
                fts_expr = f'{safe[0]}*'
            else:
                fts_expr = " ".join(safe[:-1]) + f" {safe[-1]}*"
            clauses.append("""
                rowid IN (
                    SELECT rowid FROM cards_fts
                    WHERE cards_fts MATCH ?
                )
            """)
            params.append(fts_expr)

    if color_filter:
        color_conds = []
        for c in color_filter:
            color_conds.append("color_identity LIKE ?")
            params.append(f'%"{c}"%')
        clauses.append("(" + " OR ".join(color_conds) + ")")

    if rarity_filter:
        placeholders = ",".join("?" * len(rarity_filter))
        clauses.append(f"rarity IN ({placeholders})")
        params.extend(rarity_filter)

    # Each type term must appear in type_line (AND)
    for term in type_terms:
        term = term.strip()
        if term:
            clauses.append("type_line LIKE ?")
            params.append(f"%{term}%")

    if cmc_min is not None:
        clauses.append("cmc >= ?")
        params.append(cmc_min)
    if cmc_max is not None:
        clauses.append("cmc <= ?")
        params.append(cmc_max)

    if selected_sets:
        placeholders = ",".join("?" * len(selected_sets))
        if set_mode == "include":
            clauses.append(f"set_code IN ({placeholders})")
        else:
            clauses.append(f"set_code NOT IN ({placeholders})")
        params.extend(selected_sets)

    where = " AND ".join(clauses) if clauses else "1=1"
    return where, params


def search_cards(
    query: str,
    color_filter: list,
    rarity_filter: list,
    type_terms: list[str],
    cmc_min: float | None,
    cmc_max: float | None,
    selected_sets: list[str],
    set_mode: str,  # "include" or "exclude"
    page: int = 1,
    per_page: int = CARDS_PER_PAGE,
) -> tuple[list[sqlite3.Row], int]:
    """Return (page of unique cards, total unique matching count).

    Dedupes by oracle_id (one printing per unique card). Cards without
    oracle_id fall back to unique name. Prefers printings that have an image.
    """
    conn = get_connection()
    where, params = _build_where(
        query, color_filter, rarity_filter, type_terms,
        cmc_min, cmc_max, selected_sets, set_mode,
    )

    # Unique key: oracle_id when present, else name
    uniq_key = "COALESCE(oracle_id, name)"

    # One rowid per unique card — prefer ones with an image
    dedupe_subq = f"""
        SELECT rid FROM (
            SELECT rowid AS rid,
                   ROW_NUMBER() OVER (
                       PARTITION BY {uniq_key}
                       ORDER BY
                           CASE WHEN image_normal IS NOT NULL THEN 0 ELSE 1 END,
                           released_at DESC NULLS LAST,
                           rowid
                   ) AS rn
            FROM cards
            WHERE {where}
        ) WHERE rn = 1
    """

    total = conn.execute(
        f"SELECT COUNT(*) FROM ({dedupe_subq})",
        params,
    ).fetchone()[0]

    order_by = "c.name COLLATE NOCASE"
    order_params = []
    if query.strip():
        order_by = """
            CASE
                WHEN c.name LIKE ? THEN 0
                WHEN c.name LIKE ? THEN 1
                ELSE 2
            END,
            c.name COLLATE NOCASE
        """
        q = query.strip()
        order_params = [f"{q}%", f"%{q}%"]

    offset = (page - 1) * per_page
    sql = f"""
        SELECT c.* FROM cards c
        INNER JOIN ({dedupe_subq}) u ON c.rowid = u.rid
        ORDER BY {order_by}
        LIMIT ? OFFSET ?
    """
    # dedupe_subq contains the WHERE once; then optional order params; then limit/offset
    all_params = params + order_params + [per_page, offset]
    results = conn.execute(sql, all_params).fetchall()
    return results, total


def get_random_card(
    query: str,
    color_filter: list,
    rarity_filter: list,
    type_terms: list[str],
    cmc_min: float | None,
    cmc_max: float | None,
    selected_sets: list[str],
    set_mode: str,
) -> sqlite3.Row | None:
    """Pick one random unique card matching the current filters."""
    conn = get_connection()
    where, params = _build_where(
        query, color_filter, rarity_filter, type_terms,
        cmc_min, cmc_max, selected_sets, set_mode,
    )
    uniq_key = "COALESCE(oracle_id, name)"
    sql = f"""
        SELECT c.* FROM cards c
        INNER JOIN (
            SELECT rid FROM (
                SELECT rowid AS rid,
                       ROW_NUMBER() OVER (
                           PARTITION BY {uniq_key}
                           ORDER BY
                               CASE WHEN image_normal IS NOT NULL THEN 0 ELSE 1 END,
                               released_at DESC NULLS LAST,
                               rowid
                       ) AS rn
                FROM cards
                WHERE {where}
            ) WHERE rn = 1
        ) u ON c.rowid = u.rid
        ORDER BY RANDOM()
        LIMIT 1
    """
    return conn.execute(sql, params).fetchone()


# ---------- Sidebar ----------
with st.sidebar:
    st.title("🃏 MTG Search")
    st.caption("Local Scryfall Default Cards browser")

    query = st.text_input(
        "Search",
        placeholder="Lightning Bolt, draw a card, creature...",
        help="Searches name, type line, oracle text, artist, set, keywords",
    )

    st.subheader("Filters")

    colors = st.multiselect(
        "Color Identity",
        options=["W", "U", "B", "R", "G"],
        format_func=lambda x: {"W": "White", "U": "Blue", "B": "Black", "R": "Red", "G": "Green"}[x],
    )

    rarities = st.multiselect(
        "Rarity",
        options=["common", "uncommon", "rare", "mythic", "special", "bonus"],
    )

    TYPE_OPTIONS = [
        "Legendary", "Basic", "Snow", "World",
        "Artifact", "Battle", "Creature", "Enchantment",
        "Instant", "Land", "Planeswalker", "Sorcery", "Kindred",
    ]
    type_selected = st.multiselect(
        "Type contains (all must match)",
        options=TYPE_OPTIONS,
        help="Pick multiple — a card must match ALL of them. "
             "e.g. Legendary + Creature finds legendary creatures.",
    )
    type_extra = st.text_input(
        "Extra type text",
        placeholder="Elf, Wizard, Equipment, Saga...",
        help="Optional. Comma-separated extra terms (also AND). Good for subtypes.",
    )
    # Combine multiselect + free-text terms
    type_terms = list(type_selected)
    if type_extra.strip():
        type_terms.extend([t.strip() for t in type_extra.split(",") if t.strip()])

    col1, col2 = st.columns(2)
    with col1:
        cmc_min = st.number_input("CMC ≥", min_value=0.0, value=0.0, step=1.0)
    with col2:
        cmc_max = st.number_input("CMC ≤", min_value=0.0, value=20.0, step=1.0)

    st.subheader("Expansions / Sets")

    all_sets = get_all_sets()
    families = get_families()

    # --- Family multiselect (Alchemy, Avatar, Bloomburrow, etc.) ---
    family_options = [f["name"] for f in families]
    family_label = {
        f["name"]: f"{f['name']} — {f['cnt']:,} cards · {len(f['codes'])} sets"
        for f in families
    }
    family_codes = {f["name"]: f["codes"] for f in families}

    selected_families = st.multiselect(
        "Set family",
        options=family_options,
        format_func=lambda name: family_label.get(name, name),
        help="Groups related products together (main set + promos + tokens + art series, etc.). "
             "Alchemy is one group. Type to search.",
        placeholder="e.g. Alchemy, Avatar, Bloomburrow...",
    )

    # Expand selected families into set codes
    family_set_codes: list[str] = []
    for fam in selected_families:
        family_set_codes.extend(family_codes.get(fam, []))

    # --- Optional fine-grained individual sets ---
    set_options = [row["set_code"] for row in all_sets]
    set_label = {
        row["set_code"]: f"{row['family']} › {row['set_name']} ({row['set_code'].upper()}) — {row['cnt']}"
        for row in all_sets
    }
    with st.expander("Individual sets (optional)", expanded=False):
        individual_sets = st.multiselect(
            "Choose specific sets",
            options=set_options,
            format_func=lambda code: set_label.get(code, code),
            help="Optional. Adds or narrows beyond the family selection.",
            placeholder="Search individual sets...",
        )

    # Merge family codes + individual codes (unique)
    selected_sets = list(dict.fromkeys(family_set_codes + individual_sets))

    set_mode = st.radio(
        "Set filter mode",
        options=["include", "exclude"],
        format_func=lambda x: "Only these sets" if x == "include" else "Exclude these sets",
        horizontal=True,
        disabled=not selected_sets,
    )

    if selected_sets:
        st.caption(f"{len(selected_sets)} set(s) active in filter")

    st.divider()
    st.markdown(
        f"**Database**  \n"
        f"{DB_PATH.name}  \n"
        f"116,703 cards · {len(all_sets)} sets · {len(families)} families"
    )


# ---------- Main content ----------
st.title("Magic: The Gathering Card Browser")

# Keep page number in session state so filters resetting page feels natural
if "page" not in st.session_state:
    st.session_state.page = 1

# Reset to page 1 when any filter changes
filter_key = (
    query, tuple(colors), tuple(rarities), tuple(type_terms),
    cmc_min, cmc_max, tuple(selected_sets), set_mode
)
if "last_filter_key" not in st.session_state:
    st.session_state.last_filter_key = filter_key
elif st.session_state.last_filter_key != filter_key:
    st.session_state.page = 1
    st.session_state.last_filter_key = filter_key
    st.session_state.pop("random_card", None)

_cmc_min = cmc_min if cmc_min > 0 else None
_cmc_max = cmc_max if cmc_max < 20 else None

results, total = search_cards(
    query=query,
    color_filter=colors,
    rarity_filter=rarities,
    type_terms=type_terms,
    cmc_min=_cmc_min,
    cmc_max=_cmc_max,
    selected_sets=selected_sets,
    set_mode=set_mode,
    page=st.session_state.page,
    per_page=CARDS_PER_PAGE,
)

total_pages = max(1, math.ceil(total / CARDS_PER_PAGE))
# Clamp page if it somehow went out of range
if st.session_state.page > total_pages:
    st.session_state.page = total_pages
    results, total = search_cards(
        query=query,
        color_filter=colors,
        rarity_filter=rarities,
        type_terms=type_terms,
        cmc_min=_cmc_min,
        cmc_max=_cmc_max,
        selected_sets=selected_sets,
        set_mode=set_mode,
        page=st.session_state.page,
        per_page=CARDS_PER_PAGE,
    )

# ----- Random card -----
rand_col1, rand_col2 = st.columns([1, 5])
with rand_col1:
    if st.button("🎲 Random card", use_container_width=True, disabled=total == 0):
        card = get_random_card(
            query=query,
            color_filter=colors,
            rarity_filter=rarities,
            type_terms=type_terms,
            cmc_min=_cmc_min,
            cmc_max=_cmc_max,
            selected_sets=selected_sets,
            set_mode=set_mode,
        )
        if card:
            # Store as a plain dict so it survives reruns
            st.session_state.random_card = dict(card)

with rand_col2:
    if total == 0:
        st.caption("No cards match — random disabled")
    else:
        st.caption(f"Picks 1 of {total:,} cards matching your filters")

if st.session_state.get("random_card"):
    rc = st.session_state.random_card
    with st.container(border=True):
        r1, r2 = st.columns([1, 2])
        with r1:
            img = rc.get("image_normal") or rc.get("image_large")
            if img:
                st.image(img, use_container_width=True)
        with r2:
            st.markdown(f"### {rc.get('name', '?')}")
            if rc.get("mana_cost"):
                st.markdown(f"Mana: `{rc['mana_cost']}`  ·  CMC: {rc.get('cmc')}")
            st.markdown(f"*{rc.get('type_line', '')}*")
            if rc.get("oracle_text"):
                st.markdown(rc["oracle_text"].replace("\n", "  \n"))
            pt = ""
            if rc.get("power") is not None and rc.get("toughness") is not None:
                pt = f"{rc['power']}/{rc['toughness']}"
            elif rc.get("loyalty"):
                pt = f"Loyalty {rc['loyalty']}"
            if pt:
                st.markdown(f"**{pt}**")
            st.caption(
                f"{rc.get('set_name', '')} ({str(rc.get('set_code', '')).upper()}) "
                f"#{rc.get('collector_number', '')} · {rc.get('rarity', '')}  \n"
                f"Artist: {rc.get('artist') or '—'}"
            )
            if rc.get("scryfall_uri"):
                st.markdown(f"[Open on Scryfall]({rc['scryfall_uri']})")
            if st.button("Clear random card"):
                st.session_state.pop("random_card", None)
                st.rerun()

st.divider()

# Header + pagination controls
col_info, col_prev, col_page, col_next = st.columns([3, 1, 2, 1])
with col_info:
    start = (st.session_state.page - 1) * CARDS_PER_PAGE + 1 if total else 0
    end = min(st.session_state.page * CARDS_PER_PAGE, total)
    st.write(f"**{total:,}** cards found  ·  showing {start}–{end}")

with col_prev:
    if st.button("← Prev", disabled=st.session_state.page <= 1, use_container_width=True):
        st.session_state.page -= 1
        st.rerun()

with col_page:
    new_page = st.number_input(
        "Page",
        min_value=1,
        max_value=total_pages,
        value=st.session_state.page,
        label_visibility="collapsed",
    )
    if new_page != st.session_state.page:
        st.session_state.page = int(new_page)
        st.rerun()

with col_next:
    if st.button("Next →", disabled=st.session_state.page >= total_pages, use_container_width=True):
        st.session_state.page += 1
        st.rerun()

st.caption(f"Page {st.session_state.page} of {total_pages}")

if not results:
    st.info("No cards matched your search. Try a broader query or clear some filters.")
else:
    cols_per_row = 6
    for i in range(0, len(results), cols_per_row):
        cols = st.columns(cols_per_row)
        for j, col in enumerate(cols):
            if i + j >= len(results):
                break
            card = results[i + j]
            with col:
                img = card["image_normal"] or card["image_large"]
                if img:
                    st.image(img, use_container_width=True)
                else:
                    st.markdown("🖼️ *No image*")

                name = card["name"]
                if len(name) > 28:
                    name = name[:26] + "…"
                st.markdown(f"**{name}**")
                st.caption(f"{card['set_code'].upper()} · {card['rarity']}")

                with st.expander("Details"):
                    st.markdown(f"**{card['name']}**")
                    if card["mana_cost"]:
                        st.markdown(f"Mana: `{card['mana_cost']}`  ·  CMC: {card['cmc']}")
                    st.markdown(f"*{card['type_line']}*")

                    if card["oracle_text"]:
                        st.markdown(card["oracle_text"].replace("\n", "  \n"))

                    pt = ""
                    if card["power"] is not None and card["toughness"] is not None:
                        pt = f"{card['power']}/{card['toughness']}"
                    elif card["loyalty"]:
                        pt = f"Loyalty {card['loyalty']}"
                    if pt:
                        st.markdown(f"**{pt}**")

                    st.caption(
                        f"{card['set_name']} ({card['set_code'].upper()}) "
                        f"#{card['collector_number']} · {card['rarity']}  \n"
                        f"Artist: {card['artist'] or '—'}"
                    )

                    if card["scryfall_uri"]:
                        st.markdown(f"[Open on Scryfall]({card['scryfall_uri']})")

                    if card["is_multiface"] and card["face_data"]:
                        try:
                            faces = json.loads(card["face_data"])
                            st.markdown("---")
                            st.markdown("**Card Faces**")
                            for face in faces:
                                st.markdown(f"**{face.get('name', '?')}** `{face.get('mana_cost', '')}`")
                                if face.get("type_line"):
                                    st.caption(face["type_line"])
                                if face.get("oracle_text"):
                                    st.markdown(face["oracle_text"].replace("\n", "  \n"))
                                if face.get("image_normal"):
                                    st.image(face["image_normal"], width=200)
                        except Exception:
                            pass

# Bottom pagination (convenience)
if total_pages > 1:
    st.divider()
    b1, b2, b3 = st.columns([1, 2, 1])
    with b1:
        if st.button("← Previous page", disabled=st.session_state.page <= 1, key="prev_bottom"):
            st.session_state.page -= 1
            st.rerun()
    with b2:
        st.markdown(f"<center>Page {st.session_state.page} of {total_pages}</center>", unsafe_allow_html=True)
    with b3:
        if st.button("Next page →", disabled=st.session_state.page >= total_pages, key="next_bottom"):
            st.session_state.page += 1
            st.rerun()

st.divider()
st.caption(
    "Data from Scryfall · Images hosted by Scryfall · "
    "This is a local tool for personal use. Not affiliated with Wizards of the Coast."
)
