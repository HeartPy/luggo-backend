"""
割当入力スナップショットのハッシュ計算

build_tasks().snapshot() の dict を正規化した JSON にして SHA-256 ハッシュを返す。
キュー投入時・実行時・適用時に入力が変わっていないかの比較（input_hash）に使う。
"""
import hashlib
import json


def snapshot_hash(snapshot: dict) -> str:
    payload = json.dumps(
        snapshot, sort_keys=True, separators=(',', ':'), ensure_ascii=True
    )
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()
