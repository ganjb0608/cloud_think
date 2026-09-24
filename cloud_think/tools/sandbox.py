"""skill 脚本的执行沙箱。

skill 可能来自外部分享，不能当可信代码跑。这里做四件事：
独立子进程、超时、cwd 锁定在 run 目录、环境变量白名单 + 输出上限。

注意这是"最小防线"，不是安全隔离。真要跑不可信 skill，外面还得套容器。
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.errors import SandboxError

#: 传给子进程的环境变量白名单。其余一律不传，防止 API key 泄给第三方 skill。
ENV_ALLOWLIST = ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "PYTHONPATH", "PYTHONIOENCODING")

MAX_OUTPUT_BYTES = 1 << 20  # 1MB


@dataclass
class ScriptResult:
    ok: bool
    stdout: str
    stderr: str
    returncode: int
    elapsed: float


def _run_sync(argv: list[str], stdin_data: str, cwd: str, timeout: float,
              env: dict[str, str]) -> tuple[int, str, str]:
    proc = subprocess.run(  # noqa: S603 - argv 固定为解释器 + 已校验路径
        argv, input=stdin_data, cwd=cwd, env=env, timeout=timeout,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return proc.returncode, proc.stdout, proc.stderr


async def run_script(
    script: Path, payload: Any, cwd: Path, timeout: float = 60.0,
    extra_env: dict[str, str] | None = None, allowed_root: Path | None = None,
) -> ScriptResult:
    """以 ``python script`` 执行，stdin 传 JSON，stdout 收 JSON。

    这个约定让脚本既能被引擎当节点调用，也能在命令行单独调试。
    """
    script = script.resolve()
    if not script.is_file():
        raise SandboxError(f"脚本不存在: {script}")
    if allowed_root is not None:
        root = allowed_root.resolve()
        if not str(script).startswith(str(root)):
            raise SandboxError(f"脚本 {script} 超出允许目录 {root}")

    cwd.mkdir(parents=True, exist_ok=True)
    env = {k: os.environ[k] for k in ENV_ALLOWLIST if k in os.environ}
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(extra_env or {})

    stdin_data = json.dumps(payload, ensure_ascii=False, default=str)
    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        rc, out, err = await asyncio.to_thread(
            _run_sync, [sys.executable, str(script)], stdin_data, str(cwd), timeout, env)
    except subprocess.TimeoutExpired as e:
        raise SandboxError(f"脚本 {script.name} 超时（{timeout}s）") from e
    elapsed = loop.time() - started

    if len(out.encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise SandboxError(f"脚本 {script.name} 输出超过 {MAX_OUTPUT_BYTES} 字节")
    if rc != 0:
        raise SandboxError(f"脚本 {script.name} 退出码 {rc}\nstderr:\n{err[:2000]}")
    return ScriptResult(True, out, err, rc, elapsed)


async def run_script_json(script: Path, payload: Any, cwd: Path, **kw: Any) -> Any:
    """执行脚本并把 stdout 解析成 JSON。"""
    res = await run_script(script, payload, cwd, **kw)
    text = res.stdout.strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise SandboxError(
            f"脚本 {script.name} 的 stdout 不是合法 JSON: {text[:300]!r}") from e
