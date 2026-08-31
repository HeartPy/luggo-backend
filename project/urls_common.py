from django.urls import path, URLPattern
from . import views


urlpatterns: list[URLPattern] = [
    # 外形監視用ヘルスチェック
    path('health', views.health, name='health'),

    # CSRF関連
    path('csrf', views.get_csrf_token, name='get_csrf_token'),

    # セッション管理
    path('session/start', views.start_session, name='start_session'),
    path('session/check', views.check_session_validity, name='check_session_validity'),
]
