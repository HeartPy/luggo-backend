import re
from typing import Optional


def validate_password_strength(password: str) -> Optional[str]:
    """パスワードのバリデーション"""
    if not password:
        return "パスワードを入力してください。"

    if not re.match(r'^[a-zA-Z0-9!@#$%^&*()_+\-=\[\]{}|;:,.<>?]+$', password):
        return "パスワードは半角英数字と記号のみ使用できます。"

    if len(password) < 8 or len(password) > 16:
        return "パスワードは8文字以上16文字以内で入力してください。"

    has_upper = bool(re.search(r'[A-Z]', password))
    has_lower = bool(re.search(r'[a-z]', password))
    has_number = bool(re.search(r'[0-9]', password))
    has_special = bool(re.search(r'[!@#$%^&*()_+\-=\[\]{}|;:,.<>?]', password))

    types_count = sum([has_upper, has_lower, has_number, has_special])
    if types_count < 3:
        return "パスワードは大文字・小文字・数字・記号のうち3種類以上を含む必要があります。"

    return None
