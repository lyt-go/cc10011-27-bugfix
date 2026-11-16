"""分群画像共享链路：定义解析、快照版本、来源记录与口径冲突权衡。

分群画像、筛选预设、事件分析共用这一条链路：

1. 定义解析：筛选规则、画像维度、对比基准、更新周期、数据区间统一规范化，
   任何入口看到的规则文本和指纹都一致；
2. 快照计算：目标人群与对比基准在同一数据区间取数，产出覆盖对象数、
   维度分布和画像差异；
3. 原子落库：版本快照、来源记录、画像行上的覆盖数在同一个事务里写入，
   任何一步失败全部回滚，残缺版本不会暴露；
4. 版本幂等：同一分群、同一口径、同一区间只保留一个有效版本，
   两个增量任务同时更新时也只会形成一个版本；
5. 冲突权衡：历史覆盖数与现行筛选规则对不上时，保留历史版本用于追溯，
   但新对比一律按同区间重算值，旧覆盖数不进入新结果。
"""
from __future__ import annotations

import copy
from datetime import datetime
from typing import Any, Iterable, Sequence

from app import population
from app.store import store

PROFILE_MODULE = "segmentprofile"
PRESET_MODULE = "filterpreset"
SNAPSHOT_MODULE = "segmentprofile_snapshot"
SOURCE_MODULE = "segmentprofile_source"

PROFILE_DEFINITION_FIELDS = [
    "画像编号",
    "分群名称",
    "筛选规则",
    "画像维度",
    "对比基准",
    "更新周期",
    "预设编号",
    "数据区间",
]
PROFILE_DERIVED_FIELDS = [
    "覆盖对象数",
    "画像快照",
    "快照版本",
    "规则指纹",
    "基准规则指纹",
    "人群数据版本",
]
PROFILE_PAYLOAD_FIELDS = PROFILE_DEFINITION_FIELDS + ["备注"]
# 兼容不同入口的字段叫法，统一映射到同一份定义上。
PROFILE_FIELD_ALIASES = {
    "维度": "画像维度",
    "画像维度组": "画像维度",
    "对比对象": "对比基准",
    "基准": "对比基准",
    "周期": "更新周期",
    "预设": "预设编号",
    "区间": "数据区间",
}

PRESET_FIELDS = [
    "预设编号",
    "预设名称",
    "维度条件组",
    "时间范围",
    "排序规则",
    "显示列",
    "共享状态",
    "使用次数",
    "版本号",
]
PRESET_REQUIRED = ["预设编号", "预设名称", "维度条件组"]

DEFAULT_BASELINE = population.DEFAULT_BASELINE
DEFAULT_CYCLE = population.DEFAULT_CYCLE


class DefinitionError(ValueError):
    """画像定义不合法：调用方转成可读提示，不写任何快照。"""


def now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _text(value: Any) -> str:
    return str(value or "").strip()


# ---------------------------------------------------------------------------
# 筛选预设：规范化与读取
# ---------------------------------------------------------------------------


def normalize_preset(values: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    """校验并规范化预设字段；维度条件组统一成规范文本。"""
    merged: dict[str, Any] = {}
    if existing is not None:
        merged.update({field: existing.get(field) for field in PRESET_FIELDS})
    merged.update({field: values.get(field) for field in PRESET_FIELDS if field in values})

    missing = [field for field in PRESET_REQUIRED if not _text(merged.get(field))]
    if missing:
        raise DefinitionError("缺少必填字段：" + "、".join(missing))

    try:
        conditions = population.parse_rule(merged.get("维度条件组"))
    except population.RuleError as exc:
        raise DefinitionError(f"维度条件组不合法：{exc}") from exc
    merged["维度条件组"] = population.rule_text(conditions)

    time_range = _text(merged.get("时间范围"))
    if time_range:
        try:
            population.resolve_interval(explicit=time_range)
        except population.RuleError as exc:
            raise DefinitionError(f"时间范围不合法：{exc}") from exc

    version = merged.get("版本号")
    try:
        merged["版本号"] = int(version) if version not in (None, "") else 1
    except (TypeError, ValueError) as exc:
        raise DefinitionError("预设版本号必须是整数") from exc
    if merged["版本号"] < 1:
        raise DefinitionError("预设版本号必须大于 0")

    try:
        merged["使用次数"] = int(merged.get("使用次数") or 0)
    except (TypeError, ValueError) as exc:
        raise DefinitionError("使用次数必须是整数") from exc

    merged.setdefault("共享状态", "私有")
    return {field: merged.get(field) for field in PRESET_FIELDS}


def preset_definition(preset: dict[str, Any]) -> dict[str, Any]:
    """把预设行转成共享取数口径；筛选画像引用预设时也走这里。"""
    try:
        conditions = population.parse_rule(preset.get("维度条件组"))
    except population.RuleError as exc:
        raise DefinitionError(f"预设 {preset.get('预设编号')} 的维度条件组不合法：{exc}") from exc
    text = population.rule_text(conditions)
    return {
        "预设编号": preset.get("预设编号"),
        "预设名称": preset.get("预设名称"),
        "版本号": int(preset.get("版本号") or 1),
        "状态": preset.get("status"),
        "规则文本": text,
        "规则指纹": population.rule_digest(conditions),
        "数据区间": population.interval_for_entry(preset.get("时间范围")),
        "时间范围": preset.get("时间范围"),
    }


def lookup_preset(preset_id: Any) -> dict[str, Any]:
    identifier = _text(preset_id)
    preset = store.find_by(PRESET_MODULE, "预设编号", identifier)
    if preset is None:
        raise DefinitionError(f"筛选预设 {identifier} 不存在")
    if preset.get("status") == "已废弃":
        raise DefinitionError(f"筛选预设 {identifier} 已废弃，不能再被画像引用")
    return preset


# ---------------------------------------------------------------------------
# 画像定义解析
# ---------------------------------------------------------------------------


def resolve_definition(
    values: dict[str, Any],
    existing: dict[str, Any] | None = None,
    *,
    require_rule: bool = True,
) -> dict[str, Any]:
    """把提交值与已有定义合并成一份可直接计算的规范化定义。"""
    merged: dict[str, Any] = {}
    if existing is not None:
        merged.update({field: existing.get(field) for field in PROFILE_DEFINITION_FIELDS})
    merged.update({field: values.get(field) for field in PROFILE_DEFINITION_FIELDS if field in values})

    preset_id = _text(merged.get("预设编号"))
    preset: dict[str, Any] | None = None
    preset_def: dict[str, Any] | None = None
    if preset_id:
        preset = lookup_preset(preset_id)
        preset_def = preset_definition(preset)

    explicit_rule = _text(merged.get("筛选规则"))
    if preset_def is not None:
        if explicit_rule:
            try:
                explicit_conditions = population.parse_rule(explicit_rule)
            except population.RuleError as exc:
                raise DefinitionError(f"筛选规则不合法：{exc}") from exc
            if population.rule_digest(explicit_conditions) != preset_def["规则指纹"]:
                raise DefinitionError(
                    f"筛选规则与预设 {preset_id} 的维度条件组不一致；引用预设时以预设口径为准，"
                    "请清空筛选规则或同步预设条件"
                )
        rule_source: Any = preset_def["规则文本"]
    else:
        rule_source = explicit_rule

    if not _text(rule_source):
        if require_rule:
            raise DefinitionError("缺少必填字段：筛选规则")
        rule_source = ""

    try:
        conditions = population.parse_rule(rule_source) if _text(rule_source) else []
        rule_text_value = population.rule_text(conditions)
        rule_digest = population.rule_digest(conditions)
        dimensions = population.normalize_dimensions(merged.get("画像维度"))
    except population.RuleError as exc:
        raise DefinitionError(f"筛选规则或画像维度不合法：{exc}") from exc
    cycle = _text(merged.get("更新周期")) or DEFAULT_CYCLE
    if cycle not in population.CYCLE_DAYS:
        raise DefinitionError(
            f"更新周期「{cycle}」不受支持，可用周期：{'、'.join(population.CYCLE_DAYS)}"
        )

    # 数据区间只有在「本次显式提交」或「此前就按显式区间保存」时才算显式；
    # 由更新周期推导出来的区间不能反过来把周期变更锁死。
    explicit_interval = _text(values.get("数据区间"))
    if not explicit_interval and existing is not None:
        if existing.get("区间来源") == "显式数据区间":
            explicit_interval = _text(existing.get("数据区间"))
    interval: str
    interval_source: str
    if explicit_interval:
        try:
            interval = population.resolve_interval(explicit=explicit_interval)
        except population.RuleError as exc:
            raise DefinitionError(f"数据区间不合法：{exc}") from exc
        interval_source = "显式数据区间"
    elif preset_def is not None and preset_def["数据区间"]:
        interval = preset_def["数据区间"]
        interval_source = f"预设 {preset_def['预设编号']} 的时间范围"
    else:
        interval = population.resolve_interval(cycle)
        interval_source = f"更新周期「{cycle}」"

    baseline_label = _text(merged.get("对比基准")) or DEFAULT_BASELINE
    baseline = resolve_baseline(baseline_label)

    return {
        "画像编号": _text(merged.get("画像编号")),
        "分群名称": _text(merged.get("分群名称")),
        "筛选规则": rule_text_value,
        "规则指纹": rule_digest,
        "画像维度": dimensions,
        "画像维度文本": "、".join(dimensions),
        "对比基准": baseline_label,
        "基准类型": baseline["类型"],
        "基准标识": baseline["标识"],
        "基准规则文本": baseline["规则文本"],
        "基准规则指纹": baseline["规则指纹"],
        "更新周期": cycle,
        "数据区间": interval,
        "区间来源": interval_source,
        "预设编号": preset_def["预设编号"] if preset_def else _text(merged.get("预设编号")) or None,
        "预设版本": preset_def["版本号"] if preset_def else None,
        "预设规则指纹": preset_def["规则指纹"] if preset_def else None,
    }


def resolve_baseline(label: str) -> dict[str, Any]:
    """解析对比基准：全量人群 / 预设编号 / 画像编号 / 显式规则。"""
    text_value = _text(label) or DEFAULT_BASELINE
    if text_value in {"全量人群", "全量", "全体用户", "ALL"}:
        return {"类型": "全量人群", "标识": "全量人群", "规则文本": "", "规则指纹": "ALL"}

    if text_value.startswith(("预设:", "预设：", "preset:")):
        preset_id = text_value.split(":", 1)[-1].split("：", 1)[-1].strip()
        preset_def = preset_definition(lookup_preset(preset_id))
        return {
            "类型": "筛选预设",
            "标识": preset_def["预设编号"],
            "规则文本": preset_def["规则文本"],
            "规则指纹": preset_def["规则指纹"],
        }

    if text_value.startswith(("画像:", "画像：", "segment:")):
        segment_id = text_value.split(":", 1)[-1].split("：", 1)[-1].strip()
        segment = store.find_by(PROFILE_MODULE, "画像编号", segment_id)
        if segment is None:
            raise DefinitionError(f"对比基准画像 {segment_id} 不存在")
        if segment.get("status") != "已发布":
            raise DefinitionError(
                f"对比基准画像 {segment_id} 当前状态为「{segment.get('status')}」，"
                "只有已发布画像可以作为对比基准"
            )
        try:
            conditions = population.parse_rule(segment.get("筛选规则"))
        except population.RuleError as exc:
            raise DefinitionError(f"对比基准画像 {segment_id} 的筛选规则不合法：{exc}") from exc
        return {
            "类型": "分群画像",
            "标识": segment_id,
            "规则文本": population.rule_text(conditions),
            "规则指纹": population.rule_digest(conditions),
        }

    rule_text_value = text_value
    if text_value.startswith(("规则:", "规则：")):
        rule_text_value = text_value.split(":", 1)[-1].split("：", 1)[-1].strip()
    try:
        conditions = population.parse_rule(rule_text_value)
    except population.RuleError as exc:
        raise DefinitionError(f"对比基准「{text_value}」无法识别：{exc}") from exc
    return {
        "类型": "筛选规则",
        "标识": text_value,
        "规则文本": population.rule_text(conditions),
        "规则指纹": population.rule_digest(conditions),
    }


# ---------------------------------------------------------------------------
# 快照计算与落库
# ---------------------------------------------------------------------------


def compute_snapshot(
    profile_id: int,
    *,
    trigger: str = "手动重算",
    interval: str | None = None,
    idempotency_key: str | None = None,
    solidify: bool = False,
) -> dict[str, Any]:
    """计算并原子落库一个画像快照版本。

    返回 ``{"snapshot": ..., "复用": bool, "冲突": ...}``；整个读写过程在
    仓库事务内完成，事务失败时画像行、快照、来源记录一起回滚。
    """
    with store.transaction():
        profile = store.find(PROFILE_MODULE, profile_id)
        if profile is None:
            raise DefinitionError(f"画像定义 {profile_id} 不存在或已归档")
        if profile.get("status") == "已下线":
            raise DefinitionError("画像已下线，不能再生成新快照；请新建画像后重新计算")

        override = _text(interval) or None
        if override:
            try:
                override = population.resolve_interval(explicit=override)
            except population.RuleError as exc:
                raise DefinitionError(f"数据区间不合法：{exc}") from exc
            if profile.get("status") == "已发布" and override != profile.get("数据区间"):
                raise DefinitionError(
                    "画像已发布，取数区间已随发布快照固化，不能直接换区间重算；请先下线后新建画像"
                )

        definition = resolve_definition({"数据区间": override} if override else {}, profile)
        interval_text = definition["数据区间"]
        dimensions = definition["画像维度"]

        target_rows = population.select(definition["筛选规则"], interval_text)
        baseline_rows = population.select(definition["基准规则文本"], interval_text)
        difference = population.build_difference(
            target_rows,
            baseline_rows,
            dimensions,
            interval_text,
            notes=(
                f"目标侧按现行筛选规则 {definition['规则指纹']} 重算",
                f"基准侧按「{definition['对比基准']}」口径 {definition['基准规则指纹']} 重算",
                "两侧均落在同一数据区间，未复用任何历史覆盖数",
            ),
        )
        distribution = population.distribute(target_rows, dimensions)
        coverage = len(target_rows)
        baseline_coverage = len(baseline_rows)

        previous = _latest_snapshot(profile.get("画像编号"))
        key = _text(idempotency_key) or None
        duplicate = None
        if key:
            # 幂等键只对「同一画像 + 同一口径 + 同一区间」去重：同一增量任务重试
            # 不会多开版本，定义或区间变了则必须形成新版本，不能拿旧快照顶替。
            keyed = [
                row
                for row in store.rows(SNAPSHOT_MODULE)
                if row.get("画像编号") == profile.get("画像编号") and row.get("幂等键") == key
            ]
            duplicate = next(
                (
                    row
                    for row in reversed(keyed)
                    if row.get("规则指纹") == definition["规则指纹"]
                    and row.get("数据区间") == interval_text
                    and row.get("基准规则指纹") == definition["基准规则指纹"]
                ),
                None,
            )
            if keyed and duplicate is None:
                raise DefinitionError(
                    f"幂等键 {key} 已用于版本 v{keyed[-1].get('版本号')} 的另一口径快照；"
                    "同一幂等键不能跨越规则或区间复用，请换一个幂等键"
                )
        reuse = duplicate is not None or (
            previous is not None
            and previous.get("状态") == "有效"
            and previous.get("规则指纹") == definition["规则指纹"]
            and previous.get("数据区间") == interval_text
            and previous.get("画像维度") == dimensions
            and previous.get("基准规则指纹") == definition["基准规则指纹"]
        )
        if duplicate is not None:
            # 幂等键命中：同一增量任务重复投递时不新开版本，只把指针补回已有快照。
            snapshot = duplicate
            if duplicate.get("状态") != "有效":
                for row in store.rows(SNAPSHOT_MODULE):
                    if row.get("画像编号") == profile.get("画像编号") and row.get("状态") == "有效":
                        row["状态"] = "历史"
                duplicate["状态"] = "有效"
        elif reuse:
            snapshot = previous
            if solidify and not snapshot.get("是否固化"):
                snapshot["是否固化"] = True
                snapshot["固化时间"] = now_text()
            if key and not snapshot.get("幂等键"):
                snapshot["幂等键"] = key
        else:
            conflict = _build_conflict(previous, definition, coverage, interval_text)
            snapshot = _insert_snapshot(
                profile,
                definition,
                coverage,
                baseline_coverage,
                distribution,
                difference,
                trigger=trigger,
                interval_source=definition["区间来源"],
                idempotency_key=_text(idempotency_key) or None,
                previous=previous,
                conflict=conflict,
                solidify=solidify,
            )
            _insert_sources(snapshot, definition, target_rows, baseline_rows, interval_text)

        _apply_snapshot_to_profile(profile, snapshot, definition)
        return {
            "snapshot": copy.deepcopy(snapshot),
            "复用": reuse,
            "冲突": copy.deepcopy(snapshot.get("口径冲突")),
        }


def _latest_snapshot(segment_code: Any) -> dict[str, Any] | None:
    snapshots = [
        row for row in store.rows(SNAPSHOT_MODULE) if row.get("画像编号") == segment_code
    ]
    if not snapshots:
        return None
    return max(snapshots, key=lambda row: int(row.get("版本号", 0)))


def _build_conflict(
    previous: dict[str, Any] | None,
    definition: dict[str, Any],
    coverage: int,
    interval_text: str,
) -> dict[str, Any] | None:
    """历史覆盖数与现行规则对不上时，明确记录一次权衡而不是悄悄覆盖。"""
    if previous is None:
        return None
    rule_changed = previous.get("规则指纹") != definition["规则指纹"]
    interval_changed = previous.get("数据区间") != interval_text
    baseline_changed = previous.get("基准规则指纹") != definition["基准规则指纹"]
    if not (rule_changed or interval_changed or baseline_changed):
        return None

    historical = int(previous.get("目标覆盖对象数") or 0)
    changed_parts = [
        name
        for name, flag in (
            ("筛选规则", rule_changed),
            ("数据区间", interval_changed),
            ("对比基准", baseline_changed),
        )
        if flag
    ]
    return {
        "类型": "历史覆盖数与现行定义不一致",
        "变化项": changed_parts,
        "历史版本": previous.get("版本号"),
        "历史规则指纹": previous.get("规则指纹"),
        "历史数据区间": previous.get("数据区间"),
        "历史覆盖对象数": historical,
        "现行规则指纹": definition["规则指纹"],
        "现行数据区间": interval_text,
        "现行覆盖对象数": coverage,
        "处理决定": "按现行定义在同一数据区间重算，历史覆盖数只作为版本留痕，不进入新对比结果",
        "权衡依据": (
            "覆盖对象数一旦跨规则或跨区间直接沿用，就会让画像差异两侧来自不同口径；"
            "因此选择牺牲历史数值的连续性，保留版本追溯能力，换取同区间、同口径的可比性"
        ),
        "同区间判定": {
            "目标侧区间": interval_text,
            "基准侧区间": interval_text,
            "结论": "一致",
        },
    }


def _insert_snapshot(
    profile: dict[str, Any],
    definition: dict[str, Any],
    coverage: int,
    baseline_coverage: int,
    distribution: list[dict[str, Any]],
    difference: dict[str, Any],
    *,
    trigger: str,
    interval_source: str,
    idempotency_key: str | None,
    previous: dict[str, Any] | None,
    conflict: dict[str, Any] | None,
    solidify: bool,
) -> dict[str, Any]:
    for row in store.rows(SNAPSHOT_MODULE):
        if row.get("画像编号") == profile.get("画像编号") and row.get("状态") == "有效":
            row["状态"] = "历史"
    version = max(
        (
            int(row.get("版本号", 0))
            for row in store.rows(SNAPSHOT_MODULE)
            if row.get("画像编号") == profile.get("画像编号")
        ),
        default=0,
    ) + 1
    snapshot = {
        "id": store.next_id(SNAPSHOT_MODULE),
        "快照编号": "",
        "画像编号": profile.get("画像编号"),
        "分群名称": profile.get("分群名称"),
        "版本号": version,
        "规则文本": definition["筛选规则"],
        "规则指纹": definition["规则指纹"],
        "画像维度": definition["画像维度"],
        "画像维度文本": definition["画像维度文本"],
        "对比基准": definition["对比基准"],
        "基准类型": definition["基准类型"],
        "基准标识": definition["基准标识"],
        "基准规则指纹": definition["基准规则指纹"],
        "更新周期": definition["更新周期"],
        "数据区间": definition["数据区间"],
        "区间来源": interval_source,
        "人群数据版本": population.POPULATION_VERSION,
        "目标覆盖对象数": coverage,
        "基准覆盖对象数": baseline_coverage,
        "覆盖差异": coverage - baseline_coverage,
        "维度分布": distribution,
        "画像差异": difference,
        "状态": "有效",
        "是否固化": solidify,
        "固化时间": now_text() if solidify else None,
        "触发方式": trigger,
        "幂等键": idempotency_key,
        "生成时间": now_text(),
        "前序版本": previous.get("版本号") if previous else None,
        "口径冲突": conflict,
    }
    snapshot["快照编号"] = f"SNAP-{snapshot['id']:06d}"
    store.rows(SNAPSHOT_MODULE).append(snapshot)
    return snapshot


def _source_rows(
    snapshot: dict[str, Any],
    definition: dict[str, Any],
    target_rows: Sequence[dict[str, Any]],
    baseline_rows: Sequence[dict[str, Any]],
    interval_text: str,
) -> list[dict[str, Any]]:
    """来源记录：把快照引用的原始数据、规则、基准逐条留痕。"""
    rows: list[dict[str, Any]] = [
        {
            "快照编号": snapshot["快照编号"],
            "来源类型": "目标人群数据",
            "来源标识": population.POPULATION_VERSION,
            "数据版本": population.POPULATION_VERSION,
            "规则指纹": "ALL",
            "数据区间": interval_text,
            "记录数": len(target_rows),
            "摘要": f"区间内人群记录 {len(target_rows)} 条，按现行规则取目标分群",
        },
        {
            "快照编号": snapshot["快照编号"],
            "来源类型": "分群筛选规则",
            "来源标识": definition["规则指纹"],
            "数据版本": definition["画像维度文本"],
            "规则指纹": definition["规则指纹"],
            "数据区间": interval_text,
            "记录数": len(target_rows),
            "摘要": definition["筛选规则"],
        },
        {
            "快照编号": snapshot["快照编号"],
            "来源类型": "对比基准",
            "来源标识": definition["基准标识"],
            "数据版本": definition["基准类型"],
            "规则指纹": definition["基准规则指纹"],
            "数据区间": interval_text,
            "记录数": len(baseline_rows),
            "摘要": f"基准按「{definition['对比基准']}」在同一区间取数",
        },
    ]
    if definition.get("预设编号"):
        rows.append(
            {
                "快照编号": snapshot["快照编号"],
                "来源类型": "筛选预设",
                "来源标识": definition["预设编号"],
                "数据版本": f"v{definition.get('预设版本')}",
                "规则指纹": definition["预设规则指纹"],
                "数据区间": interval_text,
                "记录数": len(target_rows),
                "摘要": f"筛选规则取自预设 {definition['预设编号']} 第 {definition.get('预设版本')} 版",
            }
        )
    return rows


def _insert_sources(
    snapshot: dict[str, Any],
    definition: dict[str, Any],
    target_rows: Sequence[dict[str, Any]],
    baseline_rows: Sequence[dict[str, Any]],
    interval_text: str,
) -> None:
    for row in _source_rows(snapshot, definition, target_rows, baseline_rows, interval_text):
        row["id"] = store.next_id(SOURCE_MODULE)
        row["生成时间"] = now_text()
        store.rows(SOURCE_MODULE).append(row)


def _apply_snapshot_to_profile(
    profile: dict[str, Any],
    snapshot: dict[str, Any],
    definition: dict[str, Any],
) -> None:
    """画像行只放快照指针与派生值，派生字段不接受外部直接写入。"""
    profile["画像编号"] = definition["画像编号"] or profile.get("画像编号")
    profile["分群名称"] = definition["分群名称"] or profile.get("分群名称")
    profile["筛选规则"] = definition["筛选规则"]
    profile["画像维度"] = definition["画像维度文本"]
    profile["对比基准"] = definition["对比基准"]
    profile["更新周期"] = definition["更新周期"]
    profile["预设编号"] = definition.get("预设编号")
    profile["数据区间"] = snapshot["数据区间"]
    profile["区间来源"] = definition["区间来源"]
    profile["覆盖对象数"] = snapshot["目标覆盖对象数"]
    profile["画像快照"] = snapshot["快照编号"]
    profile["快照版本"] = snapshot["版本号"]
    profile["规则指纹"] = snapshot["规则指纹"]
    profile["基准规则指纹"] = snapshot["基准规则指纹"]
    profile["人群数据版本"] = population.POPULATION_VERSION


def ensure_profile_snapshot(profile: dict[str, Any]) -> dict[str, Any] | None:
    """启动时补齐示例画像的快照；规则不合法的示例只跳过，不影响服务启动。"""
    try:
        with store.transaction():
            result = compute_snapshot(
                int(profile["id"]),
                trigger="启动补齐",
                solidify=profile.get("status") == "已发布",
            )
            return result["snapshot"]
    except DefinitionError:
        return None


# ---------------------------------------------------------------------------
# 读取接口
# ---------------------------------------------------------------------------


def list_snapshots(segment_code: str, *, limit: int = 50) -> list[dict[str, Any]]:
    rows = [
        copy.deepcopy(row)
        for row in store.rows(SNAPSHOT_MODULE)
        if row.get("画像编号") == segment_code
    ]
    rows.sort(key=lambda row: int(row.get("版本号", 0)), reverse=True)
    return rows[:limit]


def get_snapshot(snapshot_id: int) -> dict[str, Any] | None:
    for row in store.rows(SNAPSHOT_MODULE):
        if int(row.get("id", 0)) == snapshot_id:
            detail = copy.deepcopy(row)
            detail["来源记录"] = sources_for(detail["快照编号"])
            return detail
    return None


def get_snapshot_by_code(snapshot_code: str) -> dict[str, Any] | None:
    for row in store.rows(SNAPSHOT_MODULE):
        if row.get("快照编号") == snapshot_code:
            detail = copy.deepcopy(row)
            detail["来源记录"] = sources_for(snapshot_code)
            return detail
    return None


def sources_for(snapshot_code: str) -> list[dict[str, Any]]:
    return [
        copy.deepcopy(row)
        for row in store.rows(SOURCE_MODULE)
        if row.get("快照编号") == snapshot_code
    ]


def latest_snapshot_for(segment_code: str) -> dict[str, Any] | None:
    return _latest_snapshot(segment_code)


def snapshot_public_view(snapshot: dict[str, Any]) -> dict[str, Any]:
    """给事件分析等消费方的最小视图：不带内部 id，保留可追溯字段。"""
    return {
        "快照编号": snapshot.get("快照编号"),
        "画像编号": snapshot.get("画像编号"),
        "分群名称": snapshot.get("分群名称"),
        "版本号": snapshot.get("版本号"),
        "规则文本": snapshot.get("规则文本"),
        "规则指纹": snapshot.get("规则指纹"),
        "画像维度": snapshot.get("画像维度"),
        "对比基准": snapshot.get("对比基准"),
        "数据区间": snapshot.get("数据区间"),
        "覆盖对象数": snapshot.get("目标覆盖对象数"),
        "基准覆盖对象数": snapshot.get("基准覆盖对象数"),
        "覆盖差异": snapshot.get("覆盖差异"),
        "画像差异": snapshot.get("画像差异"),
        "维度分布": snapshot.get("维度分布"),
        "是否固化": snapshot.get("是否固化"),
        "生成时间": snapshot.get("生成时间"),
    }


def profile_view(row: dict[str, Any]) -> dict[str, Any]:
    """画像列表/明细的统一视图：补齐快照同步状态、口径漂移原因与可执行动作。"""
    view = copy.deepcopy(row)
    snapshot = latest_snapshot_for(row.get("画像编号"))
    reasons: list[str] = []
    if snapshot is None:
        reasons.append("还没有快照")
    else:
        if snapshot.get("规则指纹") != row.get("规则指纹"):
            reasons.append("现行筛选规则与快照不一致")
        if snapshot.get("数据区间") != row.get("数据区间"):
            reasons.append("现行数据区间与快照不一致")
        if snapshot.get("基准规则指纹") != row.get("基准规则指纹"):
            reasons.append("对比基准口径与快照不一致")
    preset_drift = preset_drift_reason(row)
    if preset_drift:
        reasons.append(preset_drift)
    view["快照状态"] = (
        "缺少快照" if snapshot is None else ("需重算" if reasons else "已同步")
    )
    view["口径提示"] = "；".join(dict.fromkeys(reasons)) if reasons else "口径一致"
    view["可执行动作"] = available_actions(row)
    return view


def preset_drift_reason(row: dict[str, Any]) -> str | None:
    """引用了筛选预设时，检查预设版本是否已经领先于画像保存的口径。"""
    preset_code = _text(row.get("预设编号"))
    if not preset_code:
        return None
    preset = store.find_by(PRESET_MODULE, "预设编号", preset_code)
    if preset is None:
        return f"引用的筛选预设 {preset_code} 已不存在"
    if preset.get("status") == "已废弃":
        return f"引用的筛选预设 {preset_code} 已废弃，需要换用其他预设或显式规则"
    current = population.rule_digest(preset.get("维度条件组"))
    if current != row.get("规则指纹"):
        return (
            f"引用的筛选预设 {preset_code} 已升级到 v{preset.get('版本号')}，"
            "画像规则需要同步到预设现行口径"
        )
    return None


def available_actions(row: dict[str, Any]) -> list[str]:
    status = row.get("status")
    if status == "草稿":
        return ["提交画像评审"]
    if status == "画像评审中":
        return ["发布画像"]
    if status == "已发布":
        return ["下线画像"]
    return []


def normalize_profile_values(values: dict[str, Any]) -> dict[str, Any]:
    """过滤派生字段并展开别名字段，避免调用方直接改写快照指针。"""
    normalized: dict[str, Any] = {}
    for key, value in values.items():
        field = PROFILE_FIELD_ALIASES.get(key, key)
        if field in PROFILE_PAYLOAD_FIELDS:
            normalized[field] = value
    return normalized


def assert_unique_profile_code(code: str, exclude_id: int | None = None) -> None:
    for row in store.rows(PROFILE_MODULE):
        if row.get("画像编号") == code and int(row.get("id", 0)) != int(exclude_id or 0):
            raise DefinitionError(f"画像编号 {code} 已存在，请换一个编号")


def consistency_report() -> dict[str, Any]:
    """口径核对：画像行、快照、来源记录与事件分析消费方是否指向同一口径。

    覆盖数残留、规则丢失、快照指针错位都会在这里暴露出来，供列表和事件详情对账。
    """
    issues: list[dict[str, Any]] = []
    snapshot_codes = {row.get("快照编号") for row in store.rows(SNAPSHOT_MODULE)}
    source_codes = {row.get("快照编号") for row in store.rows(SOURCE_MODULE)}

    for row in store.rows(PROFILE_MODULE):
        code = row.get("画像编号")
        snapshot = _latest_snapshot(code)
        if snapshot is None:
            issues.append({"画像编号": code, "问题": "缺少快照", "说明": "画像没有可追溯的覆盖对象数来源"})
            continue
        if row.get("画像快照") != snapshot["快照编号"]:
            issues.append(
                {
                    "画像编号": code,
                    "问题": "快照指针错位",
                    "说明": f"画像指向 {row.get('画像快照')}，最新快照为 {snapshot['快照编号']}",
                }
            )
        if row.get("覆盖对象数") != snapshot["目标覆盖对象数"]:
            issues.append(
                {
                    "画像编号": code,
                    "问题": "覆盖数残留",
                    "说明": f"画像记录 {row.get('覆盖对象数')}，快照为 {snapshot['目标覆盖对象数']}",
                }
            )
        if row.get("规则指纹") != snapshot["规则指纹"]:
            issues.append(
                {"画像编号": code, "问题": "规则丢失", "说明": "画像规则指纹与快照不一致"}
            )
        drift = preset_drift_reason(row)
        if drift:
            issues.append({"画像编号": code, "问题": "预设口径已变更", "说明": drift})
        if snapshot["快照编号"] not in source_codes:
            issues.append(
                {"画像编号": code, "问题": "来源记录缺失", "说明": f"快照 {snapshot['快照编号']} 没有来源记录"}
            )

    for row in store.rows(SNAPSHOT_MODULE):
        if row.get("快照编号") not in source_codes:
            issues.append(
                {
                    "画像编号": row.get("画像编号"),
                    "问题": "来源记录缺失",
                    "说明": f"快照 {row.get('快照编号')} 没有来源记录",
                }
            )
    for row in store.rows(SOURCE_MODULE):
        if row.get("快照编号") not in snapshot_codes:
            issues.append(
                {"画像编号": "", "问题": "来源记录孤立", "说明": f"来源记录指向不存在的快照 {row.get('快照编号')}"}
            )

    consumers: list[dict[str, Any]] = []
    for row in store.rows("eventanalysis"):
        code = str(row.get("画像编号") or "")
        if not code:
            continue
        segment = store.find_by(PROFILE_MODULE, "画像编号", code)
        snapshot = _latest_snapshot(code)
        synced = snapshot is not None and row.get("快照编号") == snapshot["快照编号"]
        consumers.append(
            {
                "模型编号": row.get("模型编号"),
                "画像编号": code,
                "画像状态": segment.get("status") if segment else "已删除",
                "消费快照": row.get("快照编号"),
                "最新快照": snapshot["快照编号"] if snapshot else None,
                "已同步": synced,
            }
        )
        if snapshot is not None and not synced:
            issues.append(
                {
                    "画像编号": code,
                    "问题": "消费方版本滞后",
                    "说明": f"{row.get('模型编号')} 仍指向 {row.get('快照编号')}，最新为 {snapshot['快照编号']}",
                }
            )

    return {
        "一致": not issues,
        "问题数": len(issues),
        "问题": issues,
        "画像数": len(store.rows(PROFILE_MODULE)),
        "快照数": len(store.rows(SNAPSHOT_MODULE)),
        "来源记录数": len(store.rows(SOURCE_MODULE)),
        "消费方": consumers,
    }


def bootstrap() -> dict[str, Any]:
    """为启动前就存在的画像补齐快照，并把事件分析消费方同步到最新快照。"""
    prepared = 0
    skipped: list[str] = []
    for row in list(store.rows(PROFILE_MODULE)):
        if row.get("画像快照"):
            continue
        snapshot = ensure_profile_snapshot(row)
        if snapshot is None:
            skipped.append(str(row.get("画像编号")))
        else:
            prepared += 1

    from app.services.eventanalysis import EventanalysisService

    consumer = EventanalysisService()
    synced = 0
    for row in store.rows("eventanalysis"):
        segment_code = str(row.get("画像编号") or "")
        if not segment_code:
            continue
        snapshot = _latest_snapshot(segment_code)
        if snapshot is not None and row.get("快照编号") != snapshot["快照编号"]:
            consumer._pin_snapshot(row, snapshot)
            synced += 1
    return {"prepared": prepared, "skipped": skipped, "consumers_synced": synced}
