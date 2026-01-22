from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated, AllowAny, BasePermission
from rest_framework.response import Response
from rest_framework.request import Request
from rest_framework import status
from rest_framework.exceptions import ValidationError
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email
from django.db import IntegrityError
from django.utils import timezone
from typing import Any, Dict, Optional, Literal, cast, Tuple
import stripe
import time
import re
import io
import logging
from project.utils import mask_sensitive_id

from .models import BusinessProfile, RegistrationToken
from .serializers import (
    CustomAccountUpdateSerializer,
    BusinessAccountRegistrationSerializer,
    RegistrationRequestSerializer,
    RegistrationTokenVerifySerializer,
)
from .utils import generate_registration_token, send_registration_email, verify_registration_token


User = get_user_model()

stripe.api_key = settings.STRIPE_SECRET_KEY
logger = logging.getLogger(__name__)

BusinessType = Literal['company', 'individual']


def validate_file_type(file: Any) -> Tuple[bool, Optional[str]]:
    """ファイルタイプを検証する"""
    ALLOWED_EXTENSIONS = {'.jpg', '.jpeg', '.png'}
    ALLOWED_MIME_TYPES = {
        'image/jpeg',
        'image/jpg',
        'image/png',
    }

    # マジックナンバー（ファイルの先頭バイト）の定義
    # JPEG: FF D8 FF で始まる
    # PNG: 89 50 4E 47 0D 0A 1A 0A で始まる
    MAGIC_NUMBERS = {
        b'\xff\xd8': ('.jpg', '.jpeg'),
        b'\x89\x50\x4e\x47': ('.png',),
    }

    # ファイル名から拡張子を取得
    file_name = file.name.lower()
    file_extension = None
    if '.' in file_name:
        file_extension = '.' + file_name.rsplit('.', 1)[1]

    # 拡張子の検証
    if file_extension not in ALLOWED_EXTENSIONS:
        return False, '許可されていないファイル形式です。JPEGまたはPNGファイルを選択してください。'

    # MIMEタイプの検証
    if hasattr(file, 'content_type') and file.content_type:
        if file.content_type not in ALLOWED_MIME_TYPES:
            return False, '許可されていないファイル形式です。JPEGまたはPNGファイルを選択してください。'

    # マジックナンバーの検証（拡張子偽装対策）
    try:
        # ファイルの先頭を読み取る（最大8バイト）
        file.seek(0)
        file_header = file.read(8)
        file.seek(0)  # 読み取り位置をリセット

        if len(file_header) < 2:
            return False, 'ファイルが空または破損しています。'

        # マジックナンバーで検証
        is_valid_magic = False
        for magic_bytes, allowed_exts in MAGIC_NUMBERS.items():
            if file_header.startswith(magic_bytes):
                # マジックナンバーから推測される拡張子と実際の拡張子が一致するか確認
                if file_extension in allowed_exts:
                    is_valid_magic = True
                    break

        if not is_valid_magic:
            return False, 'ファイルの内容が画像ファイルではありません。JPEGまたはPNGファイルを選択してください。'
    except Exception as e:
         # マジックナンバー検証に失敗した場合は警告のみ
        logger.warning(f'ファイルマジックナンバー検証エラー: {str(e)}')

    return True, None


def format_phone_number_for_stripe(phone: str) -> str:
    """
    電話番号をStripeのE.164形式に変換する
    日本の電話番号として処理: 最初の0を取り除いて+81を追加
    例: '08012345678' -> '+818012345678'
    """
    if not phone:
        return phone

    # 数字以外の文字を削除（ハイフン、スペースなど）
    phone_digits = re.sub(r'\D', '', phone)

    if not phone_digits:
        return phone

    # すでにE.164形式（+で始まる）の場合はそのまま返す
    if phone.startswith('+'):
        return phone

        # 最初の0を取り除く（例: 080 → 80）
        if phone_digits.startswith('0'):
            phone_digits = phone_digits[1:]
        # +81を追加
    return f'+81{phone_digits}'


class HasStripeCustomAccount(BasePermission):
    """
    認証済みユーザーで、かつStripeアカウントが作成され、審査が通っていることを確認するパーミッションクラス
    """

    message = 'Stripeアカウント未作成または審査未通過のためアクセスできません。'

    def has_permission(self, request: Request, view: Any) -> bool:
        user = getattr(request, 'user', None)
        if not user or not user.is_authenticated:
            return False
        if isinstance(user, AnonymousUser):
            return False

        # ビジネスオーナープロフィールとStripeアカウントIDの存在確認
        if not hasattr(user, 'business_profile') or not user.business_profile.stripe_account_id:
            return False

        # Stripeアカウントの審査状態を確認
        try:
            account_id = user.business_profile.stripe_account_id
            account = stripe.Account.retrieve(account_id)

            # 審査が通っているかチェック
            # 1. currently_dueが空（必要な情報がすべて揃っている）
            # 2. charges_enabledがTrue（決済が有効になっている）
            requirements = getattr(account, 'requirements', None)
            currently_due = requirements.get('currently_due', []) if requirements else []
            charges_enabled = getattr(account, 'charges_enabled', False)

            return len(currently_due) == 0 and charges_enabled
        except stripe.error.StripeError as e:  # type: ignore[attr-defined]
            logger.error(
                f"Stripeアカウント審査状態確認エラー: account_id={mask_sensitive_id(account_id)}, "
                f"error={str(e)}"
            )
            return False


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def custom_get_account(request: Request) -> Response:
    """Stripeアカウント情報を取得するためのAPIエンドポイント"""
    account_id = None
    try:
        if not hasattr(request.user, 'business_profile') or not request.user.business_profile.stripe_account_id:
            return Response({'error': 'アカウントが見つかりません。'}, status=status.HTTP_404_NOT_FOUND)

        account_id = request.user.business_profile.stripe_account_id
        account = stripe.Account.retrieve(account_id)
        return Response({'account_id': account.id, 'account': account})

    except stripe.error.StripeError as e:  # type: ignore[attr-defined]
        logger.error(
            f"Stripeアカウント取得エラー: account_id={mask_sensitive_id(account_id) if account_id else 'None'}, "
            f"user_id={mask_sensitive_id(request.user.id)}, error={str(e)}"
        )
        return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def custom_create_account(request: Request) -> Response:
    """ログイン後のユーザーがStripeアカウントを作成するためのAPIエンドポイント"""
    try:
        # BusinessProfileの存在確認
        if not hasattr(request.user, 'business_profile'):
            return Response(
                {'error': '事業者プロフィールが見つかりません。'},
                status=status.HTTP_404_NOT_FOUND
            )

        business_profile = request.user.business_profile

        # 既にStripeアカウントが存在する場合はエラー
        if business_profile.stripe_account_id:
            return Response(
                {'error': '既にStripeアカウントが作成されています。'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # business_typeを取得（デフォルトは'individual'）
        business_type = request.data.get('business_type', 'individual')
        typed_business_type = cast(BusinessType, business_type)

        # Stripeアカウントを作成
        account = stripe.Account.create(
            type='custom',
            country='JP',
            business_type=typed_business_type,
            capabilities={
                'transfers': {'requested': True},
                'card_payments': {'requested': True},
            },
            settings={
                'payouts': {
                    'schedule': {'interval': 'monthly', 'monthly_anchor': 25}
                }
            },
        )

        # BusinessProfileにStripeアカウントIDを保存
        business_profile.stripe_account_id = account.id
        business_profile.save()

        logger.info(
            f"Stripeアカウント作成成功: account_id={mask_sensitive_id(account.id)}, "
            f"user_id={mask_sensitive_id(request.user.id)}, business_type={typed_business_type}"
        )

        return Response({
            'account_id': account.id,
            'account': account,
            'message': 'Stripeアカウントが作成されました。'
        }, status=status.HTTP_201_CREATED)

    except stripe.error.StripeError as e:  # type: ignore[attr-defined]
        logger.error(
            f"Stripeアカウント作成エラー: user_id={mask_sensitive_id(request.user.id)}, error={str(e)}"
        )
        return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
    except Exception as e:
        logger.error(
            f"Stripeアカウント作成エラー（ログイン後）: user_id={mask_sensitive_id(request.user.id)}, error={str(e)}",
            exc_info=True
        )
        return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def custom_update_account(request: Request) -> Response:
    """Stripeアカウントの情報を更新するためのAPIエンドポイント"""
    try:
        if not hasattr(request.user, 'business_profile') or not request.user.business_profile.stripe_account_id:
            return Response({'error': 'アカウントが見つかりません。'}, status=status.HTTP_404_NOT_FOUND)

        account_id = request.user.business_profile.stripe_account_id

        # 変換が必要なフロントエンド形式のデータ（product_company, rep_infoなど）が送られてきた場合、Stripe API形式に変換
        request_data = request.data.copy() if hasattr(request.data, 'copy') else dict(request.data)

        # 変換が必要なフロントエンド形式のデータかどうかを判定
        frontend_format_keys = ['product_company', 'rep_info', 'bank_info', 'product_details', 'verif_docs']
        if any(key in request_data for key in frontend_format_keys):
            transformed_data: Dict[str, Any] = {}

            # product_company と product_details を business_profile と company に変換
            product_company = request_data.get('product_company') or {}
            product_details = request_data.get('product_details') or {}

            if product_company or product_details:
                # business_profile の構築
                business_profile: Dict[str, Any] = {}
                if product_company.get('product_name'):
                    business_profile['name'] = product_company['product_name']
                if product_company.get('support_email'):
                    business_profile['support_email'] = product_company['support_email']
                if product_details.get('product_url'):
                    business_profile['url'] = product_details['product_url']
                if product_details.get('product_description'):
                    business_profile['product_description'] = product_details['product_description']
                if product_details.get('product_mcc'):
                    business_profile['mcc'] = product_details['product_mcc']

                if business_profile:
                    transformed_data['business_profile'] = business_profile

                # company の構築
                if product_company.get('company_name') or product_company.get('company_address'):
                    company: Dict[str, Any] = {}
                    if product_company.get('company_name'):
                        company['name'] = product_company['company_name']
                    if product_company.get('company_address'):
                        company['address'] = product_company['company_address']
                    if company:
                        transformed_data['company'] = company

            # rep_info を individual に変換
            rep_info = request_data.get('rep_info') or {}
            if rep_info:
                individual: Dict[str, Any] = {}
                if rep_info.get('first_name_kanji'):
                    individual['first_name_kanji'] = rep_info['first_name_kanji']
                if rep_info.get('last_name_kanji'):
                    individual['last_name_kanji'] = rep_info['last_name_kanji']
                if rep_info.get('first_name_kana'):
                    individual['first_name_kana'] = rep_info['first_name_kana']
                if rep_info.get('last_name_kana'):
                    individual['last_name_kana'] = rep_info['last_name_kana']
                if rep_info.get('rep_email'):
                    individual['email'] = rep_info['rep_email']
                if rep_info.get('rep_phone'):
                    individual['phone'] = rep_info['rep_phone']
                if rep_info.get('rep_dob'):
                    individual['dob'] = rep_info['rep_dob']
                # address_kanji と address_kana を直接設定
                if rep_info.get('address_kanji'):
                    individual['address_kanji'] = rep_info['address_kanji']
                if rep_info.get('address_kana'):
                    individual['address_kana'] = rep_info['address_kana']

                if individual:
                    transformed_data['individual'] = individual

            # bank_info を external_account に変換
            bank_info = request_data.get('bank_info') or {}
            if bank_info:
                external_account: Dict[str, Any] = {}
                if bank_info.get('bank_code'):
                    external_account['bank_code'] = bank_info['bank_code']
                if bank_info.get('branch_code'):
                    external_account['branch_code'] = bank_info['branch_code']
                if bank_info.get('account_type'):
                    external_account['account_type'] = bank_info['account_type']
                if bank_info.get('account_number'):
                    external_account['account_number'] = bank_info['account_number']
                if bank_info.get('account_holder_name'):
                    external_account['account_holder_name'] = bank_info['account_holder_name']

                if external_account:
                    transformed_data['external_account'] = external_account

            # verif_docs を verification に変換
            verif_docs = request_data.get('verif_docs') or {}
            if verif_docs:
                verification: Dict[str, Any] = {}
                if verif_docs.get('document_front'):
                    verification['document_front'] = verif_docs['document_front']
                if verif_docs.get('document_back'):
                    verification['document_back'] = verif_docs['document_back']

                if verification:
                    transformed_data['verification'] = verification

            # 変換後のデータを使用
            serializer = CustomAccountUpdateSerializer(data=transformed_data)
        else:
            # 既にバックエンド形式のデータの場合
            serializer = CustomAccountUpdateSerializer(data=request.data)

        serializer.is_valid(raise_exception=True)
        payload = serializer.validated_data

        update_params: Dict[str, Any] = {}

        # Business Profile
        business_profile = payload.get('business_profile') or {}
        if business_profile:
            # 空文字列のフィールドを除外（Stripeは空文字列を許可しない）
            # URLフィールドは特に注意（空文字列は無効なURLとして扱われる）
            cleaned_business_profile = {}
            for key, value in business_profile.items():
                # None、空文字列、空白のみの文字列を除外
                if value is None:
                    continue
                if value == '':
                    continue
                if isinstance(value, str):
                    value = value.strip()
                    if value == '':
                        continue
                cleaned_business_profile[key] = value
            if cleaned_business_profile:
                update_params['business_profile'] = cleaned_business_profile

        # Company
        company = payload.get('company') or {}
        if company:
            cleaned_company: Dict[str, Any] = {}

            # name の処理
            if company.get('name'):
                name = company['name']
                if isinstance(name, str) and name.strip():
                    cleaned_company['name'] = name.strip()

             # address の処理
            if company.get('address'):
                cleaned_address: Dict[str, Any] = {}
                for key, value in company['address'].items():
                    if value is None:
                        continue
                    if value == '':
                        continue
                    if isinstance(value, str):
                        value = value.strip()
                        if value == '':
                            continue

                if cleaned_address:
                    cleaned_company['address'] = cleaned_address

        # Individual (Representative)
        individual = payload.get('individual') or {}
        if individual:
            # 電話番号をE.164形式に変換
            if individual.get('phone'):
                individual['phone'] = format_phone_number_for_stripe(individual['phone'])

            # address_kanji の処理
            if individual.get('address_kanji'):
                address_kanji = individual['address_kanji']
                if not address_kanji.get('country'):
                    address_kanji['country'] = 'JP'
                # country 以外のフィールドに有効な値があるかチェック
                other_fields = {key: value for key, value in address_kanji.items() if key != 'country'}
                has_valid_field = any(other_fields.values())
                if has_valid_field:
                    individual['address_kanji'] = address_kanji
                else:
                    del individual['address_kanji']

            # address_kana の処理
            if individual.get('address_kana'):
                address_kana = individual['address_kana']
                if not address_kana.get('country'):
                    address_kana['country'] = 'JP'
                # country 以外のフィールドに有効な値があるかチェック
                other_fields = {key: value for key, value in address_kana.items() if key != 'country'}
                has_valid_field = any(other_fields.values())
                if has_valid_field:
                    individual['address_kana'] = address_kana
                else:
                    del individual['address_kana']

            # 本人確認書類の処理
            verification = payload.get('verification') or {}
            verification_params: Dict[str, Any] = {}

            if verification.get('document_front') or verification.get('document_back'):
                verification_params['document'] = {}
                if verification.get('document_front'):
                    verification_params['document']['front'] = verification['document_front']
                if verification.get('document_back'):
                    verification_params['document']['back'] = verification['document_back']

            if verification_params:
                individual["verification"] = verification_params

            update_params['individual'] = individual

        # External Account (Bank Account)の処理
        external_account = payload.get('external_account') or {}
        if external_account:
            bank_code = external_account.get('bank_code')
            branch_code = external_account.get('branch_code')
            account_number = external_account.get('account_number')
            account_holder_name = external_account.get('account_holder_name')
            account_type = external_account.get('account_type')

            if all([bank_code, branch_code, account_number, account_holder_name]):
                update_params['external_account'] = {
                    'object': 'bank_account',
                    'country': 'JP',
                    'currency': 'jpy',
                    'routing_number': f'{bank_code}{branch_code}',
                    'account_number': account_number,
                    'account_holder_name': account_holder_name,
                    'account_holder_type': 'company' if account_type == 'toza' else 'individual',
                }

        # 必要に応じて、利用規約の同意を記録
        account = stripe.Account.retrieve(account_id)
        tos_acceptance = getattr(account, 'tos_acceptance', None)
        needs_tos = tos_acceptance is None or tos_acceptance.get('date') is None

        if needs_tos:
            ip = request.META.get('REMOTE_ADDR')
            user_agent = request.META.get('HTTP_USER_AGENT')
            if ip and user_agent:
                update_params['tos_acceptance'] = {
                    'date': int(time.time()),
                    'ip': ip,
                    'user_agent': user_agent,
                }

        if update_params:
            account = stripe.Account.modify(account_id, **update_params)
            logger.info(
                f"Stripeアカウント更新成功: account_id={mask_sensitive_id(account_id)}, "
                f"user_id={mask_sensitive_id(request.user.id)}"
            )
        else:
            account = stripe.Account.retrieve(account_id)

        return Response({'account': account})
    except stripe.error.StripeError as e:  # type: ignore[attr-defined]
        logger.error(
            f"Stripeアカウント更新エラー: account_id={mask_sensitive_id(account_id)}, "
            f"user_id={mask_sensitive_id(request.user.id)}, error={str(e)}"
        )
        return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def custom_account_requirements(request: Request) -> Response:
    """Stripeアカウントの審査要件の状態を取得するためのAPIエンドポイント"""
    try:
        if not hasattr(request.user, 'business_profile') or not request.user.business_profile.stripe_account_id:
            return Response({'error': 'アカウントが見つかりません。'}, status=status.HTTP_404_NOT_FOUND)

        account_id = request.user.business_profile.stripe_account_id
        account = stripe.Account.retrieve(account_id)
        req = account.requirements
        return Response({
            'currently_due': req.get('currently_due', []) if req else [],
            'eventually_due': req.get('eventually_due', []) if req else [],
            'past_due': req.get('past_due', []) if req else [],
        })
    except stripe.error.StripeError as e:  # type: ignore[attr-defined]
        logger.error(
            f"Stripeアカウント要件取得エラー: account_id={mask_sensitive_id(account_id)}, "
            f"user_id={mask_sensitive_id(request.user.id)}, error={str(e)}"
        )
        return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_current_business_profile(request: Request) -> Response:
    """現在ログイン中のビジネスオーナーのプロフィール情報を取得"""
    try:
        if not hasattr(request.user, 'business_profile'):
            return Response(
                {'error': '事業者情報が見つかりません。'},
                status=status.HTTP_404_NOT_FOUND
            )

        business_profile = request.user.business_profile

        return Response({
            'id': str(business_profile.id),
            'company_name': business_profile.company_name,
            'company_email': business_profile.company_email,
            'subdomain': business_profile.subdomain,
            'tax_id': business_profile.tax_id,
            'service_areas': business_profile.service_areas,
            'max_luggage_capacity': business_profile.max_luggage_capacity,
            'operating_hours_start': business_profile.operating_hours_start.isoformat(),
            'operating_hours_end': business_profile.operating_hours_end.isoformat(),
            'operating_days': business_profile.operating_days,
            'pricing_rules': business_profile.pricing_rules,
            'total_orders_completed': business_profile.total_orders_completed,
            'total_revenue': str(business_profile.total_revenue),
            'is_approved': business_profile.is_approved,
            'approval_date': business_profile.approval_date.isoformat() if business_profile.approval_date else None,
            'is_active': business_profile.is_active,
            'deactivated_at': business_profile.deactivated_at.isoformat() if business_profile.deactivated_at else None,
            'created_at': business_profile.created_at.isoformat(),
            'updated_at': business_profile.updated_at.isoformat(),
            'has_stripe_account': bool(business_profile.stripe_account_id),
        }, status=status.HTTP_200_OK)
    except Exception as e:
        logger.error(
            f"ビジネスオーナープロフィール取得エラー: user_id={mask_sensitive_id(request.user.id)}, error={str(e)}",
            exc_info=True
        )
        return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)


@api_view(['GET'])
@permission_classes([AllowAny])
def get_business_profile_by_subdomain(request: Request) -> Response:
    """予約フォームのURLからBusinessProfileを取得"""
    try:
        subdomain = request.query_params.get('subdomain', '').strip().lower()

        if not subdomain:
            return Response(
                {'error': '予約フォームのURLが指定されていません。'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            business_profile = BusinessProfile.objects.get(
                subdomain=subdomain,
                is_active=True
            )
        except BusinessProfile.DoesNotExist:
            return Response(
                {'error': '指定された予約フォームのURLの事業者が見つかりません。'},
                status=status.HTTP_404_NOT_FOUND
            )

        return Response({
            'id': str(business_profile.id),
            'company_name': business_profile.company_name,
            'subdomain': business_profile.subdomain,
            'is_active': business_profile.is_active,
        }, status=status.HTTP_200_OK)
    except Exception as e:
        logger.error(
            f"予約フォームのURL取得エラー: error={str(e)}",
            exc_info=True
        )
        return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)


@api_view(['GET'])
@permission_classes([AllowAny])
def check_email_availability(request: Request) -> Response:
    """メールアドレスの重複チェックAPI"""
    try:
        email = request.query_params.get('email', '').strip().lower()

        if not email:
            return Response(
                {'error': 'メールアドレスが指定されていません。'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # メールアドレスの形式チェック
        try:
            validate_email(email)
        except DjangoValidationError:
            return Response(
                {'error': '有効なメールアドレスを入力してください。'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # 重複チェック
        if User.objects.filter(email=email).exists():
            return Response({
                'available': False,
                'message': 'このメールアドレスは既に登録されています。',
            }, status=status.HTTP_200_OK)

        return Response({
            'available': True,
            'message': 'このメールアドレスは使用できます。',
        }, status=status.HTTP_200_OK)
    except Exception as e:
        logger.error(
            f"メールアドレス重複チェックエラー: error={str(e)}",
            exc_info=True
        )
        return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)


@api_view(['POST'])
@permission_classes([AllowAny])
def request_registration_email(request: Request) -> Response:
    """登録用メール送信リクエストAPI"""
    try:
        serializer = RegistrationRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data['email'].lower()

        # レート制限: 同じメールアドレスに対して5分以内に複数回送信しない
        recent_token = RegistrationToken.objects.filter(
            email=email,
            created_at__gte=timezone.now() - timezone.timedelta(minutes=5)
        ).first()

        if recent_token:
            return Response(
                {'error': 'メール送信は5分に1回までです。しばらく時間をおいて再度お試しください。'},
                status=status.HTTP_429_TOO_MANY_REQUESTS
            )

        # トークンを生成
        registration_token = generate_registration_token(email)

        # メールを送信
        success = send_registration_email(email, registration_token.token)

        if not success:
            logger.error(
                f"登録メール送信リクエスト失敗: email={email}",
                exc_info=True
            )
            return Response(
                {'error': 'メールの送信に失敗しました。しばらく時間をおいて再度お試しください。'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        logger.info(f"登録メール送信リクエスト成功: email={email}")

        return Response({
            'message': 'メールを送信しました。メール内のリンクから登録を行なってください。',
        }, status=status.HTTP_200_OK)
    except Exception as e:
        logger.error(
            f"登録メール送信リクエストエラー: error={str(e)}",
            exc_info=True
        )
        return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)


@api_view(['GET'])
@permission_classes([AllowAny])
def verify_registration_token_api(request: Request) -> Response:
    """登録トークンの検証API"""
    try:
        token = request.query_params.get('token', '').strip()

        # デバッグログ
        logger.info(f"トークン検証リクエスト: token={token[:20]}..., query_params={dict(request.query_params)}")

        if not token:
            logger.warning("トークンが指定されていません")
            return Response(
                {'error': 'リンクが正しくありません。'},
                status=status.HTTP_400_BAD_REQUEST
            )

        registration_token = verify_registration_token(token)

        if not registration_token:
            logger.warning(f"トークンが見つからないか無効: token={token[:20]}...")
            return Response(
                {'error': 'このリンクは有効期限が切れているか、既に使用済みです。'},
                status=status.HTTP_400_BAD_REQUEST
            )

        logger.info(f"トークン検証成功: email={registration_token.email}")
        return Response({
            'valid': True,
            'email': registration_token.email,
        }, status=status.HTTP_200_OK)
    except Exception as e:
        logger.error(
            f"トークン検証エラー: error={str(e)}",
            exc_info=True
        )
        return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)


@api_view(['POST'])
@permission_classes([AllowAny])
def register_business_account(request: Request) -> Response:
    """事業者アカウント登録API（トークン必須）"""
    try:
        serializer = BusinessAccountRegistrationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # トークンを再検証（念のため）
        registration_token = verify_registration_token(data['token'])
        if not registration_token:
            return Response(
                {'error': '有効期限が切れています。お手数おかけしますが、もう一度いちからやり直してください。'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # トークンとメールアドレスの一致確認
        if registration_token.email.lower() != data['email'].lower():
            return Response(
                {'error': '送信されたメールアドレスと一致しません。正しいメールアドレスを入力してください。'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # User作成
        # rep_nameをfirst_nameとlast_nameに分割
        rep_name_parts = data['rep_name'].strip().split(maxsplit=1)
        first_name = rep_name_parts[0] if rep_name_parts else ''
        last_name = rep_name_parts[1] if len(rep_name_parts) > 1 else ''

        user = User.objects.create_user(
            email=data['email'],
            password=data['password'],
            user_type='business_owner',
            phone_number=data['phone'],
            first_name=first_name,
            last_name=last_name,
        )

        # BusinessProfile作成
        BusinessProfile.objects.create(
            user=user,
            company_name=data['company_name'],
            company_email=data['email'],
            subdomain=data['subdomain'],
            stripe_account_id='',  # Stripeアカウントは後で管理画面から作成
        )

        # トークンを使用済みとしてマーク
        registration_token.mark_as_used()

        logger.info(
            f"事業者アカウント登録成功: user_id={mask_sensitive_id(user.id)}, "
            f"subdomain={data['subdomain']}, email={user.email}"
        )

        return Response({
            'message': 'アカウント登録が完了しました',
            'user_id': str(user.id),
            'subdomain': data['subdomain'],
        }, status=status.HTTP_201_CREATED)

    # シリアライザーのバリデーションエラーを処理
    except ValidationError as e:
        return Response(e.detail, status=status.HTTP_400_BAD_REQUEST)

    # データベースのユニーク制約違反をチェック
    except IntegrityError as e:
        error_message = str(e)
        # 予約フォームのURLの重複エラーをチェック
        if 'subdomain' in error_message.lower():
            logger.warning(
                f"予約フォームのURL重複エラー: {error_message}"
            )
            return Response({
                'subdomain': ['この予約フォームのURLは既に使用されています。別の文字列を選択してください。']
            }, status=status.HTTP_400_BAD_REQUEST)
        # その他のIntegrityError（例：emailの重複など）
        logger.error(
            f"データベース整合性エラー: error={str(e)}",
            exc_info=True
        )
        return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    except Exception as e:
        logger.error(
            f"事業者アカウント登録エラー: error={str(e)}",
            exc_info=True
        )
        return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)