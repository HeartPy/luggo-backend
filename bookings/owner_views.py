"""
事業者（ログイン済み）向けの予約一覧 API

公開（顧客向け）の予約 API とは分離し、必ずログイン中の事業者に紐づく予約のみを
操作対象とする。
"""
import csv
import io
import json
import logging
from datetime import date as date_type, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import stripe
from django.conf import settings
from django.db import transaction
from django.db.models import Case, IntegerField, Max, Q, QuerySet, Value, When
from django.db.models.functions import Least
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response

from project.utils import mask_sensitive_id
from drivers.models import DriverProfile
from .emails import send_booking_cancellation_emails
from .models import BookingAuditLog, LuggageBooking
from .refunds import log_booking_event, record_api_refund_result
from .serializers import OwnerBookingUpdateSerializer
from .transfers import create_transfer_for_delivered_booking


stripe.api_key = settings.STRIPE_SECRET_KEY
logger = logging.getLogger(__name__)

PAGE_SIZE = 20

# 配達状況の表示ラベル
STATUS_LABELS: dict[str, str] = dict(LuggageBooking.DELIVERY_STATUS_CHOICES)

# 事業者が画面から設定できる配達状況（キャンセルは専用ボタンで処理する）
EDITABLE_STATUSES = {'before_pickup', 'picked_up', 'delivered'}

LUGGAGE_KEYS = ['cabin', 'checked', 'oversize']
LUGGAGE_LABELS: dict[str, str] = {
    'cabin': '機内持ち込みサイズ',
    'checked': '受託手荷物サイズ',
    'oversize': '規格外サイズ',
}


def _total_luggage_count(luggage_items: Any) -> int:
    """荷物情報（JSON）から合計個数を算出"""
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


# ISO 3166-1 の国コード（alpha-2 / alpha-3 / numeric）→ 日本語の国名マッピング。
# 予約フォームで使用している i18n-iso-countries（日本語）と同じ表記に揃えるため、
# そのデータから生成した JSON を読み込む。
_COUNTRY_NAMES_PATH = Path(__file__).resolve().parent / 'data' / 'country_names_ja.json'


@lru_cache(maxsize=1)
def _country_names_ja() -> dict[str, str]:
    try:
        with _COUNTRY_NAMES_PATH.open(encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        logger.warning("国名マッピングの読み込みに失敗しました: %s", _COUNTRY_NAMES_PATH)
        return {}


def _nationality_label(code: Optional[str]) -> str:
    """
    国コード（ISO 3166-1 alpha-2/alpha-3/numeric）を日本語の国名に変換

    変換できない場合は元の値（なければ空文字）を返す。
    """
    if not code:
        return ''
    return _country_names_ja().get(code.strip().upper(), code)


def _serialize_booking(booking: LuggageBooking) -> dict[str, Any]:
    """予約一覧の行として返す情報を組み立て"""
    items = booking.luggage_items or {}
    return {
        'id': str(booking.id),
        'booking_number': booking.booking_number,
        'delivery_status': booking.delivery_status,
        'delivery_status_label': STATUS_LABELS.get(
            booking.delivery_status, booking.delivery_status
        ),
        'driver': str(booking.driver_id) if booking.driver_id else None,
        'driver_name': _driver_display_name(booking.driver) if booking.driver_id else None,
        'pickup_location_name': booking.pickup_location_name_display,
        'pickup_location_address': booking.pickup_location_address_display,
        'pickup_date': booking.pickup_date.isoformat() if booking.pickup_date else None,
        'delivery_location_name': booking.delivery_location_name_display,
        'delivery_location_address': booking.delivery_location_address_display,
        'delivery_date': booking.delivery_date.isoformat() if booking.delivery_date else None,
        'luggage_items': items,
        'total_luggage_count': _total_luggage_count(items),
        'total_amount': booking.total_amount,
        'customer_name': booking.customer_name,
        'customer_email': booking.customer_email,
        'customer_phone_number': booking.customer_phone_number,
        'customer_nationality': booking.customer_nationality,
        'customer_nationality_label': _nationality_label(booking.customer_nationality),
        'guest_name': booking.guest_name,
        'notes': booking.notes,
        'created_at': booking.created_at.isoformat() if booking.created_at else None,
        'can_cancel': booking.can_cancel(),
        'is_refundable_on_cancel': booking.is_refundable_on_cancel(),
    }


def _get_business_profile(request: Request):
    """ログイン中ユーザーの事業者プロフィールを返す"""
    if not hasattr(request.user, 'business_profile'):
        return None
    return request.user.business_profile


def _scoped_queryset(business_profile) -> QuerySet[LuggageBooking]:
    """対象事業者の予約のみを返すベースクエリ"""
    return LuggageBooking.objects.filter(
        business_owner=business_profile
    ).select_related('driver__user')


def _driver_display_name(driver: Optional[DriverProfile]) -> str:
    """配達者の表示名を返す"""
    if driver is None:
        return ''
    name = (driver.user.get_full_name() or '').strip() if driver.user_id else ''
    if name:
        return name
    if driver.company_name:
        return driver.company_name
    return str(driver.id)


def _ordered(queryset: QuerySet[LuggageBooking]) -> QuerySet[LuggageBooking]:
    """
    予約一覧の並び順を適用

    - 集荷日 or 配達日が「今日」かつ未完了（配達済・キャンセル以外）の予約（画面で
      背景が黄色になる）を最上部に表示する
    - 次に、配達日が過ぎていない（これからの）予約を上に、過ぎた予約を下に表示する
    - 各グループ内では、集荷日・配達日が近いもの順に並べる
    """
    today = timezone.localdate()
    return queryset.annotate(
        nearest_date=Least('pickup_date', 'delivery_date'),
        is_not_urgent=Case(
            When(
                ~Q(delivery_status__in=['delivered', 'cancelled'])
                & ~Q(delivery_date__lt=today)
                & (Q(pickup_date=today) | Q(delivery_date=today)),
                then=Value(0),
            ),
            default=Value(1),
            output_field=IntegerField(),
        ),
        is_delivery_past=Case(
            When(delivery_date__lt=today, then=Value(1)),
            default=Value(0),
            output_field=IntegerField(),
        ),
    ).order_by('is_not_urgent', 'is_delivery_past', 'nearest_date', 'pickup_date', 'created_at')


def _parse_month(value: Optional[str]) -> Optional[tuple[int, int]]:
    """"YYYY-MM" を (年, 月) に変換"""
    if not value:
        return None
    try:
        year_str, month_str = value.split('-')
        year = int(year_str)
        month = int(month_str)
    except (ValueError, TypeError, AttributeError):
        return None
    if 1 <= month <= 12:
        return year, month
    return None


def _add_month(year: int, month: int, delta: int) -> tuple[int, int]:
    """(年, 月) に delta か月加算した (年, 月) を返す"""
    index = (year * 12 + (month - 1)) + delta
    return index // 12, index % 12 + 1


def _month_start(ym: tuple[int, int]) -> date_type:
    return date_type(ym[0], ym[1], 1)


def _month_end(ym: tuple[int, int]) -> date_type:
    next_year, next_month = _add_month(ym[0], ym[1], 1)
    return date_type(next_year, next_month, 1) - timedelta(days=1)


def _format_month(ym: tuple[int, int]) -> str:
    return f'{ym[0]:04d}-{ym[1]:02d}'


def _default_month_range(business_profile) -> tuple[tuple[int, int], tuple[int, int]]:
    """デフォルトの年月範囲を返す。

    開始: 現在の年月の1か月前
    終了: 最も先（未来）の予約の年月。予約が無ければ現在の年月。
    終了が開始より前にならないようにクランプする。
    """
    today = timezone.localdate()
    from_ym = _add_month(today.year, today.month, -1)

    agg = _scoped_queryset(business_profile).aggregate(
        max_pickup=Max('pickup_date'),
        max_delivery=Max('delivery_date'),
    )
    candidates = [
        max_date
        for max_date in (agg['max_pickup'], agg['max_delivery'])
        if max_date
    ]
    if candidates:
        latest = max(candidates)
        to_ym = (latest.year, latest.month)
    else:
        to_ym = (today.year, today.month)

    if to_ym < from_ym:
        to_ym = from_ym

    return from_ym, to_ym


# 期間クイック絞り込みの有効な値
PERIOD_VALUES = {'today', 'tomorrow', 'day_after_tomorrow', 'week'}


def _resolve_period_range(period: str) -> Optional[tuple[date_type, date_type]]:
    """
    期間クイック絞り込みの値を (開始日, 終了日) に変換

    today: 今日 / tomorrow: 明日 / day_after_tomorrow: 明後日 /
    week: 今日から1週間（今日含む7日間）
    """
    if period not in PERIOD_VALUES:
        return None
    today = timezone.localdate()
    if period == 'today':
        return today, today
    if period == 'tomorrow':
        target = today + timedelta(days=1)
        return target, target
    if period == 'day_after_tomorrow':
        target = today + timedelta(days=2)
        return target, target
    # week
    return today, today + timedelta(days=6)


def _apply_filters(
    queryset: QuerySet[LuggageBooking],
    business_profile,
    request: Request,
) -> tuple[QuerySet[LuggageBooking], Optional[tuple[int, int]], Optional[tuple[int, int]], str]:
    """
    検索条件（年月範囲 or 期間・キーワード・配達状況）を適用

    日付の絞り込みは「年月範囲（month_from / month_to）」と
    「期間クイック（period: today/tomorrow/day_after_tomorrow/week）」のいずれか一方。
    period が有効な場合は年月範囲より優先し、年月範囲は適用しない。
    キーワード（keyword）は顧客名・集荷場所・配送場所のいずれかに部分一致する。
    実際に適用した (開始年月, 終了年月)と period を併せて返す。
    """
    period = (request.GET.get('period') or '').strip()
    period_range = _resolve_period_range(period)

    applied_from: Optional[tuple[int, int]] = None
    applied_to: Optional[tuple[int, int]] = None

    if period_range is not None:
        start, end = period_range
        queryset = queryset.filter(
            Q(pickup_date__range=(start, end))
            | Q(delivery_date__range=(start, end))
        )
    else:
        period = ''
        default_from, default_to = _default_month_range(business_profile)
        from_ym = _parse_month(request.GET.get('month_from')) or default_from
        to_ym = _parse_month(request.GET.get('month_to')) or default_to

        # 開始が終了より後なら入れ替える
        if from_ym > to_ym:
            from_ym, to_ym = to_ym, from_ym

        range_start = _month_start(from_ym)
        range_end = _month_end(to_ym)
        queryset = queryset.filter(
            Q(pickup_date__range=(range_start, range_end))
            | Q(delivery_date__range=(range_start, range_end))
        )
        applied_from = from_ym
        applied_to = to_ym

    # キーワード: 顧客名・集荷場所・配送場所のいずれかに部分一致
    keyword = (request.GET.get('keyword') or '').strip()
    if keyword:
        queryset = queryset.filter(
            Q(customer_name__icontains=keyword)
            | Q(pickup_location_name__icontains=keyword)
            | Q(pickup_location_name_ja__icontains=keyword)
            | Q(delivery_location_name__icontains=keyword)
            | Q(delivery_location_name_ja__icontains=keyword)
        )

    # 配達状況での絞り込み（有効な値のみ適用）
    status_value = (request.GET.get('status') or '').strip()
    if status_value in STATUS_LABELS:
        queryset = queryset.filter(delivery_status=status_value)

    return queryset, applied_from, applied_to, period


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def list_bookings(request: Request) -> Response:
    """予約一覧を取得（事業者スコープ・検索・並び替え・ページネーション）"""
    business_profile = _get_business_profile(request)
    if business_profile is None:
        return Response(
            {'errMsg': '事業者情報が見つかりません。'},
            status=status.HTTP_404_NOT_FOUND,
        )

    filtered, applied_from, applied_to, applied_period = _apply_filters(
        _scoped_queryset(business_profile), business_profile, request
    )
    queryset = _ordered(filtered)

    total_count = queryset.count()
    total_pages = max(1, (total_count + PAGE_SIZE - 1) // PAGE_SIZE)

    try:
        page = int(request.GET.get('page', '1'))
    except (ValueError, TypeError):
        page = 1
    page = max(1, min(page, total_pages))

    start = (page - 1) * PAGE_SIZE
    end = start + PAGE_SIZE
    bookings = list(queryset[start:end])

    return Response(
        {
            'results': [_serialize_booking(booking) for booking in bookings],
            'page': page,
            'page_size': PAGE_SIZE,
            'total_count': total_count,
            'total_pages': total_pages,
            'applied_month_from': _format_month(applied_from) if applied_from else '',
            'applied_month_to': _format_month(applied_to) if applied_to else '',
            'applied_period': applied_period,
        },
        status=status.HTTP_200_OK,
    )


@api_view(['PUT'])
@permission_classes([IsAuthenticated])
def update_booking_statuses(request: Request) -> Response:
    """配達状況の一括更新（本保存）"""
    business_profile = _get_business_profile(request)
    if business_profile is None:
        return Response(
            {'errMsg': '事業者情報が見つかりません。'},
            status=status.HTTP_404_NOT_FOUND,
        )

    updates = request.data.get('updates')
    if not isinstance(updates, dict):
        return Response(
            {'errMsg': 'updates はオブジェクトで指定してください。'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # 値の検証
    for booking_id, new_status in updates.items():
        if new_status not in EDITABLE_STATUSES:
            return Response(
                {'errMsg': f'無効な配達状況です: {new_status}'},
                status=status.HTTP_400_BAD_REQUEST,
            )

    updated_count = 0
    scoped = _scoped_queryset(business_profile)
    for booking_id, new_status in updates.items():
        try:
            booking = scoped.get(id=booking_id)
        except (LuggageBooking.DoesNotExist, ValueError, TypeError):
            continue
        # キャンセル済みの予約は状況変更の対象外
        if booking.delivery_status == 'cancelled':
            continue
        if booking.delivery_status != new_status:
            booking.delivery_status = new_status
            update_fields = ['delivery_status', 'updated_at']
            # 初めて配達完了になった日時を記録（売上集計の基準になる）
            if new_status == 'delivered' and booking.delivered_at is None:
                booking.delivered_at = timezone.now()
                update_fields.append('delivered_at')
            booking.save(update_fields=update_fields)
            updated_count += 1
        # 配達完了した予約は事業者への送金を実行（送金済み・失敗時は内部で判定）
        if booking.delivery_status == 'delivered':
            create_transfer_for_delivered_booking(booking)

    return Response(
        {'message': '配達状況を保存しました。', 'updated_count': updated_count},
        status=status.HTTP_200_OK,
    )


@api_view(['PUT'])
@permission_classes([IsAuthenticated])
def assign_booking_drivers(request: Request) -> Response:
    """配達者の一括割り当て（本保存）"""
    business_profile = _get_business_profile(request)
    if business_profile is None:
        return Response(
            {'errMsg': '事業者情報が見つかりません。'},
            status=status.HTTP_404_NOT_FOUND,
        )

    updates = request.data.get('updates')
    if not isinstance(updates, dict):
        return Response(
            {'errMsg': 'updates はオブジェクトで指定してください。'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # 指定された配達者IDが当該事業者のものであることを検証
    driver_ids = {
        str(value).strip()
        for value in updates.values()
        if value not in (None, '')
    }
    valid_driver_ids: set[str] = set()
    if driver_ids:
        valid_driver_ids = {
            str(driver_id)
            for driver_id in DriverProfile.objects.filter(
                business_owner=business_profile,
                id__in=driver_ids,
            ).values_list('id', flat=True)
        }
        invalid = driver_ids - valid_driver_ids
        if invalid:
            return Response(
                {'errMsg': '指定された配達者が見つかりません。'},
                status=status.HTTP_400_BAD_REQUEST,
            )

    updated_count = 0
    scoped = _scoped_queryset(business_profile)
    for booking_id, driver_value in updates.items():
        try:
            booking = scoped.get(id=booking_id)
        except (LuggageBooking.DoesNotExist, ValueError, TypeError):
            continue
        # キャンセル済みの予約は対象外
        if booking.delivery_status == 'cancelled':
            continue
        new_driver_id = (
            str(driver_value).strip() if driver_value not in (None, '') else None
        )
        current_driver_id = str(booking.driver_id) if booking.driver_id else None
        if current_driver_id != new_driver_id:
            booking.driver_id = new_driver_id
            booking.save(update_fields=['driver', 'updated_at'])
            updated_count += 1

    return Response(
        {'message': '配達者を保存しました。', 'updated_count': updated_count},
        status=status.HTTP_200_OK,
    )


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def list_drivers(request: Request) -> Response:
    """ログイン中の事業者に所属する配達者の一覧を返す（割り当て選択用）"""
    business_profile = _get_business_profile(request)
    if business_profile is None:
        return Response(
            {'errMsg': '事業者情報が見つかりません。'},
            status=status.HTTP_404_NOT_FOUND,
        )

    drivers = (
        DriverProfile.objects.filter(business_owner=business_profile)
        .select_related('user')
    )
    results = [
        {'id': str(driver.id), 'name': _driver_display_name(driver)}
        for driver in drivers
    ]
    results.sort(key=lambda item: item['name'])
    return Response({'results': results}, status=status.HTTP_200_OK)


@api_view(['PUT', 'PATCH'])
@permission_classes([IsAuthenticated])
def update_booking(request: Request, booking_id: str) -> Response:
    """予約の編集可能項目を更新（詳細ポップアップの「保存」）"""
    business_profile = _get_business_profile(request)
    if business_profile is None:
        return Response(
            {'errMsg': '事業者情報が見つかりません。'},
            status=status.HTTP_404_NOT_FOUND,
        )

    scoped = _scoped_queryset(business_profile)
    try:
        booking = scoped.get(id=booking_id)
    except (LuggageBooking.DoesNotExist, ValueError, TypeError):
        return Response(
            {'errMsg': '予約が見つかりません。'},
            status=status.HTTP_404_NOT_FOUND,
        )

    if booking.delivery_status == 'cancelled':
        return Response(
            {'errMsg': 'キャンセル済みの予約は編集できません。'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    serializer = OwnerBookingUpdateSerializer(
        booking,
        data=request.data,
        partial=True,
        context={'business_profile': business_profile},
    )
    if not serializer.is_valid():
        return Response(
            {'errMsg': '入力内容を確認してください。', 'valid_errs': serializer.errors},
            status=status.HTTP_400_BAD_REQUEST,
        )

    serializer.save()
    booking.refresh_from_db()

    if booking.delivery_status == 'delivered':
        # 初めて配達完了になった日時を記録（売上集計の基準になる）
        if booking.delivered_at is None:
            booking.delivered_at = timezone.now()
            booking.save(update_fields=['delivered_at', 'updated_at'])
        # 配達完了した予約は事業者への送金を実行（送金済み・失敗時は内部で判定）
        create_transfer_for_delivered_booking(booking)

    return Response(
        {'message': '予約を更新しました。', 'booking': _serialize_booking(booking)},
        status=status.HTTP_200_OK,
    )


def _refund_booking_payment(
    booking: LuggageBooking,
    source: str = BookingAuditLog.SOURCE_OWNER_API,
) -> bool:
    """
    予約に対応する Stripe 決済を全額返金する

    返金 API の結果（Refund ID・状態・金額）は予約レコードに保存し、監査ログにも
    記録する。これにより、この直後にアプリが落ちて delivery_status を更新できなくても、
    Webhook 側で予約状態を整合できる。

    冪等キー（idempotency_key）により、キャンセル API がリトライされても Stripe 上で
    二重返金は発生せず、同一の Refund が返るため状態も一貫する。
    """
    payment_intent_id = (booking.payment_intent_id or '').strip()
    if not payment_intent_id:
        logger.error(
            "返金不可: payment_intent_id が空です booking_id=%s",
            mask_sensitive_id(str(booking.id)),
        )
        return False

    try:
        refund = stripe.Refund.create(
            payment_intent=payment_intent_id,
            metadata={'booking_id': str(booking.id)},
            # 同一予約の二重返金を防ぐ（再試行時も同じ結果が返る）
            idempotency_key=f'booking_cancel_refund_{booking.id}',
        )
        # 返金結果を予約へ反映し監査ログへ記録（Webhook 整合処理との突き合わせ用）。
        # 保存失敗が返金成功の判定を覆さないよう、例外は握りつぶす。
        try:
            record_api_refund_result(booking, refund, source=source)
        except Exception:
            logger.exception(
                "返金結果の保存に失敗しました（返金自体は成功）: booking_id=%s",
                mask_sensitive_id(str(booking.id)),
            )
        return True
    except stripe.error.InvalidRequestError as e:
        # 既に返金済みの場合は成功扱いとし、キャンセル状態へ進める
        code = getattr(e, 'code', '') or ''
        message = str(e)
        if code == 'charge_already_refunded' or 'already been refunded' in message:
            logger.info(
                "既に返金済みのためスキップ: payment_intent_id=%s booking_id=%s",
                mask_sensitive_id(payment_intent_id),
                mask_sensitive_id(str(booking.id)),
            )
            return True
        logger.warning(
            "返金リクエストが不正: payment_intent_id=%s booking_id=%s error=%s",
            mask_sensitive_id(payment_intent_id),
            mask_sensitive_id(str(booking.id)),
            message,
        )
        return False
    except stripe.error.StripeError as e:
        logger.warning(
            "Stripe返金エラー: payment_intent_id=%s booking_id=%s error=%s",
            mask_sensitive_id(payment_intent_id),
            mask_sensitive_id(str(booking.id)),
            str(e),
        )
        return False


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def cancel_bookings(request: Request) -> Response:
    """
    選択した予約を一括キャンセルする

    集荷前（before_pickup）の予約のみキャンセル可能。集荷日前日23時より前の予約は
    必ず Stripe 決済を全額自動返金する。集荷日前日23時以降の予約については、リクエストの
    `refund` フラグ（事業者の選択）に従って返金有無を切り替える。返金を行う場合は、
    返金に成功した予約のみキャンセル状態にする。
    """
    business_profile = _get_business_profile(request)
    if business_profile is None:
        return Response(
            {'errMsg': '事業者情報が見つかりません。'},
            status=status.HTTP_404_NOT_FOUND,
        )

    ids = request.data.get('ids')
    if not isinstance(ids, list) or not ids:
        return Response(
            {'errMsg': 'キャンセルする予約を選択してください。'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    refund_choice = bool(request.data.get('refund', True))

    scoped = _scoped_queryset(business_profile)
    refunded_count = 0
    no_refund_count = 0
    skipped_count = 0
    refund_failed_count = 0
    for booking_id in ids:
        try:
            booking = scoped.get(id=booking_id)
        except (LuggageBooking.DoesNotExist, ValueError, TypeError):
            continue
        if not booking.can_cancel():
            skipped_count += 1
            continue
        # 集荷日前日23時より前は必ず返金。それ以降は事業者の選択に従う。
        should_refund = booking.is_refundable_on_cancel() or refund_choice
        if should_refund:
            # 先に返金を行い、成功した場合のみキャンセル状態にする
            # （返金失敗のまま「キャンセル済み」になると未返金が放置されるため）
            if not _refund_booking_payment(booking):
                refund_failed_count += 1
                continue
            refunded_count += 1
        else:
            no_refund_count += 1
        previous_delivery_status = booking.delivery_status
        booking.delivery_status = 'cancelled'
        booking.save(update_fields=['delivery_status', 'updated_at'])
        log_booking_event(
            booking=booking,
            action=BookingAuditLog.ACTION_BOOKING_CANCELLED,
            source=BookingAuditLog.SOURCE_OWNER_API,
            previous_delivery_status=previous_delivery_status,
            new_delivery_status='cancelled',
            new_refund_status=booking.refund_status,
            stripe_refund_id=booking.stripe_refund_id,
            amount=booking.refunded_amount if should_refund else 0,
            message=(
                '事業者による予約キャンセル（返金あり）。'
                if should_refund
                else '事業者による予約キャンセル（返金なし）。'
            ),
        )
        # キャンセル確定後に顧客・配達者へ通知メールを送信（メール失敗は
        # キャンセル処理自体には影響させない）。
        # 返金の有無に応じて顧客向けの文面を切り替える。
        transaction.on_commit(
            lambda b=booking, r=should_refund: send_booking_cancellation_emails(
                b, refunded=r
            )
        )

    cancelled_count = refunded_count + no_refund_count
    message_parts = []
    if refunded_count:
        message_parts.append(f'{refunded_count}件の予約をキャンセルし、返金しました。')
    if no_refund_count:
        message_parts.append(f'{no_refund_count}件の予約を返金せずにキャンセルしました。')
    if not message_parts:
        message_parts.append('キャンセルした予約はありませんでした。')
    if refund_failed_count:
        message_parts.append(
            f'{refund_failed_count}件は返金に失敗したため、キャンセルしていません。'
            'お手数ですが、時間をおいて再度お試しください。'
        )
    message = ''.join(message_parts)

    return Response(
        {
            'message': message,
            'cancelled_count': cancelled_count,
            'refunded_count': refunded_count,
            'no_refund_count': no_refund_count,
            'skipped_count': skipped_count,
            'refund_failed_count': refund_failed_count,
        },
        status=status.HTTP_200_OK,
    )


def _format_date(value: date_type | None) -> str:
    return value.isoformat() if value else ''


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def export_bookings_csv(request: Request) -> HttpResponse:
    """選択した予約を CSV で出力（全ての予約情報を含む）"""
    business_profile = _get_business_profile(request)
    if business_profile is None:
        return Response(
            {'errMsg': '事業者情報が見つかりません。'},
            status=status.HTTP_404_NOT_FOUND,
        )

    ids = request.data.get('ids')
    if not isinstance(ids, list) or not ids:
        return Response(
            {'errMsg': '出力する予約を選択してください。'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    bookings = list(
        _ordered(_scoped_queryset(business_profile).filter(id__in=ids))
    )

    output = io.StringIO()
    # Excel で文字化けしないよう BOM を付与
    output.write('\ufeff')
    writer = csv.writer(output)

    header = [
        '予約番号',
        '配達状況',
        '顧客名',
        'メールアドレス',
        '電話番号',
        '国籍',
        '宿泊予約者名',
        '集荷場所の名称',
        '集荷場所の住所',
        '集荷日',
        '配達場所の名称',
        '配達場所の住所',
        '配達日',
    ]
    header += [f'個数（{LUGGAGE_LABELS[key]}）' for key in LUGGAGE_KEYS]
    header += [
        '合計個数',
        '合計金額（円）',
        '備考',
        '予約ID',
        '作成日時',
        '更新日時',
    ]
    writer.writerow(header)

    for booking in bookings:
        items = booking.luggage_items or {}
        row = [
            booking.booking_number,
            STATUS_LABELS.get(booking.delivery_status, booking.delivery_status),
            booking.customer_name,
            booking.customer_email,
            booking.customer_phone_number,
            _nationality_label(booking.customer_nationality),
            booking.guest_name,
            booking.pickup_location_name_display,
            booking.pickup_location_address_display,
            _format_date(booking.pickup_date),
            booking.delivery_location_name_display,
            booking.delivery_location_address_display,
            _format_date(booking.delivery_date),
        ]
        for key in LUGGAGE_KEYS:
            value = items.get(key, 0) if isinstance(items, dict) else 0
            row.append(value)
        row += [
            _total_luggage_count(items),
            booking.total_amount,
            booking.notes,
            str(booking.id),
            timezone.localtime(booking.created_at).strftime('%Y-%m-%d %H:%M:%S')
            if booking.created_at else '',
            timezone.localtime(booking.updated_at).strftime('%Y-%m-%d %H:%M:%S')
            if booking.updated_at else '',
        ]
        writer.writerow(row)

    logger.info(
        "予約CSV出力: business_owner=%s, count=%s",
        mask_sensitive_id(str(business_profile.id)),
        len(bookings),
    )

    filename = f"bookings_{timezone.localtime().strftime('%Y%m%d_%H%M%S')}.csv"
    response = HttpResponse(
        output.getvalue(),
        content_type='text/csv; charset=utf-8',
    )
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response
