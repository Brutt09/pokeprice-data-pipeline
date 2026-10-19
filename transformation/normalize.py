"""Clean and standardise listings: parse number / grade / finish / language out
of free-text titles, flag non-single listings, convert prices to EUR, and mark
duplicate relists.

Run:  python -m transformation.normalize
"""
import re
import unicodedata

from sqlalchemy import text

from common.db import engine
from ingestion.fx import to_eur

NUMBER_RE = re.compile(r"\b(\d{1,3})\s*/\s*(\d{2,3})\b")
HASH_NUMBER_RE = re.compile(r"#\s*(\d{1,3})\b")
GRADE_RE = re.compile(r"\b(psa|bgs|cgc|sgc|beckett|ace)\s*(10|9\.5|9|8\.5|8|7\.5|7|6|5|4|3|2|1)\b")
REVERSE_RE = re.compile(r"\b(reverse\s*holo|rev\s*holo|reverse\s*foil|mirror\s*holo|mirror|rh)\b")  # mirror = JP term
HOLO_RE = re.compile(r"\bholo\b")
JAPANESE_RE = re.compile(r"\b(japanese|jpn|jp|sv2a|japan)\b")

# Listings that are not a single card of the catalogue.
EXCLUDE_PATTERNS = {
    # "pack fresh" describes a single card's condition, not sealed product
    "sealed_product": re.compile(r"\b(booster|pack(?!\s*fresh)|packs|bundle|etb|elite trainer|upc|"
                                 r"collection box|sealed|tin|display)\b"),
    "lot": re.compile(r"\b(lot of|lot|bulk|\d+\s*x\s*cards|bundle of)\b"),
    "fake": re.compile(r"\b(proxy|custom|fake|replica|orica|fan art|not original)\b"),
    "multi_choice": re.compile(r"\b(choose your card|pick your card|you pick|choose card|u pick)\b"),
    "empty_box": re.compile(r"\b(empty box|box only)\b"),
}
GRADE_COMPANY = {"psa": "PSA", "bgs": "BGS", "beckett": "BGS", "cgc": "CGC", "sgc": "SGC", "ace": "ACE"}


def clean_title(title: str) -> str:
    t = unicodedata.normalize("NFKD", title)
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    t = t.lower().replace("&", " and ")
    t = re.sub(r"[^a-z0-9/#.\s-]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def parse_title(title: str) -> dict:
    t = clean_title(title)
    number = set_total = None
    if m := NUMBER_RE.search(t):
        number, set_total = int(m.group(1)), int(m.group(2))
    elif m := HASH_NUMBER_RE.search(t):
        number = int(m.group(1))
    grade = "RAW"
    if m := GRADE_RE.search(t):
        grade = f"{GRADE_COMPANY[m.group(1)]} {m.group(2)}"
    finish = "reverse" if REVERSE_RE.search(t) else ("holo" if HOLO_RE.search(t) else None)
    flags = [name for name, rx in EXCLUDE_PATTERNS.items() if rx.search(t)]
    # Japanese prints are their own products in the catalogue: routed, not excluded.
    language = "ja" if JAPANESE_RE.search(t) else "en"
    return {"title_clean": t, "parsed_number": number, "parsed_set_total": set_total,
            "parsed_grade": grade, "parsed_finish": finish, "parsed_language": language,
            "flags": flags, "is_excluded": bool(flags)}


MARK_DUPLICATES = text("""
with ranked as (
    select listing_pk,
           first_value(listing_pk) over w as first_pk,
           row_number() over w as rn
    from listings
    where not is_excluded
    window w as (partition by source, coalesce(seller, ''), title_clean, price, currency
                 order by listed_at nulls last, listing_pk)
)
update listings l
set is_duplicate = r.rn > 1,
    duplicate_of = case when r.rn > 1 then r.first_pk end
from ranked r
where l.listing_pk = r.listing_pk
""")


def run(stats) -> dict:
    with engine().begin() as conn:
        rows = conn.execute(text("""select listing_pk, title, price, currency, listed_at
                                    from listings where normalized_at is null""")).all()
        missing_fx = 0
        for r in rows:
            parsed = parse_title(r.title)
            price_eur = to_eur(conn, r.price, r.currency, r.listed_at.date() if r.listed_at else None)
            if r.price is not None and price_eur is None:
                missing_fx += 1
            conn.execute(text("""
                update listings set title_clean = :title_clean, parsed_number = :parsed_number,
                    parsed_set_total = :parsed_set_total, parsed_grade = :parsed_grade,
                    parsed_finish = :parsed_finish, parsed_language = :parsed_language,
                    flags = :flags, is_excluded = :is_excluded, price_eur = :price_eur,
                    normalized_at = now()
                where listing_pk = :pk"""), {**parsed, "price_eur": price_eur, "pk": r.listing_pk})
        conn.execute(MARK_DUPLICATES)
        dupes = conn.execute(text("select count(*) from listings where is_duplicate")).scalar_one()
        excluded = conn.execute(text("select count(*) from listings where is_excluded")).scalar_one()
    stats.rows_in = stats.rows_out = len(rows)
    return {"normalized": len(rows), "duplicates_total": dupes, "excluded_total": excluded,
            "missing_fx": missing_fx}
