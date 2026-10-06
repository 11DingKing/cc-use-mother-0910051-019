from sqlalchemy.orm import Session
from typing import Optional
from app.crud.purchase import crud_inspection, crud_inventory_batch, crud_delivery
from app.schemas import InspectionCreate
from app.models import Inspection, InventoryBatch, InspectionSample
from app.crud.disposition import crud_disposition_order

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
            raise ValueError("该到货记录已存在抽检结果")

        try:
            inspection = Inspection(
                delivery_id=inspection_in.delivery_id,
                sample_size=inspection_in.sample_size,
                defective_count=inspection_in.defective_count,
                pass_rate=inspection_in.pass_rate,
                result=inspection_in.result,
                inspector=inspection_in.inspector,
                inspection_date=inspection_in.inspection_date,
                remark=inspection_in.remark,
                round_no=1,
                is_latest=True,
            )
            db.add(inspection)
            db.flush()
            # 首轮取样明细固化（复检轮次由处置服务只追加）
            db.add(InspectionSample(
                inspection_id=inspection.id,
                round_no=1,
                sample_size=inspection.sample_size,
                defective_count=inspection.defective_count,
                pass_rate=inspection.pass_rate,
                result=inspection.result,
                inspector=inspection.inspector,
                remark=inspection.remark,
            ))

            is_quarantined = inspection.result != "passed"
            quarantine_reason = None
            if is_quarantined:
                if inspection.defective_count > 0:
                    quarantine_reason = f"抽检不合格，不良品{inspection.defective_count}件，合格率{inspection.pass_rate:.2%}"
                else:
                    quarantine_reason = "抽检不合格"
            available_qty = 0 if is_quarantined else delivery.quantity
            batch = InventoryBatch(
                delivery_id=delivery.id,
                material_id=delivery.material_id,
                quantity=delivery.quantity,
                available_quantity=available_qty,
                is_quarantined=is_quarantined,
                quarantine_reason=quarantine_reason,
                location="待处理区" if is_quarantined else "合格区",
                status="quarantined" if is_quarantined else "normal",
            )
            db.add(batch)
            db.commit()
            db.refresh(inspection)
        except Exception:
            db.rollback()
            raise
        return inspection

    @staticmethod
    def release_quarantined_batch(
        db: Session,
        inventory_batch_id: int,
        release_reason: str
    ) -> Optional[InventoryBatch]:
        """整批解除（保留的应急入口）：已建立处置单的批次必须走处置流程，禁止无依据整批放行"""
        batch = crud_inventory_batch.get(db, inventory_batch_id)
        if not batch:
            raise ValueError(f"库存批次不存在: {inventory_batch_id}")
        if not batch.is_quarantined:
            raise ValueError("该批次未被隔离")
        active = [
            o for o in crud_disposition_order.get_by_batch(db, inventory_batch_id)
            if o.status in ("draft", "approving")
        ]
        if active:
            raise ValueError(
                f"该批次已建立处置单 {active[0].order_no}，请通过处置决定审批放行，不能整批解除"
            )
        return crud_inventory_batch.update(
            db,
            db_obj=batch,
            obj_in={
                "is_quarantined": False,
                "quarantine_reason": f"已解除隔离: {release_reason}",
                "available_quantity": batch.quantity,
                "location": "合格区",
                "status": "normal",
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
        return crud_inventory_batch.update(
            db,
            db_obj=batch,
            obj_in={
                "is_quarantined": True,
                "quarantine_reason": f"拒收: {reject_reason}",
                "available_quantity": 0,
                "location": "不合格品区",
                "status": "quarantined",
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
        """
        生产领料：锁定候选批次后按条件更新原子扣减，与处置审批/其他领料并发时不会超领。
        只有非隔离（含让步接收、复检合格放行、返工合格的子批次）才能参与。
        """
        if quantity <= 0:
            raise ValueError("领料数量必须为正")
        try:
            # 结束可能存在的只读事务，丢弃旧快照，随后以写锁串行化（SQLite）
            if db.in_transaction():
                db.commit()
            is_sqlite = db.bind.dialect.name == "sqlite"

            # 先取候选 id（只读），再按 id 固定顺序加锁防死锁
            candidate_ids = [
                b.id for b in crud_inventory_batch.get_available_batches(db, material_id)
            ]
            if not candidate_ids:
                return False
            q = db.query(InventoryBatch).filter(InventoryBatch.id.in_(candidate_ids))
            if not is_sqlite:
                q.with_for_update().all()
            else:
                # 空写抢占 SQLite 库级写锁；拿到锁后其他并发领料/审批均已提交
                db.query(InventoryBatch).filter(
                    InventoryBatch.id.in_(candidate_ids)
                ).update({InventoryBatch.location.name: InventoryBatch.location},
                         synchronize_session=False)
            db.expire_all()

            # 锁后重查最新可用量
            locked = crud_inventory_batch.get_available_batches(db, material_id)
            total_available = sum(b.available_quantity for b in locked)
            if total_available < quantity:
                db.rollback()
                return False

            remaining = quantity
            for batch in locked:
                if remaining <= 0:
                    break
                take = min(batch.available_quantity, remaining)
                updated = db.query(InventoryBatch).filter(
                    InventoryBatch.id == batch.id,
                    InventoryBatch.available_quantity >= take,
                ).update(
                    {"available_quantity": InventoryBatch.available_quantity - take},
                    synchronize_session=False,
                )
                if updated != 1:
                    # 并发领料导致该批数量变化，整体回滚
                    raise RuntimeError("库存扣减并发冲突")
                remaining -= take

            if remaining != 0:
                raise RuntimeError("库存扣减后数量不平，事务回滚")
            db.commit()
        except Exception:
            db.rollback()
            raise
        return True
