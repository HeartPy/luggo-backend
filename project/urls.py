from django.contrib import admin
from django.urls import path, include, URLPattern, URLResolver
from django.conf import settings
from django.conf.urls.static import static


# Admin は DJANGO_ADMIN_PATH で変更可能
_admin_path = f"{settings.DJANGO_ADMIN_PATH}/"

urlpatterns: list[URLPattern | URLResolver] = [
    path(_admin_path, admin.site.urls),
    path("api/common/", include('project.urls_common')),
    path("api/users/", include('users.urls')),
    path("api/bookings/", include('bookings.urls')),
    path("api/business/", include('business_owners.urls')),
    path("api/drivers/", include('drivers.urls')),
]

# 開発環境でのメディアファイル配信
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
