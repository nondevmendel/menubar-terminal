import os, json, time, re, urllib.request, urllib.error
from datetime import date

_STATS_DIR   = os.path.expanduser("~/.menubar_terminal")
_STATS_FILE  = os.path.join(_STATS_DIR, "daily_stats.json")
_CONFIG_FILE = os.path.join(_STATS_DIR, "config.json")

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

# ── Anthropic API key + monthly usage ─────────────────────────────────────────

_usage_cache: dict = {}
_usage_cache_time: float = 0
_USAGE_CACHE_TTL = 1800   # refresh at most every 30 minutes


def _load_config() -> dict:
    try:
        with open(_CONFIG_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_config(cfg: dict) -> None:
    try:
        with open(_CONFIG_FILE, "w") as f:
            json.dump(cfg, f)
    except Exception:
        pass


def set_api_key(key: str) -> None:
    cfg = _load_config()
    cfg["anthropic_api_key"] = key.strip()
    _save_config(cfg)
    global _usage_cache, _usage_cache_time
    _usage_cache = {}
    _usage_cache_time = 0


def get_api_key() -> str:
    return _load_config().get("anthropic_api_key", "")


def fetch_monthly_usage(force: bool = False) -> dict:
    global _usage_cache, _usage_cache_time
    if not force and _usage_cache and time.time() - _usage_cache_time < _USAGE_CACHE_TTL:
        return _usage_cache

    key = get_api_key()
    if not key:
        return {"error": "no_key"}

    today = date.today()
    start = today.replace(day=1).isoformat()
    end   = today.isoformat()
    url   = (f"https://api.anthropic.com/v1/usage"
             f"?start_date={start}&end_date={end}&limit=100")
    req = urllib.request.Request(url, headers={
        "x-api-key":           key,
        "anthropic-version":   "2023-06-01",
        "anthropic-beta":      "usage-2025-01-01",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = json.loads(r.read())
            result = _aggregate(raw)
            _usage_cache = result
            _usage_cache_time = time.time()
            return result
    except urllib.error.HTTPError as e:
        body = ""
        try: body = e.read().decode()[:200]
        except: pass
        return {"error": f"HTTP {e.code}", "detail": body}
    except Exception as e:
        return {"error": str(e)}


def _aggregate(raw: dict) -> dict:
    """Sum token counts across all models/days returned by the API."""
    rows = raw.get("data") or raw.get("usage") or []
    if not rows and isinstance(raw, list):
        rows = raw
    totals = {
        "input_tokens":                 0,
        "output_tokens":                0,
        "cache_creation_input_tokens":  0,
        "cache_read_input_tokens":      0,
    }
    for row in rows:
        for k in totals:
            totals[k] += row.get(k, 0)
    totals["raw"] = raw   # keep for debugging
    return totals
