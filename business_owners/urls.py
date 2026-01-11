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
    path('account/register/check-email', views.check_email_availability, name='check_email_availability'),
    path('account/register/request', views.request_registration_email, name='request_registration_email'),
    path('account/register/verify', views.verify_registration_token_api, name='verify_registration_token'),
    path('account/register', views.register_business_account, name='register_business_account'),
]
