"""
日次自動割当の入力データを組み立てるサービス

対象日の予約と利用可能な配達者をDBから取得し、割当処理で使用する
DriverInput・TaskInputへ変換する。割当に使えないタスクは理由付きで除外する。
"""

from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal
from typing import Optional

from bookings.models import LuggageBooking
from django.db.models import Q
from drivers.models import DriverProfile


@dataclass(frozen=True)
class DriverInput:
    id: str
    latitude: float
    longitude: float
    max_stops: Optional[int] = None
    shift_start_seconds: int = 9 * 60 * 60
    max_luggage_count: Optional[int] = None


@dataclass(frozen=True)
class TaskInput:
    id: str
    booking_id: str
    kind: str
    latitude: float
    longitude: float
    fixed_driver_id: Optional[str] = None
    pair_id: Optional[str] = None
    booking_number: str = ''
    location_name: str = ''
    location_address: str = ''
    luggage_count: int = 0


@dataclass
class AssignmentInput:
    service_date: date
    drivers: list[DriverInput]
    tasks: list[TaskInput]
    excluded: list[dict]

    def snapshot(self) -> dict:
        return {
            'service_date': self.service_date.isoformat(),
            'drivers': [asdict(driver) for driver in self.drivers],
            'tasks': [asdict(task) for task in self.tasks],
            'excluded': self.excluded,
        }


def _shift_start_seconds(driver: DriverProfile) -> int:
    if not driver.shift_start:
        return 9 * 60 * 60
    return (
        driver.shift_start.hour * 3600
        + driver.shift_start.minute * 60
        + driver.shift_start.second
    )


def _point(latitude: Optional[Decimal], longitude: Optional[Decimal]):
    if latitude is None or longitude is None:
        return None
    return float(latitude), float(longitude)


def _total_luggage_count(luggage_items) -> int:
    """荷物情報から合計個数を算出"""
    if not isinstance(luggage_items, dict):
        return 0
    total = 0
    for value in luggage_items.values():
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            total += int(value)
        elif isinstance(value, str) and value.isdigit():
            total += int(value)
    return total


def _manual_pickup_driver_id(booking) -> Optional[str]:
    """手動割当された集荷担当ID。自動割当のみの担当は固定しない。"""
    if booking.pickup_driver_id and booking.pickup_manually_assigned:
        return str(booking.pickup_driver_id)
    if (
        not booking.pickup_driver_id
        and booking.driver_id
        and booking.delivery_manually_assigned
    ):
        # 集荷担当未設定時は配達担当が集荷も兼ねる
        return str(booking.driver_id)
    return None


def _manual_delivery_driver_id(booking) -> Optional[str]:
    """手動割当された配達担当ID。自動割当のみの担当は固定しない。"""
    if booking.driver_id and booking.delivery_manually_assigned:
        return str(booking.driver_id)
    return None


def eligible_drivers(business_owner, service_date: date) -> list[DriverProfile]:
    """対象日の自動割当に参加できる配達者を返す"""
    weekday = service_date.weekday()
    candidates = (
        DriverProfile.objects.filter(
            business_owner=business_owner,
            is_available=True,
            departure_latitude__isnull=False,
            departure_longitude__isnull=False,
        )
        .select_related('user')
        .order_by('id')
    )

    eligible = []
    for driver in candidates:
        operating_days = driver.operating_days
        # operating_days は月〜日の7文字（'1'=稼働, '0'=休み）
        if len(operating_days) != 7:
            continue
        if operating_days[weekday] != '1':
            continue
        # 対象日の時点で免許証の有効期限が切れている場合は割当対象外
        if driver.license_expiry and driver.license_expiry < service_date:
            continue
        eligible.append(driver)
    return eligible


def build_tasks(
    business_owner,
    service_date: date,
    selected_driver_ids: Optional[set[str]] = None,
) -> AssignmentInput:
    """対象日の予約・配達者を割当用の入力データに変換"""
    drivers = []
    all_eligible_drivers = eligible_drivers(business_owner, service_date)
    eligible_driver_ids = {str(driver.id) for driver in all_eligible_drivers}
    for driver in all_eligible_drivers:
        # 候補が指定されている場合は、選ばれた配達者だけを割当対象にする
        if (
            selected_driver_ids is not None
            and str(driver.id) not in selected_driver_ids
        ):
            continue
        drivers.append(DriverInput(
            id=str(driver.id),
            latitude=float(driver.departure_latitude),
            longitude=float(driver.departure_longitude),
            max_stops=driver.max_daily_stops,
            shift_start_seconds=_shift_start_seconds(driver),
            max_luggage_count=driver.max_daily_luggage_count,
        ))

    known_driver_ids = {driver.id for driver in drivers}
    tasks: list[TaskInput] = []
    excluded: list[dict] = []
    bookings = LuggageBooking.objects.filter(
        business_owner=business_owner,
        delivery_status__in=['before_pickup', 'picked_up'],
    ).filter(
        Q(pickup_date=service_date) | Q(delivery_date=service_date)
    ).select_related('driver', 'pickup_driver')

    for booking in bookings:
        # 集荷は未集荷のときだけ対象。配達は配達日が一致すれば対象。
        pickup_due = (
            booking.pickup_date == service_date
            and booking.delivery_status == 'before_pickup'
        )
        delivery_due = booking.delivery_date == service_date
        if not pickup_due and not delivery_due:
            continue

        # 同日に集荷と配達がある場合だけペア化し、同じ配達者へ割り当てる
        pair_id = str(booking.id) if pickup_due and delivery_due else None
        luggage_count = _total_luggage_count(booking.luggage_items)
        definitions = []
        if pickup_due:
            definitions.append((
                'pickup',
                _point(booking.pickup_latitude, booking.pickup_longitude),
                _manual_pickup_driver_id(booking),
                booking.pickup_location_name_display,
                booking.pickup_location_address_display,
            ))
        if delivery_due:
            definitions.append((
                'delivery',
                _point(booking.delivery_latitude, booking.delivery_longitude),
                _manual_delivery_driver_id(booking),
                booking.delivery_location_name_display,
                booking.delivery_location_address_display,
            ))

        # ペアの一方でも座標が欠ける場合は、片方だけ割当できないのでまとめて除外
        if pair_id and any(definition[1] is None for definition in definitions):
            excluded.extend({
                'task_id': f'{booking.id}:{definition[0]}',
                'booking_id': str(booking.id),
                'booking_number': booking.booking_number,
                'task_type': definition[0],
                'reason': 'missing_coordinates',
            } for definition in definitions)
            continue

        fixed_ids = {
            str(definition[2]) for definition in definitions if definition[2]
        }
        # 同日ペアなのに集荷と配達で別の手動担当が付いている場合は割当不能
        if pair_id and len(fixed_ids) > 1:
            for kind, *_rest in definitions:
                excluded.append({
                    'task_id': f'{booking.id}:{kind}',
                    'booking_id': str(booking.id),
                    'booking_number': booking.booking_number,
                    'task_type': kind,
                    'reason': 'same_day_conflicting_fixed_assignments',
                })
            continue
        pair_fixed = next(iter(fixed_ids), None)

        for kind, point, fixed_id, location_name, location_address in definitions:
            task_id = f'{booking.id}:{kind}'
            if point is None:
                excluded.append({
                    'task_id': task_id,
                    'booking_id': str(booking.id),
                    'booking_number': booking.booking_number,
                    'task_type': kind,
                    'reason': 'missing_coordinates',
                })
                continue
            # ペア全体で共通の固定担当があればそれを優先する
            effective_fixed = pair_fixed or (str(fixed_id) if fixed_id else None)
            if effective_fixed and effective_fixed not in known_driver_ids:
                # 候補から外しただけか、そもそも利用不可かを理由で区別する
                excluded.append({
                    'task_id': task_id,
                    'booking_id': str(booking.id),
                    'booking_number': booking.booking_number,
                    'task_type': kind,
                    'reason': (
                        'fixed_driver_not_selected'
                        if (
                            selected_driver_ids is not None
                            and effective_fixed in eligible_driver_ids
                        )
                        else 'fixed_driver_unavailable_or_missing_location'
                    ),
                })
                continue
            tasks.append(TaskInput(
                id=task_id,
                booking_id=str(booking.id),
                kind=kind,
                latitude=point[0],
                longitude=point[1],
                fixed_driver_id=effective_fixed,
                pair_id=pair_id,
                booking_number=booking.booking_number,
                location_name=location_name,
                location_address=location_address,
                luggage_count=luggage_count,
            ))

    return AssignmentInput(service_date, drivers, tasks, excluded)
