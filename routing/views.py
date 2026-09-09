from datetime import date, timedelta
from typing import Any, Optional
import logging

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q, QuerySet
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response

from bookings.models import BookingAuditLog, LuggageBooking
from business_owners.models import BusinessProfile
from drivers.models import DriverProfile

from .models import DailyAssignmentRun, DailyTaskAssignment
from .services.snapshots import snapshot_hash
from .services.task_builder import AssignmentInput, build_tasks, eligible_drivers
from .tasks import assign_daily_tasks


logger = logging.getLogger(__name__)


def _get_business_profile(request: Request) -> Optional[BusinessProfile]:
    """ログイン中の有効な事業者プロフィールを返す"""
    profile = getattr(request.user, 'business_profile', None)
    if profile is None or not profile.is_active:
        return None
    return profile


def _expire_run_if_timed_out(run: DailyAssignmentRun) -> DailyAssignmentRun:
    """
    タイムアウトした queued / running のランを自動キャンセル

    ワーカー停止などでランが進行中のまま残ると、ポーリングが終わらず
    同日付の再実行も永久にブロックされるため、一定時間で自動的に解消する。
    """
    if run.status not in (
        DailyAssignmentRun.Status.QUEUED,
        DailyAssignmentRun.Status.RUNNING,
    ):
        return run

    timeout = timedelta(
        seconds=int(getattr(settings, 'ROUTING_RUN_TIMEOUT_SECONDS', 300))
    )
    if timezone.now() < run.created_at + timeout:
        return run

    # 完了処理と競合しても terminal 状態を上書きしないよう、条件付き更新にする
    DailyAssignmentRun.objects.filter(
        id=run.id,
        status__in=[
            DailyAssignmentRun.Status.QUEUED,
            DailyAssignmentRun.Status.RUNNING,
        ],
    ).update(
        status=DailyAssignmentRun.Status.CANCELLED,
        completed_at=timezone.now(),
    )
    run.refresh_from_db()
    return run


def _run_is_stale(business_profile: BusinessProfile, run: DailyAssignmentRun) -> bool:
    """
    自動割当結果が古くなっているかを判定

    自動割当実行後に予約の追加・変更や配達者の設定変更があると、
    画面に出ている割当結果と実際の状況がずれることがあるため。
    """
    if run.status not in (
        DailyAssignmentRun.Status.DRAFT,
        DailyAssignmentRun.Status.APPLIED,
    ):
        return False
    try:
        current_snapshot = build_tasks(
            business_profile,
            run.service_date,
            set(run.selected_driver_ids) or None,
        ).snapshot()
        return snapshot_hash(current_snapshot) != run.input_hash
    except Exception:  # noqa: BLE001
        return False


def _assignment_task_data(assignment: DailyTaskAssignment) -> dict[str, Any]:
    """担当割当1件をAPIレスポンス用のdictに変換"""
    booking = assignment.booking
    is_pickup = assignment.kind == 'pickup'
    return {
        'id': str(assignment.id),
        'booking_number': booking.booking_number,
        'task_type': assignment.kind,
        'location_name': (
            booking.pickup_location_name_display
            if is_pickup else booking.delivery_location_name_display
        ),
        'location_address': (
            booking.pickup_location_address_display
            if is_pickup else booking.delivery_location_address_display
        ),
        'manually_assigned': bool(assignment.manually_assigned),
    }


def _normalize_unassigned_task(raw: Any) -> dict[str, str]:
    """未割当タスクを API 契約どおりの形に揃える"""
    item = raw if isinstance(raw, dict) else {}
    task_id = str(item.get('task_id') or '')
    booking_from_task = ''
    type_from_task = ''
    if ':' in task_id:
        booking_from_task, _, type_from_task = task_id.partition(':')

    task_type = str(item.get('task_type') or type_from_task or 'pickup')
    if task_type not in ('pickup', 'delivery'):
        task_type = 'pickup'

    return {
        'booking_id': str(item.get('booking_id') or booking_from_task or ''),
        'booking_number': str(item.get('booking_number') or ''),
        'task_type': task_type,
        'reason': str(item.get('reason') or ''),
    }


def _serialize_run(
    run: DailyAssignmentRun,
    is_stale: bool = False,
) -> dict[str, Any]:
    """日次割当の実行結果をAPIレスポンス用のdictに変換（常に同じキー構成）"""
    grouped: dict[Any, dict[str, Any]] = {}
    for assignment in run.assignments.all():
        group = grouped.setdefault(assignment.driver_id, {
            'driver_id': str(assignment.driver_id),
            'driver_name': (
                assignment.driver.user.get_full_name()
                or str(assignment.driver_id)
            ),
            'task_count': 0,
            'tasks': [],
        })
        group['task_count'] += 1
        group['tasks'].append(_assignment_task_data(assignment))

    assignment_groups = list(grouped.values())
    return {
        'id': str(run.id),
        'service_date': run.service_date.isoformat(),
        'status': run.status,
        'is_stale': bool(is_stale),
        'selected_driver_ids': [
            str(driver_id) for driver_id in (run.selected_driver_ids or [])
        ],
        'assignment_count': sum(
            group['task_count'] for group in assignment_groups
        ),
        'assignment_groups': assignment_groups,
        'unassigned_tasks': [
            _normalize_unassigned_task(item)
            for item in (run.unassigned_tasks or [])
        ],
    }


def _run_queryset(business_profile: BusinessProfile) -> QuerySet[DailyAssignmentRun]:
    return DailyAssignmentRun.objects.filter(
        business_owner=business_profile
    ).prefetch_related(
        'assignments__driver__user', 'assignments__booking'
    )


def _service_date(request: Request) -> Optional[date]:
    raw = (
        request.data.get('date')
        if request.method == 'POST'
        else request.GET.get('date')
    )
    try:
        return date.fromisoformat(str(raw or ''))
    except ValueError:
        return None


def _selected_driver_ids(
    request: Request,
) -> tuple[Optional[set[str]], Optional[str]]:
    """正規化した候補配達者IDの集合を返す"""
    if request.method == 'POST':
        if 'selected_driver_ids' not in request.data:
            return None, None
        raw = request.data.get('selected_driver_ids')
        max_drivers = int(getattr(settings, 'ROUTING_MAX_DRIVERS', 200))
        if not isinstance(raw, list) or len(raw) > max_drivers:
            return None, '候補配達者の選択が正しくありません。選び直してください。'
        values = raw
    else:
        if 'selected_driver_ids' not in request.GET:
            return None, None
        raw = str(request.GET.get('selected_driver_ids') or '')
        values = raw.split(',') if raw else []
    normalized = {str(value).strip() for value in values if str(value).strip()}
    if len(normalized) > int(getattr(settings, 'ROUTING_MAX_DRIVERS', 200)):
        return None, '候補配達者が多すぎます。数を減らして選び直してください。'
    return normalized, None


def _problem_limits(problem: AssignmentInput) -> dict[str, Any]:
    """自動割当の処理上限・訪問枠の充足状況を返す"""
    booking_count = len({task.booking_id for task in problem.tasks})
    task_count = len(problem.tasks)
    driver_count = len(problem.drivers)
    stop_capacity = sum(
        driver.max_stops if driver.max_stops is not None else task_count
        for driver in problem.drivers
    )
    within_limit = (
        booking_count
        <= int(getattr(settings, 'ROUTING_MAX_BOOKINGS', 1000))
        and task_count <= int(getattr(settings, 'ROUTING_MAX_TASKS', 2000))
        and driver_count <= int(getattr(settings, 'ROUTING_MAX_DRIVERS', 200))
    )
    return {
        'booking_count': booking_count,
        'task_count': task_count,
        'driver_count': driver_count,
        'stop_capacity': stop_capacity,
        'capacity_sufficient': stop_capacity >= task_count,
        'within_limit': within_limit,
    }


def _eligible_driver_data(
    business_profile: BusinessProfile,
    service_date: date,
) -> tuple[list[DriverProfile], list[dict[str, Any]]]:
    """その日の候補配達者を取得し、検証用一覧とAPI表示用一覧を返す"""
    drivers = eligible_drivers(business_profile, service_date)
    return drivers, [{
        'id': str(driver.id),
        'name': driver.user.get_full_name() or str(driver.id),
        'departure_address': driver.departure_address,
    } for driver in drivers]


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def estimate(request: Request) -> Response:
    """自動割当の事前確認（候補配達者・対象規模・除外タスクを返す）"""
    business_profile = _get_business_profile(request)
    if business_profile is None:
        return Response(
            {'errMsg': '事業者情報が見つかりません。'},
            status=status.HTTP_404_NOT_FOUND,
        )

    service_date = _service_date(request)
    if service_date is None:
        return Response(
            {'errMsg': '日付の形式が正しくありません。'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    selected_driver_ids, selection_err = _selected_driver_ids(request)
    if selection_err:
        return Response(
            {'errMsg': selection_err},
            status=status.HTTP_400_BAD_REQUEST,
        )

    eligible, eligible_data = _eligible_driver_data(business_profile, service_date)
    eligible_ids = {str(driver.id) for driver in eligible}
    if selected_driver_ids is not None and not selected_driver_ids <= eligible_ids:
        return Response(
            {'errMsg': '選択できない配達者が含まれています。'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    problem = build_tasks(
        business_profile,
        service_date,
        selected_driver_ids=selected_driver_ids,
    )
    limits = _problem_limits(problem)
    return Response(
        {
            'service_date': service_date.isoformat(),
            'eligible_driver_count': len(problem.drivers),
            'eligible_drivers': eligible_data,
            **limits,
            'excluded': problem.excluded,
        },
    )


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def assign(request: Request) -> Response:
    """日次自動割当を開始する（非同期ジョブをキューに投入）"""
    business_profile = _get_business_profile(request)
    if business_profile is None:
        return Response(
            {'errMsg': '事業者情報が見つかりません。'},
            status=status.HTTP_404_NOT_FOUND,
        )

    service_date = _service_date(request)
    if service_date is None:
        return Response(
            {'errMsg': '日付の形式が正しくありません。'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    selected_driver_ids, selection_err = _selected_driver_ids(request)
    if selection_err:
        return Response(
            {'errMsg': selection_err},
            status=status.HTTP_400_BAD_REQUEST,
        )

    eligible, _ = _eligible_driver_data(business_profile, service_date)
    eligible_ids = {str(driver.id) for driver in eligible}
    if selected_driver_ids is not None:
        if not selected_driver_ids:
            return Response(
                {'errMsg': '候補配達者を1人以上選択してください。'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not selected_driver_ids <= eligible_ids:
            return Response(
                {'errMsg': '選択できない配達者が含まれています。'},
                status=status.HTTP_400_BAD_REQUEST,
            )
    else:
        selected_driver_ids = eligible_ids

    if not selected_driver_ids:
        return Response(
            {'errMsg': '利用可能な配達者がいません。'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    max_advance = int(getattr(settings, 'ROUTING_MAX_ADVANCE_DAYS', 90))
    if not timezone.localdate() <= service_date <= timezone.localdate() + timedelta(days=max_advance):
        return Response(
            {'errMsg': '自動割当できる日付の範囲外です。'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    active = DailyAssignmentRun.objects.filter(
        business_owner=business_profile,
        service_date=service_date,
        status__in=[
            DailyAssignmentRun.Status.QUEUED,
            DailyAssignmentRun.Status.RUNNING,
        ],
    ).first()
    if active:
        active = _expire_run_if_timed_out(active)
    if active and active.status in (
        DailyAssignmentRun.Status.QUEUED,
        DailyAssignmentRun.Status.RUNNING,
    ):
        return Response(
            {
                'errMsg': '同じ日付の自動割当が実行中です。',
                'run_id': str(active.id),
            },
            status=status.HTTP_409_CONFLICT,
        )

    problem = build_tasks(
        business_profile,
        service_date,
        selected_driver_ids,
    )
    snapshot = problem.snapshot()
    limits = _problem_limits(problem)
    if not limits['within_limit']:
        return Response(
            {
                'errMsg': '自動割当の予約、集荷・配達の件数、または配達者数が上限を超えています。',
                **limits,
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    if limits['task_count'] == 0:
        return Response(
            {
                'errMsg': '対象日に割り当て可能な集荷・配達がありません。',
                **limits,
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        with transaction.atomic():
            run = DailyAssignmentRun.objects.create(
                business_owner=business_profile,
                service_date=service_date,
                input_hash=snapshot_hash(snapshot),
                selected_driver_ids=sorted(selected_driver_ids),
            )
    except IntegrityError:
        return Response(
            {'errMsg': '同じ日付の自動割当が実行中です。'},
            status=status.HTTP_409_CONFLICT,
        )

    try:
        assign_daily_tasks.delay(str(run.id))
    except Exception:
        logger.exception('日次自動割当ジョブの投入に失敗しました: run_id=%s', run.id)
        DailyAssignmentRun.objects.filter(id=run.id).update(
            status=DailyAssignmentRun.Status.FAILED,
            completed_at=timezone.now(),
        )
        return Response(
            {
                'errMsg': '自動割当ジョブを開始できませんでした。',
                'run_id': str(run.id),
            },
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    return Response(
        {'run': _serialize_run(run)},
        status=status.HTTP_202_ACCEPTED,
    )


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def run_detail(request: Request, run_id: str) -> Response:
    """指定した自動割当結果の最新状態を返す（ポーリング用）"""
    business_profile = _get_business_profile(request)
    if business_profile is None:
        return Response(
            {'errMsg': '事業者情報が見つかりません。'},
            status=status.HTTP_404_NOT_FOUND,
        )

    try:
        run = _run_queryset(business_profile).get(id=run_id)
    except DailyAssignmentRun.DoesNotExist:
        return Response(
            {'errMsg': '自動割当結果が見つかりません。'},
            status=status.HTTP_404_NOT_FOUND,
        )

    run = _expire_run_if_timed_out(run)
    return Response(
        {
            'run': _serialize_run(
                run, is_stale=_run_is_stale(business_profile, run),
            ),
        },
    )


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def daily(request: Request) -> Response:
    """指定日の最新の自動割当結果を返す"""
    business_profile = _get_business_profile(request)
    if business_profile is None:
        return Response(
            {'errMsg': '事業者情報が見つかりません。'},
            status=status.HTTP_404_NOT_FOUND,
        )

    try:
        service_date = date.fromisoformat(str(request.GET.get('date') or ''))
    except ValueError:
        return Response(
            {'errMsg': '日付の形式が正しくありません。'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    run = _run_queryset(business_profile).filter(service_date=service_date).first()
    if run is not None:
        run = _expire_run_if_timed_out(run)
    is_stale = _run_is_stale(business_profile, run) if run is not None else False
    return Response(
        {
            'run': _serialize_run(run, is_stale=is_stale) if run else None,
        },
    )


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def cancel_run(request: Request, run_id: str) -> Response:
    """実行中（queued / running）の自動割当をキャンセル（冪等）"""
    business_profile = _get_business_profile(request)
    if business_profile is None:
        return Response(
            {'errMsg': '事業者情報が見つかりません。'},
            status=status.HTTP_404_NOT_FOUND,
        )

    with transaction.atomic():
        try:
            run = DailyAssignmentRun.objects.select_for_update().get(
                id=run_id, business_owner=business_profile
            )
        except DailyAssignmentRun.DoesNotExist:
            return Response(
                {'errMsg': '自動割当結果が見つかりません。'},
                status=status.HTTP_404_NOT_FOUND,
            )

        if run.status in (
            DailyAssignmentRun.Status.QUEUED,
            DailyAssignmentRun.Status.RUNNING,
        ):
            run.status = DailyAssignmentRun.Status.CANCELLED
            run.completed_at = timezone.now()
            run.save(update_fields=['status', 'completed_at'])

    # 終端状態（draft / applied 等）ならそのまま現在の状態を返す
    refreshed = _run_queryset(business_profile).get(id=run.id)
    return Response(
        {
            'run': _serialize_run(
                refreshed, is_stale=_run_is_stale(business_profile, refreshed),
            ),
        },
    )


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def apply_run(request: Request, run_id: str) -> Response:
    """自動割当の下書き結果を予約の担当者へ適用する"""
    business_profile = _get_business_profile(request)
    if business_profile is None:
        return Response(
            {'errMsg': '事業者情報が見つかりません。'},
            status=status.HTTP_404_NOT_FOUND,
        )

    with transaction.atomic():
        try:
            run = DailyAssignmentRun.objects.select_for_update().get(
                id=run_id, business_owner=business_profile
            )
        except DailyAssignmentRun.DoesNotExist:
            return Response(
                {'errMsg': '自動割当結果が見つかりません。'},
                status=status.HTTP_404_NOT_FOUND,
            )

        if run.status == DailyAssignmentRun.Status.APPLIED:
            applied = _run_queryset(business_profile).get(id=run.id)
            return Response(
                {
                    'run': _serialize_run(
                        applied,
                        is_stale=_run_is_stale(business_profile, applied),
                    ),
                },
            )

        if run.status != DailyAssignmentRun.Status.DRAFT:
            return Response(
                {'errMsg': '適用できる下書き結果ではありません。'},
                status=status.HTTP_409_CONFLICT,
            )

        # この時点の提案を先に確定しておく（以降のロック・再計算の影響を受けないようにする）
        proposed_assignments = list(
            run.assignments.select_related('driver', 'booking').all()
        )
        list(
            LuggageBooking.objects.select_for_update().filter(
                business_owner=business_profile,
            ).filter(
                Q(pickup_date=run.service_date)
                | Q(delivery_date=run.service_date)
            )
        )
        list(DriverProfile.objects.select_for_update().filter(
            business_owner=business_profile
        ))

        current = build_tasks(
            business_profile,
            run.service_date,
            set(run.selected_driver_ids) or None,
        ).snapshot()
        if snapshot_hash(current) != run.input_hash:
            run.status = DailyAssignmentRun.Status.STALE
            run.save(update_fields=['status'])
            return Response(
                {
                    'errMsg': '予約または配達者情報が変更されたため、再割当が必要です。',
                    'stale': True,
                },
                status=status.HTTP_409_CONFLICT,
            )

        assignments = {}
        for assignment in proposed_assignments:
            assignments.setdefault(
                assignment.booking_id, {}
            )[assignment.kind] = assignment.driver_id
        bookings = {
            booking.id: booking
            for booking in LuggageBooking.objects.filter(id__in=assignments)
        }
        for booking_id, assignment in assignments.items():
            booking = bookings[booking_id]
            update_fields = ['updated_at']
            if 'pickup' in assignment:
                booking.pickup_driver_id = assignment['pickup']
                booking.pickup_manually_assigned = False
                update_fields.extend(['pickup_driver', 'pickup_manually_assigned'])
            if 'delivery' in assignment:
                booking.driver_id = assignment['delivery']
                booking.delivery_manually_assigned = False
                update_fields.extend(['driver', 'delivery_manually_assigned'])
            if (
                booking.pickup_driver_id is not None
                and booking.pickup_driver_id == booking.driver_id
            ):
                booking.pickup_driver_id = None
                booking.pickup_manually_assigned = False
                if 'pickup_driver' not in update_fields:
                    update_fields.append('pickup_driver')
                if 'pickup_manually_assigned' not in update_fields:
                    update_fields.append('pickup_manually_assigned')
            booking.save(update_fields=update_fields)
            BookingAuditLog.objects.create(
                booking=booking,
                payment_intent_id=booking.payment_intent_id,
                action=BookingAuditLog.ACTION_AUTO_ASSIGNED,
                source=BookingAuditLog.SOURCE_SYSTEM,
                message='日次自動割当結果を担当割当に適用しました。',
                metadata={'assignment_run_id': str(run.id)},
            )

        run.status = DailyAssignmentRun.Status.APPLIED
        # 適用で担当が変わるのでスナップショットも変わる。
        # その差分を「入力が古くなった」と誤判定しないよう、input_hash を適用後の状態に合わせる。
        post_apply_snapshot = build_tasks(
            business_profile,
            run.service_date,
            set(run.selected_driver_ids) or None,
        ).snapshot()
        run.input_hash = snapshot_hash(post_apply_snapshot)
        run.save(update_fields=['status', 'applied_at', 'input_hash'])

    applied = _run_queryset(business_profile).get(id=run.id)
    return Response(
        {
            'run': _serialize_run(
                applied, is_stale=_run_is_stale(business_profile, applied),
            ),
        },
    )