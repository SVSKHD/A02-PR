"""
AUREON — persistent state load/save + single-instance PID lock (StateMixin).

Atomic state write with a rolling .bak; corruption-tolerant load (main -> .bak ->
fresh); refuses to start a second instance against the same account. All
persisted keys (shadow_positions_extended, shadow_pendings_extended, daily_pnl,
last_broker_date, day_start_equity, ...) keep their exact names — Saturday's live
state.json must rehydrate unchanged.

Methods extracted verbatim from live_trader.py (v3.0.0 refactor). Mixed into
LiveTrader; method bodies are byte-identical (self.* references unchanged).
"""

import json
import logging
import os
from typing import Dict

log = logging.getLogger("AUREON")


class StateMixin:
    def _load_state(self) -> Dict:
        # v2.5: try main state, then .bak fallback, then fresh
        for path, label in [(self.state_path, "main"),
                            (self.state_path + ".bak", "backup")]:
            if os.path.exists(path):
                try:
                    with open(path) as f:
                        s = json.load(f)
                    log.info(f"Restored state from {label}: {path}")
                    return s
                except Exception as e:
                    log.warning(f"State {label} corrupt ({e}); trying next source")
        log.warning("No usable state file; starting fresh")
        return {
            'daily_pnl': 0.0,
            'last_broker_date': None,
            'processed_anchors_today': [],
            'kill_switch_locked': False,
            'day_start_equity': None,  # v2.5.4: today's opening equity — kill baseline
            'shadow_positions_extended': {},  # v2.5: persisted max_fav/fill_time per ticket
        }

    def _save_state(self):
        if self.paper:
            return
        # v2.5: atomic write + rolling .bak backup
        tmp = self.state_path + '.tmp'
        bak = self.state_path + '.bak'
        # Mirror in-memory shadow lock state into the dict before writing
        try:
            self.state['shadow_positions_extended'] = {
                str(ticket): {
                    'max_fav':   shadow.get('max_fav'),
                    'fill_time': shadow.get('fill_time'),
                    'role':      shadow.get('role', 'normal'),
                    'current_sl': shadow.get('current_sl'),
                    'side':       shadow.get('side'),
                    'entry_price': shadow.get('entry_price'),
                    'anchor_label': shadow.get('anchor_label'),
                }
                for ticket, shadow in self.shadow_positions.items()
            }
        except Exception as e:
            log.warning(f"Could not snapshot shadow_positions to state: {e}")
        # v2.9.8: persist PENDINGS too -- a restart between placement and fill
        # previously orphaned them (fills undetected / rescue flag lost).
        try:
            self.state['shadow_pendings_extended'] = {
                str(tk): {
                    'anchor_label':   p.get('anchor_label'),
                    'side':           p.get('side'),
                    'sibling_ticket': p.get('sibling_ticket'),
                    'entry_price':    p.get('entry_price'),
                    'rescue_on_fill': bool(p.get('rescue_on_fill', False)),
                }
                for tk, p in self.shadow_pendings.items()
                if not isinstance(tk, str)
            }
        except Exception as e:
            log.warning(f"Could not snapshot shadow_pendings to state: {e}")
        # Copy current main → .bak before overwriting
        if os.path.exists(self.state_path):
            try:
                import shutil
                shutil.copyfile(self.state_path, bak)
            except Exception:
                pass  # backup failure is not fatal
        with open(tmp, 'w') as f:
            json.dump(self.state, f, indent=2, default=str)
        os.replace(tmp, self.state_path)

    def _acquire_pid_lock(self):
        """v2.5: Refuse to start if another bot instance is already running."""
        import psutil
        if os.path.exists(self.pid_lock_path):
            try:
                with open(self.pid_lock_path) as f:
                    other_pid = int(f.read().strip())
                if psutil.pid_exists(other_pid):
                    # Verify it's actually a python process running this bot
                    try:
                        p = psutil.Process(other_pid)
                        cmdline = " ".join(p.cmdline()).lower()
                        if "aureon" in cmdline or "live_trader" in cmdline or "bot.py" in cmdline:
                            raise RuntimeError(
                                f"Another AUREON bot is already running (PID {other_pid}). "
                                f"Refusing to start a second instance — they would conflict on "
                                f"magic number 20260522 and OCO sibling tracking. "
                                f"Kill the other instance first: taskkill /F /PID {other_pid}"
                            )
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass  # stale lock, safe to take it
                # else: stale lock, fall through and take it
            except (ValueError, OSError):
                pass  # malformed lock, take it
        with open(self.pid_lock_path, 'w') as f:
            f.write(str(os.getpid()))
        log.info(f"PID lock acquired: {self.pid_lock_path} = {os.getpid()}")

    def _release_pid_lock(self):
        try:
            if os.path.exists(self.pid_lock_path):
                with open(self.pid_lock_path) as f:
                    locked_pid = int(f.read().strip())
                if locked_pid == os.getpid():
                    os.remove(self.pid_lock_path)
                    log.info("PID lock released")
        except Exception as e:
            log.warning(f"Could not release PID lock: {e}")
