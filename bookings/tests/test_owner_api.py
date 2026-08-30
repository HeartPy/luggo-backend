"""
事業者の予約管理API（一覧・ステータス一括更新・配達者割当・個別更新）のテスト

自社スコープの認可（他社の予約・配達者に触れないこと）を各APIで確認する。
配達完了時の送金が statuses 経路で呼ばれることもここで確認する。
"""
from datetime import timedelta
from django.utils import timezone
from unittest.mock import patch

from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from business_owners.models import BusinessProfile
from drivers.models import DriverProfile
from users.models import User

from .helpers import BaseBookingTest


class OwnerAPIBaseTest(BaseBookingTest, APITestCase):
    """自社・他社の事業者と予約を用意する共通セットアップ"""

    def setUp(self):
        self.owner_user = self._create_test_user(email='owner-api@example.com')
        self.profile = BusinessProfile.objects.create(
            user=self.owner_user,
            company_name='自社テスト事業者',
            company_email='owner-api@example.com',
            subdomain='ownerapione',
            stripe_account_id='acct_test',
        )
        self.other_owner_user = self._create_test_user(
            email='other-owner-api@example.com'
        )
        self.other_profile = BusinessProfile.objects.create(
            user=self.other_owner_user,
            company_name='他社テスト事業者',
            company_email='other-owner-api@example.com',
            subdomain='ownerapitwo',
        )
        self.booking = self._create_test_booking(business_owner=self.profile)
        self.other_booking = self._create_test_booking(
            business_owner=self.other_profile,
            payment_intent_id='pi_test_other',
        )

    def _login(self):
        self.client.force_authenticate(self.owner_user)

    def _month_params(self):
        """予約日（今日+1）が必ず範囲に入る年月パラメータ"""
        today = timezone.localdate()
        return {
            'month_from': today.strftime('%Y-%m'),
            'month_to': (today + timedelta(days=40)).strftime('%Y-%m'),
        }

    def _create_driver(self, email: str, business_profile) -> DriverProfile:
        user = User.objects.create_user(
            email=email,
            password='DriverPass123!',
            user_type='delivery_driver',
            last_name='配達',
            first_name='太郎',
        )
        return DriverProfile.objects.create(
            user=user,
            business_owner=business_profile,
            license_expiry=timezone.localdate() + timedelta(days=365),
        )


class OwnerBookingListAPITest(OwnerAPIBaseTest):
    """予約一覧API（自社スコープ）のテスト"""

    def test_list_returns_only_own_bookings(self):
        # Arrange: 自社としてログインする
        self._login()

        # Act: 予約一覧APIを呼び出す
        response = self.client.get(
            reverse('business_bookings_list'), self._month_params()
        )

        # Assert: 自社の予約だけが返り、他社の予約は含まれない
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        ids = {item['id'] for item in response.data['results']}
        self.assertIn(str(self.booking.id), ids)
        self.assertNotIn(str(self.other_booking.id), ids)
        self.assertEqual(response.data['total_count'], 1)

    def test_list_requires_authentication(self):
        # Act: 未認証で予約一覧APIを呼び出す
        response = self.client.get(reverse('business_bookings_list'))

        # Assert: 認証エラーになる
        self.assertIn(response.status_code, (401, 403))

    def test_list_rejects_user_without_business_profile(self):
        # Arrange: 事業者プロフィールを持たない配達者としてログインする
        driver = self._create_driver('list-driver@example.com', self.profile)
        self.client.force_authenticate(driver.user)

        # Act: 予約一覧APIを呼び出す
        response = self.client.get(reverse('business_bookings_list'))

        # Assert: 事業者情報が見つからない扱いになる
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class OwnerBookingStatusUpdateAPITest(OwnerAPIBaseTest):
    """配達状況の一括更新APIのテスト"""

    @patch('bookings.owner_views.create_transfer_for_delivered_booking')
    def test_update_statuses_marks_delivered_and_triggers_transfer(
        self, transfer_mock
    ):
        # Arrange: 自社としてログインする
        self._login()

        # Act: 予約を配達済に一括更新する
        response = self.client.put(
            reverse('business_bookings_update_statuses'),
            {'updates': {str(self.booking.id): 'delivered'}},
            format='json',
        )

        # Assert: 更新され、配達完了日時が記録される
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['updated_count'], 1)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.delivery_status, 'delivered')
        self.assertIsNotNone(self.booking.delivered_at)

        # Assert: 事業者への送金処理が呼ばれる
        transfer_mock.assert_called_once_with(self.booking)

    @patch('bookings.owner_views.create_transfer_for_delivered_booking')
    def test_update_statuses_skips_cancelled_booking(self, transfer_mock):
        # Arrange: キャンセル済みの予約を用意する
        self.booking.delivery_status = 'cancelled'
        self.booking.save(update_fields=['delivery_status'])
        self._login()

        # Act: キャンセル済みの予約を配達済へ変更しようとする
        response = self.client.put(
            reverse('business_bookings_update_statuses'),
            {'updates': {str(self.booking.id): 'delivered'}},
            format='json',
        )

        # Assert: 変更対象外になり、送金も呼ばれない
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['updated_count'], 0)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.delivery_status, 'cancelled')
        transfer_mock.assert_not_called()

    def test_update_statuses_ignores_other_companys_booking(self):
        # Arrange: 自社としてログインする
        self._login()

        # Act: 他社の予約IDを指定して一括更新する
        response = self.client.put(
            reverse('business_bookings_update_statuses'),
            {'updates': {str(self.other_booking.id): 'delivered'}},
            format='json',
        )

        # Assert: 他社の予約は無視され、変更されない
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['updated_count'], 0)
        self.other_booking.refresh_from_db()
        self.assertEqual(self.other_booking.delivery_status, 'before_pickup')

    def test_update_statuses_rejects_invalid_status(self):
        # Arrange: 自社としてログインする
        self._login()

        # Act: 存在しない配達状況を指定する
        response = self.client.put(
            reverse('business_bookings_update_statuses'),
            {'updates': {str(self.booking.id): 'flying'}},
            format='json',
        )

        # Assert: 400 になり変更されない
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.delivery_status, 'before_pickup')

    def test_update_statuses_requires_authentication(self):
        # Act: 未認証で一括更新APIを呼び出す
        response = self.client.put(
            reverse('business_bookings_update_statuses'),
            {'updates': {str(self.booking.id): 'delivered'}},
            format='json',
        )

        # Assert: 認証エラーになる
        self.assertIn(response.status_code, (401, 403))


class OwnerBookingDriverAssignAPITest(OwnerAPIBaseTest):
    """配達者の一括割り当てAPI（自社の配達者のみ）のテスト"""

    def setUp(self):
        super().setUp()
        self.driver = self._create_driver('assign-driver@example.com', self.profile)
        self.other_driver = self._create_driver(
            'other-assign-driver@example.com', self.other_profile
        )

    def test_assign_own_driver(self):
        # Arrange: 自社としてログインする
        self._login()

        # Act: 自社の配達者を予約へ割り当てる
        response = self.client.put(
            reverse('business_bookings_assign_drivers'),
            {'updates': {str(self.booking.id): str(self.driver.id)}},
            format='json',
        )

        # Assert: 通常割当（集荷・配達とも同一配達者）になる
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['updated_count'], 1)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.driver, self.driver)
        self.assertIsNone(self.booking.pickup_driver)

    def test_assign_rejects_other_companys_driver(self):
        # Arrange: 自社としてログインする
        self._login()

        # Act: 他社の配達者を割り当てようとする
        response = self.client.put(
            reverse('business_bookings_assign_drivers'),
            {'updates': {str(self.booking.id): str(self.other_driver.id)}},
            format='json',
        )

        # Assert: 400 になり割り当ては変更されない
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.booking.refresh_from_db()
        self.assertIsNone(self.booking.driver)


class OwnerBookingUpdateAPITest(OwnerAPIBaseTest):
    """予約の個別更新API（詳細ポップアップの「保存」）のテスト"""

    def test_update_own_booking_status(self):
        # Arrange: 自社としてログインする
        self._login()

        # Act: 自社の予約を集荷済へ更新する
        response = self.client.patch(
            reverse('business_bookings_update', args=[self.booking.id]),
            {'delivery_status': 'picked_up'},
            format='json',
        )

        # Assert: 更新されることを確認
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.delivery_status, 'picked_up')

    @patch('bookings.owner_views.create_transfer_for_delivered_booking')
    def test_update_own_booking_to_delivered_triggers_transfer(
        self, transfer_mock
    ):
        # Arrange: 自社としてログインする
        self._login()

        # Act: 自社の予約を配達済へ更新する
        response = self.client.patch(
            reverse('business_bookings_update', args=[self.booking.id]),
            {'delivery_status': 'delivered'},
            format='json',
        )

        # Assert: 更新され、送金処理が呼ばれる
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.delivery_status, 'delivered')
        transfer_mock.assert_called_once_with(self.booking)

    def test_update_rejects_other_companys_booking(self):
        # Arrange: 自社としてログインする
        self._login()

        # Act: 他社の予約を更新しようとする
        response = self.client.patch(
            reverse('business_bookings_update', args=[self.other_booking.id]),
            {'delivery_status': 'picked_up'},
            format='json',
        )

        # Assert: 見つからない扱いになり変更されない
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.other_booking.refresh_from_db()
        self.assertEqual(self.other_booking.delivery_status, 'before_pickup')
