"""跨数据集参照检查。

规则语义：源数据集的某个字段值，必须存在于目标数据集「夹具键集」中。
目标键集每次入检时从目标数据集登记的夹具文件实时解析，不读取精选区
（未签发精选区不得被当作参照来源）。

目标夹具缺失 / 为空 / 解析失败 / 键集为空时，本模块返回 not ready，
调用方必须整批中止，不能把「没有参照可查」当成「全部通过」。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.engines.parse_fixture import parse_jsonl

REF_RULE_TYPE = "ref_exists"
EMPTY_KEY_DISPLAY = "(空)"

STATUS_READY = "ready"
STATUS_MISSING = "missing"
STATUS_EMPTY = "empty"
STATUS_PARSE_ERROR = "parse_error"
STATUS_CONFIG_ERROR = "config_error"
STATUS_TARGET_MISSING = "target_missing"
STATUS_TARGET_DISABLED = "target_disabled"


class ReferenceConfigError(ValueError):
    """参照规则自身配置不完整。"""


def normalize_key(value: Any) -> str:
    """统一参照键口径：None → 空串，其余转字符串并去首尾空白。"""
    if value is None:
        return ""
    return str(value).strip()


def display_key(value: str) -> str:
    return EMPTY_KEY_DISPLAY if value == "" else value


def _rule_params(rule: dict[str, Any]) -> dict[str, Any]:
    params = rule.get("params")
    if isinstance(params, dict):
        return params
    raw = rule.get("params_json")
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ReferenceConfigError(f"规则「{rule.get('name')}」参数不是合法 JSON: {exc}") from exc
        if isinstance(parsed, dict):
            return parsed
    return {}


def parse_ref_params(rule: dict[str, Any]) -> tuple[int, str]:
    params = _rule_params(rule)
    raw_target = params.get("target_dataset_id")
    try:
        target_dataset_id = int(raw_target)
    except (TypeError, ValueError) as exc:
        raise ReferenceConfigError(
            f"规则「{rule.get('name')}」缺少合法 target_dataset_id"
        ) from exc
    target_field = str(params.get("target_field") or "").strip()
    if not target_field:
        raise ReferenceConfigError(f"规则「{rule.get('name')}」缺少 target_field")
    return target_dataset_id, target_field


def resolve_fixture_path(
    fixture_path: str,
    *,
    fixtures_dir: Path,
    data_dir: Path | None = None,
) -> Path:
    """夹具解析：绝对路径原样；相对路径优先 DATA_DIR/fixtures（可增补），否则内置夹具目录。"""
    path = Path(fixture_path)
    if path.is_absolute():
        return path
    if data_dir is not None:
        candidate = Path(data_dir) / "fixtures" / path
        if candidate.exists():
            return candidate
    return Path(fixtures_dir) / path


@dataclass
class ReferenceContext:
    rule_id: int | None
    rule_name: str
    source_field: str
    target_dataset_id: int | None
    target_dataset_name: str
    target_field: str
    fixture_path: str
    status: str = STATUS_READY
    error: str | None = None
    target_rows: int = 0
    target_skipped_empty: int = 0
    keys: set[str] = field(default_factory=set)
    source_rows: int = 0
    source_values: set[str] = field(default_factory=set)
    matched: set[str] = field(default_factory=set)
    failure_records: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return self.status == STATUS_READY

    @property
    def missing_keys(self) -> set[str]:
        return self.source_values - self.keys

    def summary(self) -> dict[str, Any]:
        missing = self.missing_keys
        return {
            "rule_id": self.rule_id,
            "rule_name": self.rule_name,
            "source_field": self.source_field,
            "target_dataset_id": self.target_dataset_id,
            "target_dataset_name": self.target_dataset_name,
            "target_field": self.target_field,
            "fixture_path": self.fixture_path,
            "status": self.status,
            "error": self.error,
            "target_rows": self.target_rows,
            "target_key_count": len(self.keys),
            "target_skipped_empty": self.target_skipped_empty,
            "source_rows": self.source_rows,
            "source_key_count": len(self.source_values),
            "matched_key_count": len(self.matched),
            "missing_key_count": len(missing),
            "missing_keys": sorted(display_key(v) for v in missing),
            "failed_row_count": len({rec["row_no"] for rec in self.failure_records}),
            "failure_records": list(self.failure_records),
            "source_target_balanced": len(self.source_values)
            == len(self.matched) + len(missing),
        }


def _error_context(
    rule: dict[str, Any],
    *,
    status: str,
    error: str,
    target_dataset_id: int | None = None,
    target_dataset_name: str = "",
    target_field: str = "",
    fixture_path: str = "",
) -> ReferenceContext:
    return ReferenceContext(
        rule_id=rule.get("id"),
        rule_name=str(rule.get("name") or ""),
        source_field=str(rule.get("field") or ""),
        target_dataset_id=target_dataset_id,
        target_dataset_name=target_dataset_name,
        target_field=target_field,
        fixture_path=fixture_path,
        status=status,
        error=error,
    )


def load_context(
    *,
    rule: dict[str, Any],
    target_dataset: dict[str, Any] | None,
    fixtures_dir: Path,
    data_dir: Path | None = None,
) -> ReferenceContext:
    """加载一条参照规则的目标键集；任何不能可靠取得键集的情况都返回 not ready。"""
    try:
        target_dataset_id, target_field = parse_ref_params(rule)
    except ReferenceConfigError as exc:
        return _error_context(rule, status=STATUS_CONFIG_ERROR, error=str(exc))

    if target_dataset is None:
        return _error_context(
            rule,
            status=STATUS_TARGET_MISSING,
            error=f"目标数据集 {target_dataset_id} 不存在",
            target_dataset_id=target_dataset_id,
            target_field=target_field,
        )
    if not target_dataset.get("enabled", 1):
        return _error_context(
            rule,
            status=STATUS_TARGET_DISABLED,
            error=f"目标数据集「{target_dataset['name']}」已停用",
            target_dataset_id=target_dataset_id,
            target_dataset_name=str(target_dataset["name"]),
            target_field=target_field,
            fixture_path=str(target_dataset.get("fixture_path") or ""),
        )

    fixture_path = str(target_dataset.get("fixture_path") or "")
    path = resolve_fixture_path(fixture_path, fixtures_dir=fixtures_dir, data_dir=data_dir)
    base = dict(
        target_dataset_id=target_dataset_id,
        target_dataset_name=str(target_dataset["name"]),
        target_field=target_field,
        fixture_path=fixture_path,
    )

    try:
        rows = parse_jsonl(path)
    except FileNotFoundError as exc:
        return _error_context(rule, status=STATUS_MISSING, error=str(exc), **base)
    except ValueError as exc:
        return _error_context(
            rule,
            status=STATUS_PARSE_ERROR,
            error=f"目标夹具解析失败: {exc}",
            **base,
        )

    keys: set[str] = set()
    skipped = 0
    for row in rows:
        key = normalize_key(row.get(target_field))
        if key == "":
            skipped += 1
            continue
        keys.add(key)

    ctx = ReferenceContext(
        rule_id=rule.get("id"),
        rule_name=str(rule.get("name") or ""),
        source_field=str(rule.get("field") or ""),
        status=STATUS_READY,
        target_rows=len(rows),
        target_skipped_empty=skipped,
        keys=keys,
        **base,
    )
    if not rows:
        ctx.status = STATUS_EMPTY
        ctx.error = f"目标夹具为空: {path}"
    elif not keys:
        ctx.status = STATUS_EMPTY
        ctx.error = f"目标夹具键集为空（字段 {target_field} 无有效值）: {path}"
    return ctx


def load_contexts(
    rules: list[dict[str, Any]],
    *,
    target_datasets: dict[int | None, dict[str, Any] | None],
    fixtures_dir: Path,
    data_dir: Path | None = None,
) -> list[ReferenceContext]:
    contexts: list[ReferenceContext] = []
    for rule in rules:
        try:
            target_id, _ = parse_ref_params(rule)
        except ReferenceConfigError:
            target_id = None
        contexts.append(
            load_context(
                rule=rule,
                target_dataset=target_datasets.get(target_id),
                fixtures_dir=fixtures_dir,
                data_dir=data_dir,
            )
        )
    return contexts


def evaluate_row(
    payload: dict[str, Any],
    contexts: list[ReferenceContext],
    *,
    row_no: int,
    entity_key: str | None,
) -> list[dict[str, Any]]:
    """对单行执行参照检查；返回缺参照的 rule hit（severity 固定 error）。"""
    hits: list[dict[str, Any]] = []
    for ctx in contexts:
        if not ctx.ready:
            # 整批已在调用方中止，这里只做防御
            continue
        key = normalize_key(payload.get(ctx.source_field))
        ctx.source_rows += 1
        ctx.source_values.add(key)
        if key in ctx.keys:
            ctx.matched.add(key)
            continue

        detail = (
            f"源字段 {ctx.source_field} 为空，无法参照 "
            f"{ctx.target_dataset_name}.{ctx.target_field}"
            if key == ""
            else f"参照键 {key} 未在 {ctx.target_dataset_name}.{ctx.target_field} 键集中找到"
        )
        hit = {
            "rule_id": ctx.rule_id,
            "name": ctx.rule_name,
            "field": ctx.source_field,
            "rule_type": REF_RULE_TYPE,
            "severity": "error",
            "detail": detail,
            "reference": {
                "target_dataset_id": ctx.target_dataset_id,
                "target_dataset_name": ctx.target_dataset_name,
                "target_field": ctx.target_field,
                "fixture_path": ctx.fixture_path,
                "key": display_key(key),
            },
        }
        hits.append(hit)
        ctx.failure_records.append(
            {
                "rule_id": ctx.rule_id,
                "rule_name": ctx.rule_name,
                "source_field": ctx.source_field,
                "target_dataset_id": ctx.target_dataset_id,
                "target_dataset_name": ctx.target_dataset_name,
                "target_field": ctx.target_field,
                "row_no": row_no,
                "entity_key": entity_key,
                "key": display_key(key),
            }
        )
    return hits


def reference_report(contexts: list[ReferenceContext]) -> dict[str, Any]:
    rules = [ctx.summary() for ctx in contexts]
    missing_pairs = {
        (ctx.rule_id, value)
        for ctx in contexts
        if ctx.ready
        for value in ctx.missing_keys
    }
    failed_rows = {
        (ctx.rule_id, rec["row_no"])
        for ctx in contexts
        if ctx.ready
        for rec in ctx.failure_records
    }
    return {
        "enabled": bool(contexts),
        "ready": all(ctx.ready for ctx in contexts) if contexts else True,
        "failed_row_count": len(failed_rows),
        "failed_key_count": len(missing_pairs),
        "missing_keys": [
            {
                "rule_id": rule_id,
                "key": display_key(value),
                "target_dataset_name": next(
                    (ctx.target_dataset_name for ctx in contexts if ctx.rule_id == rule_id), ""
                ),
                "target_field": next(
                    (ctx.target_field for ctx in contexts if ctx.rule_id == rule_id), ""
                ),
            }
            for rule_id, value in sorted(missing_pairs, key=lambda pair: (pair[0] or 0, pair[1]))
        ],
        "rules": rules,
    }
