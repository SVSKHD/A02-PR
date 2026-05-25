#!/usr/bin/env python3
"""
AUREON v2 — Watchdog.

Parent supervisor for the trading bot. Responsibilities:

  1. Spawn bot.py as a subprocess
  2. Monitor heartbeat (file mtime in run dir)
  3. Auto-restart on crash, with exponential backoff
  4. Listen to Telegram commands and translate them to:
     - Direct actions (/restart, /stop, /status)
     - Commands forwarded to bot via shared file (/flatten, /pause, /resume)

Telegram commands
-----------------
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
  export AUREON_TELEGRAM_TOKEN="123:abc..."
  export AUREON_TELEGRAM_CHAT="987654321"
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
import sys
import threading
import time
from typing import Optional, Dict

import requests

from telemetry import telemetry_from_env, Severity


# ============================================================================
# Constants
# ============================================================================

HEARTBEAT_STALE_SECONDS = 180        # bot is unhealthy if heartbeat older than this
CRASH_BACKOFF_BASE      = 5          # 5s, 10s, 20s, ... up to MAX
CRASH_BACKOFF_MAX       = 600        # cap backoff at 10 minutes
MAX_CONSECUTIVE_CRASHES = 8          # give up after 8 in a row
CRASH_RESET_AFTER_S     = 600        # 10 min of stability = forget crash count
TELEGRAM_POLL_TIMEOUT   = 30         # long-poll seconds
ALLOWED_COMMANDS = {"status","restart","stop","flatten","pause",
                    "resume","today","help","start"}


HELP_TEXT = """*AUREON v2 commands*

📊 `/status` — current positions, P&L, kill switch
🔄 `/restart` — graceful bot restart
🛑 `/stop` — shut down watchdog + bot
🚨 `/flatten` — close everything now (emergency)
⏸ `/pause` — stop placing new anchor orders
▶️ `/resume` — resume anchor processing
📈 `/today` — today's trade summary
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
        self.last_start_ts = 0.0

        # Telegram polling
        self.tg_token = os.environ.get("AUREON_TELEGRAM_TOKEN", "").strip()
        self.tg_chat  = os.environ.get("AUREON_TELEGRAM_CHAT",  "").strip()
        self.tg_enabled = bool(self.tg_token and self.tg_chat)

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
    # Telegram command handling
    # ------------------------------------------------------------------------

    def _format_status(self, status: dict) -> str:
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
            f"⚓ Anchors today: `{len(anchors)}/4`",
            f"   {', '.join(anchors) if anchors else '(none yet)'}",
            f"💓 Heartbeat: {hb_str}",
        ]
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

    def _handle_command(self, cmd: str, raw_text: str):
        cmd = cmd.lower().lstrip("/")
        if cmd not in ALLOWED_COMMANDS:
            return
        if cmd == "help" or cmd == "start":
            self.tele.info(HELP_TEXT)
        elif cmd == "status":
            status = self._read_status()
            if status:
                self.tele.info(f"📊 *AUREON Status*\n{self._format_status(status)}")
            else:
                self.tele.warn("No status available — bot may still be starting")
        elif cmd == "restart":
            self.tele.warn("🔄 Restart requested via Telegram")
            self.restart_requested = True
        elif cmd == "stop":
            self.tele.warn("🛑 Shutdown requested via Telegram")
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
        elif cmd == "today":
            self.tele.info(self._format_today_summary())

    def _telegram_polling_loop(self):
        """Long-poll the Telegram bot API for commands. Runs in a daemon thread."""
        if not self.tg_enabled:
            return
        last_update_id = 0
        # Discover existing updates first to skip backlog
        try:
            r = requests.get(f"https://api.telegram.org/bot{self.tg_token}/getUpdates",
                             params={"timeout": 0, "limit": 1, "offset": -1}, timeout=5)
            for upd in r.json().get("result", []):
                last_update_id = upd["update_id"]
        except Exception:
            pass

        backoff = 1.0
        while not self.shutdown_requested:
            try:
                r = requests.get(
                    f"https://api.telegram.org/bot{self.tg_token}/getUpdates",
                    params={"offset": last_update_id + 1,
                            "timeout": TELEGRAM_POLL_TIMEOUT},
                    timeout=TELEGRAM_POLL_TIMEOUT + 5,
                )
                if r.status_code != 200:
                    raise RuntimeError(f"Telegram HTTP {r.status_code}: {r.text[:200]}")
                for upd in r.json().get("result", []):
                    last_update_id = upd["update_id"]
                    msg = upd.get("message") or upd.get("edited_message") or {}
                    chat_id = str(msg.get("chat", {}).get("id", ""))
                    if chat_id != self.tg_chat:
                        continue  # ignore messages from other chats
                    text = (msg.get("text") or "").strip()
                    if not text.startswith("/"):
                        continue
                    cmd = text.split()[0]
                    try:
                        self._handle_command(cmd, text)
                    except Exception as e:
                        self.tele.error(f"Failed to handle `{cmd}`: {e}")
                backoff = 1.0
            except Exception as e:
                logging.warning(f"Telegram polling error: {e}")
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)

    # ------------------------------------------------------------------------
    # Main supervisor loop
    # ------------------------------------------------------------------------

    def run(self):
        self.tele.success(
            f"🤖 *AUREON Watchdog started*\n"
            f"Bot args: `{' '.join(self.bot_args)}`\n"
            f"Run dir: `{self.run_dir}`\n"
            f"Telegram polling: `{'on' if self.tg_enabled else 'off'}`\n"
            f"Send `/help` to see commands."
        )

        # Signal handlers
        def _sigterm(sig, frame):
            self.tele.warn(f"Watchdog received signal {sig}; shutting down")
            self.shutdown_requested = True
        signal.signal(signal.SIGTERM, _sigterm)
        signal.signal(signal.SIGINT,  _sigterm)

        # Telegram polling thread
        if self.tg_enabled:
            t = threading.Thread(target=self._telegram_polling_loop,
                                 name="telegram-poll", daemon=True)
            t.start()

        # Spawn bot
        self._spawn_bot()

        # Supervisor loop
        try:
            while not self.shutdown_requested:
                time.sleep(5)

                # 1. Crash detection
                if self.bot_proc.poll() is not None:
                    exit_code = self.bot_proc.returncode
                    runtime = time.time() - self.last_start_ts

                    # Stable run resets crash counter
                    if runtime > CRASH_RESET_AFTER_S:
                        self.consecutive_crashes = 0

                    # Code-0 (clean exit) should NOT count as a crash.
                    # Common case: bot exits during weekend/market-closed because
                    # weekend-detection code finishes its checks and returns clean.
                    # We still want to restart it, but without burning the budget.
                    if exit_code == 0 and runtime < 30:
                        self.tele.info(
                            f"Bot clean-exited after {runtime:.0f}s "
                            f"(market probably closed). Restarting in 60s without counting as crash.")
                        time.sleep(60)
                        self._spawn_bot()
                        continue

                    self.consecutive_crashes += 1
                    sev = Severity.CRITICAL if exit_code != 0 else Severity.INFO
                    self.tele.send(
                        f"Bot exited (code `{exit_code}`, ran {runtime:.0f}s, "
                        f"crash #{self.consecutive_crashes})", sev)

                    if self.consecutive_crashes >= MAX_CONSECUTIVE_CRASHES:
                        self.tele.critical(
                            f"🚨 Hit {MAX_CONSECUTIVE_CRASHES} consecutive crashes — "
                            f"giving up. Watchdog exiting. Investigate before restarting.")
                        break

                    backoff = min(CRASH_BACKOFF_BASE * (2 ** (self.consecutive_crashes - 1)),
                                  CRASH_BACKOFF_MAX)
                    self.tele.info(f"Restarting bot in {backoff}s...")
                    time.sleep(backoff)
                    self._spawn_bot()
                    continue

                # 2. Heartbeat watchdog
                hb_age = self._heartbeat_age()
                if hb_age is not None and hb_age > HEARTBEAT_STALE_SECONDS:
                    self.tele.error(
                        f"Heartbeat stale ({hb_age:.0f}s old) — bot may be hung. "
                        f"Restarting.")
                    self._stop_bot()
                    continue

                # 3. Manual restart
                if self.restart_requested:
                    self.restart_requested = False
                    self._stop_bot()
                    time.sleep(2)
                    self._spawn_bot()
                    self.consecutive_crashes = 0
                    continue

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

    p = argparse.ArgumentParser(description="AUREON v2 watchdog supervisor")
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
