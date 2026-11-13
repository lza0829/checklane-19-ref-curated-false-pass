from __future__ import annotations

import json
from typing import Any

from app.config import DATA_DIR, FIXTURES_DIR
from app.db import connect
from app.engines.parse_fixture import parse_jsonl
from app.engines.rule_engine import decide_disposition, evaluate_row
from app.modules import cross_dataset_ref_check as ref_check
from app.repositories import batches as batch_repo
from app.repositories import curated as curated_repo
from app.repositories import datasets as dataset_repo
from app.repositories import quarantine as quarantine_repo
from app.repositories import rules as rules_repo
from app.seed import _now


class ReferenceNotReadyError(ValueError):
    """目标夹具不可用：整批参照检查失败，批次已落库但零隔离零签发。"""

    def __init__(self, message: str, batch_id: int, report: dict[str, Any]) -> None:
        super().__init__(message)
        self.batch_id = batch_id
        self.report = report


def is_reference_rule(rule: dict[str, Any]) -> bool:
    return rule.get("rule_type") == ref_check.REF_RULE_TYPE


def _load_reference_contexts(conn, rules: list[dict[str, Any]]) -> list[ref_check.ReferenceContext]:
    ref_rules = [rule for rule in rules if is_reference_rule(rule)]
    target_cache: dict[int | None, dict[str, Any] | None] = {}
    for rule in ref_rules:
        try:
            target_id, _ = ref_check.parse_ref_params(rule)
        except ref_check.ReferenceConfigError:
            target_id = None
        if target_id not in target_cache:
            target_cache[target_id] = (
                dataset_repo.get_dataset(conn, target_id) if target_id is not None else None
            )
    # 参照键集只来自目标数据集登记的夹具：精选区（curated）一律不得并入，
    # 未签发/已签发数据都不能充当参照；夹具未就绪时也不得借精选区拉成 ready。
    return ref_check.load_contexts(
        ref_rules,
        target_datasets=target_cache,
        fixtures_dir=FIXTURES_DIR,
        data_dir=DATA_DIR,
    )


def _reference_not_ready_report(
    *,
    ds: dict[str, Any],
    rules: list[dict[str, Any]],
    contexts: list[ref_check.ReferenceContext],
) -> dict[str, Any]:
    reference = ref_check.reference_report(contexts)
    reasons = "; ".join(
        f"{ctx.rule_name or ctx.rule_id}: {ctx.error or ctx.status}"
        for ctx in contexts
        if not ctx.ready
    )
    return {
        "dataset_id": ds["id"],
        "dataset_name": ds["name"],
        "fixture_path": ds["fixture_path"],
        "rule_count": len(rules),
        "counts": {
            "scanned": 0,
            "passed": 0,
            "failed": 0,
            "quarantined": 0,
            "released": 0,
        },
        "reference": reference,
        "checks": [
            {
                "name": "reference_ready",
                "ok": False,
                "detail": reasons or "目标夹具未就绪",
            }
        ],
        "error": reasons or "目标夹具未就绪，整批参照检查失败",
    }


def _persist_failed_batch(
    conn,
    *,
    dataset_id: int,
    report: dict[str, Any],
    now: str,
) -> int:
    batch_id = batch_repo.insert_batch(
        conn,
        dataset_id=dataset_id,
        status="failed",
        scanned=0,
        passed=0,
        failed=0,
        quarantined=0,
        released=0,
        report=report,
        created_at=now,
    )
    conn.commit()
    return batch_id


def _build_reference_checks(
    *,
    contexts: list[ref_check.ReferenceContext],
    row_results: list[dict[str, Any]],
    scanned: int,
    quarantined: int,
    quarantine_persisted: int,
    quarantined_row_nos: list[int],
    released_row_nos: list[int],
    reference_summary: dict[str, Any],
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    if not contexts:
        return checks

    ready = all(ctx.ready for ctx in contexts)
    checks.append(
        {
            "name": "reference_ready",
            "ok": ready,
            "detail": "；".join(
                f"{ctx.rule_name}→{ctx.target_dataset_name}.{ctx.target_field}"
                f"[{ctx.status}] 源行{ctx.source_rows}/目标行{ctx.target_rows}/目标键{len(ctx.keys)}"
                for ctx in contexts
            ),
        }
    )

    ref_failed_rows = [
        item
        for item in row_results
        if any(h.get("rule_type") == ref_check.REF_RULE_TYPE for h in item["rule_hits"])
    ]
    ref_failed_row_nos = {item["row_no"] for item in ref_failed_rows}
    checks.append(
        {
            "name": "reference_failed_rows_match_rowlist",
            "ok": len(ref_failed_row_nos) == reference_summary["failed_row_count"],
            "detail": (
                f"行清单缺参照行({len(ref_failed_row_nos)}) "
                f"= 参照报告失败行({reference_summary['failed_row_count']})"
            ),
        }
    )
    checks.append(
        {
            "name": "reference_failed_rows_quarantined",
            "ok": all(item["disposition"] == "quarantine" for item in ref_failed_rows)
            and len(ref_failed_row_nos) <= quarantined,
            "detail": (
                f"缺参照行全部隔离；隔离行({quarantined}) ≥ 缺参照行({len(ref_failed_row_nos)})"
            ),
        }
    )
    quarantined_set = set(quarantined_row_nos)
    checks.append(
        {
            "name": "reference_failed_rows_match_quarantine_table",
            "ok": quarantine_persisted == quarantined == len(quarantined_set)
            and ref_failed_row_nos <= quarantined_set,
            "detail": (
                f"本批隔离区行({quarantine_persisted}) = 隔离计数({quarantined})；"
                f"缺参照行({len(ref_failed_row_nos)}) 全部落在隔离区"
            ),
        }
    )
    checks.append(
        {
            "name": "reference_failed_rows_not_released",
            "ok": not (ref_failed_row_nos & set(released_row_nos)),
            "detail": (
                f"缺参照行({len(ref_failed_row_nos)}) 与签发行({len(released_row_nos)}) 无交集，未签发"
            ),
        }
    )

    missing_key_set = {entry["key"] for entry in reference_summary["missing_keys"]}
    row_key_set = {
        str((hit.get("reference") or {}).get("key"))
        for item in ref_failed_rows
        for hit in item["rule_hits"]
        if hit.get("rule_type") == ref_check.REF_RULE_TYPE
    }
    checks.append(
        {
            "name": "reference_failed_keys_match_rows",
            "ok": missing_key_set == row_key_set,
            "detail": (
                f"失败键({len(missing_key_set)}) 与行清单失败键({len(row_key_set)}) 一致；"
                f"失败行({len(ref_failed_row_nos)})"
            ),
        }
    )

    for ctx in contexts:
        checks.append(
            {
                "name": f"reference_source_target_balance:{ctx.rule_name}",
                "ok": ctx.source_rows == scanned
                and len(ctx.source_values) == len(ctx.matched) + len(ctx.missing_keys)
                and ctx.target_rows == len(ctx.keys) + ctx.target_skipped_empty,
                "detail": (
                    f"源侧行({ctx.source_rows}) = 扫描({scanned})；"
                    f"源键({len(ctx.source_values)}) = 命中({len(ctx.matched)}) + "
                    f"缺失({len(ctx.missing_keys)})；"
                    f"目标行({ctx.target_rows}) = 目标键({len(ctx.keys)}) + "
                    f"空键({ctx.target_skipped_empty})"
                ),
            }
        )
    return checks


def run_inspect(dataset_id: int) -> dict[str, Any]:
    conn = connect()
    try:
        ds = dataset_repo.get_dataset(conn, dataset_id)
        if not ds:
            raise ValueError(f"dataset {dataset_id} not found")
        if not ds["enabled"]:
            raise ValueError(f"dataset {dataset_id} disabled")

        rules = [r for r in rules_repo.list_rules(conn, dataset_id) if r.get("enabled")]
        row_rules = [r for r in rules if not is_reference_rule(r)]
        contexts = _load_reference_contexts(conn, rules)
        now = _now()

        # 目标夹具未就绪：整批中止，落失败批次，零隔离零签发
        if contexts and not all(ctx.ready for ctx in contexts):
            report = _reference_not_ready_report(ds=ds, rules=rules, contexts=contexts)
            batch_id = _persist_failed_batch(conn, dataset_id=dataset_id, report=report, now=now)
            raise ReferenceNotReadyError(report["error"], batch_id, report)

        path = ref_check.resolve_fixture_path(
            str(ds["fixture_path"]), fixtures_dir=FIXTURES_DIR, data_dir=DATA_DIR
        )
        rows = parse_jsonl(path)

        scanned = len(rows)
        passed = failed = quarantined = released = 0
        row_results: list[dict[str, Any]] = []

        for idx, payload in enumerate(rows, start=1):
            entity_key = str(payload.get("entity_key") or "") or None
            hits = evaluate_row(payload, row_rules)
            if contexts:
                hits.extend(
                    ref_check.evaluate_row(
                        payload,
                        contexts,
                        row_no=idx,
                        entity_key=entity_key,
                    )
                )
            disposition = decide_disposition(hits)
            if disposition == "quarantine":
                quarantined += 1
                failed += 1
            else:
                passed += 1
            row_results.append(
                {
                    "row_no": idx,
                    "entity_key": entity_key,
                    "payload": payload,
                    "disposition": disposition,
                    "rule_hits": hits,
                }
            )

        reference_summary = ref_check.reference_report(contexts)

        report = {
            "dataset_id": dataset_id,
            "dataset_name": ds["name"],
            "fixture_path": ds["fixture_path"],
            "rule_count": len(rules),
            "counts": {
                "scanned": scanned,
                "passed": passed,
                "failed": failed,
                "quarantined": quarantined,
                "released": 0,
            },
            "reference": reference_summary,
            "checks": [],
        }
        report["checks"].append(
            {
                "name": "scan_breakdown",
                "ok": scanned == passed + quarantined,
                "detail": f"scanned({scanned}) = passed({passed}) + quarantined({quarantined})",
            }
        )
        report["checks"].append(
            {
                "name": "release_matches_passed",
                "ok": passed >= 0,
                "detail": f"released 将在放行后核对 passed({passed})",
            }
        )

        batch_id = batch_repo.insert_batch(
            conn,
            dataset_id=dataset_id,
            status="inspected",
            scanned=scanned,
            passed=passed,
            failed=failed,
            quarantined=quarantined,
            released=0,
            report=report,
            created_at=now,
        )

        for item in row_results:
            batch_repo.insert_row(
                conn,
                batch_id=batch_id,
                row_no=item["row_no"],
                entity_key=item["entity_key"],
                payload=item["payload"],
                disposition=item["disposition"],
                rule_hits=item["rule_hits"],
            )
            if item["disposition"] == "quarantine":
                reasons = "; ".join(
                    f"{h['name']}:{h['detail']}" for h in item["rule_hits"] if h.get("severity") == "error"
                )
                quarantine_repo.insert(
                    conn,
                    batch_id=batch_id,
                    dataset_id=dataset_id,
                    row_no=item["row_no"],
                    entity_key=item["entity_key"],
                    reason=reasons or "rule hit",
                    payload=item["payload"],
                    created_at=now,
                )
            else:
                # pass rows auto-release to curated (base chain)
                key = item["entity_key"] or f"row-{item['row_no']}"
                curated_repo.upsert(
                    conn,
                    dataset_id=dataset_id,
                    batch_id=batch_id,
                    entity_key=key,
                    payload=item["payload"],
                    released_at=now,
                )
                released += 1

        report["counts"]["released"] = released
        report["checks"][1] = {
            "name": "release_matches_passed",
            "ok": released == passed,
            "detail": f"released({released}) = passed({passed})",
        }
        quarantine_in_batch = quarantine_repo.count_by_batch(conn, batch_id)
        quarantined_row_nos = quarantine_repo.list_row_nos_by_batch(conn, batch_id)
        released_row_nos = [
            item["row_no"] for item in row_results if item["disposition"] != "quarantine"
        ]
        report["checks"].append(
            {
                "name": "quarantine_table_matches",
                "ok": quarantine_in_batch == quarantined,
                "detail": f"本批隔离落库({quarantine_in_batch}) = 隔离计数({quarantined})",
            }
        )
        report["checks"].extend(
            _build_reference_checks(
                contexts=contexts,
                row_results=row_results,
                scanned=scanned,
                quarantined=quarantined,
                quarantine_persisted=quarantine_in_batch,
                quarantined_row_nos=quarantined_row_nos,
                released_row_nos=released_row_nos,
                reference_summary=reference_summary,
            )
        )
        report["counts"]["reference_failed_rows"] = reference_summary["failed_row_count"]
        report["counts"]["reference_failed_keys"] = reference_summary["failed_key_count"]

        conn.execute(
            """
            UPDATE inspect_batches
            SET released_count=?, report_json=?, status=?
            WHERE id=?
            """,
            (released, json.dumps(report, ensure_ascii=False), "released" if released else "inspected", batch_id),
        )
        conn.commit()

        detail = batch_repo.get_batch(conn, batch_id)
        assert detail is not None
        detail["rows"] = batch_repo.list_batch_rows(conn, batch_id)
        detail["curated_total"] = curated_repo.count_all(conn, dataset_id)
        detail["quarantine_total"] = quarantine_repo.count_all(conn)
        return detail
    finally:
        conn.close()


def dashboard_stats() -> dict[str, Any]:
    conn = connect()
    try:
        datasets = conn.execute("SELECT COUNT(*) AS c FROM datasets").fetchone()["c"]
        rules = conn.execute("SELECT COUNT(*) AS c FROM rules WHERE enabled=1").fetchone()["c"]
        batches = conn.execute("SELECT COUNT(*) AS c FROM inspect_batches").fetchone()["c"]
        return {
            "dataset_count": datasets,
            "enabled_rule_count": rules,
            "batch_count": batches,
            "curated_count": curated_repo.count_all(conn),
            "quarantine_count": quarantine_repo.count_all(conn),
        }
    finally:
        conn.close()
