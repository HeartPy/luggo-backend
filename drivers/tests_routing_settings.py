from datetime import date, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APITestCase

from business_owners.models import BusinessProfile
from bookings.models import LuggageBooking
from drivers.models import DriverInvitation, DriverProfile
from drivers.utils import token_digest
from project.geocoding import (
    GeocodingNotFound,
    GeocodingResult,
    GeocodingServiceError,
)


User = get_user_model()


def make_owner(suffix='one'):
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


def make_driver(owner, suffix='one', latitude='35.0', longitude='139.0'):
    """テスト用のドライバーを作成。出発座標は行列・距離計算で使う。"""
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


class DriverRoutingSettingsTests(APITestCase):
    """ドライバー招待・更新時のルーティング設定まわりのテスト"""

    @patch('drivers.owner_views.geocode')
    @patch('drivers.owner_views.send_driver_invitation_email', return_value=True)
    @patch('drivers.owner_views.create_invitation_token')
    def test_invitation_preserves_routing_settings_until_acceptance(
        self,
        create_token,
        send_email,
        geocode_mock,
    ):
        # Arrange: 招待トークンとジオコーディング結果を用意
        owner = make_owner('driver-settings')
        raw_token = 'routing-settings-token'
        create_token.return_value = (raw_token, token_digest(raw_token))
        geocode_mock.return_value = GeocodingResult(
            latitude=35.681236,
            longitude=139.767125,
            place_id='place-123',
            address_components=[],
        )
        self.client.force_authenticate(owner.user)

        # Act: ルーティング設定付きでドライバー招待を作成
        response = self.client.post(
            '/api/business/drivers/manage',
            {
                'email': 'new-driver@example.com',
                'last_name': 'Route',
                'first_name': 'Driver',
                'departure_address': '東京都千代田区丸の内1-9-1',
                'shift_start': '08:30',
                'max_daily_stops': '12',
                'license_expiry': '2030-12-31',
                'operating_days': '1111100',
                'is_available': 'false',
            },
            format='multipart',
        )

        # Assert: 招待に設定が保持されることを確認
        self.assertEqual(response.status_code, 202)
        invitation = DriverInvitation.objects.get(
            email='new-driver@example.com'
        )
        self.assertEqual(invitation.max_daily_stops, 12)
        self.assertEqual(invitation.license_expiry.isoformat(), '2030-12-31')
        self.assertEqual(invitation.operating_days, '1111100')
        self.assertFalse(invitation.is_available)

        # Act: 招待を承諾
        self.client.force_authenticate(user=None)
        accepted = self.client.post(
            '/api/business/drivers/invitation/accept',
            {
                'token': raw_token,
                'password': 'StrongPass123!',
                'password_confirm': 'StrongPass123!',
            },
            format='json',
        )

        # Assert: 承諾後のプロフィールに設定が引き継がれることを確認
        self.assertEqual(accepted.status_code, 201)
        driver = DriverProfile.objects.get(user__email='new-driver@example.com')
        self.assertEqual(driver.departure_place_id, 'place-123')
        self.assertEqual(driver.shift_start.strftime('%H:%M'), '08:30')
        self.assertEqual(driver.max_daily_stops, 12)
        self.assertEqual(driver.license_expiry.isoformat(), '2030-12-31')
        self.assertEqual(driver.operating_days, '1111100')
        self.assertFalse(driver.is_available)
        self.assertTrue(driver.user.check_password('StrongPass123!'))
        geocode_mock.assert_called_once_with('東京都千代田区丸の内1-9-1')
        send_email.assert_called_once()

    @patch('drivers.owner_views.geocode')
    @patch('drivers.owner_views.send_driver_invitation_email', return_value=True)
    @patch('drivers.owner_views.create_invitation_token')
    def test_max_daily_luggage_count_optional_and_persisted(
        self,
        create_token,
        send_email,
        geocode_mock,
    ):
        # Arrange: 招待トークンとジオコーディング結果を用意
        owner = make_owner('luggage-cap')
        raw_token = 'luggage-cap-token'
        create_token.return_value = (raw_token, token_digest(raw_token))
        geocode_mock.return_value = GeocodingResult(
            latitude=35.681236,
            longitude=139.767125,
            place_id='place-456',
            address_components=[],
        )
        self.client.force_authenticate(owner.user)

        # Act: 荷物個数の上限を指定せずに招待を作成
        response = self.client.post(
            '/api/business/drivers/manage',
            {
                'email': 'no-luggage-cap@example.com',
                'last_name': 'No',
                'first_name': 'Cap',
                'departure_address': '東京都千代田区丸の内1-9-1',
                'shift_start': '08:30',
                'max_daily_stops': '12',
                'license_expiry': '2030-12-31',
                'operating_days': '1111100',
                'is_available': 'true',
            },
            format='multipart',
        )

        # Assert: 未指定の場合はデフォルト20として保存されることを確認
        self.assertEqual(response.status_code, 202)
        invitation = DriverInvitation.objects.get(
            email='no-luggage-cap@example.com'
        )
        self.assertEqual(invitation.max_daily_luggage_count, 20)

        # Act: 承諾してプロフィールへ引き継ぐ
        accepted = self.client.post(
            '/api/business/drivers/invitation/accept',
            {
                'token': raw_token,
                'password': 'StrongPass123!',
                'password_confirm': 'StrongPass123!',
            },
            format='json',
        )

        # Assert: 承諾後のプロフィールにデフォルト20が引き継がれることを確認
        self.assertEqual(accepted.status_code, 201)
        driver = DriverProfile.objects.get(
            user__email='no-luggage-cap@example.com'
        )
        self.assertEqual(driver.max_daily_luggage_count, 20)
        send_email.assert_called_once()

        # Act: 荷物個数を空欄で更新
        self.client.force_authenticate(owner.user)
        cleared = self.client.put(
            f'/api/business/drivers/manage/{driver.id}',
            {
                **self._update_payload(driver, driver.departure_address),
                'max_daily_luggage_count': '',
            },
            format='multipart',
        )

        # Assert: 空欄の場合は上限なし（None）になることを確認
        self.assertEqual(cleared.status_code, 200)
        driver.refresh_from_db()
        self.assertIsNone(driver.max_daily_luggage_count)

        # Act: 有効な値で更新
        updated = self.client.put(
            f'/api/business/drivers/manage/{driver.id}',
            {
                **self._update_payload(driver, driver.departure_address),
                'max_daily_luggage_count': '30',
            },
            format='multipart',
        )

        # Assert: 指定した荷物個数上限が保存されることを確認
        self.assertEqual(updated.status_code, 200)
        driver.refresh_from_db()
        self.assertEqual(driver.max_daily_luggage_count, 30)

        # Act: 不正な値（範囲外）で更新
        invalid = self.client.put(
            f'/api/business/drivers/manage/{driver.id}',
            {
                **self._update_payload(driver, driver.departure_address),
                'max_daily_luggage_count': '0',
            },
            format='multipart',
        )

        # Assert: バリデーションエラーになり、既存値が維持されることを確認
        self.assertEqual(invalid.status_code, 400)
        self.assertIn('max_daily_luggage_count', invalid.data['valid_errs'])
        driver.refresh_from_db()
        self.assertEqual(driver.max_daily_luggage_count, 30)

    @patch('drivers.owner_views.geocode')
    @patch('drivers.owner_views.send_driver_invitation_email', return_value=True)
    @patch('drivers.owner_views.create_invitation_token')
    def test_max_daily_stops_optional_and_persisted(
        self,
        create_token,
        send_email,
        geocode_mock,
    ):
        # Arrange: 招待トークンとジオコーディング結果を用意
        owner = make_owner('stops-cap')
        raw_token = 'stops-cap-token'
        create_token.return_value = (raw_token, token_digest(raw_token))
        geocode_mock.return_value = GeocodingResult(
            latitude=35.681236,
            longitude=139.767125,
            place_id='place-789',
            address_components=[],
        )
        self.client.force_authenticate(owner.user)

        # Act: 訪問数の上限を指定せずに招待を作成
        response = self.client.post(
            '/api/business/drivers/manage',
            {
                'email': 'no-stops-cap@example.com',
                'last_name': 'No',
                'first_name': 'Stops',
                'departure_address': '東京都千代田区丸の内1-9-1',
                'shift_start': '08:30',
                'license_expiry': '2030-12-31',
                'operating_days': '1111100',
                'is_available': 'true',
            },
            format='multipart',
        )

        # Assert: 未指定の場合はデフォルト10として保存されることを確認
        self.assertEqual(response.status_code, 202)
        invitation = DriverInvitation.objects.get(
            email='no-stops-cap@example.com'
        )
        self.assertEqual(invitation.max_daily_stops, 10)

        # Act: 承諾してプロフィールへ引き継ぐ
        accepted = self.client.post(
            '/api/business/drivers/invitation/accept',
            {
                'token': raw_token,
                'password': 'StrongPass123!',
                'password_confirm': 'StrongPass123!',
            },
            format='json',
        )

        # Assert: 承諾後のプロフィールにデフォルト10が引き継がれることを確認
        self.assertEqual(accepted.status_code, 201)
        driver = DriverProfile.objects.get(
            user__email='no-stops-cap@example.com'
        )
        self.assertEqual(driver.max_daily_stops, 10)
        send_email.assert_called_once()

        # Act: 訪問数を空欄で更新
        self.client.force_authenticate(owner.user)
        cleared = self.client.put(
            f'/api/business/drivers/manage/{driver.id}',
            {
                **self._update_payload(driver, driver.departure_address),
                'max_daily_stops': '',
            },
            format='multipart',
        )

        # Assert: 空欄の場合は上限なし（None）になることを確認
        self.assertEqual(cleared.status_code, 200)
        driver.refresh_from_db()
        self.assertIsNone(driver.max_daily_stops)

        # Act: 有効な値で更新
        updated = self.client.put(
            f'/api/business/drivers/manage/{driver.id}',
            {
                **self._update_payload(driver, driver.departure_address),
                'max_daily_stops': '12',
            },
            format='multipart',
        )

        # Assert: 指定した訪問数上限が保存されることを確認
        self.assertEqual(updated.status_code, 200)
        driver.refresh_from_db()
        self.assertEqual(driver.max_daily_stops, 12)

        # Act: 不正な値（範囲外）で更新
        invalid = self.client.put(
            f'/api/business/drivers/manage/{driver.id}',
            {
                **self._update_payload(driver, driver.departure_address),
                'max_daily_stops': '0',
            },
            format='multipart',
        )

        # Assert: バリデーションエラーになり、既存値が維持されることを確認
        self.assertEqual(invalid.status_code, 400)
        self.assertIn('max_daily_stops', invalid.data['valid_errs'])
        driver.refresh_from_db()
        self.assertEqual(driver.max_daily_stops, 12)

    def test_create_requires_license_expiry(self):
        # Arrange: 免許期限なしの作成ペイロードを用意
        owner = make_owner('license-required')
        self.client.force_authenticate(owner.user)

        # Act: ドライバー作成 API を呼び出す
        response = self.client.post(
            '/api/business/drivers/manage',
            {
                'email': 'missing-license@example.com',
                'last_name': 'No',
                'first_name': 'License',
                'departure_address': '東京都千代田区丸の内1-9-1',
                'shift_start': '09:00',
                'max_daily_stops': '20',
                'operating_days': '1111111',
                'is_available': 'true',
            },
            format='multipart',
        )

        # Assert: license_expiry が必須エラーになることを確認
        self.assertEqual(response.status_code, 400)
        self.assertIn('license_expiry', response.data['valid_errs'])

    def test_acceptance_rejects_weak_password(self):
        # Arrange: 有効な招待トークンを用意
        owner = make_owner('weak-password')
        raw_token = 'weak-password-token'
        DriverInvitation.objects.create(
            business_owner=owner,
            email='weak-password@example.com',
            last_name='Password',
            first_name='Driver',
            license_expiry=date.today() + timedelta(days=365),
            token_digest=token_digest(raw_token),
            expires_at=timezone.now() + timedelta(days=7),
        )

        # Act: 強度不足のパスワードで承認
        response = self.client.post(
            '/api/business/drivers/invitation/accept',
            {
                'token': raw_token,
                'password': 'weak',
                'password_confirm': 'weak',
            },
            format='json',
        )

        # Assert: パスワード強度エラーになり、ユーザーが作成されないことを確認
        self.assertEqual(response.status_code, 400)
        self.assertIn('password', response.data['valid_errs'])
        self.assertNotIn('password_confirm', response.data['valid_errs'])
        self.assertFalse(
            User.objects.filter(email='weak-password@example.com').exists()
        )

    def test_acceptance_rejects_mismatched_password_confirm(self):
        # Arrange: 有効な招待トークンを用意
        owner = make_owner('mismatch-password')
        raw_token = 'mismatch-password-token'
        DriverInvitation.objects.create(
            business_owner=owner,
            email='mismatch-password@example.com',
            last_name='Password',
            first_name='Driver',
            license_expiry=date.today() + timedelta(days=365),
            token_digest=token_digest(raw_token),
            expires_at=timezone.now() + timedelta(days=7),
        )

        # Act: 確認用パスワードが一致しない状態で承認
        response = self.client.post(
            '/api/business/drivers/invitation/accept',
            {
                'token': raw_token,
                'password': 'StrongPass123!',
                'password_confirm': 'DifferentPass123!',
            },
            format='json',
        )

        # Assert: 確認不一致エラーになり、ユーザーが作成されないことを確認
        self.assertEqual(response.status_code, 400)
        self.assertIn('password_confirm', response.data['valid_errs'])
        self.assertNotIn('password', response.data['valid_errs'])
        self.assertFalse(
            User.objects.filter(email='mismatch-password@example.com').exists()
        )

    def _update_payload(self, driver, address):
        """ドライバー更新 API 用の multipart ペイロードを組み立てる"""
        return {
            'email': driver.user.email,
            'last_name': 'Updated',
            'first_name': 'Driver',
            'departure_address': address,
            'shift_start': '09:00',
            'max_daily_stops': '20',
            'license_expiry': (
                driver.license_expiry.isoformat()
                if driver.license_expiry
                else '2030-12-31'
            ),
            'operating_days': '1111111',
            'is_available': 'true',
        }

    @patch('drivers.owner_views.geocode')
    def test_update_geocodes_changed_address_and_skips_unchanged(self, geocode_mock):
        # Arrange: 既存住所付きドライバーと新住所のジオコーディング結果を用意
        owner = make_owner('driver-update')
        driver = make_driver(owner, 'driver-update')
        driver.departure_address = '旧住所'
        driver.departure_place_id = 'old-place'
        driver.save(update_fields=['departure_address', 'departure_place_id'])
        geocode_mock.return_value = GeocodingResult(
            latitude=34.702485,
            longitude=135.495951,
            place_id='new-place',
            address_components=[],
        )
        self.client.force_authenticate(owner.user)

        # Act: 住所を変更して更新
        changed = self.client.put(
            f'/api/business/drivers/manage/{driver.id}',
            self._update_payload(driver, '大阪府大阪市北区梅田3-1-1'),
            format='multipart',
        )

        # Assert: 変更時のみジオコーディングされることを確認
        self.assertEqual(changed.status_code, 200)
        driver.refresh_from_db()
        self.assertEqual(driver.departure_place_id, 'new-place')
        self.assertEqual(float(driver.departure_latitude), 34.702485)
        geocode_mock.assert_called_once()

        # Act: 同じ住所で再更新
        geocode_mock.reset_mock()
        unchanged = self.client.put(
            f'/api/business/drivers/manage/{driver.id}',
            self._update_payload(driver, driver.departure_address),
            format='multipart',
        )

        # Assert: 住所未変更時はジオコーディングしないことを確認
        self.assertEqual(unchanged.status_code, 200)
        geocode_mock.assert_not_called()

    @patch('drivers.owner_views.geocode')
    def test_update_clears_coordinates_or_rejects_unknown_address(self, geocode_mock):
        # Arrange: 出発地付きドライバーを用意
        owner = make_owner('driver-clear')
        driver = make_driver(owner, 'driver-clear')
        driver.departure_address = '東京都千代田区丸の内1-9-1'
        driver.departure_place_id = 'place'
        driver.save(update_fields=['departure_address', 'departure_place_id'])
        self.client.force_authenticate(owner.user)

        # Act: 住所を空にして更新
        cleared = self.client.put(
            f'/api/business/drivers/manage/{driver.id}',
            self._update_payload(driver, ''),
            format='multipart',
        )

        # Assert: 座標がクリアされることを確認
        self.assertEqual(cleared.status_code, 200)
        driver.refresh_from_db()
        self.assertEqual(driver.departure_address, '')
        self.assertIsNone(driver.departure_latitude)
        self.assertIsNone(driver.departure_longitude)
        geocode_mock.assert_not_called()

        # Act: 特定できない住所で更新
        geocode_mock.side_effect = GeocodingNotFound
        rejected = self.client.put(
            f'/api/business/drivers/manage/{driver.id}',
            self._update_payload(driver, '特定できない住所'),
            format='multipart',
        )

        # Assert: 住所エラーになり既存値が維持されることを確認
        self.assertEqual(rejected.status_code, 400)
        self.assertIn('departure_address', rejected.data['valid_errs'])
        driver.refresh_from_db()
        self.assertEqual(driver.departure_address, '')

        # Act: ジオコーディングサービス障害時に更新
        geocode_mock.side_effect = GeocodingServiceError
        unavailable = self.client.put(
            f'/api/business/drivers/manage/{driver.id}',
            self._update_payload(driver, '東京都千代田区'),
            format='multipart',
        )

        # Assert: サービス障害も住所エラーになることを確認
        self.assertEqual(unavailable.status_code, 400)
        self.assertIn('departure_address', unavailable.data['valid_errs'])


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
