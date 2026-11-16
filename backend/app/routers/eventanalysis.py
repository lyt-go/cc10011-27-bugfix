"""事件分析接口：维护分析模型，消费分群画像快照并同步版本。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.eventanalysis import EventanalysisService

router = APIRouter(prefix="/api/eventanalysis", tags=["事件分析"])

service = EventanalysisService()

LIST_FIELDS = ["模型编号", "事件名称", "分组维度", "度量指标", "时间粒度", "归因窗口", "置信水平", "最小样本量"]
STATUSES = ["草稿", "模型评审中", "已启用", "已归档"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按模型编号或事件名称检索"),
    status: str | None = Query(default=None, description="草稿、模型评审中、已启用、已归档"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按编号与状态过滤事件分析列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/audit")
def audit() -> dict[str, Any]:
    """事件分析侧核对：消费方与画像最新快照是否一致。"""
    from app.services import profilechain

    report = profilechain.consistency_report()
    return {
        "一致": report["一致"],
        "问题数": report["问题数"],
        "问题": [item for item in report["问题"] if "消费方" in item["问题"] or "覆盖数" in item["问题"]],
        "消费方": report["消费方"],
    }


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出事件分析清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "eventanalysis", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条分析模型明细，附带消费的画像快照与来源记录。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"分析模型 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条分析模型；关联画像必须已发布且有快照。"""
    entry, missing, message = service.create_entry(payload.values)
    if entry is None:
        if missing:
            return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.put("/{entry_id}", response_model=ActionResult)
def update_entry(entry_id: int, payload: EntryPayload) -> ActionResult:
    """修改分析模型并重新固化消费的画像快照。"""
    entry, message = service.update_entry(entry_id, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/{entry_id}/comparison")
def get_comparison(entry_id: int, interval: str | None = Query(default=None)) -> dict[str, Any]:
    """事件分析对比结果：覆盖对象数与画像差异来自同一份画像快照。"""
    result, message = service.comparison(entry_id, interval=interval)
    if result is None:
        raise HTTPException(status_code=400, detail=message)
    return result


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """执行提交评审、启用、归档、同步画像快照等动作。"""
    action = str(payload.values.get("action") or "").strip()
    if action == "同步画像快照":
        entry, message = service.sync_snapshot(entry_id)
    else:
        entry, message = service.run_action(entry_id, action, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
