from typing import Any


def mask_sensitive_id(id_value: Any, visible_chars: int = 4) -> str:
    """機密情報（IDなど）の一部をマスクして返す"""
    if id_value is None:
        return "None"

    id_str = str(id_value)
    if len(id_str) <= visible_chars:
        return "*" * len(id_str)

    return id_str[:visible_chars] + "*" * (len(id_str) - visible_chars)
