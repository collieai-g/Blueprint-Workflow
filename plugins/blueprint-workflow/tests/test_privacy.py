import unittest

from blueprint_workflow.privacy import sanitize, MEDIA_OMITTED, REDACTED


class PrivacyTests(unittest.TestCase):
    def test_drops_media_without_removing_useful_text(self):
        result = sanitize({"content": [{"type": "text", "text": "Created report"}, {"type": "image", "data": "private pixels"}]})
        self.assertEqual(result["content"][0]["text"], "Created report")
        self.assertEqual(result["content"][1]["description"], MEDIA_OMITTED)
        self.assertNotIn("private pixels", str(result))

    def test_keeps_binding_and_product_ids_but_redacts_secrets(self):
        result = sanitize({"host_session_id": "chat-1", "password": "private", "url": "https://user:pwd@shop.test/item?id=123&token=abc"})
        self.assertEqual(result["host_session_id"], "chat-1")
        self.assertEqual(result["password"], REDACTED)
        self.assertIn("id=123", result["url"])
        self.assertNotIn("abc", result["url"])
        self.assertNotIn("user:pwd", result["url"])

    def test_text_credentials_and_base64(self):
        self.assertNotIn("topsecret", sanitize("password=topsecret"))
        self.assertEqual(sanitize("A" * 3000), MEDIA_OMITTED)

    def test_boundaries(self):
        with self.assertRaises(ValueError):
            sanitize({1: "invalid"})


if __name__ == "__main__":
    unittest.main()
