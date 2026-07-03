"""
事業者の Stripe Connect アカウントに由来する表示用情報（所在地・問い合わせ先・
代表者名）を取得するモジュール。

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
        addr = (account.get("company") or {}).get("address_kanji") or {}
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
    merged["business_address"] = live["business_address"] or cached["business_address"]
    merged["support_email"] = live["support_email"] or cached["support_email"]
    merged["support_phone"] = live["support_phone"]
    merged["account_phone"] = live["account_phone"] or cached["account_phone"]
    merged["representative_name"] = (
        live["representative_name"] or cached["representative_name"]
    )
    return merged
