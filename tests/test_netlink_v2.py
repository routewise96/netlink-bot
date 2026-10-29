"""NetLink 2.0 pilot integration tests (stdlib only, no aiogram/aiohttp).

Covers: feature-flag gating, pilot allowlist, canonical enrollment URI,
Control Plane client contract (fake transport), employee resolution flow,
secret hygiene in logs.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.services.netlink_v2 import enrollment_uri, settings
from bot.services.netlink_v2.client import (
    ControlPlaneClient,
    ControlPlaneConflict,
    ControlPlaneUnavailable,
    ControlPlaneUnauthorized,
    EmployeeNotFound,
    UnexpectedResponse,
)
from bot.services.netlink_v2.flow import build_full_name, resolve_employee

TEST_KEY = "unit-test-internal-api-key-0123456789"


class FakeTransport:
    """Records requests; replays scripted responses or errors."""

    def __init__(self, script=None):
        # script: list of (status, body) or Exception instances
        self.script = list(script or [])
        self.calls: list[dict] = []

    async def request(self, method, path, *, headers=None, json_body=None):
        self.calls.append(
            {"method": method, "path": path, "headers": dict(headers or {}), "json": json_body}
        )
        if self.script:
            step = self.script.pop(0)
            if isinstance(step, Exception):
                raise step
            status, body = step
            return status, body
        raise AssertionError("no scripted response left")

    async def close(self):
        pass


def run(coro):
    return asyncio.run(coro)


class TestSettingsGating(unittest.TestCase):
    def setUp(self):
        self.env = os.environ.copy()
        for name in (
            "NETLINK_V2_ENABLED",
            "NETLINK_CONTROL_PLANE_URL",
            "NETLINK_CONTROL_PLANE_INTERNAL_API_KEY",
            "NETLINK_V2_PILOT_TELEGRAM_IDS",
        ):
            os.environ.pop(name, None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env)

    def test_disabled_by_default(self):
        self.assertFalse(settings.is_enabled())
        self.assertFalse(settings.is_pilot_user(1))

    def test_enabled_requires_url_and_key(self):
        os.environ["NETLINK_V2_ENABLED"] = "true"
        self.assertFalse(settings.is_enabled(), "incomplete config must fail closed")
        os.environ["NETLINK_CONTROL_PLANE_URL"] = "http://localhost:8000"
        self.assertFalse(settings.is_enabled())
        os.environ["NETLINK_CONTROL_PLANE_INTERNAL_API_KEY"] = TEST_KEY
        self.assertTrue(settings.is_enabled())

    def test_pilot_allowlist_gating(self):
        os.environ["NETLINK_V2_ENABLED"] = "true"
        os.environ["NETLINK_CONTROL_PLANE_URL"] = "http://localhost:8000"
        os.environ["NETLINK_CONTROL_PLANE_INTERNAL_API_KEY"] = TEST_KEY
        os.environ["NETLINK_V2_PILOT_TELEGRAM_IDS"] = " 111, 222 ,333"
        self.assertTrue(settings.is_pilot_user(111))
        self.assertTrue(settings.is_pilot_user(222))
        self.assertFalse(settings.is_pilot_user(999))
        # empty allowlist -> nobody
        os.environ["NETLINK_V2_PILOT_TELEGRAM_IDS"] = ""
        self.assertFalse(settings.is_pilot_user(111))
        self.assertFalse(settings.is_pilot_user(0), "no wildcard/zero users")

    def test_flag_not_true_means_off(self):
        os.environ["NETLINK_V2_ENABLED"] = "1"
        os.environ["NETLINK_CONTROL_PLANE_URL"] = "http://localhost:8000"
        os.environ["NETLINK_CONTROL_PLANE_INTERNAL_API_KEY"] = TEST_KEY
        os.environ["NETLINK_V2_PILOT_TELEGRAM_IDS"] = "111"
        self.assertFalse(settings.is_enabled())


class TestEnrollmentUri(unittest.TestCase):
    def test_roundtrip(self):
        uri = enrollment_uri.build("NL-ABC12-DEF34")
        self.assertEqual(uri, "netlink://enroll?code=NL-ABC12-DEF34")
        self.assertEqual(enrollment_uri.parse(uri), "NL-ABC12-DEF34")

    def test_special_characters_are_quoted(self):
        code = "NL-A B+C/D"
        uri = enrollment_uri.build(code)
        self.assertNotIn(" ", uri)
        self.assertEqual(enrollment_uri.parse(uri), code)

    def test_parse_rejects_wrong_host(self):
        with self.assertRaises(ValueError):
            enrollment_uri.parse("netlink://other?code=X")

    def test_parse_rejects_missing_code(self):
        with self.assertRaises(ValueError):
            enrollment_uri.parse("netlink://enroll?nope=1")


class TestControlPlaneClient(unittest.TestCase):
    def _client(self, script=None):
        transport = FakeTransport(script)
        client = ControlPlaneClient(
            base_url="http://cp.test", internal_api_key=TEST_KEY, transport=transport
        )
        return client, transport

    def test_internal_key_header_never_in_default_loggers(self):
        client, transport = self._client(
            script=[(500, {"error": {"code": "x"}}), (500, {"error": {"code": "x"}})]
        )
        stream = []
        handler = logging.Handler()
        handler.emit = lambda record: stream.append(record.getMessage())
        logging.getLogger().addHandler(handler)
        try:
            with self.assertRaises(ControlPlaneUnavailable):
                run(client.get_employee_by_telegram(5))
        finally:
            logging.getLogger().removeHandler(handler)
        joined = "\n".join(stream)
        self.assertNotIn(TEST_KEY, joined)
        # the request DID carry the key
        self.assertEqual(transport.calls[0]["headers"]["X-Internal-Api-Key"], TEST_KEY)

    def test_get_employee_200(self):
        client, transport = self._client(script=[(200, {"id": "e1", "telegram_user_id": 5})])
        employee = run(client.get_employee_by_telegram(5))
        self.assertEqual(employee["id"], "e1")
        self.assertEqual(transport.calls[0]["path"], "/v1/employees/by-telegram/5")

    def test_get_employee_404_maps_to_employee_not_found(self):
        client, _ = self._client(
            script=[(404, {"error": {"code": "employee_not_found"}})]
        )
        with self.assertRaises(EmployeeNotFound):
            run(client.get_employee_by_telegram(5))

    def test_get_employee_transient_error_single_bounded_retry(self):
        client, transport = self._client(
            script=[
                ControlPlaneUnavailable("boom"),
                (200, {"id": "e1"}),
            ]
        )
        employee = run(client.get_employee_by_telegram(5))
        self.assertEqual(employee["id"], "e1")
        self.assertEqual(len(transport.calls), 2)

    def test_get_employee_unavailable_after_retry(self):
        client, transport = self._client(
            script=[ControlPlaneUnavailable("boom"), ControlPlaneUnavailable("boom")]
        )
        with self.assertRaises(ControlPlaneUnavailable):
            run(client.get_employee_by_telegram(5))
        self.assertEqual(len(transport.calls), 2, "exactly one bounded retry")

    def test_unauthorized_mapping(self):
        client, _ = self._client(script=[(401, {"error": {"code": "invalid_internal_api_key"}})])
        with self.assertRaises(ControlPlaneUnauthorized):
            run(client.get_employee_by_telegram(5))

    def test_unexpected_response_mapping(self):
        client, _ = self._client(script=[(418, {"error": {"code": "teapot"}})])
        with self.assertRaises(UnexpectedResponse):
            run(client.get_employee_by_telegram(5))

    def test_create_employee_conflict(self):
        client, _ = self._client(script=[(409, {"error": {"code": "employee_exists"}})])
        with self.assertRaises(ControlPlaneConflict):
            run(client.create_employee(5, "Test User"))

    def test_create_employee_post_is_not_retried(self):
        client, transport = self._client(script=[ControlPlaneUnavailable("boom")])
        with self.assertRaises(ControlPlaneUnavailable):
            run(client.create_employee(5, "Test User"))
        self.assertEqual(len(transport.calls), 1, "POST must not retry automatically")

    def test_create_enrollment_returns_code_once(self):
        client, transport = self._client(
            script=[
                (201, {"id": "enr1", "code": "NL-AAAAA-BBBBB", "expires_at": "2026-10-29T20:00:00+00:00"}),
            ]
        )
        enrollment = run(client.create_enrollment("employee-uuid"))
        self.assertEqual(enrollment["code"], "NL-AAAAA-BBBBB")
        self.assertEqual(transport.calls[0]["path"], "/v1/enrollments")
        self.assertEqual(transport.calls[0]["json"], {"employee_id": "employee-uuid"})


class TestEmployeeResolutionFlow(unittest.TestCase):
    class RecordingClient:
        def __init__(self, get_results, create_results):
            self.get_results = list(get_results)
            self.create_results = list(create_results)
            self.calls = []

        async def get_employee_by_telegram(self, telegram_user_id):
            self.calls.append(("get", telegram_user_id))
            step = self.get_results.pop(0)
            if isinstance(step, Exception):
                raise step
            return step

        async def create_employee(self, telegram_user_id, full_name):
            self.calls.append(("create", telegram_user_id, full_name))
            step = self.create_results.pop(0)
            if isinstance(step, Exception):
                raise step
            return step

        async def create_enrollment(self, employee_id):
            self.calls.append(("enroll", employee_id))
            return {"id": "enr", "code": "NL-AAAAA-BBBBB", "expires_at": "2026-10-29T20:00:00+00:00"}

    def test_existing_employee_reused_no_create(self):
        client = self.RecordingClient(get_results=[{"id": "e1"}], create_results=[])
        employee = run(resolve_employee(client, 42, "Any Name"))
        self.assertEqual(employee["id"], "e1")
        self.assertEqual(client.calls, [("get", 42)])

    def test_missing_employee_created(self):
        client = self.RecordingClient(
            get_results=[EmployeeNotFound("x")], create_results=[{"id": "e2"}]
        )
        employee = run(resolve_employee(client, 42, "Ivan Ivanov"))
        self.assertEqual(employee["id"], "e2")
        self.assertEqual(
            client.calls, [("get", 42), ("create", 42, "Ivan Ivanov")]
        )

    def test_duplicate_race_resolved_by_single_get_retry(self):
        client = self.RecordingClient(
            get_results=[EmployeeNotFound("x"), {"id": "e3"}],
            create_results=[ControlPlaneConflict("employee_exists")],
        )
        employee = run(resolve_employee(client, 42, "Ivan Ivanov"))
        self.assertEqual(employee["id"], "e3")
        self.assertEqual(len(client.calls), 3, "get -> create(conflict) -> get")

    def test_unexpected_errors_propagate(self):
        client = self.RecordingClient(
            get_results=[ControlPlaneUnauthorized("bad key")], create_results=[]
        )
        with self.assertRaises(ControlPlaneUnauthorized):
            run(resolve_employee(client, 42, "Ivan"))


class TestTelegramName(unittest.TestCase):
    def test_first_and_last(self):
        self.assertEqual(build_full_name("Ivan", "Ivanov", 123), "Ivan Ivanov")

    def test_first_only(self):
        self.assertEqual(build_full_name("Ivan", "", 123), "Ivan")

    def test_fallback_when_empty(self):
        self.assertEqual(build_full_name("", None, 123456), "Telegram user 123456")
        self.assertEqual(build_full_name("  ", "  ", 123456), "Telegram user 123456")


if __name__ == "__main__":
    unittest.main()


# --------------------------------------- review fix: base URL origin policy

class TestBaseUrlPolicy(unittest.TestCase):
    """HTTPS-only for non-loopback; loopback HTTP allowed for dev."""

    def setUp(self):
        self.env = os.environ.copy()
        for name in (
            "NETLINK_V2_ENABLED",
            "NETLINK_CONTROL_PLANE_URL",
            "NETLINK_CONTROL_PLANE_INTERNAL_API_KEY",
            "NETLINK_V2_PILOT_TELEGRAM_IDS",
        ):
            os.environ.pop(name, None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env)

    def _enable(self, url: str) -> None:
        os.environ["NETLINK_V2_ENABLED"] = "true"
        os.environ["NETLINK_CONTROL_PLANE_URL"] = url
        os.environ["NETLINK_CONTROL_PLANE_INTERNAL_API_KEY"] = TEST_KEY
        os.environ["NETLINK_V2_PILOT_TELEGRAM_IDS"] = "1"

    def test_https_host_valid(self):
        self._enable("https://cp.example.com")
        self.assertTrue(settings.is_enabled())
        self.assertTrue(settings.is_pilot_user(1))

    def test_https_trailing_slash_normalized(self):
        self._enable("https://cp.example.com/")
        loaded = settings.load_v2_settings()
        self.assertEqual(loaded.base_url, "https://cp.example.com")

    def test_plain_http_non_loopback_invalid(self):
        self._enable("http://cp.example.com")
        self.assertFalse(settings.is_enabled())
        self.assertFalse(settings.is_pilot_user(1))

    def test_loopback_http_valid(self):
        for url, expected in (
            ("http://localhost:8000", "http://localhost:8000"),
            ("http://127.0.0.1:8000", "http://127.0.0.1:8000"),
            ("http://[::1]:8000", "http://[::1]:8000"),
        ):
            with self.subTest(url=url):
                self._enable(url)
                self.assertTrue(settings.is_enabled(), url)
                self.assertEqual(settings.load_v2_settings().base_url, expected)

    def test_userinfo_forbidden(self):
        self._enable("https://user:secretpass@cp.example.com")
        self.assertFalse(settings.is_enabled())
        problems = " ".join(settings.config_problems())
        self.assertNotIn("secretpass", problems)

    def test_path_prefix_forbidden(self):
        self._enable("https://cp.example.com/api")
        self.assertFalse(settings.is_enabled())

    def test_query_forbidden(self):
        self._enable("https://cp.example.com?x=1")
        self.assertFalse(settings.is_enabled())

    def test_fragment_forbidden(self):
        self._enable("https://cp.example.com#frag")
        self.assertFalse(settings.is_enabled())

    def test_malformed_url_invalid(self):
        for url in ("not-a-url", "http://", "ftp://cp.example.com", ""):
            with self.subTest(url=url):
                self._enable(url)
                self.assertFalse(settings.is_enabled(), url)

    def test_invalid_url_disables_feature_entirely(self):
        self._enable("http://cp.example.com")  # flag=true, pilot set, bad url
        self.assertFalse(settings.is_enabled())
        self.assertFalse(settings.is_pilot_user(1))
        self.assertTrue(any("URL" in p for p in settings.config_problems()))

    def test_problems_never_echo_url_credentials(self):
        self._enable("https://alice:hunter2@cp.example.com")
        problems = " ".join(settings.config_problems())
        self.assertNotIn("hunter2", problems)
        self.assertNotIn("alice", problems)


# --------------------------------------- review fix: redirects forbidden

class TestRedirectRejection(unittest.TestCase):
    def _client(self, status: int):
        transport = FakeTransport(script=[(status, None)])
        client = ControlPlaneClient(
            base_url="https://cp.test", internal_api_key=TEST_KEY, transport=transport
        )
        return client, transport

    def test_3xx_responses_rejected_not_followed(self):
        for status in (301, 302, 307, 308):
            with self.subTest(status=status):
                client, transport = self._client(status)
                with self.assertRaises(UnexpectedResponse) as ctx:
                    run(client.get_employee_by_telegram(5))
                message = str(ctx.exception)
                self.assertIn("redirect", message.lower())
                # exactly one request: the redirect itself, never a follow-up
                self.assertEqual(len(transport.calls), 1)

    def test_redirect_error_message_has_no_location(self):
        client, _ = self._client(302)
        with self.assertRaises(UnexpectedResponse) as ctx:
            run(client.get_employee_by_telegram(5))
        message = str(ctx.exception)
        self.assertNotIn("http://", message)
        self.assertNotIn("Location", message)
