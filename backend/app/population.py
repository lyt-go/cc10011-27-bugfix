"""目标人群样例数据与筛选口径：分群画像、筛选预设、事件分析共用这一份取数逻辑。

这一层只做纯计算，不读写任何业务表：
- 生成确定性的目标人群样例（同一份数据、同一套规则，任何入口算出来的覆盖对象数都一致）；
- 解析筛选规则（字符串或结构化条件），给出规范化文本与指纹；
- 解析数据区间，按区间 + 规则取人群；
- 计算画像维度分布与两侧画像差异。

各业务服务在这一层之上落库版本与快照，避免每个入口各写一套口径。
"""
from __future__ import annotations

import hashlib
import random
import re
from datetime import date, timedelta
from typing import Any, Iterable, Sequence

# 人群数据版本：人群数据本身发生变化时必须换版本号，快照指纹会把它算进去。
POPULATION_VERSION = "pop-2026.09-v1"

# 数据锚点：样例人群最近活跃日期截止到这一天，数据区间都以它为基准向后推导。
LATEST_DATE = date(2026, 9, 15)

DIMENSIONS: tuple[str, ...] = ("城市", "会员等级", "活跃度", "注册渠道", "设备类型")
NUMERIC_FIELDS: frozenset[str] = frozenset({"消费金额"})
DATE_FIELDS: frozenset[str] = frozenset({"最近活跃日期"})
SUPPORTED_FIELDS: frozenset[str] = frozenset(DIMENSIONS) | NUMERIC_FIELDS | DATE_FIELDS | {"对象编号"}

DEFAULT_DIMENSIONS: tuple[str, ...] = ("城市", "会员等级")
DEFAULT_CYCLE = "按周"
DEFAULT_BASELINE = "全量人群"

CYCLE_DAYS = {
    "按日": 1,
    "每日": 1,
    "日更": 1,
    "按周": 7,
    "每周": 7,
    "周更": 7,
    "按月": 30,
    "每月": 30,
    "月更": 30,
}

_OPERATOR_ALIASES = {
    "not in": "not in",
    "not_in": "not in",
    "不属于": "not in",
    "属于": "in",
    "in": "in",
    "包含": "包含",
    "不包含": "不包含",
    "等于": "=",
    "不等于": "!=",
    ">=": ">=",
    "<=": "<=",
    "!=": "!=",
    "<>": "!=",
    "==": "=",
    "=": "=",
    ">": ">",
    "<": "<",
}
_OPERATOR_PATTERN = "|".join(
    re.escape(op) for op in sorted(_OPERATOR_ALIASES, key=len, reverse=True)
)
_CONDITION_PATTERN = re.compile(
    rf"^\s*(?P<field>[^\s=!<>]+)\s*(?P<op>{_OPERATOR_PATTERN})\s*(?P<value>.+?)\s*$"
)
_VALUE_SPLIT = re.compile(r"[,，、|]")


class RuleError(ValueError):
    """筛选规则或数据区间不合法时抛出，调用方转成可读的提示，不静默吞掉。"""


# ---------------------------------------------------------------------------
# 人群样例数据
# ---------------------------------------------------------------------------

_CITY_WEIGHTS = (("杭州", 24), ("上海", 21), ("北京", 19), ("深圳", 18), ("成都", 18))
_LEVEL_WEIGHTS = (("金卡", 22), ("银卡", 38), ("普通", 40))
_ACTIVITY_WEIGHTS = (("高活跃", 32), ("中活跃", 43), ("低活跃", 25))
_CHANNEL_WEIGHTS = (("自然流量", 34), ("广告投放", 36), ("社交裂变", 30))
_DEVICE_WEIGHTS = (("iOS", 31), ("Android", 47), ("Web", 22))


def _weighted(rng: random.Random, pairs: Sequence[tuple[str, int]]) -> str:
    values, weights = zip(*pairs)
    return rng.choices(values, weights=weights, k=1)[0]


def build_population(size: int = 900) -> list[dict[str, Any]]:
    """生成确定性的人群样例：固定随机种子，任何进程算出来的结果完全一致。"""
    rng = random.Random(20260915)
    rows: list[dict[str, Any]] = []
    for index in range(1, size + 1):
        day = LATEST_DATE - timedelta(days=rng.randint(0, 45))
        amount = round(min(rng.lognormvariate(6.0, 0.85), 6000.0), 2)
        rows.append(
            {
                "对象编号": f"USER-{index:04d}",
                "城市": _weighted(rng, _CITY_WEIGHTS),
                "会员等级": _weighted(rng, _LEVEL_WEIGHTS),
                "活跃度": _weighted(rng, _ACTIVITY_WEIGHTS),
                "注册渠道": _weighted(rng, _CHANNEL_WEIGHTS),
                "设备类型": _weighted(rng, _DEVICE_WEIGHTS),
                "消费金额": amount,
                "最近活跃日期": day.isoformat(),
            }
        )
    return rows


POPULATION: tuple[dict[str, Any], ...] = tuple(build_population())


def population_summary() -> dict[str, Any]:
    """人群数据概况，供页面展示口径来源。"""
    return {
        "数据版本": POPULATION_VERSION,
        "对象总数": len(POPULATION),
        "可选维度": list(DIMENSIONS),
        "数值字段": sorted(NUMERIC_FIELDS),
        "数据锚点": LATEST_DATE.isoformat(),
        "默认数据区间": resolve_interval(),
    }


def in_interval(record: dict[str, Any], interval: str) -> bool:
    start, end = parse_interval(interval)
    day = str(record.get("最近活跃日期", ""))
    return start.isoformat() <= day <= end.isoformat()


def select(rule: str | Sequence[dict[str, Any]] | None, interval: str) -> list[dict[str, Any]]:
    """按数据区间与筛选规则取目标人群；规则为空表示全量人群。"""
    parse_interval(interval)
    rows = [row for row in POPULATION if in_interval(row, interval)]
    if not rule:
        return rows
    conditions = normalize_conditions(parse_rule(rule))
    return [row for row in rows if matches(row, conditions)]


# ---------------------------------------------------------------------------
# 数据区间
# ---------------------------------------------------------------------------


def parse_interval(interval: str) -> tuple[date, date]:
    """解析 ``2026-09-09~2026-09-15`` 形式的数据区间（也接受 至 / , 分隔）。"""
    text = str(interval or "").strip().replace("至", "~").replace("—", "~").replace(",", "~")
    parts = [part.strip() for part in text.split("~") if part.strip()]
    if len(parts) != 2:
        raise RuleError("数据区间格式应为 2026-09-09~2026-09-15")
    try:
        start = date.fromisoformat(parts[0])
        end = date.fromisoformat(parts[1])
    except ValueError as exc:
        raise RuleError("数据区间必须使用 YYYY-MM-DD 日期") from exc
    if start > end:
        raise RuleError("数据区间起始日期不能晚于结束日期")
    if (end - start).days > 92:
        raise RuleError("数据区间最长 92 天，请缩小统计范围")
    return start, end


def resolve_interval(cycle: str | None = None, explicit: str | None = None) -> str:
    """把更新周期或显式区间解析成统一的数据区间文本。

    同一个画像的覆盖对象数与画像差异都必须落在这个区间里，事件分析消费快照时
    也用它作为统一区间，避免旧覆盖数混进新对比。
    """
    if explicit is not None and str(explicit).strip():
        start, end = parse_interval(str(explicit))
        return f"{start.isoformat()}~{end.isoformat()}"
    cycle_text = str(cycle or "").strip()
    days = CYCLE_DAYS.get(cycle_text, 7)
    if cycle_text in {"按月", "每月", "月更"}:
        start = LATEST_DATE.replace(day=1)
        end = (start + timedelta(days=31)).replace(day=1) - timedelta(days=1)
    else:
        end = LATEST_DATE
        start = end - timedelta(days=days - 1)
    return f"{start.isoformat()}~{end.isoformat()}"


def interval_for_entry(value: Any) -> str | None:
    """把时间范围字段解析成数据区间；不是区间格式时返回 None，交给更新周期兜底。"""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return resolve_interval(explicit=text)
    except RuleError:
        return None


# ---------------------------------------------------------------------------
# 筛选规则
# ---------------------------------------------------------------------------


def parse_rule(rule: str | Sequence[dict[str, Any]] | dict[str, Any]) -> list[dict[str, Any]]:
    """解析筛选规则，支持 ``字段=值`` / ``字段 in 值1,值2`` 的文本，也接受结构化条件。"""
    if isinstance(rule, dict):
        raw_conditions = rule.get("条件", rule.get("conditions"))
        if raw_conditions is None:
            raise RuleError("筛选规则缺少条件列表")
        return _parse_structured(raw_conditions)
    if isinstance(rule, (list, tuple)):
        return _parse_structured(rule)

    text = str(rule or "").strip()
    if not text:
        raise RuleError("筛选规则不能为空")
    parts = [part.strip() for part in re.split(r"[;；\n]+", text) if part.strip()]
    if not parts:
        raise RuleError("筛选规则里没有可执行的条件")
    conditions: list[dict[str, Any]] = []
    for part in parts:
        match = _CONDITION_PATTERN.match(part)
        if not match:
            raise RuleError(f"筛选条件「{part}」格式应为 字段=取值 或 字段 in 取值1,取值2")
        field = match.group("field").strip()
        raw_op = re.sub(r"\s+", " ", match.group("op").strip().lower())
        operator = _OPERATOR_ALIASES.get(raw_op) or _OPERATOR_ALIASES.get(match.group("op").strip())
        if operator is None:
            raise RuleError(f"筛选条件「{part}」里的运算符不受支持")
        values = [value.strip() for value in _VALUE_SPLIT.split(match.group("value")) if value.strip()]
        conditions.append({"字段": field, "运算符": operator, "取值": values})
    return normalize_conditions(conditions)


def _parse_structured(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, (list, tuple)):
        raise RuleError("筛选规则的条件下发格式必须是列表")
    conditions: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise RuleError("筛选条件必须是包含 字段/运算符/取值 的对象")
        field = str(item.get("字段") or item.get("field") or "").strip()
        operator = str(item.get("运算符") or item.get("op") or "=").strip().lower()
        operator = _OPERATOR_ALIASES.get(operator, operator)
        if operator not in set(_OPERATOR_ALIASES.values()):
            raise RuleError(f"运算符「{operator}」不受支持")
        value = item.get("取值", item.get("value", ""))
        if isinstance(value, (list, tuple)):
            values = [str(part).strip() for part in value if str(part).strip()]
        else:
            values = [part.strip() for part in _VALUE_SPLIT.split(str(value)) if part.strip()]
        conditions.append({"字段": field, "运算符": operator, "取值": values})
    return normalize_conditions(conditions)


def normalize_conditions(conditions: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for condition in conditions:
        field = str(condition.get("字段") or "").strip()
        operator = str(condition.get("运算符") or "=").strip()
        if not field:
            raise RuleError("筛选条件缺少字段名")
        if field not in SUPPORTED_FIELDS:
            raise RuleError(f"未知筛选字段「{field}」，可用字段：{'、'.join(sorted(SUPPORTED_FIELDS - {'对象编号'}))}")
        if operator not in set(_OPERATOR_ALIASES.values()):
            raise RuleError(f"字段「{field}」的运算符「{operator}」不受支持")
        values: list[str] = []
        for value in condition.get("取值") or []:
            text = str(value).strip()
            if not text:
                continue
            if field in DATE_FIELDS:
                try:
                    text = date.fromisoformat(text).isoformat()
                except ValueError as exc:
                    raise RuleError(f"字段「{field}」需要 YYYY-MM-DD 日期，收到「{text}」") from exc
            values.append(text)
        if field in NUMERIC_FIELDS:
            values = _normalize_numeric_values(field, values)
        if not values:
            raise RuleError(f"筛选条件「{field}」缺少取值")
        normalized.append({"字段": field, "运算符": operator, "取值": values})
    normalized.sort(key=lambda item: (item["字段"], item["运算符"], item["取值"]))
    return normalized


def _normalize_numeric_values(field: str, values: list[str]) -> list[str]:
    normalized: list[str] = []
    for value in values:
        try:
            normalized.append(format(float(value), "g"))
        except ValueError as exc:
            raise RuleError(f"字段「{field}」需要数值，收到「{value}」") from exc
    return normalized


def rule_text(conditions: Sequence[dict[str, Any]]) -> str:
    """规范化后的规则文本：同一口径无论怎么写，文本和指纹都一致。"""
    return "；".join(
        f"{condition['字段']}{condition['运算符']}{','.join(condition['取值'])}"
        for condition in conditions
    )


def rule_digest(rule: str | Sequence[dict[str, Any]] | None) -> str:
    """筛选规则摘要指纹：用于判断快照是否过期、预设与画像是否同一口径。"""
    if not rule:
        return "ALL"
    conditions = normalize_conditions(parse_rule(rule))
    return hashlib.sha1(rule_text(conditions).encode("utf-8")).hexdigest()[:12]


def matches(record: dict[str, Any], conditions: Sequence[dict[str, Any]]) -> bool:
    for condition in conditions:
        if not _match_condition(record, condition):
            return False
    return True


def _match_condition(record: dict[str, Any], condition: dict[str, Any]) -> bool:
    field = condition["字段"]
    operator = condition["运算符"]
    values = condition["取值"]
    actual = record.get(field)
    if actual is None:
        return False
    if field in NUMERIC_FIELDS:
        return _compare_number(float(actual), operator, values)
    actual_text = str(actual)
    if operator in {"=", "!="}:
        equal = actual_text in values
        return equal if operator == "=" else not equal
    if operator == "in":
        return actual_text in values
    if operator == "not in":
        return actual_text not in values
    if operator == "包含":
        return any(value in actual_text for value in values)
    if operator == "不包含":
        return all(value not in actual_text for value in values)
    if operator in {">", ">=", "<", "<="}:
        return _compare_text(actual_text, operator, values[0])
    return False


def _compare_number(actual: float, operator: str, values: Sequence[str]) -> bool:
    numbers = [float(value) for value in values]
    if operator in {"=", "!="}:
        equal = actual in numbers
        return equal if operator == "=" else not equal
    if operator == "in":
        return actual in numbers
    if operator == "not in":
        return actual not in numbers
    if operator in {">", ">=", "<", "<="}:
        return _compare_text(str(actual), operator, values[0], numeric=True)
    return False


def _compare_text(actual: str, operator: str, expected: str, *, numeric: bool = False) -> bool:
    left: Any = float(actual) if numeric else actual
    right: Any = float(expected) if numeric else expected
    if operator == ">":
        return left > right
    if operator == ">=":
        return left >= right
    if operator == "<":
        return left < right
    if operator == "<=":
        return left <= right
    return False


def normalize_dimensions(value: Any) -> list[str]:
    """画像维度：接受列表或逗号分隔文本，空值回落到默认维度。"""
    if isinstance(value, (list, tuple)):
        parts = [str(part).strip() for part in value if str(part).strip()]
    else:
        parts = [part.strip() for part in re.split(r"[,，、|;；]+", str(value or "")) if part.strip()]
    dimensions: list[str] = []
    for part in parts:
        if part not in DIMENSIONS:
            raise RuleError(f"画像维度「{part}」不受支持，可用维度：{'、'.join(DIMENSIONS)}")
        if part not in dimensions:
            dimensions.append(part)
    return dimensions or list(DEFAULT_DIMENSIONS)


def distribute(records: Sequence[dict[str, Any]], dimensions: Sequence[str]) -> list[dict[str, Any]]:
    """按画像维度统计分布，占比保留 4 位小数。"""
    total = len(records) or 1
    output: list[dict[str, Any]] = []
    for dimension in dimensions:
        counts: dict[str, int] = {}
        for record in records:
            key = str(record.get(dimension, "未知"))
            counts[key] = counts.get(key, 0) + 1
        values = [
            {"取值": key, "对象数": count, "占比": round(count / total, 4)}
            for key, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        ]
        output.append({"维度": dimension, "取值分布": values})
    return output


def build_difference(
    target: Sequence[dict[str, Any]],
    baseline: Sequence[dict[str, Any]],
    dimensions: Sequence[str],
    interval: str,
    *,
    notes: Sequence[str] = (),
) -> dict[str, Any]:
    """计算目标分群与对比基准的画像差异；两侧必须落在同一数据区间。"""
    start, end = parse_interval(interval)
    target_ids = {str(record.get("对象编号")) for record in target}
    baseline_ids = {str(record.get("对象编号")) for record in baseline}
    overlap = target_ids & baseline_ids
    union = target_ids | baseline_ids
    target_size = len(target_ids)
    baseline_size = len(baseline_ids)

    dimension_diff = _dimension_difference(target, baseline, dimensions)
    difference: dict[str, Any] = {
        "数据区间": f"{start.isoformat()}~{end.isoformat()}",
        "同区间校验": {
            "目标侧区间": f"{start.isoformat()}~{end.isoformat()}",
            "基准侧区间": f"{start.isoformat()}~{end.isoformat()}",
            "一致": True,
        },
        "目标覆盖对象数": target_size,
        "基准覆盖对象数": baseline_size,
        "覆盖差异": target_size - baseline_size,
        "重叠对象数": len(overlap),
        "目标独有对象数": target_size - len(overlap),
        "基准独有对象数": baseline_size - len(overlap),
        "Jaccard系数": round(len(overlap) / len(union), 4) if union else 0.0,
        "维度差异": dimension_diff,
        "口径说明": "；".join(notes) if notes else "两侧均按同一数据区间与各自现行筛选规则重算，未复用历史覆盖数。",
    }
    return difference


def _dimension_difference(
    target: Sequence[dict[str, Any]],
    baseline: Sequence[dict[str, Any]],
    dimensions: Sequence[str],
) -> list[dict[str, Any]]:
    target_total = len(target) or 1
    baseline_total = len(baseline) or 1
    output: list[dict[str, Any]] = []
    for dimension in dimensions:
        target_counts = _count_values(target, dimension)
        baseline_counts = _count_values(baseline, dimension)
        candidates = sorted(
            set(target_counts) | set(baseline_counts),
            key=lambda value: (-target_counts.get(value, 0), -baseline_counts.get(value, 0), value),
        )[:8]
        values: list[dict[str, Any]] = []
        for value in candidates:
            target_count = target_counts.get(value, 0)
            baseline_count = baseline_counts.get(value, 0)
            target_ratio = target_count / target_total
            baseline_ratio = baseline_count / baseline_total
            values.append(
                {
                    "取值": value,
                    "目标对象数": target_count,
                    "基准对象数": baseline_count,
                    "目标占比": round(target_ratio, 4),
                    "基准占比": round(baseline_ratio, 4),
                    "差异百分点": round((target_ratio - baseline_ratio) * 100, 2),
                }
            )
        output.append({"维度": dimension, "取值差异": values})
    return output


def _count_values(records: Sequence[dict[str, Any]], dimension: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        key = str(record.get(dimension, "未知"))
        counts[key] = counts.get(key, 0) + 1
    return counts
