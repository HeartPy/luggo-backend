from django.test import SimpleTestCase

from business_owners.stripe_info import _extract_from_account


class StripeCompanyNameTests(SimpleTestCase):
    def test_extracts_english_company_name_for_company_account(self):
        # Arrange: 英語社名を持つ法人アカウント情報を用意
        # Act: Stripeアカウントから表示用情報を抽出
        info = _extract_from_account(
            {
                "business_type": "company",
                "business_profile": {},
                "company": {
                    "name": "LugGo Transport Inc.",
                    "address_kanji": {},
                },
            },
            "acct_test",
            include_representative=False,
        )

        # Assert: 英語社名が抽出されることを確認
        self.assertEqual(info["company_name_en"], "LugGo Transport Inc.")

    def test_does_not_extract_company_name_for_individual_account(self):
        # Arrange: 会社情報を持たない個人事業主アカウント情報を用意
        # Act: Stripeアカウントから表示用情報を抽出
        info = _extract_from_account(
            {
                "business_type": "individual",
                "business_profile": {},
                "individual": {"address_kanji": {}},
            },
            "acct_test",
            include_representative=False,
        )

        # Assert: 英語社名は空であることを確認
        self.assertEqual(info["company_name_en"], "")
