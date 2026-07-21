"""
前月25日を基準に、休日ずれを含む実際の入金の請求書・支払明細をメール送信

毎月1日 09:00（Asia/Tokyo）の実行例:
  0 9 1 * * cd /app && python manage.py send_monthly_payout_documents
"""

from datetime import date

from django.core.management.base import BaseCommand, CommandError

from business_owners.models import BusinessProfile
from business_owners.payout_documents import (
    previous_month_payout_date,
    process_profile_payouts,
)


class Command(BaseCommand):
    help = '前月25日のStripe Payoutに対する請求書・支払明細を送信する'

    def add_arguments(self, parser):
        parser.add_argument(
            '--target-date',
            help='処理対象のPayout作成日（YYYY-MM-DD）。省略時は前月25日。',
        )
        parser.add_argument(
            '--profile-id',
            help='指定した事業者だけ処理する（障害時の再実行用）。',
        )
        parser.add_argument(
            '--no-send',
            action='store_true',
            help='Stripe照合と送付レコード作成のみ行い、メールは送信しない。',
        )

    def handle(self, *args, **options):
        target_date = self._target_date(options.get('target_date'))
        profiles = BusinessProfile.active.exclude(
            stripe_account_id__isnull=True
        ).exclude(
            stripe_account_id=''
        ).exclude(
            company_email=''
        ).order_by('id')

        profile_id = options.get('profile_id')
        if profile_id:
            profiles = profiles.filter(pk=profile_id)
            if not profiles.exists():
                raise CommandError('指定された事業者が見つかりません。')

        payout_count = 0
        sent_count = 0
        errs: list[str] = []
        for profile in profiles.iterator():
            try:
                found, sent = process_profile_payouts(
                    profile,
                    target_date,
                    send=not options['no_send'],
                )
            except Exception as e:
                errs.append(f'{profile.id}: {e}')
                self.stderr.write(
                    self.style.ERROR(
                        f'事業者 {profile.id} の処理に失敗しました: {e}'
                    )
                )
                continue
            payout_count += found
            sent_count += sent

        self.stdout.write(
            self.style.SUCCESS(
                f'基準日 {target_date} の入金データを {payout_count} 件処理し、'
                f'メールを {sent_count} 件送信しました。'
            )
        )
        if errs:
            raise CommandError(f'{len(errs)}事業者の処理に失敗しました。')

    @staticmethod
    def _target_date(raw_value: str | None) -> date:
        if not raw_value:
            return previous_month_payout_date()
        try:
            return date.fromisoformat(raw_value)
        except ValueError as e:
            raise CommandError(
                '--target-date は YYYY-MM-DD 形式で指定してください。'
            ) from e
