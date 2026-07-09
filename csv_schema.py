"""AUREON — CSV header/schema self-heal + one-shot migration (fixes R-8).

R-8: the append-only CSV writers (rogue_patternlog, fetcher, boost_metrics) write the
HEADER only when the file does not yet exist. When a column (e.g. `seed_source`) was
APPENDED to a writer's column constant, every row immediately widened by one, but any file
created BEFORE that change keeps its narrower header forever -- so csv.DictReader drops the
extra trailing value into restkey and any reader keying that column reads garbage. The most
visible casualty was run/rogue_trades.csv: a 9-column header over 10-column rows.

This module rewrites a stale header IN PLACE to match the writer's CURRENT column constant,
backing the original up to `<file>.bak` first. It is:
  - IDEMPOTENT: a file whose header already matches is left untouched (re-runs are no-ops).
  - BYTE-SAFE on data: only the header LINE is replaced; every data row is preserved exactly
    (the widened rows already carry the right number of values, so a wider header reads them
    correctly; a legacy narrow row simply reads '' for the appended tail columns).
  - GUARDED: any IO error is swallowed -- a migration failure never reaches trading.

`ensure()` is called by each writer just before it appends, so a stale file self-heals on
first write; `migrate_run_dir()` sweeps the known files at boot / from the reconcile CLI.
"""
from __future__ import annotations

import csv
import logging
import os

log = logging.getLogger("AUREON")


def _header_line(columns):
    """The canonical header line for `columns` (no trailing newline)."""
    return ",".join(str(c) for c in columns)


def _split_fields(line):
    """Field list of a single CSV line (handles quoting). [] on any error."""
    try:
        return next(csv.reader([line]))
    except Exception:
        return []


def inspect(path):
    """(header_fields, first_row_fields) for a CSV file, each a list (or None if absent).
    READ-ONLY; guarded -> (None, None) on any error."""
    try:
        if not os.path.exists(path):
            return None, None
        with open(path, newline="") as f:
            header = f.readline()
            if header == "":
                return None, None
            row = f.readline()
        hf = _split_fields(header.rstrip("\r\n"))
        rf = _split_fields(row.rstrip("\r\n")) if row else None
        return hf, rf
    except Exception:
        return None, None


def needs_migration(path, columns):
    """True iff the on-disk header differs from `columns` (name-for-name / width), which
    is exactly the R-8 condition (header narrower than the appended rows). PURE-ish read."""
    hf, _rf = inspect(path)
    if hf is None:
        return False                       # missing / empty -> the writer writes it fresh
    return [str(c) for c in hf] != [str(c) for c in columns]


def migrate(path, columns, backup=True):
    """Rewrite the header of `path` to `columns` IN PLACE (data rows preserved byte-for-byte),
    backing the original up to `<path>.bak` first. Idempotent + guarded. Returns a dict:
    {migrated: bool, reason: str, backup: str|None, old_header: list|None}."""
    out = {"migrated": False, "reason": "", "backup": None, "old_header": None}
    try:
        if not os.path.exists(path):
            out["reason"] = "absent"
            return out
        with open(path, newline="") as f:
            content = f.read()
        if content == "":
            out["reason"] = "empty"
            return out
        nl = content.find("\n")
        old_header = content[:nl] if nl >= 0 else content
        rest = content[nl + 1:] if nl >= 0 else ""
        new_header = _header_line(columns)
        out["old_header"] = _split_fields(old_header.rstrip("\r\n"))
        if old_header.rstrip("\r\n") == new_header:
            out["reason"] = "already-current"
            return out
        # back up the original ONCE (never clobber an existing .bak from a prior migration).
        bak = path + ".bak"
        if backup and not os.path.exists(bak):
            try:
                with open(bak, "w", newline="") as bf:
                    bf.write(content)
                out["backup"] = bak
            except Exception as e:
                log.warning(f"csv_schema: backup {bak} failed (continuing): {e!r}")
        tmp = path + ".tmp"
        with open(tmp, "w", newline="") as f:
            f.write(new_header + "\n" + rest)
        os.replace(tmp, path)
        out["migrated"] = True
        out["reason"] = "header-rewritten"
        log.info(f"csv_schema: migrated {os.path.basename(path)} header "
                 f"{len(out['old_header'])}->{len(columns)} cols "
                 f"(backup={'yes' if out['backup'] else 'existing/none'})")
        return out
    except Exception as e:
        log.warning(f"csv_schema.migrate {path} non-fatal: {e!r}")
        out["reason"] = f"error:{e!r}"
        return out


def ensure(path, columns):
    """Self-heal hook for the append writers: if the existing header is stale, migrate it
    to `columns` before the caller appends. No-op for a missing/current file. Guarded."""
    try:
        if needs_migration(path, columns):
            migrate(path, columns, backup=True)
    except Exception:
        pass


# The known append-only CSV writers and their CURRENT authoritative column constants.
# (pnl_ledger.csv is intentionally excluded -- pnl_report rewrites it whole each run, so its
# header is never stale.) Resolved lazily so this module has no import-time dependency.
def _known_files(run_dir):
    files = []
    try:
        import rogue_patternlog as _rpl
        files.append((os.path.join(run_dir, _rpl.TRADES_CSV), list(_rpl.TRADE_COLUMNS)))
        files.append((os.path.join(run_dir, _rpl.PATTERNS_CSV), list(_rpl.PATTERN_COLUMNS)))
    except Exception:
        pass
    try:
        import fetcher as _f
        files.append((os.path.join(run_dir, _f.TRADES_CSV), list(_f.TRADE_COLUMNS)))
    except Exception:
        pass
    try:
        import boost_metrics as _bm
        files.append((os.path.join(run_dir, "boost_ledger.csv"), list(_bm.LEDGER_COLUMNS)))
    except Exception:
        pass
    return files


def migrate_run_dir(run_dir):
    """One-shot sweep: migrate every known append-only CSV in `run_dir` whose header is stale.
    Idempotent (re-runs are no-ops) and guarded. Returns the list of per-file migrate() dicts
    that actually rewrote a header (for logging / the CLI receipt)."""
    done = []
    try:
        for path, cols in _known_files(run_dir):
            res = migrate(path, cols, backup=True)
            if res.get("migrated"):
                res["file"] = os.path.basename(path)
                done.append(res)
    except Exception as e:
        log.warning(f"csv_schema.migrate_run_dir non-fatal: {e!r}")
    return done
