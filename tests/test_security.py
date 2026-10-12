"""Offline regressions: no credentials, network or provider packages required."""

import contextlib
import importlib.util
import io
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import security
import i18n


class Document:
    def __init__(self, data, uid="private-user-id", exists=True):
        self.data, self.id, self.exists = data, uid, exists

    def to_dict(self):
        return self.data


class Database:
    def __init__(self, users, cached=None):
        self.users, self.cached, self.writes = users, cached, {}

    def collection(self, name):
        database = self

        class Collection:
            def limit(self, maximum):
                self.maximum = maximum
                return self

            def stream(self):
                return iter(database.users[:self.maximum])

            def document(self, uid):
                return types.SimpleNamespace(
                    get=lambda: Document(database.cached, exists=database.cached is not None),
                    set=lambda data: database.writes.__setitem__((name, uid), data),
                )

        return Collection()


def load_script(name):
    requests = types.ModuleType("requests")
    requests.Session = mock.Mock
    requests.HTTPError = requests.RequestException = RuntimeError
    firebase = types.ModuleType("firebase_admin")
    firebase.credentials = mock.Mock()
    firebase.firestore = types.SimpleNamespace(SERVER_TIMESTAMP="server-timestamp")
    firebase.messaging = types.SimpleNamespace(
        Message=lambda **kwargs: kwargs,
        Notification=lambda **kwargs: kwargs,
        send=mock.Mock(return_value="private-provider-message-id"),
    )
    anthropic = types.ModuleType("anthropic")
    anthropic.Anthropic = mock.Mock()
    spec = importlib.util.spec_from_file_location("test_" + name, SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {
        "requests": requests, "firebase_admin": firebase, "anthropic": anthropic,
    }):
        spec.loader.exec_module(module)
    return module


class SecurityTests(unittest.TestCase):
    def test_exception_diagnostic_discards_private_messages_and_request_urls(self):
        error = RuntimeError("https://provider.example/?token=private-token private-user-id private-response")
        error.response = types.SimpleNamespace(status_code=403)
        self.assertEqual(security.safe_exception(error), "RuntimeError (HTTP 403)")
        error.response.status_code = "private-token"
        self.assertEqual(security.safe_exception(error), "RuntimeError")

    def test_malformed_watchlists_are_rejected_and_normal_symbols_preserved(self):
        for invalid in (None, "AAPL", {"AAPL": True}, 42):
            self.assertEqual(security.valid_watchlist(invalid), [])
        values = [{"x": 1}, [], None, 42, " AAPL ", "aapl", "7203.T", "BRK-B", "EURUSD=X", "A\nB", "../X", "x" * 33]
        self.assertEqual(security.valid_watchlist(values), ["AAPL", "7203.T", "BRK-B", "EURUSD=X"])
        self.assertEqual(security.valid_watchlist(values, us_only=True), ["AAPL", "BRK-B", "EURUSD=X"])

    def test_per_user_and_shared_bounds_include_other_users(self):
        oversized = ["X" + str(index) for index in range(1000)]
        self.assertEqual(len(security.valid_watchlist(oversized)), security.MAX_WATCHLIST_ITEMS)
        documents = [Document({"watchlist": oversized}), Document({"watchlist": ["AAPL"]})]
        symbols = security.aggregate_watchlists(documents, limit=3)
        self.assertEqual(len(symbols), 3)
        self.assertIn("AAPL", symbols)
        self.assertNotIn("X999", symbols)

    def test_language_and_token_types_cannot_stop_notifications(self):
        self.assertEqual(i18n.ai_language_name({"aiLanguage": {}, "language": []}), "Japanese")
        self.assertIsNone(security.notification_token({"fcmToken": {"bad": True}}))
        self.assertIsNone(security.notification_token({"fcmToken": "x" * 4097}))

    def test_morning_job_continues_after_bad_user_and_caps_paid_calls_without_log_leak(self):
        module = load_script("morning_briefing")
        users = [
            Document({"watchlist": [{"bad": "private-token"}]}, "bad-private-user"),
            Document({"watchlist": ["AAPL", "TSLA"], "fcmToken": "private-token"}, "first-private-user"),
            Document({"watchlist": ["AAPL"], "fcmToken": "other-private-token", "aiLanguage": "english"}, "second-private-user"),
        ]
        module.db = Database(users)
        module.MAX_PAID_SUMMARIES = 1
        module.get_quote = mock.Mock(return_value={"price": 100, "change_pct": 2})
        module.get_news = mock.Mock(return_value=[{"headline": "private-headline", "url": "https://news.example/"}])
        module.summarize = mock.Mock(return_value="private-model-response")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            module.main()
        self.assertEqual(module.summarize.call_count, 1)
        self.assertEqual(module.messaging.send.call_count, 2)
        self.assertEqual(len(module.db.writes), 2)
        for private in ["private", "AAPL", "TSLA", "https://"]:
            self.assertNotIn(private, output.getvalue())
        self.assertIn("送信 2 件", output.getvalue())

    def test_morning_provider_failure_never_logs_token_or_sdk_body(self):
        module = load_script("morning_briefing")
        module.db = Database([Document({"watchlist": ["AAPL"], "fcmToken": "private-token"})])
        module.get_quote = mock.Mock(side_effect=RuntimeError("https://api.example/?token=private-token response-body"))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            module.main()
        self.assertIn("RuntimeError", output.getvalue())
        for private in ["private", "AAPL", "https://", "response-body"]:
            self.assertNotIn(private, output.getvalue())

    def test_cached_and_rebuilt_symbol_lists_cannot_bypass_bounds(self):
        for name in ("market_cache", "edgar"):
            module = load_script(name)
            users = [Document({"watchlist": [{}, "AAPL", "7203.T"]})]
            database = Database(users)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(set(module.load_watched(database)), {"AAPL"})
            cached = {"tickers": [{}, "AAPL", "7203.T"], "updatedAt": module.datetime.now(module.UTC).isoformat()}
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(set(module.load_watched(Database([], cached))), {"AAPL"})

    def test_market_close_provider_failure_keeps_only_aggregate_and_error_type(self):
        module = load_script("market_close")
        module.db = Database([Document({"fcmToken": "private-token"})])
        module.messaging.send.side_effect = RuntimeError("private-user-id private-token https://provider.example/")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            module.send({"ja": ("private-title", "private-body")}, "notifyJPClose")
        self.assertIn("失敗 1 件", output.getvalue())
        self.assertNotIn("private", output.getvalue())
        self.assertNotIn("https://", output.getvalue())

    def test_watchlist_notifications_skip_bad_entries_and_continue(self):
        for name in ("edinet", "short_selling"):
            module = load_script(name)
            # These provider SDK imports are intentionally deferred by the scripts.
            module.messaging = types.SimpleNamespace(
                Message=lambda **kwargs: kwargs, Notification=lambda **kwargs: kwargs,
                send=mock.Mock(return_value="private-message-id"),
            )
            users = [Document({"watchlist": [{}, None], "fcmToken": "private-token"}),
                     Document({"watchlist": ["7203.T"], "fcmToken": "private-token"})]
            database = Database(users)
            item = {"code": "7203", "symbol": "7203.T", "name": "public-company", "filers": ["public-filer"],
                    "previous": 1, "ratio": 2, "delta": 1}
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                module.notify(database, {"7203.T": item} if name == "edinet" else [item])
            self.assertEqual(module.messaging.send.call_count, 1)
            self.assertNotIn("private", output.getvalue())
            self.assertNotIn("7203", output.getvalue())


if __name__ == "__main__":
    unittest.main()
