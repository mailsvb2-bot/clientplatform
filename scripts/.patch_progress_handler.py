from pathlib import Path

path = Path("handlers/clientplatform_control.py")
source = path.read_text(encoding="utf-8")


def replace_once(old: str, new: str, label: str) -> None:
    global source
    if source.count(old) != 1:
        raise SystemExit(f"{label} patch anchor mismatch")
    source = source.replace(old, new)


replace_once(
    "from clientplatform.application.programs import list_programs\n",
    "from clientplatform.application.programs import list_programs\n"
    "from clientplatform.application.progress import (\n"
    "    complete_customer_lesson,\n"
    "    get_customer_program,\n"
    "    list_business_program_progress,\n"
    "    list_customer_programs,\n"
    ")\n",
    "progress import",
)

replace_once(
    '''def _client_portal_keyboard(business_id: str) -> InlineKeyboardMarkup:
    return _keyboard(
        [[("Посмотреть доступную запись", f"cp:client:{_uuid_token(business_id)}")]]
    )
''',
    '''def _client_portal_keyboard(business_id: str) -> InlineKeyboardMarkup:
    token = _uuid_token(business_id)
    return _keyboard(
        [
            [("Мои программы", f"cp:cprograms:{token}")],
            [("Посмотреть доступную запись", f"cp:client:{token}")],
        ]
    )
''',
    "client portal keyboard",
)

callbacks = '''@router.callback_query(F.data.startswith("cp:cprograms:"))
async def open_customer_programs(callback: CallbackQuery) -> None:
    business_token = str(callback.data).split(":", 2)[2]
    business_id = _token_uuid(business_token)
    programs = await asyncio.to_thread(
        list_customer_programs,
        telegram_user_id=int(callback.from_user.id),
        business_id=business_id,
    )
    await callback.answer()
    message = _callback_message(callback)
    if not programs:
        await message.answer("Вам пока не выдали ни одной программы.")
        return
    lines = "\\n".join(
        f"• {item.program_title} — {item.completed_lessons}/{item.total_lessons} "
        f"({item.percent_complete}%)"
        for item in programs
    )
    await message.answer(
        f"Мои программы\\n\\n{lines}\\n\\nВыберите программу:",
        reply_markup=_keyboard(
            [
                [
                    (
                        item.program_title[:36],
                        f"cp:cprog:{business_token}:{_uuid_token(item.enrollment_id)}",
                    )
                ]
                for item in programs
            ]
        ),
    )


@router.callback_query(F.data.startswith("cp:cprog:"))
async def open_customer_program(callback: CallbackQuery) -> None:
    _, _, business_token, enrollment_token = str(callback.data).split(":", 3)
    business_id = _token_uuid(business_token)
    enrollment_id = _token_uuid(enrollment_token)
    program = await asyncio.to_thread(
        get_customer_program,
        telegram_user_id=int(callback.from_user.id),
        business_id=business_id,
        enrollment_id=enrollment_id,
    )
    icons = {
        "pending": "⏳",
        "delivered": "📬",
        "opened": "👀",
        "completed": "✅",
        "skipped": "⏭",
    }
    lines = "\\n".join(
        f"{icons.get(lesson.progress_status.value, '•')} {lesson.position}. {lesson.title}"
        for lesson in program.lessons
    ) or "В программе пока нет материалов."
    rows = [
        [
            (
                f"Готово · урок {lesson.position}",
                f"cp:done:{business_token}:{enrollment_token}:{lesson.position}",
            )
        ]
        for lesson in program.lessons
        if lesson.can_complete
    ]
    rows.append([("Назад к программам", f"cp:cprograms:{business_token}")])
    await callback.answer()
    await _callback_message(callback).answer(
        f"{program.summary.program_title}\\n\\n{lines}\\n\\n"
        f"Пройдено: {program.summary.completed_lessons}/{program.summary.total_lessons} "
        f"({program.summary.percent_complete}%)",
        reply_markup=_keyboard(rows),
    )


@router.callback_query(F.data.startswith("cp:done:"))
async def complete_customer_program_lesson(callback: CallbackQuery) -> None:
    _, _, business_token, enrollment_token, position = str(callback.data).split(":", 4)
    result = await asyncio.to_thread(
        complete_customer_lesson,
        telegram_user_id=int(callback.from_user.id),
        business_id=_token_uuid(business_token),
        enrollment_id=_token_uuid(enrollment_token),
        lesson_position=int(position),
    )
    await callback.answer("Прогресс сохранён")
    if result.next_material_queued:
        detail = "Следующий материал уже поставлен в отправку."
    elif result.program.summary.enrollment_status.value == "completed":
        detail = "Программа завершена. Отличная работа!"
    else:
        detail = "Урок отмечен выполненным."
    await _callback_message(callback).answer(
        f"{detail}\\n\\n"
        f"Пройдено: {result.program.summary.completed_lessons}/"
        f"{result.program.summary.total_lessons} "
        f"({result.program.summary.percent_complete}%)",
        reply_markup=_keyboard(
            [[("Открыть программу", f"cp:cprog:{business_token}:{enrollment_token}")]]
        ),
    )


'''
replace_once(
    '@router.callback_query(F.data.startswith("cp:client:"))\n',
    callbacks + '@router.callback_query(F.data.startswith("cp:client:"))\n',
    "customer program callbacks",
)

old_results = '''@router.callback_query(F.data.startswith("cp:results:"))
async def show_results(callback: CallbackQuery) -> None:
    business_id = _token_uuid(str(callback.data).split(":", 2)[2])
    actor = await _actor(int(callback.from_user.id), business_id)
    summary = await asyncio.to_thread(business_delivery_summary, actor=actor)
    await callback.answer()
    await _callback_message(callback).answer(
        "Результаты\\n\\n"
        f"Клиенты: {summary.customers}\\n"
        f"Активные программы: {summary.programs}\\n"
        f"Ожидают отправки: {summary.dispatch_pending}\\n"
        f"Успешно отправлено: {summary.dispatch_sent}\\n"
        f"Требуют внимания: {summary.dispatch_attention}"
    )
'''
new_results = '''@router.callback_query(F.data.startswith("cp:results:"))
async def show_results(callback: CallbackQuery) -> None:
    business_id = _token_uuid(str(callback.data).split(":", 2)[2])
    actor = await _actor(int(callback.from_user.id), business_id)
    summary = await asyncio.to_thread(business_delivery_summary, actor=actor)
    progress = await asyncio.to_thread(list_business_program_progress, actor=actor, limit=15)
    progress_lines = "\\n".join(
        f"• {item.customer_display_name or 'Клиент'}: {item.program_title} — "
        f"{item.completed_lessons}/{item.total_lessons} ({item.percent_complete}%)"
        for item in progress
    ) or "Пока нет выданных программ."
    await callback.answer()
    await _callback_message(callback).answer(
        "Результаты\\n\\n"
        f"Клиенты: {summary.customers}\\n"
        f"Активные программы: {summary.programs}\\n"
        f"Ожидают отправки: {summary.dispatch_pending}\\n"
        f"Успешно отправлено: {summary.dispatch_sent}\\n"
        f"Требуют внимания: {summary.dispatch_attention}\\n\\n"
        f"Прогресс клиентов\\n{progress_lines}"
    )
'''
replace_once(old_results, new_results, "owner progress results")
path.write_text(source, encoding="utf-8")
