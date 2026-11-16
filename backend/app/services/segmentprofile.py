"""分群画像业务规则：定义保存、状态流转、快照计算与版本追溯。

所有口径都委托给 ``app.services.profilechain``，画像、预设、事件分析共用同一套
规则解析与取数逻辑，避免各页面各算一份导致覆盖对象数对不上。
"""
from __future__ import annotations

from typing import Any

from app.services import profilechain
from app.store import store

MODULE = profilechain.PROFILE_MODULE
REQUIRED_FIELDS = ["画像编号", "分群名称", "筛选规则"]
STATUS_ORDER = ["草稿", "画像评审中", "已发布", "已下线"]
ACTION_RULES = {"提交画像评审": "画像评审中", "发布画像": "已发布", "下线画像": "已下线"}
# 发布后不允许回退：已发布只能下线，已下线即终态。
ALLOWED_TRANSITIONS = {
    "草稿": {"提交画像评审"},
    "画像评审中": {"发布画像"},
    "已发布": {"下线画像"},
    "已下线": set(),
}
REVIEW_ACTIONS = {"重算覆盖数", "生成画像快照"}


class SegmentprofileService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        snapshot_status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = [profilechain.profile_view(row) for row in store.rows(MODULE)]
        if keyword:
            rows = [
                row
                for row in rows
                if keyword in str(row.get("画像编号", "")) or keyword in str(row.get("分群名称", ""))
            ]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if snapshot_status:
            rows = [row for row in rows if row.get("快照状态") == snapshot_status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = store.find(MODULE, entry_id)
        if row is None:
            return None
        view = profilechain.profile_view(row)
        view["快照历史"] = profilechain.list_snapshots(str(row.get("画像编号")))
        return view

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str], str]:
        """新建画像时完整保存规则、维度、基准、周期，并立即落一个可追溯快照。"""
        payload = profilechain.normalize_profile_values(values)
        try:
            definition = profilechain.resolve_definition(payload)
        except profilechain.DefinitionError as exc:
            return None, _missing_fields(payload, exc), str(exc)
        segment_code = definition["画像编号"]
        if not segment_code:
            return None, ["画像编号"], "缺少必填字段：画像编号"
        if not definition["分群名称"]:
            return None, ["分群名称"], "缺少必填字段：分群名称"

        try:
            with store.transaction():
                profilechain.assert_unique_profile_code(segment_code)
                entry = {
                    "id": store.next_id(MODULE),
                    "status": STATUS_ORDER[0],
                    "pending": True,
                    "abnormal": False,
                    "画像编号": segment_code,
                    "分群名称": definition["分群名称"],
                    "筛选规则": definition["筛选规则"],
                    "画像维度": definition["画像维度文本"],
                    "对比基准": definition["对比基准"],
                    "更新周期": definition["更新周期"],
                    "预设编号": definition.get("预设编号"),
                    "数据区间": definition["数据区间"],
                    "区间来源": definition["区间来源"],
                    "规则指纹": definition["规则指纹"],
                    "基准规则指纹": definition["基准规则指纹"],
                    "快照版本": None,
                    "覆盖对象数": 0,
                    "画像快照": None,
                }
                store.rows(MODULE).append(entry)
                result = profilechain.compute_snapshot(int(entry["id"]), trigger="新建画像")
        except profilechain.DefinitionError as exc:
            return None, _missing_fields(payload, exc), str(exc)
        return entry, [], f"画像定义已登记，{result['snapshot']['快照编号']} 覆盖 {entry['覆盖对象数']} 人"

    def update_entry(self, entry_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """修改画像定义；规则或区间变化后覆盖对象数必须按新口径重算。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"画像定义 {entry_id} 不存在或已归档"
        if entry.get("status") == "已发布":
            return None, "画像已发布，定义已随发布快照固化；如需调整请先下线后新建画像，不能直接回退"
        if entry.get("status") == "已下线":
            return None, "画像已下线，不能继续修改定义；请新建画像"

        payload = profilechain.normalize_profile_values(values)
        if not payload:
            if "覆盖对象数" in values:
                row, message = self.recompute(entry_id, trigger="覆盖数重算")
                if row is None:
                    return None, message
                return row, (
                    f"提交的覆盖对象数不直接采用，已按同一快照重算；{message}"
                )
            return entry, "没有需要更新的字段"
        try:
            definition = profilechain.resolve_definition(payload, entry)
        except profilechain.DefinitionError as exc:
            return None, str(exc)

        try:
            with store.transaction():
                entry.update(
                    {
                        "画像编号": definition["画像编号"],
                        "分群名称": definition["分群名称"],
                        "筛选规则": definition["筛选规则"],
                        "画像维度": definition["画像维度文本"],
                        "对比基准": definition["对比基准"],
                        "更新周期": definition["更新周期"],
                        "预设编号": definition.get("预设编号"),
                        "数据区间": definition["数据区间"],
                        "区间来源": definition["区间来源"],
                        "规则指纹": definition["规则指纹"],
                        "基准规则指纹": definition["基准规则指纹"],
                    }
                )
                result = profilechain.compute_snapshot(
                    entry_id,
                    trigger="定义变更重算",
                    idempotency_key=str(values.get("幂等键") or "") or None,
                )
        except profilechain.DefinitionError as exc:
            return None, str(exc)
        message = "画像定义已更新"
        if "覆盖对象数" in values:
            message += "；提交的覆盖对象数不直接采用，已按同一快照重算"
        if result["冲突"]:
            message += "；历史覆盖数与现行定义不一致，已按同区间重算并保留历史版本留痕"
        return entry, f"{message}，当前覆盖 {entry['覆盖对象数']} 人"

    def recompute(
        self,
        entry_id: int,
        *,
        interval: str | None = None,
        idempotency_key: str | None = None,
        trigger: str = "手动重算",
    ) -> tuple[dict[str, Any] | None, str]:
        """按现行规则重算覆盖对象数与画像差异；同一口径只形成一个版本。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"画像定义 {entry_id} 不存在或已归档"
        try:
            result = profilechain.compute_snapshot(
                entry_id,
                trigger=trigger,
                interval=interval,
                idempotency_key=idempotency_key,
            )
        except profilechain.DefinitionError as exc:
            return None, str(exc)
        snapshot = result["snapshot"]
        action = "复用" if result["复用"] else "生成"
        message = (
            f"已{action}快照 {snapshot['快照编号']}（v{snapshot['版本号']}），"
            f"覆盖对象数 {snapshot['目标覆盖对象数']}，数据区间 {snapshot['数据区间']}"
        )
        if result["冲突"]:
            message += "；历史覆盖数与现行规则冲突，旧覆盖数未进入新对比"
        return entry, message

    def run_action(
        self,
        entry_id: int,
        action: str,
        values: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        values = values or {}
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"画像定义 {entry_id} 不存在或已归档"

        if action in REVIEW_ACTIONS:
            return self.recompute(
                entry_id,
                interval=str(values.get("数据区间") or "") or None,
                idempotency_key=str(values.get("幂等键") or "") or None,
                trigger=str(values.get("触发方式") or action),
            )

        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于分群画像可执行范围"
        status = str(entry.get("status") or "")
        if action not in ALLOWED_TRANSITIONS.get(status, set()):
            allowed = "、".join(sorted(ALLOWED_TRANSITIONS.get(status, set()))) or "无"
            return None, f"画像当前状态「{status}」不允许执行「{action}」，可执行动作：{allowed}"
        target = ACTION_RULES[action]

        try:
            with store.transaction():
                if action == "发布画像":
                    snapshot = profilechain.latest_snapshot_for(str(entry.get("画像编号")))
                    if snapshot is None:
                        return None, "画像还没有快照，不能发布；请先生成画像快照"
                    if (
                        snapshot.get("规则指纹") != entry.get("规则指纹")
                        or snapshot.get("数据区间") != entry.get("数据区间")
                    ):
                        return None, "最新快照与现行定义不一致，请先重算覆盖数再发布"
                    # 发布即固化：覆盖对象数与差异钉在发布时的快照上。
                    profilechain.compute_snapshot(
                        entry_id,
                        trigger="发布固化",
                        interval=str(snapshot["数据区间"]),
                        solidify=True,
                    )
                entry["status"] = target
                entry["pending"] = target not in {STATUS_ORDER[2], STATUS_ORDER[3]}
                entry["abnormal"] = action == "下线画像"
                entry["状态变更时间"] = profilechain.now_text()
        except profilechain.DefinitionError as exc:
            return None, str(exc)
        return entry, f"画像定义已{action}"

    def difference(self, entry_id: int, interval: str | None = None) -> tuple[dict[str, Any] | None, str]:
        """读取画像差异；缺快照、快照过期或换了区间时按现行规则重算后再返回。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"画像定义 {entry_id} 不存在或已归档"
        snapshot = profilechain.get_snapshot_by_code(str(entry.get("画像快照") or ""))
        stale = snapshot is not None and (
            snapshot.get("规则指纹") != entry.get("规则指纹")
            or snapshot.get("基准规则指纹") != entry.get("基准规则指纹")
            or snapshot.get("数据区间") != entry.get("数据区间")
        )
        if snapshot is None or stale or interval:
            entry, message = self.recompute(entry_id, interval=interval, trigger="差异重算")
            if entry is None:
                return None, message
            snapshot = profilechain.get_snapshot_by_code(str(entry.get("画像快照") or ""))
        if snapshot is None:
            return None, "画像还没有快照，无法计算画像差异"
        return {
            "画像编号": snapshot["画像编号"],
            "快照编号": snapshot["快照编号"],
            "版本号": snapshot["版本号"],
            "数据区间": snapshot["数据区间"],
            "覆盖对象数": snapshot["目标覆盖对象数"],
            "基准覆盖对象数": snapshot["基准覆盖对象数"],
            "画像差异": snapshot["画像差异"],
            "来源记录": snapshot.get("来源记录", []),
        }, ""

    def snapshot_history(self, entry_id: int) -> tuple[list[dict[str, Any]], str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return [], f"画像定义 {entry_id} 不存在或已归档"
        return profilechain.list_snapshots(str(entry.get("画像编号"))), ""


def _missing_fields(payload: dict[str, Any], error: profilechain.DefinitionError) -> list[str]:
    """把定义错误拆成缺失字段清单，便于接口按既有格式返回。

    引用预设时筛选规则允许留空（以预设口径为准），此时只报真正缺失的字段。
    """
    message = str(error)
    if not message.startswith("缺少必填字段"):
        return []
    fields = [field for field in REQUIRED_FIELDS if not str(payload.get(field) or "").strip()]
    if str(payload.get("预设编号") or "").strip():
        fields = [field for field in fields if field != "筛选规则"]
    return fields
