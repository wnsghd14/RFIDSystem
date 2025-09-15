"""
RFID 기반 재고 시스템의 핵심 유틸 함수 모음.

[전체 흐름 개요]
1) 재고 초기화(엑셀 → DB 적재)
   - piehealthcare에서 내려받은 xlsx를 정규화 후 Inventory2/Default 테이블로 업로드.
2) RFID 스캔(EPC → 파싱/매핑)
   - 리더기로 EPC 문자열 수집 → EPC 구성요소(제품코드, 유통기한, 해시 LOT 등) 파싱
   - 해시값 ↔ 원본 제조번호(LOT) 매핑 조회/캐시
   - 스캔 묶음을 RFIDScan 인스턴스로 만들고, 이를 스펙(Specification)으로 변환
3) 스펙 → 재고 반영/불일치 산출
   - 작업 타입(재고/출고/검수)에 따라 스펙을 재고에 반영
   - 동일 날짜 기준으로 스펙과 재고를 비교하여 불일치(초과/모자람/미존재) 산출

[파일 내 주요 역할]
- 캐시/성능: 사전 조회(스펙/해시/재고), 배치 처리, select_related/only로 N+1 억제
- 일관성: 트랜잭션/원자적 업데이트, 음수 재고 방지, 예외/로그 일관화
- 재사용: 단일 인스턴스 처리 → 배치 집계 구조, 헬퍼 함수로 분리
"""

from datetime import datetime
from functools import lru_cache

from django.core.cache import cache
from django.db import transaction
from django.db.models import Q, F, Prefetch
from django.conf import settings

from core.logger import logger
from core.exceptions import (
    SpecificationCreationError, DiscrepancyCalculationError,
    DateFormatError, DatabaseOperationError
)
from inventory2.backend.models.base import Date
from inventory2.backend.models.discrepancy import InventoryDiscrepancy
from inventory2.backend.models.inventory import Inventory2
from inventory2.backend.models.manufacturinghash import ManufacturingHash
from inventory2.backend.models.specification import Specification

import hashlib

# =========================
# 상수(변수) 정의 & 캐시 키
# =========================
MAX_HASH_ATTEMPTS = 10000  # 제조번호 해시 충돌 회피를 위해 시도할 최대 횟수
HASH_LENGTH = 9            # EPC 내 해시 길이(고정)
MIN_EXPIRY_DATE = datetime(2025, 1, 1).date()     # 유효한 최소 유통기한 (안전장치)
MAX_EXPIRY_DATE = datetime(2100, 12, 31).date()   # 유효한 최대 유통기한

# 캐시 기본 설정
CACHE_TIMEOUT = getattr(settings, 'CACHE_TIMEOUT', 300)  # seconds
HASH_CACHE_KEY = 'manufacturing_hash_map'                # {hashed_code: original_code}
SPEC_CACHE_KEY = 'specification_map_{date_id}'           # (pie, expiry, lot) → Spec

# 재고 업데이트 모드(가독성 목적. 현재 코드에선 직접 문자열 사용)
STOCK_UPDATE_MODE_OVERWRITE = 'overwrite'
STOCK_UPDATE_MODE_ACCUMULATE = 'accumulate'
STOCK_UPDATE_MODE_DEDUCT = 'deduct'


# =============================================================================
# 재고/스펙 조회 최적화 파트
# =============================================================================

@lru_cache(maxsize=128)
def get_inventory_for_specifications(specs):
    """
    주어진 스펙 묶음에 해당하는 재고를 '한 번에' 가져온다. (L2 캐시 - 프로세스 단위)

    Why:
      - 스펙별로 재고를 매번 조회하면 N+1 발생.
      - 공통 키(제품코드/유통기한/LOT)를 추려 한 번에 가져오면 DB round-trip 최소화.

    Returns:
      QuerySet(Inventory2): 필요한 최소 필드만 골라서 반환.
    """
    # 빈 리스트 방어는 상위 호출부에서 처리(필요 시 여기서도 guard 가능)
    pie_nums = [s.pie_healthcare_num for s in specs]
    expiry_dates = [s.expiry_date for s in specs]
    lot_numbers = [s.medication_lot_number for s in specs]

    return Inventory2.objects.filter(
        Q(pie_healthcare_num__in=pie_nums),
        Q(expiry_date__in=expiry_dates),
        Q(medication_lot_number__in=lot_numbers)
    ).select_related('date').only(
        'stock_quantity', 'pie_healthcare_num', 'expiry_date',
        'medication_lot_number', 'date'
    )


def _get_existing_specs_map(rfid_scan_instances):
    """
    동일 날짜(Date) 내에서 이미 존재하는 스펙을 캐시/DB에서 조회해
    (pie, expiry, lot) → Spec 으로 매핑 반환.

    캐시 전략:
      - 키: SPEC_CACHE_KEY(date_id)
      - 값: dict[(pie, expiry, lot)] = Spec
      - 동일 Date에서 여러 번 스캔/요청이 올 수 있어 캐시 히트율이 높음.
    """
    # 모든 인스턴스는 같은 Date를 가정(상위 계층 보장)
    date_obj = rfid_scan_instances[0].date
    cache_key = SPEC_CACHE_KEY.format(date_id=date_obj.id)

    # 1) 캐시 조회
    cached_specs = cache.get(cache_key)
    if cached_specs is not None:
        logger.info(f"[SpecMap] cache hit: {len(cached_specs)} entries")
        return cached_specs

    # 2) DB 조회 (동일 날짜 + 대상 pie/expiry 범위 제한)
    pie_nums = [r.pie_healthcare_num for r in rfid_scan_instances]
    expiry_dates = [r.expiry_date for r in rfid_scan_instances]

    existing_specs = Specification.objects.filter(
        pie_healthcare_num__in=pie_nums,
        expiry_date__in=expiry_dates,
        date=date_obj
    ).select_related('date').only(
        'pie_healthcare_num', 'expiry_date', 'medication_lot_number',
        'stock_quantity', 'date'
    )

    spec_map = {
        (s.pie_healthcare_num, s.expiry_date, s.medication_lot_number): s
        for s in existing_specs
    }

    cache.set(cache_key, spec_map, CACHE_TIMEOUT)
    logger.info(f"[SpecMap] db load: {len(existing_specs)} entries (cached)")
    return spec_map


def _get_default_inventory_filters(rfid_scan_instances):
    """
    RFID 스캔 묶음에서 공통적으로 필요한 재고(Inventory2)를 한 번에 찾기 위한 Q 필터 생성.

    Note:
      - LOT이 없는(미매핑) 케이스는 제외 가능(현epc 재 구현은 None 제외).
      - 이후 _get_optimized_inventory_queryset와 함께 사용.
    """
    pie_nums = [s.pie_healthcare_num for s in rfid_scan_instances]
    expiry_dates = [s.expiry_date for s in rfid_scan_instances]

    filters = Q(pie_healthcare_num__in=pie_nums) & Q(expiry_date__in=expiry_dates)

    # LOT이 유효한 것만 포함 (미매핑/None 제거)
    non_null_lots = [s.medication_lot_number for s in rfid_scan_instances if s.medication_lot_number]
    if non_null_lots:
        filters &= Q(medication_lot_number__in=non_null_lots)

    return filters


def _get_optimized_inventory_queryset(filters):
    """
    앞서 만든 Q로 재고를 최적화된 형태로 가져온다.
      - select_related('date', 'date__company', 'date__type')로 조인 최소화
      - only(...)로 필요한 컬럼만 로드 → 메모리/네트워크 절약
    """
    return Inventory2.objects.filter(filters).select_related(
        'date', 'date__company', 'date__type'
    ).only(
        'pie_healthcare_num', 'medication_name', 'medication_size',
        'stock_location', 'medication_created_by', 'date'
    )


# =============================================================================
# 스펙 생성/업데이트 파트
# =============================================================================

def _process_specification_instance(instance, spec_map, default_qs, operation_type="재고"):
    """
    RFIDScan 인스턴스 1개를 스펙으로 변환/업데이트 판단.

    흐름:
      1) 해당 키가 존재하는 스펙인지 확인(spec_map)
      2) 기본 재고(default_qs) 존재 확인(미등록 품목 방지)
      3) 수량 유효성 검사 (음수/과다)
      4) 중복이면 업데이트, 신규면 새 스펙 생성

    Returns:
      (new_spec, update_spec) 또는 None
    """
    try:
        # 기본 재고가 존재하지 않으면 스펙 생성 불가(메타정보 부족)
        if not default_qs.exists():
            logger.warning(f"[Spec] 기본 재고 정보 없음 → {instance.pie_healthcare_num}")
            return None

        default = default_qs.first()  # 동일 키로 조회한 결과(only로 최소 필드 로딩)
        key = (default.pie_healthcare_num, instance.expiry_date, instance.medication_lot_number)

        # 수량 sanity check
        if not _validate_stock_quantity(instance.scanned_quantity, operation_type):
            return None

        # 기존 스펙과 중복 키인 경우 → 타입별 처리(덮어쓰기/차감/설정)
        duplicate_result = _handle_duplicate_specification(spec_map, key, instance, operation_type)
        if duplicate_result:
            return duplicate_result

        # 신규 스펙 생성
        new_spec = _create_new_specification(instance, default, operation_type)
        if new_spec:
            return (new_spec, None)

        return None

    except Exception as e:
        logger.error(f"[Spec] 인스턴스 처리 오류: pie={instance.pie_healthcare_num} err={e}")
        return None


@transaction.atomic
def create_specifications_from_rfid_scan(rfid_scan_instances, operation_type="재고"):
    """
    스캔 묶음을 스펙으로 일괄 반영한다(대량 생성/업데이트).

    성능 포인트:
      - 기존 스펙 맵 캐싱
      - 재고 기본 정보 한 번에 로딩(Q 필터 → QS)
      - 배치 처리(batch_size)
      - bulk_create / bulk_update

    Returns:
      dict: {success, created, updated, processed, skipped, operation_type}
    """
    try:
        if not rfid_scan_instances:
            raise SpecificationCreationError("RFID 스캔 데이터가 없습니다.")

        specs_to_create, specs_to_update = [], []
        spec_map = _get_existing_specs_map(rfid_scan_instances)
        default_filters = _get_default_inventory_filters(rfid_scan_instances)
        default_qs = _get_optimized_inventory_queryset(default_filters)

        batch_size = 100
        processed_count = 0
        skipped_count = 0

        # 배치 순회
        for i in range(0, len(rfid_scan_instances), batch_size):
            batch = rfid_scan_instances[i:i + batch_size]

            for instance in batch:
                result = _process_specification_instance(instance, spec_map, default_qs, operation_type)
                if result:
                    new_spec, update_spec = result
                    if new_spec:
                        specs_to_create.append(new_spec)
                    if update_spec:
                        specs_to_update.append(update_spec)
                    processed_count += 1
                else:
                    skipped_count += 1

        # DB 반영 (bulk)
        try:
            if specs_to_create:
                Specification.objects.bulk_create(
                    specs_to_create,
                    batch_size=batch_size,
                    ignore_conflicts=True  # 동일 키 충돌 시 무시(로그로 추적)
                )
            if specs_to_update:
                Specification.objects.bulk_update(
                    specs_to_update,
                    ['stock_quantity', 'date'],
                    batch_size=batch_size
                )
        except Exception as e:
            # 트랜잭션 내에서 발생 → 롤백
            raise DatabaseOperationError(f"스펙 저장 실패: {e}")

        return {
            "success": True,
            "created": len(specs_to_create),
            "updated": len(specs_to_update),
            "processed": processed_count,
            "skipped": skipped_count,
            "operation_type": operation_type
        }

    except (SpecificationCreationError, DatabaseOperationError) as e:
        logger.exception(f"[Spec] 생성 실패: {e}")
        return {"success": False, "message": str(e)}
    except Exception as e:
        logger.exception(f"[Spec] 예기치 못한 오류: {e}")
        return {"success": False, "message": f"예상치 못한 오류: {e}"}


def update_inventory_from_specifications(specs, operation_type):
    """
    스펙 → 재고 반영.

    정책:
      - 재고: 해당 날짜/키의 재고를 스펙 수량으로 '덮어쓰기(또는 생성)'
      - 출고: 현재 재고에서 스펙 수량만큼 '차감'(음수 방지)
      - 검수: (여기서는 직접 반영하지 않고) 불일치 기반 별도 처리

    Returns:
      dict: {success, updated, errors, operation_type}
    """
    try:
        if not specs:
            return {"success": False, "message": "스펙 데이터가 없습니다."}

        updated_count = 0
        error_count = 0

        for spec in specs:
            try:
                if operation_type == "재고":
                    # 재고 스냅샷 개념: 해당 날짜 기준으로 값을 overwrite
                    Inventory2.objects.update_or_create(
                        pie_healthcare_num=spec.pie_healthcare_num,
                        expiry_date=spec.expiry_date,
                        medication_lot_number=spec.medication_lot_number,
                        date=spec.date,
                        defaults={
                            "stock_quantity": spec.stock_quantity,
                            "medication_name": spec.medication_name,
                            "medication_size": spec.medication_size,
                            "stock_location": spec.stock_location,
                            "medication_created_by": spec.medication_created_by
                        }
                    )
                    updated_count += 1

                elif operation_type == "출고":
                    # 출고: 기존 재고에서 차감(음수 방지)
                    current_inv = Inventory2.objects.filter(
                        pie_healthcare_num=spec.pie_healthcare_num,
                        expiry_date=spec.expiry_date,
                        medication_lot_number=spec.medication_lot_number
                    ).first()

                    if current_inv:
                        new_quantity = current_inv.stock_quantity - abs(spec.stock_quantity)
                        if new_quantity >= 0:
                            current_inv.stock_quantity = new_quantity
                            current_inv.save()
                            updated_count += 1
                        else:
                            logger.warning(f"[출고] 음수 재고 방지: {spec.pie_healthcare_num} → {new_quantity}")
                            error_count += 1
                    else:
                        logger.warning(f"[출고] 대상 재고 없음: {spec.pie_healthcare_num}")
                        error_count += 1

                elif operation_type == "검수":
                    # 검수는 불일치 처리 파이프라인에서 재고조정
                    pass

            except Exception as e:
                logger.error(f"[Inventory] 업데이트 실패: pie={spec.pie_healthcare_num} err={e}")
                error_count += 1

        return {
            "success": True,
            "updated": updated_count,
            "errors": error_count,
            "operation_type": operation_type
        }

    except Exception as e:
        logger.exception(f"[Inventory] 업데이트 실패: {e}")
        return {"success": False, "message": str(e)}


# =============================================================================
# 불일치(Discrepancy) 계산/저장 파트
# =============================================================================

def _calculate_discrepancy_for_spec(spec, inv_map):
    """
    단일 스펙 vs 재고 차이를 바탕으로 불일치 모델 생성.

    규칙:
      - 재고 미존재 → reason='미존재', qty=spec.qty
      - 재고와 수량 불일치 → diff>0 → '모자람', diff<0 → '초과'
      - 정확히 일치 → None 반환
    """
    try:
        key = (spec.pie_healthcare_num, spec.expiry_date, spec.medication_lot_number)
        inv = inv_map.get(key)

        if not inv:
            return InventoryDiscrepancy(
                date=spec.date,
                pie_healthcare_num=spec.pie_healthcare_num,
                medication_lot_number=spec.medication_lot_number,
                medication_name=spec.medication_name,
                expiry_date=spec.expiry_date,
                reason="미존재",
                discrepancy_quantity=spec.stock_quantity
            )

        diff = inv.stock_quantity - spec.stock_quantity
        if diff != 0:
            reason = "초과" if diff < 0 else "모자람"
            return InventoryDiscrepancy(
                date=spec.date,
                pie_healthcare_num=spec.pie_healthcare_num,
                medication_lot_number=spec.medication_lot_number,
                medication_name=spec.medication_name,
                expiry_date=spec.expiry_date,
                reason=reason,
                discrepancy_quantity=abs(diff)
            )
        return None

    except Exception as e:
        logger.error(f"[Discrepancy] 계산 실패: pie={spec.pie_healthcare_num} err={e}")
        return None


@transaction.atomic
def calculate_and_save_discrepancies(specs):
    """
    동일 날짜의 스펙 묶음과 재고를 비교하여 불일치 일괄 산출/저장.

    단계:
      1) 해당 날짜의 기존 불일치 삭제(시연 반복 대비)
      2) 스펙 키로 재고 한 번에 조회 → inv_map 구성
      3) 배치로 순회하며 불일치 생성
      4) bulk_create 저장

    Returns:
      dict: {success, total_discrepancies, reason_breakdown}
    """
    try:
        if not specs:
            raise DiscrepancyCalculationError("스펙 데이터가 없습니다.")

        # 1) 기존 불일치 삭제 (같은 Date 기준 리셋)
        try:
            date_obj = specs[0].date
            InventoryDiscrepancy.objects.filter(date=date_obj).delete()
        except Exception as e:
            logger.warning(f"[Discrepancy] 기존 데이터 삭제 실패: {e}")

        # 2) 재고 일괄 조회 → 매핑
        inventories = get_inventory_for_specifications(specs)
        inv_map = {
            (i.pie_healthcare_num, i.expiry_date, i.medication_lot_number): i
            for i in inventories
        }

        discrepancies = []
        reason_counter = {"미존재": 0, "초과": 0, "모자람": 0}

        # 3) 배치 처리
        batch_size = 100
        for i in range(0, len(specs), batch_size):
            batch = specs[i:i + batch_size]

            for spec in batch:
                d = _calculate_discrepancy_for_spec(spec, inv_map)
                if d:
                    discrepancies.append(d)
                    reason_counter[d.reason] += 1

        # 4) 저장
        if discrepancies:
            try:
                InventoryDiscrepancy.objects.bulk_create(
                    discrepancies,
                    batch_size=batch_size,
                    ignore_conflicts=True
                )
            except Exception as e:
                raise DatabaseOperationError(f"불일치 저장 실패: {e}")

        return {
            "success": True,
            "total_discrepancies": len(discrepancies),
            "reason_breakdown": reason_counter
        }

    except (DiscrepancyCalculationError, DatabaseOperationError) as e:
        logger.exception(f"[Discrepancy] 계산 실패: {e}")
        return {"success": False, "message": str(e)}
    except Exception as e:
        logger.exception(f"[Discrepancy] 예기치 못한 오류: {e}")
        return {"success": False, "message": f"예상치 못한 오류: {e}"}


def execute_discrepancy_check(specs):
    """불일치 계산 래퍼(명시적 엔트리 포인트)."""
    return calculate_and_save_discrepancies(specs)


# =============================================================================
# 해시(제조번호 ↔ 해시) 유틸
# =============================================================================

def _get_cached_hash_map(hashed_codes):
    """
    hashed_code 목록에 대해 캐시/DB에서 original_code 매핑을 조회.

    캐시 정책:
      - HASH_CACHE_KEY에 전체 맵을 저장.
      - 필요 목록만 잘라 사용(부분 dict 생성).
      - 생성/추가 시 cache 무효화(get_or_create_hash)로 일관성 유지.
    """
    cached_map = cache.get(HASH_CACHE_KEY)
    if cached_map is not None:
        return {k: v for k, v in cached_map.items() if k in hashed_codes}

    hash_objects = ManufacturingHash.objects.filter(
        hashed_code__in=hashed_codes
    ).values('hashed_code', 'original_code')

    hash_map = {h['hashed_code']: h['original_code'] for h in hash_objects}
    cache.set(HASH_CACHE_KEY, hash_map, CACHE_TIMEOUT)
    return hash_map


def generate_hash_for_manufacturing_code(code, max_attempts=MAX_HASH_ATTEMPTS):
    """
    제조번호 → 고정 길이 해시 생성(충돌 회피).

    방식:
      - salt: f"{code}:{i}"
      - sha256 → 상위 HASH_LENGTH(9) 대문자
      - 기존 해시 목록과 충돌 없는 값 선택

    Note:
      - 해시 공간이 제한적이라 이론상 충돌 가능성 존재 → max_attempts로 안전장치
    """
    if not code:
        raise ValueError("제조번호가 비어있습니다.")

    existing_hashes = set(
        ManufacturingHash.objects.values_list("hashed_code", flat=True)
    )

    for i in range(max_attempts):
        salt = f"{code}:{i}"
        hashed = hashlib.sha256(salt.encode()).hexdigest()[:HASH_LENGTH].upper()
        if hashed not in existing_hashes:
            return hashed

    raise Exception("제조번호 해시 생성 실패: 충돌 한도 초과")


def get_or_create_hash(code):
    """
    original_code 기준으로 해시 객체 조회 또는 신규 생성.

    캐시 일관성:
      - 신규 생성 시 HASH_CACHE_KEY 무효화(삭제) → 다음 조회 시 재빌드.
    """
    try:
        return ManufacturingHash.objects.get(original_code=code)
    except ManufacturingHash.DoesNotExist:
        hashed_code = generate_hash_for_manufacturing_code(code)
        hash_obj = ManufacturingHash.objects.create(
            original_code=code,
            hashed_code=hashed_code
        )
        cache.delete(HASH_CACHE_KEY)
        return hash_obj


# =============================================================================
# 날짜/캐시 유틸
# =============================================================================

def normalize_date(_date):
    """
    다양한 형태의 입력(문자열/Datetime/Date-like)을 date 객체로 정규화.

    지원 포맷(문자열):
      - YYYYMMDD, YYYY-MM-DD, YYYY/MM/DD
    """
    try:
        if isinstance(_date, str):
            for fmt in ("%Y%m%d", "%Y-%m-%d", "%Y/%m/%d"):
                try:
                    return datetime.strptime(_date, fmt).date()
                except ValueError:
                    continue
            raise DateFormatError(f"지원하지 않는 문자열 날짜 형식: {_date}")
        elif isinstance(_date, datetime):
            return _date.date()
        elif hasattr(_date, 'date'):
            return _date.date()
        else:
            raise DateFormatError(f"지원하지 않는 날짜 타입: {type(_date)}")
    except Exception as e:
        raise DateFormatError(f"날짜 정규화 실패: {e}")


def clear_cache():
    """
    전체 캐시 비우기(운영 환경에서는 주의).
    - 시연/테스트 때 상태 초기화 용도.
    """
    try:
        cache.clear()
        logger.info("[Cache] clear 완료")
        return True
    except Exception as e:
        logger.error(f"[Cache] clear 실패: {e}")
        return False


def get_cache_stats():
    """
    캐시 상태를 간단히 점검(디버깅/시연 가시성).
      - 현재 해시 매핑 엔트리 수
      - 캐시 백엔드/타임아웃
    """
    try:
        hash_cache = cache.get(HASH_CACHE_KEY)
        hash_count = len(hash_cache) if hash_cache else 0

        return {
            "hash_cache_entries": hash_count,
            "cache_timeout": CACHE_TIMEOUT,
            "cache_backend": getattr(settings, 'CACHES', {}).get('default', {}).get('BACKEND', 'unknown')
        }
    except Exception as e:
        logger.error(f"[Cache] 통계 조회 실패: {e}")
        return {"error": str(e)}


# =============================================================================
# 수량 검증/중복 처리/신규 스펙 생성
# =============================================================================

def _validate_stock_quantity(quantity, operation_type):
    """
    수량 sanity check.
      - 음수 입력 방지(출고는 절대값 처리 단계에서 검증)
      - 비현실적 대수량 상한
    """
    if quantity < 0:
        logger.warning(f"[Qty] 음수 감지: {quantity} (type: {operation_type})")
        return False
    if quantity > 999999:
        logger.warning(f"[Qty] 과도한 수량: {quantity} (type: {operation_type})")
        return False
    return True


def _handle_duplicate_specification(spec_map, key, instance, operation_type):
    """
    이미 존재하는 스펙 키일 때 수량 처리 정책.

    정책:
      - 재고: 스캔분만큼 누적(= day snapshot 갱신 맥락에서 덮어쓰기보다 누적이 더 자연스러울 수 있음)
      - 출고: 스펙 수량만큼 차감(음수 방지)
      - 검수: 검수 스캔 수량으로 '설정'
      - 기본: 누적

    Returns:
      (None, updated_spec) 또는 None
    """
    if key in spec_map:
        existing_spec = spec_map[key]

        if operation_type == "재고":
            existing_spec.stock_quantity += instance.scanned_quantity
            logger.info(f"[Dup/재고] 누적: {key} → +{instance.scanned_quantity}")

        elif operation_type == "출고":
            new_quantity = existing_spec.stock_quantity - instance.scanned_quantity
            if not _validate_stock_quantity(new_quantity, operation_type):
                logger.error(f"[Dup/출고] 음수 발생 위험: {existing_spec.stock_quantity} - {instance.scanned_quantity}")
                return None
            existing_spec.stock_quantity = new_quantity
            logger.info(f"[Dup/출고] 차감: {key} → {new_quantity}")

        elif operation_type == "검수":
            existing_spec.stock_quantity = instance.scanned_quantity
            logger.info(f"[Dup/검수] 설정: {key} → {instance.scanned_quantity}")

        else:
            existing_spec.stock_quantity += instance.scanned_quantity
            logger.info(f"[Dup] 누적: {key} → {existing_spec.stock_quantity}")

        existing_spec.date = instance.date  # 최신 날짜로 동기화
        return (None, existing_spec)

    return None


def _create_new_specification(instance, default, operation_type):
    """
    신규 스펙 인스턴스 생성.

    규칙:
      - 출고는 음의 수량으로 기록(후단 재고 반영 로직과 일관)
      - 그 외는 스캔 수량 그대로
    """
    initial_quantity = instance.scanned_quantity

    if operation_type == "출고":
        initial_quantity = -instance.scanned_quantity
        if not _validate_stock_quantity(abs(initial_quantity), operation_type):
            logger.error(f"[NewSpec/출고] 수량 검증 실패: {instance.scanned_quantity}")
            return None

    new_spec = Specification(
        date=instance.date,
        medication_created_by=default.medication_created_by,
        pie_healthcare_num=default.pie_healthcare_num,
        medication_name=default.medication_name,
        medication_size=default.medication_size,
        stock_location=default.stock_location,
        medication_lot_number=instance.medication_lot_number,
        expiry_date=instance.expiry_date,
        stock_quantity=initial_quantity
    )

    logger.info(f"[NewSpec] 생성: pie={default.pie_healthcare_num} qty={initial_quantity}")
    return new_spec


# =============================================================================
# 이월 처리
# =============================================================================

def carry_over_inventory(previous_date_obj, new_date_obj):
    """
    이전 날짜의 재고 스냅샷을 새로운 날짜로 이월(복사/업데이트).

    사용 시점:
      - '출고/검수' 등에서 새로운 날짜가 등장했을 때, 직전 날짜의 상태를 출발점으로 삼고자 할 때.
      - 일자 단위 스냅샷 개념 유지.

    Returns:
      dict: {success, created, updated} 또는 {success: False, message}
    """
    from django.db import transaction
    from inventory2.backend.models.inventory import Inventory2

    try:
        with transaction.atomic():
            prev_inventories = Inventory2.objects.filter(date=previous_date_obj)
            created_count = 0
            updated_count = 0

            for inv in prev_inventories:
                obj, created = Inventory2.objects.update_or_create(
                    date=new_date_obj,
                    pie_healthcare_num=inv.pie_healthcare_num,
                    expiry_date=inv.expiry_date,
                    medication_lot_number=inv.medication_lot_number,
                    defaults={
                        "medication_created_by": inv.medication_created_by,
                        "medication_name": inv.medication_name,
                        "medication_size": inv.medication_size,
                        "stock_location": inv.stock_location,
                        "stock_quantity": inv.stock_quantity
                    }
                )
                if created:
                    created_count += 1
                else:
                    updated_count += 1

            logger.info(f"[CarryOver] {previous_date_obj} → {new_date_obj} | create={created_count}, update={updated_count}")
            return {"success": True, "created": created_count, "updated": updated_count}

    except Exception as e:
        logger.error(f"[CarryOver] 실패: {e}")
        return {"success": False, "message": str(e)}