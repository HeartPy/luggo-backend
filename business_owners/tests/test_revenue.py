from datetime import datetime, timedelta
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from bookings.models import LuggageBooking
from users.models import User
from business_owners.models import BusinessProfile


class RevenueSummaryApiTests(TestCase):
    def setUp(self) -> None:
        # Arrange: テスト用ユーザー・事業者を作成し、認証済みクライアントを用意
        self.user = User.objects.create_user(
            email="owner@example.com",
            password="test-password",
            user_type="business_owner",
        )
        self.profile = BusinessProfile.objects.create(
            user=self.user,
            company_name="テスト事業者",
            company_email="owner@example.com",
            stripe_account_id="acct_test",
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def _booking(self, amount: int, payment_intent_id: str) -> LuggageBooking:
        return LuggageBooking.objects.create(
            business_owner=self.profile,
            pickup_location_name="集荷場所",
            pickup_location_address="東京都",
            pickup_date=timezone.localdate(),
            delivery_location_name="配送場所",
            delivery_location_address="東京都",
            delivery_date=timezone.localdate(),
            total_amount=amount,
            customer_name="旅行者",
            customer_email="guest@example.com",
            customer_phone_number="09012345678",
            customer_nationality="JPN",
            guest_name="宿泊者",
            payment_intent_id=payment_intent_id,
        )

    @patch("business_owners.views.list_payout_history")
    def test_returns_monthly_gross_and_net_sales(self, payout_mock) -> None:
        """対象月に配達完了し入金可能になった予約のみ集計される"""
        # Arrange: 入金履歴をモックし、通常予約と一部返金済み予約を用意
        payout_mock.return_value = ([], False)
        delivered = self._booking(10_000, "pi_delivered")
        refunded = self._booking(5_000, "pi_refunded")

        delivered_at = timezone.make_aware(datetime(2026, 7, 10, 12))
        available_at = delivered_at + timedelta(days=2)
        LuggageBooking.objects.filter(pk=delivered.pk).update(
            delivery_status="delivered",
            delivered_at=delivered_at,
            funds_available_on=available_at,
        )
        # 対象月に配達完了したが、一部返金済みの予約
        LuggageBooking.objects.filter(pk=refunded.pk).update(
            delivery_status="delivered",
            delivered_at=delivered_at,
            funds_available_on=available_at,
            refund_status=LuggageBooking.REFUND_STATUS_SUCCEEDED,
            refunded_amount=2_000,
            refunded_at=delivered_at,
        )

        # Act: 売上サマリー API を呼び出す
        response = self.client.get(
            reverse("business_revenue_summary"),
            {"month": "2026-07", "history_months": "3"},
        )

        # Assert: 売上・手数料・純売上が返金差し引き後の金額であることを確認
        self.assertEqual(response.status_code, 200)
        # 10,000 + (5,000 - 2,000) = 13,000
        self.assertEqual(response.data["gross_sales"], 13_000)
        # 1,000 + (500 - 200) = 1,300
        self.assertEqual(response.data["platform_fee"], 1_300)
        self.assertEqual(response.data["net_sales"], 11_700)
        payout_mock.assert_called_once_with(self.profile, 3)

    @patch("business_owners.views.list_payout_history")
    def test_excludes_undelivered_and_unavailable_bookings(self, payout_mock) -> None:
        """未配達・入金可能前・対象月外の予約は集計されない"""
        # Arrange: 集計対象外の予約と、唯一の集計対象予約を用意
        payout_mock.return_value = ([], False)
        in_month = timezone.make_aware(datetime(2026, 7, 10, 12))
        other_month = timezone.make_aware(datetime(2026, 6, 10, 12))

        # 配達未完了（集荷前のまま）
        self._booking(10_000, "pi_before_pickup")
        # 対象月に配達完了したが、資金がまだ入金可能になっていない
        pending_funds = self._booking(20_000, "pi_pending_funds")
        LuggageBooking.objects.filter(pk=pending_funds.pk).update(
            delivery_status="delivered",
            delivered_at=in_month,
            funds_available_on=timezone.now() + timedelta(days=5),
        )
        # 対象月外（前月）に配達完了
        other = self._booking(30_000, "pi_other_month")
        LuggageBooking.objects.filter(pk=other.pk).update(
            delivery_status="delivered",
            delivered_at=other_month,
            funds_available_on=other_month,
        )
        # 対象月に配達完了・入金可能（唯一の集計対象）
        counted = self._booking(40_000, "pi_counted")
        LuggageBooking.objects.filter(pk=counted.pk).update(
            delivery_status="delivered",
            delivered_at=in_month,
            funds_available_on=in_month,
        )

        # Act: 売上サマリー API を呼び出す
        response = self.client.get(
            reverse("business_revenue_summary"),
            {"month": "2026-07", "history_months": "3"},
        )

        # Assert: 集計対象の予約だけが売上に含まれることを確認
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["gross_sales"], 40_000)
        self.assertEqual(response.data["platform_fee"], 4_000)
        self.assertEqual(response.data["net_sales"], 36_000)

    def test_rejects_invalid_month(self) -> None:
        # Arrange: 不正な month パラメータを用意
        # Act: 売上サマリー API を呼び出す
        response = self.client.get(
            reverse("business_revenue_summary"),
            {"month": "2026-13"},
        )

        # Assert: バリデーションエラーが返ることを確認
        self.assertEqual(response.status_code, 400)

    def test_requires_authentication(self) -> None:
        # Arrange: 未認証状態にする
        self.client.force_authenticate(user=None)

        # Act: 売上サマリー API を呼び出す
        response = self.client.get(reverse("business_revenue_summary"))

        # Assert: 認証エラーが返ることを確認
        self.assertEqual(response.status_code, 403)
