from datetime import date, datetime
from unittest.mock import patch

from django.core import mail
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from bookings.models import LuggageBooking
from project.email import send_email
from users.models import User
from .models import BusinessProfile, PayoutDocumentDelivery
from .payout_documents import (
    _platform_seal_path,
    build_fee_invoice_pdf,
    build_payment_statement_pdf,
    create_delivery_record,
    list_paid_payouts,
    previous_month_payout_date,
    send_delivery_record,
)
from .utils import _render_email, owner_email_addressee_context


class OwnerEmailAddresseeTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(
            email='addressee@example.com',
            password='test-password',
            user_type='business_owner',
        )

    def _profile(self, *, business_type: str) -> BusinessProfile:
        return BusinessProfile.objects.create(
            user=self.user,
            company_name='株式会社テスト',
            company_email='addressee@example.com',
            business_type=business_type,
            rep_last_name_kanji='山田',
            rep_first_name_kanji='太郎',
            subdomain='addrtest',
        )

    def test_company_shows_company_name_above_rep_name(self) -> None:
        # Arrange: 法人の事業者とメール用コンテキストを用意
        profile = self._profile(business_type='company')
        context = owner_email_addressee_context(profile)
        context.update(
            {
                'payout_date': '2026年06月25日',
                'payout_amount': '¥9,000',
                'signature': '署名',
            }
        )

        # Act: 支払書類メール本文を生成
        _subject, body = _render_email('monthly_payout_documents', context)

        # Assert: 会社名の下に代表者名が来ることを確認
        self.assertTrue(body.startswith('株式会社テスト\n山田 太郎 様'))

    def test_individual_omits_company_name_above_rep_name(self) -> None:
        # Arrange: 個人事業主の事業者とメール用コンテキストを用意
        profile = self._profile(business_type='individual')
        context = owner_email_addressee_context(profile)
        context.update(
            {
                'payout_date': '2026年06月25日',
                'payout_amount': '¥9,000',
                'signature': '署名',
            }
        )

        # Act: 支払書類メール本文を生成
        _subject, body = _render_email('monthly_payout_documents', context)

        # Assert: 会社名なしで代表者名から始まることを確認
        self.assertTrue(body.startswith('山田 太郎 様'))
        self.assertFalse(body.startswith('株式会社テスト'))


class EmailAttachmentTests(SimpleTestCase):
    @override_settings(
        EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
        RESEND_API_KEY='',
    )
    def test_django_backend_sends_pdf_attachment(self) -> None:
        # Arrange: PDF添付付きのメール送信内容を用意
        # Act: Django のメールバックエンドで送信
        sent = send_email(
            subject='添付テスト',
            text='本文',
            to='owner@example.com',
            attachments=[('document.pdf', b'%PDF-test', 'application/pdf')],
        )

        # Assert: 送信成功し、PDFが添付されていることを確認
        self.assertTrue(sent)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].attachments[0][0], 'document.pdf')


class PayoutDocumentTests(TestCase):
    def setUp(self) -> None:
        user = User.objects.create_user(
            email='owner@example.com',
            password='test-password',
            user_type='business_owner',
        )
        self.profile = BusinessProfile.objects.create(
            user=user,
            company_name='テスト配送',
            company_email='owner@example.com',
            stripe_account_id='acct_test',
            rep_last_name_kanji='山田',
            rep_first_name_kanji='太郎',
        )
        self.booking = LuggageBooking.objects.create(
            business_owner=self.profile,
            pickup_location_name='集荷場所',
            pickup_location_address='東京都',
            pickup_date=date(2026, 6, 20),
            delivery_location_name='配送場所',
            delivery_location_address='東京都',
            delivery_date=date(2026, 6, 21),
            total_amount=10_000,
            customer_name='旅行者',
            customer_email='guest@example.com',
            customer_phone_number='09012345678',
            customer_nationality='JPN',
            guest_name='宿泊者',
            payment_intent_id='pi_test',
            delivery_status='delivered',
            stripe_transfer_id='tr_test',
            transferred_at=timezone.make_aware(datetime(2026, 6, 22, 12)),
            delivered_at=timezone.make_aware(datetime(2026, 6, 21, 15)),
            transferred_gross_amount=10_000,
            transferred_platform_fee=1_000,
        )
        payout_created = timezone.make_aware(datetime(2026, 6, 25, 9))
        arrival = timezone.make_aware(datetime(2026, 6, 27, 9))
        self.payout = {
            'id': 'po_test',
            'amount': 9_000,
            'currency': 'jpy',
            'created': int(payout_created.timestamp()),
            'arrival_date': int(arrival.timestamp()),
        }

    def test_previous_month_payout_date(self) -> None:
        # Arrange: 基準日を用意
        # Act & Assert: 前月25日が返ることを確認
        self.assertEqual(
            previous_month_payout_date(date(2026, 7, 1)),
            date(2026, 6, 25),
        )

    @patch('business_owners.payout_documents.stripe.Payout.list')
    def test_lists_automatic_payout_shifted_by_holidays(self, payout_list) -> None:
        # Arrange: 休日ずれ・手動・範囲外のPayoutをモック
        shifted = timezone.make_aware(datetime(2026, 6, 27, 9))
        manual = timezone.make_aware(datetime(2026, 6, 26, 9))
        outside_window = timezone.make_aware(datetime(2026, 7, 3, 9))
        payout_list.return_value = {
            'data': [
                {
                    'id': 'po_shifted',
                    'created': int(shifted.timestamp()),
                    'automatic': True,
                },
                {
                    'id': 'po_manual',
                    'created': int(manual.timestamp()),
                    'automatic': False,
                },
                {
                    'id': 'po_outside',
                    'created': int(outside_window.timestamp()),
                    'automatic': True,
                },
            ]
        }

        # Act: 対象日まわりの支払い済み自動入金を取得
        payouts = list_paid_payouts(self.profile, date(2026, 6, 25))

        # Assert: 休日ずれの自動入金だけが対象で、検索期間も想定どおりであることを確認
        self.assertEqual([payout['id'] for payout in payouts], ['po_shifted'])
        created_range = payout_list.call_args.kwargs['created']
        expected_start = timezone.make_aware(datetime(2026, 6, 25))
        expected_end = timezone.make_aware(datetime(2026, 7, 3))
        self.assertEqual(created_range['gte'], int(expected_start.timestamp()))
        self.assertEqual(created_range['lt'], int(expected_end.timestamp()))

    @override_settings(
        PLATFORM_INVOICE_ISSUER_NAME='LugGo運営',
        PLATFORM_INVOICE_ISSUER_ADDRESS='東京都',
        PLATFORM_INVOICE_REGISTRATION_NUMBER='',
    )
    @patch('business_owners.payout_documents.stripe.BalanceTransaction.list')
    def test_creates_record_without_registration_number(
        self,
        balance_list,
    ) -> None:
        # Arrange: 予約Transferと一致する残高取引をモック
        balance_list.return_value = {
            'data': [{'source': 'tr_test', 'net': 9_000}]
        }

        # Act: 支払書類の送付記録を作成
        record = create_delivery_record(self.profile, self.payout)

        # Assert: 金額・明細が正しく保存され、PDFも生成できることを確認
        self.assertEqual(record.payout_amount, 9_000)
        self.assertEqual(record.gross_sales, 10_000)
        self.assertEqual(record.platform_fee, 1_000)
        self.assertEqual(record.matched_transfer_amount, 9_000)
        self.assertEqual(record.adjustment_amount, 0)
        self.assertEqual(record.issuer_registration_number, '')
        self.assertEqual(record.line_items[0]['delivered_on'], '2026-06-21')
        self.assertIsNotNone(_platform_seal_path())
        invoice_pdf = build_fee_invoice_pdf(record)
        self.assertGreater(len(invoice_pdf), 100)
        # 印鑑画像を埋め込むとPDFが大きくなる
        self.assertGreater(len(invoice_pdf), 10_000)
        self.assertGreater(len(build_payment_statement_pdf(record)), 100)

    @override_settings(
        OPERATIONS_NOTIFICATION_EMAIL='ops1@example.com,ops2@example.com',
        PLATFORM_INVOICE_REGISTRATION_NUMBER='',
    )
    @patch('business_owners.payout_documents.send_email')
    @patch('business_owners.payout_documents.stripe.BalanceTransaction.list')
    def test_notifies_operations_when_payout_has_adjustment(
        self,
        balance_list,
        send_email_mock,
    ) -> None:
        # Arrange: 入金額と照合合計がずれる残高取引をモック
        balance_list.return_value = {
            'data': [{'source': 'tr_test', 'net': 9_000}]
        }
        self.payout['amount'] = 9_500

        # Act: 支払書類の送付記録を作成
        record = create_delivery_record(self.profile, self.payout)

        # Assert: 差額が保存され、運営へ通知メールが送られることを確認
        self.assertEqual(record.adjustment_amount, 500)
        send_email_mock.assert_called_once()
        email = send_email_mock.call_args.kwargs
        self.assertEqual(
            email['to'],
            ['ops1@example.com', 'ops2@example.com'],
        )
        self.assertIn('入金額と予約明細の合計が一致しません', email['subject'])
        self.assertIn('Stripe Payout ID   : po_test', email['text'])
        self.assertIn('差額               : ¥500', email['text'])

    @override_settings(
        OPERATIONS_NOTIFICATION_EMAIL='ops@example.com',
        PLATFORM_INVOICE_REGISTRATION_NUMBER='',
    )
    @patch('business_owners.payout_documents.send_email')
    @patch('business_owners.payout_documents.stripe.BalanceTransaction.list')
    def test_does_not_notify_operations_without_adjustment(
        self,
        balance_list,
        send_email_mock,
    ) -> None:
        # Arrange: 入金額と照合合計が一致する残高取引をモック
        balance_list.return_value = {
            'data': [{'source': 'tr_test', 'net': 9_000}]
        }

        # Act: 支払書類の送付記録を作成
        record = create_delivery_record(self.profile, self.payout)

        # Assert: 差額がなく、運営通知メールが送られないことを確認
        self.assertEqual(record.adjustment_amount, 0)
        send_email_mock.assert_not_called()

    @override_settings(PLATFORM_INVOICE_REGISTRATION_NUMBER='')
    @patch('business_owners.payout_documents.send_email')
    def test_sends_two_pdfs_once_without_registration_number(
        self,
        send_email_mock,
    ) -> None:
        # Arrange: 未送信の支払書類記録を用意
        send_email_mock.return_value = True
        record = PayoutDocumentDelivery.objects.create(
            business_profile=self.profile,
            stripe_payout_id='po_send',
            payout_created_at=timezone.make_aware(datetime(2026, 6, 25, 9)),
            arrival_date=date(2026, 6, 27),
            payout_amount=9_000,
            gross_sales=10_000,
            platform_fee=1_000,
            matched_transfer_amount=9_000,
            adjustment_amount=0,
            line_items=[
                {
                    'booking_id': str(self.booking.id),
                    'booking_number': self.booking.booking_number,
                    'stripe_transfer_id': 'tr_test',
                    'delivered_on': '2026-06-21',
                    'gross_sales': 10_000,
                    'platform_fee': 1_000,
                    'transfer_amount': 9_000,
                }
            ],
            issuer_name='LugGo運営',
            issuer_address='',
            issuer_registration_number='',
            recipient_email='owner@example.com',
        )

        # Act: 請求書・支払明細をメール送信
        self.assertTrue(send_delivery_record(record))

        # Assert: 送信済みになり、件名・本文・PDF2点が想定どおりであることを確認
        record.refresh_from_db()
        self.assertIsNotNone(record.sent_at)
        self.assertEqual(record.send_attempts, 1)
        kwargs = send_email_mock.call_args.kwargs
        self.assertEqual(
            kwargs['subject'],
            '【LugGo】2026年06月27日支払分の請求書・支払明細',
        )
        self.assertIn('山田 太郎 様', kwargs['text'])
        self.assertFalse(kwargs['text'].startswith('テスト配送'))
        self.assertIn('お振込み金額　：¥9,000', kwargs['text'])
        self.assertIn('請求書および支払明細', kwargs['text'])
        attachments = kwargs['attachments']
        self.assertEqual(len(attachments), 2)
        self.assertEqual(
            attachments[0][0],
            '請求書_テスト配送_20260627_po_send.pdf',
        )
        self.assertEqual(
            attachments[1][0],
            '支払明細_テスト配送_20260627_po_send.pdf',
        )
        self.assertTrue(
            all(attachment[2] == 'application/pdf' for attachment in attachments)
        )

        # Act: 送信済みレコードに対して再送を試みる
        # Assert: 再送されず、メール送信は1回のままであることを確認
        self.assertFalse(send_delivery_record(record))
        send_email_mock.assert_called_once()
