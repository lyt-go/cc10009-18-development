"""渲染结果业务规则：等待条件核验、超时处置、队列容量与幂等交付。

路由层只负责参数与错误信息，下面的规则全部收在这里：

1. **渲染结果**：读取目标 URL、等待策略（含版本）与节点容量，逐条校验等待条件；
   条件全满足且容量已确认才生成成功页面，否则走超时/终止/异常处置，并保留诊断边界。
2. **队列位置**：同一渲染作业重复下发只占一个队列位置（以作业编号为键），
   重新排队沿用原等待策略及其版本；超时释放槽位后，晚到结果不能覆盖较新的成功结果。
3. **幂等处理**：同一份渲染结果反复处理只算一次（按处理键去重）；
   并发处理由模块锁串行，结果版本只增不覆盖，不会分叉。
4. **调度落点**：落点侧读取队列状态并回写容量占用，渲染结果只交给已确认有容量的作业；
   结果快照与处理结果同步到落点清单，关联业务记录一并刷新。
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta
from typing import Any

from app.store import store

RESULT_MODULE = "renderresult"
QUEUE_MODULE = "renderqueue"
LANDING_MODULE = "renderlanding"

RESULT_STATUSES = ["待渲染", "排队中", "渲染中", "已完成", "已超时", "已终止", "已失败"]
QUEUE_STATUSES = ["排队中", "渲染中", "已完成", "已超时", "已终止", "已失败"]
FAILURE_STATUSES = {"已超时", "已终止", "已失败"}
ACTIVE_STATUSES = {"排队中", "渲染中"}

# 诊断类型：等待超时、连接中断、页面异常、版本冲突、容量耗尽。
DIAG_TIMEOUT = "渲染等待超时"
DIAG_CONNECTION = "连接中断"
DIAG_PAGE_ERROR = "页面异常"
DIAG_VERSION_CONFLICT = "渲染版本冲突"
DIAG_CAPACITY = "节点容量耗尽"

WAIT_STRATEGIES: dict[str, list[str]] = {
    "DOM就绪": ["DOM就绪"],
    "DOM就绪且接口返回": ["DOM就绪", "接口返回"],
    "DOM就绪且两次接口返回": ["DOM就绪", "两次接口返回"],
    "网络空闲": ["网络空闲"],
    "选择器可见": ["选择器可见"],
}

ACTION_RULES = [
    "提交排队",
    "启动渲染",
    "完成渲染",
    "终止渲染",
    "标记超时",
    "标记连接中断",
    "标记页面异常",
]

_LOCK = threading.RLock()
_CLOCK_BASE = datetime(2026, 11, 17, 10, 0, 0)
_tick_value = 0


class RenderConflict(Exception):
    """业务规则拒绝当前动作时抛出，路由层转成 ok=false 的可读说明。"""


def _now() -> str:
    global _tick_value
    _tick_value += 1
    return (_CLOCK_BASE + timedelta(seconds=_tick_value)).isoformat(timespec="seconds")


def _next_id(module: str) -> int:
    return max((int(row.get("id", 0)) for row in store.rows(module)), default=0) + 1


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_text(value: Any, fallback: str = "") -> str:
    text = str(value if value is not None else "").strip()
    return text or fallback


class RendertaskService:
    # ------------------------------------------------------------------
    # 渲染作业（沿用脚手架口径，动作会驱动结果与队列）
    # ------------------------------------------------------------------
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows("rendertask")
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("作业编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find("rendertask", entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        required = ["作业编号", "目标URL", "浏览器版本"]
        missing = [field for field in required if not _as_text(values.get(field))]
        if missing:
            return None, missing
        rows = store.rows("rendertask")
        if any(_as_text(row.get("作业编号")) == _as_text(values.get("作业编号")) for row in rows):
            return None, ["作业编号（已存在，请换一个编号）"]
        entry = {"id": _next_id("rendertask")}
        entry.update({field: _as_text(values.get(field)) for field in required})
        entry.update(
            {
                "视口宽度": _as_text(values.get("视口宽度"), "1440"),
                "视口高度": _as_text(values.get("视口高度"), "900"),
                "等待策略": _as_text(values.get("等待策略"), "DOM就绪"),
                "等待策略版本": _int_or_none(values.get("等待策略版本")) or 1,
                "页面版本": _int_or_none(values.get("页面版本")) or 1,
                "节点标识": _as_text(values.get("节点标识"), "WORK-0003"),
                "脚本注入": _as_text(values.get("脚本注入"), "无"),
                "超时阈值秒": _as_text(values.get("超时阈值秒"), "30"),
                "status": "草稿",
                "pending": True,
                "abnormal": False,
            }
        )
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find("rendertask", entry_id)
        if entry is None:
            return None, f"渲染作业 {entry_id} 不存在或已归档"
        try:
            with _LOCK:
                message = self._apply_job_action(entry, action)
        except RenderConflict as exc:
            return None, str(exc)
        return entry, message

    def _apply_job_action(self, entry: dict[str, Any], action: str) -> str:
        job_code = _as_text(entry.get("作业编号"))
        if not job_code:
            raise RenderConflict("渲染作业缺少作业编号，无法排队")

        if action not in ACTION_RULES:
            raise RenderConflict(
                f"动作「{action}」不属于渲染任务可执行范围；可选动作：" + "、".join(ACTION_RULES)
            )

        if action == "提交排队":
            queue = self._queue_for_job(job_code)
            # 同一渲染作业重复下发只算一次：已在队列里且占着槽位时，直接返回原队列位置，
            # 既不新增队列行，也不把已存在的活跃结果再复制一份。
            if queue is not None and queue.get("status") in ACTIVE_STATUSES and queue.get("容量占用"):
                return (
                    f"渲染作业 {job_code} 已在队列 {queue.get('队列编号')} 中，"
                    f"重复下发不新增队列位置（当前位置 {queue.get('队列位置')}）"
                )
            # 队列还在但容量已被回收（例如节点离线）时允许重新占位，位置仍只有一个。
            result = self._create_pending_result(entry, reuse=queue)
            self._enqueue_result(entry, result, reuse=queue)
            entry["status"] = "排队中"
            entry["pending"] = True
            entry["abnormal"] = False
            return f"渲染作业已提交排队，队列位置 {result.get('队列位置')}"

        if action == "启动渲染":
            queue = self._queue_for_job(job_code)
            if queue is None:
                raise RenderConflict(f"渲染作业 {job_code} 尚未排队，不能直接启动渲染")
            if queue.get("status") != "排队中":
                raise RenderConflict(
                    f"渲染作业 {job_code} 当前队列状态为 {queue.get('status')}，不能重复启动"
                )
            result = self._latest_result(job_code)
            if result is not None:
                result["status"] = "渲染中"
                result["pending"] = True
                result["更新时间"] = _now()
            queue["status"] = "渲染中"
            queue["更新时间"] = _now()
            entry["status"] = "渲染中"
            entry["pending"] = True
            entry["abnormal"] = False
            self._sync_landing_from_queue(queue, result=result, reason="启动渲染占用槽位")
            return "渲染作业已开始渲染"

        if action == "完成渲染":
            result = self._latest_result(job_code)
            queue = self._queue_for_job(job_code)
            if result is None or queue is None:
                raise RenderConflict(f"渲染作业 {job_code} 未进入渲染，不能标记完成")
            self._complete_render(result, queue, entry)
            return "渲染作业已完成"

        reason_map = {
            "终止渲染": ("已终止", "用户终止，保留已满足与未满足等待条件", DIAG_PAGE_ERROR),
            "标记超时": ("已超时", "等待超时，结果不得写成成功页面", DIAG_TIMEOUT),
            "标记连接中断": ("已失败", "连接中断，保留可重试的诊断边界", DIAG_CONNECTION),
            "标记页面异常": ("已失败", "页面异常，保留可重试的诊断边界", DIAG_PAGE_ERROR),
        }
        status, reason, kind = reason_map[action]
        result = self._latest_result(job_code)
        queue = self._queue_for_job(job_code)
        if result is None:
            raise RenderConflict(f"渲染作业 {job_code} 还没有结果，无法{action}")
        self._finalize_failure(result, queue, entry, status, action, reason, kind)
        return f"渲染作业已{action}"

    # ------------------------------------------------------------------
    # 渲染结果
    # ------------------------------------------------------------------
    def list_results(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        result_code: str | None = None,
        job_code: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(RESULT_MODULE)
        if keyword:
            rows = [
                row
                for row in rows
                if keyword in _as_text(row.get("结果编号")) or keyword in _as_text(row.get("作业编号"))
            ]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if result_code:
            rows = [row for row in rows if _as_text(row.get("结果编号")) == result_code]
        if job_code:
            rows = [row for row in rows if _as_text(row.get("作业编号")) == job_code]
        rows = sorted(
            rows,
            key=lambda row: (_as_text(row.get("结果编号")), _int_or_none(row.get("结果版本")) or 0),
        )
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_result(self, result_id: int) -> dict[str, Any] | None:
        return store.find(RESULT_MODULE, result_id)

    def validate_wait(
        self, result_id: int, conditions: list[dict[str, Any]] | None = None
    ) -> tuple[dict[str, Any] | None, str]:
        """校验等待条件。

        ``conditions`` 传 ``[{"条件": "接口返回", "满足": true, "证据": "200"}]`` 时按传入结果校验；
        不传则把当前第一条未满足条件推进为满足，方便演示逐步校验。
        已满足/未满足两份清单始终同时保留，终止或失败后也不会丢。
        """
        result = store.find(RESULT_MODULE, result_id)
        if result is None:
            return None, f"渲染结果 {result_id} 不存在或已归档"
        with _LOCK:
            if result.get("status") not in ACTIVE_STATUSES:
                return None, f"渲染结果当前状态为 {result.get('status')}，不能再校验等待条件"

            updates: dict[str, bool] | None = None
            if conditions:
                updates = {
                    _as_text(item.get("条件")): bool(item.get("满足"))
                    for item in conditions
                    if _as_text(item.get("条件"))
                }

            rows = result.setdefault("等待条件", [])
            if updates is None:
                for row in rows:
                    if not row.get("满足"):
                        row["满足"] = True
                        row["校验时间"] = _now()
                        row["证据"] = "按等待策略顺序推进"
                        break
            else:
                for row in rows:
                    name = _as_text(row.get("条件"))
                    if name in updates:
                        row["满足"] = updates[name]
                        row["校验时间"] = _now() if updates[name] else None
                        row["证据"] = "接口校验通过" if updates[name] else "接口校验未满足"

            self._refresh_wait_lists(result)
            result["结果边界"] = (
                "等待条件已全部满足，等待容量确认后生成结果"
                if not result["未满足等待条件"]
                else "等待条件尚未全部满足，结果未生成"
            )
            result["更新时间"] = _now()
            return result, (
                f"等待条件已校验：已满足 {len(result['已满足等待条件'])} 条、"
                f"未满足 {len(result['未满足等待条件'])} 条"
            )

    def _refresh_wait_lists(self, result: dict[str, Any]) -> None:
        rows = result.get("等待条件", [])
        result["已满足等待条件"] = [row["条件"] for row in rows if row.get("满足")]
        result["未满足等待条件"] = [row["条件"] for row in rows if not row.get("满足")]
        result["条件校验时间"] = _now()
        retry = result.setdefault("重试边界", {})
        preserve = retry.setdefault("保留条件", {})
        preserve["已满足等待条件"] = list(result["已满足等待条件"])
        preserve["未满足等待条件"] = list(result["未满足等待条件"])

    def finalize_render(self, result_id: int) -> tuple[dict[str, Any] | None, str]:
        """等待条件满足且容量确认后生成渲染结果。

        两种情况会落在不同边界，需要显式说明：
        - 等待条件刚满足而节点容量同时耗尽：结果停在“待容量”，排队停在“容量边界”。
        - 结果版本落后于已成功的新版本：按晚到结果拦截，不覆盖较新成功结果。
        """
        result = store.find(RESULT_MODULE, result_id)
        if result is None:
            return None, f"渲染结果 {result_id} 不存在或已归档"
        with _LOCK:
            if result.get("status") == "已完成":
                return result, f"渲染结果 {result.get('结果编号')} 已完成，重复提交不会重复生成"
            newer = self._newer_success(_as_text(result.get("结果标识")), result)
            if newer is not None:
                # 晚到结果即使已超时/失败，也要留下“被较新成功结果拦截”的可追溯记录。
                self._mark_late_result(result, newer)
                return None, (
                    f"渲染结果版本 v{result.get('结果版本')} 晚于成功版本 v{newer.get('结果版本')}，"
                    "已按晚到结果拦截，不覆盖较新成功结果"
                )
            if result.get("status") in FAILURE_STATUSES:
                return None, f"渲染结果当前状态为 {result.get('status')}，不能写成成功页面"
            try:
                message = self._complete_render(
                    result, self._queue_for_job(_as_text(result.get("结果标识"))), None
                )
            except RenderConflict as exc:
                return None, str(exc)
            return result, message

    def _complete_render(
        self,
        result: dict[str, Any],
        queue: dict[str, Any] | None,
        entry: dict[str, Any] | None,
    ) -> str:
        """成功结果的唯一收口：等待条件、版本先后、容量确认都过了才写成功页面。"""
        if result.get("未满足等待条件"):
            raise RenderConflict(
                "等待条件尚未满足："
                + "、".join(result["未满足等待条件"])
                + "；结果保持在原边界，不生成成功页面"
            )
        job_code = _as_text(result.get("结果标识"))
        newer = self._newer_success(job_code, result)
        if newer is not None:
            self._mark_late_result(result, newer)
            raise RenderConflict(
                f"渲染结果版本 v{result.get('结果版本')} 晚于成功版本 v{newer.get('结果版本')}，"
                "已按晚到结果拦截，不覆盖较新成功结果"
            )
        if self._page_version_conflict(result):
            entry = self._job_entry(job_code)
            expected = _int_or_none((entry or {}).get("页面版本")) or 0
            self._mark_version_conflict(result, expected_page_version=expected)
            raise RenderConflict(
                f"渲染结果页面版本 v{result.get('页面版本')} 落后于作业当前页面版本 v{expected}，"
                "已保留冲突版本，新旧版本同时可见，不覆盖较新结果"
            )
        if queue is None:
            raise RenderConflict(f"渲染作业 {job_code} 没有队列记录，结果不能交付")
        self._refresh_capacity(queue)
        capacity_ok, capacity_reason = self._capacity_for_render(queue)
        if not capacity_ok:
            self._capacity_exhausted_boundary(result, queue, capacity_reason)
            raise RenderConflict(capacity_reason + "；结果生成与排队状态落在不同边界")
        self._occupy(queue, "渲染结果生成前确认容量")
        result["容量已确认"] = True
        self._finalize_success(result, queue, entry)
        return f"渲染结果 {result.get('结果编号')} 已生成"

    def _capacity_for_render(self, queue: dict[str, Any]) -> tuple[bool, str]:
        node = _as_text(queue.get("节点标识"))
        if not self._node_online(node):
            return False, f"节点 {node} 离线，队列容量已回收，等待节点恢复"
        if queue.get("容量占用") and queue.get("status") in ACTIVE_STATUSES:
            return True, ""
        capacity = self._node_capacity(node)
        occupied = self._occupied_slots(node)
        if occupied >= capacity:
            return False, f"节点 {node} 容量耗尽（{occupied}/{capacity}），结果暂不能生成"
        return True, ""

    def _capacity_exhausted_boundary(
        self, result: dict[str, Any], queue: dict[str, Any], reason: str
    ) -> None:
        result["结果边界"] = "等待条件已满足但节点容量不可用，结果停在待容量边界"
        result["排队边界"] = "容量耗尽，排队停在容量边界"
        result["边界一致"] = False
        result["容量已确认"] = False
        self._append_diagnostic(
            result,
            stage="容量确认",
            kind=DIAG_CAPACITY,
            detail="等待条件刚满足而节点容量同时耗尽，结果生成与排队状态落在不同边界",
            retryable=True,
            boundary=self._diagnostic_boundary(result),
            queue_code=_as_text(queue.get("队列编号")),
        )
        result["更新时间"] = _now()
        queue["abnormal"] = True
        queue["更新时间"] = _now()
        self._sync_landing_from_queue(queue, result=result, reason=reason)

    def _newer_success(self, job_code: str, result: dict[str, Any]) -> dict[str, Any] | None:
        version = _int_or_none(result.get("结果版本")) or 0
        candidates = [
            row
            for row in store.rows(RESULT_MODULE)
            if row is not result
            and _as_text(row.get("结果标识")) == job_code
            and row.get("status") == "已完成"
            and row.get("成功页面")
            and (_int_or_none(row.get("结果版本")) or 0) > version
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda row: _int_or_none(row.get("结果版本")) or 0)

    def _page_version_conflict(self, result: dict[str, Any]) -> bool:
        """同名结果页面版本落后于作业当前页面版本，即视为版本冲突。"""
        entry = self._job_entry(_as_text(result.get("结果标识")))
        if entry is None:
            return False
        current = _int_or_none(entry.get("页面版本")) or 0
        return bool(current) and (_int_or_none(result.get("页面版本")) or 0) < current

    def _mark_version_conflict(self, result: dict[str, Any], *, expected_page_version: int) -> None:
        """版本冲突后保留新旧两个版本，较新版本继续可读。

        冲突后的新版本会在重新排队时生成；当前旧结果停在冲突诊断边界并释放槽位，
        这样结果与排队即使落在不同边界也能各自追溯。
        """
        already = bool(result.get("版本冲突"))
        result["版本冲突"] = True
        result["冲突前版本"] = result.get("冲突前版本") or result.get("结果版本")
        newer_versions = [
            _int_or_none(row.get("结果版本")) or 0
            for row in store.rows(RESULT_MODULE)
            if row is not result
            and _as_text(row.get("结果标识")) == _as_text(result.get("结果标识"))
            and (_int_or_none(row.get("结果版本")) or 0) > (_int_or_none(result.get("结果版本")) or 0)
        ]
        result["冲突后版本"] = max(
            newer_versions + [(_int_or_none(result.get("结果版本")) or 0) + 1]
        )
        if not result.get("成功页面"):
            result["status"] = "已超时"
            result["pending"] = False
            result["abnormal"] = True
            result["页面内容"] = ""
            result["超时处置"] = "版本冲突后旧版本保留"
            result["结果边界"] = (
                f"版本冲突：页面版本 v{result.get('页面版本')} 落后于 v{expected_page_version}，"
                "旧版本按冲突线保留，不写成成功页面"
            )
            result["排队边界"] = "容量边界与结果边界分离，旧版本不覆盖新结果"
            result["边界一致"] = False
            queue = self._queue_for_job(_as_text(result.get("结果标识")))
            if queue is not None and queue.get("容量占用"):
                queue["status"] = "已超时"
                queue["pending"] = False
                queue["abnormal"] = True
                self._release(queue, "版本冲突，旧版本释放槽位")
                result["排队边界"] = "槽位已释放，冲突旧版本出队"
                self._sync_landing_from_queue(queue, result=result, reason="版本冲突释放槽位")
        if not already:
            self._append_diagnostic(
                result,
                stage="结果落库",
                kind=DIAG_VERSION_CONFLICT,
                detail="同名渲染结果页面版本落后，保留冲突前后两个版本，较新版本不被覆盖",
                retryable=True,
                boundary=self._diagnostic_boundary(result),
                queue_code=_as_text(result.get("队列编号")),
            )
        result["重试边界"].update(
            {
                "可重试": True,
                "重试令牌": f"RT-{result.get('结果编号')}-V{result.get('结果版本')}",
                "说明": "按原等待策略及版本重新排队；冲突版本只保留诊断，不覆盖较新结果",
            }
        )
        result["更新时间"] = _now()

    def _mark_late_result(self, result: dict[str, Any], newer: dict[str, Any]) -> None:
        already_recorded = any(
            item.get("较新结果版本") == newer.get("结果版本") for item in result.get("晚到结果", [])
        )
        result["status"] = "已超时"
        result["pending"] = False
        result["abnormal"] = True
        result["成功页面"] = False
        result["页面内容"] = ""
        result["超时处置"] = "晚到结果拦截，仅保留诊断"
        result["结果边界"] = "晚到结果被较新的成功结果拦截，不覆盖新结果"
        result["排队边界"] = "槽位已释放，晚到结果不入队"
        result["边界一致"] = False
        if not already_recorded:
            result["晚到结果"].append(
                {
                    "结果版本": result.get("结果版本"),
                    "较新结果版本": newer.get("结果版本"),
                    "处置": "保留为诊断版本，不写入成功页面",
                    "时间": _now(),
                }
            )
            self._append_diagnostic(
                result,
                stage="结果交付",
                kind=DIAG_TIMEOUT,
                detail="超时释放槽位后晚到结果尝试覆盖较新成功结果，已拦截",
                retryable=False,
                boundary=self._diagnostic_boundary(result),
                queue_code=_as_text(result.get("队列编号")),
            )
        queue = self._queue_for_job(_as_text(result.get("结果标识")))
        if queue is not None:
            if queue.get("容量占用") and queue.get("status") in ACTIVE_STATUSES:
                # 晚到版本不再推进，占着的槽位必须回收，否则会长期漏占容量。
                queue["status"] = "已超时"
                queue["pending"] = False
                queue["abnormal"] = True
                self._release(queue, "晚到结果被拦截，回收槽位")
            self._sync_landing_from_queue(queue, result=result, reason="晚到结果拦截")
        result["更新时间"] = _now()

    def _finalize_success(
        self,
        result: dict[str, Any],
        queue: dict[str, Any],
        entry: dict[str, Any] | None,
    ) -> None:
        job_code = _as_text(result.get("结果标识"))
        result["status"] = "已完成"
        result["成功页面"] = True
        result["pending"] = False
        result["abnormal"] = False
        result["超时处置"] = "未超时"
        result["容量已确认"] = True
        result["页面内容"] = (
            f"<html><body>{job_code} 渲染成功页面"
            f"（页面版本 v{result.get('页面版本')}）</body></html>"
        )
        result["结果边界"] = "等待条件全部满足且容量已确认，结果已交付"
        result["排队边界"] = "槽位已释放，作业已完成"
        result["边界一致"] = True
        result["快照引用"] = f"SNAP-{result.get('结果编号')}-V{result.get('结果版本')}"
        result["快照状态"] = "已同步"
        result["更新时间"] = _now()
        result["版本历史"].append(
            {
                "结果版本": result.get("结果版本"),
                "状态": "已完成",
                "结果边界": result["结果边界"],
                "排队边界": result["排队边界"],
                "版本原因": "等待条件满足且容量确认后生成成功页面",
                "页面版本": result.get("页面版本"),
                "成功页面": True,
                "生成时间": _now(),
            }
        )
        self._release(queue, "结果交付完成后释放")
        queue["status"] = "已完成"
        queue["pending"] = False
        queue["abnormal"] = False
        queue["结果编号"] = result.get("结果编号")
        queue["结果版本"] = result.get("结果版本")
        queue["更新时间"] = _now()
        if entry is not None:
            entry["status"] = "已完成"
            entry["pending"] = False
            entry["abnormal"] = False
        self._sync_landing_from_result(result, queue, reason="渲染结果成功交付")

    def _finalize_failure(
        self,
        result: dict[str, Any],
        queue: dict[str, Any] | None,
        entry: dict[str, Any] | None,
        status: str,
        action: str,
        reason: str,
        kind: str,
    ) -> None:
        if result.get("成功页面"):
            raise RenderConflict("已生成成功页面的结果不能再改成超时或终止状态")
        result["status"] = status
        result["pending"] = False
        result["abnormal"] = True
        result["成功页面"] = False
        result["页面内容"] = ""
        result["超时处置"] = "释放槽位并保留诊断" if status == "已超时" else reason
        result["结果边界"] = f"{reason}；结果不写成成功页面"
        result["版本历史"].append(
            {
                "结果版本": result.get("结果版本"),
                "状态": status,
                "结果边界": result["结果边界"],
                "排队边界": "槽位释放，作业出队",
                "版本原因": reason,
                "页面版本": result.get("页面版本"),
                "成功页面": False,
                "生成时间": _now(),
            }
        )
        retry = result.setdefault("重试边界", {})
        retry.update(
            {
                "可重试": True,
                "重试令牌": f"RT-{result.get('结果编号')}-V{result.get('结果版本')}",
                "已尝试次数": result.get("尝试次数") or 1,
                "沿用等待策略": result.get("等待策略"),
                "等待策略版本": result.get("等待策略版本"),
                "页面版本": result.get("页面版本"),
                "说明": "按原等待策略及版本重新排队，保留已满足与未满足条件",
            }
        )
        preserve = retry.setdefault("保留条件", {})
        preserve.update(
            {
                "作业编号": result.get("作业编号"),
                "队列编号": result.get("队列编号"),
                "已满足等待条件": list(result.get("已满足等待条件") or []),
                "未满足等待条件": list(result.get("未满足等待条件") or []),
                "等待策略": result.get("等待策略"),
                "等待策略版本": result.get("等待策略版本"),
                "页面版本": result.get("页面版本"),
            }
        )
        self._append_diagnostic(
            result,
            stage="等待页面就绪",
            kind=kind,
            detail=reason,
            retryable=True,
            boundary=self._diagnostic_boundary(result),
            queue_code=_as_text((queue or {}).get("队列编号")),
        )
        if queue is not None:
            queue["status"] = status
            queue["pending"] = False
            queue["abnormal"] = True
            queue["结果编号"] = result.get("结果编号")
            queue["结果版本"] = result.get("结果版本")
            self._release(queue, reason)
            result["排队边界"] = "槽位已释放，作业已出队"
            self._sync_landing_from_queue(queue, result=result, reason=reason)
        else:
            result["排队边界"] = "无队列记录"
        result["边界一致"] = True
        result["更新时间"] = _now()
        self._register_retry_entry(result, kind, reason)
        if entry is not None:
            entry["status"] = status
            entry["pending"] = False
            entry["abnormal"] = True

    def _register_retry_entry(self, result: dict[str, Any], kind: str, reason: str) -> None:
        """把可重试的诊断边界登记到重试队列，复核人员可从重试队列反查渲染结果。

        同一结果版本只登记一次，避免反复标记异常把重试队列刷出重复条目。
        """
        token = _as_text(result.get("重试边界", {}).get("重试令牌"))
        if not token:
            return
        rows = store.rows("retryqueue")
        if any(_as_text(row.get("重试令牌")) == token for row in rows):
            return
        rows.append(
            {
                "id": _next_id("retryqueue"),
                "status": "等待重试",
                "pending": True,
                "abnormal": True,
                "条目编号": f"RETR-{9000 + len([r for r in rows if _as_text(r.get('条目编号')).startswith('RETR-9')]) + 1:04d}",
                "失败URL": result.get("目标URL"),
                "失败原因码": kind,
                "首次失败时间": _now(),
                "已重试次数": max((_int_or_none(result.get("尝试次数")) or 1) - 1, 0),
                "下次重试时间": None,
                "退避级别": "原等待策略",
                "最终处置": "待重试",
                "重试令牌": token,
                "关联结果编号": result.get("结果编号"),
                "关联结果版本": result.get("结果版本"),
                "保留边界": result.get("重试边界", {}).get("保留条件"),
                "说明": reason,
            }
        )

    def _append_diagnostic(
        self,
        result: dict[str, Any],
        *,
        stage: str,
        kind: str,
        detail: str,
        retryable: bool,
        boundary: dict[str, Any],
        queue_code: str,
    ) -> None:
        diagnostics = result.setdefault("诊断信息", [])
        diagnostics.append(
            {
                "阶段": stage,
                "类型": kind,
                "详情": detail,
                "可重试": retryable,
                "重试令牌": f"RT-{result.get('结果编号')}-V{result.get('结果版本')}",
                "保留边界": boundary,
                "发生时间": _now(),
            }
        )

    def _diagnostic_boundary(self, result: dict[str, Any]) -> dict[str, Any]:
        return {
            "作业编号": result.get("作业编号"),
            "队列编号": result.get("队列编号"),
            "页面版本": result.get("页面版本"),
            "结果版本": result.get("结果版本"),
            "等待策略": result.get("等待策略"),
            "等待策略版本": result.get("等待策略版本"),
            "已满足等待条件": list(result.get("已满足等待条件") or []),
            "未满足等待条件": list(result.get("未满足等待条件") or []),
        }

    # ------------------------------------------------------------------
    # 结果创建与版本线
    # ------------------------------------------------------------------
    def _create_pending_result(
        self, entry: dict[str, Any], *, reuse: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        job_code = _as_text(entry.get("作业编号"))
        strategy = _as_text(entry.get("等待策略"), "DOM就绪")
        strategy_version = _int_or_none(entry.get("等待策略版本")) or 1
        # 页面版本跟随作业当前值：版本冲突后重新排队要用新页面版本，不能沿用手上的旧页面。
        page_version = _int_or_none(entry.get("页面版本")) or 1
        if reuse is not None:
            # 重新排队只沿用原等待策略及其版本，页面版本按作业当前版本走。
            strategy = _as_text(reuse.get("等待策略"), strategy)
            strategy_version = _int_or_none(reuse.get("等待策略版本")) or strategy_version
        conditions = WAIT_STRATEGIES.get(strategy)
        if conditions is None:
            raise RenderConflict(
                f"等待策略「{strategy}」不在支持的策略清单里：" + "、".join(WAIT_STRATEGIES)
            )
        node = _as_text(entry.get("节点标识"), "WORK-0003")

        rows = store.rows(RESULT_MODULE)
        active = [
            row
            for row in rows
            if _as_text(row.get("结果标识")) == job_code
            and row.get("status") in ACTIVE_STATUSES
            and _int_or_none(row.get("页面版本")) == page_version
            and _int_or_none(row.get("等待策略版本")) == strategy_version
        ]
        if active:
            return active[0]

        previous = self._latest_result(job_code)
        # 同名结果按版本线管理：已有结果编号时沿用，只递增结果版本，冲突后新旧两个版本可同时读取。
        result_code = _as_text(previous.get("结果编号")) if previous is not None else self._next_result_code()
        attempt = 1 + max(
            (
                _int_or_none(row.get("尝试次数")) or 0
                for row in rows
                if _as_text(row.get("结果标识")) == job_code
            ),
            default=0,
        )
        result_version = max(
            (
                _int_or_none(row.get("结果版本")) or 0
                for row in rows
                if _as_text(row.get("结果标识")) == job_code
                or _as_text(row.get("结果编号")) == result_code
            ),
            default=0,
        ) + 1
        result = {
            "id": _next_id(RESULT_MODULE),
            "status": "排队中",
            "pending": True,
            "abnormal": False,
            "结果编号": result_code,
            "作业编号": job_code,
            "结果标识": job_code,
            "目标URL": _as_text(entry.get("目标URL")),
            "浏览器版本": _as_text(entry.get("浏览器版本")),
            "等待策略": strategy,
            "等待策略版本": strategy_version,
            "页面版本": page_version,
            "结果版本": result_version,
            "尝试次数": attempt,
            "队列编号": "",
            "队列位置": 0,
            "节点标识": node,
            "槽位标识": "",
            "节点容量": self._node_capacity(node),
            "容量读取来源": "worker.并发槽位数",
            "容量已确认": False,
            "等待条件": [
                {"条件": name, "满足": False, "证据": "尚未校验", "校验时间": None}
                for name in conditions
            ],
            "已满足等待条件": [],
            "未满足等待条件": list(conditions),
            "条件校验时间": None,
            "页面内容": "",
            "成功页面": False,
            "超时处置": "未超时",
            "结果边界": "等待条件尚未校验，结果未生成",
            "排队边界": "尚未进入队列",
            "边界一致": True,
            "版本冲突": False,
            "冲突前版本": None,
            "冲突后版本": None,
            "版本历史": [],
            "诊断信息": [],
            "重试边界": {
                "可重试": True,
                "重试令牌": "",
                "已尝试次数": 0,
                "保留条件": {
                    "作业编号": job_code,
                    "队列编号": "",
                    "已满足等待条件": [],
                    "未满足等待条件": list(conditions),
                    "等待策略": strategy,
                    "等待策略版本": strategy_version,
                    "页面版本": page_version,
                },
                "沿用等待策略": strategy,
                "等待策略版本": strategy_version,
                "页面版本": page_version,
                "说明": "结果尚未定稿，按当前等待策略继续校验",
            },
            "处理键": f"{result_code}:v{result_version}:p{page_version}",
            "已处理键": [],
            "处理次数": 0,
            "已处理": False,
            "处理时间": None,
            "处理边界": "尚未完成，禁止交付下游",
            "幂等键": f"{job_code}:{attempt}:{result_version}",
            "快照引用": "",
            "快照状态": "待同步",
            "晚到结果": [],
            "更新时间": _now(),
        }
        rows.append(result)
        return result

    def _next_result_code(self) -> str:
        max_code = 0
        for row in store.rows(RESULT_MODULE):
            code = _as_text(row.get("结果编号"))
            if code.startswith("RRES-"):
                max_code = max(max_code, _int_or_none(code[5:]) or 0)
        return f"RRES-{max(max_code, 1000) + 1:04d}"

    def _latest_result(self, job_code: str) -> dict[str, Any] | None:
        rows = [row for row in store.rows(RESULT_MODULE) if _as_text(row.get("结果标识")) == job_code]
        if not rows:
            return None
        return max(rows, key=lambda row: _int_or_none(row.get("结果版本")) or 0)

    # ------------------------------------------------------------------
    # 队列与容量
    # ------------------------------------------------------------------
    def _enqueue_result(
        self, entry: dict[str, Any], result: dict[str, Any], *, reuse: dict[str, Any] | None
    ) -> dict[str, Any]:
        job_code = _as_text(entry.get("作业编号"))
        node = _as_text(result.get("节点标识"), "WORK-0003")
        capacity = self._node_capacity(node)
        online = self._node_online(node)
        occupied = self._occupied_slots(node) if online else capacity
        if online and occupied >= capacity:
            raise RenderConflict(
                f"节点 {node} 容量已满（{occupied}/{capacity}），渲染作业 {job_code} 暂不能排队"
            )

        if reuse is not None:
            queue = reuse
            # 重新排队沿用原等待策略及其版本，页面版本与队列位置都不新增。
            queue["status"] = "排队中"
            queue["pending"] = True
            queue["abnormal"] = False
            queue["结果编号"] = result.get("结果编号")
            queue["结果版本"] = result.get("结果版本")
            queue["尝试次数"] = result.get("尝试次数")
            queue["等待策略"] = result.get("等待策略")
            queue["等待策略版本"] = result.get("等待策略版本")
            queue["页面版本"] = result.get("页面版本")
            queue["节点容量"] = capacity
            queue["容量确认时间"] = _now()
            queue["容量释放时间"] = None
            queue["容量回收原因"] = ""
            queue["更新时间"] = _now()
        else:
            queue = {
                "id": _next_id(QUEUE_MODULE),
                "status": "排队中",
                "pending": True,
                "abnormal": False,
                "队列编号": self._next_queue_code(),
                "作业编号": job_code,
                "结果编号": result.get("结果编号"),
                "结果版本": result.get("结果版本"),
                "目标URL": result.get("目标URL"),
                "等待策略": result.get("等待策略"),
                "等待策略版本": result.get("等待策略版本"),
                "页面版本": result.get("页面版本"),
                "尝试次数": result.get("尝试次数"),
                "队列位置": self._next_queue_position(),
                "节点标识": node,
                "槽位标识": "",
                "节点容量": capacity,
                "容量已占位数": occupied,
                "容量占用": False,
                "容量确认时间": None,
                "容量释放时间": None,
                "容量回收原因": "",
                "排队时间": _now(),
                "更新时间": _now(),
            }
            store.rows(QUEUE_MODULE).append(queue)

        if online:
            self._occupy(queue, "提交排队占用槽位")
            result["容量已确认"] = True
            result["排队边界"] = "已占用节点容量，等待条件满足后继续"
        else:
            # 节点离线时仍可重新排队占位，但没有容量就不算确认，等节点恢复再占槽位。
            self._release(queue, "节点离线，排队占位但容量未确认")
            result["容量已确认"] = False
            result["排队边界"] = "排队占位中，节点离线，容量未确认"
            queue["abnormal"] = True
        result["队列编号"] = queue.get("队列编号")
        result["队列位置"] = queue.get("队列位置")
        result["槽位标识"] = queue.get("槽位标识")
        result["节点容量"] = capacity
        result["重试边界"]["保留条件"]["队列编号"] = queue.get("队列编号")
        self._sync_landing_from_queue(queue, result=result, reason="提交排队占用槽位")
        return queue

    def _occupy(self, queue: dict[str, Any], reason: str) -> None:
        if queue.get("容量占用"):
            queue["容量已占位数"] = self._occupied_slots(_as_text(queue.get("节点标识")))
            queue["更新时间"] = _now()
            return
        node = _as_text(queue.get("节点标识"))
        occupied = self._occupied_slots(node)
        queue["容量占用"] = True
        queue["容量已占位数"] = occupied + 1
        queue["槽位标识"] = f"{node}#{occupied + 1}"
        queue["容量确认时间"] = queue.get("容量确认时间") or _now()
        queue["容量释放时间"] = None
        queue["容量回收原因"] = ""
        queue["节点容量"] = self._node_capacity(node)
        queue["更新时间"] = _now()

    def _release(self, queue: dict[str, Any], reason: str) -> None:
        node = _as_text(queue.get("节点标识"))
        queue["容量占用"] = False
        queue["容量释放时间"] = _now()
        queue["容量回收原因"] = reason
        queue["容量已占位数"] = sum(
            1
            for row in store.rows(QUEUE_MODULE)
            if row is not queue
            and _as_text(row.get("节点标识")) == node
            and row.get("容量占用")
            and row.get("status") in ACTIVE_STATUSES
        )
        queue["更新时间"] = _now()

    def _refresh_capacity(self, queue: dict[str, Any]) -> None:
        """从节点侧重新读取容量：节点离线立即回收槽位，容量满则退回排队。"""
        node = _as_text(queue.get("节点标识"))
        queue["节点容量"] = self._node_capacity(node)
        if not self._node_online(node):
            if queue.get("容量占用"):
                self._release(queue, "节点离线，队列容量回收")
                queue["status"] = "排队中"
                queue["pending"] = True
                queue["abnormal"] = True
                self._sync_landing_from_queue(queue, result=None, reason="节点离线，容量回收")
            return
        queue["容量已占位数"] = self._occupied_slots(node)

    def _node_capacity(self, node_code: str) -> int:
        for row in store.rows("worker"):
            if _as_text(row.get("节点编号")) == node_code:
                parsed = _int_or_none(row.get("并发槽位数"))
                if parsed is not None:
                    return parsed if parsed > 0 else 0
                return 4 if row.get("status") == "在线" else 0
        return 4

    def _node_online(self, node_code: str) -> bool:
        for row in store.rows("worker"):
            if _as_text(row.get("节点编号")) == node_code:
                return row.get("status") == "在线"
        return True

    def _occupied_slots(self, node_code: str) -> int:
        return sum(
            1
            for row in store.rows(QUEUE_MODULE)
            if _as_text(row.get("节点标识")) == node_code
            and row.get("容量占用")
            and row.get("status") in ACTIVE_STATUSES
        )

    def _queue_for_job(self, job_code: str) -> dict[str, Any] | None:
        rows = [row for row in store.rows(QUEUE_MODULE) if _as_text(row.get("作业编号")) == job_code]
        if not rows:
            return None
        return max(rows, key=lambda row: int(row.get("id", 0)))

    def _next_queue_code(self) -> str:
        max_code = 0
        for row in store.rows(QUEUE_MODULE):
            code = _as_text(row.get("队列编号"))
            if code.startswith("RQ-"):
                max_code = max(max_code, _int_or_none(code[3:]) or 0)
        return f"RQ-{max(max_code, 1000) + 1:04d}"

    def _next_queue_position(self) -> int:
        active = [
            _int_or_none(row.get("队列位置")) or 0
            for row in store.rows(QUEUE_MODULE)
            if row.get("status") in ACTIVE_STATUSES
        ]
        return max(active, default=0) + 1

    def _job_entry(self, job_code: str) -> dict[str, Any] | None:
        for row in store.rows("rendertask"):
            if _as_text(row.get("作业编号")) == job_code:
                return row
        return None

    # ------------------------------------------------------------------
    # 队列状态数据集 / 落点清单
    # ------------------------------------------------------------------
    def list_queues(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(QUEUE_MODULE)
        if keyword:
            rows = [
                row
                for row in rows
                if keyword in _as_text(row.get("队列编号")) or keyword in _as_text(row.get("作业编号"))
            ]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        rows = sorted(rows, key=lambda row: _int_or_none(row.get("队列位置")) or 0)
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_queue(self, queue_id: int) -> dict[str, Any] | None:
        return store.find(QUEUE_MODULE, queue_id)

    def refresh_queue_capacity(self, queue_id: int) -> tuple[dict[str, Any] | None, str]:
        queue = store.find(QUEUE_MODULE, queue_id)
        if queue is None:
            return None, f"队列 {queue_id} 不存在或已归档"
        with _LOCK:
            before = bool(queue.get("容量占用"))
            self._refresh_capacity(queue)
            if before and not queue.get("容量占用"):
                return queue, f"节点 {queue.get('节点标识')} 离线，队列容量已回收，作业 {queue.get('作业编号')} 退回排队"
            return queue, f"队列 {queue.get('队列编号')} 容量状态已刷新为 {queue.get('容量已占位数')}/{queue.get('节点容量')}"

    def list_landing(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(LANDING_MODULE)
        if keyword:
            rows = [
                row
                for row in rows
                if keyword in _as_text(row.get("落点编号"))
                or keyword in _as_text(row.get("作业编号"))
                or keyword in _as_text(row.get("结果编号"))
            ]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_landing(self, landing_id: int) -> dict[str, Any] | None:
        return store.find(LANDING_MODULE, landing_id)

    def _landing_for(self, queue_code: str, result_code: str = "") -> dict[str, Any] | None:
        for row in store.rows(LANDING_MODULE):
            if queue_code and _as_text(row.get("队列编号")) == queue_code:
                return row
            if result_code and _as_text(row.get("结果编号")) == result_code:
                return row
        return None

    def _create_landing(self, queue: dict[str, Any]) -> dict[str, Any]:
        rows = store.rows(LANDING_MODULE)
        max_code = 1000
        for row in rows:
            code = _as_text(row.get("落点编号"))
            if code.startswith("LAND-"):
                max_code = max(max_code, _int_or_none(code[5:]) or 0)
        landing = {
            "id": _next_id(LANDING_MODULE),
            "status": "已占用" if queue.get("容量占用") else "已释放",
            "pending": bool(queue.get("容量占用")),
            "abnormal": False,
            "落点编号": f"LAND-{max_code + 1:04d}",
            "作业编号": queue.get("作业编号"),
            "队列编号": queue.get("队列编号"),
            "结果编号": queue.get("结果编号"),
            "结果版本": queue.get("结果版本"),
            "目标URL": queue.get("目标URL"),
            "节点标识": queue.get("节点标识"),
            "槽位标识": queue.get("槽位标识"),
            "节点容量": queue.get("节点容量"),
            "已占槽位数": queue.get("容量已占位数"),
            "容量状态": "已占用" if queue.get("容量占用") else "已释放",
            "容量回写时间": _now(),
            "容量释放原因": "",
            "等待策略": queue.get("等待策略"),
            "等待策略版本": queue.get("等待策略版本"),
            "页面版本": queue.get("页面版本"),
            "快照引用": "",
            "快照状态": "待同步",
            "快照": None,
            "处理状态": "未处理",
            "处理键": [],
            "处理次数": 0,
            "已刷新业务记录": [],
            "最后同步时间": _now(),
            "更新时间": _now(),
        }
        rows.append(landing)
        return landing

    def _sync_landing_capacity(self, queue: dict[str, Any], reason: str) -> dict[str, Any]:
        landing = self._landing_for(_as_text(queue.get("队列编号")))
        if landing is None:
            landing = self._create_landing(queue)
        landing.update(
            {
                "status": "已占用" if queue.get("容量占用") else "已释放",
                "pending": bool(queue.get("容量占用")),
                "节点标识": queue.get("节点标识"),
                "槽位标识": queue.get("槽位标识"),
                "节点容量": queue.get("节点容量"),
                "已占槽位数": queue.get("容量已占位数"),
                "容量状态": "已占用" if queue.get("容量占用") else "已释放",
                "容量回写时间": _now(),
                "容量释放原因": "" if queue.get("容量占用") else reason,
                "等待策略": queue.get("等待策略"),
                "等待策略版本": queue.get("等待策略版本"),
                "页面版本": queue.get("页面版本"),
                "最后同步时间": _now(),
                "更新时间": _now(),
            }
        )
        return landing

    def _sync_landing_from_queue(
        self,
        queue: dict[str, Any],
        *,
        result: dict[str, Any] | None,
        reason: str,
    ) -> dict[str, Any]:
        landing = self._sync_landing_capacity(queue, reason)
        if result is not None and result.get("结果编号"):
            landing["结果编号"] = result.get("结果编号")
            landing["结果版本"] = result.get("结果版本")
            landing["快照引用"] = result.get("快照引用") or landing.get("快照引用") or ""
            landing["快照状态"] = result.get("快照状态") or landing.get("快照状态") or "待同步"
            landing["快照"] = self._build_snapshot(result) if result.get("成功页面") else landing.get("快照")
            if result.get("abnormal"):
                landing["abnormal"] = True
        return landing

    def _sync_landing_from_result(
        self,
        result: dict[str, Any],
        queue: dict[str, Any],
        *,
        reason: str,
    ) -> dict[str, Any]:
        landing = self._sync_landing_from_queue(queue, result=result, reason=reason)
        landing["快照"] = self._build_snapshot(result)
        landing["快照引用"] = result.get("快照引用")
        landing["快照状态"] = "已同步"
        landing["结果编号"] = result.get("结果编号")
        landing["结果版本"] = result.get("结果版本")
        landing["status"] = "已同步"
        landing["pending"] = False
        landing["abnormal"] = bool(result.get("abnormal"))
        landing["最后同步时间"] = _now()
        landing["更新时间"] = _now()
        return landing

    def _build_snapshot(self, result: dict[str, Any]) -> dict[str, Any]:
        return {
            "快照引用": result.get("快照引用"),
            "结果编号": result.get("结果编号"),
            "结果版本": result.get("结果版本"),
            "页面版本": result.get("页面版本"),
            "目标URL": result.get("目标URL"),
            "成功页面": bool(result.get("成功页面")),
            "可交付": bool(result.get("成功页面")),
            "页面摘要": _as_text(result.get("页面内容"))[:120],
            "诊断摘要": [item.get("类型") for item in result.get("诊断信息", [])],
            "快照时间": _now(),
        }

    def dispatch_to_landing(self, result_id: int) -> tuple[dict[str, Any] | None, str]:
        """把渲染结果快照同步到调度落点侧清单。

        落点侧先读队列状态、回写容量占用，再判断是否交付：
        只有成功页面且容量已确认、节点在线的结果才会写进清单。
        """
        result = store.find(RESULT_MODULE, result_id)
        if result is None:
            return None, f"渲染结果 {result_id} 不存在或已归档"
        with _LOCK:
            job_code = _as_text(result.get("结果标识"))
            queue = self._queue_for_job(job_code)
            if queue is None:
                return None, f"渲染作业 {job_code} 没有队列记录，结果不能交付"
            self._refresh_capacity(queue)
            if not result.get("成功页面"):
                return None, "只有已生成成功页面的渲染结果才能交付下游"
            if not self._delivery_capacity_ok(result, queue):
                landing = self._landing_for(_as_text(queue.get("队列编号")), _as_text(result.get("结果编号")))
                if landing is not None:
                    landing["快照状态"] = "待同步"
                    landing["容量状态"] = "已释放"
                    landing["更新时间"] = _now()
                return None, (
                    f"渲染作业 {job_code} 未确认可用容量，结果不交付；"
                    "落点清单仍保留队列状态与容量占用"
                )
            landing = self._sync_landing_from_result(result, queue, reason="渲染结果交付下游")
            landing["已刷新业务记录"] = self._refresh_business_records(result, landing)
            result["快照状态"] = "已同步"
            result["更新时间"] = _now()
        return landing, f"渲染结果 {result.get('结果编号')} 已同步到调度落点清单"

    def _delivery_capacity_ok(self, result: dict[str, Any], queue: dict[str, Any]) -> bool:
        if not result.get("容量已确认"):
            return False
        node = _as_text(queue.get("节点标识"))
        if not self._node_online(node):
            return False
        if self._node_capacity(node) <= 0:
            return False
        return True

    def _refresh_business_records(
        self, result: dict[str, Any], landing: dict[str, Any]
    ) -> list[dict[str, Any]]:
        refreshed: list[dict[str, Any]] = []
        job_code = _as_text(result.get("作业编号"))
        entry = self._job_entry(job_code)
        if entry is not None:
            entry["status"] = "已完成"
            entry["pending"] = False
            entry["abnormal"] = False
            refreshed.append({"模块": "rendertask", "记录编号": job_code, "动作": "状态刷新为已完成"})
        for row in store.rows("qualityreport"):
            if _as_text(row.get("作业编号")) == job_code:
                row["渲染结果编号"] = result.get("结果编号")
                row["渲染结果版本"] = result.get("结果版本")
                refreshed.append(
                    {
                        "模块": "qualityreport",
                        "记录编号": row.get("报告编号"),
                        "动作": "刷新关联渲染结果",
                    }
                )
        return refreshed

    def process_result(self, result_id: int) -> tuple[dict[str, Any] | None, str]:
        """处理渲染结果并同步落点清单；同一处理键只入账一次。"""
        result = store.find(RESULT_MODULE, result_id)
        if result is None:
            return None, f"渲染结果 {result_id} 不存在或已归档"
        with _LOCK:
            if result.get("status") != "已完成" or not result.get("成功页面"):
                return None, f"渲染结果当前状态为 {result.get('status')}，未生成成功页面，不能处理"
            process_key = _as_text(result.get("处理键")) or (
                f"{result.get('结果编号')}:v{result.get('结果版本')}:p{result.get('页面版本')}"
            )
            handled = result.setdefault("已处理键", [])
            if process_key in handled:
                result["处理次数"] = len(handled)
                return result, (
                    f"渲染结果 {result.get('结果编号')} 已处理过（处理键 {process_key}），本次不重复入账"
                )
            queue = self._queue_for_job(_as_text(result.get("结果标识")))
            if queue is None:
                return None, "渲染结果缺少对应队列，不能处理"
            landing = self._landing_for(_as_text(queue.get("队列编号")), _as_text(result.get("结果编号")))
            if landing is None:
                landing = self._create_landing(queue)
            keys = landing.setdefault("处理键", [])
            if process_key not in keys:
                keys.append(process_key)
            landing.update(
                {
                    "快照": self._build_snapshot(result),
                    "快照引用": result.get("快照引用"),
                    "快照状态": "已同步",
                    "结果编号": result.get("结果编号"),
                    "结果版本": result.get("结果版本"),
                    "处理状态": "已处理",
                    "处理次数": len(keys),
                    "已刷新业务记录": self._refresh_business_records(result, landing),
                    "status": "已同步",
                    "pending": False,
                    "最后同步时间": _now(),
                    "更新时间": _now(),
                }
            )
            handled.append(process_key)
            result["处理次数"] = len(handled)
            result["已处理"] = True
            result["处理时间"] = _now()
            result["处理边界"] = "结果已交付且快照已同步，按处理键幂等入账一次"
            result["更新时间"] = _now()
        return result, f"渲染结果 {result.get('结果编号')} 已处理并同步到调度落点清单"


service = RendertaskService()
