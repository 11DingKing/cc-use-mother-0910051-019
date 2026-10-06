# 制造零部件供应协同服务

本项目是使用 Python、FastAPI 与 SQLite 实现的服务端应用，覆盖车型、物料、BOM、供应商、采购、到货、检验、库存和短缺分析。它可在单个 Linux 应用容器内完成安装、测试、编译和接口验收，不依赖浏览器、外部数据库、缓存、消息队列或额外运行服务。

## 检验处置单

来料抽检不合格后，整批进入隔离，由检验处置单（`/api/v1/dispositions`）完成拆批处置：

- 建单时**固化取样量、不良数、合格率与检验结论快照**，后续复检只追加轮次（`inspections.round_no`/`inspection_samples`），不改写首轮证据。
- 隔离数量可同时分配到五类决定：`re_inspect`（复检）、`concession`（让步接收）、`rework`（返工）、`return`（退货）、`scrap`（报废），每类一单一条，逐条审批。
- 决定批准后在单事务内生成可追溯子批次（`inventory_batches.origin_batch_id`/`origin_disposition_id`）：让步接收批准即放行；复检批准后子批次仍隔离，复检合格才放行；返工完成拆出"合格孙批+未完成隔离孙批"；退货/报废为终态分录。
- **只有已放行子批次参与领料**；数量分配按"已批准+待决"预留，审批以条件更新（CAS）原子推进 `allocated_quantity`，领料按批次条件扣减。
- 复检推翻或审批改判可 `reverse`/`reject` 决定（已被领料消耗的放行决定禁止推翻），数量与子批次同时回滚。
- 任意时点可用 `GET /api/v1/dispositions/inventory/{batch_id}/balance` 查看数量平衡，用 `/integrity` 沿批次树校验"原始数量 = 所有去向之和，且每单位已释放库存都有放行依据"。

## 安装

```bash
python3 -m pip install -r requirements.txt -r requirements-dev.txt
```

## 测试

```bash
python3 -m pytest -q tests
```

## 编译

```bash
python3 -m compileall -q .
```

## 接口验收

```bash
python3 -c "from app.main import app; assert len(app.routes) > 5; print(len(app.routes))"
```

## 启动

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```
