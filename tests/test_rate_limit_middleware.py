from __future__ import annotations

from importlib.util import find_spec

import pytest

if find_spec('aiogram') is None:
    pytestmark = pytest.mark.skip(reason='aiogram is not installed in this test environment')
else:
    from datetime import datetime, UTC

    from aiogram.types import CallbackQuery, Chat, Message, User

    from core.middlewares import QuickAckCallbackMiddleware, SoftRateLimitMiddleware


    def _make_callback(user_id: int, data: str = 'x') -> CallbackQuery:
        user = User(id=user_id, is_bot=False, first_name='Test')
        cb = CallbackQuery(id='1', from_user=user, chat_instance='chat', data=data)
        object.__setattr__(cb, "answer", _async_stub())
        return cb


    def _make_message(user_id: int, text: str = 'hello') -> Message:
        user = User(id=user_id, is_bot=False, first_name='Test')
        chat = Chat(id=user_id, type='private')
        msg = Message(message_id=1, date=datetime.now(UTC), chat=chat, from_user=user, text=text)
        object.__setattr__(msg, "answer", _async_stub())
        return msg


    def _async_stub():
        calls = []

        async def _inner(*args, **kwargs):
            calls.append((args, kwargs))
            return None

        _inner.calls = calls
        return _inner


    @pytest.mark.asyncio
    async def test_callback_and_message_are_limited_separately():
        mw = SoftRateLimitMiddleware(callback_interval_sec=1.0, message_interval_sec=1.0)
        cb = _make_callback(1, 'same')
        msg = _make_message(1, 'same')
        seen = []

        async def handler(event, data):
            seen.append(type(event).__name__)
            return 'ok'

        assert await mw(handler, cb, {}) == 'ok'
        assert await mw(handler, msg, {}) == 'ok'
        assert seen == ['CallbackQuery', 'Message']


    @pytest.mark.asyncio
    async def test_duplicate_callback_is_soft_blocked():
        mw = SoftRateLimitMiddleware(callback_interval_sec=1.0, message_interval_sec=1.0)
        cb = _make_callback(1, 'same')
        seen = []

        async def handler(event, data):
            seen.append('handled')
            return 'ok'

        assert await mw(handler, cb, {}) == 'ok'
        assert await mw(handler, cb, {}) is None
        assert seen == ['handled']
        assert len(cb.answer.calls) == 1


    @pytest.mark.asyncio
    async def test_quick_ack_answers_safe_navigation_before_handler():
        mw = QuickAckCallbackMiddleware()
        cb = _make_callback(1, 'cpo:ads:business-1')
        calls = []

        async def handler(event, data):
            calls.append(('handler', None))
            await event.answer('later')
            return 'ok'

        original = cb.answer
        async def tracked(*args, **kwargs):
            calls.append(('answer', (args, kwargs)))
            return await original(*args, **kwargs)
        tracked.calls = original.calls
        object.__setattr__(cb, 'answer', tracked)

        assert await mw(handler, cb, {}) == 'ok'
        assert calls[0][0] == 'answer'
        assert calls[1][0] == 'handler'
        assert len(cb.answer.calls) == 1
        assert cb.answer.calls[0][1] == {'cache_time': 0}


    @pytest.mark.asyncio
    async def test_quick_ack_answers_ad_offering_navigation_before_handler():
        mw = QuickAckCallbackMiddleware()
        cb = _make_callback(1, 'cpo:start:business-1')
        calls = []

        original = cb.answer

        async def tracked(*args, **kwargs):
            calls.append(('answer', (args, kwargs)))
            return await original(*args, **kwargs)

        tracked.calls = original.calls
        object.__setattr__(cb, 'answer', tracked)

        async def handler(event, data):
            calls.append(('handler', None))
            return 'ok'

        assert await mw(handler, cb, {}) == 'ok'
        assert calls[0][0] == 'answer'
        assert calls[1][0] == 'handler'
        assert cb.answer.calls == [((), {'cache_time': 0})]


    @pytest.mark.asyncio
    async def test_quick_ack_preserves_handler_alert_for_semantic_callback():
        mw = QuickAckCallbackMiddleware()
        cb = _make_callback(1, 'cpj:wizdate:business-1:2026-09-27')

        async def handler(event, data):
            await event.answer('Эта дата недоступна', show_alert=True)
            return 'ok'

        assert await mw(handler, cb, {}) == 'ok'
        assert len(cb.answer.calls) == 1
        assert cb.answer.calls[0][0] == ('Эта дата недоступна',)
        assert cb.answer.calls[0][1] == {'show_alert': True}


    @pytest.mark.asyncio
    async def test_quick_ack_retries_failed_semantic_alert_with_same_payload():
        mw = QuickAckCallbackMiddleware()
        cb = _make_callback(1, 'cpj:wizdate:business-1:2026-09-27')
        attempts = []

        async def flaky_answer(*args, **kwargs):
            attempts.append((args, kwargs))
            if len(attempts) == 1:
                raise TimeoutError()
            return None

        flaky_answer.calls = attempts
        object.__setattr__(cb, 'answer', flaky_answer)

        async def handler(event, data):
            await event.answer('Эта дата недоступна', show_alert=True)
            return 'ok'

        assert await mw(handler, cb, {}) == 'ok'
        assert attempts == [
            (('Эта дата недоступна',), {'show_alert': True}),
            (('Эта дата недоступна',), {'show_alert': True}),
        ]


    @pytest.mark.asyncio
    async def test_quick_ack_and_rate_limit_preserve_semantic_feedback():
        quick = QuickAckCallbackMiddleware()
        limiter = SoftRateLimitMiddleware(callback_interval_sec=1.0, message_interval_sec=1.0)
        first = _make_callback(1, 'cpj:wizdate:business-1:2026-09-27')
        second = _make_callback(1, 'cpj:wizdate:business-1:2026-09-28')

        async def business_handler(event, data):
            return 'ok'

        async def through_limiter(event, data):
            return await limiter(business_handler, event, data)

        assert await quick(through_limiter, first, {}) == 'ok'
        assert await quick(through_limiter, second, {}) is None
        assert second.answer.calls == [(('Секунду…',), {'show_alert': False})]


    @pytest.mark.asyncio
    async def test_quick_ack_closes_silent_semantic_callback_after_handler():
        mw = QuickAckCallbackMiddleware()
        cb = _make_callback(1, 'unknown:semantic')
        seen = []

        async def handler(event, data):
            seen.append('handled')
            return 'ok'

        assert await mw(handler, cb, {}) == 'ok'
        assert seen == ['handled']
        assert len(cb.answer.calls) == 1
        assert cb.answer.calls[0][1] == {'cache_time': 0}

