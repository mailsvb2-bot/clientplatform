from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

from clientplatform.application import ad_spend_consent
from clientplatform.domain.ad_spend import AdSpendAuthorizationStatus


@contextmanager
def _connection():
    yield object()


class _Repository:
    calls: list[tuple[str, dict[str, object]]] = []
    authorization = SimpleNamespace(
        id="authorization",
        business_id="business",
        terms_hash="terms-hash",
        snapshot=SimpleNamespace(snapshot_hash="snapshot-hash"),
        status=AdSpendAuthorizationStatus.DRAFT,
    )
    receipt = object()

    def __init__(self, conn: object):
        assert conn is not None

    def get(self, **kwargs: object) -> object:
        self.calls.append(("get", kwargs))
        return self.authorization

    def request_consent(self, **kwargs: object) -> object:
        self.calls.append(("request", kwargs))
        return self.authorization

    def authorize(self, **kwargs: object) -> tuple[object, object]:
        self.calls.append(("authorize", kwargs))
        return self.authorization, self.receipt

    def revoke(self, **kwargs: object) -> object:
        self.calls.append(("revoke", kwargs))
        return self.authorization

    def list_authorizations(self, **kwargs: object) -> list[object]:
        self.calls.append(("list", kwargs))
        return [self.authorization]


def test_consent_application_keeps_launch_separate(monkeypatch) -> None:
    actor = SimpleNamespace(
        user_id=101,
        business_id="business",
        membership_id="membership",
    )
    _Repository.calls.clear()
    monkeypatch.setattr(ad_spend_consent, "get_db", _connection)
    monkeypatch.setattr(ad_spend_consent, "get_db_ro", _connection)
    monkeypatch.setattr(ad_spend_consent, "AdSpendRepository", _Repository)

    requested = ad_spend_consent.request_ad_spend_consent(
        actor=actor,
        authorization_id="authorization",
        now="2026-08-05T18:00:00+00:00",
    )
    granted = ad_spend_consent.grant_ad_spend_consent(
        actor=actor,
        authorization_id="authorization",
        expected_terms_hash="terms-hash",
        expected_snapshot_hash="snapshot-hash",
        now="2026-08-05T18:01:00+00:00",
        receipt_id="receipt",
    )
    revoked = ad_spend_consent.revoke_ad_spend_consent(
        actor=actor,
        authorization_id="authorization",
        now="2026-08-05T18:02:00+00:00",
    )
    listed = ad_spend_consent.list_ad_spend_authorizations(actor=actor, limit=7)

    assert requested is _Repository.authorization
    assert granted.authorization is _Repository.authorization
    assert granted.receipt is _Repository.receipt
    assert revoked is _Repository.authorization
    assert listed == [_Repository.authorization]
    assert [name for name, _ in _Repository.calls] == [
        "request",
        "get",
        "authorize",
        "get",
        "revoke",
        "list",
    ]
    authorize_call = _Repository.calls[2][1]
    assert authorize_call["receipt_id"] == "receipt"
    assert "launch" not in " ".join(name for name, _ in _Repository.calls)


def test_grant_generates_receipt_id_server_side(monkeypatch) -> None:
    actor = SimpleNamespace(user_id=101, business_id="business")
    _Repository.calls.clear()
    monkeypatch.setattr(ad_spend_consent, "get_db", _connection)
    monkeypatch.setattr(ad_spend_consent, "AdSpendRepository", _Repository)

    ad_spend_consent.grant_ad_spend_consent(
        actor=actor,
        authorization_id="authorization",
        expected_terms_hash="terms-hash",
        expected_snapshot_hash="snapshot-hash",
        now="2026-08-05T18:01:00+00:00",
    )

    receipt_id = str(_Repository.calls[1][1]["receipt_id"])
    assert len(receipt_id) == 36
    assert receipt_id.count("-") == 4
