"""本地看板（``radar serve``）的后端测试。

看板的价值全在「数据算得对不对」上：曲线有没有同值压缩、
「在监控范围内」判得准不准、缓存会不会把新数据挡在外面。
所以这里测的是这些，而不是 HTML 长什么样。

HTTP 层用**真的起一个服务**的方式测——绑在 0 端口上让内核分配，
再用 httpx 打进去。比 mock 掉 handler 更接近真实行为，
而且能顺带证明「真的能收到请求」。
"""

from __future__ import annotations

import datetime as dt
import threading

import httpx
import pytest
from typer.testing import CliRunner

from radar.adapters.base import Adapter
from radar.cli import app
from radar.config import AppConfig, TaskConfig, WatchRule
from radar.serve import Board, build_server, seat_value
from radar.store import Store
from tests.conftest import make_snapshot

#: 固定基准时间：曲线测试要断言具体的时间戳，不能用「现在」
BASE = dt.datetime(2026, 9, 29, 12, 0, tzinfo=dt.timezone.utc)


def _task(**kwargs) -> TaskConfig:
    base = {
        "id": "t1",
        "adapter": "rail12306",
        "params": {"from": "北京南", "to": "上海虹桥", "date": "2026-10-06"},
    }
    return TaskConfig(**{**base, **kwargs})


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "radar.db")
    yield s
    s.close()


def _seed(
    store: Store,
    values: list[int | None],
    *,
    task_id: str = "t1",
    seat: str = "二等座",
    depart: str = "06:43",
    start: int = 0,
) -> None:
    """往库里塞一串快照：第 i 条里 G1 的那个席别等于 ``values[i]``。"""
    for i, value in enumerate(values):
        store.save_snapshot(
            make_snapshot(
                task_id=task_id,
                trains={"G1": {seat: value}},
                when=BASE + dt.timedelta(minutes=5 * (start + i)),
                depart_times={"G1": depart},
            )
        )


@pytest.fixture
def board(store):
    return Board(config=AppConfig(tasks=[_task()]), store=store, config_path="tasks.yaml")


# --- 三态映射 -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("available", "count", "expected"),
    [
        (False, None, 0),
        (False, 0, 0),
        (True, None, None),  # 12306 返回「有」时就是这种
        (True, 5, 5),
        (True, 0, 0),
    ],
)
def test_seat_value_keeps_unknown_apart_from_zero(available, count, expected):
    """「有票但数量未知」和「0 张」必须分开——搞反整条曲线会完全反过来。"""
    assert seat_value(available, count) == expected


# --- 概览 ---------------------------------------------------------------------


def test_status_reports_db_counts_and_task_state(board, store):
    _seed(store, [0, 3])

    data = board.status()

    assert data["counts"] == {"snapshots": 2, "changes": 0}
    assert [t["id"] for t in data["tasks"]] == ["t1"]
    assert data["tasks"][0]["in_db"] is True
    assert data["config_path"] == "tasks.yaml"


def test_status_keeps_tasks_that_only_exist_in_db(store):
    """任务从 tasks.yaml 删掉了，历史还在——看板必须还能翻到它。"""
    _seed(store, [1], task_id="ghost")

    data = Board(config=AppConfig(tasks=[]), store=store).status()

    assert [t["id"] for t in data["tasks"]] == ["ghost"]
    assert data["tasks"][0]["in_db"] is True
    assert "已移除" in data["tasks"][0]["name"]


# --- 曲线 ---------------------------------------------------------------------


def test_series_compresses_consecutive_equal_values(board, store):
    """余票一直不变时不该每个采样都留一个点——阶梯图只需要变化点。"""
    _seed(store, [0, 0, 0, 0, 5, 5, 0])

    data = board.series("t1")
    points = data["trains"][0]["seats"]["二等座"]

    assert [p["v"] for p in points] == [0, 5, 0]
    assert len(points) == 3
    # 但采样次数要如实报告，否则用户以为只抓了 3 次
    assert data["points"] == 7


def test_series_reports_covered_span(board, store):
    _seed(store, [0, 1, 2])

    data = board.series("t1")

    assert data["from"] == BASE.isoformat()
    assert data["to"] == (BASE + dt.timedelta(minutes=10)).isoformat()


def test_series_keeps_unknown_count_as_null(board, store):
    """「有票」要传成 null 而不是某个数字——前端据此画在「有票」那条虚线上。"""
    _seed(store, [None, None, 4])

    points = board.series("t1")["trains"][0]["seats"]["二等座"]

    assert [p["v"] for p in points] == [None, 4]
    assert points[0]["raw"] == "有"


def test_series_marks_watched_by_depart_window(store):
    """时间窗外的车次不能被算进监控范围——否则用户以为它在被盯着。"""
    _seed(store, [0], depart="06:43")
    inside = WatchRule(seat_types=["二等座"], depart_after="06:00", depart_before="12:00")
    outside = WatchRule(seat_types=["二等座"], depart_after="18:00", depart_before="23:00")

    board_in = Board(config=AppConfig(tasks=[_task(watch=inside)]), store=store)
    board_out = Board(config=AppConfig(tasks=[_task(watch=outside)]), store=store)

    assert board_in.series("t1")["trains"][0]["watched"] is True
    assert board_out.series("t1")["trains"][0]["watched"] is False


def test_series_watched_requires_seat_match(store):
    """普速车没有「二等座」这一档，放票也不会推——不能算成监控范围内。"""
    _seed(store, [0], seat="硬座")
    watch = WatchRule(seat_types=["二等座"])

    board = Board(config=AppConfig(tasks=[_task(watch=watch)]), store=store)

    assert board.series("t1")["trains"][0]["watched"] is False


def test_series_cache_returns_same_object_until_data_changes(board, store):
    _seed(store, [0])
    assert board.series("t1")["points"] == 1
    # 数据没动 → 命中缓存（同一个对象，不是重新算的等价对象）
    assert board.series("t1") is board.series("t1")

    _seed(store, [5], start=1)
    assert board.series("t1")["points"] == 2


def test_series_of_unknown_task_is_empty_not_an_error(board):
    """配置里没有、库里也没有的任务，返回空曲线就好——不该 500。"""
    data = board.series("nope")

    assert data["trains"] == []
    assert data["points"] == 0


# --- 扫描 ---------------------------------------------------------------------


class _FakeAdapter(Adapter):
    name = "fake"
    min_interval = 0.0

    async def fetch(self, task, client):  # noqa: ARG002
        return make_snapshot(task_id=task.id, trains={"G1": {"二等座": 7, "一等座": None}})


def test_scan_flattens_snapshot_like_check_json(store, monkeypatch):
    """看板的「立即扫描」要和 ``radar check --json`` 同构，否则两边会各说各话。"""
    monkeypatch.setattr("radar.serve.create_adapter", lambda *a, **k: _FakeAdapter({}))
    board = Board(config=AppConfig(tasks=[_task()]), store=store)

    data = board.scan("t1")

    assert data["task_id"] == "t1"
    train = data["trains"][0]
    assert train["train_code"] == "G1"
    assert train["seats"]["二等座"]["count"] == 7
    assert train["seats"]["一等座"]["count"] is None
    # 扫描不写库：看板是只读的
    assert store.counts()["snapshots"] == 0


def test_scan_unknown_task_raises_keyerror(board):
    with pytest.raises(KeyError):
        board.scan("nope")


# --- 生成配置片段 --------------------------------------------------------------


def test_snippet_is_globally_sorted_and_uppercased(board):
    text = board.snippet("t1", ["g3", "G1", "G1 "])

    assert '"G1", "G3"' in text
    assert text.startswith("    watch:")


def test_snippet_of_nothing_is_empty(board):
    assert board.snippet("t1", []) == ""


# --- HTTP 层 ------------------------------------------------------------------


@pytest.fixture
def server(board):
    """真的起一个服务，绑 0 端口让内核分配——比 mock handler 更接近真实。"""
    httpd = build_server(board, "127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def _get(base: str, path: str) -> httpx.Response:
    # trust_env=False：本机常开代理，127.0.0.1 不该走代理
    return httpx.get(base + path, timeout=5.0, trust_env=False)


def _post(base: str, path: str, payload: dict) -> httpx.Response:
    return httpx.post(base + path, json=payload, timeout=5.0, trust_env=False)


def test_serves_the_board_page(server):
    res = _get(server, "/")

    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]
    assert "ticket-radar 看板" in res.text
    # 零外部依赖：页面不许引 CDN，否则断网/内网环境下直接白屏
    for cdn in ("cdn.", "unpkg", "jsdelivr", "<link"):
        assert cdn not in res.text, f"看板页面不该依赖外部资源：{cdn}"


def test_api_status_round_trips(server):
    res = _get(server, "/api/status")

    assert res.status_code == 200
    assert res.json()["tasks"][0]["id"] == "t1"


def test_api_series_defaults_to_the_active_task(server, store):
    _seed(store, [0, 4])

    res = _get(server, "/api/series")

    assert res.status_code == 200
    assert res.json()["trains"][0]["train_code"] == "G1"


def test_api_series_accepts_explicit_task(server, store):
    _seed(store, [2])

    res = _get(server, "/api/series?task_id=t1")

    assert res.status_code == 200
    assert res.json()["points"] == 1


def test_api_unknown_path_is_404_with_json(server):
    res = _get(server, "/api/nope")

    assert res.status_code == 404
    assert "error" in res.json()


def test_api_scan_of_unknown_task_is_404(server):
    res = _get(server, "/api/scan?task_id=missing")

    assert res.status_code == 404
    assert "未找到任务" in res.json()["error"]


def test_api_snippet_endpoint(server):
    res = _post(server, "/api/snippet", {"task_id": "t1", "train_codes": ["G5", "G1"]})

    assert res.status_code == 200
    assert '"G1", "G5"' in res.json()["snippet"]


def test_api_snippet_rejects_unknown_task(server):
    res = _post(server, "/api/snippet", {"task_id": "nope", "train_codes": ["G1"]})

    assert res.status_code == 404


def test_serve_command_defaults_to_loopback_only():
    """看板没有鉴权。默认值必须是 127.0.0.1——这条是安全边界，不是偏好。"""
    result = CliRunner().invoke(app, ["serve", "--help"])

    assert result.exit_code == 0
    assert "127.0.0.1" in result.output
