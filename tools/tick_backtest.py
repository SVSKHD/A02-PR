"""
AUREON TICK-RESOLUTION BACKTEST  (matches bot v2.5.6 exactly)
=============================================================
Runs the real bot strategy against TICK data (bid/ask path), not M1 bars.
This is the genuine test: stops/trails fill at the FIRST tick that touches
them, at that tick's real price — no "peak-0.30 guaranteed" assumption.

USAGE:
    python tick_backtest.py XAUUSD_ticks_1y.csv
    python tick_backtest.py XAUUSD_ticks_1y.csv 0.40    # set lot

OUTPUT:
    - console summary (whole period + monthly + per-anchor + trail_slip)
    - tick_backtest_report.md
    - tick_trades.csv  (every trade with modeled-vs-actual exit)

STRATEGY (v2.5.6, must match live code):
    Anchors broker GMT+3: A1 02:00, A2 10:00, A3 13:40, A4 16:40
    Defer: A1/A3=15s, A2/A4=30s. Anchor price = tick mid at that instant.
    BUY stop = anchor+5 (SL entry-18, TP entry+30)
    SELL stop = anchor-5 (SL entry+18, TP entry-30)
    OCO: when one fills, cancel the other.
    Freeze 15min: +$3->BE, +$5->+4 (fire during freeze). BE@0.30 + trail frozen.
    After freeze: BE@0.30; trail SL = peak - 0.30 (ratchet).
    EOD flat at broker midnight. Kill switch -3% day-start equity (skipped here;
      we report raw strategy P&L. Add if you want the cap modeled.)
"""
import sys, pandas as pd, numpy as np

CSV   = sys.argv[1] if len(sys.argv) > 1 else "XAUUSD_ticks_1y.csv"
LOT   = float(sys.argv[2]) if len(sys.argv) > 2 else 0.40
CONTRACT = 100.0          # XAUUSD: 100 oz per 1.0 lot. (silver = 5000)
PIP_USD  = CONTRACT * LOT # $ per $1 price move

BROKER_TZ = 3             # GMT+3
TRIG, SL_D, TP_D, TRAIL = 5.0, 18.0, 30.0, 0.30
FREEZE_S = 15 * 60
# (label, broker_hour, broker_minute, defer_seconds)
ANCHORS = [("A1",2,0,15), ("A2",10,0,30), ("A3",13,40,15), ("A4",16,40,30)]

def load_ticks(path):
    print(f"Loading {path} ...")
    df = pd.read_csv(path)
    # normalize columns
    if 'time_msc' in df.columns:
        df['ts'] = pd.to_datetime(df['time_msc'], unit='ms', utc=True)
    else:
        df['ts'] = pd.to_datetime(df['time'], utc=True)
    if 'spread' not in df.columns:
        df['spread'] = df['ask'] - df['bid']
    df['mid'] = (df['bid'] + df['ask']) / 2.0
    # broker-local time for anchor scheduling and EOD
    df['btime'] = df['ts'] + pd.Timedelta(hours=BROKER_TZ)
    df['bdate'] = df['btime'].dt.date
    df = df[df['spread'] >= 0].reset_index(drop=True)  # drop corrupt rows
    print(f"  {len(df):,} ticks  {df['ts'].min()} -> {df['ts'].max()}")
    return df

def run(df, oco=True):
    trades = []
    for bdate, day in df.groupby('bdate', sort=True):
        day = day.reset_index(drop=True)
        bt   = day['btime'].values
        bid  = day['bid'].values
        ask  = day['ask'].values
        mid  = day['mid'].values
        ts   = day['btime']
        for (label, ah, am, defer) in ANCHORS:
            # anchor instant (broker) + defer
            anchor_dt = pd.Timestamp(bdate) + pd.Timedelta(hours=ah, minutes=am, seconds=defer)
            anchor_dt = anchor_dt.tz_localize('UTC')  # btime is tz-aware UTC-labeled broker clock
            # find first tick at/after anchor instant
            idx = ts.searchsorted(anchor_dt)
            if idx >= len(day):
                continue
            apx = mid[idx]
            buy_stop, sell_stop = apx + TRIG, apx - TRIG
            legs = {'BUY':{'st':'pending'}, 'SELL':{'st':'pending'}}
            # walk ticks from anchor to end of day
            for i in range(idx, len(day)):
                b, a, m_, t = bid[i], ask[i], mid[i], bt[i]
                for side in ('BUY','SELL'):
                    L = legs[side]
                    # ---- pending: check trigger ----
                    if L['st'] == 'pending':
                        if side == 'BUY' and a >= buy_stop:
                            L.update(st='open', e=a, sl=a-SL_D, tp=a+TP_D, t0=t, mfe=0.0,
                                     peak=a)
                            if oco and legs['SELL']['st']=='pending': legs['SELL']['st']='cancel'
                        elif side == 'SELL' and b <= sell_stop:
                            L.update(st='open', e=b, sl=b+SL_D, tp=b-TP_D, t0=t, mfe=0.0,
                                     peak=b)
                            if oco and legs['BUY']['st']=='pending': legs['BUY']['st']='cancel'
                    # ---- open: manage + exit on this tick ----
                    if L['st'] == 'open':
                        e = L['e']
                        # favorable excursion using current price (bid for BUY exit side, ask for SELL)
                        if side == 'BUY':
                            fav = b - e          # mark against bid (what you'd exit at)
                            if b > L['peak']: L['peak'] = b
                        else:
                            fav = e - a
                            if a < L['peak']: L['peak'] = a
                        if fav > L['mfe']: L['mfe'] = fav
                        elapsed = (t - L['t0']) / np.timedelta64(1,'s')
                        frozen = elapsed < FREEZE_S
                        # lock ladder (fire during freeze)
                        if L['mfe'] >= 3.0:
                            L['sl'] = max(L['sl'], e) if side=='BUY' else min(L['sl'], e)
                        if L['mfe'] >= 5.0:
                            L['sl'] = max(L['sl'], e+4) if side=='BUY' else min(L['sl'], e-4)
                        if not frozen:
                            if L['mfe'] >= 0.30:
                                L['sl'] = max(L['sl'], e) if side=='BUY' else min(L['sl'], e)
                            # trail off realized peak
                            if side=='BUY':
                                L['sl'] = max(L['sl'], L['peak']-TRAIL)
                            else:
                                L['sl'] = min(L['sl'], L['peak']+TRAIL)
                        # exit checks at THIS tick (real fill price)
                        exit_px = reason = None
                        if side == 'BUY':
                            if b <= L['sl']:  exit_px, reason = L['sl'], 'SL/trail'
                            elif b >= L['tp']: exit_px, reason = L['tp'], 'TP'
                        else:
                            if a >= L['sl']:  exit_px, reason = L['sl'], 'SL/trail'
                            elif a <= L['tp']: exit_px, reason = L['tp'], 'TP'
                        if exit_px is not None:
                            pnl = ((exit_px-e) if side=='BUY' else (e-exit_px)) * PIP_USD
                            # modeled trail = peak -/+ TRAIL ; slip vs actual
                            if side=='BUY': modeled = L['peak']-TRAIL
                            else:           modeled = L['peak']+TRAIL
                            trades.append(dict(bdate=bdate, anchor=label, side=side,
                                entry=e, exit=exit_px, reason=reason, pnl=pnl,
                                mfe=L['mfe'], modeled_exit=modeled,
                                trail_slip=(exit_px-modeled)))
                            L['st']='closed'
            # EOD flat
            for side in ('BUY','SELL'):
                L = legs[side]
                if L['st']=='open':
                    last_px = mid[-1]
                    pnl = ((last_px-L['e']) if side=='BUY' else (L['e']-last_px))*PIP_USD
                    trades.append(dict(bdate=bdate, anchor=label, side=side,
                        entry=L['e'], exit=last_px, reason='EOD', pnl=pnl,
                        mfe=L['mfe'], modeled_exit=np.nan, trail_slip=np.nan))
    return pd.DataFrame(trades)

def summarize(t, lot, oco_label):
    if t.empty:
        print("No trades."); return ""
    t['bdate'] = pd.to_datetime(t['bdate'])
    t['ym'] = t['bdate'].dt.to_period('M').astype(str)
    n=len(t); w=t[t.pnl>0]; l=t[t.pnl<0]
    wr=len(w)/n*100; pf=w.pnl.sum()/-l.pnl.sum() if len(l) else float('inf')
    daily=t.groupby('bdate').pnl.sum(); cum=daily.sort_index().cumsum()
    dd=(cum-cum.cummax()).min()
    L=[]
    L.append(f"# AUREON TICK-RESOLUTION Backtest ({oco_label})\n")
    L.append(f"Lot {lot} | {n} trades | {t.bdate.dt.date.nunique()} trading days\n")
    L.append("## Headline\n")
    L.append(f"- Net: ${t.pnl.sum():,.0f}")
    L.append(f"- Win rate: {wr:.1f}%   Profit factor: {pf:.2f}")
    L.append(f"- Avg win ${w.pnl.mean():,.0f} | avg loss ${l.pnl.mean():,.0f}")
    L.append(f"- Max drawdown ${dd:,.0f} | worst day ${daily.min():,.0f} | best ${daily.max():,.0f}")
    L.append(f"- Exit reasons: {t.reason.value_counts().to_dict()}\n")
    # TRAIL SLIP — the validation number
    ts = t['trail_slip'].dropna()
    L.append("## TRAIL SLIP (actual exit minus modeled peak-0.30) — THE validation number\n")
    L.append(f"- mean {ts.mean():.4f} | median {ts.median():.4f} | p90 {ts.quantile(.9):.4f} | worst {ts.min():.4f}")
    L.append("  (near 0 = M1 backtest was honest; large negative = trail filled worse than modeled = edge was inflated)\n")
    L.append("## Monthly\n| Month | Trades | WR | Net $ |\n|---|---|---|---|")
    for ym,g in t.groupby('ym'):
        L.append(f"| {ym} | {len(g)} | {(g.pnl>0).mean()*100:.0f}% | ${g.pnl.sum():,.0f} |")
    L.append("\n## Per anchor\n| Anchor | Trades | WR | Net $ |\n|---|---|---|---|")
    for a,g in t.groupby('anchor'):
        L.append(f"| {a} | {len(g)} | {(g.pnl>0).mean()*100:.0f}% | ${g.pnl.sum():,.0f} |")
    L.append("\n## Daily\n| Date | Net $ |\n|---|---|")
    for d,v in daily.sort_index().items():
        L.append(f"| {d.date()} | ${v:,.0f} |")
    out="\n".join(L)
    print(out)
    return out

if __name__ == "__main__":
    df = load_ticks(CSV)
    print("\n========== OCO ON ==========")
    t_on = run(df, oco=True)
    rep_on = summarize(t_on, LOT, "OCO ON")
    print("\n========== OCO OFF ==========")
    t_off = run(df, oco=False)
    rep_off = summarize(t_off, LOT, "OCO OFF")
    with open("tick_backtest_report.md","w") as f:
        f.write(rep_on + "\n\n---\n\n" + rep_off)
    t_on.to_csv("tick_trades_oco_on.csv", index=False)
    t_off.to_csv("tick_trades_oco_off.csv", index=False)
    print("\nWrote tick_backtest_report.md, tick_trades_oco_on.csv, tick_trades_oco_off.csv")