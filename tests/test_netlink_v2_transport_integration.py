"""Local-only integration tests for the v2 transport (require aiohttp).

CI runs the suite without dependencies installed; these tests skip there and
run in the development venv. They prove, with real HTTP on two loopback
origins, that redirects are NOT followed and the internal API key never
reaches the redirect target.
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

try:
    import importlib.util

    _AIOHTTP = importlib.util.find_spec("aiohttp") is not None
except ImportError:  # pragma: no cover
    _AIOHTTP = False

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.services.netlink_v2.client import (  # noqa: E402
    AiohttpTransport,
    ControlPlaneClient,
    UnexpectedResponse,
)

TEST_KEY = "integration-test-internal-api-key-0123"


@unittest.skipUnless(_AIOHTTP, "aiohttp not installed (CI stdlib-only run)")
class TestCrossOriginRedirect(unittest.TestCase):
    def test_redirect_not_followed_and_credential_never_forwarded(self):
        from aiohttp import web

        target_requests: list[dict] = []

        async def target(request):
            target_requests.append(dict(request.headers))
            return {"json": True}

        async def redirector(request):
            raise web.HTTPFound("http://127.0.0.1:9972/target")

        async def scenario() -> None:
            app_a = web.Application()
            app_a.router.add_route("*", "/{tail:.*}", redirector)
            app_b = web.Application()
            app_b.router.add_get("/target", target)
            runner_a = web.AppRunner(app_a)
            runner_b = web.AppRunner(app_b)
            await runner_a.setup()
            await runner_b.setup()
            site_a = web.TCPSite(runner_a, "127.0.0.1", 9971)
            site_b = web.TCPSite(runner_b, "127.0.0.1", 9972)
            await site_a.start()
            await site_b.start()
            try:
                client = ControlPlaneClient(
                    base_url="http://127.0.0.1:9971",
                    internal_api_key=TEST_KEY,
                )
                try:
                    with self.assertRaises(UnexpectedResponse) as ctx:
                        await client.get_employee_by_telegram(1)
                    self.assertIn("redirect", str(ctx.exception).lower())
                finally:
                    await client.close()
            finally:
                await runner_a.cleanup()
                await runner_b.cleanup()

        asyncio.run(scenario())
        self.assertEqual(
            target_requests,
            [],
            "redirect target must receive ZERO requests (no credential forwarding)",
        )


@unittest.skipUnless(_AIOHTTP, "aiohttp not installed (CI stdlib-only run)")
class TestTransportNoRedirectsParam(unittest.TestCase):
    def test_session_request_uses_allow_redirects_false(self):
        import inspect

        source = inspect.getsource(AiohttpTransport.request)
        self.assertIn("allow_redirects=False", source)


if __name__ == "__main__":
    unittest.main()
