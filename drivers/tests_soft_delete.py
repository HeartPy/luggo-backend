"""配達者の利用停止・再招待まわりのテスト"""
from datetime import date, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from bookings.models import LuggageBooking
from business_owners.models import BusinessProfile
from drivers.models import DriverProfile
from drivers.utils import token_digest
from project.geocoding import GeocodingResult


User = get_user_model()


def make_owner(suffix='soft-del'):
    """テスト用の事業者プロフィールを作成"""
    user = User.objects.create_user(
        email=f'owner-{suffix}@example.com',
        password='StrongPass123!',
        user_type='business_owner',
    )
    return BusinessProfile.objects.create(
        user=user,
        company_name=f'Owner {suffix}',
        company_email=user.email,
        subdomain=f'owner{suffix}'[:12],
    )


def make_driver(owner, suffix='soft-del', latitude='35.0', longitude='139.0'):
    """テスト用の配達者を作成"""
    user = User.objects.create_user(
        email=f'driver-{suffix}@example.com',
        password='StrongPass123!',
        user_type='delivery_driver',
    )
    return DriverProfile.objects.create(
        user=user,
        business_owner=owner,
        departure_latitude=latitude,
        departure_longitude=longitude,
        max_daily_stops=10,
        license_expiry=date.today() + timedelta(days=365),
    )


class DriverSoftDeleteTests(APITestCase):
    """事業者の配達者削除は利用停止（is_active=False）になる"""

    def setUp(self):
        self.owner = make_owner('soft-del')
        self.driver = make_driver(self.owner, 'soft-del')
        self.client.force_authenticate(self.owner.user)

    def test_delete_deactivates_driver_and_unassigns_active_bookings(self):
        # Arrange: 未完了の担当予約を用意
        today = date.today()
        booking = LuggageBooking.objects.create(
            business_owner=self.owner,
            driver=self.driver,
            delivery_manually_assigned=True,
            pickup_location_name='Pickup',
            pickup_location_address='Tokyo',
            pickup_date=today + timedelta(days=1),
            delivery_location_name='Delivery',
            delivery_location_address='Osaka',
            delivery_date=today + timedelta(days=2),
            customer_name='顧客',
            customer_email='guest@example.com',
            customer_phone_number='09012345678',
            customer_nationality='JPN',
            guest_name='宿泊者',
            payment_intent_id='pi_soft_delete_1',
        )

        # Act: 事業者が配達者を削除する
        response = self.client.delete(
            f'/api/business/drivers/manage/{self.driver.id}'
        )

        # Assert: レコードは残り、利用停止になり担当は外れる
        self.assertEqual(response.status_code, 200)
        self.driver.refresh_from_db()
        self.driver.user.refresh_from_db()
        booking.refresh_from_db()
        self.assertFalse(self.driver.is_active)
        self.assertFalse(self.driver.user.is_active)
        self.assertIsNone(booking.driver_id)
        self.assertFalse(booking.delivery_manually_assigned)

    def test_deactivated_driver_is_hidden_from_owner_list(self):
        # Arrange: 配達者を利用停止にする
        self.driver.is_active = False
        self.driver.save()

        # Act: 事業者の配達者一覧を取得
        response = self.client.get('/api/business/drivers/manage')

        # Assert: 利用停止の配達者は一覧に出ない
        self.assertEqual(response.status_code, 200)
        ids = [row['id'] for row in response.data['results']]
        self.assertNotIn(str(self.driver.id), ids)

    @patch('drivers.owner_views.geocode')
    @patch('drivers.owner_views.send_driver_invitation_email', return_value=True)
    @patch('drivers.owner_views.create_invitation_token')
    def test_reinvite_reactivates_deactivated_driver(
        self,
        create_token,
        _send_email,
        geocode_mock,
    ):
        # Arrange: 削除（利用停止）済みの配達者を再招待する
        self.driver.is_active = False
        self.driver.save()
        raw_token = 'reactivate-token'
        create_token.return_value = (raw_token, token_digest(raw_token))
        geocode_mock.return_value = GeocodingResult(
            latitude=35.681236,
            longitude=139.767125,
            place_id='place-reactivate',
            address_components=[],
        )

        invite = self.client.post(
            '/api/business/drivers/manage',
            {
                'last_name': '再開',
                'first_name': '太郎',
                'email': self.driver.user.email,
                'departure_address': '東京都千代田区丸の内1-1-1',
                'license_expiry': (date.today() + timedelta(days=365)).isoformat(),
                'is_available': 'true',
            },
            format='multipart',
        )
        self.assertEqual(invite.status_code, 202)

        # Act: 招待を承諾する
        accept = self.client.post(
            '/api/business/drivers/invitation/accept',
            {
                'token': raw_token,
                'password': 'StrongPass123!',
                'password_confirm': 'StrongPass123!',
            },
            format='json',
        )

        # Assert: 同じ配達者が再開される
        self.assertEqual(accept.status_code, 201)
        self.driver.refresh_from_db()
        self.driver.user.refresh_from_db()
        self.assertTrue(self.driver.is_active)
        self.assertTrue(self.driver.user.is_active)
        self.assertEqual(self.driver.user.last_name, '再開')
        self.assertEqual(DriverProfile.objects.filter(user=self.driver.user).count(), 1)
