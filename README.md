# 04-checklane（检道）

Checklane — 数据质检通道：入检批次装载 → 规则命中与严重度裁决 → 隔离/放行签发 → 签发清单与源批对账。

**不是** Feed 多源归并（Feedstitch），**不是** 作业依赖编排（Batchgate），**不是** CDC 变更流按序落库（Changeflow）。
**也不是** 报表看板 / CRUD 后台 / 电商订单 / OA 工单。

## 启动

`ash
docker compose up --build
`

| 入口 | 地址 |
| --- | --- |
| 前端 | http://localhost:5400 |
| API | http://localhost:10400 |

本地开发（可选）：

`ash
# backend
cd backend && pip install -r requirements.txt
set DATA_DIR=..\\data
uvicorn app.main:app --reload --port 10400

# frontend
cd frontend && npm install && npm run dev
`

## 主链

登记数据集与规则 → 装载夹具批次入检 → 按规则裁决放行/隔离 → 签发到精选区 → 批次报告与各列表数字同源

## 种子说明

- **Orders Snapshot**：JSONL 商品快照，含必填缺失与类型错误行，用于演示入检与隔离。
- **Inventory Snapshot**：库存快照，独立数据集，作为 Orders 的跨数据集参照目标。
- **Inventory Pending (空夹具 / 坏夹具)**：演示目标未就绪时整批参照检查失败、不得假通过。

打开「入检台」选数据集跑一次，再看隔离区 / 精选区 / 批次报告计数是否与本次报告一致。

## 跨数据集参照检查

模块：`backend/app/modules/cross_dataset_ref_check/`。

规则类型 `ref_exists`，参数（`rules.params_json`）：

```json
{"target_dataset_id": 2, "target_field": "sku"}
```

语义与约束：

1. **参照来源固定为目标数据集登记的夹具键集**（`fixture_path` → `target_field` 去空后的键集合），
   每次入检实时解析；**不读取精选区**，未签发数据不得充当参照。
2. 源行字段值不在目标键集中（含源字段为空）→ 该行按 error 隔离，**不得签发**。
   命中详情与 `reference` 一并写入 `inspect_rows.rule_hits_json`，可定位失败键。
3. 目标夹具**缺失 / 0 行 / 解析失败 / 键集为空 / 目标数据集不存在或停用** → 整批参照检查失败：
   写入 `status=failed` 的空批次（scanned=passed=quarantined=released=0），不写行清单、
   不写隔离、不签发；API 返回 **409** 并附失败批次报告。
4. 入检报告 `report.reference` + `report.checks` 互相印证：
   - 源侧行数 = 扫描行数；源键 = 命中 + 缺失；目标行 = 目标键 + 空键
   - 缺参照行数 = 行清单缺参照行数 = 参照报告失败行数
   - 缺参照行全部隔离、且与签发行无交集
   - 失败键集合与行清单里记录的失败键一致
5. 目标夹具增补键后**再次入检**即可，先前失败键转为通过（键集每次入检刷新，不缓存）。
   本地要覆盖内置夹具时，把同名文件放到 `DATA_DIR/fixtures/`，解析优先取该覆盖层。

演示（`Orders Snapshot` → `Inventory Snapshot`）：

- `ORD-1` 的 `SKU-A` 在库存键集中 → 通过；`ORD-6` 的 `SKU-E` 不存在 → 隔离。
- 把 Inventory 数据集的夹具改成 `inventory_pending_empty.jsonl`（空）或
  `inventory_pending_broken.jsonl`（坏 JSONL）再入检 → 409，整批失败、零签发。

## 技术栈

Python 3.12 + FastAPI + SQLite；Vue 3 + Vite + Nginx。

## 0-1 切口（未实现，见题单）

当前仓库已实现「单批入检 + 基础规则 + 放行签发」主链，并已实现**跨数据集参照检查**。
其余切口（严重度整批阻断、规则包版本、抽样入检、跨字段规则、批次重开、金标对照、批内重复键、放行冻结窗、规则临时豁免）仍在 `backend/app/modules/*` 留空包，按题单逐题换主链路实现。
