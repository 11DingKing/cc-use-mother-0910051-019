# 制造零部件供应协同服务

本项目是使用 Python、FastAPI 与 SQLite 实现的服务端应用，覆盖车型、物料、BOM、供应商、采购、到货、检验、库存和短缺分析。它可在单个 Linux 应用容器内完成安装、测试、编译和接口验收，不依赖浏览器、外部数据库、缓存、消息队列或额外运行服务。

## 检验处置单（不合格品拆批处置）

来料抽检不合格后，隔离批次不再只能整批解除或整批拒收，而是通过检验处置单把隔离数量分配到多种去向：

- `POST /api/v1/dispositions` 对隔离批次建单，创建时把取样数、不良数、合格率、检验结论快照冻结到单上（首检结论或上一轮复检结论），一个批次同时只允许一张处置单
- 处置决定支持 `recheck`（复检）、`concession`（让步接收）、`rework`（返工）、`return`（退货）、`scrap`（报废），按行审批
- 批准即在同一事务内生成可追溯子批次（携带 `parent_batch_id` 与 `disposition_line_id` 处置依据），只有让步接收与复检合格的数量进入可领料库存
- 复检合格会推翻原判释放库存；复检仍不合格的子批次可再开处置单，形成完整处置链
- `GET /api/v1/dispositions/{id}/trace` 校验不变量：原始数量 = 所有去向之和，且任何已释放库存都有处置依据
- 已绑定处置单的批次禁止整批解除/拒收；并发审批、并发领料通过按单/按物料互斥与原子扣减保证不超分、不超发，任何一步失败整体回滚

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
