"""事業者サブドメイン Origin の CORS 許可パターン（本番用）"""

# サブドメインは英小文字 3〜12 文字
TENANT_SUBDOMAIN_ORIGIN_REGEX = r"^https://[a-z]{3,12}\.luggo\.delivery$"
