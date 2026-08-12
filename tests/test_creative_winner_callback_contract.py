from __future__ import annotations

from uuid import uuid4

from clientplatform.application.control_callbacks import uuid_token


def test_real_uuid_token_keeps_creative_confirmation_within_telegram_limit() -> None:
    trial_token = uuid_token(str(uuid4()))
    fingerprint = "0123456789abcdef"
    revision = 999_999_999_999
    callback_data = f"cpw:apply:{trial_token}:{revision}:b:{fingerprint}"

    assert len(trial_token) == 22
    assert len(callback_data.encode("utf-8")) == 64
