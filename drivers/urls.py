from django.urls import path, URLPattern

from . import me_views, views

urlpatterns: list[URLPattern] = [
    path('auth/login', views.driver_login, name='driver_login'),
    path('me/bookings', me_views.my_bookings, name='driver_my_bookings'),
    path(
        'me/bookings/<uuid:booking_id>',
        me_views.my_booking_detail,
        name='driver_my_booking_detail',
    ),
    path('me/route', me_views.my_route, name='driver_my_route'),
]
