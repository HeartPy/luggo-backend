from typing import Any


def is_business_owner_account_blocked(user: Any) -> bool:
    """事業者アカウントが無効、またはプロフィールが無いために利用できないか"""
    if getattr(user, 'user_type', None) != 'business_owner':
        return False
    profile = getattr(user, 'business_profile', None)
    return profile is None or not profile.is_active


def is_driver_account_blocked(user: Any) -> bool:
    """配達者アカウントが無効、または所属事業者が無効なために利用できないか"""
    if getattr(user, 'user_type', None) != 'delivery_driver':
        return False
    profile = getattr(user, 'driver_profile', None)
    if profile is None or not profile.is_active:
        return True
    business = getattr(profile, 'business_owner', None)
    return business is None or not business.is_active


def is_account_blocked(user: Any) -> bool:
    """事業者または配達者としてログイン・API利用できないか"""
    return is_business_owner_account_blocked(user) or is_driver_account_blocked(user)
