"""
Google Routes API を使って、地点間の道路所要時間・距離の行列を取得する

複数の出発地点と目的地の組み合わせごとに、実際の走行時間（秒）と距離（メートル）
の正方行列（MatrixResult）を返す。キャッシュ・チャンク分割・リトライも担う。
割当済みルートの訪問順最適化（solver.py）や到着予定時刻計算の材料として使用想定。
"""
import math
import hashlib
import json
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time
from typing import Any, Optional

import requests
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone


class RoutesAPIError(RuntimeError):
    pass


@dataclass
class MatrixResult:
    """地点数 N に対する N×N の所要時間・距離行列"""
    durations: list[list[int]]
    distances: list[list[int]]
    element_count: int


def _seconds(value: Any) -> int:
    """API の '12.3s' 形式を切り上げ秒（整数）に変換"""
    if not isinstance(value, str) or not value.endswith('s'):
        raise RoutesAPIError(f'Routes API の所要時間が不正です: {value!r}')
    try:
        return max(0, math.ceil(float(value[:-1])))
    except ValueError as exc:
        raise RoutesAPIError(f'Routes API の所要時間が不正です: {value!r}') from exc


def _status_code(el: dict) -> int:
    status = el.get('status') or {}
    try:
        return int(status.get('code', 0) or 0)
    except (TypeError, ValueError):
        return 0


def _is_cancelled_element(el: dict) -> bool:
    """Google が行列要素を途中キャンセルした場合"""
    # gRPC では code 1 = CANCELLED
    if _status_code(el) == 1:
        return True
    message = str((el.get('status') or {}).get('message') or '').lower()
    return 'cancelled' in message or 'canceled' in message


def _chunk_needs_retry(els: list, expected_els: int) -> bool:
    """このチャンクをもう一度 API に問い合わせるべきか判定"""
    if len(els) < expected_els:
        return True
    return any(_is_cancelled_element(el) for el in els)


def resolve_driver_departure_time(service_date: date, driver: Any) -> datetime:
    """
    1人の配達者のルート表示用に、その人のシフト開始から出発時刻を決める。
    出発予定が過ぎていれば現在時刻を使う。
    """
    start_seconds = int(getattr(driver, 'shift_start_seconds', 9 * 60 * 60))
    start_time = dt_time(
        hour=start_seconds // 3600,
        minute=(start_seconds % 3600) // 60,
        second=start_seconds % 60,
    )
    planned = timezone.make_aware(datetime.combine(service_date, start_time))
    now = timezone.now()
    # Routes API は過去の departureTime を受け付けない
    return planned if planned > now else now


class GoogleRoutesMatrixClient:
    endpoint = 'https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix'

    def __init__(self, api_key: str | None = None, session=None):
        self.api_key = api_key or getattr(settings, 'GOOGLE_ROUTES_API_KEY', '')
        self.session = session or requests
        self.routing_preference = getattr(
            settings, 'ROUTING_GOOGLE_ROUTING_PREFERENCE', 'TRAFFIC_AWARE'
        )
        # OPTIMAL は API 側のチャンク上限が小さい
        max_chunk_size = (
            10 if self.routing_preference == 'TRAFFIC_AWARE_OPTIMAL' else 25
        )
        self.chunk_size = min(
            max_chunk_size,
            max(1, int(getattr(settings, 'ROUTING_MATRIX_CHUNK_SIZE', 25))),
        )

    def _cache_key(
        self,
        points: list[tuple[float, float]],
        departure_time: Optional[datetime] = None,
    ) -> str:
        """API 結果を再利用するため、地点・設定・出発時刻からキャッシュ用のキーを作る"""
        # 座標は小数6桁に丸めて、ほぼ同じ地点の再利用を増やす
        payload = json.dumps(
            {
                'points': [
                    [round(latitude, 6), round(longitude, 6)]
                    for latitude, longitude in points
                ],
                'preference': self.routing_preference,
                'departure_time': (
                    departure_time.isoformat() if departure_time else None
                ),
            },
            separators=(',', ':'),
        )
        return f'routing:matrix:{hashlib.sha256(payload.encode()).hexdigest()}'

    @staticmethod
    def _reserve_elements(el_count: int) -> None:
        """1分あたりのAPI呼び出し量が上限を超えないよう、並行処理同士で枠を分け合う"""
        limit = int(getattr(settings, 'ROUTING_MATRIX_ELEMENTS_PER_MINUTE', 3000))
        if limit <= 0:
            return

        while True:
            now = time.time()
            # 分単位のバケットでカウントする
            bucket = int(now // 60)
            key = f'routing:routes-api-elements:{bucket}'
            if cache.add(key, el_count, timeout=70):
                return
            try:
                reserved = cache.incr(key, el_count)
            except ValueError:
                continue
            if reserved <= limit:
                return
            # 枠が足りなければ次の分まで待つ
            time.sleep(max(0.1, 60 - (now % 60) + 0.05))

    @staticmethod
    def _waypoint(point: tuple[float, float]) -> dict:
        """緯度経度を Google Routes API 用の地点形式に変換"""
        return {
            'waypoint': {
                'location': {
                    'latLng': {'latitude': point[0], 'longitude': point[1]}
                }
            }
        }

    def _request_chunk_once(
        self,
        *,
        origins: list[tuple[float, float]],
        destinations: list[tuple[float, float]],
        headers: dict,
        departure_time: Optional[datetime] = None,
    ) -> list:
        """origins × destinations の1チャンクだけ API を1回呼ぶ"""
        payload = {
            'origins': [self._waypoint(point) for point in origins],
            'destinations': [self._waypoint(point) for point in destinations],
            'travelMode': 'DRIVE',
            'routingPreference': self.routing_preference,
        }
        # TRAFFIC_UNAWARE では departureTime を付けられない
        if (
            departure_time is not None
            and self.routing_preference != 'TRAFFIC_UNAWARE'
        ):
            payload['departureTime'] = departure_time.isoformat()

        self._reserve_elements(len(origins) * len(destinations))
        try:
            response = self.session.post(
                self.endpoint,
                json=payload,
                headers=headers,
                timeout=getattr(settings, 'ROUTING_HTTP_TIMEOUT_SECONDS', 30),
            )
            response.raise_for_status()
            els = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise RoutesAPIError(f'Routes API のリクエストに失敗しました: {exc}') from exc

        if not isinstance(els, list):
            raise RoutesAPIError('Routes API の応答がリストではありません')
        return els

    def _fill_block(
        self,
        *,
        points: list[tuple[float, float]],
        durations: list[list[int]],
        distances: list[list[int]],
        headers: dict,
        origin_start: int,
        origin_end: int,
        destination_start: int,
        destination_end: int,
        departure_time: Optional[datetime] = None,
    ) -> None:
        """行列の部分ブロックを埋める。失敗時は再試行し、だめなら半分に分割する"""
        origins = points[origin_start:origin_end]
        destinations = points[destination_start:destination_end]
        if not origins or not destinations:
            return

        same_size_retries = max(
            1, int(getattr(settings, 'ROUTING_MATRIX_CHUNK_RETRIES', 2))
        )
        els: list = []
        last_err: Exception | None = None

        for attempt in range(1, same_size_retries + 1):
            try:
                els = self._request_chunk_once(
                    origins=origins,
                    destinations=destinations,
                    headers=headers,
                    departure_time=departure_time,
                )
            except RoutesAPIError as exc:
                last_err = exc
                if attempt < same_size_retries:
                    time.sleep(min(2 ** (attempt - 1), 8))
                    continue
                break

            if not _chunk_needs_retry(els, len(origins) * len(destinations)):
                self._apply_elements(
                    els=els,
                    durations=durations,
                    distances=distances,
                    origin_start=origin_start,
                    destination_start=destination_start,
                )
                return

            if attempt < same_size_retries:
                # 少し待ってから、同じチャンクをもう一度試す
                time.sleep(min(2 ** (attempt - 1), 8))

        # 同じサイズでだめなら、出発側・到着側を半分に割って再帰する
        origin_span = origin_end - origin_start
        dest_span = destination_end - destination_start
        if origin_span > 1 or dest_span > 1:
            mid_origin = origin_start + max(1, origin_span // 2)
            mid_dest = destination_start + max(1, dest_span // 2)
            if origin_span == 1:
                mid_origin = origin_end
            if dest_span == 1:
                mid_dest = destination_end

            for next_o_start, next_o_end in (
                (origin_start, mid_origin),
                (mid_origin, origin_end),
            ):
                if next_o_start >= next_o_end:
                    continue
                for next_d_start, next_d_end in (
                    (destination_start, mid_dest),
                    (mid_dest, destination_end),
                ):
                    if next_d_start >= next_d_end:
                        continue
                    self._fill_block(
                        points=points,
                        durations=durations,
                        distances=distances,
                        headers=headers,
                        origin_start=next_o_start,
                        origin_end=next_o_end,
                        destination_start=next_d_start,
                        destination_end=next_d_end,
                        departure_time=departure_time,
                    )
            return

        # 1×1 まで分割しても埋まらない場合
        if last_err is not None:
            raise last_err
        if els:
            self._apply_elements(
                els=els,
                durations=durations,
                distances=distances,
                origin_start=origin_start,
                destination_start=destination_start,
            )
            return
        raise RoutesAPIError(
            f'Routes API が行列ブロックをキャンセルしました '
            f'{origin_start}:{origin_end} -> {destination_start}:{destination_end}'
        )

    def _apply_elements(
        self,
        *,
        els: list,
        durations: list[list[int]],
        distances: list[list[int]],
        origin_start: int,
        destination_start: int,
    ) -> None:
        """チャンク内のローカル番号を全体行列のインデックスに直して書き込む"""
        for el in els:
            local_i = el.get('originIndex')
            local_j = el.get('destinationIndex')
            if not isinstance(local_i, int) or not isinstance(local_j, int):
                raise RoutesAPIError('Routes API の応答に行列インデックスがありません')

            i = origin_start + local_i
            j = destination_start + local_j
            status = el.get('status') or {}
            if _is_cancelled_element(el):
                raise RoutesAPIError(
                    f'Routes API が行列要素をキャンセルしました {i}->{j}: {el}'
                )
            if status.get('code', 0) != 0 or el.get('condition') not in (
                None, 'ROUTE_EXISTS'
            ):
                raise RoutesAPIError(
                    f'行列要素 {i}->{j} のルートがありません: {el}'
                )

            durations[i][j] = _seconds(el.get('duration'))
            distances[i][j] = int(el.get('distanceMeters', 0))

    def compute(
        self,
        points: list[tuple[float, float]],
        departure_time: Optional[datetime] = None,
    ) -> MatrixResult:
        """全地点間の所要時間・距離行列を返す（キャッシュがあればそれを使う）"""
        if not self.api_key:
            raise RoutesAPIError('GOOGLE_ROUTES_API_KEY が設定されていません')

        cache_key = self._cache_key(points, departure_time)
        cache_ttl = int(getattr(settings, 'ROUTING_MATRIX_CACHE_SECONDS', 300))
        if cache_ttl > 0:
            cached = cache.get(cache_key)
            if isinstance(cached, MatrixResult):
                return cached

        count = len(points)
        # 未取得は -1。同じ地点同士（対角）は移動なしなので 0
        durations = [[-1] * count for _ in range(count)]
        distances = [[-1] * count for _ in range(count)]
        for i in range(count):
            durations[i][i] = 0
            distances[i][i] = 0
        headers = {
            'Content-Type': 'application/json',
            'X-Goog-Api-Key': self.api_key,
            'X-Goog-FieldMask': (
                'originIndex,destinationIndex,status,condition,distanceMeters,duration'
            ),
        }

        # 大きすぎる行列は chunk_size ごとのブロックに分けて埋める
        for origin_start in range(0, count, self.chunk_size):
            origin_end = min(count, origin_start + self.chunk_size)
            for destination_start in range(0, count, self.chunk_size):
                destination_end = min(count, destination_start + self.chunk_size)
                self._fill_block(
                    points=points,
                    durations=durations,
                    distances=distances,
                    headers=headers,
                    origin_start=origin_start,
                    origin_end=origin_end,
                    destination_start=destination_start,
                    destination_end=destination_end,
                    departure_time=departure_time,
                )

        if any(value < 0 for row in durations for value in row):
            raise RoutesAPIError('Routes API の所要時間行列が不完全です')

        result = MatrixResult(durations, distances, count * count)
        if cache_ttl > 0:
            cache.set(cache_key, result, timeout=cache_ttl)
        return result
