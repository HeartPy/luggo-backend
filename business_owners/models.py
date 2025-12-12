from django.db import models
from django.conf import settings
from django.utils import timezone
from decimal import Decimal
from typing import Optional
import uuid


class BusinessProfileManager(models.Manager):
    """有効なビジネスオーナーのみを取得するマネージャー"""
    def get_queryset(self):
        return super().get_queryset().filter(is_active=True)


class BusinessProfile(models.Model):
    """事業者プロフィール"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='business_profile')
    company_name = models.CharField(max_length=200, verbose_name='会社名')
    company_email = models.EmailField(verbose_name='メールアドレス')
    tax_id = models.CharField(max_length=20, blank=True, verbose_name='法人番号')

    # サービス情報
    service_areas = models.JSONField(default=list, blank=True, verbose_name='事業所在地')
    max_luggage_capacity = models.PositiveIntegerField(default=20, verbose_name='最大荷物個数')

    # 営業情報
    operating_hours_start = models.TimeField(default='09:00', verbose_name='営業開始時間')
    operating_hours_end = models.TimeField(default='17:00', verbose_name='営業終了時間')
    operating_days = models.CharField(max_length=7, default='1111111', verbose_name='営業日')

    # 料金設定
    pricing_rules = models.JSONField(
        default=dict,
        blank=True,
        verbose_name='料金設定',
    )

    # 実績
    total_orders_completed = models.PositiveIntegerField(default=0, verbose_name='総注文数')
    total_revenue = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name='総売上')

    # アカウント状態
    is_approved = models.BooleanField(default=False, verbose_name='承認状態')
    approval_date = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True, verbose_name='有効/無効')
    deactivated_at = models.DateTimeField(null=True, blank=True, verbose_name='無効化日')

    #　作成日・更新日
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='作成日')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新日')

    # マネージャー
    objects = models.Manager()
    active = BusinessProfileManager()

    # Stripe Connect
    stripe_account_id = models.CharField(max_length=255, blank=True, default="", verbose_name='StripeアカウントID')

    class Meta:
        db_table = 'business_profiles'
        verbose_name = '事業者プロフィール'
        verbose_name_plural = '事業者プロフィール'

    def __str__(self) -> str:
        company_info = self.company_name if self.company_name else "個人事業主"
        return f"Business: {company_info}"

    def is_operating_now(self) -> bool:
        now = timezone.now()
        current_time = now.time()
        current_day = str(now.weekday())
        return (
            self.operating_days[int(current_day)] == '1' and
            self.operating_hours_start <= current_time <= self.operating_hours_end
        )

    def get_price(self, prefecture_code: Optional[str], luggage_type: str) -> Decimal:
        """都道府県と荷物の種類から料金を取得"""
        if not self.pricing_rules:
            return Decimal('0')

        # 都道府県ごとの料金設定を取得
        if prefecture_code and prefecture_code in self.pricing_rules:
            prefecture_prices = self.pricing_rules[prefecture_code]
            if luggage_type in prefecture_prices:
                return Decimal(str(prefecture_prices[luggage_type]))

        return Decimal('0')

    def deactivate(self) -> None:
        self.is_active = False
        self.deactivated_at = timezone.now()
        self.save()

    def activate(self) -> None:
        self.is_active = True
        self.deactivated_at = None
        self.save()
