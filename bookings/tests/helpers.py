"""予約テストの共通ヘルパー"""
from datetime import date, timedelta

from django.contrib.auth import get_user_model

from bookings.models import LuggageBooking

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
        # 23時以降は翌日分の予約受付が締め切られるため、時刻に左右されない日付にする
        pickup_date = date.today() + timedelta(days=2)
        delivery_date = date.today() + timedelta(days=2)
        defaults['pickup_date'] = pickup_date.isoformat()
        defaults['delivery_date'] = delivery_date.isoformat()
        # 各荷物の数量フィールド（views.pyでluggage_itemsとtotal_amountに変換される）
        defaults['cabin'] = 1
        defaults['checked'] = 0
        defaults['oversize'] = 0
        defaults.update(kwargs)
        return defaults
