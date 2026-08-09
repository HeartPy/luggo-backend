"""
日次自動割当の Celery ジョブ

API（views.assign）からキュー投入され、ワーカー側で担当割当を計算して
下書き結果（draft）を保存するまでを担う。
予約への反映はこのファイルでは行わず、views.apply_run が担当する。
"""
import logging

from celery import shared_task
from django.db import transaction
from django.utils import timezone

from .models import DailyAssignmentRun, DailyTaskAssignment
from .services.assignment import assign_tasks
from .services.snapshots import snapshot_hash
from .services.task_builder import build_tasks


logger = logging.getLogger(__name__)


@shared_task
def assign_daily_tasks(run_id: str) -> None:
    run = DailyAssignmentRun.objects.select_related('business_owner').get(id=run_id)
    run.status = DailyAssignmentRun.Status.RUNNING
    run.started_at = timezone.now()
    run.save(update_fields=['status', 'started_at'])
    try:
        problem = build_tasks(
            run.business_owner,
            run.service_date,
            set(run.selected_driver_ids) or None,
        )
        if snapshot_hash(problem.snapshot()) != run.input_hash:
            raise RuntimeError('キュー投入後に割当対象の入力データが変更されました')
        result = assign_tasks(problem.drivers, problem.tasks)

        with transaction.atomic():
            locked = DailyAssignmentRun.objects.select_for_update().get(id=run.id)
            locked.assignments.all().delete()
            DailyTaskAssignment.objects.bulk_create([
                DailyTaskAssignment(
                    run=locked,
                    booking_id=item.task.booking_id,
                    driver_id=item.driver.id,
                    kind=item.task.kind,
                    manually_assigned=bool(item.task.fixed_driver_id),
                )
                for item in result.assignments
            ])
            locked.status = DailyAssignmentRun.Status.DRAFT
            locked.unassigned_tasks = problem.excluded + result.unassigned
            locked.completed_at = timezone.now()
            locked.save(update_fields=[
                'status', 'unassigned_tasks', 'completed_at',
            ])
    except Exception:
        logger.exception('日次自動割当に失敗しました: run_id=%s', run_id)
        DailyAssignmentRun.objects.filter(id=run.id).update(
            status=DailyAssignmentRun.Status.FAILED,
            completed_at=timezone.now(),
        )
        raise
