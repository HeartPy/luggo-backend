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


def validate_invoice_registration_number(value: str) -> None:
    """適格請求書発行事業者登録番号（インボイス番号）のバリデーション"""
    if not value:
        return
    if not re.match(r'^T\d{13}$', value):
        raise ValidationError('適格請求書発行事業者登録番号は「T」＋13桁の数字で入力してください。')


class BusinessProfileManager(models.Manager):
    """有効な事業者のみを取得するマネージャー"""
    def get_queryset(self):
        return super().get_queryset().filter(is_active=True)


class StripeReviewStatus(models.TextChoices):
    """Stripe Connect アカウントの審査状態（当サービス側で扱う集約ステータス）"""
    UNKNOWN = 'unknown', '未取得'
    INCOMPLETE = 'incomplete', '入力未完了'
    PENDING = 'pending', '審査中'
    RESTRICTED = 'restricted', '要対応'
    ENABLED = 'enabled', '利用可能'
    REJECTED = 'rejected', '利用不可'


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

    invoice_registration_number = models.CharField(
        max_length=14,
        blank=True,
        default='',
        verbose_name='適格請求書発行事業者登録番号',
        help_text='「T」＋13桁の数字（例: T1234567890123）。任意。',
        validators=[validate_invoice_registration_number],
    )

    # 代表者情報
    rep_last_name_kanji = models.CharField(max_length=50, blank=True, verbose_name='代表者姓（漢字）')
    rep_first_name_kanji = models.CharField(max_length=50, blank=True, verbose_name='代表者名（漢字）')
    rep_last_name_kana = models.CharField(max_length=50, blank=True, verbose_name='代表者姓（カナ）')
    rep_first_name_kana = models.CharField(max_length=50, blank=True, verbose_name='代表者名（カナ）')

    # サービス情報
    service_areas = models.JSONField(default=list, blank=True, verbose_name='事業所在地')

    # 営業情報
    operating_hours_start = models.TimeField(default='09:00', verbose_name='営業開始時間')
    operating_hours_end = models.TimeField(default='17:00', verbose_name='営業終了時間')
    operating_days = models.CharField(max_length=7, default='1111111', verbose_name='営業日')

    # 第N週曜日の定休日（例: ["1-0","3-4"] → 第1月曜・第3金曜）
    nth_weekday_holidays = models.JSONField(
        default=list,
        blank=True,
        verbose_name='第N週曜日定休日',
        help_text='["週番号-曜日番号", ...] 週:1-4, 曜日:0=月〜6=日'
    )

    # 1日の最大荷物個数（-1=制限なし, 0=予約不可, 1以上=上限値）
    daily_max_luggage = models.IntegerField(
        default=0,
        verbose_name='1日の最大荷物個数',
        help_text='-1の場合は制限なし。0の場合は予約不可。1以上で上限値。'
    )

    # 臨時休業日
    temporary_closures = models.JSONField(
        default=list,
        blank=True,
        verbose_name='臨時休業日',
        help_text='ISO形式の日付文字列リスト例: ["2026-04-10", "2026-05-03"]'
    )

    # ===== 事業者の予約サイト（旅行者向けページ）に関する同意・確認状態 =====
    # 対象:
    #   - /booking/transaction-law（特定商取引法に基づく表記）
    #   - /booking/privacy（プライバシーポリシー）
    # 旅行者への公開可否（初回同意）と、公開後の変更・テンプレート改訂の
    # 確認状態をここで管理する。

    # 旅行者への公開について事業者が初回同意した日時。
    # 同意済みでないと決済（Stripe）の設定に進めない。
    public_info_consent_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name='公開情報表示同意日時',
        help_text='特定商取引法に基づく表記とプライバシーポリシーをユーザーに表示することへ同意した日時'
    )

    # ページのテンプレート（プラットフォーム側で管理する静的文言）について
    # 事業者が最後に確認したバージョン。policy_versions.py の現行値と
    # 一致しない場合、ログイン後に変更内容の確認ポップアップが表示される
    # （ソフトブロック: 未確認でも閲覧・操作は継続可能）。
    booking_transaction_law_acknowledged_version = models.CharField(
        max_length=10,
        null=True,
        blank=True,
        verbose_name='確認済み予約サイト特商法テンプレートバージョン',
        help_text='ISO 8601 形式の日付（YYYY-MM-DD）。null の場合は未確認。'
    )
    booking_transaction_law_acknowledged_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name='予約サイト特商法テンプレート確認日時'
    )
    booking_privacy_acknowledged_version = models.CharField(
        max_length=10,
        null=True,
        blank=True,
        verbose_name='確認済み予約サイトプライバシーポリシーテンプレートバージョン',
        help_text='ISO 8601 形式の日付（YYYY-MM-DD）。null の場合は未確認。'
    )
    booking_privacy_acknowledged_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name='予約サイトプライバシーポリシーテンプレート確認日時'
    )

    # ===== LugGo プラットフォーム本体の利用規約・プライバシーポリシーへの同意状態 =====
    # 規約が改訂された場合、agreed_version が現行バージョン
    # （policy_versions.py の値）と一致しないため、ログイン後に再同意の
    # ポップアップが表示される。
    terms_agreed_version = models.CharField(
        max_length=10,
        null=True,
        blank=True,
        verbose_name='同意済み利用規約バージョン',
        help_text='ISO 8601 形式の日付（YYYY-MM-DD）。null の場合は未同意。'
    )
    terms_agreed_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name='利用規約同意日時'
    )
    privacy_agreed_version = models.CharField(
        max_length=10,
        null=True,
        blank=True,
        verbose_name='同意済みプライバシーポリシーバージョン',
        help_text='ISO 8601 形式の日付（YYYY-MM-DD）。null の場合は未同意。'
    )
    privacy_agreed_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name='プライバシーポリシー同意日時'
    )

    # 料金設定
    pricing_rules = models.JSONField(
        default=dict,
        blank=True,
        verbose_name='料金設定',
    )

    # 事業設定の一時保存
    settings_draft = models.JSONField(
        default=None,
        null=True,
        blank=True,
        verbose_name='事業設定の一時保存',
    )
    settings_draft_saved_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name='事業設定の一時保存日時',
    )

    # 料金設定の一時保存
    pricing_draft = models.JSONField(
        default=None,
        null=True,
        blank=True,
        verbose_name='料金設定の一時保存',
    )
    pricing_draft_saved_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name='料金設定の一時保存日時',
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
    stripe_account_id = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        default=None,
        unique=True,
        verbose_name='StripeアカウントID',
    )

    # Stripe Connect アカウント由来の表示用情報のキャッシュ。
    # 領収書・特定商取引法に基づく表記・確認メールで発行者情報を表示する際、
    # Stripe API 取得に失敗してもここへフォールバックできるようにする。
    stripe_address_cache = models.TextField(
        blank=True,
        default="",
        verbose_name='Stripe所在地キャッシュ',
    )
    stripe_support_email_cache = models.CharField(
        max_length=254,
        blank=True,
        default="",
        verbose_name='Stripe問い合わせメールキャッシュ',
    )
    stripe_support_phone_cache = models.CharField(
        max_length=50,
        blank=True,
        default="",
        verbose_name='Stripe電話番号キャッシュ',
    )
    stripe_representative_name_cache = models.CharField(
        max_length=200,
        blank=True,
        default="",
        verbose_name='Stripe代表者名キャッシュ',
    )
    stripe_info_cached_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name='Stripe事業者情報キャッシュ更新日時',
    )

    # Stripe Connect 審査状態（account.updated Webhook で同期）
    stripe_review_status = models.CharField(
        max_length=20,
        choices=StripeReviewStatus.choices,
        default=StripeReviewStatus.UNKNOWN,
        verbose_name='Stripe審査状態',
    )
    stripe_charges_enabled = models.BooleanField(
        default=False,
        verbose_name='Stripe決済有効',
    )
    stripe_payouts_enabled = models.BooleanField(
        default=False,
        verbose_name='Stripe入金有効',
    )
    stripe_details_submitted = models.BooleanField(
        default=False,
        verbose_name='Stripe情報提出済み',
    )
    stripe_disabled_reason = models.CharField(
        max_length=100,
        blank=True,
        default='',
        verbose_name='Stripe無効化理由',
    )
    stripe_currently_due = models.JSONField(
        default=list,
        blank=True,
        verbose_name='Stripe要対応項目',
        help_text='requirements.currently_due + past_due の項目リスト',
    )
    stripe_review_status_updated_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name='Stripe審査状態更新日時',
    )

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
