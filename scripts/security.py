"""Bounds and public-log diagnostics for data supplied by app users.

The app's free watchlist contains at most 10 entries; Pro has no UI limit.
These generous operational ceilings also apply to Pro, independently of UI
limits. They keep one malformed record or one oversized list from stopping
or expanding a shared scheduled job.
"""

import re
from itertools import zip_longest

MAX_WATCHLIST_ITEMS = 100
MAX_SHARED_SYMBOLS = 500
MAX_USERS_PER_RUN = 10_000
MAX_SUMMARIES_PER_RUN = 200
_SYMBOL = re.compile(r"[A-Z0-9^][A-Z0-9^=.-]{0,31}\Z")


def safe_exception(error):
    """Never include SDK messages, URLs, response bodies, tokens or user data."""
    name = re.sub(r"[^A-Za-z0-9_]", "", type(error).__name__)[:64] or "Error"
    status = getattr(error, "status_code", None)
    if status is None:
        status = getattr(getattr(error, "response", None), "status_code", None)
    if type(status) is int and 100 <= status <= 599:
        return f"{name} (HTTP {status})"
    return name


def user_data(document):
    data = document.to_dict()
    return data if isinstance(data, dict) else {}


def notification_token(data):
    token = data.get("fcmToken")
    return token if isinstance(token, str) and 0 < len(token) <= 4096 else None


def valid_watchlist(value, *, us_only=False, limit=MAX_WATCHLIST_ITEMS):
    if not isinstance(value, list):
        return []
    symbols = []
    seen = set()
    # Bound inspection too, including arrays consisting entirely of invalid data.
    for raw in value[:limit]:
        if not isinstance(raw, str) or len(raw) > 32:
            continue
        symbol = raw.strip().upper()
        if not _SYMBOL.fullmatch(symbol) or (us_only and "." in symbol):
            continue
        if symbol not in seen:
            seen.add(symbol)
            symbols.append(symbol)
    return symbols


def load_users(db):
    return list(db.collection("users").limit(MAX_USERS_PER_RUN).stream())


def aggregate_watchlists(documents, *, us_only=False, limit=MAX_SHARED_SYMBOLS):
    lists = [valid_watchlist(user_data(doc).get("watchlist"), us_only=us_only)
             for doc in documents]
    symbols = set()
    # Round robin across users instead of letting the first oversized list win.
    for row in zip_longest(*lists):
        for symbol in row:
            if symbol is not None:
                symbols.add(symbol)
                if len(symbols) >= limit:
                    return sorted(symbols)
    return sorted(symbols)


class WorkBudget:
    def __init__(self, limit):
        self.remaining = limit

    def take(self):
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        return True
