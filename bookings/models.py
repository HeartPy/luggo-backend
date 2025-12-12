from django.db import models
from django.conf import settings
import uuid
from datetime import date
import time
from typing import Any
import secrets


class LuggageBooking(models.Model):
    """荷物配送予約モデル"""

    # 配達状況
    DELIVERY_STATUS_CHOICES = [
        ('before_pickup', '集荷前'),
        ('picked_up', '集荷済'),
        ('delivered', '配達済'),
        ('cancelled', 'キャンセル'),
    ]

    # 基本情報
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    booking_number = models.CharField(max_length=20, unique=True, blank=True)
    business_owner = models.ForeignKey(
        'business_owners.BusinessProfile',
        on_delete=models.CASCADE,
        related_name='bookings',
        verbose_name='ビジネスオーナー',
        null=True,
        blank=True,
        help_text='この予約を担当するビジネスオーナー'
    )
    delivery_status = models.CharField(
        max_length=20,
        choices=DELIVERY_STATUS_CHOICES,
        default='before_pickup',
        verbose_name='配達状況'
    )

    # 集荷情報
    pickup_location_name = models.CharField(
        max_length=200,
        verbose_name='集荷場所の名称'
    )
    pickup_location_address = models.TextField(
        verbose_name='集荷場所の住所'
    )
    pickup_date = models.DateField(
        verbose_name='集荷日'
    )

    # 配送情報
    delivery_location_name = models.CharField(
        max_length=200,
        verbose_name='配送場所の名称'
    )
    delivery_location_address = models.TextField(
        verbose_name='配送場所の住所'
    )
    delivery_date = models.DateField(
        verbose_name='配送日'
    )

    # 追加情報
    notes = models.TextField(
        blank=True,
        verbose_name='備考'
    )

    # 荷物情報
    luggage_items = models.JSONField(
        default=dict,
        verbose_name='荷物情報',
    )
    total_amount = models.PositiveIntegerField(
        default=0,
        verbose_name='合計金額',
    )

    # 顧客情報
    customer_name = models.CharField(
        max_length=200,
        verbose_name='顧客名'
    )
    customer_email = models.EmailField(
        verbose_name='メールアドレス'
    )
    customer_phone_number = models.CharField(
        max_length=15,
        verbose_name='電話番号'
    )
    customer_nationality = models.CharField(
        max_length=3,
        verbose_name='国籍'
    )
    guest_name = models.CharField(
        max_length=200,
        verbose_name='宿泊予約者名'
    )

    # 支払い情報
    payment_intent_id = models.CharField(
        max_length=255,
        verbose_name='Stripe Payment Intent ID'
    )

    # 作成日・更新日
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='作成日')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新日')

    class Meta:
        db_table = 'luggage_bookings'
        verbose_name = '予約情報'
        verbose_name_plural = '予約情報'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['delivery_status']),
            models.Index(fields=['pickup_date']),
            models.Index(fields=['delivery_date']),
            models.Index(fields=['customer_name']),
        ]

    def __str__(self) -> str:
        return f'{self.booking_number} - {self.pickup_location_name} → {self.delivery_location_name}'

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self.booking_number:
            self.booking_number = self.generate_booking_number()
        super().save(*args, **kwargs)

    def generate_booking_number(self) -> str:
        """予約番号を自動生成（英数字ランダム形式、暗号学的に安全）"""

        # 使用する文字: 数字と大文字（0, O, I, 1 を除外して読みやすく）
        chars = '23456789ABCDEFGHJKLMNPQRSTUVWXYZ'

        # 4桁のブロックを3つ生成（暗号学的に安全な乱数生成器を使用）
        block1 = ''.join(secrets.choice(chars) for _ in range(4))
        block2 = ''.join(secrets.choice(chars) for _ in range(4))
        block3 = ''.join(secrets.choice(chars) for _ in range(4))

        booking_number = f'LG-{block1}-{block2}-{block3}'

        # 一意性チェック（既存の予約番号と重複しないか確認）
        max_retries = 10
        retries = 0
        while self.__class__.objects.filter(booking_number=booking_number).exists() and retries < max_retries:
            block1 = ''.join(secrets.choice(chars) for _ in range(4))
            block2 = ''.join(secrets.choice(chars) for _ in range(4))
            block3 = ''.join(secrets.choice(chars) for _ in range(4))
            booking_number = f'LG-{block1}-{block2}-{block3}'
            retries += 1

        if retries >= max_retries:
            # リトライ上限に達した場合はタイムスタンプを追加
            timestamp = str(int(time.time()))[-6:]
            booking_number = f'LG-{block1}-{block2}-{timestamp}'

        return booking_number

    def can_cancel(self) -> bool:
        """キャンセル可能かチェック"""
        return self.delivery_status == 'before_pickup'

    def days_until_pickup(self) -> int:
        """集荷日までの日数"""
        today = date.today()
        if self.pickup_date >= today:
            return (self.pickup_date - today).days
        return 0