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



def _stripe_object_to_dict(obj: Any) -> Dict[str, Any]:
    """Stripeのネストオブジェクトを辞書に変換。部分更新時に既存値をマージするために使用。"""
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return dict(obj)
    try:
        return dict(obj)
    except (TypeError, ValueError):
        return {}


# Account.update の company に送信可能なキーのみ（レスポンスの tax_id_provided, verification 等は送れない）
ALLOWED_COMPANY_UPDATE_KEYS = frozenset({
    'name', 'name_kanji', 'name_kana', 'phone', 'tax_id',
    'address_kanji', 'address_kana', 'directors_provided',
})

# Account.update の business_profile に送信可能なキーのみ
ALLOWED_BUSINESS_PROFILE_KEYS = frozenset({
    'name', 'url', 'mcc', 'product_description', 'support_email', 'support_phone', 'support_url', 'support_address',
    'annual_revenue', 'estimated_worker_count',
})

# Account.update の individual に送信可能なキーのみ（verification は本人確認書類提出用で送信可）
ALLOWED_INDIVIDUAL_UPDATE_KEYS = frozenset({
    'last_name_kanji', 'first_name_kanji', 'last_name_kana', 'first_name_kana', 'email', 'phone', 'dob',
    'address_kanji', 'address_kana', 'verification',
})

# Account.update の settings.payments に送信可能なキーのみ
ALLOWED_SETTINGS_PAYMENTS_KEYS = frozenset({
    'statement_descriptor', 'statement_descriptor_kana', 'statement_descriptor_kanji',
})


def _company_for_update(company_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Stripe Account.update に送る company から、送信不可キーを除き、address_kanji 等のネストした値は dict に変換する。"""
    out: Dict[str, Any] = {}
    for key, value in company_dict.items():
        if key not in ALLOWED_COMPANY_UPDATE_KEYS:
            continue
        if key in ('address_kanji', 'address_kana') and value is not None and not isinstance(value, dict):
            value = _stripe_object_to_dict(value)
        out[key] = value
    return out


def _business_profile_for_update(bp_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Account.update に送る business_profile から、送信不可キーを除く。"""
    return {key: value for key, value in bp_dict.items() if key in ALLOWED_BUSINESS_PROFILE_KEYS}


def _individual_for_update(individual_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Stripe Account.update に送る individual から、送信不可キーを除き、address_kanji 等のネストした値は dict に変換する。"""
    out: Dict[str, Any] = {}
    for key, value in individual_dict.items():
        if key not in ALLOWED_INDIVIDUAL_UPDATE_KEYS:
            continue
        if key in ('address_kanji', 'address_kana') and value is not None and not isinstance(value, dict):
            value = _stripe_object_to_dict(value)
        if key == 'verification' and value is not None and not isinstance(value, dict):
            value = _stripe_object_to_dict(value)
        out[key] = value
    return out


def _settings_payments_for_update(payments_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Account.update に送る settings.payments から、送信不可キーを除く。"""
    return {key: value for key, value in payments_dict.items() if key in ALLOWED_SETTINGS_PAYMENTS_KEYS}


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

        except Exception as e:
            logger.error(
                f"Stripeアカウント審査状態確認予期しないエラー: account_id={mask_sensitive_id(account_id)}, "
                f"error={str(e)}",
                exc_info=True
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

    except Exception as e:
        logger.error(
            f"Stripeアカウント取得予期しないエラー: account_id={mask_sensitive_id(account_id) if account_id else 'None'}, "
            f"user_id={mask_sensitive_id(request.user.id)}, error={str(e)}",
            exc_info=True
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
        # アプリ登録時のメールアドレスをStripeアカウントのメールアドレスとして設定
        account = stripe.Account.create(
            type='custom',
            country='JP',
            business_type=typed_business_type,
            email=business_profile.company_email,
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

        account_id = account.id

        # フォームデータが送信されている場合は、アカウント情報を更新
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
                business_profile_data: Dict[str, Any] = {}
                company_name = product_company.get('company_name')
                if company_name and isinstance(company_name, str) and company_name.strip():
                    business_profile_data['name'] = company_name.strip()

                # business_profile.name_kanaはStripe APIでサポートされていないため、設定しない
                # name_kanaはcompany.name_kanaとして設定される

                support_email = product_company.get('support_email')
                if support_email and isinstance(support_email, str) and support_email.strip():
                    business_profile_data['support_email'] = support_email.strip()

                product_url = product_details.get('product_url')
                if product_url and isinstance(product_url, str) and product_url.strip():
                    product_url = product_url.strip()
                    business_profile_data['url'] = product_url

                product_description = product_details.get('product_description')
                if product_description and isinstance(product_description, str) and product_description.strip():
                    business_profile_data['product_description'] = product_description.strip()

                product_mcc = product_details.get('product_mcc')
                if product_mcc:
                    business_profile_data['mcc'] = product_mcc

                if business_profile_data:
                    transformed_data['business_profile'] = business_profile_data

                # company の構築
                company_data: Dict[str, Any] = {}
                rep_info = request_data.get('rep_info') or {}

                company_name_romaji = product_company.get('company_name_romaji', '')
                if company_name_romaji and isinstance(company_name_romaji, str) and company_name_romaji.strip():
                    company_data['name'] = company_name_romaji.strip()

                company_name = product_company.get('company_name', '')
                if company_name and isinstance(company_name, str) and company_name.strip():
                    company_data['name_kanji'] = company_name.strip()

                company_name_kana = product_company.get('company_name_kana', '')
                if company_name_kana and isinstance(company_name_kana, str) and company_name_kana.strip():
                    company_data['name_kana'] = company_name_kana.strip()

                company_address_kanji = product_company.get('company_address_kanji')
                if company_address_kanji and isinstance(company_address_kanji, dict):
                    company_data['address_kanji'] = company_address_kanji

                company_address_kana = product_company.get('company_address_kana')
                if company_address_kana and isinstance(company_address_kana, dict):
                    company_data['address_kana'] = company_address_kana

                if typed_business_type == 'company':
                    rep_phone = rep_info.get('rep_phone')
                    if rep_phone and isinstance(rep_phone, str) and rep_phone.strip():
                        company_data['phone'] = format_phone_number_for_stripe(rep_phone.strip())

                    tax_id = product_company.get('tax_id')
                    if tax_id and isinstance(tax_id, str) and tax_id.strip():
                        company_data['tax_id'] = tax_id.strip()

                    directors = rep_info.get('directors')
                    if isinstance(directors, list):
                        company_data['directors_provided'] = True

                if company_data:
                    transformed_data['company'] = company_data

                # 明細書表記（settings.payments）の構築
                payments: Dict[str, Any] = {}
                for key in ('statement_descriptor', 'statement_descriptor_kana', 'statement_descriptor_kanji'):
                    value = product_company.get(key)
                    if value and isinstance(value, str) and value.strip():
                        payments[key] = value.strip()

                if payments:
                    transformed_data['settings'] = {'payments': payments}

            # rep_info を individual に変換
            rep_info = request_data.get('rep_info') or {}

            if rep_info:
                individual: Dict[str, Any] = {}
                last_name_kanji = rep_info.get('last_name_kanji')
                if last_name_kanji and isinstance(last_name_kanji, str) and last_name_kanji.strip():
                    individual['last_name_kanji'] = last_name_kanji.strip()

                first_name_kanji = rep_info.get('first_name_kanji')
                if first_name_kanji and isinstance(first_name_kanji, str) and first_name_kanji.strip():
                    individual['first_name_kanji'] = first_name_kanji.strip()

                last_name_kana = rep_info.get('last_name_kana')
                if last_name_kana and isinstance(last_name_kana, str) and last_name_kana.strip():
                    individual['last_name_kana'] = last_name_kana.strip()

                first_name_kana = rep_info.get('first_name_kana')
                if first_name_kana and isinstance(first_name_kana, str) and first_name_kana.strip():
                    individual['first_name_kana'] = first_name_kana.strip()

                rep_email = rep_info.get('rep_email')
                if rep_email and isinstance(rep_email, str) and rep_email.strip():
                    individual['email'] = rep_email.strip()

                rep_phone = rep_info.get('rep_phone')
                if rep_phone and isinstance(rep_phone, str) and rep_phone.strip():
                    individual['phone'] = format_phone_number_for_stripe(rep_phone.strip())

                rep_dob = rep_info.get('rep_dob')
                if rep_dob and isinstance(rep_dob, dict):
                    individual['dob'] = rep_dob

                address_kanji = rep_info.get('address_kanji')
                if address_kanji and isinstance(address_kanji, dict):
                    individual['address_kanji'] = address_kanji

                address_kana = rep_info.get('address_kana')
                if address_kana and isinstance(address_kana, dict):
                    individual['address_kana'] = address_kana

                if individual:
                    transformed_data['individual'] = individual

            # bank_info を external_account に変換
            bank_info = request_data.get('bank_info') or {}

            if bank_info:
                external_account: Dict[str, Any] = {}
                bank_code = bank_info.get('bank_code')
                if bank_code and isinstance(bank_code, str) and bank_code.strip():
                    external_account['bank_code'] = bank_code.strip()

                branch_code = bank_info.get('branch_code')
                if branch_code and isinstance(branch_code, str) and branch_code.strip():
                    external_account['branch_code'] = branch_code.strip()

                account_type = bank_info.get('account_type')
                if account_type and isinstance(account_type, str) and account_type.strip():
                    external_account['account_type'] = account_type.strip()

                account_number = bank_info.get('account_number')
                if account_number and isinstance(account_number, str) and account_number.strip():
                    external_account['account_number'] = account_number.strip()

                account_holder_name = bank_info.get('account_holder_name')
                if account_holder_name and isinstance(account_holder_name, str) and account_holder_name.strip():
                    external_account['account_holder_name'] = account_holder_name.strip()

                if external_account:
                    transformed_data['external_account'] = external_account

            # verif_docs を verification に変換
            verif_docs = request_data.get('verif_docs') or {}

            if verif_docs:
                verification: Dict[str, Any] = {}
                document_front = verif_docs.get('document_front')
                if document_front and isinstance(document_front, str) and document_front.strip():
                    verification['document_front'] = document_front.strip()
                document_back = verif_docs.get('document_back')
                if document_back and isinstance(document_back, str) and document_back.strip():
                    verification['document_back'] = document_back.strip()

                if verification:
                    transformed_data['verification'] = verification

            # 変換後のデータを使用
            serializer = CustomAccountUpdateSerializer(data=transformed_data)
        else:
            # 既にバックエンド形式のデータの場合
            serializer = CustomAccountUpdateSerializer(data=request.data)

        # フォームデータがある場合のみ更新処理を実行
        if serializer.is_valid():
            try:
                payload = serializer.validated_data
                update_params: Dict[str, Any] = {}

                # Business Profile
                business_profile_payload = payload.get('business_profile') or {}
                if business_profile_payload:
                    cleaned_business_profile: Dict[str, Any] = {}
                    for key, value in business_profile_payload.items():
                        # 文字列以外、空文字列、空白のみの文字列を除外
                        if not isinstance(value, str):
                            continue
                        value = value.strip()
                        if value == '':
                            continue
                        cleaned_business_profile[key] = value

                    if cleaned_business_profile:
                        update_params['business_profile'] = cleaned_business_profile

                # Settings（明細書表記など）
                settings_payload = payload.get('settings') or {}
                if settings_payload:
                    cleaned_settings: Dict[str, Any] = {}
                    payments_payload = settings_payload.get('payments') or {}

                    if payments_payload:
                        cleaned_payments: Dict[str, Any] = {}
                        for key, value in payments_payload.items():
                            # 文字列以外、空文字列、空白のみの文字列を除外
                            if not isinstance(value, str):
                                continue
                            value = value.strip()
                            if value == '':
                                continue
                            cleaned_payments[key] = value

                        if cleaned_payments:
                            cleaned_settings['payments'] = cleaned_payments

                    if cleaned_settings:
                        update_params['settings'] = cleaned_settings

                # Company（business_typeが'company'の場合のみcompanyパラメータを使用）
                if typed_business_type == 'company':
                    company = payload.get('company') or {}
                    if company:
                        cleaned_company: Dict[str, Any] = {}

                        if company.get('name'):
                            name = company['name']
                            if isinstance(name, str) and name.strip():
                                cleaned_company['name'] = name.strip()

                        if company.get('name_kanji'):
                            name_kanji = company['name_kanji']
                            if isinstance(name_kanji, str) and name_kanji.strip():
                                cleaned_company['name_kanji'] = name_kanji.strip()

                        if company.get('name_kana'):
                            name_kana = company['name_kana']
                            if isinstance(name_kana, str) and name_kana.strip():
                                cleaned_company['name_kana'] = name_kana.strip()

                        if company.get('phone'):
                            phone = company['phone']
                            if isinstance(phone, str) and phone.strip():
                                cleaned_company['phone'] = format_phone_number_for_stripe(phone.strip())

                        if company.get('tax_id'):
                            tax_id = company['tax_id']
                            if isinstance(tax_id, str) and tax_id.strip():
                                cleaned_company['tax_id'] = tax_id.strip()

                        # address_kanji の処理
                        address_kanji = company.get('address_kanji')
                        if isinstance(address_kanji, dict):
                            cleaned_address_kanji: Dict[str, Any] = {}
                            for key, value in address_kanji.items():
                                # 文字列以外、空文字列、空白のみの文字列を除外
                                if not isinstance(value, str):
                                    continue
                                value = value.strip()
                                if value == '':
                                    continue
                                cleaned_address_kanji[key] = value

                            if not cleaned_address_kanji.get('country'):
                                cleaned_address_kanji['country'] = 'JP'

                            other_fields = {key: value for key, value in cleaned_address_kanji.items() if key != 'country'}

                            if other_fields:
                                cleaned_company['address_kanji'] = cleaned_address_kanji

                        # address_kana の処理
                        address_kana = company.get('address_kana')
                        if isinstance(address_kana, dict):
                            cleaned_address_kana: Dict[str, Any] = {}
                            for key, value in address_kana.items():
                                # 文字列以外、空文字列、空白のみの文字列を除外
                                if not isinstance(value, str):
                                    continue
                                value = value.strip()
                                if value == '':
                                    continue
                                cleaned_address_kana[key] = value

                            if not cleaned_address_kana.get('country'):
                                cleaned_address_kana['country'] = 'JP'

                            other_fields = {key: value for key, value in cleaned_address_kana.items() if key != 'country'}

                            if other_fields:
                                cleaned_company['address_kana'] = cleaned_address_kana

                        if cleaned_company:
                            update_params['company'] = cleaned_company

                # Individual（business_typeが'individual'の場合のみindividualパラメータを使用）
                if typed_business_type == 'individual':
                    individual = payload.get('individual') or {}
                    if individual:
                        cleaned_individual: Dict[str, Any] = {}

                        if individual.get('last_name_kanji'):
                            last_name_kanji = individual['last_name_kanji']
                            if isinstance(last_name_kanji, str) and last_name_kanji.strip():
                                cleaned_individual['last_name_kanji'] = last_name_kanji.strip()

                        if individual.get('first_name_kanji'):
                            first_name_kanji = individual['first_name_kanji']
                            if isinstance(first_name_kanji, str) and first_name_kanji.strip():
                                cleaned_individual['first_name_kanji'] = first_name_kanji.strip()

                        if individual.get('last_name_kana'):
                            last_name_kana = individual['last_name_kana']
                            if isinstance(last_name_kana, str) and last_name_kana.strip():
                                cleaned_individual['last_name_kana'] = last_name_kana.strip()

                        if individual.get('first_name_kana'):
                            first_name_kana = individual['first_name_kana']
                            if isinstance(first_name_kana, str) and first_name_kana.strip():
                                cleaned_individual['first_name_kana'] = first_name_kana.strip()

                        if individual.get('email'):
                            email = individual['email']
                            if isinstance(email, str) and email.strip():
                                cleaned_individual['email'] = email.strip()

                        if individual.get('phone'):
                            phone = individual['phone']
                            if isinstance(phone, str) and phone.strip():
                                cleaned_individual['phone'] = format_phone_number_for_stripe(phone.strip())

                        dob = individual.get('dob')
                        if isinstance(dob, dict) and dob:
                            cleaned_individual['dob'] = dob

                        # address_kanji の処理
                        address_kanji = individual.get('address_kanji')
                        if isinstance(address_kanji, dict):
                            cleaned_address_kanji: Dict[str, Any] = {}
                            for key, value in address_kanji.items():
                                # 文字列以外、空文字列、空白のみの文字列を除外
                                if not isinstance(value, str):
                                    continue
                                value = value.strip()
                                if value == '':
                                    continue
                                cleaned_address_kanji[key] = value

                            if not cleaned_address_kanji.get('country'):
                                cleaned_address_kanji['country'] = 'JP'

                            other_fields = {key: value for key, value in cleaned_address_kanji.items() if key != 'country'}

                            if other_fields:
                                cleaned_individual['address_kanji'] = cleaned_address_kanji

                        # address_kana の処理
                        address_kana = individual.get('address_kana')
                        if isinstance(address_kana, dict):
                            cleaned_address_kana: Dict[str, Any] = {}
                            for key, value in address_kana.items():
                                # 文字列以外、空文字列、空白のみの文字列を除外
                                if not isinstance(value, str):
                                    continue
                                value = value.strip()
                                if value == '':
                                    continue
                                cleaned_address_kana[key] = value

                            if not cleaned_address_kana.get('country'):
                                cleaned_address_kana['country'] = 'JP'

                            other_fields = {key: value for key, value in cleaned_address_kana.items() if key != 'country'}

                            if other_fields:
                                cleaned_individual['address_kana'] = cleaned_address_kana

                        # 本人確認書類の処理（アカウントが検証済みの場合は送信しない）
                        # 新規作成時は検証済みではないため、常に送信可能
                        verification = payload.get('verification') or {}
                        verification_params: Dict[str, Any] = {}
                        document: Dict[str, str] = {}

                        document_front = verification.get('document_front')
                        if isinstance(document_front, str) and document_front.strip():
                            document['front'] = document_front.strip()

                        document_back = verification.get('document_back')
                        if isinstance(document_back, str) and document_back.strip():
                            document['back'] = document_back.strip()

                        if document:
                            verification_params['document'] = document
                        if verification_params:
                            cleaned_individual['verification'] = verification_params

                        if cleaned_individual:
                            update_params['individual'] = cleaned_individual

                # External Account (Bank Account)の処理
                external_account = payload.get('external_account') or {}
                if external_account:
                    bank_code_raw = external_account.get('bank_code')
                    bank_code = bank_code_raw.strip() if isinstance(bank_code_raw, str) else None

                    branch_code_raw = external_account.get('branch_code')
                    branch_code = branch_code_raw.strip() if isinstance(branch_code_raw, str) else None

                    account_number_raw = external_account.get('account_number')
                    account_number = account_number_raw.strip() if isinstance(account_number_raw, str) else None

                    account_holder_name_raw = external_account.get('account_holder_name')
                    account_holder_name = account_holder_name_raw.strip() if isinstance(account_holder_name_raw, str) else None

                    account_type_raw = external_account.get('account_type')
                    account_type = account_type_raw.strip() if isinstance(account_type_raw, str) else None

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

                # 利用規約の同意を記録
                ip = request.META.get('REMOTE_ADDR')
                user_agent = request.META.get('HTTP_USER_AGENT')
                if ip and user_agent:
                    update_params['tos_acceptance'] = {
                        'date': int(time.time()),
                        'ip': ip,
                        'user_agent': user_agent,
                    }

                # アカウント情報を更新
                if update_params:
                    account = stripe.Account.modify(account_id, **update_params)

                # business_typeが'company'の場合、Persons APIで代表者と取締役を追加
                if typed_business_type == 'company':
                    rep_info = request_data.get('rep_info') or {}
                    directors_info = rep_info.get('directors', []) if rep_info else []

                    # 代表者を追加（代表者兼取締役として）
                    if rep_info:
                        rep_title_raw = rep_info.get('rep_title')
                        rep_title = rep_title_raw.strip() if isinstance(rep_title_raw, str) and rep_title_raw.strip() else '代表取締役'
                        person_params: Dict[str, Any] = {
                            'relationship': {
                                'representative': True,
                                'director': True,
                                'title': rep_title,
                            },
                        }

                        last_name_kanji = rep_info.get('last_name_kanji')
                        if isinstance(last_name_kanji, str) and last_name_kanji.strip():
                            person_params['last_name_kanji'] = last_name_kanji.strip()

                        first_name_kanji = rep_info.get('first_name_kanji')
                        if isinstance(first_name_kanji, str) and first_name_kanji.strip():
                            person_params['first_name_kanji'] = first_name_kanji.strip()

                        last_name_kana = rep_info.get('last_name_kana')
                        if isinstance(last_name_kana, str) and last_name_kana.strip():
                            person_params['last_name_kana'] = last_name_kana.strip()

                        first_name_kana = rep_info.get('first_name_kana')
                        if isinstance(first_name_kana, str) and first_name_kana.strip():
                            person_params['first_name_kana'] = first_name_kana.strip()

                        rep_email = rep_info.get('rep_email')
                        if isinstance(rep_email, str) and rep_email.strip():
                            person_params['email'] = rep_email.strip()

                        rep_phone = rep_info.get('rep_phone')
                        if isinstance(rep_phone, str) and rep_phone.strip():
                            person_params['phone'] = format_phone_number_for_stripe(rep_phone.strip())

                        rep_dob = rep_info.get('rep_dob')
                        if isinstance(rep_dob, dict) and rep_dob:
                            person_params['dob'] = rep_dob

                        # address_kanji の処理
                        address_kanji = rep_info.get('address_kanji')
                        if isinstance(address_kanji, dict):
                            cleaned_address_kanji: Dict[str, Any] = {}
                            for key, value in address_kanji.items():
                                # 文字列以外、空文字列、空白のみの文字列を除外
                                if not isinstance(value, str):
                                    continue
                                value = value.strip()
                                if value == '':
                                    continue
                                cleaned_address_kanji[key] = value

                            if not cleaned_address_kanji.get('country'):
                                cleaned_address_kanji['country'] = 'JP'

                            other_fields = {key: value for key, value in cleaned_address_kanji.items() if key != 'country'}

                            if other_fields:
                                person_params['address_kanji'] = cleaned_address_kanji

                        # address_kana の処理
                        address_kana = rep_info.get('address_kana')
                        if isinstance(address_kana, dict):
                            cleaned_address_kana: Dict[str, Any] = {}
                            for key, value in address_kana.items():
                                # 文字列以外、空文字列、空白のみの文字列を除外
                                if not isinstance(value, str):
                                    continue
                                value = value.strip()
                                if value == '':
                                    continue
                                cleaned_address_kana[key] = value

                            if not cleaned_address_kana.get('country'):
                                cleaned_address_kana['country'] = 'JP'

                            other_fields = {key: value for key, value in cleaned_address_kana.items() if key != 'country'}

                            if other_fields:
                                person_params['address_kana'] = cleaned_address_kana

                        # 本人確認書類の処理（アカウントが検証済みの場合は送信しない）
                        # 新規作成時は検証済みではないため、常に送信可能
                        verif_docs = request_data.get('verif_docs') or {}
                        document: Dict[str, str] = {}

                        document_front = verif_docs.get('document_front')
                        if isinstance(document_front, str) and document_front.strip():
                            document['front'] = document_front.strip()

                        document_back = verif_docs.get('document_back')
                        if isinstance(document_back, str) and document_back.strip():
                            document['back'] = document_back.strip()

                        if document:
                            person_params['verification'] = {'document': document}

                        # Persons APIで代表者を追加
                        stripe.Account.create_person(account_id, **person_params)

                    # 他の取締役を追加
                    for director_info in directors_info:
                        director_title_raw = director_info.get('title')
                        director_title = director_title_raw.strip() if isinstance(director_title_raw, str) and director_title_raw.strip() else '取締役'
                        director_params: Dict[str, Any] = {
                            'relationship': {'director': True, 'title': director_title},
                        }

                        last_name_kanji = director_info.get('last_name_kanji')
                        if isinstance(last_name_kanji, str) and last_name_kanji.strip():
                            director_params['last_name_kanji'] = last_name_kanji.strip()

                        first_name_kanji = director_info.get('first_name_kanji')
                        if isinstance(first_name_kanji, str) and first_name_kanji.strip():
                            director_params['first_name_kanji'] = first_name_kanji.strip()

                        last_name_kana = director_info.get('last_name_kana')
                        if isinstance(last_name_kana, str) and last_name_kana.strip():
                            director_params['last_name_kana'] = last_name_kana.strip()

                        first_name_kana = director_info.get('first_name_kana')
                        if isinstance(first_name_kana, str) and first_name_kana.strip():
                            director_params['first_name_kana'] = first_name_kana.strip()

                        email = director_info.get('email')
                        if isinstance(email, str) and email.strip():
                            director_params['email'] = email.strip()

                        phone = director_info.get('phone')
                        if isinstance(phone, str) and phone.strip():
                            director_params['phone'] = format_phone_number_for_stripe(phone.strip())

                        dob = director_info.get('dob')
                        if isinstance(dob, dict) and dob:
                            director_params['dob'] = dob

                        # address_kanji の処理
                        address_kanji = director_info.get('address_kanji')
                        if isinstance(address_kanji, dict):
                            cleaned_address_kanji: Dict[str, Any] = {}
                            for key, value in address_kanji.items():
                                # 文字列以外、空文字列、空白のみの文字列を除外
                                if not isinstance(value, str):
                                    continue
                                value = value.strip()
                                if value == '':
                                    continue
                                cleaned_address_kanji[key] = value

                            if not cleaned_address_kanji.get('country'):
                                cleaned_address_kanji['country'] = 'JP'

                            other_fields = {key: value for key, value in cleaned_address_kanji.items() if key != 'country'}

                            if other_fields:
                                director_params['address_kanji'] = cleaned_address_kanji

                        # address_kana の処理
                        address_kana = director_info.get('address_kana')
                        if isinstance(address_kana, dict):
                            cleaned_address_kana: Dict[str, Any] = {}
                            for key, value in address_kana.items():
                                # 文字列以外、空文字列、空白のみの文字列を除外
                                if not isinstance(value, str):
                                    continue
                                value = value.strip()
                                if value == '':
                                    continue
                                cleaned_address_kana[key] = value

                            if not cleaned_address_kana.get('country'):
                                cleaned_address_kana['country'] = 'JP'

                            other_fields = {key: value for key, value in cleaned_address_kana.items() if key != 'country'}

                            if other_fields:
                                director_params['address_kana'] = cleaned_address_kana

                        # Persons APIで取締役を追加
                        stripe.Account.create_person(account_id, **director_params)

                    # Stripe 要件: 取締役を Persons API で登録した後に directors_provided を true で更新する。
                    # company を部分指定だけすると既存の法人名・カナ・ローマ字等が上書きされるため、初回送信分を維持して directors_provided のみ追加する。
                    company_with_directors = {**(update_params.get('company') or {}), 'directors_provided': True}
                    stripe.Account.modify(account_id, company=company_with_directors)

            except stripe.error.StripeError as update_error:  # type: ignore[attr-defined]
                # 更新処理でエラーが発生した場合、stripe_account_idをロールバック
                business_profile.stripe_account_id = ''
                business_profile.save()
                logger.error(
                    f"Stripeアカウント更新エラー（作成後）: account_id={mask_sensitive_id(account_id)}, "
                    f"user_id={mask_sensitive_id(request.user.id)}, error={str(update_error)}"
                )
                raise update_error

            except Exception as update_error:
                # 更新処理でエラーが発生した場合、stripe_account_idをロールバック
                business_profile.stripe_account_id = ''
                business_profile.save()
                logger.error(
                    f"Stripeアカウント更新予期しないエラー（作成後）: account_id={mask_sensitive_id(account_id)}, "
                    f"user_id={mask_sensitive_id(request.user.id)}, error={str(update_error)}",
                    exc_info=True
                )
                raise update_error

        logger.info(
            f"Stripeアカウント作成成功: account_id={mask_sensitive_id(account_id)}, "
            f"user_id={mask_sensitive_id(request.user.id)}, business_type={typed_business_type}"
        )

        return Response({
            'account_id': account_id,
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

            # business_typeを取得（更新の場合は既存のアカウントから取得）
            account = stripe.Account.retrieve(account_id)
            account_business_type = getattr(account, 'business_type', None)

            if product_company or product_details:
                # business_profile の構築
                business_profile: Dict[str, Any] = {}
                company_name = product_company.get('company_name')
                if company_name and isinstance(company_name, str) and company_name.strip():
                    business_profile['name'] = company_name.strip()

                # business_profile.name_kanaはStripe APIでサポートされていないため、設定しない
                # name_kanaはcompany.name_kanaとして設定される

                support_email = product_company.get('support_email')
                if support_email and isinstance(support_email, str) and support_email.strip():
                    business_profile['support_email'] = support_email.strip()

                product_url = product_details.get('product_url')
                if product_url and isinstance(product_url, str) and product_url.strip():
                    product_url = product_url.strip()
                    business_profile['url'] = product_url

                product_description = product_details.get('product_description')
                if product_description and isinstance(product_description, str) and product_description.strip():
                    business_profile['product_description'] = product_description.strip()

                product_mcc = product_details.get('product_mcc')
                if product_mcc:
                    business_profile['mcc'] = product_mcc

                if business_profile:
                    transformed_data['business_profile'] = business_profile

                # company の構築
                company_data: Dict[str, Any] = {}
                rep_info = request_data.get('rep_info') or {}

                company_name_romaji = product_company.get('company_name_romaji', '')
                if company_name_romaji and isinstance(company_name_romaji, str) and company_name_romaji.strip():
                    company_data['name'] = company_name_romaji.strip()

                company_name = product_company.get('company_name', '')
                if company_name and isinstance(company_name, str) and company_name.strip():
                    company_data['name_kanji'] = company_name.strip()

                company_name_kana = product_company.get('company_name_kana', '')
                if company_name_kana and isinstance(company_name_kana, str) and company_name_kana.strip():
                    company_data['name_kana'] = company_name_kana.strip()

                company_address_kanji = product_company.get('company_address_kanji')
                if isinstance(company_address_kanji, dict) and company_address_kanji:
                    company_data['address_kanji'] = company_address_kanji

                company_address_kana = product_company.get('company_address_kana')
                if isinstance(company_address_kana, dict) and company_address_kana:
                    company_data['address_kana'] = company_address_kana

                if account_business_type == 'company':
                    rep_phone = rep_info.get('rep_phone')
                    if rep_phone and isinstance(rep_phone, str) and rep_phone.strip():
                        company_data['phone'] = format_phone_number_for_stripe(rep_phone.strip())

                    tax_id = product_company.get('tax_id')
                    if tax_id and isinstance(tax_id, str) and tax_id.strip():
                        company_data['tax_id'] = tax_id.strip()

                    directors = rep_info.get('directors')
                    if isinstance(directors, list):
                        company_data['directors_provided'] = True

                if company_data:
                    transformed_data['company'] = company_data

                # 明細書表記（settings.payments）の構築
                payments_update: Dict[str, Any] = {}
                for key in ('statement_descriptor', 'statement_descriptor_kana', 'statement_descriptor_kanji'):
                    value = product_company.get(key)
                    if value and isinstance(value, str) and value.strip():
                        payments_update[key] = value.strip()
                if payments_update:
                    transformed_data['settings'] = {'payments': payments_update}

            # rep_info を individual に変換
            rep_info = request_data.get('rep_info') or {}

            if rep_info:
                individual: Dict[str, Any] = {}
                last_name_kanji = rep_info.get('last_name_kanji')
                if last_name_kanji and isinstance(last_name_kanji, str) and last_name_kanji.strip():
                    individual['last_name_kanji'] = last_name_kanji.strip()

                first_name_kanji = rep_info.get('first_name_kanji')
                if first_name_kanji and isinstance(first_name_kanji, str) and first_name_kanji.strip():
                    individual['first_name_kanji'] = first_name_kanji.strip()

                last_name_kana = rep_info.get('last_name_kana')
                if last_name_kana and isinstance(last_name_kana, str) and last_name_kana.strip():
                    individual['last_name_kana'] = last_name_kana.strip()

                first_name_kana = rep_info.get('first_name_kana')
                if first_name_kana and isinstance(first_name_kana, str) and first_name_kana.strip():
                    individual['first_name_kana'] = first_name_kana.strip()

                rep_email = rep_info.get('rep_email')
                if rep_email and isinstance(rep_email, str) and rep_email.strip():
                    individual['email'] = rep_email.strip()

                rep_phone = rep_info.get('rep_phone')
                if rep_phone and isinstance(rep_phone, str) and rep_phone.strip():
                    individual['phone'] = format_phone_number_for_stripe(rep_phone.strip())

                rep_dob = rep_info.get('rep_dob')
                if rep_dob and isinstance(rep_dob, dict):
                    individual['dob'] = rep_dob

                address_kanji = rep_info.get('address_kanji')
                if address_kanji and isinstance(address_kanji, dict):
                    individual['address_kanji'] = address_kanji

                address_kana = rep_info.get('address_kana')
                if address_kana and isinstance(address_kana, dict):
                    individual['address_kana'] = address_kana

                if individual:
                    transformed_data['individual'] = individual

            # bank_info を external_account に変換
            bank_info = request_data.get('bank_info') or {}

            if bank_info:
                external_account: Dict[str, Any] = {}
                bank_code = bank_info.get('bank_code')
                if bank_code and isinstance(bank_code, str) and bank_code.strip():
                    external_account['bank_code'] = bank_code.strip()

                branch_code = bank_info.get('branch_code')
                if branch_code and isinstance(branch_code, str) and branch_code.strip():
                    external_account['branch_code'] = branch_code.strip()

                account_type = bank_info.get('account_type')
                if account_type and isinstance(account_type, str) and account_type.strip():
                    external_account['account_type'] = account_type.strip()

                account_number = bank_info.get('account_number')
                if account_number and isinstance(account_number, str) and account_number.strip():
                    external_account['account_number'] = account_number.strip()

                account_holder_name = bank_info.get('account_holder_name')
                if account_holder_name and isinstance(account_holder_name, str) and account_holder_name.strip():
                    external_account['account_holder_name'] = account_holder_name.strip()

                if external_account:
                    transformed_data['external_account'] = external_account

            # verif_docs を verification に変換
            verif_docs = request_data.get('verif_docs') or {}

            if verif_docs:
                verification: Dict[str, Any] = {}
                document_front = verif_docs.get('document_front')
                if document_front and isinstance(document_front, str) and document_front.strip():
                    verification['document_front'] = document_front.strip()

                document_back = verif_docs.get('document_back')
                if document_back and isinstance(document_back, str) and document_back.strip():
                    verification['document_back'] = document_back.strip()

                if verification:
                    transformed_data['verification'] = verification

            # 変換後のデータを使用
            serializer = CustomAccountUpdateSerializer(data=transformed_data)
        else:
            # 既にバックエンド形式のデータの場合
            serializer = CustomAccountUpdateSerializer(data=request.data)

        serializer.is_valid(raise_exception=True)
        payload = serializer.validated_data

        # 部分更新時に既存データが消えないよう、既存アカウントを取得してマージに使用する
        update_params: Dict[str, Any] = {}
        account = stripe.Account.retrieve(account_id)
        account_business_type = getattr(account, 'business_type', None)

        # Business Profileのマージ
        business_profile = payload.get('business_profile') or {}
        if business_profile:
            cleaned_business_profile: Dict[str, Any] = {}
            for key, value in business_profile.items():
                # 文字列以外、空文字列、空白のみの文字列を除外
                if not isinstance(value, str):
                    continue
                value = value.strip()
                if value == '':
                    continue
                cleaned_business_profile[key] = value

            if cleaned_business_profile:
                existing_bp = _stripe_object_to_dict(getattr(account, 'business_profile', None))
                update_params['business_profile'] = _business_profile_for_update(
                    {**existing_bp, **cleaned_business_profile}
                )

        # Settingsのマージ
        settings_payload = payload.get('settings')
        if settings_payload:
            cleaned_settings: Dict[str, Any] = {}
            payments_payload = settings_payload.get('payments') or {}

            if payments_payload:
                cleaned_payments: Dict[str, Any] = {}
                for key, value in payments_payload.items():
                    # 文字列以外、空文字列、空白のみの文字列を除外
                    if not isinstance(value, str):
                        continue
                    value = value.strip()
                    if value == '':
                        continue
                    cleaned_payments[key] = value

                if cleaned_payments:
                    cleaned_settings['payments'] = cleaned_payments

            if cleaned_settings:
                existing_settings = _stripe_object_to_dict(getattr(account, 'settings', None))

                existing_payments = (existing_settings.get('payments') or {}) if isinstance(existing_settings.get('payments'), dict) else _stripe_object_to_dict(existing_settings.get('payments'))

                merged_payments = {**existing_payments, **cleaned_settings.get('payments', {})}
                update_params['settings'] = {'payments': _settings_payments_for_update(merged_payments)}

        # Companyのマージ（business_typeが'company'の場合のみcompanyパラメータを使用）
        if account_business_type == 'company':
            company = payload.get('company') or {}

            if company:
                cleaned_company: Dict[str, Any] = {}

                if company.get('name'):
                    name = company['name']
                    if isinstance(name, str) and name.strip():
                        cleaned_company['name'] = name.strip()

                if company.get('name_kanji'):
                    name_kanji = company['name_kanji']
                    if isinstance(name_kanji, str) and name_kanji.strip():
                        cleaned_company['name_kanji'] = name_kanji.strip()

                if company.get('name_kana'):
                    name_kana = company['name_kana']
                    if isinstance(name_kana, str) and name_kana.strip():
                        cleaned_company['name_kana'] = name_kana.strip()

                if company.get('phone'):
                    phone = company['phone']
                    if isinstance(phone, str) and phone.strip():
                        cleaned_company['phone'] = format_phone_number_for_stripe(phone.strip())

                if company.get('tax_id'):
                    tax_id = company['tax_id']
                    if isinstance(tax_id, str) and tax_id.strip():
                        cleaned_company['tax_id'] = tax_id.strip()

                # address_kanji の処理
                address_kanji = company.get('address_kanji')
                if isinstance(address_kanji, dict):
                    cleaned_address_kanji: Dict[str, Any] = {}
                    for key, value in address_kanji.items():
                        # 文字列以外、空文字列、空白のみの文字列を除外
                        if not isinstance(value, str):
                            continue
                        value = value.strip()
                        if value == '':
                            continue
                        cleaned_address_kanji[key] = value

                    if not cleaned_address_kanji.get('country'):
                        cleaned_address_kanji['country'] = 'JP'

                    other_fields = {key: value for key, value in cleaned_address_kanji.items() if key != 'country'}

                    if other_fields:
                        cleaned_company['address_kanji'] = cleaned_address_kanji

                # address_kana の処理
                address_kana = company.get('address_kana')
                if isinstance(address_kana, dict):
                    cleaned_address_kana: Dict[str, Any] = {}
                    for key, value in address_kana.items():
                        # 文字列以外、空文字列、空白のみの文字列を除外
                        if not isinstance(value, str):
                            continue
                        value = value.strip()
                        if value == '':
                            continue
                        cleaned_address_kana[key] = value

                    if not cleaned_address_kana.get('country'):
                        cleaned_address_kana['country'] = 'JP'

                    other_fields = {key: value for key, value in cleaned_address_kana.items() if key != 'country'}

                    if other_fields:
                        cleaned_company['address_kana'] = cleaned_address_kana

                if cleaned_company:
                    existing_company = _stripe_object_to_dict(getattr(account, 'company', None))
                    update_params['company'] = _company_for_update({**existing_company, **cleaned_company})

        # External Account (Bank Account)のマージ
        external_account = payload.get('external_account') or {}
        if external_account:
            bank_code_raw = external_account.get('bank_code')
            bank_code = bank_code_raw.strip() if isinstance(bank_code_raw, str) else None

            branch_code_raw = external_account.get('branch_code')
            branch_code = branch_code_raw.strip() if isinstance(branch_code_raw, str) else None

            account_number_raw = external_account.get('account_number')
            account_number = account_number_raw.strip() if isinstance(account_number_raw, str) else None

            account_holder_name_raw = external_account.get('account_holder_name')
            account_holder_name = account_holder_name_raw.strip() if isinstance(account_holder_name_raw, str) else None

            account_type_raw = external_account.get('account_type')
            account_type = account_type_raw.strip() if isinstance(account_type_raw, str) else None

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

        # アカウントのbusiness_typeと検証状態を取得
        account = stripe.Account.retrieve(account_id)
        account_business_type = getattr(account, 'business_type', None)

        # 本人確認書類の再送信が必要かどうかを確認
        # requirements.currently_dueまたはpast_dueにverification.document関連の要件が含まれている場合、再送信可能
        requirements = getattr(account, 'requirements', None)
        currently_due = requirements.get('currently_due', []) if requirements else []
        past_due = requirements.get('past_due', []) if requirements else []
        all_due = currently_due + past_due

        # verification.document関連の要件が含まれているか確認
        # Stripeの要件名は通常以下のような形式:
        # - verification.document.front
        # - verification.document.back
        # - individual.verification.document.front
        # - representative.verification.document.front
        verification_document_required = any(
            'verification.document' in str(req)
            for req in all_due
        )

        # アカウントが検証済みで、かつ本人確認書類の再送信が不要な場合のみ、verificationパラメータを送信しない
        is_account_verified = getattr(account, 'charges_enabled', False) or getattr(account, 'payouts_enabled', False)
        can_send_verification = verification_document_required or not is_account_verified

        # Individual（business_typeが'individual'の場合のみindividualパラメータを使用）
        if account_business_type == 'individual':
            individual = payload.get('individual') or {}
            if individual:
                cleaned_individual: Dict[str, Any] = {}

                if individual.get('last_name_kanji'):
                    last_name_kanji = individual['last_name_kanji']
                    if isinstance(last_name_kanji, str) and last_name_kanji.strip():
                        cleaned_individual['last_name_kanji'] = last_name_kanji.strip()

                if individual.get('first_name_kanji'):
                    first_name_kanji = individual['first_name_kanji']
                    if isinstance(first_name_kanji, str) and first_name_kanji.strip():
                        cleaned_individual['first_name_kanji'] = first_name_kanji.strip()

                if individual.get('last_name_kana'):
                    last_name_kana = individual['last_name_kana']
                    if isinstance(last_name_kana, str) and last_name_kana.strip():
                        cleaned_individual['last_name_kana'] = last_name_kana.strip()

                if individual.get('first_name_kana'):
                    first_name_kana = individual['first_name_kana']
                    if isinstance(first_name_kana, str) and first_name_kana.strip():
                        cleaned_individual['first_name_kana'] = first_name_kana.strip()

                if individual.get('email'):
                    email = individual['email']
                    if isinstance(email, str) and email.strip():
                        cleaned_individual['email'] = email.strip()

                if individual.get('phone'):
                    phone = individual['phone']
                    if isinstance(phone, str) and phone.strip():
                        cleaned_individual['phone'] = format_phone_number_for_stripe(phone.strip())

                dob = individual.get('dob')
                if isinstance(dob, dict) and dob:
                    cleaned_individual['dob'] = dob

                # address_kanji の処理
                address_kanji = individual.get('address_kanji')
                if isinstance(address_kanji, dict):
                    cleaned_address_kanji: Dict[str, Any] = {}
                    for key, value in address_kanji.items():
                        # 文字列以外、空文字列、空白のみの文字列を除外
                        if not isinstance(value, str):
                            continue
                        value = value.strip()
                        if value == '':
                            continue
                        cleaned_address_kanji[key] = value

                    if not cleaned_address_kanji.get('country'):
                        cleaned_address_kanji['country'] = 'JP'

                    other_fields = {key: value for key, value in cleaned_address_kanji.items() if key != 'country'}

                    if other_fields:
                        cleaned_individual['address_kanji'] = cleaned_address_kanji

                # address_kana の処理
                address_kana = individual.get('address_kana')
                if isinstance(address_kana, dict):
                    cleaned_address_kana: Dict[str, Any] = {}
                    for key, value in address_kana.items():
                        # 文字列以外、空文字列、空白のみの文字列を除外
                        if not isinstance(value, str):
                            continue
                        value = value.strip()
                        if value == '':
                            continue
                        cleaned_address_kana[key] = value

                    if not cleaned_address_kana.get('country'):
                        cleaned_address_kana['country'] = 'JP'

                    other_fields = {key: value for key, value in cleaned_address_kana.items() if key != 'country'}

                    if other_fields:
                        cleaned_individual['address_kana'] = cleaned_address_kana

                # 本人確認書類の処理（再送信が必要な場合のみ送信）
                if can_send_verification:
                    verification = payload.get('verification') or {}
                    verification_params: Dict[str, Any] = {}
                    document: Dict[str, str] = {}

                    document_front = verification.get('document_front')
                    if isinstance(document_front, str) and document_front.strip():
                        document['front'] = document_front.strip()

                    document_back = verification.get('document_back')
                    if isinstance(document_back, str) and document_back.strip():
                        document['back'] = document_back.strip()

                    if document:
                        verification_params['document'] = document
                    if verification_params:
                        cleaned_individual['verification'] = verification_params

                if cleaned_individual:
                    existing_individual = _stripe_object_to_dict(getattr(account, 'individual', None))
                    update_params['individual'] = _individual_for_update({**existing_individual, **cleaned_individual})

        elif account_business_type == 'company':
            # business_typeが'company'の場合、individualパラメータは使用できない
            # Stripe APIでは、companyアカウントの代表者情報はPersons APIを使用して設定する必要がある
            rep_info = request_data.get('rep_info') or {}
            if rep_info:

                # 既存の代表者Personを検索
                persons = stripe.Account.list_persons(account_id, limit=100)
                representative_person = None
                for person in persons.data:
                    if person.relationship and person.relationship.get('representative'):
                        representative_person = person
                        break

                rep_title_raw = rep_info.get('rep_title')
                rep_title = rep_title_raw.strip() if isinstance(rep_title_raw, str) and rep_title_raw.strip() else '代表取締役'
                person_params: Dict[str, Any] = {
                    'relationship': {
                        'representative': True,
                        'director': True,
                        'title': rep_title,
                    },
                }

                last_name_kanji = rep_info.get('last_name_kanji')
                if isinstance(last_name_kanji, str) and last_name_kanji.strip():
                    person_params['last_name_kanji'] = last_name_kanji.strip()

                first_name_kanji = rep_info.get('first_name_kanji')
                if isinstance(first_name_kanji, str) and first_name_kanji.strip():
                    person_params['first_name_kanji'] = first_name_kanji.strip()

                last_name_kana = rep_info.get('last_name_kana')
                if isinstance(last_name_kana, str) and last_name_kana.strip():
                    person_params['last_name_kana'] = last_name_kana.strip()

                first_name_kana = rep_info.get('first_name_kana')
                if isinstance(first_name_kana, str) and first_name_kana.strip():
                    person_params['first_name_kana'] = first_name_kana.strip()

                rep_email = rep_info.get('rep_email')
                if isinstance(rep_email, str) and rep_email.strip():
                    person_params['email'] = rep_email.strip()

                rep_phone = rep_info.get('rep_phone')
                if isinstance(rep_phone, str) and rep_phone.strip():
                    person_params['phone'] = format_phone_number_for_stripe(rep_phone.strip())

                rep_dob = rep_info.get('rep_dob')
                if isinstance(rep_dob, dict) and rep_dob:
                    person_params['dob'] = rep_dob

                address_kanji = rep_info.get('address_kanji')
                if isinstance(address_kanji, dict):
                    cleaned_address_kanji: Dict[str, Any] = {}
                    for key, value in address_kanji.items():
                        # 文字列以外、空文字列、空白のみの文字列を除外
                        if not isinstance(value, str):
                            continue
                        value = value.strip()
                        if value == '':
                            continue
                        cleaned_address_kanji[key] = value

                    if not cleaned_address_kanji.get('country'):
                        cleaned_address_kanji['country'] = 'JP'

                    other_fields = {key: value for key, value in cleaned_address_kanji.items() if key != 'country'}

                    if other_fields:
                        person_params['address_kanji'] = cleaned_address_kanji

                address_kana = rep_info.get('address_kana')
                if isinstance(address_kana, dict):
                    cleaned_address_kana: Dict[str, Any] = {}
                    for key, value in address_kana.items():
                        # 文字列以外、空文字列、空白のみの文字列を除外
                        if not isinstance(value, str):
                            continue
                        value = value.strip()
                        if value == '':
                            continue
                        cleaned_address_kana[key] = value

                    if not cleaned_address_kana.get('country'):
                        cleaned_address_kana['country'] = 'JP'

                    other_fields = {key: value for key, value in cleaned_address_kana.items() if key != 'country'}

                    if other_fields:
                        person_params['address_kana'] = cleaned_address_kana

                # 本人確認書類の処理（再送信が必要な場合のみ送信）
                if can_send_verification:
                    verif_docs = request_data.get('verif_docs') or {}
                    document: Dict[str, str] = {}

                    document_front = verif_docs.get('document_front')
                    if isinstance(document_front, str) and document_front.strip():
                        document['front'] = document_front.strip()

                    document_back = verif_docs.get('document_back')
                    if isinstance(document_back, str) and document_back.strip():
                        document['back'] = document_back.strip()

                    if document:
                        person_params['verification'] = {'document': document}

                # 既存の代表者がいる場合は更新、いない場合は作成
                if representative_person:
                    stripe.Account.modify_person(account_id, representative_person.id, **person_params)
                else:
                    stripe.Account.create_person(account_id, **person_params)

                # 他の取締役を処理
                directors_info = rep_info.get('directors', [])
                if directors_info:
                    # 既存の取締役（代表者以外）を取得
                    existing_director_persons = []

                    for person in persons.data:
                        if person.relationship and person.relationship.get('director') and not person.relationship.get('representative'):
                            existing_director_persons.append(person)

                    # 新しい取締役を追加または既存の取締役を更新
                    for director_info in directors_info:
                        # 取締役の照合: 名前またはメールアドレスのいずれかが一致すれば同一人物とみなす
                        # （名前やメールの修正時に重複が発生するのを防ぐ）
                        existing_director = None
                        dir_name_key = f"{director_info.get('first_name_kanji', '')}_{director_info.get('last_name_kanji', '')}"
                        dir_email = director_info.get('email', '')

                        for person in existing_director_persons:
                            person_name_key = f"{person.first_name_kanji}_{person.last_name_kanji}"
                            person_email = getattr(person, 'email', '') or ''
                            if (dir_name_key and dir_name_key == person_name_key) or (dir_email and dir_email == person_email):
                                existing_director = person
                                break

                        director_title_raw = director_info.get('title')
                        director_title = director_title_raw.strip() if isinstance(director_title_raw, str) and director_title_raw.strip() else '取締役'
                        director_params: Dict[str, Any] = {
                            'relationship': {'director': True, 'title': director_title},
                        }

                        last_name_kanji = director_info.get('last_name_kanji')
                        if isinstance(last_name_kanji, str) and last_name_kanji.strip():
                            director_params['last_name_kanji'] = last_name_kanji.strip()

                        first_name_kanji = director_info.get('first_name_kanji')
                        if isinstance(first_name_kanji, str) and first_name_kanji.strip():
                            director_params['first_name_kanji'] = first_name_kanji.strip()

                        last_name_kana = director_info.get('last_name_kana')
                        if isinstance(last_name_kana, str) and last_name_kana.strip():
                            director_params['last_name_kana'] = last_name_kana.strip()

                        first_name_kana = director_info.get('first_name_kana')
                        if isinstance(first_name_kana, str) and first_name_kana.strip():
                            director_params['first_name_kana'] = first_name_kana.strip()

                        email = director_info.get('email')
                        if isinstance(email, str) and email.strip():
                            director_params['email'] = email.strip()

                        phone = director_info.get('phone')
                        if isinstance(phone, str) and phone.strip():
                            director_params['phone'] = format_phone_number_for_stripe(phone.strip())

                        dob = director_info.get('dob')
                        if isinstance(dob, dict) and dob:
                            director_params['dob'] = dob

                        # address_kanji の処理
                        address_kanji = director_info.get('address_kanji')
                        if isinstance(address_kanji, dict):
                            cleaned_address_kanji: Dict[str, Any] = {}
                            for key, value in address_kanji.items():
                                # 文字列以外、空文字列、空白のみの文字列を除外
                                if not isinstance(value, str):
                                    continue
                                value = value.strip()
                                if value == '':
                                    continue
                                cleaned_address_kanji[key] = value

                            if not cleaned_address_kanji.get('country'):
                                cleaned_address_kanji['country'] = 'JP'

                            other_fields = {key: value for key, value in cleaned_address_kanji.items() if key != 'country'}

                            if other_fields:
                                director_params['address_kanji'] = cleaned_address_kanji

                        # address_kana の処理
                        address_kana = director_info.get('address_kana')
                        if isinstance(address_kana, dict):
                            cleaned_address_kana: Dict[str, Any] = {}
                            for key, value in address_kana.items():
                                # 文字列以外、空文字列、空白のみの文字列を除外
                                if not isinstance(value, str):
                                    continue
                                value = value.strip()
                                if value == '':
                                    continue
                                cleaned_address_kana[key] = value

                            if not cleaned_address_kana.get('country'):
                                cleaned_address_kana['country'] = 'JP'

                            other_fields = {key: value for key, value in cleaned_address_kana.items() if key != 'country'}

                            if other_fields:
                                director_params['address_kana'] = cleaned_address_kana

                        if existing_director:
                            # 既存の取締役を更新
                            stripe.Account.modify_person(account_id, existing_director.id, **director_params)
                        else:
                            # 新しい取締役を追加
                            stripe.Account.create_person(account_id, **director_params)

        # 必要に応じて、利用規約の同意を記録
        tos_acceptance = getattr(account, 'tos_acceptance', None)
        needs_tos = tos_acceptance is None or tos_acceptance.get('date') is None
        product_company = request_data.get('product_company') or {}
        accept_tos = bool(product_company.get('accept_tos'))

        # 法人番号が変更される場合は再同意が必要
        tax_id_changed = False
        if product_company.get('tax_id'):
            existing_tax_id = None
            if getattr(account, 'company', None):
                existing_tax_id = getattr(account.company, 'tax_id', None)
            tax_id_changed = product_company.get('tax_id') != existing_tax_id

        if (needs_tos or tax_id_changed) and not accept_tos:
            return Response(
                {
                    'error': '利用規約に同意してください。',
                    'needs_tos': True,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if accept_tos:
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

    except Exception as e:
        logger.error(
            f"Stripeアカウント更新予期しないエラー: account_id={mask_sensitive_id(account_id)}, "
            f"user_id={mask_sensitive_id(request.user.id)}, error={str(e)}",
            exc_info=True
        )
        return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def custom_upload_document(request: Request) -> Response:
    """本人確認書類をStripeにアップロード"""
    try:
        if 'file' not in request.FILES:
            return Response({'error': 'ファイルが選択されていません。'}, status=status.HTTP_400_BAD_REQUEST)

        file = request.FILES['file']

        # ファイルタイプの検証
        is_valid, error_message = validate_file_type(file)
        if not is_valid:
            return Response({'error': error_message}, status=status.HTTP_400_BAD_REQUEST)

        # ファイルサイズチェック（10MB）
        if file.size > settings.LUGGO_MAX_FILE_SIZE:
            return Response({'error': f'ファイルサイズは{settings.LUGGO_MAX_FILE_SIZE // (1024 * 1024)}MB以下にしてください。'}, status=status.HTTP_400_BAD_REQUEST)

        file.seek(0)
        file_content = file.read()

        # Stripe File APIにアップロード
        # purpose='identity_document' は本人確認書類用
        stripe_file = stripe.File.create(
            purpose='identity_document',
            file=io.BytesIO(file_content),
        )

        logger.info(
            f"本人確認書類アップロード成功: file_id={mask_sensitive_id(stripe_file.id)}, "
            f"user_id={mask_sensitive_id(request.user.id)}"
        )

        return Response({
            'file_id': stripe_file.id,
        }, status=status.HTTP_200_OK)

    except stripe.error.StripeError as e:  # type: ignore[attr-defined]
        logger.error(
            f"本人確認書類アップロードエラー: user_id={mask_sensitive_id(request.user.id)}, "
            f"error={str(e)}"
        )
        return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    except Exception as e:
        logger.error(
            f"本人確認書類アップロード予期しないエラー: user_id={mask_sensitive_id(request.user.id)}, "
            f"error={str(e)}",
            exc_info=True
        )
        return Response({'error': 'ファイルのアップロードに失敗しました。'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


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
        currently_due = req.get('currently_due', []) if req else []
        eventually_due = req.get('eventually_due', []) if req else []
        past_due = req.get('past_due', []) if req else []
        return Response({
            'currently_due': currently_due,
            'eventually_due': eventually_due,
            'past_due': past_due,
        })

    except stripe.error.StripeError as e:  # type: ignore[attr-defined]
        logger.error(
            f"Stripeアカウント要件取得エラー: account_id={mask_sensitive_id(account_id)}, "
            f"user_id={mask_sensitive_id(request.user.id)}, error={str(e)}"
        )
        return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    except Exception as e:
        logger.error(
            f"Stripeアカウント要件取得予期しないエラー: account_id={mask_sensitive_id(account_id)}, "
            f"user_id={mask_sensitive_id(request.user.id)}, error={str(e)}",
            exc_info=True
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
            'business_type': business_profile.business_type,
            'company_name': business_profile.company_name,
            'company_email': business_profile.company_email,
            'subdomain': business_profile.subdomain,
            'tax_id': business_profile.tax_id,
            'rep_last_name_kanji': business_profile.rep_last_name_kanji,
            'rep_first_name_kanji': business_profile.rep_first_name_kanji,
            'rep_last_name_kana': business_profile.rep_last_name_kana,
            'rep_first_name_kana': business_profile.rep_first_name_kana,
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
        user = User.objects.create_user(
            email=data['email'],
            password=data['password'],
            user_type='business_owner',
            phone_number=data['phone'],
            last_name=data['rep_last_name'],
            first_name=data['rep_first_name'],
        )

        # BusinessProfile作成
        BusinessProfile.objects.create(
            user=user,
            business_type=data['business_type'],
            company_name=data['company_name'],
            company_email=data['email'],
            subdomain=data['subdomain'],
            rep_last_name_kanji=data['rep_last_name'],
            rep_first_name_kanji=data['rep_first_name'],
            rep_last_name_kana=data['rep_last_name_kana'],
            rep_first_name_kana=data['rep_first_name_kana'],
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