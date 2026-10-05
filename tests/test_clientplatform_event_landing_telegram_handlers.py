from __future__ import annotations

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from clientplatform.domain.tenancy import PlatformRole, TenantContext

try:
    from handlers import clientplatform_events as events
except ImportError:  # dependency-light Canon intentionally omits aiogram
    events = None


_BUSINESS = "11111111-1111-4111-8111-111111111111"
_EVENT = "33333333-3333-4333-8333-333333333333"
_MEMBER = "22222222-2222-4222-8222-222222222222"


def _actor() -> TenantContext:
    return TenantContext(
        business_id=_BUSINESS,
        user_id=101,
        membership_id=_MEMBER,
        role=PlatformRole.OWNER,
    )


def _draft(*, subtitle: str = "Подзаголовок"):
    return SimpleNamespace(
        hero_title="Заголовок",
        hero_subtitle=subtitle,
        audience_points=("Аудитория",),
        outcome_points=("Польза",),
        agenda_points=("Шаг",),
        faq=(),
        theme=events.EventLandingTheme.CALM,
    )


def _profile(
    *,
    published: bool = False,
    changed: bool = False,
    ai_status: str | None = None,
):
    return SimpleNamespace(
        draft=_draft(),
        draft_source="template",
        revision=2,
        is_published=published,
        has_unpublished_changes=changed,
        ai_status=ai_status,
    )


def _callback(data: str):
    target = SimpleNamespace(answer=AsyncMock())
    callback = SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=101),
        answer=AsyncMock(),
        message=target,
    )
    return callback, target


def _token_uuid(token: str) -> str:
    if token == "evt":
        return _EVENT
    if token == "biz":
        return _BUSINESS
    raise ValueError("unexpected token")


def _uuid_token(value: str) -> str:
    if value == _EVENT:
        return "evt"
    if value == _BUSINESS:
        return "biz"
    raise ValueError("unexpected uuid")


@unittest.skipIf(events is None, "aiogram is not installed")
class EventLandingTelegramPresentationTests(unittest.TestCase):
    def test_editor_copy_rows_prompts_and_send_cover_all_states(self) -> None:
        item = SimpleNamespace(title="Вебинар")
        plain = events._landing_editor_text(item, _profile())
        published = events._landing_editor_text(
            item,
            _profile(published=True),
        )
        changed = events._landing_editor_text(
            item,
            _profile(published=True, changed=True, ai_status="ambiguous"),
        )
        self.assertIn("простой лендинг", plain)
        self.assertIn("Опубликован", published)
        self.assertIn("предыдущая версия", changed)
        self.assertIn("неоднозначно", changed)

        with patch.object(events.control, "_uuid_token", side_effect=_uuid_token):
            compact = events._landing_editor_rows(
                event_id=_EVENT,
                business_id=_BUSINESS,
                published=False,
                ai_status=None,
            )
            expanded = events._landing_editor_rows(
                event_id=_EVENT,
                business_id=_BUSINESS,
                published=True,
                ai_status="ambiguous",
            )
        callbacks = [callback for row in expanded for _label, callback in row]
        self.assertFalse(
            any("cpev:lar:" in callback for row in compact for _label, callback in row)
        )
        self.assertTrue(any("cpev:lar:" in callback for callback in callbacks))
        self.assertTrue(any("cpev:ls:" in callback for callback in callbacks))

        for section in ("hero", "audience", "outcomes", "agenda", "speaker", "faq", "cta"):
            self.assertTrue(events._landing_edit_prompt(section))
        with self.assertRaisesRegex(ValueError, "неизвестный"):
            events._landing_edit_prompt("bad")

        target = SimpleNamespace(answer=AsyncMock())
        snapshot = SimpleNamespace(items=(SimpleNamespace(id=_EVENT, title="Вебинар"),))
        actor = _actor()
        with (
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(events, "resolve_cockpit_events", return_value=snapshot),
            patch.object(
                events,
                "get_event_landing_editor_state",
                return_value=_profile(ai_status="ambiguous"),
            ),
            patch.object(events.control, "_uuid_token", side_effect=_uuid_token),
            patch.object(events.control, "_keyboard", side_effect=lambda rows: rows),
        ):
            asyncio.run(
                events._send_event_landing_editor(
                    target,
                    user_id=101,
                    business_id=_BUSINESS,
                    event_id=_EVENT,
                )
            )
        target.answer.assert_awaited_once()

    def test_open_and_ai_confirmation_callbacks_cover_success_and_safe_failure(self) -> None:
        actor = _actor()
        callback, target = _callback("cpev:landing:evt:biz")
        with (
            patch.object(events.control, "_token_uuid", side_effect=_token_uuid),
            patch.object(events.control, "_callback_message", return_value=target),
            patch.object(events, "_send_event_landing_editor", new=AsyncMock()),
        ):
            asyncio.run(events.open_event_landing_builder(callback))
        callback.answer.assert_awaited_once()

        bad, _ = _callback("cpev:landing:evt")
        asyncio.run(events.open_event_landing_builder(bad))
        bad.answer.assert_awaited_once_with("Кнопка устарела", show_alert=True)

        confirm, target = _callback("cpev:la:evt:biz")
        with (
            patch.object(events.control, "_token_uuid", side_effect=_token_uuid),
            patch.object(events.control, "_uuid_token", side_effect=_uuid_token),
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(events.control, "_callback_message", return_value=target),
            patch.object(events.control, "_keyboard", side_effect=lambda rows: rows),
            patch.object(
                events,
                "prepare_event_landing_ai_confirmation",
                return_value=SimpleNamespace(revision=7),
            ),
        ):
            asyncio.run(events.confirm_event_landing_ai(confirm))
        confirm.answer.assert_awaited_once()
        self.assertIn("внешний текстовый AI-вызов", target.answer.await_args.args[0])

        unavailable, unavailable_target = _callback("cpev:la:evt:biz")
        with (
            patch.object(events.control, "_token_uuid", side_effect=_token_uuid),
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(
                events.control,
                "_callback_message",
                return_value=unavailable_target,
            ),
            patch.object(
                events,
                "prepare_event_landing_ai_confirmation",
                side_effect=events.EventLandingAIUnavailable("AI недоступен"),
            ),
        ):
            asyncio.run(events.confirm_event_landing_ai(unavailable))
        unavailable.answer.assert_awaited_once()
        unavailable_target.answer.assert_awaited_once_with("AI недоступен")

    def test_ai_generation_and_ambiguity_recovery_callbacks(self) -> None:
        actor = _actor()
        callback, target = _callback("cpev:laok:evt:biz:7")
        send_editor = AsyncMock()
        with (
            patch.object(events.control, "_token_uuid", side_effect=_token_uuid),
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(events.control, "_callback_message", return_value=target),
            patch.object(events, "generate_event_landing_ai", return_value=_profile()),
            patch.object(events, "_send_event_landing_editor", new=send_editor),
        ):
            asyncio.run(events.generate_event_landing(callback))
        callback.answer.assert_awaited_once_with("Готовлю AI-черновик…")
        self.assertTrue(any("AI-черновик готов" in call.args[0] for call in target.answer.await_args_list))
        send_editor.assert_awaited_once()

        bad_revision, _ = _callback("cpev:laok:evt:biz:not-a-number")
        with patch.object(events.control, "_token_uuid", side_effect=_token_uuid):
            asyncio.run(events.generate_event_landing(bad_revision))
        bad_revision.answer.assert_awaited_once_with("Кнопка устарела", show_alert=True)

        explain, explain_target = _callback("cpev:lar:evt:biz")
        with (
            patch.object(events.control, "_token_uuid", side_effect=_token_uuid),
            patch.object(events.control, "_uuid_token", side_effect=_uuid_token),
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(events.control, "_callback_message", return_value=explain_target),
            patch.object(events.control, "_keyboard", side_effect=lambda rows: rows),
            patch.object(
                events,
                "get_event_landing_editor_state",
                return_value=_profile(ai_status="ambiguous"),
            ),
        ):
            asyncio.run(events.explain_event_landing_ai_ambiguity(explain))
        self.assertIn("Неоднозначный AI-вызов", explain_target.answer.await_args.args[0])

        no_longer, _ = _callback("cpev:lar:evt:biz")
        with (
            patch.object(events.control, "_token_uuid", side_effect=_token_uuid),
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(
                events,
                "get_event_landing_editor_state",
                return_value=_profile(ai_status=None),
            ),
        ):
            asyncio.run(events.explain_event_landing_ai_ambiguity(no_longer))
        no_longer.answer.assert_awaited_once_with(
            "Неопределённого AI-вызова уже нет",
            show_alert=True,
        )

        resolve, resolve_target = _callback("cpev:larok:evt:biz")
        send_editor = AsyncMock()
        with (
            patch.object(events.control, "_token_uuid", side_effect=_token_uuid),
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(events.control, "_callback_message", return_value=resolve_target),
            patch.object(events, "resolve_event_landing_ai_ambiguity", return_value=_profile()),
            patch.object(events, "_send_event_landing_editor", new=send_editor),
        ):
            asyncio.run(events.resolve_event_landing_ai_ambiguity_callback(resolve))
        resolve.answer.assert_awaited_once_with("AI-блокировка снята")
        send_editor.assert_awaited_once()

    def test_manual_edit_callbacks_cover_edit_cancel_stale_success_and_failure(self) -> None:
        actor = _actor()
        callback, target = _callback("cpev:le:h:evt:biz")
        state = SimpleNamespace(
            set_state=AsyncMock(),
            update_data=AsyncMock(),
        )
        with (
            patch.object(events.control, "_token_uuid", side_effect=_token_uuid),
            patch.object(events.control, "_uuid_token", side_effect=_uuid_token),
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(events.control, "_callback_message", return_value=target),
            patch.object(events, "ensure_event_landing_draft", return_value=_profile()),
        ):
            asyncio.run(events.edit_event_landing_section(callback, state))
        state.set_state.assert_awaited_once()
        state.update_data.assert_awaited_once()
        target.answer.assert_awaited_once()

        invalid, _ = _callback("cpev:le:z:evt:biz")
        with patch.object(events.control, "_token_uuid", side_effect=_token_uuid):
            asyncio.run(events.edit_event_landing_section(invalid, state))
        invalid.answer.assert_awaited_once_with("Кнопка устарела", show_alert=True)

        stale_state = SimpleNamespace(
            get_data=AsyncMock(return_value={}),
            clear=AsyncMock(),
        )
        stale_message = SimpleNamespace(
            text="Текст",
            from_user=SimpleNamespace(id=101),
            answer=AsyncMock(),
        )
        asyncio.run(events.receive_event_landing_section(stale_message, stale_state))
        stale_state.clear.assert_awaited_once()
        self.assertIn("устарела", stale_message.answer.await_args.args[0])

        cancel_state = SimpleNamespace(
            get_data=AsyncMock(
                return_value={
                    "landing_business_id": _BUSINESS,
                    "landing_event_id": _EVENT,
                    "landing_section": "hero",
                }
            ),
            clear=AsyncMock(),
        )
        cancel_message = SimpleNamespace(
            text="Отмена",
            from_user=SimpleNamespace(id=101),
            answer=AsyncMock(),
        )
        send_editor = AsyncMock()
        with patch.object(events, "_send_event_landing_editor", new=send_editor):
            asyncio.run(events.receive_event_landing_section(cancel_message, cancel_state))
        cancel_state.clear.assert_awaited_once()
        send_editor.assert_awaited_once()

        success_state = SimpleNamespace(
            get_data=AsyncMock(
                return_value={
                    "landing_business_id": _BUSINESS,
                    "landing_event_id": _EVENT,
                    "landing_section": "hero",
                }
            ),
            clear=AsyncMock(),
        )
        success_message = SimpleNamespace(
            text="Новый заголовок\nПодзаголовок",
            from_user=SimpleNamespace(id=101),
            answer=AsyncMock(),
        )
        send_editor = AsyncMock()
        with (
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(events, "update_event_landing_section", return_value=_profile()),
            patch.object(events, "_send_event_landing_editor", new=send_editor),
        ):
            asyncio.run(events.receive_event_landing_section(success_message, success_state))
        success_state.clear.assert_awaited_once()
        self.assertTrue(any("Черновик обновлён" in call.args[0] for call in success_message.answer.await_args_list))
        send_editor.assert_awaited_once()

        failure_state = SimpleNamespace(
            get_data=AsyncMock(
                return_value={
                    "landing_business_id": _BUSINESS,
                    "landing_event_id": _EVENT,
                    "landing_section": "faq",
                }
            ),
            clear=AsyncMock(),
        )
        failure_message = SimpleNamespace(
            text="Некорректный FAQ",
            from_user=SimpleNamespace(id=101),
            answer=AsyncMock(),
        )
        with (
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(
                events,
                "update_event_landing_section",
                side_effect=ValueError("ошибка формата"),
            ),
        ):
            asyncio.run(events.receive_event_landing_section(failure_message, failure_state))
        self.assertIn("ошибка формата", failure_message.answer.await_args.args[0])

    def test_landing_callbacks_fail_closed_with_safe_user_messages(self) -> None:
        actor_error = AsyncMock(side_effect=ValueError("internal detail"))

        callback, target = _callback("cpev:landing:evt:biz")
        with (
            patch.object(events.control, "_token_uuid", side_effect=_token_uuid),
            patch.object(events.control, "_callback_message", return_value=target),
            patch.object(
                events,
                "_send_event_landing_editor",
                new=AsyncMock(side_effect=ValueError("internal detail")),
            ),
        ):
            asyncio.run(events.open_event_landing_builder(callback))
        callback.answer.assert_awaited_once_with(
            "Не удалось открыть конструктор лендинга",
            show_alert=True,
        )

        confirm, _ = _callback("cpev:la:evt:biz")
        with (
            patch.object(events.control, "_token_uuid", side_effect=_token_uuid),
            patch.object(events.control, "_actor", new=actor_error),
        ):
            asyncio.run(events.confirm_event_landing_ai(confirm))
        confirm.answer.assert_awaited_once_with(
            "AI-генерация недоступна",
            show_alert=True,
        )

        generate, generate_target = _callback("cpev:laok:evt:biz:7")
        with (
            patch.object(events.control, "_token_uuid", side_effect=_token_uuid),
            patch.object(
                events.control,
                "_actor",
                new=AsyncMock(side_effect=ValueError("internal detail")),
            ),
            patch.object(events.control, "_callback_message", return_value=generate_target),
            patch.object(events, "_send_event_landing_editor", new=AsyncMock()),
        ):
            asyncio.run(events.generate_event_landing(generate))
        self.assertTrue(
            any(
                "Не удалось безопасно создать AI-версию" in call.args[0]
                for call in generate_target.answer.await_args_list
            )
        )

        explain, _ = _callback("cpev:lar:evt:biz")
        with (
            patch.object(events.control, "_token_uuid", side_effect=_token_uuid),
            patch.object(
                events.control,
                "_actor",
                new=AsyncMock(side_effect=ValueError("internal detail")),
            ),
        ):
            asyncio.run(events.explain_event_landing_ai_ambiguity(explain))
        explain.answer.assert_awaited_once_with(
            "Не удалось проверить AI-вызов",
            show_alert=True,
        )

        resolve, _ = _callback("cpev:larok:evt:biz")
        with (
            patch.object(events.control, "_token_uuid", side_effect=_token_uuid),
            patch.object(
                events.control,
                "_actor",
                new=AsyncMock(side_effect=ValueError("internal detail")),
            ),
        ):
            asyncio.run(events.resolve_event_landing_ai_ambiguity_callback(resolve))
        resolve.answer.assert_awaited_once_with(
            "Не удалось снять AI-блокировку",
            show_alert=True,
        )

        edit, _ = _callback("cpev:le:h:evt:biz")
        state = SimpleNamespace(set_state=AsyncMock(), update_data=AsyncMock())
        with (
            patch.object(events.control, "_token_uuid", side_effect=_token_uuid),
            patch.object(
                events.control,
                "_actor",
                new=AsyncMock(side_effect=ValueError("internal detail")),
            ),
        ):
            asyncio.run(events.edit_event_landing_section(edit, state))
        edit.answer.assert_awaited_once_with(
            "Не удалось открыть редактирование",
            show_alert=True,
        )

        cases = (
            (
                events.set_event_landing_style,
                "cpev:lt:b:evt:biz",
                "Не удалось изменить стиль",
            ),
            (
                events.reset_event_landing,
                "cpev:lr:evt:biz",
                "Не удалось вернуть автоверсию",
            ),
            (
                events.preview_event_landing,
                "cpev:lp:evt:biz",
                "Не удалось создать предпросмотр",
            ),
            (
                events.publish_event_landing_callback,
                "cpev:lx:evt:biz",
                "Не удалось опубликовать лендинг",
            ),
            (
                events.restore_simple_event_landing_callback,
                "cpev:ls:evt:biz",
                "Не удалось вернуть простой лендинг",
            ),
        )
        for handler, data, expected in cases:
            item, _ = _callback(data)
            with (
                patch.object(events.control, "_token_uuid", side_effect=_token_uuid),
                patch.object(
                    events.control,
                    "_actor",
                    new=AsyncMock(side_effect=ValueError("internal detail")),
                ),
            ):
                asyncio.run(handler(item))
            item.answer.assert_awaited_once_with(expected, show_alert=True)

    def test_style_reset_preview_publish_and_simple_callbacks(self) -> None:
        actor = _actor()

        style, style_target = _callback("cpev:lt:b:evt:biz")
        send_editor = AsyncMock()
        with (
            patch.object(events.control, "_token_uuid", side_effect=_token_uuid),
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(events.control, "_callback_message", return_value=style_target),
            patch.object(events, "set_event_landing_theme", return_value=_profile()),
            patch.object(events, "_send_event_landing_editor", new=send_editor),
        ):
            asyncio.run(events.set_event_landing_style(style))
        style.answer.assert_awaited_once_with("Стиль сохранён")
        send_editor.assert_awaited_once()

        invalid_style, _ = _callback("cpev:lt:z:evt:biz")
        with patch.object(events.control, "_token_uuid", side_effect=_token_uuid):
            asyncio.run(events.set_event_landing_style(invalid_style))
        invalid_style.answer.assert_awaited_once_with("Кнопка устарела", show_alert=True)

        reset, reset_target = _callback("cpev:lr:evt:biz")
        send_editor = AsyncMock()
        with (
            patch.object(events.control, "_token_uuid", side_effect=_token_uuid),
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(events.control, "_callback_message", return_value=reset_target),
            patch.object(events, "reset_event_landing_template", return_value=_profile()),
            patch.object(events, "_send_event_landing_editor", new=send_editor),
        ):
            asyncio.run(events.reset_event_landing(reset))
        reset.answer.assert_awaited_once_with("Автоверсия восстановлена")

        preview, preview_target = _callback("cpev:lp:evt:biz")
        with (
            patch.object(events.control, "_token_uuid", side_effect=_token_uuid),
            patch.object(events.control, "_uuid_token", side_effect=_uuid_token),
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(events.control, "_callback_message", return_value=preview_target),
            patch.object(events, "_public_base_url", return_value="https://example.test"),
            patch.object(
                events,
                "issue_event_landing_preview",
                return_value=SimpleNamespace(url="https://example.test/preview"),
            ),
        ):
            asyncio.run(events.preview_event_landing(preview))
        preview.answer.assert_awaited_once()
        self.assertIn("Предпросмотр", preview_target.answer.await_args.args[0])

        publish, publish_target = _callback("cpev:lx:evt:biz")
        send_editor = AsyncMock()
        with (
            patch.object(events.control, "_token_uuid", side_effect=_token_uuid),
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(events.control, "_callback_message", return_value=publish_target),
            patch.object(events, "publish_event_landing", return_value=_profile(published=True)),
            patch.object(events, "_send_event_landing_editor", new=send_editor),
        ):
            asyncio.run(events.publish_event_landing_callback(publish))
        publish.answer.assert_awaited_once_with("Лендинг опубликован")

        simple, simple_target = _callback("cpev:ls:evt:biz")
        send_editor = AsyncMock()
        with (
            patch.object(events.control, "_token_uuid", side_effect=_token_uuid),
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(events.control, "_callback_message", return_value=simple_target),
            patch.object(events, "restore_simple_event_landing", return_value=_profile()),
            patch.object(events, "_send_event_landing_editor", new=send_editor),
        ):
            asyncio.run(events.restore_simple_event_landing_callback(simple))
        simple.answer.assert_awaited_once_with(
            "Публично снова используется простой лендинг"
        )


if __name__ == "__main__":
    unittest.main()
