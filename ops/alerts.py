"""Alerting: surface what needs a human after every run.

Collected since the previous alert run:
  * failed pipeline stages
  * failing data-quality checks (latest result per check)
  * NEW error-severity anomalies (not open in the previous anomaly batch)
  * stale data: newest reference price older than STALE_HOURS

Always written to data/alerts/<timestamp>.json and the log. If ALERT_WEBHOOK_URL
is set (Slack, Discord or any JSON webhook), a short message is POSTed too.
"""
import json
import logging
import os
from datetime import datetime, timezone

import httpx
from sqlalchemy import text

from common.config import ROOT
from common.db import engine

log = logging.getLogger(__name__)
WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL", "")
STALE_HOURS = 72


def collect(conn) -> dict:
    since = conn.execute(text("""
        select coalesce(max(started_at), timestamptz '2000-01-01') from pipeline_runs
        where job_name = 'send_alerts' and status = 'success'""")).scalar_one()
    failed_runs = [dict(r._mapping) for r in conn.execute(text("""
        select job_name, started_at, left(error, 200) as error from pipeline_runs
        where status = 'failed' and started_at > :since order by started_at"""), {"since": since})]
    failed_checks = [dict(r._mapping) for r in conn.execute(text("""
        select check_name, severity, failing_rows from (
            select distinct on (check_name) check_name, severity, passed, failing_rows
            from dq_results order by check_name, run_at desc) x
        where not passed order by severity, check_name"""))]
    batches = [r.detected_at for r in conn.execute(text(
        "select detected_at from anomaly_runs order by detected_at desc limit 2"))]
    new_errors = []
    if batches:
        new_errors = [dict(r._mapping) for r in conn.execute(text("""
            select a.card_id, c.name, a.finish, a.grade, a.anomaly_type, a.observed_value, a.expected_value
            from price_anomalies a join cards c using (card_id)
            where a.detected_at = :cur and a.severity = 'error'
              and not exists (select 1 from price_anomalies p
                              where p.detected_at = :prev and p.severity = 'error'
                                and (p.card_id, p.finish, p.grade, p.anomaly_type)
                                    is not distinct from (a.card_id, a.finish, a.grade, a.anomaly_type))
            order by c.name, a.grade"""), {"cur": batches[0], "prev": batches[1] if len(batches) > 1 else None})]
    newest = conn.execute(text("select max(observed_at) from price_observations")).scalar()
    stale_hours = (datetime.now(timezone.utc) - newest).total_seconds() / 3600 if newest else None
    return {"failed_runs": failed_runs, "failed_checks": failed_checks, "new_error_anomalies": new_errors,
            "stale": stale_hours is None or stale_hours > STALE_HOURS,
            "newest_price_age_hours": round(stale_hours, 1) if stale_hours is not None else None}


def summarise(a: dict) -> str:
    lines = []
    if a["failed_runs"]:
        lines.append(f"{len(a['failed_runs'])} failed stage(s): "
                     + ", ".join(r["job_name"] for r in a["failed_runs"]))
    if a["failed_checks"]:
        lines.append("data-quality: " + ", ".join(f"{c['check_name']} ({c['severity']}, {c['failing_rows']})"
                                                  for c in a["failed_checks"]))
    if a["new_error_anomalies"]:
        lines.append(f"{len(a['new_error_anomalies'])} new price anomaly error(s): "
                     + ", ".join(f"{x['name']} {x['grade']} {x['anomaly_type']}" for x in a["new_error_anomalies"][:5]))
    if a["stale"]:
        lines.append(f"prices are stale: newest is {a['newest_price_age_hours']} h old")
    return "PokéPrice: " + ("; ".join(lines) if lines else "all clear")


def run(stats) -> dict:
    with engine().connect() as conn:
        alerts = collect(conn)
    message = summarise(alerts)
    out = ROOT / "data" / "alerts"
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (out / f"{stamp}.json").write_text(json.dumps({"message": message, **alerts}, indent=2, default=str),
                                       encoding="utf-8")
    (log.warning if message != "PokéPrice: all clear" else log.info)(message)
    sent = False
    if WEBHOOK_URL and message != "PokéPrice: all clear":
        resp = httpx.post(WEBHOOK_URL, json={"text": message, "content": message}, timeout=15)
        resp.raise_for_status()
        sent = True
    n = (len(alerts["failed_runs"]) + len(alerts["failed_checks"])
         + len(alerts["new_error_anomalies"]) + int(alerts["stale"]))
    stats.rows_out = n
    return {"alerts": n, "message": message, "webhook_sent": sent}
