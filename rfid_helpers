
# -----------------------------
# 쿼리/검수 관련 헬퍼 – 주석 강화
# -----------------------------

def get_outgoing_specifications(date_obj, company_obj):
    """특정 날짜/회사에 기록된 '출고' 타입의 스펙 목록을 반환."""
    outgoing_type = Type.objects.get(name='출고')
    return Specification.objects.filter(
        date__date=date_obj.date,
        date__type=outgoing_type,
        date__company=company_obj
    )


def get_inspection_specifications(date_obj):
    """특정 date(A 회사 기준)에 기록된 '검수' 스펙만 조회."""
    inspection_type = Type.objects.get(name='검수')
    return Specification.objects.filter(
        date__date=date_obj.date,
        date__company=date_obj.company,
        date__type=inspection_type
    )


def get_existing_inventories_for_company(company):
    """회사의 현재 재고 목록을 필요한 필드만 select하여 반환(성능 최적화)."""
    return Inventory2.objects.filter(
        date__company=company
    ).select_related('date').only(
        'pie_healthcare_num', 'expiry_date', 'medication_lot_number', 'stock_quantity', 'date'
    )


def create_inventory_mapping(inventories):
    """Inventory2 목록 → (pie, expiry, lot) 키 매핑 dict."""
    return {
        (inv.pie_healthcare_num, inv.expiry_date, inv.medication_lot_number): inv
        for inv in inventories
    }


def get_discrepancies_for_date(date_obj):
    """특정 date에 저장된 불일치 레코드 조회."""
    return InventoryDiscrepancy.objects.filter(date=date_obj)


@transaction.atomic
@monitor_performance("inspection_transfer")
@monitor_database_queries
def process_inspection_transfer(date_obj, outgoing_specs, inspected_specs):
    """
    출고(-) + 검수(+)의 '부호 있는 합'으로 정상/불일치 판정.
    - 합 == 0  → 정상 매칭
    - 합 <  0  → 출고가 검수보다 많음(모자람/미검수)
    - 합 >  0  → 검수가 출고보다 많음(초과/미출고_검수)
    """

    # 출고는 강제로 음수, 검수는 강제로 양수로 집계(안전 장치)
    def key_of(s):
        return (s.pie_healthcare_num, s.expiry_date, s.medication_lot_number)

    out_sum = defaultdict(int)
    for s in outgoing_specs:
        q = int(s.stock_quantity or 0)
        out_sum[key_of(s)] += -abs(q)  # 출고는 음수로 일괄 처리

    in_sum = defaultdict(int)
    for s in inspected_specs:
        q = int(s.stock_quantity or 0)
        in_sum[key_of(s)] += abs(q)  # 검수는 양수로 일괄 처리

    all_keys = set(out_sum.keys()) | set(in_sum.keys())

    discrepancies = []
    matched = 0

    for k in all_keys:
        out_q = out_sum.get(k, 0)  # ≤ 0
        in_q = in_sum.get(k, 0)  # ≥ 0
        total = out_q + in_q  # 0이면 정상

        if total == 0:
            matched += 1
            continue

        pie, expiry, lot = k
        # 사유 분기
        if out_q == 0 and in_q > 0:
            reason = "미출고_검수"  # 출고 없는데 검수만 있음
            disc_qty = in_q  # > 0
        elif in_q == 0 and out_q < 0:
            reason = "미검수"  # 출고만 있고 검수 없음
            disc_qty = -out_q  # 양수화
        else:
            # 양쪽 다 있는데 차이남
            if total < 0:
                reason = "모자람"  # 출고 > 검수
                disc_qty = -total  # 양수화
            else:
                reason = "초과"  # 검수 > 출고
                disc_qty = total  # 이미 양수

        discrepancies.append(InventoryDiscrepancy(
            date=date_obj,
            pie_healthcare_num=pie,
            medication_lot_number=lot,
            medication_name=None,
            expiry_date=expiry,
            reason=reason,
            discrepancy_quantity=int(disc_qty)
        ))

    if discrepancies:
        InventoryDiscrepancy.objects.bulk_create(discrepancies)

    return Response({
        "status": "불일치 계산 완료",
        "matched_items": matched,
        "total_discrepancies_found": len(discrepancies),
        "total_outgoing_items": len(outgoing_specs),
        "total_inspected_items": len(inspected_specs)
    })


def create_specification_mapping(specs):
    """Specification 목록을 (pie, expiry, lot) 키로 dict 매핑."""
    return {
        (s.pie_healthcare_num, s.expiry_date, s.medication_lot_number): s
        for s in specs
    }


def create_discrepancy_record(date_obj, spec, reason, discrepancy_quantity):
    """불일치 레코드(메모리 객체) 생성. bulk_create로 저장 예정."""
    return InventoryDiscrepancy(
        date=date_obj,
        pie_healthcare_num=spec.pie_healthcare_num,
        medication_lot_number=spec.medication_lot_number,
        medication_name=getattr(spec, "medication_name", None),
        expiry_date=spec.expiry_date,
        reason=reason,
        discrepancy_quantity=int(discrepancy_quantity)
    )


# -----------------------------
# "검수 시점 단일 이동" 전용 헬퍼
# -----------------------------

def compute_spec_key(spec):
    return (spec.pie_healthcare_num, spec.expiry_date, spec.medication_lot_number)


def compute_matched_specs_for_transfer(outgoing_specs, inspected_specs):
    """
    출고(-), 검수(+)를 각각 절대값 합으로 집계 후
    matched = min(out_abs, in_abs) 로 이동 수량 결정.
    반환: dict[key] = matched_qty  (key = (pie, expiry, lot))
    """
    def key_of(s):
        return (s.pie_healthcare_num, s.expiry_date, s.medication_lot_number)

    out_abs = defaultdict(int)
    for s in outgoing_specs:
        out_abs[key_of(s)] += abs(int(s.stock_quantity or 0))

    in_abs = defaultdict(int)
    for s in inspected_specs:
        in_abs[key_of(s)] += abs(int(s.stock_quantity or 0))

    matched = {}
    for k in (set(out_abs.keys()) | set(in_abs.keys())):
        m = min(out_abs.get(k, 0), in_abs.get(k, 0))
        if m > 0:
            matched[k] = m
    return matched


@transaction.atomic
@monitor_performance("apply_transfer_by_match_v2")
@monitor_database_queries
def apply_transfer_by_match_v2(
        matched_specs: dict,  # {(pie, expiry, lot): qty}
        from_company: Company,  # A
        from_date_obj: Date,  # A의 현재 date_obj (검수 요청 date)
        to_company: Company,  # B
        to_date_obj: Date  # B의 동일 날짜, type='재고' Date
):
    """
    matched 만큼:
      - A 재고에서 차감 (date는 from_date_obj로 업데이트)
      - B 재고에 가산 (date는 to_date_obj에 귀속; 없으면 생성)
    """
    if not matched_specs:
        return {"success": True, "moved_keys": 0, "moved_qty": 0, "message": "일치분 없음"}

    # 현재 재고 맵(회사별 전체 재고에서 키 검색)
    from_inv_map = create_inventory_mapping(get_existing_inventories_for_company(from_company))
    to_inv_map = create_inventory_mapping(get_existing_inventories_for_company(to_company))

    to_update_from, to_update_to, to_create = [], [], []
    moved_keys, moved_qty = 0, 0

    for (pie, expiry, lot), qty in matched_specs.items():
        qty = int(abs(qty))

        # --- A에서 차감 ---
        src = from_inv_map.get((pie, expiry, lot))
        if src:
            new_q = max(0, int(src.stock_quantity) - qty)
            if new_q != src.stock_quantity:
                src.stock_quantity = new_q
                src.date = from_date_obj
                to_update_from.append(src)
        else:
            logger.warning(f"[검수이동] A({from_company.company_name}) 재고 없음: {pie}/{expiry}/{lot} - 이동요청 {qty}")

        # --- B에 가산 ---
        dst = to_inv_map.get((pie, expiry, lot))
        if dst:
            dst.stock_quantity = int(dst.stock_quantity) + qty
            dst.date = to_date_obj
            to_update_to.append(dst)
        else:
            to_create.append(Inventory2(
                date=to_date_obj,
                pie_healthcare_num=pie,
                expiry_date=expiry,
                medication_lot_number=lot,
                stock_quantity=qty,
            ))

        moved_keys += 1
        moved_qty += qty

    # DB 반영
    if to_update_from:
        Inventory2.objects.bulk_update(to_update_from, ['stock_quantity', 'date'], batch_size=100)
    if to_update_to:
        Inventory2.objects.bulk_update(to_update_to, ['stock_quantity', 'date'], batch_size=100)
    if to_create:
        Inventory2.objects.bulk_create(to_create, batch_size=100)

    return {
        "success": True,
        "moved_keys": moved_keys,  # 품목 조합 수
        "moved_qty": moved_qty,  # 총 이동 수량
        "updated_from": len(to_update_from),
        "updated_to": len(to_update_to),
        "created_to": len(to_create),
    }


# -----------------------------
# (선택) 불일치 기반 재구성 유틸 – 현재는 리포트 전용으로 사용, 필요시 유지
# -----------------------------

@transaction.atomic
@monitor_performance("inventory_rebuild")
@monitor_database_queries
def rebuild_current_inventory_from_discrepancy(company: Company, date: Date):
    """
    동일 날짜의 불일치 목록을 읽어 수신 회사의 Inventory를 재구성.
    - 현재 플로우에서는 이동을 matched 기반으로 처리하므로 기본적으로 사용하지 않음.
    """
    discrepancies = get_discrepancies_for_date(date)
    current_inventories_qs = get_existing_inventories_for_company(company)
    current_inventory_map = create_inventory_mapping(current_inventories_qs)

    inventories_to_update = []
    updated_count = 0

    for discrepancy in discrepancies:
        try:
            key = (discrepancy.pie_healthcare_num, discrepancy.expiry_date, discrepancy.medication_lot_number)
            current_inventory = current_inventory_map.get(key)
            if current_inventory:
                original_quantity = current_inventory.stock_quantity
                adjust_inventory_quantity(current_inventory, discrepancy)
                if current_inventory.stock_quantity < 0:
                    logger.warning(
                        f"재고 재구성 중 음수 재고 발생 및 0으로 보정: {key} "
                        f"이전: {original_quantity}, 조정 후: {current_inventory.stock_quantity}, 불일치: {discrepancy.discrepancy_quantity} ({discrepancy.reason})"
                    )
                    current_inventory.stock_quantity = 0
                inventories_to_update.append(current_inventory)
                updated_count += 1
            else:
                logger.warning(
                    f"불일치({discrepancy.reason})에 해당하는 재고({discrepancy.pie_healthcare_num})가 "
                    f"회사 {company.company_name}의 재고에 없어 업데이트 불가"
                )
        except Exception as e:
            logger.error(f"재고 재구성 실패: {discrepancy.pie_healthcare_num} - {e}")
            continue

    return execute_inventory_rebuild(inventories_to_update, updated_count, len(discrepancies))


def adjust_inventory_quantity(current_inventory, discrepancy):
    """(비권장: 과거 방식 호환용) 불일치 reason에 따른 수량 가감 규칙."""
    if discrepancy.reason == "초과":
        current_inventory.stock_quantity += int(discrepancy.discrepancy_quantity)
    elif discrepancy.reason in ("모자람", "미검수", "미출고_검수"):
        current_inventory.stock_quantity -= int(discrepancy.discrepancy_quantity)


def execute_inventory_rebuild(inventories_to_update, updated_count, total_discrepancies):
    """재고 재구성 결과를 DB에 반영(bulk_update)하고 요약 리턴."""
    try:
        if inventories_to_update:
            Inventory2.objects.bulk_update(
                inventories_to_update,
                ['stock_quantity'],
                batch_size=100
            )
    except Exception as e:
        logger.error(f"재고 재구성 데이터베이스 저장 실패: {e}")
        return {"success": False, "message": f"재고 재구성 DB 저장 실패: {e}", "updated_inventory": updated_count}

    logger.info(f"재고 재구성 완료: {updated_count}개 재고 업데이트, 총 불일치 {total_discrepancies}개")
    return {
        "status": "재고 재구성 완료",
        "updated_inventory": updated_count,
        "total_discrepancies": total_discrepancies
    }
