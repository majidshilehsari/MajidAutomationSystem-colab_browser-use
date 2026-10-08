"""Tests for read-only CAPTCHA/security-challenge classification."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from automation.challenge import CHALLENGE_SCRIPT, assess_page, safe_origin  # noqa: E402


class ChallengeAssessmentTest(unittest.TestCase):
    def test_visible_provider_frame_is_a_strong_signal(self):
        result = assess_page({"text": "", "hints": ["IFRAME https://www.google.com/recaptcha/anchor"]})
        self.assertTrue(result["detected"])
        self.assertIn("visible-captcha-or-challenge-widget", result["signals"])

    def test_human_verification_phrase_is_detected(self):
        result = assess_page({"text": "Please verify you are human to continue."})
        self.assertIn("human-verification-wording", result["signals"])

    def test_captcha_instructions_are_detected(self):
        result = assess_page({"text": "Enter the CAPTCHA code shown in the image."})
        self.assertIn("captcha-instructions", result["signals"])

    def test_security_verification_is_detected(self):
        result = assess_page({"text": "Security Verification required before continuing."})
        self.assertIn("security-verification-wording", result["signals"])

    def test_persian_human_check_phrase_is_detected(self):
        result = assess_page({"text": "لطفاً تأیید کنید که انسان هستید."})
        self.assertTrue(result["detected"])
        self.assertIn("localized-human-verification-wording", result["signals"])

    def test_cloudflare_browser_interstitial_is_detected(self):
        result = assess_page({
            "title": "Just a moment...",
            "text": "Checking your browser before accessing this website.",
        })
        self.assertIn("browser-challenge-wording", result["signals"])
        self.assertIn("browser-challenge-title", result["signals"])

    def test_plain_help_text_mention_is_not_enough(self):
        result = assess_page({"text": "CAPTCHA is one way websites discuss security in documentation."})
        self.assertFalse(result["detected"])
        self.assertEqual(result["signals"], [])

    def test_assessment_never_returns_raw_body_or_query_parameters(self):
        result = assess_page({
            "url": "https://alice:secret@example.com/private?token=do-not-log#fragment",
            "text": "Verify you are human; private text",
        })
        self.assertEqual(result["pageOrigin"], "https://example.com")
        self.assertEqual(set(result), {"detected", "signals", "pageOrigin"})
        self.assertNotIn("private text", str(result))
        self.assertNotIn("secret", str(result))
        self.assertNotIn("token=", str(result))

    def test_safe_origin_strips_path_query_and_credentials(self):
        self.assertEqual(safe_origin("https://user:pass@example.com:8443/path?q=1"),
                         "https://example.com:8443")
        self.assertEqual(safe_origin("http://example.com:80/path"), "http://example.com")
        self.assertEqual(safe_origin("file:///tmp/page"), "")

    def test_probe_is_read_only(self):
        self.assertIn("document.querySelectorAll", CHALLENGE_SCRIPT)
        self.assertNotIn(".click(", CHALLENGE_SCRIPT)
        self.assertNotIn(".submit(", CHALLENGE_SCRIPT)
        self.assertNotIn(".type(", CHALLENGE_SCRIPT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
