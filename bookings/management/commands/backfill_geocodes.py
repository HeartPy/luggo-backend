"""
座標が欠けている予約・配達者データを、あとからジオコーディングで埋める管理コマンド

事業者向け画面からは使わず、開発者・運営が必要時だけ実行する。
日常の新規登録・更新では画面側の geocode が座標を入れるため、通常運用では使わない。
"""
import time
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from bookings.models import LuggageBooking
from drivers.models import DriverProfile
from project.geocoding import (
    GeocodingNotFound,
    GeocodingServiceError,
    geocode,
)


class Command(BaseCommand):
    help = '欠けている予約・配達者の座標を補完する（デフォルトは dry-run）'

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply', action='store_true',
            help='API 結果を保存する。指定しない場合は API 呼び出しも書き込みも行わない。',
        )
        parser.add_argument(
            '--limit',
            type=int,
            default=100,
            help='一度に処理する件数上限',
        )
        parser.add_argument(
            '--delay',
            type=float,
            default=0.05,
            help='API を実際に呼ぶときの、呼び出し間隔（秒）',
        )

    def handle(self, *args, **options):
        apply = options['apply']
        limit = max(1, options['limit'])
        candidates = []
        # 予約の集荷・配達で緯度経度が欠けているものを優先して集める（事業者の絞り込みなし）
        for booking in LuggageBooking.objects.all().iterator():
            for prefix in ('pickup', 'delivery'):
                if (
                    getattr(booking, f'{prefix}_latitude') is None
                    or getattr(booking, f'{prefix}_longitude') is None
                ):
                    address = getattr(
                        booking, f'{prefix}_location_address'
                    ).strip()
                    if address:
                        candidates.append((booking, prefix, address))
                    if len(candidates) >= limit:
                        break
            if len(candidates) >= limit:
                break
        # 枠が余っていれば、出発地点の座標が欠けている配達者も候補に足す
        remaining = limit - len(candidates)
        if remaining:
            for driver in DriverProfile.objects.filter(
                Q(departure_latitude__isnull=True)
                | Q(departure_longitude__isnull=True)
            ).exclude(departure_address='')[:remaining]:
                candidates.append((driver, 'departure', driver.departure_address))

        self.stdout.write(
            f'座標未設定の地点: {len(candidates)}件; '
            f'モード={"apply" if apply else "dry-run"}'
        )
        # --apply なしでは件数確認だけで終了（API 課金・DB 書き込みなし）
        if not apply:
            self.stdout.write(
                '--apply を付けて再実行すると、Geocoding API を呼び出して結果を保存します。'
            )
            return
        api_key = getattr(settings, 'GOOGLE_GEOCODING_API_KEY', '')
        if not api_key:
            raise CommandError('GOOGLE_GEOCODING_API_KEY が設定されていません')

        updated = failed = 0
        # 同一住所・Place ID の重複呼び出しを避ける（プロセス内キャッシュ）
        geocode_cache = {}
        for obj, prefix, address in candidates:
            try:
                place_id = getattr(obj, f'{prefix}_place_id', '')
                cache_key = place_id or address
                result = geocode_cache.get(cache_key)
                if result is None:
                    result = geocode(address, place_id=place_id)
                    geocode_cache[cache_key] = result
                    if options['delay'] > 0:
                        time.sleep(options['delay'])
                setattr(obj, f'{prefix}_latitude', result.latitude)
                setattr(obj, f'{prefix}_longitude', result.longitude)
                if prefix != 'departure':
                    # 予約: 座標に加え Place ID・郵便番号・検証ステータスも更新
                    setattr(obj, f'{prefix}_place_id', result.place_id)
                    postal_code = next((
                        component.get('long_name', '')
                        for component in result.address_components
                        if 'postal_code' in component.get('types', [])
                    ), '')
                    if postal_code:
                        setattr(obj, f'{prefix}_postal_code', postal_code)
                    setattr(obj, f'{prefix}_geocode_status', 'verified')
                    fields = [
                        f'{prefix}_latitude', f'{prefix}_longitude',
                        f'{prefix}_place_id', f'{prefix}_postal_code',
                        f'{prefix}_geocode_status',
                        'updated_at',
                    ]
                else:
                    # 配達者出発地点: 座標と Place ID のみ
                    obj.departure_place_id = result.place_id
                    fields = [
                        'departure_latitude', 'departure_longitude',
                        'departure_place_id', 'updated_at',
                    ]
                obj.save(update_fields=fields)
                updated += 1
            except GeocodingNotFound:
                failed += 1
                # 見つからない住所は failed を残す（座標は空のままなので再実行でも候補になり得る）
                if prefix != 'departure':
                    setattr(obj, f'{prefix}_geocode_status', 'failed')
                    obj.save(update_fields=[f'{prefix}_geocode_status', 'updated_at'])
            except GeocodingServiceError:
                failed += 1
        self.stdout.write(self.style.SUCCESS(
            f'更新 {updated}件; 失敗 {failed}件。再実行しても安全です。'
        ))
