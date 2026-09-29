from __future__ import annotations

import asyncio
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from clientplatform.application import editable_advertising
from clientplatform.domain.editable_advertising import (
    EditableAdProject,
    EditableAdProjectStatus,
)
from clientplatform.infrastructure.editable_ad_project_repository import (
    EditableAdProjectRepository,
)
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from handlers import clientplatform_creative_studio as creative_studio
from handlers import clientplatform_goal_first_autopilot as goal
from handlers import clientplatform_goal_first_safety as goal_safety
from handlers import clientplatform_interaction_safety as interaction_safety
from services.db.schema import (
    clientplatform_ad_connections,
    clientplatform_creative_experiments,
    clientplatform_tenancy,
)


BRAND = {
    "primary_color": "#172033",
    "accent_color": "#E9C46A",
    "text_color": "#FFFFFF",
}


def _repository_fixture():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    clientplatform_tenancy.ensure(conn)
    clientplatform_ad_connections.ensure(conn)
    clientplatform_creative_experiments.ensure(conn)
    tenancy = TenancyRepository(conn)
    access = tenancy.create_business(owner_user_id=101, name="Editable Ads")
    actor = tenancy.resolve_context(
        user_id=101,
        business_id=access.business.id,
    )
    publication_job_id = str(uuid4())
    now = "2026-09-30T00:00:00+00:00"
    conn.execute(
        """
        INSERT INTO ad_publication_jobs(
            id,business_id,promotion_campaign_id,connection_id,
            external_campaign_id,external_campaign_name,region_ids_json,
            source_url,title,text,status,idempotency_key,attempts,available_at,
            created_by_member_id,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,'draft',?,0,?,?,?,?)
        """,
        (
            publication_job_id,
            actor.business_id,
            str(uuid4()),
            str(uuid4()),
            "campaign-1",
            "Campaign",
            "[47]",
            "https://example.test/",
            "Готовый заголовок",
            "Готовый текст",
            "editable-test-" + publication_job_id,
            now,
            actor.membership_id,
            now,
            now,
        ),
    )
    return conn, actor, publication_job_id


def test_schema_has_metadata_only_editable_project() -> None:
    conn, _actor, _job_id = _repository_fixture()
    try:
        columns = {
            str(row["name"]): str(row["type"]).upper()
            for row in conn.execute("PRAGMA table_info(editable_ad_projects)").fetchall()
        }
        assert "source_job_id" in columns
        assert "source_receipt_id" not in columns
        assert all("BLOB" not in declared for declared in columns.values())
    finally:
        conn.close()


def test_editable_project_reentry_and_expiry_preserve_copy_without_paid_reset() -> None:
    conn, actor, publication_job_id = _repository_fixture()
    try:
        repo = EditableAdProjectRepository(conn)
        project = repo.create_or_get(
            actor=actor,
            publication_job_id=publication_job_id,
            kind="image",
            headline="Первый заголовок",
            body="Первый текст",
            cta="Записаться",
            layout="lower_card",
            brand=BRAND,
        )
        assert project.status == EditableAdProjectStatus.DRAFT
        assert project.revision == 1

        ready = repo.bind_source(
            actor=actor,
            project_id=project.id,
            source_job_id="visual-job-1",
        )
        assert ready.status == EditableAdProjectStatus.SOURCE_READY

        edited = repo.update_composition(
            actor=actor,
            project_id=project.id,
            headline="Новый заголовок",
        )
        assert edited.revision == 2
        finished = repo.finish(actor=actor, project_id=project.id)
        assert finished.status == EditableAdProjectStatus.FINISHED

        reopened = repo.create_or_get(
            actor=actor,
            publication_job_id=publication_job_id,
            kind="image",
            headline="Черновик объявления не должен затереть редактор",
            body="Новый внешний текст",
            cta="Купить",
            layout="top_card",
            brand=BRAND,
        )
        assert reopened.id == project.id
        assert reopened.headline == "Новый заголовок"
        assert reopened.source_job_id == "visual-job-1"
        assert reopened.status == EditableAdProjectStatus.FINISHED

        dirty = repo.update_composition(
            actor=actor,
            project_id=project.id,
            body="Отредактированный текст",
        )
        assert dirty.status == EditableAdProjectStatus.SOURCE_READY

        expired = repo.mark_source_expired(
            actor=actor,
            project_id=project.id,
            expected_source_job_id="visual-job-1",
        )
        assert expired.status == EditableAdProjectStatus.SOURCE_EXPIRED
        preserved_revision = expired.revision
        preserved_headline = expired.headline
        preserved_body = expired.body

        still_expired = repo.create_or_get(
            actor=actor,
            publication_job_id=publication_job_id,
            kind="image",
            headline="Не перезаписывать",
            body="Не перезаписывать",
            cta="",
            layout="lower_card",
            brand=BRAND,
        )
        assert still_expired.status == EditableAdProjectStatus.SOURCE_EXPIRED
        assert still_expired.headline == preserved_headline
        assert still_expired.body == preserved_body

        rotated = repo.prepare_new_source(actor=actor, project_id=project.id)
        assert rotated.status == EditableAdProjectStatus.DRAFT
        assert rotated.source_job_id == ""
        assert rotated.revision == preserved_revision + 1
        assert rotated.headline == preserved_headline
        assert rotated.body == preserved_body
    finally:
        conn.close()


def test_definitive_failed_source_rotates_next_idempotency_revision_only_once() -> None:
    conn, actor, publication_job_id = _repository_fixture()
    try:
        repo = EditableAdProjectRepository(conn)
        project = repo.create_or_get(
            actor=actor,
            publication_job_id=publication_job_id,
            kind="video",
            headline="Видео",
            body="Текст видео",
            cta="Подробнее",
            layout="lower_card",
            brand=BRAND,
        )
        advanced = repo.advance_failed_source_revision(
            actor=actor,
            project_id=project.id,
        )
        assert advanced.revision == project.revision + 1
        assert advanced.status == EditableAdProjectStatus.DRAFT
    finally:
        conn.close()


def test_invalid_brand_color_is_rejected_before_persistence() -> None:
    conn, actor, publication_job_id = _repository_fixture()
    try:
        repo = EditableAdProjectRepository(conn)
        with pytest.raises(ValueError, match="editable_ad_brand_invalid"):
            repo.create_or_get(
                actor=actor,
                publication_job_id=publication_job_id,
                kind="image",
                headline="Заголовок",
                body="Текст",
                cta="",
                layout="lower_card",
                brand={**BRAND, "primary_color": "javascript:red"},
            )
    finally:
        conn.close()


class _Context:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self.value

    def __exit__(self, *_args):
        return False


def _project(*, status=EditableAdProjectStatus.SOURCE_READY) -> EditableAdProject:
    return EditableAdProject(
        id="11111111-1111-4111-8111-111111111111",
        business_id="22222222-2222-4222-8222-222222222222",
        created_by_member_id="33333333-3333-4333-8333-333333333333",
        publication_job_id="44444444-4444-4444-8444-444444444444",
        kind="image",
        headline="Новый заголовок",
        body="Текст",
        cta="Записаться",
        layout="lower_card",
        brand_json='{"accent_color":"#E9C46A","primary_color":"#172033","text_color":"#FFFFFF"}',
        source_job_id="visual-job-1",
        status=status,
        revision=3,
        created_at="2026-09-30T00:00:00+00:00",
        updated_at="2026-09-30T00:00:00+00:00",
    )


def test_render_edit_uses_existing_source_and_never_submits_new_visual(monkeypatch) -> None:
    project = _project()
    actor = SimpleNamespace(business_id=project.business_id)
    job = SimpleNamespace(
        id=project.source_job_id,
        status="succeeded",
        asset_ready=True,
        kind="image",
    )
    pack = SimpleNamespace(status="succeeded")
    render = Mock(return_value=pack)

    class FakeRepository:
        def __init__(self, _conn):
            pass

        def get(self, **_kwargs):
            return project

    monkeypatch.setattr(editable_advertising, "EditableAdProjectRepository", FakeRepository)
    monkeypatch.setattr(editable_advertising, "get_db_ro", lambda: _Context(object()))
    monkeypatch.setattr(editable_advertising, "poll_visual", Mock(return_value=job))
    monkeypatch.setattr(editable_advertising, "render_visual_pack", render)

    result_project, result_pack = editable_advertising.render_editable_ad_project(
        actor=actor,
        project_id=project.id,
        formats=("square",),
    )

    assert result_project is project
    assert result_pack is pack
    render.assert_called_once()
    assert "submit_visual" not in editable_advertising.render_editable_ad_project.__code__.co_names


def test_definitive_missing_source_marks_project_expired(monkeypatch) -> None:
    project = _project()
    actor = SimpleNamespace(business_id=project.business_id)
    marked: dict[str, object] = {}

    class FakeRepository:
        def __init__(self, _conn):
            pass

        def get(self, **_kwargs):
            return project

        def mark_source_expired(self, **kwargs):
            marked.update(kwargs)
            return project

    monkeypatch.setattr(editable_advertising, "EditableAdProjectRepository", FakeRepository)
    monkeypatch.setattr(editable_advertising, "get_db_ro", lambda: _Context(object()))
    monkeypatch.setattr(editable_advertising, "get_db", lambda: _Context(object()))
    monkeypatch.setattr(
        editable_advertising,
        "poll_visual",
        Mock(return_value=SimpleNamespace(status="succeeded", asset_ready=False, kind="image")),
    )

    with pytest.raises(editable_advertising.EditableAdvertisingSourceExpired):
        editable_advertising.render_editable_ad_project(
            actor=actor,
            project_id=project.id,
        )

    assert marked["project_id"] == project.id
    assert marked["expected_source_job_id"] == project.source_job_id


async def _direct(function, *args, **kwargs):
    return function(*args, **kwargs)


@pytest.mark.asyncio
async def test_editable_preview_has_no_ad_provider_side_effect(monkeypatch, tmp_path: Path) -> None:
    asset = tmp_path / "preview.jpg"
    asset.write_bytes(b"jpeg")
    target = SimpleNamespace(answer_photo=AsyncMock(), answer_video=AsyncMock())
    actor = SimpleNamespace(business_id="business-id")
    attach_image = Mock()
    attach_video = Mock()

    monkeypatch.setattr(goal.asyncio, "to_thread", _direct)
    monkeypatch.setattr(
        goal,
        "render_editable_ad_project",
        lambda **_kwargs: (_project(), SimpleNamespace(status="succeeded")),
    )
    monkeypatch.setattr(goal, "download_render_asset", lambda *_args, **_kwargs: asset)
    monkeypatch.setattr(goal, "attach_image_file", attach_image)
    monkeypatch.setattr(goal, "attach_video_bytes", attach_video)

    await goal._preview_editable_project(
        target,
        actor=actor,
        data={"business_token": "token"},
        project_id=_project().id,
        kind="image",
    )

    target.answer_photo.assert_awaited_once()
    attach_image.assert_not_called()
    attach_video.assert_not_called()


class _State:
    def __init__(self, data):
        self.data = dict(data)
        self.state = None

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **values):
        self.data.update(values)

    async def set_state(self, value):
        self.state = value


@pytest.mark.asyncio
async def test_finish_editable_ad_is_the_single_provider_commit_boundary(
    monkeypatch,
    tmp_path: Path,
) -> None:
    asset = tmp_path / "final.jpg"
    asset.write_bytes(b"jpeg")
    project = _project()
    finished = SimpleNamespace(revision=project.revision)
    pack = SimpleNamespace(status="succeeded")
    target = SimpleNamespace(answer=AsyncMock())
    callback = SimpleNamespace(
        data="cpo:editdone:business-token",
        from_user=SimpleNamespace(id=101),
        answer=AsyncMock(),
        message=target,
    )
    state = _State(
        {
            "business_id": project.business_id,
            "business_token": "business-token",
            "job_id": project.publication_job_id,
            "editable_ad_project_id": project.id,
            "editable_ad_kind": "image",
        }
    )
    attach = Mock()
    finish = Mock(return_value=finished)

    monkeypatch.setattr(goal.asyncio, "to_thread", _direct)
    monkeypatch.setattr(goal.control, "_actor", AsyncMock(return_value=SimpleNamespace(business_id=project.business_id)))
    monkeypatch.setattr(goal.control, "_callback_message", lambda _callback: target)
    monkeypatch.setattr(goal, "render_editable_ad_project", lambda **_kwargs: (project, pack))
    monkeypatch.setattr(goal, "download_render_asset", lambda *_args, **_kwargs: asset)
    monkeypatch.setattr(goal, "attach_image_file", attach)
    monkeypatch.setattr(goal, "finish_editable_ad_project", finish)

    await goal.finish_editable_ad(callback, state)

    attach.assert_called_once()
    finish.assert_called_once()
    assert state.data["editable_ad_project_id"] == ""
    assert state.state == goal.GoalFirstAutopilotState.customizing
    assert "передан в рекламный provider" in target.answer.await_args.args[0]


def test_exact_editable_buttons_are_rendered_in_both_owner_entry_points() -> None:
    labels = [
        button.text
        for row in goal._custom_keyboard("business-token").inline_keyboard
        for button in row
    ]
    assert "картинка для рекламы (возможность редактирования)" in labels
    assert "видео для рекламы (возможность редактирования)" in labels

    studio_labels = [
        button.text
        for row in creative_studio._menu_rows(
            "business-token",
            None,
            image_ready=True,
            video_ready=True,
            video_mode="motion",
        ).inline_keyboard
        for button in row
    ]
    assert "картинка для рекламы (возможность редактирования)" in studio_labels
    assert "видео для рекламы (возможность редактирования)" in studio_labels


def test_goal_first_safety_allows_editor_callbacks_without_weakening_other_states() -> None:
    goal_safety.install_goal_first_safety(interaction_safety)
    assert interaction_safety._state_local_callback_allowed(
        "GoalFirstAutopilotState:customizing",
        "cpo:editfield:headline:business-token",
    )
    assert interaction_safety._state_local_callback_allowed(
        "GoalFirstAutopilotState:confirming_generation",
        "cpo:editgen:image:business-token",
    )
    assert interaction_safety._state_local_callback_allowed(
        "GoalFirstAutopilotState:customizing",
        "cpo:editgen:image:business-token",
    )
    assert interaction_safety._state_local_callback_allowed(
        "GoalFirstAutopilotState:waiting_editable_headline",
        "cpo:custom:business-token",
    )
    assert not interaction_safety._state_local_callback_allowed(
        "GoalFirstAutopilotState:generation_pending",
        "cpo:editdone:business-token",
    )


def _goal_data() -> dict[str, object]:
    project = _project()
    return {
        "business_id": project.business_id,
        "business_token": "business-token",
        "job_id": project.publication_job_id,
        "creative_title": "Обычный заголовок",
        "creative_body": "Обычный текст",
        "creative_job_id": "",
        "creative_variant_id": "",
    }


def _goal_target():
    return SimpleNamespace(
        answer=AsyncMock(),
        answer_photo=AsyncMock(),
        answer_video=AsyncMock(),
    )


def _goal_callback(data: str, target=None):
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=101),
        answer=AsyncMock(),
        message=target or _goal_target(),
    )


@pytest.mark.asyncio
async def test_reopening_finished_editable_project_skips_paid_generation(monkeypatch) -> None:
    project = _project(status=EditableAdProjectStatus.FINISHED)
    data = _goal_data()
    state = _State(data)
    target = _goal_target()
    callback = _goal_callback("cpo:editask:image:business-token", target)
    show = AsyncMock()
    create_visual = Mock()

    monkeypatch.setattr(goal.asyncio, "to_thread", _direct)
    monkeypatch.setattr(
        goal.control,
        "_actor",
        AsyncMock(return_value=SimpleNamespace(business_id=project.business_id)),
    )
    monkeypatch.setattr(goal.control, "_callback_message", lambda _callback: target)
    monkeypatch.setattr(
        goal,
        "load_goal_visual_brand",
        lambda **_kwargs: SimpleNamespace(render_brand=lambda: BRAND),
    )
    monkeypatch.setattr(goal, "create_editable_ad_project", lambda **_kwargs: project)
    monkeypatch.setattr(goal, "_show_editable_editor", show)
    monkeypatch.setattr(goal, "create_ad_visual", create_visual)

    await goal.ask_editable_ad_confirmation(callback, state)

    show.assert_awaited_once()
    create_visual.assert_not_called()
    assert state.data["editable_ad_project_id"] == project.id
    assert state.state == goal.GoalFirstAutopilotState.customizing


@pytest.mark.asyncio
async def test_expired_editable_project_requires_explicit_new_source_confirmation(
    monkeypatch,
) -> None:
    project = _project(status=EditableAdProjectStatus.SOURCE_EXPIRED)
    data = _goal_data()
    state = _State(data)
    target = _goal_target()
    callback = _goal_callback("cpo:editask:image:business-token", target)

    monkeypatch.setattr(goal.asyncio, "to_thread", _direct)
    monkeypatch.setattr(
        goal.control,
        "_actor",
        AsyncMock(return_value=SimpleNamespace(business_id=project.business_id)),
    )
    monkeypatch.setattr(goal.control, "_callback_message", lambda _callback: target)
    monkeypatch.setattr(
        goal,
        "load_goal_visual_brand",
        lambda **_kwargs: SimpleNamespace(render_brand=lambda: BRAND),
    )
    monkeypatch.setattr(goal, "create_editable_ad_project", lambda **_kwargs: project)

    await goal.ask_editable_ad_confirmation(callback, state)

    labels_and_callbacks = [
        (button.text, button.callback_data)
        for row in target.answer.await_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert any(
        callback_data == "cpo:editgen:image:business-token"
        for _label, callback_data in labels_and_callbacks
    )
    assert "Предыдущая AI-основа" in target.answer.await_args.args[0]
    assert state.state == goal.GoalFirstAutopilotState.confirming_generation


@pytest.mark.asyncio
async def test_generate_editable_source_uses_explicit_project_revision(monkeypatch) -> None:
    draft = _project(status=EditableAdProjectStatus.DRAFT)
    draft = EditableAdProject(
        **{
            **{name: getattr(draft, name) for name in draft.__dataclass_fields__},
            "source_job_id": "",
            "revision": 7,
        }
    )
    data = {
        **_goal_data(),
        "editable_ad_project_id": draft.id,
        "editable_ad_kind": "image",
    }
    state = _State(data)
    callback = _goal_callback("cpo:editgen:image:business-token")
    generate = AsyncMock()

    monkeypatch.setattr(
        goal.control,
        "_actor",
        AsyncMock(return_value=SimpleNamespace(business_id=draft.business_id)),
    )
    monkeypatch.setattr(goal.asyncio, "to_thread", _direct)
    monkeypatch.setattr(goal, "get_editable_ad_project", lambda **_kwargs: draft)
    monkeypatch.setattr(goal, "prepare_editable_ad_source", lambda **_kwargs: draft)
    monkeypatch.setattr(goal, "_generate_custom_visual", generate)

    await goal.generate_editable_ad_source(callback, state)

    generate.assert_awaited_once()
    assert generate.await_args.kwargs["editable_project_id"] == draft.id
    assert generate.await_args.kwargs["editable_revision"] == 7
    assert generate.await_args.kwargs["source_title"] == draft.headline
    assert generate.await_args.kwargs["source_body"] == draft.body


@pytest.mark.asyncio
async def test_editable_generation_identity_is_separate_from_ordinary_generation(
    monkeypatch,
) -> None:
    target = _goal_target()
    callback = _goal_callback("unused", target)
    editable_state = _State(_goal_data())
    ordinary_state = _State(_goal_data())
    jobs = [
        SimpleNamespace(status="running", asset_ready=False, job_id="editable-job"),
        SimpleNamespace(status="running", asset_ready=False, job_id="ordinary-job"),
    ]
    create = Mock(side_effect=jobs)

    monkeypatch.setattr(goal.asyncio, "to_thread", _direct)
    monkeypatch.setattr(goal, "visual_generation_ready", lambda **_kwargs: True)
    monkeypatch.setattr(goal, "create_ad_visual", create)
    monkeypatch.setattr(
        goal,
        "_finish_editable_source_generation",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        goal,
        "_finish_generated_visual",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(goal.control, "_callback_message", lambda _callback: target)

    await goal._generate_custom_visual(
        callback,
        editable_state,
        business_token="business-token",
        kind="image",
        editable_project_id=_project().id,
        editable_revision=4,
        source_title="Заголовок редактора",
        source_body="Текст редактора",
    )
    await goal._generate_custom_visual(
        callback,
        ordinary_state,
        business_token="business-token",
        kind="image",
    )

    editable_key = create.call_args_list[0].kwargs["idempotency_key"]
    ordinary_key = create.call_args_list[1].kwargs["idempotency_key"]
    assert editable_key != ordinary_key
    assert editable_state.data["editable_generation_active"] is True
    assert ordinary_state.data["editable_generation_active"] is False


@pytest.mark.asyncio
async def test_succeeded_but_expired_editable_source_is_bound_before_recovery(
    monkeypatch,
) -> None:
    project = _project()
    data = {
        **_goal_data(),
        "editable_ad_project_id": project.id,
        "editable_ad_kind": "image",
    }
    state = _State(data)
    callback = _goal_callback("unused")
    actor = SimpleNamespace(business_id=project.business_id)
    bind = Mock(return_value=project)
    show = AsyncMock()

    monkeypatch.setattr(
        goal.control,
        "_actor",
        AsyncMock(return_value=actor),
    )
    monkeypatch.setattr(goal.control, "_callback_message", lambda cb: cb.message)
    monkeypatch.setattr(goal.asyncio, "to_thread", _direct)
    monkeypatch.setattr(goal, "bind_editable_ad_source", bind)
    monkeypatch.setattr(goal, "_show_editable_editor", show)

    completed = await goal._finish_editable_source_generation(
        callback,
        state,
        data=data,
        kind="image",
        project_id=project.id,
        job=SimpleNamespace(
            id="visual-job-1",
            status="succeeded",
            asset_ready=False,
        ),
    )

    assert completed is True
    bind.assert_called_once_with(
        actor=actor,
        project_id=project.id,
        source_job_id="visual-job-1",
    )
    show.assert_awaited_once()
    assert state.data["editable_generation_active"] is False
