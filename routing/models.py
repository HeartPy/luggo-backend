import uuid

from django.db import models
from django.db.models import Q


class DailyAssignmentRun(models.Model):
    """日次自動割当の実行記録"""

    class Status(models.TextChoices):
        QUEUED = 'queued', '実行待ち'
        RUNNING = 'running', '自動割当中'
        DRAFT = 'draft', '提案を確認中'
        APPLIED = 'applied', '適用済み'
        STALE = 'stale', '再割当が必要'
        FAILED = 'failed', '失敗'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business_owner = models.ForeignKey(
        'business_owners.BusinessProfile',
        on_delete=models.CASCADE,
        related_name='assignment_runs',
    )
    service_date = models.DateField(db_index=True, verbose_name='割当対象日')
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.QUEUED,
        db_index=True,
        verbose_name='割当状態',
    )
    input_hash = models.CharField(max_length=64)
    selected_driver_ids = models.JSONField(default=list, blank=True)
    unassigned_tasks = models.JSONField(
        default=list, blank=True, verbose_name='未割当の集荷・配達',
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='作成日')
    started_at = models.DateTimeField(
        null=True, blank=True, verbose_name='開始日時',
    )
    completed_at = models.DateTimeField(
        null=True, blank=True, verbose_name='完了日時',
    )
    applied_at = models.DateTimeField(
        null=True, blank=True, verbose_name='適用日時',
    )

    class Meta:
        db_table = 'daily_assignment_runs'
        ordering = ['-created_at']
        verbose_name = '日次自動割当の実行記録'
        verbose_name_plural = '日次自動割当の実行記録'
        constraints = [
            models.UniqueConstraint(
                fields=['business_owner', 'service_date'],
                condition=Q(status__in=['queued', 'running']),
                name='uniq_active_routing_run_per_owner_date',
            )
        ]


class DailyTaskAssignment(models.Model):
    """集荷・配達ごとの担当割当"""

    run = models.ForeignKey(
        DailyAssignmentRun, on_delete=models.CASCADE, related_name='assignments'
    )
    booking = models.ForeignKey(
        'bookings.LuggageBooking',
        on_delete=models.PROTECT,
        related_name='daily_task_assignments',
    )
    driver = models.ForeignKey(
        'drivers.DriverProfile',
        on_delete=models.PROTECT,
        related_name='daily_task_assignments',
    )
    kind = models.CharField(
        max_length=10,
        choices=(
            ('pickup', '集荷'),
            ('delivery', '配達'),
        ),
        verbose_name='集荷・配達の種別',
    )
    manually_assigned = models.BooleanField(default=False, verbose_name='手動割当')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='作成日')

    class Meta:
        db_table = 'daily_task_assignments'
        ordering = ['driver_id', 'booking_id', 'kind']
        verbose_name = '集荷・配達ごとの担当割当'
        verbose_name_plural = '集荷・配達ごとの担当割当'
        constraints = [
            models.UniqueConstraint(
                fields=['run', 'booking', 'kind'],
                name='uniq_task_assignment_per_run',
            )
        ]


class DriverDailyRoute(models.Model):
    """配達者別の日次ルート（将来の訪問順生成用）"""

    run = models.ForeignKey(
        DailyAssignmentRun, on_delete=models.CASCADE, related_name='routes'
    )
    driver = models.ForeignKey(
        'drivers.DriverProfile', on_delete=models.PROTECT, related_name='daily_routes'
    )
    total_duration_seconds = models.PositiveIntegerField(
        default=0, verbose_name='合計所要時間（秒）',
    )
    total_distance_meters = models.PositiveIntegerField(
        default=0, verbose_name='合計距離（メートル）',
    )
    stop_count = models.PositiveSmallIntegerField(default=0, verbose_name='訪問先数')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='作成日')

    class Meta:
        db_table = 'driver_daily_routes'
        verbose_name = '配達者日次ルート'
        verbose_name_plural = '配達者日次ルート'
        constraints = [
            models.UniqueConstraint(
                fields=['run', 'driver'], name='uniq_driver_route_per_run'
            )
        ]


class RouteStop(models.Model):
    """ルート上の訪問先（将来の訪問順生成用）"""

    class Kind(models.TextChoices):
        PICKUP = 'pickup', '集荷'
        DELIVERY = 'delivery', '配達'

    route = models.ForeignKey(
        DriverDailyRoute, on_delete=models.CASCADE, related_name='stops'
    )
    booking = models.ForeignKey(
        'bookings.LuggageBooking', on_delete=models.PROTECT, related_name='route_stops'
    )
    kind = models.CharField(max_length=10, choices=Kind.choices, verbose_name='集荷・配達の種別')
    sequence = models.PositiveSmallIntegerField(verbose_name='訪問順')
    latitude = models.DecimalField(max_digits=9, decimal_places=6)
    longitude = models.DecimalField(max_digits=9, decimal_places=6)
    location_name = models.CharField(
        max_length=200, blank=True, default='', verbose_name='地点名',
    )
    location_address = models.TextField(blank=True, default='', verbose_name='住所')
    planned_arrival_at = models.DateTimeField(
        null=True, blank=True, verbose_name='到着予定日時',
    )
    duration_from_previous_seconds = models.PositiveIntegerField(
        default=0, verbose_name='前地点からの所要時間（秒）',
    )
    distance_from_previous_meters = models.PositiveIntegerField(
        default=0, verbose_name='前地点からの距離（メートル）',
    )
    manually_assigned = models.BooleanField(default=False, verbose_name='手動割当')

    class Meta:
        db_table = 'route_stops'
        ordering = ['sequence']
        verbose_name = 'ルート訪問先'
        verbose_name_plural = 'ルート訪問先'
        constraints = [
            models.UniqueConstraint(
                fields=['route', 'sequence'], name='uniq_stop_sequence_per_route'
            ),
            models.UniqueConstraint(
                fields=['route', 'booking', 'kind'], name='uniq_task_per_route'
            ),
        ]
