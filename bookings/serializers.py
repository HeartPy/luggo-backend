from typing import Any, Dict
from datetime import date
from rest_framework import serializers
from .models import LuggageBooking


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
        """集荷日のバリデーション"""
        if value < date.today():
            raise serializers.ValidationError('集荷日は今日以降の日付を指定してください。')
        return value

    def validate_delivery_date(self, value: date) -> date:
        """配送日のバリデーション"""
        if value < date.today():
            raise serializers.ValidationError('配送日は今日以降の日付を指定してください。')
        return value

    def validate(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """全体的なバリデーション"""
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
        """集荷日のバリデーション"""
        if value < date.today():
            raise serializers.ValidationError('集荷日は今日以降の日付を指定してください。')
        return value

    def validate_delivery_date(self, value: date) -> date:
        """配送日のバリデーション"""
        if value < date.today():
            raise serializers.ValidationError('配送日は今日以降の日付を指定してください。')
        return value

    def validate(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """全体的なバリデーション"""
        pickup_date = data.get('pickup_date')
        delivery_date = data.get('delivery_date')

        if pickup_date and delivery_date:
            if delivery_date < pickup_date:
                raise serializers.ValidationError({
                    'delivery_date': '配送日は集荷日以降の日付を指定してください。'
                })

        return data