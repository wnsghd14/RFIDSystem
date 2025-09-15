from django.contrib import admin
from inventory2.backend.models.base import Company, Type, Date, DefaultInventory
from inventory2.backend.models.inventory import Inventory2
from inventory2.backend.models.discrepancy import InventoryDiscrepancy
from inventory2.backend.models.manufacturinghash import ManufacturingHash
from inventory2.backend.models.specification import Specification
from inventory2.backend.models.rfidscan import RFIDScan, EPCdata


# --- Inline 설정 ---
class DateInline(admin.TabularInline):
    model = Date
    extra = 0
    ordering = ['-date']


class Inventory2Inline(admin.TabularInline):
    model = Inventory2
    extra = 0
    ordering = ['expiry_date']
    fields = ['pie_healthcare_num', 'expiry_date', 'medication_lot_number', 'stock_quantity']
    readonly_fields = fields


# --- CompanyAdmin ---
@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    list_display = ('company_name', 'company_code')
    search_fields = ('company_name', 'company_code')
    filter_horizontal = ('available_type',)  # 다대다 관계

    inlines = [DateInline]


# --- TypeAdmin ---
@admin.register(Type)
class TypeAdmin(admin.ModelAdmin):
    list_display = ('name',)
    search_fields = ('name',)


# --- DateAdmin ---
@admin.register(Date)
class DateAdmin(admin.ModelAdmin):
    list_display = ('company', 'type', 'date', 'created_at')
    list_filter = ('company', 'type')
    search_fields = ('company__company_name', 'type__name', 'date')

    inlines = [Inventory2Inline]


@admin.register(DefaultInventory)
class DefaultInventoryAdmin(admin.ModelAdmin):
    list_display = ('pie_healthcare_num', 'expiry_date', 'medication_lot_number', 'stock_quantity')
    list_filter = ('expiry_date',)
    search_fields = ('pie_healthcare_num', 'medication_lot_number')


# --- Inventory2Admin ---
@admin.register(Inventory2)
class Inventory2Admin(admin.ModelAdmin):
    list_display = ('date', 'pie_healthcare_num', 'expiry_date', 'medication_lot_number', 'stock_quantity')
    list_filter = ('date',)
    search_fields = ('pie_healthcare_num', 'medication_lot_number')


# --- RFIDScanAdmin ---
@admin.register(RFIDScan)
class RFIDScanAdmin(admin.ModelAdmin):
    list_display = ( 'date', 'pie_healthcare_num', 'expiry_date', 'scanned_quantity',)
    list_filter = ('date',)
    search_fields = ('pie_healthcare_num',)


# --- EPCdataAdmin ---
@admin.register(EPCdata)
class EPCdataAdmin(admin.ModelAdmin):
    list_display = ('data', 'date')
    list_filter = ('date',)
    search_fields = ('data',)


# --- SpecificationAdmin ---
@admin.register(Specification)
class SpecificationAdmin(admin.ModelAdmin):
    list_display = ('date', 'medication_name', 'pie_healthcare_num', 'expiry_date')
    list_filter = ('date',)
    search_fields = ('pie_healthcare_num', 'medication_lot_number')


# --- InventoryDiscrepancyAdmin ---
@admin.register(InventoryDiscrepancy)
class InventoryDiscrepancyAdmin(admin.ModelAdmin):
    list_display = ('date', 'medication_name', 'pie_healthcare_num', 'expiry_date', 'reason')
    list_filter = ('created_at',)
    search_fields = ('reason', 'pie_healthcare_num', 'created_at')


admin.site.register(ManufacturingHash)
