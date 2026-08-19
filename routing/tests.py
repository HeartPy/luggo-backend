from datetime import date, datetime, timedelta, time as dt_time
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from bookings.models import BookingAuditLog, LuggageBooking
from bookings.serializers import LuggageBookingCreateSerializer
from business_owners.models import BusinessProfile
from drivers.models import DriverProfile

from .models import DailyAssignmentRun, DailyTaskAssignment
from .services.assignment import assign_tasks
from .services.google_routes import (
    GoogleRoutesMatrixClient,
    resolve_driver_departure_time,
)
from .services.snapshots import snapshot_hash
from .services.solver import SolverError, solve_driver_route
from .services.task_builder import DriverInput, TaskInput, build_tasks
from .tasks import assign_daily_tasks


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


def make_booking(owner, suffix='one', **overrides):
    """検証済み座標付きの当日ペア予約を作成。overrides で個別フィールドを上書きできる。"""
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
        'payment_intent_id': f'pi-routing-{suffix}',
    }
    values.update(overrides)
    return LuggageBooking.objects.create(**values)


class TaskBuilderTests(TestCase):
    """予約・ドライバーから割当用タスクを構築する処理のテスト"""

    def test_builds_same_day_pair_and_preserves_fixed_driver(self):
        # Arrange: 手動割当済みドライバー付きの当日ペア予約を用意
        owner = make_owner()
        driver = make_driver(owner)
        booking = make_booking(
            owner,
            driver=driver,
            delivery_manually_assigned=True,
        )

        # Act: タスクを構築
        problem = build_tasks(owner, booking.pickup_date)

        # Assert: 集荷と配達が同一ペアで固定ドライバー付きであることを確認
        self.assertEqual(len(problem.tasks), 2)
        self.assertEqual({task.pair_id for task in problem.tasks}, {str(booking.id)})
        self.assertEqual(
            {task.fixed_driver_id for task in problem.tasks}, {str(driver.id)}
        )

    def test_auto_assigned_driver_is_not_fixed(self):
        # Arrange: 自動割当ドライバー付きの予約を用意
        owner = make_owner('auto-fixed')
        driver = make_driver(owner, 'auto-fixed')
        booking = make_booking(
            owner,
            'auto-fixed',
            driver=driver,
            delivery_manually_assigned=False,
        )

        # Act: タスクを構築
        problem = build_tasks(owner, booking.pickup_date)

        # Assert: 固定ドライバーが付かないことを確認
        self.assertEqual(len(problem.tasks), 2)
        self.assertEqual(
            {task.fixed_driver_id for task in problem.tasks},
            {None},
        )

    def test_computes_luggage_count_from_booking_and_driver_cap(self):
        # Arrange: 荷物個数の上限付きドライバーと、荷物情報付きの当日ペア予約を用意
        owner = make_owner('luggage-task')
        driver = make_driver(owner, 'luggage-task')
        driver.max_daily_luggage_count = 15
        driver.save(update_fields=['max_daily_luggage_count'])
        booking = make_booking(
            owner,
            'luggage-task',
            luggage_items={'cabin': 2, 'checked': 1, 'oversize': 0},
        )

        # Act: タスクを構築
        problem = build_tasks(owner, booking.pickup_date)

        # Assert: ドライバーの上限とタスクの荷物合計（cabin+checked）が反映されることを確認
        self.assertEqual(problem.drivers[0].max_luggage_count, 15)
        self.assertEqual(len(problem.tasks), 2)
        self.assertTrue(all(task.luggage_count == 3 for task in problem.tasks))

    def test_treats_different_service_dates_independently(self):
        # Arrange: 集荷日と配達日が異なる予約を用意
        owner = make_owner()
        make_driver(owner)
        booking = make_booking(
            owner, delivery_date=date.today() + timedelta(days=3)
        )

        # Act: それぞれのサービス日でタスクを構築
        pickup = build_tasks(owner, booking.pickup_date)
        delivery = build_tasks(owner, booking.delivery_date)

        # Assert: 各日のタスク種別が独立していることを確認
        self.assertEqual([task.kind for task in pickup.tasks], ['pickup'])
        self.assertEqual([task.kind for task in delivery.tasks], ['delivery'])

    def test_booking_serializer_rejects_out_of_range_coordinates(self):
        # Arrange: 緯度が範囲外の予約データを用意
        service_date = date.today() + timedelta(days=2)
        serializer = LuggageBookingCreateSerializer(data={
            'payment_intent_id': 'pi-coordinate-test',
            'pickup_location_name': 'Pickup',
            'pickup_location_address': 'Tokyo',
            'pickup_latitude': 91,
            'pickup_longitude': 139,
            'pickup_date': service_date.isoformat(),
            'delivery_location_name': 'Delivery',
            'delivery_location_address': 'Yokohama',
            'delivery_date': service_date.isoformat(),
            'customer_name': 'Test Customer',
            'customer_email': 'customer@example.com',
            'customer_phone_number': '09012345678',
            'customer_nationality': 'JPN',
            'guest_name': 'Guest',
        })

        # Act & Assert: バリデーションが失敗し緯度エラーになることを確認
        self.assertFalse(serializer.is_valid())
        self.assertIn('pickup_latitude', serializer.errors)

    def test_missing_same_day_coordinate_excludes_the_pair(self):
        # Arrange: 座標欠落の当日ペア予約を用意
        owner = make_owner('missing')
        make_driver(owner, 'missing')
        booking = make_booking(
            owner,
            'missing',
            pickup_latitude=None,
            pickup_longitude=None,
        )

        # Act: タスクを構築
        problem = build_tasks(owner, booking.pickup_date)

        # Assert: ペア全体が除外されることを確認
        self.assertFalse(problem.tasks)
        self.assertEqual(len(problem.excluded), 2)
        self.assertEqual(
            {item['reason'] for item in problem.excluded},
            {'missing_coordinates'},
        )

    def test_excludes_driver_on_regular_holiday(self):
        # Arrange: サービス日が定休日のドライバーを用意
        owner = make_owner('holiday')
        driver = make_driver(owner, 'holiday')
        booking = make_booking(owner, 'holiday')
        days = list('1111111')
        days[booking.pickup_date.weekday()] = '0'
        driver.operating_days = ''.join(days)
        driver.save(update_fields=['operating_days'])

        # Act: タスクを構築
        problem = build_tasks(owner, booking.pickup_date)

        # Assert: ドライバーが除外されることを確認
        self.assertFalse(problem.drivers)

    def test_selected_drivers_filter_and_preserve_manual_assignment(self):
        # Arrange: 選択ドライバーと固定ドライバーが異なる予約を用意
        owner = make_owner('selected')
        selected = make_driver(owner, 'selected-a')
        fixed = make_driver(owner, 'selected-b')
        booking = make_booking(
            owner,
            'selected',
            driver=fixed,
            delivery_manually_assigned=True,
        )

        # Act: 選択ドライバーを指定してタスクを構築
        problem = build_tasks(
            owner,
            booking.pickup_date,
            selected_driver_ids={str(selected.id)},
        )

        # Assert: 選択ドライバーのみ残り、固定ドライバー未選択で除外されることを確認
        self.assertEqual([driver.id for driver in problem.drivers], [str(selected.id)])
        self.assertFalse(problem.tasks)
        self.assertEqual(
            {item['reason'] for item in problem.excluded},
            {'fixed_driver_not_selected'},
        )


class AssignmentTests(TestCase):
    """ドライバーへのタスク割当ロジックのテスト"""

    def test_keeps_pair_together_and_respects_fixed_driver(self):
        # Arrange: 固定ドライバー付きのペアタスクを用意
        drivers = [
            DriverInput('a', 35.0, 139.0, 4),
            DriverInput('b', 36.0, 140.0, 4),
        ]
        tasks = [
            TaskInput(
                'booking:pickup', 'booking', 'pickup', 35.1, 139.1,
                fixed_driver_id='b', pair_id='booking',
            ),
            TaskInput(
                'booking:delivery', 'booking', 'delivery', 35.2, 139.2,
                fixed_driver_id='b', pair_id='booking',
            ),
        ]

        # Act: 割当を実行
        result = assign_tasks(drivers, tasks)

        # Assert: ペアが同一の固定ドライバーに割当されることを確認
        self.assertFalse(result.unassigned)
        self.assertEqual(
            {item.driver.id for item in result.assignments},
            {'b'},
        )

    def test_returns_unassigned_when_stop_capacity_is_insufficient(self):
        # Arrange: キャパシティ不足のドライバーと複数タスクを用意
        drivers = [DriverInput('a', 35.0, 139.0, 1)]
        tasks = [
            TaskInput('one', 'one', 'pickup', 35.1, 139.1),
            TaskInput('two', 'two', 'pickup', 35.2, 139.2),
        ]

        # Act: 割当を実行
        result = assign_tasks(drivers, tasks)

        # Assert: 1件割当・1件未割当になることを確認
        self.assertEqual(len(result.assignments), 1)
        self.assertEqual(len(result.unassigned), 1)
        self.assertEqual(
            result.unassigned[0]['reason'],
            'assignment_capacity_exceeded',
        )

    def test_driver_without_stop_cap_is_unconstrained(self):
        # Arrange: 訪問数の上限を設定していないドライバーと複数タスクを用意
        drivers = [DriverInput('a', 35.0, 139.0, max_stops=None)]
        tasks = [
            TaskInput('one', 'one', 'pickup', 35.1, 139.1),
            TaskInput('two', 'two', 'pickup', 35.2, 139.2),
            TaskInput('three', 'three', 'pickup', 35.3, 139.3),
        ]

        # Act: 割当を実行
        result = assign_tasks(drivers, tasks)

        # Assert: 上限なしのためすべて割当できることを確認
        self.assertFalse(result.unassigned)
        self.assertEqual(len(result.assignments), 3)

    def test_returns_unassigned_when_luggage_capacity_is_insufficient(self):
        # Arrange: 荷物個数の上限が1個のドライバーと、それぞれ1個の荷物を持つ2タスクを用意
        drivers = [DriverInput('a', 35.0, 139.0, 10, max_luggage_count=1)]
        tasks = [
            TaskInput('one', 'one', 'pickup', 35.1, 139.1, luggage_count=1),
            TaskInput('two', 'two', 'pickup', 35.2, 139.2, luggage_count=1),
        ]

        # Act: 割当を実行
        result = assign_tasks(drivers, tasks)

        # Assert: 荷物個数の上限により1件だけ割当されることを確認
        self.assertEqual(len(result.assignments), 1)
        self.assertEqual(len(result.unassigned), 1)
        self.assertEqual(
            result.unassigned[0]['reason'],
            'assignment_capacity_exceeded',
        )

    def test_same_day_pair_luggage_counted_once(self):
        # Arrange: 荷物個数の上限が1個のドライバーと、同一予約の集荷・配達ペアを用意
        # ペアは同じ荷物を運ぶだけなので、上限を超えず割当できるはず
        drivers = [DriverInput('a', 35.0, 139.0, 10, max_luggage_count=1)]
        tasks = [
            TaskInput(
                'booking:pickup', 'booking', 'pickup', 35.1, 139.1,
                pair_id='booking', luggage_count=1,
            ),
            TaskInput(
                'booking:delivery', 'booking', 'delivery', 35.2, 139.2,
                pair_id='booking', luggage_count=1,
            ),
        ]

        # Act: 割当を実行
        result = assign_tasks(drivers, tasks)

        # Assert: ペアの荷物は1回分としてカウントされ、両方割当されることを確認
        self.assertFalse(result.unassigned)
        self.assertEqual(len(result.assignments), 2)

    def test_driver_without_luggage_cap_is_unconstrained(self):
        # Arrange: 荷物個数の上限を設定していないドライバーと大量の荷物タスクを用意
        drivers = [DriverInput('a', 35.0, 139.0, 10, max_luggage_count=None)]
        tasks = [
            TaskInput('one', 'one', 'pickup', 35.1, 139.1, luggage_count=50),
            TaskInput('two', 'two', 'pickup', 35.2, 139.2, luggage_count=50),
        ]

        # Act: 割当を実行
        result = assign_tasks(drivers, tasks)

        # Assert: 上限なしのため両方とも割当できることを確認
        self.assertFalse(result.unassigned)
        self.assertEqual(len(result.assignments), 2)

    def test_same_input_produces_same_assignment(self):
        # Arrange: 同一入力のドライバーとタスクを用意
        drivers = [
            DriverInput('a', 35.0, 139.0, 2),
            DriverInput('b', 35.2, 139.2, 2),
        ]
        tasks = [
            TaskInput('one', 'one', 'pickup', 35.01, 139.01),
            TaskInput('two', 'two', 'pickup', 35.19, 139.19),
        ]

        # Act: 同じ入力で2回割当を実行
        first = assign_tasks(drivers, tasks)
        second = assign_tasks(drivers, tasks)

        # Assert: 割当結果が一致することを確認
        self.assertEqual(
            [(item.task.id, item.driver.id) for item in first.assignments],
            [(item.task.id, item.driver.id) for item in second.assignments],
        )

    @override_settings(ROUTING_ASSIGNMENT_TIME_LIMIT_SECONDS=10)
    def test_scales_to_600_tasks_and_30_drivers(self):
        # Arrange: 30ドライバー・600タスクの大規模入力を用意
        # タスクはドライバー近傍に配置し、訪問数上限内で解けやすくする
        drivers = [
            DriverInput(
                str(index),
                35.0 + index * 0.001,
                139.0 + index * 0.001,
                20,
            )
            for index in range(30)
        ]
        tasks = []
        for booking_index in range(300):
            cluster = booking_index % 30
            latitude = 35.0 + cluster * 0.001
            longitude = 139.0 + cluster * 0.001
            pair_id = f'booking-{booking_index}'
            tasks.extend([
                TaskInput(
                    f'{pair_id}:pickup',
                    pair_id,
                    'pickup',
                    latitude,
                    longitude,
                    pair_id=pair_id,
                ),
                TaskInput(
                    f'{pair_id}:delivery',
                    pair_id,
                    'delivery',
                    latitude + 0.0001,
                    longitude + 0.0001,
                    pair_id=pair_id,
                ),
            ])

        # Act: 割当を実行
        result = assign_tasks(drivers, tasks)

        # Assert: 全タスクが割当され、訪問数上限とペア制約を満たすことを確認
        self.assertEqual(len(result.assignments), 600)
        self.assertFalse(result.unassigned)
        counts = {}
        booking_drivers = {}
        for assignment in result.assignments:
            counts[assignment.driver.id] = counts.get(assignment.driver.id, 0) + 1
            booking_drivers.setdefault(assignment.task.booking_id, set()).add(
                assignment.driver.id
            )
        self.assertTrue(all(count <= 20 for count in counts.values()))
        self.assertTrue(all(len(values) == 1 for values in booking_drivers.values()))


@override_settings(
    CACHES={
        'default': {
            'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        }
    }
)
class MatrixClientTests(TestCase):
    """Google Routes 行列クライアント（チャンク・リトライ・キャッシュ）のテスト"""

    def setUp(self):
        cache.clear()

    @override_settings(ROUTING_MATRIX_CHUNK_SIZE=25)
    def test_chunks_and_parses_matrix_elements(self):
        # Arrange: チャンク分割される規模の地点とモック応答を用意
        session = Mock()

        def response_for_request(*args, **kwargs):
            # リクエスト内の origins×destinations 分だけ要素を返す
            payload = kwargs['json']
            elements = []
            for i in range(len(payload['origins'])):
                for j in range(len(payload['destinations'])):
                    elements.append({
                        'originIndex': i,
                        'destinationIndex': j,
                        'condition': 'ROUTE_EXISTS',
                        'duration': '1.2s',
                        'distanceMeters': 7,
                    })
            response = Mock()
            response.json.return_value = elements
            return response

        session.post.side_effect = response_for_request

        # Act: 行列を計算
        result = GoogleRoutesMatrixClient(
            api_key='test-key', session=session
        ).compute([(35.0, 139.0)] * 26)

        # Assert: チャンク分割と、応答から時間・距離を正しく読めたことを確認
        self.assertEqual(session.post.call_count, 4)
        self.assertEqual(result.element_count, 676)
        self.assertEqual(result.durations[0][25], 2)
        self.assertEqual(result.distances[25][0], 7)

    @override_settings(
        ROUTING_MATRIX_CACHE_SECONDS=0,
        ROUTING_MATRIX_ELEMENTS_PER_MINUTE=0,
        ROUTING_MATRIX_CHUNK_RETRIES=2,
    )
    def test_retries_cancelled_matrix_chunk(self):
        # Arrange: キャンセル後に成功するモック応答を用意
        session = Mock()
        cancelled = Mock()
        cancelled.status_code = 200
        cancelled.json.return_value = [{
            'originIndex': 0,
            'destinationIndex': 0,
            'status': {'code': 1, 'message': 'Request was cancelled.'},
        }]
        ok = Mock()
        ok.status_code = 200
        ok.json.return_value = [{
            'originIndex': 0,
            'destinationIndex': 0,
            'condition': 'ROUTE_EXISTS',
            'duration': '3s',
            'distanceMeters': 10,
        }]
        session.post.side_effect = [cancelled, ok]

        # Act: 行列を計算
        result = GoogleRoutesMatrixClient(
            api_key='test-key', session=session
        ).compute([(35.0, 139.0)])

        # Assert: リトライ後に成功結果が返ることを確認
        self.assertEqual(session.post.call_count, 2)
        self.assertEqual(result.durations[0][0], 3)
        self.assertEqual(result.distances[0][0], 10)

    @override_settings(
        ROUTING_MATRIX_CACHE_SECONDS=0,
        ROUTING_MATRIX_ELEMENTS_PER_MINUTE=0,
        ROUTING_MATRIX_CHUNK_SIZE=2,
        ROUTING_MATRIX_CHUNK_RETRIES=1,
    )
    def test_splits_cancelled_chunk_into_smaller_requests(self):
        # Arrange: 大きいチャンクがキャンセルされるモック応答を用意
        session = Mock()

        def response_for_request(*args, **kwargs):
            # 2×2 はキャンセル、分割後の小さいチャンクは成功させる
            payload = kwargs['json']
            origins = len(payload['origins'])
            destinations = len(payload['destinations'])
            response = Mock()
            response.status_code = 200
            if origins == 2 and destinations == 2:
                response.json.return_value = [{
                    'originIndex': 0,
                    'destinationIndex': 0,
                    'status': {'code': 1, 'message': 'Request was cancelled.'},
                }]
            else:
                response.json.return_value = [
                    {
                        'originIndex': i,
                        'destinationIndex': j,
                        'condition': 'ROUTE_EXISTS',
                        'duration': '2s',
                        'distanceMeters': 5,
                    }
                    for i in range(origins)
                    for j in range(destinations)
                ]
            return response

        session.post.side_effect = response_for_request

        # Act: 行列を計算
        result = GoogleRoutesMatrixClient(
            api_key='test-key', session=session
        ).compute([(35.0, 139.0), (35.1, 139.1)])

        # Assert: 小さいリクエストへ分割して完了することを確認
        self.assertGreater(session.post.call_count, 1)
        self.assertEqual(result.element_count, 4)
        self.assertEqual(result.durations[0][1], 2)

    @override_settings(
        ROUTING_MATRIX_CACHE_SECONDS=60,
        ROUTING_MATRIX_ELEMENTS_PER_MINUTE=0,
    )
    def test_reuses_short_lived_matrix_cache(self):
        # Arrange: 行列クライアントと成功応答を用意
        session = Mock()
        response = Mock()
        response.json.return_value = [{
            'originIndex': 0,
            'destinationIndex': 0,
            'condition': 'ROUTE_EXISTS',
            'duration': '0s',
            'distanceMeters': 0,
        }]
        session.post.return_value = response
        client = GoogleRoutesMatrixClient(api_key='test-key', session=session)

        # Act: 同一入力で2回計算
        first = client.compute([(35.123456, 139.123456)])
        second = client.compute([(35.123456, 139.123456)])

        # Assert: キャッシュ再利用で API 呼び出しが1回であることを確認
        self.assertEqual(first, second)
        self.assertEqual(session.post.call_count, 1)

    @override_settings(
        ROUTING_GOOGLE_ROUTING_PREFERENCE='TRAFFIC_AWARE_OPTIMAL',
        ROUTING_MATRIX_CHUNK_SIZE=25,
        ROUTING_MATRIX_CACHE_SECONDS=0,
        ROUTING_MATRIX_ELEMENTS_PER_MINUTE=0,
    )
    def test_optimal_traffic_requests_use_100_element_chunks(self):
        # Arrange: OPTIMAL 向けのモック応答を用意
        session = Mock()

        def response_for_request(*args, **kwargs):
            # TRAFFIC_AWARE_OPTIMAL では要素数が 100 以下になるようチャンクされる想定
            payload = kwargs['json']
            response = Mock()
            response.json.return_value = [
                {
                    'originIndex': i,
                    'destinationIndex': j,
                    'condition': 'ROUTE_EXISTS',
                    'duration': '1s',
                    'distanceMeters': 1,
                }
                for i in range(len(payload['origins']))
                for j in range(len(payload['destinations']))
            ]
            return response

        session.post.side_effect = response_for_request

        # Act: 11地点の行列を計算
        GoogleRoutesMatrixClient(
            api_key='test-key', session=session
        ).compute([(35.0 + index / 1000, 139.0) for index in range(11)])

        # Assert: 各リクエストが100要素以下でチャンクされることを確認
        self.assertEqual(session.post.call_count, 4)
        for request in session.post.call_args_list:
            payload = request.kwargs['json']
            self.assertLessEqual(
                len(payload['origins']) * len(payload['destinations']),
                100,
            )


@override_settings(
    CACHES={
        'default': {
            'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        }
    }
)
class DriverDepartureTimeTests(TestCase):
    """ドライバー出発時刻の解決と行列リクエストへの反映のテスト"""

    def setUp(self):
        cache.clear()

    def test_resolve_driver_departure_time_uses_driver_shift(self):
        # Arrange: 未来日とシフト開始時刻付きドライバーを用意
        service_date = date.today() + timedelta(days=3)
        driver = DriverInput(
            id='1',
            latitude=35.0,
            longitude=139.0,
            max_stops=10,
            shift_start_seconds=8 * 3600 + 30 * 60,
        )

        # Act: 出発時刻を解決
        departure = resolve_driver_departure_time(service_date, driver)
        expected = timezone.make_aware(
            datetime.combine(service_date, dt_time(8, 30))
        )

        # Assert: シフト開始時刻が出発になることを確認
        self.assertEqual(departure, expected)

    def test_resolve_driver_departure_time_clamps_past_to_now(self):
        # Arrange: 過去日のサービス日を用意
        service_date = date.today() - timedelta(days=1)
        driver = DriverInput(
            id='1',
            latitude=35.0,
            longitude=139.0,
            max_stops=10,
            shift_start_seconds=9 * 3600,
        )

        # Act: 出発時刻を解決
        before = timezone.now()
        departure = resolve_driver_departure_time(service_date, driver)
        after = timezone.now()

        # Assert: 過去時刻が現在時刻にクランプされることを確認
        self.assertGreaterEqual(departure, before)
        self.assertLessEqual(departure, after)

    @override_settings(
        ROUTING_MATRIX_CACHE_SECONDS=0,
        ROUTING_MATRIX_ELEMENTS_PER_MINUTE=0,
        ROUTING_GOOGLE_ROUTING_PREFERENCE='TRAFFIC_AWARE',
    )
    def test_compute_sends_departure_time_for_traffic_aware(self):
        # Arrange: TRAFFIC_AWARE 用のモックと出発時刻を用意
        session = Mock()
        response = Mock()
        response.json.return_value = [{
            'originIndex': 0,
            'destinationIndex': 0,
            'condition': 'ROUTE_EXISTS',
            'duration': '1s',
            'distanceMeters': 1,
        }]
        session.post.return_value = response
        departure = timezone.make_aware(
            datetime.combine(date.today() + timedelta(days=2), dt_time(9, 0))
        )

        # Act: 出発時刻付きで行列を計算
        GoogleRoutesMatrixClient(
            api_key='test-key', session=session
        ).compute([(35.0, 139.0)], departure_time=departure)

        # Assert: departureTime がペイロードに含まれることを確認
        payload = session.post.call_args.kwargs['json']
        self.assertEqual(payload['departureTime'], departure.isoformat())
        self.assertEqual(payload['routingPreference'], 'TRAFFIC_AWARE')

    @override_settings(
        ROUTING_MATRIX_CACHE_SECONDS=0,
        ROUTING_MATRIX_ELEMENTS_PER_MINUTE=0,
        ROUTING_GOOGLE_ROUTING_PREFERENCE='TRAFFIC_UNAWARE',
    )
    def test_compute_omits_departure_time_for_traffic_unaware(self):
        # Arrange: TRAFFIC_UNAWARE 用のモックと出発時刻を用意
        session = Mock()
        response = Mock()
        response.json.return_value = [{
            'originIndex': 0,
            'destinationIndex': 0,
            'condition': 'ROUTE_EXISTS',
            'duration': '1s',
            'distanceMeters': 1,
        }]
        session.post.return_value = response
        departure = timezone.make_aware(
            datetime.combine(date.today() + timedelta(days=2), dt_time(9, 0))
        )

        # Act: 出発時刻付きで行列を計算
        GoogleRoutesMatrixClient(
            api_key='test-key', session=session
        ).compute([(35.0, 139.0)], departure_time=departure)

        # Assert: departureTime が省略されることを確認
        payload = session.post.call_args.kwargs['json']
        self.assertNotIn('departureTime', payload)


class SolverTests(TestCase):
    """ドライバー単位の訪問順序ソルバーのテスト"""

    @override_settings(
        ROUTING_SERVICE_SECONDS_PER_STOP=0,
        ROUTING_SOLVER_TIME_LIMIT_SECONDS=1,
    )
    def test_same_day_pair_visits_pickup_before_delivery(self):
        # Arrange: 当日ペアのタスクと行列を用意
        driver = DriverInput('a', 0, 0, 10)
        tasks = [
            TaskInput('p', 'booking', 'pickup', 0, 0.1, pair_id='booking'),
            TaskInput('d', 'booking', 'delivery', 0, 0.2, pair_id='booking'),
        ]
        matrix = [
            [0, 1, 2],
            [1, 0, 1],
            [2, 1, 0],
        ]

        # Act: ルートを求解
        route = solve_driver_route(driver, tasks, matrix, matrix)

        # Assert: 集荷が配達より先になることを確認
        self.assertEqual([stop.task.kind for stop in route.stops], [
            'pickup', 'delivery'
        ])

    @override_settings(
        ROUTING_SERVICE_SECONDS_PER_STOP=0,
        ROUTING_SOLVER_TIME_LIMIT_SECONDS=1,
    )
    def test_visits_all_tasks_and_returns_to_departure(self):
        # Arrange: 複数タスクと非対称コスト行列を用意
        driver = DriverInput('a', 0, 0, 10)
        tasks = [
            TaskInput('a', 'booking-a', 'pickup', 0, 0.1),
            TaskInput('b', 'booking-b', 'delivery', 0, 0.2),
        ]
        durations = [
            [0, 10, 20],
            [50, 0, 10],
            [100, 100, 0],
        ]
        distances = [
            [0, 100, 200],
            [500, 0, 100],
            [1000, 1000, 0],
        ]

        # Act: ルートを求解
        route = solve_driver_route(driver, tasks, durations, distances)

        # Assert: 全訪問と、出発地点に戻るまでの合計時間・距離を確認
        self.assertEqual([stop.task.id for stop in route.stops], ['a', 'b'])
        self.assertEqual(route.total_duration, 120)
        self.assertEqual(route.total_distance, 1200)

    def test_rejects_matrix_with_wrong_size(self):
        # Arrange: サイズ不一致の行列を用意
        driver = DriverInput('a', 0, 0, 10)
        tasks = [
            TaskInput('a', 'booking-a', 'pickup', 0, 0.1),
        ]

        # Act & Assert: SolverError になることを確認
        with self.assertRaises(SolverError):
            solve_driver_route(driver, tasks, [[0]], [[0, 1], [1, 0]])

    @override_settings(ROUTING_SOLVER_TIME_LIMIT_SECONDS=0)
    def test_raises_when_solver_cannot_find_route(self):
        # Arrange: 時間制限0 + 100タスクで意図的に求解失敗させる
        driver = DriverInput('a', 0, 0, 10)
        tasks = [
            TaskInput(
                str(index),
                f'booking-{index}',
                'pickup',
                0,
                index / 100,
            )
            for index in range(100)
        ]
        size = len(tasks) + 1
        # 対角0・それ以外1のダミー行列（サイズ整合だけ満たす）
        matrix = []
        for row in range(size):
            row_values = []
            for column in range(size):
                if column == row:
                    row_values.append(0)
                else:
                    row_values.append(1)
            matrix.append(row_values)

        # Act & Assert: SolverError になることを確認
        with self.assertRaises(SolverError):
            solve_driver_route(driver, tasks, matrix, matrix)


class AssignmentTaskTests(TestCase):
    """日次割当タスクの永続化と手動固定フラグのテスト"""

    def test_persists_assignments_without_route_api(self):
        # Arrange: 日次割当の実行記録と入力スナップショットを用意
        owner = make_owner('task')
        driver = make_driver(owner, 'task')
        booking = make_booking(
            owner,
            'task',
            delivery_date=date.today() + timedelta(days=3),
        )
        problem = build_tasks(owner, booking.pickup_date)
        snapshot = problem.snapshot()
        run = DailyAssignmentRun.objects.create(
            business_owner=owner,
            service_date=booking.pickup_date,
            input_hash=snapshot_hash(snapshot),
        )

        # Act: Routes API をモックした状態で日次割当を実行
        with patch(
            'routing.services.google_routes.GoogleRoutesMatrixClient.compute'
        ) as compute:
            assign_daily_tasks(str(run.id))

        # Assert: 割当が永続化され Routes API が呼ばれないことを確認
        run.refresh_from_db()
        assignment = DailyTaskAssignment.objects.get(run=run)
        self.assertEqual(run.status, DailyAssignmentRun.Status.DRAFT)
        self.assertEqual(assignment.booking, booking)
        self.assertEqual(assignment.driver, driver)
        self.assertEqual(assignment.kind, 'pickup')
        self.assertFalse(assignment.manually_assigned)
        compute.assert_not_called()

    def test_marks_only_manual_fixed_assignments(self):
        # Arrange: 手動固定と自動割当の予約を用意
        owner = make_owner('manual-flag')
        driver = make_driver(owner, 'manual-flag')
        other = make_driver(owner, 'manual-flag-b', latitude=35.5, longitude=139.5)
        manual = make_booking(
            owner,
            'manual-flag',
            driver=driver,
            delivery_manually_assigned=True,
            delivery_date=date.today() + timedelta(days=3),
        )
        auto = make_booking(
            owner,
            'auto-flag',
            driver=other,
            delivery_manually_assigned=False,
            pickup_latitude='35.5',
            pickup_longitude='139.5',
            delivery_date=date.today() + timedelta(days=3),
        )
        problem = build_tasks(owner, manual.pickup_date)
        run = DailyAssignmentRun.objects.create(
            business_owner=owner,
            service_date=manual.pickup_date,
            input_hash=snapshot_hash(problem.snapshot()),
            selected_driver_ids=[str(driver.id), str(other.id)],
        )

        # Act: 日次割当を実行
        assign_daily_tasks(str(run.id))

        # Assert: 手動固定のみ manually_assigned になることを確認
        flags = {
            (item.booking_id, item.kind): item.manually_assigned
            for item in DailyTaskAssignment.objects.filter(run=run)
        }
        self.assertTrue(flags[(manual.id, 'pickup')])
        self.assertFalse(flags[(auto.id, 'pickup')])


class RoutingAPITests(APITestCase):
    """ルーティング割当・見積・適用などの Business API のテスト"""

    def setUp(self):
        # Arrange: API テスト用の事業者・ドライバー・予約を用意
        self.owner = make_owner('api')
        self.driver = make_driver(self.owner, 'api')
        self.booking = make_booking(
            self.owner,
            'api',
            delivery_date=date.today() + timedelta(days=3),
        )

    def test_assign_requires_authentication(self):
        # Act: 未認証で割当 API を呼び出す
        response = self.client.post(
            '/api/business/routing/assign',
            {'date': self.booking.pickup_date.isoformat()},
            format='json',
        )

        # Assert: 認証エラーになることを確認
        self.assertIn(response.status_code, (401, 403))

    @patch('routing.views.assign_daily_tasks.delay')
    def test_assign_queues_scoped_run_and_rejects_duplicate(self, delay):
        # Arrange: 認証済みクライアントと同一日付のペイロードを用意
        self.client.force_authenticate(self.owner.user)
        payload = {'date': self.booking.pickup_date.isoformat()}

        # Act: 割当を2回キューする
        response = self.client.post(
            '/api/business/routing/assign', payload, format='json'
        )
        duplicate = self.client.post(
            '/api/business/routing/assign', payload, format='json'
        )

        # Assert: 初回は受付・重複は拒否され、自社の割当実行だけが作られることを確認
        self.assertEqual(response.status_code, 202)
        self.assertEqual(duplicate.status_code, 409)
        run_data = response.data['run']
        self.assertEqual(run_data['assignment_count'], 0)
        self.assertEqual(run_data['assignment_groups'], [])
        self.assertEqual(run_data['unassigned_tasks'], [])
        self.assertIn('selected_driver_ids', run_data)
        run = DailyAssignmentRun.objects.get(id=run_data['id'])
        self.assertEqual(run.business_owner, self.owner)
        delay.assert_called_once_with(str(run.id))

    def test_estimate_reports_assignment_capacity_without_api_elements(self):
        # Arrange: 認証済みクライアントを用意
        self.client.force_authenticate(self.owner.user)

        # Act: 見積 API を呼び出す
        response = self.client.get(
            '/api/business/routing/estimate',
            {'date': self.booking.pickup_date.isoformat()},
        )

        # Assert: 割当容量が見積もられることを確認
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['eligible_driver_count'], 1)
        self.assertEqual(
            response.data['eligible_drivers'][0]['id'],
            str(self.driver.id),
        )
        self.assertEqual(response.data['task_count'], 1)
        self.assertEqual(response.data['booking_count'], 1)
        self.assertEqual(response.data['stop_capacity'], 10)
        self.assertTrue(response.data['capacity_sufficient'])
        self.assertTrue(response.data['within_limit'])

    @patch('routing.views.assign_daily_tasks.delay')
    def test_assign_persists_selected_drivers(self, delay):
        # Arrange: 選択ドライバーを用意して認証
        other_driver = make_driver(self.owner, 'api-other')
        self.client.force_authenticate(self.owner.user)

        # Act: 選択ドライバー付きで割当 API を呼び出す
        response = self.client.post(
            '/api/business/routing/assign',
            {
                'date': self.booking.pickup_date.isoformat(),
                'selected_driver_ids': [str(other_driver.id)],
            },
            format='json',
        )

        # Assert: 選択ドライバーが run に保存されることを確認
        self.assertEqual(response.status_code, 202)
        run = DailyAssignmentRun.objects.get(id=response.data['run']['id'])
        self.assertEqual(run.selected_driver_ids, [str(other_driver.id)])
        delay.assert_called_once()

    def test_assign_rejects_empty_or_ineligible_driver_selection(self):
        # Arrange: 認証済みクライアントを用意
        self.client.force_authenticate(self.owner.user)

        # Act: 空選択と他社ドライバー選択で割当を試みる
        empty = self.client.post(
            '/api/business/routing/assign',
            {
                'date': self.booking.pickup_date.isoformat(),
                'selected_driver_ids': [],
            },
            format='json',
        )
        other_owner = make_owner('foreign-driver')
        foreign_driver = make_driver(other_owner, 'foreign-driver')
        foreign = self.client.post(
            '/api/business/routing/assign',
            {
                'date': self.booking.pickup_date.isoformat(),
                'selected_driver_ids': [str(foreign_driver.id)],
            },
            format='json',
        )

        # Assert: いずれもバリデーションエラーになることを確認
        self.assertEqual(empty.status_code, 400)
        self.assertEqual(foreign.status_code, 400)

    def test_run_detail_is_owner_scoped(self):
        # Arrange: 他社オーナーと既存 run を用意
        other = make_owner('other')
        run = self._draft_run()
        self.client.force_authenticate(other.user)

        # Act: 他社として run 詳細を取得
        response = self.client.get(f'/api/business/routing/runs/{run.id}')

        # Assert: 見つからないことを確認
        self.assertEqual(response.status_code, 404)

    def test_run_detail_groups_unordered_assignments_by_driver(self):
        # Arrange: 割当付き draft run を用意
        run = self._draft_run()
        DailyTaskAssignment.objects.create(
            run=run,
            booking=self.booking,
            driver=self.driver,
            kind='pickup',
        )
        self.client.force_authenticate(self.owner.user)

        # Act: run 詳細を取得
        response = self.client.get(f'/api/business/routing/runs/{run.id}')

        # Assert: ドライバー単位でグループ化され不要フィールドが露出しないことを確認
        self.assertEqual(response.status_code, 200)
        run_data = response.data['run']
        self.assertEqual(run_data['assignment_count'], 1)
        self.assertEqual(len(run_data['assignment_groups']), 1)
        group = run_data['assignment_groups'][0]
        self.assertEqual(group['driver_id'], str(self.driver.id))
        task = group['tasks'][0]
        self.assertEqual(task['task_type'], 'pickup')
        self.assertNotIn('sequence', task)
        self.assertNotIn('booking_id', task)
        self.assertNotIn('latitude', task)
        self.assertNotIn('longitude', task)
        self.assertNotIn('routes', run_data)
        self.assertNotIn('created_at', run_data)
        self.assertNotIn('completed_at', run_data)
        self.assertNotIn('applied_at', run_data)
        self.assertEqual(
            set(run_data.keys()),
            {
                'id',
                'service_date',
                'status',
                'is_stale',
                'selected_driver_ids',
                'assignment_count',
                'assignment_groups',
                'unassigned_tasks',
            },
        )

    def test_apply_sets_pickup_driver_and_detects_stale_input(self):
        # Arrange: 適用対象の draft run と割当を用意
        run = self._draft_run()
        DailyTaskAssignment.objects.create(
            run=run,
            booking=self.booking,
            driver=self.driver,
            kind='pickup',
        )
        self.client.force_authenticate(self.owner.user)

        # Act: 割当を適用
        response = self.client.post(
            f'/api/business/routing/runs/{run.id}/apply', {}, format='json'
        )

        # Assert: 集荷ドライバーが設定されることを確認
        self.assertEqual(response.status_code, 200)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.pickup_driver, self.driver)
        self.assertIsNone(self.booking.driver)
        self.assertFalse(self.booking.pickup_manually_assigned)
        self.assertFalse(self.booking.delivery_manually_assigned)

        # Act: 同じ run を再適用
        repeated = self.client.post(
            f'/api/business/routing/runs/{run.id}/apply', {}, format='json'
        )

        # Assert: 再適用でも監査ログは1件のままであることを確認
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(
            BookingAuditLog.objects.filter(
                booking=self.booking,
                action=BookingAuditLog.ACTION_AUTO_ASSIGNED,
            ).count(),
            1,
        )

        # Act & Assert: 自動適用は手動固定ではないので、再 build しても付け替え可能のまま
        rebuilt = build_tasks(self.owner, self.booking.pickup_date)
        self.assertTrue(rebuilt.tasks)
        self.assertTrue(all(task.fixed_driver_id is None for task in rebuilt.tasks))

        # Arrange: 入力を変更して古い run を用意
        stale_run = self._draft_run()
        self.booking.pickup_latitude = '35.3'
        self.booking.save(update_fields=['pickup_latitude', 'updated_at'])

        # Act: 古い入力の run を適用
        stale_response = self.client.post(
            f'/api/business/routing/runs/{stale_run.id}/apply', {}, format='json'
        )

        # Assert: stale として拒否されることを確認
        self.assertEqual(stale_response.status_code, 409)
        self.assertTrue(stale_response.data['stale'])
        stale_run.refresh_from_db()
        self.assertEqual(stale_run.status, DailyAssignmentRun.Status.STALE)

    def _draft_run(self):
        """現在の入力スナップショットから draft run を作成"""
        problem = build_tasks(self.owner, self.booking.pickup_date)
        snapshot = problem.snapshot()
        return DailyAssignmentRun.objects.create(
            business_owner=self.owner,
            service_date=self.booking.pickup_date,
            status=DailyAssignmentRun.Status.DRAFT,
            input_hash=snapshot_hash(snapshot),
        )