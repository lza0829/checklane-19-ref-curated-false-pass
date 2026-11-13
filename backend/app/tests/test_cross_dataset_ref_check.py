import importlib
import json
import shutil
from pathlib import Path

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import app.config
    import app.db
    import app.seed
    import app.services.inspect_service as svc

    importlib.reload(app.config)
    importlib.reload(app.db)
    importlib.reload(app.seed)
    importlib.reload(svc)
    app.seed.init_db()
    return svc


def _dataset_id(env, fixture_path: str) -> int:
    import app.db

    conn = app.db.connect()
    try:
        row = conn.execute(
            "SELECT id FROM datasets WHERE fixture_path=?", (fixture_path,)
        ).fetchone()
        assert row is not None, fixture_path
        return int(row["id"])
    finally:
        conn.close()


def _checks(report):
    return {c["name"]: c for c in report["checks"]}


def test_reference_existing_and_missing_key(env):
    detail = env.run_inspect(1)
    report = detail["report"]

    assert detail["scanned_count"] == 6
    # ORD-3 缺 sku、ORD-4 价格非数字、ORD-6 的 SKU-E 无库存参照 → 隔离 3
    assert detail["quarantined_count"] == 3
    assert detail["passed_count"] == 3
    assert detail["released_count"] == 3

    ref = report["reference"]
    assert ref["enabled"] is True
    assert ref["ready"] is True
    # ORD-3（空 sku，无法参照）+ ORD-6（SKU-E 不在目标键集）
    assert ref["failed_row_count"] == 2
    assert ref["failed_key_count"] == 2
    assert sorted(m["key"] for m in ref["missing_keys"]) == sorted(["(空)", "SKU-E"])

    rule = ref["rules"][0]
    assert rule["target_dataset_name"] == "Inventory Snapshot"
    assert rule["target_field"] == "sku"
    assert rule["fixture_path"] == "inventory_snapshot.jsonl"
    assert rule["target_rows"] == 5
    assert rule["target_key_count"] == 4  # SKU-A/B/C/D，空 sku 被跳过
    assert rule["source_rows"] == 6
    assert rule["matched_key_count"] == 4
    assert set(rule["missing_keys"]) == {"SKU-E", "(空)"}
    assert rule["target_rows"] == rule["target_key_count"] + rule["target_skipped_empty"]

    # 行清单里能定位到失败行与失败键
    ref_rows = [
        r
        for r in detail["rows"]
        if any(h.get("rule_type") == "ref_exists" for h in r["rule_hits"])
    ]
    assert [r["entity_key"] for r in ref_rows] == ["ORD-3", "ORD-6"]
    assert all(r["disposition"] == "quarantine" for r in ref_rows)
    hit = next(
        h for h in ref_rows[1]["rule_hits"] if h["rule_type"] == "ref_exists"
    )
    assert hit["reference"]["key"] == "SKU-E"
    assert hit["reference"]["target_dataset_name"] == "Inventory Snapshot"
    assert hit["reference"]["target_field"] == "sku"

    # 互证 checks 全部为真
    checks = _checks(report)
    for name in (
        "scan_breakdown",
        "release_matches_passed",
        "quarantine_table_matches",
        "reference_ready",
        "reference_failed_rows_match_rowlist",
        "reference_failed_rows_quarantined",
        "reference_failed_rows_match_quarantine_table",
        "reference_failed_keys_match_rows",
    ):
        assert checks[name]["ok"] is True, (name, checks[name]["detail"])
    assert checks["reference_source_target_balance:sku 须存在于库存夹具"]["ok"] is True

    # 隔离区只含失败行，且精选区不含 SKU-E 对应 ORD-6
    import app.db
    import app.repositories.quarantine as quarantine_repo

    conn = app.db.connect()
    try:
        q_rows = quarantine_repo.list_all(conn)
        assert {r["entity_key"] for r in q_rows} == {"ORD-3", "ORD-4", "ORD-6"}
        curated_keys = {
            r["entity_key"]
            for r in conn.execute("SELECT entity_key FROM curated WHERE dataset_id=1").fetchall()
        }
        assert "ORD-6" not in curated_keys
    finally:
        conn.close()


def test_reference_passes_after_target_fixture_gains_key(env, tmp_path):
    # 目标夹具走 DATA_DIR/fixtures 覆盖层，模拟增补键后重新入检
    first = env.run_inspect(1)
    assert first["report"]["reference"]["failed_key_count"] == 2
    assert "SKU-E" in first["report"]["reference"]["rules"][0]["missing_keys"]

    import app.config

    overlay = Path(app.config.DATA_DIR) / "fixtures"
    overlay.mkdir(parents=True, exist_ok=True)
    source = Path(app.config.FIXTURES_DIR) / "inventory_snapshot.jsonl"
    lines = source.read_text(encoding="utf-8").splitlines()
    lines.append(json.dumps({"entity_key": "INV-6", "sku": "SKU-E", "on_hand": 4}, ensure_ascii=False))
    (overlay / "inventory_snapshot.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    second = env.run_inspect(1)
    report = second["report"]
    # SKU-E 已能参照；仅剩 ORD-3 的空 sku 仍不可参照
    assert report["reference"]["failed_key_count"] == 1
    assert report["reference"]["failed_row_count"] == 1
    assert report["reference"]["rules"][0]["missing_keys"] == ["(空)"]
    # 仅剩 ORD-3 缺 sku（空值无法参照）、ORD-4 价格非数字
    assert second["quarantined_count"] == 2
    assert second["released_count"] == 4
    ref_row = next(r for r in second["rows"] if r["entity_key"] == "ORD-6")
    assert ref_row["disposition"] == "pass"
    assert all(h.get("rule_type") != "ref_exists" for h in ref_row["rule_hits"])


@pytest.mark.parametrize(
    "fixture_path, expected_status",
    [
        ("inventory_pending_empty.jsonl", "empty"),
        ("inventory_pending_broken.jsonl", "parse_error"),
    ],
)
def test_reference_target_not_ready_fails_whole_batch(env, fixture_path, expected_status):
    target_id = _dataset_id(env, fixture_path)
    import app.db
    import app.repositories.rules as rules_repo

    conn = app.db.connect()
    try:
        normal_ref = conn.execute(
            "SELECT id FROM rules WHERE dataset_id=1 AND rule_type='ref_exists'"
        ).fetchall()
        for row in normal_ref:
            rules_repo.set_enabled(conn, int(row["id"]), False)  # 停掉指向正常 Inventory 的参照规则
        conn.execute(
            "INSERT INTO rules(dataset_id, name, field, rule_type, severity, params_json, enabled) "
            "VALUES (1,?,?,?,?,?,1)",
            (
                "sku 须存在于待就绪库存夹具",
                "sku",
                "ref_exists",
                "error",
                json.dumps({"target_dataset_id": target_id, "target_field": "sku"}),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(env.ReferenceNotReadyError) as excinfo:
        env.run_inspect(1)

    exc = excinfo.value
    assert exc.batch_id > 0
    report = exc.report
    assert report["counts"]["scanned"] == 0
    assert report["counts"]["quarantined"] == 0
    assert report["counts"]["released"] == 0
    assert report["reference"]["ready"] is False
    assert report["reference"]["rules"][0]["status"] == expected_status
    assert _checks(report)["reference_ready"]["ok"] is False

    # 不得签发、不得写隔离、不得写行清单，批次标记 failed
    conn = app.db.connect()
    try:
        batch = conn.execute(
            "SELECT status FROM inspect_batches WHERE id=?", (exc.batch_id,)
        ).fetchone()
        assert batch["status"] == "failed"
        assert conn.execute("SELECT COUNT(*) AS c FROM inspect_rows").fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) AS c FROM quarantine").fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) AS c FROM curated").fetchone()["c"] == 0
    finally:
        conn.close()


def test_reference_target_missing_dataset_fails_whole_batch(env):
    import app.db

    conn = app.db.connect()
    try:
        conn.execute(
            "INSERT INTO rules(dataset_id, name, field, rule_type, severity, params_json, enabled) "
            "VALUES (1,?,?,?,?,?,1)",
            (
                "sku 须存在于不存在的数据集",
                "sku",
                "ref_exists",
                "error",
                json.dumps({"target_dataset_id": 999, "target_field": "sku"}),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(env.ReferenceNotReadyError) as excinfo:
        env.run_inspect(1)
    statuses = [r["status"] for r in excinfo.value.report["reference"]["rules"]]
    assert "target_missing" in statuses


def test_curated_key_does_not_satisfy_missing_fixture_key(env):
    """精选区同键不算参照：夹具缺键的行必须隔离；夹具补键复检后才放行。"""
    import app.config
    import app.db
    import app.repositories.curated as curated_repo

    overlay = Path(app.config.DATA_DIR) / "fixtures"
    overlay.mkdir(parents=True, exist_ok=True)
    builtin = Path(app.config.FIXTURES_DIR) / "inventory_snapshot.jsonl"

    # 覆盖层夹具删掉 SKU-D，模拟目标夹具缺键（精选区却已有同键）
    trimmed = [
        line
        for line in builtin.read_text(encoding="utf-8").splitlines()
        if '"sku":"SKU-D"' not in line
    ]
    (overlay / "inventory_snapshot.jsonl").write_text(
        "\n".join(trimmed) + "\n", encoding="utf-8"
    )

    conn = app.db.connect()
    try:
        curated_repo.upsert(
            conn,
            dataset_id=2,
            batch_id=0,
            entity_key="SKU-D",
            payload={"entity_key": "SKU-D", "sku": "SKU-D", "on_hand": 7},
            released_at="2026-11-13T00:00:00Z",
        )
        conn.commit()
    finally:
        conn.close()

    detail = env.run_inspect(1)
    ref = detail["report"]["reference"]
    rule = ref["rules"][0]

    # 目标键只来自夹具（SKU-A/B/C），精选区的 SKU-D 一律不算数
    assert rule["status"] == "ready"
    assert rule["target_key_count"] == 3
    assert set(rule["missing_keys"]) == {"(空)", "SKU-D", "SKU-E"}

    # ORD-5 的 SKU-D 夹具缺键 → 必须隔离不得签发，即使精选区已有 SKU-D
    ref_rows = [
        r
        for r in detail["rows"]
        if any(h.get("rule_type") == "ref_exists" for h in r["rule_hits"])
    ]
    assert {r["entity_key"] for r in ref_rows} == {"ORD-3", "ORD-5", "ORD-6"}
    assert all(r["disposition"] == "quarantine" for r in ref_rows)
    assert detail["quarantined_count"] == 4
    assert detail["released_count"] == 2

    conn = app.db.connect()
    try:
        assert "ORD-5" not in {
            r["entity_key"] for r in curated_repo.list_all(conn, 1)
        }
    finally:
        conn.close()

    # 互证 checks 全部为真，参照失败数/隔离区/目标夹具键数对得上
    checks = _checks(detail["report"])
    for name in (
        "reference_ready",
        "reference_failed_rows_match_rowlist",
        "reference_failed_rows_quarantined",
        "reference_failed_rows_match_quarantine_table",
        "reference_failed_rows_not_released",
        "reference_failed_keys_match_rows",
    ):
        assert checks[name]["ok"] is True, (name, checks[name]["detail"])
    assert checks["reference_source_target_balance:sku 须存在于库存夹具"]["ok"] is True

    # 修好夹具（补回 SKU-D）后复检：ORD-5 转为通过，SKU-D 不再出现在缺失键里
    full_lines = builtin.read_text(encoding="utf-8").splitlines()
    (overlay / "inventory_snapshot.jsonl").write_text(
        "\n".join(full_lines) + "\n", encoding="utf-8"
    )
    second = env.run_inspect(1)
    rule2 = second["report"]["reference"]["rules"][0]
    assert rule2["target_key_count"] == 4
    assert "SKU-D" not in rule2["missing_keys"]
    ord5 = next(r for r in second["rows"] if r["entity_key"] == "ORD-5")
    assert ord5["disposition"] == "pass"
    assert all(h.get("rule_type") != "ref_exists" for h in ord5["rule_hits"])
    assert second["quarantined_count"] == 3
    assert second["released_count"] == 3


@pytest.mark.parametrize(
    "fixture_path, expected_status",
    [
        ("inventory_pending_empty.jsonl", "empty"),
        ("inventory_pending_broken.jsonl", "parse_error"),
        ("inventory_missing_file.jsonl", "missing"),
    ],
)
def test_target_fixture_unavailable_fails_even_with_curated_keys(
    env, fixture_path, expected_status
):
    """目标夹具空/解析失败/缺失时，精选区有键也不得假通过：整批参照失败闭。"""
    import app.db
    import app.repositories.curated as curated_repo
    import app.repositories.rules as rules_repo

    conn = app.db.connect()
    try:
        existing = conn.execute(
            "SELECT id FROM datasets WHERE fixture_path=?", (fixture_path,)
        ).fetchone()
        if existing:
            target_id = int(existing["id"])
        else:
            cur = conn.execute(
                "INSERT INTO datasets(name, fixture_path, entity_type, enabled, note) "
                "VALUES (?,?,?,?,?)",
                ("Inventory Pending (缺夹具)", fixture_path, "sku", 1, "夹具文件缺失"),
            )
            target_id = int(cur.lastrowid)

        # 目标精选区预置同键行——旧实现会借此把 not-ready 拉成 ready
        curated_repo.upsert(
            conn,
            dataset_id=target_id,
            batch_id=0,
            entity_key="SKU-A",
            payload={"entity_key": "SKU-A", "sku": "SKU-A", "on_hand": 1},
            released_at="2026-11-13T00:00:00Z",
        )

        for row in conn.execute(
            "SELECT id FROM rules WHERE dataset_id=1 AND rule_type='ref_exists'"
        ).fetchall():
            rules_repo.set_enabled(conn, int(row["id"]), False)
        conn.execute(
            "INSERT INTO rules(dataset_id, name, field, rule_type, severity, params_json, enabled) "
            "VALUES (1,?,?,?,?,?,1)",
            (
                "sku 须存在于待就绪库存夹具",
                "sku",
                "ref_exists",
                "error",
                json.dumps({"target_dataset_id": target_id, "target_field": "sku"}),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(env.ReferenceNotReadyError) as excinfo:
        env.run_inspect(1)

    report = excinfo.value.report
    assert report["reference"]["ready"] is False
    rule = report["reference"]["rules"][0]
    assert rule["status"] == expected_status
    # 精选键未被并入，目标键口径仍为 0
    assert rule["target_key_count"] == 0
    assert report["counts"] == {
        "scanned": 0,
        "passed": 0,
        "failed": 0,
        "quarantined": 0,
        "released": 0,
    }
    assert _checks(report)["reference_ready"]["ok"] is False

    conn = app.db.connect()
    try:
        batch = conn.execute(
            "SELECT status FROM inspect_batches WHERE id=?", (excinfo.value.batch_id,)
        ).fetchone()
        assert batch["status"] == "failed"
        assert conn.execute("SELECT COUNT(*) AS c FROM inspect_rows").fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) AS c FROM quarantine").fetchone()["c"] == 0
        # 源数据集零签发；目标侧仅保留预置的精选行
        assert curated_repo.count_all(conn, 1) == 0
        assert curated_repo.count_all(conn, target_id) == 1
    finally:
        conn.close()
