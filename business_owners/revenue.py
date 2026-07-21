"""事業者ダッシュボードの売上・Stripe 入金履歴を組み立てる処理"""

from datetime import date, datetime, timezone as dt_timezone
import logging
from typing import Any, Iterable

import stripe
from django.utils import timezone

from bookings.models import LuggageBooking
from .models import BusinessProfile


logger = logging.getLogger(__name__)
PLATFORM_FEE_PERCENT = 10


def month_bounds(year: int, month: int) -> tuple[datetime, datetime]:
    """指定月の開始（含む）と翌月開始（含まない）を日本のタイムゾーンで返す"""
    start = timezone.make_aware(datetime(year, month, 1))
    if month == 12:
        end = timezone.make_aware(datetime(year + 1, 1, 1))
    else:
        end = timezone.make_aware(datetime(year, month + 1, 1))
    return start, end


def _months_ago_start(months: int) -> datetime:
    """Nか月前の「その月の1日 0時」（日本時間）を返す。N=0は今月、N=1は先月。"""
    today = timezone.localdate()
    month_index = today.year * 12 + (today.month - 1) - months
    year, zero_based_month = divmod(month_index, 12)
    return timezone.make_aware(datetime(year, zero_based_month + 1, 1))


def platform_fee(amount: int) -> int:
    """合計金額に対するプラットフォーム手数料（10%）を返す"""
    return amount * PLATFORM_FEE_PERCENT // 100


def refunded_platform_fee(total: int, refunded: int) -> int:
    """返金額に応じて取り消すプラットフォーム手数料を返す（全額返金なら手数料も全額、半分なら半分）"""
    if total <= 0 or refunded <= 0:
        return 0
    return min(platform_fee(total), platform_fee(total) * refunded // total)


def calculate_monthly_revenue(
    profile: BusinessProfile,
    year: int,
    month: int,
) -> dict[str, int]:
    """
    対象月に配達完了し、Stripe 上で入金可能になった予約から売上を算出

    - 集計対象は「対象月に配達完了（delivered）した予約」のみ
    - 配達完了していても、決済資金がまだ Stripe 上で入金可能になっていない
      予約（funds_available_on が未確定または未来）は含めない
    - 返金があった予約は、返金額とその分の手数料を差し引く
    """
    start, end = month_bounds(year, month)
    now = timezone.now()

    rows = LuggageBooking.objects.filter(
        business_owner=profile,
        delivery_status="delivered",
        delivered_at__gte=start,
        delivered_at__lt=end,
        funds_available_on__isnull=False,
        funds_available_on__lte=now,
    ).values_list("total_amount", "refunded_amount", "refund_status")

    gross_sales = 0
    fee_total = 0
    for total, refunded, refund_status in rows:
        total_amount = int(total or 0)
        refunded_amount = 0
        if refund_status == LuggageBooking.REFUND_STATUS_SUCCEEDED:
            refunded_amount = min(total_amount, int(refunded or 0))
        gross_sales += total_amount - refunded_amount
        fee_total += platform_fee(total_amount) - refunded_platform_fee(
            total_amount, refunded_amount
        )

    return {
        "gross_sales": gross_sales,
        "platform_fee": fee_total,
        "net_sales": gross_sales - fee_total,
    }


def _get(value: Any, key: str, default: Any = None) -> Any:
    if value is None:
        return default
    if hasattr(value, "get"):
        return value.get(key, default)
    return getattr(value, key, default)


def _iter_payouts(result: Any) -> Iterable[Any]:
    """Stripe の Payout 一覧を1件ずつ回せる形にする"""
    auto_paging_iter = getattr(result, "auto_paging_iter", None)
    if callable(auto_paging_iter):
        return auto_paging_iter()
    return _get(result, "data", []) or []


def _payout_destination_label(destination: Any) -> str:
    """入金先口座の表示用ラベルを返す"""
    if not destination or isinstance(destination, str):
        return "登録口座"

    bank_name = str(_get(destination, "bank_name", "") or "").strip()
    last4 = str(_get(destination, "last4", "") or "").strip()
    if bank_name and last4:
        return f"{bank_name} •••• {last4}"
    if last4:
        return f"口座 •••• {last4}"
    return bank_name or "登録口座"


def _payout_date(timestamp: Any) -> date:
    """StripeのUnix秒を日本時間の日付に変換"""
    try:
        value = int(timestamp)
    except (TypeError, ValueError):
        return timezone.localdate()
    return timezone.localtime(datetime.fromtimestamp(value, tz=dt_timezone.utc)).date()


def list_payout_history(
    profile: BusinessProfile,
    history_months: int,
) -> tuple[list[dict[str, Any]], bool]:
    """連結アカウントから銀行口座へ入金済みの Payout を返す"""
    account_id = (profile.stripe_account_id or "").strip()
    if not account_id:
        return [], False

    one_year_start = _months_ago_start(11)
    visible_start = _months_ago_start(history_months - 1)
    result = stripe.Payout.list(
        stripe_account=account_id,
        created={"gte": int(one_year_start.timestamp())},
        status="paid",
        limit=100,
        expand=["data.destination"],
    )

    payouts: list[dict[str, Any]] = []
    has_more = False
    for payout in _iter_payouts(result):
        created = _get(payout, "created", 0)
        try:
            created_at = datetime.fromtimestamp(int(created), tz=dt_timezone.utc)
        except (TypeError, ValueError, OSError, OverflowError):
            continue

        if created_at < visible_start:
            has_more = True
            continue

        arrival_date = _get(payout, "arrival_date", created)
        payouts.append(
            {
                "id": str(_get(payout, "id", "")),
                "amount": int(_get(payout, "amount", 0) or 0),
                "destination": _payout_destination_label(
                    _get(payout, "destination")
                ),
                "paid_at": _payout_date(arrival_date).isoformat(),
            }
        )

    payouts.sort(key=lambda item: (item["paid_at"], item["id"]), reverse=True)
    return payouts, has_more and history_months < 12
