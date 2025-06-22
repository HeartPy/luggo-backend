from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models
from django.core.validators import RegexValidator
import uuid

class UserManager(BaseUserManager):
    """カスタムUserManager - emailベースの認証用"""

    def create_user(self, email, password=None, **extra_fields):
        """通常ユーザーの作成"""
        if not email:
            raise ValueError('メールアドレスは必須です')

        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        """スーパーユーザーの作成"""
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        extra_fields.setdefault('is_active', True)

        if extra_fields.get('is_staff') is not True:
            raise ValueError('スーパーユーザーはis_staff=Trueである必要があります')
        if extra_fields.get('is_superuser') is not True:
            raise ValueError('スーパーユーザーはis_superuser=Trueである必要があります')

        return self.create_user(email, password, **extra_fields)

class User(AbstractUser):
    """
    Luggo統合ユーザーモデル
    旅行者、事業者、配達者、管理者を統合管理
    """

    # カスタムマネージャーを指定
    objects = UserManager()

    # usernameフィールドを無効化
    username = None

    # 基本認証情報
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(unique=True)
    phone_number = models.CharField(
        max_length=15,
        blank=True,
        validators=[RegexValidator(r'^\+?1?\d{9,15}$', '有効な電話番号が必要です。')]
    )

    # ユーザータイプ
    USER_TYPE_CHOICES = [
        ('customer', '旅行者'),
        ('business_owner', '事業者'),
        ('delivery_driver', '配達者'),
        ('admin', '管理者'),
    ]
    user_type = models.CharField(
        max_length=20,
        choices=USER_TYPE_CHOICES,
        default='customer'
    )

    # 共通プロフィール情報
    profile_picture = models.ImageField(upload_to='profile_pics/', blank=True, null=True)
    date_of_birth = models.DateField(null=True, blank=True)

    # 位置情報
    country = models.CharField(max_length=50, blank=True, default='Japan')
    city = models.CharField(max_length=50, blank=True)
    address = models.TextField(blank=True)
    postal_code = models.CharField(max_length=10, blank=True)

    # アカウント管理
    is_verified = models.BooleanField(default=False)
    verification_code = models.CharField(max_length=6, blank=True)
    last_active = models.DateTimeField(auto_now=True)

    # 通知設定
    email_notifications = models.BooleanField(default=True)
    sms_notifications = models.BooleanField(default=False)

    # システム管理
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['user_type']

    class Meta:
        db_table = 'users'
        verbose_name = 'ユーザー'
        verbose_name_plural = 'ユーザー'
        indexes = [
            models.Index(fields=['user_type']),
            models.Index(fields=['email']),
            models.Index(fields=['created_at']),
        ]

    def __str__(self):
        return f"{self.email} ({self.get_user_type_display()})"

    # ユーザータイプ別判定メソッド
    def is_customer(self):
        return self.user_type == 'customer'

    def is_business_owner(self):
        return self.user_type == 'business_owner'

    def is_delivery_driver(self):
        return self.user_type == 'delivery_driver'

    def is_admin_user(self):
        return self.user_type == 'admin'

    def get_dashboard_url(self):
        """ユーザータイプ別のダッシュボードURL"""
        urls = {
            'customer': '/dashboard/customer/',
            'business_owner': '/dashboard/business/',
            'delivery_driver': '/dashboard/driver/',
            'admin': '/admin/',
        }
        return urls.get(self.user_type, '/dashboard/')

    def get_full_name(self):
        """フルネームを返す"""
        return f"{self.first_name} {self.last_name}".strip() or self.email


class CustomerProfile(models.Model):
    """旅行者向け拡張プロフィール"""
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='customer_profile')

    # 旅行情報
    preferred_luggage_type = models.CharField(
        max_length=20,
        choices=[
            ('stroller', 'ベビーカー'),
            ('cardboard', 'ダンボール'),
            ('suitcase', 'スーツケース'),
            ('other', 'その他'),
        ],
        default='suitcase'
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'customer_profiles'
        verbose_name = '顧客プロフィール'
        verbose_name_plural = '顧客プロフィール'

    def __str__(self):
        return f"Customer: {self.user.email}"


class BusinessProfile(models.Model):
    """事業者向け拡張プロフィール"""
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='business_profile')

    # 事業者情報
    company_name = models.CharField(max_length=200)
    business_license = models.CharField(max_length=50, unique=True, blank=True)
    tax_number = models.CharField(max_length=20, blank=True)

    # サービス情報
    service_areas = models.JSONField(default=list, blank=True)  # ['tokyo', 'osaka']
    max_luggage_capacity = models.PositiveIntegerField(default=10)

    # 営業情報
    operating_hours_start = models.TimeField(default='08:00')
    operating_hours_end = models.TimeField(default='20:00')
    operating_days = models.CharField(max_length=7, default='1111111')  # 月〜日

    # 料金設定
    base_rate = models.DecimalField(max_digits=8, decimal_places=2, default=1000)
    per_km_rate = models.DecimalField(max_digits=6, decimal_places=2, default=100)

    # 実績
    total_orders_completed = models.PositiveIntegerField(default=0)
    total_revenue = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    # アカウント状態
    is_approved = models.BooleanField(default=False)
    approval_date = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'business_profiles'
        verbose_name = '事業者プロフィール'
        verbose_name_plural = '事業者プロフィール'

    def __str__(self):
        return f"Business: {self.company_name}"

    def is_operating_now(self):
        """現在営業中かどうか判定"""
        from django.utils import timezone
        now = timezone.now()
        current_time = now.time()
        current_day = str(now.weekday())  # 0=月曜日

        return (
            self.operating_days[int(current_day)] == '1' and
            self.operating_hours_start <= current_time <= self.operating_hours_end
        )


class DriverProfile(models.Model):
    """配達者向け拡張プロフィール"""
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='driver_profile')
    business = models.ForeignKey(BusinessProfile, on_delete=models.CASCADE, related_name='drivers')

    # 配達者情報
    driver_license = models.CharField(max_length=20, unique=True)
    license_expiry = models.DateField()
    vehicle_plate = models.CharField(max_length=10)

    # 稼働状況
    is_available = models.BooleanField(default=True)
    last_location_update = models.DateTimeField(null=True, blank=True)

    # 実績
    total_deliveries = models.PositiveIntegerField(default=0)
    total_earnings = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'driver_profiles'
        verbose_name = '配達者プロフィール'
        verbose_name_plural = '配達者プロフィール'

    def __str__(self):
        return f"Driver: {self.user.email} ({self.business.company_name})"

    def update_location(self, lat, lng):
        """位置情報更新"""
        from django.utils import timezone
        self.last_location_update = timezone.now()
        self.save()

    def complete_delivery(self, earnings):
        """配達完了時の統計更新"""
        self.total_deliveries += 1
        self.total_earnings += earnings
        self.save()
