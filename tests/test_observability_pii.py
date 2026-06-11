import unittest

from app.observability.pii import prompt_hash, redact_pii


class PIIRedactionTests(unittest.TestCase):
    def test_redact_pii_replaces_email_phone_and_card(self):
        raw = "Мой email ivan@mail.ru, тел +7 (999) 123-45-67, карта 4111 1111 1111 1111"

        prompt_preview = redact_pii(raw)[:120]

        self.assertIn("[EMAIL]", prompt_preview)
        self.assertIn("[PHONE_RU]", prompt_preview)
        self.assertIn("[CARD]", prompt_preview)
        for leaked in (
            "ivan",
            "mail.ru",
            "+7",
            "999",
            "123-45-67",
            "4111",
        ):
            self.assertNotIn(leaked, prompt_preview)

    def test_prompt_hash_is_stable_and_not_raw_prompt(self):
        raw = "email@example.com, +7 999 123 45 67"

        digest = prompt_hash(raw)
        prompt_preview = redact_pii(raw)

        self.assertEqual(digest, prompt_hash(raw))
        self.assertTrue(digest.startswith("sha256:"))
        self.assertNotIn("email@example.com", prompt_preview)
        self.assertNotIn("+7 999 123 45 67", prompt_preview)
        self.assertIn("[EMAIL]", prompt_preview)
        self.assertIn("[PHONE_RU]", prompt_preview)
