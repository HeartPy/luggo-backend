"""
配達者本人の最適化ルートを生成するサービス

ログイン中の配達者に割り当て済みの集荷・配達（対象日）を集め、
Google Routes API の距離行列と訪問順ソルバー（solver.py）を接続して、
出発地点から各訪問先を回り出発地点へ戻る最適ルートを組み立てる。

生成結果は DriverRouteResult に保存し、担当タスクのスナップショット
ハッシュが一致する限り再利用する。完了・キャンセルなどでタスクが減った
場合は保存済みルートから該当停留所を除いて順序を維持し、新規割当や
座標変更のときだけ Google API を呼び直す。
"""
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from typing import Any, Optional

from bookings.models import LuggageBooking
from django.conf import settings
from django.db.models import Q
from drivers.models import DriverProfile
from routing.models import DriverRouteResult

from .google_routes import (
    GoogleRoutesMatrixClient,
    resolve_driver_departure_time,
)
from .snapshots import snapshot_hash
from .solver import solve_driver_route
from .task_builder import DriverInput, TaskInput, _point, _shift_start_seconds


class DriverRouteError(RuntimeError):
    """ルート生成の前提条件を満たさない場合のエラー"""


@dataclass
class DriverTasks:
    """対象日の担当タスク一式（座標欠損などの除外理由付き）"""
    driver_input: DriverInput
    tasks: list[TaskInput]
    excluded: list[dict]

    def snapshot(self) -> dict:
        """入力ハッシュ計算用のスナップショット（担当・座標・除外の変化を検知）"""
        return {
            'driver': asdict(self.driver_input),
            'tasks': [asdict(task) for task in self.tasks],
            'excluded': self.excluded,
        }


def _driver_task_bookings(driver: DriverProfile, service_date: date):
    """対象日にこの配達者が担当する予約を返す"""
    # 集荷担当: pickup_driver が自分、または pickup_driver 未設定で driver が自分
    pickup_assigned = Q(pickup_driver=driver) | (
        Q(pickup_driver__isnull=True) & Q(driver=driver)
    )
    return (
        LuggageBooking.objects.filter(
            delivery_status__in=['before_pickup', 'picked_up'],
        )
        .filter(
            (Q(pickup_date=service_date) & pickup_assigned)
            | (Q(delivery_date=service_date) & Q(driver=driver))
        )
        .order_by('created_at')
    )


def build_driver_tasks(driver: DriverProfile, service_date: date) -> DriverTasks:
    """対象日にこの配達者が回る集荷・配達をソルバー入力へ変換"""
    if driver.departure_latitude is None or driver.departure_longitude is None:
        raise DriverRouteError(
            '出発地点の位置情報が未設定のため、ルートを生成できません。'
        )

    driver_input = DriverInput(
        id=str(driver.id),
        latitude=float(driver.departure_latitude),
        longitude=float(driver.departure_longitude),
        max_stops=driver.max_daily_stops,
        shift_start_seconds=_shift_start_seconds(driver),
        max_luggage_count=driver.max_daily_luggage_count,
    )

    tasks: list[TaskInput] = []
    excluded: list[dict] = []
    for booking in _driver_task_bookings(driver, service_date):
        pickup_due = (
            booking.pickup_date == service_date
            and booking.delivery_status == 'before_pickup'
            and (
                booking.pickup_driver_id == driver.id
                or (booking.pickup_driver_id is None and booking.driver_id == driver.id)
            )
        )
        delivery_due = (
            booking.delivery_date == service_date
            and booking.driver_id == driver.id
        )
        if not pickup_due and not delivery_due:
            continue

        # 同日に集荷・配達の両方を担当する場合はペア化（集荷→配達の順を保証）
        pair_id = str(booking.id) if pickup_due and delivery_due else None
        definitions = []
        if pickup_due:
            definitions.append((
                'pickup',
                _point(booking.pickup_latitude, booking.pickup_longitude),
                booking.pickup_location_name_display,
                booking.pickup_location_address_display,
            ))
        if delivery_due:
            definitions.append((
                'delivery',
                _point(booking.delivery_latitude, booking.delivery_longitude),
                booking.delivery_location_name_display,
                booking.delivery_location_address_display,
            ))

        # ペアの一方でも座標が欠ける場合は、順序制約を満たせないのでまとめて除外
        if pair_id and any(definition[1] is None for definition in definitions):
            excluded.extend({
                'task_id': f'{booking.id}:{definition[0]}',
                'booking_id': str(booking.id),
                'booking_number': booking.booking_number,
                'task_type': definition[0],
                'reason': 'missing_coordinates',
            } for definition in definitions)
            continue

        for kind, point, location_name, location_address in definitions:
            if point is None:
                excluded.append({
                    'task_id': f'{booking.id}:{kind}',
                    'booking_id': str(booking.id),
                    'booking_number': booking.booking_number,
                    'task_type': kind,
                    'reason': 'missing_coordinates',
                })
                continue
            tasks.append(TaskInput(
                id=f'{booking.id}:{kind}',
                booking_id=str(booking.id),
                kind=kind,
                latitude=point[0],
                longitude=point[1],
                pair_id=pair_id,
                booking_number=booking.booking_number,
                location_name=location_name,
                location_address=location_address,
            ))

    return DriverTasks(driver_input, tasks, excluded)


def build_driver_route(
    driver: DriverProfile,
    service_date: date,
    matrix_client: Optional[GoogleRoutesMatrixClient] = None,
    driver_tasks: Optional[DriverTasks] = None,
) -> dict[str, Any]:
    """
    配達者本人の最適化ルートを生成し、フロントの DriverDailyRoute 型に
    一致する dict を返す
    """
    if driver_tasks is None:
        driver_tasks = build_driver_tasks(driver, service_date)
    driver_input = driver_tasks.driver_input
    driver_name = (
        (driver.user.get_full_name() or '').strip() if driver.user_id else ''
    ) or str(driver.id)

    base = {
        'id': f'{driver.id}:{service_date.isoformat()}',
        'driver_id': str(driver.id),
        'driver_name': driver_name,
        'departure_latitude': driver_input.latitude,
        'departure_longitude': driver_input.longitude,
        'total_duration_seconds': 0,
        'total_distance_meters': 0,
        'stop_count': 0,
        'stops': [],
        'excluded': driver_tasks.excluded,
    }
    if not driver_tasks.tasks:
        return base

    departure_time = resolve_driver_departure_time(service_date, driver_input)
    points = [(driver_input.latitude, driver_input.longitude)] + [
        (task.latitude, task.longitude) for task in driver_tasks.tasks
    ]
    client = matrix_client or GoogleRoutesMatrixClient()
    matrix = client.compute(points, departure_time=departure_time)

    solved = solve_driver_route(
        driver_input,
        driver_tasks.tasks,
        matrix.durations,
        matrix.distances,
    )

    service_seconds = int(getattr(settings, 'ROUTING_SERVICE_SECONDS_PER_STOP', 300))
    stops: list[dict[str, Any]] = []
    arrival = departure_time
    for index, stop in enumerate(solved.stops):
        # 到着予定 = 前地点の出発時刻 + 移動時間（初回以外は作業時間も加算）
        arrival = arrival + timedelta(
            seconds=stop.duration_from_previous + (service_seconds if index > 0 else 0)
        )
        stops.append({
            'id': stop.task.id,
            'sequence': index + 1,
            'booking_id': stop.task.booking_id,
            'booking_number': stop.task.booking_number,
            'task_type': stop.task.kind,
            'location_name': stop.task.location_name,
            'location_address': stop.task.location_address,
            'latitude': stop.task.latitude,
            'longitude': stop.task.longitude,
            'planned_arrival_at': arrival.isoformat(),
            'travel_seconds_from_previous': stop.duration_from_previous,
            'distance_meters_from_previous': stop.distance_from_previous,
        })

    return {
        **base,
        'total_duration_seconds': solved.total_duration,
        'total_distance_meters': solved.total_distance,
        'stop_count': len(stops),
        'stops': stops,
    }


def _rounded_point(latitude: Any, longitude: Any) -> Optional[tuple[float, float]]:
    """緯度経度を小数6桁に丸めて比較用の組にする"""
    try:
        return (round(float(latitude), 6), round(float(longitude), 6))
    except (TypeError, ValueError):
        return None


def can_prune_saved_route(
    saved_route: dict[str, Any],
    driver_tasks: DriverTasks,
) -> bool:
    """
    現在の担当タスクが保存済み停留所の部分集合なら、順序を維持した
    間引きで足りる（Google API は不要）
    """
    stops = saved_route.get('stops')
    if not isinstance(stops, list):
        return False

    departure = _rounded_point(
        saved_route.get('departure_latitude'),
        saved_route.get('departure_longitude'),
    )
    current_departure = _rounded_point(
        driver_tasks.driver_input.latitude,
        driver_tasks.driver_input.longitude,
    )
    if departure is None or departure != current_departure:
        return False

    saved_by_id = {
        stop.get('id'): stop
        for stop in stops
        if isinstance(stop, dict) and isinstance(stop.get('id'), str)
    }
    for task in driver_tasks.tasks:
        stop = saved_by_id.get(task.id)
        if stop is None:
            return False
        stop_point = _rounded_point(stop.get('latitude'), stop.get('longitude'))
        task_point = _rounded_point(task.latitude, task.longitude)
        if stop_point is None or stop_point != task_point:
            return False
    return True


def prune_saved_route(
    saved_route: dict[str, Any],
    driver_tasks: DriverTasks,
) -> dict[str, Any]:
    """保存済みルートから完了・キャンセル分の停留所を除き、訪問順を維持する"""
    remaining_ids = {task.id for task in driver_tasks.tasks}
    original_stops = [
        stop for stop in (saved_route.get('stops') or [])
        if isinstance(stop, dict)
    ]
    # dict() でコピーし、後の sequence 更新で保存ルートを書き換えないようにする
    remaining_stops = [
        dict(stop) for stop in original_stops if stop.get('id') in remaining_ids
    ]
    for index, stop in enumerate(remaining_stops, start=1):
        stop['sequence'] = index

    service_seconds = int(getattr(settings, 'ROUTING_SERVICE_SECONDS_PER_STOP', 300))
    original_n = len(original_stops)
    remaining_n = len(remaining_stops)
    original_travel = sum(
        int(stop.get('travel_seconds_from_previous') or 0)
        for stop in original_stops
    )
    original_distance = sum(
        int(stop.get('distance_meters_from_previous') or 0)
        for stop in original_stops
    )
    remaining_travel = sum(
        int(stop.get('travel_seconds_from_previous') or 0)
        for stop in remaining_stops
    )
    remaining_distance = sum(
        int(stop.get('distance_meters_from_previous') or 0)
        for stop in remaining_stops
    )
    original_total_duration = int(saved_route.get('total_duration_seconds') or 0)
    original_total_distance = int(saved_route.get('total_distance_meters') or 0)
    # 停留所リストに無い「最終停留所→拠点」の戻り距離・時間を全体合計から切り出す
    return_distance = max(0, original_total_distance - original_distance)
    return_duration = max(
        0,
        original_total_duration - service_seconds * original_n - original_travel,
    )

    # 最後の停留所が元と同じなら、切り出した戻り距離・時間を合計に足せる
    last_original_id = original_stops[-1].get('id') if original_stops else None
    last_remaining_id = remaining_stops[-1].get('id') if remaining_stops else None
    include_return = bool(remaining_stops) and last_original_id == last_remaining_id

    if not remaining_stops:
        total_duration = 0
        total_distance = 0
    else:
        total_distance = remaining_distance + (
            return_distance if include_return else 0
        )
        total_duration = remaining_travel + service_seconds * remaining_n + (
            return_duration if include_return else 0
        )

    return {
        **saved_route,
        'stops': remaining_stops,
        'stop_count': remaining_n,
        'excluded': driver_tasks.excluded,
        'total_duration_seconds': total_duration,
        'total_distance_meters': total_distance,
    }


def get_or_build_driver_route(
    driver: DriverProfile,
    service_date: date,
    force: bool = False,
    matrix_client: Optional[GoogleRoutesMatrixClient] = None,
) -> dict[str, Any]:
    """
    保存済みルートがあり担当タスクに変化がなければそれを返す。
    完了・キャンセルなどでタスクが減っただけなら停留所を間引いて順序を維持する。
    新規割当・座標変更があるか force=True の場合のみ再生成して保存する。

    返り値には生成日時 `generated_at`（ISO文字列）を含む。
    間引きでは Google 再生成時刻を更新しない。
    """
    driver_tasks = build_driver_tasks(driver, service_date)
    input_hash = snapshot_hash(driver_tasks.snapshot())

    if not force:
        saved = DriverRouteResult.objects.filter(
            driver=driver, service_date=service_date
        ).first()
        if saved and isinstance(saved.route, dict):
            if saved.input_hash == input_hash:
                return {
                    **saved.route,
                    'generated_at': saved.generated_at.isoformat(),
                }
            if can_prune_saved_route(saved.route, driver_tasks):
                route = prune_saved_route(saved.route, driver_tasks)
                # auto_now の generated_at を動かさない
                DriverRouteResult.objects.filter(pk=saved.pk).update(
                    input_hash=input_hash,
                    route=route,
                )
                return {
                    **route,
                    'generated_at': saved.generated_at.isoformat(),
                }

    route = build_driver_route(
        driver,
        service_date,
        matrix_client=matrix_client,
        driver_tasks=driver_tasks,
    )
    result, _ = DriverRouteResult.objects.update_or_create(
        driver=driver,
        service_date=service_date,
        defaults={'input_hash': input_hash, 'route': route},
    )
    return {
        **route,
        'generated_at': result.generated_at.isoformat(),
    }
