"""Unit tests for title parsing, matching, and pricing maths. No DB or network needed."""
from types import SimpleNamespace

import pytest

from ingestion.pokemon_prices import parse_prices
from ingestion.tcgdex import parse_cardmarket_prices, parse_variants
from pricing.estimator import listing_confidence, mad_outliers, weighted_median
from transformation.matching import Card, index_by_number, match_listing
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
    Card("sv2a-201", "Charizard ex", "charizard ex", 201, 165, "special illustration rare", ["holo"], "ja"),
]
BY_NUMBER = index_by_number(CATALOGUE)


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


def test_japanese_listing_matches_japanese_print_only():
    m = match_listing(listing("Charizard ex 201/165 SV2a Japanese Pokemon Card 151"), CATALOGUE, BY_NUMBER)
    assert (m.card_id, m.status) == ("sv2a-201", "auto")


def test_japanese_listing_never_matches_english_print():
    # 006 exists only in English in this catalogue -> no Japanese candidate by number
    m = match_listing(listing("Charizard ex 006/165 Japanese"), CATALOGUE, BY_NUMBER)
    assert m.card_id != "sv03.5-006"


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


# ---------- Bulbapedia scraper parsing ----------
# Rows shaped like the live set-list table (Bulbapedia, CC BY-NC-SA 2.5).
def cell(text="", alts=()):
    return {"text": text, "alts": list(alts)}


BULBA_ROWS = [
    [cell("Pokémon Card 151")],
    [cell("No."), cell("Mark"), cell("Card name"), cell("Type"), cell("Rarity"), cell("Promotion")],
    [cell("006/165"), cell("", ["G"]), cell("Charizard", ["ex"]), cell("", ["Fire"]), cell("", ["RR"]), cell("")],
    [cell("197/165"), cell("", ["G"]), cell("Giovanni's Charisma"), cell("Su"), cell("", ["SR"]), cell("")],
    [cell("201/165"), cell("", ["G"]), cell("Charizard", ["ex"]), cell("", ["Fire"]), cell("", ["SAR"]), cell("")],
    [cell("")],
]


def test_parse_bulbapedia_rows():
    from ingestion.bulbapedia import parse_set_rows
    cards = parse_set_rows(BULBA_ROWS)
    assert [(c["number"], c["name"], c["rarity"]) for c in cards] == [
        (6, "Charizard ex", "Double rare"),
        (197, "Giovanni's Charisma", "Ultra Rare"),
        (201, "Charizard ex", "Special illustration rare"),
    ]


def test_jp_card_links_to_english_print_by_name_and_rarity():
    from ingestion.bulbapedia import link_equivalent
    en = [SimpleNamespace(card_id="sv03.5-006", name="Charizard ex", rarity="Double rare", collector_number=6),
          SimpleNamespace(card_id="sv03.5-183", name="Charizard ex", rarity="Ultra Rare", collector_number=183),
          SimpleNamespace(card_id="sv03.5-199", name="Charizard ex", rarity="Special illustration rare",
                          collector_number=199)]
    jp_sar = {"name": "Charizard ex", "rarity": "Special illustration rare", "number": 201}
    assert link_equivalent(jp_sar, en) == ("sv03.5-199", "name+rarity")
    assert link_equivalent({"name": "Mewtwo", "rarity": "Rare", "number": 150}, en) == (None, "unlinked")


def test_same_name_different_rarity_is_not_an_equivalent():
    # Regression (found by the data audit): JP Snorlax illustration rare was linked to EN Snorlax uncommon.
    from ingestion.bulbapedia import link_equivalent
    en = [SimpleNamespace(card_id="sv03.5-143", name="Snorlax", rarity="Uncommon", collector_number=143)]
    assert link_equivalent({"name": "Snorlax", "rarity": "Illustration rare", "number": 181}, en) ==         (None, "jp_exclusive")


def test_scraper_refuses_sites_without_reviewed_terms():
    from ingestion.scraper import PoliteScraper, ScrapeNotAllowed
    with pytest.raises(ScrapeNotAllowed):
        PoliteScraper("x").check_allowed("https://www.tcgplayer.com/product/1")


def test_japanese_mirror_holo_is_reverse_finish():
    p = parse_title("Gastly Mirror Holo sv2a 092/165 JPN")
    assert (p["parsed_language"], p["parsed_finish"]) == ("ja", "reverse")


# ---------- PokemonPriceTracker parsing (both documented response shapes) ----------
def test_ppt_graded_prices_shape():
    from ingestion.pokepricetracker import parse_graded
    out = parse_graded({"gradedPrices": {"psa10": 15750.0, "psa9": 1850.0, "bogus": 3}})
    assert {(o["grade"], o["price_usd"]) for o in out} == {("PSA 10", 15750.0), ("PSA 9", 1850.0)}


def test_ppt_ebay_sales_list_shape_uses_median_and_count():
    from ingestion.pokepricetracker import parse_graded
    out = parse_graded({"ebay_data": {"psa_10_sales": [{"price": 200.0}, {"price": 210.0}, {"price": 900.0}]}})
    assert out == [{"grade": "PSA 10", "price_usd": 210.0, "sample_size": 3, "last_sale_at": None}]


def test_ppt_sold_data_beats_bare_graded_price():
    from ingestion.pokepricetracker import parse_graded
    out = parse_graded({"gradedPrices": {"psa10": 999.0},
                        "ebay": {"psa10": {"averagePrice": 300.0, "count": 12}, "cgc9.5": {"avg": 150, "count": 2}}})
    assert sorted((o["grade"], o["price_usd"], o["sample_size"]) for o in out) == \
        [("CGC 9.5", 150.0, 2), ("PSA 10", 300.0, 12)]


def test_ppt_real_sales_by_grade_shape():
    # Shape verified on a real response (2026-10-19); numbers here are made up.
    from ingestion.pokepricetracker import parse_graded
    card = {"ebay": {"updatedAt": "2026-10-15T13:35:41.898Z", "salesByGrade": {
        "psa10": {"count": 6, "medianPrice": 113.9, "averagePrice": 142.3, "lastSaleDate": "2026-08-27T05:56:36.000Z"},
        "cgc9_5": {"count": 10, "medianPrice": 9.98, "averagePrice": 10.1, "lastSaleDate": "2026-10-01T00:00:00.000Z"},
        "cgc8_5": {"count": 1, "medianPrice": 8.0, "averagePrice": 8.0, "lastSaleDate": None},
    }}}
    out = {o["grade"]: o for o in parse_graded(card)}
    assert set(out) == {"PSA 10", "CGC 9.5", "CGC 8.5"}
    assert out["PSA 10"]["price_usd"] == 113.9            # median, not the skewed average
    assert out["PSA 10"]["sample_size"] == 6
    assert out["PSA 10"]["last_sale_at"].month == 8


def test_sales_confidence_penalises_thin_and_stale_markets():
    from pricing.estimator import sales_confidence
    assert sales_confidence(40, 10) == "high"
    assert sales_confidence(40, 60) == "medium"
    assert sales_confidence(6, 53) == "medium"
    assert sales_confidence(2, 5) == "low"
    assert sales_confidence(50, 200) == "low"
