"""渲染结果与队列状态链路的回归测试。

覆盖需求里的关键约束：
- 重复下发只占一个队列位置；
- 等待条件未满足/超时不写成成功页面；
- 用户终止保留已满足与未满足条件；
- 晚到结果不覆盖较新的成功结果；
- 同一结果重复处理只算一次；
- 并发处理结果不分叉；
- 节点离线回收容量、重新排队沿用原等待策略及版本；
- 结果快照与处理结果同步到调度落点清单。
"""
from __future__ import annotations

import concurrent.futures

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.store import store


@pytest.fixture()
def client() -> TestClient:
    store.reset()
    return TestClient(app)


def _queue_rows(client: TestClient) -> list[dict]:
    return client.get("/api/renderqueue?size=200").json()["items"]


def _result_rows(client: TestClient, job_code: str) -> list[dict]:
    return client.get(f"/api/renderresult?job_code={job_code}&size=200").json()["items"]


def _action(client: TestClient, entry_id: int, action: str) -> dict:
    return client.post(
        f"/api/rendertask/{entry_id}/actions", json={"values": {"action": action}}
    ).json()


def _satisfy_all(client: TestClient, result_id: int, times: int = 5) -> None:
    for _ in range(times):
        payload = client.post(f"/api/renderresult/{result_id}/validate-wait", json={}).json()
        if not payload["entry"]["未满足等待条件"]:
            return


def test_repeated_submit_keeps_single_queue_position(client: TestClient) -> None:
    before = len(_queue_rows(client))
    first = _action(client, 2, "提交排队")
    second = _action(client, 2, "提交排队")
    assert first["ok"] is False or "已在队列" in second["message"]
    assert len(_queue_rows(client)) == before
    rows = [row for row in _queue_rows(client) if row["作业编号"] == "REND-0002"]
    assert len(rows) == 1


def test_render_success_requires_all_wait_conditions(client: TestClient) -> None:
    result_id = [row for row in _result_rows(client, "REND-0002") if row["status"] == "排队中"][0]["id"]
    blocked = client.post(f"/api/renderresult/{result_id}/finalize").json()
    assert blocked["ok"] is False
    assert "等待条件尚未满足" in blocked["message"]
    assert client.get(f"/api/renderresult/{result_id}").json()["成功页面"] is False

    _satisfy_all(client, result_id)
    ok = client.post(f"/api/renderresult/{result_id}/finalize").json()
    assert ok["ok"] is True
    result = client.get(f"/api/renderresult/{result_id}").json()
    assert result["成功页面"] is True
    assert result["结果边界"] == result["排队边界"] or result["边界一致"] is True


def test_timeout_never_written_as_success_page(client: TestClient) -> None:
    result_id = [row for row in _result_rows(client, "REND-0003") if row["status"] == "已超时"][0]["id"]
    payload = client.post(f"/api/renderresult/{result_id}/finalize").json()
    assert payload["ok"] is False
    result = client.get(f"/api/renderresult/{result_id}").json()
    assert result["成功页面"] is False
    assert result["页面内容"] == ""
    assert result["诊断信息"] and result["诊断信息"][0]["类型"] == "渲染等待超时"
    assert result["诊断信息"][0]["可重试"] is True
    assert result["重试边界"]["保留条件"]["未满足等待条件"] == ["接口返回"]


def test_user_terminate_preserves_satisfied_and_unsatisfied(client: TestClient) -> None:
    client.post("/api/worker/3/actions", json={"values": {"action": "标记在线"}})
    _action(client, 3, "提交排队")
    result_id = [row for row in _result_rows(client, "REND-0003") if row["status"] in {"排队中", "渲染中"}][0]["id"]
    client.post(
        f"/api/renderresult/{result_id}/validate-wait",
        json={"conditions": [{"条件": "DOM就绪", "满足": True}]},
    )
    payload = _action(client, 3, "终止渲染")
    assert payload["ok"] is True
    result = client.get(f"/api/renderresult/{result_id}").json()
    assert result["status"] == "已终止"
    assert result["成功页面"] is False
    assert result["已满足等待条件"] == ["DOM就绪"]
    assert result["未满足等待条件"] == ["接口返回"]
    assert result["诊断信息"][-1]["保留边界"]["已满足等待条件"] == ["DOM就绪"]
    assert result["诊断信息"][-1]["保留边界"]["未满足等待条件"] == ["接口返回"]


def test_late_result_cannot_overwrite_newer_success(client: TestClient) -> None:
    rows = _result_rows(client, "REND-0004")
    old = [row for row in rows if row["结果版本"] == 1][0]
    new = [row for row in rows if row["结果版本"] == 2][0]
    assert new["成功页面"] is True

    payload = client.post(f"/api/renderresult/{old['id']}/finalize").json()
    assert payload["ok"] is False
    old_after = client.get(f"/api/renderresult/{old['id']}").json()
    assert old_after["成功页面"] is False
    assert old_after["晚到结果"][0]["较新结果版本"] == 2

    new_after = client.get(f"/api/renderresult/{new['id']}").json()
    assert new_after["成功页面"] is True
    assert new_after["status"] == "已完成"
    assert new_after["结果版本"] == 2


def test_process_is_idempotent(client: TestClient) -> None:
    result_id = [row for row in _result_rows(client, "REND-0001") if row["成功页面"]][0]["id"]
    first = client.post(f"/api/renderresult/{result_id}/process").json()
    second = client.post(f"/api/renderresult/{result_id}/process").json()
    assert first["entry"]["处理次数"] == 1
    assert second["entry"]["处理次数"] == 1
    assert "不重复入账" in second["message"]
    landing = [row for row in client.get("/api/renderlanding?size=200").json()["items"] if row["作业编号"] == "REND-0001"][0]
    assert landing["处理键"] == ["RRES-1001:v1:p1"]


def test_concurrent_process_does_not_fork_result(client: TestClient) -> None:
    result_id = [row for row in _result_rows(client, "REND-0002") if row["status"] in {"排队中", "渲染中"}][0]["id"]
    _satisfy_all(client, result_id)
    assert client.post(f"/api/renderresult/{result_id}/finalize").json()["ok"] is True

    def _process(_: int) -> dict:
        return client.post(f"/api/renderresult/{result_id}/process").json()

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        payloads = list(pool.map(_process, range(8)))

    counts = {payload["entry"]["处理次数"] for payload in payloads if payload["ok"]}
    assert counts == {1}
    result = client.get(f"/api/renderresult/{result_id}").json()
    assert result["处理次数"] == 1
    assert len(result["已处理键"]) == 1
    assert result["status"] == "已完成"
    assert len({row["结果版本"] for row in _result_rows(client, "REND-0002") if row["status"] == "已完成"}) == 1


def test_node_offline_reclaims_capacity_and_requeue_keeps_policy(client: TestClient) -> None:
    queue = [row for row in _queue_rows(client) if row["作业编号"] == "REND-0002"][0]
    assert queue["容量占用"] is True
    client.post("/api/worker/3/actions", json={"values": {"action": "标记离线"}})
    refreshed = client.post(f"/api/renderqueue/{queue['id']}/refresh").json()
    assert "容量已回收" in refreshed["message"]
    after = client.get(f"/api/renderqueue/{queue['id']}").json()
    assert after["容量占用"] is False
    assert after["status"] == "排队中"
    assert after["等待策略"] == queue["等待策略"]
    assert after["等待策略版本"] == queue["等待策略版本"]

    client.post("/api/worker/3/actions", json={"values": {"action": "标记在线"}})
    payload = _action(client, 2, "提交排队")
    assert payload["ok"] is True
    rows = [row for row in _queue_rows(client) if row["作业编号"] == "REND-0002"]
    assert len(rows) == 1
    assert rows[0]["等待策略"] == queue["等待策略"]
    assert rows[0]["等待策略版本"] == queue["等待策略版本"]


def test_capacity_exhaustion_lands_on_different_boundary(client: TestClient) -> None:
    # 单独建一个 1 槽位节点，避免示例数据里的容量口径影响断言。
    client.post(
        "/api/worker",
        json={
            "values": {
                "节点编号": "WORK-C1",
                "主机地址": "10.0.0.11",
                "并发槽位数": "1",
                "节点状态标签": "在线",
            }
        },
    )
    worker_id = client.get("/api/worker?size=200").json()["items"][-1]["id"]
    client.post(f"/api/worker/{worker_id}/actions", json={"values": {"action": "标记在线"}})

    first = _create_job_on_node(client, "REND-C1", "WORK-C1")
    second = _create_job_on_node(client, "REND-C2", "WORK-C1")
    assert _action(client, first["id"], "提交排队")["ok"] is True
    blocked = _action(client, second["id"], "提交排队")
    assert blocked["ok"] is False
    assert "容量已满" in blocked["message"]

    # 节点离线后容量立即回收，其他作业可以重新排队占位。
    client.post(f"/api/worker/{worker_id}/actions", json={"values": {"action": "标记离线"}})
    queue = [row for row in _queue_rows(client) if row["作业编号"] == "REND-C1"][0]
    refreshed = client.post(f"/api/renderqueue/{queue['id']}/refresh").json()
    assert "容量已回收" in refreshed["message"]
    assert client.get(f"/api/renderqueue/{queue['id']}").json()["容量占用"] is False


def _create_job_on_node(client: TestClient, job_code: str, node_code: str) -> dict:
    payload = client.post(
        "/api/rendertask",
        json={
            "values": {
                "作业编号": job_code,
                "目标URL": f"https://example.com/{job_code}",
                "浏览器版本": "Chrome 120",
                "等待策略": "DOM就绪",
                "节点标识": node_code,
            }
        },
    ).json()
    assert payload["ok"] is True
    return payload["entry"]


def test_snapshot_synced_to_landing(client: TestClient) -> None:
    result_id = [row for row in _result_rows(client, "REND-0002") if row["status"] in {"排队中", "渲染中"}][0]["id"]
    _satisfy_all(client, result_id)
    assert client.post(f"/api/renderresult/{result_id}/finalize").json()["ok"] is True
    payload = client.post(f"/api/renderresult/{result_id}/dispatch").json()
    assert payload["ok"] is True
    landing_rows = client.get("/api/renderlanding?size=200").json()["items"]
    landing = [row for row in landing_rows if row["作业编号"] == "REND-0002"][0]
    assert landing["快照状态"] == "已同步"
    assert landing["快照"]["结果编号"] == f"RRES-{result_id + 1000:04d}"
    assert landing["容量状态"] == "已释放"
    assert landing["已刷新业务记录"]
    quality = [row for row in client.get("/api/qualityreport?size=200").json()["items"] if row["作业编号"] == "REND-0002"]
    assert quality and quality[0]["渲染结果编号"] == landing["结果编号"]


def _create_job(client: TestClient, job_code: str, *, page_version: int = 1) -> dict:
    payload = client.post(
        "/api/rendertask",
        json={
            "values": {
                "作业编号": job_code,
                "目标URL": f"https://example.com/{job_code}",
                "浏览器版本": "Chrome 120",
                "等待策略": "DOM就绪",
                "节点标识": "WORK-0003",
                "页面版本": page_version,
            }
        },
    ).json()
    assert payload["ok"] is True
    return payload["entry"]


def test_version_conflict_keeps_two_versions(client: TestClient) -> None:
    entry = _create_job(client, "REND-VC1")
    assert _action(client, entry["id"], "提交排队")["ok"] is True
    store.find("rendertask", entry["id"])["页面版本"] = 2

    old = [row for row in _result_rows(client, "REND-VC1") if row["status"] == "排队中"][0]
    client.post(f"/api/renderresult/{old['id']}/validate-wait", json={})
    conflict = client.post(f"/api/renderresult/{old['id']}/finalize").json()
    assert conflict["ok"] is False
    assert "版本冲突" in conflict["message"] or "页面版本" in conflict["message"]

    old_after = client.get(f"/api/renderresult/{old['id']}").json()
    assert old_after["版本冲突"] is True
    assert old_after["冲突前版本"] == 1
    assert old_after["冲突后版本"] == 2
    assert old_after["成功页面"] is False
    assert old_after["边界一致"] is False
    assert old_after["诊断信息"][-1]["类型"] == "渲染版本冲突"

    # 冲突后重新排队：旧版本保留，新版本按新页面版本生成，两个版本同时可见。
    assert _action(client, entry["id"], "提交排队")["ok"] is True
    versions = sorted(_result_rows(client, "REND-VC1"), key=lambda row: row["结果版本"])
    assert [row["结果版本"] for row in versions] == [1, 2]
    assert versions[0]["成功页面"] is False

    new = [row for row in versions if row["status"] in {"排队中", "渲染中"}][0]
    client.post(f"/api/renderresult/{new['id']}/validate-wait", json={})
    assert client.post(f"/api/renderresult/{new['id']}/finalize").json()["ok"] is True
    final = sorted(_result_rows(client, "REND-VC1"), key=lambda row: row["结果版本"])
    assert [row["结果版本"] for row in final] == [1, 2]
    assert final[0]["成功页面"] is False
    assert final[1]["成功页面"] is True
    assert final[1]["页面版本"] == 2
    # 同名结果：冲突后保留两个版本，且共享同一个结果编号，便于按编号追溯。
    assert {row["结果编号"] for row in final} == {old["结果编号"]}


def test_capacity_exhaustion_records_diagnostic(client: TestClient) -> None:
    queue = [row for row in _queue_rows(client) if row["作业编号"] == "REND-0002"][0]
    client.post("/api/worker/3/actions", json={"values": {"action": "标记离线"}})
    client.post(f"/api/renderqueue/{queue['id']}/refresh")
    result_id = [row for row in _result_rows(client, "REND-0002")][0]["id"]
    _satisfy_all(client, result_id)
    payload = client.post(f"/api/renderresult/{result_id}/finalize").json()
    assert payload["ok"] is False
    assert "不同边界" in payload["message"]
    result = client.get(f"/api/renderresult/{result_id}").json()
    assert result["边界一致"] is False
    assert "待容量" in result["结果边界"]
    assert result["诊断信息"][-1]["类型"] == "节点容量耗尽"
    landing = [row for row in client.get("/api/renderlanding?size=200").json()["items"] if row["作业编号"] == "REND-0002"][0]
    assert landing["快照状态"] == "待同步"


def test_failure_registers_retry_boundary(client: TestClient) -> None:
    payload = _action(client, 3, "标记超时")
    assert payload["ok"] is True
    retries = [row for row in client.get("/api/retryqueue?size=200").json()["items"] if row.get("关联结果编号")]
    assert len(retries) == 1
    retry = retries[0]
    assert retry["失败原因码"] == "渲染等待超时"
    assert retry["重试令牌"]
    assert retry["保留边界"]["未满足等待条件"] == ["接口返回"]

    _action(client, 3, "标记超时")
    retries = [row for row in client.get("/api/retryqueue?size=200").json()["items"] if row.get("关联结果编号")]
    assert len(retries) == 1, "同一结果版本只登记一次重试条目"

