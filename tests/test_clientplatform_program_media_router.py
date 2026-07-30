from __future__ import annotations

import importlib
import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import patch
from uuid import uuid4

from clientplatform.domain.programs import ContentKind

AIOGRAM_AVAILABLE = importlib.util.find_spec("aiogram") is not None
media_router: Any = None
ProgramMediaIngestError: Any = RuntimeError
if AIOGRAM_AVAILABLE:
    media_router = importlib.import_module("handlers.clientplatform_program_media_router")
    ProgramMediaIngestError = importlib.import_module(
        "handlers.clientplatform_program_media"
    ).ProgramMediaIngestError


class FakeUser:
    def __init__(self, user_id: int = 101) -> None:
        self.id = user_id


class FakeMessage:
    def __init__(self, *, user_id: int = 101) -> None:
        self.from_user = FakeUser(user_id)
        self.answers: list[tuple[str, dict[str, Any]]] = []

    async def answer(self, text: str, **kwargs: Any) -> None:
        self.answers.append((text, kwargs))


class FakeState:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = dict(data)
        self.states: list[Any] = []
        self.clear_count = 0

    async def get_data(self) -> dict[str, Any]:
        return dict(self.data)

    async def update_data(self, **kwargs: Any) -> None:
        self.data.update(kwargs)

    async def set_state(self, value: Any) -> None:
        self.states.append(value)

    async def clear(self) -> None:
        self.clear_count += 1
        self.data.clear()


async def direct_to_thread(
    func: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    return func(*args, **kwargs)


@unittest.skipUnless(AIOGRAM_AVAILABLE, "aiogram is not installed")
class ProgramMediaRouterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.to_thread_patch = patch.object(
            media_router.asyncio,
            "to_thread",
            direct_to_thread,
        )
        self.to_thread_patch.start()

    async def asyncTearDown(self) -> None:
        self.to_thread_patch.stop()

    async def test_builder_persists_only_externalized_reference(self) -> None:
        business_id = str(uuid4())
        program_id = str(uuid4())
        actor = object()
        initial = SimpleNamespace(lessons=())
        updated = SimpleNamespace(lessons=(SimpleNamespace(id=str(uuid4())),))
        writes: list[dict[str, Any]] = []
        reviews: list[Any] = []

        async def materialize(_message: Any, *, business_id: str) -> tuple[Any, str]:
            self.assertTrue(business_id)
            return (
                ContentKind.AUDIO,
                "s3://clientplatform-production/program-media/audio.ogg",
            )

        async def load_draft(**_kwargs: Any) -> Any:
            return initial

        async def resolve_actor(_user_id: int, selected_business_id: str) -> object:
            self.assertEqual(selected_business_id, business_id)
            return actor

        def add_lesson(**kwargs: Any) -> None:
            writes.append(kwargs)

        def get_draft(**_kwargs: Any) -> Any:
            return updated

        async def send_review(_message: Any, record: Any) -> None:
            reviews.append(record)

        with (
            patch.object(media_router, "materialize_program_content", materialize),
            patch.object(media_router.builder, "_load_draft", load_draft),
            patch.object(media_router.control, "_actor", resolve_actor),
            patch.object(media_router.builder, "add_program_lesson", add_lesson),
            patch.object(media_router.builder, "get_program_draft", get_draft),
            patch.object(media_router.builder, "_send_draft_review", send_review),
        ):
            state = FakeState(
                {
                    "business_id": business_id,
                    "program_id": program_id,
                    "lesson_title": "Аудиоурок",
                }
            )
            await media_router.capture_persistent_lesson_content(FakeMessage(), state)

        self.assertEqual(len(writes), 1)
        self.assertIs(writes[0]["actor"], actor)
        self.assertEqual(writes[0]["content_kind"], ContentKind.AUDIO)
        self.assertTrue(writes[0]["content_ref"].startswith("s3://"))
        self.assertNotIn("control-bot", writes[0]["content_ref"])
        self.assertEqual(reviews, [updated])
        self.assertEqual(state.data["lesson_title"], "")

    async def test_ingest_failure_never_mutates_builder_draft(self) -> None:
        business_id = str(uuid4())
        program_id = str(uuid4())
        writes: list[dict[str, Any]] = []

        async def fail_ingest(_message: Any, *, business_id: str) -> tuple[Any, str]:
            self.assertTrue(business_id)
            raise ProgramMediaIngestError(
                "program_media_upload_transport_failure",
                retryable=True,
            )

        async def load_draft(**_kwargs: Any) -> Any:
            return SimpleNamespace(lessons=())

        with (
            patch.object(media_router, "materialize_program_content", fail_ingest),
            patch.object(media_router.builder, "_load_draft", load_draft),
            patch.object(
                media_router.builder,
                "add_program_lesson",
                lambda **kwargs: writes.append(kwargs),
            ),
        ):
            message = FakeMessage()
            state = FakeState(
                {
                    "business_id": business_id,
                    "program_id": program_id,
                    "lesson_title": "Документ",
                }
            )
            await media_router.capture_persistent_lesson_content(message, state)

        self.assertEqual(writes, [])
        self.assertEqual(state.data["lesson_title"], "Документ")
        self.assertIn("Попробуйте отправить его ещё раз", message.answers[-1][0])

    async def test_editor_replaces_material_only_after_externalization(self) -> None:
        business_id = str(uuid4())
        lesson_id = str(uuid4())
        actor = object()
        writes: list[dict[str, Any]] = []
        detail_calls: list[tuple[Any, Any]] = []
        record = SimpleNamespace(program=SimpleNamespace(title="Черновик"), lessons=())
        lesson = SimpleNamespace(id=lesson_id)

        async def materialize(_message: Any, *, business_id: str) -> tuple[Any, str]:
            self.assertTrue(business_id)
            return (
                ContentKind.DOCUMENT,
                "s3://clientplatform-production/program-media/file.pdf",
            )

        async def resolve_actor(_user_id: int, selected_business_id: str) -> object:
            self.assertEqual(selected_business_id, business_id)
            return actor

        def replace(**kwargs: Any) -> tuple[Any, Any]:
            writes.append(kwargs)
            return record, lesson

        async def send_detail(_message: Any, *, record: Any, lesson: Any) -> None:
            detail_calls.append((record, lesson))

        with (
            patch.object(media_router, "materialize_program_content", materialize),
            patch.object(media_router.control, "_actor", resolve_actor),
            patch.object(
                media_router.editor,
                "replace_program_draft_lesson_content",
                replace,
            ),
            patch.object(media_router.editor, "_send_lesson_detail", send_detail),
        ):
            state = FakeState(
                {
                    "editor_business_id": business_id,
                    "editor_lesson_id": lesson_id,
                }
            )
            await media_router.replace_persistent_lesson_content(FakeMessage(), state)

        self.assertTrue(writes[0]["content_ref"].startswith("s3://"))
        self.assertEqual(writes[0]["lesson_id"], lesson_id)
        self.assertEqual(state.clear_count, 1)
        self.assertEqual(detail_calls, [(record, lesson)])


class ProgramMediaRouterCompositionTests(unittest.TestCase):
    def test_media_router_is_first_in_canonical_entry_composition(self) -> None:
        source = Path("handlers/clientplatform_entry.py").read_text(encoding="utf-8")
        media = source.index("router.include_router(program_media.router)")
        editor = source.index("router.include_router(lesson_editor.router)")
        builder = source.index("router.include_router(program_builder.router)")
        legacy = source.index("router.include_router(original_router)")
        self.assertLess(media, editor)
        self.assertLess(editor, builder)
        self.assertLess(builder, legacy)
        self.assertIn("control.router = router", source)


if __name__ == "__main__":
    unittest.main()
