"""筛选预设业务规则：定义保存、版本推进与共享口径。

预设是分群画像筛选规则的来源之一。预设发布后规则即固化，引用它的画像
通过规则指纹校验拿到同一口径；预设被废弃后不能再被引用或作为对比基准。
"""
from __future__ import annotations

import copy
from typing import Any

from app import population
from app.services import profilechain
from app.store import store

MODULE = profilechain.PRESET_MODULE
REQUIRED_FIELDS = ["预设编号", "预设名称", "维度条件组"]
STATUS_ORDER = ["草稿", "预设评审中", "已发布", "已废弃"]
ACTION_RULES = {"提交预设评审": "预设评审中", "发布预设": "已发布", "废弃预设": "已废弃"}
ALLOWED_TRANSITIONS = {
    "草稿": {"提交预设评审"},
    "预设评审中": {"发布预设"},
    "已发布": {"废弃预设"},
    "已废弃": set(),
}
RULE_FIELDS = {"维度条件组", "时间范围"}


class FilterpresetService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = [self._view(row) for row in store.rows(MODULE)]
        if keyword:
            rows = [
                row
                for row in rows
                if keyword in str(row.get("预设编号", "")) or keyword in str(row.get("预设名称", ""))
            ]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = store.find(MODULE, entry_id)
        if row is None:
            return None
        view = self._view(row)
        view["引用画像"] = self._referencing_profiles(str(row.get("预设编号")))
        return view

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str], str]:
        try:
            normalized = profilechain.normalize_preset(values)
        except profilechain.DefinitionError as exc:
            return None, _missing(values, str(exc)), str(exc)
        with store.transaction():
            if store.find_by(MODULE, "预设编号", normalized["预设编号"]) is not None:
                message = f"预设编号 {normalized['预设编号']} 已存在，请换一个编号"
                return None, [], message
            entry = {
                "id": store.next_id(MODULE),
                "status": STATUS_ORDER[0],
                "pending": True,
                "abnormal": False,
            }
            entry.update(normalized)
            entry["版本号"] = 1
            entry["规则指纹"] = population.rule_digest(entry.get("维度条件组"))
            entry["生成时间"] = profilechain.now_text()
            store.rows(MODULE).append(entry)
        return entry, [], "预设模板已登记"

    def update_entry(self, entry_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"预设模板 {entry_id} 不存在或已归档"
        if entry.get("status") == "已发布":
            return None, "预设已发布，筛选口径已固化；如需调整请新建预设，不能直接改写已发布版本"
        if entry.get("status") == "已废弃":
            return None, "预设已废弃，不能继续修改；请新建预设"
        try:
            normalized = profilechain.normalize_preset(values, entry)
        except profilechain.DefinitionError as exc:
            return None, str(exc)
        if normalized["预设编号"] != entry.get("预设编号") and store.find_by(
            MODULE, "预设编号", normalized["预设编号"]
        ):
            return None, f"预设编号 {normalized['预设编号']} 已存在，请换一个编号"
        rule_changed = any(
            str(normalized.get(field)) != str(entry.get(field)) for field in RULE_FIELDS
        )
        with store.transaction():
            entry.update(normalized)
            if rule_changed:
                entry["版本号"] = int(entry.get("版本号") or 1) + 1
                entry["规则指纹"] = population.rule_digest(entry.get("维度条件组"))
        return entry, "预设模板已更新" + ("，版本号已推进" if rule_changed else "")

    def record_usage(self, entry_id: int) -> dict[str, Any] | None:
        """画像引用预设成功时累加使用次数；失败的引用不计。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        entry["使用次数"] = int(entry.get("使用次数") or 0) + 1
        return entry

    def run_action(
        self,
        entry_id: int,
        action: str,
        values: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"预设模板 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于筛选预设可执行范围"
        status = str(entry.get("status") or "")
        if action not in ALLOWED_TRANSITIONS.get(status, set()):
            allowed = "、".join(sorted(ALLOWED_TRANSITIONS.get(status, set()))) or "无"
            return None, f"预设当前状态「{status}」不允许执行「{action}」，可执行动作：{allowed}"
        target = ACTION_RULES[action]
        if action == "废弃预设":
            referencing = self._referencing_profiles(str(entry.get("预设编号")))
            active = [item for item in referencing if item["状态"] != "已下线"]
            if active:
                names = "、".join(f"{item['画像编号']}({item['状态']})" for item in active)
                return None, f"预设仍被画像引用：{names}；请先下线相关画像再废弃预设"
        with store.transaction():
            entry["status"] = target
            entry["pending"] = target not in {STATUS_ORDER[2], STATUS_ORDER[3]}
            entry["abnormal"] = action == "废弃预设"
            entry["状态变更时间"] = profilechain.now_text()
            if action == "发布预设":
                entry["版本号"] = int(entry.get("版本号") or 1)
                entry["规则指纹"] = population.rule_digest(entry.get("维度条件组"))
        return entry, f"预设模板已{action}"

    def preview(self, entry_id: int, interval: str | None = None) -> tuple[dict[str, Any] | None, str]:
        """预览预设口径下的覆盖对象数，与画像快照使用同一取数链路。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"预设模板 {entry_id} 不存在或已归档"
        definition = profilechain.preset_definition(entry)
        try:
            interval_text = population.resolve_interval(
                explicit=interval or definition["数据区间"] or profilechain.DEFAULT_CYCLE
            )
            rows = population.select(definition["规则文本"], interval_text)
            dimensions = preset_dimensions(entry)
        except (profilechain.DefinitionError, population.RuleError) as exc:
            return None, str(exc)
        return {
            "预设编号": definition["预设编号"],
            "版本号": int(entry.get("版本号") or 1),
            "规则文本": definition["规则文本"],
            "规则指纹": definition["规则指纹"],
            "状态": entry.get("status"),
            "数据区间": interval_text,
            "覆盖对象数": len(rows),
            "维度分布": population.distribute(rows, dimensions),
            "人群数据版本": population.POPULATION_VERSION,
        }, ""

    def _view(self, row: dict[str, Any]) -> dict[str, Any]:
        view = copy.deepcopy(row)
        try:
            definition = profilechain.preset_definition(row)
        except profilechain.DefinitionError as exc:
            view["规则状态"] = f"规则不可用：{exc}"
            view["规则指纹"] = None
            return view
        view["规则指纹"] = definition["规则指纹"]
        view["数据区间"] = definition["数据区间"] or population.resolve_interval(
            profilechain.DEFAULT_CYCLE
        )
        view["引用画像"] = self._referencing_profiles(str(row.get("预设编号")))
        view["规则状态"] = "已固化" if row.get("status") == "已发布" else status_label(row)
        return view

    def _referencing_profiles(self, preset_code: str) -> list[dict[str, Any]]:
        if not preset_code:
            return []
        rows = [
            row
            for row in store.rows(profilechain.PROFILE_MODULE)
            if str(row.get("预设编号") or "") == preset_code
        ]
        return [
            {
                "画像编号": row.get("画像编号"),
                "分群名称": row.get("分群名称"),
                "状态": row.get("status"),
                "画像快照": row.get("画像快照"),
                "覆盖对象数": row.get("覆盖对象数"),
                "快照版本": row.get("快照版本"),
            }
            for row in rows
        ]


def preset_dimensions(row: dict[str, Any]) -> list[str]:
    """预设预览用的分组维度：优先取条件里出现的画像维度，没有则用默认维度。"""
    try:
        conditions = population.parse_rule(row.get("维度条件组"))
    except population.RuleError:
        return list(population.DEFAULT_DIMENSIONS)
    fields = [condition["字段"] for condition in conditions if condition["字段"] in population.DIMENSIONS]
    return fields or list(population.DEFAULT_DIMENSIONS)


def status_label(row: dict[str, Any]) -> str:
    status = str(row.get("status") or "")
    return {"草稿": "待评审", "预设评审中": "评审中", "已发布": "已固化", "已废弃": "已废弃"}.get(
        status, status
    )


def _missing(values: dict[str, Any], message: str) -> list[str]:
    if not message.startswith("缺少必填字段"):
        return []
    return [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
