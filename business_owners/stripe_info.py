"""
事業者の Stripe Connect アカウントに由来する表示用情報（会社名英語表記・
所在地・問い合わせ先・代表者名）を取得するモジュール。

Stripe API は障害やレート制限で一時的に失敗しうるため、取得に成功した値は
BusinessProfile にキャッシュし、失敗時はキャッシュした値へフォールバックする。
これにより、領収書・特定商取引法に基づく表記・確認メールで発行者情報が欠落する
可能性を下げる。

このモジュールは bookings など他アプリから参照されるため、循環参照を避ける目的で
bookings への依存を持たない。
"""
import logging
import re
from typing import Any, Optional, TYPE_CHECKING

import stripe
from django.utils import timezone

if TYPE_CHECKING:
    from .models import BusinessProfile


logger = logging.getLogger(__name__)

def format_phone_for_display(phone: Optional[str]) -> str:
    """電話番号を国内表示用に整形（+81... → 0...）"""
    phone = (phone or "").strip()
    if not phone:
        return ""
    if phone.startswith("+"):
        digits = re.sub(r"\D", "", phone)
        if digits.startswith("81") and len(digits) > 2:
            return f"0{digits[2:]}"
    return phone


def format_address_kanji(addr: Optional[dict[str, Any]]) -> str:
    """Stripe の address_kanji を『〒xxx-xxxx\\n住所本文』形式のプレーンテキストに整形"""
    addr = addr or {}
    parts = [
        addr.get("state", ""),
        addr.get("city", ""),
        addr.get("town", ""),
        addr.get("line1", ""),
        addr.get("line2", ""),
    ]
    address_body = "".join(part for part in parts if part)

    postal_raw = addr.get("postal_code", "")
    postal_half = (
        postal_raw.translate(str.maketrans("０１２３４５６７８９－", "0123456789-"))
        if postal_raw
        else ""
    )
    digits = "".join(char for char in postal_half if char.isdigit())
    if len(digits) == 7:
        postal_formatted = f"〒{digits[:3]}-{digits[3:]}"
    elif postal_half:
        postal_formatted = f"〒{postal_half}"
    else:
        postal_formatted = ""

    if postal_formatted and address_body:
        return f"{postal_formatted}\n{address_body}"
    if address_body:
        return address_body
    if postal_formatted:
        return postal_formatted
    return ""


# get_business_stripe_info が返す辞書のキー
_INFO_KEYS = (
    "company_name_en",
    "business_address",
    "support_email",
    "support_phone",
    "account_phone",
    "representative_name",
)


def _empty_info() -> dict[str, str]:
    return {key: "" for key in _INFO_KEYS}


def _cached_info(profile: "BusinessProfile") -> dict[str, str]:
    """BusinessProfile にキャッシュ済みの事業者情報を返す"""
    info = _empty_info()
    if profile.business_type == "company":
        info["company_name_en"] = profile.stripe_company_name_en_cache or ""
    info["business_address"] = profile.stripe_address_cache or ""
    info["support_email"] = profile.stripe_support_email_cache or ""
    # フォールバック用のため、support_phone ではなく account_phone に入れる
    info["account_phone"] = profile.stripe_support_phone_cache or ""
    info["representative_name"] = profile.stripe_representative_name_cache or ""
    return info


def _extract_from_account(
    account: Any, account_id: str, include_representative: bool
) -> dict[str, str]:
    """Stripe Account（＋必要なら Persons）から表示用情報を抽出"""
    info = _empty_info()

    business_profile = account.get("business_profile") or {}
    info["support_email"] = business_profile.get("support_email") or ""

    support_phone = business_profile.get("support_phone")
    if support_phone:
        info["support_phone"] = format_phone_for_display(support_phone)

    business_type = account.get("business_type", "")
    if business_type == "company":
        company = account.get("company") or {}
        info["company_name_en"] = (company.get("name") or "").strip()
        addr = company.get("address_kanji") or {}
    else:
        addr = (account.get("individual") or {}).get("address_kanji") or {}
    info["business_address"] = format_address_kanji(addr)

    if business_type == "company":
        account_phone = (account.get("company") or {}).get("phone")
    else:
        account_phone = (account.get("individual") or {}).get("phone")
    if account_phone:
        info["account_phone"] = format_phone_for_display(account_phone)

    if include_representative:
        if business_type == "company":
            try:
                persons = stripe.Account.list_persons(account_id, limit=100)
                for person in persons.data:
                    if person.relationship and person.relationship.get("representative"):
                        last = person.get("last_name_kanji") or ""
                        first = person.get("first_name_kanji") or ""
                        info["representative_name"] = f"{last} {first}".strip()
                        break
            except stripe.error.StripeError:  # type: ignore[attr-defined]
                logger.warning("代表者名の取得に失敗しました: account_id=%s", account_id)
        else:
            individual = account.get("individual") or {}
            last = individual.get("last_name_kanji") or ""
            first = individual.get("first_name_kanji") or ""
            info["representative_name"] = f"{last} {first}".strip()

    return info


def _update_cache(
    profile: "BusinessProfile", live: dict[str, str], include_representative: bool
) -> None:
    """取得できた（空でない）値のみをキャッシュへ反映"""
    # 実効電話番号（support 優先、無ければ account）をキャッシュ
    effective_phone = live.get("support_phone") or live.get("account_phone") or ""

    updates: dict[str, str] = {
        "stripe_company_name_en_cache": live.get("company_name_en") or "",
        "stripe_address_cache": live.get("business_address") or "",
        "stripe_support_email_cache": live.get("support_email") or "",
        "stripe_support_phone_cache": effective_phone,
    }
    if include_representative:
        updates["stripe_representative_name_cache"] = live.get("representative_name") or ""

    changed: list[str] = []
    for field, value in updates.items():
        if value and getattr(profile, field) != value:
            setattr(profile, field, value)
            changed.append(field)

    if changed:
        profile.stripe_info_cached_at = timezone.now()
        changed += ["stripe_info_cached_at", "updated_at"]
        try:
            profile.save(update_fields=changed)
        except Exception:
            logger.warning(
                "Stripe事業者情報キャッシュの保存に失敗しました: profile_id=%s",
                getattr(profile, "id", None),
            )


def derive_stripe_review_status(account: Any) -> str:
    """Stripe Account を LugGo の審査ステータス（1つ）に変換"""
    # 循環参照を避けるため関数内で import する
    from .models import StripeReviewStatus

    def _get(obj: Any, key: str, default: Any = None) -> Any:
        if obj is None:
            return default
        if hasattr(obj, "get"):
            return obj.get(key, default)
        return getattr(obj, key, default)

    details_submitted = bool(_get(account, "details_submitted", False))
    charges_enabled = bool(_get(account, "charges_enabled", False))
    payouts_enabled = bool(_get(account, "payouts_enabled", False))

    requirements = _get(account, "requirements") or {}
    currently_due = list(_get(requirements, "currently_due", []) or [])
    past_due = list(_get(requirements, "past_due", []) or [])
    disabled_reason = _get(requirements, "disabled_reason") or ""

    # Stripe に却下された（不正・利用規約違反など）場合は最優先で利用不可扱い
    if isinstance(disabled_reason, str) and disabled_reason.startswith("rejected"):
        return StripeReviewStatus.REJECTED

    # まだ決済アカウント登録フォームへの入力が完了していない
    if not details_submitted:
        return StripeReviewStatus.INCOMPLETE

    # 決済・入金ともに有効で、対応が必要な要件も残っていない = 審査通過
    if charges_enabled and payouts_enabled and not currently_due and not past_due:
        return StripeReviewStatus.ENABLED

    # 事業者側の追加対応（書類再提出・情報入力）が必要
    if currently_due or past_due:
        return StripeReviewStatus.RESTRICTED

    # 情報提出済み・未対応要件なしだが未有効 = Stripe 側で審査中
    return StripeReviewStatus.PENDING


def _requirements_due_list(account: Any) -> list[str]:
    """requirements.currently_due と past_due を結合した重複がないリストを返す"""
    requirements = account.get("requirements") if hasattr(account, "get") else getattr(
        account, "requirements", None
    )
    requirements = requirements or {}

    def _due(key: str) -> list[str]:
        if hasattr(requirements, "get"):
            return list(requirements.get(key, []) or [])
        return list(getattr(requirements, key, []) or [])

    combined = _due("currently_due") + _due("past_due")
    # 順序を保ちつつ重複を除去
    seen: set[str] = set()
    result: list[str] = []
    for item in combined:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def sync_stripe_review_status(
    profile: "BusinessProfile",
    account: Any,
    *,
    notify: bool = True,
) -> bool:
    """
    Stripe Account の審査状態を BusinessProfile へ同期

    審査状態が変化した場合、notify=True なら事業者へメール通知
    """
    from .models import StripeReviewStatus

    new_status = derive_stripe_review_status(account)
    old_status = profile.stripe_review_status or StripeReviewStatus.UNKNOWN

    disabled_reason = ""
    requirements = account.get("requirements") if hasattr(account, "get") else getattr(
        account, "requirements", None
    )
    if requirements:
        raw_reason = (
            requirements.get("disabled_reason")
            if hasattr(requirements, "get")
            else getattr(requirements, "disabled_reason", None)
        )
        disabled_reason = raw_reason or ""

    def _flag(key: str) -> bool:
        if hasattr(account, "get"):
            return bool(account.get(key, False))
        return bool(getattr(account, key, False))

    profile.stripe_review_status = new_status
    profile.stripe_charges_enabled = _flag("charges_enabled")
    profile.stripe_payouts_enabled = _flag("payouts_enabled")
    profile.stripe_details_submitted = _flag("details_submitted")
    profile.stripe_disabled_reason = disabled_reason[:100]
    profile.stripe_currently_due = _requirements_due_list(account)
    profile.stripe_review_status_updated_at = timezone.now()

    update_fields = [
        "stripe_review_status",
        "stripe_charges_enabled",
        "stripe_payouts_enabled",
        "stripe_details_submitted",
        "stripe_disabled_reason",
        "stripe_currently_due",
        "stripe_review_status_updated_at",
        "updated_at",
    ]
    try:
        profile.save(update_fields=update_fields)
    except Exception:
        logger.warning(
            "Stripe審査状態の保存に失敗しました: profile_id=%s",
            getattr(profile, "id", None),
        )
        return False

    status_changed = old_status != new_status
    if status_changed:
        logger.info(
            "Stripe審査状態が変化しました: profile_id=%s %s -> %s",
            getattr(profile, "id", None),
            old_status,
            new_status,
        )
        if notify and new_status != StripeReviewStatus.INCOMPLETE:
            # 循環 import を避けるため遅延 import
            from .utils import send_stripe_review_status_email

            try:
                send_stripe_review_status_email(profile, old_status, new_status)
            except Exception:
                logger.warning(
                    "Stripe審査状態変更メールの送信に失敗しました: profile_id=%s",
                    getattr(profile, "id", None),
                    exc_info=True,
                )

    return status_changed


def get_business_stripe_info(
    profile: "BusinessProfile",
    *,
    include_representative: bool = False,
    refresh: bool = True,
) -> dict[str, str]:
    """事業者の Stripe 由来の情報を取得

    - refresh=True かつ Stripe アカウントがある場合は API から取得し、成功時は
      キャッシュを更新する。取得できなかった項目はキャッシュ値で補完する。
    - Stripe 取得に失敗（例外・アカウント無し）した場合はキャッシュ値を返す。
    """
    cached = _cached_info(profile)

    account_id = (getattr(profile, "stripe_account_id", "") or "").strip()
    if not refresh or not account_id:
        return cached

    try:
        account = stripe.Account.retrieve(account_id)
    except stripe.error.StripeError:  # type: ignore[attr-defined]
        logger.warning(
            "Stripeアカウント取得に失敗したためキャッシュにフォールバックします: profile_id=%s",
            getattr(profile, "id", None),
        )
        return cached

    live = _extract_from_account(account, account_id, include_representative)
    _update_cache(profile, live, include_representative)

    # 取得できた値を優先し、空の項目はキャッシュ値で補完する
    merged = _empty_info()
    merged["company_name_en"] = live["company_name_en"] or cached["company_name_en"]
    merged["business_address"] = live["business_address"] or cached["business_address"]
    merged["support_email"] = live["support_email"] or cached["support_email"]
    merged["support_phone"] = live["support_phone"]
    merged["account_phone"] = live["account_phone"] or cached["account_phone"]
    merged["representative_name"] = (
        live["representative_name"] or cached["representative_name"]
    )
    return merged
