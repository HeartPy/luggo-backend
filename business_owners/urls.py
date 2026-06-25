from django.urls import path, URLPattern
from . import views
from bookings import owner_views as booking_owner_views


urlpatterns: list[URLPattern] = [
    # ログイン済み事業者向け
    path('profile', views.get_current_business_profile, name='get_current_business_profile'),

    # 予約一覧
    path('bookings', booking_owner_views.list_bookings, name='business_bookings_list'),
    path('bookings/statuses', booking_owner_views.update_booking_statuses, name='business_bookings_update_statuses'),
    path('bookings/drivers', booking_owner_views.assign_booking_drivers, name='business_bookings_assign_drivers'),
    path('bookings/cancel', booking_owner_views.cancel_bookings, name='business_bookings_cancel'),
    path('bookings/export', booking_owner_views.export_bookings_csv, name='business_bookings_export'),
    path('bookings/<uuid:booking_id>', booking_owner_views.update_booking, name='business_bookings_update'),
    path('drivers', booking_owner_views.list_drivers, name='business_drivers_list'),
    path('profile/pricing', views.update_profile_pricing, name='update_profile_pricing'),
    path('profile/pricing/draft', views.pricing_draft, name='pricing_draft'),
    path('profile/settings', views.business_settings, name='business_settings'),
    path('profile/settings/draft', views.business_settings_draft, name='business_settings_draft'),
    path('profile/public-info-consent', views.record_public_info_consent, name='record_public_info_consent'),
    path('profile/policy-agreement', views.record_policy_agreement, name='record_policy_agreement'),
    path('profile/booking-template-acknowledge', views.record_booking_template_acknowledge, name='record_booking_template_acknowledge'),
    path('stripe/custom/account', views.custom_get_account, name='stripe_custom_get_account'),
    path('stripe/custom/create-account', views.custom_create_account, name='stripe_custom_create_account'),
    path('stripe/custom/update-account', views.custom_update_account, name='stripe_custom_update_account'),
    path('stripe/custom/requirements', views.custom_account_requirements, name='stripe_custom_account_requirements'),
    path('stripe/custom/upload-document', views.custom_upload_document, name='stripe_custom_upload_document'),

    # 新規登録
    path('account/register/check-email', views.check_email_availability, name='check_email_availability'),
    path('account/register/request', views.request_registration_email, name='request_registration_email'),
    path('account/register/verify', views.verify_registration_token_api, name='verify_registration_token'),
    path('account/register', views.register_business_account, name='register_business_account'),

    # サブドメイン関連
    path('subdomain/profile', views.get_business_profile_by_subdomain, name='get_business_profile_by_subdomain'),
    path('subdomain/transaction-law', views.get_transaction_law_by_subdomain, name='get_transaction_law_by_subdomain'),
]
