# AUREON OS ingest contract (bot → FastAPI → Postgres)

The trading bot (this repo, on the ISP-restricted VPS) ships logs + trades +
rescue events to **AUREON OS** so they're readable from the React app **without
SSHing the server**. The bot holds **no DB creds** — it only `POST`s to one
FastAPI endpoint with a bearer token. Postgres is the system of record;
Firestore remains the existing daily-journal mirror.

This file is the **contract** the AUREON OS repo implements (FastAPI ingest/query
routes + Postgres schema + React views). The bot side (`ingest.py`) is done.

## Why this shape
- Bot → FastAPI (HTTPS + token), **not** bot → Postgres directly: the bot never
  holds DB creds, the network/security surface stays tiny, and it mirrors the
  Dhan bot pattern. FastAPI owns schema, validation, dedup and retention.
- **Network-robust:** the VPS ISP is flaky/blocks endpoints. The bot enqueues
  in memory + a persistent on-disk NDJSON buffer, flushes in batches on a
  background thread with backoff, and **never blocks trading**. An outage or a
  bot restart never loses events (buffer survives on disk).

## Endpoint (AUREON OS implements)
```
POST {AUREON_INGEST_URL}            # e.g. https://os.aureon.app/api/ingest
Authorization: Bearer {AUREON_INGEST_TOKEN}
Content-Type: application/json

{ "events": [ <event>, <event>, ... ] }   # up to AUREON_INGEST_BATCH (50) per call
```
Respond **2xx** (200/201/202/204) only when the batch is durably persisted — the
bot drops a batch from its buffer **only** on a 2xx. Any other status / timeout →
the bot keeps the batch and retries with backoff.

### Idempotency (REQUIRED)
Every event has a stable `id`. The bot re-flushes after a half-acked batch or a
restart, so the server **must upsert by `id`** (e.g. `INSERT ... ON CONFLICT (id)
DO NOTHING`). This is how a re-send never double-inserts.

## Event envelope
```json
{
  "id":   "close:123456",                 // stable, unique -> upsert key
  "type": "log" | "trade" | "rescue",
  "ts":   "2026-06-18T05:32:10.123+00:00", // emit time, UTC ISO-8601
  "payload": { ... }                       // type-specific (below)
}
```

### type = "log"   (id auto-hashed; INFO+ only, DEBUG stays local)
```json
{ "component": "AUREON", "severity": "INFO|SUCCESS|WARN|ERROR|CRITICAL",
  "msg": "🎯 FILL A1_02h_Asia BUY @ $4334.00 (ticket 555)",
  "ts": "2026-06-18T05:32:10+00:00", "tags": null }
```

### type = "trade"   (id = "trade:{ticket}")   one per closed leg
```json
{ "date_ist": "2026-06-18", "anchor": "A1_02h_Asia", "side": "BUY",
  "role": "normal|rescue|boost", "entry": 4334.0, "exit": 4364.0,
  "exit_reason": "TP", "pnl_usd": 1050.0, "ticket": 555 }
```
`role` keeps original vs boost P&L as separate rows (never pooled).

### type = "rescue"   (id = "rescue:{event_id}")   one per finalized rescue event
Same fields as `rescue_events.csv` (see `rescue_log.RESCUE_CSV_HEADER`): includes
`event_type` (FLEET|LONE_RESCUE), `branch` (CRASH_WIN|WHIPSAW_LOSS|SCRATCH),
`net_usd`, `orig_pnl`, `boost_pnl`, `no_boost_net`, the leg tickets/fills, etc.

## Suggested Postgres schema (AUREON OS)
Partition by month so 3-month retention is a `DROP PARTITION` (cheap), and the
React app gets fast time-range + pagination + text search.
```sql
CREATE TABLE logs   (id text PRIMARY KEY, ts timestamptz, component text,
                     severity text, msg text, tags jsonb)         PARTITION BY RANGE (ts);
CREATE TABLE trades (id text PRIMARY KEY, ts timestamptz, date_ist date, anchor text,
                     side text, role text, entry numeric, exit numeric,
                     exit_reason text, pnl_usd numeric, ticket bigint);
CREATE TABLE rescue_events (id text PRIMARY KEY, ts timestamptz, event_type text,
                     branch text, net_usd numeric, orig_pnl numeric,
                     boost_pnl numeric, no_boost_net numeric, doc jsonb);
-- retention: monthly partitions on logs; nightly DROP partitions older than 3 months.
```

## Read endpoints the React app will want (AUREON OS implements)
`GET /logs?since=&until=&severity=&q=&limit=&cursor=` ·
`GET /trades?month=YYYY-MM&anchor=&role=` ·
`GET /rescue?month=YYYY-MM&branch=` — all time-range + paginated.

## Bot-side env (this repo)
| var | default | meaning |
|---|---|---|
| `AUREON_INGEST_URL` | (unset = OFF) | FastAPI ingest URL. Unset ⇒ emitter is a no-op. |
| `AUREON_INGEST_TOKEN` | (unset) | bearer token sent as `Authorization: Bearer …` |
| `AUREON_INGEST_ENABLED` | `on` | `off` disables even if URL set |
| `AUREON_INGEST_BATCH` | `50` | max events per POST |
| `AUREON_INGEST_FLUSH_S` | `10` | flush interval (s) |

Buffer file: `{AUREON_RUN_DIR}/ingest_buffer.ndjson` (restart-safe; capped at
50k events). Local daily CSVs are purged after `cfg.local_retention_days` (90)
since the data lives on in Postgres + Firestore.
