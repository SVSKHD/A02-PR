"""AUREON v3.0.8 — Telegram reachability: DNS-pin + backoff.

WHY THIS EXISTS
---------------
The VPS sits on an ISP that DNS-poisons `api.telegram.org`: the system resolver
returns a sinkhole IP, so every send/poll times out, floods the log, and stalls
cycles. Cloudflare resolves the REAL IP (149.154.166.110). The operator can't
change the box's DNS, so the fix lives here:

  1. DNS-PIN: connect Telegram HTTP to a known-good IP, bypassing the poisoned
     system resolver, WITHOUT weakening TLS. We keep the request URL as
     `https://api.telegram.org/...` (so SNI, Host header and certificate
     hostname verification all use api.telegram.org) and override ONLY the
     socket-level address resolution for that one host. Certificate verification
     stays ON — we never set verify=False.
     - Preferred IP source: DNS-over-HTTPS to Cloudflare's 1.1.1.1 literal
       (no DNS needed, can't be poisoned), refreshed periodically.
     - Fallback: a configurable pinned-IP list (default 149.154.166.110).
     - If every pinned candidate fails, fall through to the system resolver so
       the bot self-heals if the network is later fixed.

  2. BACKOFF + LOG COLLAPSE (FailureStreak): one warning on the first failure of
     a streak, then suppressed repeats + a periodic summary, and an exponential
     poll/retry interval (base -> 2x -> ... -> cap) that resets on first success.

Used by telemetry (sendMessage) and watchdog (getUpdates). Trading never calls
this on its path — sends are queued on a worker thread, polls run in the
watchdog process — so a slow/blocked Telegram can never stall a fill.
"""
import logging
import os
import threading
import time

try:
    import requests
    import urllib3.util.connection as _u3_conn
    _NET_OK = True
except Exception:  # pragma: no cover - requests is a hard dep in practice
    _NET_OK = False

log = logging.getLogger("AUREON")

PINNED_HOST = "api.telegram.org"
DEFAULT_PINNED_IPS = ["149.154.166.110"]   # Cloudflare-resolved, Jun 2026
DOH_URLS = ["https://1.1.1.1/dns-query", "https://cloudflare-dns.com/dns-query"]

# Timeouts (v3.0.8): fail fast on a sinkhole that won't connect. (connect, read).
CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 10.0
TLS_VERIFY = True                          # NEVER disable; assert in selftest


# ============================================================================
# Socket-level host override (thread-local so concurrent callers never race)
# ============================================================================
_tls = threading.local()
_patched = False
_patch_lock = threading.Lock()


def _install_patch():
    """Wrap urllib3's create_connection ONCE so that, while a thread has an
    override IP set, connections to PINNED_HOST go to that IP instead. The URL,
    SNI and certificate hostname are untouched, so TLS still validates against
    api.telegram.org."""
    global _patched
    if _patched or not _NET_OK:
        return
    with _patch_lock:
        if _patched:
            return
        _orig = _u3_conn.create_connection

        def _patched_create_connection(address, *args, **kwargs):
            host, port = address[0], address[1]
            ip = getattr(_tls, "override_ip", None)
            if ip and host == PINNED_HOST:
                address = (ip, port)
            return _orig(address, *args, **kwargs)

        _u3_conn.create_connection = _patched_create_connection
        _patched = True


# ============================================================================
# Config (env first, with a configure() override for the Config dataclass)
# ============================================================================
def _env_enabled():
    return os.environ.get("AUREON_TELEGRAM_DNS_PIN", "on").strip().lower() not in (
        "0", "off", "false", "no")


def _env_pinned_ips():
    raw = os.environ.get("AUREON_TELEGRAM_PINNED_IPS", "").strip()
    ips = [p.strip() for p in raw.split(",") if p.strip()]
    return ips or list(DEFAULT_PINNED_IPS)


def _env_refresh_min():
    try:
        return float(os.environ.get("AUREON_TELEGRAM_REFRESH_MIN", "15").strip() or 15)
    except ValueError:
        return 15.0


_enabled_flag = _env_enabled()
_pinned_ips = _env_pinned_ips()
_session_refresh_min = _env_refresh_min()   # rebuild/re-resolve cadence (min)
_doh_ips = []            # last DoH-resolved IP(s)
_doh_ts = 0.0           # when we last resolved
_cfg_lock = threading.Lock()
_timer_started = False


def _doh_ttl_s():
    # The DoH cache is considered stale after one refresh interval, so a
    # long-running process re-resolves on its own cadence (v3.0.9: was a fixed
    # 30m). A manual restart used to be the only thing that re-resolved.
    return max(60.0, _session_refresh_min * 60.0)


def configure(enabled=None, pinned_ips=None, session_refresh_min=None):
    """Let the Config dataclass override the env defaults at startup (idempotent).
    Safe to call before/after telemetry is built; defaults already work."""
    global _enabled_flag, _pinned_ips, _session_refresh_min
    with _cfg_lock:
        if enabled is not None:
            _enabled_flag = bool(enabled)
        if pinned_ips:
            _pinned_ips = [str(ip).strip() for ip in pinned_ips if str(ip).strip()]
        if session_refresh_min is not None:
            try:
                _session_refresh_min = float(session_refresh_min)
            except (TypeError, ValueError):
                pass
    _install_patch()


def is_enabled():
    return bool(_enabled_flag) and _NET_OK


# ============================================================================
# DNS-over-HTTPS resolution (Cloudflare 1.1.1.1 literal — can't be poisoned)
# ============================================================================
def resolve_via_doh(hostname=PINNED_HOST, timeout=CONNECT_TIMEOUT):
    """Return a list of A-record IPs for `hostname` via Cloudflare DoH, or []
    on any failure. Hits the 1.1.1.1 IP literal (TLS cert includes 1.1.1.1), so
    no system DNS is involved and verification stays ON."""
    if not _NET_OK:
        return []
    for base in DOH_URLS:
        try:
            r = requests.get(base, params={"name": hostname, "type": "A"},
                             headers={"accept": "application/dns-json"},
                             timeout=(CONNECT_TIMEOUT, timeout), verify=TLS_VERIFY)
            if r.status_code != 200:
                continue
            answers = r.json().get("Answer", []) or []
            ips = [a.get("data") for a in answers
                   if a.get("type") == 1 and a.get("data")]
            ips = [ip for ip in ips if _looks_ipv4(ip)]
            if ips:
                return ips
        except Exception:
            continue
    return []


def _looks_ipv4(s):
    parts = str(s).split(".")
    if len(parts) != 4:
        return False
    try:
        return all(0 <= int(p) <= 255 for p in parts)
    except ValueError:
        return False


def refresh_doh(force=False):
    """Refresh the DoH-resolved IP cache if stale (or forced). Never raises."""
    global _doh_ips, _doh_ts
    now = time.time()
    if not force and _doh_ips and (now - _doh_ts) < _doh_ttl_s():
        return _doh_ips
    ips = resolve_via_doh()
    if ips:
        _doh_ips = ips
        _doh_ts = now
    return _doh_ips


def candidate_ips():
    """Ordered, de-duplicated connect candidates: DoH-resolved first (freshest
    truth), then the static pinned list."""
    refresh_doh(force=False)
    out, seen = [], set()
    for ip in list(_doh_ips) + list(_pinned_ips):
        if ip and ip not in seen:
            seen.add(ip)
            out.append(ip)
    return out


def first_candidate_ip():
    ips = candidate_ips()
    return ips[0] if ips else (_pinned_ips[0] if _pinned_ips else DEFAULT_PINNED_IPS[0])


def pin_status_line():
    """The loud one-line startup receipt."""
    if not is_enabled():
        return "Telegram DNS-pin OFF (system resolver)"
    return f"Telegram DNS-pin ON → {first_candidate_ip()}"


# ============================================================================
# Transport: rotate pinned candidates, fall back to the system resolver
# ============================================================================
def _is_telegram_url(url):
    return isinstance(url, str) and url.startswith(f"https://{PINNED_HOST}")


def request(method, url, fresh=False, **kwargs):
    """Drop-in for requests.request that DNS-pins Telegram. Returns a Response on
    any HTTP reply (incl. non-200 — that means we REACHED Telegram), and raises
    the last connection error only if every candidate AND the system resolver
    fail to connect. TLS verification is always ON.

    `fresh=True` (v3.0.9) forces a brand-new DoH re-resolve before connecting —
    the per-message fresh-connect fallback for a dead/stale pinned socket. Every
    call already uses a transient session (no shared keep-alive pool), so a
    fresh request is genuinely a new socket; `fresh` additionally re-pins the IP."""
    kwargs.setdefault("timeout", (CONNECT_TIMEOUT, READ_TIMEOUT))
    kwargs.setdefault("verify", TLS_VERIFY)
    if not _NET_OK:
        raise RuntimeError("requests unavailable")
    if not is_enabled() or not _is_telegram_url(url):
        return requests.request(method, url, **kwargs)

    if fresh:
        refresh_doh(force=True)
    _install_patch()
    last_exc = None
    for ip in candidate_ips():
        _tls.override_ip = ip
        try:
            return requests.request(method, url, **kwargs)
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as e:
            last_exc = e
        finally:
            _tls.override_ip = None
    # Every pinned candidate failed — try the system resolver so a later
    # network fix self-heals without a restart.
    try:
        return requests.request(method, url, **kwargs)
    except (requests.exceptions.ConnectionError,
            requests.exceptions.Timeout) as e:
        last_exc = e
    raise last_exc


def get(url, fresh=False, **kwargs):
    return request("GET", url, fresh=fresh, **kwargs)


def post(url, fresh=False, **kwargs):
    return request("POST", url, fresh=fresh, **kwargs)


# ============================================================================
# Session rebuild — do automatically what a manual restart does (v3.0.9)
# ============================================================================
# Operator observation: "when we restart it works, then it jams." That pattern
# is a stale pinned socket / dead keep-alive — not necessarily a hard ISP block.
# A restart re-resolves DoH and opens fresh sockets; rebuild() does the same on a
# timer, on wake, and after a failure streak, without a restart.
def rebuild(reason=""):
    """Tear down + rebuild the Telegram connection: force a fresh DoH re-resolve
    and re-pin. (Sends already use transient sessions, so there is no long-lived
    pool to close; the meaningful state is the resolved IP.) Returns the IP we
    will connect to next. Never raises."""
    try:
        refresh_doh(force=True)
    except Exception:
        pass
    ip = first_candidate_ip()
    if is_enabled():
        log.info(f"Telegram session rebuilt ({reason or 'manual'}) → {ip}")
    return ip


def start_refresh_timer():
    """Start ONE daemon thread per process that rebuilds the session every
    `telegram_session_refresh_min` minutes, so a long-idle process re-resolves
    even with no traffic. Idempotent; never blocks the caller."""
    global _timer_started
    if _timer_started or not _NET_OK:
        return
    with _cfg_lock:
        if _timer_started:
            return
        _timer_started = True

    def _loop():
        while True:
            time.sleep(max(60.0, _session_refresh_min * 60.0))
            try:
                rebuild("timer")
            except Exception:
                pass

    threading.Thread(target=_loop, name="telegram-refresh", daemon=True).start()


# ============================================================================
# FailureStreak — exponential backoff + collapsed logging
# ============================================================================
class FailureStreak:
    """Tracks consecutive failures for one channel (sends or polls):
      - on_failure(): log the FIRST failure of a streak fully (returns True);
        suppress the rest and emit ONE summary line every `summary_every_s`.
      - interval(): the current retry/poll interval — `base` for the first few
        failures, then doubling once past 3 consecutive, capped at `cap`.
      - on_success(): reset; log a one-line recovery if we had been failing.
    Not thread-safe by design — each channel owns its own instance on one thread.
    """

    def __init__(self, name, base_interval=30.0, max_interval=300.0,
                 summary_every_s=300.0, logger=None):
        self.name = name
        self.base = float(base_interval)
        self.cap = float(max_interval)
        self.summary_every = float(summary_every_s)
        self.log = logger or log
        self.count = 0
        self._first_ts = None
        self._last_summary_ts = 0.0
        self._suppressed = 0

    def interval(self):
        if self.count <= 2:
            return self.base
        # 3 consecutive -> 2x base, then keep doubling, capped.
        return min(self.base * (2 ** (self.count - 2)), self.cap)

    def on_failure(self, err=None):
        """Record a failure; return True if the caller should log it FULLY now."""
        now = time.time()
        self.count += 1
        if self.count == 1:
            self._first_ts = now
            self._last_summary_ts = now
            self._suppressed = 0
            return True
        self._suppressed += 1
        if now - self._last_summary_ts >= self.summary_every:
            mins = (now - self._first_ts) / 60.0
            self.log.warning(
                f"{self.name} unreachable for {mins:.0f}m, "
                f"{self._suppressed} attempt(s) failed since last summary "
                f"(suppressing per-attempt logs)")
            self._last_summary_ts = now
            self._suppressed = 0
        return False

    def on_success(self):
        if self.count > 0:
            self.log.info(f"{self.name} reachable again (after {self.count} "
                          f"consecutive failure(s))")
        self.count = 0
        self._first_ts = None
        self._suppressed = 0


# Install the socket patch at import so any early send is already pinned.
_install_patch()
