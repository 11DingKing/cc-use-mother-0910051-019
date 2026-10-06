from datetime import date

from fastapi.testclient import TestClient

from tests.test_data_factory import DataFactory
from app.main import app

from app.services.inspection import InspectionService
from app.crud.purchase import crud_delivery
from app.models import InventoryBatch
from app.schemas import DeliveryCreate, InspectionCreate


def _failed_batch(factory, db, qty=100):
    factory.create_purchase_order("PO-API", "TS002", "TM002",
                                  quantity=qty, expected_date=date.today())
    delivery = crud_delivery.create(db, obj_in=DeliveryCreate(
        delivery_no="D-API", purchase_order_id=factory.purchase_orders["PO-API"].id,
        supplier_id=factory.suppliers["TS002"].id,
        material_id=factory.materials["TM002"].id,
        quantity=qty, delivery_date=date.today(), batch_no="B-API"))
    InspectionService.record_inspection_and_process(db, InspectionCreate(
        delivery_id=delivery.id, sample_size=20, defective_count=8,
        pass_rate=0.6, result="failed", inspector="王检验",
        inspection_date=date.today()))
    return db.query(InventoryBatch).filter(
        InventoryBatch.delivery_id == delivery.id).first()


class TestDispositionAPI:
    """处置单 HTTP 主链路 + 冲突码"""

    def test_full_disposition_flow_over_http(self, db_session, override_get_db):
        factory = DataFactory(db_session)
        factory.setup_basic_supply_chain()
        batch = _failed_batch(factory, db_session)
        client = TestClient(app)

        # 建单：60 复检 + 40 让步
        resp = client.post("/api/v1/dispositions", json={
            "inventory_batch_id": batch.id,
            "created_by": "质量员",
            "decisions": [
                {"decision_type": "re_inspect", "quantity": 60, "reason": "复检"},
                {"decision_type": "concession", "quantity": 40, "reason": "让步"}
            ]
        })
        assert resp.status_code == 200, resp.text
        order = resp.json()
        order_id = order["id"]
        dec = {d["decision_type"]: d["id"] for d in order["decisions"]}
        assert order["sample_size"] == 20
        assert order["inspection_result"] == "failed"

        # 超量建单 → 400
        resp = client.post("/api/v1/dispositions", json={
            "inventory_batch_id": batch.id,
            "decisions": [{"decision_type": "scrap", "quantity": 1}]
        })
        assert resp.status_code == 400

        # 批准让步 → 子批次放行
        resp = client.post(
            f"/api/v1/dispositions/{order_id}/decisions/{dec['concession']}/approve",
            json={"approver": "张主管"})
        assert resp.status_code == 200, resp.text

        # 重复批准同一决定 → 400
        resp = client.post(
            f"/api/v1/dispositions/{order_id}/decisions/{dec['concession']}/approve",
            json={"approver": "张主管"})
        assert resp.status_code == 400

        # 批准复检并登记合格 → 再放行 60
        resp = client.post(
            f"/api/v1/dispositions/{order_id}/decisions/{dec['re_inspect']}/approve",
            json={"approver": "张主管"})
        assert resp.status_code == 200
        resp = client.post(
            f"/api/v1/dispositions/{order_id}/decisions/{dec['re_inspect']}/reinspection",
            json={"sample_size": 30, "defective_count": 0, "pass_rate": 1.0,
                  "result": "passed", "inspector": "复检员",
                  "inspection_date": str(date.today())})
        assert resp.status_code == 200, resp.text

        # 单据关闭、平衡与完整性
        resp = client.get(f"/api/v1/dispositions/{order_id}")
        assert resp.json()["status"] == "closed"
        resp = client.get(f"/api/v1/dispositions/inventory/{batch.id}/balance")
        bal = resp.json()
        assert bal["allocated_quantity"] == 100
        assert bal["is_balanced"] is True
        assert bal["released_available_quantity"] == 100
        resp = client.get(f"/api/v1/dispositions/inventory/{batch.id}/integrity")
        assert resp.json()["integrity"] == "ok"

        # 只有放行的 100 件可领料
        resp = client.post("/api/v1/purchase/consume",
                           params={"material_id": factory.materials["TM002"].id,
                                   "quantity": 100})
        assert resp.status_code == 200, resp.text
        resp = client.post("/api/v1/purchase/consume",
                           params={"material_id": factory.materials["TM002"].id,
                                   "quantity": 1})
        assert resp.status_code == 400

    def test_terminals_not_released_over_http(self, db_session, override_get_db):
        factory = DataFactory(db_session)
        factory.setup_basic_supply_chain()
        batch = _failed_batch(factory, db_session, qty=50)
        client = TestClient(app)
        resp = client.post("/api/v1/dispositions", json={
            "inventory_batch_id": batch.id,
            "decisions": [
                {"decision_type": "return", "quantity": 20},
                {"decision_type": "scrap", "quantity": 30}
            ]
        })
        order = resp.json()
        dec = {d["decision_type"]: d["id"] for d in order["decisions"]}
        for t in ("return", "scrap"):
            r = client.post(
                f"/api/v1/dispositions/{order['id']}/decisions/{dec[t]}/approve",
                json={"approver": "张主管"})
            assert r.status_code == 200
        bal = client.get(f"/api/v1/dispositions/inventory/{batch.id}/balance").json()
        assert bal["released_available_quantity"] == 0
        assert client.get(
            f"/api/v1/dispositions/inventory/{batch.id}/integrity").json()["integrity"] == "ok"
