"""Stage-2 sandbox tools — OpenSandbox-backed file/exec surface.

Every tool here mirrors one host tool that is listed in ``tools.disabled``
(``read_file``, ``write_file``, ... ``execute_code``), but operates on paths
*inside* an OpenSandbox container instead of the host filesystem.

Design notes:

- One lazily-created sandbox is reused across tool calls and recycled after
  ``idle_timeout_s`` of inactivity (see ``_acquire``).
- The sandbox server is an HTTP service (``opensandbox-server``); it is reached
  via ``ConnectionConfig(domain=..., api_key=...)``. When it is down, tools
  return a readable error instead of raising.
- SDK calls used here were verified by signature introspection against
  ``opensandbox==1.2.0`` — see ``docs/stage2-tool-mapping.md``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import posixpath
import re
import shlex
import socket
import subprocess
import time
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

log = logging.getLogger("protoagent.plugins.sbx_tools")

# --------------------------------------------------------------------------- #
# resolved config + single reused sandbox
# --------------------------------------------------------------------------- #

CFG: dict[str, Any] = {
    "domain": "localhost:8090",
    "image": "docgen-base:2",
    "workdir": "/workspace",
    "idle_timeout_s": 0,
    "ready_timeout_s": 60,
    "lifetime_min": 120,
    "command_timeout_s": 120,
    "max_output_chars": 20000,
    "api_key": "",
    # 走 SSH 隧道/反向代理时必须为 True：否则 SDK 会直连沙箱映射的 127.0.0.1:<随机端口>，跨网络必然失败
    "server_proxy": True,
    # SDK 默认 30s 太短，首次创建沙箱（拉镜像/启动 execd）常超时
    "request_timeout_s": 240,
    # 隧道断了就自动重开（已配免密时无需人工窗口）
    "auto_tunnel": True,
    "ssh_target": "root@47.101.71.56",
    "ssh_remote_host": "127.0.0.1",
    "ssh_remote_port": 8080,
    "tunnel_wait_s": 15,
    # 连续失败即熔断，阻止模型在坏环境里反复重试烧 token
    "failure_threshold": 3,
    "circuit_cooldown_s": 60,
}

_LOCK = asyncio.Lock()
_SANDBOX = None
_CREATED_AT = 0.0
_LAST_USED = 0.0
_FAIL_COUNT = 0
_FAIL_LAST = 0.0
_OPEN_UNTIL = 0.0


def _circuit_remaining() -> float:
    return max(0.0, _OPEN_UNTIL - time.monotonic())


def _note_failure() -> bool:
    """Count a create/connect failure; open the breaker at the threshold."""
    global _FAIL_COUNT, _FAIL_LAST, _OPEN_UNTIL
    now = time.monotonic()
    if now - _FAIL_LAST > 300:  # 失败计数按 5 分钟窗口滚动
        _FAIL_COUNT = 0
    _FAIL_COUNT += 1
    _FAIL_LAST = now
    if _FAIL_COUNT >= _int("failure_threshold"):
        _OPEN_UNTIL = now + _int("circuit_cooldown_s")
        log.warning("[sbx] circuit open for %ss after %s failures",
                    _int("circuit_cooldown_s"), _FAIL_COUNT)
        return True
    return False


def _note_success() -> None:
    global _FAIL_COUNT
    _FAIL_COUNT = 0


def _int(key: str) -> int:
    try:
        return int(CFG[key])
    except (TypeError, ValueError):
        return 1


def _connection():
    from opensandbox.config.connection import ConnectionConfig

    return ConnectionConfig(
        domain=CFG["domain"] or None,
        api_key=CFG["api_key"] or None,
        request_timeout=timedelta(seconds=_int("request_timeout_s")),
        use_server_proxy=bool(CFG.get("server_proxy", True)),
    )


def _err(exc: BaseException, action: str) -> str:
    name = type(exc).__name__
    msg = str(exc) or name
    hint = ""
    lowered = msg.lower()
    if "<html" in lowered or "<!doctype" in lowered:
        clean = " ".join(re.sub(r"<[^>]+>", " ", msg).split())[:200]
        msg = clean
        if "404" in lowered:
            hint = (
                f"\n\n{CFG['domain']} answered HTTP 404 with an HTML page — that port is served by "
                "something that is not opensandbox-server. Start the sandbox server on a free port "
                "and set sbx_tools.domain to it."
            )
    elif any(k in name.lower() for k in ("connect", "transport", "timeout", "network")) or "connect" in lowered:
        hint = (
            f"\n\nThe sandbox server at {CFG['domain']} is not reachable. Start it with:"
            "\n  uvx opensandbox-server   (needs Docker running), then retry."
        )
    log.warning("[sbx] %s failed: %s: %s", action, name, msg)
    return f"ERROR: {action} failed — {name}: {msg}{hint}"


def _p(path: str) -> str:
    """Resolve a (possibly relative) path against the sandbox work directory."""
    if not path:
        path = "."
    if path.startswith("/"):
        return path
    return posixpath.normpath(posixpath.join(CFG["workdir"], path))


def _clip(text: str) -> str:
    limit = _int("max_output_chars")
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… [truncated, {len(text) - limit} chars omitted]"


def _split_domain() -> tuple[str, int]:
    """Turn "localhost:18090" / "http://host:port" into (host, port)."""
    raw = str(CFG.get("domain") or "").strip()
    if "://" in raw:
        raw = raw.split("://", 1)[1]
    raw = raw.strip("/")
    host, sep, port = raw.rpartition(":")
    if not sep:
        return raw or "127.0.0.1", 80
    try:
        return host or "127.0.0.1", int(port)
    except ValueError:
        return host or "127.0.0.1", 80


def _port_open(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout):
            return True
    except OSError:
        return False


_TUNNEL_PROC = None


def _ensure_tunnel() -> str | None:
    """Make sure the local tunnel port is open; reopen it via ssh if needed.

    Returns None on success, or a short error description. Blocking — call it
    from a worker thread.
    """
    global _TUNNEL_PROC
    host, port = _split_domain()
    if _port_open(host, port):
        return None
    if not bool(CFG.get("auto_tunnel", True)):
        return "auto_tunnel is disabled and the tunnel port is closed"
    target = str(CFG.get("ssh_target") or "").strip()
    if not target:
        return "ssh_target is empty (cannot reopen the tunnel)"

    if _TUNNEL_PROC is None or _TUNNEL_PROC.poll() is not None:
        cmd = [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3",
            "-N",
            "-L", f"{port}:{CFG['ssh_remote_host']}:{CFG['ssh_remote_port']}",
            target,
        ]
        flags = 0
        if os.name == "nt":
            # 脱离服务进程，服务重启后隧道仍可用
            flags = 0x00000008 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        try:
            _TUNNEL_PROC = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
                close_fds=True,
            )
            log.info("[sbx] reopening ssh tunnel %s:%s -> %s:%s", port, host,
                     CFG["ssh_remote_host"], CFG["ssh_remote_port"])
        except Exception as exc:  # noqa: BLE001
            return f"cannot start ssh: {exc}"

    deadline = time.monotonic() + float(CFG.get("tunnel_wait_s") or 15)
    while time.monotonic() < deadline:
        if _port_open(host, port, timeout=1.0):
            log.info("[sbx] ssh tunnel is up on %s:%s", host, port)
            return None
        if _TUNNEL_PROC.poll() is not None:
            return "ssh exited immediately (check key auth and the server address)"
        time.sleep(0.5)
    return f"ssh tunnel did not come up within {CFG.get('tunnel_wait_s')}s"


async def _create() -> Any:
    from opensandbox import Sandbox

    tunnel_err = await asyncio.to_thread(_ensure_tunnel)
    if tunnel_err:
        raise RuntimeError(
            f"sandbox server {CFG['domain']} is unreachable ({tunnel_err}). "
            "Check that opensandbox-server is running on the remote host, or start "
            "the tunnel manually: ssh -N -L 18090:127.0.0.1:8080 root@47.101.71.56"
        )

    log.info("[sbx] creating sandbox image=%s domain=%s", CFG["image"], CFG["domain"])
    sandbox = await Sandbox.create(
        CFG["image"],
        timeout=timedelta(minutes=_int("lifetime_min")),
        ready_timeout=timedelta(seconds=_int("ready_timeout_s")),
        connection_config=_connection(),
    )
    log.info("[sbx] sandbox ready id=%s", getattr(sandbox, "sandbox_id", "?"))
    # 新容器里 workdir 并不存在；不预建的话首个 commands.run 会 400
    # （working directory does not exist）。
    try:
        await sandbox.files.create_directories(
            [_mk_dir_entry(CFG["workdir"]), _mk_dir_entry("/tmp/sbx_exec")]
        )
    except Exception:  # noqa: BLE001 - 已存在时忽略
        log.debug("[sbx] workdir pre-create skipped", exc_info=True)
    return sandbox


async def _destroy_locked() -> None:
    global _SANDBOX, _CREATED_AT, _LAST_USED
    if _SANDBOX is None:
        return
    sandbox, _SANDBOX = _SANDBOX, None
    _CREATED_AT = _LAST_USED = 0.0
    try:
        await sandbox.destroy()
    except Exception as exc:  # noqa: BLE001 - best effort teardown
        try:
            await sandbox.kill()
        except Exception:  # noqa: BLE001
            log.warning("[sbx] destroy/kill of stale sandbox failed: %s", exc)


async def _acquire() -> Any:
    """Return the live sandbox, creating (or recycling) it as needed."""
    global _SANDBOX, _CREATED_AT, _LAST_USED
    async with _LOCK:
        remain = _circuit_remaining()
        if remain > 0:
            raise RuntimeError(
                f"circuit breaker open for {remain:.0f}s after "
                f"{_FAIL_COUNT} consecutive sandbox failures. Stop calling sbx_* tools "
                "this turn and tell the user the capability is temporarily unavailable."
            )
        idle_limit = _int("idle_timeout_s")
        if _SANDBOX is not None and idle_limit > 0 and (time.monotonic() - _LAST_USED) > idle_limit:
            log.info("[sbx] idle timeout hit — recycling sandbox")
            await _destroy_locked()
        if _SANDBOX is None:
            try:
                _SANDBOX = await _create()
            except Exception:
                _note_failure()
                raise
            _note_success()
            _CREATED_AT = time.monotonic()
        _LAST_USED = time.monotonic()
        return _SANDBOX


async def _drop() -> None:
    """Forget the current sandbox (after a failed call) and tear it down."""
    async with _LOCK:
        await _destroy_locked()


def _out(logs: Any) -> tuple[str, str]:
    def _join(items: Any) -> str:
        parts = []
        for m in items or []:
            parts.append(getattr(m, "text", None) or str(m))
        return "".join(parts)

    return _join(getattr(logs, "stdout", None)), _join(getattr(logs, "stderr", None))


def _fmt_entries(entries: Any) -> str:
    rows = []
    for e in entries or []:
        kind = (getattr(e, "entry_type", None) or "?")[:4]
        size = getattr(e, "size", 0) or 0
        rows.append(f"{kind:4} {size:>10}  {getattr(e, 'path', '?')}")
    return "\n".join(rows) if rows else "(empty)"


# --------------------------------------------------------------------------- #
# sandbox lifecycle
# --------------------------------------------------------------------------- #


@tool
async def sbx_sandbox_create(image: str = "", workdir: str = "") -> str:
    """Create (or return) the reusable sandbox container.

    Args:
        image: container image override, e.g. "python:3.11-slim". Empty = configured default.
        workdir: working directory to create and use inside the sandbox. Empty = configured default.

    Returns a short status line with the sandbox id. Calling it when a sandbox
    is already live just reports the live one — it does not create a second one.
    """
    if image:
        CFG["image"] = image
    if workdir:
        CFG["workdir"] = workdir
    try:
        sandbox = await _acquire()
    except Exception as exc:  # noqa: BLE001
        return _err(exc, "sbx_sandbox_create")
    try:
        await sandbox.files.create_directories([_mk_dir_entry(CFG["workdir"])])
    except Exception:  # noqa: BLE001 - directory may already exist
        log.debug("[sbx] workdir pre-create skipped", exc_info=True)
    sid = getattr(sandbox, "sandbox_id", None) or getattr(sandbox, "id", "unknown")
    return f"OK sandbox live: id={sid} image={CFG['image']} workdir={CFG['workdir']}"


def _mk_dir_entry(path: str):
    from opensandbox.models.filesystem import WriteEntry

    return WriteEntry(path=path, mode=755)


@tool
async def sbx_sandbox_destroy(action: str = "destroy") -> str:
    """Tear down the sandbox.

    Args:
        action: "destroy" (default, removes the container), "kill" (immediate
            termination) or "pause" (stop but keep for resume).
    """
    global _SANDBOX, _CREATED_AT, _LAST_USED
    async with _LOCK:
        if _SANDBOX is None:
            return "OK no live sandbox"
        sandbox = _SANDBOX
        try:
            if action == "pause":
                await sandbox.pause()
                _SANDBOX = None
                _CREATED_AT = _LAST_USED = 0.0
                return f"OK sandbox paused: id={getattr(sandbox, 'sandbox_id', '?')}"
            if action == "kill":
                await sandbox.kill()
            else:
                await sandbox.destroy()
        except Exception as exc:  # noqa: BLE001
            _SANDBOX = None
            _CREATED_AT = _LAST_USED = 0.0
            return _err(exc, f"sbx_sandbox_{action}")
        _SANDBOX = None
        _CREATED_AT = _LAST_USED = 0.0
    return f"OK sandbox {action}ed"


def _server_reachable() -> bool:
    """Cheap reachability probe for the sandbox server (used by sbx_sandbox_info)."""
    import urllib.error
    import urllib.request

    domain = str(CFG["domain"] or "")
    if not domain:
        return False
    url = domain if "://" in domain else f"http://{domain}"
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/docs", timeout=5) as resp:
            return 200 <= int(getattr(resp, "status", 200)) < 400
    except (urllib.error.URLError, OSError, ValueError):
        return False


@tool
async def sbx_sandbox_info() -> str:
    """Report sandbox configuration, liveness and server reachability (no side effects)."""
    async with _LOCK:
        sandbox = _SANDBOX
        uptime = (time.monotonic() - _CREATED_AT) if sandbox is not None else 0.0
        idle = (time.monotonic() - _LAST_USED) if sandbox is not None else 0.0
    if sandbox is None:
        up = await asyncio.to_thread(_server_reachable)
        if up:
            return (
                f"sandbox status=not_created (normal, lazily created on first use) "
                f"server={CFG['domain']} reachable=yes image={CFG['image']} "
                f"workdir={CFG['workdir']} idle_timeout={_int('idle_timeout_s')}s\n"
                "No sandbox container exists yet because none was needed. The next "
                "sbx_* call creates one automatically — this is NOT an outage."
            )
        return (
            f"sandbox status=not_created server={CFG['domain']} reachable=NO "
            f"image={CFG['image']} workdir={CFG['workdir']}\n"
            "The sandbox server is unreachable (SSH tunnel closed or opensandbox-server "
            "stopped). Reopen the tunnel before using sbx_* tools."
        )
    try:
        healthy = await sandbox.is_healthy()
        info = await sandbox.get_info()
        dump = info.model_dump() if hasattr(info, "model_dump") else str(info)
    except Exception as exc:  # noqa: BLE001
        return _err(exc, "sbx_sandbox_info")
    return _clip(
        f"sandbox id={getattr(sandbox, 'sandbox_id', '?')} healthy={healthy} "
        f"uptime={uptime:.0f}s idle={idle:.0f}s image={CFG['image']} workdir={CFG['workdir']}\n"
        f"info={dump}"
    )


# --------------------------------------------------------------------------- #
# filesystem
# --------------------------------------------------------------------------- #


@tool
async def sbx_read_file(path: str, offset: int = 0, limit: int = 0) -> str:
    """Read a text file from inside the sandbox.

    Args:
        path: sandbox path, absolute ("/workspace/a.md") or relative to the workdir.
        offset: start offset in bytes. 0 = beginning.
        limit: max bytes to read. 0 = whole file.
    """
    try:
        sandbox = await _acquire()
        text = await sandbox.files.read_file(
            _p(path),
            offset=offset or None,
            limit=limit or None,
        )
    except Exception as exc:  # noqa: BLE001
        return _err(exc, "sbx_read_file")
    return _clip(text if text is not None else "")


@tool
async def sbx_write_file(path: str, data: str, mode: str = "644") -> str:
    """Write (create or overwrite) a text file inside the sandbox.

    Args:
        path: sandbox path, absolute or relative to the workdir.
        data: full file content.
        mode: octal permission string, default "644".
    """
    target = _p(path)
    try:
        sandbox = await _acquire()
        parent = posixpath.dirname(target)
        if parent and parent != "/":
            await sandbox.files.create_directories([_mk_dir_entry(parent)])
        await sandbox.files.write_file(target, data, mode=int(str(mode).removeprefix("0o"), 8))
    except Exception as exc:  # noqa: BLE001
        return _err(exc, "sbx_write_file")
    return f"OK wrote {len(data)} chars to {target}"


@tool
async def sbx_edit_file(path: str, old_content: str, new_content: str) -> str:
    """Exact-string replacement inside a sandbox file.

    Args:
        path: sandbox path.
        old_content: exact text to find (include enough context to be unique).
        new_content: replacement text.

    Use sbx_write_file to rewrite a whole file; this tool is for surgical edits.
    """
    try:
        sandbox = await _acquire()
        await sandbox.files.replace_contents(
            [_mk_replace(_p(path), old_content, new_content)]
        )
    except Exception as exc:  # noqa: BLE001
        return _err(exc, "sbx_edit_file")
    return f"OK replaced in {_p(path)}"


def _mk_replace(path: str, old: str, new: str):
    from opensandbox.models.filesystem import ContentReplaceEntry

    return ContentReplaceEntry(path=path, old_content=old, new_content=new)


@tool
async def sbx_delete_file(path: str, kind: str = "auto") -> str:
    """Delete a file or directory inside the sandbox.

    Args:
        path: sandbox path.
        kind: "auto" (stat first), "file" or "dir".
    """
    target = _p(path)
    try:
        sandbox = await _acquire()
        is_dir = kind == "dir"
        if kind == "auto":
            info = await sandbox.files.get_file_info([target])
            entry = info.get(target) if isinstance(info, dict) else None
            etype = (getattr(entry, "entry_type", None) or "").lower()
            is_dir = "dir" in etype
        if is_dir:
            await sandbox.files.delete_directories([target])
        else:
            await sandbox.files.delete_files([target])
    except Exception as exc:  # noqa: BLE001
        return _err(exc, "sbx_delete_file")
    return f"OK deleted {'directory' if is_dir else 'file'} {target}"


@tool
async def sbx_list_dir(path: str = ".", depth: int = 1) -> str:
    """List a sandbox directory.

    Args:
        path: sandbox path, default the workdir.
        depth: recursion depth, 1 = direct children only.
    """
    target = _p(path)
    try:
        from opensandbox.models.filesystem import DirectoryListEntry

        sandbox = await _acquire()
        entries = await sandbox.files.list_directory(
            DirectoryListEntry(path=target, depth=max(1, depth))
        )
    except Exception as exc:  # noqa: BLE001
        return _err(exc, "sbx_list_dir")
    return _clip(f"{target}\n{_fmt_entries(entries)}")


@tool
async def sbx_find_files(pattern: str, path: str = ".") -> str:
    """Find files by name pattern inside the sandbox.

    Args:
        pattern: name pattern understood by the sandbox's search (e.g. "*.py").
        path: directory to search under, default the workdir.
    """
    target = _p(path)
    try:
        from opensandbox.models.filesystem import SearchEntry

        sandbox = await _acquire()
        entries = await sandbox.files.search(SearchEntry(path=target, pattern=pattern))
    except Exception as exc:  # noqa: BLE001
        return _err(exc, "sbx_find_files")
    if not entries:
        return f"no matches for {pattern!r} under {target}"
    return _clip("\n".join(getattr(e, "path", "?") for e in entries))


@tool
async def sbx_search_files(pattern: str, path: str = ".", regex: bool = False) -> str:
    """Search file *contents* inside the sandbox (grep -rn).

    Args:
        pattern: text or regex to look for.
        path: directory or file to search, default the workdir.
        regex: treat pattern as an extended regex.
    """
    target = _p(path)
    flag = "-E" if regex else "-F"
    cmd = f"grep -rn {flag} {shlex.quote(pattern)} {shlex.quote(target)}"
    try:
        sandbox = await _acquire()
        execution = await sandbox.commands.run(
            f"bash -lc {shlex.quote(cmd)}",
            opts=_opts(CFG["workdir"], _int("command_timeout_s")),
        )
    except Exception as exc:  # noqa: BLE001
        return _err(exc, "sbx_search_files")
    out, err = _out(getattr(execution, "logs", None))
    if not out.strip():
        code = getattr(execution, "exit_code", None)
        return f"no matches for {pattern!r} under {target}" + (f" ({err.strip()[:400]})" if err.strip() else f" (exit {code})")
    return _clip(out)


def _opts(workdir: str, timeout_s: int, envs: dict[str, str] | None = None):
    from opensandbox.models.execd import RunCommandOpts

    return RunCommandOpts(
        working_directory=workdir or None,
        timeout=timedelta(seconds=max(1, timeout_s)),
        envs=envs or None,
    )


# --------------------------------------------------------------------------- #
# execution
# --------------------------------------------------------------------------- #


@tool
async def sbx_run_command(command: str, workdir: str = "", timeout_s: int = 0, envs: str = "") -> str:
    """Run a shell command inside the sandbox (bash -lc) and return its output.

    Args:
        command: shell command line.
        workdir: directory to run in, default the configured workdir.
        timeout_s: seconds before the command is killed, 0 = configured default.
        envs: optional "KEY=VALUE" pairs separated by newlines or semicolons.
    """
    env_map: dict[str, str] = {}
    for chunk in (envs or "").replace(";", "\n").splitlines():
        if "=" in chunk:
            k, v = chunk.split("=", 1)
            if k.strip():
                env_map[k.strip()] = v.strip()
    cwd = workdir or CFG["workdir"]
    try:
        sandbox = await _acquire()
        execution = await sandbox.commands.run(
            f"bash -lc {shlex.quote(command)}",
            opts=_opts(cwd, timeout_s or _int("command_timeout_s"), env_map),
        )
    except Exception as exc:  # noqa: BLE001
        return _err(exc, "sbx_run_command")
    out, err = _out(getattr(execution, "logs", None))
    code = getattr(execution, "exit_code", None)
    parts = [f"exit_code: {code}"]
    if out:
        parts.append(f"stdout:\n{out}")
    if err:
        parts.append(f"stderr:\n{err}")
    return _clip("\n".join(parts))


@tool
async def sbx_execute_code(code: str, language: str = "python", timeout_s: int = 0) -> str:
    """Execute a snippet inside the sandbox and return its output.

    Args:
        code: source code. Multi-line is fine.
        language: "python" (default) or "bash".
        timeout_s: seconds before the run is killed, 0 = configured default.

    Python snippets are written to a temp file and run with python3, so
    tracebacks and produced files survive the call.
    """
    lang = (language or "python").lower()
    ext = "sh" if lang in ("bash", "shell", "sh") else "py"
    path = f"/tmp/sbx_exec/{uuid.uuid4().hex}.{ext}"
    try:
        sandbox = await _acquire()
        await sandbox.files.create_directories([_mk_dir_entry("/tmp/sbx_exec")])
        await sandbox.files.write_file(path, code, mode=644)
        runner = "bash" if ext == "sh" else "python3"
        execution = await sandbox.commands.run(
            f"{runner} {shlex.quote(path)}",
            opts=_opts(CFG["workdir"], timeout_s or _int("command_timeout_s")),
        )
    except Exception as exc:  # noqa: BLE001
        return _err(exc, "sbx_execute_code")
    out, err = _out(getattr(execution, "logs", None))
    code_exit = getattr(execution, "exit_code", None)
    parts = [f"exit_code: {code_exit}"]
    if out:
        parts.append(f"stdout:\n{out}")
    if err:
        parts.append(f"stderr:\n{err}")
    return _clip("\n".join(parts))


# --------------------------------------------------------------------------- #
# host <-> sandbox transfer
# --------------------------------------------------------------------------- #


@tool
async def sbx_upload_file(local_path: str, sandbox_path: str) -> str:
    """Copy a host file into the sandbox.

    Args:
        local_path: absolute host path to read.
        sandbox_path: destination path inside the sandbox.
    """
    src = Path(local_path)
    if not src.is_file():
        return f"ERROR: host file not found: {local_path}"
    target = _p(sandbox_path)
    try:
        payload = src.read_bytes()
        sandbox = await _acquire()
        parent = posixpath.dirname(target)
        if parent and parent != "/":
            await sandbox.files.create_directories([_mk_dir_entry(parent)])
        await sandbox.files.write_file(target, payload, mode=644)
    except Exception as exc:  # noqa: BLE001
        return _err(exc, "sbx_upload_file")
    return f"OK uploaded {len(payload)} bytes → {target}"


_PLUGIN_ID = "sbx_tools"


def _publish_to_panel(path: Path) -> str:
    """Register the downloaded file with the Artifact plugin so it shows up in the panel.

    Uses the ``artifact.save_file`` service (ADR 0116/#4253); returns a short suffix for
    the tool reply, or "" when the artifact plugin is unavailable. Blocking — caller runs
    it in a worker thread.
    """
    try:
        from graph import sdk

        save = sdk.service("artifact.save_file")
        if save is None:
            return ""
        result = save(
            path=str(path),
            title=path.name,
            plugin_id=_PLUGIN_ID,
            roots=[str(path.parent.resolve())],
        )
        if result.get("ok"):
            return f"\nartifact: id={result['id']} v{result['version']} ({path.name} 已进 Artifact 面板)"
        log.warning("[sbx] artifact panel refused %s: %s", path, result.get("message"))
        return f"\n(artifact panel: {result.get('message')})"
    except Exception as exc:  # noqa: BLE001 — panel registration is best-effort
        log.warning("[sbx] artifact publish failed for %s: %s", path, exc)
        return ""


@tool
async def sbx_download_file(sandbox_path: str, local_path: str) -> str:
    """Copy a sandbox file back to the host.

    Args:
        sandbox_path: path inside the sandbox.
        local_path: absolute host destination path (parent dirs are created).

    The file is also registered with the Artifact panel (when that plugin is enabled),
    so the user can preview/download it in the console instead of hunting on disk.
    """
    source = _p(sandbox_path)
    dest = Path(local_path)
    try:
        sandbox = await _acquire()
        payload = await sandbox.files.read_bytes(source)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(payload)
    except Exception as exc:  # noqa: BLE001
        return _err(exc, "sbx_download_file")
    extra = await asyncio.to_thread(_publish_to_panel, dest)
    return f"OK downloaded {len(payload)} bytes → {dest}{extra}"


# --------------------------------------------------------------------------- #
# registration
# --------------------------------------------------------------------------- #

TOOLS = [
    sbx_sandbox_create,
    sbx_sandbox_destroy,
    sbx_sandbox_info,
    sbx_read_file,
    sbx_write_file,
    sbx_edit_file,
    sbx_delete_file,
    sbx_list_dir,
    sbx_find_files,
    sbx_search_files,
    sbx_run_command,
    sbx_execute_code,
    sbx_upload_file,
    sbx_download_file,
]


def register(registry) -> None:
    """Entry point — merge config, then publish every tool."""
    global _PLUGIN_ID
    _PLUGIN_ID = getattr(registry, "plugin_id", None) or "sbx_tools"
    cfg = getattr(registry, "config", None) or {}
    for key in list(CFG):
        value = cfg.get(key)
        if value not in (None, ""):
            CFG[key] = value
    for tool_fn in TOOLS:
        registry.register_tool(tool_fn)
    log.info(
        "[sbx] registered %d tools (domain=%s image=%s workdir=%s)",
        len(TOOLS),
        CFG["domain"],
        CFG["image"],
        CFG["workdir"],
    )
