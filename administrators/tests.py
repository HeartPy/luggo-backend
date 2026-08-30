"""運営者用 Django Admin の権限制御のテスト"""
from datetime import timedelta
from django.utils import timezone

from django.test import TestCase
from django.urls import reverse

from bookings.models import LuggageBooking
from business_owners.models import BusinessProfile
from drivers.models import DriverProfile
from users.models import User


class AdminSiteTestCase(TestCase):
    """運営者としてログインした状態で Admin の挙動を検証する基底クラス"""

    def setUp(self) -> None:
        self.admin_user = User.objects.create_superuser(
            email='admin@example.com',
            password='admin-test-pass-123',
        )
        self.client.force_login(self.admin_user)

        self.owner_user = User.objects.create_user(
            email='owner@example.com',
            password='owner-test-pass-123',
            user_type='business_owner',
        )
        self.business_profile = BusinessProfile.objects.create(
            user=self.owner_user,
            company_name='テスト運送',
            company_email='owner@example.com',
            subdomain='sampleco',
        )


class BusinessProfileAdminTests(AdminSiteTestCase):
    """事業者 Admin の権限制御と有効/無効"""

    def test_deactivating_business_also_deactivates_user(self) -> None:
        # Arrange: 有効な事業者とユーザーを用意
        self.assertTrue(self.business_profile.is_active)
        self.assertTrue(self.owner_user.is_active)

        # Act: is_active を送らず保存（チェックボックスOFF = 無効）
        response = self.client.post(
            reverse(
                'admin:business_owners_businessprofile_change',
                args=[self.business_profile.pk],
            ),
            {'_save': '保存'},
        )

        # Assert: 事業者と紐づくユーザーの両方が停止されることを確認
        self.assertEqual(response.status_code, 302)
        self.business_profile.refresh_from_db()
        self.owner_user.refresh_from_db()
        self.assertFalse(self.business_profile.is_active)
        self.assertIsNotNone(self.business_profile.deactivated_at)
        self.assertFalse(self.owner_user.is_active)

    def test_reactivating_business_also_reactivates_user(self) -> None:
        # Arrange: 無効な事業者を用意
        self.business_profile.is_active = False
        self.business_profile.save()
        self.owner_user.refresh_from_db()
        self.assertFalse(self.owner_user.is_active)

        # Act: is_active をオンにして保存
        response = self.client.post(
            reverse(
                'admin:business_owners_businessprofile_change',
                args=[self.business_profile.pk],
            ),
            {'is_active': 'on', '_save': '保存'},
        )

        # Assert: 事業者と紐づくユーザーの両方が再開されることを確認
        self.assertEqual(response.status_code, 302)
        self.business_profile.refresh_from_db()
        self.owner_user.refresh_from_db()
        self.assertTrue(self.business_profile.is_active)
        self.assertIsNone(self.business_profile.deactivated_at)
        self.assertTrue(self.owner_user.is_active)

    def test_deactivating_business_also_deactivates_drivers(self) -> None:
        # Arrange: 紐づく配達者を用意
        driver_user = User.objects.create_user(
            email='driver@example.com',
            password='driver-test-pass-123',
            user_type='delivery_driver',
            first_name='太郎',
            last_name='配達',
        )
        driver = DriverProfile.objects.create(
            user=driver_user,
            business_owner=self.business_profile,
            license_expiry=timezone.localdate() + timedelta(days=365),
        )
        self.assertTrue(driver.is_active)

        # Act: 事業者を無効化する
        response = self.client.post(
            reverse(
                'admin:business_owners_businessprofile_change',
                args=[self.business_profile.pk],
            ),
            {'_save': '保存'},
        )

        # Assert: 所属配達者とそのユーザーも停止される
        self.assertEqual(response.status_code, 302)
        driver.refresh_from_db()
        driver_user.refresh_from_db()
        self.assertFalse(driver.is_active)
        self.assertIsNotNone(driver.deactivated_at)
        self.assertFalse(driver_user.is_active)

    def test_reactivating_business_does_not_reactivate_drivers(self) -> None:
        # Arrange: 事業者無効化で配達者も停止した状態
        driver_user = User.objects.create_user(
            email='driver-off@example.com',
            password='driver-test-pass-123',
            user_type='delivery_driver',
            first_name='花子',
            last_name='配達',
        )
        driver = DriverProfile.objects.create(
            user=driver_user,
            business_owner=self.business_profile,
            license_expiry=timezone.localdate() + timedelta(days=365),
        )
        self.business_profile.is_active = False
        self.business_profile.save()
        driver.refresh_from_db()
        self.assertFalse(driver.is_active)

        # Act: 事業者だけを再開する
        response = self.client.post(
            reverse(
                'admin:business_owners_businessprofile_change',
                args=[self.business_profile.pk],
            ),
            {'is_active': 'on', '_save': '保存'},
        )

        # Assert: 配達者は個別に再開するまで停止のまま
        self.assertEqual(response.status_code, 302)
        driver.refresh_from_db()
        driver_user.refresh_from_db()
        self.assertFalse(driver.is_active)
        self.assertFalse(driver_user.is_active)

    def test_add_and_delete_are_denied(self) -> None:
        # Act: 追加画面と削除画面にアクセス
        add_response = self.client.get(
            reverse('admin:business_owners_businessprofile_add')
        )
        delete_response = self.client.get(
            reverse(
                'admin:business_owners_businessprofile_delete',
                args=[self.business_profile.pk],
            )
        )

        # Assert: どちらも拒否されることを確認
        self.assertEqual(add_response.status_code, 403)
        self.assertEqual(delete_response.status_code, 403)


class LuggageBookingAdminTests(AdminSiteTestCase):
    """予約 Admin は閲覧専用（追加・削除不可、詳細は開ける）"""

    def setUp(self) -> None:
        super().setUp()
        self.booking = LuggageBooking.objects.create(
            business_owner=self.business_profile,
            pickup_location_name='ホテルA',
            pickup_location_address='東京都千代田区1-1',
            pickup_date=timezone.localdate() + timedelta(days=3),
            delivery_location_name='ホテルB',
            delivery_location_address='大阪府大阪市1-1',
            delivery_date=timezone.localdate() + timedelta(days=4),
            customer_name='山田太郎',
            customer_email='taro@example.com',
            customer_phone_number='+819012345678',
            customer_nationality='JPN',
            guest_name='山田太郎',
            payment_intent_id='pi_test_admin_1',
        )

    def test_add_is_denied(self) -> None:
        # Act: 予約の追加画面にアクセス
        response = self.client.get(reverse('admin:bookings_luggagebooking_add'))

        # Assert: 403 が返ることを確認
        self.assertEqual(response.status_code, 403)

    def test_delete_is_denied(self) -> None:
        # Act: 予約の削除画面にアクセス
        response = self.client.get(
            reverse('admin:bookings_luggagebooking_delete', args=[self.booking.pk])
        )

        # Assert: 403 が返ることを確認
        self.assertEqual(response.status_code, 403)

    def test_detail_view_is_accessible(self) -> None:
        # Act: 予約の詳細画面を開く
        response = self.client.get(
            reverse('admin:bookings_luggagebooking_change', args=[self.booking.pk])
        )

        # Assert: 詳細が閲覧できることを確認
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.booking.booking_number)

    def test_changelist_searchable_by_booking_number(self) -> None:
        # Act: 予約番号で一覧を検索
        response = self.client.get(
            reverse('admin:bookings_luggagebooking_changelist'),
            {'q': self.booking.booking_number},
        )

        # Assert: 該当予約が一覧に出ることを確認
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.booking.booking_number)


class UserAdminTests(AdminSiteTestCase):
    """User Admin は停止（is_active）のみ編集可"""

    def test_add_and_delete_are_denied(self) -> None:
        # Act: 追加画面と削除画面にアクセス
        add_response = self.client.get(reverse('admin:users_user_add'))
        delete_response = self.client.get(
            reverse('admin:users_user_delete', args=[self.owner_user.pk])
        )

        # Assert: どちらも拒否されることを確認
        self.assertEqual(add_response.status_code, 403)
        self.assertEqual(delete_response.status_code, 403)

    def test_can_deactivate_user(self) -> None:
        # Arrange: 有効なユーザーを用意
        self.assertTrue(self.owner_user.is_active)

        # Act: is_active を送らず保存（チェックボックスOFF = 停止）
        response = self.client.post(
            reverse('admin:users_user_change', args=[self.owner_user.pk]),
            {'_save': '保存'},
        )

        # Assert: アカウントが停止されることを確認
        self.assertEqual(response.status_code, 302)
        self.owner_user.refresh_from_db()
        self.assertFalse(self.owner_user.is_active)

    def test_email_is_not_editable(self) -> None:
        # Act: メールアドレスの書き換えを試して保存
        response = self.client.post(
            reverse('admin:users_user_change', args=[self.owner_user.pk]),
            {'is_active': 'on', 'email': 'hacked@example.com', '_save': '保存'},
        )

        # Assert: readonly のためメールは変わらないことを確認
        self.assertEqual(response.status_code, 302)
        self.owner_user.refresh_from_db()
        self.assertEqual(self.owner_user.email, 'owner@example.com')


class DriverProfileAdminTests(AdminSiteTestCase):
    """配達者 Admin は is_active / is_available のみ編集可"""

    def setUp(self) -> None:
        super().setUp()
        self.driver_user = User.objects.create_user(
            email='driver-admin@example.com',
            password='driver-test-pass-123',
            user_type='delivery_driver',
            first_name='太郎',
            last_name='配達',
        )
        self.driver = DriverProfile.objects.create(
            user=self.driver_user,
            business_owner=self.business_profile,
            license_expiry=timezone.localdate() + timedelta(days=365),
        )

    def test_can_deactivate_driver_without_changing_availability(self) -> None:
        # Arrange: 有効かつ自動割当候補の配達者
        self.assertTrue(self.driver.is_active)
        self.assertTrue(self.driver.is_available)

        # Act: is_active を送らず、is_available だけオンで保存
        response = self.client.post(
            reverse(
                'admin:drivers_driverprofile_change',
                args=[self.driver.pk],
            ),
            {'is_available': 'on', '_save': '保存'},
        )

        # Assert: アカウントは停止し、自動割当フラグは維持される
        self.assertEqual(response.status_code, 302)
        self.driver.refresh_from_db()
        self.driver_user.refresh_from_db()
        self.assertFalse(self.driver.is_active)
        self.assertIsNotNone(self.driver.deactivated_at)
        self.assertFalse(self.driver_user.is_active)
        self.assertTrue(self.driver.is_available)

    def test_can_reactivate_driver(self) -> None:
        # Arrange: 無効な配達者
        self.driver.is_active = False
        self.driver.save()
        self.driver_user.refresh_from_db()
        self.assertFalse(self.driver_user.is_active)

        # Act: is_active をオンにして保存
        response = self.client.post(
            reverse(
                'admin:drivers_driverprofile_change',
                args=[self.driver.pk],
            ),
            {'is_active': 'on', 'is_available': 'on', '_save': '保存'},
        )

        # Assert: 配達者とユーザーが再開される
        self.assertEqual(response.status_code, 302)
        self.driver.refresh_from_db()
        self.driver_user.refresh_from_db()
        self.assertTrue(self.driver.is_active)
        self.assertIsNone(self.driver.deactivated_at)
        self.assertTrue(self.driver_user.is_active)
