"""Structured session-review log — offline tests.

Runnable under pytest or standalone (`python tests/test_review_log.py`).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import review_log as RV


def _logger(d, day="2026-07-18", clock="12:00:00"):
    return RV.ReviewLogger(log_dir=str(d), clock=lambda: clock, date_fn=lambda: day)


def _lines(path):
    with open(path) as f:
        return [ln.rstrip("\n") for ln in f if ln.strip()]


# --- one line per event type ------------------------------------------------------
def test_every_event_writes_exactly_one_line(tmp_path):
    r = _logger(tmp_path)
    r.fill("ANCHOR", "BUY", 0.35, 4028.77, tag="A1")
    r.close("ANCHOR", "BUY", 0.35, 4001.80, "LADDER_LOCK4", 140.0, tag="A1")
    r.lock("ANCHOR", "armed", intended=4001.80, level="LOCK4")
    r.lock("ANCHOR", "fallback", intended=4001.80, level="LOCK_FALLBACK_CLOSE")
    r.pending("ROGUE", "placed", "RGS:S1", price=3994.80)
    r.anchor("ROGUE", 3977.80, "SCHEDULED", label="ROGUE_S1")
    r.governor("ANCHOR", "loss_stop", detail="-630")
    r.testrun("PASS", 7, 7)
    lines = _lines(r.path())
    assert len(lines) == 8                       # exactly one line per call
    types = [ln.split()[1] for ln in lines]
    assert types == ["FILL", "CLOSE", "LOCK", "LOCK", "PENDING", "ANCHOR", "GOV", "TEST"]
    # readable key=value content
    assert "engine=ANCHOR side=BUY lot=0.35 price=4028.77 tag=A1" in lines[0]
    assert "reason=LADDER_LOCK4 pnl=+140.00" in lines[1]


def test_line_starts_with_timestamp_and_type():
    r = RV.ReviewLogger(clock=lambda: "14:03:11", date_fn=lambda: "d")
    # build a line via the pure kv helper + the format the emitter uses
    kv = RV._kv(engine="ANCHOR", side="BUY", lot=0.35, price=4028.77)
    line = f"14:03:11 {'FILL':<8} {kv}"
    rec = RV.parse_line(line)
    assert rec["_ts"] == "14:03:11" and rec["_type"] == "FILL"
    assert rec["engine"] == "ANCHOR" and rec["price"] == "4028.77"


# --- daily rotation ---------------------------------------------------------------
def test_daily_rotation(tmp_path):
    day = {"d": "2026-07-18"}
    r = RV.ReviewLogger(log_dir=str(tmp_path), clock=lambda: "09:00:00",
                        date_fn=lambda: day["d"])
    r.fill("ANCHOR", "BUY", 0.35, 4000.0)
    day["d"] = "2026-07-19"                       # next day
    r.fill("ANCHOR", "SELL", 0.35, 4100.0)
    p18 = tmp_path / "review_2026-07-18.log"
    p19 = tmp_path / "review_2026-07-19.log"
    assert p18.exists() and p19.exists()
    assert len(_lines(p18)) == 1 and len(_lines(p19)) == 1   # split across days


# --- digest matches file contents -------------------------------------------------
def test_summarize_matches_file(tmp_path):
    r = _logger(tmp_path)
    r.fill("ANCHOR", "BUY", 0.35, 4028.77, tag="A1")
    r.fill("ROGUE", "SELL", 0.35, 3977.80, tag="RGS:S1")
    r.close("ANCHOR", "BUY", 0.35, 4001.80, "LADDER_LOCK4", 140.0)
    r.close("ANCHOR", "SELL", 0.35, 3990.0, "SL", -6.30)
    r.close("ROGUE", "SELL", 0.35, 3960.0, "TP", 210.0)
    r.lock("ANCHOR", "armed", intended=4001.80)
    r.lock("ANCHOR", "modified", intended=4003.0)
    r.lock("ANCHOR", "fallback", intended=4001.80)
    r.lock("ANCHOR", "rejected_retried", intended=4001.80)
    r.anchor("ROGUE", 3977.80, "SCHEDULED", label="ROGUE_S1")

    s = RV.read_summary(str(tmp_path), "2026-07-18")
    assert s["fills"] == 2
    assert s["closes_by_reason"] == {"LADDER_LOCK4": 1, "SL": 1, "TP": 1}
    assert s["net_by_engine"] == {"ANCHOR": 133.70, "ROGUE": 210.0}
    assert abs(s["net_total"] - 343.70) < 1e-6
    assert s["locks"] == {"armed": 1, "fired": 1, "fallback": 1}
    assert s["rejects"] == 1 and s["anchors"] == 1

    digest = RV.format_digest(s, "2026-07-18")
    assert "fills: 2" in digest and "total +343.70" in digest
    assert "armed 1 / fired 1 / fallback 1" in digest and "rejects 1" in digest
    # digest is derived FROM the file — re-summarizing the file gives the same numbers
    assert RV.summarize(_lines(r.path())) == s


def test_post_digest_reads_shared_logger_dir(tmp_path):
    # the EOD/`/review` digest must read the SAME dir the live logger writes to
    r = _logger(tmp_path)
    RV._SHARED = r
    try:
        r.fill("ANCHOR", "BUY", 0.35, 4028.77)
        r.close("ANCHOR", "BUY", 0.35, 4001.80, "LADDER_LOCK4", 140.0)
        text = RV.post_review_digest(None, None, day="2026-07-18")
        assert "fills: 1" in text and "total +140.00" in text
        # exactly matches summarizing the file itself
        assert RV.summarize(_lines(r.path())) == RV.read_summary(str(tmp_path), "2026-07-18")
    finally:
        RV._SHARED = None


def test_missing_file_empty_summary(tmp_path):
    s = RV.read_summary(str(tmp_path), "2099-01-01")
    assert s["fills"] == 0 and s["net_total"] == 0.0 and s["closes_by_reason"] == {}


def test_shared_accessor_singleton():
    a = RV.get_review_logger()
    b = RV.get_review_logger()
    assert a is b


# --- standalone runner ------------------------------------------------------------
def _run_all():
    import tempfile, pathlib, inspect
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    fails = 0
    for name, fn in tests:
        try:
            if "tmp_path" in inspect.signature(fn).parameters:
                with tempfile.TemporaryDirectory() as d:
                    fn(pathlib.Path(d))
            else:
                fn()
            print(f"PASS  {name}")
        except Exception as e:
            fails += 1
            import traceback; print(f"FAIL  {name}: {e!r}"); traceback.print_exc()
    print(f"\n{len(tests) - fails}/{len(tests)} passed")
    return fails


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
