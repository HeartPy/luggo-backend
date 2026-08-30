from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APITestCase

from bookings.models import LuggageBooking
from business_owners.models import BusinessProfile
from drivers.models import DriverProfile
from drivers.owner_views import (
    _apply_booking_sales_to_stats,
    _booking_sales_amount,
    _driver_job_count,
    _driver_sales_share,
    _monthly_stats,
)


User = get_user_model()


def make_owner(suffix='sales'):
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
        subdomain=f'own{suffix}'[:12],
    )


def make_driver(owner, suffix='one'):
    """テスト用の配達ドライバーを作成"""
    user = User.objects.create_user(
        email=f'driver-{suffix}@example.com',
        password='StrongPass123!',
        user_type='delivery_driver',
        last_name='Driver',
        first_name=suffix,
    )
    return DriverProfile.objects.create(
        user=user,
        business_owner=owner,
        license_expiry=timezone.localdate() + timedelta(days=365),
    )


def make_delivered_booking(owner, *, driver, pickup_driver=None, amount=10000, **overrides):
    """
    売上集計対象の配達完了予約を作成

    pickup_driver 未指定時は通常割当（集荷=配達）。分業時のみ明示する。
    サービス日は昨日にし、配達完了済み予約として日付が矛盾しないようにする。
    月次統計の当月判定は delivered_at（現在時刻）で行う。
    """
    service_date = timezone.localdate() - timedelta(days=1)
    values = {
        'business_owner': owner,
        'pickup_location_name': 'Pickup',
        'pickup_location_address': 'Tokyo',
        'pickup_date': service_date,
        'delivery_location_name': 'Delivery',
        'delivery_location_address': 'Yokohama',
        'delivery_date': service_date,
        'customer_name': 'Test Customer',
        'customer_email': 'customer@example.com',
        'customer_phone_number': '09012345678',
        'customer_nationality': 'JPN',
        'guest_name': 'Guest',
        'payment_intent_id': f'pi-sales-{driver.id}-{amount}',
        'total_amount': amount,
        'driver': driver,
        'pickup_driver': pickup_driver,
        # 売上・件数は配達完了（delivered + delivered_at）のみ集計対象
        'delivery_status': 'delivered',
        'delivered_at': timezone.now(),
    }
    values.update(overrides)
    return LuggageBooking.objects.create(**values)


class DriverSalesShareTests(TestCase):
    """ドライバー売上按分ロジック（ヘルパー関数）のテスト"""

    def test_same_driver_gets_full_amount(self):
        # Arrange: 同一ドライバーが集荷・配達を担当する配達完了予約を用意
        owner = make_owner('same')
        driver = make_driver(owner, 'same')
        booking = make_delivered_booking(owner, driver=driver, amount=10001)

        # Act & Assert: 売上全額が当該ドライバーに帰属することを確認
        self.assertEqual(_booking_sales_amount(booking), 10001)
        self.assertEqual(_driver_sales_share(booking, driver.id), 10001)

    def test_split_assignment_halves_sales_with_remainder_to_delivery(self):
        # Arrange: 集荷と配達で別ドライバーの配達完了予約を用意
        owner = make_owner('split')
        pickup = make_driver(owner, 'pickup')
        delivery = make_driver(owner, 'delivery')
        booking = make_delivered_booking(
            owner,
            driver=delivery,
            pickup_driver=pickup,
            amount=10001,
        )

        # Act & Assert: 半額按分し、端数は配達側に付くことを確認
        self.assertEqual(_driver_sales_share(booking, pickup.id), 5000)
        self.assertEqual(_driver_sales_share(booking, delivery.id), 5001)

    def test_same_driver_job_count_is_two(self):
        # Arrange: 同一ドライバーが集荷・配達を担当する配達完了予約を用意
        owner = make_owner('both')
        driver = make_driver(owner, 'both')
        booking = make_delivered_booking(owner, driver=driver, amount=7000)

        # Act: 統計に予約売上を反映
        stats = {}
        _apply_booking_sales_to_stats(stats, booking)

        # Assert: 件数は集荷+配達で2、売上は二重計上しない
        self.assertEqual(_driver_job_count(booking, driver.id), 2)
        self.assertEqual(stats[str(driver.id)], {'count': 2, 'sales': 7000})

    def test_apply_stats_credits_both_drivers_once_each(self):
        # Arrange: 集荷と配達で別ドライバーの配達完了予約を用意
        owner = make_owner('stats')
        pickup = make_driver(owner, 'p')
        delivery = make_driver(owner, 'd')
        booking = make_delivered_booking(
            owner,
            driver=delivery,
            pickup_driver=pickup,
            amount=8000,
        )

        # Act: 統計に予約売上を反映
        stats = {}
        _apply_booking_sales_to_stats(stats, booking)

        # Assert: 各ドライバーに件数1・半額売上が付くことを確認
        self.assertEqual(_driver_job_count(booking, pickup.id), 1)
        self.assertEqual(_driver_job_count(booking, delivery.id), 1)
        self.assertEqual(stats[str(pickup.id)], {'count': 1, 'sales': 4000})
        self.assertEqual(stats[str(delivery.id)], {'count': 1, 'sales': 4000})

    def test_monthly_stats_include_pickup_only_share(self):
        # Arrange: 集荷と配達で別ドライバーの配達完了予約を用意
        owner = make_owner('month')
        pickup = make_driver(owner, 'month-p')
        delivery = make_driver(owner, 'month-d')
        make_delivered_booking(
            owner,
            driver=delivery,
            pickup_driver=pickup,
            amount=9000,
        )

        # Act: 各ドライバーの月次統計を取得
        pickup_stats = _monthly_stats(pickup)
        delivery_stats = _monthly_stats(delivery)
        this_month = timezone.localdate().strftime('%Y-%m')

        # Assert: 各ドライバーに件数1・半額売上が付くことを確認
        self.assertEqual(
            next(item for item in pickup_stats if item['month'] == this_month),
            {'month': this_month, 'count': 1, 'sales': 4500},
        )
        self.assertEqual(
            next(item for item in delivery_stats if item['month'] == this_month),
            {'month': this_month, 'count': 1, 'sales': 4500},
        )

    def test_monthly_stats_count_both_roles_for_same_driver(self):
        # Arrange: 同一ドライバーが集荷・配達を担当する配達完了予約を用意
        owner = make_owner('month-both')
        driver = make_driver(owner, 'month-both')
        make_delivered_booking(owner, driver=driver, amount=6000)

        # Act: 月次統計を取得
        stats = _monthly_stats(driver)
        this_month = timezone.localdate().strftime('%Y-%m')

        # Assert: 件数2・売上全額であることを確認
        self.assertEqual(
            next(item for item in stats if item['month'] == this_month),
            {'month': this_month, 'count': 2, 'sales': 6000},
        )


class DriverSalesApiTests(APITestCase):
    """ドライバー管理APIの売上・件数の返却とソートのテスト"""

    def test_list_and_detail_expose_split_sales(self):
        # Arrange: 集荷と配達で別ドライバーの配達完了予約を用意し、事業者として認証
        owner = make_owner('api')
        pickup = make_driver(owner, 'api-p')
        delivery = make_driver(owner, 'api-d')
        booking = make_delivered_booking(
            owner,
            driver=delivery,
            pickup_driver=pickup,
            amount=10000,
            payment_intent_id='pi-sales-api',
        )
        self.client.force_authenticate(owner.user)

        # Act: 一覧APIを呼び出し
        listing = self.client.get('/api/business/drivers/manage')

        # Assert: 各ドライバーに半額売上・件数1が返ることを確認
        self.assertEqual(listing.status_code, 200)
        by_id = {row['id']: row for row in listing.data['results']}
        self.assertEqual(by_id[str(pickup.id)]['month_sales'], 5000)
        self.assertEqual(by_id[str(delivery.id)]['month_sales'], 5000)
        self.assertEqual(by_id[str(pickup.id)]['month_delivery_count'], 1)
        self.assertEqual(by_id[str(delivery.id)]['month_delivery_count'], 1)

        # Act: 集荷ドライバー詳細APIを呼び出し
        detail = self.client.get(
            f'/api/business/drivers/manage/{pickup.id}'
        )

        # Assert: 過去配達に役割・按分売上・予約総額が含まれることを確認
        self.assertEqual(detail.status_code, 200)
        past = detail.data['driver']['past_deliveries']
        self.assertEqual(len(past), 1)
        self.assertEqual(past[0]['id'], str(booking.id))
        self.assertEqual(past[0]['assignment_role'], 'pickup')
        # attributed_sales は担当分、total_amount は予約全体の金額
        self.assertEqual(past[0]['attributed_sales'], 5000)
        self.assertEqual(past[0]['total_amount'], 10000)

    def test_list_counts_two_when_same_driver_handles_both(self):
        # Arrange: 同一ドライバーが集荷・配達を担当する配達完了予約を用意し、認証
        owner = make_owner('api-both')
        driver = make_driver(owner, 'api-both')
        make_delivered_booking(
            owner,
            driver=driver,
            amount=10000,
            payment_intent_id='pi-sales-api-both',
        )
        self.client.force_authenticate(owner.user)

        # Act: 一覧APIを呼び出し
        listing = self.client.get('/api/business/drivers/manage')

        # Assert: 件数は集荷+配達で2、売上は二重計上しない
        self.assertEqual(listing.status_code, 200)
        by_id = {row['id']: row for row in listing.data['results']}
        self.assertEqual(by_id[str(driver.id)]['month_delivery_count'], 2)
        self.assertEqual(by_id[str(driver.id)]['month_sales'], 10000)

    def test_list_sorts_by_sales_and_count(self):
        # Arrange: 売上・件数の異なるドライバーを用意し、認証
        # 期待: sales 順 high(10000) > mid(5000) > low(1000)
        #       count 順 high(4) > mid(2) > low(2)
        owner = make_owner('api-sort')
        low = make_driver(owner, 'sort-low')
        mid = make_driver(owner, 'sort-mid')
        high = make_driver(owner, 'sort-high')
        make_delivered_booking(
            owner, driver=low, amount=1000, payment_intent_id='pi-sort-low',
        )
        make_delivered_booking(
            owner, driver=mid, amount=5000, payment_intent_id='pi-sort-mid',
        )
        make_delivered_booking(
            owner, driver=high, amount=9000, payment_intent_id='pi-sort-high',
        )
        # high にもう1件追加し件数4にする
        make_delivered_booking(
            owner, driver=high, amount=1000, payment_intent_id='pi-sort-high-2',
        )
        self.client.force_authenticate(owner.user)

        # Act: 売上順で一覧を取得
        by_sales = self.client.get('/api/business/drivers/manage', {'sort': 'sales'})

        # Assert: 売上の高い順に並ぶことを確認
        self.assertEqual(by_sales.status_code, 200)
        self.assertEqual(by_sales.data['sort'], 'sales')
        sales_ids = [row['id'] for row in by_sales.data['results']]
        self.assertEqual(
            sales_ids[:3],
            [str(high.id), str(mid.id), str(low.id)],
        )

        # Act: 件数順で一覧を取得
        by_count = self.client.get('/api/business/drivers/manage', {'sort': 'count'})

        # Assert: 件数の多い順に並ぶことを確認
        self.assertEqual(by_count.status_code, 200)
        self.assertEqual(by_count.data['sort'], 'count')
        count_ids = [row['id'] for row in by_count.data['results']]
        self.assertEqual(count_ids[0], str(high.id))
        self.assertGreater(
            by_count.data['results'][0]['month_delivery_count'],
            by_count.data['results'][1]['month_delivery_count'],
        )

    def test_list_defaults_to_newest_created_first(self):
        # Arrange: 作成順の異なるドライバーを用意（後から作った方が newer）
        owner = make_owner('api-created')
        older = make_driver(owner, 'created-old')
        newer = make_driver(owner, 'created-new')
        self.client.force_authenticate(owner.user)

        # Act: デフォルトソート（sort 未指定）で一覧を取得
        listing = self.client.get('/api/business/drivers/manage')

        # Assert: デフォルトは created 降順（新しい順）
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.data['sort'], 'created')
        ids = [row['id'] for row in listing.data['results']]
        self.assertLess(ids.index(str(newer.id)), ids.index(str(older.id)))
