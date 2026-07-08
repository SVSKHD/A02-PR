#!/usr/bin/env python3
"""
AUREON v2 — Watchdog.

Parent supervisor for the trading bot. Responsibilities:

  1. Spawn bot.py as a subprocess
  2. Monitor heartbeat (file mtime in run dir)
  3. Auto-restart on crash, with exponential backoff
  4. Listen to Discord commands and translate them to:
     - Direct actions (/restart, /stop, /status)
     - Commands forwarded to bot via shared file (/flatten, /pause, /resume)

Commands
--------
  /status     — current state (positions, daily P&L, kill switch)
  /restart    — graceful bot restart (watchdog handles)
  /stop       — graceful shutdown of watchdog + bot
  /flatten    — close every open position immediately (emergency)
  /pause      — stop placing new anchor orders (existing positions keep trailing)
  /resume     — resume anchor processing
  /today      — today's trade summary
  /help       — list of commands

Inter-process communication
---------------------------
  run/heartbeat              — bot touches this file every loop iteration
  run/status.json            — bot writes current state every 30 seconds
  run/commands.json          — watchdog appends; bot consumes & removes

Usage
-----
  # Make sure MT5 terminal is RUNNING AND LOGGED IN on this machine first.
  export DISCORD_BOT_TOKEN="..."
  export DISCORD_CHANNEL_ID="987654321"
  python watchdog.py paper
  python watchdog.py live --i-understand-the-risks
  python watchdog.py backtest --csv data.csv   # also supervises backtest if you like
"""

import argparse
import json
import logging
import os
import signal
import subprocess
try:
    from version import __version__ as AUREON_VERSION
except ImportError:
    AUREON_VERSION = '?'
import sys
import time
from typing import Optional, Dict

from telemetry import telemetry_from_env, Severity
import discord_cards as dc  # v3.1.2: rich /status card


# ============================================================================
# Constants
# ============================================================================

HEARTBEAT_STALE_SECONDS = 180        # bot is unhealthy if heartbeat older than this
CRASH_BACKOFF_BASE      = 5          # 5s, 10s, 20s, ... up to MAX
CRASH_BACKOFF_MAX       = 600        # cap backoff at 10 minutes
MAX_CONSECUTIVE_CRASHES = 8          # give up after 8 in a row
CRASH_RESET_AFTER_S     = 600        # 10 min of stability = forget crash/self-restart counts

# Exit-code relaunch policy (Fix 4 / E-12 L3): the ONLY exit code that triggers an
# auto-relaunch is 42 (the bot's controlled feed self-restart). Any other exit code
# (crash, clean /stop, clock-drift abort) STOPS the watchdog + alerts -- a crashing bot
# must never crash-loop and spam-place orders on each boot.
FEED_SELFRESTART_EXIT_CODE   = 42    # bot's controlled feed-death self-restart (sys.exit(42))
FEED_RESTART_PAUSE_S         = 5     # brief settle before relaunching on a 42 exit
MAX_CONSECUTIVE_SELFRESTARTS = 12    # runaway 42-loop guard (feed unrecoverable -> stop + alert)


def relaunch_policy(exit_code, consecutive_selfrestarts,
                    max_selfrestarts=MAX_CONSECUTIVE_SELFRESTARTS):
    """PURE exit-code relaunch policy (Fix 4 / E-12 Level 3). Returns:
      'relaunch'     -> exit 42 (controlled feed self-restart) under the runaway cap.
      'stop_runaway' -> exit 42 but the self-restart has looped >= max_selfrestarts with no
                        stable run in between (feed looks unrecoverable) -> stop for a human.
      'stop'         -> ANY OTHER exit code (crash / clean /stop / clock-drift abort exit 0)
                        -> do NOT relaunch.
    Only exit 42 ever relaunches; everything else stops so a crashing bot can never
    crash-loop and spam-place orders on each boot. `consecutive_selfrestarts` is the count
    INCLUDING the exit being judged (increment before calling)."""
    if exit_code == FEED_SELFRESTART_EXIT_CODE:
        if consecutive_selfrestarts >= max_selfrestarts:
            return 'stop_runaway'
        return 'relaunch'
    return 'stop'
ALLOWED_COMMANDS = {"status","restart","stop","flatten","pause",
                    "resume","today","help","start",
                    # v3.6.0 engine switches (runtime, no restart)
                    # v3.7.0 adds /fetcher (mirrors /rogue)
                    "anchors","rogue","fetcher","engines",
                    # v3.7.1 manual current-tick re-seed (live testing)
                    "rogueseed","fetchseed",
                    # v3.7.3 per-engine daily stops status + overrides
                    "daylock"}


HELP_TEXT = """*AUREON v2 commands*

📊 `/status` — current positions, P&L, kill switch
🔄 `/restart` — graceful bot restart
🛑 `/stop` — shut down watchdog + bot
🚨 `/flatten` — close everything now (emergency)
⏸ `/pause` — stop placing new anchor orders
▶️ `/resume` — resume anchor processing
📈 `/today` — today's trade summary
⚓ `/anchors on|off|status` — anchor engine switch (off = manage-only)
⚓ `/anchors flatten confirm` — close ONLY anchor-magic (20260522) positions
🦏 `/rogue on|off|status` — Rogue engine switch (off = manage-only)
🦏 `/rogue flatten confirm` — close ONLY Rogue-magic (20260626) positions
🪣 `/fetcher on|off|status` — Fetcher engine switch (off = manage-only)
🪣 `/fetcher flatten confirm` — close ONLY Fetcher-magic (20260707) positions
🌱 `/rogueseed` — re-anchor Rogue at the current tick (live testing; DEMO-only)
🌱 `/fetchseed` — re-anchor Fetcher at the current tick (live testing; DEMO-only)
🔒 `/daylock status` — per-engine day P&L vs profit/loss stops + lock state
🔓 `/daylock anchors off` — override the anchors profit lock (loss stop stays)
🔓 `/daylock off` — override the account lock (disabled by default)
⚙️ `/engines status` — all engines' state + open count per magic
❓ `/help` — this message
"""


# ============================================================================
# Watchdog
# ============================================================================

class Watchdog:
    def __init__(self, bot_args, run_dir: str = "./run"):
        self.bot_args = bot_args
        self.run_dir = run_dir
        os.makedirs(run_dir, exist_ok=True)

        self.heartbeat_path = os.path.join(run_dir, "heartbeat")
        self.status_path    = os.path.join(run_dir, "status.json")
        self.commands_path  = os.path.join(run_dir, "commands.json")
        self.daylog_path    = os.path.join(run_dir, "today_trades.csv")

        self.tele = telemetry_from_env(component="AUREON-watchdog")

        self.bot_proc: Optional[subprocess.Popen] = None
        self.shutdown_requested = False
        self.restart_requested = False
        self.consecutive_crashes = 0
        self.consecutive_selfrestarts = 0    # consecutive exit-42 feed self-restarts
        self.last_start_ts = 0.0

        # Auto-deploy (INFRA, default OFF). When ON, poll master, pull+validate,
        # and restart the bot ONLY when the book is flat / at EOD (never mid-trade).
        self.autodeploy_enabled = os.environ.get("AUTODEPLOY_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")
        try:
            self.autodeploy_poll_min = float(os.environ.get("AUTODEPLOY_POLL_MIN", "5"))
        except ValueError:
            self.autodeploy_poll_min = 5.0
        self.deployed_sha_path = os.path.join(run_dir, "deployed_sha.txt")
        self.update_pending = None          # validated sha awaiting a safe apply window
        self._autodeploy_failed_sha = None  # sha that failed validation/merge (don't re-alert/re-pull)
        self._last_autodeploy_poll = 0.0
        self.deployed_sha = self._git_head_sha()

        # Clean stale heartbeat from previous crash
        if os.path.exists(self.heartbeat_path):
            os.remove(self.heartbeat_path)

    # ------------------------------------------------------------------------
    # Subprocess management
    # ------------------------------------------------------------------------

    def _spawn_bot(self):
        env = os.environ.copy()
        env["AUREON_RUN_DIR"] = self.run_dir
        env["PYTHONUNBUFFERED"] = "1"
        argv = [sys.executable, "bot.py"] + self.bot_args
        self.tele.info(f"Spawning bot: `{' '.join(self.bot_args)}`")
        self.bot_proc = subprocess.Popen(argv, env=env)
        self.last_start_ts = time.time()
        self.tele.success(f"Bot started (PID {self.bot_proc.pid})")

    def _stop_bot(self, timeout: float = 30.0):
        if not self.bot_proc:
            return
        if self.bot_proc.poll() is not None:
            self.bot_proc = None
            return
        self.tele.info(f"Stopping bot (PID {self.bot_proc.pid}) gracefully")
        try:
            self.bot_proc.send_signal(signal.SIGTERM)
            self.bot_proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.tele.warn(f"Bot did not exit within {timeout}s — forcing kill")
            self.bot_proc.kill()
            try:
                self.bot_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        self.bot_proc = None

    # ------------------------------------------------------------------------
    # Auto-deploy (INFRA): pull master always; restart only when flat or at EOD
    # ------------------------------------------------------------------------

    def _run(self, argv, cwd=None, timeout=180):
        """Run a subprocess; return (rc, combined_output). Never raises."""
        try:
            r = subprocess.run(argv, capture_output=True, text=True, cwd=cwd, timeout=timeout)
            return r.returncode, (r.stdout + r.stderr).strip()
        except Exception as e:
            return 1, f"{argv[0]} raised: {e!r}"

    def _git(self, *args, timeout=120):
        return self._run(["git", *args], timeout=timeout)

    def _git_head_sha(self):
        rc, out = self._git("rev-parse", "HEAD")
        return out.split()[0] if rc == 0 and out else None

    def _remote_master_sha(self):
        # Read remote master HEAD WITHOUT modifying the working tree.
        rc, out = self._git("ls-remote", "origin", "refs/heads/master")
        if rc != 0 or not out:
            return None
        return out.split()[0]  # "<sha>\trefs/heads/master"

    def _autodeploy_validate(self, sha):
        """Validate the fetched sha in an ISOLATED git worktree BEFORE applying:
        py_compile all .py + an import smoke. A broken merge must never take down
        the live bot. Returns (ok, detail). Never raises."""
        import glob as _glob, tempfile as _tf, shutil as _sh
        wt = _tf.mkdtemp(prefix="aureon_stage_")
        try:
            rc, out = self._git("worktree", "add", "--detach", wt, sha)
            if rc != 0:
                return False, f"worktree add failed: {out[:200]}"
            pyfiles = _glob.glob(os.path.join(wt, "*.py"))
            rc, out = self._run([sys.executable, "-m", "py_compile", *pyfiles])
            if rc != 0:
                return False, f"py_compile failed: {out[:300]}"
            mods = ("bot live_trader watchdog config strategy mt5_adapter backtest "
                    "state risk anchors fills trails journal utils firebase_journal "
                    "telemetry env_loader version").split()
            rc, out = self._run([sys.executable, "-c", "import " + ", ".join(mods)], cwd=wt)
            if rc != 0:
                return False, f"import test failed: {out[:300]}"
            return True, "py_compile + import OK"
        finally:
            self._git("worktree", "remove", "--force", wt)
            try:
                _sh.rmtree(wt, ignore_errors=True)
            except Exception:
                pass

    def _autodeploy_apply(self, sha):
        """Apply a validated pending update at a safe window: graceful-stop the
        bot, ff-only merge origin/master into the live dir (NEVER reset --hard
        with the book open; ff-only leaves git-ignored .env / state.json /
        firebase_key.json / logs untouched), record the sha, relaunch. Returns
        (ok, detail)."""
        self._stop_bot()
        rc, out = self._git("merge", "--ff-only", "origin/master")
        if rc != 0:
            # Do NOT force. Relaunch the CURRENT code so the bot is never left down.
            self.tele.warn(
                f"⚠️ auto-deploy: ff-only merge failed, manual intervention needed.\n"
                f"`{out[:300]}`\nRelaunching CURRENT code; update NOT applied.")
            self._spawn_bot()
            return False, "ff-only merge failed"
        new_sha = self._git_head_sha() or sha
        try:
            with open(self.deployed_sha_path, "w") as f:
                f.write(new_sha + "\n")
        except Exception:
            pass
        self.deployed_sha = new_sha
        self._spawn_bot()
        self.tele.success(
            f"✅ auto-deploy: applied master `{new_sha[:10]}`, bot restarted on "
            f"v{AUREON_VERSION} (banner/module receipt confirms what is running).")
        return True, "applied"

    def _autodeploy_check(self):
        """Poll master, validate off-tree, and apply only when the book is FLAT
        or EOD is done (whichever first). Pull/validate are always safe; RESTART
        is gated. Default OFF (AUTODEPLOY_ENABLED). Never raises."""
        if not self.autodeploy_enabled:
            return
        try:
            # Apply gate first: while an update is pending, apply as soon as safe.
            if self.update_pending:
                st = self._read_status() or {}
                if st.get("flat") is True or st.get("eod_done") is True:
                    why = "flat" if st.get("flat") else "EOD"
                    self.tele.info(f"📦 auto-deploy: book {why} — applying pending master "
                                   f"`{self.update_pending[:10]}`.")
                    ok, _ = self._autodeploy_apply(self.update_pending)
                    if not ok:
                        self._autodeploy_failed_sha = self.update_pending
                    self.update_pending = None
                return  # while pending, do nothing else

            now = time.time()
            if (now - self._last_autodeploy_poll) < self.autodeploy_poll_min * 60:
                return
            self._last_autodeploy_poll = now
            remote = self._remote_master_sha()
            if not remote or remote == self.deployed_sha or remote == self._autodeploy_failed_sha:
                return
            rc, out = self._git("fetch", "origin", "master")
            if rc != 0:
                self.tele.warn(f"⚠️ auto-deploy: git fetch failed: `{out[:200]}`")
                return
            ok, detail = self._autodeploy_validate(remote)
            if not ok:
                self._autodeploy_failed_sha = remote
                self.tele.warn(
                    f"⚠️ auto-deploy: new master `{remote[:10]}` FAILED validation, "
                    f"NOT applied, staying on `{(self.deployed_sha or '?')[:10]}`.\n"
                    f"`{detail[:200]}`")
                return
            self.update_pending = remote
            self.tele.info(
                f"📥 auto-deploy: master `{remote[:10]}` pulled + validated — "
                f"will apply at next flat/EOD window.")
        except Exception as e:
            logging.warning(f"auto-deploy check raised (non-fatal): {e}")

    # ------------------------------------------------------------------------
    # Heartbeat & status
    # ------------------------------------------------------------------------

    def _heartbeat_age(self) -> Optional[float]:
        if not os.path.exists(self.heartbeat_path):
            return None
        return time.time() - os.path.getmtime(self.heartbeat_path)

    def _read_status(self) -> Optional[dict]:
        if not os.path.exists(self.status_path):
            return None
        try:
            with open(self.status_path) as f:
                return json.load(f)
        except Exception:
            return None

    def _write_command(self, cmd: str, args: Optional[dict] = None):
        existing = []
        if os.path.exists(self.commands_path):
            try:
                with open(self.commands_path) as f:
                    existing = json.load(f)
            except Exception:
                existing = []
        existing.append({
            "id": f"cmd_{int(time.time()*1000)}",
            "cmd": cmd,
            "args": args or {},
            "ts": time.time(),
        })
        tmp = self.commands_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(existing, f, indent=2)
        os.replace(tmp, self.commands_path)

    # ------------------------------------------------------------------------
    # Command handling
    # ------------------------------------------------------------------------

    def _day_pnl_by_engine_rows(self, status: dict):
        """(label, value) rows for the /status 'Realized P&L by engine (today)' section, built
        from the bot-written day_pnl_by_engine payload -- the SAME realized day P&L the daily
        stops read (_engine_day_pnls; never recomputed here). Signed per-engine realized values
        -- Non-OCO (anchors), Rogue, Fetcher -- then Total (their sum). Returns [] when the
        payload is absent (older bot) so the rest of the card renders unchanged. Guarded."""
        try:
            dpe = status.get("day_pnl_by_engine") or {}
            if not dpe:
                return []

            def _m(v):
                try:
                    f = float(v)
                    return f"{'+' if f >= 0 else '-'}${abs(f):,.2f}"
                except (TypeError, ValueError):
                    return "n/a"

            rows = []
            for key, name in (('anchors', 'Non-OCO (anchors)'), ('rogue', 'Rogue'),
                              ('fetcher', 'Fetcher')):
                e = dpe.get(key) or {}
                rows.append((name, _m(e.get('pnl'))))
            acct = dpe.get('account') or {}
            rows.append(("Total", _m(acct.get('pnl'))))
            return rows
        except Exception:
            return []

    def _status_card(self, status: dict):
        """v3.1.2: /status as a clean field-grid card (account · P&L · positions).
        v3.7.4: + a 'Realized P&L by engine (today)' section -- ADDITIVE, every existing
        field (Account..Heartbeat, incl. the account-wide Realized P&L line) is unchanged."""
        def _money(v):
            try:
                f = float(v)
                return f"{'+' if f >= 0 else '-'}${abs(f):,.2f}"
            except (TypeError, ValueError):
                return "n/a"

        def _price(v):
            try:
                return f"${float(v):,.2f}"
            except (TypeError, ValueError):
                return "—"

        bal = status.get("broker_balance")
        eq = status.get("broker_equity")
        locked = status.get("kill_switch_locked")
        kill_th = status.get("kill_threshold_usd", 0) or 0
        anchors = status.get("anchors_processed_today", []) or []
        hb_age = self._heartbeat_age()
        snap = {}
        if status.get("broker_login"):
            snap["Account"] = f"#{status.get('broker_login')} @ {status.get('broker_server','?')}"
        snap["Balance"] = _price(bal)
        snap["Equity"] = _price(eq)
        if bal is not None and eq is not None:
            snap["Floating P&L"] = _money(eq - bal)
        snap["Realized P&L"] = _money(status.get("daily_pnl_realized", 0))
        snap["Open"] = status.get("open_positions", 0)
        snap["Pending"] = status.get("pending_orders", 0)
        snap["Anchors today"] = " ".join(anchors) if anchors else "none yet"
        snap["Kill switch"] = (("🔴 LOCKED" if locked else "🟢 OK")
                               + (f" (-${kill_th:,.0f})" if kill_th else ""))
        snap["Heartbeat"] = f"{hb_age:.0f}s ago" if hb_age is not None else "none yet"
        # v3.7.4 Realized P&L by engine (today) -- ADDITIVE section: signed per-magic realized
        # day P&L (Non-OCO/Rogue/Fetcher) + Total, from the SAME source the daystops read.
        _rows = self._day_pnl_by_engine_rows(status)
        if _rows:
            snap["── Realized P&L by engine (today) ──"] = ""
            for _k, _v in _rows:
                if _k == "Total":
                    snap["─────────"] = ""
                snap[_k] = _v
        return dc.card_status(snap)

    def _format_status(self, status: dict) -> str:
        if status.get("sleeping"):
            return self._format_sleeping_status(status)
        kill = "🔴 *LOCKED*" if status.get("kill_switch_locked") else "🟢 OK"
        anchors = status.get("anchors_processed_today", [])
        hb_age = self._heartbeat_age()
        hb_str = f"`{hb_age:.0f}s ago`" if hb_age is not None else "`none yet`"

        # Compose live balance lines if available
        lines = []
        login = status.get("broker_login")
        if login:
            lines.append(f"🏦 Account: `#{login}` @ `{status.get('broker_server','?')}`")
        bal = status.get("broker_balance")
        eq  = status.get("broker_equity")
        if bal is not None:
            lines.append(f"💵 Balance: `${bal:,.2f}`  Equity: `${eq:,.2f}`")
            floating = eq - bal
            lines.append(f"📊 Floating P&L: `${floating:+,.2f}`")
        kill_th = status.get("kill_threshold_usd", 0)
        if kill_th:
            kill_pct = status.get("daily_loss_pct", 0) * 100
            lines.append(f"🛑 Kill switch at: `-${kill_th:,.0f}` (`-{kill_pct:.1f}%`)  {kill}")

        lines += [
            f"📅 Broker date: `{status.get('broker_date','?')}`",
            f"📦 Lot: `{status.get('lot_size','?')}`",
            f"💰 Realized P&L: `${status.get('daily_pnl_realized', 0):.2f}`",
            f"📈 Open positions: `{status.get('open_positions', 0)}`",
            f"📋 Pending orders: `{status.get('pending_orders', 0)}`",
            f"⚓ Anchors today: `{len(anchors)}/5`",   # v3.3.8: 5 anchors (A1-A5)
            f"   {', '.join(anchors) if anchors else '(none yet)'}",
            f"💓 Heartbeat: {hb_str}",
        ]
        # v3.7.4 Realized P&L by engine (today) -- ADDITIVE section (signed per-magic + Total).
        _rows = self._day_pnl_by_engine_rows(status)
        if _rows:
            lines.append("📊 *Realized P&L by engine (today):*")
            for _k, _v in _rows:
                if _k == "Total":
                    lines.append("   ─────────────")
                lines.append(f"   {_k}: `{_v}`")
        return "\n".join(lines)

    def _format_sleeping_status(self, status: dict) -> str:
        """💤 weekend/holiday reply: last trading day per-anchor P&L +
        week to date, from the stats the bot embedded in status.json while
        asleep. Fully fail-safe -- a missing/empty stats block still returns
        the sleeping header rather than erroring."""
        def money(v):
            try:
                return f"${float(v):+.2f}"
            except (TypeError, ValueError):
                return "$?"
        # v3.3.6: the live status now writes a resolver-derived next_anchor (Monday
        # A1 = 03:30 broker / 06:00 IST). Fallback mirrors that truth, not the old
        # stale "A1 02:00 broker", for the rare status-without-the-key case.
        next_anchor = status.get("next_anchor", "A1_02h_Asia 03:30 broker / 06:00 IST")
        lines = ["💤 AUREON — sleeping (market closed, auto-resume Monday)",
                 f"Next anchor: {next_anchor}"]
        ws = status.get("weekend_stats") or {}
        last_day = ws.get("last_day") or {}
        week = ws.get("week") or {}
        if not last_day:
            lines += ["", "📊 Stats unavailable (no trades journal yet)."]
            return "\n".join(lines)
        anchors = last_day.get("anchors") or {}
        anchor_str = " · ".join(f"{a} {money(anchors[a])}" for a in sorted(anchors))
        lines += ["",
                  f"📊 Last trading day ({last_day.get('date', '?')}):",
                  f"  {anchor_str or '(no anchors)'}",
                  f"  Day total: {money(last_day.get('total', 0))}"]
        days = week.get("days") or []
        lines += ["", f"📈 Week to date ({week.get('n', len(days))} days):"]
        for entry in days:
            try:
                d, tot = entry
            except (ValueError, TypeError):
                continue
            lines.append(f"  {d}: {money(tot)}")
        lines.append(f"  Week total: {money(week.get('total', 0))}")
        return "\n".join(lines)

    def _format_today_summary(self) -> str:
        if not os.path.exists(self.daylog_path):
            return "No trades yet today."
        try:
            import csv
            with open(self.daylog_path) as f:
                rows = list(csv.DictReader(f))
        except Exception as e:
            return f"Could not read today's trades: {e}"
        if not rows:
            return "No trades yet today."
        total_pnl = sum(float(r["pnl_usd"]) for r in rows)
        wins = sum(1 for r in rows if float(r["pnl_usd"]) > 0)
        sls = sum(1 for r in rows if r["outcome"] == "SL")
        lines = [f"📊 *Today's trades* ({len(rows)} total)",
                 f"P&L: `${total_pnl:+,.2f}` | Wins `{wins}`/{len(rows)} | SLs `{sls}`",
                 ""]
        for r in rows[-10:]:  # last 10
            sign = "✅" if float(r["pnl_usd"]) > 0 else "❌"
            lines.append(f"{sign} `{r['anchor']}` `{r['side']}` "
                         f"→ `${float(r['pnl_usd']):+.0f}` ({r['outcome']})")
        return "\n".join(lines)

    def _handle_command(self, cmd: str, raw_text: str, source: str = "Discord"):
        cmd = cmd.lower().lstrip("/")
        if cmd not in ALLOWED_COMMANDS:
            return
        if cmd == "help" or cmd == "start":
            self.tele.info(HELP_TEXT)
        elif cmd == "status":
            status = self._read_status()
            if status and status.get("sleeping"):
                # weekend/holiday deep-sleep: the payload IS the sleep summary
                self.tele.info(self._format_status(status))
            elif status:
                self.tele.send(self._format_status(status), Severity.INFO,
                               card=self._status_card(status))
            else:
                self.tele.warn("No status available — bot may still be starting")
        elif cmd == "restart":
            self.tele.warn(f"🔄 Restart requested via {source}")
            self.restart_requested = True
        elif cmd == "stop":
            self.tele.warn(f"🛑 Shutdown requested via {source}")
            self.shutdown_requested = True
        elif cmd == "flatten":
            self._write_command("flatten")
            self.tele.warn("🚨 Flatten command queued — bot will close all positions")
        elif cmd == "pause":
            self._write_command("pause")
            self.tele.info("⏸ Pause queued — no new anchor orders until /resume")
        elif cmd == "resume":
            self._write_command("resume")
            self.tele.info("▶️ Resume queued — anchor processing back on")
        elif cmd == "rogueseed":
            # v3.7.1 manual Rogue re-seed: the watchdog only PARSES + queues; the bot
            # plants the anchor at ITS current tick next tick and replies (DEMO-only;
            # refuses under an open ticket / engine off / market closed / kill switch).
            self._write_command("rogueseed")
            self.tele.info("🌱 `/rogueseed` queued — bot re-anchors Rogue at its current "
                           "tick next tick (or replies with the refusal reason).")
        elif cmd == "fetchseed":
            self._write_command("fetchseed")
            self.tele.info("🌱 `/fetchseed` queued — bot re-anchors Fetcher at its current "
                           "tick next tick (or replies with the refusal reason).")
        elif cmd == "daylock":
            # v3.7.3 /daylock status | anchors off | off. The watchdog only PARSES + queues;
            # the bot renders / applies next tick. 'anchors off' overrides the anchors
            # profit lock; bare 'off' overrides the account lock.
            toks = (raw_text or "").split()[1:]
            sub = toks[0].lower() if toks else "status"
            if sub == "anchors" and len(toks) >= 2 and toks[1].lower() == "off":
                self._write_command("daylock_override", {"which": "anchors"})
                self.tele.info("🔓 `/daylock anchors off` queued — clears the anchors "
                               "profit lock next tick (loss stop stays active).")
            elif sub == "off":
                self._write_command("daylock_override", {"which": "account"})
                self.tele.info("🔓 `/daylock off` queued — clears the account lock next tick.")
            elif sub == "status":
                self._write_command("daylock_status")
            else:
                self.tele.info("Usage: `/daylock status` · `/daylock anchors off` · "
                               "`/daylock off`")
        elif cmd == "today":
            self.tele.info(self._format_today_summary())
        elif cmd in ("anchors", "rogue", "fetcher"):
            # v3.6.0 engine switches: /anchors on|off|status|flatten [confirm] ·
            # /rogue on|off|status|flatten [confirm] · v3.7.0 /fetcher on|off|status|
            # flatten [confirm]. The watchdog only PARSES and queues; the bot applies
            # the toggle next tick and posts the confirm embed itself (engines state +
            # open-position count per magic).
            toks = (raw_text or "").split()[1:]
            sub = toks[0].lower() if toks else "status"
            if sub in ("on", "off"):
                self._write_command("engine", {"engine": cmd, "action": sub})
                self.tele.info(f"⚙️ `/{cmd} {sub}` queued — effective next tick "
                               f"(no restart); bot will confirm with both engines' "
                               f"state + open counts per magic.")
            elif sub == "flatten":
                confirm = any(t.lower() == "confirm" for t in toks[1:])
                self._write_command(f"{cmd}_flatten", {"confirm": confirm})
                if confirm:
                    self.tele.warn(f"🚨 `/{cmd} flatten confirm` queued — bot will "
                                   f"close ONLY that engine's magic.")
                else:
                    self.tele.info(f"`/{cmd} flatten` queued — bot will reply with "
                                   f"the open-position count and ask for "
                                   f"`/{cmd} flatten confirm`.")
            elif sub == "status":
                self._write_command("engines_status", {"engine": cmd})
            else:
                self.tele.info(f"Usage: `/{cmd} on|off|status` or "
                               f"`/{cmd} flatten confirm`")
        elif cmd == "engines":
            # v3.6.0: /engines status — both engines' state + open count per magic.
            self._write_command("engines_status", {})

    # ------------------------------------------------------------------------
    # Main supervisor loop
    # ------------------------------------------------------------------------

    def run(self):
        self.tele.success(
            f"🤖 *AUREON Watchdog started*\n"
            f"Bot args: `{' '.join(self.bot_args)}`\n"
            f"Run dir: `{self.run_dir}`\n"
            f"Alerts: Discord (embed cards)\n"
            f"Auto-deploy: `{'ON' if self.autodeploy_enabled else 'off'}`"
            f"{f' (poll {self.autodeploy_poll_min:.0f}m)' if self.autodeploy_enabled else ''}\n"
            f"Send `/help` to see commands."
        )

        # Signal handlers
        def _sigterm(sig, frame):
            self.tele.warn(f"Watchdog received signal {sig}; shutting down")
            self.shutdown_requested = True
        signal.signal(signal.SIGTERM, _sigterm)
        signal.signal(signal.SIGINT,  _sigterm)

        # v3.1.0: Discord is the sole command channel. Start its gateway (own
        # daemon thread; verifies Message Content Intent, posts the connect card).
        _dc = getattr(self.tele, 'discord', None)
        if _dc is not None:
            _dc.start_gateway(self._handle_command)

        # Spawn bot
        self._spawn_bot()

        # Supervisor loop
        try:
            while not self.shutdown_requested:
                time.sleep(5)

                # 1. Crash detection
                if self.bot_proc is None:
                    # Defensive: shouldn't happen, but if restart logic ever leaves
                    # bot_proc unset, respawn rather than crashing the watchdog itself.
                    self.tele.warn("bot_proc unexpectedly None — respawning")
                    self._spawn_bot()
                    continue
                if self.bot_proc.poll() is not None:
                    exit_code = self.bot_proc.returncode
                    runtime = time.time() - self.last_start_ts

                    # A stable run clears both restart counters.
                    if runtime > CRASH_RESET_AFTER_S:
                        self.consecutive_crashes = 0
                        self.consecutive_selfrestarts = 0

                    # RELAUNCH ONLY on the controlled feed self-restart (exit 42, Fix 4 /
                    # E-12 Level 3). The bot persisted its state (E-16) before exiting and
                    # recovers the SAME trading day on boot -- anchors already placed are
                    # SKIPPED and open positions are adopted -- so a 42 relaunch cannot
                    # re-place orders. A short pause keeps a reinit loop from hammering; a
                    # runaway (feed unrecoverable) stops the watchdog for a human. Any other
                    # exit code STOPS the watchdog (no crash-loop). Count 42s BEFORE deciding
                    # so the runaway cap includes this exit.
                    if exit_code == FEED_SELFRESTART_EXIT_CODE:
                        self.consecutive_selfrestarts += 1
                    action = relaunch_policy(exit_code, self.consecutive_selfrestarts)

                    if action == 'relaunch':
                        self.tele.warn(
                            f"🔁 Bot exited 42 (controlled feed self-restart "
                            f"#{self.consecutive_selfrestarts}, ran {runtime:.0f}s) — "
                            f"relaunching in {FEED_RESTART_PAUSE_S}s. State persisted; "
                            f"same-day recovery on boot (no re-placed orders).")
                        time.sleep(FEED_RESTART_PAUSE_S)
                        self._spawn_bot()
                        continue

                    if action == 'stop_runaway':
                        self.tele.critical(
                            f"🚨 *Feed self-restart looped {self.consecutive_selfrestarts}x* "
                            f"(exit 42) with no stable run — feed looks unrecoverable. "
                            f"Watchdog STOPPING; check the MT5 terminal / Market Watch, "
                            f"then restart manually.")
                        break

                    # action == 'stop': ANY other exit code -> do NOT relaunch. A crashing
                    # bot that respawns on every boot re-arms anchors and could spam-place
                    # orders; a clean /stop or a clock-drift abort (exit 0) should also stay
                    # down. Alert loudly and STOP so a human investigates before it trades
                    # again. (Heartbeat-hung + manual /restart below are separate controlled
                    # paths and still restart; only the exit-code path stops here.)
                    self.tele.send(
                        f"🛑 *Bot exited (code `{exit_code}`, ran {runtime:.0f}s) — NOT "
                        f"relaunching.*\nOnly exit 42 (controlled feed self-restart) triggers "
                        f"an auto-relaunch. Any other exit (crash / clean `/stop` / clock-drift "
                        f"abort) stops the watchdog so a crashing bot can never crash-loop and "
                        f"spam-place orders on each boot.\nInvestigate the logs, then restart "
                        f"manually with `python watchdog.py ...`. Open positions remain "
                        f"protected by their broker-side SL.",
                        Severity.CRITICAL)
                    break

                # 2. Heartbeat watchdog
                hb_age = self._heartbeat_age()
                if hb_age is not None and hb_age > HEARTBEAT_STALE_SECONDS:
                    self.tele.error(
                        f"Heartbeat stale ({hb_age:.0f}s old) — bot may be hung. "
                        f"Restarting.")
                    self._stop_bot()
                    time.sleep(2)
                    self._spawn_bot()
                    continue

                # 3. Manual restart
                if self.restart_requested:
                    self.restart_requested = False
                    self._stop_bot()
                    time.sleep(2)
                    self._spawn_bot()
                    self.consecutive_crashes = 0
                    continue

                # 4. Auto-deploy (INFRA, default OFF): pull master always; restart
                #    only when the book is flat or EOD done. Never mid-trade.
                self._autodeploy_check()

        finally:
            self.tele.warn("Watchdog shutting down — stopping bot")
            self._stop_bot()
            self.tele.info("Watchdog stopped")
            self.tele.stop()


# ============================================================================
# CLI
# ============================================================================

def main():
    # Load .env if present
    from env_loader import load_env
    load_env()

    p = argparse.ArgumentParser(description=f"AUREON v{AUREON_VERSION} watchdog supervisor")
    p.add_argument("mode", choices=["backtest", "paper", "live"],
                   help="Forwarded to bot.py as the first arg")
    p.add_argument("--run-dir", default="./run",
                   help="Directory for heartbeat, status, commands (default ./run)")
    # Forward-through args for bot.py
    p.add_argument("--csv")
    p.add_argument("--start"); p.add_argument("--end")
    p.add_argument("--output-dir")
    p.add_argument("--lot", type=float)
    p.add_argument("--balance", type=float)
    p.add_argument("--i-understand-the-risks", action="store_true")
    p.add_argument("--log-level", default="INFO")
    args, unknown = p.parse_known_args()

    # Reconstruct bot.py CLI args
    bot_args = [args.mode]
    for k, v in vars(args).items():
        if k in ("mode", "run_dir") or v is None or v is False:
            continue
        flag = "--" + k.replace("_", "-")
        if isinstance(v, bool):
            bot_args.append(flag)
        else:
            bot_args.extend([flag, str(v)])
    bot_args += unknown

    # Use the same logging setup as bot.py but with app_name='watchdog'
    # so we get logs/watchdog.log alongside logs/aureon.log
    from bot import setup_logging
    setup_logging(level=args.log_level, log_dir="./logs", app_name="watchdog")

    Watchdog(bot_args, run_dir=args.run_dir).run()


if __name__ == "__main__":
    main()