from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.contrib.auth.base_user import BaseUserManager
from django.db import models
from django.core.validators import RegexValidator
from django.utils import timezone
from typing import Any, Dict
import uuid


class UserManager(BaseUserManager['User']):
    """カスタムUserManager - emailベースの認証用"""

    def create_user(self, email: str, password: str, **extra_fields: Any) -> 'User':
        """通常ユーザーの作成"""
        if not email:
            raise ValueError('メールアドレスは必須です')
        if not password:
            raise ValueError('パスワードは必須です')

        # 旅行者（customer）タイプの作成を禁止
        user_type = extra_fields.get('user_type')
        if user_type == 'customer':
            raise ValueError('旅行者はUserとして保存できません。予約情報に直接保存してください。')

        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email: str, password: str, **extra_fields: Any) -> 'User':
        """スーパーユーザーの作成"""
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        extra_fields.setdefault('is_active', True)
        extra_fields.setdefault('user_type', 'admin')

        if extra_fields.get('is_staff') is not True:
            raise ValueError('スーパーユーザーはis_staff=Trueである必要があります')
        if extra_fields.get('is_superuser') is not True:
            raise ValueError('スーパーユーザーはis_superuser=Trueである必要があります')

        return self.create_user(email, password, **extra_fields)


class User(AbstractUser):
    """
    LugGo統合ユーザーモデル
    事業者、配達者、管理者を統合管理
    旅行者はUserとして保存せず、予約情報に直接保存されます
    """

    # カスタムマネージャーを指定（親の型定義と食い違うため無視）
    objects = UserManager()  # type: ignore[misc,assignment]

    # usernameフィールドを無効化（型上の上書きを抑制）
    username = None  # type: ignore[assignment]

    # 基本認証情報
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(unique=True, verbose_name='メールアドレス')
    phone_number = models.CharField(
        max_length=15,
        blank=True,
        verbose_name='電話番号',
        validators=[RegexValidator(r'^\+?\d{1,3}?\d{9,15}$', '有効な電話番号が必要です。')]
    )

    # ユーザータイプ
    USER_TYPE_CHOICES = [
        ('business_owner', '事業者'),
        ('delivery_driver', '配達者'),
        ('admin', '管理者'),
    ]
    user_type = models.CharField(
        max_length=20,
        choices=USER_TYPE_CHOICES,
        verbose_name='ユーザータイプ',
    )

    # 共通プロフィール情報
    profile_picture = models.ImageField(upload_to='profile_pics/', blank=True, null=True)

    # アカウント管理
    verification_code = models.CharField(max_length=6, blank=True)
    verification_code_expires_at = models.DateTimeField(null=True, blank=True, verbose_name='認証コード有効期限')
    verification_code_attempts = models.IntegerField(default=0, verbose_name='認証コード検証失敗回数')
    login_session_started_at = models.DateTimeField(null=True, blank=True, verbose_name='ログインセッション開始時刻')

    # 作成・更新日時
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='作成日時')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新日時')

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = []

    class Meta:
        db_table = 'users'
        verbose_name = 'ユーザー'
        verbose_name_plural = 'ユーザー'
        indexes = [
            models.Index(fields=['user_type']),
            models.Index(fields=['email']),
            models.Index(fields=['created_at']),
        ]

    def __str__(self) -> str:
        if self.is_business_owner():
            if hasattr(self, 'business_profile') and self.business_profile.company_name:
                return f"{self.business_profile.company_name} ({self.get_user_type_display()})"
            return f"{self.email} ({self.get_user_type_display()})"
        else:
            return f"{self.get_full_name()} ({self.get_user_type_display()})"

    # ユーザータイプ別判定メソッド
    def is_business_owner(self) -> bool:
        return self.user_type == 'business_owner'

    def is_delivery_driver(self) -> bool:
        return self.user_type == 'delivery_driver'

    def is_admin_user(self) -> bool:
        return self.user_type == 'admin'

    def get_dashboard_url(self) -> str:
        """ユーザータイプ別のダッシュボードURL"""
        admin_path = getattr(settings, 'DJANGO_ADMIN_PATH', 'admin').strip().strip('/')
        urls: Dict[str, str] = {
            'business_owner': '/business-owner/dashboard',
            'delivery_driver': '/driver/dashboard',
            'admin': f'/{admin_path}/',
        }
        return urls.get(self.user_type, '/')

    def get_full_name(self) -> str:
        return f"{self.last_name} {self.first_name}".strip() or self.email


class PasswordResetToken(models.Model):
    """パスワード再設定用のトークンモデル"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='password_reset_tokens',
        verbose_name='ユーザー',
    )
    token = models.CharField(max_length=64, unique=True, db_index=True, verbose_name='トークン')
    expires_at = models.DateTimeField(verbose_name='有効期限')
    used_at = models.DateTimeField(null=True, blank=True, verbose_name='使用日時')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='作成日時')

    class Meta:
        db_table = 'password_reset_tokens'
        verbose_name = 'パスワード再設定トークン'
        verbose_name_plural = 'パスワード再設定トークン'
        indexes = [
            models.Index(fields=['user', 'expires_at']),
            models.Index(fields=['token']),
        ]

    def __str__(self) -> str:
        return f"PasswordResetToken: {self.user.email}"

    def is_valid(self) -> bool:
        """トークンが有効かどうかをチェック"""
        if self.used_at is not None:
            return False
        if timezone.now() > self.expires_at:
            return False
        return True

    def mark_as_used(self) -> None:
        """トークンを使用済みとしてマーク"""
        self.used_at = timezone.now()
        self.save(update_fields=['used_at'])
