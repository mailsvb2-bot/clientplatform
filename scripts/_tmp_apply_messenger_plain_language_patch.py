from __future__ import annotations

from pathlib import Path
import subprocess

BASE = "1053d48a47a590a1a8dddda3c3fe503bf8070f5f"
EXPECTED_TREE = "9be6933e0f968a9db7d13f867295eff064138494"


def run(*args: str) -> str:
    return subprocess.check_output(args, text=True).strip()


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"expected one match in {path}, found {count}: {old!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


subprocess.check_call(["git", "merge-base", "--is-ancestor", BASE, "HEAD"])
allowed = {
    ".github/workflows/apply-messenger-plain-language-patch.yml",
    ".github/workflows/apply-messenger-plain-language-patch-v2.yml",
    ".github/workflows/apply-messenger-plain-language-patch-v3.yml",
    ".github/workflows/apply-messenger-plain-language-patch-v4.yml",
    "scripts/_tmp_apply_messenger_plain_language_patch.py",
}
changed = {line for line in run("git", "diff", "--name-only", BASE, "HEAD", "--").splitlines() if line}
unexpected = changed - allowed
if unexpected:
    raise SystemExit(f"unexpected branch paths before materialization: {sorted(unexpected)}")

replace_once(
    "handlers/clientplatform_admin_extension.py",
    "Подтвердите возврат. Он изменит статус оплаты и создаст ",
    "Подтвердите возврат. Он изменит статус оплаты и отдельно учтёт ",
)
replace_once(
    "handlers/clientplatform_admin_extension.py",
    "отдельный канонический факт возврата; повторное нажатие ",
    "возврат в результатах бизнеса; повторное нажатие ",
)
replace_once(
    "handlers/clientplatform_admin_extension.py",
    " Канонический факт выручки подтверждён.",
    " Выручка учтена в результатах бизнеса.",
)

partner = Path("handlers/clientplatform_partner_growth.py")
text = partner.read_text(encoding="utf-8")
marker = "_TERMINAL_CONTACT_STATUSES = {\n    PartnerCandidateStatus.DO_NOT_CONTACT,\n    PartnerCandidateStatus.INVALID,\n}\n"
block = marker + "_DISPATCH_STATUS_LABELS = {\n    \"pending\": \"ожидает отправки\",\n    \"sending\": \"отправляется\",\n    \"retry\": \"ожидает повторной попытки\",\n    \"sent\": \"отправлено\",\n    \"dead\": \"не отправлено\",\n    \"cancelled\": \"отменено\",\n}\n\n\ndef _dispatch_status_label(status: object) -> str:\n    raw = str(getattr(status, \"value\", status) or \"\").strip().lower()\n    return _DISPATCH_STATUS_LABELS.get(raw, \"состояние обновлено\")\n"
if text.count(marker) != 1:
    raise SystemExit("partner status marker mismatch")
text = text.replace(marker, block, 1)
old = '        "📨 Предложение поставлено в каноническую очередь отправки.\\n\\n"\n        f"Статус dispatch: {dispatch.status.value}. "'
new = '        "📨 Предложение поставлено в очередь отправки.\\n\\n"\n        f"Статус отправки: {_dispatch_status_label(dispatch.status)}. "'
if text.count(old) != 1:
    raise SystemExit("partner dispatch copy mismatch")
partner.write_text(text.replace(old, new, 1), encoding="utf-8")

dispatcher = Path("services/messenger/reply_dispatcher.py")
text = dispatcher.read_text(encoding="utf-8")
marker = "async def _send_clientplatform_interaction(\n"
helper = '''_OWNER_COPY_REPLACEMENTS = {
    "Канонический факт выручки подтверждён.": "Выручка учтена в результатах бизнеса.",
    "Подтверждение изменит статус оплаты и создаст отдельный канонический факт возврата.": (
        "Подтверждение изменит статус оплаты и отдельно учтёт возврат в результатах бизнеса."
    ),
}


def _plain_language_clientplatform_interaction(
    interaction: CustomerInteractionMessage,
) -> CustomerInteractionMessage:
    text = interaction.text
    for technical, human in _OWNER_COPY_REPLACEMENTS.items():
        text = text.replace(technical, human)
    if text == interaction.text:
        return interaction
    return CustomerInteractionMessage(text=text, rows=interaction.rows)


'''
if text.count(marker) != 1:
    raise SystemExit("dispatcher marker mismatch")
text = text.replace(marker, helper + marker, 1)
old = "    interaction = CustomerInteractionMessage.from_json(raw_interaction)\n"
new = "    interaction = _plain_language_clientplatform_interaction(\n        CustomerInteractionMessage.from_json(raw_interaction)\n    )\n"
if text.count(old) != 1:
    raise SystemExit("dispatcher interaction mismatch")
dispatcher.write_text(text.replace(old, new, 1), encoding="utf-8")

Path("tests/test_clientplatform_messenger_plain_language_copy.py").write_text(
    '''from __future__ import annotations

from clientplatform.domain.customer_interactions import (
    CustomerInteractionButton,
    CustomerInteractionMessage,
)
from services.messenger.reply_dispatcher import _plain_language_clientplatform_interaction


def _render(text: str) -> CustomerInteractionMessage:
    return _plain_language_clientplatform_interaction(
        CustomerInteractionMessage(
            text=text,
            rows=((CustomerInteractionButton(label="Оплаты", command="cpm:payments"),),),
        )
    )


def test_native_messenger_dispatch_hides_internal_revenue_jargon() -> None:
    rendered = _render(
        "✅ Оплата сохранена: 3 500.00 RUB. Канонический факт выручки подтверждён."
    )
    assert "Выручка учтена в результатах бизнеса" in rendered.text
    assert "каноничес" not in rendered.text.casefold()


def test_native_messenger_dispatch_hides_internal_refund_jargon() -> None:
    rendered = _render(
        "Подтверждение изменит статус оплаты и создаст отдельный канонический факт возврата."
    )
    assert "возврат в результатах бизнеса" in rendered.text
    assert "каноничес" not in rendered.text.casefold()
''',
    encoding="utf-8",
)

replace_once(
    "tests/test_handlers_clientplatform_admin_extension_coverage.py",
    'assert "Канонический факт выручки подтверждён" in payment.answers[-1]',
    'assert "Выручка учтена в результатах бизнеса" in payment.answers[-1]',
)
replace_once(
    "tests/test_handlers_clientplatform_admin_extension_coverage.py",
    '    assert "Подтвердите возврат" in fake_admin.edits[-1][0]',
    '    refund_text = fake_admin.edits[-1][0]\n    assert "Подтвердите возврат" in refund_text\n    assert "возврат в результатах бизнеса" in refund_text\n    assert "каноничес" not in refund_text.casefold()',
)
replace_once(
    "tests/test_handlers_clientplatform_partner_growth_surfaces.py",
    '        self.assertIn("Повторное нажатие не создаст дубль", self.output.answer.await_args.args[0])',
    '        text = self.output.answer.await_args.args[0]\n        self.assertIn("Предложение поставлено в очередь отправки", text)\n        self.assertIn("Статус отправки: ожидает отправки", text)\n        self.assertNotIn("каноничес", text.casefold())\n        self.assertNotIn("dispatch", text.casefold())\n        self.assertIn("Повторное нажатие не создаст дубль", text)',
)

for path in allowed:
    Path(path).unlink(missing_ok=True)
subprocess.check_call(["git", "diff", "--check"])
subprocess.check_call(["git", "add", "-A"])
tree = run("git", "write-tree")
print(f"candidate_tree={tree}")
if tree != EXPECTED_TREE:
    raise SystemExit(f"tree mismatch: {tree} != {EXPECTED_TREE}")
