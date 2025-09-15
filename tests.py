import pytest
import time
from django.db import transaction
from django.test import TestCase
from django.core.exceptions import ValidationError

from inventory2.backend.models.discrepancy import InventoryDiscrepancy
from inventory2.backend.models.inventory import Inventory2
from inventory2.backend.models.rfidscan import RFIDScan
from inventory2.backend.models.specification import Specification
from inventory2.backend.utils.utils import calculate_and_save_discrepancies, create_specifications_from_rfid_scan


# 테스트 클래스로 구현 (데이터베이스 격리)
class InventoryTests(TestCase):
    def setUp(self):
        # 공통 초기 데이터
        self.inventory = Inventory2.objects.create(
            pie_healthcare_num="PIE123",
            medication_name="Test Drug",
            expiry_date="2025-12-31",
            stock_quantity=100
        )

    def test_duplicate_rfid_scan(self):
        """중복 스캔 시 Specification 갱신 테스트"""
        # 1. 첫 번째 스캔

        scan1 = RFIDScan.objects.create(
            pie_healthcare_num="PIE123",
            expiry_date="2025-12-31",
            scanned_quantity=10
        )
        create_specifications_from_rfid_scan([scan1])

        # 2. 두 번째 스캔 (업데이트)
        scan2 = RFIDScan.objects.create(
            pie_healthcare_num="PIE123",
            expiry_date="2025-12-31",
            scanned_quantity=20
        )
        create_specifications_from_rfid_scan([scan2])

        # 3. 검증 (최신 값으로 덮어쓰기 되어야 함)
        spec = Specification.objects.get(pie_healthcare_num="PIE123")
        self.assertEqual(spec.stock_quantity, 20)  # 20으로 갱신

    def test_missing_inventory(self):
        """Inventory2 없는 경우 오차 기록 테스트"""
        # Specification 생성 (Inventory2 없음)
        spec = Specification.objects.create(
            pie_healthcare_num="PIE999",
            medication_name="Ghost Drug",
            stock_quantity=50,
            expiry_date="2025-12-31"
        )

        # 오차 계산 실행
        calculate_and_save_discrepancies(company_inventory_data=[])

        # 오차 기록 검증
        discrepancy = InventoryDiscrepancy.objects.get(pie_healthcare_num="PIE999")
        self.assertEqual(discrepancy.reason, "Inventory data not found")
        self.assertEqual(discrepancy.discrepancy_quantity, 50)

    def test_bulk_performance(self):
        """대량 데이터 성능 테스트"""
        # 1. 10,000개 RFIDScan 생성
        batch_size = 1000  # 실제 테스트시 10000으로 변경
        rfid_scans = [
            RFIDScan(
                pie_healthcare_num=f"PIE{i:05}",
                expiry_date="2025-12-31",
                scanned_quantity=i
            ) for i in range(batch_size)
        ]
        RFIDScan.objects.bulk_create(rfid_scans)

        # 2. 성능 측정
        start = time.time()
        create_specifications_from_rfid_scan(RFIDScan.objects.all())
        elapsed = time.time() - start

        # 3. 검증
        self.assertEqual(Specification.objects.count(), batch_size)
        print(f"\n{batch_size}건 처리 시간: {elapsed:.2f}초")

    def test_transaction_rollback(self):
        """트랜잭션 롤백 테스트"""
        with self.assertRaises(ValidationError):  # 의도적 오류 발생
            with transaction.atomic():
                # 잘못된 데이터 생성
                scan = RFIDScan.objects.create(
                    pie_healthcare_num="PIE_ERROR",
                    expiry_date="2025-13-32",  # 존재하지 않는 날짜
                    scanned_quantity=10
                )
                create_specifications_from_rfid_scan([scan])

        # 롤백 검증
        self.assertFalse(RFIDScan.objects.filter(pie_healthcare_num="PIE_ERROR").exists())


# pytest용 테스트
@pytest.mark.django_db
def test_error_logging(caplog):
    """에러 로깅 테스트"""

    # 모의(mock) 오류 설정
    import unittest
    with pytest.raises(Exception), \
            unittest.mock.patch.object(Inventory2.objects, 'get', side_effect=Exception("DB Crash")):
        # 테스트용 RFIDScan 생성
        scan = RFIDScan.objects.create(
            pie_healthcare_num="PIE123",
            expiry_date="2025-12-31",
            scanned_quantity=10
        )
        create_specifications_from_rfid_scan([scan])

    # 로그 메시지 검증
    assert "DB Crash" in caplog.text