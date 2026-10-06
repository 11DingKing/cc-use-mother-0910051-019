from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional

from app.database import get_db
from app.schemas import (
    DispositionOrder, DispositionOrderCreate, DispositionDecision,
    DispositionApproveRequest, DispositionRejectRequest,
    ReinspectionCreate, DispositionBalance, DispositionReverseRequest,
    ReworkCompleteRequest,
)
from app.services.disposition import DispositionService, QuantityBalanceError

router = APIRouter(prefix="/dispositions", tags=["检验处置单"])


@router.post("", response_model=DispositionOrder)
def create_disposition_order(order_in: DispositionOrderCreate, db: Session = Depends(get_db)):
    try:
        return DispositionService.create_disposition_order(db, order_in)
    except (ValueError, QuantityBalanceError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("", response_model=List[DispositionOrder])
def list_disposition_orders(
    inventory_batch_id: Optional[int] = None,
    status: Optional[str] = None,
    db: Session = Depends(get_db)
):
    return DispositionService.list_orders(db, inventory_batch_id, status)


@router.get("/{order_id}", response_model=DispositionOrder)
def get_disposition_order(order_id: int, db: Session = Depends(get_db)):
    try:
        return DispositionService.get_order(db, order_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{order_id}/decisions/{decision_id}/approve", response_model=DispositionDecision)
def approve_decision(
    order_id: int,
    decision_id: int,
    req: DispositionApproveRequest,
    db: Session = Depends(get_db)
):
    try:
        return DispositionService.approve_decision(db, order_id, decision_id, req)
    except QuantityBalanceError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{order_id}/decisions/{decision_id}/reject", response_model=DispositionDecision)
def reject_decision(
    order_id: int,
    decision_id: int,
    req: DispositionRejectRequest,
    db: Session = Depends(get_db)
):
    try:
        return DispositionService.reject_decision(db, order_id, decision_id, req)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{order_id}/decisions/{decision_id}/reinspection", response_model=DispositionDecision)
def record_reinspection(
    order_id: int,
    decision_id: int,
    result_in: ReinspectionCreate,
    db: Session = Depends(get_db)
):
    try:
        return DispositionService.record_reinspection(db, order_id, decision_id, result_in)
    except QuantityBalanceError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{order_id}/decisions/{decision_id}/rework-complete", response_model=DispositionDecision)
def complete_rework(
    order_id: int,
    decision_id: int,
    req: ReworkCompleteRequest,
    db: Session = Depends(get_db)
):
    try:
        return DispositionService.complete_rework(db, order_id, decision_id, req)
    except QuantityBalanceError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{order_id}/decisions/{decision_id}/reverse", response_model=DispositionDecision)
def reverse_decision(
    order_id: int,
    decision_id: int,
    req: DispositionReverseRequest,
    db: Session = Depends(get_db)
):
    try:
        return DispositionService.reverse_decision(
            db, order_id, decision_id, req.approver, req.reverse_reason
        )
    except QuantityBalanceError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/inventory/{batch_id}/balance", response_model=DispositionBalance)
def get_disposition_balance(batch_id: int, db: Session = Depends(get_db)):
    try:
        return DispositionService.get_balance(db, batch_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/inventory/{batch_id}/integrity")
def assert_integrity(batch_id: int, db: Session = Depends(get_db)):
    try:
        DispositionService.assert_integrity(db, batch_id)
        return {"inventory_batch_id": batch_id, "integrity": "ok"}
    except (ValueError, QuantityBalanceError) as e:
        raise HTTPException(status_code=409, detail=str(e))
