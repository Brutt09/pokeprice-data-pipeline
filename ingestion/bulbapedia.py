"""Japanese set lists scraped from Bulbapedia (browser automation).

Why: Japanese prints are separate products with their own numbering (JP 151
has 210 cards, EN has 207) and are traded heavily in Asia. TCGdex has no
JP<->EN link, but Bulbapedia's set pages list each Japanese card with its
English name and rarity code, which lets us
  1. add Japanese cards to the catalogue as their own products (language='ja'),
  2. link each one to its English equivalent (card_equivalents).
Content: Bulbapedia, CC BY-NC-SA 2.5 - attribution in README.

Run:  python -m ingestion.bulbapedia
"""
import logging
import re

from sqlalchemy import text

from common.db import engine
from ingestion.scraper import PoliteScraper
from transformation.normalize import clean_title

log = logging.getLogger(__name__)
SOURCE = "bulbapedia"
BASE = "https://bulbapedia.bulbagarden.net/wiki/"

JP_SETS = [
    # page, caption of the Japanese set-list table, our JP set id, matching EN set id
    {"page": "151_(TCG)", "caption": "Pokémon Card 151", "set_id": "sv2a",
     "set_name": "Pokémon Card 151 (JP)", "en_set_id": "sv03.5"},
]

# Japanese rarity codes -> the English rarity names used by TCGdex.
RARITY_JP_TO_EN = {
    "C": "Common", "U": "Uncommon", "R": "Rare", "RR": "Double rare",
    "AR": "Illustration rare", "SR": "Ultra Rare", "SAR": "Special illustration rare",
    "UR": "Hyper rare",
}
NUMBER_RE = re.compile(r"^(\d{1,3})/(\d{2,3})$")

TABLE_JS = """(t) => [...t.querySelectorAll('tr')].map(tr => [...tr.children].map(td => ({
    text: td.innerText.trim(),
    alts: [...td.querySelectorAll('img')].map(i => i.alt).filter(Boolean)
})))"""


def parse_set_rows(rows: list[list[dict]]) -> list[dict]:
    """Rows of the set-list table -> card dicts. Header/blank rows are skipped.
    Columns: No. | Mark | Card name | Type | Rarity | Promotion."""
    cards = []
    for cells in rows:
        if len(cells) < 5:
            continue
        m = NUMBER_RE.match(cells[0]["text"])
        if not m:
            continue
        name_cell, rarity_cell = cells[2], cells[4]
        name = name_cell["text"]
        if "ex" in name_cell["alts"]:          # the "ex" suffix is an image on Bulbapedia
            name += " ex"
        rarity_code = (rarity_cell["alts"] or [rarity_cell["text"]])[0]
        cards.append({"number": int(m.group(1)), "total": int(m.group(2)), "local_id": m.group(1),
                      "name": name, "rarity_code": rarity_code,
                      "rarity": RARITY_JP_TO_EN.get(rarity_code)})
    return cards


def jp_finishes(rarity_code: str, en_finishes: list[str] | None) -> list[str]:
    """ASSUMPTION (not verified per card): a Japanese print comes in the same
    finishes as its linked English print. Without a link, fall back to
    rarity: C/U/R -> normal + reverse, RR and above -> holo."""
    if en_finishes:
        return en_finishes
    return ["normal", "reverse"] if rarity_code in {"C", "U", "R"} else ["holo"]


def link_equivalent(jp: dict, en_cards: list) -> tuple[str | None, str]:
    """Pick the English print of the same card: same name and rarity; ties broken by nearest number."""
    same_name = [c for c in en_cards if clean_title(c.name) == clean_title(jp["name"])]
    same_rarity = [c for c in same_name if c.rarity == jp["rarity"]]
    pool, method = (same_rarity, "name+rarity") if same_rarity else (same_name, "name_only")
    if not pool:
        return None, "unlinked"
    if len(pool) > 1:
        method += "+nearest_number"
    best = min(pool, key=lambda c: abs(c.collector_number - jp["number"]))
    return best.card_id, method


def scrape(spec: dict) -> list[dict]:
    with PoliteScraper(SOURCE) as s:
        page = s.fetch(BASE + spec["page"])
        tables = page.locator("table")
        for i in range(tables.count()):
            first = tables.nth(i).locator("tr").first
            if first.count() and first.inner_text().strip().startswith(spec["caption"]):
                cards = parse_set_rows(tables.nth(i).evaluate(TABLE_JS))
                if cards:
                    return cards
    raise RuntimeError(f"set-list table '{spec['caption']}' not found on {spec['page']}")


def run(stats) -> dict:
    summary = {}
    for spec in JP_SETS:
        cards = scrape(spec)
        stats.rows_in += len(cards)
        links = {"name+rarity": 0, "name+rarity+nearest_number": 0, "name_only": 0,
                 "name_only+nearest_number": 0, "unlinked": 0}
        with engine().begin() as conn:
            en_cards = conn.execute(text("""
                select c.card_id, c.name, c.rarity, c.collector_number,
                       array_agg(distinct v.finish) filter (where not v.is_special) as finishes
                from cards c join card_variants v on v.card_id = c.card_id
                where c.set_id = :s group by c.card_id"""), {"s": spec["en_set_id"]}).all()
            en_finishes = {c.card_id: c.finishes for c in en_cards}
            for c in cards:
                card_id = f"{spec['set_id']}-{c['number']:03d}"
                conn.execute(text("""
                    insert into cards (card_id, set_id, set_name, set_official_count, name, local_id,
                                       collector_number, rarity, language)
                    values (:id, :s, :sn, :tot, :name, :lid, :num, :rar, 'ja')
                    on conflict (card_id) do update set name = excluded.name, rarity = excluded.rarity,
                        ingested_at = now()"""),
                    {"id": card_id, "s": spec["set_id"], "sn": spec["set_name"], "tot": c["total"],
                     "name": c["name"], "lid": c["local_id"], "num": c["number"], "rar": c["rarity"]})
                en_id, method = link_equivalent(c, en_cards)
                links[method] += 1
                for finish in jp_finishes(c["rarity_code"], en_finishes.get(en_id)):
                    conn.execute(text("""
                        insert into card_variants (variant_id, card_id, finish)
                        values (:v, :c, :f) on conflict (variant_id) do nothing"""),
                        {"v": f"{card_id}:{finish}", "c": card_id, "f": finish})
                if en_id:
                    conn.execute(text("""
                        insert into card_equivalents (card_id, equivalent_card_id, method)
                        values (:c, :e, :m)
                        on conflict (card_id, equivalent_card_id) do update set method = excluded.method"""),
                        {"c": card_id, "e": en_id, "m": method})
        stats.rows_out += len(cards)
        summary[spec["set_id"]] = {"cards": len(cards), "links": {k: v for k, v in links.items() if v}}
    return summary
