from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from bookings.emails import _business_signature, build_issuer_snapshot
from bookings.receipts import _resolve_issuer


class EmailBusinessDisplayNameTests(SimpleTestCase):
    def setUp(self):
        self.profile = SimpleNamespace(
            company_name="株式会社ラグゴー",
            company_email="owner@example.com",
            user=None,
            invoice_registration_number="",
        )

    @patch("bookings.emails.get_business_stripe_info")
    def test_non_japanese_email_uses_stripe_english_name(self, stripe_info):
        # Arrange: Stripeが英語社名を返すようモック
        stripe_info.return_value = {"company_name_en": "LugGo Transport Inc."}

        # Act: 英語メール用の署名を組み立てる
        signature = _business_signature(self.profile, "en")

        # Assert: 英語社名が使われることを確認
        self.assertEqual(signature["business_name"], "LugGo Transport Inc.")

    @patch("bookings.emails.get_business_stripe_info")
    def test_japanese_email_keeps_japanese_name(self, stripe_info):
        # Arrange: Stripeが英語社名を返すようモック
        stripe_info.return_value = {"company_name_en": "LugGo Transport Inc."}

        # Act: 日本語メール用の署名を組み立てる
        signature = _business_signature(self.profile, "ja")

        # Assert: 日本語社名のままであることを確認
        self.assertEqual(signature["business_name"], "株式会社ラグゴー")

    @patch("bookings.emails.get_business_stripe_info")
    def test_non_japanese_email_falls_back_to_japanese_name(self, stripe_info):
        # Arrange: 英語社名が無い状態をモック
        stripe_info.return_value = {"company_name_en": ""}

        # Act: 日本語以外のメール用署名を組み立てる
        signature = _business_signature(self.profile, "zh-Hans")

        # Assert: 英語名が無いため日本語社名へフォールバックすることを確認
        self.assertEqual(signature["business_name"], "株式会社ラグゴー")

    @patch("bookings.emails.get_business_stripe_info")
    def test_issuer_snapshot_preserves_both_names(self, stripe_info):
        # Arrange: Stripeが英語社名を返すようモック
        stripe_info.return_value = {"company_name_en": "LugGo Transport Inc."}

        # Act: 発行者スナップショットを組み立てる
        snapshot = build_issuer_snapshot(self.profile)

        # Assert: 日本語名と英語名の両方が保持されることを確認
        self.assertEqual(snapshot["issuer_name"], "株式会社ラグゴー")
        self.assertEqual(snapshot["issuer_name_en"], "LugGo Transport Inc.")


class ReceiptBusinessDisplayNameTests(SimpleTestCase):
    def _booking(self, **overrides):
        values = {
            "id": "booking-test",
            "issuer_name": "株式会社ラグゴー",
            "issuer_name_en": "LugGo Transport Inc.",
            "issuer_address": "東京都",
            "issuer_invoice_number": "",
            "business_owner": None,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_non_japanese_receipt_uses_english_snapshot(self):
        # Arrange: 日英両方の発行者名を持つ予約を用意
        # Act: 海外向け領収書の発行者名を解決
        issuer, _, _ = _resolve_issuer(self._booking(), "en")

        # Assert: 英語名が使われることを確認
        self.assertEqual(issuer, "LugGo Transport Inc.")

    def test_japanese_receipt_uses_japanese_snapshot(self):
        # Arrange: 日英両方の発行者名を持つ予約を用意
        # Act: 日本語領収書の発行者名を解決
        issuer, _, _ = _resolve_issuer(self._booking(), "ja")

        # Assert: 日本語名が使われることを確認
        self.assertEqual(issuer, "株式会社ラグゴー")

    @patch("bookings.receipts.build_issuer_snapshot")
    def test_existing_booking_fetches_english_name(self, build_snapshot):
        # Arrange: 英語名未保存の予約と、Stripe補完のスナップショットを用意
        build_snapshot.return_value = {
            "issuer_name": "株式会社ラグゴー",
            "issuer_name_en": "LugGo Transport Inc.",
            "issuer_address": "東京都",
            "issuer_invoice_number": "",
        }
        booking = self._booking(issuer_name_en="")

        # Act: 海外向け領収書の発行者名を解決
        issuer, _, _ = _resolve_issuer(booking, "zh-Hant")

        # Assert: 補完された英語名が使われることを確認
        self.assertEqual(issuer, "LugGo Transport Inc.")
