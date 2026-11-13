import json
from datetime import datetime, timezone

from app.config import DATA_DIR
from app.db import connect


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = connect()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS settings(
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS datasets(
          id INTEGER PRIMARY KEY,
          name TEXT NOT NULL,
          fixture_path TEXT NOT NULL,
          entity_type TEXT NOT NULL,
          enabled INTEGER NOT NULL DEFAULT 1,
          note TEXT
        );
        CREATE TABLE IF NOT EXISTS rules(
          id INTEGER PRIMARY KEY,
          dataset_id INTEGER NOT NULL,
          name TEXT NOT NULL,
          field TEXT NOT NULL,
          rule_type TEXT NOT NULL,
          severity TEXT NOT NULL,
          params_json TEXT NOT NULL DEFAULT '{}',
          enabled INTEGER NOT NULL DEFAULT 1,
          FOREIGN KEY(dataset_id) REFERENCES datasets(id)
        );
        CREATE TABLE IF NOT EXISTS inspect_batches(
          id INTEGER PRIMARY KEY,
          dataset_id INTEGER NOT NULL,
          status TEXT NOT NULL,
          scanned_count INTEGER NOT NULL DEFAULT 0,
          passed_count INTEGER NOT NULL DEFAULT 0,
          failed_count INTEGER NOT NULL DEFAULT 0,
          quarantined_count INTEGER NOT NULL DEFAULT 0,
          released_count INTEGER NOT NULL DEFAULT 0,
          report_json TEXT NOT NULL,
          created_at TEXT NOT NULL,
          FOREIGN KEY(dataset_id) REFERENCES datasets(id)
        );
        CREATE TABLE IF NOT EXISTS inspect_rows(
          id INTEGER PRIMARY KEY,
          batch_id INTEGER NOT NULL,
          row_no INTEGER NOT NULL,
          entity_key TEXT,
          payload_json TEXT NOT NULL,
          disposition TEXT NOT NULL,
          rule_hits_json TEXT NOT NULL,
          FOREIGN KEY(batch_id) REFERENCES inspect_batches(id)
        );
        CREATE TABLE IF NOT EXISTS quarantine(
          id INTEGER PRIMARY KEY,
          batch_id INTEGER NOT NULL,
          dataset_id INTEGER NOT NULL,
          row_no INTEGER NOT NULL,
          entity_key TEXT,
          reason TEXT NOT NULL,
          payload_json TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS curated(
          id INTEGER PRIMARY KEY,
          dataset_id INTEGER NOT NULL,
          batch_id INTEGER NOT NULL,
          entity_key TEXT NOT NULL,
          payload_json TEXT NOT NULL,
          released_at TEXT NOT NULL,
          UNIQUE(dataset_id, entity_key)
        );
        """
    )
    n = conn.execute("SELECT COUNT(*) AS c FROM datasets").fetchone()["c"]
    if n == 0:
        conn.execute(
            "INSERT INTO datasets(name, fixture_path, entity_type, enabled, note) VALUES (?,?,?,?,?)",
            ("Orders Snapshot", "orders_snapshot.jsonl", "order", 1, "含缺失与类型错误行，演示入检"),
        )
        conn.execute(
            "INSERT INTO datasets(name, fixture_path, entity_type, enabled, note) VALUES (?,?,?,?,?)",
            ("Inventory Snapshot", "inventory_snapshot.jsonl", "sku", 1, "库存快照第二路"),
        )
        conn.execute(
            "INSERT INTO datasets(name, fixture_path, entity_type, enabled, note) VALUES (?,?,?,?,?)",
            (
                "Inventory Pending (空夹具)",
                "inventory_pending_empty.jsonl",
                "sku",
                1,
                "演示用目标数据集：夹具为空，参照检查整批失败、不得签发",
            ),
        )
        conn.execute(
            "INSERT INTO datasets(name, fixture_path, entity_type, enabled, note) VALUES (?,?,?,?,?)",
            (
                "Inventory Pending (坏夹具)",
                "inventory_pending_broken.jsonl",
                "sku",
                1,
                "演示用目标数据集：夹具解析失败，参照检查整批失败、不得签发",
            ),
        )
        conn.executemany(
            "INSERT INTO rules(dataset_id, name, field, rule_type, severity, params_json, enabled) VALUES (?,?,?,?,?,?,?)",
            [
                (1, "sku 必填", "sku", "required", "error", "{}", 1),
                (1, "price 须为数字", "price", "type_number", "error", "{}", 1),
                (1, "qty 须为整数", "qty", "type_int", "warn", "{}", 1),
                (2, "sku 必填", "sku", "required", "error", "{}", 1),
                (2, "on_hand 须为整数", "on_hand", "type_int", "error", "{}", 1),
                # 跨数据集参照：Orders.sku 必须存在于 Inventory（目标夹具键集）的 sku 中
                (
                    1,
                    "sku 须存在于库存夹具",
                    "sku",
                    "ref_exists",
                    "error",
                    json.dumps(
                        {"target_dataset_id": 2, "target_field": "sku"},
                        ensure_ascii=False,
                    ),
                    1,
                ),
            ],
        )
        conn.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?)",
            ("release_mode", "pass_only"),
        )
    else:
        _ensure_reference_seed(conn)
    conn.commit()
    conn.close()


def _ensure_reference_seed(conn) -> None:
    """已有库时补写参照检查所需的数据集与规则（幂等）。"""
    pending_datasets = [
        (
            "Inventory Pending (空夹具)",
            "inventory_pending_empty.jsonl",
            "sku",
            "演示用目标数据集：夹具为空，参照检查整批失败、不得签发",
        ),
        (
            "Inventory Pending (坏夹具)",
            "inventory_pending_broken.jsonl",
            "sku",
            "演示用目标数据集：夹具解析失败，参照检查整批失败、不得签发",
        ),
    ]
    for name, fixture_path, entity_type, note in pending_datasets:
        exists = conn.execute(
            "SELECT id FROM datasets WHERE fixture_path=?", (fixture_path,)
        ).fetchone()
        if not exists:
            conn.execute(
                "INSERT INTO datasets(name, fixture_path, entity_type, enabled, note) VALUES (?,?,?,?,?)",
                (name, fixture_path, entity_type, 1, note),
            )

    inventory = conn.execute(
        "SELECT id FROM datasets WHERE fixture_path=?", ("inventory_snapshot.jsonl",)
    ).fetchone()
    orders = conn.execute(
        "SELECT id FROM datasets WHERE fixture_path=?", ("orders_snapshot.jsonl",)
    ).fetchone()
    if not inventory or not orders:
        return
    existing_rule = conn.execute(
        "SELECT id FROM rules WHERE dataset_id=? AND rule_type='ref_exists' AND field='sku'",
        (orders["id"],),
    ).fetchone()
    if not existing_rule:
        conn.execute(
            "INSERT INTO rules(dataset_id, name, field, rule_type, severity, params_json, enabled) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                orders["id"],
                "sku 须存在于库存夹具",
                "sku",
                "ref_exists",
                "error",
                json.dumps({"target_dataset_id": inventory["id"], "target_field": "sku"}),
                1,
            ),
        )

