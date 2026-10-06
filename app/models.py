from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, Boolean, Text, Date, UniqueConstraint
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base

class Material(Base):
    __tablename__ = "materials"
    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(50), unique=True, index=True, nullable=False)
    name = Column(String(100), nullable=False)
    category = Column(String(50), nullable=False)
    spec = Column(String(200))
    unit = Column(String(20), nullable=False)
    safety_stock = Column(Integer, default=0)
    is_critical = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    bom_items = relationship("BOMItem", back_populates="material")
    supply_capacities = relationship("SupplyCapacity", back_populates="material")
    purchase_suggestions = relationship("PurchaseSuggestion", back_populates="material")
    purchase_orders = relationship("PurchaseOrder", back_populates="material")
    deliveries = relationship("Delivery", back_populates="material")
    inventory_batches = relationship("InventoryBatch", back_populates="material")
    alternative_materials = relationship("AlternativeMaterial", 
                                         foreign_keys="AlternativeMaterial.material_id", 
                                         back_populates="material")
    alternative_for = relationship("AlternativeMaterial",
                                   foreign_keys="AlternativeMaterial.alternative_material_id",
                                   back_populates="alternative_material")

class VehicleModel(Base):
    __tablename__ = "vehicle_models"
    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(50), unique=True, index=True, nullable=False)
    name = Column(String(100), nullable=False)
    priority = Column(Integer, default=5)
    description = Column(Text)
    status = Column(String(20), default="active")
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    bom_items = relationship("BOMItem", back_populates="vehicle_model")
    production_batches = relationship("ProductionBatch", back_populates="vehicle_model")
    alternative_restrictions = relationship("AlternativeMaterialRestriction", back_populates="vehicle_model")

class BOMItem(Base):
    __tablename__ = "bom_items"
    id = Column(Integer, primary_key=True, index=True)
    vehicle_model_id = Column(Integer, ForeignKey("vehicle_models.id"), nullable=False)
    material_id = Column(Integer, ForeignKey("materials.id"), nullable=False)
    quantity = Column(Integer, nullable=False)
    remark = Column(String(200))
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    vehicle_model = relationship("VehicleModel", back_populates="bom_items")
    material = relationship("Material", back_populates="bom_items")

class Supplier(Base):
    __tablename__ = "suppliers"
    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(50), unique=True, index=True, nullable=False)
    name = Column(String(100), nullable=False)
    contact = Column(String(50))
    phone = Column(String(30))
    address = Column(String(300))
    rating = Column(Float, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    supply_capacities = relationship("SupplyCapacity", back_populates="supplier")
    purchase_orders = relationship("PurchaseOrder", back_populates="supplier")
    deliveries = relationship("Delivery", back_populates="supplier")

class SupplyCapacity(Base):
    __tablename__ = "supply_capacities"
    id = Column(Integer, primary_key=True, index=True)
    supplier_id = Column(Integer, ForeignKey("suppliers.id"), nullable=False)
    material_id = Column(Integer, ForeignKey("materials.id"), nullable=False)
    daily_capacity = Column(Integer, nullable=False)
    delivery_days = Column(Integer, nullable=False)
    pass_rate = Column(Float, nullable=False)
    current_stock = Column(Integer, default=0)
    unit_price = Column(Float, default=0)
    is_preferred = Column(Boolean, default=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    supplier = relationship("Supplier", back_populates="supply_capacities")
    material = relationship("Material", back_populates="supply_capacities")

class PurchaseSuggestion(Base):
    __tablename__ = "purchase_suggestions"
    id = Column(Integer, primary_key=True, index=True)
    material_id = Column(Integer, ForeignKey("materials.id"), nullable=False)
    suggested_quantity = Column(Integer, nullable=False)
    reason = Column(String(300))
    priority = Column(Integer, default=5)
    suggested_supplier_id = Column(Integer, ForeignKey("suppliers.id"))
    expected_delivery_date = Column(Date)
    status = Column(String(20), default="pending")
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    material = relationship("Material", back_populates="purchase_suggestions")

class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"
    id = Column(Integer, primary_key=True, index=True)
    order_no = Column(String(50), unique=True, index=True, nullable=False)
    supplier_id = Column(Integer, ForeignKey("suppliers.id"), nullable=False)
    material_id = Column(Integer, ForeignKey("materials.id"), nullable=False)
    quantity = Column(Integer, nullable=False)
    expected_date = Column(Date, nullable=False)
    actual_date = Column(Date)
    status = Column(String(20), default="ordered")
    remark = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    supplier = relationship("Supplier", back_populates="purchase_orders")
    material = relationship("Material", back_populates="purchase_orders")
    deliveries = relationship("Delivery", back_populates="purchase_order")
    delay_impacts = relationship("DelayImpact", back_populates="purchase_order")

class Delivery(Base):
    __tablename__ = "deliveries"
    id = Column(Integer, primary_key=True, index=True)
    delivery_no = Column(String(50), unique=True, index=True, nullable=False)
    purchase_order_id = Column(Integer, ForeignKey("purchase_orders.id"), nullable=False)
    supplier_id = Column(Integer, ForeignKey("suppliers.id"), nullable=False)
    material_id = Column(Integer, ForeignKey("materials.id"), nullable=False)
    quantity = Column(Integer, nullable=False)
    delivery_date = Column(Date, nullable=False)
    batch_no = Column(String(50))
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    purchase_order = relationship("PurchaseOrder", back_populates="deliveries")
    supplier = relationship("Supplier", back_populates="deliveries")
    material = relationship("Material", back_populates="deliveries")
    inspections = relationship(
        "Inspection", back_populates="delivery",
        cascade="all, delete-orphan", order_by="Inspection.id"
    )
    inventory_batches = relationship(
        "InventoryBatch", back_populates="delivery", order_by="InventoryBatch.id"
    )

class Inspection(Base):
    __tablename__ = "inspections"
    id = Column(Integer, primary_key=True, index=True)
    delivery_id = Column(Integer, ForeignKey("deliveries.id"), nullable=False)
    sample_size = Column(Integer, nullable=False)
    defective_count = Column(Integer, default=0)
    pass_rate = Column(Float, nullable=False)
    result = Column(String(20), nullable=False)
    inspector = Column(String(50))
    inspection_date = Column(Date, nullable=False)
    remark = Column(Text)
    # 复检轮次：首轮为1，复检只追加不覆盖；is_latest 标记当前生效结论
    round_no = Column(Integer, nullable=False, default=1, server_default="1")
    is_latest = Column(Boolean, nullable=False, default=True, server_default="1")
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    delivery = relationship("Delivery", back_populates="inspections")
    samples = relationship(
        "InspectionSample", back_populates="inspection",
        cascade="all, delete-orphan", order_by="InspectionSample.id"
    )
    disposition_orders = relationship("DispositionOrder", back_populates="inspection")

class InspectionSample(Base):
    """检验取样明细：处置单一旦建立即固化快照，复检取样只追加，保证原批次证据链完整"""
    __tablename__ = "inspection_samples"
    id = Column(Integer, primary_key=True, index=True)
    inspection_id = Column(Integer, ForeignKey("inspections.id"), nullable=False)
    round_no = Column(Integer, nullable=False, default=1)
    sample_size = Column(Integer, nullable=False)
    defective_count = Column(Integer, nullable=False, default=0)
    pass_rate = Column(Float, nullable=False)
    result = Column(String(20), nullable=False)
    inspector = Column(String(50))
    inspected_at = Column(DateTime(timezone=True), server_default=func.now())
    remark = Column(Text)

    inspection = relationship("Inspection", back_populates="samples")

class InventoryBatch(Base):
    __tablename__ = "inventory_batches"
    id = Column(Integer, primary_key=True, index=True)
    delivery_id = Column(Integer, ForeignKey("deliveries.id"), nullable=False)
    material_id = Column(Integer, ForeignKey("materials.id"), nullable=False)
    quantity = Column(Integer, nullable=False)
    available_quantity = Column(Integer, nullable=False)
    is_quarantined = Column(Boolean, default=False)
    quarantine_reason = Column(String(300))
    location = Column(String(100))
    expire_date = Column(Date)
    # 拆批溯源：处置决定批准后生成的子批次记录来源；原批次 origin_* 为空
    origin_batch_id = Column(Integer, ForeignKey("inventory_batches.id"))
    origin_disposition_id = Column(
        Integer,
        ForeignKey("disposition_decisions.id", use_alter=True, name="fk_batch_origin_decision"),
        nullable=True
    )
    # 批次状态：normal（正常）/ quarantined（隔离）/ rework（返工中）/ returned（已退货）/ scrapped（已报废）
    status = Column(String(20), nullable=False, default="normal", server_default="normal")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    delivery = relationship("Delivery", back_populates="inventory_batches")
    material = relationship("Material", back_populates="inventory_batches")
    origin_batch = relationship(
        "InventoryBatch", remote_side="InventoryBatch.id", foreign_keys=[origin_batch_id]
    )
    origin_decision = relationship(
        "DispositionDecision", foreign_keys=[origin_disposition_id]
    )

class DispositionOrder(Base):
    """检验处置单：不合格隔离后建立，固化取样与检验结论快照，数量分配到若干处置决定"""
    __tablename__ = "disposition_orders"
    id = Column(Integer, primary_key=True, index=True)
    order_no = Column(String(50), unique=True, index=True, nullable=False)
    inventory_batch_id = Column(Integer, ForeignKey("inventory_batches.id"), nullable=False)
    inspection_id = Column(Integer, ForeignKey("inspections.id"), nullable=False)
    material_id = Column(Integer, ForeignKey("materials.id"), nullable=False)
    # 处置单覆盖的隔离数量（通常=原批次数量，复检推翻可只处置剩余部分）
    source_quantity = Column(Integer, nullable=False)
    # 已批准去向数量合计，随审批原子推进；closed 时必须等于 source_quantity
    allocated_quantity = Column(Integer, nullable=False, default=0, server_default="0")
    # 建单时固化的取样与检验结论快照，复检只追加新轮次，不改写本快照
    sample_size = Column(Integer, nullable=False)
    defective_count = Column(Integer, nullable=False, default=0)
    pass_rate = Column(Float, nullable=False)
    inspection_result = Column(String(20), nullable=False)
    inspection_round = Column(Integer, nullable=False, default=1)
    inspector = Column(String(50))
    inspection_date = Column(Date, nullable=False)
    status = Column(String(20), nullable=False, default="draft", server_default="draft")  # draft/approving/closed/cancelled
    remark = Column(Text)
    created_by = Column(String(50))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    inventory_batch = relationship("InventoryBatch", foreign_keys=[inventory_batch_id])
    inspection = relationship("Inspection", back_populates="disposition_orders")
    material = relationship("Material")
    decisions = relationship(
        "DispositionDecision", back_populates="disposition_order",
        cascade="all, delete-orphan", order_by="DispositionDecision.id"
    )

class DispositionDecision(Base):
    """处置决定：复检/让步接收/返工/退货/报废的数量分录，逐个审批，批准即生成可追溯子批次或终态记录"""
    __tablename__ = "disposition_decisions"
    __table_args__ = (
        UniqueConstraint("disposition_order_id", "decision_type", name="uq_disposition_decision_type"),
    )
    id = Column(Integer, primary_key=True, index=True)
    disposition_order_id = Column(Integer, ForeignKey("disposition_orders.id"), nullable=False)
    # re_inspect（复检）/ concession（让步接收）/ rework（返工）/ return（退货）/ scrap（报废）
    decision_type = Column(String(20), nullable=False)
    quantity = Column(Integer, nullable=False)
    reason = Column(String(300))
    status = Column(String(20), nullable=False, default="pending", server_default="pending")  # pending/approved/rejected/reversed
    approver = Column(String(50))
    approved_at = Column(DateTime(timezone=True))
    # 批准后生成的子批次（复检/让步/返工的数量去向载体）
    child_batch_id = Column(Integer, ForeignKey("inventory_batches.id"), nullable=True)
    # 复检结论（仅 decision_type=re_inspect）：pending_inspection/passed/failed
    reinspect_result = Column(String(30), nullable=True)
    # 返工完成登记（仅 decision_type=rework）：已完成放行数量
    rework_completed_qty = Column(Integer, nullable=True)
    rework_completed_at = Column(DateTime(timezone=True))
    rework_inspector = Column(String(50))
    reversed_at = Column(DateTime(timezone=True))
    reverse_reason = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    disposition_order = relationship("DispositionOrder", back_populates="decisions")
    child_batch = relationship(
        "InventoryBatch", foreign_keys=[child_batch_id], post_update=True
    )

class AlternativeMaterial(Base):
    __tablename__ = "alternative_materials"
    id = Column(Integer, primary_key=True, index=True)
    material_id = Column(Integer, ForeignKey("materials.id"), nullable=False)
    alternative_material_id = Column(Integer, ForeignKey("materials.id"), nullable=False)
    priority = Column(Integer, default=1)
    is_active = Column(Boolean, default=True)
    remark = Column(String(300))
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    material = relationship("Material", foreign_keys=[material_id], back_populates="alternative_materials")
    alternative_material = relationship("Material", foreign_keys=[alternative_material_id], back_populates="alternative_for")
    restrictions = relationship("AlternativeMaterialRestriction", back_populates="alternative")

class AlternativeMaterialRestriction(Base):
    __tablename__ = "alternative_restrictions"
    id = Column(Integer, primary_key=True, index=True)
    alternative_id = Column(Integer, ForeignKey("alternative_materials.id"), nullable=False)
    vehicle_model_id = Column(Integer, ForeignKey("vehicle_models.id"), nullable=False)
    is_allowed = Column(Boolean, default=False)
    remark = Column(String(300))
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    alternative = relationship("AlternativeMaterial", back_populates="restrictions")
    vehicle_model = relationship("VehicleModel", back_populates="alternative_restrictions")

class ProductionBatch(Base):
    __tablename__ = "production_batches"
    id = Column(Integer, primary_key=True, index=True)
    batch_no = Column(String(50), unique=True, index=True, nullable=False)
    vehicle_model_id = Column(Integer, ForeignKey("vehicle_models.id"), nullable=False)
    quantity = Column(Integer, nullable=False)
    plan_date = Column(Date, nullable=False)
    status = Column(String(20), default="planned")
    remark = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    vehicle_model = relationship("VehicleModel", back_populates="production_batches")
    delay_impacts = relationship("DelayImpact", back_populates="production_batch")

class DelayImpact(Base):
    __tablename__ = "delay_impacts"
    id = Column(Integer, primary_key=True, index=True)
    purchase_order_id = Column(Integer, ForeignKey("purchase_orders.id"), nullable=False)
    production_batch_id = Column(Integer, ForeignKey("production_batches.id"), nullable=False)
    impact_level = Column(String(20), nullable=False)
    estimated_delay_days = Column(Integer, default=0)
    remark = Column(String(300))
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    purchase_order = relationship("PurchaseOrder", back_populates="delay_impacts")
    production_batch = relationship("ProductionBatch", back_populates="delay_impacts")

class SupplierConfirmation(Base):
    __tablename__ = "supplier_confirmations"
    id = Column(Integer, primary_key=True, index=True)
    confirmation_no = Column(String(50), unique=True, index=True, nullable=False)
    purchase_suggestion_id = Column(Integer, ForeignKey("purchase_suggestions.id"), nullable=False)
    supplier_id = Column(Integer, ForeignKey("suppliers.id"), nullable=False)
    material_id = Column(Integer, ForeignKey("materials.id"), nullable=False)
    requested_quantity = Column(Integer, nullable=False)
    committed_quantity = Column(Integer, nullable=False)
    committed_delivery_date = Column(Date)
    shortage_quantity = Column(Integer, default=0)
    status = Column(String(20), default="pending")
    confirmation_note = Column(Text)
    confirmed_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    supplier = relationship("Supplier")
    material = relationship("Material")
    purchase_suggestion = relationship("PurchaseSuggestion")
    batches = relationship("SupplierConfirmationBatch", back_populates="confirmation", cascade="all, delete-orphan")
    shortage_impacts = relationship("SupplierShortageImpact", back_populates="confirmation", cascade="all, delete-orphan")

class SupplierConfirmationBatch(Base):
    __tablename__ = "supplier_confirmation_batches"
    id = Column(Integer, primary_key=True, index=True)
    confirmation_id = Column(Integer, ForeignKey("supplier_confirmations.id"), nullable=False)
    batch_no = Column(String(50), nullable=False)
    quantity = Column(Integer, nullable=False)
    planned_date = Column(Date, nullable=False)
    remark = Column(String(300))
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    confirmation = relationship("SupplierConfirmation", back_populates="batches")

class SupplierShortageImpact(Base):
    __tablename__ = "supplier_shortage_impacts"
    id = Column(Integer, primary_key=True, index=True)
    confirmation_id = Column(Integer, ForeignKey("supplier_confirmations.id"), nullable=False)
    production_batch_id = Column(Integer, ForeignKey("production_batches.id"), nullable=False)
    affected_vehicle_model_id = Column(Integer, ForeignKey("vehicle_models.id"), nullable=False)
    shortage_material_id = Column(Integer, ForeignKey("materials.id"), nullable=False)
    shortage_quantity = Column(Integer, nullable=False)
    impact_level = Column(String(20), nullable=False)
    estimated_delay_days = Column(Integer, default=0)
    remark = Column(String(300))
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    confirmation = relationship("SupplierConfirmation", back_populates="shortage_impacts")
    production_batch = relationship("ProductionBatch")
    vehicle_model = relationship("VehicleModel")
    material = relationship("Material")
