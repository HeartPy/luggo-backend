from typing import TypedDict, Literal
try:
    from typing import Required
except ImportError:
    # Python 3.9以下ではtyping_extensionsを使用
    from typing_extensions import Required
from rest_framework import serializers

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


class BusinessOwnerRegistrationData(TypedDict, total=False):
    email: Required[str]
    password: Required[str]
    business_type: BusinessType
    company_name: str


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


class BusinessOwnerRegistrationSerializer(serializers.Serializer[BusinessOwnerRegistrationData]):
    email = serializers.EmailField()
    password = serializers.CharField(min_length=8, write_only=True)
    business_type = serializers.ChoiceField(choices=['individual', 'company'], required=False, default='individual')
    company_name = serializers.CharField(required=False, allow_blank=True)

    def validate(self, data: BusinessOwnerRegistrationData) -> BusinessOwnerRegistrationData:
        business_type: BusinessType = data.get('business_type', 'individual')
        company_name = data.get('company_name', '').strip()

        if business_type == 'company' and not company_name:
            raise serializers.ValidationError("法人の場合、会社名は必須です。登記簿上の正式名称を入力してください")

        if business_type == 'individual' and not company_name:
            raise serializers.ValidationError("個人事業主の場合、屋号は必須です。屋号がない場合は、代表者名(姓＋名)を入力してください")

        return data


__all__ = [
    "BusinessProfileSerializer",
    "AddressSerializer",
    "CompanySerializer",
    "CustomAccountUpdateSerializer",
    "BusinessOwnerRegistrationSerializer",
]