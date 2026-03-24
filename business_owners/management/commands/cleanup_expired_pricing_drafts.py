"""
2週間以上経過した料金設定ドラフトを削除するマネジメントコマンド

使い方:
  python manage.py cleanup_expired_pricing_drafts

cron 等で定期実行する想定（例: 毎日深夜に実行）
"""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from business_owners.models import BusinessProfile


DRAFT_EXPIRY_DAYS = 14


class Command(BaseCommand):
    help = "2週間以上経過した料金設定の一時保存データを削除する"

    def handle(self, *args, **options):
        cutoff = timezone.now() - timedelta(days=DRAFT_EXPIRY_DAYS)
        expired = BusinessProfile.objects.filter(
            pricing_draft__isnull=False,
            pricing_draft_saved_at__isnull=False,
            pricing_draft_saved_at__lt=cutoff,
        )
        count = expired.update(
            pricing_draft=None,
            pricing_draft_saved_at=None,
        )
        self.stdout.write(
            self.style.SUCCESS(f"期限切れの一時保存データを {count} 件削除しました。")
        )
