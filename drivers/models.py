from typing import Optional

from django.db import models
from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.utils import timezone
import uuid


class DriverProfile(models.Model):
    """配達者プロフィール"""
    # 基本情報
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='driver_profile',
        verbose_name='ユーザー',
    )
    business_owner = models.ForeignKey(
        'business_owners.BusinessProfile',
        on_delete=models.CASCADE,
        related_name='drivers',
        verbose_name='事業者',
    )
    company_name = models.CharField(
        max_length=200,
        blank=True,
        verbose_name='会社名',
    )

    # 出発地点（一覧表示・検索・ルート最適化の起点）
    departure_address = models.TextField(blank=True, default='', verbose_name='出発地点')
    departure_place_id = models.CharField(max_length=255, blank=True, default='')
    departure_latitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True,
        validators=[MinValueValidator(-90), MaxValueValidator(90)],
    )
    departure_longitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True,
        validators=[MinValueValidator(-180), MaxValueValidator(180)],
    )
    shift_start = models.TimeField(null=True, blank=True, verbose_name='稼働開始')
    max_daily_stops = models.PositiveSmallIntegerField(
        null=True, blank=True, default=10, verbose_name='1日の最大訪問数',
    )
    max_daily_luggage_count = models.PositiveSmallIntegerField(
        null=True, blank=True, default=20, verbose_name='1日の最大荷物個数',
    )
    operating_days = models.CharField(
        max_length=7,
        default='1111111',
        verbose_name='稼働曜日',
    )

    # ステータス情報
    license_expiry = models.DateField(verbose_name='免許証有効期限')
    is_available = models.BooleanField(
        default=True,
        verbose_name='自動割当の候補',
        help_text='オンの配達者だけが日次自動割当の候補になります。',
    )
    is_active = models.BooleanField(
        default=True,
        verbose_name='有効',
        help_text=(
            '配達者が有効かどうかを示します。'
            'プロフィールを削除する代わりに選択を解除してください。'
        ),
    )
    deactivated_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name='無効化日時',
    )

    # 作成日時・更新日時
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='作成日時')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新日時')

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

    def save(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        """
        is_active の変更時だけ、関連データを連動させる

        - 無効化: deactivated_at を記録し、紐づく User も停止。
        - 再有効化: deactivated_at をクリアし、紐づく User を再開。
        """
        previous_is_active: Optional[bool] = None
        if not self._state.adding and self.pk:
            previous_is_active = (
                type(self).objects.filter(pk=self.pk)
                .values_list('is_active', flat=True)
                .first()
            )

        if previous_is_active is True and not self.is_active:
            if self.deactivated_at is None:
                self.deactivated_at = timezone.now()
        elif previous_is_active is False and self.is_active:
            self.deactivated_at = None

        super().save(*args, **kwargs)

        if previous_is_active is not None and previous_is_active == self.is_active:
            return
        if not self.user_id:
            return
        driver_user = self.user
        if driver_user.is_active != self.is_active:
            driver_user.is_active = self.is_active
            driver_user.save(update_fields=['is_active'])


class DriverInvitation(models.Model):
    """事業者が送信した配達者登録の確認依頼"""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business_owner = models.ForeignKey(
        'business_owners.BusinessProfile',
        on_delete=models.CASCADE,
        related_name='driver_invitations',
    )
    email = models.EmailField(db_index=True)
    last_name = models.CharField(max_length=150, blank=True)
    first_name = models.CharField(max_length=150, blank=True)
    company_name = models.CharField(max_length=200, blank=True)
    departure_address = models.TextField(blank=True)
    departure_place_id = models.CharField(max_length=255, blank=True)
    departure_latitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True,
        validators=[MinValueValidator(-90), MaxValueValidator(90)],
    )
    departure_longitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True,
        validators=[MinValueValidator(-180), MaxValueValidator(180)],
    )
    shift_start = models.TimeField(null=True, blank=True)
    max_daily_stops = models.PositiveSmallIntegerField(
        null=True, blank=True, default=10, verbose_name='1日の最大訪問数',
    )
    max_daily_luggage_count = models.PositiveSmallIntegerField(
        null=True, blank=True, default=20, verbose_name='1日の最大荷物個数',
    )
    license_expiry = models.DateField(verbose_name='免許証有効期限')
    operating_days = models.CharField(max_length=7, default='1111111')
    is_available = models.BooleanField(default=True)
    profile_picture = models.ImageField(
        upload_to='driver_invites/',
        blank=True,
        null=True,
    )
    # メールに載せる生トークンは保存せず、SHA-256 のハッシュ値だけを保存
    token_digest = models.CharField(max_length=64, unique=True, db_index=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'driver_invitations'
        indexes = [
            models.Index(fields=['business_owner', 'email', 'created_at']),
            models.Index(fields=['email', 'expires_at']),
        ]

    def is_valid(self) -> bool:
        return self.used_at is None and timezone.now() <= self.expires_at

    def mark_as_used(self) -> None:
        self.used_at = timezone.now()
        self.profile_picture = None
        self.save(update_fields=['used_at', 'profile_picture'])
