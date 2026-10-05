from __future__ import annotations

import unittest

from local_api import opportunities


class LocalApiTests(unittest.TestCase):
    def test_response_omits_message_body_and_private_data(self) -> None:
        report = {"generated_at": "2026-10-01T18:00:00-04:00", "messages": [{
            "source": "gmail", "body": "private email body", "sender": "person@example.test",
        }]}
        result = opportunities(report)
        self.assertEqual(result["count"], 0)
        self.assertNotIn("private email body", str(result))
        self.assertNotIn("person@example.test", str(result))
