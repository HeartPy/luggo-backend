from django.urls import path, URLPattern
from . import views


urlpatterns: list[URLPattern] = [
    # ログイン済み事業者向け
    path('stripe/custom/account', views.custom_get_account, name='bo_custom_get_account'),
    path('stripe/custom/update-account', views.custom_update_account, name='bo_custom_update_account'),
    path('stripe/custom/requirements', views.custom_account_requirements, name='bo_custom_account_requirements'),

    # 誰でも使える（セッションで管理）
    path('public/stripe/custom/create-or-get-account', views.public_custom_create_or_get_account, name='bo_public_custom_create_or_get_account'),
    path('public/stripe/custom/update-account', views.public_custom_update_account, name='bo_public_custom_update_account'),
    path('public/stripe/custom/requirements', views.public_custom_account_requirements, name='bo_public_custom_account_requirements'),
    path('public/stripe/custom/upload-document', views.public_custom_upload_verification_document, name='bo_public_custom_upload_document'),

    # 新規登録
    path('register', views.register_business_owner, name='register_business_owner'),
]
