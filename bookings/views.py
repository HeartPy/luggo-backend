from rest_framework import generics, status, permissions
from rest_framework.response import Response
from rest_framework.decorators import api_view, permission_classes, authentication_classes
from rest_framework.request import Request
from django.db.models import Q, QuerySet
from django.db import transaction, IntegrityError
from django.conf import settings
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.http import HttpResponse
from typing import Any, Optional
from uuid import UUID
from datetime import date as date_type, datetime as dt_datetime, timezone as dt_timezone
import requests
import stripe
import logging
import re
import time

from project.utils import mask_sensitive_id
from business_owners.models import BusinessProfile
from business_owners.stripe_info import sync_stripe_review_status
from .models import BookingAuditLog, LuggageBooking, PendingBooking, ChargeDispute
from .owner_views import _refund_booking_payment
from .refunds import (
    claim_webhook_event,
    log_booking_event,
    mark_webhook_event_processed,
    normalize_refund_status,
    reconcile_booking_refund,
)
from .serializers import LuggageBookingSerializer, LuggageBookingCreateSerializer
from .emails import (
    build_issuer_snapshot,
    send_booking_cancellation_emails,
    send_booking_confirmation_email,
    send_charge_dispute_alert,
    send_unmatched_payment_alert,
)
from .receipts import build_receipt_pdf, receipt_filename


stripe.api_key = settings.STRIPE_SECRET_KEY
logger = logging.getLogger(__name__)

# 都道府県名とコードのマッピング
_PREF_MAP: dict[str, str] = {
    '01': '北海道', '02': '青森県', '03': '岩手県', '04': '宮城県',
    '05': '秋田県', '06': '山形県', '07': '福島県', '08': '茨城県',
    '09': '栃木県', '10': '群馬県', '11': '埼玉県', '12': '千葉県',
    '13': '東京都', '14': '神奈川県', '15': '新潟県', '16': '富山県',
    '17': '石川県', '18': '福井県', '19': '山梨県', '20': '長野県',
    '21': '岐阜県', '22': '静岡県', '23': '愛知県', '24': '三重県',
    '25': '滋賀県', '26': '京都府', '27': '大阪府', '28': '兵庫県',
    '29': '奈良県', '30': '和歌山県', '31': '鳥取県', '32': '島根県',
    '33': '岡山県', '34': '広島県', '35': '山口県', '36': '徳島県',
    '37': '香川県', '38': '愛媛県', '39': '高知県', '40': '福岡県',
    '41': '佐賀県', '42': '長崎県', '43': '熊本県', '44': '大分県',
    '45': '宮崎県', '46': '鹿児島県', '47': '沖縄県',
}

# 郵便番号の先頭2桁から都道府県コードへのマッピング
_POSTAL_PREFIX_TO_PREF: dict[str, str] = {
    '00': '01', '04': '01', '05': '01', '06': '01', '07': '01',
    '08': '01', '09': '01',
    '03': '02', '02': '03', '98': '04', '01': '05', '99': '06',
    '96': '07', '97': '07',
    '30': '08', '31': '08',
    '32': '09',
    '37': '10',
    '33': '11', '34': '11', '35': '11', '36': '11',
    '26': '12', '27': '12', '28': '12', '29': '12',
    '10': '13', '11': '13', '12': '13', '13': '13', '14': '13',
    '15': '13', '16': '13', '17': '13', '18': '13', '19': '13',
    '20': '13',
    '21': '14', '22': '14', '23': '14', '24': '14', '25': '14',
    '94': '15', '95': '15',
    '93': '16',
    '92': '17',
    '91': '18',
    '40': '19',
    '38': '20', '39': '20',
    '50': '21',
    '41': '22', '42': '22', '43': '22',
    '44': '23', '45': '23', '46': '23', '47': '23', '48': '23',
    '49': '23',
    '51': '24',
    '52': '25',
    '60': '26', '61': '26',
    '53': '27', '54': '27', '55': '27', '56': '27', '57': '27',
    '58': '27', '59': '27',
    '65': '28', '66': '28', '67': '28', '62': '28',
    '63': '29',
    '64': '30',
    '68': '31',
    '69': '32',
    '70': '33', '71': '33',
    '72': '34', '73': '34',
    '74': '35', '75': '35',
    '77': '36',
    '76': '37',
    '79': '38',
    '78': '39',
    '80': '40', '81': '40', '82': '40', '83': '40',
    '84': '41',
    '85': '42',
    '86': '43',
    '87': '44',
    '88': '45',
    '89': '46',
    '90': '47',
}


def pref_code_from_postal(postal_code: str) -> Optional[str]:
    """郵便番号の先頭2桁から都道府県コードを返す"""
    if not postal_code or len(postal_code) < 2:
        return None
    return _POSTAL_PREFIX_TO_PREF.get(postal_code[:2])


def calculate_server_total_amount(
    business_profile: BusinessProfile,
    delivery_postal_code: str,
    luggage_counts: dict[str, int],
) -> int:
    """事業者の pricing_rules と配達先郵便番号、荷物個数からサーバー側で合計金額を再計算する。

    フロントから送られてくる total_amount を信用せず、決済前に必ずこの関数で計算した値と
    突合する事で、フロントのキャッシュ古化や改ざんによる不正金額決済を防止する。
    """
    pricing_rules: dict[str, dict[str, Any]] = business_profile.pricing_rules or {}
    delivery_pref = pref_code_from_postal(delivery_postal_code) if delivery_postal_code else None
    pref_prices: dict[str, Any] = pricing_rules.get(delivery_pref, {}) if delivery_pref else {}

    total = 0
    for key, count in luggage_counts.items():
        raw_price = pref_prices.get(key, 0) or 0
        try:
            price = int(raw_price)
        except (ValueError, TypeError):
            continue
        try:
            qty = int(count)
        except (ValueError, TypeError):
            continue
        if price <= 0 or qty <= 0:
            continue
        total += price * qty
    return total


@api_view(['GET'])
@permission_classes([permissions.AllowAny])
def daily_remaining(request: Request) -> Response:
    """指定日の荷物残り受付可能数を返す"""
    try:
        business_owner_id = request.GET.get('business_owner')
        target_date_str = request.GET.get('date')

        if not business_owner_id or not target_date_str:
            return Response(
                {'errMsg': 'business_owner と date は必須です。'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            business_profile = BusinessProfile.objects.get(id=business_owner_id)
        except (BusinessProfile.DoesNotExist, ValueError):
            return Response(
                {'errMsg': '事業者が見つかりません。'},
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            target_date = date_type.fromisoformat(target_date_str)
        except (ValueError, TypeError):
            return Response(
                {'errMsg': '日付の形式が正しくありません。'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        daily_max = business_profile.daily_max_luggage
        if daily_max < 0:
            return Response(
                {
                    'daily_max': -1,
                    'existing_total': 0,
                    'remaining': -1,
                },
                status=status.HTTP_200_OK,
            )

        existing_bookings = LuggageBooking.objects.filter(
            Q(pickup_date=target_date) | Q(delivery_date=target_date),
            business_owner=business_profile,
        ).exclude(delivery_status='cancelled')

        existing_total = 0
        for booking in existing_bookings:
            items = booking.luggage_items or {}
            existing_total += sum(
                int(value)
                for value in items.values()
                if isinstance(value, (int, float, str)) and str(value).isdigit()
            )

        remaining = max(0, daily_max - existing_total)
        return Response(
            {
                'daily_max': daily_max,
                'existing_total': existing_total,
                'remaining': remaining,
            },
            status=status.HTTP_200_OK,
        )

    except Exception as e:
        logger.error("daily_remaining error: %s", str(e), exc_info=True)
        return Response(
            {'errMsg': str(e)},
            status=status.HTTP_400_BAD_REQUEST,
        )


@api_view(['GET'])
@permission_classes([permissions.AllowAny])
def luggage_items(request: Request) -> Response:
    """事業者の料金設定に基づいて荷物情報を取得"""
    try:
        # リクエストパラメータから取得
        business_owner_id = request.GET.get('business_owner')
        if not business_owner_id:
            return Response(
                {'errMsg': '事業者が指定されていません。'},
                status=status.HTTP_400_BAD_REQUEST
            )

        delivery_postal_code = request.GET.get('delivery_postal_code', '')

        # 事業者を取得
        try:
            business_owner = BusinessProfile.objects.get(id=business_owner_id)
        except BusinessProfile.DoesNotExist:
            return Response(
                {'errMsg': '指定された事業者が見つかりません。'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # 都道府県コードを取得（配送先郵便番号から）
        delivery_prefecture_code = pref_code_from_postal(delivery_postal_code)

        # 荷物の種類の定義
        LUGGAGE_TYPES = [
            {
                'id': 1,
                'name': '機内持ち込みサイズ（3辺計：〜120cm）',
                'key': 'cabin',
                'image_src': 'backpack.svg',
            },
            {
                'id': 2,
                'name': '受託手荷物サイズ（3辺計：〜160cm）',
                'key': 'checked',
                'image_src': 'suitcase.svg',
            },
            {
                'id': 3,
                'name': '規格外サイズ（3辺計：〜180cm）',
                'key': 'oversize',
                'image_src': 'guitar-case.svg',
            },
        ]

        pricing_rules = business_owner.pricing_rules or {}

        # 配達先都道府県の料金設定を取得
        pref_prices = {}
        if delivery_prefecture_code and delivery_prefecture_code in pricing_rules:
            pref_prices = pricing_rules[delivery_prefecture_code]

        # 配達可能な荷物タイプのみ返す（料金が設定されているもの）
        items = []
        for item in LUGGAGE_TYPES:
            key = item['key']
            price = pref_prices.get(key)

            if price is None or price == 0:
                continue

            items.append({
                'id': item['id'],
                'name': item['name'],
                'price': int(price),
                'key': item['key'],
                'image_src': item['image_src'],
            })

        return Response(
            {'items': items},
            status=status.HTTP_200_OK
        )
    except Exception as e:
        logger.error(f"荷物情報取得エラー: {str(e)}", exc_info=True)
        return Response(
            {'errMsg': f'予期しないエラーが発生しました: {str(e)}'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


# 都道府県ごとの概略バウンディングボックス (south, north, west, east) 単位：度
_PREF_BBOX: dict[str, tuple[float, float, float, float]] = {
    '01': (41.40, 45.55, 139.35, 145.82),  # 北海道
    '02': (40.20, 41.60, 139.70, 141.70),  # 青森県
    '03': (38.70, 40.50, 140.60, 142.10),  # 岩手県
    '04': (37.70, 39.00, 140.25, 141.70),  # 宮城県
    '05': (38.85, 40.55, 139.50, 141.05),  # 秋田県
    '06': (37.70, 39.00, 139.40, 140.90),  # 山形県
    '07': (36.75, 37.98, 139.05, 141.05),  # 福島県
    '08': (35.70, 36.80, 139.70, 140.85),  # 茨城県
    '09': (36.18, 37.22, 139.32, 140.35),  # 栃木県
    '10': (36.05, 37.05, 138.40, 139.70),  # 群馬県
    '11': (35.73, 36.30, 138.95, 139.92),  # 埼玉県
    '12': (34.88, 35.94, 139.72, 140.88),  # 千葉県
    '13': (35.50, 35.90, 138.94, 139.92),  # 東京都（本土）
    '14': (35.12, 35.70, 138.90, 139.80),  # 神奈川県
    '15': (36.75, 38.58, 137.62, 139.62),  # 新潟県
    '16': (36.30, 36.90, 136.77, 137.78),  # 富山県
    '17': (36.00, 37.90, 136.33, 137.38),  # 石川県
    '18': (35.40, 36.35, 135.42, 136.58),  # 福井県
    '19': (35.18, 35.95, 138.32, 139.25),  # 山梨県
    '20': (35.15, 37.02, 137.37, 138.60),  # 長野県
    '21': (35.15, 36.33, 136.25, 137.65),  # 岐阜県
    '22': (34.57, 35.48, 137.47, 138.88),  # 静岡県
    '23': (34.55, 35.37, 136.67, 137.82),  # 愛知県
    '24': (33.58, 35.05, 135.83, 136.97),  # 三重県
    '25': (34.80, 35.72, 135.75, 136.58),  # 滋賀県
    '26': (34.70, 35.77, 135.05, 135.98),  # 京都府
    '27': (34.25, 34.90, 135.08, 135.78),  # 大阪府
    '28': (34.25, 35.70, 134.27, 135.50),  # 兵庫県
    '29': (33.82, 34.75, 135.53, 136.12),  # 奈良県
    '30': (33.42, 34.40, 135.00, 136.10),  # 和歌山県
    '31': (35.00, 35.58, 133.28, 134.58),  # 鳥取県
    '32': (34.28, 35.58, 131.65, 133.42),  # 島根県
    '33': (34.30, 35.30, 133.20, 134.55),  # 岡山県
    '34': (34.12, 35.05, 132.10, 133.45),  # 広島県
    '35': (33.72, 34.73, 130.77, 132.33),  # 山口県
    '36': (33.50, 34.28, 133.75, 134.82),  # 徳島県
    '37': (34.00, 34.55, 133.42, 134.50),  # 香川県
    '38': (32.87, 34.13, 132.00, 133.43),  # 愛媛県
    '39': (32.68, 33.92, 132.42, 134.32),  # 高知県
    '40': (33.00, 33.98, 130.00, 131.20),  # 福岡県
    '41': (32.82, 33.60, 129.70, 130.68),  # 佐賀県
    '42': (32.58, 33.52, 129.48, 130.22),  # 長崎県（本土）
    '43': (31.98, 33.22, 130.05, 131.38),  # 熊本県
    '44': (32.62, 33.68, 130.72, 132.02),  # 大分県
    '45': (31.33, 32.87, 130.63, 131.98),  # 宮崎県
    '46': (30.98, 32.25, 130.18, 131.32),  # 鹿児島県（本土）
    '47': (25.70, 26.92, 127.65, 128.55),  # 沖縄県（本島）
}


def _calc_bounding_box(
    codes: list[str],
) -> tuple[float, float, float, float] | None:
    """都道府県コードリストから各バウンディングボックスを合算"""
    boxes = [_PREF_BBOX[code] for code in codes if code in _PREF_BBOX]
    if not boxes:
        return None

    south = min(box[0] for box in boxes)
    north = max(box[1] for box in boxes)
    west = min(box[2] for box in boxes)
    east = max(box[3] for box in boxes)

    return south, north, west, east


@api_view(['GET'])
@permission_classes([permissions.AllowAny])
def location_suggestions(request: Request) -> Response:
    """Google Places APIを使用した場所のサジェスト取得"""
    query = request.GET.get('q', '').strip()
    prefectures_param = request.GET.get('prefectures', '').strip()

    if not query or len(query) < 2:
        return Response({'suggestions': []})

    # 指定された都道府県コード・名称リストを構築
    codes: list[str] = []
    allowed_pref_names: list[str] = []
    if prefectures_param:
        codes = [chunk.strip() for chunk in prefectures_param.split(',') if chunk.strip()]
        allowed_pref_names = [_PREF_MAP[code] for code in codes if code in _PREF_MAP]

    try:
        # Places API — Text Search エンドポイント
        url = 'https://places.googleapis.com/v1/places:searchText'

        request_body: dict[str, Any] = {
            'textQuery': query,
            'languageCode': 'ja',
            'regionCode': 'JP',
            'maxResultCount': 20,
        }

        # 都道府県バウンディングボックスで locationRestriction を設定
        if codes:
            bbox = _calc_bounding_box(codes)
            if bbox:
                south, north, west, east = bbox
                request_body['locationRestriction'] = {
                    'rectangle': {
                        'low': {'latitude': south, 'longitude': west},
                        'high': {'latitude': north, 'longitude': east},
                    }
                }

        headers = {
            'Content-Type': 'application/json',
            'X-Goog-Api-Key': settings.GOOGLE_PLACES_API_KEY,
            'X-Goog-FieldMask': (
                'places.id,'
                'places.displayName,'
                'places.formattedAddress,'
                'places.types,'
                'places.primaryType,'
                'places.rating,'
                'places.userRatingCount,'
                'places.location'
            ),
        }

        response = requests.post(url, json=request_body, headers=headers, timeout=10)
        response.raise_for_status()

        data = response.json()

        # 宿泊施設・空港・鉄道駅のみ許可
        allowed_types = {
            'lodging',
            'hotel',
            'airport',
            'train_station',
            'subway_station',
            'transit_station',
        }

        suggestions = []
        for place in data.get('places', []):
            place_types = set(place.get('types', []))
            primary_type = place.get('primaryType', '')

            if primary_type not in allowed_types and not place_types.intersection(allowed_types):
                continue

            formatted_address = place.get('formattedAddress', '')

            # 都道府県フィルタが指定されている場合、formattedAddress に都道府県名が含まれるか確認
            if allowed_pref_names and not any(pname in formatted_address for pname in allowed_pref_names):
                continue

            name = place.get('displayName', {}).get('text', '')
            location = place.get('location', {})

            suggestion = {
                'place_id': place.get('id', ''),
                'name': name,
                'address': formatted_address,
                'types': place.get('types', []),
                'rating': place.get('rating'),
                'user_ratings_total': place.get('userRatingCount'),
                'geometry': {
                    'lat': location.get('latitude'),
                    'lng': location.get('longitude'),
                },
            }
            suggestions.append(suggestion)

            # 最大10件まで
            if len(suggestions) >= 10:
                break

        return Response({'suggestions': suggestions})

    except requests.RequestException as e:
        return Response(
            {'errMsg': f'Request failed: {str(e)}'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )
    except Exception as e:
        return Response(
            {'errMsg': f'Unexpected error: {str(e)}'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )

LUGGAGE_ITEM_KEYS = ('cabin', 'checked', 'oversize')

# 予約作成に必要な顧客・集荷・配送フィールド
# （payment_intent_id・合計金額・荷物個数・事業者・発行者スナップショットを除く）
_BOOKING_PAYLOAD_FIELDS = (
    'pickup_location_name',
    'pickup_location_address',
    'pickup_date',
    'delivery_location_name',
    'delivery_location_address',
    'delivery_date',
    'notes',
    'customer_email',
    'customer_name',
    'customer_phone_number',
    'customer_nationality',
    'guest_name',
)

_BOOKING_DATE_FIELDS = ('pickup_date', 'delivery_date')


def normalize_luggage_counts(source: dict[str, Any]) -> tuple[dict[str, int], int]:
    """荷物種別ごとの個数を正規化し、(各荷物の個数, 合計個数) を返す"""
    counts: dict[str, int] = {}
    total = 0
    for key in LUGGAGE_ITEM_KEYS:
        raw = source.get(key, 0)
        try:
            value = int(raw)
        except (ValueError, TypeError):
            value = 0
        if value < 0:
            value = 0
        counts[key] = value
        total += value
    return counts, total


def materialize_booking(
    *,
    payment_intent_id: str,
    business_profile: BusinessProfile,
    total_amount: int,
    booking_fields: dict[str, Any],
    luggage_counts: dict[str, int],
) -> tuple[LuggageBooking, bool]:
    """
    予約レコードを冪等に作成する。

    payment_intent_id の一意制約により、フロントの予約作成POSTと Webhook
    フォールバックが競合しても二重作成されない。既に存在する場合は既存を返す。
    """
    existing = LuggageBooking.objects.filter(payment_intent_id=payment_intent_id).first()
    if existing:
        return existing, False

    issuer_snapshot = build_issuer_snapshot(business_profile)
    try:
        # create 部分だけ別トランザクションにし、競合時の IntegrityError をここで安全に処理
        with transaction.atomic():
            booking = LuggageBooking.objects.create(
                payment_intent_id=payment_intent_id,
                business_owner=business_profile,
                luggage_items=luggage_counts,
                total_amount=total_amount,
                issuer_name=issuer_snapshot["issuer_name"],
                issuer_address=issuer_snapshot["issuer_address"],
                issuer_email=issuer_snapshot["issuer_email"],
                issuer_phone=issuer_snapshot["issuer_phone"],
                issuer_invoice_number=issuer_snapshot["issuer_invoice_number"],
                issuer_snapshot_at=timezone.now(),
                **booking_fields,
            )
        return booking, True
    except IntegrityError:
        # 競合により他方（フロント/Webhook）が先に作成済み。既存を取得して返す。
        existing = LuggageBooking.objects.filter(payment_intent_id=payment_intent_id).first()
        if existing:
            return existing, False
        raise


def _mark_pending_booking_consumed(payment_intent_id: str) -> None:
    """フォールバック用 PendingBooking を消費済みにする"""
    try:
        PendingBooking.objects.filter(
            payment_intent_id=payment_intent_id,
            consumed_at__isnull=True,
        ).update(consumed_at=timezone.now())
    except Exception:
        logger.warning(
            "PendingBooking の消費済み更新に失敗: payment_intent_id=%s",
            mask_sensitive_id(payment_intent_id),
        )


def _booking_fields_from_payload(payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    """PendingBooking.payload から予約作成用のフィールド辞書を組み立て"""
    fields: dict[str, Any] = {}
    for key in _BOOKING_PAYLOAD_FIELDS:
        value = payload.get(key)
        if key in _BOOKING_DATE_FIELDS:
            if not value:
                return None
            try:
                fields[key] = date_type.fromisoformat(str(value))
            except (ValueError, TypeError):
                return None
        else:
            fields[key] = value if value is not None else ''
    return fields


class LuggageBookingCreateView(generics.CreateAPIView):  # type: ignore[type-arg]
    """予約作成"""
    permission_classes = [permissions.AllowAny]

    def get_serializer_class(self) -> type[LuggageBookingCreateSerializer]:
        """予約作成用シリアライザーを返す"""
        return LuggageBookingCreateSerializer

    def create(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """予約作成のカスタム処理（冪等性・トランザクション・エラーハンドリング対応）"""
        serializer = self.get_serializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {'valid_errs': serializer.errors},
                status=status.HTTP_400_BAD_REQUEST
            )

        payment_intent_id = request.data.get('payment_intent_id')

        if not payment_intent_id:
            return Response(
                {
                    'errMsg': '予約の送信に失敗しました。しばらく時間をおいて再度お試しください。',
                },
                status=status.HTTP_400_BAD_REQUEST
            )

        # 冪等性の確保: 同じpayment_intent_idで既に予約が存在する場合は既存の予約を返す
        existing_booking = LuggageBooking.objects.filter(
            payment_intent_id=payment_intent_id
        ).first()

        if existing_booking:
            logger.info(
                f"既存の予約を返却: payment_intent_id={mask_sensitive_id(payment_intent_id)}, "
                f"booking_id={existing_booking.id}"
            )
            response_serializer = LuggageBookingSerializer(existing_booking)
            return Response(
                {
                    'message': '予約は既に作成されています。',
                    'booking': response_serializer.data
                },
                status=status.HTTP_200_OK
            )

        # トランザクション内で処理
        try:
            with transaction.atomic():
                # 決済状態を確認
                try:
                    payment_intent = stripe.PaymentIntent.retrieve(payment_intent_id)

                    if payment_intent.status != 'succeeded':
                        logger.warning(
                            f"決済が完了していない: payment_intent_id={mask_sensitive_id(payment_intent_id)}, "
                            f"status={payment_intent.status}"
                        )
                        return Response(
                            {
                                'errMsg': '決済処理が完了していません。お支払い情報に問題がないかご確認いただき、再度予約手続きを行ってください。',
                                'payment_status': payment_intent.status
                            },
                            status=status.HTTP_400_BAD_REQUEST
                        )
                except stripe.error.StripeError as e:
                    logger.error(
                        f"Stripe決済確認エラー: payment_intent_id={mask_sensitive_id(payment_intent_id)}, "
                        f"error={str(e)}"
                    )
                    return Response(
                        {
                            'errMsg': '決済情報の確認に失敗しました。',
                        },
                        status=status.HTTP_400_BAD_REQUEST
                    )

                # 予約を担当する事業者を PaymentIntent から逆引き。
                # create-payment-intent 時に transfer_data.destination = business_profile.stripe_account_id を
                # サーバー側で設定しているため、ここを真とすればクライアントの business_owner_id 改ざんを
                # 受け付けず、決済の着金先と予約レコードの紐付けが必ず一致する。
                transfer_data = getattr(payment_intent, 'transfer_data', None)
                destination_account_id = ''
                if transfer_data is not None:
                    destination_account_id = (
                        getattr(transfer_data, 'destination', '')
                        or (transfer_data.get('destination', '') if isinstance(transfer_data, dict) else '')
                        or ''
                    )
                destination_account_id = (destination_account_id or '').strip()

                if not destination_account_id:
                    logger.error(
                        f"PaymentIntent に transfer_data.destination が設定されていません: "
                        f"payment_intent_id={mask_sensitive_id(payment_intent_id)}"
                    )
                    return Response(
                        {'errMsg': '決済情報の確認に失敗しました。'},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

                try:
                    business_profile = BusinessProfile.objects.get(
                        stripe_account_id=destination_account_id,
                    )
                except BusinessProfile.DoesNotExist:
                    logger.error(
                        f"transfer_data.destination に対応する事業者が見つかりません: "
                        f"payment_intent_id={mask_sensitive_id(payment_intent_id)}, "
                        f"destination={mask_sensitive_id(destination_account_id)}"
                    )
                    return Response(
                        {'errMsg': '決済情報の確認に失敗しました。'},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

                # Step2で選択された荷物情報を取得
                luggage_items = request.data.get('luggage_items', {})

                # 合計金額は Stripe の PaymentIntent.amount を真として扱う。
                # フロントから送られる total_amount は信用せず、実際に決済された金額を予約レコードに保存する。
                try:
                    total_amount = int(payment_intent.amount)
                except (AttributeError, ValueError, TypeError):
                    logger.error(
                        f"PaymentIntent.amount の取得に失敗: payment_intent_id={mask_sensitive_id(payment_intent_id)}"
                    )
                    return Response(
                        {'errMsg': '決済情報の確認に失敗しました。'},
                        status=status.HTTP_400_BAD_REQUEST
                    )

                if total_amount <= 0:
                    return Response(
                        {'errMsg': '決済金額が不正です。'},
                        status=status.HTTP_400_BAD_REQUEST
                    )

                # 荷物情報の検証（最低1点以上の荷物が必要）
                luggage_source: dict[str, Any] = {}
                for key in LUGGAGE_ITEM_KEYS:
                    if isinstance(luggage_items, dict) and key in luggage_items:
                        luggage_source[key] = luggage_items[key]
                    else:
                        luggage_source[key] = request.data.get(key, 0)
                luggage_counts, total_items = normalize_luggage_counts(luggage_source)

                # 最低1点以上の荷物が必要
                if total_items < 1:
                    return Response(
                        {'errMsg': '最低1点以上の荷物を選択してください。'},
                        status=status.HTTP_400_BAD_REQUEST
                    )

                # 予約作成用フィールドを検証済みデータから取得
                validated = dict(serializer.validated_data)
                validated.pop('payment_intent_id', None)
                booking_fields = {
                    key: validated.get(key, '')
                    for key in _BOOKING_PAYLOAD_FIELDS
                }

                # 予約を冪等に作成する（フロント/Webhook が競合しても二重作成しない）
                try:
                    booking, created = materialize_booking(
                        payment_intent_id=payment_intent_id,
                        business_profile=business_profile,
                        total_amount=total_amount,
                        booking_fields=booking_fields,
                        luggage_counts=luggage_counts,
                    )
                except Exception as save_error:
                    # 予約保存に失敗した場合（トランザクションはロールバックされる）
                    logger.error(
                        f"予約保存エラー: payment_intent_id={mask_sensitive_id(payment_intent_id)}, "
                        f"error={str(save_error)}",
                        exc_info=True
                    )
                    raise

                # フォールバック用の PendingBooking を消費済みにする
                _mark_pending_booking_consumed(payment_intent_id)

                if created:
                    logger.info(
                        f"予約作成成功: booking_id={booking.id}, "
                        f"business_owner_id={business_profile.id}, "
                        f"payment_intent_id={mask_sensitive_id(payment_intent_id)}"
                    )
                    # 予約確認メールはトランザクション確定後に送信（新規作成時のみ）
                    transaction.on_commit(
                        lambda: send_booking_confirmation_email(booking)
                    )

                # レスポンス返却用: ID・予約番号を含む完全な予約情報をシリアライズ
                response_serializer = LuggageBookingSerializer(booking)
                return Response(
                    {
                        'message': (
                            '予約が正常に作成されました。' if created
                            else '予約は既に作成されています。'
                        ),
                        'booking': response_serializer.data,
                    },
                    status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
                )

        except Exception as e:
            # 予期しないエラー
            logger.error(
                f"予約作成処理エラー: payment_intent_id={mask_sensitive_id(payment_intent_id)}, "
                f"error={str(e)}",
                exc_info=True
            )

            # 決済は完了しているが予約保存に失敗した場合の情報を返す
            return Response(
                {
                    'errMsg': '予約の保存に失敗しました。管理者に連絡してください。',
                    'payment_intent_id': payment_intent_id,
                    'retry_recommended': True
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class LuggageBookingDetailView(generics.RetrieveUpdateAPIView):  # type: ignore[type-arg]
    """予約詳細取得・更新（予約IDと認証コードで認証）"""

    permission_classes = [permissions.AllowAny]

    def get_queryset(self) -> QuerySet[LuggageBooking]:
        """すべての予約を取得（認証コードで検証）"""
        # get_object()で認証コードを検証するため、ここではすべての予約を返す
        return LuggageBooking.objects.all()

    def get_object(self) -> LuggageBooking:
        """予約オブジェクトを取得（認証コードで検証）"""
        # TODO: 認証コードの検証を追加（後ほど実装）
        # verification_code = self.request.data.get('verification_code') or self.request.query_params.get('verification_code')
        # if not verification_code:
        #     raise Http404('認証コードが必要です。')

        obj = super().get_object()

        # TODO: 認証コードの検証を追加（後ほど実装）
        # if obj.verification_code != verification_code:
        #     raise Http404('認証コードが正しくありません。')

        return obj

    def get_serializer_class(self) -> type[LuggageBookingSerializer]:
        """シリアライザークラスを返す"""
        return LuggageBookingSerializer

    def update(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """予約更新のカスタム処理"""
        partial = kwargs.pop('partial', False)
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=partial)

        if serializer.is_valid():
            serializer.save()
            return Response(
                {
                    'message': '予約が正常に更新されました。',
                    'booking': serializer.data
                },
                status=status.HTTP_200_OK
            )
        return Response(
            {'valid_errs': serializer.errors},
            status=status.HTTP_400_BAD_REQUEST
        )


def _card_details(payment_intent_id: str) -> Optional[dict[str, Any]]:
    """予約に紐づく Stripe 決済から、表示用の支払い方法情報を取得"""
    pid = (payment_intent_id or '').strip()
    if not pid:
        return None

    try:
        payment_intent = stripe.PaymentIntent.retrieve(pid, expand=['payment_method'])
    except stripe.error.StripeError as e:
        logger.warning(
            "支払い方法の取得に失敗: payment_intent_id=%s error=%s",
            mask_sensitive_id(pid),
            str(e),
        )
        return None

    payment_method = getattr(payment_intent, 'payment_method', None)
    if payment_method is None:
        return None

    method_type = getattr(payment_method, 'type', '') or ''
    card = getattr(payment_method, 'card', None)
    if card is None:
        return {'method': method_type or 'unknown'}

    # Apple Pay / Google Pay は Stripe 上ではカード決済として扱われ、
    # card.wallet.type に 'apple_pay' / 'google_pay' が入る。
    wallet = getattr(card, 'wallet', None)
    wallet_type = (getattr(wallet, 'type', '') or '') if wallet is not None else ''

    return {
        'method': 'card',
        'wallet': wallet_type,
        'brand': getattr(card, 'brand', '') or '',
        'last4': getattr(card, 'last4', '') or '',
        'exp_month': getattr(card, 'exp_month', None),
        'exp_year': getattr(card, 'exp_year', None),
    }


@api_view(['GET'])
@permission_classes([permissions.AllowAny])
def lookup_booking(request: Request) -> Response:
    """予約番号から予約情報を取得（旅行者向けの予約内容確認・キャンセル画面用）"""
    booking_number = (request.GET.get('booking_number') or '').strip()
    if not booking_number:
        return Response(
            {'errMsg': '予約番号を入力してください。'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        booking = LuggageBooking.objects.get(booking_number=booking_number)
    except LuggageBooking.DoesNotExist:
        return Response(
            {'errMsg': '予約が見つかりません。予約番号をご確認ください。'},
            status=status.HTTP_404_NOT_FOUND,
        )

    status_labels = dict(LuggageBooking.DELIVERY_STATUS_CHOICES)
    data = LuggageBookingSerializer(booking).data
    data['delivery_status_label'] = status_labels.get(
        booking.delivery_status, booking.delivery_status
    )
    data['can_cancel'] = booking.can_cancel()
    data['can_download_receipt'] = booking.can_download_receipt()
    data['payment'] = _card_details(booking.payment_intent_id)

    return Response(data, status=status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([permissions.AllowAny])
def download_receipt(request: Request, booking_id: UUID) -> HttpResponse:
    """領収書PDFをダウンロードする（集荷済以降の予約のみ）"""
    try:
        booking = LuggageBooking.objects.select_related('business_owner').get(id=booking_id)
    except LuggageBooking.DoesNotExist:
        return Response(
            {'errMsg': '予約が見つかりません。'},
            status=status.HTTP_404_NOT_FOUND,
        )

    if not booking.can_download_receipt():
        return Response(
            {'errMsg': '集荷完了後に領収書を発行できます。'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        pdf_bytes = build_receipt_pdf(booking)
    except Exception:
        logger.exception("領収書PDFの生成に失敗: booking_id=%s", booking_id)
        return Response(
            {'errMsg': '領収書の生成に失敗しました。しばらく時間をおいて再度お試しください。'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    response = HttpResponse(pdf_bytes, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{receipt_filename(booking)}"'
    return response


@api_view(['POST'])
@permission_classes([permissions.AllowAny])
def cancel_booking(request: Request, booking_id: UUID) -> Response:
    """予約キャンセル（集荷前の予約のみ・全額返金）"""
    try:
        # TODO: 認証コードの検証を追加（後ほど実装）
        # verification_code = request.data.get('verification_code')
        # if not verification_code:
        #     return Response(
        #         {'errMsg': '認証コードが必要です。'},
        #         status=status.HTTP_400_BAD_REQUEST
        #     )

        booking = LuggageBooking.objects.get(id=booking_id)

        # TODO: 認証コードの検証を追加（後ほど実装）
        # if booking.verification_code != verification_code:
        #     return Response(
        #         {'errMsg': '認証コードが正しくありません。'},
        #         status=status.HTTP_401_UNAUTHORIZED
        #     )
    except LuggageBooking.DoesNotExist:
        return Response(
            {'errMsg': '予約が見つかりません。'},
            status=status.HTTP_404_NOT_FOUND
        )

    if not booking.can_cancel():
        return Response(
            {'errMsg': 'この予約はキャンセルできません。'},
            status=status.HTTP_400_BAD_REQUEST
        )

    # 集荷日前日の23時00分以降のキャンセルは返金対象外。
    # それより前のキャンセルのみ全額返金を行い、成功した場合だけキャンセル状態にする。
    # （返金失敗のまま「キャンセル済み」になると未返金が放置されるため）
    refunded = booking.is_refundable_on_cancel()
    if refunded:
        if not _refund_booking_payment(
            booking, source=BookingAuditLog.SOURCE_CUSTOMER_API
        ):
            return Response(
                {
                    'errMsg': (
                        '返金処理に失敗したため、キャンセルできませんでした。'
                        'お手数ですが、時間をおいて再度お試しください。'
                    ),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

    previous_delivery_status = booking.delivery_status
    booking.delivery_status = 'cancelled'
    booking.save(update_fields=['delivery_status', 'updated_at'])
    log_booking_event(
        booking=booking,
        action=BookingAuditLog.ACTION_BOOKING_CANCELLED,
        source=BookingAuditLog.SOURCE_CUSTOMER_API,
        previous_delivery_status=previous_delivery_status,
        new_delivery_status='cancelled',
        new_refund_status=booking.refund_status,
        stripe_refund_id=booking.stripe_refund_id,
        amount=booking.refunded_amount if refunded else 0,
        message=(
            '顧客による予約キャンセル（返金あり）。'
            if refunded
            else '顧客による予約キャンセル（返金なし）。'
        ),
    )

    # キャンセル確定後に顧客・配達者へ通知メールを送信
    transaction.on_commit(
        lambda: send_booking_cancellation_emails(booking, refunded=refunded)
    )

    return Response(
        {'message': '予約がキャンセルされました。'},
        status=status.HTTP_200_OK
    )


@api_view(['GET'])
@permission_classes([permissions.AllowAny])
def booking_status(request: Request, booking_id: UUID) -> Response:
    """予約ステータス取得（予約IDと認証コードで認証）"""
    try:
        # TODO: 認証コードの検証を追加（後ほど実装）
        # verification_code = request.GET.get('verification_code')
        # if not verification_code:
        #     return Response(
        #         {'errMsg': '認証コードが必要です。'},
        #         status=status.HTTP_400_BAD_REQUEST
        #     )

        booking = LuggageBooking.objects.get(id=booking_id)

        # TODO: 認証コードの検証を追加（後ほど実装）
        # if booking.verification_code != verification_code:
        #     return Response(
        #         {'errMsg': '認証コードが正しくありません。'},
        #         status=status.HTTP_401_UNAUTHORIZED
        #     )
    except LuggageBooking.DoesNotExist:
        return Response(
            {'errMsg': '予約が見つかりません。'},
            status=status.HTTP_404_NOT_FOUND
        )

    serializer = LuggageBookingSerializer(booking)
    return Response(serializer.data)


@api_view(['POST'])
@permission_classes([permissions.AllowAny])
def create_payment_intent(request: Request) -> Response:
    """Stripe Payment Intentを作成"""
    try:
        # Step2で選択された荷物情報と金額を取得（フロントエンドから送られてくる値をそのまま使用）
        luggage_items = request.data.get('luggage_items', {})
        total_amount = request.data.get('total_amount')

        if total_amount is None:
            logger.warning("create_payment_intent: total_amount is missing")
            return Response(
                {'errMsg': '支払い情報の取得に失敗しました。'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # total_amountを数値に変換
        try:
            amount_in_yen = int(float(total_amount))
        except (ValueError, TypeError):
            logger.warning("create_payment_intent: invalid total_amount format: %r", total_amount)
            return Response(
                {'errMsg': '支払い情報の取得に失敗しました。'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if amount_in_yen <= 0:
            logger.warning("create_payment_intent: non-positive total_amount: %s", amount_in_yen)
            return Response(
                {'errMsg': '支払い情報の取得に失敗しました。'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # 荷物情報の検証（最低1点以上の荷物が必要）
        LUGGAGE_ITEM_KEYS = ['cabin', 'checked', 'oversize']

        luggage_counts = {}
        total_items = 0
        for key in LUGGAGE_ITEM_KEYS:
            count = luggage_items.get(key, request.data.get(key, 0))

            if isinstance(count, str):
                try:
                    count = int(count)
                except ValueError:
                    count = 0

            elif isinstance(count, (int, float)):
                count = int(count)
                if count < 0:
                    count = 0

            else:
                count = 0

            luggage_counts[key] = count
            total_items += count

        # 最低1点以上の荷物が必要
        if total_items < 1:
            logger.warning("create_payment_intent: no luggage items selected")
            return Response(
                {'errMsg': '支払い情報の取得に失敗しました。'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # 決済金額の10%
        platform_fee = int(amount_in_yen * 0.1)

        # 顧客情報を取得
        customer_name = request.data.get('customer_name', '')
        customer_email = request.data.get('customer_email', '')

        # 予約フォームの事業者（サブドメインで特定）から Stripe Connect アカウントID を取得
        business_owner_id = request.data.get('business_owner_id')
        if not business_owner_id:
            logger.warning("create_payment_intent: business_owner_id is missing")
            return Response(
                {'errMsg': '支払い情報の取得に失敗しました。'},
                status=status.HTTP_400_BAD_REQUEST
            )
        try:
            business_profile = BusinessProfile.objects.get(id=business_owner_id)
        except (BusinessProfile.DoesNotExist, ValueError):
            logger.warning(
                "create_payment_intent: business profile not found: %s",
                mask_sensitive_id(str(business_owner_id)),
            )
            return Response(
                {'errMsg': '支払い情報の取得に失敗しました。'},
                status=status.HTTP_400_BAD_REQUEST
            )
        connected_account_id = (business_profile.stripe_account_id or '').strip()
        if not connected_account_id:
            return Response(
                {'errMsg': 'この事業者はまだ決済の設定が完了していません。'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # 決済直前に事業者の Stripe アカウント審査が完了しているか確認
        try:
            connected_account = stripe.Account.retrieve(connected_account_id)
        except stripe.error.StripeError as e:
            logger.warning(
                "create_payment_intent: failed to retrieve connected account: %s error=%s",
                mask_sensitive_id(connected_account_id),
                str(e),
            )
            return Response(
                {'errMsg': 'この事業者はまだ決済の設定が完了していません。'},
                status=status.HTTP_400_BAD_REQUEST
            )

        account_requirements = getattr(connected_account, 'requirements', None)
        currently_due = account_requirements.get('currently_due', []) if account_requirements else []
        charges_enabled = getattr(connected_account, 'charges_enabled', False)
        if not charges_enabled or currently_due:
            logger.warning(
                "create_payment_intent: connected account not ready: %s charges_enabled=%s currently_due=%s",
                mask_sensitive_id(connected_account_id),
                charges_enabled,
                len(currently_due),
            )
            return Response(
                {'errMsg': 'この事業者はまだ決済の設定が完了していません。'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # 集荷・配達地域バリデーション
        service_areas = business_profile.service_areas or []
        if not service_areas:
            return Response(
                {'errMsg': '集荷地域が設定されていないため予約できません。'},
                status=status.HTTP_400_BAD_REQUEST
            )

        pickup_postal_code = request.data.get('pickup_postal_code', '')
        pickup_pref = pref_code_from_postal(pickup_postal_code)
        if not pickup_pref or pickup_pref not in service_areas:
            return Response(
                {'errMsg': '集荷場所の郵便番号は集荷地域の対象外です。'},
                status=status.HTTP_400_BAD_REQUEST
            )

        pricing_rules = business_profile.pricing_rules or {}
        deliverable_prefectures = list(pricing_rules.keys())
        if not deliverable_prefectures:
            return Response(
                {'errMsg': '配達地域が設定されていないため予約できません。'},
                status=status.HTTP_400_BAD_REQUEST
            )

        delivery_postal_code = request.data.get('delivery_postal_code', '')
        delivery_pref = pref_code_from_postal(delivery_postal_code)
        if not delivery_pref or delivery_pref not in deliverable_prefectures:
            return Response(
                {'errMsg': '配送場所の郵便番号は配達地域の対象外です。'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # 定休日・臨時休業日バリデーション
        operating_days = business_profile.operating_days or '1111111'
        nth_weekday_holidays = business_profile.nth_weekday_holidays or []
        temporary_closures = business_profile.temporary_closures or []

        for field_name, label in [('pickup_date', '集荷日'), ('delivery_date', '配送日')]:
            raw_date = request.data.get(field_name, '')
            if not raw_date:
                continue

            try:
                target_date = date_type.fromisoformat(str(raw_date))
            except (ValueError, TypeError):
                continue

            weekday_idx = target_date.weekday()
            if len(operating_days) == 7 and operating_days[weekday_idx] == '0':
                return Response(
                    {'errMsg': f'{label}に指定された日は定休日のため選択できません。'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            nth = (target_date.day - 1) // 7 + 1
            nth_key = f'{nth}-{weekday_idx}'
            if nth_key in nth_weekday_holidays:
                return Response(
                    {'errMsg': f'{label}に指定された日は定休日のため選択できません。'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if target_date.isoformat() in temporary_closures:
                return Response(
                    {'errMsg': f'{label}に指定された日は臨時休業日のため選択できません。'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        # 1日の最大荷物個数チェック（-1 = 制限なし）
        daily_max = business_profile.daily_max_luggage
        if daily_max >= 0:
            pickup_date_raw = request.data.get('pickup_date', '')
            delivery_date_raw = request.data.get('delivery_date', '')

            for raw_dt, label in [(pickup_date_raw, '集荷日'), (delivery_date_raw, '配送日')]:
                if not raw_dt:
                    continue

                try:
                    target_date = date_type.fromisoformat(str(raw_dt))
                except (ValueError, TypeError):
                    continue

                existing_bookings = LuggageBooking.objects.filter(
                    Q(pickup_date=target_date) | Q(delivery_date=target_date),
                    business_owner=business_profile,
                ).exclude(delivery_status='cancelled')

                existing_total = 0
                for booking in existing_bookings:
                    items = booking.luggage_items or {}
                    existing_total += sum(
                        int(value)
                        for value in items.values()
                        if isinstance(value, (int, float, str)) and str(value).isdigit()
                    )

                if existing_total + total_items > daily_max:
                    remaining = max(0, daily_max - existing_total)
                    return Response(
                        {
                            'errMsg': (
                                f'{label}の荷物受付可能数の残りは{remaining}個です。'
                                f'予約個数を{remaining}個以下にしてください。'
                            ),
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )

        # サーバー側で合計金額を再計算してフロントの total_amount と突合する。
        # ここでズレが出るのは「事業者が料金を変更した」「フロントのキャッシュが古い」「リクエスト改ざん」の何れか。
        # 不一致時はサーバー再計算結果を正として返し、ユーザーに最新金額の確認を促す。
        server_total_amount = calculate_server_total_amount(
            business_profile,
            delivery_postal_code,
            luggage_counts,
        )

        if server_total_amount <= 0:
            return Response(
                {'errMsg': '料金が設定されていません。事業者にお問い合わせください。'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if server_total_amount != amount_in_yen:
            logger.warning(
                "total_amount mismatch: client=%s server=%s business_owner=%s delivery_postal=%s",
                amount_in_yen,
                server_total_amount,
                business_owner_id,
                mask_sensitive_id(delivery_postal_code),
            )
            return Response(
                {
                    'errMsg': (
                        f'料金が更新されました。最新の合計金額は ¥{server_total_amount:,} です。'
                        'お手数ですが、お戻りいただき内容をご確認の上、もう一度お進みください。'
                    ),
                    'server_total_amount': server_total_amount,
                    'client_total_amount': amount_in_yen,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Payment Intent作成パラメータを準備
        payment_intent_params = {
            'amount': amount_in_yen,
            'currency': 'jpy',
            'automatic_payment_methods': {
                'enabled': True,
            },
            'payment_method_options': {
                'card': {
                    'request_three_d_secure': 'automatic',
                },
            },
            'on_behalf_of': connected_account_id,
            'application_fee_amount': platform_fee,
            'transfer_data': {
                'destination': connected_account_id,
            },
            'metadata': {
                key: str(count) for key, count in luggage_counts.items()
            },
        }

        # メールアドレスをreceipt_emailに設定
        if customer_email:
            payment_intent_params['receipt_email'] = customer_email

        # 顧客名をmetadataに追加
        if customer_name:
            payment_intent_params['metadata']['customer_name'] = customer_name

        # Payment Intentを作成
        payment_intent = stripe.PaymentIntent.create(**payment_intent_params)

        # 決済成功後の予約作成フォールバック用に、検証済みの予約情報を保存する。
        # フロントの予約作成POSTが失敗・未実行でも、Webhook がこのデータから予約を作成できる。
        # 顧客の個人情報を Stripe metadata に載せないよう、専用テーブルへ保持する。
        try:
            pending_payload: dict[str, Any] = {
                key: request.data.get(key, '')
                for key in _BOOKING_PAYLOAD_FIELDS
            }
            pending_payload['luggage_items'] = luggage_counts
            PendingBooking.objects.update_or_create(
                payment_intent_id=payment_intent.id,
                defaults={
                    'business_owner': business_profile,
                    'payload': pending_payload,
                    'total_amount': amount_in_yen,
                    'consumed_at': None,
                },
            )
        except Exception:
            # フォールバック情報の保存失敗は決済フローを止めない（通常の予約POSTは別途成立しうる）
            logger.warning(
                "PendingBooking の保存に失敗: payment_intent_id=%s",
                mask_sensitive_id(payment_intent.id),
            )

        return Response(
            {
                'client_secret': payment_intent.client_secret,
                'amount': amount_in_yen,
            },
            status=status.HTTP_200_OK
        )

    except stripe.error.StripeError as e:
        logger.warning("Stripe error in create_payment_intent: %s", str(e))
        return Response(
            {'errMsg': '支払い情報の取得に失敗しました。'},
            status=status.HTTP_400_BAD_REQUEST
        )
    except Exception:
        logger.exception("Unexpected error in create_payment_intent")
        return Response(
            {'errMsg': '支払い情報の取得に失敗しました。'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


def _booking_exists_with_grace(payment_intent_id: str, grace_seconds: int) -> bool:
    """
    指定の payment_intent_id に対応する予約が存在するかを確認する。

    フロントの予約作成POST（confirm.vue は最大3回・指数バックオフで再送）が完了する前に
    Webhook が到達するレースがあるため、即時チェックで見つからない場合は猶予時間内で
    数回ポーリングし、誤った「予約なし」判定を避ける。
    """
    if LuggageBooking.objects.filter(payment_intent_id=payment_intent_id).exists():
        return True

    if grace_seconds <= 0:
        return False

    poll_interval = 1.0
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        time.sleep(poll_interval)
        if LuggageBooking.objects.filter(payment_intent_id=payment_intent_id).exists():
            return True
    return False


def _get_booking_with_grace(
    payment_intent_id: str, grace_seconds: int
) -> Optional[LuggageBooking]:
    """
    payment_intent_id に対応する予約を取得

    決済成功から予約レコードが DB に載るまでには、予約作成 POST や
    payment_intent.succeeded の処理までのタイムラグがある。
    返金 Webhook 処理時に即座に見つからなくても、猶予時間内で数回ポーリングし、
    作成完了を待ってから「予約なし」と確定する。
    """
    booking = LuggageBooking.objects.filter(
        payment_intent_id=payment_intent_id
    ).first()
    if booking is not None or grace_seconds <= 0:
        return booking

    poll_interval = 1.0
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        time.sleep(poll_interval)
        booking = LuggageBooking.objects.filter(
            payment_intent_id=payment_intent_id
        ).first()
        if booking is not None:
            return booking
    return None


def _notify_unmatched_payment(payment_intent: Any) -> bool:
    """決済成功済みだが予約レコードが無い場合に運営へ通知する。送信成功で True。"""
    payment_intent_id = payment_intent.get('id', '') or ''
    amount = payment_intent.get('amount')
    receipt_email = payment_intent.get('receipt_email')

    metadata = payment_intent.get('metadata') or {}
    customer_name = metadata.get('customer_name') if hasattr(metadata, 'get') else None

    created_iso: Optional[str] = None
    created_ts = payment_intent.get('created')
    if created_ts:
        try:
            created_iso = (
                dt_datetime.fromtimestamp(int(created_ts), tz=dt_timezone.utc)
                .astimezone()
                .isoformat()
            )
        except (ValueError, TypeError, OSError):
            created_iso = None

    # 事業者を transfer_data.destination（Stripe Connect アカウントID）から逆引きする
    business_name: Optional[str] = None
    transfer_data = payment_intent.get('transfer_data') or {}
    destination = (
        transfer_data.get('destination') if hasattr(transfer_data, 'get') else None
    )
    if destination:
        try:
            business_profile = BusinessProfile.objects.get(stripe_account_id=destination)
            business_name = business_profile.company_name
        except BusinessProfile.DoesNotExist:
            business_name = None

    logger.error(
        "未照合の成功決済を検知（対応する予約レコードなし）: payment_intent_id=%s, amount=%s",
        mask_sensitive_id(payment_intent_id),
        amount,
    )

    return send_unmatched_payment_alert(
        payment_intent_id=payment_intent_id,
        amount=amount,
        customer_name=customer_name,
        customer_email=receipt_email,
        business_name=business_name,
        created_iso=created_iso,
    )


def _try_create_booking_from_pending(payment_intent_id: str) -> Optional[LuggageBooking]:
    """
    Webhook フォールバック: PendingBooking から予約レコードを作成

    決済は成功しているが予約レコードが無い場合に呼ぶ。
    """
    pending = PendingBooking.objects.filter(payment_intent_id=payment_intent_id).first()
    if pending is None:
        logger.error(
            "Webhookフォールバック不能: PendingBooking が見つかりません payment_intent_id=%s",
            mask_sensitive_id(payment_intent_id),
        )
        return None

    business_profile = pending.business_owner
    if business_profile is None:
        logger.error(
            "Webhookフォールバック不能: 事業者が特定できません payment_intent_id=%s",
            mask_sensitive_id(payment_intent_id),
        )
        return None

    payload = pending.payload or {}
    booking_fields = _booking_fields_from_payload(payload)
    if booking_fields is None:
        logger.error(
            "Webhookフォールバック不能: 予約データが不正です payment_intent_id=%s",
            mask_sensitive_id(payment_intent_id),
        )
        return None

    luggage_counts, total_items = normalize_luggage_counts(payload.get('luggage_items') or {})
    if total_items < 1:
        logger.error(
            "Webhookフォールバック不能: 荷物個数が不正です payment_intent_id=%s",
            mask_sensitive_id(payment_intent_id),
        )
        return None

    try:
        booking, created = materialize_booking(
            payment_intent_id=payment_intent_id,
            business_profile=business_profile,
            total_amount=pending.total_amount,
            booking_fields=booking_fields,
            luggage_counts=luggage_counts,
        )
    except Exception:
        logger.error(
            "Webhookフォールバックの予約作成に失敗: payment_intent_id=%s",
            mask_sensitive_id(payment_intent_id),
            exc_info=True,
        )
        return None

    _mark_pending_booking_consumed(payment_intent_id)

    if created:
        logger.info(
            "Webhookフォールバックで予約を作成: booking_id=%s payment_intent_id=%s",
            booking.id,
            mask_sensitive_id(payment_intent_id),
        )
        # トランザクション外での呼び出しのため on_commit は即時実行される
        transaction.on_commit(lambda: send_booking_confirmation_email(booking))

    return booking


def _handle_account_updated(account: Any) -> Response:
    """
    Stripe account.updated を受け、事業者の Stripe 審査状態を DB へ同期

    連結アカウント（事業者の Stripe Connect アカウント）の状態が変わるたびに送られる。
    状態が変化した場合は同期処理内で事業者へメール通知。
    """
    account_id = account.get('id', '') if hasattr(account, 'get') else getattr(account, 'id', '')
    account_id = account_id or ''

    if not account_id:
        logger.warning("Stripe Webhook(account.updated): account.id が空です")
        return Response(status=status.HTTP_200_OK)

    try:
        profile = BusinessProfile.objects.get(stripe_account_id=account_id)
    except BusinessProfile.DoesNotExist:
        logger.info(
            "Stripe Webhook(account.updated): 対応する事業者が見つかりません account_id=%s",
            mask_sensitive_id(account_id),
        )
        return Response(status=status.HTTP_200_OK)
    except BusinessProfile.MultipleObjectsReturned:
        logger.error(
            "Stripe Webhook(account.updated): 同一 account_id の事業者が複数存在します account_id=%s",
            mask_sensitive_id(account_id),
        )
        return Response(status=status.HTTP_200_OK)

    try:
        sync_stripe_review_status(profile, account)
    except Exception:
        logger.error(
            "Stripe Webhook(account.updated): 審査状態の同期に失敗しました account_id=%s",
            mask_sensitive_id(account_id),
            exc_info=True,
        )
        # 一時的な失敗の可能性があるため 500 を返し Stripe の再送に委ねる。
        return Response(status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    return Response(status=status.HTTP_200_OK)


def _stripe_ts_to_datetime(value: Any) -> Optional[dt_datetime]:
    """Stripe の Unix タイムスタンプをタイムゾーン付きの datetime に変換"""
    if not value:
        return None
    try:
        return dt_datetime.fromtimestamp(int(value), tz=dt_timezone.utc)
    except (ValueError, TypeError, OSError):
        return None


def _datetime_to_local_display(value: Optional[dt_datetime]) -> Optional[str]:
    """datetime を現地時刻の「YYYY年MM月DD日 HH:MM」形式の文字列に変換"""
    if value is None:
        return None
    try:
        return timezone.localtime(value).strftime("%Y年%m月%d日 %H:%M")
    except (ValueError, OSError):
        return None


def _handle_charge_dispute(event: Any) -> Response:
    """
    Stripe charge.dispute.created / charge.dispute.closed を受け、
    チャージバック（異議申立て）を DB に記録し、運営へメール通知
    """
    event_type = event.get('type') or ''
    dispute = event['data']['object']

    def _field(key: str) -> Any:
        return dispute.get(key) if hasattr(dispute, 'get') else getattr(dispute, key, None)

    dispute_id = _field('id') or ''
    if not dispute_id:
        logger.warning("Stripe Webhook(%s): dispute.id が空です", event_type)
        return Response(status=status.HTTP_200_OK)

    charge_id = _field('charge') or ''

    payment_intent_raw = _field('payment_intent')
    if isinstance(payment_intent_raw, str):
        payment_intent_id = payment_intent_raw
    elif payment_intent_raw and hasattr(payment_intent_raw, 'get'):
        payment_intent_id = payment_intent_raw.get('id', '') or ''
    else:
        payment_intent_id = getattr(payment_intent_raw, 'id', '') or ''

    amount = _field('amount')
    currency = _field('currency') or ''
    reason = _field('reason') or ''
    dispute_status = _field('status') or ''
    is_charge_refundable = bool(_field('is_charge_refundable'))

    evidence_details = _field('evidence_details') or {}
    due_by_ts = (
        evidence_details.get('due_by') if hasattr(evidence_details, 'get') else None
    )
    evidence_due_by = _stripe_ts_to_datetime(due_by_ts)

    opened_at = _stripe_ts_to_datetime(_field('created'))
    is_closed = event_type == 'charge.dispute.closed'
    closed_at = _stripe_ts_to_datetime(event.get('created')) if is_closed else None

    # payment_intent から対応する予約・事業者を逆引きする
    booking: Optional[LuggageBooking] = None
    if payment_intent_id:
        booking = LuggageBooking.objects.filter(
            payment_intent_id=payment_intent_id
        ).first()
    business_profile = booking.business_owner if booking else None

    defaults: dict[str, Any] = {
        'charge_id': charge_id,
        'payment_intent_id': payment_intent_id,
        'booking': booking,
        'business_owner': business_profile,
        'amount': amount if isinstance(amount, int) and amount >= 0 else 0,
        'currency': currency,
        'reason': reason,
        'status': dispute_status,
        'is_charge_refundable': is_charge_refundable,
        'evidence_due_by': evidence_due_by,
    }
    if opened_at is not None:
        defaults['opened_at'] = opened_at
    if closed_at is not None:
        defaults['closed_at'] = closed_at

    try:
        _, created = ChargeDispute.objects.update_or_create(
            dispute_id=dispute_id,
            defaults=defaults,
        )
    except Exception:
        logger.error(
            "Stripe Webhook(%s): 異議申立ての記録に失敗しました dispute_id=%s",
            event_type,
            mask_sensitive_id(dispute_id),
            exc_info=True,
        )
        return Response(status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    logger.warning(
        "Stripe Webhook(%s): チャージバックを記録しました dispute_id=%s status=%s "
        "payment_intent_id=%s new=%s",
        event_type,
        mask_sensitive_id(dispute_id),
        dispute_status,
        mask_sensitive_id(payment_intent_id) if payment_intent_id else '-',
        created,
    )

    notified = send_charge_dispute_alert(
        event_type=event_type,
        dispute_id=dispute_id,
        payment_intent_id=payment_intent_id,
        charge_id=charge_id,
        amount=amount if isinstance(amount, int) else None,
        currency=currency,
        reason=reason,
        status_value=dispute_status,
        is_charge_refundable=is_charge_refundable,
        evidence_due_iso=_datetime_to_local_display(evidence_due_by),
        opened_iso=_datetime_to_local_display(opened_at),
        booking_number=booking.booking_number if booking else None,
        customer_name=booking.customer_name if booking else None,
        business_name=business_profile.company_name if business_profile else None,
    )

    # 記録は成功しているが通知に失敗した場合は 500 を返し、Stripe の再送に委ねる
    if not notified:
        return Response(status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    return Response(status=status.HTTP_200_OK)


def _stripe_id(value: Any) -> str:
    """Stripe のフィールド（文字列 or オブジェクト）から ID を取り出す"""
    if not value:
        return ''
    if isinstance(value, str):
        return value
    if hasattr(value, 'get'):
        return value.get('id', '') or ''
    return getattr(value, 'id', '') or ''


def _reconcile_and_notify(
    *,
    event_type: str,
    event_id: str,
    payment_intent_id: str,
    refund_status: str,
    stripe_refund_id: str,
    stripe_charge_id: str,
    amount: int,
    refunded_at: Any,
) -> Response:
    """
    返金 Webhook 受信後の共通後処理

    予約を検索し、返金・キャンセル状態を Stripe に合わせて更新する。
    今回の処理で初めてキャンセルになった場合のみ、顧客へ通知する。

    予約が見つからない場合は運営が調査できるようログに残して 200 を返す。
    """
    if not payment_intent_id:
        logger.warning(
            "Stripe Webhook(%s): payment_intent が特定できないためスキップします", event_type
        )
        mark_webhook_event_processed(event_id)
        return Response(status=status.HTTP_200_OK)

    grace_seconds = getattr(settings, 'STRIPE_WEBHOOK_BOOKING_GRACE_SECONDS', 10)
    booking = _get_booking_with_grace(payment_intent_id, grace_seconds)
    if booking is None:
        # 予約が見つからない = LugGo 未登録の決済への返金など。運営調査用に記録し、
        # Stripe が無限に再送しないよう 200 で受理する。監査ログにも残す。
        logger.error(
            "Stripe Webhook(%s): 返金に対応する予約が見つかりません payment_intent_id=%s "
            "refund_id=%s",
            event_type,
            mask_sensitive_id(payment_intent_id),
            mask_sensitive_id(stripe_refund_id) if stripe_refund_id else '-',
        )
        log_booking_event(
            booking=None,
            action=BookingAuditLog.ACTION_RECONCILED,
            source=BookingAuditLog.SOURCE_WEBHOOK,
            payment_intent_id=payment_intent_id,
            new_refund_status=refund_status,
            stripe_event_id=event_id,
            stripe_refund_id=stripe_refund_id,
            stripe_charge_id=stripe_charge_id,
            amount=amount,
            message=(
                f'{event_type} を受信しましたが、対応する予約が見つかりませんでした。'
            ),
        )
        mark_webhook_event_processed(event_id)
        return Response(status=status.HTTP_200_OK)

    try:
        result = reconcile_booking_refund(
            payment_intent_id=payment_intent_id,
            refund_status=refund_status,
            stripe_refund_id=stripe_refund_id,
            stripe_charge_id=stripe_charge_id,
            amount=amount,
            refunded_at=refunded_at,
            source=BookingAuditLog.SOURCE_WEBHOOK,
            stripe_event_id=event_id,
        )
    except Exception:
        # 一時的な DB エラー等の可能性。処理未完了のまま 500 を返し Stripe の再送に委ねる。
        logger.exception(
            "Stripe Webhook(%s): 返金整合処理に失敗しました payment_intent_id=%s",
            event_type,
            mask_sensitive_id(payment_intent_id),
        )
        return Response(status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    if result.cancelled_now:
        logger.info(
            "Stripe Webhook(%s): 返金に伴い予約をキャンセルに更新しました booking_id=%s",
            event_type,
            mask_sensitive_id(str(result.booking.id)) if result.booking else '-',
        )
        # API 途中失敗（アプリ落ち）やダッシュボード手動返金で未キャンセルだった予約を
        # ここでキャンセル済みにした場合のみ、顧客・配達者へ通知
        booking_to_notify = result.booking
        if booking_to_notify is not None:
            transaction.on_commit(
                lambda b=booking_to_notify: send_booking_cancellation_emails(
                    b, refunded=True
                )
            )

    mark_webhook_event_processed(event_id)
    return Response(status=status.HTTP_200_OK)


def _handle_charge_refunded(event: Any) -> Response:
    """
    Stripe charge.refunded を受け、返金に合わせて予約状態を整合する

    主に Stripe ダッシュボードからの手動返金や、API 返金後にアプリが落ちて
    delivery_status を更新できなかったケースの復旧に用いる。
    """
    event_id = event.get('id') or ''
    event_type = event.get('type') or 'charge.refunded'
    charge = event['data']['object']

    def _field(key: str) -> Any:
        return charge.get(key) if hasattr(charge, 'get') else getattr(charge, key, None)

    payment_intent_id = _stripe_id(_field('payment_intent'))
    charge_id = _field('id') or ''
    amount_refunded = _field('amount_refunded')
    amount_refunded = amount_refunded if isinstance(amount_refunded, int) else 0
    is_refunded = bool(_field('refunded'))

    # 最新の Refund オブジェクトから ID を取得
    refund_id = ''
    refunds = _field('refunds')
    refund_list = None
    if refunds is not None:
        refund_list = refunds.get('data') if hasattr(refunds, 'get') else None
    if refund_list:
        refund_id = _stripe_id(refund_list[0])

    # charge.refunded は返金確定後に届くイベントのため succeeded とみなす。
    refund_status = (
        LuggageBooking.REFUND_STATUS_SUCCEEDED
        if (is_refunded or amount_refunded > 0)
        else LuggageBooking.REFUND_STATUS_PENDING
    )
    refunded_at = _stripe_ts_to_datetime(event.get('created'))

    return _reconcile_and_notify(
        event_type=event_type,
        event_id=event_id,
        payment_intent_id=payment_intent_id,
        refund_status=refund_status,
        stripe_refund_id=refund_id,
        stripe_charge_id=charge_id,
        amount=amount_refunded,
        refunded_at=refunded_at,
    )


def _handle_refund_updated(event: Any) -> Response:
    """
    refund.updated を受け、返金の最新状態に合わせて予約状態を整合する

    返金の status が変わったときや、
    ダッシュボードでの手動返金による状態変化を、
    LugGo の予約状態にも反映する。
    """
    event_id = event.get('id') or ''
    event_type = event.get('type') or 'refund.updated'
    refund = event['data']['object']

    def _field(key: str) -> Any:
        return refund.get(key) if hasattr(refund, 'get') else getattr(refund, key, None)

    payment_intent_id = _stripe_id(_field('payment_intent'))
    charge_id = _stripe_id(_field('charge'))
    refund_id = _field('id') or ''
    amount = _field('amount')
    amount = amount if isinstance(amount, int) and amount >= 0 else 0
    refund_status = normalize_refund_status(_field('status'))
    refunded_at = _stripe_ts_to_datetime(event.get('created'))

    return _reconcile_and_notify(
        event_type=event_type,
        event_id=event_id,
        payment_intent_id=payment_intent_id,
        refund_status=refund_status,
        stripe_refund_id=refund_id,
        stripe_charge_id=charge_id,
        amount=amount,
        refunded_at=refunded_at,
    )


def _handle_refund_event(event: Any) -> Response:
    """
    返金系 Webhook（charge.refunded / refund.updated）の入口

    冪等化のため、同一イベントの再送は最初の 1 回のみ処理する。
    """
    event_type = event.get('type') or ''
    event_id = event.get('id') or ''

    if not claim_webhook_event(event_id, event_type):
        logger.info(
            "Stripe Webhook(%s): 処理済みイベントのため無視します event_id=%s",
            event_type,
            mask_sensitive_id(event_id) if event_id else '-',
        )
        return Response(status=status.HTTP_200_OK)

    if event_type == 'charge.refunded':
        return _handle_charge_refunded(event)
    return _handle_refund_updated(event)


@csrf_exempt
@api_view(['POST'])
@authentication_classes([])
@permission_classes([permissions.AllowAny])
def stripe_webhook(request: Request) -> Response:
    """
    Stripe Webhook 受信エンドポイント

    payment_intent.succeeded を受け取り、対応する予約レコードが存在しない場合に、
    まず PendingBooking からの予約自動作成（フォールバック）を試みる。作成できない
    場合のみ運営へメール通知する（決済成功済みだが予約データが復元できないケース）。

    これにより、決済直後のユーザー離脱や予約POST失敗があっても、可能な限り予約を
    自動復旧できる。

    あわせて account.updated（事業者の審査状態同期）、
    charge.dispute.created / charge.dispute.closed（チャージバックの記録・運営通知）、
     charge.refunded / refund.updated（返金状態の整合性担保）も処理する。

    PaymentIntent はプラットフォームアカウント上で destination charge として作成して
    いるため、本 Webhook はプラットフォームアカウントに対して設定する。
    """
    webhook_secret = settings.STRIPE_WEBHOOK_SECRET
    if not webhook_secret:
        logger.error("STRIPE_WEBHOOK_SECRET が未設定のため Webhook を検証できません")
        return Response(status=status.HTTP_503_SERVICE_UNAVAILABLE)

    sig_header = request.META.get('HTTP_STRIPE_SIGNATURE', '')
    payload = request.body

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    except ValueError:
        # ペイロードが不正
        logger.warning("Stripe Webhook: ペイロードの解析に失敗しました")
        return Response(status=status.HTTP_400_BAD_REQUEST)
    except stripe.error.SignatureVerificationError:
        # 署名検証に失敗（なりすましの可能性）
        logger.warning("Stripe Webhook: 署名検証に失敗しました")
        return Response(status=status.HTTP_400_BAD_REQUEST)

    event_type = event.get('type')

    # 連結アカウントの審査状態変更を DB に同期
    if event_type == 'account.updated':
        return _handle_account_updated(event['data']['object'])

    # チャージバック（異議申立て）の発生・クローズを記録し、運営へ通知
    if event_type in ('charge.dispute.created', 'charge.dispute.closed'):
        return _handle_charge_dispute(event)

    # 返金の発生・状態変化を受け、Stripe を正として予約の返金・配達状況を整合
    if event_type in ('charge.refunded', 'refund.updated'):
        return _handle_refund_event(event)

    # それ以外で関心があるのは決済成功イベントのみ。それ以外は 200 で受理して無視する。
    if event_type != 'payment_intent.succeeded':
        return Response(status=status.HTTP_200_OK)

    payment_intent = event['data']['object']
    payment_intent_id = payment_intent.get('id', '') or ''

    if not payment_intent_id:
        logger.warning("Stripe Webhook: payment_intent.id が空です")
        return Response(status=status.HTTP_200_OK)

    grace_seconds = getattr(settings, 'STRIPE_WEBHOOK_BOOKING_GRACE_SECONDS', 10)

    # 予約が存在すれば正常に処理済み。何もしない。
    if _booking_exists_with_grace(payment_intent_id, grace_seconds):
        return Response(status=status.HTTP_200_OK)

    # 予約が無い = 決済成功済みだが予約未保存。まず保留データからの自動作成を試みる。
    if _try_create_booking_from_pending(payment_intent_id) is not None:
        return Response(status=status.HTTP_200_OK)

    # フォールバック不能の場合のみ運営へ通知する。
    notified = _notify_unmatched_payment(payment_intent)

    # 通知に失敗した場合は 500 を返し、Stripe の自動再送で再試行させる
    if not notified:
        return Response(status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    return Response(status=status.HTTP_200_OK)