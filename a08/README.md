# AUREON A08 — MCX gold port (DhanHQ)

India-market port of the **frozen AUREON v2.9.8/v3** strategy onto **DhanHQ**,
trading **MCX gold futures**. The MT5/cTrader builds (repo root) stay
independent; this package is on its own version track (`3.x = MCX`).

> **PAPER/SIM FIRST.** No real ₹ until A08 shows its own multi-week green
> record. The MT5 forward record does **not** transfer — the netting change
> (see below) alters the branch math, so A08 earns its own demo record.

## Module structure (mirrors the v3 build)

| module | role |
|---|---|
| `version.py` | single source of truth for the version + banner |
| `config.py` | strategy config — **$ distances frozen, never hardcoded ₹**; instrument table |
| `conversion.py` | `R = MCX_quote / XAUUSD`; converts every $ distance → ₹, rounds to tick |
| `dhan_adapter.py` | DhanHQ seam — SL-M / MARKET / Super Order, feed, margin, expiry roll; **PAPER sim built in** |
| `anchors.py` | IST anchor scheduling (A1 dropped), straddle placement |
| `strategy.py` | hold / ladder / trail / TSTOP + **netting-adapted fleet** |
| `risk.py` | kill switch, per-anchor margin gate, EOD flatten |
| `journal.py` | 19-col CSV (same schema as MT5) + `aureon_mcx` Firebase doc (schema v2) |
| `branch_math.py` | **first-task deliverable** — prints fleet branch numbers per lot/R |
| `runner.py` | startup banner (config receipt) + restart-safe state + orchestration |

## The R methodology (nothing hardcoded in rupees)

Every distance lives in the **frozen source units ($ on XAUUSD)** and converts at
runtime through the live ratio

```
R = MCX_quote_price (₹/quote_grams) / XAUUSD_price ($/oz)
₹_distance = $_distance × R     (round to MCX tick ₹1)
```

`R` drifts daily with USDINR + import duty, so it is **recomputed at the first
anchor** each session and frozen for the rest of the day. All of ±$5 trigger,
$18 SL, $30 TP, the $2.5/$6/$10 ladder tiers, $2 gap, $1 TSTOP, $6 boost SL pass
through the same `R`.

## STRUCTURAL DIFFERENCE #1 — netting

Indian futures **net per contract**: a long and a short in the same contract
square off, so the MT5 coexisting fleet (trapped leg + live rescue + boosts) is
impossible. Adapted fleet:

- Straddle = two pending **SL-M** orders, ±(5×R). First fill = position; the
  sibling stop stays working.
- If price travels the full spread and triggers the sibling, it **closes the
  trapped leg at ≈ −($10×R)** (better than riding to the $18 SL), and the
  **rescue + 2 boosts** fire as NEW net positions in the rescue direction
  (rescue-class exits, tight $6×R boost SL).

## Anchors (IST)

`A1 05:00` is **dropped** (MCX closed). Live: **A2 12:30 · A3 16:20 · A4 19:10**
(A4 in the COMEX-overlap evening — the ~2× range session). An optional late
anchor is a post-launch, data-driven decision.

## First task — confirm the branch math

```bash
python -m a08.branch_math --instrument GOLDM --lots 1 --mcx 98000 --xau 3300
```

Prints CLEAN / CLEAN_SL / CRASH / WHIPSAW net ₹ at the chosen lot and live `R`,
plus the kill-switch guard (WHIPSAW must clear the −3% threshold). Confirm these
numbers with Hithesh and pick **GOLDM** (granular sizing, recommended) vs
**GOLDPETAL** (micro-validation) before any orders. Swap with `--instrument`.

## Run the banner (config receipt)

```bash
python -m a08.runner --banner-only            # PAPER
python -m a08.runner --instrument GOLDPETAL --banner-only
```

## Boundaries

- Paper/sim until a multi-week green demo record of A08's own.
- Spec changes beyond the netting adaptation: state win/cost first, Hithesh
  decides. Nothing that lets one anchor breach the kill switch.
