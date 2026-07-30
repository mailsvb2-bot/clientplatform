from __future__ import annotations

import importlib
import importlib.util
import unittest
from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import patch
from uuid import uuid4

from clientplatform.application.program_media import ProgramMediaIngestPolicy
from clientplatform.domain.programs import ContentKind

AIOGRAM_AVAILABLE = importlib.util.find_spec("aiogram") is not None
router_module: Any = None
if AIOGRAM_AVAILABLE:
    router_module = importlib.import_module("handlers.clientplatform_program_media_router")


class FakeUser:
    id = 101


class FakeMessage:
    def __init__(self) -> None:
        self.from_user = FakeUser()
        self.answers: list[str] = []

    async def answer(self, text: str, **_kwargs: Any) -> None:
        self.answers.append(text)


class FakeState:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = dict(data)
        self.clear_count = 0

    async def get_data(self) -> dict[str, Any]:
        return dict(self.data)

    async def update_data(self, **kwargs: Any) -> None:
        self.data.update(kwargs)

    async def set_state(self, _value: Any) -> None:
        return None

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
class ProgramMediaCompensationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.thread_patch = patch.object(
            router_module.asyncio,
            "to_thread",
            direct_to_thread,
        )
        self.thread_patch.start()

    async def asyncTearDown(self) -> None:
        self.thread_patch.stop()

    async def test_successful_replacement_cancels_new_and_queues_old(self) -> None:
        business_id = str(uuid4())
        lesson_id = str(uuid4())
        actor = object()
        old_reference = (
            "s3://clientplatform-production/program-media/old/audio/aa/old.mp3"
        )
        new_reference = (
            "s3://clientplatform-production/program-media/new/audio/bb/new.mp3"
        )
        previous = SimpleNamespace(id=lesson_id, content_ref=old_reference)
        replacement = SimpleNamespace(id=lesson_id, content_ref=new_reference)
        record = SimpleNamespace(program=SimpleNamespace(title="Черновик"), lessons=())
        cancelled: list[str] = []
        queued: list[tuple[str, str]] = []

        async def actor_for(_user_id: int, selected_business_id: str) -> object:
            self.assertEqual(selected_business_id, business_id)
            return actor

        async def load_lesson(**_kwargs: Any) -> tuple[Any, Any, Any]:
            return actor, record, previous

        async def materialize(_message: Any, *, business_id: str) -> tuple[Any, str]:
            self.assertTrue(business_id)
            return ContentKind.AUDIO, new_reference

        def replace(**kwargs: Any) -> tuple[Any, Any]:
            self.assertEqual(kwargs["content_ref"], new_reference)
            return record, replacement

        async def detail(*_args: Any, **_kwargs: Any) -> None:
            return None

        with (
            patch.object(router_module.control, "_actor", actor_for),
            patch.object(router_module.editor, "_load_lesson", load_lesson),
            patch.object(router_module, "materialize_program_content", materialize),
            patch.object(
                router_module,
                "program_media_ingest_policy",
                return_value=ProgramMediaIngestPolicy(True, 20_000_000, 30.0),
            ),
            patch.object(router_module, "stage_program_media_cleanup", return_value=True),
            patch.object(
                router_module,
                "cancel_program_media_cleanup",
                side_effect=lambda *, media_reference: cancelled.append(media_reference) or True,
            ),
            patch.object(
                router_module,
                "queue_program_media_cleanup",
                side_effect=lambda *, business_id, media_reference, reason: (
                    queued.append((media_reference, reason)) or True
                ),
            ),
            patch.object(
                router_module.editor,
                "replace_program_draft_lesson_content",
                replace,
            ),
            patch.object(router_module.editor, "_send_lesson_detail", detail),
        ):
            state = FakeState(
                {
                    "editor_business_id": business_id,
                    "editor_lesson_id": lesson_id,
                }
            )
            await router_module.replace_persistent_lesson_content(FakeMessage(), state)

        self.assertEqual(cancelled, [new_reference])
        self.assertEqual(queued, [(old_reference, "superseded_lesson_material")])
        self.assertEqual(state.clear_count, 1)

    async def test_failed_add_expedites_new_object_cleanup(self) -> None:
        business_id = str(uuid4())
        program_id = str(uuid4())
        new_reference = (
            "s3://clientplatform-production/program-media/new/document/cc/new.pdf"
        )
        queued: list[tuple[str, str]] = []

        async def load_draft(**_kwargs: Any) -> Any:
            return SimpleNamespace(lessons=())

        async def actor_for(_user_id: int, _business_id: str) -> object:
            return object()

        async def materialize(_message: Any, *, business_id: str) -> tuple[Any, str]:
            self.assertTrue(business_id)
            return ContentKind.DOCUMENT, new_reference

        def fail_add(**_kwargs: Any) -> None:
            raise RuntimeError("database write failed")

        with (
            patch.object(router_module.builder, "_load_draft", load_draft),
            patch.object(router_module.control, "_actor", actor_for),
            patch.object(router_module, "materialize_program_content", materialize),
            patch.object(router_module, "stage_program_media_cleanup", return_value=True),
            patch.object(
                router_module,
                "queue_program_media_cleanup",
                side_effect=lambda *, business_id, media_reference, reason: (
                    queued.append((media_reference, reason)) or True
                ),
            ),
            patch.object(router_module.builder, "add_program_lesson", fail_add),
        ):
            state = FakeState(
                {
                    "business_id": business_id,
                    "program_id": program_id,
                    "lesson_title": "Документ",
                }
            )
            with self.assertRaisesRegex(RuntimeError, "database write failed"):
                await router_module.capture_persistent_lesson_content(
                    FakeMessage(),
                    state,
                )

        self.assertEqual(queued, [(new_reference, "failed_lesson_add")])


if __name__ == "__main__":
    unittest.main()
