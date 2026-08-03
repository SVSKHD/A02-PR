# AUREON — Discord Command Reference

> Documentation only. No fixes proposed. Every claim cites `file:line` against the
> tree as read on 2026-08-03. Where behaviour could not be established from code,
> the entry says **NOT FOUND** rather than guessing.

## How commands are registered and dispatched

There is **no decorator, slash-command tree, or command dict.** Command handling is a
**two-tier `if/elif` chain** across two processes:

1. **Gateway → parse tier (watchdog process).** `discord_client.py` runs a `discord.py`
   gateway (`start_gateway`, `discord_client.py:287`). Its `on_message` handler
   (`discord_client.py:312`) filters by channel + author, then for any message starting
   with `/` calls `command_handler(content.split()[0], content)` at
   **`discord_client.py:327`**. The handler passed in is the watchdog's
   **`_handle_command`** (`watchdog.py:605`), wired at `watchdog.py:740`.
   `_handle_command` is an `if/elif` chain gated by a membership test against the
   **`ALLOWED_COMMANDS` set** (`watchdog.py:94`, checked at `watchdog.py:607`). A few
   commands are answered entirely in-process (help/status/restart/stop/today); the rest
   are **queued to `run/commands.json`** via `_write_command` (`watchdog.py:354`).

2. **Consume → execute tier (bot process).** The live bot reads the queue with
   `_consume_commands` (`live_trader.py:723`) and dispatches it in a second `if/elif`
   chain, **`_handle_commands`** (`live_trader.py:739`), called once per tick from
   `live_trader.py:2383`.

So the authoritative command list is `ALLOWED_COMMANDS` (`watchdog.py:94-104`); the work
happens in `live_trader._handle_commands` (`live_trader.py:739-829`). The user-facing menu
is the separate `HELP_TEXT` string (`watchdog.py:107-131`).

---

## QUICK REFERENCE — every command

| Command | Class | One-line | Confirm? |
|---|---|---|---|
| `/help` (`/start`) | READ | Print `HELP_TEXT` menu | — |
| `/status` | READ | Positions, P&L, kill switch, engine states | — |
| `/today` | READ | Today's trade summary from `today_trades.csv` | — |
| `/engines` | READ | Both engines' state + open counts per magic | — |
| `/anchors status` · `/rogue status` · `/fetcher status` | READ | Same engines card, scoped note | — |
| `/daylock status` | READ | Per-engine day P&L vs profit/loss stops + lock state | — |
| `/testfire status` | READ | Last testfire run/result/in-flight | — |
| `/pause` | CONFIG | Stop placing **new** anchor orders (`self.paused=True`) | no |
| `/resume` | CONFIG | Resume anchor placement | no |
| `/anchors on\|off` · `/rogue on\|off` · `/fetcher on\|off` | CONFIG | Engine switch; OFF = manage-only | no |
| `/daylock anchors off` | CONFIG | Clear anchors **profit** lock (loss stop stays) | no |
| `/daylock off` | CONFIG | Clear the (default-off) account lock | no |
| `/restart` | PROCESS | Graceful bot restart (watchdog respawns) | no |
| `/stop` | PROCESS | Shut down watchdog **and** bot | no |
| `/testfire` | ORDER *(demo-only)* | Fire ONE isolated `TF_` straddle in-process | **no** ⚠ |
| `/rogueseed` | ORDER *(demo-only)* | Re-anchor Rogue at current tick | no |
| `/fetchseed` | ORDER *(demo-only)* | Re-anchor Fetcher at current tick | no |
| `/flatten` | **DESTRUCTIVE** | Close **all** positions + cancel **all** pendings, all magics | **no** ⚠ |
| `/anchors flatten confirm` | **DESTRUCTIVE** | Close only anchor magic `20260522` | yes |
| `/rogue flatten confirm` | **DESTRUCTIVE** | Close only Rogue magic `20260626` | yes |
| `/fetcher flatten confirm` | **DESTRUCTIVE** | Close only Fetcher magic `20260707` | yes |

**Unconfirmed ORDER/DESTRUCTIVE commands (flagged):**

- **`/flatten`** — DESTRUCTIVE, **no confirmation step.** One message closes every open
  position and cancels every pending across all three magics (`live_trader.py:766-768` →
  `risk.py:127`). The confirm gate exists only on the *per-engine* flatten, never on the
  global one.
- **`/testfire`** — ORDER, **no confirmation and no demo override.** It is hard demo-only
  (rail 1), so on a live/funded account it refuses outright; on a demo account it places a
  real straddle with no confirm prompt. See Part 3.
- **`/rogueseed`, `/fetchseed`** — ORDER, no confirm, but demo-only + rails; refuse on a
  funded/live account (`rogue.py:1682-1690`).

The per-engine flattens (`/anchors|/rogue|/fetcher flatten`) **do** confirm: without
`confirm` they only report the open count and ask again (`watchdog.py:695-704`,
`live_trader.py:1628-1667`).

---

## PART 2 — Per-command detail

Engine magics used throughout: **anchors `20260522`** (`live_trader.py:857`),
**Rogue `20260626`** (`rogue.py:24`), **Fetcher `20260707`** (`fetcher.py:41`).
XAUUSD contract size is 100 oz/lot (`risk.py:34`), so at **lot 0.53** each $1 gold move =
**$53**, and an 18-point anchor SL = **$954 per leg**.

### `/help` · `/start`
- **Syntax:** no args. `/start` is an alias (both handled at `watchdog.py:609`).
- **Path:** `watchdog.py:609-610` → `self.tele.info(HELP_TEXT)` (`watchdog.py:107`).
- **Preconditions:** none beyond the `ALLOWED_COMMANDS`/channel/author gate.
- **Side effects / output:** none; posts the menu. **Risk:** $0.

### `/status`
- **Syntax:** no args.
- **Path:** `watchdog.py:611-620`. Reads `run/status.json` via `_read_status`
  (`watchdog.py:345`), which the **bot** writes each tick (`_write_status`,
  `live_trader.py:648`). Renders `_format_status` + `_status_card`.
- **Preconditions:** if `status.json` absent → "No status available — bot may still be
  starting" (`watchdog.py:620`).
- **Side effects:** none (reads a file the bot wrote). **Output:** status card. **Risk:** $0.

### `/today`
- **Syntax:** no args.
- **Path:** `watchdog.py:680-681` → `_format_today_summary` (`watchdog.py:582`), which reads
  `run/today_trades.csv` (`watchdog.py:147`) **inside the watchdog process.**
- **Note:** this does **not** queue anything to the bot. The bot's `today_summary`
  handler (`live_trader.py:775`) is a separate, orphaned path (see Part 5). **Risk:** $0.

### `/engines`, `/anchors status`, `/rogue status`, `/fetcher status`
- **Syntax:** `/engines status` (bare `/engines` → `engines_status`, `watchdog.py:710-712`);
  `/{engine} status` (bare `/{engine}` defaults to `status`, `watchdog.py:689`).
- **Path:** queued `engines_status` (`watchdog.py:706`/`:712`) → `live_trader.py:753-756`
  → `_post_engines_status` (`live_trader.py:1567`).
- **Side effects:** none. **Output:** `card_status` (`discord_cards.py:394`) with each
  engine's ON / "OFF (manage-only)" state, broker open/pending counts per magic
  (`_open_counts_per_magic`, `live_trader.py:1550`), boost mode, and daylock lines.
- **Risk:** $0. **Drift note:** the "Boost mode" field can misreport F-B — see Part 4.

### `/pause` · `/resume`
- **Syntax:** no args.
- **Path:** queued `pause`/`resume` (`watchdog.py:631`/`:634`) →
  `live_trader.py:769-774` sets `self.paused = True/False`.
- **Where it bites:** `anchors.py:181-182` (`if self.paused: return`) gates new straddle
  placement; also aborts the stale-tick wait (`anchors.py:434`).
- **Side effects:** in-memory flag only. **Does NOT survive a restart** — `self.paused`
  is re-initialised to `False` in `__init__` (`live_trader.py:279`) and is never persisted
  (it appears only in the display-only `status.json`, `live_trader.py:648`). A paused bot
  silently resumes trading after any restart. **Risk:** $0 direct (a *pause* can't place;
  a forgotten pause that reverts on restart is the hazard).

### `/anchors on|off` · `/rogue on|off` · `/fetcher on|off`
- **Syntax:** `on` / `off` (any other token → usage line, `watchdog.py:707-709`).
- **Path:** queued `engine {engine,action}` (`watchdog.py:690-691`) →
  `live_trader.py:744-750` → `_set_engine` (`live_trader.py:1018`).
- **Mutation:** `self.engines[engine] = bool(on)` (`live_trader.py:1025`), read by
  `_engine_enabled` (`live_trader.py:1009`) and the `_*_entries_blocked` seams
  (`live_trader.py:1042-1068`). OFF = **manage-only** (no new entries; trails/exits/SL/
  EOD/kill-switch keep running).
- **Persistence:** YES for a **same-day** restart — `_set_engine` calls
  `p1_state.save(force=True)` (`live_trader.py:1031-1033`); on boot the persisted value
  **wins** over the config default and emits an "ENGINE STATE OVERRIDE" alert
  (`live_trader.py:285-286`, `p1_state.py:279-311`). A **new** trading day discards the
  stale file and reverts to config defaults (`p1_state.py:270-273`).
- **Side effects:** no order placed by the toggle. **Output:** `_post_engines_status`
  confirm card. **Risk:** $0 direct; enabling an engine re-arms its schedule (indirect).

### `/daylock anchors off` · `/daylock off` · `/daylock status`
- **Syntax:** `status` (default) · `anchors off` · `off` (`watchdog.py:651-664`).
- **Path:** `daylock_status` (`watchdog.py:661`) → `_post_daylock_status`
  (`live_trader.py:1500`); `daylock_override {which}` (`watchdog.py:654`/`:658`) →
  `_daylock_override` (`live_trader.py:1509`).
- **Mutation (`anchors off`):** sets `state['anchors_profit_override']=True`
  (`daystops.py:34`) — but if the anchors stop is a **loss** halt it is **ignored**
  (`live_trader.py:1516-1519`); the hard loss stop is never clearable.
- **Mutation (`off`):** sets the account-override keys (`live_trader.py:1524-1534`),
  re-enabling all engines incl. A5 for the day; per-engine loss stops stay hard.
- **Persistence:** these keys live in `self.state` (persisted to `state.json`,
  `state.py:95`) and are **day-scoped** (`daystops.py:44-46`) — survive a same-day
  restart, cleared on a new broker day.
- **Output:** `tele.warn` + a daylock status re-post. **Risk:** $0 direct (it *removes* a
  brake, so it re-enables the engines' own risk-taking).

### `/restart`
- **Syntax:** no args. **Path:** `watchdog.py:621-623` sets `self.restart_requested=True`;
  the watchdog supervisor loop stops and respawns the bot. **Class:** PROCESS. **Risk:** $0
  order exposure (state is persisted/recovered on boot).

### `/stop`
- **Syntax:** no args. **Path:** `watchdog.py:624-626` sets `self.shutdown_requested=True`
  → watchdog **and** bot exit. **Class:** PROCESS. **Note:** stops management of open
  positions (they are left at the broker with their SL/TP). **Risk:** $0 new exposure.

### `/testfire` · `/testfire status`
- See **Part 3** (dedicated). Class ORDER, demo-only.

### `/rogueseed` · `/fetchseed`
- **Syntax:** no args.
- **Path:** queued `rogueseed`/`fetchseed` (`watchdog.py:640`/`:644`) →
  `live_trader.py:777-798` → `rogue.manual_seed` / `fetcher.manual_seed`.
- **Preconditions (all enforced in `manual_seed`):** DEMO-only + `rogue_a1_anchor_mode`
  on (`manual_seed_ok`, `rogue.py:1682-1690`, funded refused fail-closed); plus rails —
  no open ticket, engine switch on, market open, kill switch clear
  (`manual_seed_rails_blocked`, `rogue.py:1652-1679`); and the hard Rogue loss stop
  (`rogue.py:1734+`). On refusal the user sees the specific reason via `tele.warn`.
- **Side effects:** on a passing DEMO run, plants a real Rogue/Fetcher anchor at the
  current tick (magic `20260626`/`20260707`) → real orders. **Risk:** **$0 on a
  live/funded account** (refused). On demo, exposure is the Rogue/Fetcher engine's own
  sizing, not the anchor lot.

### `/flatten`
- **Syntax:** no args.
- **Path:** queued `flatten` (`watchdog.py:628`) → `live_trader.py:766-768` →
  `_flatten_all(reason="ManualFlatten")` (bound from `risk.py:127`, `live_trader.py:2730`).
- **Behaviour:** iterates **every** ticket in `shadow_positions` (`risk.py:163`) and
  `shadow_pendings` (`risk.py:195`) with 3× retry — **no magic/label filter** — then
  force-closes the open Rogue (`risk.py:230`) and Fetcher (`risk.py:240`) tickets, and
  drops any deferred anchor (`risk.py:218`). **This includes `TF_` testfire legs**, which
  ride the anchor magic and live in the same shadow books.
- **Confirmation:** **NONE.** **Output:** `tele.warn` (`live_trader.py:767`, `risk.py:155`);
  `tele.critical` "FLATTEN INCOMPLETE" if any close/cancel fails (`risk.py:260`).
- **Risk:** does not *create* exposure — it realizes all open P&L at market immediately;
  worst case is close-slippage across the whole book. A mis-fire flattens everything with
  no second step.

### `/anchors flatten [confirm]` · `/rogue flatten [confirm]` · `/fetcher flatten [confirm]`
- **Syntax:** `flatten` alone = **dry** (count + prompt); `flatten confirm` executes
  (`watchdog.py:695-704`).
- **Path:** queued `{engine}_flatten {confirm}` → `live_trader.py:759-763` →
  `_handle_engine_flatten` (`live_trader.py:1616`).
- **`confirm=False`:** posts the open count and "Send `/… flatten confirm`"; **no orders
  touched** (`live_trader.py:1628-1633`/`:1641-1646`/`:1662-1667`).
- **`confirm=True`:** closes **only that engine's magic** — anchors via
  `_flatten_all(scope="ANCHORS")` (`live_trader.py:1634`, Rogue/Fetcher force-closes
  skipped by `risk.py:230/240`), Rogue via `force_close_open`+`cancel_pendings`
  (`live_trader.py:1647-1657`), Fetcher via `_flatten_all(scope="FETCHER")`
  (`live_trader.py:1668-1672`).
- **Risk:** reduces exposure (DESTRUCTIVE close), scoped to one magic.

---

## PART 3 — `/testfire`, specifically

**Bottom line up front:** the module docstring is wrong, and the prior audit was right.
`/testfire` places a **two-leg pending-STOP straddle**, not "one real anchor entry at
MARKET". It is **hard demo-only** — on a live/funded account it refuses at rail 1 and
places nothing. The concerns below therefore bite only on a *demo* account.

### a) MARKET vs PENDING-STOP — which is true?
**PENDING-STOP.** `testfire.py`'s module docstring says *"one real anchor entry, on
demand, at market"* (`testfire.py:1`), and `handle_testfire_command`'s own text says
"placing ONE isolated straddle at current mid" (`testfire.py:518`). The actual code path:

`handle_testfire_command` (`testfire.py:489`) → `arm_testfire_inproc` (`testfire.py:365`)
sets `trader._testfire_deferred` → the tick loop calls `_complete_testfire_anchor`
(`anchors.py:477`) → `_place_completed_anchor` (`anchors.py:499`) →
**`_place_orders_for_anchor`** (`anchors.py:579`) — exactly the PENDING-STOP path the prior
audit named.

Inside it, both legs are sent through `_send_stop` (`anchors.py:767`) →
`adapter.place_stop_order` (`anchors.py:769-775`), and the order type reaching
`mt5.order_send` is:

- `order_type = mt5.ORDER_TYPE_BUY_STOP` / `mt5.ORDER_TYPE_SELL_STOP`
  (**`mt5_adapter.py:433` / `:435`**),
- `"action": mt5.TRADE_ACTION_PENDING` (**`mt5_adapter.py:437`**),
- sent at **`mt5_adapter.py:470`** (`result = mt5.order_send(req)`),
- with `type_time: ORDER_TIME_DAY` (`mt5_adapter.py:447`) — a **day** order.

So the geometry is BUY-stop at `anchor+trigger_dist` and SELL-stop at `anchor−trigger_dist`
(`anchors.py:649-650`), $18 SL / $30 TP — never a `TRADE_ACTION_DEAL` market order. The
docstring's "at MARKET" is drift.

### b) One order or a full straddle? Does No-OCO leave a live sibling?
**A full straddle** — BUY-stop **and** SELL-stop (`anchors.py:789-810`), unless one side
fails pre-flight validity (`anchors.py:659-731`). **`no_oco` applies** (`config.py:167`,
default `True`): when one leg fills, the sibling is **not cancelled** — it is left live and
tagged `rescue_on_fill=True` (`fills.py:247-261`, "No-OCO: sibling … left live … will run
as RESCUE"). So yes: a `/testfire` normally rests **two** legs, and a fill leaves a **live
opposite-side sibling**.

### c) Does a `TF_` fill trigger rescue-boost-v2 placement?
**Yes — the `TF_` isolation does not extend to rescue-boost-v2, and it is ON by default.**

- The gate `_AdapterBoostBroker.is_parent` (`rescue_boost.py:350-353`) returns
  `("A:" in comment) and not is_boost_comment(comment)`.
- Every `TF_` straddle leg carries an `A:<price>` origin tag: the comment is built by
  `tag_comment(f"TF_AUR_{label[:2]}_BUY…", anchor_price)` (`anchors.py:788-794`), and
  `tag_comment` **always appends** `A:<price>` (`stale_leg_sweep.py:52-64`). So a filled
  `TF_` position's comment is e.g. `TF_AUR_TF_BUY A:4028.77` — it contains `A:` and is not
  an `RB` comment ⇒ **`is_parent` → True**.
- The broker view is magic-scoped to `rescue_boost_v2_magic` (default = straddle magic
  `20260522`, `rescue_boost.py:299`), and `TF_` orders ride `20260522`
  (`mt5_adapter.py:426`) ⇒ they pass `_ours` (`rescue_boost.py:301-302`).
- The hook is flag-gated on `rescue_boost_v2_enabled`, which is **`True` by default**
  (`config.py:31`) — note this **contradicts** `rescue_boost.py:32`, whose docstring
  claims "OFF by default".

So on a demo `/testfire`, when a `TF_` leg fills, `place_boosts_for_new_fills`
(`rescue_boost.py:188`) will rest two counter `RB1:/RB2:` stops on it. Those boost orders
carry magic `20260522` but **no `TF_` marker**, so their realized P&L is **not** excluded
from the anchors daily stop (see (f)) — an isolation leak specific to the rescue-boost path.
(This assumes, as the whole registry/boost design does, that the filled position retains
its order comment.)

### d) `stale_leg_sweep` `TF_` exemption — and what actually cancels an orphaned `TF_` leg?
**Confirmed exempt, and nothing in the isolation path cancels it.**

- The `TF_` straddle **never runs the sweep itself**: `anchors.py:755` guards
  `if not _is_tf:` before `_sweep_stale_legs`.
- A **real** anchor's sweep is exempt from touching `TF_` orders: `_is_rescue_boost_comment`
  (`stale_leg_sweep.py:337-347`) returns True when `"TF_" in c` (**`stale_leg_sweep.py:347`**;
  the `RB`/`RGS` regexes are `:331`/`:334`), and `sweep_stale_legs` skips those
  (`stale_leg_sweep.py:286-287`).

So an orphaned `TF_` sibling (from (b)) is **cancelled by no `TF_`-aware mechanism**. It
rests as a live No-OCO **DAY** pending (`ORDER_TIME_DAY`, `mt5_adapter.py:447`) until one of:
(1) it **fills** on a reversal — becoming a live position at lot 0.53 that the loop then
trails; (2) the broker **expires** it at end of the trading day; or (3) a manual
**`/flatten`** or the EOD/Friday flatten clears it (no magic/label filter — `risk.py:163-195`).
`testfire_maybe_teardown` (`testfire.py:465`) only *releases the one-at-a-time latch* once no
`TF_` orders remain — **it cancels nothing** (`testfire.py:469-486`).

### e) The `testfire_collision_min` guard — what it compares
`testfire_collision_min` defaults to **30** (`config.py:254`). It is used **only in the CLI
path** `testfire_preflight` (`testfire.py:175-190`):

```
n    = cfg.testfire_collision_min            # testfire.py:175-176
near = minutes_to_nearest_anchor(cfg, now)   # testfire.py:177
if near is not None and near <= n: … refuse  # testfire.py:178
```

i.e. it compares **minutes to the nearest scheduled anchor** (scanning yesterday/today/
tomorrow, `testfire.py:50-68`) against `n`, and refuses inside the window (bypassable only
by `--force-window`). **The Discord `/testfire` does NOT use this guard.** The in-process
gate `testfire_preflight_inproc` (`testfire.py:295`) uses a *narrower* rail 4 —
`_active_real_anchor` (`testfire.py:265`), which refuses only while a real anchor's
placement window is **active this minute** (`testfire.py:336-343`), not merely near.

### f) Is `TF_` P&L excluded from the anchors daily loss stop?
**Yes.** The anchors decision-path day P&L is `computed_anchors_day_pnl`
(`daystops.py:255`), which calls
`pnl_source.magic_day_net(deals, ANCHORS_MAGIC, exclude_test=True)` (**`daystops.py:282`**).
`exclude_test=True` drops any deal where `_is_test` is true, and `_is_test` is
`"TF_" in deal.comment` (**`pnl_source.py:58-63`**, marker `TEST_COMMENT_MARK="TF_"`
`pnl_source.py:55`). The rebuild used by the testfire preflight itself is the same
(`daystops.py:224`). The exclusion is **symmetric** — a `TF_` loss and a `TF_` win are both
dropped (`pnl_source.py:71-73`). (Caveat: rescue-boost `RB` legs spawned per (c) have no
`TF_` marker, so they are **not** excluded.)

### g) What lot does it use?
**`cfg.lot_size`** — not a fixed value, not an argument. `/testfire` takes no lot argument;
`_place_orders_for_anchor` sets `gap_lot = self.cfg.lot_size` (`anchors.py:611`; halved to
`lot_size/2` only if gap-mode triggers, `anchors.py:634`). At the stated 0.53 that is
0.53 lots per leg.

### h) Is there any demo-only or confirmation gate (as `testfire_immediate.py` has)?
**Demo-only: yes (hard). Confirmation/override: none.**

- `testfire_preflight_inproc` enforces, fail-closed: **rail 1 DEMO-ONLY**
  (`trade_mode == ACCOUNT_TRADE_MODE_DEMO`, `testfire.py:309-323`), **rail 2 NO-FP**
  (`account_profile == STANDARD_5PCT`, `testfire.py:325-329`), **rail 5 one-at-a-time**
  (`testfire.py:331-334`), **rail 4 active-window** (`testfire.py:336-343`), **rail 6
  anchors-brake** (loss halt / profit lock / account lock / Friday hold / engine off,
  `testfire.py:345-359`), plus a **10-minute rate limit** in the caller
  (`testfire.py:493-506`).
- There is **no `--i-know-real-account` / `--lot-min` override** and **no confirm prompt.**
  The CLI `testfireimmediate` (`testfire_immediate.py`) *does* have such a bypass — it runs
  on a non-demo account only when **both** `--i-know-real-account` **and** `--lot-min` are
  passed (`testfire_immediate.py:3-4, 22-25`). The Discord `/testfire` has no equivalent:
  it is demo-only with no escape hatch, so **on a live/funded account it simply refuses**
  (`REFUSED [rail 1 DEMO-ONLY]`) and places nothing.

### `/testfire` — RISK at lot 0.53
- **On a live/funded account: $0.** Rail 1 blocks it; no override exists (h).
- **On a demo account** (the only place it fires): worst case in one invocation is the
  straddle's two 18-point SLs on a whipsaw — 2 × $954 = **≈ $1,908** — plus, because
  rescue-boost-v2 is on by default (c), up to two `RB` counter legs at 0.45 lot with SLs
  15/25 points from entry (≈ $675 + $1,125 = **$1,800** more if both fill and stop).

---

## PART 4 — What the bot sends unprompted

Delivery goes through `self.tele.{info,warn,error,success,critical,send}` → `deliver`
(`discord_client.py:184`), plus the heartbeat card via `dc_client.heartbeat`
(`discord_client.py:226`). Card builders live in `discord_cards.py`.

**FILL**
- Anchor fill — `card_fill` (`discord_cards.py:287`), posted `fills.py:235` when a tracked
  pending is detected filled. Values broker-supplied (no drift).
- Rogue entry/chain fill — `card_monster_fill` (`discord_cards.py:236`),
  `rogue_monster_live.py:233`.
- Boost leg filled — `card_boost` (`discord_cards.py:340`), `boosts_common.py:112` (rc 10009).
- *Anchor placement* posts are **text**, not a card: `anchors.py:389/456/600`.
  `card_anchor_placed` (`discord_cards.py:138`) is built **only in selftest** — dead on the
  live path.

**EXIT / CLOSE**
- Anchor close — `card_close` (`discord_cards.py:311`), `fills.py:510` (with deal) and the
  degraded no-deal-yet variant `fills.py:581` (so a close is never silent).
- Boost exit — text only, `trails.py:213`.
- LOCK_FALLBACK_CLOSE — `card_lock_fallback_close` (`discord_cards.py:273`), `trails.py:483`
  (profit-lock SL rejected twice and price through it → forced market close).
- Rogue sequence close — `card_monster_sequence` (`discord_cards.py:246`),
  `rogue_monster_live.py:233`.
- Fleet event — `card_fleet` (`discord_cards.py:355`), `rescue_log.py:233`.

**BOOST / RESCUE**
- Boost fired — `card_rescue` (`discord_cards.py:329`), `boosts_common.py:73` (RALLY +$5
  fav / RESCUE +$10 adverse arms).
- Boost exception — `tele.error`, `boosts_common.py:92`.
- **F-B trapped rescue** — text, `fills.py:718`; fires only if `trapped_late_rescue_enabled`
  (`fills.py:704`), which is **False by default** (`config.py:181`) ⇒ never fires by default.
- Rogue armed / re-anchor / guard — `discord_cards.py:216/227/259`,
  `rogue_monster_live.py:233`.
- `rescue_boost.py` and `boost_spec.py` post **nothing** themselves (pure logic).

**HALT / DAYLOCK / KILL-SWITCH**
- Kill switch triggered — `tele.critical`, `live_trader.py:2474` (equity drawdown ≥ limit,
  then `_flatten_all("KillSwitch")`).
- Anchors day profit stop — `tele.warn`, `live_trader.py:1179` (once/day).
- Account day lock (legacy pct) — `tele.warn`, `live_trader.py:1228` (inert unless armed).
- DAY LOCKED secured / give-back — `card_day_locked` (`discord_cards.py:442`),
  `live_trader.py:1311`.
- Anchor skipped by day stop — `tele.warn`, `live_trader.py:1348`.
- Friday flatten confirmed/failed — `live_trader.py:985/992`.
- Flatten notices — `risk.py:149/155`; "FLATTEN INCOMPLETE" `tele.critical`, `risk.py:260`.
- Rogue governor halt — `card_monster_governor` (`discord_cards.py:266`),
  `rogue_monster_live.py:233`.

**ERROR**
- Tick failed `live_trader.py:2341`; fatal loop `live_trader.py:1928/2178` (`tele.critical`);
  account-info read `live_trader.py:374`; SL-modify failed `trails.py:492`; PTRACE
  violation / phantom-lock `live_trader.py:529/533`; offset mismatch
  `live_trader.py:1918/1928`; MT5 reconcile failed `fills.py:121`. Watchdog-process errors:
  heartbeat stale `watchdog.py:816`, bot-exited-not-relaunching `watchdog.py:802`,
  self-restart looped `watchdog.py:789`.

**HEARTBEAT**
- Discord heartbeat card — `card_heartbeat` (`discord_cards.py:380`), posted on a daemon
  thread every `discord_heartbeat_min` (default 60), skipped when the signature is
  unchanged (`live_trader.py:470-491`). (The per-position `ptrace.heartbeat`,
  `live_trader.py:581`, sinks to the **log**, not Discord.)

**PREFLIGHT / STARTUP / BOOT**
- 🚀 Startup banner — `card_startup` (`discord_cards.py:494`), posted `live_trader.py:2247`
  (built `:2262`). **Contains drift — see below.**
- `[BOOST-SPEC-V2] ACTIVE` block — `live_trader.py:2279`, only when `boost_spec_v2` on
  (default off ⇒ nothing).
- Module init receipts — `tele.info`, `live_trader.py:309-350`.
- Boot preflight flags — `boost_metrics.py:205` (flags built dynamically from every cfg
  bool, `boost_metrics.py:196` — **no drift, good pattern**).
- Rogue boot card — `card_monster_boot` (`discord_cards.py:201`), `rogue.py:397`.
- Watchdog started — `tele.success`, `watchdog.py:719`; gateway connect card
  `card_connect` (`discord_cards.py:400`) / intent warning `card_intent_warning`
  (`discord_cards.py:406`) on connect.
- The `broker_preflight` summary card (`discord_cards.py`/`broker_preflight.py:630`) is
  **operator-invoked** via `bot.py preflight` — not unprompted.

**EOD**
- 🌙 EOD summary — `card_eod` (`discord_cards.py:368`), `journal.py:244` on the broker-day
  roll.

**FEED-WATCHDOG**
- Feed recovered `live_trader.py:1706`; feed down/blind `live_trader.py:1743`
  (`tele.critical`); reinit attempt/ok `live_trader.py:1766/1781`; self-restart (exit 42)
  `live_trader.py:1806`. (`feed_watchdog.py` posts nothing itself.)

### Hardcoded-vs-config text drift (cards whose text isn't read from live config)

1. **The F-B boot banner (the one you named), confirmed.** `boost_spec.startup_card_line`
   returns the literal ``"Boost mode: `F-B` (trapped-leg hedge at $10 adverse)"``
   (**`boost_spec.py:83`**) whenever `boost_spec_v2` is off — **without ever reading
   `trapped_late_rescue_enabled`**. It is appended to the 🚀 startup banner at
   **`live_trader.py:2254`** (posted `:2247`, every boot). Meanwhile F-B is gated off by
   default (`trapped_late_rescue_enabled=False`, `config.py:181`; the fire site
   `fills.py:704` never triggers). So every boot the banner asserts a $10 trapped-leg hedge
   is armed while it is disabled. The "$10" is `trapped_rescue_arm_dollars` and the real
   stop would be `trapped_rescue_sl_dollars=13` — both are literals in the string, neither
   read from cfg.

2. **Its `/status` + `/engines` twin.** `boost_spec.boost_mode_line`
   (**`boost_spec.py:76`**) returns the same literal string, fed into `status.json`
   (`live_trader.py:663`) and the `/engines` card "Boost mode" field
   (`live_trader.py:1596`). Same root cause: it distinguishes only `SPEC_V2` vs `F-B` and
   never consults `trapped_late_rescue_enabled`. The selftests **enshrine** this —
   `selftest.py:9618` asserts `'F-B' in boost_mode_line(off)`, `:9642` the startup twin —
   so the drift is locked in, not caught.

3. **Startup banner "Ladder" line.** The banner text (`live_trader.py:2252`) and the
   `card_startup` `ladder` arg (`live_trader.py:2267`) pass the literal
   `"5>BE | 6>+4 | 10>peak-2"`, not derived from cfg. It matches today's (also hardcoded)
   tier thresholds in `fills.py:456-460`, so it is internally consistent now but would
   drift silently if either side were tuned.

4. **Startup banner boost line ungated by enable flags.** The "Boost RALLY … RESCUE …"
   banner line (`live_trader.py:2255`) and the `card_startup` `boost_sl` field
   (`live_trader.py:2268`) are emitted with **no check of `rally_boosts_enabled` /
   `rescue_boost_v2_enabled`** (`config.py:179`/`:31`). The $ values are live cfg reads,
   but if either enable flag were off the banner would still advertise the boosts as
   active — the same class of drift as #1.

5. **`v3.2.0` boot receipt.** `live_trader.py:346-350` states `"$10 SL + $3.50 trail;
   -$700 hard cap"` as literals in a static version receipt. Matches current defaults but
   is not cfg-driven (lower severity — a historical receipt, not a live-state claim).

---

## PART 5 — Gaps

**Commands registered but with no handler / handlers never registered.**
- `today_summary` is a **registered handler with no producer.** The bot dispatches
  `cmd=="today_summary"` → `_send_today_summary` (`live_trader.py:775-776` →
  `journal.py:248`), but **nothing anywhere queues `today_summary`** (no
  `_write_command("today_summary")` exists). `/today` is answered entirely in the watchdog
  from CSV (`watchdog.py:680-681`, `_format_today_summary` `watchdog.py:582`). So the
  bot-side `today_summary` path is **dead/unreachable via Discord**.
- Every entry in `ALLOWED_COMMANDS` (`watchdog.py:94-104`) has a live handler in
  `_handle_command`; the queued ones all have a matching arm in `_handle_commands`. No
  orphaned *command names* were found.

**Help text vs reality.**
- `HELP_TEXT` (`watchdog.py:107-131`) omits `/start` — it exists (alias of `/help`,
  `watchdog.py:609`) but is undocumented. Minor.
- Everything listed in `HELP_TEXT` maps to a real command; no phantom commands in help.
- Nothing in `HELP_TEXT` claims a lot argument for `/testfire`/seeds — consistent with the
  code (none accept args).

**Runtime config mutations that a restart can silently revert.** (You flagged this as
having cost a day's P&L.)
- **`/pause`** — `self.paused` is **not persisted** (`live_trader.py:279` re-inits to
  `False`; only mirrored to the display-only `status.json`, `:648`). A restart of a paused
  bot **silently resumes trading.** This is the clearest instance of the failure mode you
  described.
- **`/anchors|/rogue|/fetcher on|off`** — *does* persist for a **same-day** restart
  (`p1_state.save`, `live_trader.py:1033`; persisted wins on boot, `live_trader.py:285-286`)
  but is **discarded on a new broker day** (`p1_state.py:270-273`) → reverts to
  `config.py` defaults (`non_oco_enabled`/`rogue_enabled`/`fetcher_enabled`,
  `live_trader.py:290-292`). A switch you flipped yesterday is back to the config default
  today.
- **`/daylock anchors off` / `/daylock off`** — persists same-day via `state.json`
  (day-scoped keys, `daystops.py:44-46`), cleared on a new day (by design).
- None of these are written back into `config.py`; all are in-memory/state-file only, so no
  runtime toggle changes the on-disk defaults.

**Who can reach these commands (author / channel check).**
- Both checks live in `on_message` (`discord_client.py:312-320`):
  - **Channel gate (always on):** `if str(message.channel.id) != str(self.cfg.channel_id):
    return` (`discord_client.py:316`). Commands only work in the one configured channel.
  - **Author gate (conditional):** `if (self.cfg.allowed_user_ids and
    str(message.author.id) not in self.cfg.allowed_user_ids): return`
    (`discord_client.py:318-320`). The `allowed_user_ids and …` short-circuit means that
    **if `DISCORD_ALLOWED_USER_IDS` is empty, the per-user check is skipped entirely** —
    *any* user who can post in the channel can run every command, including `/flatten`.
  - `allowed_user_ids` is built from `DISCORD_ALLOWED_USER_IDS`
    (`discord_client.py:118-119`), and that variable is **commented out by default**
    (`.env.example:24`). So out of the box the only barrier between an arbitrary channel
    member and `/flatten` is channel membership. Bot/self messages are ignored
    (`discord_client.py:314`).
- There is **no per-command authorization** (no command is restricted to a stricter set of
  users than any other); the gate is all-or-nothing at the channel/allowlist level.

**Other notable isolation/consistency gaps (documentation, not fixes).**
- `rescue_boost_v2_enabled` **defaults `True`** (`config.py:31`) while
  `rescue_boost.py:32` documents it as "OFF by default" — a code/comment contradiction
  that also drives the `/testfire` isolation leak in Part 3(c).
- `/testfire`'s No-OCO sibling and any rescue-boost legs it spawns have **no `TF_`-aware
  cleanup** (Part 3(b)-(d)); they are cleared only by fill, day-order expiry, or a manual/
  EOD flatten.
