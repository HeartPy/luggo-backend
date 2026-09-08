"""Stripe business_profile.url 解決の単体テスト"""
from django.test import SimpleTestCase

from business_owners.utils import (
    build_stripe_business_profile_url,
    resolve_stripe_business_profile_url,
)


class ResolveStripeBusinessProfileUrlTest(SimpleTestCase):
    """localhost URL を公開テナント URL へ置き換える"""

    def test_public_url_is_unchanged(self) -> None:
        # Arrange: すでに公開可能な URL
        url = 'https://example.com/booking'

        # Act: Stripe 向け URL を解決する
        result = resolve_stripe_business_profile_url(url, 'acme')

        # Assert: 公開 URL はそのまま返る
        self.assertEqual(result, url)

    def test_localhost_is_rewritten_with_subdomain_arg(self) -> None:
        # Arrange: 開発用 localhost URL と subdomain 引数
        url = 'http://localhost:3000?subdomain=acme'

        # Act: subdomain 引数付きで解決する
        result = resolve_stripe_business_profile_url(url, 'acme')

        # Assert: 公開テナント URL に置き換わる
        self.assertEqual(result, 'https://acme.luggo.delivery')

    def test_localhost_uses_query_subdomain_when_arg_missing(self) -> None:
        # Arrange: subdomain 引数なしの開発用 URL（クエリに subdomain あり）
        url = 'http://127.0.0.1:3000/?subdomain=demo'

        # Act: subdomain 引数なしで解決する
        result = resolve_stripe_business_profile_url(url, None)

        # Assert: クエリの subdomain から公開 URL を組み立てる
        self.assertEqual(result, 'https://demo.luggo.delivery')

    def test_localhost_without_subdomain_is_unchanged(self) -> None:
        # Arrange: subdomain がどこにも無い localhost URL
        url = 'http://localhost:3000/'

        # Act: 置き換え先が分からない状態で解決する
        result = resolve_stripe_business_profile_url(url, None)

        # Assert: 無理に変えず入力のまま返す
        self.assertEqual(result, url)

    def test_build_stripe_business_profile_url(self) -> None:
        # Arrange: 公開 URL を組み立てる subdomain
        subdomain = 'sampleco'

        # Act: Stripe 向け公開テナント URL を組み立てる
        result = build_stripe_business_profile_url(subdomain)

        # Assert: https://{subdomain}.luggo.delivery 形式になる
        self.assertEqual(result, 'https://sampleco.luggo.delivery')
