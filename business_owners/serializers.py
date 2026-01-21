from typing import TypedDict, Literal
try:
    from typing import Required
except ImportError:
    # Python 3.9以下ではtyping_extensionsを使用
    from typing_extensions import Required
from django.contrib.auth import get_user_model
from rest_framework import serializers

from .models import BusinessProfile, validate_subdomain
from .utils import verify_registration_token
from users.validators import validate_password_strength

BusinessType = Literal['company', 'individual']


class BusinessProfileData(TypedDict, total=False):
    name: str
    support_email: str
    url: str
    product_description: str
    mcc: str


class AddressData(TypedDict, total=False):
    country: str
    postal_code: str
    state: str
    state_kana: str
    city: str
    city_kana: str
    line1: str
    line1_kana: str
    line2: str


class CompanyData(TypedDict, total=False):
    name: str
    address: AddressData


class DateOfBirthData(TypedDict, total=False):
    year: int
    month: int
    day: int


class AddressKanjiData(TypedDict, total=False):
    postal_code: str
    state: str
    city: str
    line1: str
    line2: str


class AddressKanaData(TypedDict, total=False):
    postal_code: str
    state: str
    city: str
    line1: str


class IndividualData(TypedDict, total=False):
    first_name_kanji: str
    last_name_kanji: str
    first_name_kana: str
    last_name_kana: str
    email: str
    phone: str
    dob: DateOfBirthData
    address_kanji: AddressKanjiData
    address_kana: AddressKanaData


class ExternalAccountData(TypedDict, total=False):
    bank_code: str
    branch_code: str
    account_type: str
    account_number: str
    account_holder_name: str


class VerificationData(TypedDict, total=False):
    document_front: str
    document_back: str


class CustomAccountUpdateData(TypedDict, total=False):
    business_profile: BusinessProfileData
    company: CompanyData
    individual: IndividualData
    external_account: ExternalAccountData
    verification: VerificationData


class RegistrationRequestData(TypedDict, total=False):
    email: Required[str]


class RegistrationTokenVerifyData(TypedDict, total=False):
    token: Required[str]


class BusinessAccountRegistrationData(TypedDict, total=False):
    token: Required[str]
    company_name: Required[str]
    rep_name: Required[str]
    email: Required[str]
    phone: Required[str]
    subdomain: Required[str]
    password: Required[str]


class BusinessProfileSerializer(serializers.Serializer[BusinessProfileData]):
    name = serializers.CharField(max_length=255, required=False, allow_null=True, allow_blank=True)
    support_email = serializers.EmailField(required=False, allow_null=True, allow_blank=True)
    url = serializers.URLField(required=False, allow_null=True, allow_blank=True)
    product_description = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    mcc = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=4)


class AddressSerializer(serializers.Serializer[AddressData]):
    country = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=2)
    postal_code = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=32)
    state = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=128)
    state_kana = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=128)
    city = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=128)
    city_kana = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=128)
    line1 = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=255)
    line1_kana = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=255)
    line2 = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=255)


class AddressKanjiSerializer(serializers.Serializer):
    country = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=2)
    postal_code = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=32)
    state = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=128)
    city = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=128)
    line1 = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=255)
    line2 = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=255)


class AddressKanaSerializer(serializers.Serializer):
    country = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=2)
    postal_code = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=32)
    state = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=128)
    city = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=128)
    line1 = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=255)


class CompanySerializer(serializers.Serializer[CompanyData]):
    name = serializers.CharField(max_length=255, required=False, allow_null=True, allow_blank=True)
    address = AddressSerializer(required=False)


class DateOfBirthSerializer(serializers.Serializer[DateOfBirthData]):
    year = serializers.IntegerField(required=False)
    month = serializers.IntegerField(required=False, min_value=1, max_value=12)
    day = serializers.IntegerField(required=False, min_value=1, max_value=31)


class IndividualSerializer(serializers.Serializer[IndividualData]):
    first_name_kanji = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=255)
    last_name_kanji = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=255)
    first_name_kana = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=255)
    last_name_kana = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=255)
    email = serializers.EmailField(required=False, allow_null=True, allow_blank=True)
    phone = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=32)
    dob = DateOfBirthSerializer(required=False)
    address_kanji = AddressKanjiSerializer(required=False)
    address_kana = AddressKanaSerializer(required=False)


class ExternalAccountSerializer(serializers.Serializer[ExternalAccountData]):
    bank_code = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=4)
    branch_code = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=3)
    account_type = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=10)
    account_number = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=7)
    account_holder_name = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=255)


class VerificationSerializer(serializers.Serializer[VerificationData]):
    document_front = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    document_back = serializers.CharField(required=False, allow_null=True, allow_blank=True)


class CustomAccountUpdateSerializer(serializers.Serializer[CustomAccountUpdateData]):
    business_profile = BusinessProfileSerializer(required=False)
    company = CompanySerializer(required=False)
    individual = IndividualSerializer(required=False)
    external_account = ExternalAccountSerializer(required=False)
    verification = VerificationSerializer(required=False)

    def validate(self, attrs: CustomAccountUpdateData) -> CustomAccountUpdateData:
        if not attrs:
            raise serializers.ValidationError("少なくとも1項目は更新してください。")
        return attrs


class RegistrationRequestSerializer(serializers.Serializer[RegistrationRequestData]):
    email = serializers.EmailField(required=True)

    def validate_email(self, value: str) -> str:
        """メールアドレスの重複チェック"""
        User = get_user_model()
        if User.objects.filter(email=value.lower()).exists():
            raise serializers.ValidationError("このメールアドレスは既に登録されています。")
        return value.lower()


class RegistrationTokenVerifySerializer(serializers.Serializer[RegistrationTokenVerifyData]):
    token = serializers.CharField(required=True)


class BusinessAccountRegistrationSerializer(serializers.Serializer[BusinessAccountRegistrationData]):
    token = serializers.CharField(required=True)
    company_name = serializers.CharField(max_length=100, required=True)
    rep_name = serializers.CharField(max_length=50, required=True)
    email = serializers.EmailField(required=True)
    phone = serializers.CharField(max_length=15, required=True)
    subdomain = serializers.CharField(min_length=3, max_length=12, required=True)
    password = serializers.CharField(
        min_length=8,
        max_length=16,
        write_only=True,
        required=True,
    )

    def validate_password(self, value: str) -> str:
        """パスワードのバリデーション：半角英数字+記号、8文字以上16文字以内、3種類以上"""
        validation_error = validate_password_strength(value)
        if validation_error:
            raise serializers.ValidationError(validation_error)

        return value

    def validate_token(self, value: str) -> str:
        """トークンの検証"""
        registration_token = verify_registration_token(value)
        if not registration_token:
            raise serializers.ValidationError("有効期限が切れています。お手数おかけしますが、もう一度いちからやり直してください。")
        return value

    def validate_subdomain(self, value: str) -> str:
        """予約フォームのURLのバリデーション"""
        # モデルのバリデーターを使用
        validate_subdomain(value)

        # 重複チェック
        if BusinessProfile.objects.filter(subdomain=value.lower()).exists():
            raise serializers.ValidationError("この予約フォームのURLは既に使用されています。別の文字列を選択してください。")

        return value.lower()

    def validate_email(self, value: str) -> str:
        """メールアドレスの重複チェックとトークンとの一致確認"""
        User = get_user_model()
        if User.objects.filter(email=value.lower()).exists():
            raise serializers.ValidationError("このメールアドレスは既に登録されています。")

        # トークンとメールアドレスの一致確認
        if hasattr(self, 'initial_data') and 'token' in self.initial_data:
            registration_token = verify_registration_token(self.initial_data['token'])
            if registration_token and registration_token.email.lower() != value.lower():
                raise serializers.ValidationError("送信されたメールアドレスと一致しません。正しいメールアドレスを入力してください。")

        return value.lower()


__all__ = [
    "BusinessProfileSerializer",
    "AddressSerializer",
    "CompanySerializer",
    "CustomAccountUpdateSerializer",
]