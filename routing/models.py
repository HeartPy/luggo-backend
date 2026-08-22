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
        verbose_name='事業者',
    )
    service_date = models.DateField(db_index=True, verbose_name='割当対象日')
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.QUEUED,
        db_index=True,
        verbose_name='割当状態',
    )
    input_hash = models.CharField(max_length=64, verbose_name='入力ハッシュ')
    selected_driver_ids = models.JSONField(
        default=list, blank=True, verbose_name='選択した配達者ID',
    )
    unassigned_tasks = models.JSONField(
        default=list, blank=True, verbose_name='未割当の集荷・配達',
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='作成日時')
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

    def __str__(self) -> str:
        company = self.business_owner.company_name if self.business_owner_id else '事業者未設定'
        return (
            f'{company} / {self.service_date} / '
            f'{self.get_status_display()} / {self.id}'
        )


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
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='作成日時')

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


class DriverRouteResult(models.Model):
    """
    配達者用ダッシュボードの最適化ルート保存結果

    担当タスクのスナップショットハッシュ（input_hash）が一致する限り
    保存済みルートを再利用する。完了・キャンセルなどでタスクが減った
    場合は停留所を間引いて順序を維持し、新規割当や座標変更のときだけ
    Google Routes API を呼び直す。
    """

    driver = models.ForeignKey(
        'drivers.DriverProfile',
        on_delete=models.CASCADE,
        related_name='route_results',
    )
    service_date = models.DateField(db_index=True, verbose_name='対象日')
    input_hash = models.CharField(max_length=64, verbose_name='入力ハッシュ')
    route = models.JSONField(default=dict, verbose_name='生成済みルート')
    generated_at = models.DateTimeField(auto_now=True, verbose_name='生成日時')

    class Meta:
        db_table = 'driver_route_results'
        verbose_name = '配達者ルート保存結果'
        verbose_name_plural = '配達者ルート保存結果'
        constraints = [
            models.UniqueConstraint(
                fields=['driver', 'service_date'],
                name='uniq_route_result_per_driver_date',
            )
        ]
