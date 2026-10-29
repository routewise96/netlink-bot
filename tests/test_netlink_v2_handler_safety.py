"""Local-only handler-safety tests (require aiogram; CI skips).

Proves the pilot flow degrades to the safe "unavailable" message for
configuration/construction failures without leaking secrets and without
propagating exceptions into the polling loop.
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest
from pathlib import Path

try:
    import importlib.util

    _AIOGRAM = importlib.util.find_spec("aiogram") is not None
except ImportError:  # pragma: no cover
    _AIOGRAM = False

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

KEY = "handler-test-internal-api-key-0123456"


class FakeUser:
    def __init__(self, user_id: int) -> None:
        self.id = user_id
        self.first_name = "Pilot"
        self.last_name = "User"


class FakeMessage:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def answer(self, text: str, parse_mode: str | None = None, **_: object) -> None:
        self.sent.append(text)


class FakeCallback:
    def __init__(self, user_id: int) -> None:
        self.from_user = FakeUser(user_id)
        self.message = FakeMessage()
        self.answers: list[tuple] = []

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))


@unittest.skipUnless(_AIOGRAM, "aiogram not installed (CI stdlib-only run)")
class TestHandlerSafety(unittest.TestCase):
    def setUp(self):
        self.env = os.environ.copy()
        os.environ["NETLINK_V2_ENABLED"] = "true"
        os.environ["NETLINK_CONTROL_PLANE_URL"] = "http://localhost:8000"
        os.environ["NETLINK_CONTROL_PLANE_INTERNAL_API_KEY"] = KEY
        os.environ["NETLINK_V2_PILOT_TELEGRAM_IDS"] = "777"
        from bot.handlers import netlink_v2 as handler

        self.handler = handler
        self.get_user_orig = handler.db.get_user

        async def approved(_tid: int):
            return {"status": "approved"}

        handler.db.get_user = approved

    def tearDown(self):
        self.handler.db.get_user = self.get_user_orig
        os.environ.clear()
        os.environ.update(self.env)

    def test_invalid_url_config_gates_before_any_client(self):
        os.environ["NETLINK_CONTROL_PLANE_URL"] = "http://non-loopback.example"
        callback = FakeCallback(777)
        asyncio.run(self.handler.v2_enroll(callback))
        self.assertEqual(callback.answers, [("Недоступно", True)])
        self.assertEqual(callback.message.sent, [])

    def test_client_construction_failure_yields_safe_message(self):
        original = self.handler.ControlPlaneClient

        class BrokenClient:
            def __init__(self, *, base_url: str, internal_api_key: str) -> None:
                raise ValueError("Cannot write to closing transport")

        self.handler.ControlPlaneClient = BrokenClient  # type: ignore[assignment]
        try:
            callback = FakeCallback(777)
            asyncio.run(self.handler.v2_enroll(callback))
        finally:
            self.handler.ControlPlaneClient = original  # type: ignore[assignment]
        self.assertEqual(len(callback.message.sent), 1)
        self.assertIn("временно недоступно", callback.message.sent[0])
        self.assertNotIn(KEY, callback.message.sent[0])

    def test_control_plane_error_yields_safe_message(self):
        from bot.services.netlink_v2.client import ControlPlaneUnavailable

        class FailingClient:
            def __init__(self, *, base_url: str, internal_api_key: str) -> None:
                pass

            async def close(self) -> None:
                pass

            async def get_employee_by_telegram(self, telegram_user_id: int):
                raise ControlPlaneUnavailable("control plane transport error: XYZ")

            async def create_employee(self, telegram_user_id: int, full_name: str):
                raise AssertionError("must not be reached")

            async def create_enrollment(self, employee_id: str):
                raise AssertionError("must not be reached")

        original = self.handler.ControlPlaneClient
        self.handler.ControlPlaneClient = FailingClient  # type: ignore[assignment]
        try:
            callback = FakeCallback(777)
            asyncio.run(self.handler.v2_enroll(callback))
        finally:
            self.handler.ControlPlaneClient = original  # type: ignore[assignment]
        self.assertEqual(len(callback.message.sent), 1)
        self.assertIn("временно недоступно", callback.message.sent[0])
        self.assertNotIn(KEY, callback.message.sent[0])

    def test_non_pilot_user_rejected_before_any_work(self):
        callback = FakeCallback(999)
        asyncio.run(self.handler.v2_enroll(callback))
        self.assertEqual(callback.answers, [("Недоступно", True)])
        self.assertEqual(callback.message.sent, [])


if __name__ == "__main__":
    unittest.main()
