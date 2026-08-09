import re
from typing import Any, Dict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from rest_framework import serializers
from drivers.models import DriverProfile
from .models import LuggageBooking

_JST = ZoneInfo("Asia/Tokyo")
_MAX_BOOKING_DAYS = 180

# 電話番号の許可形式
# - 国際形式: 先頭が + の国番号付き（+ と 7〜15 桁）
# - 国内形式: 10〜11 桁の数字のみ（日本語ページのみ許可）
_PHONE_INTERNATIONAL_RE = re.compile(r'^\+\d{7,15}$')
_PHONE_ANY_RE = re.compile(r'^(?:\+\d{7,15}|\d{10,11})$')


def normalize_phone_number(value: Any) -> Any:
    """電話番号からスペース・ハイフンを除去"""
    if isinstance(value, str):
        return re.sub(r'[\s-]', '', value)
    return value


def validate_customer_phone_by_language(
    data: Dict[str, Any],
    instance: LuggageBooking | None = None,
) -> None:
    """
    言語に応じた電話番号形式を検証

    日本語以外は国番号付き国際形式のみ。日本語は国内形式も許可。
    """
    phone = data.get('customer_phone_number')
    if not phone:
        return
    language = data.get(
        'customer_language',
        getattr(instance, 'customer_language', None) if instance else None,
    ) or 'ja'
    if language != 'ja':
        if not _PHONE_INTERNATIONAL_RE.match(phone):
            raise serializers.ValidationError({
                'customer_phone_number': (
                    '電話番号は国番号と + から始まる国際形式で入力してください。'
                )
            })
    elif not _PHONE_ANY_RE.match(phone):
        raise serializers.ValidationError({
            'customer_phone_number': '有効な電話番号を入力してください。'
        })


def validate_location_coordinates(
    data: Dict[str, Any],
    instance: LuggageBooking | None = None,
) -> None:
    """緯度・経度の片方だけの指定を拒否し、ジオコード状態を整合させる"""
    for prefix in ('pickup', 'delivery'):
        lat_key = f'{prefix}_latitude'
        lng_key = f'{prefix}_longitude'
        latitude = data.get(
            lat_key, getattr(instance, lat_key, None) if instance else None
        )
        longitude = data.get(
            lng_key, getattr(instance, lng_key, None) if instance else None
        )
        if (latitude is None) != (longitude is None):
            raise serializers.ValidationError({
                lat_key: '緯度と経度は両方指定してください。',
                lng_key: '緯度と経度は両方指定してください。',
            })
        if lat_key in data or lng_key in data:
            data[f'{prefix}_geocode_status'] = (
                'verified' if latitude is not None else 'pending'
            )


def _min_pickup_date() -> date:
    """前日23時締切: 23時JST未満→翌日、23時以降→翌々日"""
    now = datetime.now(_JST)
    days_ahead = 1 if now.hour < 23 else 2
    return (now + timedelta(days=days_ahead)).date()


def _max_booking_date() -> date:
    now = datetime.now(_JST)
    return (now + timedelta(days=_MAX_BOOKING_DAYS)).date()


class LuggageBookingSerializer(serializers.ModelSerializer[LuggageBooking]):
    """荷物配送予約シリアライザー"""

    days_until_pickup = serializers.SerializerMethodField()

    class Meta:
        model = LuggageBooking
        fields = [
            'id',
            'booking_number',
            'business_owner',
            'customer_name',
            'customer_email',
            'customer_phone_number',
            'customer_nationality',
            'guest_name',
            'delivery_status',
            'pickup_location_name',
            'pickup_location_address',
            'pickup_place_id',
            'pickup_latitude',
            'pickup_longitude',
            'pickup_postal_code',
            'pickup_geocode_status',
            'pickup_date',
            'delivery_location_name',
            'delivery_location_address',
            'delivery_place_id',
            'delivery_latitude',
            'delivery_longitude',
            'delivery_postal_code',
            'delivery_geocode_status',
            'delivery_date',
            'notes',
            'luggage_items',
            'total_amount',
            'days_until_pickup',
            'created_at',
            'updated_at',
        ]
        read_only_fields = [
            'id', 'booking_number', 'business_owner',
            'pickup_geocode_status', 'delivery_geocode_status',
            'created_at', 'updated_at',
        ]

    def get_days_until_pickup(self, obj: LuggageBooking) -> int:
        """集荷日までの日数を返す"""
        return obj.days_until_pickup()

    def validate_pickup_date(self, value: date) -> date:
        if value < _min_pickup_date():
            raise serializers.ValidationError('前日の23時を過ぎているため、この日付は指定できません。')
        if value > _max_booking_date():
            raise serializers.ValidationError('予約できるのは半年先までです。')
        return value

    def validate_delivery_date(self, value: date) -> date:
        if value < _min_pickup_date():
            raise serializers.ValidationError('前日の23時を過ぎているため、この日付は指定できません。')
        if value > _max_booking_date():
            raise serializers.ValidationError('予約できるのは半年先までです。')
        return value

    def validate_customer_phone_number(self, value: str) -> str:
        return normalize_phone_number(value)

    def validate(self, data: Dict[str, Any]) -> Dict[str, Any]:
        pickup_date = data.get('pickup_date')
        delivery_date = data.get('delivery_date')

        if pickup_date and delivery_date:
            if delivery_date < pickup_date:
                raise serializers.ValidationError({
                    'delivery_date': '配送日は集荷日以降の日付を指定してください。'
                })

        validate_location_coordinates(data, self.instance)
        validate_customer_phone_by_language(data, self.instance)

        return data


class OwnerBookingUpdateSerializer(serializers.ModelSerializer[LuggageBooking]):
    """
    事業者が予約一覧の詳細から編集できる項目を更新するためのシリアライザー

    顧客向けの予約期間（前日23時締切・半年先まで）バリデーションは適用しない。
    """

    # キャンセルは専用ボタンで行うため編集対象外
    EDITABLE_STATUSES = ('before_pickup', 'picked_up', 'delivered')

    driver = serializers.PrimaryKeyRelatedField(
        queryset=DriverProfile.objects.none(),
        required=False,
        allow_null=True,
    )
    pickup_driver = serializers.PrimaryKeyRelatedField(
        queryset=DriverProfile.objects.none(),
        required=False,
        allow_null=True,
    )

    class Meta:
        model = LuggageBooking
        fields = [
            'delivery_status',
            'driver',
            'pickup_driver',
            'pickup_location_name',
            'pickup_location_address',
            'pickup_place_id',
            'pickup_latitude',
            'pickup_longitude',
            'pickup_postal_code',
            'pickup_date',
            'delivery_location_name',
            'delivery_location_address',
            'delivery_place_id',
            'delivery_latitude',
            'delivery_longitude',
            'delivery_postal_code',
            'delivery_date',
            'customer_name',
            'customer_email',
            'customer_phone_number',
            'customer_nationality',
            'guest_name',
            'notes',
        ]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # 配達者の選択肢をログイン中の事業者所属のものに限定する
        business_profile = self.context.get('business_profile')
        if business_profile is not None:
            self.fields['driver'].queryset = DriverProfile.objects.filter(
                business_owner=business_profile
            )
            self.fields['pickup_driver'].queryset = DriverProfile.objects.filter(
                business_owner=business_profile
            )

    def validate_delivery_status(self, value: str) -> str:
        if value not in self.EDITABLE_STATUSES:
            raise serializers.ValidationError('指定できない配達状況です。')
        return value

    def validate(self, data: Dict[str, Any]) -> Dict[str, Any]:
        assignment_changed = 'driver' in data or 'pickup_driver' in data
        # driver は配達担当兼デフォルト集荷担当。通常の単一担当割り当てでは
        # pickup_driver を省略すると分業設定を解除する。
        if 'driver' in data and 'pickup_driver' not in data:
            data['pickup_driver'] = None

        delivery_driver = data.get(
            'driver', self.instance.driver if self.instance is not None else None
        )
        pickup_driver = data.get(
            'pickup_driver',
            self.instance.pickup_driver if self.instance is not None else None,
        )
        if assignment_changed and pickup_driver is not None and delivery_driver is None:
            raise serializers.ValidationError({
                'driver': '集荷担当を分ける場合は配達担当も選択してください。'
            })
        # 同じ人を両方へ指定した場合は通常の単一担当へ正規化する。
        if (
            pickup_driver is not None
            and delivery_driver is not None
            and pickup_driver.pk == delivery_driver.pk
        ):
            data['pickup_driver'] = None

        # 部分更新でも、最終的な集荷日・配送日の関係を検証する
        pickup_date = data.get('pickup_date')
        delivery_date = data.get('delivery_date')
        if pickup_date is None and self.instance is not None:
            pickup_date = self.instance.pickup_date
        if delivery_date is None and self.instance is not None:
            delivery_date = self.instance.delivery_date

        if pickup_date and delivery_date and delivery_date < pickup_date:
            raise serializers.ValidationError({
                'delivery_date': '配送日は集荷日以降の日付を指定してください。'
            })
        validate_location_coordinates(data, self.instance)
        return data

    # 非日本語予約の場所編集: 事業者の変更は _ja フィールドへ振り向ける。
    # base フィールドは旅行者の入力表記のまま保持する。
    _LOCATION_JA_FIELDS = {
        'pickup_location_name': 'pickup_location_name_ja',
        'pickup_location_address': 'pickup_location_address_ja',
        'delivery_location_name': 'delivery_location_name_ja',
        'delivery_location_address': 'delivery_location_address_ja',
    }

    def update(self, instance: LuggageBooking, validated_data: Dict[str, Any]) -> LuggageBooking:
        if instance.customer_language != 'ja':
            for base_field, ja_field in self._LOCATION_JA_FIELDS.items():
                if base_field in validated_data:
                    setattr(instance, ja_field, validated_data.pop(base_field))
        if 'driver' in validated_data:
            validated_data['delivery_manually_assigned'] = (
                validated_data['driver'] is not None
            )
        if 'pickup_driver' in validated_data:
            validated_data['pickup_manually_assigned'] = (
                validated_data['pickup_driver'] is not None
            )
        return super().update(instance, validated_data)


class LuggageBookingCreateSerializer(serializers.ModelSerializer[LuggageBooking]):
    """予約作成用シリアライザー"""
    payment_intent_id = serializers.CharField(required=True, allow_blank=False)
    customer_name = serializers.CharField(required=True, allow_blank=False, max_length=200)
    customer_email = serializers.EmailField(required=True, allow_blank=False)
    customer_phone_number = serializers.CharField(required=True, allow_blank=False, max_length=16)
    customer_nationality = serializers.CharField(required=True, allow_blank=False, max_length=3)
    guest_name = serializers.CharField(required=True, allow_blank=False, max_length=200)
    customer_language = serializers.ChoiceField(
        choices=LuggageBooking.CUSTOMER_LANGUAGE_CHOICES,
        required=False,
        default='ja',
    )
    # Google サジェストの日本語表記（事業者の予約一覧用）
    pickup_location_name_ja = serializers.CharField(
        required=False, allow_blank=True, default='', max_length=200
    )
    pickup_location_address_ja = serializers.CharField(
        required=False, allow_blank=True, default=''
    )
    delivery_location_name_ja = serializers.CharField(
        required=False, allow_blank=True, default='', max_length=200
    )
    delivery_location_address_ja = serializers.CharField(
        required=False, allow_blank=True, default=''
    )

    class Meta:
        model = LuggageBooking
        fields = [
            'payment_intent_id',
            'pickup_location_name',
            'pickup_location_address',
            'pickup_location_name_ja',
            'pickup_location_address_ja',
            'pickup_place_id',
            'pickup_latitude',
            'pickup_longitude',
            'pickup_postal_code',
            'pickup_date',
            'delivery_location_name',
            'delivery_location_address',
            'delivery_location_name_ja',
            'delivery_location_address_ja',
            'delivery_place_id',
            'delivery_latitude',
            'delivery_longitude',
            'delivery_postal_code',
            'delivery_date',
            'notes',
            'customer_email',
            'customer_name',
            'customer_phone_number',
            'customer_nationality',
            'guest_name',
            'customer_language',
        ]

    def validate_pickup_date(self, value: date) -> date:
        if value < _min_pickup_date():
            raise serializers.ValidationError('前日の23時を過ぎているため、この日付は指定できません。')
        if value > _max_booking_date():
            raise serializers.ValidationError('予約できるのは半年先までです。')
        return value

    def validate_delivery_date(self, value: date) -> date:
        if value < _min_pickup_date():
            raise serializers.ValidationError('前日の23時を過ぎているため、この日付は指定できません。')
        if value > _max_booking_date():
            raise serializers.ValidationError('予約できるのは半年先までです。')
        return value

    def validate_customer_phone_number(self, value: str) -> str:
        return normalize_phone_number(value)

    def validate(self, data: Dict[str, Any]) -> Dict[str, Any]:
        pickup_date = data.get('pickup_date')
        delivery_date = data.get('delivery_date')

        if pickup_date and delivery_date:
            if delivery_date < pickup_date:
                raise serializers.ValidationError({
                    'delivery_date': '配送日は集荷日以降の日付を指定してください。'
                })

        validate_location_coordinates(data)
        validate_customer_phone_by_language(data)

        return data
