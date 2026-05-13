import os, json, time, re
from datetime import date

_STATS_DIR  = os.path.expanduser("~/.menubar_terminal")
_STATS_FILE = os.path.join(_STATS_DIR, "daily_stats.json")

_app_start        = time.time()
_session_start    = None      # set when popover opens
_today_usage_secs = 0.0
_token_count      = 0
_token_max        = 200_000   # Claude 4.x context window

# ANSI escape stripper (strip before scanning for tokens)
_ANSI_RE = re.compile(rb'\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

# Matches "45,230 / 200,000 tokens" or "45.2k / 200k tokens"
_TOKEN_PAIR_RE = re.compile(
    rb'([\d,]+(?:\.\d+)?[kK]?)\s*/\s*([\d,]+(?:\.\d+)?[kK]?)\s*tokens',
    re.IGNORECASE)

# Matches "3.6k tokens" (single count)
_TOKEN_SOLO_RE = re.compile(
    rb'([\d,]+(?:\.\d+)?[kK])\s+tokens',
    re.IGNORECASE)


def _parse_num(b: bytes) -> int:
    s = b.decode("utf-8", errors="ignore").replace(",", "").strip()
    if s.lower().endswith("k"):
        return int(float(s[:-1]) * 1_000)
    return int(float(s))


def _today() -> str:
    return date.today().isoformat()


# ── persistence ───────────────────────────────────────────────────────────────

def _load() -> None:
    global _today_usage_secs, _token_count, _token_max
    try:
        with open(_STATS_FILE) as f:
            d = json.load(f)
        if d.get("date") == _today():
            _today_usage_secs = float(d.get("usage_secs", 0))
            _token_count      = int(d.get("token_count", 0))
            _token_max        = int(d.get("token_max", 200_000))
        # else: new day — start fresh (globals already at 0)
    except Exception:
        pass


def _save() -> None:
    try:
        with open(_STATS_FILE, "w") as f:
            json.dump({
                "date":        _today(),
                "usage_secs":  _today_usage_secs,
                "token_count": _token_count,
                "token_max":   _token_max,
            }, f)
    except Exception:
        pass


# ── public API ────────────────────────────────────────────────────────────────

def popover_opened() -> None:
    global _session_start
    _session_start = time.time()


def popover_closed() -> None:
    global _session_start, _today_usage_secs
    if _session_start is not None:
        _today_usage_secs += time.time() - _session_start
        _session_start = None
        _save()


def scan_output(data: bytes) -> None:
    global _token_count, _token_max
    clean = _ANSI_RE.sub(b"", data)
    m = _TOKEN_PAIR_RE.search(clean)
    if m:
        used  = _parse_num(m.group(1))
        total = _parse_num(m.group(2))
        if total > 0:
            _token_max = total
        if used > _token_count:
            _token_count = used
            _save()
        return
    m = _TOKEN_SOLO_RE.search(clean)
    if m:
        count = _parse_num(m.group(1))
        if count > _token_count:
            _token_count = count
            _save()


def reset_tokens() -> None:
    global _token_count
    _token_count = 0
    _save()


def get() -> dict:
    usage = _today_usage_secs
    if _session_start is not None:
        usage += time.time() - _session_start
    return {
        "usage_secs":  int(usage),
        "uptime_secs": int(time.time() - _app_start),
        "token_count": _token_count,
        "token_max":   _token_max,
    }


_load()
