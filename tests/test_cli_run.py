"""``radar run --announce`` 启动播报的测试。

用户真正会问的问题不是「功能有没有」，而是「我双击了、手机没响，是不是挂了」。
启动播报就是回答这个的，所以**它的措辞本身就是被测对象**——
少了那句「安静 = 没变化」，它就只是一条噪音。
"""

from __future__ import annotations

from radar.cli import _startup_message
from radar.config import TaskConfig


def _task(tid: str, name: str = "", link: str | None = None) -> TaskConfig:
    return TaskConfig(id=tid, name=name, adapter="rail12306", link=link)


def test_startup_message_lists_every_task():
    msg = _startup_message([_task("a", "广州南 → 长沙南"), _task("b", "京沪早班")])

    assert msg.title == "【余票监控】已启动"
    assert "广州南 → 长沙南" in msg.body
    assert "京沪早班" in msg.body
    assert "2 个任务" in msg.body


def test_startup_message_explains_that_silence_is_normal():
    """必须把「安静＝没变化，不是停了」说出口。

    这是启动播报存在的唯一理由，所以钉住措辞本身。
    """
    body = _startup_message([_task("a", "X")]).body

    assert "只有检测到余票变化才会再提醒你" in body
    assert "不是程序停了" in body
    assert "关掉命令行窗口就会停止监控" in body


def test_startup_message_carries_first_task_link():
    msg = _startup_message([_task("a", "X", link="https://www.12306.cn/index/")])
    assert msg.url == "https://www.12306.cn/index/"


def test_startup_message_without_link_is_fine():
    assert _startup_message([_task("a", "X")]).url is None


def test_startup_message_with_no_tasks_does_not_explode():
    """正常路径下 run 会先拦掉空任务列表，但播报函数自己也不该炸。"""
    msg = _startup_message([])

    assert msg.url is None
    assert "0 个任务" in msg.body


def test_startup_message_falls_back_to_task_id_when_unnamed():
    """没写 name 的任务要用 id 兜底，否则播报里会是一条空行。"""
    assert "gz-cs-morning" in _startup_message([_task("gz-cs-morning")]).body
