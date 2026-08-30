from datetime import timedelta
from django.utils import timezone
from io import StringIO
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings

from bookings.models import LuggageBooking
from business_owners.models import BusinessProfile


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


def make_booking(owner, suffix='one', **overrides):
    """検証済み座標付きの当日ペア予約を作成。overrides で個別フィールドを上書きできる。"""
    service_date = timezone.localdate() + timedelta(days=2)
    values = {
        'business_owner': owner,
        'pickup_location_name': 'Pickup',
        'pickup_location_address': 'Tokyo',
        'pickup_latitude': '35.1',
        'pickup_longitude': '139.1',
        'pickup_geocode_status': 'verified',
        'pickup_date': service_date,
        'delivery_location_name': 'Delivery',
        'delivery_location_address': 'Yokohama',
        'delivery_latitude': '35.2',
        'delivery_longitude': '139.2',
        'delivery_geocode_status': 'verified',
        'delivery_date': service_date,
        'customer_name': 'Test Customer',
        'customer_email': 'customer@example.com',
        'customer_phone_number': '09012345678',
        'customer_nationality': 'JPN',
        'guest_name': 'Guest',
        'payment_intent_id': f'pi-backfill-{suffix}',
    }
    values.update(overrides)
    return LuggageBooking.objects.create(**values)


class GeocodeBackfillTests(TestCase):
    """住所ジオコードのバックフィルコマンドのテスト"""

    @override_settings(GOOGLE_GEOCODING_API_KEY='test-key')
    @patch('project.geocoding.requests.get')
    def test_dry_run_makes_no_requests_and_apply_reuses_address(self, get):
        # Arrange: 座標未設定の予約を用意
        owner = make_owner('backfill')
        booking = make_booking(
            owner,
            'backfill',
            pickup_location_address='東京都千代田区',
            delivery_location_address='東京都千代田区',
            pickup_latitude=None,
            pickup_longitude=None,
            delivery_latitude=None,
            delivery_longitude=None,
            pickup_geocode_status='pending',
            delivery_geocode_status='pending',
        )
        output = StringIO()

        # Act: dry-run でバックフィルする
        call_command('backfill_geocodes', limit=2, stdout=output)

        # Assert: API リクエストが発行されないことを確認
        get.assert_not_called()

        # Arrange: ジオコーディング成功応答を用意
        response = Mock()
        response.json.return_value = {
            'status': 'OK',
            'results': [{
                'place_id': 'backfilled-place',
                'geometry': {
                    'location': {'lat': 35.68, 'lng': 139.76},
                },
                'address_components': [],
            }],
        }
        get.return_value = response

        # Act: apply でバックフィルする
        call_command(
            'backfill_geocodes',
            apply=True,
            limit=2,
            delay=0,
            stdout=output,
        )

        # Assert: 同一住所は1回の API 呼び出しで両方更新されることを確認
        booking.refresh_from_db()
        self.assertEqual(booking.pickup_place_id, 'backfilled-place')
        self.assertEqual(booking.delivery_place_id, 'backfilled-place')
        self.assertEqual(booking.pickup_geocode_status, 'verified')
        self.assertEqual(booking.delivery_geocode_status, 'verified')
        self.assertEqual(get.call_count, 1)
