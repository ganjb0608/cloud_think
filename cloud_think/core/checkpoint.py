"""持久化层：run / checkpoint / node_runs / effects / interrupts / events。

本地场景用 SQLite + WAL：单文件、零运维、原子事务、可查询历史。
sqlite3 是同步库，统一用 ``asyncio.to_thread`` 包一层，不阻塞事件循环。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol


def new_run_id(prefix: str = "run") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@dataclass
class TaskRef:
    """frontier 里的一个待执行任务。``arg`` 是 fan-out 时分到的那份输入。"""

    node: str
    arg: Any = None
    instance: str = ""

    def __post_init__(self) -> None:
        if not self.instance:
            self.instance = self.node

    def to_dict(self) -> dict[str, Any]:
        return {"node": self.node, "arg": self.arg, "instance": self.instance}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> TaskRef:
        return cls(node=d["node"], arg=d.get("arg"), instance=d.get("instance", ""))


@dataclass
class Checkpoint:
    """一个超步结束后的全局快照。resume / fork / time-travel 都基于它。"""

    run_id: str
    step: int
    state: dict[str, Any]
    frontier: list[TaskRef] = field(default_factory=list)
    arrivals: dict[str, list[str]] = field(default_factory=dict)
    visits: dict[str, int] = field(default_factory=dict)
    status: str = "running"            # running | paused | done | failed
    parent_step: int | None = None
    interrupt: dict[str, Any] | None = None
    created_at: float = field(default_factory=time.time)

    def to_row(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "step": self.step,
            "parent_step": self.parent_step,
            "state": json.dumps(self.state, ensure_ascii=False, default=str),
            "frontier": json.dumps([t.to_dict() for t in self.frontier], ensure_ascii=False, default=str),
            "arrivals": json.dumps(self.arrivals, ensure_ascii=False),
            "visits": json.dumps(self.visits, ensure_ascii=False),
            "status": self.status,
            "interrupt": json.dumps(self.interrupt, ensure_ascii=False, default=str) if self.interrupt else None,
            "created_at": self.created_at,
        }

    @classmethod
    def from_row(cls, row: sqlite3.Row | Mapping[str, Any]) -> Checkpoint:
        return cls(
            run_id=row["run_id"], step=row["step"], parent_step=row["parent_step"],
            state=json.loads(row["state"]),
            frontier=[TaskRef.from_dict(d) for d in json.loads(row["frontier"])],
            arrivals=json.loads(row["arrivals"]), visits=json.loads(row["visits"]),
            status=row["status"],
            interrupt=json.loads(row["interrupt"]) if row["interrupt"] else None,
            created_at=row["created_at"],
        )


class Checkpointer(Protocol):
    async def create_run(self, run_id: str, workflow: str, meta: dict[str, Any]) -> None: ...
    async def set_status(self, run_id: str, status: str, error: str | None = None) -> None: ...
    async def get_run(self, run_id: str) -> dict[str, Any] | None: ...
    async def list_runs(self, limit: int = 20) -> list[dict[str, Any]]: ...
    async def save(self, ckpt: Checkpoint) -> None: ...
    async def load_latest(self, run_id: str) -> Checkpoint | None: ...
    async def load_at(self, run_id: str, step: int) -> Checkpoint | None: ...
    async def list_steps(self, run_id: str) -> list[int]: ...
    async def record_node_run(self, **kw: Any) -> None: ...
    async def get_effect(self, run_id: str, key: str) -> tuple[bool, Any]: ...
    async def put_effect(self, run_id: str, key: str, result: Any) -> None: ...
    async def put_interrupt(self, run_id: str, step: int, node: str, payload: Any) -> None: ...
    async def get_interrupt_answer(self, run_id: str, step: int, node: str) -> tuple[bool, Any]: ...
    async def answer_interrupt(self, run_id: str, step: int, node: str, answer: Any) -> None: ...
    async def append_event(self, event: Any) -> None: ...


_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;
-- 多个进程共用同一个库是预期用法（比如两个 agent 都 shell out 到 ct），
-- 写锁冲突时等待而不是立刻报 "database is locked"。
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, workflow TEXT NOT NULL, status TEXT NOT NULL,
  error TEXT, meta TEXT, created_at REAL, updated_at REAL
);

CREATE TABLE IF NOT EXISTS checkpoints (
  run_id TEXT NOT NULL, step INTEGER NOT NULL, parent_step INTEGER,
  state TEXT NOT NULL, frontier TEXT NOT NULL, arrivals TEXT NOT NULL,
  visits TEXT NOT NULL, status TEXT NOT NULL, interrupt TEXT, created_at REAL,
  PRIMARY KEY (run_id, step)
);

CREATE TABLE IF NOT EXISTS node_runs (
  run_id TEXT NOT NULL, step INTEGER NOT NULL, instance TEXT NOT NULL,
  node TEXT NOT NULL, attempt INTEGER NOT NULL, status TEXT NOT NULL,
  output TEXT, error TEXT, started_at REAL, ended_at REAL,
  tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0,
  PRIMARY KEY (run_id, step, instance, attempt)
);

CREATE TABLE IF NOT EXISTS effects (
  run_id TEXT NOT NULL, key TEXT NOT NULL, result TEXT, created_at REAL,
  PRIMARY KEY (run_id, key)
);

CREATE TABLE IF NOT EXISTS interrupts (
  run_id TEXT NOT NULL, step INTEGER NOT NULL, node TEXT NOT NULL,
  payload TEXT, answer TEXT, status TEXT NOT NULL, created_at REAL, answered_at REAL,
  PRIMARY KEY (run_id, step, node)
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, step INTEGER,
  node TEXT, ts REAL, type TEXT, payload TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, id);
CREATE INDEX IF NOT EXISTS idx_ckpt_run ON checkpoints(run_id, step);
"""


class SQLiteCheckpointer:
    """本地单文件持久化实现，同时兼任事件 sink。"""

    def __init__(self, path: str | Path = "state.db") -> None:
        self.path = str(path)
        self.sink_key = f"sqlite:{self.path}"
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = asyncio.Lock()
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def _exec(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        cur = self._conn.execute(sql, params)
        rows = cur.fetchall()
        self._conn.commit()
        return rows

    async def _run(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        async with self._lock:
            return await asyncio.to_thread(self._exec, sql, params)

    # ---- runs ----
    async def create_run(self, run_id: str, workflow: str, meta: dict[str, Any]) -> None:
        now = time.time()
        await self._run(
            "INSERT OR REPLACE INTO runs(run_id,workflow,status,meta,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?)",
            (run_id, workflow, "running", json.dumps(meta, ensure_ascii=False, default=str), now, now),
        )

    async def set_status(self, run_id: str, status: str, error: str | None = None) -> None:
        await self._run(
            "UPDATE runs SET status=?, error=?, updated_at=? WHERE run_id=?",
            (status, error, time.time(), run_id),
        )

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        rows = await self._run("SELECT * FROM runs WHERE run_id=?", (run_id,))
        return dict(rows[0]) if rows else None

    async def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = await self._run("SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    # ---- checkpoints ----
    async def save(self, ckpt: Checkpoint) -> None:
        r = ckpt.to_row()
        await self._run(
            "INSERT OR REPLACE INTO checkpoints"
            "(run_id,step,parent_step,state,frontier,arrivals,visits,status,interrupt,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (r["run_id"], r["step"], r["parent_step"], r["state"], r["frontier"],
             r["arrivals"], r["visits"], r["status"], r["interrupt"], r["created_at"]),
        )

    async def load_latest(self, run_id: str) -> Checkpoint | None:
        rows = await self._run(
            "SELECT * FROM checkpoints WHERE run_id=? ORDER BY step DESC LIMIT 1", (run_id,))
        return Checkpoint.from_row(rows[0]) if rows else None

    async def load_at(self, run_id: str, step: int) -> Checkpoint | None:
        rows = await self._run(
            "SELECT * FROM checkpoints WHERE run_id=? AND step=?", (run_id, step))
        return Checkpoint.from_row(rows[0]) if rows else None

    async def list_steps(self, run_id: str) -> list[int]:
        rows = await self._run(
            "SELECT step FROM checkpoints WHERE run_id=? ORDER BY step", (run_id,))
        return [r["step"] for r in rows]

    # ---- node runs ----
    async def record_node_run(
        self, run_id: str, step: int, instance: str, node: str, attempt: int,
        status: str, output: Any = None, error: str | None = None,
        started_at: float = 0.0, ended_at: float = 0.0,
        tokens_in: int = 0, tokens_out: int = 0,
    ) -> None:
        await self._run(
            "INSERT OR REPLACE INTO node_runs"
            "(run_id,step,instance,node,attempt,status,output,error,started_at,ended_at,tokens_in,tokens_out)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, step, instance, node, attempt, status,
             json.dumps(output, ensure_ascii=False, default=str) if output is not None else None,
             error, started_at, ended_at, tokens_in, tokens_out),
        )

    async def node_runs(self, run_id: str) -> list[dict[str, Any]]:
        rows = await self._run(
            "SELECT * FROM node_runs WHERE run_id=? ORDER BY step, instance, attempt", (run_id,))
        return [dict(r) for r in rows]

    # ---- effects（副作用幂等）----
    async def get_effect(self, run_id: str, key: str) -> tuple[bool, Any]:
        rows = await self._run("SELECT result FROM effects WHERE run_id=? AND key=?", (run_id, key))
        if not rows:
            return False, None
        return True, json.loads(rows[0]["result"]) if rows[0]["result"] is not None else None

    async def put_effect(self, run_id: str, key: str, result: Any) -> None:
        await self._run(
            "INSERT OR REPLACE INTO effects(run_id,key,result,created_at) VALUES(?,?,?,?)",
            (run_id, key, json.dumps(result, ensure_ascii=False, default=str), time.time()),
        )

    # ---- interrupts（人工介入）----
    async def put_interrupt(self, run_id: str, step: int, node: str, payload: Any) -> None:
        await self._run(
            "INSERT OR IGNORE INTO interrupts(run_id,step,node,payload,status,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (run_id, step, node, json.dumps(payload, ensure_ascii=False, default=str),
             "waiting", time.time()),
        )

    async def get_interrupt_answer(self, run_id: str, step: int, node: str) -> tuple[bool, Any]:
        rows = await self._run(
            "SELECT answer,status FROM interrupts WHERE run_id=? AND step=? AND node=?",
            (run_id, step, node))
        if not rows or rows[0]["status"] != "answered":
            return False, None
        return True, json.loads(rows[0]["answer"]) if rows[0]["answer"] is not None else None

    async def answer_interrupt(self, run_id: str, step: int, node: str, answer: Any) -> None:
        await self._run(
            "UPDATE interrupts SET answer=?, status='answered', answered_at=?"
            " WHERE run_id=? AND step=? AND node=?",
            (json.dumps(answer, ensure_ascii=False, default=str), time.time(), run_id, step, node))

    async def pending_interrupt(self, run_id: str) -> dict[str, Any] | None:
        rows = await self._run(
            "SELECT * FROM interrupts WHERE run_id=? AND status='waiting' ORDER BY step DESC LIMIT 1",
            (run_id,))
        if not rows:
            return None
        d = dict(rows[0])
        d["payload"] = json.loads(d["payload"]) if d["payload"] else None
        return d

    # ---- events ----
    async def append_event(self, event: Any) -> None:
        await self._run(
            "INSERT INTO events(run_id,step,node,ts,type,payload) VALUES(?,?,?,?,?,?)",
            (event.run_id, event.step, event.node, event.ts, event.type,
             json.dumps(event.payload, ensure_ascii=False, default=str)),
        )

    async def events(self, run_id: str, limit: int = 1000) -> list[dict[str, Any]]:
        rows = await self._run(
            "SELECT * FROM events WHERE run_id=? ORDER BY id LIMIT ?", (run_id, limit))
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d["payload"]) if d["payload"] else {}
            out.append(d)
        return out

    async def usage(self, run_id: str) -> dict[str, int]:
        rows = await self._run(
            "SELECT COALESCE(SUM(tokens_in),0) ti, COALESCE(SUM(tokens_out),0) to_"
            " FROM node_runs WHERE run_id=?", (run_id,))
        return {"tokens_in": rows[0]["ti"], "tokens_out": rows[0]["to_"]}

    # 兼容 EventSink 协议：同步入口，交给后台任务落库
    def handle(self, event: Any) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._exec(
                "INSERT INTO events(run_id,step,node,ts,type,payload) VALUES(?,?,?,?,?,?)",
                (event.run_id, event.step, event.node, event.ts, event.type,
                 json.dumps(event.payload, ensure_ascii=False, default=str)))
            return
        loop.create_task(self.append_event(event))


class MemoryCheckpointer:
    """测试用内存实现，接口与 SQLiteCheckpointer 一致。"""

    def __init__(self) -> None:
        self.runs: dict[str, dict[str, Any]] = {}
        self.ckpts: dict[tuple[str, int], Checkpoint] = {}
        self.effects: dict[tuple[str, str], Any] = {}
        self.interrupts: dict[tuple[str, int, str], dict[str, Any]] = {}
        self.node_runs_log: list[dict[str, Any]] = []
        self.events_log: list[Any] = []

    async def create_run(self, run_id: str, workflow: str, meta: dict[str, Any]) -> None:
        self.runs[run_id] = {"run_id": run_id, "workflow": workflow, "status": "running",
                             "meta": meta, "error": None, "created_at": time.time()}

    async def set_status(self, run_id: str, status: str, error: str | None = None) -> None:
        self.runs.setdefault(run_id, {"run_id": run_id})["status"] = status
        self.runs[run_id]["error"] = error

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        return self.runs.get(run_id)

    async def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        return list(self.runs.values())[:limit]

    async def save(self, ckpt: Checkpoint) -> None:
        self.ckpts[(ckpt.run_id, ckpt.step)] = Checkpoint.from_row(ckpt.to_row())

    async def load_latest(self, run_id: str) -> Checkpoint | None:
        steps = [s for (r, s) in self.ckpts if r == run_id]
        return self.ckpts[(run_id, max(steps))] if steps else None

    async def load_at(self, run_id: str, step: int) -> Checkpoint | None:
        return self.ckpts.get((run_id, step))

    async def list_steps(self, run_id: str) -> list[int]:
        return sorted(s for (r, s) in self.ckpts if r == run_id)

    async def record_node_run(self, **kw: Any) -> None:
        self.node_runs_log.append(kw)

    async def node_runs(self, run_id: str) -> list[dict[str, Any]]:
        return [n for n in self.node_runs_log if n.get("run_id") == run_id]

    async def get_effect(self, run_id: str, key: str) -> tuple[bool, Any]:
        k = (run_id, key)
        return (True, self.effects[k]) if k in self.effects else (False, None)

    async def put_effect(self, run_id: str, key: str, result: Any) -> None:
        self.effects[(run_id, key)] = result

    async def put_interrupt(self, run_id: str, step: int, node: str, payload: Any) -> None:
        self.interrupts.setdefault(
            (run_id, step, node),
            {"run_id": run_id, "step": step, "node": node, "payload": payload,
             "status": "waiting", "answer": None})

    async def get_interrupt_answer(self, run_id: str, step: int, node: str) -> tuple[bool, Any]:
        rec = self.interrupts.get((run_id, step, node))
        if not rec or rec["status"] != "answered":
            return False, None
        return True, rec["answer"]

    async def answer_interrupt(self, run_id: str, step: int, node: str, answer: Any) -> None:
        rec = self.interrupts.setdefault(
            (run_id, step, node), {"run_id": run_id, "step": step, "node": node, "payload": None})
        rec["answer"] = answer
        rec["status"] = "answered"

    async def pending_interrupt(self, run_id: str) -> dict[str, Any] | None:
        cands = [v for k, v in self.interrupts.items() if k[0] == run_id and v["status"] == "waiting"]
        return max(cands, key=lambda v: v["step"]) if cands else None

    async def append_event(self, event: Any) -> None:
        self.events_log.append(event)

    async def events(self, run_id: str, limit: int = 1000) -> list[dict[str, Any]]:
        return [e.to_dict() for e in self.events_log if e.run_id == run_id][:limit]

    async def usage(self, run_id: str) -> dict[str, int]:
        rows = [n for n in self.node_runs_log if n.get("run_id") == run_id]
        return {"tokens_in": sum(n.get("tokens_in", 0) for n in rows),
                "tokens_out": sum(n.get("tokens_out", 0) for n in rows)}

    def handle(self, event: Any) -> None:
        self.events_log.append(event)
