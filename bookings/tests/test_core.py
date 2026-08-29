from django.test import TestCase, override_settings
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase
from rest_framework import status
from datetime import date, datetime, timedelta, timezone as dt_timezone
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import stripe

from business_owners.models import BusinessProfile
from drivers.models import DriverProfile
from bookings.models import LuggageBooking, PendingBooking
from bookings.serializers import (
    LuggageBookingCreateSerializer,
    LuggageBookingSerializer,
    OwnerBookingUpdateSerializer,
    _max_booking_date,
    _min_pickup_date,
)
from bookings.transfers import create_transfer_for_delivered_booking

from .helpers import BaseBookingTest

User = get_user_model()


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
        self.assertEqual(
            str(booking), f'集荷前 / {booking.booking_number} / 東京駅 → 新宿駅'
        )

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

    @patch('bookings.views.stripe.PaymentIntent.retrieve')
    def test_create_booking_rejects_unpaid_payment_intent(self, retrieve_mock):
        """決済が完了していない PaymentIntent では予約を作成しない"""
        # Arrange: 未決済状態の PaymentIntent をモック
        retrieve_mock.return_value = SimpleNamespace(
            status='requires_payment_method',
            amount=2000,
            metadata={'business_owner_id': str(self.profile.id)},
        )
        url = reverse('bookings:booking-create')

        # Act: 予約作成APIを呼び出し
        response = self.client.post(url, self._get_booking_data(), format='json')

        # Assert: 400 になり予約は作成されないことを確認
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data.get('payment_status'), 'requires_payment_method')
        self.assertEqual(LuggageBooking.objects.count(), 0)

    @patch('bookings.views.stripe.PaymentIntent.retrieve')
    def test_create_booking_rejects_stripe_retrieve_failure(self, retrieve_mock):
        """PaymentIntent の取得に失敗した場合は予約を作成しない"""
        # Arrange: Stripe API エラーをモック
        retrieve_mock.side_effect = stripe.error.StripeError('retrieve failed')
        url = reverse('bookings:booking-create')

        # Act: 予約作成APIを呼び出し
        response = self.client.post(url, self._get_booking_data(), format='json')

        # Assert: 400 になり予約は作成されないことを確認
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(LuggageBooking.objects.count(), 0)

    @patch('bookings.views.stripe.PaymentIntent.retrieve')
    def test_create_booking_rejects_nonpositive_amount(self, retrieve_mock):
        """PaymentIntent の決済金額が 0 円以下の場合は予約を作成しない"""
        # Arrange: 金額 0 の PaymentIntent をモック
        retrieve_mock.return_value = SimpleNamespace(
            status='succeeded',
            amount=0,
            metadata={'business_owner_id': str(self.profile.id)},
        )
        url = reverse('bookings:booking-create')

        # Act: 予約作成APIを呼び出し
        response = self.client.post(url, self._get_booking_data(), format='json')

        # Assert: 400 になり予約は作成されないことを確認
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(LuggageBooking.objects.count(), 0)

    @patch('bookings.views.stripe.PaymentIntent.retrieve')
    def test_create_booking_is_idempotent_for_same_payment_intent(
        self, retrieve_mock
    ):
        """同じ payment_intent_id での再送は既存予約を返す（二重予約しない）"""
        # Arrange: 既に同じ payment_intent_id の予約が存在する状態を用意
        existing = self._create_test_booking(business_owner=self.profile)
        retrieve_mock.return_value = SimpleNamespace(
            status='succeeded',
            amount=2000,
            metadata={'business_owner_id': str(self.profile.id)},
        )
        url = reverse('bookings:booking-create')

        # Act: 同じ payment_intent_id で予約作成APIを呼び出し
        response = self.client.post(url, self._get_booking_data(), format='json')

        # Assert: 200 で既存予約が返り、新規予約は作成されないことを確認
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['booking']['id'], str(existing.id))
        self.assertEqual(LuggageBooking.objects.count(), 1)


class BookingDeadlineBoundaryTest(TestCase):
    """予約可能期間（前日23時JST締切・半年先まで）の境界のテスト"""

    @patch('bookings.serializers.datetime')
    def test_min_pickup_date_is_tomorrow_before_2300(self, datetime_mock):
        """JST 23時前は翌日から集荷可能"""
        # Arrange: JST 22:59 の時刻をモック
        datetime_mock.now.return_value = datetime(
            2026, 8, 28, 22, 59, tzinfo=ZoneInfo('Asia/Tokyo')
        )

        # Act & Assert: 最短集荷日は翌日になる
        self.assertEqual(_min_pickup_date(), date(2026, 8, 29))

    @patch('bookings.serializers.datetime')
    def test_min_pickup_date_shifts_after_2300(self, datetime_mock):
        """JST 23時以降は翌日分が締め切られ、翌々日から集荷可能"""
        # Arrange: JST 23:00 の時刻をモック
        datetime_mock.now.return_value = datetime(
            2026, 8, 28, 23, 0, tzinfo=ZoneInfo('Asia/Tokyo')
        )

        # Act & Assert: 最短集荷日は翌々日になる
        self.assertEqual(_min_pickup_date(), date(2026, 8, 30))

    @patch('bookings.serializers.datetime')
    def test_max_booking_date_is_180_days_ahead(self, datetime_mock):
        """予約できるのは 180 日先まで"""
        # Arrange: 固定時刻をモック
        now = datetime(2026, 8, 28, 12, 0, tzinfo=ZoneInfo('Asia/Tokyo'))
        datetime_mock.now.return_value = now

        # Act & Assert: 最長予約日は 180 日後になる
        self.assertEqual(_max_booking_date(), (now + timedelta(days=180)).date())

    def test_serializer_rejects_pickup_before_min_date(self):
        """最短集荷日より前の日付はシリアライザで弾かれる"""
        # Arrange: 今日（必ず締切済み）の集荷日を指定
        serializer = LuggageBookingSerializer()

        # Act & Assert: バリデーションエラーになる
        with self.assertRaises(Exception):
            serializer.validate_pickup_date(date.today())


@override_settings(E2E_STRIPE_MOCK=False)
class CreatePaymentIntentAPITest(BaseBookingTest, APITestCase):
    """PaymentIntent 作成API（決済開始）のテスト（E2E モックは無効化して実分岐を通す）"""

    def setUp(self):
        # Arrange: 東京エリア対応の事業者を作成
        self.user = self._create_test_user(email='owner-pi@example.com')
        self.profile = BusinessProfile.objects.create(
            user=self.user,
            company_name='PIテスト事業者',
            company_email='owner-pi@example.com',
            stripe_account_id='acct_test',
            service_areas=['13'],
            pricing_rules={'13': {'cabin': 1000, 'checked': 2000}},
            daily_max_luggage=-1,
        )
        self.url = reverse('bookings:create-payment-intent')

    def _payload(self, **kwargs):
        pickup_date = date.today() + timedelta(days=2)
        data = {
            'total_amount': 1000,
            'cabin': 1,
            'checked': 0,
            'oversize': 0,
            'business_owner_id': str(self.profile.id),
            'pickup_postal_code': '1000001',
            'delivery_postal_code': '1600001',
            'pickup_date': pickup_date.isoformat(),
            'delivery_date': pickup_date.isoformat(),
            'customer_name': 'テスト 太郎',
            'customer_email': 'test@example.com',
        }
        data.update(kwargs)
        return data

    @patch('bookings.views.stripe.PaymentIntent.create')
    @patch('bookings.views.stripe.Account.retrieve')
    def test_create_payment_intent_success(self, account_mock, pi_create_mock):
        """審査完了済みの事業者に対して PaymentIntent を発行できる"""
        # Arrange: 審査完了済みの連結アカウントと PaymentIntent 作成をモック
        account_mock.return_value = SimpleNamespace(
            charges_enabled=True,
            requirements={'currently_due': []},
        )
        pi_create_mock.return_value = SimpleNamespace(
            id='pi_test_new',
            client_secret='pi_test_new_secret',
        )

        # Act: PaymentIntent 作成APIを呼び出し
        response = self.client.post(self.url, self._payload(), format='json')

        # Assert: client_secret と金額が返ることを確認
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['client_secret'], 'pi_test_new_secret')
        self.assertEqual(response.data['amount'], 1000)

        # Assert: サーバー計算金額が amount に、business_owner_id が metadata に設定されることを確認
        _, called_kwargs = pi_create_mock.call_args
        self.assertEqual(called_kwargs['amount'], 1000)
        self.assertEqual(
            called_kwargs['metadata']['business_owner_id'], str(self.profile.id)
        )

        # Assert: Webhook フォールバック用の PendingBooking が保存されることを確認
        pending = PendingBooking.objects.get(payment_intent_id='pi_test_new')
        self.assertEqual(pending.business_owner, self.profile)
        self.assertEqual(pending.total_amount, 1000)

    @patch('bookings.views.stripe.PaymentIntent.create')
    @patch('bookings.views.stripe.Account.retrieve')
    def test_create_payment_intent_requires_total_amount(
        self, account_mock, pi_create_mock
    ):
        """total_amount 欠落時は PaymentIntent を作成しない"""
        # Arrange: total_amount を除いたデータを用意
        payload = self._payload()
        del payload['total_amount']

        # Act: PaymentIntent 作成APIを呼び出し
        response = self.client.post(self.url, payload, format='json')

        # Assert: 400 になり Stripe を呼ばないことを確認
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        pi_create_mock.assert_not_called()

    @patch('bookings.views.stripe.PaymentIntent.create')
    @patch('bookings.views.stripe.Account.retrieve')
    def test_create_payment_intent_rejects_amount_mismatch(
        self, account_mock, pi_create_mock
    ):
        """フロント金額とサーバー再計算金額が不一致なら決済を開始しない"""
        # Arrange: 審査完了済みの連結アカウントをモックし、改ざんされた金額を用意
        account_mock.return_value = SimpleNamespace(
            charges_enabled=True,
            requirements={'currently_due': []},
        )

        # Act: サーバー計算（1000円）と異なる金額で呼び出し
        response = self.client.post(
            self.url, self._payload(total_amount=1), format='json'
        )

        # Assert: 400 でサーバー計算金額が返り、Stripe を呼ばないことを確認
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data['server_total_amount'], 1000)
        pi_create_mock.assert_not_called()

    @patch('bookings.views.stripe.PaymentIntent.create')
    @patch('bookings.views.stripe.Account.retrieve')
    def test_create_payment_intent_rejects_unready_account(
        self, account_mock, pi_create_mock
    ):
        """連結アカウントの審査が未完了の事業者では決済を開始しない"""
        # Arrange: 審査未完了（charges 無効）の連結アカウントをモック
        account_mock.return_value = SimpleNamespace(
            charges_enabled=False,
            requirements={'currently_due': ['individual.verification.document']},
        )

        # Act: PaymentIntent 作成APIを呼び出し
        response = self.client.post(self.url, self._payload(), format='json')

        # Assert: 400 になり Stripe の PaymentIntent を作成しないことを確認
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        pi_create_mock.assert_not_called()


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

    @patch('bookings.transfers.stripe.Transfer.create')
    @patch('bookings.transfers.stripe.PaymentIntent.retrieve')
    def test_skips_when_stripe_account_missing(self, retrieve_mock, transfer_mock):
        """連結アカウント未設定の事業者には送金しない"""
        # Arrange: 連結アカウントを持たない事業者の配達完了予約を作成
        self.profile.stripe_account_id = ''
        self.profile.save(update_fields=['stripe_account_id'])
        booking = self._delivered_booking()

        # Act: 送金処理を実行
        result = create_transfer_for_delivered_booking(booking)

        # Assert: スキップされ Stripe API は呼ばれないことを確認
        self.assertFalse(result)
        retrieve_mock.assert_not_called()
        transfer_mock.assert_not_called()

    @patch('bookings.transfers.stripe.Transfer.create')
    @patch('bookings.transfers.stripe.PaymentIntent.retrieve')
    def test_skips_when_amount_not_positive(self, retrieve_mock, transfer_mock):
        """全額返金済みなどで送金対象額が 0 円以下の場合は送金しない"""
        # Arrange: 全額返金済みの配達完了予約を作成
        retrieve_mock.return_value = {
            'latest_charge': {
                'id': 'ch_test_refunded',
                'balance_transaction': {'available_on': 1782000000},
            },
        }
        booking = self._delivered_booking(refunded_amount=10_000)

        # Act: 送金処理を実行
        result = create_transfer_for_delivered_booking(booking)

        # Assert: スキップされ Transfer は作成されないことを確認
        self.assertFalse(result)
        transfer_mock.assert_not_called()

    @patch('bookings.transfers.stripe.Transfer.create')
    @patch('bookings.transfers.stripe.PaymentIntent.retrieve')
    def test_returns_false_when_transfer_fails(self, retrieve_mock, transfer_mock):
        """Stripe の送金失敗時は False を返すだけで例外にしない（次回再試行できる）"""
        # Arrange: 送金 API の失敗をモック
        retrieve_mock.return_value = {
            'latest_charge': {
                'id': 'ch_test_fail',
                'balance_transaction': {'available_on': 1782000000},
            },
        }
        transfer_mock.side_effect = stripe.error.StripeError('transfer failed')
        booking = self._delivered_booking()

        # Act: 送金処理を実行
        result = create_transfer_for_delivered_booking(booking)

        # Assert: False が返り、送金済み記録は付かないことを確認
        self.assertFalse(result)
        booking.refresh_from_db()
        self.assertFalse(booking.stripe_transfer_id)


class BookingDriverAssignmentTest(BaseBookingTest, TestCase):
    """集荷・配達担当の通常割り当てと分業割り当て"""

    def setUp(self):
        # Arrange: 事業者・集荷/配達ドライバー・予約を用意
        owner_user = self._create_test_user(email='owner-assignment@example.com')
        self.profile = BusinessProfile.objects.create(
            user=owner_user,
            company_name='担当テスト事業者',
            company_email='owner-assignment@example.com',
        )
        self.delivery_driver = self._create_driver(
            'delivery@example.com', '配達', '太郎'
        )
        self.pickup_driver = self._create_driver(
            'pickup@example.com', '集荷', '花子'
        )
        self.booking = self._create_test_booking(business_owner=self.profile)

    def _create_driver(
        self, email: str, last_name: str, first_name: str
    ) -> DriverProfile:
        user = User.objects.create_user(
            email=email,
            password='testpass123',
            user_type='delivery_driver',
            last_name=last_name,
            first_name=first_name,
        )
        return DriverProfile.objects.create(
            user=user,
            business_owner=self.profile,
            license_expiry=date.today() + timedelta(days=365),
        )

    def _serializer(self, data):
        return OwnerBookingUpdateSerializer(
            self.booking,
            data=data,
            partial=True,
            context={'business_profile': self.profile},
        )

    def test_split_assignment_uses_separate_pickup_driver(self):
        # Arrange: 集荷と配達で別ドライバーを指定する更新データを用意
        serializer = self._serializer({
            'driver': str(self.delivery_driver.id),
            'pickup_driver': str(self.pickup_driver.id),
        })

        # Act: バリデーションして保存
        self.assertTrue(serializer.is_valid(), serializer.errors)
        booking = serializer.save()

        # Assert: 分業割当として集荷・配達が分かれることを確認
        self.assertEqual(booking.driver, self.delivery_driver)
        self.assertEqual(booking.effective_pickup_driver, self.pickup_driver)
        self.assertTrue(booking.is_split_assignment)

    def test_normal_assignment_clears_previous_split_assignment(self):
        # Arrange: 既存の分業割当を通常割当に戻すデータを用意
        self.booking.driver = self.delivery_driver
        self.booking.pickup_driver = self.pickup_driver
        self.booking.save()
        serializer = self._serializer({'driver': str(self.pickup_driver.id)})

        # Act: バリデーションして保存
        self.assertTrue(serializer.is_valid(), serializer.errors)
        booking = serializer.save()

        # Assert: pickup_driver が消え、通常割当になることを確認
        self.assertEqual(booking.driver, self.pickup_driver)
        self.assertIsNone(booking.pickup_driver)
        self.assertEqual(booking.effective_pickup_driver, self.pickup_driver)


class OwnerBookingDetailAPITest(BaseBookingTest, APITestCase):
    """事業者向け予約単体取得API（詳細ポップアップ表示用）"""

    def setUp(self):
        # Arrange: 事業者と、他事業者（アクセス不可の確認用）を用意
        owner_user = self._create_test_user(email='owner-detail@example.com')
        self.profile = BusinessProfile.objects.create(
            user=owner_user,
            company_name='詳細テスト事業者',
            company_email='owner-detail@example.com',
            subdomain='detailone',
        )
        other_owner_user = self._create_test_user(email='other-owner-detail@example.com')
        self.other_profile = BusinessProfile.objects.create(
            user=other_owner_user,
            company_name='他事業者',
            company_email='other-owner-detail@example.com',
            subdomain='detailtwo',
        )
        self.booking = self._create_test_booking(business_owner=self.profile)

    def test_get_returns_own_booking(self):
        # Arrange: 自社の予約を認証済みで取得
        self.client.force_authenticate(self.profile.user)

        # Act: 予約単体取得APIを呼び出す
        response = self.client.get(f'/api/business/bookings/{self.booking.id}')

        # Assert: 予約詳細が返ることを確認
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['booking']['id'], str(self.booking.id))
        self.assertEqual(
            response.data['booking']['booking_number'], self.booking.booking_number
        )

    def test_get_rejects_other_owners_booking(self):
        # Arrange: 他事業者として認証
        self.client.force_authenticate(self.other_profile.user)

        # Act: 自社ではない予約の取得を試みる
        response = self.client.get(f'/api/business/bookings/{self.booking.id}')

        # Assert: 見つからない扱いになることを確認
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_get_returns_404_for_missing_booking(self):
        # Arrange: 存在しないIDを用意
        self.client.force_authenticate(self.profile.user)
        missing_id = '00000000-0000-0000-0000-000000000000'

        # Act: 存在しない予約の取得を試みる
        response = self.client.get(f'/api/business/bookings/{missing_id}')

        # Assert: 見つからない扱いになることを確認
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_get_requires_authentication(self):
        # Act: 未認証で予約単体取得APIを呼び出す
        response = self.client.get(f'/api/business/bookings/{self.booking.id}')

        # Assert: 認証エラーになることを確認
        self.assertIn(response.status_code, (401, 403))


class LuggageBookingCreatePhoneValidationTest(BaseBookingTest, TestCase):
    """予約作成時の言語別電話番号バリデーション"""

    def _create_serializer(self, **overrides):
        data = self._get_booking_data(**overrides)
        return LuggageBookingCreateSerializer(data=data)

    def test_ja_accepts_domestic_phone(self):
        # Arrange: 日本語ページ向けにハイフン付き国内電話番号を用意
        serializer = self._create_serializer(
            customer_language='ja',
            customer_phone_number='090-1234-5678',
        )

        # Act: バリデーションを実行
        is_valid = serializer.is_valid()

        # Assert: 通ること、およびハイフン除去後の値が入ることを確認
        self.assertTrue(is_valid, serializer.errors)
        self.assertEqual(serializer.validated_data['customer_phone_number'], '09012345678')

    def test_non_ja_rejects_domestic_phone(self):
        # Arrange: 英語ページ向けに国内形式の電話番号を用意
        serializer = self._create_serializer(
            customer_language='en',
            customer_phone_number='09012345678',
        )

        # Act: バリデーションを実行
        is_valid = serializer.is_valid()

        # Assert: 電話番号エラーになることを確認
        self.assertFalse(is_valid)
        self.assertIn('customer_phone_number', serializer.errors)

    def test_non_ja_accepts_international_phone(self):
        # Arrange: 英語ページ向けに国際形式の電話番号を用意
        serializer = self._create_serializer(
            customer_language='en',
            customer_phone_number='+819012345678',
        )

        # Act: バリデーションを実行
        is_valid = serializer.is_valid()

        # Assert: 国際形式は通ることを確認
        self.assertTrue(is_valid, serializer.errors)
