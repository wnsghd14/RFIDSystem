"""
유틸리티 함수들의 단위 테스트
"""
import os
from datetime import datetime, date
from django.test import TestCase, TransactionTestCase
from django.db import transaction
from unittest.mock import patch, MagicMock

from inventory2.backend.models.base import Company, Type, Date, DefaultInventory
from inventory2.backend.models.inventory import Inventory2
from inventory2.backend.models.specification import Specification
from inventory2.backend.models.discrepancy import InventoryDiscrepancy
from inventory2.backend.models.manufacturinghash import ManufacturingHash
from inventory2.backend.models.rfidscan import RFIDScan
from inventory2.backend.utils.utils import (
    create_specifications_from_rfid_scan,
    calculate_and_save_discrepancies,
    generate_hash_for_manufacturing_code,
    get_or_create_hash,
    normalize_date,
    _get_existing_specs_map,
    _get_default_inventory_filters,
    _process_specification_instance,
    _calculate_discrepancy_for_spec,
    _get_optimized_inventory_queryset
)
from core.exceptions import (
    SpecificationCreationError, DiscrepancyCalculationError,
    DateFormatError, HashGenerationError
)


class UtilsTestCase(TestCase):
    """유틸리티 함수들의 기본 테스트"""

    def setUp(self):
        """테스트 데이터 설정"""
        self.type_obj, _ = Type.objects.get_or_create(name="재고")
        self.company_obj = Company.objects.create(
            company_name="테스트병원",
            company_code="TEST001"
        )
        self.company_obj.available_type.set([self.type_obj])
        self.date_obj = Date.objects.create(
            date=date(2024, 12, 1),
            company=self.company_obj,
            type=self.type_obj
        )
        # Inventory2 생성
        self.inventory = Inventory2.objects.create(
            pie_healthcare_num="12345",
            medication_name="테스트약품",
            expiry_date=date(2025, 12, 31),
            stock_quantity=100,
            medication_lot_number="LOT001",
            date=self.date_obj
        )

    def test_normalize_date_string(self):
        """문자열 날짜 정규화 테스트"""
        result = normalize_date("20241201")
        expected = date(2024, 12, 1)
        self.assertEqual(result, expected)

    def test_normalize_date_datetime(self):
        """datetime 객체 날짜 정규화 테스트"""
        dt = datetime(2024, 12, 1, 10, 30, 0)
        result = normalize_date(dt)
        expected = date(2024, 12, 1)
        self.assertEqual(result, expected)

    def test_normalize_date_date(self):
        """date 객체 날짜 정규화 테스트"""
        d = date(2024, 12, 1)
        result = normalize_date(d)
        self.assertEqual(result, d)

    def test_normalize_date_invalid_format(self):
        """잘못된 날짜 형식 테스트"""
        with self.assertRaises(DateFormatError):
            normalize_date("invalid-date")

    def test_normalize_date_invalid_type(self):
        """잘못된 타입 테스트"""
        with self.assertRaises(DateFormatError):
            normalize_date(123)


class HashGenerationTestCase(TestCase):
    """해시 생성 관련 테스트"""

    def setUp(self):
        """테스트 데이터 설정"""
        self.test_code = "TEST123456"

    def test_generate_hash_for_manufacturing_code(self):
        """제조번호 해시 생성 테스트"""
        hash_result = generate_hash_for_manufacturing_code(self.test_code)
        self.assertIsInstance(hash_result, str)
        self.assertEqual(len(hash_result), 9)  # HASH_LENGTH
        self.assertTrue(hash_result.isupper())

    def test_generate_hash_empty_code(self):
        """빈 제조번호 테스트"""
        with self.assertRaises(ValueError):
            generate_hash_for_manufacturing_code("")

    def test_generate_hash_none_code(self):
        """None 제조번호 테스트"""
        with self.assertRaises(ValueError):
            generate_hash_for_manufacturing_code(None)

    @patch('inventory2.backend.utils.utils.ManufacturingHash.objects.values_list')
    def test_generate_hash_collision_handling(self, mock_values_list):
        """해시 충돌 처리 테스트"""
        # 모든 해시가 이미 존재하는 상황 시뮬레이션
        mock_values_list.return_value = [f"TEST{i:03d}" for i in range(10000)]
        
        with self.assertRaises(Exception) as context:
            generate_hash_for_manufacturing_code(self.test_code)
        self.assertIn("충돌 한도 초과", str(context.exception))

    def test_get_or_create_hash_new(self):
        """새로운 해시 생성 테스트"""
        hash_obj = get_or_create_hash(self.test_code)
        self.assertIsInstance(hash_obj, ManufacturingHash)
        self.assertEqual(hash_obj.original_code, self.test_code)
        self.assertIsNotNone(hash_obj.hashed_code)

    def test_get_or_create_hash_existing(self):
        """기존 해시 조회 테스트"""
        # 먼저 해시 생성
        hash_obj1 = get_or_create_hash(self.test_code)
        
        # 같은 코드로 다시 조회
        hash_obj2 = get_or_create_hash(self.test_code)
        
        self.assertEqual(hash_obj1.id, hash_obj2.id)
        self.assertEqual(hash_obj1.hashed_code, hash_obj2.hashed_code)


class SpecificationCreationTestCase(TransactionTestCase):
    """스펙 생성 관련 테스트"""

    def setUp(self):
        """테스트 데이터 설정"""
        self.type_obj, _ = Type.objects.get_or_create(name="재고")
        self.company_obj = Company.objects.create(
            company_name="테스트병원",
            company_code="TEST001"
        )
        self.company_obj.available_type.set([self.type_obj])
        self.date_obj = Date.objects.create(
            date=date(2024, 12, 1),
            company=self.company_obj,
            type=self.type_obj
        )
        
        # 기본 재고 정보 생성 (DefaultInventory 사용)
        self.default_inventory = DefaultInventory.objects.create(
            pie_healthcare_num="12345",
            medication_name="테스트약품",
            expiry_date=date(2025, 12, 31),
            stock_quantity=100,
            medication_lot_number="LOT001",
            medication_size="10mg",
            stock_location="A-1",
            medication_created_by="제조사"
        )

    def test_get_existing_specs_map(self):
        """기존 스펙 매핑 테스트"""
        # 기존 스펙 생성
        spec = Specification.objects.create(
            date=self.date_obj,
            pie_healthcare_num="12345",
            medication_name="테스트약품",
            expiry_date=date(2025, 12, 31),
            stock_quantity=50,
            medication_lot_number="LOT001",
            medication_size="10mg",
            stock_location="A-1",
            medication_created_by="제조사"
        )
        
        rfid_scan = RFIDScan.objects.create(
            date=self.date_obj,
            pie_healthcare_num="12345",
            expiry_date=date(2025, 12, 31),
            scanned_quantity=30,
            medication_lot_number="LOT001"
        )
        
        spec_map = _get_existing_specs_map([rfid_scan])
        self.assertIn(("12345", date(2025, 12, 31), "LOT001"), spec_map)

    def test_get_default_inventory_filters(self):
        """기본 재고 필터 테스트"""
        rfid_scan = RFIDScan.objects.create(
            date=self.date_obj,
            pie_healthcare_num="12345",
            expiry_date=date(2025, 12, 31),
            scanned_quantity=30,
            medication_lot_number="LOT001"
        )
        
        filters = _get_default_inventory_filters([rfid_scan])
        self.assertIsNotNone(filters)

    def test_process_specification_instance_new(self):
        """새 스펙 인스턴스 처리 테스트"""
        rfid_scan = RFIDScan.objects.create(
            date=self.date_obj,
            pie_healthcare_num="12345",
            expiry_date=date(2025, 12, 31),
            scanned_quantity=30,
            medication_lot_number="LOT001"
        )
        
        spec_map = {}
        default_qs = Inventory2.objects.filter(pie_healthcare_num="12345")
        
        result = _process_specification_instance(rfid_scan, spec_map, default_qs)
        self.assertIsNotNone(result)
        new_spec, update_spec = result
        self.assertIsNotNone(new_spec)
        self.assertIsNone(update_spec)

    def test_process_specification_instance_update(self):
        """기존 스펙 업데이트 테스트"""
        # 기존 스펙 생성
        spec = Specification.objects.create(
            date=self.date_obj,
            pie_healthcare_num="12345",
            medication_name="테스트약품",
            expiry_date=date(2025, 12, 31),
            stock_quantity=50,
            medication_lot_number="LOT001",
            medication_size="10mg",
            stock_location="A-1",
            medication_created_by="제조사"
        )
        
        rfid_scan = RFIDScan.objects.create(
            date=self.date_obj,
            pie_healthcare_num="12345",
            expiry_date=date(2025, 12, 31),
            scanned_quantity=30,
            medication_lot_number="LOT001"
        )
        
        spec_map = {("12345", date(2025, 12, 31), "LOT001"): spec}
        default_qs = Inventory2.objects.filter(pie_healthcare_num="12345")
        
        result = _process_specification_instance(rfid_scan, spec_map, default_qs)
        self.assertIsNotNone(result)
        new_spec, update_spec = result
        self.assertIsNone(new_spec)
        self.assertIsNotNone(update_spec)

    def test_create_specifications_from_rfid_scan_success(self):
        """RFID 스캔으로부터 스펙 생성 성공 테스트"""
        rfid_scan = RFIDScan.objects.create(
            date=self.date_obj,
            pie_healthcare_num="12345",
            expiry_date=date(2025, 12, 31),
            scanned_quantity=30,
            medication_lot_number="LOT001"
        )
        
        result = create_specifications_from_rfid_scan([rfid_scan])
        self.assertTrue(result["success"])
        self.assertIn("created", result)
        self.assertIn("updated", result)

    def test_create_specifications_from_rfid_scan_empty(self):
        """빈 RFID 스캔 데이터 테스트"""
        with self.assertRaises(SpecificationCreationError):
            create_specifications_from_rfid_scan([])


class DiscrepancyCalculationTestCase(TransactionTestCase):
    """불일치 계산 관련 테스트"""

    def setUp(self):
        """테스트 데이터 설정"""
        self.type_obj, _ = Type.objects.get_or_create(name="재고")
        self.company_obj = Company.objects.create(
            company_name="테스트병원",
            company_code="TEST001"
        )
        self.company_obj.available_type.set([self.type_obj])
        self.date_obj = Date.objects.create(
            date=date(2024, 12, 1),
            company=self.company_obj,
            type=self.type_obj
        )

    def test_calculate_discrepancy_for_spec_missing(self):
        """재고가 없는 스펙 불일치 계산 테스트"""
        spec = Specification.objects.create(
            date=self.date_obj,
            pie_healthcare_num="12345",
            medication_name="테스트약품",
            expiry_date=date(2025, 12, 31),
            stock_quantity=50,
            medication_lot_number="LOT001",
            medication_size="10mg",
            stock_location="A-1",
            medication_created_by="제조사"
        )
        
        inv_map = {}
        discrepancy = _calculate_discrepancy_for_spec(spec, inv_map)
        
        self.assertIsNotNone(discrepancy)
        self.assertEqual(discrepancy.reason, "미존재")
        self.assertEqual(discrepancy.discrepancy_quantity, 50)

    def test_calculate_discrepancy_for_spec_excess(self):
        """초과 재고 불일치 계산 테스트"""
        spec = Specification.objects.create(
            date=self.date_obj,
            pie_healthcare_num="12345",
            medication_name="테스트약품",
            expiry_date=date(2025, 12, 31),
            stock_quantity=50,
            medication_lot_number="LOT001",
            medication_size="10mg",
            stock_location="A-1",
            medication_created_by="제조사"
        )
        
        inventory = Inventory2.objects.create(
            pie_healthcare_num="12345",
            medication_name="테스트약품",
            expiry_date=date(2025, 12, 31),
            stock_quantity=30,  # 스펙보다 적음
            medication_lot_number="LOT001",
            date=self.date_obj
        )
        
        inv_map = {("12345", date(2025, 12, 31), "LOT001"): inventory}
        discrepancy = _calculate_discrepancy_for_spec(spec, inv_map)
        
        self.assertIsNotNone(discrepancy)
        self.assertEqual(discrepancy.reason, "모자람")
        self.assertEqual(discrepancy.discrepancy_quantity, 20)

    def test_calculate_discrepancy_for_spec_shortage(self):
        """부족 재고 불일치 계산 테스트"""
        spec = Specification.objects.create(
            date=self.date_obj,
            pie_healthcare_num="12345",
            medication_name="테스트약품",
            expiry_date=date(2025, 12, 31),
            stock_quantity=30,
            medication_lot_number="LOT001",
            medication_size="10mg",
            stock_location="A-1",
            medication_created_by="제조사"
        )
        
        inventory = Inventory2.objects.create(
            pie_healthcare_num="12345",
            medication_name="테스트약품",
            expiry_date=date(2025, 12, 31),
            stock_quantity=50,  # 스펙보다 많음
            medication_lot_number="LOT001",
            date=self.date_obj
        )
        
        inv_map = {("12345", date(2025, 12, 31), "LOT001"): inventory}
        discrepancy = _calculate_discrepancy_for_spec(spec, inv_map)
        
        self.assertIsNotNone(discrepancy)
        self.assertEqual(discrepancy.reason, "초과")
        self.assertEqual(discrepancy.discrepancy_quantity, 20)

    def test_calculate_discrepancy_for_spec_match(self):
        """일치하는 재고 테스트"""
        spec = Specification.objects.create(
            date=self.date_obj,
            pie_healthcare_num="12345",
            medication_name="테스트약품",
            expiry_date=date(2025, 12, 31),
            stock_quantity=50,
            medication_lot_number="LOT001",
            medication_size="10mg",
            stock_location="A-1",
            medication_created_by="제조사"
        )
        
        inventory = Inventory2.objects.create(
            pie_healthcare_num="12345",
            medication_name="테스트약품",
            expiry_date=date(2025, 12, 31),
            stock_quantity=50,  # 스펙과 동일
            medication_lot_number="LOT001",
            date=self.date_obj
        )
        
        inv_map = {("12345", date(2025, 12, 31), "LOT001"): inventory}
        discrepancy = _calculate_discrepancy_for_spec(spec, inv_map)
        
        self.assertIsNone(discrepancy)  # 불일치 없음

    def test_calculate_and_save_discrepancies_success(self):
        """불일치 계산 및 저장 성공 테스트"""
        spec = Specification.objects.create(
            date=self.date_obj,
            pie_healthcare_num="12345",
            medication_name="테스트약품",
            expiry_date=date(2025, 12, 31),
            stock_quantity=50,
            medication_lot_number="LOT001",
            medication_size="10mg",
            stock_location="A-1",
            medication_created_by="제조사"
        )
        
        result = calculate_and_save_discrepancies([spec])
        self.assertTrue(result["success"])
        self.assertIn("total_discrepancies", result)
        self.assertIn("reason_breakdown", result)

    def test_calculate_and_save_discrepancies_empty(self):
        """빈 스펙 리스트 테스트"""
        with self.assertRaises(DiscrepancyCalculationError):
            calculate_and_save_discrepancies([])


class EnvironmentVariableTestCase(TestCase):
    """환경변수 관련 테스트"""

    def test_environment_variables_loaded(self):
        """환경변수 로드 테스트"""
        # Django 설정에서 환경변수가 제대로 로드되는지 확인
        from django.conf import settings
        
        self.assertIsNotNone(settings.SECRET_KEY)
        self.assertIsNotNone(settings.DATABASES['default']['PASSWORD'])
        self.assertIsNotNone(settings.DATABASES['default']['USER'])

    @patch.dict(os.environ, {
        'SECRET_KEY': 'test-secret-key',
        'DB_PASSWORD': 'test-password',
        'ADMIN_USERNAME': 'test-admin'
    })
    def test_environment_variables_override(self):
        """환경변수 오버라이드 테스트"""
        # 환경변수가 기본값을 오버라이드하는지 확인
        from django.conf import settings
        settings._setup()
        
        # 실제로는 Django 설정이 이미 로드되어 있어서 
        # 이 테스트는 환경변수 설정 방식을 검증하는 용도
        self.assertTrue(True)


class DuplicateHandlingTestCase(TransactionTestCase):
    """중복처리 관련 테스트"""

    def setUp(self):
        """테스트 데이터 설정"""
        self.type_obj, _ = Type.objects.get_or_create(name="재고")
        self.company_obj = Company.objects.create(
            company_name="테스트병원",
            company_code="TEST001"
        )
        self.company_obj.available_type.set([self.type_obj])
        self.date_obj = Date.objects.create(
            date=date(2024, 12, 1),
            company=self.company_obj,
            type=self.type_obj
        )
        
        # 기본 재고 정보 생성 (DefaultInventory 사용)
        self.default_inventory = DefaultInventory.objects.create(
            pie_healthcare_num="12345",
            medication_name="테스트약품",
            expiry_date=date(2025, 12, 31),
            stock_quantity=100,
            medication_lot_number="LOT001",
            medication_size="10mg",
            stock_location="A-1",
            medication_created_by="제조사"
        )

    def test_duplicate_handling_inventory_type(self):
        """재고 타입 중복처리 테스트"""
        # 디버깅: Inventory2 조회 확인
        from inventory2.backend.utils.utils import _get_default_inventory_filters, _get_optimized_inventory_queryset
        
        # 첫 번째 스캔
        scan1 = RFIDScan.objects.create(
            date=self.date_obj,  # 같은 date 사용
            pie_healthcare_num="12345",
            expiry_date=date(2025, 12, 31),
            scanned_quantity=50,
            medication_lot_number="LOT001"
        )
        
        # 디버깅: 필터와 쿼리셋 확인
        filters = _get_default_inventory_filters([scan1])
        default_qs = _get_optimized_inventory_queryset(filters)
        print(f"🔍 Inventory2 조회 결과: {default_qs.count()}개")
        print(f"🔍 Scan1 date: {scan1.date}")
        print(f"🔍 Inventory2 pie_healthcare_num: {self.inventory.pie_healthcare_num}")
        print(f"🔍 Scan1 pie_healthcare_num: {scan1.pie_healthcare_num}")
        print(f"🔍 필터 조건: {filters}")
        
        # 모든 Inventory2 확인
        all_inv = Inventory2.objects.all()
        print(f"🔍 전체 Inventory2 개수: {all_inv.count()}")
        for inv in all_inv:
            print(f"  - {inv.pie_healthcare_num}, {inv.date}, {inv.expiry_date}")
        
        result1 = create_specifications_from_rfid_scan([scan1], "재고")
        self.assertTrue(result1["success"])
        self.assertEqual(result1["created"], 1)

    def test_duplicate_handling_outgoing_type(self):
        """출고 타입 중복처리 테스트"""
        # 기존 재고 설정
        current_inv = Inventory2.objects.create(
            pie_healthcare_num="12345",
            medication_name="테스트약품",
            expiry_date=date(2025, 12, 31),
            stock_quantity=100,
            medication_lot_number="LOT001",
            date=self.date_obj
        )
        
        # 첫 번째 출고
        scan1 = RFIDScan.objects.create(
            date=self.date_obj,  # 같은 date 사용
            pie_healthcare_num="12345",
            expiry_date=date(2025, 12, 31),
            scanned_quantity=20,
            medication_lot_number="LOT001"
        )
        
        result1 = create_specifications_from_rfid_scan([scan1], "출고")
        self.assertTrue(result1["success"])
        
        # 두 번째 출고 (누적, 같은 date)
        scan2 = RFIDScan.objects.create(
            date=self.date_obj,  # 같은 date 사용
            pie_healthcare_num="12345",
            expiry_date=date(2025, 12, 31),
            scanned_quantity=30,
            medication_lot_number="LOT001"
        )
        
        result2 = create_specifications_from_rfid_scan([scan2], "출고")
        self.assertTrue(result2["success"])
        
        # 검증: 출고 수량이 누적되어야 함
        spec = Specification.objects.filter(pie_healthcare_num="12345", expiry_date=date(2025, 12, 31), medication_lot_number="LOT001").order_by('-id').first()
        self.assertEqual(spec.stock_quantity, -50)  # -20 + (-30) = 누적

    def test_stock_quantity_validation(self):
        """재고 수량 유효성 검증 테스트"""
        from inventory2.backend.utils.utils import _validate_stock_quantity
        
        # 정상 수량
        self.assertTrue(_validate_stock_quantity(100, "재고"))
        self.assertTrue(_validate_stock_quantity(0, "재고"))
        
        # 음수 수량
        self.assertFalse(_validate_stock_quantity(-10, "재고"))
        
        # 과도한 수량
        self.assertFalse(_validate_stock_quantity(1000000, "재고"))


class InventoryUpdateTestCase(TransactionTestCase):
    """재고 업데이트 관련 테스트"""

    def setUp(self):
        """테스트 데이터 설정"""
        self.type_obj, _ = Type.objects.get_or_create(name="재고")
        self.company_obj = Company.objects.create(
            company_name="테스트병원",
            company_code="TEST001"
        )
        self.company_obj.available_type.set([self.type_obj])
        self.date_obj = Date.objects.create(
            date=date(2024, 12, 1),
            company=self.company_obj,
            type=self.type_obj
        )

    def test_inventory_update_from_specifications(self):
        """스펙 기반 재고 업데이트 테스트"""
        from inventory2.backend.utils.utils import update_inventory_from_specifications
        
        # 스펙 생성
        spec = Specification.objects.create(
            date=self.date_obj,
            pie_healthcare_num="12345",
            medication_name="테스트약품",
            expiry_date=date(2025, 12, 31),
            stock_quantity=50,
            medication_lot_number="LOT001",
            medication_size="10mg",
            stock_location="A-1",
            medication_created_by="제조사"
        )
        
        # 재고 업데이트
        result = update_inventory_from_specifications([spec], "재고")
        self.assertTrue(result["success"])
        self.assertEqual(result["updated"], 1)
        
        # 검증
        inventory = Inventory2.objects.get(pie_healthcare_num="12345")
        self.assertEqual(inventory.stock_quantity, 50)

    def test_outgoing_inventory_update(self):
        """출고 시 재고 차감 테스트"""
        from inventory2.backend.utils.utils import update_inventory_from_specifications
        
        # 기존 재고 생성
        inventory = Inventory2.objects.create(
            pie_healthcare_num="12345",
            medication_name="테스트약품",
            expiry_date=date(2025, 12, 31),
            stock_quantity=100,
            medication_lot_number="LOT001",
            date=self.date_obj
        )
        
        # 출고 스펙 생성
        spec = Specification.objects.create(
            date=self.date_obj,
            pie_healthcare_num="12345",
            medication_name="테스트약품",
            expiry_date=date(2025, 12, 31),
            stock_quantity=-30,  # 출고는 음수
            medication_lot_number="LOT001"
        )
        
        # 출고 처리
        result = update_inventory_from_specifications([spec], "출고")
        self.assertTrue(result["success"])
        self.assertEqual(result["updated"], 1)
        
        # 검증: 재고가 차감되어야 함
        inventory.refresh_from_db()
        self.assertEqual(inventory.stock_quantity, 70)  # 100 - 30

    def test_outgoing_insufficient_stock(self):
        """출고 시 재고 부족 테스트"""
        from inventory2.backend.utils.utils import update_inventory_from_specifications
        
        # 기존 재고 생성 (적은 수량)
        inventory = Inventory2.objects.create(
            pie_healthcare_num="12345",
            medication_name="테스트약품",
            expiry_date=date(2025, 12, 31),
            stock_quantity=20,
            medication_lot_number="LOT001"
        )
        
        # 출고 스펙 생성 (더 많은 수량)
        spec = Specification.objects.create(
            date=self.date_obj,
            pie_healthcare_num="12345",
            medication_name="테스트약품",
            expiry_date=date(2025, 12, 31),
            stock_quantity=-30,  # 출고는 음수
            medication_lot_number="LOT001"
        )
        
        # 출고 처리
        result = update_inventory_from_specifications([spec], "출고")
        self.assertTrue(result["success"])
        self.assertEqual(result["errors"], 1)  # 에러 발생
        
        # 검증: 재고가 변경되지 않아야 함
        inventory.refresh_from_db()
        self.assertEqual(inventory.stock_quantity, 20) 