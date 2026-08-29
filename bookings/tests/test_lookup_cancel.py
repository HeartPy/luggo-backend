"""ユーザー（旅行者）の予約照会・キャンセル・返金APIのテスト"""
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import stripe
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from business_owners.models import BusinessProfile

from .helpers import BaseBookingTest


class BookingLookupAPITest(BaseBookingTest, APITestCase):
    """予約番号による予約照会API（旅行者向け）のテスト"""

    def setUp(self):
        # Arrange: 事業者と照会対象の予約を作成
        self.user = self._create_test_user(email='owner-lookup@example.com')
        self.profile = BusinessProfile.objects.create(
            user=self.user,
            company_name='照会テスト事業者',
            company_email='owner-lookup@example.com',
        )
        self.booking = self._create_test_booking(business_owner=self.profile)
        self.url = reverse('bookings:booking-lookup')

    def test_lookup_requires_booking_number(self):
        """予約番号なしでは照会できない"""
        # Act: 予約番号なしで照会APIを呼び出し
        response = self.client.get(self.url)

        # Assert: 400 になることを確認
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_lookup_returns_404_for_unknown_number(self):
        """存在しない予約番号は見つからない扱いになる"""
        # Act: 存在しない予約番号で照会APIを呼び出し
        response = self.client.get(self.url, {'booking_number': 'LG-XXXX-XXXX-XXXX'})

        # Assert: 404 になることを確認
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @patch('bookings.views.stripe.PaymentIntent.retrieve')
    def test_lookup_returns_booking_with_cancelability(self, retrieve_mock):
        """予約番号で照会でき、キャンセル可否とステータス表示名が返る"""
        # Arrange: 支払い方法の取得（表示用）をモック
        retrieve_mock.return_value = SimpleNamespace(payment_method=None)

        # Act: 予約番号で照会APIを呼び出し
        response = self.client.get(
            self.url, {'booking_number': self.booking.booking_number}
        )

        # Assert: 予約情報とキャンセル可否が返ることを確認
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['booking_number'], self.booking.booking_number)
        self.assertTrue(response.data['can_cancel'])
        self.assertIn('delivery_status_label', response.data)


class BookingCancelAPITest(BaseBookingTest, APITestCase):
    """予約キャンセルAPI（旅行者向け・全額返金）のテスト"""

    def setUp(self):
        # Arrange: 事業者を作成
        self.user = self._create_test_user(email='owner-cancel@example.com')
        self.profile = BusinessProfile.objects.create(
            user=self.user,
            company_name='キャンセルテスト事業者',
            company_email='owner-cancel@example.com',
            stripe_account_id='acct_test',
        )

    def _cancel_url(self, booking_id):
        return reverse('bookings:cancel-booking', args=[booking_id])

    def _refundable_booking(self, **kwargs):
        """集荷日前日23時より前（返金対象）の予約を作成"""
        defaults = {
            'business_owner': self.profile,
            'pickup_date': date.today() + timedelta(days=5),
            'delivery_date': date.today() + timedelta(days=5),
        }
        defaults.update(kwargs)
        return self._create_test_booking(**defaults)

    @patch('bookings.views.send_booking_cancellation_emails')
    @patch('bookings.owner_views.stripe.Refund.create')
    def test_cancel_refundable_booking_refunds_and_cancels(
        self, refund_mock, email_mock
    ):
        """返金期限内のキャンセルは全額返金してキャンセル状態にする"""
        # Arrange: 返金対象の予約と返金成功をモック
        booking = self._refundable_booking()
        refund_mock.return_value = {
            'id': 'rf_test_1',
            'charge': 'ch_test_1',
            'amount': 2000,
            'status': 'succeeded',
        }

        # Act: キャンセルAPIを呼び出し
        response = self.client.post(self._cancel_url(booking.id))

        # Assert: 返金 API が冪等キー付きで呼ばれることを確認
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        _, refund_kwargs = refund_mock.call_args
        self.assertEqual(refund_kwargs['payment_intent'], booking.payment_intent_id)
        self.assertEqual(
            refund_kwargs['idempotency_key'], f'booking_cancel_refund_{booking.id}'
        )

        # Assert: 予約がキャンセルされ、返金結果が記録されることを確認
        booking.refresh_from_db()
        self.assertEqual(booking.delivery_status, 'cancelled')
        self.assertEqual(booking.stripe_refund_id, 'rf_test_1')
        self.assertEqual(booking.refunded_amount, 2000)

    @patch('bookings.views.send_booking_cancellation_emails')
    @patch('bookings.owner_views.stripe.Refund.create')
    def test_cancel_is_aborted_when_refund_fails(self, refund_mock, email_mock):
        """返金に失敗した場合はキャンセル状態にしない（未返金の放置を防ぐ）"""
        # Arrange: 返金対象の予約と返金失敗をモック
        booking = self._refundable_booking()
        refund_mock.side_effect = stripe.error.StripeError('refund failed')

        # Act: キャンセルAPIを呼び出し
        response = self.client.post(self._cancel_url(booking.id))

        # Assert: 400 になり予約は集荷前のまま残ることを確認
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        booking.refresh_from_db()
        self.assertEqual(booking.delivery_status, 'before_pickup')

    @patch('bookings.views.send_booking_cancellation_emails')
    @patch('bookings.owner_views.stripe.Refund.create')
    def test_cancel_treats_already_refunded_as_success(
        self, refund_mock, email_mock
    ):
        """既に返金済みの Stripe エラーは成功扱いでキャンセルに進む"""
        # Arrange: 返金対象の予約と「返金済み」エラーをモック
        booking = self._refundable_booking()
        refund_mock.side_effect = stripe.error.InvalidRequestError(
            'Charge has already been refunded.', param=None,
            code='charge_already_refunded',
        )

        # Act: キャンセルAPIを呼び出し
        response = self.client.post(self._cancel_url(booking.id))

        # Assert: キャンセル状態になることを確認
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        booking.refresh_from_db()
        self.assertEqual(booking.delivery_status, 'cancelled')

    @patch('bookings.views.send_booking_cancellation_emails')
    @patch('bookings.owner_views.stripe.Refund.create')
    def test_cancel_after_refund_deadline_skips_refund(
        self, refund_mock, email_mock
    ):
        """返金期限（集荷日前日23時）を過ぎたキャンセルは返金せずキャンセルのみ行う"""
        # Arrange: 集荷日当日（返金期限超過）の予約を用意
        booking = self._refundable_booking(
            pickup_date=date.today(),
            delivery_date=date.today(),
        )
        self.assertFalse(booking.is_refundable_on_cancel())

        # Act: キャンセルAPIを呼び出し
        response = self.client.post(self._cancel_url(booking.id))

        # Assert: 返金 API を呼ばずにキャンセル状態になることを確認
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        refund_mock.assert_not_called()
        booking.refresh_from_db()
        self.assertEqual(booking.delivery_status, 'cancelled')

    @patch('bookings.owner_views.stripe.Refund.create')
    def test_cancel_rejects_picked_up_booking(self, refund_mock):
        """集荷済の予約はキャンセルできない"""
        # Arrange: 集荷済の予約を用意
        booking = self._refundable_booking(delivery_status='picked_up')

        # Act: キャンセルAPIを呼び出し
        response = self.client.post(self._cancel_url(booking.id))

        # Assert: 400 になり返金もステータス変更も行われないことを確認
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        refund_mock.assert_not_called()
        booking.refresh_from_db()
        self.assertEqual(booking.delivery_status, 'picked_up')

    def test_cancel_returns_404_for_missing_booking(self):
        """存在しない予約は見つからない扱いになる"""
        # Act: 存在しないIDでキャンセルAPIを呼び出し
        response = self.client.post(self._cancel_url(uuid4()))

        # Assert: 404 になることを確認
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
