from django.urls import path, URLPattern
from . import views

app_name = 'bookings'

urlpatterns: list[URLPattern] = [
    path('daily-remaining', views.daily_remaining, name='daily-remaining'),
    path('luggage-items', views.luggage_items, name='luggage-items'),
    path('location-suggestions', views.location_suggestions, name='location-suggestions'),
    path('create-payment-intent', views.create_payment_intent, name='create-payment-intent'),
    path('stripe/webhook', views.stripe_webhook, name='stripe-webhook'),
    path('lookup', views.lookup_booking, name='booking-lookup'),
    path('', views.LuggageBookingCreateView.as_view(), name='booking-create'),
    path('<uuid:pk>', views.LuggageBookingDetailView.as_view(), name='booking-detail'),
    path('<uuid:booking_id>/cancel', views.cancel_booking, name='cancel-booking'),
    path('<uuid:booking_id>/receipt', views.download_receipt, name='booking-receipt'),
]