"""Shared HTTP client: timeouts, retries with exponential backoff, a concurrency
cap, and raw-payload persistence (DB + data/raw/ files)."""
import asyncio
import hashlib
import json
import logging
import random
from datetime import datetime, timezone

import httpx
from sqlalchemy import text

from common.config import RAW_DIR
from common.db import engine

log = logging.getLogger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504}


class FetchError(RuntimeError):
    pass


async def get_json(client: httpx.AsyncClient, url: str, *, params=None, headers=None,
                   retries: int = 4, sem: asyncio.Semaphore | None = None):
    """GET a JSON document. Retries network errors and 429/5xx with backoff;
    honours Retry-After. Raises FetchError when retries run out."""
    for attempt in range(retries + 1):
        try:
            if sem:
                async with sem:
                    resp = await client.get(url, params=params, headers=headers)
            else:
                resp = await client.get(url, params=params, headers=headers)
            if resp.status_code in RETRY_STATUSES:
                raise httpx.HTTPStatusError(f"HTTP {resp.status_code}", request=resp.request, response=resp)
            resp.raise_for_status()
            return resp.json()
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status is not None and status not in RETRY_STATUSES:
                raise FetchError(f"{url}: HTTP {status}") from exc
            if attempt == retries:
                raise FetchError(f"{url}: gave up after {retries + 1} attempts ({exc})") from exc
            retry_after = getattr(getattr(exc, "response", None), "headers", {}).get("Retry-After")
            delay = float(retry_after) if retry_after and retry_after.isdigit() else 2 ** attempt
            delay += random.uniform(0, 0.5)
            log.warning("retry %s/%s for %s in %.1fs (%s)", attempt + 1, retries, url, delay, exc)
            await asyncio.sleep(delay)


def save_raw(source: str, endpoint: str, payload) -> bool:
    """Store a payload exactly as received. Returns False when an identical
    payload for this endpoint is already stored (nothing changed upstream)."""
    body = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    digest = hashlib.sha256(body.encode()).hexdigest()
    with engine().begin() as conn:
        inserted = conn.execute(
            text("""insert into raw_ingestions (source, endpoint, payload_hash, payload)
                    values (:s, :e, :h, cast(:p as jsonb))
                    on conflict (source, endpoint, payload_hash) do nothing
                    returning ingestion_id"""),
            {"s": source, "e": endpoint, "h": digest, "p": body},
        ).first()
    if inserted:
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        out = RAW_DIR / source / day
        out.mkdir(parents=True, exist_ok=True)
        safe = endpoint.strip("/").replace("/", "_").replace("?", "_") or "root"
        (out / f"{safe}_{digest[:12]}.json").write_text(body, encoding="utf-8")
    return inserted is not None
