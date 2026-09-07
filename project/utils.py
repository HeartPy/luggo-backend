from typing import Any


def mask_sensitive_id(value: str | bytes | None) -> str:
    """機密情報（IDなど）をマスクしてログ出力用の文字列を返す"""
    if value is None:
        return "None"

    str_value = str(value)
    if len(str_value) <= 8:
        # 8文字以下の場合は全てマスク
        return "***"
    elif len(str_value) <= 16:
        # 16文字以下の場合は最初の4文字と最後の4文字を表示
        return f"{str_value[:4]}***{str_value[-4:]}"
    else:
        # 16文字より長い場合は最初の6文字と最後の6文字を表示
        return f"{str_value[:6]}***{str_value[-6:]}"


def stripe_get(obj: Any, key: str, default: Any = None) -> Any:
    """dict / StripeObject 両対応でキーを取得"""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    try:
        if key in obj:
            return obj[key]
        return default
    except TypeError:
        return getattr(obj, key, default)


def as_stripe_dict(obj: Any) -> Any:
    """StripeObject なら再帰的に dict へ変換"""
    if obj is None or isinstance(obj, dict):
        return obj
    to_dict = getattr(obj, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    return obj
