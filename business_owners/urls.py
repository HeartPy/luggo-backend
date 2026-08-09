from django.urls import path, URLPattern
from . import views
from bookings import owner_views as booking_owner_views
from drivers import owner_views as driver_owner_views
from routing import views as routing_views


urlpatterns: list[URLPattern] = [
    # ログイン済み事業者: 基本プロフィール
    path('profile', views.get_current_business_profile, name='get_current_business_profile'),

    # 予約管理
    path('bookings', booking_owner_views.list_bookings, name='business_bookings_list'),
    path('bookings/statuses', booking_owner_views.update_booking_statuses, name='business_bookings_update_statuses'),
    path('bookings/drivers', booking_owner_views.assign_booking_drivers, name='business_bookings_assign_drivers'),
    path('bookings/cancel', booking_owner_views.cancel_bookings, name='business_bookings_cancel'),
    path('bookings/export', booking_owner_views.export_bookings_csv, name='business_bookings_export'),
    path('bookings/<uuid:booking_id>', booking_owner_views.update_booking, name='business_bookings_update'),

    # 配達者（予約への割当選択用一覧）
    path('drivers', booking_owner_views.list_drivers, name='business_drivers_list'),

    # 配達者管理
    path('drivers/manage', driver_owner_views.manage_drivers, name='business_drivers_manage'),
    path('drivers/manage/<uuid:driver_id>', driver_owner_views.manage_driver_detail, name='business_drivers_manage_detail'),
    path('drivers/manage/<uuid:driver_id>/assign', driver_owner_views.assign_bookings_to_driver, name='business_drivers_assign'),
    path('drivers/manage/<uuid:driver_id>/unassign', driver_owner_views.unassign_bookings_from_driver, name='business_drivers_unassign'),

    # 配達者招待
    path('drivers/invitation/verify', driver_owner_views.verify_driver_invitation_api, name='driver_invitation_verify'),
    path('drivers/invitation/accept', driver_owner_views.accept_driver_invitation, name='driver_invitation_accept'),

    # ルート最適化・自動割当
    path('routing/assign', routing_views.assign, name='routing_assign'),
    path('routing/estimate', routing_views.estimate, name='routing_estimate'),
    path('routing/runs/<uuid:run_id>', routing_views.run_detail, name='routing_run_detail'),
    path('routing/runs/<uuid:run_id>/apply', routing_views.apply_run, name='routing_run_apply'),
    path('routing/daily', routing_views.daily, name='routing_daily'),

    # 売上
    path('revenue', views.revenue_summary, name='business_revenue_summary'),

    # 事業者設定（料金・表示・請求・同意）
    path('profile/pricing', views.update_profile_pricing, name='update_profile_pricing'),
    path('profile/pricing/draft', views.pricing_draft, name='pricing_draft'),
    path('profile/settings', views.business_settings, name='business_settings'),
    path('profile/settings/draft', views.business_settings_draft, name='business_settings_draft'),
    path('profile/invoice', views.business_invoice_settings, name='business_invoice_settings'),
    path('profile/public-info-consent', views.record_public_info_consent, name='record_public_info_consent'),
    path('profile/policy-agreement', views.record_policy_agreement, name='record_policy_agreement'),
    path('profile/booking-template-acknowledge', views.record_booking_template_acknowledge, name='record_booking_template_acknowledge'),

    # Stripe Connect
    path('stripe/custom/account', views.custom_get_account, name='stripe_custom_get_account'),
    path('stripe/custom/create-account', views.custom_create_account, name='stripe_custom_create_account'),
    path('stripe/custom/update-account', views.custom_update_account, name='stripe_custom_update_account'),
    path('stripe/custom/requirements', views.custom_account_requirements, name='stripe_custom_account_requirements'),
    path('stripe/custom/upload-document', views.custom_upload_document, name='stripe_custom_upload_document'),

    # 事業者新規登録
    path('account/register/check-email', views.check_email_availability, name='check_email_availability'),
    path('account/register/request', views.request_registration_email, name='request_registration_email'),
    path('account/register/verify', views.verify_registration_token_api, name='verify_registration_token'),
    path('account/register', views.register_business_account, name='register_business_account'),

    # 公開サブドメイン向け
    path('subdomain/profile', views.get_business_profile_by_subdomain, name='get_business_profile_by_subdomain'),
    path('subdomain/transaction-law', views.get_transaction_law_by_subdomain, name='get_transaction_law_by_subdomain'),
]
