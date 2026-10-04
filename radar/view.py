"""展示层：把领域对象摊平成「给人看 / 给前端用」的字典。

为什么单独一个模块
------------------
同一份摊平逻辑有两个消费者：

* ``radar check --json`` —— 命令行里先把候选筛一遍，再决定盯哪几趟车
* ``radar serve`` 的本地看板 —— 浏览器里看余票曲线、点选车次

如果把它留在 CLI 模块里，看板就得反向依赖 CLI；如果各写一份，
两份实现必然随时间漂移，然后就会出现最难查的那种 bug——
「命令行说这趟在监控范围内，看板说不是」。所以摊平规则只写在这里，
两边共用同一份判断。

``watched`` 与 ``matched`` 的区别是本模块的核心，见 :func:`snapshot_to_dict`。
"""

from __future__ import annotations

from typing import Any

from .config import TaskConfig
from .models import Snapshot


def snapshot_to_dict(task: TaskConfig, snapshot: Snapshot) -> dict[str, Any]:
    """把快照摊平成结构化字典，供 ``radar check --json`` 和看板共用。

    比原始返回体多三样东西，都是「先看再挑」时真正需要的：

    * ``context`` —— 相对日期解析成了哪一天（``+7`` → ``2026-10-06``）
    * ``watched`` —— 规则覆不覆盖这趟车（时间窗 / 车次 / 席别）
    * ``matched`` —— 覆盖，**并且**现在真的有票

    ``matched`` 单独看会骗人：光看「有票」不够，还得看规则抓不抓得到，
    否则很容易挑出一趟「明明有票、却永远不会推给你」的车，白高兴一场。
    反过来只看 ``watched`` 也骗人：**无票的车**同样是 ``watched=True``，
    而那恰恰是余票监控里最需要盯的一类。
    """
    watch = task.watch
    trains: list[dict[str, Any]] = []
    for code, train in sorted(
        snapshot.trains.items(), key=lambda kv: (kv[1].depart_time or "99:99", kv[0])
    ):
        hit_seats = [
            s
            for s in train.seats.values()
            if watch.matches_seat(s.seat_type) and s.effective >= watch.min_count
        ]
        # ``watched`` 与 ``matched`` 必须分开，否则会把人带进沟里：
        #   watched —— 规则覆不覆盖这趟车（时间窗 / 车次 / **席别**）
        #   matched —— 覆盖，**并且**现在真的有票
        # 只留一个的话，「无票的车」会被读成「不会被监控」，
        # 而那恰恰是余票监控里最需要盯的一类。
        #
        # 席别也要算进 watched：上午的普速车（K/Z/T）压根没有「二等座」这一档，
        # 放票也不会推。把它们算成「在监控范围内」同样是虚假安全感。
        watched = (
            watch.matches_train(code)
            and watch.matches_depart_time(train.depart_time)
            and any(watch.matches_seat(s) for s in train.seats)
        )
        trains.append(
            {
                "train_code": code,
                "from": train.from_station,
                "to": train.to_station,
                "depart": train.depart_time,
                "arrive": train.arrive_time,
                "duration": train.duration,
                "watched": watched,
                "matched": watched and bool(hit_seats),
                "seats": {
                    s.seat_type: {
                        "raw": s.raw,
                        "count": s.count,
                        "available": s.available,
                        "price": s.price,
                        "watched": watch.matches_seat(s.seat_type),
                    }
                    for s in train.seats.values()
                },
            }
        )

    return {
        "task_id": task.id,
        "task_name": task.display_name,
        "adapter": task.adapter,
        "platform": snapshot.platform,
        "link": task.link,
        "context": dict(snapshot.context),
        "captured_at": snapshot.captured_at.isoformat(),
        "query": dict(task.params),
        "watch": {
            "seat_types": list(watch.seat_types),
            "min_count": watch.min_count,
            "train_codes": list(watch.train_codes),
            "depart_after": watch.depart_after,
            "depart_before": watch.depart_before,
            "notify_on": [k.value for k in watch.notify_on],
        },
        "matched_count": sum(1 for t in trains if t["matched"]),
        "watched_count": sum(1 for t in trains if t["watched"]),
        "trains": trains,
    }


def build_snippet(task: TaskConfig, train_codes: list[str]) -> str:
    """按勾选的车次生成一段可直接粘进 ``tasks.yaml`` 的 watch 片段。

    看板的「点选生成配置」就是调它。刻意只生成 ``watch`` 那一小块，
    不生成整个任务——因为日期、出发站这些只有用户知道，
    替用户编一个完整任务反而是在制造需要他回头删掉的东西。

    车次很多时不换行（YAML 流式序列对长列表更友好），但超过 12 个就拆行，
    否则一行能到几百字符、编辑器里没法看。
    """
    codes = sorted({c.strip().upper() for c in train_codes if c and c.strip()})
    if not codes:
        return ""
    quoted = [f'"{c}"' for c in codes]
    if len(quoted) <= 12:
        codes_line = ", ".join(quoted)
    else:
        codes_line = "\n" + "\n".join(f'        - {q}' for q in quoted)
    return (
        "    watch:\n"
        f"      train_codes: [{codes_line}]\n"
        "      # 下面两项按需要保留或删掉\n"
        "      seat_types: [\"二等座\"]\n"
        "      notify_on: [\"appeared\"]\n"
    )


__all__ = ["build_snippet", "snapshot_to_dict"]
