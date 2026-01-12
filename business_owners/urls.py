from django.urls import path, URLPattern
from . import views


urlpatterns: list[URLPattern] = [
    # ログイン済み事業者向け
    path('stripe/custom/account', views.custom_get_account, name='stripe_custom_get_account'),
    path('stripe/custom/create-account', views.custom_create_account, name='stripe_custom_create_account'),
    path('stripe/custom/update-account', views.custom_update_account, name='stripe_custom_update_account'),
    path('stripe/custom/requirements', views.custom_account_requirements, name='stripe_custom_account_requirements'),

    # 新規登録
    path('account/register/check-email', views.check_email_availability, name='check_email_availability'),
    path('account/register/request', views.request_registration_email, name='request_registration_email'),
    path('account/register/verify', views.verify_registration_token_api, name='verify_registration_token'),
    path('account/register', views.register_business_account, name='register_business_account'),

    # サブドメイン関連
    path('subdomain/profile', views.get_business_profile_by_subdomain, name='get_business_profile_by_subdomain'),
]
