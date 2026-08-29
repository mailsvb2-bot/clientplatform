from __future__ import annotations

import importlib.util
import unittest


_AIOHTTP_AVAILABLE = importlib.util.find_spec("aiohttp") is not None


class _Request:
    def __init__(
        self,
        *,
        path: str,
        body: bytes = b"{}",
        content_length: int | None = None,
        method: str = "POST",
    ) -> None:
        self.path = path
        self.method = method
        self.content_length = (
            len(body) if content_length is None else content_length
        )
        self._body = body
        self.reads = 0

    async def read(self) -> bytes:
        self.reads += 1
        return self._body


@unittest.skipUnless(
    _AIOHTTP_AVAILABLE,
    "aiohttp runtime dependency is not installed in dependency-light Canon",
)
class NativeMessengerHttpAdmissionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        from clientplatform.runtime import native_messenger_http_admission

        self.admission = native_messenger_http_admission
        self.admission.reset_native_messenger_http_admission_state_for_tests()

    async def test_canonical_webhook_rejects_oversized_body_before_read(self) -> None:
        request = _Request(
            path="/clientplatform/webhooks/max/route-id",
            content_length=262_145,
        )
        called = False

        from aiohttp import web

        async def handler(_request: object) -> web.Response:
            nonlocal called
            called = True
            return web.Response(text="unexpected")

        response = await self.admission.native_messenger_http_admission_middleware(
            request,  # type: ignore[arg-type]
            handler,  # type: ignore[arg-type]
        )

        self.assertEqual(response.status, 413)
        self.assertFalse(called)
        self.assertEqual(request.reads, 0)
        self.assertEqual(response.headers["Cache-Control"], "no-store, max-age=0")

    async def test_setup_post_uses_narrow_body_limit_and_releases_slot(self) -> None:
        request = _Request(
            path="/clientplatform/connect/capability",
            body=b"provider_token=value",
        )

        from aiohttp import web

        async def handler(_request: object) -> web.Response:
            return web.Response(text="ok")

        first = await self.admission.native_messenger_http_admission_middleware(
            request,  # type: ignore[arg-type]
            handler,  # type: ignore[arg-type]
        )
        second = await self.admission.native_messenger_http_admission_middleware(
            request,  # type: ignore[arg-type]
            handler,  # type: ignore[arg-type]
        )

        self.assertEqual(first.status, 200)
        self.assertEqual(second.status, 200)
        self.assertEqual(request.reads, 2)

    async def test_external_product_rejects_oversized_body_before_read(self) -> None:
        request = _Request(
            path="/clientplatform/external-products/connector/events",
            content_length=65_537,
        )
        called = False

        from aiohttp import web

        async def handler(_request: object) -> web.Response:
            nonlocal called
            called = True
            return web.Response(text="unexpected")

        response = await self.admission.native_messenger_http_admission_middleware(
            request,  # type: ignore[arg-type]
            handler,  # type: ignore[arg-type]
        )

        self.assertEqual(response.status, 413)
        self.assertFalse(called)
        self.assertEqual(request.reads, 0)

    async def test_external_product_slot_is_released_after_handler(self) -> None:
        request = _Request(
            path="/clientplatform/external-products/connector/events",
            body=b"{}",
        )

        from aiohttp import web

        async def handler(_request: object) -> web.Response:
            return web.Response(text="ok")

        first = await self.admission.native_messenger_http_admission_middleware(
            request,  # type: ignore[arg-type]
            handler,  # type: ignore[arg-type]
        )
        second = await self.admission.native_messenger_http_admission_middleware(
            request,  # type: ignore[arg-type]
            handler,  # type: ignore[arg-type]
        )
        self.assertEqual(first.status, 200)
        self.assertEqual(second.status, 200)
        self.assertEqual(request.reads, 2)

    async def test_unrelated_route_bypasses_native_admission(self) -> None:
        request = _Request(
            path="/health",
            content_length=10_000_000,
            method="GET",
        )

        from aiohttp import web

        async def handler(_request: object) -> web.Response:
            return web.Response(text="healthy")

        response = await self.admission.native_messenger_http_admission_middleware(
            request,  # type: ignore[arg-type]
            handler,  # type: ignore[arg-type]
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(request.reads, 0)


if __name__ == "__main__":
    unittest.main()
