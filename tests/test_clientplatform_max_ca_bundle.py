from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from runtime.messenger_max_sender import MaxBotSender


class MaxCaBundleTests(unittest.TestCase):
    def test_custom_max_ca_extends_system_trust_instead_of_replacing_it(self) -> None:
        with tempfile.NamedTemporaryFile() as bundle:
            context = MagicMock()
            with (
                patch.dict(os.environ, {"MAX_CA_BUNDLE": bundle.name}),
                patch(
                    "runtime.messenger_max_sender.ssl.create_default_context",
                    return_value=context,
                ) as create_context,
            ):
                resolved = MaxBotSender(token="provider-token")._ssl_context()

        self.assertIs(resolved, context)
        create_context.assert_called_once_with()
        context.load_verify_locations.assert_called_once_with(cafile=bundle.name)


if __name__ == "__main__":
    unittest.main()
