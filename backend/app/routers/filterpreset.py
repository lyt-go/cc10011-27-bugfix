"""筛选预设接口：维护预设模板，覆盖保存定义、预览口径、评审、发布与废弃等动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.filterpreset import FilterpresetService

router = APIRouter(prefix="/api/filterpreset", tags=["筛选预设"])

service = FilterpresetService()

LIST_FIELDS = ["预设编号", "预设名称", "维度条件组", "时间范围", "排序规则", "显示列", "共享状态", "使用次数"]
STATUSES = ["草稿", "预设评审中", "已发布", "已废弃"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按预设编号或预设名称检索"),
    status: str | None = Query(default=None, description="草稿、预设评审中、已发布、已废弃"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按编号与状态过滤筛选预设列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出筛选预设清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "filterpreset", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条预设模板明细，附带引用它的画像清单。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"预设模板 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条预设模板，缺字段时说明原因而不是静默丢弃。"""
    entry, missing, message = service.create_entry(payload.values)
    if entry is None:
        if missing:
            return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.put("/{entry_id}", response_model=ActionResult)
def update_entry(entry_id: int, payload: EntryPayload) -> ActionResult:
    """修改预设模板；规则变化时推进版本号，已发布预设不可直接改写。"""
    entry, message = service.update_entry(entry_id, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/{entry_id}/preview")
def preview(entry_id: int, interval: str | None = Query(default=None)) -> dict[str, Any]:
    """按预设口径预览覆盖对象数，与画像快照共用同一取数链路。"""
    result, message = service.preview(entry_id, interval=interval)
    if result is None:
        raise HTTPException(status_code=400, detail=message)
    return result


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条预设执行提交评审、发布、废弃；被画像引用时不允许直接废弃。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
