"""Unit tests for title parsing, matching, and pricing maths. No DB or network needed."""
from types import SimpleNamespace

import pytest

from ingestion.pokemon_prices import parse_prices
from ingestion.tcgdex import parse_cardmarket_prices, parse_variants
from pricing.estimator import listing_confidence, mad_outliers, weighted_median
from transformation.matching import Card, match_listing
from transformation.normalize import parse_title


# ---------- normalize ----------
@pytest.mark.parametrize("title, number, total, grade, finish", [
    ("Charizard ex 199/165 Pokemon 151 SIR", 199, 165, "RAW", None),
    ("PSA 10 GEM MINT Charizard EX #199 Scarlet & Violet 151", 199, None, "PSA 10", None),
    ("Bulbasaur 001/165 Reverse Holo Pokémon 151 NM", 1, 165, "RAW", "reverse"),
    ("cgc 9.5 mew ex 151/165 holo", 151, 165, "CGC 9.5", "holo"),
    ("Beckett 9 Pikachu 025 / 165 RH", 25, 165, "BGS 9", "reverse"),
])
def test_parse_title_fields(title, number, total, grade, finish):
    p = parse_title(title)
    assert (p["parsed_number"], p["parsed_set_total"], p["parsed_grade"], p["parsed_finish"]) == \
        (number, total, grade, finish)
    assert not p["is_excluded"]


@pytest.mark.parametrize("title, flag", [
    ("Pokemon 151 Booster Bundle 6 packs NEW", "sealed_product"),
    ("Lot of 50 Pokemon 151 cards bulk", "lot"),
    ("Charizard ex 199/165 PROXY custom card", "fake"),
    ("Charizard ex 201/165 Japanese SV2a", "non_english"),
    ("Pikachu Pokemon 151 Choose Your Card", "multi_choice"),
])
def test_parse_title_excludes_non_singles(title, flag):
    p = parse_title(title)
    assert p["is_excluded"] and flag in p["flags"]


def test_pack_fresh_is_a_condition_not_sealed_product():
    assert not parse_title("Mew ex 151/165 Pack Fresh NM")["is_excluded"]


# ---------- matching ----------
CATALOGUE = [
    Card("sv03.5-006", "Charizard ex", "charizard ex", 6, 165, "double rare", ["holo"]),
    Card("sv03.5-199", "Charizard ex", "charizard ex", 199, 165, "special illustration rare", ["holo"]),
    Card("sv03.5-151", "Mew ex", "mew ex", 151, 165, "double rare", ["holo"]),
    Card("sv03.5-001", "Bulbasaur", "bulbasaur", 1, 165, "common", ["normal", "reverse"]),
]
BY_NUMBER = {c.number: [c] for c in CATALOGUE}


def listing(title):
    p = parse_title(title)
    return SimpleNamespace(**p)


def test_number_and_name_gives_auto_match():
    m = match_listing(listing("Charizard ex 199/165 Pokemon 151"), CATALOGUE, BY_NUMBER)
    assert (m.card_id, m.status, m.method) == ("sv03.5-199", "auto", "number+name")


def test_grade_and_finish_are_part_of_the_product():
    m = match_listing(listing("PSA 10 Bulbasaur 001/165 Reverse Holo"), CATALOGUE, BY_NUMBER)
    assert (m.card_id, m.finish, m.grade) == ("sv03.5-001", "reverse", "PSA 10")


def test_typo_still_matches():
    m = match_listing(listing("Bulbasur 001/165 151"), CATALOGUE, BY_NUMBER)
    assert m.card_id == "sv03.5-001" and m.status == "auto"


def test_same_name_split_by_rarity_hint():
    m = match_listing(listing("Charizard ex Pokemon 151 SIR"), CATALOGUE, BY_NUMBER)
    assert (m.card_id, m.method) == ("sv03.5-199", "name+rarity")


def test_same_name_without_hint_goes_to_review():
    m = match_listing(listing("Charizard ex Pokemon 151"), CATALOGUE, BY_NUMBER)
    assert m.status == "review" and m.method == "name_ambiguous"


def test_short_name_inside_set_alias_does_not_win():
    # "MEW" is a set alias; "Mew ex" must not beat the longer "Charizard ex"
    m = match_listing(listing("Charizard ex MEW Special Illustration Rare"), CATALOGUE, BY_NUMBER)
    assert m.card_id == "sv03.5-199"


def test_number_name_conflict_is_not_auto():
    m = match_listing(listing("Bulbasaur 199/165"), CATALOGUE, BY_NUMBER)
    assert m.status != "auto"


def test_excluded_listing_is_rejected():
    m = match_listing(listing("Lot of 50 Pokemon 151 cards bulk"), CATALOGUE, BY_NUMBER)
    assert m.status == "rejected" and m.card_id is None


# ---------- pricing ----------
def test_weighted_median():
    assert weighted_median([1, 2, 3], [1, 1, 1]) == 2
    assert weighted_median([1, 2, 100], [1, 1, 10]) == 100
    assert weighted_median([5], [1]) == 5


def test_mad_flags_fat_finger_and_scam_prices():
    prices = [100, 104, 98, 101, 99, 600, 12]
    assert mad_outliers(prices) == [False, False, False, False, False, True, True]


def test_mad_needs_four_points():
    assert mad_outliers([1, 1000, 5]) == [False, False, False]


def test_listing_confidence_levels():
    assert listing_confidence([100, 101, 99, 100, 102, 98, 100, 101]) == "high"
    assert listing_confidence([100, 300]) == "low"


# ---------- source parsing ----------
def tcgdex_card(**over):
    base = {"id": "sv03.5-001", "variants_detailed": [
        {"type": "normal", "variantId": "endfynwn4n10gzq", "size": "standard", "thirdParty": {"cardmarket": 1},
         "pricing": {"cardmarket": {"updated": "2026-10-18T10:29:26.026Z", "unit": "EUR", "trend": 0.12,
                                    "avg7": 0.12, "low": 0.02, "trend-holo": 0.35, "avg7-holo": 0.37}}},
        {"type": "reverse", "variantId": "cm4kqul3x1bwlz1f", "size": "standard", "thirdParty": {"cardmarket": 1},
         "pricing": {"cardmarket": {"updated": "2026-10-18T10:29:26.026Z", "unit": "EUR", "trend": 0.12,
                                    "trend-holo": 0.35, "avg7-holo": 0.37}}},
        {"type": "normal", "variantId": "3takscxpcqodqyjzqnsbuwq6", "size": "standard", "stamp": ["set-logo"],
         "thirdParty": {"cardmarket": 2}, "pricing": None},
    ]}
    return {**base, **over}


def test_variant_ids_are_scoped_to_the_card():
    # Regression: TCGdex variantIds repeat across cards (they identify a variant type).
    a = parse_variants(tcgdex_card())
    b = parse_variants(tcgdex_card(id="sv03.5-053"))
    assert {v["variant_id"] for v in a}.isdisjoint({v["variant_id"] for v in b})


def test_special_variants_are_flagged():
    v = parse_variants(tcgdex_card())
    assert [x["is_special"] for x in v] == [False, False, True]
    assert v[2]["special_label"] == "stamp:set-logo"


def test_reverse_uses_cardmarket_holo_fields():
    normal, reverse, _ = parse_variants(tcgdex_card())
    n = {o["price_type"]: o["price"] for o in parse_cardmarket_prices(normal)}
    r = {o["price_type"]: o["price"] for o in parse_cardmarket_prices(reverse)}
    assert n == {"trend": 0.12, "avg7": 0.12, "low": 0.02}
    assert r == {"trend": 0.35, "avg7": 0.37}


def test_pokemontcg_price_parsing():
    card = {"number": "6", "tcgplayer": {"updatedAt": "2026/10/15", "prices": {
        "holofoil": {"low": 5.0, "mid": 7.5, "high": 40.0, "market": 6.9}}}}
    obs = parse_prices(card)
    assert {(o["finish"], o["price_type"], o["price"]) for o in obs} == \
        {("holo", "market", 6.9), ("holo", "mid", 7.5), ("holo", "low", 5.0)}
    assert all(o["number"] == 6 and o["currency"] == "USD" for o in obs)


def test_snapshot_timestamp_jitter_is_removed():
    # Regression: the same snapshot came back as .805Z and .837Z on two requests.
    a, b = tcgdex_card(), tcgdex_card()
    b["variants_detailed"][0]["pricing"]["cardmarket"]["updated"] = "2026-10-18T10:29:25.805Z"
    oa = parse_cardmarket_prices(parse_variants(a)[0])
    ob = parse_cardmarket_prices(parse_variants(b)[0])
    assert {o["observed_at"] for o in oa} == {o["observed_at"] for o in ob}
