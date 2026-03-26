from typing import Any, Dict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from rest_framework import serializers
from .models import LuggageBooking

_JST = ZoneInfo("Asia/Tokyo")
_MAX_BOOKING_DAYS = 180


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
            'pickup_date',
            'delivery_location_name',
            'delivery_location_address',
            'delivery_date',
            'notes',
            'luggage_items',
            'total_amount',
            'days_until_pickup',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'booking_number', 'business_owner', 'created_at', 'updated_at']

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

    def validate(self, data: Dict[str, Any]) -> Dict[str, Any]:
        pickup_date = data.get('pickup_date')
        delivery_date = data.get('delivery_date')

        if pickup_date and delivery_date:
            if delivery_date < pickup_date:
                raise serializers.ValidationError({
                    'delivery_date': '配送日は集荷日以降の日付を指定してください。'
                })

        return data


class LuggageBookingCreateSerializer(serializers.ModelSerializer[LuggageBooking]):
    """予約作成用シリアライザー"""
    payment_intent_id = serializers.CharField(required=True, allow_blank=False)
    customer_name = serializers.CharField(required=True, allow_blank=False, max_length=200)
    customer_email = serializers.EmailField(required=True, allow_blank=False)
    customer_phone_number = serializers.CharField(required=True, allow_blank=False, max_length=15)
    customer_nationality = serializers.CharField(required=True, allow_blank=False, max_length=3)
    guest_name = serializers.CharField(required=True, allow_blank=False, max_length=200)

    class Meta:
        model = LuggageBooking
        fields = [
            'payment_intent_id',
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

    def validate(self, data: Dict[str, Any]) -> Dict[str, Any]:
        pickup_date = data.get('pickup_date')
        delivery_date = data.get('delivery_date')

        if pickup_date and delivery_date:
            if delivery_date < pickup_date:
                raise serializers.ValidationError({
                    'delivery_date': '配送日は集荷日以降の日付を指定してください。'
                })

        return data