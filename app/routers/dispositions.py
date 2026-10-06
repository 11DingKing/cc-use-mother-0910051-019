from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional
from app.database import get_db
from app.crud.disposition import crud_disposition_order
from app.schemas import (
    DispositionOrder, DispositionOrderCreate, DispositionOrderDetail,
    DispositionLine, DispositionLineCreate,
    DispositionLineApproveRequest, DispositionLineRejectRequest,
    RecheckResultSubmit, DispositionTrace
)
from app.services.disposition import DispositionService

router = APIRouter(prefix="/dispositions", tags=["检验处置"])

@router.post("", response_model=DispositionOrder)
def create_disposition_order(order_in: DispositionOrderCreate, db: Session = Depends(get_db)):
    """对隔离批次创建检验处置单，固定取样与检验结论作为处置依据。"""
    try:
        return DispositionService.create_order(db, order_in)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.get("", response_model=List[DispositionOrder])
def get_disposition_orders(
    status: Optional[str] = None,
    material_id: Optional[int] = None,
    db: Session = Depends(get_db)
):
    return crud_disposition_order.get_multi(db, status=status, material_id=material_id)

@router.get("/{order_id}", response_model=DispositionOrderDetail)
def get_disposition_order(order_id: int, db: Session = Depends(get_db)):
    try:
        return DispositionService.get_order_detail(db, order_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

@router.get("/{order_id}/trace", response_model=DispositionTrace)
def get_disposition_trace(order_id: int, db: Session = Depends(get_db)):
    """追溯视图：校验原始数量等于所有去向之和，且释放库存均有处置依据。"""
    try:
        return DispositionService.get_trace(db, order_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

@router.post("/{order_id}/lines", response_model=DispositionLine)
def add_disposition_line(order_id: int, line_in: DispositionLineCreate, db: Session = Depends(get_db)):
    try:
        return DispositionService.add_line(db, order_id, line_in)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.delete("/{order_id}/lines/{line_id}")
def remove_disposition_line(order_id: int, line_id: int, db: Session = Depends(get_db)):
    try:
        DispositionService.remove_line(db, order_id, line_id)
        return {"message": "已删除待审批决定"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/{order_id}/lines/{line_id}/approve", response_model=DispositionLine)
def approve_disposition_line(
    order_id: int, line_id: int,
    approve_in: DispositionLineApproveRequest,
    db: Session = Depends(get_db)
):
    """批准决定：同一事务内生成可追溯子批次，释放部分才可领料。"""
    try:
        return DispositionService.approve_line(
            db, order_id, line_id, approve_in.approver, approve_in.remark
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/{order_id}/lines/{line_id}/reject", response_model=DispositionLine)
def reject_disposition_line(
    order_id: int, line_id: int,
    reject_in: DispositionLineRejectRequest,
    db: Session = Depends(get_db)
):
    try:
        return DispositionService.reject_line(
            db, order_id, line_id, reject_in.approver, reject_in.remark
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/{order_id}/lines/{line_id}/recheck-result", response_model=DispositionLine)
def submit_recheck_result(
    order_id: int, line_id: int,
    result_in: RecheckResultSubmit,
    db: Session = Depends(get_db)
):
    """录入复检结论：合格则推翻原判释放，不合格则维持隔离可再处置。"""
    try:
        return DispositionService.submit_recheck_result(db, order_id, line_id, result_in)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
