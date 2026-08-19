"""
配達者本人（ログイン済み）向けのダッシュボード API

配達者ダッシュボードで使用する。必ずログイン中の配達者本人に
割り当てられた予約のみを対象とする。
"""
import logging
from datetime import date as date_type
from typing import Any, Optional

from django.core.paginator import EmptyPage, Paginator
from django.db.models import Count, Q, QuerySet
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response

from bookings.models import LuggageBooking
from routing.services.driver_route import (
    DriverRouteError,
    get_or_build_driver_route,
)
from routing.services.google_routes import RoutesAPIError
from routing.services.solver import SolverError
from routing.services.task_builder import _total_luggage_count

from .models import DriverProfile

logger = logging.getLogger(__name__)

PAGE_SIZE = 10

STATUS_KEYS = [choice[0] for choice in LuggageBooking.DELIVERY_STATUS_CHOICES]


def _get_driver_profile(request: Request) -> Optional[DriverProfile]:
    """ログイン中ユーザーの配達者プロフィールを返す"""
    if getattr(request.user, 'user_type', None) != 'delivery_driver':
        return None
    if not hasattr(request.user, 'driver_profile'):
        return None
    return request.user.driver_profile


def _service_date(request: Request) -> Optional[date_type]:
    try:
        return date_type.fromisoformat(str(request.GET.get('date') or ''))
    except ValueError:
        return None


def _scoped_bookings(
    driver: DriverProfile,
    service_date: date_type,
) -> QuerySet[LuggageBooking]:
    """対象日にこの配達者が担当する予約（キャンセル含む全ステータス）"""
    pickup_assigned = Q(pickup_driver=driver) | (
        Q(pickup_driver__isnull=True) & Q(driver=driver)
    )
    return LuggageBooking.objects.filter(
        (Q(pickup_date=service_date) & pickup_assigned)
        | (Q(delivery_date=service_date) & Q(driver=driver))
    ).order_by('created_at')


def _location_data(booking: LuggageBooking, kind: str) -> dict[str, Any]:
    """集荷・配送場所をカード表示用のdictに変換"""
    if kind == 'pickup':
        latitude = booking.pickup_latitude
        longitude = booking.pickup_longitude
        return {
            'name': booking.pickup_location_name_display,
            'address': booking.pickup_location_address_display,
            'lat': float(latitude) if latitude is not None else None,
            'lng': float(longitude) if longitude is not None else None,
            'date': booking.pickup_date.isoformat(),
        }
    latitude = booking.delivery_latitude
    longitude = booking.delivery_longitude
    return {
        'name': booking.delivery_location_name_display,
        'address': booking.delivery_location_address_display,
        'lat': float(latitude) if latitude is not None else None,
        'lng': float(longitude) if longitude is not None else None,
        'date': booking.delivery_date.isoformat(),
    }


def _booking_data(booking: LuggageBooking, driver: DriverProfile) -> dict[str, Any]:
    """予約1件をカード表示用のdictに変換"""
    is_pickup_assignee = (
        booking.pickup_driver_id == driver.id
        or (booking.pickup_driver_id is None and booking.driver_id == driver.id)
    )
    return {
        'id': str(booking.id),
        'booking_number': booking.booking_number,
        'delivery_status': booking.delivery_status,
        'pickup': _location_data(booking, 'pickup'),
        'delivery': _location_data(booking, 'delivery'),
        'luggage_count': _total_luggage_count(booking.luggage_items),
        'luggage_items': booking.luggage_items or {},
        'customer_name': booking.customer_name,
        'guest_name': booking.guest_name,
        'customer_phone_number': booking.customer_phone_number,
        'notes': booking.notes or '',
        'is_pickup_assignee': is_pickup_assignee,
        'is_delivery_assignee': booking.driver_id == driver.id,
    }


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def my_bookings(request: Request) -> Response:
    """ログイン中の配達者が担当する予約一覧を返す"""
    driver = _get_driver_profile(request)
    if driver is None:
        return Response(
            {'errMsg': '配達者情報が見つかりません。'},
            status=status.HTTP_403_FORBIDDEN,
        )

    service_date = _service_date(request)
    if service_date is None:
        return Response(
            {'errMsg': '日付の形式が正しくありません。'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    status_filter = str(request.GET.get('status') or 'all')
    if status_filter not in ('all', *STATUS_KEYS):
        return Response(
            {'errMsg': 'ステータスの指定が正しくありません。'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    base_queryset = _scoped_bookings(driver, service_date)

    # タブごとの件数（ステータス絞り込み前の全件から集計）
    counts_by_status = dict(
        base_queryset.order_by().values_list('delivery_status').annotate(Count('id'))
    )
    status_counts = {
        key: int(counts_by_status.get(key, 0)) for key in STATUS_KEYS
    }
    status_counts['all'] = sum(status_counts.values())

    queryset = base_queryset
    if status_filter != 'all':
        queryset = queryset.filter(delivery_status=status_filter)

    paginator = Paginator(queryset, PAGE_SIZE)
    try:
        page_number = max(1, int(request.GET.get('page', 1)))
    except (TypeError, ValueError):
        page_number = 1
    try:
        page = paginator.page(page_number)
    except EmptyPage:
        page = paginator.page(paginator.num_pages)

    return Response({
        'results': [_booking_data(booking, driver) for booking in page.object_list],
        'page': page.number,
        'page_size': PAGE_SIZE,
        'total_count': paginator.count,
        'total_pages': paginator.num_pages,
        'status_counts': status_counts,
    })


# 手書きサイン（PNGのdata URL）の上限サイズ（文字数、約375KBのPNGに相当）
SIGNATURE_MAX_LENGTH = 500_000
SIGNATURE_PREFIX = 'data:image/png;base64,'

# 金額入力（手数料・交通費）の上限
FEE_MAX_VALUE = 10_000_000


def _booking_detail_data(booking: LuggageBooking, driver: DriverProfile) -> dict[str, Any]:
    """予約1件を詳細ダイアログ表示用のdictに変換"""
    return {
        **_booking_data(booking, driver),
        'picked_up_at': (
            booking.picked_up_at.isoformat() if booking.picked_up_at else None
        ),
        'delivered_at': (
            booking.delivered_at.isoformat() if booking.delivered_at else None
        ),
        'facility_fee': booking.facility_fee,
        'transport_cost': booking.transport_cost,
        'delivery_signature': booking.delivery_signature,
    }


def _validate_fee(value: Any) -> tuple[bool, Optional[int]]:
    """手数料・交通費の入力値を検証（None または 0以上の整数）"""
    if value is None:
        return True, None
    if isinstance(value, bool) or not isinstance(value, int):
        return False, None
    if value < 0 or value > FEE_MAX_VALUE:
        return False, None
    return True, value


def _handle_pickup_complete(
    booking: LuggageBooking, driver: DriverProfile,
) -> Optional[Response]:
    """集荷完了アクション。エラー時はResponseを返す"""
    is_pickup_assignee = (
        booking.pickup_driver_id == driver.id
        or (booking.pickup_driver_id is None and booking.driver_id == driver.id)
    )
    if not is_pickup_assignee:
        return Response(
            {'errMsg': 'この予約の集荷担当ではないため、操作できません。'},
            status=status.HTTP_403_FORBIDDEN,
        )
    if booking.delivery_status != 'before_pickup':
        return Response(
            {'errMsg': '集荷前の予約のみ集荷完了にできます。'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    booking.delivery_status = 'picked_up'
    booking.picked_up_at = timezone.now()
    booking.save(update_fields=['delivery_status', 'picked_up_at', 'updated_at'])
    return None


def _handle_deliver_complete(
    booking: LuggageBooking, driver: DriverProfile, signature: Any,
) -> Optional[Response]:
    """配達完了アクション（手書きサイン必須）。エラー時はResponseを返す"""
    if booking.driver_id != driver.id:
        return Response(
            {'errMsg': 'この予約の配達担当ではないため、操作できません。'},
            status=status.HTTP_403_FORBIDDEN,
        )
    if booking.delivery_status != 'picked_up':
        return Response(
            {'errMsg': '集荷済の予約のみ配達完了にできます。'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if (
        not isinstance(signature, str)
        or not signature.startswith(SIGNATURE_PREFIX)
        or len(signature) <= len(SIGNATURE_PREFIX)
    ):
        return Response(
            {'errMsg': '配達完了には手書きサインが必要です。'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if len(signature) > SIGNATURE_MAX_LENGTH:
        return Response(
            {'errMsg': '手書きサインのデータが大きすぎます。'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    booking.delivery_status = 'delivered'
    booking.delivery_signature = signature
    update_fields = ['delivery_status', 'delivery_signature', 'updated_at']
    # 事業者側のステータス変更と同じ流儀で、未設定のときのみ配達完了日時を記録
    if booking.delivered_at is None:
        booking.delivered_at = timezone.now()
        update_fields.append('delivered_at')
    booking.save(update_fields=update_fields)
    return None


def _handle_fees_update(booking: LuggageBooking, data: Any) -> Optional[Response]:
    """手数料・交通費の保存。エラー時はResponseを返す"""
    update_fields: list[str] = []
    for field in ('facility_fee', 'transport_cost'):
        if field not in data:
            continue
        is_valid, value = _validate_fee(data.get(field))
        if not is_valid:
            return Response(
                {'errMsg': '金額は0以上の整数で入力してください。'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        setattr(booking, field, value)
        update_fields.append(field)
    if update_fields:
        booking.save(update_fields=[*update_fields, 'updated_at'])
    return None


@api_view(['GET', 'PATCH'])
@permission_classes([IsAuthenticated])
def my_booking_detail(request: Request, booking_id: str) -> Response:
    """
    ログイン中の配達者が担当する予約1件の詳細取得・更新

    PATCHは以下を受け付ける:
    - 集荷完了（集荷前 → 集荷済）
    - 配達完了（集荷済 → 配達済、手書きサイン必須）
    - 費用の保存（任意入力）

    集荷完了日時・配達完了日時・保存済みサインはサーバー側でのみ設定する。
    """
    driver = _get_driver_profile(request)
    if driver is None:
        return Response(
            {'errMsg': '配達者情報が見つかりません。'},
            status=status.HTTP_403_FORBIDDEN,
        )

    booking = LuggageBooking.objects.filter(
        Q(id=booking_id) & (Q(driver=driver) | Q(pickup_driver=driver))
    ).first()
    if booking is None:
        return Response(
            {'errMsg': '予約が見つかりません。'},
            status=status.HTTP_404_NOT_FOUND,
        )

    if request.method == 'PATCH':
        action = request.data.get('action')
        if action == 'pickup_complete':
            err = _handle_pickup_complete(booking, driver)
        elif action == 'deliver_complete':
            err = _handle_deliver_complete(
                booking, driver, request.data.get('signature'),
            )
        elif action is None:
            err = _handle_fees_update(booking, request.data)
        else:
            err = Response(
                {'errMsg': '操作の指定が正しくありません。'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if err is not None:
            return err

    return Response({'booking': _booking_detail_data(booking, driver)})


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def my_route(request: Request) -> Response:
    """
    ログイン中の配達者の最適化ルートを返す

    担当タスクに変化がなければ保存済みルートを再利用する。
    完了・キャンセルなどでタスクが減っただけなら停留所を間引く。
    `refresh=1` を指定すると強制的に再生成する（再生成ボタン用）。
    """
    driver = _get_driver_profile(request)
    if driver is None:
        return Response(
            {'errMsg': '配達者情報が見つかりません。'},
            status=status.HTTP_403_FORBIDDEN,
        )

    service_date = _service_date(request)
    if service_date is None:
        return Response(
            {'errMsg': '日付の形式が正しくありません。'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    force = str(request.GET.get('refresh') or '') == '1'

    try:
        route = get_or_build_driver_route(driver, service_date, force=force)
    except DriverRouteError as exc:
        return Response(
            {'errMsg': str(exc)},
            status=status.HTTP_400_BAD_REQUEST,
        )
    except (RoutesAPIError, SolverError):
        logger.exception(
            '配達者ルート生成に失敗しました: driver_id=%s, date=%s',
            driver.id, service_date,
        )
        return Response(
            {'errMsg': 'ルートの生成に失敗しました。しばらく時間をおいて再度お試しください。'},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    return Response({'route': route})
