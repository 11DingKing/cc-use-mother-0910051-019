import threading
import pytest
from datetime import date, timedelta
from sqlalchemy.orm import sessionmaker

from tests.test_data_factory import DataFactory

from app.services.inspection import InspectionService
from app.services.disposition import DispositionService
from app.crud.disposition import crud_disposition_order
from app.schemas import (
    InspectionCreate, DispositionOrderCreate, DispositionLineCreate,
    RecheckResultSubmit
)
from app.models import InventoryBatch, DispositionLine


def make_quarantined_batch(db, factory, qty=100, tag="001"):
    """走真实流程：采购订单 → 到货 → 抽检不合格 → 自动隔离批次。"""
    factory.create_purchase_order(
        f"PO-DISP-{tag}", "TS002", "TM002",
        quantity=qty, expected_date=date.today()
    )
    delivery = factory.create_delivery(
        f"DEL-DISP-{tag}", f"PO-DISP-{tag}", "TS002", "TM002",
        quantity=qty, delivery_date=date.today(), batch_no=f"BATCH-DISP-{tag}"
    )
    inspection = InspectionService.record_inspection_and_process(
        db,
        InspectionCreate(
            delivery_id=delivery.id, sample_size=50, defective_count=20,
            pass_rate=0.60, result="failed", inspector="检验员A",
            inspection_date=date.today(), remark="抽检不合格"
        )
    )
    batch = db.query(InventoryBatch).filter(
        InventoryBatch.delivery_id == delivery.id
    ).one()
    assert batch.is_quarantined and batch.available_quantity == 0
    return inspection, batch


def create_order_with_lines(db, batch, lines):
    return DispositionService.create_order(
        db,
        DispositionOrderCreate(
            inventory_batch_id=batch.id,
            lines=[DispositionLineCreate(**l) for l in lines]
        )
    )


class TestDispositionOrderCreation:
    """处置单创建：固定取样与检验结论"""

    def test_create_snapshots_inspection_conclusion(self, db_session):
        factory = DataFactory(db_session)
        factory.setup_basic_supply_chain()
        inspection, batch = make_quarantined_batch(db_session, factory, 100)

        order = DispositionService.create_order(
            db_session, DispositionOrderCreate(inventory_batch_id=batch.id)
        )

        assert order.order_no.startswith("DISP-")
        assert order.status == "open"
        assert order.total_quantity == 100
        assert order.conclusion_source == "inspection"
        assert order.inspection_id == inspection.id
        # 检验结论被冻结到处置单上，作为后续所有处置的依据
        assert order.sample_size == 50
        assert order.defective_count == 20
        assert order.pass_rate == 0.60
        assert order.inspection_result == "failed"

    def test_create_rejects_non_quarantined_batch(self, db_session):
        factory = DataFactory(db_session)
        factory.setup_basic_supply_chain()
        normal_batch = factory.create_inventory_batch(
            "TM002", quantity=50, is_quarantined=False
        )
        with pytest.raises(ValueError, match="隔离批次"):
            DispositionService.create_order(
                db_session, DispositionOrderCreate(inventory_batch_id=normal_batch.id)
            )

    def test_create_rejects_duplicate_order_for_same_batch(self, db_session):
        factory = DataFactory(db_session)
        factory.setup_basic_supply_chain()
        _, batch = make_quarantined_batch(db_session, factory, 100)
        DispositionService.create_order(
            db_session, DispositionOrderCreate(inventory_batch_id=batch.id)
        )
        with pytest.raises(ValueError, match="已存在检验处置单"):
            DispositionService.create_order(
                db_session, DispositionOrderCreate(inventory_batch_id=batch.id)
            )

    def test_create_with_initial_lines_validates_total(self, db_session):
        factory = DataFactory(db_session)
        factory.setup_basic_supply_chain()
        _, batch = make_quarantined_batch(db_session, factory, 100)
        with pytest.raises(ValueError, match="超出待分配数量"):
            create_order_with_lines(db_session, batch, [
                {"action": "concession", "quantity": 60},
                {"action": "scrap", "quantity": 50},
            ])
        # 失败后不能留下半创建的处置单
        assert crud_disposition_order.get_by_batch(db_session, batch.id) is None

    def test_create_rejects_unknown_action(self, db_session):
        factory = DataFactory(db_session)
        factory.setup_basic_supply_chain()
        _, batch = make_quarantined_batch(db_session, factory, 100)
        with pytest.raises(ValueError, match="不支持的处置方式"):
            create_order_with_lines(db_session, batch, [
                {"action": "giveaway", "quantity": 10},
            ])


class TestDispositionApproval:
    """处置审批：批准后生成可追溯子批次，释放部分才能领料"""

    def test_mixed_disposition_conserves_quantity(self, db_session):
        """核心场景：200件隔离 → 让步120 + 返工30 + 退货20 + 报废30"""
        factory = DataFactory(db_session)
        factory.setup_basic_supply_chain()
        _, batch = make_quarantined_batch(db_session, factory, 200)

        order = create_order_with_lines(db_session, batch, [
            {"action": "concession", "quantity": 120, "reason": "关键尺寸在公差带内"},
            {"action": "rework", "quantity": 30, "reason": "表面划伤可返修"},
            {"action": "return", "quantity": 20, "reason": "错料退回供应商"},
            {"action": "scrap", "quantity": 30, "reason": "裂纹不可修复"},
        ])
        for line in order.lines:
            DispositionService.approve_line(
                db_session, order.id, line.id, approver="质量经理"
            )

        db_session.refresh(order)
        assert order.status == "completed", "全部数量有去向后处置单应完结"

        children = db_session.query(InventoryBatch).filter(
            InventoryBatch.parent_batch_id == batch.id
        ).all()
        assert len(children) == 4
        # 不变量：原始数量 = 所有去向之和
        assert sum(c.quantity for c in children) == 200
        # 每个子批次都携带处置依据
        for child in children:
            assert child.disposition_line_id is not None
            assert child.batch_no.startswith(order.order_no)

        # 只有让步接收的部分可领料
        concession = next(c for c in children if c.location == "合格区")
        assert concession.is_quarantined == False
        assert concession.available_quantity == 120
        for loc in ("返工区", "退货区", "报废区"):
            child = next(c for c in children if c.location == loc)
            assert child.is_quarantined == True
            assert child.available_quantity == 0

        # 领料验证：只能领释放出来的120件
        material_id = batch.material_id
        assert InspectionService.consume_material(db_session, material_id, 121) == False
        assert InspectionService.consume_material(db_session, material_id, 120) == True

        # 原批次证据保持不变
        db_session.refresh(batch)
        assert batch.quantity == 200
        assert batch.is_quarantined == True

    def test_trace_balanced_after_full_disposition(self, db_session):
        factory = DataFactory(db_session)
        factory.setup_basic_supply_chain()
        _, batch = make_quarantined_batch(db_session, factory, 150)
        order = create_order_with_lines(db_session, batch, [
            {"action": "concession", "quantity": 100},
            {"action": "scrap", "quantity": 50},
        ])
        for line in order.lines:
            DispositionService.approve_line(db_session, order.id, line.id, "审批人")

        trace = DispositionService.get_trace(db_session, order.id)
        assert trace.balanced == True
        assert trace.total_quantity == 150
        assert trace.finalized_quantity == 150
        assert trace.remaining_quantity == 0
        assert trace.child_batch_quantity_sum == 150
        assert trace.releasable_quantity == 100

    def test_reject_line_returns_quantity_to_pool(self, db_session):
        factory = DataFactory(db_session)
        factory.setup_basic_supply_chain()
        _, batch = make_quarantined_batch(db_session, factory, 100)
        order = create_order_with_lines(db_session, batch, [
            {"action": "scrap", "quantity": 40},
        ])
        line = order.lines[0]
        DispositionService.reject_line(db_session, order.id, line.id, "审批人", "证据不足")

        detail = DispositionService.get_order_detail(db_session, order.id)
        assert detail.remaining_quantity == 100, "驳回后数量应回到待分配池"
        assert detail.status == "open"

        # 驳回的数量可以重新分配
        DispositionService.add_line(
            db_session, order.id,
            DispositionLineCreate(action="concession", quantity=100)
        )
        detail = DispositionService.get_order_detail(db_session, order.id)
        assert detail.remaining_quantity == 0

    def test_cannot_exceed_total_when_adding_lines(self, db_session):
        factory = DataFactory(db_session)
        factory.setup_basic_supply_chain()
        _, batch = make_quarantined_batch(db_session, factory, 100)
        order = create_order_with_lines(db_session, batch, [
            {"action": "concession", "quantity": 70},
        ])
        with pytest.raises(ValueError, match="超出待分配数量"):
            DispositionService.add_line(
                db_session, order.id,
                DispositionLineCreate(action="scrap", quantity=31)
            )

    def test_approve_twice_rejected(self, db_session):
        factory = DataFactory(db_session)
        factory.setup_basic_supply_chain()
        _, batch = make_quarantined_batch(db_session, factory, 100)
        order = create_order_with_lines(db_session, batch, [
            {"action": "concession", "quantity": 50},
        ])
        line = order.lines[0]
        DispositionService.approve_line(db_session, order.id, line.id, "审批人")
        with pytest.raises(ValueError, match="不允许批准"):
            DispositionService.approve_line(db_session, order.id, line.id, "审批人")
        # 重复批准没有生成第二个子批次
        children = db_session.query(InventoryBatch).filter(
            InventoryBatch.parent_batch_id == batch.id
        ).all()
        assert len(children) == 1


class TestRecheckFlow:
    """复检流程：复检可以推翻原结论，失败批次可再开处置单"""

    def test_recheck_passed_overturns_conclusion_and_releases(self, db_session):
        factory = DataFactory(db_session)
        factory.setup_basic_supply_chain()
        _, batch = make_quarantined_batch(db_session, factory, 100)
        order = create_order_with_lines(db_session, batch, [
            {"action": "recheck", "quantity": 100, "reason": "供应商异议，申请全数复检"},
        ])
        line = order.lines[0]
        DispositionService.approve_line(db_session, order.id, line.id, "审批人")

        # 批准复检后：数量进入复检区，仍不可领
        child = db_session.query(InventoryBatch).filter(
            InventoryBatch.parent_batch_id == batch.id
        ).one()
        assert child.location == "复检区"
        assert child.is_quarantined == True
        assert child.available_quantity == 0
        assert InspectionService.consume_material(db_session, batch.material_id, 1) == False

        # 录入复检结论：合格，推翻原判
        DispositionService.submit_recheck_result(
            db_session, order.id, line.id,
            RecheckResultSubmit(
                sample_size=100, defective_count=2, pass_rate=0.98,
                result="passed", inspector="复检员B",
                inspection_date=date.today(), remark="全数复检合格"
            )
        )
        db_session.refresh(child)
        assert child.is_quarantined == False
        assert child.available_quantity == 100
        assert child.location == "合格区"

        db_session.refresh(order)
        assert order.status == "completed"

        # 复检结论固定在处置行上
        db_session.refresh(line)
        assert line.recheck_result == "passed"
        assert line.recheck_sample_size == 100
        assert line.status == "executed"

        # 释放的数量可以领料
        assert InspectionService.consume_material(db_session, batch.material_id, 100) == True

    def test_recheck_failed_allows_follow_up_order(self, db_session):
        """复检维持不合格 → 子批次再开处置单，新单固定的是复检结论"""
        factory = DataFactory(db_session)
        factory.setup_basic_supply_chain()
        _, batch = make_quarantined_batch(db_session, factory, 100)
        order = create_order_with_lines(db_session, batch, [
            {"action": "recheck", "quantity": 100},
        ])
        line = order.lines[0]
        DispositionService.approve_line(db_session, order.id, line.id, "审批人")
        DispositionService.submit_recheck_result(
            db_session, order.id, line.id,
            RecheckResultSubmit(
                sample_size=100, defective_count=30, pass_rate=0.70,
                result="failed", inspector="复检员B", inspection_date=date.today()
            )
        )
        child = db_session.query(InventoryBatch).filter(
            InventoryBatch.parent_batch_id == batch.id
        ).one()
        assert child.is_quarantined == True
        assert child.location == "隔离区"

        # 对复检失败的子批次再开处置单
        order2 = DispositionService.create_order(
            db_session, DispositionOrderCreate(inventory_batch_id=child.id)
        )
        assert order2.conclusion_source == "recheck"
        assert order2.sample_size == 100
        assert order2.defective_count == 30
        assert order2.inspection_result == "failed"
        assert order2.total_quantity == 100

        # 第二轮处置：让步60 + 报废40
        DispositionService.add_line(db_session, order2.id,
                                    DispositionLineCreate(action="concession", quantity=60))
        DispositionService.add_line(db_session, order2.id,
                                    DispositionLineCreate(action="scrap", quantity=40))
        for l in crud_disposition_order.get_lines(db_session, order2.id):
            DispositionService.approve_line(db_session, order2.id, l.id, "审批人")

        db_session.refresh(order2)
        assert order2.status == "completed"

        # 两级追溯都守恒：100 = 复检100 = 让步60 + 报废40
        trace1 = DispositionService.get_trace(db_session, order.id)
        trace2 = DispositionService.get_trace(db_session, order2.id)
        assert trace1.balanced and trace2.balanced
        follow_item = trace1.items[0]
        assert follow_item.follow_up_order_no == order2.order_no

    def test_recheck_result_requires_approved_recheck_line(self, db_session):
        factory = DataFactory(db_session)
        factory.setup_basic_supply_chain()
        _, batch = make_quarantined_batch(db_session, factory, 100)
        order = create_order_with_lines(db_session, batch, [
            {"action": "concession", "quantity": 100},
        ])
        line = order.lines[0]
        with pytest.raises(ValueError, match="不是复检"):
            DispositionService.submit_recheck_result(
                db_session, order.id, line.id,
                RecheckResultSubmit(
                    sample_size=10, defective_count=0, pass_rate=1.0,
                    result="passed", inspection_date=date.today()
                )
            )


class TestAtomicityAndLegacyGuards:
    """原子性：任何一步失败不得留下已释放库存却缺少处置依据的状态"""

    def test_failed_approval_rolls_back_everything(self, db_session, monkeypatch):
        factory = DataFactory(db_session)
        factory.setup_basic_supply_chain()
        _, batch = make_quarantined_batch(db_session, factory, 100)
        order = create_order_with_lines(db_session, batch, [
            {"action": "concession", "quantity": 60},
        ])
        line_id = order.lines[0].id

        def boom():
            raise RuntimeError("模拟数据库写入失败")

        monkeypatch.setattr(db_session, "commit", boom)
        with pytest.raises(RuntimeError):
            DispositionService.approve_line(db_session, order.id, line_id, "审批人")
        monkeypatch.undo()
        db_session.rollback()

        # 没有留下任何子批次
        children = db_session.query(InventoryBatch).filter(
            InventoryBatch.parent_batch_id == batch.id
        ).all()
        assert children == [], "失败的批准不得留下已生成的子批次"
        # 决定仍是待审批
        line = db_session.query(DispositionLine).filter(
            DispositionLine.id == line_id
        ).one()
        assert line.status == "pending"
        assert line.result_batch_id is None
        # 原批次仍整批隔离，可用库存没有凭空增加
        db_session.refresh(batch)
        assert batch.is_quarantined == True
        assert batch.available_quantity == 0
        assert InspectionService.consume_material(db_session, batch.material_id, 1) == False

    def test_legacy_release_blocked_when_order_exists(self, db_session):
        factory = DataFactory(db_session)
        factory.setup_basic_supply_chain()
        _, batch = make_quarantined_batch(db_session, factory, 100)
        DispositionService.create_order(
            db_session, DispositionOrderCreate(inventory_batch_id=batch.id)
        )
        with pytest.raises(ValueError, match="检验处置单"):
            InspectionService.release_quarantined_batch(db_session, batch.id, "想整批解除")
        with pytest.raises(ValueError, match="检验处置单"):
            InspectionService.reject_quarantined_batch(db_session, batch.id, "想整批拒收")
        # 批次状态未被破坏
        db_session.refresh(batch)
        assert batch.is_quarantined == True

    def test_consume_rejects_non_positive_quantity(self, db_session):
        factory = DataFactory(db_session)
        factory.setup_basic_supply_chain()
        factory.create_inventory_batch("TM002", quantity=100, is_quarantined=False)
        material_id = factory.materials["TM002"].id
        assert InspectionService.consume_material(db_session, material_id, 0) == False
        assert InspectionService.consume_material(db_session, material_id, -50) == False
        # 库存未被负数量污染
        from app.crud.purchase import crud_inventory_batch
        assert crud_inventory_batch.get_total_stock(db_session, material_id) == 100


class TestConcurrency:
    """并发：多处置同时审批 + 生产领料并发，数量始终守恒"""

    def _new_session(self, db_engine):
        TestingSessionLocal = sessionmaker(
            autocommit=False, autoflush=False, bind=db_engine
        )
        return TestingSessionLocal()

    def test_concurrent_approvals_same_line_only_succeeds_once(self, db_engine):
        setup_db = self._new_session(db_engine)
        factory = DataFactory(setup_db)
        factory.setup_basic_supply_chain()
        _, batch = make_quarantined_batch(setup_db, factory, 90)
        order = create_order_with_lines(setup_db, batch, [
            {"action": "concession", "quantity": 30},
            {"action": "rework", "quantity": 30},
            {"action": "scrap", "quantity": 30},
        ])
        order_id = order.id
        line_ids = [l.id for l in order.lines]
        batch_id = batch.id
        setup_db.close()

        results = []
        def worker(line_id):
            s = self._new_session(db_engine)
            try:
                DispositionService.approve_line(s, order_id, line_id, "并发审批人")
                results.append(("ok", line_id))
            except ValueError as e:
                results.append(("fail", str(e)))
            finally:
                s.close()

        # 每条决定3个线程同时批准
        threads = [
            threading.Thread(target=worker, args=(lid,))
            for lid in line_ids for _ in range(3)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        ok = [r for r in results if r[0] == "ok"]
        assert len(ok) == 3, f"每条决定只应被批准一次，实际成功{len(ok)}次"

        check = self._new_session(db_engine)
        try:
            children = check.query(InventoryBatch).filter(
                InventoryBatch.parent_batch_id == batch_id
            ).all()
            assert len(children) == 3, "每条决定只能生成一个子批次"
            assert sum(c.quantity for c in children) == 90
            trace = DispositionService.get_trace(check, order_id)
            assert trace.balanced == True
        finally:
            check.close()

    def test_concurrent_add_lines_never_exceed_total(self, db_engine):
        setup_db = self._new_session(db_engine)
        factory = DataFactory(setup_db)
        factory.setup_basic_supply_chain()
        _, batch = make_quarantined_batch(setup_db, factory, 100)
        order = DispositionService.create_order(
            setup_db, DispositionOrderCreate(inventory_batch_id=batch.id)
        )
        order_id = order.id
        setup_db.close()

        outcomes = []
        def worker():
            s = self._new_session(db_engine)
            try:
                DispositionService.add_line(
                    s, order_id, DispositionLineCreate(action="concession", quantity=30)
                )
                outcomes.append("ok")
            except ValueError:
                outcomes.append("rejected")
            finally:
                s.close()

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        ok_count = outcomes.count("ok")
        assert ok_count == 3, f"100件最多容纳3条30件的决定，实际成功{ok_count}条"
        check = self._new_session(db_engine)
        try:
            detail = DispositionService.get_order_detail(check, order_id)
            assert detail.allocated_quantity == 90
            assert detail.remaining_quantity == 10
        finally:
            check.close()

    def test_concurrent_consume_never_oversells(self, db_engine):
        setup_db = self._new_session(db_engine)
        factory = DataFactory(setup_db)
        factory.setup_basic_supply_chain()
        _, batch = make_quarantined_batch(setup_db, factory, 100)
        order = create_order_with_lines(setup_db, batch, [
            {"action": "concession", "quantity": 100},
        ])
        DispositionService.approve_line(setup_db, order.id, order.lines[0].id, "审批人")
        material_id = batch.material_id
        setup_db.close()

        outcomes = []
        def worker():
            s = self._new_session(db_engine)
            try:
                ok = InspectionService.consume_material(s, material_id, 30)
                outcomes.append(ok)
            finally:
                s.close()

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        success_count = outcomes.count(True)
        assert success_count == 3, f"100件库存最多成功领3次30件，实际{success_count}次"

        check = self._new_session(db_engine)
        try:
            remaining = check.query(InventoryBatch).filter(
                InventoryBatch.material_id == material_id,
                InventoryBatch.is_quarantined == False
            ).all()
            total_left = sum(b.available_quantity for b in remaining)
            assert total_left == 100 - success_count * 30
            assert all(b.available_quantity >= 0 for b in remaining), "可用库存不得为负"
        finally:
            check.close()

    def test_concurrent_consume_and_release(self, db_engine):
        """领料与处置放行并发：释放前领不到，释放后总量守恒"""
        setup_db = self._new_session(db_engine)
        factory = DataFactory(setup_db)
        factory.setup_basic_supply_chain()
        _, batch = make_quarantined_batch(setup_db, factory, 60)
        order = create_order_with_lines(setup_db, batch, [
            {"action": "concession", "quantity": 60},
        ])
        order_id = order.id
        line_id = order.lines[0].id
        material_id = batch.material_id
        setup_db.close()

        outcomes = []
        def releaser():
            s = self._new_session(db_engine)
            try:
                DispositionService.approve_line(s, order_id, line_id, "审批人")
            finally:
                s.close()

        def consumer():
            s = self._new_session(db_engine)
            try:
                ok = InspectionService.consume_material(s, material_id, 60)
                outcomes.append(ok)
            finally:
                s.close()

        threads = [threading.Thread(target=releaser)]
        threads += [threading.Thread(target=consumer) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 无论放行与领料如何交错，成功领料次数至多为1，且库存不为负
        assert outcomes.count(True) <= 1
        check = self._new_session(db_engine)
        try:
            batches = check.query(InventoryBatch).filter(
                InventoryBatch.material_id == material_id,
                InventoryBatch.is_quarantined == False
            ).all()
            total_available = sum(b.available_quantity for b in batches)
            consumed = 60 if outcomes.count(True) == 1 else 0
            assert total_available == 60 - consumed
            assert all(b.available_quantity >= 0 for b in batches)
        finally:
            check.close()
