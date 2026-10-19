"""Entity resolution: map each listing to a canonical product
(card_id, finish, grade) with a score and the method that produced it.

Strategy
  1. Excluded listings (sealed, lots, fakes, non-English) -> rejected.
  2. Number path: printed number (and set size) picks the candidate; the
     name must agree (fuzzy), otherwise it goes to review.
  3. Name path (no number): fuzzy name search over the catalogue; cards that
     share a name are split by rarity words in the title (SIR, IR, gold ...),
     otherwise the match is ambiguous -> review.
  Score >= 85 auto, 65-85 review, < 65 rejected.

Run:  python -m transformation.matching
"""
import re
from dataclasses import dataclass

from rapidfuzz import fuzz
from sqlalchemy import text

from common.db import engine
from transformation.normalize import clean_title

AUTO_THRESHOLD = 85
REVIEW_THRESHOLD = 65

# Title words -> catalogue rarity (lower-case), used to split same-name cards.
RARITY_HINTS = [
    (re.compile(r"\b(sir|sar|special illustration)\b"), "special illustration rare"),
    (re.compile(r"\b(ir|illustration rare|art rare|ar)\b"), "illustration rare"),
    (re.compile(r"\b(hyper rare|gold|secret rare)\b"), "hyper rare"),
    (re.compile(r"\b(ultra rare|full art|fa)\b"), "ultra rare"),
]


@dataclass
class Card:
    card_id: str
    name: str
    name_clean: str
    number: int
    set_total: int | None
    rarity: str
    finishes: list[str]
    language: str = "en"


@dataclass
class Match:
    card_id: str | None
    finish: str | None
    grade: str | None
    score: float
    method: str
    status: str


def name_score(card_name_clean: str, title_clean: str) -> float:
    # token_set handles extra words; partial handles typos inside a long title
    return max(fuzz.token_set_ratio(card_name_clean, title_clean),
               fuzz.partial_ratio(card_name_clean, title_clean))


def pick_finish(card: Card, parsed_finish: str | None) -> str:
    if parsed_finish in card.finishes:
        return parsed_finish
    if parsed_finish == "holo" and "holo" not in card.finishes and "normal" in card.finishes:
        return "normal"   # sellers often write "holo" for any shiny card
    for default in ("normal", "holo"):
        if default in card.finishes:
            return default
    return card.finishes[0]


def status_for(score: float) -> str:
    return "auto" if score >= AUTO_THRESHOLD else ("review" if score >= REVIEW_THRESHOLD else "rejected")


def index_by_number(cards: list[Card]) -> dict[tuple[str, int], list[Card]]:
    idx: dict[tuple[str, int], list[Card]] = {}
    for c in cards:
        idx.setdefault((c.language, c.number), []).append(c)
    return idx


def match_listing(listing, cards: list[Card], by_number: dict[tuple[str, int], list[Card]]) -> Match:
    grade = listing.parsed_grade or "RAW"
    if listing.is_excluded:
        return Match(None, None, None, 0, "excluded:" + ",".join(listing.flags or []), "rejected")
    title = listing.title_clean
    # Language is part of the product: a Japanese listing only matches Japanese prints.
    lang = getattr(listing, "parsed_language", None) or "en"
    cards = [c for c in cards if c.language == lang]
    if not cards:
        return Match(None, None, None, 0, f"no_catalogue:{lang}", "rejected")

    if listing.parsed_number is not None and (lang, listing.parsed_number) in by_number:
        cands = [c for c in by_number[(lang, listing.parsed_number)]
                 if listing.parsed_set_total in (None, c.set_total)]
        if cands:
            best = max(cands, key=lambda c: name_score(c.name_clean, title))
            s = name_score(best.name_clean, title)
            score = 50 + 0.5 * s if s >= 80 else 0.8 * s
            method = "number+name" if s >= 80 else "number_name_conflict"
            return Match(best.card_id, pick_finish(best, listing.parsed_finish), grade,
                         round(score, 2), method, status_for(score))

    scored = sorted(((name_score(c.name_clean, title), c) for c in cards), key=lambda x: -x[0])
    top_score = scored[0][0]
    tied = [c for s, c in scored if s >= top_score - 1e-9]
    # A short name can score 100 inside a longer one ("Mew" in "Charizard ex MEW"):
    # among equal scores, the most specific (longest) name wins.
    longest = max(len(c.name_clean) for c in tied)
    tied = [c for c in tied if len(c.name_clean) == longest]
    if len({c.name_clean for c in tied}) == 1 and len(tied) > 1:
        hinted = None
        for rx, rarity in RARITY_HINTS:
            if rx.search(title):
                hinted = [c for c in tied if c.rarity == rarity]
                break
        if hinted and len(hinted) == 1:
            best, method, score = hinted[0], "name+rarity", 0.9 * top_score
        else:
            best, method, score = tied[0], "name_ambiguous", min(0.9 * top_score, AUTO_THRESHOLD - 5)
    else:
        best, method, score = tied[0], "name_only", 0.9 * top_score
    return Match(best.card_id, pick_finish(best, listing.parsed_finish), grade,
                 round(score, 2), method, status_for(score))


def load_catalogue(conn) -> list[Card]:
    rows = conn.execute(text("""
        select c.card_id, c.name, c.collector_number, c.set_official_count, lower(coalesce(c.rarity, '')) rarity,
               c.language,
               array_agg(distinct v.finish) filter (where not v.is_special) finishes
        from cards c join card_variants v on v.card_id = c.card_id
        where c.collector_number is not null
        group by c.card_id""")).all()
    return [Card(r.card_id, r.name, clean_title(r.name), r.collector_number, r.set_official_count,
                 r.rarity, r.finishes or ["normal"], r.language) for r in rows]


def evaluate(conn) -> dict:
    """Precision / recall of auto matches against synthetic ground truth."""
    rows = conn.execute(text("""
        select l.true_card_id, l.true_finish, l.true_grade, l.noise_kind,
               m.card_id, m.finish, m.grade, m.status
        from listing_labels l join card_matches m using (source, listing_id)""")).all()
    if not rows:
        return {}
    real = [r for r in rows if r.true_card_id != "NONE"]
    autos = [r for r in rows if r.status == "auto"]
    correct = [r for r in autos if (r.card_id, r.finish, r.grade) == (r.true_card_id, r.true_finish, r.true_grade)]
    card_only = [r for r in autos if r.card_id == r.true_card_id]
    distractors = [r for r in rows if r.true_card_id == "NONE"]
    by_noise = {}
    for r in real:
        b = by_noise.setdefault(r.noise_kind, {"n": 0, "auto_correct": 0})
        b["n"] += 1
        b["auto_correct"] += (r.status == "auto" and
                              (r.card_id, r.finish, r.grade) == (r.true_card_id, r.true_finish, r.true_grade))
    return {
        "labelled_listings": len(rows),
        "auto_matches": len(autos),
        "precision_product": round(len(correct) / max(len(autos), 1), 4),
        "precision_card_only": round(len(card_only) / max(len(autos), 1), 4),
        "recall_product": round(len(correct) / max(len(real), 1), 4),
        "review_rate": round(sum(r.status == "review" for r in rows) / len(rows), 4),
        "distractors_rejected": f"{sum(r.status == 'rejected' for r in distractors)}/{len(distractors)}",
        "by_noise_kind": by_noise,
    }


def run(stats) -> dict:
    with engine().begin() as conn:
        cards = load_catalogue(conn)
        by_number = index_by_number(cards)
        listings = conn.execute(text("""
            select source, listing_id, title_clean, parsed_number, parsed_set_total, parsed_grade,
                   parsed_finish, parsed_language, flags, is_excluded
            from listings where normalized_at is not null""")).all()
        counts = {"auto": 0, "review": 0, "rejected": 0}
        for l in listings:
            m = match_listing(l, cards, by_number)
            counts[m.status] += 1
            conn.execute(text("""
                insert into card_matches (source, listing_id, card_id, finish, grade,
                                          match_score, match_method, status)
                values (:s, :l, :c, :f, :g, :sc, :m, :st)
                on conflict (source, listing_id) do update set
                    card_id = excluded.card_id, finish = excluded.finish, grade = excluded.grade,
                    match_score = excluded.match_score, match_method = excluded.match_method,
                    status = excluded.status, matched_at = now()"""),
                {"s": l.source, "l": l.listing_id, "c": m.card_id, "f": m.finish, "g": m.grade,
                 "sc": m.score, "m": m.method, "st": m.status})
        metrics = evaluate(conn)
    stats.rows_in = len(listings)
    stats.rows_out = counts["auto"]
    return {**counts, "evaluation": metrics}
