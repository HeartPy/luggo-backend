from django.test import TestCase
from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework.test import APITestCase
from rest_framework import status
from datetime import date, timedelta
from .models import LuggageBooking

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
        defaults['luggage_items'] = {'baby_stroller_count': 1, 'cardboard_count': 0, 'suitcase_count': 0}
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
        defaults['baby_stroller_count'] = 1
        defaults['cardboard_count'] = 0
        defaults['suitcase_count'] = 0
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
        # Arrange: テスト用ユーザーを作成
        self.user = self._create_test_user()

    def test_create_booking(self):
        """予約作成APIのテスト"""
        # Arrange: APIエンドポイントURL準備
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