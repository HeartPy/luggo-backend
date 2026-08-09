"""
予約の座標補完の Celery ジョブ

住所を手入力した予約（候補選択なし＝座標なし）に対して、郵便番号から
概算座標を取得して保存する。自動割当・ルート生成が距離計算に使えるようにする。
"""
import logging

from celery import shared_task

from project.geocoding import (
    GeocodingNotConfigured,
    GeocodingNotFound,
    GeocodingServiceError,
    geocode,
)

from .models import LuggageBooking


logger = logging.getLogger(__name__)


def _postal_code_query(postal_code: str) -> str:
    """郵便番号をジオコーディング用のクエリ文字列に変換"""
    digits = ''.join(ch for ch in postal_code if ch.isdigit())
    if len(digits) == 7:
        return f'〒{digits[:3]}-{digits[3:]} 日本'
    return f'{postal_code} 日本'


@shared_task
def geocode_booking_postal_coordinates(booking_id: str) -> None:
    """座標が欠けている集荷・配達地点を、郵便番号の概算座標で補完"""
    booking = LuggageBooking.objects.filter(id=booking_id).first()
    if booking is None:
        logger.warning('概算座標の補完対象の予約が見つかりません: booking_id=%s', booking_id)
        return

    for prefix in ('pickup', 'delivery'):
        if (
            getattr(booking, f'{prefix}_latitude') is not None
            and getattr(booking, f'{prefix}_longitude') is not None
        ):
            continue
        postal_code = (getattr(booking, f'{prefix}_postal_code') or '').strip()
        if not postal_code:
            continue

        try:
            result = geocode(_postal_code_query(postal_code))
        except GeocodingNotFound:
            # 座標は空のままにして backfill_geocodes の再実行候補として残す
            setattr(booking, f'{prefix}_geocode_status', 'failed')
            booking.save(update_fields=[f'{prefix}_geocode_status', 'updated_at'])
            logger.warning(
                '郵便番号から座標を取得できませんでした: booking_id=%s, prefix=%s',
                booking_id, prefix,
            )
            continue
        except (GeocodingServiceError, GeocodingNotConfigured):
            # 一時的な失敗の可能性があるため pending のまま（再実行で補完できる）
            logger.exception(
                '郵便番号ジオコーディングに失敗しました: booking_id=%s, prefix=%s',
                booking_id, prefix,
            )
            continue

        setattr(booking, f'{prefix}_latitude', result.latitude)
        setattr(booking, f'{prefix}_longitude', result.longitude)
        # 概算座標なので place_id は保存せず、状態だけ approximate にする
        setattr(booking, f'{prefix}_geocode_status', 'approximate')
        booking.save(update_fields=[
            f'{prefix}_latitude', f'{prefix}_longitude',
            f'{prefix}_geocode_status', 'updated_at',
        ])
