"""
본 파일은 사용자 코드 두 개(RFIDScanViewSet 모듈, Inventory/Spec 유틸 모듈)에
주석과 가이드를 촘촘히 추가한 "주석 강화 버전"입니다. 실행 코드는 원본과 동일하며,
가독성과 유지보수를 돕기 위한 설명/주의/성능 관련 코멘트만 추가했습니다.

전체 파이프라인 요약
1) 재고등록(DefaultInventory 업로드) → 2) RFID 스캔 수집 → 3) Specification 생성
4) Inventory 업데이트(재고/출고/검수 분기) → 5) Discrepancy(불일치) 계산/저장
6) 검수 시: 출고 vs 검수 비교 → **일치분만 A 차감 + B 가산(한 번에 이동)** → 불일치 리포트 저장

EPC 포맷(가정)
- prefix: 5자리 (예: 06022)
- PIE product code: 5자리
- expiry(유효기간): 6자리(YYMMDD)
- hashed lot: 9자리(대문자 HEX)
- serial: 4자리
총 길이 최소 29자리(공백 없음). 실제 환경에서 prefix 길이/규칙은 반드시 소스와 일치시킬 것.

시간/타임존
- 시연/운영 서버 타임존이 다르면 오늘 날짜 계산이 달라질 수 있음 → Django timezone 권장

중복/멱등성
- 동일 date에 같은 EPC를 다시 보내면 EPCdata 유니크/필터링으로 중복 방지

성능 주의
- 대량 처리 시 bulk_create/bulk_update, batch_size 적용
- 캐시: hash 매핑, spec 매핑 등 메모리 캐시 사용. 무효화 타이밍 유의
"""

from django.db import transaction
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from datetime import datetime
from collections import defaultdict

from inventory2.backend.mixins.queryparams import QueryParamFilterMixin
from inventory2.backend.models.base import Company, Type, Date
from inventory2.backend.models.manufacturinghash import ManufacturingHash
from inventory2.backend.models.rfidscan import RFIDScan, EPCdata
from inventory2.backend.models.inventory import Inventory2
from inventory2.backend.models.specification import Specification
from inventory2.backend.models.discrepancy import InventoryDiscrepancy
from inventory2.backend.serializers.rfidscan import RFIDScanSerializer
from inventory2.backend.utils.utils import create_specifications_from_rfid_scan, execute_discrepancy_check, \
    update_inventory_from_specifications, carry_over_inventory
from core.logger import logger
from core.monitoring import monitor_performance, monitor_database_queries, log_business_operation

# -----------------------------
# 상수 정의 (EPC 파싱 관련)
# -----------------------------
# NOTE: EPC 포맷이 바뀌면 아래 상수도 반드시 함께 업데이트할 것.
MIN_EXPIRY_DATE = datetime(2025, 1, 1).date()
MAX_EXPIRY_DATE = datetime(2100, 12, 31).date()
EPC_PREFIX_LENGTH = 4  # ⚠️ 실제 환경에서 4가 아닌 5일 수 있음. 운영 데이터와 반드시 일치시킬 것.
PIE_NUM_LENGTH = 5
EXPIRY_DATE_LENGTH = 6
HASH_LENGTH = 9


class RFIDScanViewSet(QueryParamFilterMixin, viewsets.GenericViewSet):
    """
    RFID 스캔 데이터 → 스펙/재고/불일치까지 한 번에 처리하는 ViewSet.

    구성 포인트
    - bulk_create 엔드포인트 1개로 재고/출고/검수 3가지 플로우를 통합
    - EPCdata로 같은 날짜/같은 EPC 중복 처리 방지 (멱등성 확보)
    - 해시(ManufacturingHash) 매핑을 통해 hashed lot → original lot 복원
    - 모듈별 책임 분리: 스펙 생성/재고 업데이트/불일치 계산은 utils 함수 호출

    운영 팁
    - 시연/운영 시 로그는 반드시 수집: new_epc 수, 중복 필터 수, null_lot 수 등
    - 대량 입력 시 batch_size/BULK 사용으로 DB 부하 완화
    """

    model = RFIDScan
    queryset = RFIDScan.objects.all()
    serializer_class = RFIDScanSerializer

    @monitor_performance("epc_parsing")
    def _parse_epc_data(self, epc):
        """EPC(문자열)를 파싱하여 (pie_num, expiry_date, hashed_lot)을 반환."""
        try:
            epc_data = epc[EPC_PREFIX_LENGTH:]
            pie_num = epc_data[:PIE_NUM_LENGTH]
            expiry_date_str = epc_data[PIE_NUM_LENGTH:PIE_NUM_LENGTH + EXPIRY_DATE_LENGTH]
            hashed_lot = epc_data[PIE_NUM_LENGTH + EXPIRY_DATE_LENGTH:PIE_NUM_LENGTH + EXPIRY_DATE_LENGTH + HASH_LENGTH]
            expiry_date = datetime.strptime(expiry_date_str, "%y%m%d").date()

            if not (MIN_EXPIRY_DATE <= expiry_date <= MAX_EXPIRY_DATE):
                logger.warning(f"유효하지 않은 expiry_date → {epc}")
                return None

            return (pie_num, expiry_date, hashed_lot)
        except Exception as e:
            logger.warning(f"EPC 파싱 실패: {epc} | 이유: {e}")
            return None

    def _get_existing_epcs(self, date_obj, datalist):
        """현재 date에 이미 저장된 EPC 문자열 집합을 반환하여 중복 전송을 필터링."""
        return set(EPCdata.objects.filter(date=date_obj, data__in=datalist).values_list("data", flat=True))

    def _create_new_epc_records(self, new_epcs, date_obj):
        """EPCdata에 신규 EPC를 일괄 저장."""
        new_epcs_objs = EPCdata.objects.bulk_create([
            EPCdata(date=date_obj, data=epc) for epc in new_epcs
        ])
        logger.info(f"{len(new_epcs_objs)} new EPCs created.")
        return new_epcs_objs

    def _get_hash_mapping(self, hashed_codes):
        """hashed lot 목록에 대해 DB에서 original lot 매핑을 조회."""
        return {
            h.hashed_code: h.original_code
            for h in ManufacturingHash.objects.filter(hashed_code__in=hashed_codes)
        }

    def _aggregate_scan_counts(self, parsed_info, hash_map):
        """파싱된 EPC → (pie_num, expiry, original_lot)별 스캔 수량 집계."""
        scanned_count = defaultdict(int)
        null_lot_count = 0

        for pie_healthcare_num, expiry_date, hashed_lot in parsed_info:
            original_lot = hash_map.get(hashed_lot)
            if original_lot is None:
                null_lot_count += 1
                logger.warning(f"해시값 미존재 → {hashed_lot}, None 처리")
            scanned_count[(pie_healthcare_num, expiry_date, original_lot)] += 1

        return scanned_count, null_lot_count

    def _create_rfid_scan_instances(self, scanned_count, date_obj):
        """집계 결과를 RFIDScan 인스턴스 리스트로 변환(메모리 상에서만)."""
        return [
            RFIDScan(
                date=date_obj,
                pie_healthcare_num=pie_healthcare_num,
                expiry_date=expiry_date,
                medication_lot_number=original_lot,
                scanned_quantity=count
            )
            for (pie_healthcare_num, expiry_date, original_lot), count in scanned_count.items()
        ]

    @monitor_performance("epc_processing")
    @monitor_database_queries
    def _process_epc_data(self, datalist, date_obj):
        """원시 EPC 문자열 리스트(datalist)를 처리하여 RFIDScan 인스턴스 리스트를 생성."""
        existing_epcs = self._get_existing_epcs(date_obj, datalist)
        new_epcs = [epc for epc in datalist if epc not in existing_epcs]

        self._create_new_epc_records(new_epcs, date_obj)

        parsed_info = []
        for epc in new_epcs:
            parsed = self._parse_epc_data(epc)
            if parsed:
                parsed_info.append(parsed)

        hashed_codes = [x[2] for x in parsed_info]
        hash_map = self._get_hash_mapping(hashed_codes)
        scanned_count, null_lot_count = self._aggregate_scan_counts(parsed_info, hash_map)

        # NOTE: 운영 가시성 향상을 위해 null_lot_count/new_epcs 수를 Response에 포함 권장
        return self._create_rfid_scan_instances(scanned_count, date_obj)

    def _get_previous_date(self, date_obj):
        """같은 회사/타입에서 현재 date보다 과거 중 가장 최근 Date를 반환."""
        return (
            Date.objects
            .filter(company=date_obj.company, type=date_obj.type, date__lt=date_obj.date)
            .order_by('-date')
            .first()
        )

    def carry_over_if_needed(self, date_obj):
        """검수 시, 과거 최신 재고를 현재 날짜로 이월."""
        prev_date = self._get_previous_date(date_obj)
        if prev_date:
            carry_over_inventory(prev_date, date_obj)

    def _validate_request_data(self, request):
        """
        요청 payload에서 필수 파라미터를 수집/검증.
        필수: a(EPC 리스트), company, code, type
        선택: other_company(검수 시 수신처), date(YYYYMMDD; 미지정 시 오늘)
        """
        datalist = request.data.get("a")
        if not datalist:
            return None, Response({"status": "error", "message": "No data provided."}, status=400)

        company_name = request.data.get("company")
        company_code = request.data.get("code")
        type_name = request.data.get("type")
        other_company_name = request.data.get("other_company", "남양주백병원")
        date_str = request.data.get("date") or datetime.today().strftime("%Y%m%d")  # NOTE: timezone 고려 필요

        return {
            'datalist': datalist,
            'company_name': company_name,
            'company_code': company_code,
            'type_name': type_name,
            'other_company_name': other_company_name,
            'date_str': date_str
        }, None

    def _get_type_and_company(self, type_name, company_name, company_code):
        """유효한 Type/Company 객체를 조회하고, 회사가 해당 타입을 사용할 수 있는지 권한 확인."""
        try:
            type_obj = Type.objects.get(name=type_name)
        except Type.DoesNotExist:
            return None, None, Response({"status": "error", "message": f"타입 '{type_name}'이 존재하지 않습니다."}, status=400)

        try:
            company_obj = Company.objects.get(company_name=company_name, company_code=company_code)
            if not company_obj.available_type.filter(id=type_obj.id).exists():
                return None, None, Response(
                    {"status": "error", "message": f"회사 '{company_name}'에서 타입 '{type_name}'을 사용할 수 없습니다."},
                    status=400)
        except Company.DoesNotExist:
            return None, None, Response(
                {"status": "error", "message": f"회사 '{company_name}' (코드: {company_code})이 존재하지 않습니다."}, status=400)

        return type_obj, company_obj, None

    def _get_other_company(self, other_company_name):
        """검수 시 수신 회사 조회(없거나 미존재해도 에러로 중단하지 않음; 경고 로그)."""
        if not other_company_name:
            return None
        try:
            return Company.objects.get(company_name=other_company_name)
        except Company.DoesNotExist:
            logger.warning(f"다른 회사 '{other_company_name}'이 존재하지 않습니다.")
            return None

    def _process_datalist(self, datalist):
        """프론트에서 문자열로 온 EPC 리스트를 파싱하여 list[str] 형태로 통일."""
        if isinstance(datalist, str):
            return datalist.strip("[]").replace(" ", "").split(",")
        return datalist

    # -----------------------------
    # 스펙 양수 정규화 헬퍼
    # -----------------------------
    def normalize_specs_positive(self, date_obj, type_name: str):
        """
        같은 날짜/회사/타입의 Specification 수량을 전부 양수로 보정.
        유틸이 출고를 음수로 저장하더라도 여기서 일괄 양수화.
        """
        t = Type.objects.get(name=type_name)
        qs = Specification.objects.filter(
            date__date=date_obj.date,
            date__company=date_obj.company,
            date__type=t
        ).only('id', 'stock_quantity')
        to_update = []
        for s in qs:
            q = int(s.stock_quantity or 0)
            if q < 0:
                s.stock_quantity = abs(q)
                to_update.append(s)
        if to_update:
            Specification.objects.bulk_update(to_update, ['stock_quantity'], batch_size=200)

    # -----------------------------
    # 분기 처리
    # -----------------------------

    def _handle_inventory_type(self, rfid_scan_instances, date_obj):
        """
        타입==재고 처리
        1) 스펙 생성(create_specifications_from_rfid_scan, operation_type="재고")
        2) 스펙 기반 Inventory 덮어쓰기(update_inventory_from_specifications)
        3) 불일치 계산/저장(execute_discrepancy_check)
        """
        spec_result = create_specifications_from_rfid_scan(rfid_scan_instances, "재고")
        if spec_result.get("success", True):
            specs = Specification.objects.filter(date=date_obj)
            inventory_result = update_inventory_from_specifications(specs, "재고")
        else:
            inventory_result = {"success": False, "message": "스펙 생성 실패"}

        discrepancy_result = execute_discrepancy_check(Specification.objects.filter(date=date_obj))
        return {
            "status": "재고조사 완료",
            "spec": spec_result,
            "inventory": inventory_result,
            "discrepancy": discrepancy_result
        }

    def _handle_outgoing_type(self, rfid_scan_instances, date_obj):
        """
        타입==출고 처리
        - 스펙만 생성(재고는 변경하지 않음; 실제 A→B 이동은 검수 시점에 수행)
        - 생성 직후 스펙 수량을 양수로 정규화
        """
        spec_result = create_specifications_from_rfid_scan(rfid_scan_instances, "출고")
        self.normalize_specs_positive(date_obj, "출고")  # ✅ 출고 스펙 양수 일원화
        return {"status": "출고 스펙 기록(재고 미변경)", "spec": spec_result}

    def _handle_inspection_type(self, rfid_scan_instances, date_obj, company_obj, type_obj, other_company_obj):
        """
        타입==검수 처리(A=company_obj, B=other_company_obj)
        1) 검수 스펙 생성(+양수 정규화)
        2) 같은 날짜 A-출고 vs A-검수 비교 → 불일치 리포트 저장
        3) 일치분(matched)만 A 차감 + B 가산(없으면 생성) — 단일 트랜잭션
        """
        if not other_company_obj:
            return {"status": "error", "message": "수신 회사(other_company)가 필요합니다."}

        # 1) 검수 스펙 생성 (검수만 적재)
        spec_result = create_specifications_from_rfid_scan(rfid_scan_instances, "검수")
        self.normalize_specs_positive(date_obj, "검수")  # ✅ 검수 스펙 양수 일원화

        # A 측 과거 재고 이월(필요 시)
        self.carry_over_if_needed(date_obj)

        # 2) 스펙 수집 (A 회사 기준)
        outgoing_specs = get_outgoing_specifications(date_obj, company_obj)  # A: 출고
        inspected_specs = get_inspection_specifications(date_obj)  # A: 검수(이번에 받은 것)

        # 불일치 계산/저장(리포트 전용) — 절대값 비교
        inspection_response = process_inspection_transfer(date_obj, outgoing_specs, inspected_specs)
        inspection_report = inspection_response.data if isinstance(inspection_response,
                                                                   Response) else inspection_response

        # 3) matched 계산 → 이동 실행
        matched = compute_matched_specs_for_transfer(outgoing_specs, inspected_specs)

        # B 쪽 동일 날짜 + type='재고' Date 준비
        stock_type = Type.objects.get(name='재고')
        recv_date_obj, _ = Date.objects.get_or_create(
            company=other_company_obj,
            type=stock_type,
            date=date_obj.date
        )

        transfer_result = apply_transfer_by_match_v2(
            matched_specs=matched,
            from_company=company_obj,
            from_date_obj=date_obj,
            to_company=other_company_obj,
            to_date_obj=recv_date_obj
        )

        logger.info(
            f"[검수 이송 요약] matched_keys={len(matched)}, "
            f"matched_qty={sum(int(q) for q in matched.values())}"
        )

        return {
            "status": "검수 처리 완료(A 차감 + B 가산)",
            "inspection_report": inspection_report,
            "transfer_result": transfer_result
        }

    @transaction.atomic
    @action(detail=False, methods=['post'], url_path='bulk_create')
    @monitor_performance("rfid_bulk_create")
    @monitor_database_queries
    def bulk_create(self, request, *args, **kwargs):
        """메인 엔드포인트: RFID 스캔 데이터 일괄 처리(재고/출고/검수 통합)"""
        start_time = datetime.now()
        try:
            validated_data, error_response = self._validate_request_data(request)
            if error_response:
                return error_response

            type_obj, company_obj, error_response = self._get_type_and_company(
                validated_data['type_name'],
                validated_data['company_name'],
                validated_data['company_code']
            )
            if error_response:
                return error_response

            date_ = datetime.strptime(validated_data['date_str'], "%Y%m%d")
            logger.info(f"bulk_create : {validated_data['date_str']} | date_ : {date_}")
            date_obj, _ = Date.objects.get_or_create(date=date_, company=company_obj, type=type_obj)
            logger.info(f"date_obj={date_obj}")

            # 이월은 '검수'에서만
            if validated_data['type_name'] == "검수":
                self.carry_over_if_needed(date_obj)

            other_company_obj = self._get_other_company(validated_data['other_company_name'])
            datalist = self._process_datalist(validated_data['datalist'])

            rfid_scan_instances = self._process_epc_data(datalist, date_obj)
            RFIDScan.objects.bulk_create(rfid_scan_instances)

            if validated_data['type_name'] == "재고":
                result = self._handle_inventory_type(rfid_scan_instances, date_obj)
            elif validated_data['type_name'] == "출고":
                result = self._handle_outgoing_type(rfid_scan_instances, date_obj)
            elif validated_data['type_name'] == "검수":
                result = self._handle_inspection_type(rfid_scan_instances, date_obj, company_obj, type_obj,
                                                      other_company_obj)
            else:
                return Response({"status": "error", "message": "알 수 없는 타입입니다."}, status=400)

            duration = (datetime.now() - start_time).total_seconds()
            log_business_operation(
                operation_name=f"rfid_bulk_create_{validated_data['type_name']}",
                duration=duration,
                success=True,
                epc_count=len(datalist),
                company=validated_data['company_name'],
                type=validated_data['type_name']
            )
            return Response(result)

        except Exception as e:
            duration = (datetime.now() - start_time).total_seconds()
            log_business_operation(
                operation_name="rfid_bulk_create_error",
                duration=duration,
                success=False,
                error=str(e)
            )
            logger.error(f"RFID bulk_create 에러: {str(e)}")
            return Response({"status": "error", "message": f"처리 중 오류가 발생했습니다: {str(e)}"}, status=500)
