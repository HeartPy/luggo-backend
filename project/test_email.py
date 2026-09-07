"""メール送信（表示名・Reply-To）のテスト"""
from django.core import mail
from django.test import SimpleTestCase, override_settings

from project.email import format_from_header, platform_from_header, send_email


class FormatFromHeaderTests(SimpleTestCase):
    def test_plain_name(self) -> None:
        # Act: 記号なしの表示名で From を組み立てる
        header = format_from_header("LugGo", "noreply@luggo.delivery")

        # Assert: 引用なしの Name <addr>
        self.assertEqual(header, "LugGo <noreply@luggo.delivery>")

    def test_name_with_parentheses_is_quoted(self) -> None:
        # Act: 括弧を含む表示名で From を組み立てる
        header = format_from_header("LugGo(ラグゴー)", "noreply@luggo.delivery")

        # Assert: RFC 向けに引用される
        self.assertEqual(
            header, '"LugGo(ラグゴー)" <noreply@luggo.delivery>'
        )


class PlatformFromHeaderTests(SimpleTestCase):
    @override_settings(
        DEFAULT_FROM_EMAIL="noreply@luggo.delivery",
        PLATFORM_FROM_DISPLAY_NAME="LugGo(ラグゴー)",
    )
    def test_platform_from_uses_luggo_display_name(self) -> None:
        # Act: 運営用 From を組み立てる
        header = platform_from_header()

        # Assert: 表示名が LugGo(ラグゴー)
        self.assertEqual(
            header, '"LugGo(ラグゴー)" <noreply@luggo.delivery>'
        )


class SendEmailHeaderTests(SimpleTestCase):
    @override_settings(
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        RESEND_API_KEY="",
        DEFAULT_FROM_EMAIL="noreply@luggo.delivery",
        PLATFORM_FROM_DISPLAY_NAME="LugGo(ラグゴー)",
    )
    def test_default_from_is_platform_display_name(self) -> None:
        # Act: from_email 未指定で送信
        sent = send_email(subject="件名", text="本文", to="user@example.com")

        # Assert: 運営の表示名付き From
        self.assertTrue(sent)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(
            mail.outbox[0].from_email,
            '"LugGo(ラグゴー)" <noreply@luggo.delivery>',
        )
        self.assertEqual(mail.outbox[0].reply_to, [])

    @override_settings(
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        RESEND_API_KEY="",
        DEFAULT_FROM_EMAIL="noreply@luggo.delivery",
    )
    def test_reply_to_and_business_display_name(self) -> None:
        # Act: 事業者表示名・Reply-To 付きで送信
        sent = send_email(
            subject="予約確認",
            text="本文",
            to="traveler@example.com",
            from_email=format_from_header(
                "テスト配送", "noreply@luggo.delivery"
            ),
            reply_to="owner@example.com",
        )

        # Assert: From は事業者名、Reply-To は事業者メール
        self.assertTrue(sent)
        message = mail.outbox[0]
        self.assertEqual(
            message.from_email, "テスト配送 <noreply@luggo.delivery>"
        )
        self.assertEqual(message.reply_to, ["owner@example.com"])
