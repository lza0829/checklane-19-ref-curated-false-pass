from fastapi import APIRouter, HTTPException

from app.config import DATA_DIR, FIXTURES_DIR
from app.db import connect
from app.modules import cross_dataset_ref_check as ref_check
from app.repositories import datasets as dataset_repo
from app.repositories import rules as rules_repo
from app.schemas.entities import RuleEnablePatch

router = APIRouter(tags=["rules"])


@router.get("/rules")
def list_rules(dataset_id: int | None = None):
    conn = connect()
    try:
        rows = rules_repo.list_rules(conn, dataset_id)
        datasets = {d["id"]: d for d in dataset_repo.list_datasets(conn)}
        out = []
        for r in rows:
            item = dict(r)
            if r.get("rule_type") == ref_check.REF_RULE_TYPE:
                item["reference"] = _describe_reference(conn, datasets, r)
            out.append(item)
        return out
    finally:
        conn.close()


def _describe_reference(conn, datasets, rule) -> dict:
    """规则页展示参照来源：目标数据集、目标字段、夹具、当前目标键集状态。"""
    try:
        target_id, target_field = ref_check.parse_ref_params(rule)
    except ref_check.ReferenceConfigError as exc:
        return {"ready": False, "error": str(exc), "status": "config_error"}
    target = datasets.get(target_id)
    ctx = ref_check.load_context(
        rule=rule,
        target_dataset=target,
        fixtures_dir=FIXTURES_DIR,
        data_dir=DATA_DIR,
    )
    return {
        "ready": ctx.ready,
        "status": ctx.status,
        "error": ctx.error,
        "target_dataset_id": target_id,
        "target_dataset_name": target["name"] if target else None,
        "target_field": target_field,
        "fixture_path": target["fixture_path"] if target else None,
        "target_rows": ctx.target_rows,
        "target_key_count": len(ctx.keys),
        "target_sample_keys": sorted(ctx.keys)[:10],
        "source": "目标数据集登记的夹具键集（只读夹具，不读精选区）",
    }


@router.patch("/rules/{rule_id}")
def patch_rule(rule_id: int, body: RuleEnablePatch):
    conn = connect()
    try:
        existing = conn.execute("SELECT id FROM rules WHERE id=?", (rule_id,)).fetchone()
        if not existing:
            raise HTTPException(404, "rule not found")
        rules_repo.set_enabled(conn, rule_id, body.enabled)
        conn.commit()
        return {"id": rule_id, "enabled": body.enabled}
    finally:
        conn.close()
