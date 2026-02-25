from django.urls import path, URLPattern
from . import views


urlpatterns: list[URLPattern] = [
    # 認証関連
    path('auth/check', views.check_authentication, name='check_authentication'),
    path('auth/send-login-code', views.send_login_code, name='send_login_code'),
    path('auth/verify-login-code', views.verify_login_code_api, name='verify_login_code'),
    path('auth/logout', views.logout_api, name='logout'),

    # パスワード再設定
    path('password/request-reset', views.request_password_reset, name='request_password_reset'),
    path('password/verify-token', views.verify_password_reset_token_api, name='verify_password_reset_token'),
    path('password/reset', views.reset_password, name='reset_password'),
]
