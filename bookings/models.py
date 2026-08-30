from django.db import models
from django.db.models import Q
from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.utils import timezone
import uuid
from datetime import date, datetime, timedelta, time as dtime
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

    # 返金状況（Stripe 上の返金状態を LugGo 側で追跡し、整合性を担保するために使う）
    REFUND_STATUS_NONE = 'none'
    REFUND_STATUS_PENDING = 'pending'
    REFUND_STATUS_SUCCEEDED = 'succeeded'
    REFUND_STATUS_FAILED = 'failed'
    REFUND_STATUS_CANCELED = 'canceled'
    REFUND_STATUS_CHOICES = [
        (REFUND_STATUS_NONE, '返金なし'),
        (REFUND_STATUS_PENDING, '返金処理中'),
        (REFUND_STATUS_SUCCEEDED, '返金完了'),
        (REFUND_STATUS_FAILED, '返金失敗'),
        (REFUND_STATUS_CANCELED, '返金取消'),
    ]

    # 基本情報
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
        verbose_name='ID',
    )
    booking_number = models.CharField(
        max_length=20,
        unique=True,
        blank=True,
        verbose_name='予約番号',
    )
    business_owner = models.ForeignKey(
        'business_owners.BusinessProfile',
        # 予約は会計・監査の証跡のため、事業者の物理削除では消さない
        # （運用上の停止は BusinessProfile.is_active のソフト無効化で行う）
        on_delete=models.PROTECT,
        related_name='bookings',
        verbose_name='事業者',
        help_text='この予約を担当する事業者',
    )
    delivery_status = models.CharField(
        max_length=20,
        choices=DELIVERY_STATUS_CHOICES,
        default='before_pickup',
        verbose_name='配達状況'
    )
    driver = models.ForeignKey(
        'drivers.DriverProfile',
        on_delete=models.SET_NULL,
        related_name='assigned_bookings',
        null=True,
        blank=True,
        verbose_name='配達者',
        help_text='この予約の配達担当者。集荷担当未指定時は集荷も担当する'
    )
    pickup_driver = models.ForeignKey(
        'drivers.DriverProfile',
        on_delete=models.SET_NULL,
        related_name='pickup_assigned_bookings',
        null=True,
        blank=True,
        verbose_name='集荷担当者',
        help_text='配達担当者と集荷担当者を分ける場合のみ指定する'
    )
    delivery_manually_assigned = models.BooleanField(
        default=False,
        verbose_name='配達の手動割当',
        help_text='人が手動で配達担当を付けた場合に True。自動割当では False',
    )
    pickup_manually_assigned = models.BooleanField(
        default=False,
        verbose_name='集荷の手動割当',
        help_text='人が手動で集荷担当を付けた場合に True。自動割当では False',
    )

    # 集荷情報
    pickup_location_name = models.CharField(
        max_length=200,
        verbose_name='集荷場所の名称'
    )
    pickup_location_address = models.TextField(
        verbose_name='集荷場所の住所'
    )
    pickup_location_name_ja = models.CharField(
        max_length=200,
        blank=True,
        default='',
        verbose_name='集荷場所の名称（日本語）',
        help_text='非日本語で予約された場合の日本語表記（事業者の予約一覧用）',
    )
    pickup_location_address_ja = models.TextField(
        blank=True,
        default='',
        verbose_name='集荷場所の住所（日本語）',
        help_text='非日本語で予約された場合の日本語表記（事業者の予約一覧用）',
    )
    pickup_place_id = models.CharField(
        max_length=255,
        blank=True,
        default='',
        verbose_name='集荷場所の Place ID',
    )
    pickup_latitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True,
        validators=[MinValueValidator(-90), MaxValueValidator(90)],
        verbose_name='集荷場所の緯度',
    )
    pickup_longitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True,
        validators=[MinValueValidator(-180), MaxValueValidator(180)],
        verbose_name='集荷場所の経度',
    )
    pickup_postal_code = models.CharField(
        max_length=20,
        blank=True,
        default='',
        verbose_name='集荷場所の郵便番号',
    )
    pickup_geocode_status = models.CharField(
        max_length=20,
        choices=[
            ('pending', '未処理'),
            ('verified', '確認済み'),
            ('approximate', '概算'),
            ('failed', '失敗'),
        ],
        default='pending',
        db_index=True,
        verbose_name='集荷場所のジオコード状態',
    )
    pickup_date = models.DateField(
        verbose_name='集荷日'
    )

    # 配達情報
    delivery_location_name = models.CharField(
        max_length=200,
        verbose_name='配達場所の名称'
    )
    delivery_location_address = models.TextField(
        verbose_name='配達場所の住所'
    )
    delivery_location_name_ja = models.CharField(
        max_length=200,
        blank=True,
        default='',
        verbose_name='配達場所の名称（日本語）',
        help_text='非日本語で予約された場合の日本語表記（事業者の予約一覧用）',
    )
    delivery_location_address_ja = models.TextField(
        blank=True,
        default='',
        verbose_name='配達場所の住所（日本語）',
        help_text='非日本語で予約された場合の日本語表記（事業者の予約一覧用）',
    )
    delivery_place_id = models.CharField(
        max_length=255,
        blank=True,
        default='',
        verbose_name='配達場所の Place ID',
    )
    delivery_latitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True,
        validators=[MinValueValidator(-90), MaxValueValidator(90)],
        verbose_name='配達場所の緯度',
    )
    delivery_longitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True,
        validators=[MinValueValidator(-180), MaxValueValidator(180)],
        verbose_name='配達場所の経度',
    )
    delivery_postal_code = models.CharField(
        max_length=20,
        blank=True,
        default='',
        verbose_name='配達場所の郵便番号',
    )
    delivery_geocode_status = models.CharField(
        max_length=20,
        choices=[
            ('pending', '未処理'),
            ('verified', '確認済み'),
            ('approximate', '概算'),
            ('failed', '失敗'),
        ],
        default='pending',
        db_index=True,
        verbose_name='配達場所のジオコード状態',
    )
    delivery_date = models.DateField(
        verbose_name='配達日'
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
        max_length=16,
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
    # 旅行客が予約時に選択した表示言語（メール送信の言語判定に使用）
    CUSTOMER_LANGUAGE_CHOICES = [
        ('ja', '日本語'),
        ('en', '英語'),
        ('zh-Hans', '中国語（簡体字）'),
        ('zh-Hant', '中国語（繁体字）'),
    ]
    customer_language = models.CharField(
        max_length=10,
        choices=CUSTOMER_LANGUAGE_CHOICES,
        default='ja',
        verbose_name='顧客の表示言語',
    )

    # 支払い情報
    payment_intent_id = models.CharField(
        max_length=255,
        verbose_name='Stripe Payment Intent ID'
    )

    # 返金情報。Stripe と LugGo の間で
    # 返金状態のズレが生じないよう、返金の識別子・状態・金額・日時を保持。
    refund_status = models.CharField(
        max_length=20,
        choices=REFUND_STATUS_CHOICES,
        default=REFUND_STATUS_NONE,
        db_index=True,
        verbose_name='返金状況',
    )
    stripe_refund_id = models.CharField(
        max_length=255,
        blank=True,
        default='',
        verbose_name='Stripe Refund ID',
    )
    stripe_charge_id = models.CharField(
        max_length=255,
        blank=True,
        default='',
        verbose_name='Stripe Charge ID',
    )
    refunded_amount = models.PositiveIntegerField(
        default=0,
        verbose_name='返金済み金額',
    )
    refunded_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name='返金完了日時',
    )
    refund_reconciled_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name='返金整合性チェック日時',
    )

    # 配達者による集荷・配達の実績情報
    picked_up_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name='集荷完了日時',
    )
    facility_fee = models.PositiveIntegerField(
        null=True,
        blank=True,
        verbose_name='施設側の手数料（円）',
    )
    transport_cost = models.PositiveIntegerField(
        null=True,
        blank=True,
        verbose_name='高速代などの交通費（円）',
    )
    # 配達完了時に施設側の担当者が入力する手書きサイン（PNGのdata URL）
    delivery_signature = models.TextField(
        blank=True,
        default='',
        verbose_name='配達完了時の手書きサイン',
    )

    # 配達完了と事業者への送金情報
    delivered_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name='配達完了日時',
    )
    stripe_transfer_id = models.CharField(
        max_length=255,
        blank=True,
        default='',
        verbose_name='Stripe Transfer ID',
    )
    transferred_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name='事業者への送金日時',
    )
    transferred_gross_amount = models.PositiveIntegerField(
        null=True,
        blank=True,
        verbose_name='送金時の返金控除後売上額',
    )
    transferred_platform_fee = models.PositiveIntegerField(
        null=True,
        blank=True,
        verbose_name='送金時のプラットフォーム手数料',
    )
    # 決済資金が Stripe 上で入金可能になる日時
    funds_available_on = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name='Stripe資金の入金可能日時',
    )

    # 発行者情報のスナップショット。
    # 決済確定時点の事業者名・住所・連絡先を保存し、領収書はこの値を優先して使用する。
    issuer_name = models.CharField(
        max_length=255,
        blank=True,
        default='',
        verbose_name='発行者名（スナップショット）',
    )
    issuer_name_en = models.CharField(
        max_length=255,
        blank=True,
        default='',
        verbose_name='発行者名英語表記（スナップショット）',
    )
    issuer_address = models.TextField(
        blank=True,
        default='',
        verbose_name='発行者住所（スナップショット）',
    )
    issuer_email = models.CharField(
        max_length=254,
        blank=True,
        default='',
        verbose_name='発行者メール（スナップショット）',
    )
    issuer_phone = models.CharField(
        max_length=50,
        blank=True,
        default='',
        verbose_name='発行者電話番号（スナップショット）',
    )
    issuer_invoice_number = models.CharField(
        max_length=14,
        blank=True,
        default='',
        verbose_name='適格請求書発行事業者登録番号（スナップショット）',
    )
    issuer_snapshot_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name='発行者情報スナップショット保存日時',
    )

    # 作成日・更新日
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='予約日時')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新日時')

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
            models.Index(fields=['delivered_at']),
            models.Index(fields=['business_owner', 'pickup_date']),
            models.Index(fields=['business_owner', 'delivery_date']),
            models.Index(fields=['business_owner', 'delivery_status']),
        ]
        constraints = [
            # 同一決済（payment_intent_id）に対する予約の二重作成を DB レベルで防ぐ
            models.UniqueConstraint(
                fields=['payment_intent_id'],
                condition=~Q(payment_intent_id=''),
                name='uniq_booking_payment_intent_id',
            ),
        ]

    def __str__(self) -> str:
        return (
            f'{self.get_delivery_status_display()} / {self.booking_number} / '
            f'{self.pickup_location_name} → {self.delivery_location_name}'
        )

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

    # 領収書を発行できる配達状況（集荷済以降。集荷前は返金キャンセルの余地があるため除外）
    RECEIPT_ELIGIBLE_STATUSES = ('picked_up', 'delivered')

    def can_cancel(self) -> bool:
        """キャンセル可能かチェック"""
        return self.delivery_status == 'before_pickup'

    def can_download_receipt(self) -> bool:
        """領収書を発行できるかチェック（集荷済以降のみ）"""
        return self.delivery_status in self.RECEIPT_ELIGIBLE_STATUSES

    def refund_deadline(self) -> datetime:
        """返金対象となるキャンセルの締切（集荷日前日の23時00分）"""
        deadline_naive = datetime.combine(
            self.pickup_date - timedelta(days=1),
            dtime(23, 0),
        )
        if settings.USE_TZ:
            return timezone.make_aware(deadline_naive)
        return deadline_naive

    def is_refundable_on_cancel(self) -> bool:
        """キャンセル時に返金可能か（集荷日前日23時00分以降は返金対象外）"""
        now = timezone.now() if settings.USE_TZ else datetime.now()
        return now < self.refund_deadline()

    def days_until_pickup(self) -> int:
        """集荷日までの日数（Asia/Tokyo の日付基準）"""
        today = timezone.localdate() if settings.USE_TZ else date.today()
        if self.pickup_date >= today:
            return (self.pickup_date - today).days
        return 0

    # 事業者向け表示（日本語優先）
    # 事業者の予約一覧・CSV では日本語表記（_ja）があればそちらを優先

    @property
    def pickup_location_name_display(self) -> str:
        return self.pickup_location_name_ja or self.pickup_location_name

    @property
    def pickup_location_address_display(self) -> str:
        return self.pickup_location_address_ja or self.pickup_location_address

    @property
    def delivery_location_name_display(self) -> str:
        return self.delivery_location_name_ja or self.delivery_location_name

    @property
    def delivery_location_address_display(self) -> str:
        return self.delivery_location_address_ja or self.delivery_location_address

    @property
    def effective_pickup_driver(self):
        """集荷担当者（分業指定がなければ配達担当者）"""
        return self.pickup_driver or self.driver

    @property
    def is_split_assignment(self) -> bool:
        """集荷担当と配達担当が別々に設定されているか"""
        return (
            self.pickup_driver_id is not None
            and self.pickup_driver_id != self.driver_id
        )


class PendingBooking(models.Model):
    """
    決済成功後に予約レコードを確実に作成するためのフォールバック用データ

    create_payment_intent 時点で検証済みの予約情報をここへ保存しておく。
    通常はフロントの予約作成POSTで LuggageBooking が作成されるが、
    決済直後にユーザーが離脱した・通信エラー等で予約POSTが失敗した場合でも、
    Stripe Webhook（payment_intent.succeeded）がこのデータから予約を作成できる。

    顧客の個人情報を Stripe metadata に載せずに済むよう、別テーブルで保持する。
    """

    payment_intent_id = models.CharField(
        max_length=255,
        unique=True,
        verbose_name='Stripe Payment Intent ID',
    )
    business_owner = models.ForeignKey(
        'business_owners.BusinessProfile',
        on_delete=models.CASCADE,
        related_name='pending_bookings',
        null=True,
        blank=True,
        verbose_name='事業者',
    )
    # 予約作成に必要なフィールド一式（集荷/配達先・日付・顧客情報・荷物個数など）。
    # 検証済みの値のみを保存する。
    payload = models.JSONField(
        default=dict,
        verbose_name='予約作成用データ',
    )
    total_amount = models.PositiveIntegerField(
        default=0,
        verbose_name='合計金額',
    )
    # LuggageBooking が作成された日時（通常 POST / Webhook どちらでも記録）。
    # NULL = 未使用（フォールバック待ち）、日時あり = 予約作成済みで使用済み。
    consumed_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name='予約作成完了日時',
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='作成日時')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新日時')

    class Meta:
        db_table = 'pending_bookings'
        verbose_name = '保留中の予約（フォールバック用）'
        verbose_name_plural = '保留中の予約（フォールバック用）'
        ordering = ['-created_at']

    def __str__(self) -> str:
        status = '予約作成済み' if self.consumed_at else '予約未作成'
        amount = f'¥{int(self.total_amount):,}'
        company = ''
        if self.business_owner_id is not None:
            company = getattr(self.business_owner, 'company_name', '') or ''
        company = company or '事業者未設定'
        return f'{status} / {amount} / {company} / {self.payment_intent_id}'


class ChargeDispute(models.Model):
    """Stripe のチャージバック（異議申立て / dispute）の記録"""

    dispute_id = models.CharField(
        max_length=255,
        unique=True,
        verbose_name='Stripe 異議申立て ID',
    )
    charge_id = models.CharField(
        max_length=255,
        blank=True,
        default='',
        verbose_name='Stripe Charge ID',
    )
    payment_intent_id = models.CharField(
        max_length=255,
        blank=True,
        default='',
        db_index=True,
        verbose_name='Stripe Payment Intent ID',
    )
    booking = models.ForeignKey(
        'LuggageBooking',
        on_delete=models.SET_NULL,
        related_name='disputes',
        null=True,
        blank=True,
        verbose_name='対応する予約',
    )
    business_owner = models.ForeignKey(
        'business_owners.BusinessProfile',
        on_delete=models.SET_NULL,
        related_name='disputes',
        null=True,
        blank=True,
        verbose_name='事業者',
    )
    amount = models.PositiveIntegerField(
        default=0,
        verbose_name='異議申立て金額',
    )
    currency = models.CharField(
        max_length=10,
        blank=True,
        default='',
        verbose_name='通貨',
    )
    reason = models.CharField(
        max_length=100,
        blank=True,
        default='',
        verbose_name='理由',
    )
    status = models.CharField(
        max_length=50,
        blank=True,
        default='',
        verbose_name='ステータス',
    )
    is_charge_refundable = models.BooleanField(
        default=False,
        verbose_name='チャージ返金可否',
    )
    evidence_due_by = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name='証拠提出期限日時',
    )
    opened_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name='異議申立て発生日時',
    )
    closed_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name='クローズ日時',
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='作成日時')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新日時')

    class Meta:
        db_table = 'charge_disputes'
        verbose_name = 'チャージバック（異議申立て）'
        verbose_name_plural = 'チャージバック（異議申立て）'
        ordering = ['-created_at']

    # Stripe Dispute.status の Admin / 表示用ラベル
    STATUS_DISPLAY = {
        'needs_response': '要対応',
        'under_review': '審査中',
        'won': '勝訴',
        'lost': '敗訴',
        'warning_needs_response': '警告・要対応',
        'warning_under_review': '警告・審査中',
        'warning_closed': '警告・クローズ',
        'prevented': '防止済み',
    }

    def __str__(self) -> str:
        status_label = self.STATUS_DISPLAY.get(self.status, self.status or '状態不明')
        reason = self.reason or '理由不明'
        amount = f'¥{int(self.amount):,}'
        return f'{status_label} / {reason} / {amount} / {self.dispute_id}'


class StripeWebhookEvent(models.Model):
    """受信した Stripe Webhook イベントの処理記録（冪等化用）"""

    event_id = models.CharField(
        max_length=255,
        unique=True,
        verbose_name='Stripe Event ID',
    )
    event_type = models.CharField(
        max_length=100,
        blank=True,
        default='',
        verbose_name='イベント種別',
    )
    received_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name='受信日時',
    )
    processed_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name='処理完了日時',
    )

    class Meta:
        db_table = 'stripe_webhook_events'
        verbose_name = 'Stripe Webhook イベント'
        verbose_name_plural = 'Stripe Webhook イベント'
        ordering = ['-received_at']

    def __str__(self) -> str:
        status = '処理済み' if self.processed_at else '未処理'
        event_type = self.event_type or '種別不明'
        return f'{event_type} / {status} / {self.event_id}'


class BookingAuditLog(models.Model):
    """予約の返金・キャンセルに関する監査ログ"""

    # 変更の発生経路
    SOURCE_OWNER_API = 'owner_api'
    SOURCE_CUSTOMER_API = 'customer_api'
    SOURCE_WEBHOOK = 'webhook'
    SOURCE_DASHBOARD = 'dashboard'
    SOURCE_SYSTEM = 'system'
    SOURCE_CHOICES = [
        (SOURCE_OWNER_API, '事業者API'),
        (SOURCE_CUSTOMER_API, '顧客API'),
        (SOURCE_WEBHOOK, 'Stripe Webhook'),
        (SOURCE_DASHBOARD, 'Stripe ダッシュボード（手動）'),
        (SOURCE_SYSTEM, 'システム'),
    ]

    # 記録する操作の種類
    ACTION_REFUND_REQUESTED = 'refund_requested'
    ACTION_REFUND_SUCCEEDED = 'refund_succeeded'
    ACTION_REFUND_PENDING = 'refund_pending'
    ACTION_REFUND_FAILED = 'refund_failed'
    ACTION_REFUND_CANCELED = 'refund_canceled'
    ACTION_BOOKING_CANCELLED = 'booking_cancelled'
    ACTION_RECONCILED = 'reconciled'
    ACTION_TRANSFER_CREATED = 'transfer_created'
    ACTION_AUTO_ASSIGNED = 'auto_assigned'
    ACTION_CHOICES = [
        (ACTION_REFUND_REQUESTED, '返金リクエスト'),
        (ACTION_REFUND_SUCCEEDED, '返金完了'),
        (ACTION_REFUND_PENDING, '返金処理中'),
        (ACTION_REFUND_FAILED, '返金失敗'),
        (ACTION_REFUND_CANCELED, '返金取消'),
        (ACTION_BOOKING_CANCELLED, '予約キャンセル'),
        (ACTION_RECONCILED, '整合性同期'),
        (ACTION_TRANSFER_CREATED, '事業者への送金'),
        (ACTION_AUTO_ASSIGNED, '自動割当による担当割当'),
    ]

    booking = models.ForeignKey(
        'LuggageBooking',
        on_delete=models.SET_NULL,
        related_name='audit_logs',
        null=True,
        blank=True,
        verbose_name='対応する予約',
    )
    payment_intent_id = models.CharField(
        max_length=255,
        blank=True,
        default='',
        db_index=True,
        verbose_name='Stripe Payment Intent ID',
    )
    action = models.CharField(
        max_length=40,
        choices=ACTION_CHOICES,
        verbose_name='操作',
    )
    source = models.CharField(
        max_length=20,
        choices=SOURCE_CHOICES,
        verbose_name='発生経路',
    )
    previous_delivery_status = models.CharField(
        max_length=20,
        blank=True,
        default='',
        choices=LuggageBooking.DELIVERY_STATUS_CHOICES,
        verbose_name='変更前の配達状況',
    )
    new_delivery_status = models.CharField(
        max_length=20,
        blank=True,
        default='',
        choices=LuggageBooking.DELIVERY_STATUS_CHOICES,
        verbose_name='変更後の配達状況',
    )
    previous_refund_status = models.CharField(
        max_length=20,
        blank=True,
        default='',
        choices=LuggageBooking.REFUND_STATUS_CHOICES,
        verbose_name='変更前の返金状況',
    )
    new_refund_status = models.CharField(
        max_length=20,
        blank=True,
        default='',
        choices=LuggageBooking.REFUND_STATUS_CHOICES,
        verbose_name='変更後の返金状況',
    )
    stripe_event_id = models.CharField(
        max_length=255,
        blank=True,
        default='',
        verbose_name='Stripe Event ID',
    )
    stripe_refund_id = models.CharField(
        max_length=255,
        blank=True,
        default='',
        verbose_name='Stripe Refund ID',
    )
    stripe_charge_id = models.CharField(
        max_length=255,
        blank=True,
        default='',
        verbose_name='Stripe Charge ID',
    )
    amount = models.PositiveIntegerField(
        default=0,
        verbose_name='金額',
    )
    message = models.TextField(
        blank=True,
        default='',
        verbose_name='メモ',
    )
    metadata = models.JSONField(
        default=dict,
        blank=True,
        verbose_name='付随情報',
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='記録日時')

    class Meta:
        db_table = 'booking_audit_logs'
        verbose_name = '予約監査ログ'
        verbose_name_plural = '予約監査ログ'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['action']),
            models.Index(fields=['source']),
            models.Index(fields=['created_at']),
        ]

    def __str__(self) -> str:
        action_label = self.get_action_display()
        source_label = self.get_source_display()
        booking_number = ''
        if self.booking_id is not None:
            booking_number = getattr(self.booking, 'booking_number', '') or ''
        if booking_number:
            return f'{action_label} / {source_label} / {booking_number}'
        return f'{action_label} / {source_label}'
