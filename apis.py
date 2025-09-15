from django.urls import path, include
from rest_framework.routers import SimpleRouter

from inventory2.backend.views.default import DefaultInventoryViewSet, CompanyViewSet
from inventory2.backend.views.discrepancy import DiscrepancyViewSet
from inventory2.backend.views.inventory import InventoryViewSet
from inventory2.backend.views.performance import PerformanceViewSet
from inventory2.backend.views.rfidscan import RFIDScanViewSet
from inventory2.backend.views.specification import SpecificationsViewSet

router = SimpleRouter()
router.register(r"inventory", InventoryViewSet, basename="inventory")
router.register(r"specification", SpecificationsViewSet, basename="specifications")
router.register(r"rfid", RFIDScanViewSet, basename="rfid_scan", )
router.register(r'discrepancy', DiscrepancyViewSet, basename="discrepancy", )
router.register(r"default", DefaultInventoryViewSet, basename="default", )
router.register(r'companies', CompanyViewSet, basename='company')
router.register(r'performance', PerformanceViewSet, basename='performance')

urlpatterns = [
    path("", include(router.urls)),
]
