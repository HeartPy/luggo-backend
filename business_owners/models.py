from django.db import models
from django.conf import settings
from django.utils import timezone
from django.core.exceptions import ValidationError
from decimal import Decimal
from typing import Optional
import uuid
import re


# 禁止単語リスト
FORBIDDEN_SUBDOMAINS = {
    # 汎用性の高い単語
    'test', 'admin', 'administrator', 'root', 'www', 'mail', 'email', 'ftp', 'localhost',
    'api', 'app', 'dev', 'development', 'staging', 'prod', 'production', 'demo', 'example',
    'blog', 'news', 'help', 'support', 'contact', 'about', 'terms', 'privacy', 'policy',
    'login', 'logout', 'signup', 'signin', 'register', 'reserve', 'account', 'dashboard', 'panel',
    'manage', 'management', 'system', 'server', 'service', 'services', 'site', 'sites',
    'web', 'website', 'page', 'pages', 'home', 'index', 'main', 'default', 'public',
    'private', 'secure', 'ssl', 'http', 'https', 'tcp', 'udp', 'ip', 'dns', 'domain',
    'subdomain', 'sub', 'domain', 'host', 'hosting', 'server', 'cloud', 'aws', 'azure',
    'google', 'microsoft', 'apple', 'facebook', 'twitter', 'instagram', 'youtube',
    # 卑猥・卑語
    'sex', 'porn', 'xxx', 'nsfw', 'adult', 'erotic', 'nude', 'naked', 'hentai', 'fetish',
    'fuck', 'shit', 'ass', 'dick', 'cock', 'cunt', 'bitch', 'whore',
    # 暴力的な単語
    'kill', 'death', 'violence', 'attack', 'war', 'fight', 'murder', 'blood', 'gun', 'bomb',
    'terror', 'suicide', 'rape', 'abuse', 'torture', 'slaughter', 'weapon', 'hate',
}


def validate_subdomain(value: str) -> None:
    """予約フォームのURLのバリデーション"""
    if not value:
        raise ValidationError('予約フォームのURLは必須です。')

    # 3文字以上12文字以内チェック
    if len(value) < 3:
        raise ValidationError('予約フォームのURLは3文字以上である必要があります。')
    if len(value) > 12:
        raise ValidationError('予約フォームのURLは12文字以内である必要があります。')

    # 半角小文字英字のみチェック
    if not re.match(r'^[a-z]+$', value):
        raise ValidationError('予約フォームのURLは半角小文字の英字のみ使用できます。')

    # 禁止単語チェック
    if value.lower() in FORBIDDEN_SUBDOMAINS:
        raise ValidationError('この予約フォームのURLは使用できません。別の文字列を入力してください。')


class BusinessProfileManager(models.Manager):
    """有効な事業者のみを取得するマネージャー"""
    def get_queryset(self):
        return super().get_queryset().filter(is_active=True)


class BusinessProfile(models.Model):
    """事業者プロフィール"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='business_profile')
    business_type = models.CharField(
        max_length=20,
        choices=[('company', '法人'), ('individual', '個人事業主')],
        default='individual',
        verbose_name='事業形態'
    )
    company_name = models.CharField(max_length=100, verbose_name='会社名')
    company_email = models.EmailField(verbose_name='メールアドレス')
    tax_id = models.CharField(max_length=20, blank=True, verbose_name='法人番号')

    # 代表者情報
    rep_last_name_kanji = models.CharField(max_length=50, blank=True, verbose_name='代表者姓（漢字）')
    rep_first_name_kanji = models.CharField(max_length=50, blank=True, verbose_name='代表者名（漢字）')
    rep_last_name_kana = models.CharField(max_length=50, blank=True, verbose_name='代表者姓（カナ）')
    rep_first_name_kana = models.CharField(max_length=50, blank=True, verbose_name='代表者名（カナ）')

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
    approval_date = models.DateTimeField(null=True, blank=True, verbose_name='承認日')
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

    # 予約フォームのURL
    subdomain = models.CharField(
        max_length=12,
        unique=True,
        null=False,
        blank=False,
        db_index=True,
        verbose_name='予約フォームのURL',
        help_text='3文字以上12文字以内、半角小文字英字のみ',
        validators=[validate_subdomain]
    )

    class Meta:
        db_table = 'business_profiles'
        verbose_name = '事業者プロフィール'
        verbose_name_plural = '事業者プロフィール'

    def __str__(self) -> str:
        company_info = self.company_name if self.company_name else "個人事業主"
        return company_info

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


class RegistrationToken(models.Model):
    """事業者アカウント登録用のトークンモデル"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(verbose_name='メールアドレス', db_index=True)
    token = models.CharField(max_length=64, unique=True, db_index=True, verbose_name='トークン')
    expires_at = models.DateTimeField(verbose_name='有効期限')
    used_at = models.DateTimeField(null=True, blank=True, verbose_name='使用日時')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='作成日時')

    class Meta:
        db_table = 'registration_tokens'
        verbose_name = '登録トークン'
        verbose_name_plural = '登録トークン'
        indexes = [
            models.Index(fields=['email', 'expires_at']),
            models.Index(fields=['token']),
        ]

    def __str__(self) -> str:
        return f"RegistrationToken: {self.email}"

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
        self.save()
