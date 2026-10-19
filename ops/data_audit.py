"""Data audit: profile every pipeline table and list oddities.

For each table: row count, and per column the share missing, distinct count,
min / max (numbers and dates) or most common values (text). Then a set of
targeted "weird data" probes that go beyond the pass/fail data-quality checks.

Writes data/audit/latest.md (git-ignored: it contains licensed price data).

Run:  python -m ops.data_audit
"""
from datetime import datetime, timezone

from sqlalchemy import inspect, text

from common.config import ROOT
from common.db import engine

TABLES = ["cards", "card_variants", "card_equivalents", "price_observations", "listings", "card_matches",
          "price_estimates", "price_anomalies", "buyback_quotes", "fx_rates", "raw_ingestions", "pipeline_runs"]
SKIP_COLUMNS = {"payload", "details", "inputs", "notes", "reasons"}   # JSON blobs

# (question, SQL returning rows that look wrong). Empty result = fine.
PROBES = [
    ("Cards with no printed number / rarity",
     "select card_id, name, local_id, rarity from cards where collector_number is null or rarity is null"),
    ("English cards with no non-special variant (cannot be priced)",
     """select c.card_id, c.name from cards c where c.language = 'en' and not exists
        (select 1 from card_variants v where v.card_id = c.card_id and not v.is_special)"""),
    ("Same card + finish with more than one non-special variant row",
     """select card_id, finish, count(*) from card_variants where not is_special
        group by 1, 2 having count(*) > 1"""),
    ("Reference prices <= €0.02 that feed estimates ('low' = cheapest listing, weight 0, is excluded)",
     """select card_id, finish, source, price_type, price_eur from price_observations
        where grade = 'RAW' and price_eur <= 0.02 and price_type <> 'low' order by price_eur limit 20"""),
    ("Sold medians outside min/plausible range (< €1 or > €20,000)",
     """select card_id, grade, price_eur, sample_size from price_observations
        where source = 'ebay_sold_via_ppt' and (price_eur < 1 or price_eur > 20000)"""),
    ("Sold estimates resting on sales over a year old that are NOT marked low confidence",
     """select e.card_id, e.grade, e.estimated_price, e.confidence from v_latest_estimates e
        where e.estimate_type = 'sales' and e.confidence <> 'low' and exists (
          select 1 from price_observations o where o.source = 'ebay_sold_via_ppt' and o.card_id = e.card_id
          and o.finish = e.finish and o.grade = e.grade and o.last_sale_at < now() - interval '365 days')"""),
    ("Grades outside the 1-10 scale or unknown companies",
     """select distinct grade from price_observations
        where grade <> 'RAW' and grade !~ '^(PSA|CGC|BGS|SGC|ACE) ([1-9](\\.5)?|10)$'"""),
    ("Listings priced at <= 0, or with a currency we cannot convert",
     """select source, listing_id, price, currency, price_eur from listings
        where price <= 0 or (price is not null and price_eur is null)"""),
    ("Listings with a number larger than the set (e.g. 250/165)",
     "select listing_id, title, parsed_number, parsed_set_total from listings where parsed_number > 260"),
    ("Auto matches with a low score (should not happen)",
     "select source, listing_id, match_score, match_method from card_matches where status = 'auto' and match_score < 85"),
    ("Estimates with a price range that does not contain the estimate",
     """select card_id, finish, grade, estimate_type, estimated_price, price_low, price_high from v_latest_estimates
        where estimated_price < price_low or estimated_price > price_high"""),
    ("INSIGHT, not an error: graded products selling BELOW the raw card (low grades of chase cards)",
     """select s.card_id, s.grade, s.estimated_price as graded, r.estimated_price as raw, s.n_observations
        from v_latest_estimates s join v_latest_estimates r on r.card_id = s.card_id and r.finish = s.finish
         and r.grade = 'RAW' and r.estimate_type = 'reference'
        where s.estimate_type = 'sales' and s.estimated_price < r.estimated_price and s.n_observations >= 10
        order by s.estimated_price / r.estimated_price limit 15"""),
    ("Japanese cards whose English link has a different rarity",
     """select jp.card_id, jp.name, jp.rarity as jp_rarity, en.card_id as en_card, en.rarity as en_rarity, e.method
        from card_equivalents e join cards jp on jp.card_id = e.card_id join cards en on en.card_id = e.equivalent_card_id
        where jp.rarity is distinct from en.rarity"""),
    ("Gaps in FX history (weekdays with no rate)",
     """select d::date as missing_day from generate_series(
            (select min(rate_date) from fx_rates), (select max(rate_date) from fx_rates), interval '1 day') d
        where extract(isodow from d) < 6
          and not exists (select 1 from fx_rates f where f.rate_date = d::date and f.quote = 'USD')"""),
]


def profile_table(conn, table: str) -> list[str]:
    n = conn.execute(text(f"select count(*) from {table}")).scalar_one()
    lines = [f"### `{table}`: {n:,} rows", ""]
    if n == 0:
        return lines + ["*(empty)*", ""]
    lines += ["| column | missing | distinct | range / top values |", "|---|---|---|---|"]
    for col in inspect(conn).get_columns(table):
        name, ctype = col["name"], str(col["type"]).lower()
        if name in SKIP_COLUMNS:
            continue
        nulls, distinct = conn.execute(text(
            f'select count(*) filter (where "{name}" is null), count(distinct "{name}") from {table}')).one()
        if any(t in ctype for t in ("int", "numeric", "float", "double", "timestamp", "date")):
            lo, hi = conn.execute(text(f'select min("{name}"), max("{name}") from {table}')).one()
            detail = f"{lo} … {hi}"
        elif "[]" in ctype or "array" in ctype:
            detail = ""
        else:
            top = conn.execute(text(f'''select "{name}"::text, count(*) from {table} where "{name}" is not null
                                        group by 1 order by 2 desc limit 3''')).all()
            detail = ", ".join(f"`{str(v)[:28]}` ({c})" for v, c in top) if distinct <= 50 else f"e.g. `{str(top[0][0])[:40]}`"
        flag = " ⚠" if nulls and nulls / n > 0.5 else ""
        lines.append(f"| {name} | {nulls / n:.0%}{flag} | {distinct:,} | {detail} |")
    return lines + [""]


def run_probes(conn) -> tuple[list[str], dict]:
    lines, counts = [], {}
    for question, sql in PROBES:
        rows = conn.execute(text(sql)).all()
        counts[question] = len(rows)
        lines.append(f"### {'✅' if not rows else '⚠'} {question}: {len(rows)}")
        if rows:
            keys = list(rows[0]._mapping.keys())
            lines += ["", "| " + " | ".join(keys) + " |", "|" + "---|" * len(keys)]
            lines += ["| " + " | ".join(str(v) for v in r) + " |" for r in rows[:10]]
            if len(rows) > 10:
                lines.append(f"| … {len(rows) - 10} more |" + " |" * (len(keys) - 1))
        lines.append("")
    return lines, counts


def main() -> dict:
    with engine().connect() as conn:
        report = [f"# Data audit, {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC", "",
                  "## 1. Oddities (targeted probes)", ""]
        probe_lines, counts = run_probes(conn)
        report += probe_lines + ["## 2. Table profiles", ""]
        for t in TABLES:
            report += profile_table(conn, t)
    out = ROOT / "data" / "audit"
    out.mkdir(parents=True, exist_ok=True)
    (out / "latest.md").write_text("\n".join(report), encoding="utf-8")
    return counts


if __name__ == "__main__":
    for q, c in main().items():
        print(f"{'OK ' if c == 0 else 'LOOK'} {c:4d}  {q}")
