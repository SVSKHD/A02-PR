"""AUREON offline SIMULATOR (Part 1B) — drives the REAL LiveTrader tick loop
against a fake broker, over cached ticks, with MT5 disconnected.

!!! GATE-NOT-RUN — baseline never reproduced against MT5 truth.
!!! No number this produces is trustworthy.

DESIGN (no strategy fork; see backtest/SIMULATOR_STATUS.md):
  - FakeMT5 (sim_broker) simulates the broker at TICK resolution. The REAL
    mt5_adapter.MT5Adapter is wrapped around it (its order/reconcile/price logic
    is reused verbatim -- only self.mt5 is fake).
  - LiveTrader is constructed with paper=False (paper mode disables the fill
    reconcile + boost engine -- see the agent finding), so the REAL order path,
    fills reconcile, trails, rogue, fetcher, boost family, daystops governors,
    the 3% kill switch (risk._check_kill_switch, on equity INCL. unrealized), and
    the EOD/Friday flatten all run unchanged against the fake broker.
  - Simulated clock: pandas.Timestamp.now is monkeypatched to the sim tick time
    for the duration of the run (the tick loop reads wall-clock now(), not the
    tick), and the fake adapter's server_time_utc() returns the same sim time.
  - All run/-tree writes are redirected to a sim scratch dir via AUREON_RUN_DIR;
    NOTHING is written under the live run/ (asserted by selftest).
"""
from __future__ import annotations

import os
import sys

_THIS = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS)
for _p in (_ROOT, _THIS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pandas as pd

import sim_broker as _sb


# --------------------------------------------------------------------------- #
# simulated clock (monkeypatches pandas.Timestamp.now for the run)
# --------------------------------------------------------------------------- #
class SimClock:
    def __init__(self, t0):
        self.t = pd.Timestamp(t0)
        if self.t.tzinfo is None:
            self.t = self.t.tz_localize('UTC')

    def set(self, ts):
        t = pd.Timestamp(ts)
        self.t = t.tz_localize('UTC') if t.tzinfo is None else t.tz_convert('UTC')

    def now(self, tz=None):
        if tz is None:
            return self.t.tz_localize(None)
        return self.t.tz_convert(tz)


class _patch_now:
    """Context manager: pandas.Timestamp.now -> clock.now for the run."""
    def __init__(self, clock):
        self.clock = clock
        self._orig = None

    def __enter__(self):
        self._orig = pd.Timestamp.now
        clock = self.clock
        def _now(tz=None):
            return clock.now(tz)
        pd.Timestamp.now = staticmethod(_now)
        return self

    def __exit__(self, *a):
        pd.Timestamp.now = self._orig
        return False


# --------------------------------------------------------------------------- #
# no-op telemetry (no thread, no network)
# --------------------------------------------------------------------------- #
class NoOpTele:
    discord = None
    def _noop(self, *a, **k):
        return None
    info = success = warn = error = critical = debug = send = _noop
    def stop(self, *a, **k):
        return None


# --------------------------------------------------------------------------- #
# fake adapter = REAL MT5Adapter wrapped around FakeMT5 (no adapter logic forked)
# --------------------------------------------------------------------------- #
def build_adapter(broker, cfg):
    from mt5_adapter import MT5Adapter
    adapter = object.__new__(MT5Adapter)     # skip __init__ (offset detection / real MT5)
    adapter.mt5 = _sb.FakeMT5(broker)
    adapter.symbol = cfg.symbol
    adapter.tick_time_offset_hours = float(cfg.broker_tz_offset_hours)
    adapter.expected_offset_hours = float(cfg.broker_tz_offset_hours)
    return adapter


# --------------------------------------------------------------------------- #
# tick source: a day's cached frame -> iterator of tick objects
# --------------------------------------------------------------------------- #
def is_tick_frame(df):
    """True iff `df` is a real QUOTE-TICK sequence (bid/ask columns). An M1 bar
    frame (open/high/low/close) is NOT a tick frame -- the simulator REFUSES to
    run it rather than interpolating invented intrabar prices."""
    return df is not None and len(df) > 0 and 'bid' in df.columns and 'ask' in df.columns


def _ticks_from_frame(df):
    """Yield tick objects (time_utc, bid, ask) from the CACHED tick sequence. This
    is the cached ticks VERBATIM -- never a resampled bar. Refuses (yields nothing)
    for a non-tick frame; callers must have excluded M1 days already."""
    if not is_tick_frame(df):
        return
    for row in df.itertuples(index=False):
        yield _sb._Obj(time_utc=pd.Timestamp(getattr(row, 'time')),
                       bid=float(getattr(row, 'bid')), ask=float(getattr(row, 'ask')))


# --------------------------------------------------------------------------- #
# §3 build-integrity: every simulated leg must carry a comment that classifies.
# --------------------------------------------------------------------------- #
def unclassified_comments(deals):
    """The set of (comment, magic) on OUT deals whose comment does NOT match a
    known AUR_* pattern (engine is None). Per the spec these are BUILD ERRORS, not
    an 'unknown' bucket -- the simulator must never emit a leg pnl_report would
    dump into a phantom '??'/'ext' bucket."""
    import pnl_report as _pr
    bad = set()
    for d in deals:
        if getattr(d, 'entry', None) != 1:
            continue
        c = _pr.classify_comment(getattr(d, 'comment', ''), getattr(d, 'magic', 0))
        if c['engine'] is None:
            bad.add((str(getattr(d, 'comment', '')), int(getattr(d, 'magic', 0) or 0)))
    return bad


# --------------------------------------------------------------------------- #
# the run
# --------------------------------------------------------------------------- #
def simulate(cfg, day_frames, *, scratch_dir, tick_cadence_s=1.0, spread=0.20,
             slippage=0.0, starting_balance=None, progress=None,
             apply_config_timeline=False):
    """Drive the real LiveTrader over `day_frames` (ordered list of (date_str,
    DataFrame)). Returns {'deals': [...], 'closed_positions': int, 'days': [...],
    'run_dir': scratch_dir, 'account': {...}}. Writes engine state ONLY under
    scratch_dir (never the live run/). MT5 disconnected."""
    import importlib
    import live_trader as lt
    import sim_config as _scfg

    _saved_env = {k: os.environ.get(k) for k in
                  ('AUREON_RUN_DIR', 'DISCORD_BOT_TOKEN', 'DISCORD_CHANNEL_ID')}
    os.environ['AUREON_RUN_DIR'] = scratch_dir
    os.makedirs(scratch_dir, exist_ok=True)
    # fresh, scratch state file (paper=False WILL persist state -> keep it in scratch)
    cfg.state_file = os.path.join(scratch_dir, 'state.json')
    # ensure Discord is off (no network)
    os.environ.pop('DISCORD_BOT_TOKEN', None)
    os.environ.pop('DISCORD_CHANNEL_ID', None)

    broker = _sb.FakeBroker(cfg.symbol, cfg, starting_balance=starting_balance,
                            spread=spread, slippage=slippage,
                            broker_tz_offset_hours=cfg.broker_tz_offset_hours)
    adapter = build_adapter(broker, cfg)

    # first tick seeds the clock + broker BEFORE construction (init reads now()
    # and, once, the tick).
    first_ts = None
    first_tick = None
    for _, df in day_frames:
        if df is not None and len(df):
            for tk in _ticks_from_frame(df):
                first_tick = tk; first_ts = tk.time_utc; break
        if first_tick is not None:
            break
    if first_tick is None:
        return {'deals': [], 'closed_positions': 0, 'days': [], 'run_dir': scratch_dir,
                'account': {}, 'empty': True}
    clock = SimClock(first_ts)

    # Baseline = July AS TRADED: reconstruct the config live at the run's first
    # instant BEFORE construction, so self.engines boots to that day's engine set
    # (rogue on / fetcher off before 07-07, etc.). See sim_config.
    if apply_config_timeline:
        _cfg_over, _eng_over, _ = _scfg.active_config(first_ts)
        for _k, _v in _cfg_over.items():
            if hasattr(cfg, _k):
                setattr(cfg, _k, _v)
        for _e, _on in _eng_over.items():
            if hasattr(cfg, _e + '_enabled'):
                setattr(cfg, _e + '_enabled', bool(_on))
        if hasattr(cfg, 'non_oco_enabled'):
            cfg.non_oco_enabled = bool(_eng_over.get('anchors', True))

    _orig_factory = lt.telemetry_from_env
    lt.telemetry_from_env = lambda *a, **k: NoOpTele()
    try:
        with _patch_now(clock):
            broker.advance(first_tick)   # seed the broker's current tick
            trader = lt.LiveTrader(cfg, adapter, paper=False)
            trader.tele = NoOpTele()
            _cp_idx = None
            if apply_config_timeline:
                _scfg.apply_to_trader(trader, first_ts)
                _cp_idx = _scfg.active_index(first_ts)
            # The run() startup path validates the broker time-offset (a LIVE feed-
            # detection step). The simulator's offset is KNOWN and fixed (set on the
            # fake adapter), so it is validated by construction -- flip the gate the
            # scheduler checks. This is sim SETUP, not a strategy change.
            trader.offset_validated = True

            days_seen = []
            refused_days = []
            last_tick_call = None
            for date_str, df in day_frames:
                if df is None or len(df) == 0:
                    continue
                if not is_tick_frame(df):
                    # confirm-1: an M1 (or any non-tick) day is REFUSED, never
                    # interpolated into invented ticks.
                    refused_days.append(date_str)
                    if progress:
                        progress(f"REFUSED {date_str}: not a tick frame (M1) — the "
                                 "simulator does not interpolate intrabar prices")
                    continue
                broker._bars = df
                days_seen.append(date_str)
                for tick in _ticks_from_frame(df):
                    clock.set(tick.time_utc)
                    broker.advance(tick)                 # fills + SL/TP at full tick res
                    tsec = tick.time_utc.timestamp()
                    if last_tick_call is None or (tsec - last_tick_call) >= tick_cadence_s:
                        last_tick_call = tsec
                        # apply the per-day config timeline when a change-point (incl.
                        # the 07-07 14:58 rogue flip) advances -- baseline = as traded.
                        if apply_config_timeline:
                            _i = _scfg.active_index(tick.time_utc)
                            if _i != _cp_idx:
                                _cp_idx = _i
                                _, _, _cites = _scfg.apply_to_trader(trader, tick.time_utc)
                                if progress:
                                    progress(f"config change-point @ {tick.time_utc}: {_cites}")
                        try:
                            trader._tick()               # the REAL per-tick engine
                        except Exception as e:
                            # never let one tick kill the run; record + continue
                            if progress:
                                progress(f"tick error @ {tick.time_utc}: {e!r}")
                # end-of-day: force the real EOD flatten path by advancing the clock
                # past eod and ticking once more with the last price held
                try:
                    trader._tick()
                except Exception:
                    pass
        account = {'balance': broker.balance, 'equity': round(broker.balance + broker.unrealized(), 2),
                   'open_positions': len(broker.positions)}
        deals = list(broker.deals)
        return {'deals': deals, 'closed_positions':
                sum(1 for d in deals if d.entry == _sb.DEAL_ENTRY_OUT),
                'days': days_seen, 'refused_days': refused_days, 'run_dir': scratch_dir,
                'account': account, 'broker': broker,
                # confirm-2: the broker-day the ENGINE rolled to, taken from the
                # injected clock (state['last_broker_date']). If the day-roll used
                # the wall clock it would read today's date, not the sim's.
                'last_broker_date': str((trader.state or {}).get('last_broker_date', '')),
                'build_errors': sorted(unclassified_comments(deals))}
    finally:
        lt.telemetry_from_env = _orig_factory
        for k, v in _saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# --------------------------------------------------------------------------- #
# tick loading: cached per-day (Part 1A) with a LOUD synthetic fallback
# --------------------------------------------------------------------------- #
def _load_day_frames(d_from, d_to, cfg, ticks_dir):
    """[(date_str, frame, resolution)] for each calendar day in the range. Uses the
    committed per-day cache; falls back to a deterministic SYNTHETIC day (LOUD)
    when a day isn't cached -- so the pipeline runs offline, never silently."""
    import importlib.util as _ilu
    tcp = os.path.join(_THIS, 'tick_cache.py')
    _spec = _ilu.spec_from_file_location('aureon_tick_cache_sim', tcp)
    tc = _ilu.module_from_spec(_spec); _spec.loader.exec_module(tc)
    manifest = tc.read_manifest(ticks_dir)
    frames = []
    for day in tc._daterange(d_from, d_to):
        df = tc.load_day(ticks_dir, cfg.symbol, day)
        if df is not None and len(df):
            res = (manifest.get(day, {}) or {}).get('resolution', 'tick')
            frames.append((day, df, res))
        else:
            syn = _synthetic_day(day, cfg)
            if syn is not None and len(syn):
                frames.append((day, syn, 'synthetic'))
    return frames


def _synthetic_day(day, cfg):
    """One deterministic synthetic tick day (from tick_fetcher.synthetic_month_ticks,
    sliced). LOUD-labelled 'synthetic' by the caller."""
    try:
        import importlib.util as _ilu
        tfp = os.path.join(_THIS, 'tick_fetcher.py')
        _spec = _ilu.spec_from_file_location('aureon_tick_fetcher_sim', tfp)
        tf = _ilu.module_from_spec(_spec); _spec.loader.exec_module(tf)
        d = pd.Timestamp(day)
        month = tf.synthetic_month_ticks(d.year, d.month,
                                         broker_tz_offset_hours=cfg.broker_tz_offset_hours)
        lo = pd.Timestamp(day, tz='UTC') - pd.Timedelta(hours=cfg.broker_tz_offset_hours)
        hi = lo + pd.Timedelta(days=1)
        sub = month[(month['time'] >= lo) & (month['time'] < hi)].reset_index(drop=True)
        return sub
    except Exception:
        return None


def run_cli(d_from, d_to, *, run_id=None, ticks_dir=None, tick_cadence_s=1.0):
    """`python bot.py simulate --from D1 --to D2`. Runs the offline sim over the
    cached ticks, writes sim/reports/<run-id>/, runs THE GATE, prints everything
    with the GATE-NOT-RUN header. Returns 0 iff the gate passed (it cannot on
    synthetic/M1 data), else 1."""
    import tempfile
    import sim_report as srep
    import sim_gate as sgate
    from config import Config
    cfg = Config()
    cfg.util_daily_pnl_report = False
    if ticks_dir is None:
        ticks_dir = os.path.join(_THIS, 'ticks')
    if not (d_from and d_to):
        print("simulate: --from and --to (YYYY-MM-DD) are required")
        return 2
    run_id = run_id or f"{d_from}_{d_to}"
    frames = _load_day_frames(d_from, d_to, cfg, ticks_dir)
    if not frames:
        print("simulate: no ticks for the range (cache empty and synthetic fallback "
              "produced nothing) — run `bot.py fetchticks` on the VPS first.")
        return 2
    resolutions = {res for _, _, res in frames}
    all_tick = (resolutions == {'tick'})

    scratch = tempfile.mkdtemp(prefix='aureon_sim_state_')
    day_frames = [(d, df) for d, df, _ in frames]
    # BASELINE = July AS TRADED: reconstruct each day's config from the D-series
    # (sim_config). Without this the sim runs today's config and cannot match July.
    res = simulate(cfg, day_frames, scratch_dir=os.path.join(scratch, 'st'),
                   tick_cadence_s=tick_cadence_s, apply_config_timeline=True)
    deals = res.get('deals', [])
    day_list = [d for d, _, _ in frames]

    day_list = res.get('days', day_list)   # only days that actually ran (M1 refused)
    out_dir, _reports, summ = srep.write_reports(run_id, deals, cfg, day_list)
    export_path = _find_deal_export(ticks_dir)
    gate = sgate.run_gate(deals, deal_export_path=export_path, resolution_all_tick=all_tick,
                          refused_days=res.get('refused_days'),
                          build_errors=res.get('build_errors'))
    # persist the gate verdict alongside the reports
    with srep.sc.open_sim_file(os.path.join(out_dir, 'GATE.txt'), 'w') as f:
        f.write(sgate.render_gate(gate) + "\n")

    print(srep.sc.gate_header())
    print()
    print(f"AUREON OFFLINE SIM — run {run_id}  ({len(day_list)} day(s) ran)")
    print("resolution per day: " + ", ".join(f"{d}={r}" for d, _, r in frames))
    if res.get('refused_days'):
        print("⚠  REFUSED (M1/non-tick, NOT interpolated): " + ", ".join(res['refused_days']))
    if res.get('build_errors'):
        print(f"‼  BUILD ERROR — {len(res['build_errors'])} non-classifying comment(s): "
              f"{res['build_errors'][:5]}")
    print()
    print(sgate.render_gate(gate))
    print()
    print(f"reports written -> {out_dir}")
    # exit 0 only on a real PASS; refused / build-error / mismatch all non-zero.
    return 0 if gate['passed'] else 1


def _find_deal_export(ticks_dir):
    """Locate a committed MT5 deal export (…/deal_export*.csv under backtest/ or
    its ticks dir). Returns a path or None."""
    cands = []
    for base in (os.path.dirname(ticks_dir), ticks_dir, _THIS):
        if not os.path.isdir(base):
            continue
        for fn in os.listdir(base):
            low = fn.lower()
            if low.endswith('.csv') and ('deal' in low or 'export' in low or 'truth' in low):
                cands.append(os.path.join(base, fn))
    return cands[0] if cands else None
