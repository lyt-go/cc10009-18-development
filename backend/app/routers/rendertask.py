"""渲染任务接口：渲染作业、渲染结果、队列状态与调度落点清单。

路由层只做参数解析与结果包装，所有状态流转、容量判断、等待条件校验和幂等处理
都在 ``app.services.rendertask`` 里完成。

四个数据集各自独立前缀，避免 ``/{entry_id}`` 吞掉 ``/results`` 这类静态路径：

- ``/api/rendertask``          渲染作业
- ``/api/renderresult``       渲染结果
- ``/api/renderqueue``         队列状态
- ``/api/renderlanding``       调度落点侧清单
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.rendertask import (
    ACTION_RULES,
    QUEUE_STATUSES,
    RESULT_STATUSES,
    RendertaskService,
)

router = APIRouter(prefix="/api/rendertask", tags=["渲染任务"])
results_router = APIRouter(prefix="/api/renderresult", tags=["渲染结果"])
queue_router = APIRouter(prefix="/api/renderqueue", tags=["渲染队列"])
landing_router = APIRouter(prefix="/api/renderlanding", tags=["调度落点"])

service = RendertaskService()

LIST_FIELDS = [
    "作业编号",
    "目标URL",
    "浏览器版本",
    "视口宽度",
    "视口高度",
    "等待策略",
    "脚本注入",
    "超时阈值秒",
]
STATUSES = ["草稿", "排队中", "渲染中", "已完成", "已超时"]


class WaitConditionPayload(BaseModel):
    """等待条件校验入参：条件名 + 是否满足 + 证据。"""

    conditions: list[dict[str, Any]] = Field(default_factory=list)


def _page_size(size: int) -> None:
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")


# ----------------------------------------------------------------------
# 渲染作业
# ----------------------------------------------------------------------
@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按作业编号检索"),
    status: str | None = Query(default=None, description="草稿、排队中、渲染中、已完成、已超时"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按作业编号与状态过滤渲染任务列表；没有数据时返回空页，不报错。"""
    _page_size(size)
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出渲染任务清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "rendertask", "total": total, "items": items}


@router.get("/actions")
def list_actions() -> dict[str, Any]:
    """返回渲染作业可执行动作，前端按钮以这里为准。"""
    return {"actions": ACTION_RULES, "statuses": STATUSES, "resultStatuses": RESULT_STATUSES}


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条渲染作业，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段或字段冲突：{'、'.join(missing)}")
    return ActionResult(ok=True, message="渲染作业已登记", entry=entry)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条渲染作业明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"渲染作业 {entry_id} 不存在或已归档")
    return entry


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """执行提交排队、启动渲染、完成渲染、终止渲染与各类异常标记。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


# ----------------------------------------------------------------------
# 渲染结果
# ----------------------------------------------------------------------
@results_router.get("", response_model=PageResult[dict])
def list_results(
    keyword: str | None = Query(default=None, description="按结果编号或作业编号检索"),
    status: str | None = Query(default=None, description="、".join(RESULT_STATUSES)),
    result_code: str | None = Query(default=None, description="按结果编号精确检索"),
    job_code: str | None = Query(default=None, description="按作业编号精确检索"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """渲染结果数据集：同名结果冲突后会同时保留新旧版本，便于复核。"""
    _page_size(size)
    items, total = service.list_results(
        keyword=keyword,
        status=status,
        result_code=result_code,
        job_code=job_code,
        page=page,
        size=size,
    )
    return PageResult(items=items, total=total, page=page, size=size)


@results_router.get("/export")
def export_results() -> dict[str, Any]:
    """导出渲染结果数据集（含等待条件、版本线与诊断信息）。"""
    items, total = service.list_results(page=1, size=10000)
    return {"module": "renderresult", "total": total, "items": items}


@results_router.get("/{result_id}", response_model=dict)
def get_result(result_id: int) -> dict:
    """读取单条渲染结果：等待条件、版本历史、诊断边界都可追溯。"""
    result = service.get_result(result_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"渲染结果 {result_id} 不存在或已归档")
    return result


@results_router.post("/{result_id}/validate-wait", response_model=ActionResult)
def validate_wait(result_id: int, payload: WaitConditionPayload) -> ActionResult:
    """校验等待条件；不传条件时按等待策略顺序推进下一条。"""
    result, message = service.validate_wait(result_id, payload.conditions)
    if result is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=result)


@results_router.post("/{result_id}/finalize", response_model=ActionResult)
def finalize_render(result_id: int) -> ActionResult:
    """等待条件满足且容量确认后生成渲染结果；超时/晚到结果不会写成成功页面。"""
    result, message = service.finalize_render(result_id)
    if result is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=result)


@results_router.post("/{result_id}/process", response_model=ActionResult)
def process_result(result_id: int) -> ActionResult:
    """处理渲染结果并同步调度落点清单；同一结果重复处理只算一次。"""
    result, message = service.process_result(result_id)
    if result is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=result)


@results_router.post("/{result_id}/dispatch", response_model=ActionResult)
def dispatch_result(result_id: int) -> ActionResult:
    """把渲染结果快照同步到调度落点侧清单。"""
    landing, message = service.dispatch_to_landing(result_id)
    if landing is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=landing)


# ----------------------------------------------------------------------
# 队列状态数据集
# ----------------------------------------------------------------------
@queue_router.get("", response_model=PageResult[dict])
def list_queues(
    keyword: str | None = Query(default=None, description="按队列编号或作业编号检索"),
    status: str | None = Query(default=None, description="、".join(QUEUE_STATUSES)),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """队列状态数据集：同一渲染作业只占一个位置，容量占用与回收原因可追溯。"""
    _page_size(size)
    items, total = service.list_queues(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@queue_router.get("/export")
def export_queues() -> dict[str, Any]:
    """导出队列状态数据集。"""
    items, total = service.list_queues(page=1, size=10000)
    return {"module": "renderqueue", "total": total, "items": items}


@queue_router.get("/{queue_id}", response_model=dict)
def get_queue(queue_id: int) -> dict:
    queue = service.get_queue(queue_id)
    if queue is None:
        raise HTTPException(status_code=404, detail=f"队列 {queue_id} 不存在或已归档")
    return queue


@queue_router.post("/{queue_id}/refresh", response_model=ActionResult)
def refresh_queue(queue_id: int) -> ActionResult:
    """从节点侧重新读取容量；节点离线时立即回收队列容量。"""
    queue, message = service.refresh_queue_capacity(queue_id)
    if queue is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=queue)


# ----------------------------------------------------------------------
# 调度落点侧清单
# ----------------------------------------------------------------------
@landing_router.get("", response_model=PageResult[dict])
def list_landing(
    keyword: str | None = Query(default=None, description="按落点编号、作业编号或结果编号检索"),
    status: str | None = Query(default=None, description="已占用、已释放、已同步"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """调度落点清单：读取队列状态、回写容量占用、接收渲染结果快照。"""
    _page_size(size)
    items, total = service.list_landing(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@landing_router.get("/export")
def export_landing() -> dict[str, Any]:
    """导出调度落点清单（含结果快照与已刷新业务记录）。"""
    items, total = service.list_landing(page=1, size=10000)
    return {"module": "renderlanding", "total": total, "items": items}


@landing_router.get("/{landing_id}", response_model=dict)
def get_landing(landing_id: int) -> dict:
    landing = service.get_landing(landing_id)
    if landing is None:
        raise HTTPException(status_code=404, detail=f"落点记录 {landing_id} 不存在或已归档")
    return landing
