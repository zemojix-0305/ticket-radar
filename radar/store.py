"""SQLite 持久化：快照历史 + 变更流水。

为什么用 SQLite 而不是别的
--------------------------
单机、单进程、零依赖、标准库自带。这个项目的写入量是「每分钟几条」级别，
任何更重的东西都是过度设计。

用 WAL 模式，读历史的时候不会阻塞正在写入的快照。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Collection, Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import Change, Snapshot

_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT    NOT NULL,
    platform    TEXT    NOT NULL,
    captured_at TEXT    NOT NULL,
    payload     TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_task ON snapshots(task_id, captured_at DESC);

CREATE TABLE IF NOT EXISTS changes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id       TEXT    NOT NULL,
    platform      TEXT    NOT NULL,
    train_code    TEXT    NOT NULL,
    seat_type     TEXT    NOT NULL,
    kind          TEXT    NOT NULL,
    before_count  INTEGER,
    after_count   INTEGER,
    detected_at   TEXT    NOT NULL,
    notified      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_changes_task ON changes(task_id, detected_at DESC);
"""


class Store:
    """极简持久层。所有方法都是同步的——SQLite 本地写入快到不值得上 async。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # -- 写入 ---------------------------------------------------------------

    def save_snapshot(self, snapshot: Snapshot) -> None:
        self._conn.execute(
            "INSERT INTO snapshots (task_id, platform, captured_at, payload) VALUES (?, ?, ?, ?)",
            (
                snapshot.task_id,
                snapshot.platform,
                snapshot.captured_at.isoformat(),
                json.dumps(snapshot.to_payload(), ensure_ascii=False),
            ),
        )
        self._conn.commit()

    def record_changes(self, changes: Iterable[Change], notified: Collection[Change] = ()) -> int:
        """落库变更流水。

        ``notified`` 是**真的推送成功**的那一批变更，通常是 ``changes`` 的子集。
        刻意不做成 bool：一轮轮询里可能同时产生「该推的」和「只入库的」两类变更
        （比如 ``notify_on`` 只要 ``appeared``，但也出现了 ``increased``），
        用一个布尔值统标所有行，会让 ``radar history`` 的「已推送」列说谎。
        """
        notified_ids = set(notified)
        rows = [
            (
                c.task_id,
                c.platform,
                c.train_code,
                c.seat_type,
                c.kind.value,
                c.before,
                c.after,
                c.detected_at.isoformat(),
                1 if c in notified_ids else 0,
            )
            for c in changes
        ]
        if not rows:
            return 0
        self._conn.executemany(
            "INSERT INTO changes (task_id, platform, train_code, seat_type, kind,"
            " before_count, after_count, detected_at, notified)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        self._conn.commit()
        return len(rows)

    # -- 读取 ---------------------------------------------------------------

    def latest_snapshot(self, task_id: str) -> Snapshot | None:
        """取该任务最近一次快照，作为下一轮 diff 的基准。"""
        cur = self._conn.execute(
            "SELECT payload FROM snapshots WHERE task_id = ? ORDER BY captured_at DESC, id DESC"
            " LIMIT 1",
            (task_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return Snapshot.from_payload(json.loads(row["payload"]))

    def history(
        self, task_id: str | None = None, limit: int = 50, days: int | None = None
    ) -> list[dict]:
        """变更流水，倒序。"""
        sql = "SELECT * FROM changes"
        params: list[object] = []
        clauses: list[str] = []
        if task_id:
            clauses.append("task_id = ?")
            params.append(task_id)
        if days is not None:
            clauses.append("detected_at >= ?")
            params.append((datetime.now(timezone.utc) - timedelta(days=days)).isoformat())
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY detected_at DESC, id DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def snapshot_count(self, task_id: str) -> int:
        cur = self._conn.execute(
            "SELECT COUNT(*) AS n FROM snapshots WHERE task_id = ?", (task_id,)
        )
        return int(cur.fetchone()["n"])

    def task_ids(self) -> list[str]:
        """库里留有快照的任务 id，最近活跃的排前面。

        看板用它决定下拉框里有什么。刻意读**快照表**而不是配置文件：
        用户可能刚把某个任务从 tasks.yaml 里删掉，但历史还在，
        看板应该仍然翻得到那段历史，而不是让人以为数据丢了。
        """
        cur = self._conn.execute(
            "SELECT task_id, MAX(captured_at) AS last FROM snapshots GROUP BY task_id"
            " ORDER BY last DESC"
        )
        return [str(r["task_id"]) for r in cur.fetchall()]

    def recent_snapshots(self, task_id: str, limit: int = 200) -> list[Snapshot]:
        """该任务最近的若干条快照，**按时间正序**返回（便于直接画曲线）。

        ``limit`` 是对解析成本设的保护线：一条 payload 里塞着那一轮的
        全部车次，200 条就可能是几十 MB 的 JSON。曲线只需要最近这一段。

        用倒序 SQL 再反转列表，是因为要的是「最近 N 条」而不是「最早 N 条」。
        """
        cur = self._conn.execute(
            "SELECT payload FROM snapshots WHERE task_id = ?"
            " ORDER BY captured_at DESC, id DESC LIMIT ?",
            (task_id, limit),
        )
        rows = cur.fetchall()
        return [Snapshot.from_payload(json.loads(r["payload"])) for r in reversed(rows)]

    def snapshot_span(self, task_id: str) -> tuple[str | None, str | None]:
        """该任务最早 / 最晚一条快照的时间（ISO 字符串）；空库返回 (None, None)。

        看板用它告诉用户「这段曲线覆盖了多长时间」。没有这个，
        用户看到一条很短的曲线会以为程序坏了，其实只是刚开始跑。
        """
        cur = self._conn.execute(
            "SELECT MIN(captured_at) AS first, MAX(captured_at) AS last"
            " FROM snapshots WHERE task_id = ?",
            (task_id,),
        )
        row = cur.fetchone()
        if row is None:
            return (None, None)
        return (row["first"], row["last"])

    def counts(self) -> dict[str, int]:
        """快照与变更的总行数，看板顶栏用。"""
        cur = self._conn.execute(
            "SELECT (SELECT COUNT(*) FROM snapshots) AS snapshots,"
            " (SELECT COUNT(*) FROM changes) AS changes"
        )
        row = cur.fetchone()
        return {"snapshots": int(row["snapshots"]), "changes": int(row["changes"])}

    def latest_snapshot_id(self) -> int:
        """全表最大快照 id，当作看板缓存的版本号用。

        比给缓存设 TTL 更准：用「数据本身有没有变」当失效条件，
        监控停下来的时候看板就不会每隔几秒白白重算一遍几十 MB 的 JSON。
        """
        cur = self._conn.execute("SELECT COALESCE(MAX(id), 0) AS n FROM snapshots")
        return int(cur.fetchone()["n"])

    # -- 维护 ---------------------------------------------------------------

    def purge_before(self, days: int) -> int:
        """清理 N 天前的快照。变更流水保留（量小且是价值所在）。"""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        cur = self._conn.execute("DELETE FROM snapshots WHERE captured_at < ?", (cutoff,))
        self._conn.commit()
        return cur.rowcount

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


__all__ = ["Store"]
