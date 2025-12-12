from decimal import Decimal
from django.db import models
from django.conf import settings
import uuid


class DriverProfile(models.Model):
    """配達者プロフィール"""
    # 基本情報
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='driver_profile')
    business_owner = models.ForeignKey('business_owners.BusinessProfile', on_delete=models.CASCADE, related_name='drivers')
    company_name = models.CharField(
        max_length=200,
        blank=True,
        verbose_name='会社名',
    )

    # ステータス情報
    license_expiry = models.DateField(verbose_name='免許証有効期限')
    is_available = models.BooleanField(default=True, verbose_name='有効/無効')
    total_deliveries = models.PositiveIntegerField(default=0, verbose_name='総配達数')
    total_earnings = models.DecimalField(max_digits=10, decimal_places=2, default=0, verbose_name='総売上')

    # 作成日・更新日
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='作成日')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新日')

    class Meta:
        db_table = 'driver_profiles'
        verbose_name = '配達者プロフィール'
        verbose_name_plural = '配達者プロフィール'

    def __str__(self) -> str:
        driver_name = self.user.get_full_name()
        if self.company_name:
            company_info = self.company_name
        else:
            company_info = self.business_owner.company_name
        return f"{driver_name} ({company_info})"

    def complete_delivery(self, earnings: Decimal) -> None:
        self.total_deliveries += 1
        self.total_earnings += earnings
        self.save()
