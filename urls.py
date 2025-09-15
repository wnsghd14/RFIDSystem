from django.conf import settings
from django.conf.urls.static import static
from django.urls import re_path as url
from django.contrib import admin
from django.urls import path, include
from rest_framework import routers
from drf_yasg.views import get_schema_view
from drf_yasg import openapi
from rest_framework import permissions

#
# schema_url_v1_patterns = [
#     url(r'^sample/v1', include('sample.urls', namespace='sample')),
# ]

schema_view = get_schema_view(
    openapi.Info(
        title="PIE Healthcare Inventory API",
        default_version='v1',
        description="RFID 기반 의료용품 재고 관리 시스템 API",
        terms_of_service="https://www.piehealthcare.com/terms/",
        contact=openapi.Contact(email="support@piehealthcare.com"),
        license=openapi.License(name="MIT License"),
    ),
    public=True,
    permission_classes=(permissions.AllowAny,),
)

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/', include("inventory2.backend.apis.apis")),
    path('swagger/', schema_view.with_ui('swagger', cache_timeout=0), name='schema-swagger-ui'),
    path('redoc/', schema_view.with_ui('redoc', cache_timeout=0), name='schema-redoc'),
    path('', include('inventory2.front.urls')),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT) + static(settings.STATIC_URL,
                                                                                        document_root=settings.STATIC_ROOT)

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL,
                          document_root=settings.MEDIA_ROOT)
