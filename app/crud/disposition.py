from sqlalchemy.orm import Session
from typing import List, Optional
from app.models import DispositionOrder, DispositionLine

class CRUDDispositionOrder:
    """检验处置单查询。写操作由 DispositionService 在同一事务内完成，这里不做 commit。"""

    def get(self, db: Session, order_id: int) -> Optional[DispositionOrder]:
        return db.query(DispositionOrder).filter(DispositionOrder.id == order_id).first()

    def get_by_order_no(self, db: Session, order_no: str) -> Optional[DispositionOrder]:
        return db.query(DispositionOrder).filter(DispositionOrder.order_no == order_no).first()

    def get_by_batch(self, db: Session, inventory_batch_id: int) -> Optional[DispositionOrder]:
        return db.query(DispositionOrder).filter(
            DispositionOrder.inventory_batch_id == inventory_batch_id
        ).first()

    def get_multi(self, db: Session, status: Optional[str] = None,
                  material_id: Optional[int] = None,
                  skip: int = 0, limit: int = 100) -> List[DispositionOrder]:
        query = db.query(DispositionOrder)
        if status:
            query = query.filter(DispositionOrder.status == status)
        if material_id:
            query = query.filter(DispositionOrder.material_id == material_id)
        return query.order_by(DispositionOrder.id.desc()).offset(skip).limit(limit).all()

    def get_lines(self, db: Session, order_id: int) -> List[DispositionLine]:
        return db.query(DispositionLine).filter(
            DispositionLine.order_id == order_id
        ).order_by(DispositionLine.line_no).all()

crud_disposition_order = CRUDDispositionOrder()
