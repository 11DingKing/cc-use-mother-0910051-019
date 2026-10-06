from sqlalchemy.orm import Session, joinedload
from typing import List, Optional
from app.crud.base import CRUDBase
from app.models import (
    DispositionOrder, DispositionDecision, InspectionSample, Inspection
)


class CRUDDispositionOrder(CRUDBase[DispositionOrder, dict, dict]):
    def get_by_order_no(self, db: Session, order_no: str) -> Optional[DispositionOrder]:
        return db.query(DispositionOrder).filter(DispositionOrder.order_no == order_no).first()

    def get_by_batch(self, db: Session, inventory_batch_id: int) -> List[DispositionOrder]:
        return db.query(DispositionOrder).filter(
            DispositionOrder.inventory_batch_id == inventory_batch_id
        ).order_by(DispositionOrder.id).all()

    def get_with_decisions(self, db: Session, order_id: int) -> Optional[DispositionOrder]:
        return db.query(DispositionOrder).options(
            joinedload(DispositionOrder.decisions)
        ).filter(DispositionOrder.id == order_id).first()

    def get_by_inspection(self, db: Session, inspection_id: int) -> List[DispositionOrder]:
        return db.query(DispositionOrder).filter(
            DispositionOrder.inspection_id == inspection_id
        ).all()


crud_disposition_order = CRUDDispositionOrder(DispositionOrder)


class CRUDDispositionDecision(CRUDBase[DispositionDecision, dict, dict]):
    def get_by_order(self, db: Session, disposition_order_id: int) -> List[DispositionDecision]:
        return db.query(DispositionDecision).filter(
            DispositionDecision.disposition_order_id == disposition_order_id
        ).order_by(DispositionDecision.id).all()

    def get_by_order_and_type(
        self, db: Session, disposition_order_id: int, decision_type: str
    ) -> Optional[DispositionDecision]:
        return db.query(DispositionDecision).filter(
            DispositionDecision.disposition_order_id == disposition_order_id,
            DispositionDecision.decision_type == decision_type
        ).first()

    def approved_quantity_by_order(self, db: Session, disposition_order_id: int) -> int:
        """已批准（未被推翻）决定的数量合计"""
        rows = db.query(DispositionDecision).filter(
            DispositionDecision.disposition_order_id == disposition_order_id,
            DispositionDecision.status == "approved"
        ).all()
        return sum(r.quantity for r in rows)


crud_disposition_decision = CRUDDispositionDecision(DispositionDecision)


class CRUDInspectionSample(CRUDBase[InspectionSample, dict, dict]):
    def get_by_inspection(self, db: Session, inspection_id: int) -> List[InspectionSample]:
        return db.query(InspectionSample).filter(
            InspectionSample.inspection_id == inspection_id
        ).order_by(InspectionSample.round_no, InspectionSample.id).all()

    def latest_round(self, db: Session, inspection_id: int) -> int:
        rows = self.get_by_inspection(db, inspection_id)
        return max((r.round_no for r in rows), default=0)


crud_inspection_sample = CRUDInspectionSample(InspectionSample)


class CRUDInspectionRound:
    """复检轮次：同一到货的检验记录只追加，is_latest 标记当前结论"""

    def list_by_delivery(self, db: Session, delivery_id: int) -> List[Inspection]:
        return db.query(Inspection).filter(
            Inspection.delivery_id == delivery_id
        ).order_by(Inspection.round_no, Inspection.id).all()

    def latest_by_delivery(self, db: Session, delivery_id: int) -> Optional[Inspection]:
        return db.query(Inspection).filter(
            Inspection.delivery_id == delivery_id,
            Inspection.is_latest == True  # noqa: E712
        ).first()


crud_inspection_round = CRUDInspectionRound()
