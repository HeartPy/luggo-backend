"""
住所・Place ID から緯度経度を取得するジオコーディング

Google Geocoding API を呼び出し、集荷・配達地点や配達者の出発地点などの
座標解決に使う。
"""
from dataclasses import dataclass

import requests
from django.conf import settings


# 呼び出し側は種別だけで分岐（メッセージに API キーや詳細を載せない）
class GeocodingError(Exception):
    """ジオコーディング失敗の基底例外"""


class GeocodingNotConfigured(GeocodingError):
    """GOOGLE_GEOCODING_API_KEY が未設定"""


class GeocodingNotFound(GeocodingError):
    """住所・Place ID に該当する結果がない"""


class GeocodingServiceError(GeocodingError):
    """API 通信失敗・想定外の応答など"""


@dataclass(frozen=True)
class GeocodingResult:
    latitude: float
    longitude: float
    place_id: str
    address_components: list[dict]


def geocode(address: str, *, place_id: str = '') -> GeocodingResult:
    """住所または Place ID から座標を取得する。例外に認証情報は含めない。"""
    api_key = getattr(settings, 'GOOGLE_GEOCODING_API_KEY', '')
    if not api_key:
        raise GeocodingNotConfigured

    params = {'key': api_key, 'region': 'jp', 'language': 'ja'}
    # Place ID があればそれを優先し、なければ住所文字列で検索する
    params['place_id' if place_id else 'address'] = place_id or address
    try:
        response = requests.get(
            'https://maps.googleapis.com/maps/api/geocode/json',
            params=params,
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        # 元の例外は残しつつ、呼び出し側には詳細のない GeocodingServiceError だけ返す
        raise GeocodingServiceError from exc

    status = payload.get('status')
    if status == 'ZERO_RESULTS':
        raise GeocodingNotFound
    if status != 'OK':
        raise GeocodingServiceError
    try:
        # 先頭の候補を採用する（複数ヒット時も第1件）
        result = payload['results'][0]
        location = result['geometry']['location']
        return GeocodingResult(
            latitude=float(location['lat']),
            longitude=float(location['lng']),
            place_id=str(result.get('place_id', '') or ''),
            address_components=result.get('address_components', []) or [],
        )
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise GeocodingServiceError from exc
