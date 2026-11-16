"""事件分析业务规则：分析模型的保存、状态流转与分群画像快照消费。

事件分析不再自己算覆盖对象数：它只消费分群画像的快照。
- 只有「已发布」画像可以作为对比对象，已下线画像会被拦下；
- 模型上固化快照编号与版本，画像发布新版本时由同步入口把消费方刷新到新快照；
- 读取结果时校验画像状态与快照版本，旧覆盖数不会混进新对比。
"""
from __future__ import annotations

import copy
from typing import Any

from app.services import profilechain
from app.store import store

MODULE = "eventanalysis"
REQUIRED_FIELDS = ["模型编号", "事件名称", "分组维度"]
STATUS_ORDER = ["草稿", "模型评审中", "已启用", "已归档"]
ACTION_RULES = {"提交模型评审": "模型评审中", "启用模型": "已启用", "归档模型": "已归档"}
ALLOWED_TRANSITIONS = {
    "草稿": {"提交模型评审"},
    "模型评审中": {"启用模型"},
    "已启用": {"归档模型"},
    "已归档": set(),
}
SEGMENT_FIELD = "画像编号"


class EventanalysisService:
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
                if keyword in str(row.get("模型编号", "")) or keyword in str(row.get("事件名称", ""))
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
        return self._view(row, detail=True)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str], str]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing, f"缺少必填字段：{'、'.join(missing)}"
        segment_code = str(values.get(SEGMENT_FIELD) or "").strip()
        snapshot = None
        if segment_code:
            try:
                snapshot = self._require_published_snapshot(segment_code)
            except profilechain.DefinitionError as exc:
                return None, [], str(exc)
        with store.transaction():
            if store.find_by(MODULE, "模型编号", str(values.get("模型编号")).strip()) is not None:
                return None, [], f"模型编号 {values.get('模型编号')} 已存在，请换一个编号"
            entry = {
                "id": store.next_id(MODULE),
                "status": STATUS_ORDER[0],
                "pending": True,
                "abnormal": False,
            }
            entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
            entry[SEGMENT_FIELD] = segment_code or None
            self._pin_snapshot(entry, snapshot)
            store.rows(MODULE).append(entry)
        return entry, [], "分析模型已登记"

    def update_entry(self, entry_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"分析模型 {entry_id} 不存在或已归档"
        if entry.get("status") == "已归档":
            return None, "分析模型已归档，不能继续修改"
        created = dict(entry)
        for field in REQUIRED_FIELDS:
            if field in values:
                created[field] = values.get(field)
        missing = [field for field in REQUIRED_FIELDS if not str(created.get(field) or "").strip()]
        if missing:
            return None, f"缺少必填字段：{'、'.join(missing)}"

        segment_code = str(values.get(SEGMENT_FIELD, entry.get(SEGMENT_FIELD)) or "").strip()
        snapshot = None
        if segment_code:
            try:
                snapshot = self._require_published_snapshot(segment_code)
            except profilechain.DefinitionError as exc:
                return None, str(exc)
        with store.transaction():
            entry.update({field: values.get(field) for field in REQUIRED_FIELDS if field in values})
            entry[SEGMENT_FIELD] = segment_code or None
            if segment_code != entry.get("快照画像编号") or snapshot is not None:
                self._pin_snapshot(entry, snapshot)
        return entry, "分析模型已更新"

    def run_action(
        self,
        entry_id: int,
        action: str,
        values: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"分析模型 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于事件分析可执行范围"
        status = str(entry.get("status") or "")
        if action not in ALLOWED_TRANSITIONS.get(status, set()):
            allowed = "、".join(sorted(ALLOWED_TRANSITIONS.get(status, set()))) or "无"
            return None, f"模型当前状态「{status}」不允许执行「{action}」，可执行动作：{allowed}"
        if action == "启用模型" and entry.get(SEGMENT_FIELD):
            try:
                snapshot = self._require_published_snapshot(str(entry.get(SEGMENT_FIELD)))
            except profilechain.DefinitionError as exc:
                return None, str(exc)
        else:
            snapshot = None
        with store.transaction():
            if snapshot is not None:
                self._pin_snapshot(entry, snapshot)
            entry["status"] = ACTION_RULES[action]
            entry["pending"] = ACTION_RULES[action] not in {STATUS_ORDER[2], STATUS_ORDER[3]}
            entry["abnormal"] = action == "归档模型"
            entry["状态变更时间"] = profilechain.now_text()
        return entry, f"分析模型已{action}"

    # ------------------------------------------------------------------
    # 快照消费
    # ------------------------------------------------------------------

    def comparison(self, entry_id: int, interval: str | None = None) -> tuple[dict[str, Any] | None, str]:
        """输出事件分析的对比结果：覆盖对象数与画像差异都来自同一份快照。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"分析模型 {entry_id} 不存在或已归档"
        segment_code = str(entry.get(SEGMENT_FIELD) or "").strip()
        if not segment_code:
            return None, "分析模型未关联分群画像，无法生成对比结果"
        segment = store.find_by(profilechain.PROFILE_MODULE, "画像编号", segment_code)
        if segment is None:
            return None, f"关联画像 {segment_code} 不存在或已归档"
        if segment.get("status") != "已发布":
            return None, (
                f"关联画像 {segment_code} 当前状态为「{segment.get('status')}」，"
                "已下线或未发布的画像不能参与事件分析对比"
            )

        snapshot = profilechain.latest_snapshot_for(segment_code)
        if snapshot is None:
            return None, f"关联画像 {segment_code} 还没有快照，无法生成对比结果"
        try:
            interval_text = profilechain.population.resolve_interval(
                explicit=interval or snapshot["数据区间"]
            )
        except profilechain.population.RuleError as exc:
            return None, str(exc)
        if interval_text != snapshot["数据区间"]:
            return None, (
                f"请求区间 {interval_text} 与画像快照区间 {snapshot['数据区间']} 不一致；"
                "画像差异只比较同一数据区间，请改为重算画像快照后再对比"
            )

        self._pin_snapshot(entry, snapshot)
        pinned = profilechain.snapshot_public_view(snapshot)
        return {
            "模型编号": entry.get("模型编号"),
            "事件名称": entry.get("事件名称"),
            "画像编号": segment_code,
            "快照编号": snapshot["快照编号"],
            "快照版本": snapshot["版本号"],
            "数据区间": snapshot["数据区间"],
            "覆盖对象数": snapshot["目标覆盖对象数"],
            "基准覆盖对象数": snapshot["基准覆盖对象数"],
            "画像差异": snapshot["画像差异"],
            "维度分布": snapshot["维度分布"],
            "画像快照": pinned,
            "来源记录": profilechain.sources_for(snapshot["快照编号"]),
            "口径一致性": {
                "画像状态": segment.get("status"),
                "快照状态": snapshot.get("状态"),
                "规则指纹一致": segment.get("规则指纹") == snapshot["规则指纹"],
                "数据区间一致": segment.get("数据区间") == snapshot["数据区间"],
                "消费版本": entry.get("快照版本"),
                "最新版本": snapshot["版本号"],
            },
        }, ""

    def sync_snapshot(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """画像发布新版本后，把消费方刷新到最新快照，避免旧覆盖数残留。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"分析模型 {entry_id} 不存在或已归档"
        segment_code = str(entry.get(SEGMENT_FIELD) or "").strip()
        if not segment_code:
            return entry, "分析模型未关联分群画像，无需同步"
        try:
            snapshot = self._require_published_snapshot(segment_code)
        except profilechain.DefinitionError as exc:
            return None, str(exc)
        with store.transaction():
            self._pin_snapshot(entry, snapshot)
        return entry, f"已同步到画像快照 {snapshot['快照编号']}（v{snapshot['版本号']}）"

    def _require_published_snapshot(self, segment_code: str) -> dict[str, Any]:
        segment = store.find_by(profilechain.PROFILE_MODULE, "画像编号", segment_code)
        if segment is None:
            raise profilechain.DefinitionError(f"分群画像 {segment_code} 不存在")
        if segment.get("status") != "已发布":
            raise profilechain.DefinitionError(
                f"分群画像 {segment_code} 当前状态为「{segment.get('status')}」，"
                "只有已发布画像可以参与事件分析对比"
            )
        snapshot = profilechain.latest_snapshot_for(segment_code)
        if snapshot is None:
            raise profilechain.DefinitionError(f"分群画像 {segment_code} 还没有快照，请先生成画像快照")
        return snapshot

    def _pin_snapshot(self, entry: dict[str, Any], snapshot: dict[str, Any] | None) -> None:
        if snapshot is None:
            entry["快照编号"] = None
            entry["快照版本"] = None
            entry["快照画像编号"] = None
            entry["覆盖对象数"] = None
            entry["画像差异摘要"] = None
            return
        entry["快照编号"] = snapshot["快照编号"]
        entry["快照版本"] = snapshot["版本号"]
        entry["快照画像编号"] = snapshot["画像编号"]
        entry["覆盖对象数"] = snapshot["目标覆盖对象数"]
        entry["画像差异摘要"] = {
            "数据区间": snapshot["数据区间"],
            "覆盖差异": snapshot["覆盖差异"],
            "Jaccard系数": snapshot["画像差异"].get("Jaccard系数"),
        }

    def _view(self, row: dict[str, Any], *, detail: bool = False) -> dict[str, Any]:
        view = copy.deepcopy(row)
        segment_code = str(row.get(SEGMENT_FIELD) or "").strip()
        if not segment_code:
            view["画像状态"] = "未关联"
            view["快照同步状态"] = "无需同步"
            return view
        segment = store.find_by(profilechain.PROFILE_MODULE, "画像编号", segment_code)
        snapshot = profilechain.latest_snapshot_for(segment_code) if segment else None
        view["画像状态"] = segment.get("status") if segment else "已删除"
        if segment is None:
            view["快照同步状态"] = "画像不存在"
            if detail:
                view["快照同步提示"] = f"关联画像 {segment_code} 已不存在，请重新关联"
        elif segment.get("status") != "已发布":
            view["快照同步状态"] = "画像不可用"
            if detail:
                view["快照同步提示"] = (
                    f"关联画像 {segment_code} 当前为「{segment.get('status')}」，不能参与对比"
                )
        elif snapshot is None:
            view["快照同步状态"] = "缺少快照"
        elif view.get("快照编号") != snapshot["快照编号"]:
            view["快照同步状态"] = "待同步"
            if detail:
                view["快照同步提示"] = (
                    f"画像最新快照为 {snapshot['快照编号']}（v{snapshot['版本号']}），"
                    f"当前消费 {view.get('快照编号') or '无'}，可执行同步"
                )
        else:
            view["快照同步状态"] = "已同步"
        if detail and snapshot is not None:
            view["画像快照"] = profilechain.snapshot_public_view(snapshot)
            view["来源记录"] = profilechain.sources_for(snapshot["快照编号"])
            view["可执行动作"] = list(ALLOWED_TRANSITIONS.get(str(row.get("status")), set())) + (
                ["同步画像快照"] if view.get("快照同步状态") == "待同步" else []
            )
        return view
