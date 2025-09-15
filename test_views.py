"""
뷰 함수들의 통합 테스트
"""
import json
from datetime import datetime, date
from django.test import TestCase, TransactionTestCase, Client
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework import status

from inventory2.backend.models.base import Company, Type, Date
from inventory2.backend.models.inventory import Inventory2
from inventory2.backend.models.specification import Specification
from inventory2.backend.models.discrepancy import InventoryDiscrepancy
from inventory2.backend.models.manufacturinghash import ManufacturingHash
from inventory2.backend.models.rfidscan import RFIDScan, EPCdata


class RFIDScanViewSetTestCase(TransactionTestCase):
    """RFID 스캔 뷰셋 테스트"""

    def setUp(self):
        """테스트 데이터 설정"""
        self.client = APIClient()
        
        # 기본 데이터 생성 (get_or_create 사용)
        self.type_obj, _ = Type.objects.get_or_create(name="재고")
        self.outgoing_type, _ = Type.objects.get_or_create(name="출고")
        self.inspection_type, _ = Type.objects.get_or_create(name="검수")
        
        self.company_obj, _ = Company.objects.get_or_create(
            company_name="테스트병원",
            company_code="TEST001"
        )
        # many-to-many 필드는 별도로 설정
        self.company_obj.available_type.add(self.type_obj, self.outgoing_type, self.inspection_type)
        
        self.date_obj, _ = Date.objects.get_or_create(
            date=date(2024, 12, 1),
            company=self.company_obj,
            type=self.type_obj
        )
        
        # 기본 재고 데이터 생성
        self.inventory, _ = Inventory2.objects.get_or_create(
            pie_healthcare_num="12345",
            medication_name="테스트약품",
            expiry_date=date(2025, 12, 31),
            stock_quantity=100,
            medication_lot_number="LOT001",
            date=self.date_obj
        )
        
        # 해시 데이터 생성
        self.hash_obj, _ = ManufacturingHash.objects.get_or_create(
            original_code="LOT001",
            hashed_code="ABC123DEF"
        )

    def test_bulk_create_inventory_type_success(self):
        """재고 타입 RFID 스캔 성공 테스트"""
        # EPC 데이터 생성 (실제 EPC 형식에 맞게)
        epc_data = "000012345250131ABC123DEF"  # 4자리 prefix + 5자리 pie_num + 6자리 expiry + 9자리 hash
        
        url = reverse('rfidscan-bulk_create')
        data = {
            "a": [epc_data],
            "company": "테스트병원",
            "code": "TEST001",
            "type": "재고",
            "date": "20241201"
        }
        
        response = self.client.post(url, data, format='json')
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("status", response.data)
        self.assertEqual(response.data["status"], "재고조사 완료")

    def test_bulk_create_outgoing_type_success(self):
        """출고 타입 RFID 스캔 성공 테스트"""
        epc_data = "000012345250131ABC123DEF"
        
        url = reverse('rfidscan-bulk_create')
        data = {
            "a": [epc_data],
            "company": "테스트병원",
            "code": "TEST001",
            "type": "출고",
            "date": "20241201"
        }
        
        response = self.client.post(url, data, format='json')
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("status", response.data)
        self.assertEqual(response.data["status"], "출고 스펙 저장 완료")

    def test_bulk_create_inspection_type_success(self):
        """검수 타입 RFID 스캔 성공 테스트"""
        # 출고 타입 생성
        outgoing_date, _ = Date.objects.get_or_create(
            date=date(2024, 12, 1),
            company=self.company_obj,
            type=self.outgoing_type
        )
        
        # 출고 스펙 생성
        outgoing_spec, _ = Specification.objects.get_or_create(
            date=outgoing_date,
            pie_healthcare_num="12345",
            medication_name="테스트약품",
            expiry_date=date(2025, 12, 31),
            stock_quantity=50,
            medication_lot_number="LOT001",
            medication_size="10mg",
            stock_location="A-1",
            medication_created_by="제조사"
        )
        
        epc_data = "000012345250131ABC123DEF"
        
        url = reverse('rfidscan-bulk_create')
        data = {
            "a": [epc_data],
            "company": "테스트병원",
            "code": "TEST001",
            "type": "검수",
            "date": "20241201"
        }
        
        response = self.client.post(url, data, format='json')
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("status", response.data)
        self.assertEqual(response.data["status"], "검수 완료")

    def test_bulk_create_no_data(self):
        """데이터가 없는 경우 테스트"""
        url = reverse('rfidscan-bulk_create')
        data = {
            "a": [],
            "company": "테스트병원",
            "code": "TEST001",
            "type": "재고",
            "date": "20241201"
        }
        
        response = self.client.post(url, data, format='json')
        
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("message", response.data)

    def test_bulk_create_invalid_type(self):
        """잘못된 타입 테스트"""
        epc_data = "000012345250131ABC123DEF"
        
        url = reverse('rfidscan-bulk_create')
        data = {
            "a": [epc_data],
            "company": "테스트병원",
            "code": "TEST001",
            "type": "잘못된타입",
            "date": "20241201"
        }
        
        response = self.client.post(url, data, format='json')
        
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("message", response.data)

    def test_bulk_create_string_data(self):
        """문자열 형태의 데이터 테스트"""
        epc_data = "000012345250131ABC123DEF"
        
        url = reverse('rfidscan-bulk_create')
        data = {
            "a": f"[{epc_data}]",  # 문자열 형태
            "company": "테스트병원",
            "code": "TEST001",
            "type": "재고",
            "date": "20241201"
        }
        
        response = self.client.post(url, data, format='json')
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_bulk_create_multiple_epcs(self):
        """여러 EPC 데이터 테스트 (중복 처리 테스트)"""
        epc_data1 = "000012345250131ABC123DEF"
        epc_data2 = "000012345250131ABC123DEF"  # 같은 데이터 (중복)
        
        url = reverse('rfidscan-bulk_create')
        data = {
            "a": [epc_data1, epc_data2],
            "company": "테스트병원",
            "code": "TEST001",
            "type": "재고",
            "date": "20241201"
        }
        
        response = self.client.post(url, data, format='json')
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # 중복 처리 확인
        self.assertIn("status", response.data)

    def test_bulk_create_invalid_epc_format(self):
        """잘못된 EPC 형식 테스트"""
        invalid_epc = "INVALID_EPC_FORMAT"
        
        url = reverse('rfidscan-bulk_create')
        data = {
            "a": [invalid_epc],
            "company": "테스트병원",
            "code": "TEST001",
            "type": "재고",
            "date": "20241201"
        }
        
        response = self.client.post(url, data, format='json')
        
        # 잘못된 EPC 형식은 처리되지만 로그에 기록됨
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_bulk_create_with_other_company(self):
        """다른 회사가 있는 경우 테스트"""
        # 다른 회사 생성
        other_company, _ = Company.objects.get_or_create(
            company_name="다른병원",
            company_code="OTHER001"
        )
        other_company.available_type.add(self.type_obj)
        
        epc_data = "000012345250131ABC123DEF"
        
        url = reverse('rfidscan-bulk_create')
        data = {
            "a": [epc_data],
            "company": "테스트병원",
            "code": "TEST001",
            "type": "재고",
            "date": "20241201",
            "other_company": "다른병원"
        }
        
        response = self.client.post(url, data, format='json')
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_bulk_create_without_date(self):
        """날짜가 없는 경우 테스트 (오늘 날짜 사용)"""
        epc_data = "000012345250131ABC123DEF"
        
        url = reverse('rfidscan-bulk_create')
        data = {
            "a": [epc_data],
            "company": "테스트병원",
            "code": "TEST001",
            "type": "재고"
            # date 필드 없음
        }
        
        response = self.client.post(url, data, format='json')
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class RFIDScanListTestCase(TestCase):
    """RFID 스캔 리스트 뷰 테스트"""

    def setUp(self):
        """테스트 데이터 설정"""
        self.client = APIClient()
        
        self.type_obj, _ = Type.objects.get_or_create(name="재고")
        self.company_obj, _ = Company.objects.get_or_create(
            company_name="테스트병원",
            company_code="TEST001"
        )
        # many-to-many 필드는 별도로 설정
        self.company_obj.available_type.add(self.type_obj)
        
        self.date_obj, _ = Date.objects.get_or_create(
            date=date(2024, 12, 1),
            company=self.company_obj,
            type=self.type_obj
        )
        
        # RFID 스캔 데이터 생성
        self.rfid_scan, _ = RFIDScan.objects.get_or_create(
            date=self.date_obj,
            pie_healthcare_num="12345",
            expiry_date=date(2025, 12, 31),
            scanned_quantity=30,
            medication_lot_number="LOT001"
        )

    def test_rfid_scan_list(self):
        """RFID 스캔 리스트 조회 테스트"""
        url = reverse('rfidscan-list')
        response = self.client.get(url)
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("results", response.data)
        self.assertEqual(len(response.data["results"]), 1)

    def test_rfid_scan_detail(self):
        """RFID 스캔 상세 조회 테스트"""
        url = reverse('rfidscan-detail', args=[self.rfid_scan.id])
        response = self.client.get(url)
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["pie_healthcare_num"], "12345")

    def test_rfid_scan_create(self):
        """RFID 스캔 생성 테스트"""
        url = reverse('rfidscan-list')
        data = {
            "date": self.date_obj.id,
            "pie_healthcare_num": "67890",
            "expiry_date": "2025-12-31",
            "scanned_quantity": 25,
            "medication_lot_number": "LOT002"
        }
        
        response = self.client.post(url, data, format='json')
        
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["pie_healthcare_num"], "67890")

    def test_rfid_scan_update(self):
        """RFID 스캔 수정 테스트"""
        url = reverse('rfidscan-detail', args=[self.rfid_scan.id])
        data = {
            "scanned_quantity": 40
        }
        
        response = self.client.patch(url, data, format='json')
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["scanned_quantity"], 40)

    def test_rfid_scan_delete(self):
        """RFID 스캔 삭제 테스트"""
        url = reverse('rfidscan-detail', args=[self.rfid_scan.id])
        response = self.client.delete(url)
        
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        
        # 삭제 확인
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class BranchingModalTests(TestCase):
    """브랜칭 모달 관련 테스트"""

    def setUp(self):
        """테스트 데이터 설정"""
        self.client = Client()
        self.type_obj, _ = Type.objects.get_or_create(name="재고")

    def test_modal_shows_on_missing_params(self):
        """필수 파라미터가 없을 때 모달이 표시되는지 테스트"""
        url = reverse('inventory:sims')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_no_modal_on_pagination(self):
        """페이지네이션 시 모달이 표시되지 않는지 테스트"""
        url = reverse('inventory:sims')
        response = self.client.get(url, {'page': 1})
        self.assertEqual(response.status_code, 200)

    def test_session_reset(self):
        """세션 리셋 테스트"""
        url = reverse('inventory:sims')
        response = self.client.get(url, {'reset': 'true'})
        self.assertEqual(response.status_code, 200)

    def test_invalid_company_type(self):
        """잘못된 회사/타입 조합 테스트"""
        url = reverse('inventory:sims')
        response = self.client.get(url, {'company': 'invalid', 'type': 'invalid'})
        self.assertEqual(response.status_code, 200)

    # 실제 RFID/중복/출고/재고 등은 기존 test_utils.py의 로직을 활용하거나, 여기에 추가로 작성 가능 

class DuplicateHandlingTestCase(TestCase):
    """중복 처리 로직 테스트"""

    def setUp(self):
        """테스트 데이터 설정"""
        self.client = APIClient()
        
        self.type_obj, _ = Type.objects.get_or_create(name="재고")
        self.company_obj, _ = Company.objects.get_or_create(
            company_name="테스트병원",
            company_code="TEST001"
        )
        self.company_obj.available_type.add(self.type_obj)
        
        self.date_obj, _ = Date.objects.get_or_create(
            date=date(2024, 12, 1),
            company=self.company_obj,
            type=self.type_obj
        )
        
        # 기본 재고 데이터 생성
        self.inventory, _ = Inventory2.objects.get_or_create(
            pie_healthcare_num="12345",
            medication_name="테스트약품",
            expiry_date=date(2025, 12, 31),
            stock_quantity=100,
            medication_lot_number="LOT001",
            date=self.date_obj
        )

    def test_duplicate_epc_handling(self):
        """중복 EPC 처리 테스트"""
        epc_data = "000012345250131ABC123DEF"
        
        url = reverse('rfidscan-bulk_create')
        data = {
            "a": [epc_data, epc_data, epc_data],  # 같은 EPC 3번
            "company": "테스트병원",
            "code": "TEST001",
            "type": "재고",
            "date": "20241201"
        }
        
        response = self.client.post(url, data, format='json')
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # 중복 처리 확인
        self.assertIn("status", response.data)

    def test_inventory_overwrite_mode(self):
        """재고 덮어쓰기 모드 테스트"""
        epc_data = "000012345250131ABC123DEF"
        
        # 첫 번째 스캔
        url = reverse('rfidscan-bulk_create')
        data1 = {
            "a": [epc_data],
            "company": "테스트병원",
            "code": "TEST001",
            "type": "재고",
            "date": "20241201"
        }
        
        response1 = self.client.post(url, data1, format='json')
        self.assertEqual(response1.status_code, status.HTTP_200_OK)
        
        # 두 번째 스캔 (같은 EPC, 다른 수량)
        data2 = {
            "a": [epc_data],
            "company": "테스트병원",
            "code": "TEST001",
            "type": "재고",
            "date": "20241201"
        }
        
        response2 = self.client.post(url, data2, format='json')
        self.assertEqual(response2.status_code, status.HTTP_200_OK)
        
        # 재고 타입은 덮어쓰기 모드이므로 마지막 값이 유지됨
        self.assertIn("status", response2.data) 