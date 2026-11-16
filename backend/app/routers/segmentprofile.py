"""分群画像接口：维护画像定义，覆盖保存定义、重算覆盖数、快照、差异与状态动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services import profilechain
from app.services.segmentprofile import SegmentprofileService

router = APIRouter(prefix="/api/segmentprofile", tags=["分群画像"])

service = SegmentprofileService()

LIST_FIELDS = ["画像编号", "分群名称", "筛选规则", "画像维度", "对比基准", "更新周期", "覆盖对象数", "画像快照"]
STATUSES = ["草稿", "画像评审中", "已发布", "已下线"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按画像编号或分群名称检索"),
    status: str | None = Query(default=None, description="草稿、画像评审中、已发布、已下线"),
    snapshot_status: str | None = Query(default=None, description="已同步、需重算、缺少快照、画像不可用"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按编号、状态与快照同步状态过滤画像列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(
        keyword=keyword, status=status, snapshot_status=snapshot_status, page=page, size=size
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/options")
def options() -> dict[str, Any]:
    """画像定义可选项：维度、周期、基准候选、预设与人群数据口径来源。"""
    presets = []
    for preset in profilechain.store.rows(profilechain.PRESET_MODULE):
        if preset.get("status") == "已废弃":
            continue
        presets.append(
            {
                "预设编号": preset.get("预设编号"),
                "预设名称": preset.get("预设名称"),
                "状态": preset.get("status"),
                "版本号": preset.get("版本号"),
                "规则文本": profilechain.preset_definition(preset)["规则文本"],
            }
        )
    baselines = ["全量人群"] + [f"预设:{preset['预设编号']}" for preset in presets]
    baselines += [
        f"画像:{row.get('画像编号')}"
        for row in profilechain.store.rows(profilechain.PROFILE_MODULE)
        if row.get("status") == "已发布"
    ]
    return {
        "画像维度": list(profilechain.population.DIMENSIONS),
        "更新周期": list(profilechain.population.CYCLE_DAYS),
        "对比基准候选": baselines,
        "筛选预设": presets,
        "人群数据": profilechain.population.population_summary(),
    }


@router.get("/audit")
def audit() -> dict[str, Any]:
    """口径核对：画像、快照、来源记录与事件分析消费方是否一致，暴露覆盖数残留与规则丢失。"""
    return profilechain.consistency_report()


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出分群画像清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "segmentprofile", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条画像定义明细，附带快照历史；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"画像定义 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记画像定义：规则、维度、基准、周期与首个快照一起保存。"""
    entry, missing, message = service.create_entry(payload.values)
    if entry is None:
        if missing:
            return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.put("/{entry_id}", response_model=ActionResult)
def update_entry(entry_id: int, payload: EntryPayload) -> ActionResult:
    """修改画像定义并按时口径重算覆盖对象数；已发布画像不可直接回退。"""
    entry, message = service.update_entry(entry_id, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/{entry_id}/snapshots")
def list_snapshots(entry_id: int) -> dict[str, Any]:
    """画像快照版本列表：按版本倒序，保留每次计算的规则、区间与覆盖数。"""
    items, message = service.snapshot_history(entry_id)
    if message:
        raise HTTPException(status_code=404, detail=message)
    return {"items": items, "total": len(items)}


@router.get("/{entry_id}/difference")
def get_difference(entry_id: int, interval: str | None = Query(default=None)) -> dict[str, Any]:
    """画像差异：两侧同区间取数，返回覆盖差异、维度差异与来源记录。"""
    result, message = service.difference(entry_id, interval=interval)
    if result is None:
        raise HTTPException(status_code=400, detail=message)
    return result


@router.get("/{entry_id}/sources")
def get_sources(entry_id: int) -> dict[str, Any]:
    """画像来源记录：最新快照引用的目标人群、筛选规则、基准与预设版本。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"画像定义 {entry_id} 不存在或已归档")
    snapshot = profilechain.latest_snapshot_for(str(entry.get("画像编号")))
    if snapshot is None:
        return {"items": [], "total": 0, "message": "画像还没有快照"}
    items = profilechain.sources_for(snapshot["快照编号"])
    return {"snapshot": profilechain.snapshot_public_view(snapshot), "items": items, "total": len(items)}


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """执行重算覆盖数、生成快照、提交评审、发布、下线等动作。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
