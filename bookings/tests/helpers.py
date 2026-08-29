"""予約テストの共通ヘルパー"""
from datetime import date, timedelta
from uuid import uuid4

from django.contrib.auth import get_user_model

from bookings.models import LuggageBooking
from business_owners.models import BusinessProfile

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

    def _ensure_business_owner(self):
        """予約作成用の事業者プロフィールを用意"""
        suffix = uuid4().hex[:8]
        # subdomain は半角小文字英字のみ（3〜12文字）。
        # uuid の hex（0-9a-f）をそのまま使うと数字が混ざるので、英字だけに変換。
        letters = ''.join(chr(ord('a') + int(c, 16) % 26) for c in suffix)[:8]
        subdomain = f't{letters}'[:12]
        user = self._create_test_user(email=f'owner-{suffix}@example.com')
        return BusinessProfile.objects.create(
            user=user,
            company_name='テスト事業者',
            company_email=user.email,
            subdomain=subdomain,
        )

    def _create_test_booking(self, **kwargs):
        """テスト用予約オブジェクトを作成するヘルパーメソッド"""
        defaults = self._BOOKING_DEFAULTS.copy()
        defaults['pickup_date'] = date.today() + timedelta(days=1)
        defaults['delivery_date'] = date.today() + timedelta(days=1)
        defaults['luggage_items'] = {'cabin': 1, 'checked': 0, 'oversize': 0}
        defaults['total_amount'] = 2000
        defaults.update(kwargs)
        if defaults.get('business_owner') is None:
            defaults['business_owner'] = self._ensure_business_owner()
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
