import threading
from datetime import date

import pytest
from sqlalchemy.orm import sessionmaker

from tests.test_data_factory import DataFactory

from app.models import InventoryBatch
from app.crud.purchase import crud_delivery, crud_inventory_batch
from app.crud.disposition import (
    crud_disposition_order, crud_inspection_round,
)
from app.services.inspection import InspectionService
from app.services.disposition import (
    DispositionService, QuantityBalanceError,
    RE_INSPECT, CONCESSION, REWORK, RETURN_GOODS, SCRAP,
)
from app.schemas import (
    DeliveryCreate, InspectionCreate,
    DispositionOrderCreate, DispositionDecisionCreate,
    DispositionApproveRequest, ReinspectionCreate,
    ReworkCompleteRequest,
)


def _failed_batch(factory, db, po_no="PO-DISP", del_no="D-DISP", qty=100):
    """创建到货并登记不合格抽检，返回 (delivery, 隔离库存批次)"""
    factory.create_purchase_order(po_no, "TS002", "TM002", quantity=qty,
                                  expected_date=date.today())
    delivery = crud_delivery.create(db, obj_in=DeliveryCreate(
        delivery_no=del_no, purchase_order_id=factory.purchase_orders[po_no].id,
        supplier_id=factory.suppliers["TS002"].id,
        material_id=factory.materials["TM002"].id,
        quantity=qty, delivery_date=date.today(), batch_no=f"B-{del_no}"
    ))
    InspectionService.record_inspection_and_process(db, InspectionCreate(
        delivery_id=delivery.id, sample_size=20, defective_count=8,
        pass_rate=0.6, result="failed", inspector="王检验",
        inspection_date=date.today(), remark="首轮抽检不合格"
    ))
    batch = db.query(InventoryBatch).filter(
        InventoryBatch.delivery_id == delivery.id
    ).first()
    return delivery, batch


def _make_order(db, batch_id, specs, created_by="质量员", remark=None):
    return DispositionService.create_disposition_order(db, DispositionOrderCreate(
        inventory_batch_id=batch_id, created_by=created_by,
        remark=remark,
        decisions=[DispositionDecisionCreate(decision_type=t, quantity=q, reason=r)
                   for t, q, r in specs]
    ))


def _dec(order, dtype):
    return next(d for d in order.decisions if d.decision_type == dtype)


def _approve(db, order, dtype, approver="张主管", reason=None):
    d = _dec(order, dtype)
    return DispositionService.approve_decision(
        db, order.id, d.id,
        DispositionApproveRequest(approver=approver, reason=reason)
    )


class TestDispositionOrderSnapshot:
    """建单即固化取样与检验结论"""

    def test_snapshot_fixed_at_creation(self, db_session):
        factory = DataFactory(db_session)
        factory.setup_basic_supply_chain()
        delivery, batch = _failed_batch(factory, db_session)

        order = _make_order(db_session, batch.id, [(CONCESSION, 100, "让步")])

        assert order.sample_size == 20
        assert order.defective_count == 8
        assert order.pass_rate == 0.6
        assert order.inspection_result == "failed"
        assert order.inspection_round == 1
        assert order.source_quantity == 100
        assert order.allocated_quantity == 0
        assert order.status == "draft"

        # 后续追加复检轮次，处置单快照不变
        d = _dec(order, CONCESSION)
        DispositionService.approve_decision(
            db_session, order.id, d.id, DispositionApproveRequest(approver="张主管"))
        # 让步接收子批次直接放行，无需复检；快照仍是首轮证据
        db_session.refresh(order)
        assert order.sample_size == 20
        assert order.inspection_result == "failed"

    def test_only_quarantined_batch(self, db_session):
        factory = DataFactory(db_session)
        factory.setup_basic_supply_chain()
        normal = factory.create_inventory_batch("TM002", quantity=100, is_quarantined=False)
        with pytest.raises(ValueError):
            _make_order(db_session, normal.id, [(SCRAP, 100, "")])

    def test_quantity_over_batch_rejected(self, db_session):
        factory = DataFactory(db_session)
        factory.setup_basic_supply_chain()
        _, batch = _failed_batch(factory, db_session, qty=100)
        with pytest.raises(QuantityBalanceError):
            _make_order(db_session, batch.id,
                        [(CONCESSION, 60, ""), (SCRAP, 50, "")])

    def test_duplicate_type_and_empty_rejected(self, db_session):
        factory = DataFactory(db_session)
        factory.setup_basic_supply_chain()
        _, batch = _failed_batch(factory, db_session, qty=100)
        with pytest.raises(ValueError):
            _make_order(db_session, batch.id,
                        [(CONCESSION, 30, ""), (CONCESSION, 30, "")])
        with pytest.raises(ValueError):
            _make_order(db_session, batch.id, [])

    def test_reservation_blocks_overlapping_orders(self, db_session):
        factory = DataFactory(db_session)
        factory.setup_basic_supply_chain()
        _, batch = _failed_batch(factory, db_session, qty=100)
        # 第一张单待审批预留 60
        _make_order(db_session, batch.id, [(CONCESSION, 60, "")])
        # 第二张单最多只能再占 40
        with pytest.raises(QuantityBalanceError):
            _make_order(db_session, batch.id, [(SCRAP, 41, "")])


class TestSplitDisposition:
    """一批分多路去向：让步/返工/退货/报废"""

    def test_full_split_and_traceability(self, db_session):
        factory = DataFactory(db_session)
        factory.setup_basic_supply_chain()
        delivery, batch = _failed_batch(factory, db_session, qty=100)

        order = _make_order(db_session, batch.id, [
            (CONCESSION, 40, "外观轻微不良，让步接收"),
            (REWORK, 30, "可返工"),
            (RETURN_GOODS, 10, "退货供应商"),
            (SCRAP, 20, "报废处理"),
        ])

        _approve(db_session, order, CONCESSION)
        _approve(db_session, order, REWORK)
        _approve(db_session, order, RETURN_GOODS)
        _approve(db_session, order, SCRAP)

        db_session.refresh(order)
        assert order.status == "closed"
        assert order.allocated_quantity == 100

        # 每个决定都有可追溯子批次/分录，溯源链完整
        children = {}
        for dtype in (CONCESSION, REWORK, RETURN_GOODS, SCRAP):
            d = _dec(order, dtype)
            assert d.status == "approved"
            assert d.child_batch_id is not None
            child = crud_inventory_batch.get(db_session, d.child_batch_id)
            assert child.origin_batch_id == batch.id
            assert child.origin_disposition_id == d.id
            children[dtype] = child

        assert children[CONCESSION].quantity == 40
        assert children[CONCESSION].available_quantity == 40
        assert children[CONCESSION].is_quarantined is False
        assert children[CONCESSION].status == "normal"

        assert children[REWORK].quantity == 30
        assert children[REWORK].available_quantity == 0
        assert children[REWORK].is_quarantined is True
        assert children[REWORK].status == "rework"

        assert children[RETURN_GOODS].status == "returned"
        assert children[SCRAP].status == "scrapped"
        assert children[RETURN_GOODS].available_quantity == 0
        assert children[SCRAP].available_quantity == 0

        # 不变量：原始数量 = 所有去向之和
        DispositionService.assert_integrity(db_session, batch.id)
        balance = DispositionService.get_balance(db_session, batch.id)
        assert balance.is_balanced is True
        assert balance.allocated_quantity == 100
        assert balance.approved_by_type[CONCESSION] == 40
        assert balance.approved_by_type[REWORK] == 30
        assert balance.approved_by_type[RETURN_GOODS] == 10
        assert balance.approved_by_type[SCRAP] == 20

    def test_only_released_part_can_be_consumed(self, db_session):
        factory = DataFactory(db_session)
        factory.setup_basic_supply_chain()
        _, batch = _failed_batch(factory, db_session, qty=100)
        order = _make_order(db_session, batch.id, [
            (CONCESSION, 40, ""), (REWORK, 30, ""),
            (RETURN_GOODS, 10, ""), (SCRAP, 20, ""),
        ])
        for t in (CONCESSION, REWORK, RETURN_GOODS, SCRAP):
            _approve(db_session, order, t)

        material_id = factory.materials["TM002"].id
        available = crud_inventory_batch.get_available_batches(db_session, material_id)
        assert sum(b.available_quantity for b in available) == 40

        # 50 件领不出：返工/退货/报废与父批都不能参与
        assert InspectionService.consume_material(db_session, material_id, 50) is False
        # 释放的 40 件可以领
        assert InspectionService.consume_material(db_session, material_id, 40) is True
        assert InspectionService.consume_material(db_session, material_id, 1) is False
        DispositionService.assert_integrity(db_session, batch.id)

    def test_reject_then_redispose_remaining(self, db_session):
        """驳回不产生任何去向，数量可再建新单处置"""
        factory = DataFactory(db_session)
        factory.setup_basic_supply_chain()
        _, batch = _failed_batch(factory, db_session, qty=100)
        order = _make_order(db_session, batch.id,
                            [(CONCESSION, 40, ""), (SCRAP, 60, "")])
        _approve(db_session, order, CONCESSION)
        scrap = _dec(order, SCRAP)
        DispositionService.reject_decision(
            db_session, order.id, scrap.id,
            DispositionApproveRequest(approver="李经理", reason="报废数量待复核"))
        db_session.refresh(order)
        assert order.status == "closed"  # 无待决决定
        assert order.allocated_quantity == 40

        # 60 件可重新处置
        order2 = _make_order(db_session, batch.id,
                             [(RETURN_GOODS, 60, "改判退货")])
        _approve(db_session, order2, RETURN_GOODS)
        balance = DispositionService.get_balance(db_session, batch.id)
        assert balance.allocated_quantity == 100
        assert balance.available_for_new_disposition == 0
        DispositionService.assert_integrity(db_session, batch.id)


class TestReinspection:
    """复检结论只追加；通过才放行，推翻不改写原证据"""

    def test_reinspect_pass_releases_child(self, db_session):
        factory = DataFactory(db_session)
        factory.setup_basic_supply_chain()
        delivery, batch = _failed_batch(factory, db_session, qty=100)
        order = _make_order(db_session, batch.id,
                            [(RE_INSPECT, 60, ""), (CONCESSION, 40, "")])
        _approve(db_session, order, RE_INSPECT)
        _approve(db_session, order, CONCESSION)

        re_dec = _dec(order, RE_INSPECT)
        child = crud_inventory_batch.get(db_session, re_dec.child_batch_id)
        assert child.is_quarantined is True
        assert child.available_quantity == 0

        # 复检合格登记 → 子批次放行
        DispositionService.record_reinspection(db_session, order.id, re_dec.id,
                                               ReinspectionCreate(
            sample_size=30, defective_count=0, pass_rate=1.0, result="passed",
            inspector="复检员", inspection_date=date.today()))
        db_session.refresh(child)
        db_session.refresh(re_dec)
        assert child.is_quarantined is False
        assert child.available_quantity == 60
        assert re_dec.reinspect_result == "passed"

        # 检验轮次只追加：首轮仍在，最新轮为合格
        rounds = crud_inspection_round.list_by_delivery(db_session, delivery.id)
        assert [r.round_no for r in rounds] == [1, 2]
        assert [r.result for r in rounds] == ["failed", "passed"]
        latest = crud_inspection_round.latest_by_delivery(db_session, delivery.id)
        assert latest.round_no == 2 and latest.is_latest is True
        assert rounds[0].is_latest is False

        # 处置单快照仍是首轮不合格证据
        db_session.refresh(order)
        assert order.inspection_round == 1
        assert order.inspection_result == "failed"

        # 放行后可领料 40+60=100
        material_id = factory.materials["TM002"].id
        assert InspectionService.consume_material(db_session, material_id, 100) is True
        DispositionService.assert_integrity(db_session, batch.id)

    def test_reinspect_fail_keeps_quarantine_and_redispose(self, db_session):
        factory = DataFactory(db_session)
        factory.setup_basic_supply_chain()
        delivery, batch = _failed_batch(factory, db_session, qty=100)
        order = _make_order(db_session, batch.id, [(RE_INSPECT, 100, "")])
        _approve(db_session, order, RE_INSPECT)
        re_dec = _dec(order, RE_INSPECT)

        DispositionService.record_reinspection(db_session, order.id, re_dec.id,
                                               ReinspectionCreate(
            sample_size=30, defective_count=9, pass_rate=0.7, result="failed",
            inspector="复检员", inspection_date=date.today()))
        db_session.refresh(re_dec)
        assert re_dec.reinspect_result == "failed"
        child = crud_inventory_batch.get(db_session, re_dec.child_batch_id)
        assert child.is_quarantined is True
        assert child.available_quantity == 0

        # 复检子批次本身是隔离批，可对其再处置
        order2 = _make_order(db_session, child.id,
                             [(CONCESSION, 60, ""), (SCRAP, 40, "")])
        _approve(db_session, order2, CONCESSION)
        _approve(db_session, order2, SCRAP)
        db_session.refresh(order2)
        assert order2.status == "closed"
        # 原批与子批两层账都平
        DispositionService.assert_integrity(db_session, child.id)
        DispositionService.assert_integrity(db_session, batch.id)

    def test_reinspection_cannot_overwrite(self, db_session):
        factory = DataFactory(db_session)
        factory.setup_basic_supply_chain()
        _, batch = _failed_batch(factory, db_session, qty=100)
        order = _make_order(db_session, batch.id, [(RE_INSPECT, 100, "")])
        _approve(db_session, order, RE_INSPECT)
        re_dec = _dec(order, RE_INSPECT)
        payload = ReinspectionCreate(
            sample_size=30, defective_count=0, pass_rate=1.0, result="passed",
            inspection_date=date.today())
        DispositionService.record_reinspection(db_session, order.id, re_dec.id, payload)
        # 结论不可改写
        with pytest.raises(ValueError):
            DispositionService.record_reinspection(
                db_session, order.id, re_dec.id,
                payload.model_copy(update={"result": "failed"}))

    def test_reinspection_validation(self, db_session):
        factory = DataFactory(db_session)
        factory.setup_basic_supply_chain()
        _, batch = _failed_batch(factory, db_session, qty=100)
        order = _make_order(db_session, batch.id, [(RE_INSPECT, 50, ""),
                                                   (CONCESSION, 50, "")])
        _approve(db_session, order, RE_INSPECT)
        re_dec = _dec(order, RE_INSPECT)
        with pytest.raises(ValueError):
            DispositionService.record_reinspection(db_session, order.id, re_dec.id,
                ReinspectionCreate(sample_size=0, defective_count=0,
                                   pass_rate=0, result="passed"))
        with pytest.raises(ValueError):
            DispositionService.record_reinspection(db_session, order.id, re_dec.id,
                ReinspectionCreate(sample_size=10, defective_count=20,
                                   pass_rate=0, result="passed"))
        # 让步接收决定不能登记复检
        con = _dec(order, CONCESSION)
        with pytest.raises(ValueError):
            DispositionService.record_reinspection(db_session, order.id, con.id,
                ReinspectionCreate(sample_size=10, defective_count=0,
                                   pass_rate=1.0, result="passed"))


class TestReworkComplete:
    """返工完成拆出放行孙批 + 未完成隔离孙批，多层批次树数量恒等"""

    def test_rework_partial_completion(self, db_session):
        factory = DataFactory(db_session)
        factory.setup_basic_supply_chain()
        _, batch = _failed_batch(factory, db_session, qty=100)
        order = _make_order(db_session, batch.id, [(REWORK, 100, "")])
        _approve(db_session, order, REWORK)
        rw = _dec(order, REWORK)
        container = crud_inventory_batch.get(db_session, rw.child_batch_id)
        assert container.status == "rework"
        assert container.available_quantity == 0

        DispositionService.complete_rework(db_session, order.id, rw.id,
                                           ReworkCompleteRequest(
            completed_quantity=70, inspector="返工检验员", remark="70件修好"))
        db_session.refresh(rw)
        assert rw.rework_completed_qty == 70

        children = crud_inventory_batch.get_children(db_session, container.id)
        assert len(children) == 2
        good = next(c for c in children if c.status == "normal")
        bad = next(c for c in children if c.status == "quarantined")
        assert good.quantity == 70 and good.available_quantity == 70
        assert good.origin_disposition_id == rw.id
        assert bad.quantity == 30 and bad.available_quantity == 0

        # 整棵树叶子合计 = 100，放行部分有返工合格依据
        DispositionService.assert_integrity(db_session, batch.id)
        # 返工未完成的 30 件可再处置（让步接收）
        order2 = _make_order(db_session, bad.id, [(CONCESSION, 30, "")])
        _approve(db_session, order2, CONCESSION)
        DispositionService.assert_integrity(db_session, batch.id)

        # 最终可领 70+30=100
        material_id = factory.materials["TM002"].id
        assert InspectionService.consume_material(db_session, material_id, 100) is True
        DispositionService.assert_integrity(db_session, batch.id)

    def test_rework_completion_once_only(self, db_session):
        factory = DataFactory(db_session)
        factory.setup_basic_supply_chain()
        _, batch = _failed_batch(factory, db_session, qty=100)
        order = _make_order(db_session, batch.id, [(REWORK, 100, "")])
        _approve(db_session, order, REWORK)
        rw = _dec(order, REWORK)
        req = ReworkCompleteRequest(completed_quantity=100)
        DispositionService.complete_rework(db_session, order.id, rw.id, req)
        with pytest.raises(ValueError):
            DispositionService.complete_rework(db_session, order.id, rw.id, req)
        with pytest.raises(ValueError):
            DispositionService.complete_rework(
                db_session, order.id, rw.id,
                ReworkCompleteRequest(completed_quantity=101))


class TestReverseDecision:
    """后续复检/审批推翻原结论：数量与证据同时回滚"""

    def test_reverse_approved_concession(self, db_session):
        factory = DataFactory(db_session)
        factory.setup_basic_supply_chain()
        _, batch = _failed_batch(factory, db_session, qty=100)
        order = _make_order(db_session, batch.id, [(CONCESSION, 40, ""),
                                                   (SCRAP, 60, "")])
        _approve(db_session, order, CONCESSION)
        con = _dec(order, CONCESSION)
        old_child_id = con.child_batch_id

        DispositionService.reverse_decision(
            db_session, order.id, con.id, "经理", "复检推翻：实际不合格")
        db_session.refresh(order)
        db_session.refresh(con)
        assert con.status == "reversed"
        assert con.child_batch_id is None
        assert order.allocated_quantity == 0
        assert order.status == "approving"
        # 子批次已撤销
        assert crud_inventory_batch.get(db_session, old_child_id) is None
        # 数量回到可处置池（先驳回旧单仍待决的报废决定，释放预留）
        balance = DispositionService.get_balance(db_session, batch.id)
        assert balance.available_for_new_disposition == 40
        DispositionService.reject_decision(
            db_session, order.id, _dec(order, SCRAP).id,
            DispositionApproveRequest(approver="李经理", reason="原处置单作废"))
        balance = DispositionService.get_balance(db_session, batch.id)
        assert balance.available_for_new_disposition == 100
        order2 = _make_order(db_session, batch.id,
                             [(RETURN_GOODS, 40, "推翻后退货"), (SCRAP, 60, "")])
        _approve(db_session, order2, RETURN_GOODS)
        _approve(db_session, order2, SCRAP)
        DispositionService.assert_integrity(db_session, batch.id)

    def test_reverse_blocked_after_consumption(self, db_session):
        factory = DataFactory(db_session)
        factory.setup_basic_supply_chain()
        _, batch = _failed_batch(factory, db_session, qty=100)
        order = _make_order(db_session, batch.id, [(CONCESSION, 40, ""),
                                                   (SCRAP, 60, "")])
        _approve(db_session, order, CONCESSION)
        _approve(db_session, order, SCRAP)
        con = _dec(order, CONCESSION)
        InspectionService.consume_material(
            db_session, factory.materials["TM002"].id, 10)
        with pytest.raises(QuantityBalanceError):
            DispositionService.reverse_decision(
                db_session, order.id, con.id, "经理", "试图推翻")
        # 子批次仍在、仍可追溯
        db_session.refresh(con)
        assert con.status == "approved"
        assert crud_inventory_batch.get(db_session, con.child_batch_id) is not None


class TestAtomicityAndGuards:
    """失败不留半成品；旧的整批放行入口被处置单管住"""

    def test_double_approval_is_idempotent_guard(self, db_session):
        factory = DataFactory(db_session)
        factory.setup_basic_supply_chain()
        _, batch = _failed_batch(factory, db_session, qty=100)
        order = _make_order(db_session, batch.id, [(CONCESSION, 40, ""),
                                                   (SCRAP, 60, "")])
        _approve(db_session, order, CONCESSION)
        con = _dec(order, CONCESSION)
        with pytest.raises(ValueError):
            DispositionService.approve_decision(
                db_session, order.id, con.id,
                DispositionApproveRequest(approver="张主管"))
        db_session.refresh(order)
        assert order.allocated_quantity == 40  # 未重复计数

    def test_legacy_release_blocked_with_disposition(self, db_session):
        factory = DataFactory(db_session)
        factory.setup_basic_supply_chain()
        _, batch = _failed_batch(factory, db_session, qty=100)
        _make_order(db_session, batch.id, [(CONCESSION, 100, "")])
        with pytest.raises(ValueError):
            InspectionService.release_quarantined_batch(
                db_session, batch.id, "绕过处置单整批放行")

    def test_failed_approval_leaves_no_child(self, db_session):
        """审批数量校验失败时，不产生任何子批次、不推进 allocated"""
        factory = DataFactory(db_session)
        factory.setup_basic_supply_chain()
        _, batch = _failed_batch(factory, db_session, qty=100)
        order = _make_order(db_session, batch.id, [(SCRAP, 100, "")])
        scrap = _dec(order, SCRAP)
        # 人为把单据 allocated 调高模拟异常并发：CAS 必须拦下
        db_session.query(type(order)).filter(
            type(order).id == order.id).update({"allocated_quantity": 50})
        db_session.commit()
        with pytest.raises(QuantityBalanceError):
            DispositionService.approve_decision(
                db_session, order.id, scrap.id,
                DispositionApproveRequest(approver="张主管"))
        db_session.refresh(scrap)
        assert scrap.status == "pending"
        assert scrap.child_batch_id is None
        balance = DispositionService.get_balance(db_session, batch.id)
        # 待决 100 仍预留，没有任何已释放库存
        assert balance.released_available_quantity == 0
        assert balance.pending_quantity == 100


class TestConcurrentApprovalAndConsume:
    """多处置同时审批 + 生产领料并发：数量不超分、不超领"""

    @staticmethod
    def _run_threads(target, n=2):
        threads = [threading.Thread(target=target) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    def test_same_decision_approved_concurrently_once(self, db_engine):
        SessionLocal = sessionmaker(bind=db_engine, autocommit=False, autoflush=False)

        def setup():
            s = SessionLocal()
            try:
                factory = DataFactory(s)
                factory.setup_basic_supply_chain()
                _, batch = _failed_batch(factory, s, qty=100)
                order = _make_order(s, batch.id, [(CONCESSION, 100, "")])
                return order.id, _dec(order, CONCESSION).id
            finally:
                s.close()

        order_id, decision_id = setup()
        results = []

        def attempt():
            s = SessionLocal()
            try:
                DispositionService.approve_decision(
                    s, order_id, decision_id,
                    DispositionApproveRequest(approver="并发主管"))
                results.append(True)
            except Exception:
                results.append(False)
            finally:
                s.close()

        self._run_threads(attempt)
        assert results.count(True) == 1
        assert results.count(False) == 1

        s = SessionLocal()
        try:
            order = crud_disposition_order.get_with_decisions(s, order_id)
            assert order.allocated_quantity == 100
            children = [d.child_batch_id for d in order.decisions
                        if d.status == "approved" and d.child_batch_id]
            assert len(children) == 1
        finally:
            s.close()

    def test_two_orders_concurrent_total_not_exceed(self, db_engine):
        SessionLocal = sessionmaker(bind=db_engine, autocommit=False, autoflush=False)

        def setup():
            s = SessionLocal()
            try:
                factory = DataFactory(s)
                factory.setup_basic_supply_chain()
                _, batch = _failed_batch(factory, s, qty=100)
                o1 = _make_order(s, batch.id, [(CONCESSION, 60, "")])
                o2 = _make_order(s, batch.id, [(SCRAP, 40, "")])
                return batch.id, o1.id, _dec(o1, CONCESSION).id, o2.id, _dec(o2, SCRAP).id
            finally:
                s.close()

        batch_id, o1, d1, o2, d2 = setup()
        outcomes = []

        def approve(order_id, decision_id, tag):
            def _run():
                s = SessionLocal()
                try:
                    DispositionService.approve_decision(
                        s, order_id, decision_id,
                        DispositionApproveRequest(approver="并发主管"))
                    outcomes.append((tag, True))
                except Exception:
                    outcomes.append((tag, False))
                finally:
                    s.close()
            return _run

        t1 = threading.Thread(target=approve(o1, d1, "A"))
        t2 = threading.Thread(target=approve(o2, d2, "B"))
        t1.start(); t2.start(); t1.join(); t2.join()

        assert dict(outcomes)["A"] is True
        assert dict(outcomes)["B"] is True
        s = SessionLocal()
        try:
            balance = DispositionService.get_balance(s, batch_id)
            assert balance.allocated_quantity == 100
            assert balance.is_balanced is True
            DispositionService.assert_integrity(s, batch_id)
        finally:
            s.close()

    def test_concurrent_consume_never_oversell(self, db_engine):
        import time
        SessionLocal = sessionmaker(bind=db_engine, autocommit=False, autoflush=False)

        def setup():
            s = SessionLocal()
            try:
                factory = DataFactory(s)
                factory.setup_basic_supply_chain()
                _, batch = _failed_batch(factory, s, qty=100)
                order = _make_order(s, batch.id, [(CONCESSION, 30, ""),
                                                  (SCRAP, 70, "")])
                _approve(s, order, CONCESSION)
                _approve(s, order, SCRAP)
                return factory.materials["TM002"].id
            finally:
                s.close()

        material_id = setup()
        results = []

        def consume():
            s = SessionLocal()
            try:
                for _ in range(20):
                    try:
                        ok = InspectionService.consume_material(s, material_id, 20)
                        if ok:
                            results.append(True)
                            return
                        results.append(False)
                        return
                    except Exception:
                        time.sleep(0.01)
                        continue
                results.append(False)
            finally:
                s.close()

        self._run_threads(consume)
        # 只有 30 件可用：两个线程各领 20，仅一个成功，绝不超领
        assert results.count(True) == 1
        s = SessionLocal()
        try:
            batches = crud_inventory_batch.get_available_batches(s, material_id)
            remaining = sum(b.available_quantity for b in batches)
            assert remaining == 10
        finally:
            s.close()

    def test_concurrent_create_orders_reservation_exact(self, db_engine):
        SessionLocal = sessionmaker(bind=db_engine, autocommit=False, autoflush=False)

        def setup():
            s = SessionLocal()
            try:
                factory = DataFactory(s)
                factory.setup_basic_supply_chain()
                _, batch = _failed_batch(factory, s, qty=100)
                return batch.id
            finally:
                s.close()

        batch_id = setup()
        created = []

        def create_order(idx):
            s = SessionLocal()
            try:
                _make_order(s, batch_id, [(CONCESSION, 40, f"并发{idx}")])
                created.append(True)
            except Exception:
                created.append(False)
            finally:
                s.close()

        threads = [threading.Thread(target=create_order, args=(i,)) for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 100 件、每单预留 40：恰好 2 张成功，1 张被预留校验拒绝
        assert created.count(True) == 2, created
        assert created.count(False) == 1
        s = SessionLocal()
        try:
            balance = DispositionService.get_balance(s, batch_id)
            assert balance.pending_quantity == 80
            assert balance.available_for_new_disposition == 20
            assert balance.is_balanced is True
        finally:
            s.close()

