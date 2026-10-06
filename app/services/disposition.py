"""
检验处置单服务

核心不变量（任何时点、任何并发下成立）：
  原始批次数量 = 已批准各去向数量之和 + 处置单未决数量 + 尚未建立处置的隔离数量
  已释放（可领料）库存的每一单位都必须能追溯到一条 approved 且放行类的处置决定。

实现手段：
- 审批在单一事务内完成"加锁父批次 → 重算平衡 → 原子推进 allocated_quantity → 生成子批次/终态"，
  任一步失败整体回滚，不会留下已释放库存却缺少处置依据的中间状态。
- 数量推进用条件更新（CAS），并发审批/领料冲突时影响行数为0即回滚，杜绝超分超领。
"""
from datetime import datetime, date
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from app.models import (
    DispositionOrder, DispositionDecision, InspectionSample,
    InventoryBatch, Inspection,
)
from app.crud.purchase import crud_inventory_batch, crud_inspection
from app.crud.disposition import (
    crud_disposition_order, crud_disposition_decision,
    crud_inspection_sample, crud_inspection_round,
)
from app.schemas import (
    DispositionOrderCreate, DispositionApproveRequest,
    ReinspectionCreate, DispositionBalance,
)

# 处置类型 → 批准后子批次状态
RE_INSPECT = "re_inspect"
CONCESSION = "concession"
REWORK = "rework"
RETURN_GOODS = "return"
SCRAP = "scrap"

DECISION_TYPES = {RE_INSPECT, CONCESSION, REWORK, RETURN_GOODS, SCRAP}
# 批准即放行、可参与领料的去向
RELEASE_ON_APPROVE = {CONCESSION}
# 终态去向：数量不再参与任何库存
TERMINAL_TYPES = {RETURN_GOODS, SCRAP}

TYPE_LOCATIONS = {
    RE_INSPECT: "待复检区",
    CONCESSION: "让步接收区",
    REWORK: "返工区",
    RETURN_GOODS: "退货区",
    SCRAP: "报废区",
}
TYPE_LABELS = {
    RE_INSPECT: "复检",
    CONCESSION: "让步接收",
    REWORK: "返工",
    RETURN_GOODS: "退货",
    SCRAP: "报废",
}


class QuantityBalanceError(ValueError):
    """数量平衡被破坏时抛出，事务必须回滚"""


class DispositionService:

    # ------------------------------------------------------------------ #
    # 检验处置单建立
    # ------------------------------------------------------------------ #

    @staticmethod
    def create_disposition_order(
        db: Session, order_in: DispositionOrderCreate
    ) -> DispositionOrder:
        batch = crud_inventory_batch.get(db, order_in.inventory_batch_id)
        if not batch:
            raise ValueError(f"库存批次不存在: {order_in.inventory_batch_id}")
        if not batch.is_quarantined:
            raise ValueError("只有隔离批次才能建立检验处置单")

        try:
            # 行锁父批次：与审批/复检放行/返工走同一把锁，串行化数量预留
            batch = DispositionService._lock_batch(db, batch.id)
            if not batch.is_quarantined:
                raise ValueError("只有隔离批次才能建立检验处置单")

            # 同一隔离批次可并存多张处置单（复检推翻、驳回后补处置），
            # 但按"已批准 + 待审批"预留数量，保证各单数量区间互不重叠
            reserved = DispositionService._reserved_quantity_for_batch(db, batch.id)
            available = batch.quantity - reserved
            if available <= 0:
                raise QuantityBalanceError("该隔离批次已全部处置或已预留，无剩余数量可分配")

            inspection = crud_inspection_round.latest_by_delivery(db, batch.delivery_id)
            if not inspection:
                raise ValueError("该到货批次缺少检验记录，无法建立处置单")

            decisions = order_in.decisions or []
            if not decisions:
                raise ValueError("处置单至少包含一条处置决定")
            total = DispositionService._validate_decisions(decisions)
            if total > available:
                raise QuantityBalanceError(
                    f"处置数量合计{total}超过可处置数量{available}"
                    f"（原批{batch.quantity}，已批准/待决{reserved}）"
                )

            seen = set()
            for d in decisions:
                if d.decision_type in seen:
                    raise ValueError(f"同一处置单中处置类型不能重复: {d.decision_type}")
                seen.add(d.decision_type)

            order = DispositionOrder(
                order_no=DispositionService._gen_order_no(db),
                inventory_batch_id=batch.id,
                inspection_id=inspection.id,
                material_id=batch.material_id,
                source_quantity=total,
                allocated_quantity=0,
                status="draft",
                remark=order_in.remark,
                created_by=order_in.created_by,
                # 固化检验证据快照
                sample_size=inspection.sample_size,
                defective_count=inspection.defective_count,
                pass_rate=inspection.pass_rate,
                inspection_result=inspection.result,
                inspection_round=inspection.round_no,
                inspector=inspection.inspector,
                inspection_date=inspection.inspection_date,
            )
            db.add(order)
            db.flush()

            for d in decisions:
                db.add(DispositionDecision(
                    disposition_order_id=order.id,
                    decision_type=d.decision_type,
                    quantity=d.quantity,
                    reason=d.reason,
                    status="pending",
                    reinspect_result="pending_inspection" if d.decision_type == RE_INSPECT else None,
                ))

            # 固化取样明细（建单时检验轮次的全部取样记录复制为快照）
            samples = crud_inspection_sample.get_by_inspection(db, inspection.id)
            if not samples:
                # 兼容历史检验记录：无取样明细时以检验主记录补一条
                db.add(InspectionSample(
                    inspection_id=inspection.id,
                    round_no=inspection.round_no,
                    sample_size=inspection.sample_size,
                    defective_count=inspection.defective_count,
                    pass_rate=inspection.pass_rate,
                    result=inspection.result,
                    inspector=inspection.inspector,
                    remark=inspection.remark,
                ))
            db.commit()
        except Exception:
            db.rollback()
            raise

        return crud_disposition_order.get_with_decisions(db, order.id)

    @staticmethod
    def _validate_decisions(decisions) -> int:
        total = 0
        for d in decisions:
            if d.decision_type not in DECISION_TYPES:
                raise ValueError(f"未知处置类型: {d.decision_type}")
            if d.quantity <= 0:
                raise ValueError(f"处置数量必须为正: {d.decision_type}={d.quantity}")
            total += d.quantity
        return total

    # ------------------------------------------------------------------ #
    # 处置决定审批：批准即原子生成子批次/终态分录
    # ------------------------------------------------------------------ #

    @staticmethod
    def approve_decision(
        db: Session, order_id: int, decision_id: int, req: DispositionApproveRequest
    ) -> DispositionDecision:
        # 轻量预检（不锁）
        if not crud_disposition_order.get(db, order_id):
            raise ValueError(f"处置单不存在: {order_id}")
        try:
            # 父批行锁是同一批次所有审批/建单/放行的唯一串行点，必须最先获取
            order0 = crud_disposition_order.get(db, order_id)
            parent = DispositionService._lock_batch(db, order0.inventory_batch_id)

            # 锁后重新加载单据与决定（READ COMMITTED 下此处读到的是最新已提交数据）
            order = crud_disposition_order.get_with_decisions(db, order_id)
            if order.status not in ("draft", "approving"):
                raise ValueError(f"处置单当前状态{order.status}不允许审批")
            decision = next((d for d in order.decisions if d.id == decision_id), None)
            if not decision:
                raise ValueError(f"处置决定不存在: {decision_id}")
            if decision.status != "pending":
                raise ValueError(f"该决定已处于{decision.status}状态，不能重复审批")

            # 从决定分录重算已批准合计（不信任缓存列），与单据 allocated 对账
            approved_total = crud_disposition_decision.approved_quantity_by_order(db, order_id)
            if order.allocated_quantity != approved_total:
                raise QuantityBalanceError(
                    f"处置单分配列({order.allocated_quantity})与已批准分录合计"
                    f"({approved_total})不一致，疑似并发改动，审批回滚"
                )
            if approved_total + decision.quantity > order.source_quantity:
                raise QuantityBalanceError(
                    f"批准后合计{approved_total + decision.quantity}"
                    f"超过处置单数量{order.source_quantity}"
                )

            consumed_elsewhere = DispositionService._approved_quantity_for_batch(db, parent.id)
            if consumed_elsewhere + decision.quantity > parent.quantity:
                raise QuantityBalanceError(
                    f"批次{parent.id}已处置{consumed_elsewhere}，"
                    f"再分配{decision.quantity}将超过原始数量{parent.quantity}"
                )

            child = DispositionService._apply_decision(db, order, parent, decision, req)

            # 原子推进处置单已分配合计（CAS 兜底防并发双花）
            new_allocated = approved_total + decision.quantity
            updated = db.query(DispositionOrder).filter(
                DispositionOrder.id == order.id,
                DispositionOrder.allocated_quantity == approved_total,
            ).update({"allocated_quantity": new_allocated}, synchronize_session=False)
            if updated != 1:
                raise QuantityBalanceError("处置单分配数量并发冲突，审批回滚")

            decision.status = "approved"
            decision.approver = req.approver
            decision.approved_at = datetime.now()
            if req.reason:
                decision.reason = req.reason
            if child is not None:
                decision.child_batch_id = child.id

            order.status = "approving"
            # 所有决定均已终决（批准/驳回/推翻）即关单；数量恰好分满时账实全清
            pending = [d for d in order.decisions if d.status == "pending"]
            if not pending:
                order.status = "closed"
                if new_allocated == order.source_quantity and parent.available_quantity == 0 \
                        and parent.is_quarantined:
                    parent.location = "已拆批"

            db.commit()
        except Exception:
            db.rollback()
            raise

        db.refresh(decision)
        return decision

    @staticmethod
    def _apply_decision(
        db: Session, order: DispositionOrder,
        parent: InventoryBatch, decision: DispositionDecision,
        req: DispositionApproveRequest
    ) -> Optional[InventoryBatch]:
        """按去向生成可追溯子批次或终态记录。放行类子批次批准即有 available_quantity。"""
        dtype = decision.decision_type
        label = TYPE_LABELS[dtype]

        if dtype in TERMINAL_TYPES:
            # 退货/报废：不生成可库存子批次，写终态分录批次（quantity 保留在账，available=0）
            child = InventoryBatch(
                delivery_id=parent.delivery_id,
                material_id=parent.material_id,
                quantity=decision.quantity,
                available_quantity=0,
                is_quarantined=True,
                quarantine_reason=f"{label}终态: {req.reason or decision.reason or ''}",
                location=TYPE_LOCATIONS[dtype],
                origin_batch_id=parent.id,
                status="returned" if dtype == RETURN_GOODS else "scrapped",
            )
        else:
            # 复检/让步/返工：生成隔离子批次；让步接收批准即放行，其余待后续结论
            release_now = dtype in RELEASE_ON_APPROVE
            child = InventoryBatch(
                delivery_id=parent.delivery_id,
                material_id=parent.material_id,
                quantity=decision.quantity,
                available_quantity=decision.quantity if release_now else 0,
                is_quarantined=not release_now,
                quarantine_reason=None if release_now else f"处置子批-待{label}",
                location=TYPE_LOCATIONS[dtype],
                origin_batch_id=parent.id,
                status="normal" if release_now else (
                    "rework" if dtype == REWORK else "quarantined"
                ),
            )
        db.add(child)
        db.flush()
        child.origin_disposition_id = decision.id
        db.flush()
        return child

    @staticmethod
    def reject_decision(
        db: Session, order_id: int, decision_id: int, req: DispositionApproveRequest
    ) -> DispositionDecision:
        """驳回决定：数量回到可再分配池，不产生任何子批次、不释放库存"""
        order0 = crud_disposition_order.get_with_decisions(db, order_id)
        if not order0:
            raise ValueError(f"处置单不存在: {order_id}")
        try:
            # 与审批互斥：锁根批后重读决定状态
            DispositionService._lock_batch(db, order0.inventory_batch_id)
            order = crud_disposition_order.get_with_decisions(db, order_id)
            decision = next((d for d in order.decisions if d.id == decision_id), None)
            if not decision:
                raise ValueError(f"处置决定不存在: {decision_id}")
            if decision.status != "pending":
                raise ValueError(f"该决定已处于{decision.status}状态，不能驳回")
            decision.status = "rejected"
            decision.approver = req.approver
            decision.approved_at = datetime.now()
            if req.reason:
                decision.reason = req.reason
            # 无待决决定即关单（可能存在已批准的其他决定）
            if not any(d.status == "pending" for d in order.decisions):
                order.status = "closed"
            db.commit()
        except Exception:
            db.rollback()
            raise
        db.refresh(decision)
        return decision

    # ------------------------------------------------------------------ #
    # 复检：结论只追加；通过才放行子批次，不通过可对其再处置
    # ------------------------------------------------------------------ #

    @staticmethod
    def record_reinspection(
        db: Session, order_id: int, decision_id: int, result_in: ReinspectionCreate
    ) -> DispositionDecision:
        if result_in.result not in ("passed", "failed"):
            raise ValueError("复检结论必须为 passed 或 failed")
        if result_in.sample_size <= 0:
            raise ValueError("复检取样数量必须为正")
        if result_in.defective_count < 0 or result_in.defective_count > result_in.sample_size:
            raise ValueError("不良数必须在 0 与取样量之间")

        # 预检（只读）
        order0 = crud_disposition_order.get_with_decisions(db, order_id)
        if not order0:
            raise ValueError(f"处置单不存在: {order_id}")
        decision0 = next((d for d in order0.decisions if d.id == decision_id), None)
        if not decision0:
            raise ValueError(f"处置决定不存在: {decision_id}")
        if decision0.decision_type != RE_INSPECT:
            raise ValueError("只有复检决定可以登记复检结论")
        if decision0.status != "approved":
            raise ValueError("复检决定须先经批准，才能登记复检结论")
        if decision0.reinspect_result in ("passed", "failed"):
            raise ValueError("该复检决定已有结论，复检结论不可改写；如需推翻请新建处置单")
        if not decision0.child_batch_id:
            raise QuantityBalanceError("复检子批次缺失，拒绝登记")

        try:
            # 先锁复检子批次（本方法此前不得有写操作），锁后重读最新状态
            locked = DispositionService._lock_batch(db, decision0.child_batch_id)
            order = crud_disposition_order.get_with_decisions(db, order_id)
            decision = next((d for d in order.decisions if d.id == decision_id), None)
            if decision.reinspect_result in ("passed", "failed"):
                raise QuantityBalanceError("复检结论并发登记冲突，事务回滚")
            parent_inspection = crud_inspection.get(db, order.inspection_id)
            delivery_id = parent_inspection.delivery_id

            # 新一轮检验记录：只追加，旧轮次 is_latest 置 False
            rounds = crud_inspection_round.list_by_delivery(db, delivery_id)
            next_round = max((r.round_no for r in rounds), default=1) + 1
            for r in rounds:
                r.is_latest = False
            new_inspection = Inspection(
                delivery_id=delivery_id,
                sample_size=result_in.sample_size,
                defective_count=result_in.defective_count,
                pass_rate=result_in.pass_rate,
                result=result_in.result,
                inspector=result_in.inspector,
                inspection_date=result_in.inspection_date or date.today(),
                remark=result_in.remark,
                round_no=next_round,
                is_latest=True,
            )
            db.add(new_inspection)
            db.flush()
            db.add(InspectionSample(
                inspection_id=new_inspection.id,
                round_no=next_round,
                sample_size=result_in.sample_size,
                defective_count=result_in.defective_count,
                pass_rate=result_in.pass_rate,
                result=result_in.result,
                inspector=result_in.inspector,
                remark=result_in.remark,
            ))

            if result_in.result == "passed":
                if locked.available_quantity != 0 or not locked.is_quarantined:
                    raise QuantityBalanceError("复检子批次状态异常，已中止放行")
                reason = f"第{next_round}轮复检合格放行"
                # CAS 原子放行：仅当仍为全量隔离(available=0)时 0 → quantity
                updated = db.query(InventoryBatch).filter(
                    InventoryBatch.id == locked.id,
                    InventoryBatch.available_quantity == 0,
                    InventoryBatch.is_quarantined == True,  # noqa: E712
                ).update(
                    {"available_quantity": locked.quantity,
                     "is_quarantined": False,
                     "quarantine_reason": reason,
                     "location": "合格区", "status": "normal"},
                    synchronize_session=False,
                )
                if updated != 1:
                    raise QuantityBalanceError("复检放行并发冲突，事务回滚")
                decision.reinspect_result = "passed"
            else:
                locked.quarantine_reason = f"第{next_round}轮复检仍不合格，待再处置"
                locked.location = "待处理区"
                locked.status = "quarantined"
                decision.reinspect_result = "failed"

            db.commit()
        except Exception:
            db.rollback()
            raise

        db.refresh(decision)
        return decision

    # ------------------------------------------------------------------ #
    # 推翻：驳回一条已批准的放行决定（让步接收/复检通过），数量与证据同时回滚
    # ------------------------------------------------------------------ #

    @staticmethod
    def reverse_decision(
        db: Session, order_id: int, decision_id: int, approver: str, reverse_reason: str
    ) -> DispositionDecision:
        # 预检（只读）
        order0 = crud_disposition_order.get_with_decisions(db, order_id)
        if not order0:
            raise ValueError(f"处置单不存在: {order_id}")
        decision0 = next((d for d in order0.decisions if d.id == decision_id), None)
        if not decision0:
            raise ValueError(f"处置决定不存在: {decision_id}")
        if decision0.status != "approved":
            raise ValueError("只能推翻已批准的决定")
        if decision0.decision_type == REWORK and decision0.rework_completed_qty is not None:
            raise QuantityBalanceError(
                "返工已完成登记并产生合格/未完成子批，不能推翻；请对未完成子批再处置")

        try:
            # 按固定层级一次性加锁：根批（与审批互斥）+ 子批（与领料互斥）
            lock_ids = [order0.inventory_batch_id]
            if decision0.child_batch_id:
                lock_ids.append(decision0.child_batch_id)
            locked_rows = DispositionService._acquire_locks(db, lock_ids)
            locked_map = {b.id: b for b in locked_rows}
            child = locked_map.get(decision0.child_batch_id) if decision0.child_batch_id else None

            # 锁后重读最新状态
            order = crud_disposition_order.get_with_decisions(db, order_id)
            decision = next((d for d in order.decisions if d.id == decision_id), None)
            if decision.status != "approved":
                raise QuantityBalanceError(f"决定并发变更为{decision.status}，推翻回滚")

            if child is not None and not child.is_quarantined:
                # 仅放行类叶子会被领料；终态/返工/待复检子批次 available 恒为0，不算消耗
                consumed = child.quantity - child.available_quantity
                if consumed > 0:
                    raise QuantityBalanceError(
                        f"该子批次已有{consumed}件被领料消耗，不能推翻；请走新处置单流程"
                    )

            # 原子回收处置单分配额度（CAS）
            new_allocated = order.allocated_quantity - decision.quantity
            updated = db.query(DispositionOrder).filter(
                DispositionOrder.id == order.id,
                DispositionOrder.allocated_quantity == order.allocated_quantity,
            ).update({"allocated_quantity": new_allocated}, synchronize_session=False)
            if updated != 1:
                raise QuantityBalanceError("推翻时处置单分配数量并发冲突，事务回滚")

            if child is not None:
                # 子批次整体撤销（放行/终态/返工/待复检均无下游分录挂在其上）
                db.delete(child)
                decision.child_batch_id = None

            decision.status = "reversed"
            decision.reversed_at = datetime.now()
            decision.reverse_reason = f"[{approver}] {reverse_reason}"
            if decision.decision_type == RE_INSPECT:
                decision.reinspect_result = None
            if order.status == "closed":
                order.status = "approving"
            db.commit()
        except Exception:
            db.rollback()
            raise
        db.refresh(decision)
        return decision

    # ------------------------------------------------------------------ #
    # 返工完成：返工子批拆成"放行孙批 + 未完成隔离孙批"，保持批次树完整拆分
    # ------------------------------------------------------------------ #

    @staticmethod
    def complete_rework(
        db: Session, order_id: int, decision_id: int, req
    ):
        # 预检（只读，本方法此前不得写库）
        order0 = crud_disposition_order.get_with_decisions(db, order_id)
        if not order0:
            raise ValueError(f"处置单不存在: {order_id}")
        decision0 = next((d for d in order0.decisions if d.id == decision_id), None)
        if not decision0:
            raise ValueError(f"处置决定不存在: {decision_id}")
        if decision0.decision_type != REWORK:
            raise ValueError("只有返工决定可以登记返工完成")
        if decision0.status != "approved":
            raise ValueError("返工决定须先经批准")
        if decision0.rework_completed_qty is not None:
            raise ValueError("返工完成只能一次性登记，不可重复登记")
        if not decision0.child_batch_id:
            raise QuantityBalanceError("返工子批次缺失，拒绝登记")

        try:
            # 锁返工容器批，锁后重读最新状态
            container = DispositionService._lock_batch(db, decision0.child_batch_id)
            order = crud_disposition_order.get_with_decisions(db, order_id)
            decision = next((d for d in order.decisions if d.id == decision_id), None)
            if decision.rework_completed_qty is not None:
                raise QuantityBalanceError("返工完成并发登记冲突，事务回滚")
            if container.status != "rework":
                raise QuantityBalanceError("返工子批次状态异常，拒绝登记")
            if crud_inventory_batch.get_children(db, container.id):
                raise QuantityBalanceError("返工子批次已被拆分，不能重复登记")
            q = req.completed_quantity
            if q < 0 or q > container.quantity:
                raise ValueError(f"返工合格数量须在 0 与 {container.quantity} 之间")

            if q > 0:
                good = InventoryBatch(
                    delivery_id=container.delivery_id,
                    material_id=container.material_id,
                    quantity=q,
                    available_quantity=q,
                    is_quarantined=False,
                    quarantine_reason=f"返工合格放行: {req.remark or ''}",
                    location="合格区",
                    origin_batch_id=container.id,
                    origin_disposition_id=decision.id,
                    status="normal",
                )
                db.add(good)
            bad_qty = container.quantity - q
            if bad_qty > 0:
                bad = InventoryBatch(
                    delivery_id=container.delivery_id,
                    material_id=container.material_id,
                    quantity=bad_qty,
                    available_quantity=0,
                    is_quarantined=True,
                    quarantine_reason=f"返工未完成，待再处置: {req.remark or ''}",
                    location="待处理区",
                    origin_batch_id=container.id,
                    origin_disposition_id=decision.id,
                    status="quarantined",
                )
                db.add(bad)
            container.location = "返工已拆分"
            decision.rework_completed_qty = q
            decision.rework_completed_at = datetime.now()
            decision.rework_inspector = req.inspector
            db.commit()
        except Exception:
            db.rollback()
            raise
        db.refresh(decision)
        return decision

    # ------------------------------------------------------------------ #
    # 查询与平衡校验
    # ------------------------------------------------------------------ #

    @staticmethod
    def get_order(db: Session, order_id: int) -> DispositionOrder:
        order = crud_disposition_order.get_with_decisions(db, order_id)
        if not order:
            raise ValueError(f"处置单不存在: {order_id}")
        return order

    @staticmethod
    def list_orders(db: Session, batch_id: Optional[int] = None,
                    status: Optional[str] = None) -> List[DispositionOrder]:
        query = db.query(DispositionOrder)
        if batch_id:
            query = query.filter(DispositionOrder.inventory_batch_id == batch_id)
        if status:
            query = query.filter(DispositionOrder.status == status)
        return query.order_by(DispositionOrder.id).all()

    @staticmethod
    def get_balance(db: Session, inventory_batch_id: int) -> DispositionBalance:
        batch = crud_inventory_batch.get(db, inventory_batch_id)
        if not batch:
            raise ValueError(f"库存批次不存在: {inventory_batch_id}")
        orders = crud_disposition_order.get_by_batch(db, batch.id)

        by_type: Dict[str, int] = {t: 0 for t in DECISION_TYPES}
        allocated = 0
        for o in orders:
            if o.status == "cancelled":
                continue
            for d in o.decisions:
                if d.status == "approved":
                    by_type[d.decision_type] += d.quantity
                    allocated += d.quantity

        pending = sum(
            d.quantity for o in orders if o.status in ("draft", "approving")
            for d in o.decisions if d.status == "pending"
        )
        accounted = allocated + pending
        is_balanced = (
            allocated <= batch.quantity
            and accounted <= batch.quantity
            and all(o.allocated_quantity ==
                    sum(d.quantity for d in o.decisions if d.status == "approved")
                    for o in orders)
        )
        return DispositionBalance(
            inventory_batch_id=batch.id,
            original_quantity=batch.quantity,
            source_quantity=sum(o.source_quantity for o in orders if o.status != "cancelled"),
            allocated_quantity=allocated,
            pending_quantity=pending,
            approved_by_type=by_type,
            available_for_new_disposition=batch.quantity - allocated - pending,
            is_balanced=is_balanced,
            released_available_quantity=DispositionService._released_qty(db, batch),
        )

    @staticmethod
    def assert_integrity(db: Session, inventory_batch_id: int) -> None:
        """
        完整不变量校验（沿批次树递归，支持复检失败再处置、返工孙批次等多级拆分）：
        1) 任一已拆分节点：子批次数量之和 = 节点原始数量，容器自身无可用量；
        2) 全部叶子批次数量之和 = 根（原批）数量；
        3) 每个非隔离叶子的每一单位可用量都必须能追溯到一条放行类批准决定。
        """
        root = crud_inventory_batch.get(db, inventory_batch_id)
        if not root:
            raise ValueError(f"库存批次不存在: {inventory_batch_id}")

        leaf_total = {"qty": 0}

        def walk(node: InventoryBatch):
            children = crud_inventory_batch.get_children(db, node.id)
            if children:
                child_sum = sum(c.quantity for c in children)
                if child_sum > node.quantity:
                    raise QuantityBalanceError(
                        f"批次{node.id}去向不平: 子批合计{child_sum} 超过原数量{node.quantity}"
                    )
                if node.available_quantity != 0:
                    raise QuantityBalanceError(
                        f"已拆分批次{node.id}仍残留可用量{node.available_quantity}，去向不明"
                    )
                for c in children:
                    walk(c)
                # 尚未拆出的余量仍物理留在该节点（隔离/在制），作为叶子桶计入
                residual = node.quantity - child_sum
                if residual > 0:
                    if node.status == "rework":
                        # 返工容器登记完成时必须整批拆成 合格+未完成
                        raise QuantityBalanceError(
                            f"返工批次{node.id}有{residual}件去向不明"
                        )
                    if not node.is_quarantined:
                        raise QuantityBalanceError(
                            f"批次{node.id}有{residual}件余量未隔离却无去向"
                        )
                    leaf_total["qty"] += residual
            else:
                leaf_total["qty"] += node.quantity
                DispositionService._assert_leaf_basis(db, node)

        walk(root)
        if leaf_total["qty"] != root.quantity:
            raise QuantityBalanceError(
                f"批次树叶子合计{leaf_total['qty']} != 原批数量{root.quantity}"
            )

    @staticmethod
    def _assert_leaf_basis(db: Session, node: InventoryBatch) -> None:
        """叶子批次：有可用量则必须有放行类批准决定作为依据，且数量不得超过决定数量"""
        if node.available_quantity <= 0:
            return
        if node.is_quarantined or node.status != "normal":
            raise QuantityBalanceError(
                f"批次{node.id}状态异常却有可用量{node.available_quantity}"
            )
        if node.origin_disposition_id is None:
            # 首轮合格入库批次（无处置来源）属于正常
            if node.origin_batch_id is None:
                return
            raise QuantityBalanceError(f"批次{node.id}已释放但缺少处置决定依据")

        from app.models import DispositionDecision as DD
        decision = db.query(DD).filter(DD.id == node.origin_disposition_id).first()
        if not decision or decision.status != "approved":
            raise QuantityBalanceError(
                f"批次{node.id}的处置决定{node.origin_disposition_id}未批准，库存不得释放"
            )
        valid = (
            decision.decision_type == CONCESSION
            or (decision.decision_type == RE_INSPECT and decision.reinspect_result == "passed")
            or (decision.decision_type == REWORK and decision.rework_completed_qty is not None)
        )
        if not valid:
            raise QuantityBalanceError(
                f"批次{node.id}的处置类型{decision.decision_type}不构成放行依据"
            )
        if node.available_quantity > decision.quantity:
            raise QuantityBalanceError(
                f"批次{node.id}放行{node.available_quantity}超过决定数量{decision.quantity}"
            )

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #

    @staticmethod
    def _approved_quantity_for_batch(db: Session, batch_id: int) -> int:
        """该批次所有未取消处置单中 approved 决定数量合计"""
        orders = crud_disposition_order.get_by_batch(db, batch_id)
        total = 0
        for o in orders:
            if o.status == "cancelled":
                continue
            total += sum(
                d.quantity for d in o.decisions if d.status == "approved"
            )
        return total

    @staticmethod
    def _reserved_quantity_for_batch(db: Session, batch_id: int) -> int:
        """已占用 = 所有单据已批准数量 + 未关闭单据的待审批数量（驳回/推翻/关单后释放可再分配）"""
        orders = crud_disposition_order.get_by_batch(db, batch_id)
        total = 0
        for o in orders:
            if o.status == "cancelled":
                continue
            total += sum(
                d.quantity for d in o.decisions if d.status == "approved"
            )
            if o.status in ("draft", "approving"):
                total += sum(
                    d.quantity for d in o.decisions if d.status == "pending"
                )
        return total

    @staticmethod
    def _released_qty(db: Session, batch: InventoryBatch) -> int:
        orders = crud_disposition_order.get_by_batch(db, batch.id)
        total = 0
        for o in orders:
            for d in o.decisions:
                if d.status == "approved" and d.child_batch_id:
                    child = crud_inventory_batch.get(db, d.child_batch_id)
                    if child and not child.is_quarantined:
                        total += child.available_quantity
        return total

    @staticmethod
    def _lock_batch(db: Session, batch_id: int) -> InventoryBatch:
        rows = DispositionService._acquire_locks(db, [batch_id])
        return rows[0]

    @staticmethod
    def _acquire_locks(db: Session, batch_ids: List[int]) -> List[InventoryBatch]:
        """
        一次性按 id 顺序锁定多批次（固定加锁顺序防死锁）。契约：调用时尚未执行写操作。
        - 先结束当前只读事务，丢弃旧快照；
        - PostgreSQL 等：SELECT ... FOR UPDATE 行锁；
        - SQLite：等幂空写抢占库级写锁，第二个事务阻塞到前者提交，随后重读最新数据。
        """
        ids = sorted({i for i in batch_ids if i is not None})
        if not ids:
            return []
        if db.in_transaction():
            db.commit()
        is_sqlite = db.bind.dialect.name == "sqlite"
        base = db.query(InventoryBatch).filter(InventoryBatch.id.in_(ids))
        if not is_sqlite:
            base.with_for_update().all()
        else:
            base.update(
                {InventoryBatch.location.name: InventoryBatch.location},
                synchronize_session=False,
            )
        db.expire_all()
        rows = db.query(InventoryBatch).filter(
            InventoryBatch.id.in_(ids)
        ).order_by(InventoryBatch.id).all()
        if len(rows) != len(ids):
            raise ValueError(f"库存批次不存在: {set(ids) - {r.id for r in rows}}")
        return rows

    @staticmethod
    def _gen_order_no(db: Session) -> str:
        ts = datetime.now().strftime("%Y%m%d%H%M%S")
        base = f"DO-{ts}"
        no = base
        seq = 1
        while crud_disposition_order.get_by_order_no(db, no):
            seq += 1
            no = f"{base}-{seq}"
        return no
