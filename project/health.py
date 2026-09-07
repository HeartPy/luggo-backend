"""外形監視・ALB ヘルスチェック用の共通ロジック"""

from __future__ import annotations

import logging

from django.db import connection

logger = logging.getLogger(__name__)


def health_payload() -> tuple[dict[str, str], int]:
    """DB 疎通のみ確認し、(本文, HTTP ステータス) を返す"""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:
        logger.exception("ヘルスチェック失敗: DB に接続できません")
        return {"status": "error"}, 503
    return {"status": "ok"}, 200
