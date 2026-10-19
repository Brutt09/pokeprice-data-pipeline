"""SYNTHETIC marketplace listings — a labelled benchmark, not market data.

Until eBay keys are added there is no real listing source. To still build and
*measure* the cleaning + matching stages, this generates messy listing titles
from the real catalogue, with known ground truth stored in `listing_labels`.

Every row is source='synthetic' and is_synthetic=true. Estimates built from it
are flagged synthetic and shown as demo data. Deterministic (fixed seed).

Run:  python -m ingestion.synthetic_listings
"""
import random
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from common.config import SETS
from common.db import engine

SEED = 42
N_LISTINGS = 800
SOURCE = "synthetic"

GRADES = [("RAW", 0.70), ("PSA 10", 0.15), ("PSA 9", 0.08), ("CGC 9.5", 0.04), ("BGS 9", 0.03)]
CURRENCIES = [("USD", 0.6), ("EUR", 0.3), ("GBP", 0.1)]
SET_ALIASES = ["Pokemon 151", "Scarlet & Violet 151", "SV 151", "MEW", "sv3.5", "151", "Pokémon 151"]
RARITY_ALIASES = {
    "Special illustration rare": ["SIR", "Special Illustration Rare", "SAR"],
    "Illustration rare": ["IR", "Illustration Rare", "Art Rare"],
    "Double rare": ["Double Rare", "RR", "ex"],
    "Ultra Rare": ["Ultra Rare", "Full Art", "FA"],
    "Hyper rare": ["Hyper Rare", "Gold", "Secret Rare"],
}
FILLER = ["NM", "Near Mint", "Mint", "English", "🔥", "LP", "Pack Fresh", "Fast Ship", "RARE", "TCG"]
GRADE_MULT = {"RAW": 1.0, "PSA 10": 2.4, "PSA 9": 1.35, "CGC 9.5": 1.6, "BGS 9": 1.3}
GRADE_FLOOR = {"RAW": 0, "PSA 10": 30, "PSA 9": 18, "CGC 9.5": 22, "BGS 9": 18}
# Indicative FX only, so synthetic prices in other currencies are plausible.
EUR_TO = {"EUR": 1.0, "USD": 1.13, "GBP": 0.85}


def pick(rng, weighted):
    return rng.choices([v for v, _ in weighted], weights=[w for _, w in weighted])[0]


def typo(rng, word: str) -> str:
    if len(word) < 5:
        return word
    i = rng.randrange(1, len(word) - 1)
    return rng.choice([word[:i] + word[i + 1:], word[:i] + word[i] + word[i:],
                       word[:i - 1] + word[i] + word[i - 1] + word[i + 1:]])


def make_title(rng, card, finish, grade) -> tuple[str, str]:
    noise = rng.choice(["clean", "clean", "typo", "no_number", "hash_number", "lowercase", "shuffled", "noisy"])
    name = card.name
    if noise == "typo":
        name = " ".join(typo(rng, w) for w in name.split())
    number = f"{card.local_id}/{card.set_official_count}"
    if noise == "hash_number":
        number = f"#{int(card.collector_number)}"
    parts = []
    if grade != "RAW":
        parts.append(rng.choice([grade, f"{grade} GEM MINT" if grade.endswith("10") else grade]))
    parts += [rng.choice(["Pokemon", "Pokémon", "Pokemon TCG", ""]), name]
    if noise != "no_number":
        parts.append(number)
    parts.append(rng.choice(SET_ALIASES))
    if finish == "reverse":
        parts.append(rng.choice(["Reverse Holo", "Rev Holo", "REVERSE HOLO", "RH"]))
    elif finish == "holo" and rng.random() < 0.5:
        parts.append("Holo")
    if card.rarity in RARITY_ALIASES and rng.random() < 0.6:
        parts.append(rng.choice(RARITY_ALIASES[card.rarity]))
    if noise == "noisy" or rng.random() < 0.4:
        parts += rng.sample(FILLER, k=rng.randint(1, 3))
    parts = [p for p in parts if p]
    if noise == "shuffled":
        rng.shuffle(parts)
    title = " ".join(parts)
    if noise == "lowercase":
        title = title.lower()
    return title, noise


DISTRACTORS = [
    "Pokemon 151 Booster Pack Sealed English",
    "Pokemon 151 Booster Bundle 6 packs NEW",
    "Lot of 50 Pokemon 151 cards bulk commons uncommons",
    "{name} {local_id}/{total} PROXY custom card not original",
    "Pokemon 151 Ultra Premium Collection UPC empty box",
    "{name} Pokemon 151 Choose Your Card",
]


JP_TITLES = [
    "{grade}{name} {local_id}/{total} Japanese SV2a Pokemon Card 151",
    "{grade}Pokemon Japanese {name} {local_id}/{total} sv2a {rarity}",
    "{grade}{name} sv2a {local_id}/{total} JPN NM",
]
JP_RARITY_CODES = {"Special illustration rare": "SAR", "Illustration rare": "AR", "Ultra Rare": "SR",
                   "Hyper rare": "UR", "Double rare": "RR"}


def jp_listing(rng, jp, ref_prices):
    """A listing for a Japanese print. No JP reference price exists yet, so the
    price is illustrative: the English equivalent's price x 0.8."""
    en_price = max((ref_prices.get((jp.en_card_id, f), 0) for f in ("normal", "holo", "reverse")), default=0)
    if not en_price:
        return None
    finish = rng.choice(jp.finishes)
    grade = pick(rng, GRADES)
    finish_word = rng.choice([" Mirror Holo", " Reverse Holo"]) if finish == "reverse" else ""
    title = finish_word + " " + rng.choice(JP_TITLES).format(
        grade=f"{grade} " if grade != "RAW" else "", name=jp.name, local_id=jp.local_id,
        total=jp.set_official_count, rarity=JP_RARITY_CODES.get(jp.rarity, ""))
    price = max(en_price * 0.8 * GRADE_MULT[grade], en_price * 0.8 + GRADE_FLOOR[grade])
    return title.strip(), finish, grade, price * rng.lognormvariate(0.08, 0.15)


def generate(cards, ref_prices, rng=None, jp_cards=()):
    rng = rng or random.Random(SEED)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    priced = [c for c in cards if (c.card_id, "normal") in ref_prices or (c.card_id, "holo") in ref_prices
              or (c.card_id, "reverse") in ref_prices]
    weights = [max(ref_prices.get((c.card_id, "holo"), 0), ref_prices.get((c.card_id, "normal"), 0), 1) ** 0.5
               for c in priced]
    listings, labels = [], []
    i = 0
    while len(listings) < N_LISTINGS:
        i += 1
        lid = f"syn-{SEED}-{i:05d}"
        currency = pick(rng, CURRENCIES)
        listed_at = now - timedelta(days=rng.uniform(0, 30))
        seller = f"seller_{rng.randint(1, 120):03d}"
        jp_card = rng.choice(jp_cards) if jp_cards and rng.random() < 0.06 else None
        jp = jp_listing(rng, jp_card, ref_prices) if jp_card else None
        if jp:
            title, finish, grade, price_eur = jp
            labels.append((lid, jp_card.card_id, finish, grade, "japanese"))
        elif rng.random() < 0.05:
            card = rng.choice(priced)
            tpl = rng.choice(DISTRACTORS)
            title = tpl.format(name=card.name, local_id=card.local_id, total=card.set_official_count)
            price_eur = rng.uniform(4, 120)
            labels.append((lid, "NONE", "NONE", "NONE", "distractor"))
        else:
            card = rng.choices(priced, weights=weights)[0]
            finishes = [f for f in ("normal", "holo", "reverse") if (card.card_id, f) in ref_prices]
            finish = rng.choice(finishes)
            grade = pick(rng, GRADES)
            base = ref_prices[(card.card_id, finish)]
            price_eur = max(base * GRADE_MULT[grade], base + GRADE_FLOOR[grade])
            price_eur *= rng.lognormvariate(0.08, 0.15)          # asking-price premium + spread
            if rng.random() < 0.03:
                price_eur *= rng.choice([6.0, 0.12])             # fat-finger / scam outliers
            title, noise = make_title(rng, card, finish, grade)
            labels.append((lid, card.card_id, finish, grade, noise))
        listings.append({
            "listing_id": lid, "title": title, "currency": currency, "seller": seller,
            "price": round(max(price_eur, 0.1) * EUR_TO[currency], 2),
            "shipping": rng.choice([0, 0, 1.5, 3.99, 4.5]), "listed_at": listed_at,
            "url": None, "condition_raw": None,
        })
        if rng.random() < 0.04:                                  # relisted duplicate
            i += 1
            dup = dict(listings[-1], listing_id=f"syn-{SEED}-{i:05d}",
                       listed_at=listed_at + timedelta(hours=rng.uniform(1, 48)))
            listings.append(dup)
            labels.append((dup["listing_id"],) + labels[-1][1:4] + ("duplicate",))
    return listings, labels


def run(stats) -> dict:
    with engine().begin() as conn:
        cards = conn.execute(text("select * from cards where set_id = any(:s) and collector_number is not null"),
                             {"s": SETS}).all()
        ref_prices = {(r.card_id, r.finish): float(r.price_eur) for r in conn.execute(text("""
            select distinct on (o.card_id, o.finish) o.card_id, o.finish, o.price_eur
            from price_observations o
            join card_variants v on v.variant_id = o.variant_id and not v.is_special
            where o.price_type = 'trend' and o.grade = 'RAW'
            order by o.card_id, o.finish, o.observed_at desc""")).all()}
        jp_cards = conn.execute(text("""
            select c.card_id, c.name, c.local_id, c.set_official_count, c.rarity, e.equivalent_card_id as en_card_id,
                   array_agg(v.finish order by v.finish) as finishes
            from cards c
            join card_equivalents e on e.card_id = c.card_id
            join card_variants v on v.card_id = c.card_id
            where c.language = 'ja'
            group by c.card_id, e.equivalent_card_id
            order by c.card_id""")).all()
        listings, labels = generate(cards, ref_prices, jp_cards=jp_cards)
        inserted = 0
        for row in listings:
            inserted += conn.execute(text("""
                insert into listings (source, listing_id, title, price, currency, shipping, seller,
                                      condition_raw, url, listed_at, is_synthetic)
                values ('synthetic', :listing_id, :title, :price, :currency, :shipping, :seller,
                        :condition_raw, :url, :listed_at, true)
                on conflict (source, listing_id) do nothing"""), row).rowcount
        for lid, card_id, finish, grade, noise in labels:
            conn.execute(text("""
                insert into listing_labels values ('synthetic', :l, :c, :f, :g, :n)
                on conflict do nothing"""), {"l": lid, "c": card_id, "f": finish, "g": grade, "n": noise})
    stats.rows_in = len(listings)
    stats.rows_out = inserted
    return {"generated": len(listings), "inserted_new": inserted}
