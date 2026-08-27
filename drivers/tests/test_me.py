"""配達者本人向けダッシュボード API（me/bookings, me/route）のテスト"""
from datetime import date, timedelta
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from bookings.models import LuggageBooking
from business_owners.models import BusinessProfile
from routing.services.google_routes import MatrixResult

from drivers.models import DriverProfile


User = get_user_model()


def make_owner(suffix='me'):
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


def make_driver(owner, suffix='me', latitude='35.0', longitude='139.0'):
    user = User.objects.create_user(
        email=f'driver-{suffix}@example.com',
        password='StrongPass123!',
        user_type='delivery_driver',
        first_name='太郎',
        last_name='配達',
    )
    return DriverProfile.objects.create(
        user=user,
        business_owner=owner,
        departure_latitude=latitude,
        departure_longitude=longitude,
        max_daily_stops=10,
        license_expiry=date.today() + timedelta(days=365),
    )


def make_booking(owner, suffix='me', **overrides):
    service_date = date.today() + timedelta(days=2)
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
        'payment_intent_id': f'pi-driver-me-{suffix}',
    }
    values.update(overrides)
    return LuggageBooking.objects.create(**values)


class DriverMyBookingsTests(APITestCase):
    """予約一覧 API のテスト"""

    def setUp(self):
        self.owner = make_owner('list')
        self.driver = make_driver(self.owner, 'list')
        self.service_date = date.today() + timedelta(days=2)

    def _get(self, **params):
        params.setdefault('date', self.service_date.isoformat())
        return self.client.get('/api/drivers/me/bookings', params)

    def test_rejects_unauthenticated_and_non_driver_users(self):
        # Act: 未ログインでアクセス
        anonymous = self._get()

        # Act: 事業者ユーザーでアクセス
        self.client.force_authenticate(self.owner.user)
        as_owner = self._get()

        # Assert: どちらも拒否されることを確認
        self.assertIn(anonymous.status_code, (401, 403))
        self.assertEqual(as_owner.status_code, 403)

    def test_rejects_invalid_date(self):
        # Arrange: 配達者として認証
        self.client.force_authenticate(self.driver.user)

        # Act: 不正な日付で取得
        response = self._get(date='not-a-date')

        # Assert: 400 が返ることを確認
        self.assertEqual(response.status_code, 400)

    def test_returns_only_own_assignments_for_date(self):
        # Arrange: 自分担当・他人担当・未割当・別日の予約を用意
        mine = make_booking(self.owner, 'mine', driver=self.driver)
        other_driver = make_driver(self.owner, 'list-other')
        make_booking(self.owner, 'other', driver=other_driver)
        make_booking(self.owner, 'unassigned')
        make_booking(
            self.owner, 'other-date',
            driver=self.driver,
            pickup_date=self.service_date + timedelta(days=1),
            delivery_date=self.service_date + timedelta(days=1),
        )
        self.client.force_authenticate(self.driver.user)

        # Act: 一覧を取得
        response = self._get()

        # Assert: 自分の当日担当のみが返ることを確認
        self.assertEqual(response.status_code, 200)
        results = response.json()['results']
        self.assertEqual([item['id'] for item in results], [str(mine.id)])
        self.assertTrue(results[0]['is_pickup_assignee'])
        self.assertTrue(results[0]['is_delivery_assignee'])
        self.assertEqual(results[0]['pickup']['lat'], 35.1)
        self.assertEqual(results[0]['delivery']['lng'], 139.2)

    def test_includes_pickup_only_assignment_via_pickup_driver(self):
        # Arrange: 集荷のみ自分担当（配達は他人）の予約を用意
        other_driver = make_driver(self.owner, 'list-dlv')
        booking = make_booking(
            self.owner, 'split',
            driver=other_driver,
            pickup_driver=self.driver,
        )
        self.client.force_authenticate(self.driver.user)

        # Act: 一覧を取得
        response = self._get()

        # Assert: 集荷担当として含まれ、配達担当ではないことを確認
        results = response.json()['results']
        self.assertEqual([item['id'] for item in results], [str(booking.id)])
        self.assertTrue(results[0]['is_pickup_assignee'])
        self.assertFalse(results[0]['is_delivery_assignee'])

    def test_filters_by_status_and_reports_counts(self):
        # Arrange: ステータス違いの担当予約を用意（キャンセル含む）
        make_booking(self.owner, 'st-before', driver=self.driver)
        picked = make_booking(
            self.owner, 'st-picked',
            driver=self.driver, delivery_status='picked_up',
        )
        make_booking(
            self.owner, 'st-cancel',
            driver=self.driver, delivery_status='cancelled',
        )
        self.client.force_authenticate(self.driver.user)

        # Act: 集荷済のみで絞り込み
        response = self._get(status='picked_up')

        # Assert: 絞り込み結果と全件の内訳件数を確認
        data = response.json()
        self.assertEqual(
            [item['id'] for item in data['results']], [str(picked.id)],
        )
        self.assertEqual(data['status_counts'], {
            'before_pickup': 1,
            'picked_up': 1,
            'delivered': 0,
            'cancelled': 1,
            'all': 3,
        })

    def test_paginates_results(self):
        # Arrange: 1ページ（10件）を超える担当予約を用意
        for index in range(12):
            make_booking(self.owner, f'page-{index}', driver=self.driver)
        self.client.force_authenticate(self.driver.user)

        # Act: 1ページ目と2ページ目を取得
        first = self._get().json()
        second = self._get(page=2).json()

        # Assert: ページ件数・総数を確認
        self.assertEqual(len(first['results']), 10)
        self.assertEqual(len(second['results']), 2)
        self.assertEqual(first['total_count'], 12)
        self.assertEqual(first['total_pages'], 2)


class DriverMyRouteTests(APITestCase):
    """最適化ルート API のテスト"""

    def setUp(self):
        self.owner = make_owner('route')
        self.driver = make_driver(self.owner, 'route')
        self.service_date = date.today() + timedelta(days=2)

    def _get(self, **params):
        params.setdefault('date', self.service_date.isoformat())
        return self.client.get('/api/drivers/me/route', params)

    @staticmethod
    def _matrix(size):
        """全区間 600 秒・1000m の簡易行列（対角は 0）"""
        durations = [
            [0 if i == j else 600 for j in range(size)] for i in range(size)
        ]
        distances = [
            [0 if i == j else 1000 for j in range(size)] for i in range(size)
        ]
        return MatrixResult(durations, distances, size * size)

    def test_rejects_non_driver_users(self):
        # Act: 事業者ユーザーでアクセス
        self.client.force_authenticate(self.owner.user)
        response = self._get()

        # Assert: 403 が返ることを確認
        self.assertEqual(response.status_code, 403)

    def test_returns_empty_route_without_tasks(self):
        # Arrange: 担当予約なしの配達者として認証
        self.client.force_authenticate(self.driver.user)

        # Act: ルートを取得
        response = self._get()

        # Assert: 空ルートが返ることを確認（Routes API は呼ばれない）
        self.assertEqual(response.status_code, 200)
        route = response.json()['route']
        self.assertEqual(route['stop_count'], 0)
        self.assertEqual(route['stops'], [])
        self.assertEqual(route['driver_id'], str(self.driver.id))

    @patch('routing.services.driver_route.GoogleRoutesMatrixClient')
    def test_builds_route_with_pickup_before_delivery(self, client_class):
        # Arrange: 同日ペアの担当予約と距離行列モックを用意
        booking = make_booking(self.owner, 'route-pair', driver=self.driver)
        client_class.return_value = Mock(
            compute=Mock(return_value=self._matrix(3)),
        )
        self.client.force_authenticate(self.driver.user)

        # Act: ルートを取得
        response = self._get()

        # Assert: 集荷→配達の順で訪問順が組まれることを確認
        self.assertEqual(response.status_code, 200)
        route = response.json()['route']
        self.assertEqual(route['stop_count'], 2)
        self.assertEqual(
            [stop['task_type'] for stop in route['stops']],
            ['pickup', 'delivery'],
        )
        self.assertEqual(
            {stop['booking_id'] for stop in route['stops']},
            {str(booking.id)},
        )
        self.assertEqual(
            [stop['sequence'] for stop in route['stops']], [1, 2],
        )
        self.assertGreater(route['total_duration_seconds'], 0)
        self.assertGreater(route['total_distance_meters'], 0)
        for stop in route['stops']:
            self.assertIsNotNone(stop['planned_arrival_at'])

    @patch('routing.services.driver_route.GoogleRoutesMatrixClient')
    def test_excludes_tasks_with_missing_coordinates(self, client_class):
        # Arrange: 座標欠損の配達先を持つ担当予約（配達日のみ当日）を用意
        make_booking(
            self.owner, 'route-missing',
            driver=self.driver,
            pickup_date=self.service_date - timedelta(days=1),
            delivery_status='picked_up',
            delivery_latitude=None,
            delivery_longitude=None,
        )
        client_class.return_value = Mock(
            compute=Mock(return_value=self._matrix(1)),
        )
        self.client.force_authenticate(self.driver.user)

        # Act: ルートを取得
        response = self._get()

        # Assert: 訪問先なし・除外理由付きで返ることを確認
        route = response.json()['route']
        self.assertEqual(route['stop_count'], 0)
        self.assertEqual(len(route['excluded']), 1)
        self.assertEqual(route['excluded'][0]['reason'], 'missing_coordinates')
        self.assertEqual(route['excluded'][0]['task_type'], 'delivery')

    def test_rejects_driver_without_departure_coordinates(self):
        # Arrange: 出発座標未設定の配達者を用意
        self.driver.departure_latitude = None
        self.driver.departure_longitude = None
        self.driver.save(update_fields=['departure_latitude', 'departure_longitude'])
        self.client.force_authenticate(self.driver.user)

        # Act: ルートを取得
        response = self._get()

        # Assert: 400 が返ることを確認
        self.assertEqual(response.status_code, 400)


SIGNATURE = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUg=='


class DriverBookingDetailTests(APITestCase):
    """予約詳細 API（取得・集荷完了・配達完了・費用保存）のテスト"""

    def setUp(self):
        self.owner = make_owner('detail')
        self.driver = make_driver(self.owner, 'detail')
        self.booking = make_booking(self.owner, 'detail', driver=self.driver)
        self.client.force_authenticate(self.driver.user)

    def _url(self, booking=None):
        return f'/api/drivers/me/bookings/{(booking or self.booking).id}'

    def test_returns_detail_with_result_fields(self):
        # Act: 詳細を取得
        response = self.client.get(self._url())

        # Assert: 実績フィールドを含む詳細が返ることを確認
        self.assertEqual(response.status_code, 200)
        booking = response.json()['booking']
        self.assertEqual(booking['id'], str(self.booking.id))
        self.assertEqual(booking['customer_name'], 'Test Customer')
        self.assertEqual(booking['guest_name'], 'Guest')
        self.assertEqual(booking['customer_phone_number'], '09012345678')
        self.assertEqual(booking['notes'], '')
        self.assertEqual(booking['luggage_items'], {})
        self.assertIsNone(booking['picked_up_at'])
        self.assertIsNone(booking['delivered_at'])
        self.assertIsNone(booking['facility_fee'])
        self.assertIsNone(booking['transport_cost'])
        self.assertEqual(booking['delivery_signature'], '')

    def test_returns_notes_luggage_and_contact(self):
        # Arrange: 備考・荷物内訳付きの予約を用意
        booking = make_booking(
            self.owner, 'detail-info',
            driver=self.driver,
            notes='フロントで受け渡し',
            luggage_items={'cabin': 1, 'checked': 2, 'oversize': 0},
        )

        # Act: 詳細を取得
        response = self.client.get(self._url(booking))

        # Assert: 備考・荷物内訳・連絡先が返ることを確認
        data = response.json()['booking']
        self.assertEqual(data['notes'], 'フロントで受け渡し')
        self.assertEqual(data['luggage_items'], {'cabin': 1, 'checked': 2, 'oversize': 0})
        self.assertEqual(data['customer_name'], 'Test Customer')
        self.assertEqual(data['guest_name'], 'Guest')
        self.assertEqual(data['customer_phone_number'], '09012345678')
        self.assertEqual(data['pickup']['date'], booking.pickup_date.isoformat())
        self.assertEqual(data['delivery']['date'], booking.delivery_date.isoformat())

    def test_rejects_other_drivers_booking(self):
        # Arrange: 他の配達者が担当する予約を用意
        other_driver = make_driver(self.owner, 'detail-other')
        other_booking = make_booking(self.owner, 'detail-other', driver=other_driver)

        # Act: 詳細を取得
        response = self.client.get(self._url(other_booking))

        # Assert: 404 が返ることを確認
        self.assertEqual(response.status_code, 404)

    def test_pickup_complete_updates_status_and_timestamp(self):
        # Act: 集荷完了にする
        response = self.client.patch(
            self._url(), {'action': 'pickup_complete'}, format='json',
        )

        # Assert: 集荷済 + 集荷完了日時が記録されることを確認
        self.assertEqual(response.status_code, 200)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.delivery_status, 'picked_up')
        self.assertIsNotNone(self.booking.picked_up_at)
        self.assertIsNotNone(response.json()['booking']['picked_up_at'])

    def test_pickup_complete_rejects_wrong_status(self):
        # Arrange: すでに集荷済にしておく
        self.booking.delivery_status = 'picked_up'
        self.booking.save(update_fields=['delivery_status'])

        # Act: 再度集荷完了にする
        response = self.client.patch(
            self._url(), {'action': 'pickup_complete'}, format='json',
        )

        # Assert: 400 が返りステータスが変わらないことを確認
        self.assertEqual(response.status_code, 400)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.delivery_status, 'picked_up')

    def test_pickup_complete_rejects_non_pickup_assignee(self):
        # Arrange: 集荷担当が他の配達者の予約（自分は配達担当のみ）
        other_driver = make_driver(self.owner, 'detail-pk')
        booking = make_booking(
            self.owner, 'detail-split',
            driver=self.driver, pickup_driver=other_driver,
        )

        # Act: 集荷完了にする
        response = self.client.patch(
            self._url(booking), {'action': 'pickup_complete'}, format='json',
        )

        # Assert: 403 が返ることを確認
        self.assertEqual(response.status_code, 403)

    def test_deliver_complete_requires_signature(self):
        # Arrange: 集荷済にしておく
        self.booking.delivery_status = 'picked_up'
        self.booking.save(update_fields=['delivery_status'])

        # Act: サインなし・形式不正で配達完了にする
        without_signature = self.client.patch(
            self._url(), {'action': 'deliver_complete'}, format='json',
        )
        invalid_format = self.client.patch(
            self._url(),
            {'action': 'deliver_complete', 'signature': 'not-a-data-url'},
            format='json',
        )

        # Assert: どちらも400でステータスが変わらないことを確認
        self.assertEqual(without_signature.status_code, 400)
        self.assertEqual(invalid_format.status_code, 400)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.delivery_status, 'picked_up')
        self.assertIsNone(self.booking.delivered_at)

    def test_deliver_complete_with_signature(self):
        # Arrange: 集荷済にしておく
        self.booking.delivery_status = 'picked_up'
        self.booking.save(update_fields=['delivery_status'])

        # Act: サイン付きで配達完了にする
        response = self.client.patch(
            self._url(),
            {'action': 'deliver_complete', 'signature': SIGNATURE},
            format='json',
        )

        # Assert: 配達済 + 配達完了日時 + サインが保存されることを確認
        self.assertEqual(response.status_code, 200)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.delivery_status, 'delivered')
        self.assertIsNotNone(self.booking.delivered_at)
        self.assertEqual(self.booking.delivery_signature, SIGNATURE)

    def test_deliver_complete_rejects_before_pickup(self):
        # Act: 集荷前のまま配達完了にする
        response = self.client.patch(
            self._url(),
            {'action': 'deliver_complete', 'signature': SIGNATURE},
            format='json',
        )

        # Assert: 400 が返ることを確認
        self.assertEqual(response.status_code, 400)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.delivery_status, 'before_pickup')

    def test_saves_fees(self):
        # Act: 手数料・交通費を保存
        response = self.client.patch(
            self._url(),
            {'facility_fee': 500, 'transport_cost': 1200},
            format='json',
        )

        # Assert: 保存されることを確認
        self.assertEqual(response.status_code, 200)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.facility_fee, 500)
        self.assertEqual(self.booking.transport_cost, 1200)

    def test_rejects_invalid_fees(self):
        # Act: 負数・文字列で保存
        negative = self.client.patch(
            self._url(), {'facility_fee': -1}, format='json',
        )
        non_integer = self.client.patch(
            self._url(), {'transport_cost': 'abc'}, format='json',
        )

        # Assert: どちらも400が返ることを確認
        self.assertEqual(negative.status_code, 400)
        self.assertEqual(non_integer.status_code, 400)


class DriverRouteCacheTests(APITestCase):
    """最適化ルートの保存・再利用（再生成の抑制）のテスト"""

    def setUp(self):
        self.owner = make_owner('cache')
        self.driver = make_driver(self.owner, 'cache')
        self.service_date = date.today() + timedelta(days=2)
        self.client.force_authenticate(self.driver.user)

    def _get(self, **params):
        params.setdefault('date', self.service_date.isoformat())
        return self.client.get('/api/drivers/me/route', params)

    @staticmethod
    def _matrix(size):
        durations = [
            [0 if i == j else 600 for j in range(size)] for i in range(size)
        ]
        distances = [
            [0 if i == j else 1000 for j in range(size)] for i in range(size)
        ]
        return MatrixResult(durations, distances, size * size)

    def _mock_client(self, client_class, size):
        compute = Mock(return_value=self._matrix(size))
        client_class.return_value = Mock(compute=compute)
        return compute

    @patch('routing.services.driver_route.GoogleRoutesMatrixClient')
    def test_second_request_uses_saved_route_without_matrix_call(self, client_class):
        # Arrange: 担当予約1件と距離行列モックを用意
        make_booking(self.owner, 'cache-hit', driver=self.driver)
        compute = self._mock_client(client_class, 3)

        # Act: 2回連続で取得
        first = self._get().json()['route']
        second = self._get().json()['route']

        # Assert: 距離行列は1回だけ呼ばれ、保存済みルートが再利用されることを確認
        self.assertEqual(compute.call_count, 1)
        self.assertEqual(first['stops'], second['stops'])
        self.assertIsNotNone(second['generated_at'])
        self.assertEqual(first['generated_at'], second['generated_at'])

    @patch('routing.services.driver_route.GoogleRoutesMatrixClient')
    def test_regenerates_when_booking_changes(self, client_class):
        # Arrange: 初回生成を済ませておく
        booking = make_booking(self.owner, 'cache-change', driver=self.driver)
        compute = self._mock_client(client_class, 3)
        first = self._get().json()['route']

        # Act: 配達先の座標が変わった後に再取得
        booking.delivery_latitude = '35.9'
        booking.delivery_longitude = '139.9'
        booking.save(update_fields=['delivery_latitude', 'delivery_longitude'])
        response = self._get()
        second = response.json()['route']

        # Assert: 距離行列が再計算され、生成日時が更新されることを確認
        self.assertEqual(response.status_code, 200)
        self.assertEqual(compute.call_count, 2)
        self.assertNotEqual(first['generated_at'], second['generated_at'])

    @patch('routing.services.driver_route.GoogleRoutesMatrixClient')
    def test_prunes_cancelled_booking_without_matrix_call(self, client_class):
        # Arrange: 担当予約2件で初回生成を済ませておく
        booking = make_booking(self.owner, 'cache-cancel-1', driver=self.driver)
        make_booking(self.owner, 'cache-cancel-2', driver=self.driver)
        compute = self._mock_client(client_class, 5)
        first = self._get().json()['route']
        remaining_ids = [
            stop['id']
            for stop in first['stops']
            if stop['booking_id'] != str(booking.id) # キャンセル予定予約は除外
        ]

        # Act: 1件キャンセル後に再取得
        booking.delivery_status = 'cancelled'
        booking.save(update_fields=['delivery_status'])
        second = self._get().json()['route']

        # Assert: 行列は初回のみ、残停留所は元の相対順のまま除外されることを確認
        self.assertEqual(compute.call_count, 1)
        self.assertEqual(first['stop_count'], 4)
        self.assertEqual(second['stop_count'], 2)
        self.assertEqual([stop['id'] for stop in second['stops']], remaining_ids)
        self.assertEqual(
            [stop['sequence'] for stop in second['stops']], [1, 2],
        )
        self.assertEqual(first['generated_at'], second['generated_at'])

    @patch('routing.services.driver_route.GoogleRoutesMatrixClient')
    def test_prunes_pickup_stop_when_picked_up(self, client_class):
        # Arrange: 同日の集荷・配達1件で初回生成を済ませておく
        booking = make_booking(self.owner, 'cache-pickup', driver=self.driver)
        compute = self._mock_client(client_class, 3)
        first = self._get().json()['route']

        # Act: 集荷完了後に再取得
        booking.delivery_status = 'picked_up'
        booking.save(update_fields=['delivery_status'])
        second = self._get().json()['route']

        # Assert: 集荷だけ外れ、配達は元の順で残り、行列は追加呼び出しされない
        self.assertEqual(compute.call_count, 1)
        self.assertEqual(
            [stop['task_type'] for stop in first['stops']],
            ['pickup', 'delivery'],
        )
        self.assertEqual(
            [stop['task_type'] for stop in second['stops']],
            ['delivery'],
        )
        self.assertEqual(second['stops'][0]['booking_id'], str(booking.id))
        self.assertEqual(second['stops'][0]['sequence'], 1)
        self.assertEqual(first['generated_at'], second['generated_at'])

    @patch('routing.services.driver_route.GoogleRoutesMatrixClient')
    def test_prunes_stops_when_delivered(self, client_class):
        # Arrange: 担当予約2件で初回生成を済ませておく
        booking = make_booking(self.owner, 'cache-deliver-1', driver=self.driver)
        make_booking(self.owner, 'cache-deliver-2', driver=self.driver)
        compute = self._mock_client(client_class, 5)
        first = self._get().json()['route']
        remaining_ids = [
            stop['id']
            for stop in first['stops']
            if stop['booking_id'] != str(booking.id) # 配達完了予定予約は除外
        ]

        # Act: 1件を配達完了にして再取得
        booking.delivery_status = 'delivered'
        booking.save(update_fields=['delivery_status'])
        second = self._get().json()['route']

        # Assert: 完了した予約の停留所がすべて外れ、行列は追加呼び出しされない
        self.assertEqual(compute.call_count, 1)
        self.assertEqual(first['stop_count'], 4)
        self.assertEqual(second['stop_count'], 2)
        self.assertEqual([stop['id'] for stop in second['stops']], remaining_ids)
        self.assertEqual(first['generated_at'], second['generated_at'])

    @patch('routing.services.driver_route.GoogleRoutesMatrixClient')
    def test_refresh_param_forces_regeneration(self, client_class):
        # Arrange: 初回生成を済ませておく（入力は変化させない）
        make_booking(self.owner, 'cache-force', driver=self.driver)
        compute = self._mock_client(client_class, 3)
        self._get()

        # Act: refresh=1 で再取得
        response = self._get(refresh='1')

        # Assert: 入力が同じでも再生成されることを確認
        self.assertEqual(response.status_code, 200)
        self.assertEqual(compute.call_count, 2)
