from datetime import date, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from business_owners.models import BusinessProfile
from project.geocoding import (
    GeocodingNotFound,
    GeocodingResult,
    GeocodingServiceError,
)

from bookings.models import LuggageBooking
from bookings.tasks import geocode_booking_postal_coordinates
from bookings.views import materialize_booking


User = get_user_model()


def make_owner(suffix='one'):
    """テスト用の事業者プロフィールを作成"""
    user = User.objects.create_user(
        email=f'owner-postal-{suffix}@example.com',
        password='StrongPass123!',
        user_type='business_owner',
    )
    return BusinessProfile.objects.create(
        user=user,
        company_name=f'Owner {suffix}',
        company_email=user.email,
        subdomain=f'postal{suffix}'[:12],
    )


def make_booking(owner, suffix='one', **overrides):
    """座標なし・郵便番号ありの手入力予約を作成。overrides で上書きできる。"""
    service_date = date.today() + timedelta(days=2)
    values = {
        'business_owner': owner,
        'pickup_location_name': 'Pickup',
        'pickup_location_address': '東京都千代田区丸の内',
        'pickup_postal_code': '1000005',
        'pickup_latitude': None,
        'pickup_longitude': None,
        'pickup_geocode_status': 'pending',
        'pickup_date': service_date,
        'delivery_location_name': 'Delivery',
        'delivery_location_address': '神奈川県横浜市西区',
        'delivery_postal_code': '2200011',
        'delivery_latitude': None,
        'delivery_longitude': None,
        'delivery_geocode_status': 'pending',
        'delivery_date': service_date,
        'customer_name': 'Test Customer',
        'customer_email': 'customer@example.com',
        'customer_phone_number': '09012345678',
        'customer_nationality': 'JPN',
        'guest_name': 'Guest',
        'payment_intent_id': f'pi-postal-{suffix}',
    }
    values.update(overrides)
    return LuggageBooking.objects.create(**values)


def geocoding_result(latitude=35.68, longitude=139.76):
    return GeocodingResult(
        latitude=latitude,
        longitude=longitude,
        place_id='postal-place',
        address_components=[],
    )


class PostalGeocodeTaskTests(TestCase):
    """郵便番号からの概算座標補完タスクのテスト"""

    @patch('bookings.tasks.geocode')
    def test_fills_missing_coordinates_as_approximate(self, geocode_mock):
        # Arrange: 座標未設定・郵便番号ありの予約と成功応答を用意
        owner = make_owner('fill')
        booking = make_booking(owner, 'fill')
        geocode_mock.return_value = geocoding_result()

        # Act: 郵便番号からの概算座標補完を実行
        geocode_booking_postal_coordinates(str(booking.id))

        # Assert: 両側が approximate で埋まり、place_id は保存されないことを確認
        booking.refresh_from_db()
        self.assertEqual(float(booking.pickup_latitude), 35.68)
        self.assertEqual(float(booking.pickup_longitude), 139.76)
        self.assertEqual(booking.pickup_geocode_status, 'approximate')
        self.assertEqual(float(booking.delivery_latitude), 35.68)
        self.assertEqual(float(booking.delivery_longitude), 139.76)
        self.assertEqual(booking.delivery_geocode_status, 'approximate')
        # 概算なので place_id は保存しない
        self.assertEqual(booking.pickup_place_id, '')
        self.assertEqual(booking.delivery_place_id, '')
        # 郵便番号はハイフン付きの日本住所形式で問い合わせる
        geocode_mock.assert_any_call('〒100-0005 日本')
        geocode_mock.assert_any_call('〒220-0011 日本')

    @patch('bookings.tasks.geocode')
    def test_skips_sides_with_existing_coordinates(self, geocode_mock):
        # Arrange: 集荷側は検証済み座標あり、配達側のみ未設定の予約を用意
        owner = make_owner('skip')
        booking = make_booking(
            owner,
            'skip',
            pickup_latitude='35.1',
            pickup_longitude='139.1',
            pickup_geocode_status='verified',
        )
        geocode_mock.return_value = geocoding_result()

        # Act: 郵便番号からの概算座標補完を実行
        geocode_booking_postal_coordinates(str(booking.id))

        # Assert: 集荷側は上書きせず、配達側のみ補完されることを確認
        booking.refresh_from_db()
        self.assertEqual(float(booking.pickup_latitude), 35.1)
        self.assertEqual(booking.pickup_geocode_status, 'verified')
        self.assertEqual(booking.delivery_geocode_status, 'approximate')
        geocode_mock.assert_called_once_with('〒220-0011 日本')

    @patch('bookings.tasks.geocode')
    def test_skips_sides_without_postal_code(self, geocode_mock):
        # Arrange: 郵便番号なしの予約を用意
        owner = make_owner('nopostal')
        booking = make_booking(
            owner, 'nopostal', pickup_postal_code='', delivery_postal_code='',
        )

        # Act: 郵便番号からの概算座標補完を実行
        geocode_booking_postal_coordinates(str(booking.id))

        # Assert: 座標もステータスも変わらず、API も呼ばれないことを確認
        booking.refresh_from_db()
        self.assertIsNone(booking.pickup_latitude)
        self.assertEqual(booking.pickup_geocode_status, 'pending')
        geocode_mock.assert_not_called()

    @patch('bookings.tasks.geocode')
    def test_not_found_marks_failed_and_keeps_coordinates_empty(self, geocode_mock):
        # Arrange: ジオコーディングが見つからない応答を用意
        owner = make_owner('notfound')
        booking = make_booking(owner, 'notfound')
        geocode_mock.side_effect = GeocodingNotFound

        # Act: 郵便番号からの概算座標補完を実行
        geocode_booking_postal_coordinates(str(booking.id))

        # Assert: 座標は空のまま failed になることを確認
        booking.refresh_from_db()
        self.assertIsNone(booking.pickup_latitude)
        self.assertEqual(booking.pickup_geocode_status, 'failed')
        self.assertEqual(booking.delivery_geocode_status, 'failed')

    @patch('bookings.tasks.geocode')
    def test_service_error_keeps_pending_for_retry(self, geocode_mock):
        # Arrange: ジオコーディングサービスエラーを用意
        owner = make_owner('svcerr')
        booking = make_booking(owner, 'svcerr')
        geocode_mock.side_effect = GeocodingServiceError

        # Act: 郵便番号からの概算座標補完を実行
        geocode_booking_postal_coordinates(str(booking.id))

        # Assert: 再試行できるよう pending のまま残ることを確認
        booking.refresh_from_db()
        self.assertIsNone(booking.pickup_latitude)
        self.assertEqual(booking.pickup_geocode_status, 'pending')
        self.assertEqual(booking.delivery_geocode_status, 'pending')


class MaterializeBookingGeocodeScheduleTests(TestCase):
    """予約作成時の概算座標補完ジョブ投入のテスト"""

    def _booking_fields(self, **overrides):
        service_date = date.today() + timedelta(days=2)
        fields = {
            'pickup_location_name': 'Pickup',
            'pickup_location_address': '東京都千代田区丸の内',
            'pickup_postal_code': '1000005',
            'pickup_latitude': None,
            'pickup_longitude': None,
            'pickup_geocode_status': 'pending',
            'pickup_date': service_date,
            'delivery_location_name': 'Delivery',
            'delivery_location_address': '神奈川県横浜市西区',
            'delivery_postal_code': '2200011',
            'delivery_latitude': None,
            'delivery_longitude': None,
            'delivery_geocode_status': 'pending',
            'delivery_date': service_date,
            'customer_name': 'Test Customer',
            'customer_email': 'customer@example.com',
            'customer_phone_number': '09012345678',
            'customer_nationality': 'JPN',
            'guest_name': 'Guest',
        }
        fields.update(overrides)
        return fields

    @patch('bookings.views.geocode_booking_postal_coordinates.delay')
    def test_enqueues_task_when_coordinates_missing(self, delay_mock):
        # Arrange: 座標未設定・郵便番号ありの予約作成データを用意
        owner = make_owner('enqueue')

        # Act: 予約を作成し、on_commit コールバックを実行
        with self.captureOnCommitCallbacks(execute=True):
            booking, created = materialize_booking(
                payment_intent_id='pi-postal-enqueue',
                business_profile=owner,
                total_amount=3000,
                booking_fields=self._booking_fields(),
                luggage_counts={'cabin': 1, 'checked': 0, 'oversize': 0},
            )

        # Assert: 概算座標補完ジョブが投入されることを確認
        self.assertTrue(created)
        delay_mock.assert_called_once_with(str(booking.id))

    @patch('bookings.views.geocode_booking_postal_coordinates.delay')
    def test_does_not_enqueue_when_coordinates_present(self, delay_mock):
        # Arrange: 両側とも検証済み座標付きの予約作成データを用意
        owner = make_owner('noqueue')

        # Act: 予約を作成し、on_commit コールバックを実行
        with self.captureOnCommitCallbacks(execute=True):
            _, created = materialize_booking(
                payment_intent_id='pi-postal-noqueue',
                business_profile=owner,
                total_amount=3000,
                booking_fields=self._booking_fields(
                    pickup_latitude='35.1',
                    pickup_longitude='139.1',
                    pickup_geocode_status='verified',
                    delivery_latitude='35.2',
                    delivery_longitude='139.2',
                    delivery_geocode_status='verified',
                ),
                luggage_counts={'cabin': 1, 'checked': 0, 'oversize': 0},
            )

        # Assert: 座標があるためジョブは投入されないことを確認
        self.assertTrue(created)
        delay_mock.assert_not_called()

    @patch('bookings.views.geocode_booking_postal_coordinates.delay')
    def test_does_not_enqueue_when_postal_code_missing(self, delay_mock):
        # Arrange: 郵便番号なしの予約作成データを用意
        owner = make_owner('nopost')

        # Act: 予約を作成し、on_commit コールバックを実行
        with self.captureOnCommitCallbacks(execute=True):
            _, created = materialize_booking(
                payment_intent_id='pi-postal-nopost',
                business_profile=owner,
                total_amount=3000,
                booking_fields=self._booking_fields(
                    pickup_postal_code='',
                    delivery_postal_code='',
                ),
                luggage_counts={'cabin': 1, 'checked': 0, 'oversize': 0},
            )

        # Assert: 郵便番号がないためジョブは投入されないことを確認
        self.assertTrue(created)
        delay_mock.assert_not_called()
