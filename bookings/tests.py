from django.test import TestCase
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase
from rest_framework import status
from datetime import date, datetime, timedelta, timezone as dt_timezone
from types import SimpleNamespace
from unittest.mock import patch

from business_owners.models import BusinessProfile
from .models import LuggageBooking
from .transfers import create_transfer_for_delivered_booking

User = get_user_model()


class BaseBookingTest:
    """予約テストのベースクラス（共通のユーティリティメソッド）"""

    # 共通のデフォルト値をクラス変数として定義
    _BOOKING_DEFAULTS = {
        'pickup_location_name': '東京駅',
        'pickup_location_address': '東京都千代田区丸の内1-1-1',
        'delivery_location_name': '新宿駅',
        'delivery_location_address': '東京都新宿区新宿3-38-1',
        'customer_name': 'テスト 太郎',
        'customer_email': 'test@example.com',
        'customer_phone_number': '09012345678',
        'customer_nationality': 'JPN',
        'guest_name': 'Test Taro',
        'payment_intent_id': 'pi_test_1234567890',
        'notes': 'テスト予約です',
    }

    def _create_test_user(self, **kwargs):
        """テスト用ユーザーを作成するヘルパーメソッド"""
        defaults = {
            'email': 'test@example.com',
            'password': 'testpass123',
            'user_type': 'business_owner'
        }
        defaults.update(kwargs)
        return User.objects.create_user(**defaults)

    def _create_test_booking(self, **kwargs):
        """テスト用予約オブジェクトを作成するヘルパーメソッド"""
        defaults = self._BOOKING_DEFAULTS.copy()
        defaults['pickup_date'] = date.today() + timedelta(days=1)
        defaults['delivery_date'] = date.today() + timedelta(days=1)
        defaults['luggage_items'] = {'cabin': 1, 'checked': 0, 'oversize': 0}
        defaults['total_amount'] = 2000
        defaults.update(kwargs)
        return LuggageBooking.objects.create(**defaults)

    def _get_booking_data(self, **kwargs):
        """テスト用予約データを取得するヘルパーメソッド（API用）"""
        defaults = self._BOOKING_DEFAULTS.copy()
        pickup_date = date.today() + timedelta(days=1)
        delivery_date = date.today() + timedelta(days=1)
        defaults['pickup_date'] = pickup_date.isoformat()
        defaults['delivery_date'] = delivery_date.isoformat()
        # 各荷物の数量フィールド（views.pyでluggage_itemsとtotal_amountに変換される）
        defaults['cabin'] = 1
        defaults['checked'] = 0
        defaults['oversize'] = 0
        defaults.update(kwargs)
        return defaults


class LuggageBookingModelTest(BaseBookingTest, TestCase):
    """荷物配送予約モデルのテスト"""

    def setUp(self):
        # Arrange: テスト用ユーザーを作成
        self.user = self._create_test_user()

    def test_booking_creation(self):
        """予約作成のテスト"""
        # Act: 予約オブジェクトを作成
        booking = self._create_test_booking()

        # Assert: 予約が正しく作成されたことを検証
        self.assertTrue(booking.booking_number)
        self.assertTrue(booking.booking_number.startswith('LG'))
        self.assertEqual(booking.delivery_status, 'before_pickup')
        self.assertEqual(str(booking), f'{booking.booking_number} - 東京駅 → 新宿駅')

    def test_can_cancel(self):
        """キャンセル可能チェックのテスト"""
        # Arrange: テスト用予約を作成
        booking = self._create_test_booking()

        # Act & Assert: 初期状態（before_pickup）ではキャンセル可能であることを確認
        self.assertTrue(booking.can_cancel())

        # Arrange: 配達状況を配達済に変更
        booking.delivery_status = 'delivered'
        booking.save()

        # Act & Assert: 完了状態ではキャンセル不可であることを確認
        self.assertFalse(booking.can_cancel())


class LuggageBookingAPITest(BaseBookingTest, APITestCase):
    """荷物配送予約APIのテスト"""

    def setUp(self):
        # Arrange: テスト用ユーザーと事業者を作成
        self.user = self._create_test_user()
        self.profile = BusinessProfile.objects.create(
            user=self.user,
            company_name='テスト事業者',
            company_email='owner@example.com',
            stripe_account_id='acct_test',
        )

    @patch('bookings.views.stripe.PaymentIntent.retrieve')
    def test_create_booking(self, retrieve_mock):
        """予約作成APIのテスト（決済完了済みの PaymentIntent から予約を確定する）"""
        # Arrange: 決済完了済みの PaymentIntent をモック
        retrieve_mock.return_value = SimpleNamespace(
            status='succeeded',
            amount=2000,
            metadata={'business_owner_id': str(self.profile.id)},
        )
        url = reverse('bookings:booking-create')

        # Act: 予約作成APIを呼び出し
        response = self.client.post(url, self._get_booking_data(), format='json')

        # Assert: レスポンスの検証
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIn('message', response.data)
        self.assertIn('booking', response.data)
        self.assertEqual(LuggageBooking.objects.count(), 1)

        # Assert: 作成された予約データの検証
        booking = LuggageBooking.objects.first()
        self.assertEqual(booking.pickup_location_name, '東京駅')
        self.assertEqual(booking.business_owner, self.profile)
        self.assertEqual(booking.total_amount, 2000)

    def test_create_booking_invalid_date(self):
        """無効な日付での予約作成テスト"""
        # Arrange: APIエンドポイントと無効なデータを準備
        url = reverse('bookings:booking-create')
        invalid_data = self._get_booking_data(
            pickup_date=(date.today() - timedelta(days=1)).isoformat()  # 過去の日付
        )

        # Act: 無効なデータで予約作成APIを呼び出し
        response = self.client.post(url, invalid_data, format='json')

        # Assert: バリデーションエラーが返されることを確認
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class DeliveryTransferTest(BaseBookingTest, TestCase):
    """配達完了時の事業者への送金処理のテスト"""

    def setUp(self):
        # Arrange: テスト用ユーザーと事業者を作成
        self.user = self._create_test_user()
        self.profile = BusinessProfile.objects.create(
            user=self.user,
            company_name='テスト事業者',
            company_email='owner@example.com',
            stripe_account_id='acct_test',
        )

    def _delivered_booking(self, **kwargs):
        defaults = {
            'business_owner': self.profile,
            'delivery_status': 'delivered',
            'delivered_at': timezone.now(),
            'total_amount': 10_000,
        }
        defaults.update(kwargs)
        return self._create_test_booking(**defaults)

    @patch('bookings.transfers.stripe.Transfer.create')
    @patch('bookings.transfers.stripe.PaymentIntent.retrieve')
    def test_creates_transfer_and_records_availability(
        self, retrieve_mock, transfer_mock
    ):
        """配達完了予約の送金を作成し、入金可能日時を記録"""
        # Arrange: 決済・送金 API をモックし、配達完了済みの予約を作成
        available_on = 1782000000  # 2026-06-21 UTC
        retrieve_mock.return_value = {
            'latest_charge': {
                'id': 'ch_test_1',
                'balance_transaction': {'available_on': available_on},
            },
        }
        transfer_mock.return_value = {'id': 'tr_test_1'}
        booking = self._delivered_booking()

        # Act: 事業者への送金を実行
        result = create_transfer_for_delivered_booking(booking)

        # Assert: 送金作成と予約への記録を検証
        self.assertTrue(result)
        transfer_mock.assert_called_once()
        self.assertEqual(transfer_mock.call_args.kwargs['amount'], 9_000)
        self.assertEqual(transfer_mock.call_args.kwargs['destination'], 'acct_test')
        self.assertEqual(
            transfer_mock.call_args.kwargs['source_transaction'], 'ch_test_1'
        )
        booking.refresh_from_db()
        self.assertEqual(booking.stripe_transfer_id, 'tr_test_1')
        self.assertIsNotNone(booking.transferred_at)
        self.assertEqual(booking.transferred_gross_amount, 10_000)
        self.assertEqual(booking.transferred_platform_fee, 1_000)
        self.assertEqual(
            booking.funds_available_on,
            datetime.fromtimestamp(available_on, tz=dt_timezone.utc),
        )

    @patch('bookings.transfers.stripe.Transfer.create')
    @patch('bookings.transfers.stripe.PaymentIntent.retrieve')
    def test_skips_when_already_transferred(self, retrieve_mock, transfer_mock):
        """送金済みの予約には二重送金しない"""
        # Arrange: すでに送金済みの予約を作成
        booking = self._delivered_booking(stripe_transfer_id='tr_done')

        # Act: 送金処理を再実行
        result = create_transfer_for_delivered_booking(booking)

        # Assert: 成功扱いだが Stripe API は呼ばれないことを確認
        self.assertTrue(result)
        retrieve_mock.assert_not_called()
        transfer_mock.assert_not_called()

    def test_skips_when_not_delivered(self):
        """配達完了前の予約は送金しない"""
        # Arrange: 未配達の予約を作成
        booking = self._create_test_booking(
            business_owner=self.profile,
            total_amount=10_000,
        )

        # Act & Assert: 送金がスキップされることを確認
        self.assertFalse(create_transfer_for_delivered_booking(booking))