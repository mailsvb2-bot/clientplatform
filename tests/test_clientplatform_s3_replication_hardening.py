from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.clientplatform_s3_replication import (
    ObjectEntry,
    ReplicationConfig,
    S3Client,
    _source_matches,
    sync_objects,
)
from tests.test_clientplatform_s3_replication import FakeS3, _config


class ClientPlatformS3ReplicationHardeningTests(unittest.TestCase):
    def test_request_percent_encodes_unicode_and_spaces(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            client = S3Client(_config(Path(temp)))
            captured: list[str] = []

            class Response:
                status = 200
                headers: dict[str, str] = {}

                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, traceback):
                    return False

                def read(self) -> bytes:
                    return b""

            def fake_urlopen(request, *, timeout):
                captured.append(request.full_url)
                return Response()

            with patch("scripts.clientplatform_s3_replication.urlopen", fake_urlopen):
                client._request(
                    "GET",
                    bucket="clientplatform-production-8493913",
                    key="папка/a b.txt",
                )
            self.assertIn("%D0%BF%D0%B0%D0%BF%D0%BA%D0%B0", captured[0])
            self.assertIn("a%20b.txt", captured[0])

    def test_source_head_must_match_listed_object(self) -> None:
        entry = ObjectEntry("file", "abc", 3, "")
        self.assertTrue(
            _source_matches({"content-length": "3", "etag": '"abc"'}, entry)
        )
        self.assertFalse(
            _source_matches({"content-length": "3", "etag": '"changed"'}, entry)
        )

    def test_evidence_hashes_prefix_instead_of_disclosing_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            client = FakeS3()
            config: ReplicationConfig = _config(Path(temp))
            client.put_object(
                config.source_bucket,
                "sensitive/customer/file.bin",
                b"payload",
                content_type="application/octet-stream",
                metadata={},
            )
            evidence = sync_objects(client, config, prefix="sensitive/customer/")
            serialized = json.dumps(evidence)
            self.assertTrue(evidence["ok"])
            self.assertTrue(evidence["prefix_applied"])
            self.assertNotIn("sensitive/customer/", serialized)
            self.assertEqual(len(str(evidence["prefix_sha256"])), 64)


if __name__ == "__main__":
    unittest.main()
