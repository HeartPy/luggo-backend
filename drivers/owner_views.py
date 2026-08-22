"""
事業者（ログイン済み）向けの配達者管理 API

配達者一覧タブで使用する。必ずログイン中の事業者に紐づく配達者のみを
操作対象とする。
"""
import logging
from datetime import date as date_type, datetime, time as time_type
from typing import Any, Optional

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.db.models import Q, QuerySet
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response

from bookings.models import LuggageBooking
from business_owners.models import BusinessProfile
from business_owners.views import validate_file_type
from django.conf import settings
from users.validators import validate_password_strength
from project.geocoding import (
    GeocodingNotConfigured,
    GeocodingNotFound,
    GeocodingServiceError,
    geocode,
)
from project.utils import mask_sensitive_id
from .models import DriverInvitation, DriverProfile
from .utils import (
    create_invitation_token,
    invitation_expiry,
    send_driver_invitation_email,
    token_digest,
    verify_driver_invitation,
)


logger = logging.getLogger(__name__)
User = get_user_model()

PAGE_SIZE = 20

# 一覧・詳細で返す配達予定の最大件数
DETAIL_BOOKINGS_LIMIT = 30
# 過去の配達履歴の最大件数
PAST_DELIVERIES_LIMIT = 30
# 配達の割り当て候補の最大件数
ASSIGNABLE_BOOKINGS_LIMIT = 50

# 月別集計の対象月数（当月を含む直近12か月）
MONTHLY_STATS_MONTHS = 12

STATUS_LABELS: dict[str, str] = dict(LuggageBooking.DELIVERY_STATUS_CHOICES)


def _get_business_profile(request: Request) -> Optional[BusinessProfile]:
    """ログイン中の有効な事業者プロフィールを返す"""
    profile = getattr(request.user, 'business_profile', None)
    if profile is None or not profile.is_active:
        return None
    return profile


def _scoped_drivers(business_profile: BusinessProfile) -> QuerySet[DriverProfile]:
    """対象事業者の有効な配達者のみを返すベースクエリ"""
    return DriverProfile.objects.filter(
        business_owner=business_profile,
        is_active=True,
    ).select_related('user')


def _email_blocks_new_driver_invite(
    email: str,
    business_profile: BusinessProfile,
) -> bool:
    """
    このメールで新規配達者を招待できないか

    同じ事業者の利用停止中配達者なら再招待（再開）できるため False。
    """
    user = (
        User.objects.filter(email=email)
        .select_related('driver_profile')
        .first()
    )
    if user is None:
        return False
    profile = getattr(user, 'driver_profile', None)
    if (
        user.user_type == 'delivery_driver'
        and profile is not None
        and profile.business_owner_id == business_profile.id
        and not profile.is_active
    ):
        return False
    return True


def _inactive_driver_for_invite(
    email: str,
    business_profile: BusinessProfile,
) -> Optional[DriverProfile]:
    """再招待で再開できる、同じ事業者の利用停止中配達者を返す"""
    user = (
        User.objects.filter(
            email=email,
            user_type='delivery_driver',
        )
        .select_related('driver_profile')
        .first()
    )
    if user is None:
        return None
    profile = getattr(user, 'driver_profile', None)
    if (
        profile is None
        or profile.business_owner_id != business_profile.id
        or profile.is_active
    ):
        return None
    return profile


def _unassign_driver_from_active_bookings(
    business_profile: BusinessProfile,
    driver: DriverProfile,
) -> int:
    """未完了の集荷・配達からこの配達者の割り当てを外す"""
    scoped = LuggageBooking.objects.filter(
        business_owner=business_profile,
        delivery_status__in=['before_pickup', 'picked_up'],
    ).filter(Q(driver=driver) | Q(pickup_driver=driver))
    unassigned_count = 0
    for booking in scoped:
        update_fields = ['updated_at']
        if booking.driver_id == driver.id:
            booking.driver = None
            booking.delivery_manually_assigned = False
            update_fields.extend(['driver', 'delivery_manually_assigned'])
        if booking.pickup_driver_id == driver.id:
            booking.pickup_driver = None
            booking.pickup_manually_assigned = False
            update_fields.extend(['pickup_driver', 'pickup_manually_assigned'])
        booking.save(update_fields=update_fields)
        unassigned_count += 1
    return unassigned_count


def _driver_name(driver: DriverProfile) -> str:
    """配達者の表示名"""
    name = (driver.user.get_full_name() or '').strip() if driver.user_id else ''
    return name or str(driver.id)


def _departure_label(departure_address: str = '') -> str:
    """出発地点の表示ラベル"""
    return (departure_address or '').strip()


def _profile_picture_url(request: Request, driver: DriverProfile) -> Optional[str]:
    """プロフィール画像の絶対URL"""
    picture = driver.user.profile_picture if driver.user_id else None
    if not picture:
        return None
    try:
        return request.build_absolute_uri(picture.url)
    except ValueError:
        return None


def _add_month(year: int, month: int, delta: int) -> tuple[int, int]:
    index = (year * 12 + (month - 1)) + delta
    return index // 12, index % 12 + 1


def _month_key(value) -> str:
    local = timezone.localtime(value)
    return f'{local.year:04d}-{local.month:02d}'


def _booking_sales_amount(booking: LuggageBooking) -> int:
    """予約1件の売上額（返金があれば差し引く）"""
    total = int(booking.total_amount or 0)
    if booking.refund_status == LuggageBooking.REFUND_STATUS_SUCCEEDED:
        total -= min(total, int(booking.refunded_amount or 0))
    return total


def _driver_sales_share(booking: LuggageBooking, driver_id) -> int:
    """
    配達者に紐づく売上按分額

    集荷・配達が同一担当なら全額。分業時は折半（端数は配達担当へ）。
    関与していなければ 0。
    """
    if driver_id is None or booking.driver_id is None:
        return 0
    total = _booking_sales_amount(booking)
    delivery_id = booking.driver_id
    pickup_id = booking.pickup_driver_id or delivery_id
    if pickup_id == delivery_id:
        return total if driver_id == delivery_id else 0
    half = total // 2
    if driver_id == pickup_id:
        return half
    if driver_id == delivery_id:
        return total - half
    return 0


def _driver_job_count(booking: LuggageBooking, driver_id) -> int:
    """
    配達者に紐づく作業件数

    集荷・配達をそれぞれ1件として数える。両方担当なら2件。
    """
    if driver_id is None or booking.driver_id is None:
        return 0
    delivery_id = booking.driver_id
    pickup_id = booking.pickup_driver_id or delivery_id
    count = 0
    if driver_id == pickup_id:
        count += 1
    if driver_id == delivery_id:
        count += 1
    return count


def _apply_booking_sales_to_stats(
    stats_by_driver: dict[str, dict[str, int]],
    booking: LuggageBooking,
    *,
    allowed_driver_ids: Optional[set[str]] = None,
) -> None:
    """
    完了予約の件数・売上を担当配達者の集計へ反映

    件数は集荷・配達をそれぞれ1件（同一担当なら2件）。売上は分業時のみ折半。
    """
    if booking.driver_id is None:
        return
    total = _booking_sales_amount(booking)
    delivery_key = str(booking.driver_id)
    pickup_key = str(booking.pickup_driver_id or booking.driver_id)

    def credit(driver_key: str, sales: int, job_count: int) -> None:
        if allowed_driver_ids is not None and driver_key not in allowed_driver_ids:
            return
        bucket = stats_by_driver.setdefault(driver_key, {'count': 0, 'sales': 0})
        bucket['count'] += job_count
        bucket['sales'] += sales

    if pickup_key == delivery_key:
        credit(delivery_key, total, 2)
        return
    half = total // 2
    credit(pickup_key, half, 1)
    credit(delivery_key, total - half, 1)


def _serialize_booking_summary(
    booking: LuggageBooking,
    assigned_driver: Optional[DriverProfile] = None,
) -> dict[str, Any]:
    """配達者詳細で表示する予約の要約"""
    role = None
    attributed_sales = None
    if assigned_driver is not None:
        is_pickup = booking.effective_pickup_driver.id == assigned_driver.id if booking.effective_pickup_driver else False
        is_delivery = booking.driver_id == assigned_driver.id
        role = 'both' if is_pickup and is_delivery else 'pickup' if is_pickup else 'delivery'
        attributed_sales = _driver_sales_share(booking, assigned_driver.id)
    return {
        'id': str(booking.id),
        'booking_number': booking.booking_number,
        'delivery_status': booking.delivery_status,
        'delivery_status_label': STATUS_LABELS.get(
            booking.delivery_status, booking.delivery_status
        ),
        'pickup_location_name': booking.pickup_location_name_display,
        'pickup_date': booking.pickup_date.isoformat() if booking.pickup_date else None,
        'delivery_location_name': booking.delivery_location_name_display,
        'delivery_date': booking.delivery_date.isoformat() if booking.delivery_date else None,
        'total_amount': booking.total_amount,
        'attributed_sales': attributed_sales,
        'assignment_role': role,
        'pickup_driver_name': (
            _driver_name(booking.effective_pickup_driver)
            if booking.effective_pickup_driver else None
        ),
        'delivery_driver_name': (
            _driver_name(booking.driver) if booking.driver else None
        ),
    }


def _active_bookings(queryset: QuerySet[LuggageBooking]) -> QuerySet[LuggageBooking]:
    """これからの配達（未完了・未キャンセル・配達日が過ぎていない）"""
    today = timezone.localdate()
    return queryset.filter(
        delivery_status__in=['before_pickup', 'picked_up'],
        delivery_date__gte=today,
    )


def _monthly_stats(driver: DriverProfile) -> list[dict[str, Any]]:
    """直近12か月の月別集荷・配達件数・売上（配達完了日時ベース、新しい月順）"""
    today = timezone.localdate()
    start_year, start_month = _add_month(
        today.year, today.month, -(MONTHLY_STATS_MONTHS - 1)
    )
    start = timezone.make_aware(datetime(start_year, start_month, 1))

    rows = LuggageBooking.objects.filter(
        Q(driver=driver) | Q(pickup_driver=driver),
        delivery_status='delivered',
        delivered_at__gte=start,
    ).only(
        'delivered_at', 'driver_id', 'pickup_driver_id',
        'total_amount', 'refunded_amount', 'refund_status',
    )

    stats: dict[str, dict[str, int]] = {}
    for offset in range(MONTHLY_STATS_MONTHS):
        year, month = _add_month(today.year, today.month, -offset)
        stats[f'{year:04d}-{month:02d}'] = {'count': 0, 'sales': 0}

    for booking in rows:
        if booking.delivered_at is None:
            continue
        key = _month_key(booking.delivered_at)
        if key not in stats:
            continue
        job_count = _driver_job_count(booking, driver.id)
        if job_count == 0:
            continue
        # 集荷・配達をそれぞれ1件（両方担当なら2件）。売上は分業時のみ折半。
        stats[key]['count'] += job_count
        stats[key]['sales'] += _driver_sales_share(booking, driver.id)

    return [
        {'month': key, 'count': value['count'], 'sales': value['sales']}
        for key, value in sorted(stats.items(), reverse=True)
    ]


def _serialize_driver_row(
    request: Request,
    driver: DriverProfile,
    next_delivery_dates: dict[str, Optional[str]],
    month_stats: dict[str, dict[str, int]],
) -> dict[str, Any]:
    """配達者一覧の行として返す情報を組み立て"""
    driver_id = str(driver.id)
    stats = month_stats.get(driver_id, {'count': 0, 'sales': 0})
    return {
        'id': driver_id,
        'name': _driver_name(driver),
        'company_name': driver.company_name,
        'email': driver.user.email if driver.user_id else '',
        'departure_address': driver.departure_address,
        'departure_place_id': driver.departure_place_id,
        'departure_latitude': driver.departure_latitude,
        'departure_longitude': driver.departure_longitude,
        'shift_start': driver.shift_start.isoformat() if driver.shift_start else None,
        'max_daily_stops': driver.max_daily_stops,
        'max_daily_luggage_count': driver.max_daily_luggage_count,
        'license_expiry': (
            driver.license_expiry.isoformat() if driver.license_expiry else None
        ),
        'operating_days': driver.operating_days,
        'is_available': driver.is_available,
        'departure_label': _departure_label(driver.departure_address),
        'profile_picture_url': _profile_picture_url(request, driver),
        'next_delivery_date': next_delivery_dates.get(driver_id),
        'month_delivery_count': stats['count'],
        'month_sales': stats['sales'],
    }


def _serialize_driver_detail(request: Request, driver: DriverProfile) -> dict[str, Any]:
    """配達者詳細（ポップアップ）として返す情報を組み立て"""
    assigned = LuggageBooking.objects.filter(
        Q(driver=driver) | Q(pickup_driver=driver)
    ).select_related('driver__user', 'pickup_driver__user')

    upcoming = list(
        _active_bookings(assigned).order_by('pickup_date', 'delivery_date')[
            :DETAIL_BOOKINGS_LIMIT
        ]
    )
    past = list(
        LuggageBooking.objects.filter(
            Q(driver=driver) | Q(pickup_driver=driver),
            delivery_status='delivered',
        ).select_related('driver__user', 'pickup_driver__user').order_by(
            '-delivery_date', '-updated_at'
        )[:PAST_DELIVERIES_LIMIT]
    )

    # 割り当て候補: 事業者の未割り当てで、これからの配達
    assignable = list(
        _active_bookings(
            LuggageBooking.objects.filter(
                business_owner=driver.business_owner,
                driver__isnull=True,
                pickup_driver__isnull=True,
            )
        ).select_related('driver__user', 'pickup_driver__user').order_by(
            'pickup_date', 'delivery_date'
        )[:ASSIGNABLE_BOOKINGS_LIMIT]
    )

    user = driver.user
    return {
        'id': str(driver.id),
        'last_name': user.last_name if user else '',
        'first_name': user.first_name if user else '',
        'name': _driver_name(driver),
        'company_name': driver.company_name,
        'email': user.email if user else '',
        'departure_address': driver.departure_address,
        'departure_place_id': driver.departure_place_id,
        'departure_latitude': driver.departure_latitude,
        'departure_longitude': driver.departure_longitude,
        'shift_start': driver.shift_start.isoformat() if driver.shift_start else None,
        'max_daily_stops': driver.max_daily_stops,
        'max_daily_luggage_count': driver.max_daily_luggage_count,
        'license_expiry': (
            driver.license_expiry.isoformat() if driver.license_expiry else None
        ),
        'operating_days': driver.operating_days,
        'is_available': driver.is_available,
        'departure_label': _departure_label(driver.departure_address),
        'profile_picture_url': _profile_picture_url(request, driver),
        'created_at': driver.created_at.isoformat() if driver.created_at else None,
        'upcoming_deliveries': [
            _serialize_booking_summary(b, driver) for b in upcoming
        ],
        'past_deliveries': [
            _serialize_booking_summary(b, driver) for b in past
        ],
        'monthly_stats': _monthly_stats(driver),
        'assignable_bookings': [_serialize_booking_summary(b) for b in assignable],
    }


def _validate_driver_input(
    request: Request,
    *,
    current_user=None,
    business_profile: Optional[BusinessProfile] = None,
) -> tuple[dict[str, Any], dict[str, list[str]]]:
    """
    追加・編集フォームの入力を検証し、(正規化済みデータ, フィールド別エラー) を返す
    """
    errs: dict[str, list[str]] = {}

    last_name = str(request.data.get('last_name') or '').strip()
    first_name = str(request.data.get('first_name') or '').strip()
    if not last_name and not first_name:
        errs['last_name'] = ['名前を入力してください。']
    if len(last_name) > 150 or len(first_name) > 150:
        errs['last_name'] = ['名前は150文字以内で入力してください。']

    company_name = str(request.data.get('company_name') or '').strip()
    if len(company_name) > 200:
        errs['company_name'] = ['会社名は200文字以内で入力してください。']

    email = str(request.data.get('email') or '').strip().lower()
    if not email:
        errs['email'] = ['メールアドレスを入力してください。']
    else:
        try:
            validate_email(email)
        except DjangoValidationError:
            errs['email'] = ['有効なメールアドレスを入力してください。']
        else:
            duplicated = User.objects.filter(email=email)
            if current_user is not None:
                duplicated = duplicated.exclude(id=current_user.id)
            if duplicated.exists():
                if (
                    current_user is None
                    and business_profile is not None
                    and not _email_blocks_new_driver_invite(email, business_profile)
                ):
                    pass
                else:
                    errs['email'] = ['このメールアドレスは既に登録されています。']

    departure_address = str(request.data.get('departure_address') or '').strip()

    def parse_time(field: str):
        raw = request.data.get(field)
        if raw in (None, ''):
            return None
        try:
            return time_type.fromisoformat(str(raw))
        except ValueError:
            errs[field] = ['時刻の形式が正しくありません。']
            return None

    shift_start = parse_time('shift_start')

    license_expiry_raw = request.data.get('license_expiry')
    license_expiry = None
    if license_expiry_raw in (None, ''):
        errs['license_expiry'] = ['免許証有効期限を入力してください。']
    else:
        try:
            license_expiry = date_type.fromisoformat(str(license_expiry_raw))
        except ValueError:
            errs['license_expiry'] = ['日付の形式が正しくありません。']

    try:
        max_daily_stops_raw = request.data.get('max_daily_stops', 10)
        max_daily_stops: Optional[int]
        if max_daily_stops_raw in (None, ''):
            # 空欄は上限なし
            max_daily_stops = None
        else:
            max_daily_stops = int(max_daily_stops_raw)
            if not 1 <= max_daily_stops <= 500:
                raise ValueError
    except (ValueError, TypeError):
        max_daily_stops = 10
        errs['max_daily_stops'] = ['1から500の範囲で入力してください。']

    max_daily_luggage_count_raw = request.data.get('max_daily_luggage_count', 20)
    max_daily_luggage_count: Optional[int]
    if max_daily_luggage_count_raw in (None, ''):
        # 空欄は上限なし
        max_daily_luggage_count = None
    else:
        try:
            max_daily_luggage_count = int(max_daily_luggage_count_raw)
            if not 1 <= max_daily_luggage_count <= 9999:
                raise ValueError
        except (ValueError, TypeError):
            max_daily_luggage_count = 20
            errs['max_daily_luggage_count'] = ['1から9999の範囲で入力してください。']

    operating_days = str(request.data.get('operating_days') or '1111111')
    if len(operating_days) != 7 or any(day not in '01' for day in operating_days):
        operating_days = '1111111'
        errs['operating_days'] = ['定休日の指定が正しくありません。']
    is_available_raw = request.data.get('is_available', 'true')
    if isinstance(is_available_raw, bool):
        is_available = is_available_raw
    elif str(is_available_raw).lower() in ('true', '1'):
        is_available = True
    elif str(is_available_raw).lower() in ('false', '0'):
        is_available = False
    else:
        is_available = True
        errs['is_available'] = ['有効または無効を指定してください。']

    picture = request.FILES.get('profile_picture')
    if picture is not None:
        is_valid, picture_err = validate_file_type(picture)
        if not is_valid:
            errs['profile_picture'] = [picture_err or '画像ファイルが不正です。']
        elif picture.size > settings.LUGGO_MAX_FILE_SIZE:
            max_mb = settings.LUGGO_MAX_FILE_SIZE // (1024 * 1024)
            errs['profile_picture'] = [f'ファイルサイズは{max_mb}MB以下にしてください。']

    data = {
        'last_name': last_name,
        'first_name': first_name,
        'company_name': company_name,
        'email': email,
        'departure_address': departure_address,
        'departure_place_id': '',
        'departure_latitude': None,
        'departure_longitude': None,
        'shift_start': shift_start,
        'max_daily_stops': max_daily_stops,
        'max_daily_luggage_count': max_daily_luggage_count,
        'license_expiry': license_expiry,
        'operating_days': operating_days,
        'is_available': is_available,
        'picture': picture,
        'remove_picture': str(request.data.get('remove_picture') or '') == 'true',
    }
    return data, errs


def _resolve_departure_coordinates(
    data: dict[str, Any],
    current_driver: Optional[DriverProfile] = None,
) -> Optional[str]:
    """出発地点から信頼できる座標を取得して data に書き込む"""
    address = data['departure_address']
    if not address:
        data['departure_place_id'] = ''
        data['departure_latitude'] = None
        data['departure_longitude'] = None
        return None

    if (
        current_driver is not None
        and address == current_driver.departure_address
        and current_driver.departure_latitude is not None
        and current_driver.departure_longitude is not None
    ):
        data['departure_place_id'] = current_driver.departure_place_id
        data['departure_latitude'] = current_driver.departure_latitude
        data['departure_longitude'] = current_driver.departure_longitude
        return None

    try:
        result = geocode(address)
    except GeocodingNotFound:
        return '住所を特定できませんでした。番地まで含めて入力してください。'
    except GeocodingNotConfigured:
        return '住所の位置情報を取得できません。運営にお問い合わせください。'
    except GeocodingServiceError:
        return '住所の位置情報を取得できませんでした。時間をおいて再度お試しください。'

    data['departure_place_id'] = result.place_id
    data['departure_latitude'] = result.latitude
    data['departure_longitude'] = result.longitude
    return None


@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def manage_drivers(request: Request) -> Response:
    """配達者一覧の取得と配達者の追加"""
    business_profile = _get_business_profile(request)
    if business_profile is None:
        return Response(
            {'errMsg': '事業者情報が見つかりません。'},
            status=status.HTTP_404_NOT_FOUND,
        )

    if request.method == 'POST':
        return _create_driver(request, business_profile)

    return _list_drivers(request, business_profile)


def _month_stats_for_drivers(
    driver_ids: list,
    *,
    today: Optional[date_type] = None,
) -> dict[str, dict[str, int]]:
    """指定配達者の今月の集荷・配達件数・売上を集計"""
    if not driver_ids:
        return {}
    if today is None:
        today = timezone.localdate()
    month_start = timezone.make_aware(datetime(today.year, today.month, 1))
    month_stats: dict[str, dict[str, int]] = {}
    allowed_ids = {str(driver_id) for driver_id in driver_ids}
    delivered = LuggageBooking.objects.filter(
        Q(driver_id__in=driver_ids) | Q(pickup_driver_id__in=driver_ids),
        delivery_status='delivered',
        delivered_at__gte=month_start,
    ).only(
        'driver_id', 'pickup_driver_id',
        'total_amount', 'refunded_amount', 'refund_status',
    )
    for booking in delivered:
        _apply_booking_sales_to_stats(
            month_stats, booking, allowed_driver_ids=allowed_ids
        )
    return month_stats


def _list_drivers(request: Request, business_profile: BusinessProfile) -> Response:
    """配達者一覧（名前・会社名・出発地点での検索＋ページネーション）"""
    queryset = _scoped_drivers(business_profile).select_related('user')

    keyword = (request.GET.get('keyword') or '').strip()
    if keyword:
        queryset = queryset.filter(
            Q(user__last_name__icontains=keyword)
            | Q(user__first_name__icontains=keyword)
            | Q(company_name__icontains=keyword)
            | Q(departure_address__icontains=keyword)
        )

    sort = (request.GET.get('sort') or 'created').strip()
    if sort not in ('created', 'sales', 'count'):
        sort = 'created'

    total_count = queryset.count()
    total_pages = max(1, (total_count + PAGE_SIZE - 1) // PAGE_SIZE)
    try:
        page = int(request.GET.get('page', '1'))
    except (ValueError, TypeError):
        page = 1
    page = max(1, min(page, total_pages))
    today = timezone.localdate()

    if sort in ('sales', 'count'):
        # 売上・件数はページ外でも正しい順になるよう、全件集計してから並べ替える
        all_drivers = list(queryset)
        month_stats = _month_stats_for_drivers(
            [driver.id for driver in all_drivers],
            today=today,
        )
        metric = 'sales' if sort == 'sales' else 'count'
        all_drivers.sort(
            key=lambda driver: (
                -month_stats.get(str(driver.id), {}).get(metric, 0),
                # 同点時は新しい登録を上に
                -(driver.created_at.timestamp() if driver.created_at else 0),
            )
        )
        offset = (page - 1) * PAGE_SIZE
        drivers = all_drivers[offset:offset + PAGE_SIZE]
        page_stats = {
            str(driver.id): month_stats.get(
                str(driver.id), {'count': 0, 'sales': 0}
            )
            for driver in drivers
        }
    else:
        # 登録順（新しい配達者を上）
        queryset = queryset.order_by('-created_at', '-id')
        drivers = list(queryset[(page - 1) * PAGE_SIZE:page * PAGE_SIZE])
        page_stats = _month_stats_for_drivers(
            [driver.id for driver in drivers],
            today=today,
        )

    driver_ids = [driver.id for driver in drivers]

    # 直近の配達予定日（配達者ごとに最も近い集荷日/配達日）
    next_delivery_dates: dict[str, Optional[str]] = {}
    active = _active_bookings(
        LuggageBooking.objects.filter(
            Q(driver_id__in=driver_ids) | Q(pickup_driver_id__in=driver_ids)
        )
    ).values_list(
        'driver_id', 'pickup_driver_id', 'pickup_date', 'delivery_date'
    )

    def record_nearest(driver_id, target_date) -> None:
        if driver_id is None or not isinstance(target_date, date_type):
            return
        if target_date < today:
            return
        key = str(driver_id)
        nearest = target_date.isoformat()
        current = next_delivery_dates.get(key)
        if current is None or nearest < current:
            next_delivery_dates[key] = nearest

    for delivery_driver_id, pickup_driver_id, pickup_date, delivery_date in active:
        effective_pickup_id = pickup_driver_id or delivery_driver_id
        record_nearest(effective_pickup_id, pickup_date)
        record_nearest(delivery_driver_id, delivery_date)

    return Response(
        {
            'results': [
                _serialize_driver_row(request, driver, next_delivery_dates, page_stats)
                for driver in drivers
            ],
            'page': page,
            'page_size': PAGE_SIZE,
            'total_count': total_count,
            'total_pages': total_pages,
            'sort': sort,
        },
        status=status.HTTP_200_OK,
    )


def _create_driver(request: Request, business_profile: BusinessProfile) -> Response:
    """配達者登録の確認メールを送信（承認されるまでUserは作成しない）"""
    data, errs = _validate_driver_input(request, business_profile=business_profile)
    if errs:
        return Response(
            {'errMsg': '入力内容を確認してください。', 'valid_errs': errs},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # 同じ事業者・メールへの連続送信を抑止
    recent_cutoff = timezone.now() - timezone.timedelta(minutes=5)
    if DriverInvitation.objects.filter(
        business_owner=business_profile,
        email=data['email'],
        created_at__gte=recent_cutoff,
    ).exists():
        return Response(
            {'errMsg': '確認メールは5分後に再送信できます。'},
            status=status.HTTP_429_TOO_MANY_REQUESTS,
        )

    geocoding_err = _resolve_departure_coordinates(data)
    if geocoding_err:
        return Response(
            {
                'errMsg': '入力内容を確認してください。',
                'valid_errs': {'departure_address': [geocoding_err]},
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    raw_token, digest = create_invitation_token()
    with transaction.atomic():
        # 再送時は、同じ事業者・メールの古い未使用リンクを無効化
        previous_invitations = list(DriverInvitation.objects.filter(
            business_owner=business_profile,
            email=data['email'],
            used_at__isnull=True,
        ))
        for previous in previous_invitations:
            if previous.profile_picture:
                previous.profile_picture.delete(save=False)
            previous.used_at = timezone.now()
            previous.profile_picture = None
            previous.save(
                update_fields=['used_at', 'profile_picture']
            )

        invitation = DriverInvitation.objects.create(
            business_owner=business_profile,
            email=data['email'],
            last_name=data['last_name'],
            first_name=data['first_name'],
            company_name=data['company_name'],
            departure_address=data['departure_address'],
            departure_place_id=data['departure_place_id'],
            departure_latitude=data['departure_latitude'],
            departure_longitude=data['departure_longitude'],
            shift_start=data['shift_start'],
            max_daily_stops=data['max_daily_stops'],
            max_daily_luggage_count=data['max_daily_luggage_count'],
            license_expiry=data['license_expiry'],
            operating_days=data['operating_days'],
            is_available=data['is_available'],
            profile_picture=data['picture'],
            token_digest=digest,
            expires_at=invitation_expiry(),
        )

    if not send_driver_invitation_email(invitation, raw_token):
        # メール未送信の招待を残さない。画像ファイルも同時に削除。
        if invitation.profile_picture:
            invitation.profile_picture.delete(save=False)
        invitation.delete()
        return Response(
            {'errMsg': '登録確認メールの送信に失敗しました。時間をおいて再度お試しください。'},
            status=status.HTTP_502_BAD_GATEWAY,
        )

    logger.info(
        "配達者登録確認メールを送信: business_owner=%s, invitation=%s",
        mask_sensitive_id(str(business_profile.id)),
        mask_sensitive_id(str(invitation.id)),
    )
    return Response(
        {
            'message': '登録確認メールを送信しました。メール内のリンクが承認されると配達者が登録されます。',
        },
        status=status.HTTP_202_ACCEPTED,
    )


@api_view(['GET', 'PUT', 'DELETE'])
@permission_classes([IsAuthenticated])
def manage_driver_detail(request: Request, driver_id: str) -> Response:
    """配達者の詳細取得・更新・利用停止"""
    business_profile = _get_business_profile(request)
    if business_profile is None:
        return Response(
            {'errMsg': '事業者情報が見つかりません。'},
            status=status.HTTP_404_NOT_FOUND,
        )

    try:
        driver = _scoped_drivers(business_profile).get(id=driver_id)
    except (DriverProfile.DoesNotExist, ValueError, TypeError):
        return Response(
            {'errMsg': '配達者が見つかりません。'},
            status=status.HTTP_404_NOT_FOUND,
        )

    if request.method == 'GET':
        return Response(
            {'driver': _serialize_driver_detail(request, driver)},
            status=status.HTTP_200_OK,
        )

    if request.method == 'DELETE':
        driver_masked = mask_sensitive_id(str(driver.id))
        with transaction.atomic():
            _unassign_driver_from_active_bookings(business_profile, driver)
            driver.is_active = False
            driver.save()
        logger.info(
            "配達者を利用停止: business_owner=%s, driver=%s",
            mask_sensitive_id(str(business_profile.id)),
            driver_masked,
        )
        return Response({'message': '配達者を削除しました。'}, status=status.HTTP_200_OK)

    # PUT: 更新
    data, errs = _validate_driver_input(request, current_user=driver.user)
    if not errs:
        geocoding_err = _resolve_departure_coordinates(data, driver)
        if geocoding_err:
            errs['departure_address'] = [geocoding_err]
    if errs:
        return Response(
            {'errMsg': '入力内容を確認してください。', 'valid_errs': errs},
            status=status.HTTP_400_BAD_REQUEST,
        )

    with transaction.atomic():
        user = driver.user
        user.last_name = data['last_name']
        user.first_name = data['first_name']
        user.email = data['email']
        update_fields = ['last_name', 'first_name', 'email', 'updated_at']
        if data['picture'] is not None:
            user.profile_picture = data['picture']
            update_fields.append('profile_picture')
        elif data['remove_picture'] and user.profile_picture:
            user.profile_picture.delete(save=False)
            user.profile_picture = None
            update_fields.append('profile_picture')
        user.save(update_fields=update_fields)

        driver.company_name = data['company_name']
        driver.departure_address = data['departure_address']
        driver.departure_place_id = data['departure_place_id']
        driver.departure_latitude = data['departure_latitude']
        driver.departure_longitude = data['departure_longitude']
        driver.shift_start = data['shift_start']
        driver.max_daily_stops = data['max_daily_stops']
        driver.max_daily_luggage_count = data['max_daily_luggage_count']
        driver.license_expiry = data['license_expiry']
        driver.operating_days = data['operating_days']
        driver.is_available = data['is_available']
        driver.save(update_fields=[
            'company_name',
            'departure_address', 'departure_place_id', 'departure_latitude',
            'departure_longitude', 'shift_start',
            'max_daily_stops', 'max_daily_luggage_count',
            'license_expiry', 'operating_days', 'updated_at',
            'is_available',
        ])

    return Response(
        {
            'message': '配達者を更新しました。',
            'driver': _serialize_driver_detail(request, driver),
        },
        status=status.HTTP_200_OK,
    )


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def assign_bookings_to_driver(request: Request, driver_id: str) -> Response:
    """選択した予約をこの配達者に割り当てる"""
    business_profile = _get_business_profile(request)
    if business_profile is None:
        return Response(
            {'errMsg': '事業者情報が見つかりません。'},
            status=status.HTTP_404_NOT_FOUND,
        )

    try:
        driver = _scoped_drivers(business_profile).get(id=driver_id)
    except (DriverProfile.DoesNotExist, ValueError, TypeError):
        return Response(
            {'errMsg': '配達者が見つかりません。'},
            status=status.HTTP_404_NOT_FOUND,
        )

    booking_ids = request.data.get('booking_ids')
    if not isinstance(booking_ids, list) or not booking_ids:
        return Response(
            {'errMsg': '割り当てる配達を選択してください。'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    assigned_count = 0
    scoped = LuggageBooking.objects.filter(business_owner=business_profile)
    for booking_id in booking_ids:
        try:
            booking = scoped.get(id=booking_id)
        except (LuggageBooking.DoesNotExist, ValueError, TypeError):
            continue
        # キャンセル済み・配達済みの予約は割り当て対象外
        if booking.delivery_status in ('cancelled', 'delivered'):
            continue
        if (
            str(booking.driver_id or '') != str(driver.id)
            or booking.pickup_driver_id is not None
            or not booking.delivery_manually_assigned
        ):
            booking.driver = driver
            # 配達者詳細からの割り当ては通常割り当て（集荷・配達の両方）に戻す
            booking.pickup_driver = None
            booking.pickup_manually_assigned = False
            booking.delivery_manually_assigned = True
            booking.save(
                update_fields=[
                    'driver',
                    'pickup_driver',
                    'pickup_manually_assigned',
                    'delivery_manually_assigned',
                    'updated_at',
                ]
            )
            assigned_count += 1

    return Response(
        {
            'message': f'{assigned_count}件の配達を割り当てました。',
            'assigned_count': assigned_count,
            'driver': _serialize_driver_detail(request, driver),
        },
        status=status.HTTP_200_OK,
    )


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def unassign_bookings_from_driver(request: Request, driver_id: str) -> Response:
    """選択した未完了の配達について、この配達者への割り当てを解除"""
    business_profile = _get_business_profile(request)
    if business_profile is None:
        return Response(
            {'errMsg': '事業者情報が見つかりません。'},
            status=status.HTTP_404_NOT_FOUND,
        )

    try:
        driver = _scoped_drivers(business_profile).get(id=driver_id)
    except (DriverProfile.DoesNotExist, ValueError, TypeError):
        return Response(
            {'errMsg': '配達者が見つかりません。'},
            status=status.HTTP_404_NOT_FOUND,
        )

    booking_ids = request.data.get('booking_ids')
    if not isinstance(booking_ids, list) or not booking_ids:
        return Response(
            {'errMsg': '割り当てを解除する配達を選択してください。'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # ログイン中の事業者・対象配達者・未完了の予約だけを更新
    scoped = LuggageBooking.objects.filter(
        business_owner=business_profile,
        id__in=booking_ids,
        delivery_status__in=['before_pickup', 'picked_up'],
    ).filter(Q(driver=driver) | Q(pickup_driver=driver))
    unassigned_count = 0
    for booking in scoped:
        update_fields = ['updated_at']
        if booking.driver_id == driver.id:
            booking.driver = None
            booking.delivery_manually_assigned = False
            update_fields.extend(['driver', 'delivery_manually_assigned'])
        if booking.pickup_driver_id == driver.id:
            booking.pickup_driver = None
            booking.pickup_manually_assigned = False
            update_fields.extend(['pickup_driver', 'pickup_manually_assigned'])
        booking.save(update_fields=update_fields)
        unassigned_count += 1

    return Response(
        {
            'message': f'{unassigned_count}件の配達の割り当てを解除しました。',
            'unassigned_count': unassigned_count,
            'driver': _serialize_driver_detail(request, driver),
        },
        status=status.HTTP_200_OK,
    )


def _serialize_invitation(invitation: DriverInvitation) -> dict[str, Any]:
    """承認画面に表示してよい招待情報だけを返す"""
    return {
        'email': invitation.email,
        'name': f'{invitation.last_name} {invitation.first_name}'.strip(),
        'company_name': invitation.company_name,
        'departure_label': _departure_label(invitation.departure_address),
        'license_expiry': invitation.license_expiry.isoformat(),
        'owner_company_name': invitation.business_owner.company_name,
        'expires_at': invitation.expires_at.isoformat(),
    }


@api_view(['GET'])
@permission_classes([AllowAny])
def verify_driver_invitation_api(request: Request) -> Response:
    """メール内リンクのトークンを検証し、承認対象の概要を返す"""
    token = (request.GET.get('token') or '').strip()
    invitation = verify_driver_invitation(token)
    if (
        invitation is None
        or not invitation.business_owner.is_active
        or _email_blocks_new_driver_invite(
            invitation.email, invitation.business_owner
        )
    ):
        return Response(
            {'valid': False, 'errMsg': 'この確認リンクは無効か、有効期限が切れています。'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    return Response(
        {'valid': True, 'invitation': _serialize_invitation(invitation)},
        status=status.HTTP_200_OK,
    )


@api_view(['POST'])
@permission_classes([AllowAny])
def accept_driver_invitation(request: Request) -> Response:
    """配達者本人の承認後にUserとDriverProfileをセットで作成"""
    token = str(request.data.get('token') or '').strip()
    if not token:
        return Response(
            {'errMsg': 'リンクが正しくありません。メールに記載のリンクから再度アクセスしてください。'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    password = str(request.data.get('password') or '')
    password_confirm = str(request.data.get('password_confirm') or '')
    password_errs = {}
    password_err = validate_password_strength(password)
    if password_err:
        password_errs['password'] = [password_err]
    if password != password_confirm:
        password_errs['password_confirm'] = ['パスワードが一致しません。']
    if password_errs:
        return Response(
            {
                'errMsg': '入力内容を確認してください。',
                'valid_errs': password_errs,
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        with transaction.atomic():
            try:
                invitation = (
                    DriverInvitation.objects.select_for_update()
                    .select_related('business_owner')
                    .get(token_digest=token_digest(token))
                )
            except DriverInvitation.DoesNotExist:
                invitation = None

            if (
                invitation is None
                or not invitation.is_valid()
                or not invitation.business_owner.is_active
            ):
                return Response(
                    {'errMsg': 'この確認リンクは無効か、有効期限が切れています。'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            existing_driver = _inactive_driver_for_invite(
                invitation.email, invitation.business_owner
            )
            if (
                existing_driver is None
                and User.objects.filter(email=invitation.email).exists()
            ):
                return Response(
                    {'errMsg': 'このメールアドレスは既に登録されています。'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if existing_driver is not None:
                user = existing_driver.user
                user.set_password(password)
                user.last_name = invitation.last_name
                user.first_name = invitation.first_name
                user.is_active = True
                user_update_fields = [
                    'password', 'last_name', 'first_name', 'is_active', 'updated_at',
                ]
                if invitation.profile_picture:
                    user.profile_picture = invitation.profile_picture.name
                    user_update_fields.append('profile_picture')
                user.save(update_fields=user_update_fields)

                existing_driver.company_name = invitation.company_name
                existing_driver.departure_address = invitation.departure_address
                existing_driver.departure_place_id = invitation.departure_place_id
                existing_driver.departure_latitude = invitation.departure_latitude
                existing_driver.departure_longitude = invitation.departure_longitude
                existing_driver.shift_start = invitation.shift_start
                existing_driver.max_daily_stops = invitation.max_daily_stops
                existing_driver.max_daily_luggage_count = (
                    invitation.max_daily_luggage_count
                )
                existing_driver.license_expiry = invitation.license_expiry
                existing_driver.operating_days = invitation.operating_days
                existing_driver.is_available = invitation.is_available
                existing_driver.is_active = True
                existing_driver.save()
                driver = existing_driver
            else:
                user = User.objects.create_user(
                    email=invitation.email,
                    password=password,
                    user_type='delivery_driver',
                    last_name=invitation.last_name,
                    first_name=invitation.first_name,
                )
                if invitation.profile_picture:
                    user.profile_picture = invitation.profile_picture.name
                    user.save(update_fields=['profile_picture'])

                driver = DriverProfile.objects.create(
                    user=user,
                    business_owner=invitation.business_owner,
                    company_name=invitation.company_name,
                    departure_address=invitation.departure_address,
                    departure_place_id=invitation.departure_place_id,
                    departure_latitude=invitation.departure_latitude,
                    departure_longitude=invitation.departure_longitude,
                    shift_start=invitation.shift_start,
                    max_daily_stops=invitation.max_daily_stops,
                    max_daily_luggage_count=invitation.max_daily_luggage_count,
                    license_expiry=invitation.license_expiry,
                    operating_days=invitation.operating_days,
                    is_available=invitation.is_available,
                )
            invitation.mark_as_used()
    except IntegrityError:
        return Response(
            {'errMsg': 'このメールアドレスは既に登録されています。'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    logger.info(
        '配達者登録を承認: driver=%s',
        mask_sensitive_id(str(driver.id)),
    )
    return Response(
        {'message': '配達者登録を承認しました。'},
        status=status.HTTP_201_CREATED,
    )
