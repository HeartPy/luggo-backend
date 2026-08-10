from django.urls import path, URLPattern

from . import views

urlpatterns: list[URLPattern] = [
    path('auth/login', views.driver_login, name='driver_login'),
]
