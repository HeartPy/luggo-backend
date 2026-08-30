from decimal import Decimal

from django.contrib import admin
from django.contrib.auth.models import Group
from django.http import HttpRequest
from django.db.models import QuerySet
from users.models import User
from business_owners.models import BusinessProfile, PayoutDocumentDelivery
from drivers.models import DriverProfile
from routing.models import DailyAssignmentRun
from bookings.models import (
    BookingAuditLog,
    ChargeDispute,
    LuggageBooking,
    PendingBooking,
    StripeWebhookEvent,
)


# 権限グループ管理は現段階では使用しないため、Adminから非表示にする
admin.site.unregister(Group)


class PendingBookingCreatedStatusFilter(admin.SimpleListFilter):
    """保留中の予約を予約未作成 / 予約作成済みで絞り込む"""

    title = '予約作成状態'
    parameter_name = 'booking_created_status'

    def lookups(self, request: HttpRequest, model_admin: admin.ModelAdmin):  # type: ignore[type-arg]  # django-stubsではModelAdminがジェネリック扱いだが、実行時は非ジェネリックのため
        return (
            ('open', '予約未作成'),
            ('done', '予約作成済み'),
        )

    def queryset(
        self, request: HttpRequest, queryset: QuerySet[PendingBooking]
    ) -> QuerySet[PendingBooking]:
        if self.value() == 'open':
            return queryset.filter(consumed_at__isnull=True)
        if self.value() == 'done':
            return queryset.filter(consumed_at__isnull=False)
        return queryset


class PayoutDocumentSentStatusFilter(admin.SimpleListFilter):
    """入金書類を未送信 / 送信済みで絞り込む"""

    title = '送信状態'
    parameter_name = 'sent_status'

    def lookups(self, request: HttpRequest, model_admin: admin.ModelAdmin):  # type: ignore[type-arg]  # django-stubsではModelAdminがジェネリック扱いだが、実行時は非ジェネリックのため
        return (
            ('open', '未送信'),
            ('done', '送信済み'),
        )

    def queryset(
        self, request: HttpRequest, queryset: QuerySet[PayoutDocumentDelivery]
    ) -> QuerySet[PayoutDocumentDelivery]:
        if self.value() == 'open':
            return queryset.filter(sent_at__isnull=True)
        if self.value() == 'done':
            return queryset.filter(sent_at__isnull=False)
        return queryset


def _format_yen(amount: Decimal | int | None) -> str:
    """Admin表示用に円金額を整数＋カンマ＋円マークで整形"""
    if amount is None:
        return '—'
    value = int(amount)
    if value < 0:
        return f'-¥{abs(value):,}'
    return f'¥{value:,}'


# operating_days は月〜日の7文字（'1'=稼働/営業, '0'=休み）
_WEEKDAY_LABELS = ('月', '火', '水', '木', '金', '土', '日')

# 集荷地域などに使う都道府県コード → 名称
_PREFECTURE_NAMES: dict[str, str] = {
    '01': '北海道', '02': '青森県', '03': '岩手県', '04': '宮城県',
    '05': '秋田県', '06': '山形県', '07': '福島県', '08': '茨城県',
    '09': '栃木県', '10': '群馬県', '11': '埼玉県', '12': '千葉県',
    '13': '東京都', '14': '神奈川県', '15': '新潟県', '16': '富山県',
    '17': '石川県', '18': '福井県', '19': '山梨県', '20': '長野県',
    '21': '岐阜県', '22': '静岡県', '23': '愛知県', '24': '三重県',
    '25': '滋賀県', '26': '京都府', '27': '大阪府', '28': '兵庫県',
    '29': '奈良県', '30': '和歌山県', '31': '鳥取県', '32': '島根県',
    '33': '岡山県', '34': '広島県', '35': '山口県', '36': '徳島県',
    '37': '香川県', '38': '愛媛県', '39': '高知県', '40': '福岡県',
    '41': '佐賀県', '42': '長崎県', '43': '熊本県', '44': '大分県',
    '45': '宮崎県', '46': '鹿児島県', '47': '沖縄県',
}


def _format_operating_days(operating_days: str | None) -> str:
    """'1111100' のような稼働フラグを定休日『土・日』形式で表示"""
    if not operating_days or len(operating_days) != 7:
        return operating_days or ''
    labels = [
        label
        for flag, label in zip(operating_days, _WEEKDAY_LABELS, strict=True)
        if flag == '0'
    ]
    return '・'.join(labels) if labels else 'なし'


def _format_service_areas(service_areas: list[str] | None) -> str:
    """都道府県コードのリストを『東京都・神奈川県』形式で表示"""
    if not service_areas:
        return 'なし'
    labels = [
        _PREFECTURE_NAMES.get(str(code), str(code))
        for code in service_areas
    ]
    return '・'.join(labels)


@admin.register(User)
class UserAdmin(admin.ModelAdmin):  # type: ignore[type-arg]  # django-stubsではModelAdminがジェネリック扱いだが、実行時は非ジェネリックのため
    """
    ユーザーの閲覧とアカウント停止用

    編集できるのは is_active（アカウント停止）のみ。
    ユーザーの作成は createsuperuser / 各登録フローに寄せ、Adminからは行わない。
    パスワード・認証コード等の機微フィールドは表示しない。
    """

    list_display = [
        'email',
        'user_type',
        'full_name',
        'is_active',
        'is_staff',
        'created_at',
    ]
    search_fields = [
        'email',
        'first_name',
        'last_name',
        'phone_number',
    ]
    list_filter = [
        'user_type',
        'is_active',
        'is_staff',
    ]
    ordering = ['-created_at']

    fieldsets = (
        ('基本情報', {
            'fields': ('email', 'phone_number', 'user_type', 'last_name', 'first_name'),
        }),
        ('アカウント状態', {
            'fields': ('is_active', 'is_staff', 'is_superuser'),
        }),
        ('利用状況', {
            'fields': ('last_login', 'created_at', 'updated_at'),
        }),
    )

    readonly_fields = [
        'email',
        'phone_number',
        'user_type',
        'last_name',
        'first_name',
        'is_staff',
        'is_superuser',
        'last_login',
        'created_at',
        'updated_at',
    ]

    @admin.display(description='氏名')
    def full_name(self, obj: User) -> str:
        return obj.get_full_name()

    def has_add_permission(self, request: HttpRequest) -> bool:
        # ユーザー作成は登録フロー・createsuperuser経由のみ
        return False

    def has_delete_permission(self, request: HttpRequest, obj=None) -> bool:
        # 削除ではなく is_active=False（停止）で対応する
        return False


@admin.register(BusinessProfile)
class BusinessProfileAdmin(admin.ModelAdmin):  # type: ignore[type-arg]  # django-stubsではModelAdminがジェネリック扱いだが、実行時は非ジェネリックのため
    """
    事業者の停止・調査用

    事業情報の正は事業者ダッシュボードと Stripe。
    Adminから編集できるのは is_active（有効）のみ。
    無効化すると紐づくユーザーも停止し、ログインとダッシュボード API が使えなくなる。
    あわせて所属する配達者も無効化する。
    """

    list_display = [
        'company_name',
        'company_email',
        'subdomain',
        'is_active',
        'stripe_review_status',
        'stripe_charges_enabled',
        'deactivated_at',
        'created_at',
    ]
    search_fields = [
        'company_name',
        'company_email',
        'subdomain',
    ]
    list_filter = [
        'is_active',
        'stripe_review_status',
        'stripe_charges_enabled',
        'created_at',
        'deactivated_at',
    ]
    readonly_fields = [
        'company_name',
        'company_email',
        'tax_id',
        'business_type',
        'subdomain',
        'rep_last_name_kanji',
        'rep_first_name_kanji',
        'rep_last_name_kana',
        'rep_first_name_kana',
        'invoice_registration_number',
        'service_areas_display',
        'operating_hours_start',
        'operating_hours_end',
        'operating_days_display',
        'daily_max_luggage',
        'pricing_rules',
        'stripe_account_id',
        'stripe_review_status',
        'stripe_charges_enabled',
        'stripe_payouts_enabled',
        'stripe_details_submitted',
        'stripe_disabled_reason',
        'stripe_currently_due',
        'stripe_review_status_updated_at',
        'created_at',
        'updated_at',
        'deactivated_at',
    ]

    @admin.display(description='定休日')
    def operating_days_display(self, obj: BusinessProfile) -> str:
        return _format_operating_days(obj.operating_days)

    @admin.display(description='集荷地域')
    def service_areas_display(self, obj: BusinessProfile) -> str:
        return _format_service_areas(obj.service_areas)

    ordering = ['-created_at']

    fieldsets = (
        ('基本情報', {
            'fields': (
                'company_name',
                'company_email',
                'tax_id',
                'business_type',
                'subdomain',
                'rep_last_name_kanji',
                'rep_first_name_kanji',
                'rep_last_name_kana',
                'rep_first_name_kana',
                'invoice_registration_number',
            )
        }),
        ('サービス情報', {
            'fields': ('service_areas_display',),
        }),
        ('営業情報', {
            'fields': (
                'operating_hours_start',
                'operating_hours_end',
                'operating_days_display',
                'daily_max_luggage',
            ),
        }),
        ('料金設定', {
            'fields': ('pricing_rules',),
            'classes': ('collapse',),
        }),
        ('アカウント状態', {
            'fields': ('is_active', 'deactivated_at'),
        }),
        ('Stripe情報', {
            'fields': (
                'stripe_account_id',
                'stripe_review_status',
                'stripe_charges_enabled',
                'stripe_payouts_enabled',
                'stripe_details_submitted',
                'stripe_disabled_reason',
                'stripe_currently_due',
                'stripe_review_status_updated_at',
            ),
            'classes': ('collapse',),
        }),
        ('作成日時・更新日時', {
            'fields': ('created_at', 'updated_at'),
        }),
    )

    def has_add_permission(self, request: HttpRequest) -> bool:
        # User と紐づかない事業者を作れないよう、作成は登録フロー経由のみ
        return False

    def has_delete_permission(self, request: HttpRequest, obj=None) -> bool:
        # 予約・決済履歴が紐づくため削除不可。停止は is_active=False で行う
        return False


@admin.register(DriverProfile)
class DriverProfileAdmin(admin.ModelAdmin):  # type: ignore[type-arg]  # django-stubsではModelAdminがジェネリック扱いだが、実行時は非ジェネリックのため
    """配達者の閲覧と無効化用。編集できるのは is_active と is_available。"""

    list_display = [
        'driver_name',
        'business_owner',
        'license_expiry',
        'is_active',
        'is_available',
        'created_at',
    ]
    search_fields = [
        'user__email',
        'user__first_name',
        'user__last_name',
        'business_owner__company_name',
    ]
    list_filter = [
        'is_active',
        'is_available',
        'license_expiry',
    ]
    ordering = ['-created_at']

    readonly_fields = [
        'user',
        'business_owner',
        'company_name',
        'departure_address',
        'shift_start',
        'max_daily_stops',
        'max_daily_luggage_count',
        'operating_days_display',
        'license_expiry',
        'deactivated_at',
        'created_at',
        'updated_at',
    ]

    fieldsets = (
        ('基本情報', {
            'fields': ('user', 'business_owner', 'company_name'),
        }),
        ('稼働設定', {
            'fields': (
                'departure_address',
                'shift_start',
                'max_daily_stops',
                'max_daily_luggage_count',
                'operating_days_display',
            ),
        }),
        ('ステータス', {
            'fields': (
                'license_expiry',
                'is_active',
                'deactivated_at',
                'is_available',
            ),
        }),
        ('作成日時・更新日時', {
            'fields': ('created_at', 'updated_at'),
        }),
    )

    @admin.display(description='配達者名')
    def driver_name(self, obj: DriverProfile) -> str:
        return obj.user.get_full_name()

    @admin.display(description='定休日')
    def operating_days_display(self, obj: DriverProfile) -> str:
        return _format_operating_days(obj.operating_days)

    def get_queryset(self, request: HttpRequest) -> QuerySet[DriverProfile]:
        return super().get_queryset(request).select_related('user', 'business_owner')

    def has_add_permission(self, request: HttpRequest) -> bool:
        # 配達者の作成は事業者の招待フロー経由のみ
        return False

    def has_delete_permission(self, request: HttpRequest, obj=None) -> bool:
        # 配達実績が紐づくため削除不可。停止は is_active=False で行う
        return False


@admin.register(LuggageBooking)
class LuggageBookingAdmin(admin.ModelAdmin):  # type: ignore[type-arg]  # django-stubsではModelAdminがジェネリック扱いだが、実行時は非ジェネリックのため
    """
    問い合わせ調査用の予約検索。全フィールド閲覧のみ。

    予約・決済・返金の状態は Stripe / 事業者フロー / Webhook が正であり、
    Adminからの手動変更は整合性を壊すため一切行わない。
    """

    list_display = [
        'booking_number',
        'business_owner',
        'delivery_status',
        'refund_status',
        'pickup_date',
        'delivery_date',
        'total_amount_display',
        'created_at',
    ]
    search_fields = [
        'booking_number',
        'customer_name',
        'customer_email',
        'customer_phone_number',
        'payment_intent_id',
        'stripe_refund_id',
        'pickup_location_name',
        'pickup_location_address',
        'pickup_location_name_ja',
        'pickup_location_address_ja',
        'delivery_location_name',
        'delivery_location_address',
        'delivery_location_name_ja',
        'delivery_location_address_ja',
        'business_owner__company_name',
        'business_owner__company_email',
        'business_owner__subdomain',
        'driver__user__email',
        'driver__user__first_name',
        'driver__user__last_name',
        'driver__company_name',
        'pickup_driver__user__email',
        'pickup_driver__user__first_name',
        'pickup_driver__user__last_name',
        'pickup_driver__company_name',
    ]
    list_filter = [
        'delivery_status',
        'refund_status',
        'pickup_date',
        'delivery_date',
        'business_owner',
    ]
    ordering = ['-created_at']

    fieldsets = (
        ('基本情報', {
            'fields': (
                'id',
                'booking_number',
                'business_owner',
                'delivery_status',
                'driver',
                'pickup_driver',
                'delivery_manually_assigned',
                'pickup_manually_assigned',
            ),
        }),
        ('集荷情報', {
            'fields': (
                'pickup_location_name',
                'pickup_location_address',
                'pickup_location_name_ja',
                'pickup_location_address_ja',
                'pickup_place_id',
                'pickup_latitude',
                'pickup_longitude',
                'pickup_postal_code',
                'pickup_geocode_status',
                'pickup_date',
            ),
        }),
        ('配達情報', {
            'fields': (
                'delivery_location_name',
                'delivery_location_address',
                'delivery_location_name_ja',
                'delivery_location_address_ja',
                'delivery_place_id',
                'delivery_latitude',
                'delivery_longitude',
                'delivery_postal_code',
                'delivery_geocode_status',
                'delivery_date',
            ),
        }),
        ('荷物・備考', {
            'fields': (
                'luggage_items',
                'notes',
                'total_amount_display',
            ),
        }),
        ('顧客情報', {
            'fields': (
                'customer_name',
                'customer_email',
                'customer_phone_number',
                'customer_nationality',
                'guest_name',
                'customer_language',
            ),
        }),
        ('支払い情報', {
            'fields': ('payment_intent_id',),
        }),
        ('返金情報', {
            'fields': (
                'refund_status',
                'stripe_refund_id',
                'stripe_charge_id',
                'refunded_amount_display',
                'refunded_at',
                'refund_reconciled_at',
            ),
        }),
        ('配達実績', {
            'fields': (
                'picked_up_at',
                'facility_fee_display',
                'transport_cost_display',
                'delivered_at',
            ),
        }),
        ('送金情報', {
            'fields': (
                'stripe_transfer_id',
                'transferred_at',
                'transferred_gross_amount_display',
                'transferred_platform_fee_display',
                'funds_available_on',
            ),
            'classes': ('collapse',),
        }),
        ('発行者情報（スナップショット）', {
            'fields': (
                'issuer_name',
                'issuer_name_en',
                'issuer_address',
                'issuer_email',
                'issuer_phone',
                'issuer_invoice_number',
                'issuer_snapshot_at',
            ),
            'classes': ('collapse',),
        }),
        ('作成日時・更新日時', {
            'fields': ('created_at', 'updated_at'),
        }),
    )

    def get_readonly_fields(self, request: HttpRequest, obj=None) -> list[str]:
        # 全フィールドを閲覧のみとする
        return [
            field
            for _title, options in self.fieldsets
            for field in options['fields']
        ]

    @admin.display(description='合計金額')
    def total_amount_display(self, obj: LuggageBooking) -> str:
        return _format_yen(obj.total_amount)

    @admin.display(description='返金済み金額')
    def refunded_amount_display(self, obj: LuggageBooking) -> str:
        return _format_yen(obj.refunded_amount)

    @admin.display(description='施設側の手数料（円）')
    def facility_fee_display(self, obj: LuggageBooking) -> str:
        return _format_yen(obj.facility_fee)

    @admin.display(description='高速代などの交通費（円）')
    def transport_cost_display(self, obj: LuggageBooking) -> str:
        return _format_yen(obj.transport_cost)

    @admin.display(description='送金時の返金控除後売上額')
    def transferred_gross_amount_display(self, obj: LuggageBooking) -> str:
        return _format_yen(obj.transferred_gross_amount)

    @admin.display(description='送金時のプラットフォーム手数料')
    def transferred_platform_fee_display(self, obj: LuggageBooking) -> str:
        return _format_yen(obj.transferred_platform_fee)

    def get_queryset(self, request: HttpRequest) -> QuerySet[LuggageBooking]:
        return super().get_queryset(request).select_related(
            'business_owner', 'driver__user', 'pickup_driver__user'
        )

    def has_add_permission(self, request: HttpRequest) -> bool:
        # 決済と紐づかない予約を作れないよう、作成は予約フロー経由のみ
        return False

    def has_change_permission(self, request: HttpRequest, obj=None) -> bool:
        # 編集不可
        return False

    def has_delete_permission(self, request: HttpRequest, obj=None) -> bool:
        # 決済・監査の証跡のため削除不可。キャンセルは事業者・顧客フローで行う
        return False


@admin.register(PendingBooking)
class PendingBookingAdmin(admin.ModelAdmin):  # type: ignore[type-arg]  # django-stubsではModelAdminがジェネリック扱いだが、実行時は非ジェネリックのため
    """決済成功時に保存したフォールバック用データの確認用（閲覧のみ）"""

    exclude = ['total_amount']
    list_display = [
        'payment_intent_id',
        'business_owner',
        'total_amount_display',
        'consumed_at',
        'created_at',
    ]
    search_fields = [
        'payment_intent_id',
        'business_owner__company_name',
        'business_owner__company_email',
        'business_owner__subdomain',
    ]
    list_filter = [
        PendingBookingCreatedStatusFilter,
        'created_at',
    ]
    readonly_fields = [
        'payment_intent_id',
        'business_owner',
        'payload',
        'total_amount_display',
        'consumed_at',
        'created_at',
        'updated_at',
    ]
    ordering = ['-created_at']

    def get_queryset(self, request: HttpRequest) -> QuerySet[PendingBooking]:
        return super().get_queryset(request).select_related('business_owner')

    @admin.display(description='合計金額')
    def total_amount_display(self, obj: PendingBooking) -> str:
        return _format_yen(obj.total_amount)

    def has_add_permission(self, request: HttpRequest) -> bool:
        # 記録は決済フロー経由でのみ作成する
        return False

    def has_change_permission(self, request: HttpRequest, obj=None) -> bool:
        # 編集不可
        return False

    def has_delete_permission(self, request: HttpRequest, obj=None) -> bool:
        # Webhookフォールバックの元データのため、手動削除不可
        return False


@admin.register(PayoutDocumentDelivery)
class PayoutDocumentDeliveryAdmin(admin.ModelAdmin):  # type: ignore[type-arg]  # django-stubsではModelAdminがジェネリック扱いだが、実行時は非ジェネリックのため
    """請求書・支払明細の生成/送信状況の確認用（閲覧のみ）"""

    exclude = [
        'payout_amount',
        'gross_sales',
        'platform_fee',
        'matched_transfer_amount',
        'adjustment_amount',
    ]
    list_display = [
        'stripe_payout_id',
        'business_profile',
        'payout_amount_display',
        'sent_at',
        'send_attempts',
        'has_error',
        'payout_created_at',
    ]
    search_fields = [
        'stripe_payout_id',
        'business_profile__company_name',
        'business_profile__subdomain',
        'recipient_email',
    ]
    list_filter = [
        PayoutDocumentSentStatusFilter,
        'payout_created_at',
    ]
    readonly_fields = [
        'business_profile',
        'stripe_payout_id',
        'payout_created_at',
        'arrival_date',
        'payout_amount_display',
        'currency',
        'gross_sales_display',
        'platform_fee_display',
        'matched_transfer_amount_display',
        'adjustment_amount_display',
        'line_items',
        'issuer_name',
        'issuer_address',
        'issuer_registration_number',
        'recipient_email',
        'send_attempts',
        'processing_started_at',
        'sent_at',
        'last_err',
        'created_at',
        'updated_at',
    ]
    ordering = ['-payout_created_at']

    @admin.display(description='エラーあり', boolean=True)
    def has_error(self, obj: PayoutDocumentDelivery) -> bool:
        return bool(obj.last_err)

    @admin.display(description='入金額')
    def payout_amount_display(self, obj: PayoutDocumentDelivery) -> str:
        return _format_yen(obj.payout_amount)

    @admin.display(description='売上合計')
    def gross_sales_display(self, obj: PayoutDocumentDelivery) -> str:
        return _format_yen(obj.gross_sales)

    @admin.display(description='プラットフォーム手数料')
    def platform_fee_display(self, obj: PayoutDocumentDelivery) -> str:
        return _format_yen(obj.platform_fee)

    @admin.display(description='振込合計')
    def matched_transfer_amount_display(self, obj: PayoutDocumentDelivery) -> str:
        return _format_yen(obj.matched_transfer_amount)

    @admin.display(description='差額')
    def adjustment_amount_display(self, obj: PayoutDocumentDelivery) -> str:
        return _format_yen(obj.adjustment_amount)

    def get_queryset(self, request: HttpRequest) -> QuerySet[PayoutDocumentDelivery]:
        return super().get_queryset(request).select_related('business_profile')

    def has_add_permission(self, request: HttpRequest) -> bool:
        # 記録は Payout 処理経由でのみ作成する
        return False

    def has_change_permission(self, request: HttpRequest, obj=None) -> bool:
        # 編集不可
        return False

    def has_delete_permission(self, request: HttpRequest, obj=None) -> bool:
        # 送信記録のため、手動削除不可
        return False

@admin.register(DailyAssignmentRun)
class DailyAssignmentRunAdmin(admin.ModelAdmin):  # type: ignore[type-arg]  # django-stubsではModelAdminがジェネリック扱いだが、実行時は非ジェネリックのため
    """日次自動割当の失敗・スタック調査用（閲覧のみ）"""

    list_display = [
        'business_owner',
        'service_date',
        'status',
        'created_at',
        'started_at',
        'completed_at',
        'applied_at',
    ]
    search_fields = [
        'business_owner__company_name',
        'business_owner__company_email',
        'business_owner__subdomain',
    ]
    list_filter = [
        'status',
        'service_date',
    ]
    readonly_fields = [
        'id',
        'business_owner',
        'service_date',
        'status',
        'input_hash',
        'selected_driver_ids',
        'unassigned_tasks',
        'created_at',
        'started_at',
        'completed_at',
        'applied_at',
    ]
    ordering = ['-created_at']

    def get_queryset(self, request: HttpRequest) -> QuerySet[DailyAssignmentRun]:
        return super().get_queryset(request).select_related('business_owner')

    def has_add_permission(self, request: HttpRequest) -> bool:
        # 実行記録は自動割当タスク経由でのみ作成する
        return False

    def has_change_permission(self, request: HttpRequest, obj=None) -> bool:
        # 編集不可
        return False

    def has_delete_permission(self, request: HttpRequest, obj=None) -> bool:
        # 割当履歴のため、手動削除不可
        return False


@admin.register(ChargeDispute)
class ChargeDisputeAdmin(admin.ModelAdmin):  # type: ignore[type-arg]  # django-stubsではModelAdminがジェネリック扱いだが、実行時は非ジェネリックのため
    list_display = [
        'dispute_id',
        'status',
        'reason',
        'amount_display',
        'currency',
        'booking',
        'business_owner',
        'opened_at',
        'closed_at',
        'created_at',
    ]
    search_fields = [
        'dispute_id',
        'charge_id',
        'payment_intent_id',
        'business_owner__company_name',
        'business_owner__company_email',
        'business_owner__subdomain',
    ]
    list_filter = [
        'status',
        'reason',
        'is_charge_refundable',
        'created_at',
        'closed_at',
    ]
    exclude = ['amount']
    readonly_fields = [
        'dispute_id',
        'charge_id',
        'payment_intent_id',
        'booking',
        'business_owner',
        'amount_display',
        'currency',
        'reason',
        'status',
        'is_charge_refundable',
        'evidence_due_by',
        'opened_at',
        'closed_at',
        'created_at',
        'updated_at',
    ]
    ordering = ['-created_at']

    @admin.display(description='異議申立て金額')
    def amount_display(self, obj: ChargeDispute) -> str:
        return _format_yen(obj.amount)

    def get_queryset(self, request: HttpRequest) -> QuerySet[ChargeDispute]:
        return super().get_queryset(request).select_related('booking', 'business_owner')

    def has_add_permission(self, request: HttpRequest) -> bool:
        # 記録は Stripe Webhook 経由でのみ作成する
        return False

    def has_change_permission(self, request: HttpRequest, obj=None) -> bool:
        # 編集不可
        return False

    def has_delete_permission(self, request: HttpRequest, obj=None) -> bool:
        # Stripe Webhook で作られた記録なので、手動削除不可
        return False


@admin.register(BookingAuditLog)
class BookingAuditLogAdmin(admin.ModelAdmin):  # type: ignore[type-arg]  # django-stubsではModelAdminがジェネリック扱いだが、実行時は非ジェネリックのため
    exclude = ['amount']
    list_display = [
        'created_at',
        'action',
        'source',
        'booking',
        'previous_delivery_status',
        'new_delivery_status',
        'previous_refund_status',
        'new_refund_status',
        'amount_display',
    ]
    search_fields = [
        'payment_intent_id',
        'stripe_refund_id',
        'stripe_charge_id',
        'stripe_event_id',
        'booking__booking_number',
    ]
    list_filter = [
        'action',
        'source',
        'new_delivery_status',
        'new_refund_status',
        'created_at',
    ]
    readonly_fields = [
        'booking',
        'payment_intent_id',
        'action',
        'source',
        'previous_delivery_status',
        'new_delivery_status',
        'previous_refund_status',
        'new_refund_status',
        'stripe_event_id',
        'stripe_refund_id',
        'stripe_charge_id',
        'amount_display',
        'message',
        'metadata',
        'created_at',
    ]
    ordering = ['-created_at']

    def get_queryset(self, request: HttpRequest) -> QuerySet[BookingAuditLog]:
        return super().get_queryset(request).select_related('booking')

    @admin.display(description='金額')
    def amount_display(self, obj: BookingAuditLog) -> str:
        return _format_yen(obj.amount)

    def has_add_permission(self, request: HttpRequest) -> bool:
        # 監査ログは処理経路からのみ作成する
        return False

    def has_change_permission(self, request: HttpRequest, obj=None) -> bool:
        # 編集不可
        return False

    def has_delete_permission(self, request: HttpRequest, obj=None) -> bool:
        # 監査証跡のため、削除不可
        return False


@admin.register(StripeWebhookEvent)
class StripeWebhookEventAdmin(admin.ModelAdmin):  # type: ignore[type-arg]  # django-stubsではModelAdminがジェネリック扱いだが、実行時は非ジェネリックのため
    list_display = [
        'event_id',
        'event_type',
        'received_at',
        'processed_at',
    ]
    search_fields = [
        'event_id',
        'event_type',
    ]
    list_filter = [
        'event_type',
        'received_at',
        'processed_at',
    ]
    readonly_fields = [
        'event_id',
        'event_type',
        'received_at',
        'processed_at',
    ]
    ordering = ['-received_at']

    def has_add_permission(self, request: HttpRequest) -> bool:
        # 記録は Stripe Webhook 経由でのみ作成する
        return False

    def has_change_permission(self, request: HttpRequest, obj=None) -> bool:
        # 編集不可
        return False

    def has_delete_permission(self, request: HttpRequest, obj=None) -> bool:
        # Webhook 処理履歴のため、手動削除不可
        return False


admin.site.site_header = 'LugGo 管理画面'
admin.site.site_title = 'LugGo(ラグゴー) | アプリ管理者用ページ'
admin.site.index_title = '運営コンソール'
