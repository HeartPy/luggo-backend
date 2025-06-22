from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.forms import UserChangeForm, UserCreationForm
from django.utils.html import format_html
from .models import User, CustomerProfile, BusinessProfile, DriverProfile


class CustomUserCreationForm(UserCreationForm):
    class Meta(UserCreationForm.Meta):
        model = User
        fields = ('email', 'user_type', 'first_name', 'last_name')


class CustomUserChangeForm(UserChangeForm):

    class Meta(UserChangeForm.Meta):
        model = User
        fields = '__all__'


class CustomerProfileInline(admin.StackedInline):
    model = CustomerProfile
    can_delete = False
    verbose_name_plural = '旅行者プロフィール'

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('user')


class BusinessProfileInline(admin.StackedInline):
    model = BusinessProfile
    can_delete = False
    verbose_name_plural = '事業者プロフィール'

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('user')


class DriverProfileInline(admin.StackedInline):
    model = DriverProfile
    can_delete = False
    verbose_name_plural = '配達者プロフィール'

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('user', 'business')


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    form = CustomUserChangeForm
    add_form = CustomUserCreationForm

    list_display = (
        'email', 'get_full_name', 'user_type', 'is_verified',
        'is_active', 'is_staff', 'created_at'
    )
    list_filter = (
        'user_type', 'is_verified', 'is_active', 'is_staff',
        'email_notifications', 'created_at'
    )
    search_fields = ('email', 'first_name', 'last_name', 'phone_number')
    ordering = ('-created_at',)

    fieldsets = (
        ('認証情報', {
            'fields': ('email', 'password')
        }),
        ('基本情報', {
            'fields': ('user_type', 'first_name', 'last_name', 'phone_number', 'profile_picture')
        }),
        ('位置情報', {
            'fields': ('country', 'city', 'address', 'postal_code'),
            'classes': ('collapse',)
        }),
        ('アカウント管理', {
            'fields': ('is_verified', 'verification_code', 'is_active'),
        }),
        ('通知設定', {
            'fields': ('email_notifications', 'sms_notifications'),
            'classes': ('collapse',)
        }),
        ('権限', {
            'fields': ('is_staff', 'is_superuser', 'groups', 'user_permissions'),
            'classes': ('collapse',)
        }),
        ('重要日付', {
            'fields': ('last_login', 'date_joined', 'last_active'),
            'classes': ('collapse',)
        }),
    )

    add_fieldsets = (
        ('アカウント作成', {
            'classes': ('wide',),
            'fields': ('email', 'user_type', 'first_name', 'last_name', 'password1', 'password2'),
        }),
    )

    readonly_fields = ('last_login', 'date_joined', 'last_active', 'created_at', 'updated_at')

    def get_full_name(self, obj):
        return obj.get_full_name()
    get_full_name.short_description = 'フルネーム'

    def get_inlines(self, request, obj):
        """ユーザータイプに応じてインラインを動的に表示"""
        if obj:
            if obj.is_customer():
                return [CustomerProfileInline]
            elif obj.is_business_owner():
                return [BusinessProfileInline]
            elif obj.is_delivery_driver():
                return [DriverProfileInline]
        return []


@admin.register(CustomerProfile)
class CustomerProfileAdmin(admin.ModelAdmin):
    list_display = (
        'user_email', 'preferred_luggage_type', 'created_at'
    )
    list_filter = ('preferred_luggage_type', 'created_at')
    search_fields = ('user__email', 'user__first_name', 'user__last_name')
    readonly_fields = ('created_at', 'updated_at')

    fieldsets = (
        ('基本情報', {
            'fields': ('user',)
        }),
        ('旅行設定', {
            'fields': ('preferred_luggage_type',)
        }),
    )

    def user_email(self, obj):
        return obj.user.email
    user_email.short_description = 'メールアドレス'


@admin.register(BusinessProfile)
class BusinessProfileAdmin(admin.ModelAdmin):
    list_display = (
        'company_name', 'user_email', 'is_approved', 'is_operating_now',
        'total_orders_completed', 'created_at'
    )
    list_filter = ('is_approved', 'created_at')
    search_fields = ('company_name', 'user__email', 'business_license')
    readonly_fields = ('created_at', 'updated_at', 'approval_date')

    fieldsets = (
        ('基本情報', {
            'fields': ('user', 'company_name')
        }),
        ('事業者登録情報', {
            'fields': ('business_license', 'tax_number', 'is_approved', 'approval_date')
        }),
        ('サービス情報', {
            'fields': ('service_areas', 'max_luggage_capacity')
        }),
        ('営業情報', {
            'fields': ('operating_hours_start', 'operating_hours_end', 'operating_days')
        }),
        ('料金設定', {
            'fields': ('base_rate', 'per_km_rate')
        }),
    )

    actions = ['approve_businesses', 'disapprove_businesses']

    def user_email(self, obj):
        return obj.user.email
    user_email.short_description = 'メールアドレス'

    def is_operating_now(self, obj):
        operating = obj.is_operating_now()
        return format_html(
            '<span style="color: {};">{}</span>',
            '#28a745' if operating else '#dc3545',
            '営業中' if operating else '営業時間外'
        )
    is_operating_now.short_description = '営業状況'

    def approve_businesses(self, request, queryset):
        """事業者を承認"""
        from django.utils import timezone
        updated = queryset.update(is_approved=True, approval_date=timezone.now())
        self.message_user(request, f'{updated}件の事業者を承認しました。')
    approve_businesses.short_description = '選択した事業者を承認'

    def disapprove_businesses(self, request, queryset):
        """事業者の承認を取り消し"""
        updated = queryset.update(is_approved=False, approval_date=None)
        self.message_user(request, f'{updated}件の事業者の承認を取り消しました。')
    disapprove_businesses.short_description = '選択した事業者の承認を取り消し'


@admin.register(DriverProfile)
class DriverProfileAdmin(admin.ModelAdmin):
    list_display = (
        'user_email', 'business_name', 'is_available',
        'total_deliveries', 'created_at'
    )
    list_filter = ('is_available', 'created_at')
    search_fields = (
        'user__email', 'user__first_name', 'user__last_name',
        'driver_license', 'vehicle_plate', 'business__company_name'
    )
    readonly_fields = ('created_at', 'updated_at', 'last_location_update')

    fieldsets = (
        ('基本情報', {
            'fields': ('user', 'business')
        }),
        ('配達者情報', {
            'fields': ('driver_license', 'license_expiry', 'vehicle_plate')
        }),
        ('稼働状況', {
            'fields': ('is_available', 'current_location_lat', 'current_location_lng', 'last_location_update')
        }),
    )

    def user_email(self, obj):
        return obj.user.email
    user_email.short_description = 'メールアドレス'

    def business_name(self, obj):
        return obj.business.company_name
    business_name.short_description = '所属事業者'

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('user', 'business')


admin.site.site_header = 'Luggo 管理画面'
admin.site.site_title = 'Luggo Admin'
admin.site.index_title = 'Luggo サービス管理'
