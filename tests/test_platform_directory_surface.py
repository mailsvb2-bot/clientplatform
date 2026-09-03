from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from handlers import clientplatform_entry as entry
from clientplatform.application import platform_directory as directory
from clientplatform.domain.platform_directory import PlatformDirectoryQueryKind
from clientplatform.domain.tenancy import BusinessStatus, PlatformRole


class PlatformDirectorySurfaceTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def message(text: str, *, user_id: int = 9001):
        return SimpleNamespace(
            text=text,
            from_user=SimpleNamespace(id=user_id, username="operator", full_name="Operator"),
            answer=AsyncMock(),
        )

    @staticmethod
    def result(*, matches=(), truncated: bool = False):
        return directory.PlatformDirectorySearchResult(
            query_kind=PlatformDirectoryQueryKind.BUSINESS_NAME,
            matches=tuple(matches),
            truncated=truncated,
            audit_id="audit-123",
            searched_at="2026-09-03T10:00:00+00:00",
        )

    async def test_directory_denies_non_operator_without_results(self) -> None:
        message = self.message("/platformdirectory name Alpha", user_id=17)
        with (
            patch.object(entry.control, "_user_id", return_value=17),
            patch.object(
                directory,
                "search_platform_directory",
                side_effect=directory.PlatformDirectoryPermissionDenied("denied"),
            ) as search,
        ):
            await entry.clientplatform_platform_directory_command(message)
        search.assert_called_once_with(
            17,
            query_kind="business_name",
            query="Alpha",
            limit=20,
        )
        message.answer.assert_awaited_once_with("Доступ к platform directory недоступен.")

    async def test_directory_maps_all_three_query_kinds(self) -> None:
        for token, expected, query in (
            ("business", "business_id", "4d607378-a479-45e8-b0e2-7d716bb42bcf"),
            ("user", "user_id", "555"),
            ("name", "business_name", "Alpha Studio"),
        ):
            message = self.message(f"/platformdirectory {token} {query}")
            with (
                patch.object(entry.control, "_user_id", return_value=9001),
                patch.object(
                    directory,
                    "search_platform_directory",
                    return_value=self.result(),
                ) as search,
            ):
                await entry.clientplatform_platform_directory_command(message)
            search.assert_called_once_with(
                9001,
                query_kind=expected,
                query=query,
                limit=20,
            )
            self.assertIn("Совпадений нет", message.answer.await_args.args[0])
            self.assertIn("audit-123", message.answer.await_args.args[0])

    async def test_directory_rejects_unknown_syntax_safely(self) -> None:
        for text in (
            "/platformdirectory",
            "/platformdirectory nope anything",
        ):
            message = self.message(text)
            with patch.object(entry.control, "_user_id", return_value=9001):
                await entry.clientplatform_platform_directory_command(message)
            self.assertIn("Использование", message.answer.await_args.args[0])

    async def test_directory_maps_broad_query_validation_to_safe_error(self) -> None:
        message = self.message("/platformdirectory name %")
        with (
            patch.object(entry.control, "_user_id", return_value=9001),
            patch.object(
                directory,
                "search_platform_directory",
                side_effect=ValueError("unbounded business name query is forbidden"),
            ) as search,
        ):
            await entry.clientplatform_platform_directory_command(message)
        search.assert_called_once_with(9001, query_kind="business_name", query="%", limit=20)
        self.assertIn("некорректны", message.answer.await_args.args[0])

    async def test_directory_formats_minimal_metadata_only(self) -> None:
        match = SimpleNamespace(
            business_id="4d607378-a479-45e8-b0e2-7d716bb42bcf",
            business_name="Alpha Studio",
            business_status=BusinessStatus.ACTIVE,
            business_created_at="2026-09-02T10:00:00+00:00",
            active_member_count=3,
            active_owner_count=1,
            matched_user_id=555,
            matched_role=PlatformRole.SUPPORT,
            matched_membership_status="active",
        )
        message = self.message("/platformdirectory user 555")
        with (
            patch.object(entry.control, "_user_id", return_value=9001),
            patch.object(
                directory,
                "search_platform_directory",
                return_value=self.result(matches=(match,)),
            ),
        ):
            await entry.clientplatform_platform_directory_command(message)
        text = "\n".join(call.args[0] for call in message.answer.await_args_list)
        self.assertIn("Alpha Studio", text)
        self.assertIn(match.business_id, text)
        self.assertIn("user=555", text)
        self.assertIn("role=support", text)
        self.assertIn("Audit: audit-123", text)

    def test_directory_chunks_remain_under_telegram_limit(self) -> None:
        matches = []
        for index in range(20):
            matches.append(
                SimpleNamespace(
                    business_id=f"00000000-0000-0000-0000-{index:012d}",
                    business_name=f"Directory Business {index:02d} " + "x" * 120,
                    business_status=BusinessStatus.ACTIVE,
                    business_created_at="2026-09-02T10:00:00+00:00",
                    active_member_count=100,
                    active_owner_count=2,
                    matched_user_id=555,
                    matched_role=PlatformRole.SUPPORT,
                    matched_membership_status="active",
                )
            )
        chunks = entry._platform_directory_chunks(self.result(matches=matches))
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= entry._TELEGRAM_SAFE_TEXT_LIMIT for chunk in chunks))
        combined = "\n".join(chunks)
        for match in matches:
            self.assertIn(match.business_id, combined)

    def test_directory_surfaces_truncation_warning(self) -> None:
        match = SimpleNamespace(
            business_id="4d607378-a479-45e8-b0e2-7d716bb42bcf",
            business_name="Alpha Studio",
            business_status=BusinessStatus.ACTIVE,
            business_created_at="2026-09-02T10:00:00+00:00",
            active_member_count=3,
            active_owner_count=1,
            matched_user_id=555,
            matched_role=PlatformRole.SUPPORT,
            matched_membership_status="active",
        )
        chunks = entry._platform_directory_chunks(self.result(matches=(match,), truncated=True))
        self.assertIn("первые 20", chunks[0])
        self.assertIn("дополнительные результаты", chunks[0])

    async def test_platform_directory_stays_out_of_public_command_menu(self) -> None:
        bot = SimpleNamespace(set_my_commands=AsyncMock(return_value=True))
        self.assertTrue(await entry.register_clientplatform_bot_commands(bot))
        commands = bot.set_my_commands.await_args.args[0]
        command_names = {item.command for item in commands}
        self.assertNotIn("platformdirectory", command_names)
        self.assertNotIn("supportqueue", command_names)
        self.assertNotIn("supportsession", command_names)


if __name__ == "__main__":
    unittest.main()
