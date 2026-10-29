"""NetLink 2.0 pilot integration (Control Plane enrollment).

Kept stdlib-only at import time so the CI test suite (which installs no
dependencies) can exercise all gating/contract logic. The HTTP transport
(aiohttp) is imported lazily inside the transport implementation only.
"""
