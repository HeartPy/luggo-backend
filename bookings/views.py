from rest_framework import generics, status, permissions
from rest_framework.response import Response
from rest_framework.decorators import api_view, permission_classes
from rest_framework.request import Request
from django.db.models import QuerySet
from django.db import transaction
from django.conf import settings
from typing import Any, Optional
from uuid import UUID
import requests
import stripe
import logging
import re
from project.utils import mask_sensitive_id
from business_owners.models import BusinessProfile
from .models import LuggageBooking
from .serializers import LuggageBookingSerializer, LuggageBookingCreateSerializer

stripe.api_key = settings.STRIPE_SECRET_KEY
logger = logging.getLogger(__name__)


def extract_prefecture_code(address: str) -> Optional[str]:
    """住所文字列から都道府県コードを抽出"""
    if not address:
        return None

    # 都道府県名とコードのマッピング
    prefecture_map = {
        '北海道': '01', '青森県': '02', '岩手県': '03', '宮城県': '04',
        '秋田県': '05', '山形県': '06', '福島県': '07', '茨城県': '08',
        '栃木県': '09', '群馬県': '10', '埼玉県': '11', '千葉県': '12',
        '東京都': '13', '神奈川県': '14', '新潟県': '15', '富山県': '16',
        '石川県': '17', '福井県': '18', '山梨県': '19', '長野県': '20',
        '岐阜県': '21', '静岡県': '22', '愛知県': '23', '三重県': '24',
        '滋賀県': '25', '京都府': '26', '大阪府': '27', '兵庫県': '28',
        '奈良県': '29', '和歌山県': '30', '鳥取県': '31', '島根県': '32',
        '岡山県': '33', '広島県': '34', '山口県': '35', '徳島県': '36',
        '香川県': '37', '愛媛県': '38', '高知県': '39', '福岡県': '40',
        '佐賀県': '41', '長崎県': '42', '熊本県': '43', '大分県': '44',
        '宮崎県': '45', '鹿児島県': '46', '沖縄県': '47',
    }

    # 都道府県名を検索
    for prefecture, code in prefecture_map.items():
        if prefecture in address:
            return code

    return None


@api_view(['GET'])
@permission_classes([permissions.AllowAny])
def luggage_items(request: Request) -> Response:
    """ビジネスオーナーの料金設定に基づいて荷物情報を取得"""
    try:
        # リクエストパラメータから取得
        business_owner_id = request.GET.get('business_owner')
        if not business_owner_id:
            return Response(
                {'errMsg': 'ビジネスオーナーが指定されていません。'},
                status=status.HTTP_400_BAD_REQUEST
            )

        pickup_address = request.GET.get('pickup_location_address', '')
        delivery_address = request.GET.get('delivery_location_address', '')

        # ビジネスオーナーを取得
        try:
            business_owner = BusinessProfile.objects.get(id=business_owner_id)
        except BusinessProfile.DoesNotExist:
            return Response(
                {'errMsg': '指定されたビジネスオーナーが見つかりません。'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # 都道府県コードを取得（集荷場所と配送場所の住所から）
        pickup_prefecture_code = extract_prefecture_code(pickup_address)
        delivery_prefecture_code = extract_prefecture_code(delivery_address)

        # 荷物の種類の定義（仮で固定）
        LUGGAGE_ITEMS = [
            {
                'id': 1,
                'name': 'ベビーカー',
                'key': 'baby_stroller_count',
                'image_src': 'baby-stroller.svg',
            },
            {
                'id': 2,
                'name': 'ダンボール',
                'key': 'cardboard_count',
                'image_src': 'cardboard.svg',
            },
            {
                'id': 3,
                'name': 'スーツケースなど',
                'key': 'suitcase_count',
                'image_src': 'suitcase.svg',
            },
        ]

        # ビジネスオーナーの料金設定から料金を取得
        items = []
        for item in LUGGAGE_ITEMS:
            key = item['key']

            # 集荷場所と配送場所の料金を取得
            pickup_price = business_owner.get_price(pickup_prefecture_code, key)
            delivery_price = business_owner.get_price(delivery_prefecture_code, key)

            # 高い方の料金を採用
            price = max(pickup_price, delivery_price)

            if price == 0:
                return Response(
                    {'errMsg': f'{item["name"]}の料金が設定されていません。'},
                    status=status.HTTP_400_BAD_REQUEST
                )

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


@api_view(['GET'])
@permission_classes([permissions.AllowAny])
def location_suggestions(request: Request) -> Response:
    """Google Places APIを使用した場所のサジェスト取得"""
    query = request.GET.get('q', '').strip()

    if not query or len(query) < 2:
        return Response({'suggestions': []})

    try:
        url = 'https://maps.googleapis.com/maps/api/place/textsearch/json'
        params = {
            'query': query,
            'key': settings.GOOGLE_PLACES_API_KEY,
            'language': 'ja',
            'region': 'jp',
            'type': 'establishment',
        }

        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()

        data = response.json()

        if data['status'] != 'OK':
            return Response(
                {'errMsg': f'Google Places API error: {data["status"]}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        allowed_types = {
            'lodging',
            'airport',
            'train_station',
            'subway_station',
        }

        suggestions = []
        for place in data.get('results', []):
            place_types = set(place.get('types', []))

            if place_types.intersection(allowed_types):
                suggestion = {
                    'place_id': place.get('place_id'),
                    'name': place.get('name', ''),
                    'address': place.get('formatted_address', ''),
                    'types': place.get('types', []),
                    'rating': place.get('rating'),
                    'user_ratings_total': place.get('user_ratings_total'),
                    'geometry': {
                        'lat': place.get('geometry', {}).get('location', {}).get('lat'),
                        'lng': place.get('geometry', {}).get('location', {}).get('lng'),
                    }
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
                f"booking_id={mask_sensitive_id(existing_booking.id)}"
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

                # Step2で選択された荷物情報と金額を取得（フロントエンドから送られてくる値をそのまま使用）
                luggage_items = request.data.get('luggage_items', {})
                total_amount = request.data.get('total_amount')

                if total_amount is None:
                    return Response(
                        {'errMsg': '合計金額が指定されていません。'},
                        status=status.HTTP_400_BAD_REQUEST
                    )

                # total_amountを数値に変換
                try:
                    total_amount = int(float(total_amount))
                except (ValueError, TypeError):
                    return Response(
                        {'errMsg': '合計金額の形式が正しくありません。'},
                        status=status.HTTP_400_BAD_REQUEST
                    )

                if total_amount <= 0:
                    return Response(
                        {'errMsg': '合計金額は1円以上である必要があります。'},
                        status=status.HTTP_400_BAD_REQUEST
                    )

                # 荷物情報の検証（最低1点以上の荷物が必要）
                LUGGAGE_ITEM_KEYS = ['baby_stroller_count', 'cardboard_count', 'suitcase_count']

                luggage_counts = {}
                total_items = 0
                for key in LUGGAGE_ITEM_KEYS:
                    # luggage_itemsから取得、なければ個別のキーから取得
                    count = luggage_items.get(key, request.data.get(key, 0))

                    if isinstance(count, str):
                        try:
                            count = int(count)
                            if count < 0:
                                count = 0
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
                    return Response(
                        {'errMsg': '最低1点以上の荷物を選択してください。'},
                        status=status.HTTP_400_BAD_REQUEST
                    )

                # 予約を保存（荷物情報と金額を含める）
                try:
                    booking = serializer.save(
                        luggage_items=luggage_counts,
                        total_amount=total_amount
                    )

                    logger.info(
                        f"予約作成成功: booking_id={mask_sensitive_id(booking.id)}, "
                        f"payment_intent_id={mask_sensitive_id(payment_intent_id)}"
                    )

                    # レスポンス返却用: 作成後に生成されたID、予約番号を含む完全な予約情報をシリアライズ
                    response_serializer = LuggageBookingSerializer(booking)

                    return Response(
                        {
                    'message': '予約が正常に作成されました。',
                    'booking': response_serializer.data
                },
                status=status.HTTP_201_CREATED
            )

                except Exception as save_error:
                    # 予約保存に失敗した場合
                    logger.error(
                        f"予約保存エラー: payment_intent_id={mask_sensitive_id(payment_intent_id)}, "
                        f"error={str(save_error)}",
                        exc_info=True
                    )
                    # トランザクションがロールバックされる
                    raise

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


@api_view(['POST'])
@permission_classes([permissions.AllowAny])
def cancel_booking(request: Request, booking_id: UUID) -> Response:
    """予約キャンセル（予約IDと認証コードで認証）"""
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

    booking.delivery_status = 'cancelled'
    booking.save()

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
            return Response(
                {'errMsg': '合計金額が指定されていません。'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # total_amountを数値に変換
        try:
            amount_in_yen = int(float(total_amount))
        except (ValueError, TypeError):
            return Response(
                {'errMsg': '合計金額の形式が正しくありません。'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if amount_in_yen <= 0:
            return Response(
                {'errMsg': '合計金額は1円以上である必要があります。'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # 荷物情報の検証（最低1点以上の荷物が必要）
        LUGGAGE_ITEM_KEYS = ['baby_stroller_count', 'cardboard_count', 'suitcase_count']

        luggage_counts = {}
        total_items = 0
        for key in LUGGAGE_ITEM_KEYS:
            # luggage_itemsから取得、なければ個別のキーから取得
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
            return Response(
                {'errMsg': '最低1点以上の荷物を選択してください。'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # 決済金額の10%
        platform_fee = int(amount_in_yen * 0.1)

        # 顧客情報を取得
        customer_name = request.data.get('customer_name', '')
        customer_email = request.data.get('customer_email', '')

        connected_account_id = "acct_1SVvis5SisbnvGqu"

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

        return Response(
            {
                'client_secret': payment_intent.client_secret,
                'amount': amount_in_yen,
            },
            status=status.HTTP_200_OK
        )

    except stripe.error.StripeError as e:
        return Response(
            {'errMsg': f'Stripeエラーが発生しました: {str(e)}'},
            status=status.HTTP_400_BAD_REQUEST
        )
    except Exception as e:
        return Response(
            {'errMsg': f'予期しないエラーが発生しました: {str(e)}'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )