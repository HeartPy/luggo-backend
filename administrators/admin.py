from django.contrib import admin
from django.http import HttpRequest
from django.db.models import QuerySet
from django.utils import timezone
from business_owners.models import BusinessProfile
from bookings.models import ChargeDispute


@admin.register(BusinessProfile)
class BusinessProfileAdmin(admin.ModelAdmin):  # type: ignore[type-arg]  # django-stubsではModelAdminがジェネリック扱いだが、実行時は非ジェネリックのため
    list_display = [
        'company_name',
        'company_email',
        'is_approved',
        'is_active',
        'deactivated_at',
        'created_at',
    ]
    search_fields = [
        'company_name',
        'company_email',
    ]
    list_filter = [
        'is_approved',
        'is_active',
        'created_at',
        'updated_at',
        'deactivated_at',
    ]
    readonly_fields = [
        'service_areas',
        'operating_hours_start',
        'operating_hours_end',
        'operating_days',
        'daily_max_luggage',
        'pricing_rules',
        'total_orders_completed',
        'total_revenue',
        'is_approved',
        'approval_date',
        'stripe_account_id',
        'created_at',
        'updated_at',
        'deactivated_at',
    ]

    def save_model(self, request: HttpRequest, obj: BusinessProfile, form, change: bool) -> None:
        """モデル保存時にis_activeに応じてdeactivated_atを自動設定"""
        if change:
            original = BusinessProfile.objects.get(pk=obj.pk)

            if original.is_active and not obj.is_active:
                obj.deactivated_at = timezone.now()

            elif not original.is_active and obj.is_active:
                obj.deactivated_at = None

        super().save_model(request, obj, form, change)

    ordering = ['-created_at']

    fieldsets = (
        ('基本情報', {
            'fields': ('company_name', 'company_email', 'tax_id')
        }),
        ('サービス情報', {
            'fields': ('service_areas',),
        }),
        ('営業情報', {
            'fields': (
                'operating_hours_start',
                'operating_hours_end',
                'operating_days',
                'daily_max_luggage',
            ),
        }),
        ('料金設定', {
            'fields': ('pricing_rules',),
            'classes': ('collapse',),
        }),
        ('実績', {
            'fields': ('total_orders_completed', 'total_revenue'),
        }),
        ('アカウント状態', {
            'fields': ('is_approved', 'approval_date', 'is_active', 'deactivated_at'),
        }),
        ('Stripe情報', {
            'fields': ('stripe_account_id',),
            'classes': ('collapse',),
        }),
        ('作成日・更新日', {
            'fields': ('created_at', 'updated_at'),
        }),
    )

    def get_queryset(self, request: HttpRequest) -> QuerySet[BusinessProfile]:
        return super().get_queryset(request).select_related('user')


@admin.register(ChargeDispute)
class ChargeDisputeAdmin(admin.ModelAdmin):  # type: ignore[type-arg]  # django-stubsではModelAdminがジェネリック扱いだが、実行時は非ジェネリックのため
    list_display = [
        'dispute_id',
        'status',
        'reason',
        'amount',
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
    ]
    list_filter = [
        'status',
        'reason',
        'is_charge_refundable',
        'created_at',
        'closed_at',
    ]
    readonly_fields = [
        'dispute_id',
        'charge_id',
        'payment_intent_id',
        'booking',
        'business_owner',
        'amount',
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

    def has_add_permission(self, request: HttpRequest) -> bool:
        # 記録は Stripe Webhook 経由でのみ作成する
        return False

    def has_delete_permission(self, request: HttpRequest, obj=None) -> bool:
        # Stripe Webhook で作られた記録なので、手動削除はさせない
        return False


admin.site.site_header = 'LugGo 管理画面'
admin.site.site_title = 'LugGo(ラグゴー) | アプリ管理者用ページ'
admin.site.index_title = '事業者管理'
