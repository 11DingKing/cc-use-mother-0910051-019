from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime
from uuid import uuid4

from app.models import DispositionOrder, DispositionLine, InventoryBatch
from app.crud.disposition import crud_disposition_order
from app.crud.purchase import crud_inventory_batch, crud_inspection
from app.schemas import (
    DispositionOrderCreate, DispositionLineCreate, RecheckResultSubmit,
    DispositionOrderDetail, DispositionTrace, DispositionTraceItem,
    DispositionOrder as DispositionOrderSchema
)
from app.services.locks import lock_pool

DISPOSITION_ACTIONS = ("recheck", "concession", "rework", "return", "scrap")
ACTION_LABELS = {
    "recheck": "复检",
    "concession": "让步接收",
    "rework": "返工",
    "return": "退货",
    "scrap": "报废",
}
ACTION_LOCATIONS = {
    "recheck": "复检区",
    "concession": "合格区",
    "rework": "返工区",
    "return": "退货区",
    "scrap": "报废区",
}

class DispositionService:
    """检验处置单服务。

    不变量：
    - 处置单 total_quantity 冻结为隔离批次原始数量，作为对账证据；
    - Σ(未驳回决定数量) <= total_quantity，完结时 Σ(已生效决定) == total_quantity；
    - 每个已批准决定生成唯一可追溯子批次（parent_batch_id + disposition_line_id），
      库存释放与处置依据在同一事务提交，任何一步失败整体回滚。
    """

    @staticmethod
    def create_order(db: Session, order_in: DispositionOrderCreate) -> DispositionOrder:
        with lock_pool.acquire(f"inventory_batch:{order_in.inventory_batch_id}"):
            try:
                batch = crud_inventory_batch.get(db, order_in.inventory_batch_id)
                if not batch:
                    raise ValueError(f"库存批次不存在: {order_in.inventory_batch_id}")
                if not batch.is_quarantined:
                    raise ValueError("仅隔离批次可以创建检验处置单")
                existing = crud_disposition_order.get_by_batch(db, batch.id)
                if existing:
                    raise ValueError(f"该批次已存在检验处置单: {existing.order_no}")
                snapshot = DispositionService._resolve_conclusion(
                    db, batch, order_in.inspection_id
                )
                order = DispositionOrder(
                    order_no=f"TMP-{uuid4().hex[:12]}",
                    inventory_batch_id=batch.id,
                    inspection_id=snapshot["inspection_id"],
                    delivery_id=batch.delivery_id,
                    material_id=batch.material_id,
                    total_quantity=batch.quantity,
                    sample_size=snapshot["sample_size"],
                    defective_count=snapshot["defective_count"],
                    pass_rate=snapshot["pass_rate"],
                    inspection_result=snapshot["result"],
                    conclusion_source=snapshot["source"],
                    status="open",
                    remark=order_in.remark,
                )
                db.add(order)
                db.flush()
                order.order_no = f"DISP-{datetime.now():%Y%m%d}-{order.id:04d}"
                for line_in in order_in.lines:
                    DispositionService._add_line_locked(db, order, line_in)
                db.commit()
                db.refresh(order)
                return order
            except Exception:
                db.rollback()
                raise

    @staticmethod
    def _resolve_conclusion(db: Session, batch: InventoryBatch,
                            inspection_id: Optional[int] = None) -> dict:
        """固定检验结论：复检失败子批次沿用其复检结论，否则固定首检记录。"""
        if batch.disposition_line_id:
            src_line = db.query(DispositionLine).filter(
                DispositionLine.id == batch.disposition_line_id
            ).first()
            if src_line and src_line.recheck_result:
                return {
                    "inspection_id": None,
                    "sample_size": src_line.recheck_sample_size,
                    "defective_count": src_line.recheck_defective_count,
                    "pass_rate": src_line.recheck_pass_rate,
                    "result": src_line.recheck_result,
                    "source": "recheck",
                }
        if inspection_id:
            inspection = crud_inspection.get(db, inspection_id)
            if not inspection:
                raise ValueError(f"检验记录不存在: {inspection_id}")
        else:
            inspection = crud_inspection.get_by_delivery(db, batch.delivery_id)
        if not inspection:
            raise ValueError("该批次缺少检验记录，无法固定检验结论")
        return {
            "inspection_id": inspection.id,
            "sample_size": inspection.sample_size,
            "defective_count": inspection.defective_count,
            "pass_rate": inspection.pass_rate,
            "result": inspection.result,
            "source": "inspection",
        }

    @staticmethod
    def _add_line_locked(db: Session, order: DispositionOrder,
                         line_in: DispositionLineCreate) -> DispositionLine:
        """在持有处置单锁的前提下添加决定；数量校验与写入处于同一临界区。"""
        if order.status != "open":
            raise ValueError("处置单已完结，不能再添加处置决定")
        if line_in.action not in DISPOSITION_ACTIONS:
            raise ValueError(
                f"不支持的处置方式: {line_in.action}，可选: {','.join(DISPOSITION_ACTIONS)}"
            )
        if line_in.quantity <= 0:
            raise ValueError("处置数量必须大于0")
        allocated = sum(l.quantity for l in order.lines if l.status != "rejected")
        remaining = order.total_quantity - allocated
        if line_in.quantity > remaining:
            raise ValueError(
                f"处置数量超出待分配数量: 剩余{remaining}，申请{line_in.quantity}"
            )
        line_no = max([l.line_no for l in order.lines], default=0) + 1
        line = DispositionLine(
            line_no=line_no,
            action=line_in.action,
            quantity=line_in.quantity,
            status="pending",
            reason=line_in.reason,
            remark=line_in.remark,
        )
        line.order = order
        db.add(line)
        db.flush()
        return line

    @staticmethod
    def add_line(db: Session, order_id: int, line_in: DispositionLineCreate) -> DispositionLine:
        with lock_pool.acquire(f"disposition_order:{order_id}"):
            try:
                order = crud_disposition_order.get(db, order_id)
                if not order:
                    raise ValueError(f"检验处置单不存在: {order_id}")
                line = DispositionService._add_line_locked(db, order, line_in)
                db.commit()
                db.refresh(line)
                return line
            except Exception:
                db.rollback()
                raise

    @staticmethod
    def remove_line(db: Session, order_id: int, line_id: int) -> None:
        with lock_pool.acquire(f"disposition_order:{order_id}"):
            try:
                order = crud_disposition_order.get(db, order_id)
                if not order:
                    raise ValueError(f"检验处置单不存在: {order_id}")
                line = next((l for l in order.lines if l.id == line_id), None)
                if not line:
                    raise ValueError(f"处置决定不存在: {line_id}")
                if line.status != "pending":
                    raise ValueError(f"仅待审批的决定可以删除，当前状态: {line.status}")
                db.delete(line)
                db.commit()
            except Exception:
                db.rollback()
                raise

    @staticmethod
    def approve_line(db: Session, order_id: int, line_id: int,
                     approver: str, remark: Optional[str] = None) -> DispositionLine:
        """批准决定：同一事务内生成可追溯子批次并更新决定状态。"""
        with lock_pool.acquire(f"disposition_order:{order_id}"):
            try:
                order = crud_disposition_order.get(db, order_id)
                if not order:
                    raise ValueError(f"检验处置单不存在: {order_id}")
                line = next((l for l in order.lines if l.id == line_id), None)
                if not line:
                    raise ValueError(f"处置决定不存在: {line_id}")
                if line.status != "pending":
                    raise ValueError(f"决定当前状态不允许批准: {line.status}")
                child = DispositionService._create_child_batch(db, order, line)
                line.result_batch_id = child.id
                line.status = "approved"
                line.approver = approver
                line.approved_at = datetime.now()
                if remark:
                    line.remark = remark
                DispositionService._refresh_order_status(db, order)
                db.commit()
                db.refresh(line)
                return line
            except Exception:
                db.rollback()
                raise

    @staticmethod
    def _create_child_batch(db: Session, order: DispositionOrder,
                            line: DispositionLine) -> InventoryBatch:
        """按决定生成子批次/数量分录；只有让步接收立即可领料。"""
        action = line.action
        releasable = action == "concession"
        child = InventoryBatch(
            delivery_id=order.delivery_id or 0,
            material_id=order.material_id,
            quantity=line.quantity,
            available_quantity=line.quantity if releasable else 0,
            is_quarantined=not releasable,
            quarantine_reason=None if releasable else (
                f"{order.order_no} {ACTION_LABELS[action]}: {line.reason or '按处置决定执行'}"
            ),
            location=ACTION_LOCATIONS[action],
            batch_no=f"{order.order_no}-L{line.line_no}",
            parent_batch_id=order.inventory_batch_id,
            disposition_line_id=line.id,
        )
        db.add(child)
        db.flush()
        return child

    @staticmethod
    def reject_line(db: Session, order_id: int, line_id: int,
                    approver: str, remark: Optional[str] = None) -> DispositionLine:
        """驳回决定：数量回到待分配池，不产生任何库存变动。"""
        with lock_pool.acquire(f"disposition_order:{order_id}"):
            try:
                order = crud_disposition_order.get(db, order_id)
                if not order:
                    raise ValueError(f"检验处置单不存在: {order_id}")
                line = next((l for l in order.lines if l.id == line_id), None)
                if not line:
                    raise ValueError(f"处置决定不存在: {line_id}")
                if line.status != "pending":
                    raise ValueError(f"决定当前状态不允许驳回: {line.status}")
                line.status = "rejected"
                line.approver = approver
                line.approved_at = datetime.now()
                if remark:
                    line.remark = remark
                db.commit()
                db.refresh(line)
                return line
            except Exception:
                db.rollback()
                raise

    @staticmethod
    def submit_recheck_result(db: Session, order_id: int, line_id: int,
                              result_in: RecheckResultSubmit) -> DispositionLine:
        """录入复检结论：合格则推翻原判释放库存，不合格则维持隔离并允许再开处置单。"""
        with lock_pool.acquire(f"disposition_order:{order_id}"):
            try:
                order = crud_disposition_order.get(db, order_id)
                if not order:
                    raise ValueError(f"检验处置单不存在: {order_id}")
                line = next((l for l in order.lines if l.id == line_id), None)
                if not line:
                    raise ValueError(f"处置决定不存在: {line_id}")
                if line.action != "recheck":
                    raise ValueError("该决定不是复检，不能录入复检结论")
                if line.status != "approved":
                    raise ValueError(f"复检决定当前状态不允许录入结论: {line.status}")
                if result_in.result not in ("passed", "failed"):
                    raise ValueError("复检结论只能是 passed 或 failed")
                child = db.query(InventoryBatch).filter(
                    InventoryBatch.id == line.result_batch_id
                ).first()
                if not child:
                    raise ValueError("复检子批次缺失，数据异常")
                line.recheck_sample_size = result_in.sample_size
                line.recheck_defective_count = result_in.defective_count
                line.recheck_pass_rate = result_in.pass_rate
                line.recheck_result = result_in.result
                line.recheck_inspector = result_in.inspector
                line.recheck_date = result_in.inspection_date
                line.status = "executed"
                line.executed_at = datetime.now()
                if result_in.remark:
                    line.remark = result_in.remark
                if result_in.result == "passed":
                    child.is_quarantined = False
                    child.available_quantity = child.quantity
                    child.location = "合格区"
                    child.quarantine_reason = (
                        f"复检合格(推翻原判)，依据{order.order_no}-L{line.line_no}"
                    )
                else:
                    child.is_quarantined = True
                    child.available_quantity = 0
                    child.location = "隔离区"
                    child.quarantine_reason = (
                        f"复检仍不合格，依据{order.order_no}-L{line.line_no}，可再开处置单"
                    )
                DispositionService._refresh_order_status(db, order)
                db.commit()
                db.refresh(line)
                return line
            except Exception:
                db.rollback()
                raise

    @staticmethod
    def _refresh_order_status(db: Session, order: DispositionOrder) -> None:
        finalized = sum(
            l.quantity for l in order.lines
            if (l.status == "approved" and l.action != "recheck") or l.status == "executed"
        )
        order.status = "completed" if finalized >= order.total_quantity else "open"

    @staticmethod
    def get_order_detail(db: Session, order_id: int) -> DispositionOrderDetail:
        order = crud_disposition_order.get(db, order_id)
        if not order:
            raise ValueError(f"检验处置单不存在: {order_id}")
        allocated, finalized = DispositionService._sum_quantities(order)
        detail = DispositionOrderDetail.model_validate(order)
        detail.allocated_quantity = allocated
        detail.finalized_quantity = finalized
        detail.remaining_quantity = order.total_quantity - allocated
        return detail

    @staticmethod
    def _sum_quantities(order: DispositionOrder) -> tuple:
        allocated = sum(l.quantity for l in order.lines if l.status != "rejected")
        finalized = sum(
            l.quantity for l in order.lines
            if (l.status == "approved" and l.action != "recheck") or l.status == "executed"
        )
        return allocated, finalized

    @staticmethod
    def get_trace(db: Session, order_id: int) -> DispositionTrace:
        """追溯视图：原始数量 = 所有去向之和，且任何已释放库存都有处置依据。"""
        order = crud_disposition_order.get(db, order_id)
        if not order:
            raise ValueError(f"检验处置单不存在: {order_id}")
        children = db.query(InventoryBatch).filter(
            InventoryBatch.parent_batch_id == order.inventory_batch_id
        ).all()
        child_by_line = {c.disposition_line_id: c for c in children}
        items: List[DispositionTraceItem] = []
        for line in order.lines:
            child = child_by_line.get(line.id)
            follow_up = None
            if child:
                follow_order = crud_disposition_order.get_by_batch(db, child.id)
                if follow_order:
                    follow_up = follow_order.order_no
            items.append(DispositionTraceItem(
                line_id=line.id,
                line_no=line.line_no,
                action=line.action,
                quantity=line.quantity,
                status=line.status,
                child_batch_id=child.id if child else None,
                child_batch_no=child.batch_no if child else None,
                child_available_quantity=child.available_quantity if child else None,
                child_location=child.location if child else None,
                child_is_quarantined=child.is_quarantined if child else None,
                follow_up_order_no=follow_up,
            ))
        allocated, finalized = DispositionService._sum_quantities(order)
        child_sum = sum(c.quantity for c in children)
        releasable = sum(
            c.available_quantity for c in children if not c.is_quarantined
        )
        orphan_release = any(
            not c.is_quarantined and c.disposition_line_id is None for c in children
        )
        balanced = (
            child_sum == finalized
            and finalized + (order.total_quantity - allocated) == order.total_quantity
            and not orphan_release
        )
        return DispositionTrace(
            order_no=order.order_no,
            inventory_batch_id=order.inventory_batch_id,
            total_quantity=order.total_quantity,
            finalized_quantity=finalized,
            remaining_quantity=order.total_quantity - allocated,
            child_batch_quantity_sum=child_sum,
            releasable_quantity=releasable,
            balanced=balanced,
            items=items,
        )
