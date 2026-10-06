from sqlalchemy import update
from sqlalchemy.orm import Session
from typing import Optional
from app.crud.purchase import crud_inspection, crud_inventory_batch, crud_delivery
from app.crud.disposition import crud_disposition_order
from app.schemas import InspectionCreate, InventoryBatchCreate
from app.models import Inspection, InventoryBatch
from app.services.locks import lock_pool

class InspectionService:
    @staticmethod
    def record_inspection_and_process(
        db: Session,
        inspection_in: InspectionCreate
    ) -> Inspection:
        delivery = crud_delivery.get(db, inspection_in.delivery_id)
        if not delivery:
            raise ValueError(f"到货记录不存在: {inspection_in.delivery_id}")
        existing_inspection = crud_inspection.get_by_delivery(db, inspection_in.delivery_id)
        if existing_inspection:
            raise ValueError(f"该到货记录已存在抽检结果")
        inspection = crud_inspection.create(db, obj_in=inspection_in)
        is_quarantined = inspection.result != "passed"
        quarantine_reason = None
        if is_quarantined:
            if inspection.defective_count > 0:
                quarantine_reason = f"抽检不合格，不良品{inspection.defective_count}件，合格率{inspection.pass_rate:.2%}"
            else:
                quarantine_reason = "抽检不合格"
        available_qty = 0 if is_quarantined else delivery.quantity
        batch_in = InventoryBatchCreate(
            delivery_id=delivery.id,
            material_id=delivery.material_id,
            quantity=delivery.quantity,
            available_quantity=available_qty,
            is_quarantined=is_quarantined,
            quarantine_reason=quarantine_reason,
            location="待处理区" if is_quarantined else "合格区",
        )
        crud_inventory_batch.create(db, obj_in=batch_in)
        return inspection

    @staticmethod
    def release_quarantined_batch(
        db: Session,
        inventory_batch_id: int,
        release_reason: str
    ) -> Optional[InventoryBatch]:
        batch = crud_inventory_batch.get(db, inventory_batch_id)
        if not batch:
            raise ValueError(f"库存批次不存在: {inventory_batch_id}")
        if not batch.is_quarantined:
            raise ValueError("该批次未被隔离")
        order = crud_disposition_order.get_by_batch(db, inventory_batch_id)
        if order:
            raise ValueError(
                f"该批次已绑定检验处置单 {order.order_no}，须通过处置单逐项批准，禁止整批解除"
            )
        return crud_inventory_batch.update(
            db,
            db_obj=batch,
            obj_in={
                "is_quarantined": False,
                "quarantine_reason": f"已解除隔离: {release_reason}",
                "available_quantity": batch.quantity,
                "location": "合格区"
            }
        )

    @staticmethod
    def reject_quarantined_batch(
        db: Session,
        inventory_batch_id: int,
        reject_reason: str
    ) -> Optional[InventoryBatch]:
        batch = crud_inventory_batch.get(db, inventory_batch_id)
        if not batch:
            raise ValueError(f"库存批次不存在: {inventory_batch_id}")
        order = crud_disposition_order.get_by_batch(db, inventory_batch_id)
        if order:
            raise ValueError(
                f"该批次已绑定检验处置单 {order.order_no}，须通过处置单逐项批准，禁止整批拒收"
            )
        return crud_inventory_batch.update(
            db,
            db_obj=batch,
            obj_in={
                "is_quarantined": True,
                "quarantine_reason": f"拒收: {reject_reason}",
                "available_quantity": 0,
                "location": "不合格品区"
            }
        )

    @staticmethod
    def can_use_batch(db: Session, inventory_batch_id: int) -> bool:
        batch = crud_inventory_batch.get(db, inventory_batch_id)
        if not batch:
            return False
        return not batch.is_quarantined and batch.available_quantity > 0

    @staticmethod
    def consume_material(
        db: Session,
        material_id: int,
        quantity: int
    ) -> bool:
        """生产领料：按物料加锁串行，原子条件扣减，单事务提交。

        任一环节失败整体回滚，不会出现部分批次已扣减的状态；
        非正数量直接拒绝（防止负数量反向增加库存）。
        """
        if quantity <= 0:
            return False
        with lock_pool.acquire(f"consume_material:{material_id}"):
            try:
                available_batches = (
                    db.query(InventoryBatch)
                    .filter(
                        InventoryBatch.material_id == material_id,
                        InventoryBatch.is_quarantined == False,
                        InventoryBatch.available_quantity > 0,
                    )
                    .order_by(InventoryBatch.id)
                    .all()
                )
                total_available = sum(b.available_quantity for b in available_batches)
                if total_available < quantity:
                    return False
                remaining = quantity
                for batch in available_batches:
                    if remaining <= 0:
                        break
                    consume_qty = min(batch.available_quantity, remaining)
                    result = db.execute(
                        update(InventoryBatch)
                        .where(
                            InventoryBatch.id == batch.id,
                            InventoryBatch.available_quantity >= consume_qty,
                        )
                        .values(
                            available_quantity=InventoryBatch.available_quantity - consume_qty
                        )
                        .execution_options(synchronize_session=False)
                    )
                    if result.rowcount != 1:
                        db.rollback()
                        return False
                    remaining -= consume_qty
                db.commit()
                return True
            except Exception:
                db.rollback()
                raise
