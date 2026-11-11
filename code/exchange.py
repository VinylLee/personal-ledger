"""Fetch and cache CNY exchange rates for display-only conversions."""

import http.client
import json
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

RATE_URL = "https://open.er-api.com/v6/latest/CNY"
USER_AGENT = "personal-ledger/1.0"
BASE_CURRENCY = "CNY"
CACHE_VERSION = 1
MAX_AGE = timedelta(hours=24)
TIMEOUT = 5
MAX_BYTES = 1024 * 1024


@dataclass(frozen=True)
class RateBook:
    rates: dict
    fetched_at: datetime
    rate_date: str
    stale: bool


def _parse_time(value):
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _rate_map(raw):
    if not isinstance(raw, dict):
        return None
    rates = {}
    for code, value in raw.items():
        if not isinstance(code, str) or not code.strip():
            return None
        try:
            rate = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
        if not rate.is_finite() or rate <= 0:
            return None
        rates[code.strip().upper()] = rate
    return rates or None


def _rate_date(timestamp):
    try:
        return datetime.fromtimestamp(int(timestamp), timezone.utc).date().isoformat()
    except (TypeError, ValueError, OSError):
        return date.today().isoformat()


def read_cache(path):
    """Read the last cached rates without touching the network."""
    cache = Path(path)
    try:
        if cache.stat().st_size > MAX_BYTES:
            return None
        payload = json.loads(cache.read_text(encoding="utf-8"), parse_float=Decimal)
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("base") != BASE_CURRENCY:
        return None
    if payload.get("version", CACHE_VERSION) != CACHE_VERSION:
        return None
    rates = _rate_map(payload.get("rates"))
    fetched_at = _parse_time(payload.get("fetched_at"))
    if rates is None or fetched_at is None:
        return None
    rate_date = payload.get("rate_date")
    if not isinstance(rate_date, str) or not rate_date:
        rate_date = fetched_at.date().isoformat()
    age = datetime.now(timezone.utc) - fetched_at
    stale = age > MAX_AGE or age < timedelta(0)
    return RateBook(rates, fetched_at, rate_date, stale)


def _write_cache(path, rates, fetched_at, rate_date):
    payload = {
        "version": CACHE_VERSION,
        "base": BASE_CURRENCY,
        "source": RATE_URL,
        "fetched_at": fetched_at.isoformat(),
        "rate_date": rate_date,
        "rates": {code: str(rate) for code, rate in rates.items()},
    }
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=target.parent,
                                     prefix=".exchange-", suffix=".json", delete=False) as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        temp = Path(stream.name)
    temp.replace(target)


def fetch_rates(path, *, timeout=TIMEOUT):
    """Fetch fresh rates, cache them, and return a RateBook; return None on failure."""
    request = Request(RATE_URL, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES or getattr(response, "status", 200) != 200:
                return None
            payload = json.loads(body.decode("utf-8"), parse_float=Decimal)
    except (URLError, http.client.HTTPException, TimeoutError, OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("base_code") != BASE_CURRENCY:
        return None
    if payload.get("result") not in (None, "success"):
        return None
    rates = _rate_map(payload.get("rates"))
    if rates is None:
        return None
    fetched_at = datetime.now(timezone.utc)
    rate_date = _rate_date(payload.get("time_last_update_unix"))
    try:
        _write_cache(path, rates, fetched_at, rate_date)
    except OSError:
        pass
    return RateBook(rates, fetched_at, rate_date, False)


def to_cny(amount, currency, book):
    """Convert an amount in `currency` to CNY using rates expressed per 1 CNY."""
    value = amount if isinstance(amount, Decimal) else Decimal(str(amount))
    code = (currency or "").strip().upper()
    if code == BASE_CURRENCY:
        return value
    rate = book.rates.get(code) if book is not None else None
    if rate is None or not rate.is_finite() or rate <= 0:
        return None
    return value / rate


def money(value):
    return f"{value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,.2f}"


def conversion_suffix(converted):
    """Format an already-converted Decimal for display in the summary labels."""
    return f"（≈ 人民币 {money(converted)}）"


def rate_note(book, currencies=()):
    if book is None:
        return ""
    note = f"{'汇率缓存' if book.stale else '汇率'} {book.rate_date}"
    details = []
    for code in sorted({code for code in currencies if code and code != BASE_CURRENCY}):
        rate = book.rates.get(code)
        if rate:
            details.append(f"1 {code} ≈ {Decimal(1) / rate:.4f} CNY")
    return note + (f" · {'；'.join(details)}" if details else "")
