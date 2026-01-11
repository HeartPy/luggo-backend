from django.urls import path, URLPattern
from . import views


urlpatterns: list[URLPattern] = [
    path('csrf', views.get_csrf_token, name='get_csrf_token'),
    path('session/start', views.start_session, name='start_session'),
    path('session/check', views.check_session_validity, name='check_session_validity'),
    path('auth/check', views.check_authentication, name='check_authentication'),
]
