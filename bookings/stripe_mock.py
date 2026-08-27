"""
E2E テスト用の Stripe モック

settings.E2E_STRIPE_MOCK が真のときだけ使用する（DEBUG 時のみ有効化できる）。
Stripe SDK を呼ばずに「決済成功済み」の PaymentIntent 相当を返すことで、
Playwright の E2E がカード入力なしで予約完了まで到達できるようにする。
"""

import uuid
from typing import Optional

from django.conf import settings

from .models import PendingBooking


# モックの PaymentIntent ID に付ける接頭辞。実際の Stripe ID（pi_ + 英数字）と
# 混同しないよう、モック由来であることが ID から分かるようにする。
E2E_PAYMENT_INTENT_PREFIX = 'pi_e2e_'


def is_stripe_mock_enabled() -> bool:
    """E2E 用 Stripe モックが有効かどうか"""
    return bool(getattr(settings, 'E2E_STRIPE_MOCK', False))


class MockPaymentIntent:
    """stripe.PaymentIntent の代替（予約フローで参照する属性のみ持つ）"""

    def __init__(
        self,
        *,
        payment_intent_id: str,
        amount: int,
        metadata: dict[str, str],
    ) -> None:
        self.id = payment_intent_id
        self.client_secret = f'{payment_intent_id}_secret_mock'
        self.status = 'succeeded'
        self.amount = amount
        self.metadata = metadata


def create_mock_payment_intent(
    amount: int, metadata: dict[str, str]
) -> MockPaymentIntent:
    """成功済み扱いのモック PaymentIntent を新規発行"""
    return MockPaymentIntent(
        payment_intent_id=f'{E2E_PAYMENT_INTENT_PREFIX}{uuid.uuid4().hex}',
        amount=amount,
        metadata=metadata,
    )


def retrieve_mock_payment_intent(
    payment_intent_id: str,
) -> Optional[MockPaymentIntent]:
    """
    モック PaymentIntent を「取得」する

    Stripe 側にデータが存在しないため、create-payment-intent 時に保存された
    PendingBooking（payment_intent_id で紐づく）から金額と事業者を復元する。
    見つからない場合は None を返す（呼び出し側で決済確認エラーにする）。
    """
    pending = (
        PendingBooking.objects.filter(payment_intent_id=payment_intent_id)
        .select_related('business_owner')
        .first()
    )
    if pending is None or pending.business_owner_id is None:
        return None

    return MockPaymentIntent(
        payment_intent_id=payment_intent_id,
        amount=int(pending.total_amount),
        metadata={'business_owner_id': str(pending.business_owner_id)},
    )
