"""多助理路由：会话绑定、人设注入、私有记忆隔离。

设计约束（对应 protoAgent 0.219.0 的插件扩展点）：
- 不修改核心源码 / 核心表结构。助理与会话的绑定关系放在插件自己的
  SQLite 文件 ``runtime/data/assistants.db``；
- 私有记忆复用 knowledge store 的 ``namespace`` 列（ADR 0021），并统一用
  ``delivery_policy="on_demand"`` 写入，避免进入全局 hot / RAG 自动注入；
- 每轮只把「当前助理」的 namespace 记忆拼进 system prompt，因此两个助理
  互不可见；未绑定会话完全沿用原有全局行为（向后兼容）；
- 在已绑定会话里调用核心 ``memory_ingest`` 时，由 ``wrap_tool_call`` 接管，
  自动补上 namespace 并改写为 on-demand，同样不污染全局热记忆。

用户入口是 ``/assistant`` 聊天命令（user-only，模型无法自行调用）：
  /assistant                 查看助理列表与当前绑定
  /assistant <id>            把当前会话切到某个助理
  /assistant new <id> [名称] 新建人设文件
  /assistant off             解除绑定，回到全局助理
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import SystemMessage, ToolMessage

log = logging.getLogger("protoagent.plugins.assistant")

CFG: dict[str, Any] = {
    "enabled": True,
    "inject_limit": 20,
    "max_inject_chars": 3000,
    "persona_dir": "",
}

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,40}$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS assistants (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    persona     TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS session_bindings (
    session_id  TEXT PRIMARY KEY,
    assistant_id TEXT NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS events (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    assistant_id       TEXT NOT NULL,
    event_type         TEXT NOT NULL,
    event_date         TEXT NOT NULL,
    remind_days_before INTEGER NOT NULL DEFAULT 3,
    message            TEXT,
    last_reminded_on   TEXT,
    created_at         TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def _box_root() -> Path:
    env = os.environ.get("PROTOAGENT_BOX_ROOT")
    if env:
        return Path(env)
    # plugins/assistant -> plugins -> default -> protoagent
    return Path(__file__).resolve().parents[3]


def _repo_root() -> Path:
    return _box_root().parent.parent


def _db_path() -> Path:
    return _repo_root() / "runtime" / "data" / "assistants.db"


def _persona_dir() -> Path:
    custom = str(CFG.get("persona_dir") or "").strip()
    if custom:
        return Path(custom)
    return _box_root() / "default" / "config" / "personas"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=10)
    con.row_factory = sqlite3.Row
    con.executescript(_SCHEMA)
    return con


def _binding(session_id: str) -> str:
    sid = (session_id or "").strip()
    if not sid:
        return ""
    try:
        with _connect() as con:
            row = con.execute(
                "SELECT assistant_id FROM session_bindings WHERE session_id = ?", (sid,)
            ).fetchone()
            return str(row["assistant_id"]) if row else ""
    except Exception:  # noqa: BLE001 - routing must never break a turn
        log.warning("[assistant] binding lookup failed", exc_info=True)
        return ""


def _bind(session_id: str, assistant_id: str) -> None:
    with _connect() as con:
        con.execute(
            "INSERT INTO session_bindings(session_id, assistant_id, updated_at) "
            "VALUES(?, ?, datetime('now')) "
            "ON CONFLICT(session_id) DO UPDATE SET "
            "assistant_id = excluded.assistant_id, updated_at = excluded.updated_at",
            (session_id, assistant_id),
        )
        con.commit()


def _unbind(session_id: str) -> None:
    with _connect() as con:
        con.execute("DELETE FROM session_bindings WHERE session_id = ?", (session_id,))
        con.commit()


def _persona_path(assistant_id: str) -> Path | None:
    root = _persona_dir()
    if not root.is_dir():
        return None
    for path in root.glob("*.md"):
        if path.stem == assistant_id:
            return path
    return None


def _persona_text(assistant_id: str) -> str:
    path = _persona_path(assistant_id)
    if path is None:
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        log.warning("[assistant] cannot read persona %s", path, exc_info=True)
        return ""


def _display_name(assistant_id: str, text: str = "") -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip() or assistant_id
    return assistant_id


def _personas() -> list[dict[str, str]]:
    root = _persona_dir()
    if not root.is_dir():
        return []
    out: list[dict[str, str]] = []
    for path in sorted(root.glob("*.md")):
        text = _persona_text(path.stem)
        out.append({"id": path.stem, "name": _display_name(path.stem, text)})
    return out


def _ensure_assistant(assistant_id: str) -> str:
    """Register the persona in our roster; returns display name ('' when absent)."""
    text = _persona_text(assistant_id)
    if not text:
        return ""
    name = _display_name(assistant_id, text)
    with _connect() as con:
        con.execute(
            "INSERT INTO assistants(id, name, persona) VALUES(?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET name = excluded.name, persona = excluded.persona",
            (assistant_id, name, str(_persona_path(assistant_id))),
        )
        con.commit()
    return name


def _create_persona(assistant_id: str, display_name: str) -> Path:
    root = _persona_dir()
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{assistant_id}.md"
    if not path.exists():
        path.write_text(
            f"# {display_name}\n\n"
            f"你是用户的私人助理「{display_name}」。\n\n"
            "- 用简体中文，先给结论，再给理由。\n"
            "- 只使用当前助理可见的记忆，不提及用户其他助理的私人内容。\n"
            "- 不要播报工具调用或运行状态。\n",
            encoding="utf-8",
        )
    return path


def _session_of(request) -> str:
    state = getattr(request, "state", None)
    if isinstance(state, dict):
        return str(state.get("session_id") or "").strip()
    return str(getattr(state, "session_id", "") or "").strip()


def _store():
    from runtime.state import STATE

    return getattr(STATE, "knowledge_store", None)


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(key, default)
    return getattr(row, key, default)


def _private_memories(assistant_id: str) -> str:
    store = _store()
    if store is None:
        return ""
    limit = max(1, int(CFG.get("inject_limit") or 20))
    try:
        rows = store.list_chunks(namespace=assistant_id, limit=limit)
    except TypeError:  # older backend without the namespace filter
        try:
            rows = [r for r in store.list_chunks(limit=limit * 5) if _row_value(r, "namespace") == assistant_id]
        except Exception:  # noqa: BLE001
            return ""
    except Exception:  # noqa: BLE001
        log.warning("[assistant] private memory lookup failed", exc_info=True)
        return ""
    lines: list[str] = []
    seen: set[str] = set()
    budget = max(200, int(CFG.get("max_inject_chars") or 3000))
    for row in rows or []:
        content = str(_row_value(row, "content", "") or "").strip()
        if not content or content in seen:
            continue
        if str(_row_value(row, "delivery_policy", "") or "") == "always":
            continue
        seen.add(content)
        heading = str(_row_value(row, "heading", "") or "").strip()
        line = f"- {content}" + (f"（{heading}）" if heading else "")
        if len(line) > 300:
            line = line[:300] + "…"
        if sum(len(x) + 1 for x in lines) + len(line) + 1 > budget:
            break
        lines.append(line)
    return "\n".join(lines)


class AssistantMiddleware(AgentMiddleware):
    """Inject the bound persona + private memories; scope memory_ingest writes."""

    def __init__(self) -> None:
        super().__init__()

    def _apply(self, request):
        if not bool(CFG.get("enabled", True)):
            return request
        session_id = _session_of(request)
        assistant_id = _binding(session_id)
        if not assistant_id:
            return request
        persona = _persona_text(assistant_id)
        name = _display_name(assistant_id, persona)
        block = [
            "## 当前助理（多助理模式）",
            f"id: {assistant_id} | 名称: {name}",
        ]
        if persona:
            block.append(persona)
        memories = _private_memories(assistant_id)
        if memories:
            block.append("## 当前助理的私有记忆（仅本助理可见；无关时不要主动提及）")
            block.append(memories)
        text = "\n\n".join(block)
        base = request.system_message.text if request.system_message is not None else ""
        return request.override(system_message=SystemMessage(content=f"{base}\n\n{text}".strip()))

    def wrap_model_call(self, request, handler):
        try:
            request = self._apply(request)
        except Exception:  # noqa: BLE001 - never break the turn
            log.warning("[assistant] persona injection failed", exc_info=True)
        return handler(request)

    async def awrap_model_call(self, request, handler):
        try:
            request = self._apply(request)
        except Exception:  # noqa: BLE001
            log.warning("[assistant] persona injection failed", exc_info=True)
        return await handler(request)

    # -- memory_ingest: auto namespace + on-demand -------------------------
    def _intercept_ingest(self, request) -> ToolMessage | None:
        tool_call = getattr(request, "tool_call", None) or {}
        if not isinstance(tool_call, dict) or tool_call.get("name") != "memory_ingest":
            return None
        assistant_id = _binding(_session_of(request))
        if not assistant_id:
            return None
        args = tool_call.get("args") or {}
        content = str(args.get("content") or "").strip()
        if not content:
            return ToolMessage(
                content="ERROR: content 不能为空",
                tool_call_id=tool_call.get("id", ""),
                name="memory_ingest",
            )
        domain = str(args.get("domain") or "general").strip() or "general"
        if domain in {"hot", "preferences"}:
            domain = "assistant_hot"
        elif domain == "fact":
            domain = "assistant_fact"
        store = _store()
        if store is None:
            return ToolMessage(
                content="ERROR: knowledge store 不可用",
                tool_call_id=tool_call.get("id", ""),
                name="memory_ingest",
            )
        try:
            chunk_id = store.add_chunk(
                content,
                domain=domain,
                heading=str(args.get("heading") or "助理记忆"),
                memory_kind=str(args.get("memory_kind") or "note"),
                delivery_policy="on_demand",
                namespace=assistant_id,
                subject=str(args.get("subject") or ""),
                source=_session_of(request) or "assistant",
                source_type="conversation",
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("[assistant] scoped memory write failed", exc_info=True)
            return ToolMessage(
                content=f"ERROR: 写入失败 — {type(exc).__name__}: {exc}",
                tool_call_id=tool_call.get("id", ""),
                name="memory_ingest",
            )
        log.info("[assistant] memory_ingest scoped to %s domain=%s id=%s", assistant_id, domain, chunk_id)
        return ToolMessage(
            content=f"Stored chunk {chunk_id} in '{domain}'（已绑定助理 {assistant_id}，仅本助理可见）。",
            tool_call_id=tool_call.get("id", ""),
            name="memory_ingest",
        )

    def wrap_tool_call(self, request, handler):
        try:
            scoped = self._intercept_ingest(request)
            if scoped is not None:
                return scoped
        except Exception:  # noqa: BLE001
            log.warning("[assistant] memory_ingest interception failed", exc_info=True)
        return handler(request)

    async def awrap_tool_call(self, request, handler):
        try:
            scoped = await asyncio.to_thread(self._intercept_ingest, request)
            if scoped is not None:
                return scoped
        except Exception:  # noqa: BLE001
            log.warning("[assistant] memory_ingest interception failed", exc_info=True)
        return await handler(request)


async def _handle_command(rest: str, session_id: str) -> str:
    tokens = (rest or "").strip().split()
    current = _binding(session_id)
    if not tokens or tokens[0] in {"list", "ls", "?"}:
        people = _personas()
        lines = ["可用助理："]
        if people:
            for person in people:
                mark = "*" if person["id"] == current else " "
                lines.append(f"{mark} {person['id']} — {person['name']}")
        else:
            lines.append("（暂无，先运行 /assistant new <id> [名称]）")
        lines.append(f"当前会话：{current or '未绑定（使用全局助理）'}")
        lines.append("切换：/assistant <id>；解除：/assistant off")
        return "\n".join(lines)

    if tokens[0] in {"off", "unbind", "none", "global"}:
        _unbind(session_id)
        return "已解除绑定，当前会话回到全局助理。"

    if tokens[0] == "new":
        if len(tokens) < 2 or not _ID_RE.match(tokens[1]):
            return "用法：/assistant new <id> [显示名称]（id 仅限字母、数字、下划线、连字符）"
        assistant_id = tokens[1]
        name = " ".join(tokens[2:]).strip() or assistant_id
        path = _create_persona(assistant_id, name)
        _ensure_assistant(assistant_id)
        return f"已创建人设 {path.name}（{name}）。用 /assistant {assistant_id} 切换。"

    assistant_id = tokens[0]
    if not _ID_RE.match(assistant_id):
        return "助理 id 仅限字母、数字、下划线、连字符。"
    name = _ensure_assistant(assistant_id)
    if not name:
        return (
            f"找不到助理 {assistant_id}。可用："
            + ("、".join(p["id"] for p in _personas()) or "（无）")
            + "。可先用 /assistant new 创建。"
        )
    _bind(session_id, assistant_id)
    return f"已切换到助理「{name}」（{assistant_id}）。本会话之后的消息、记忆与人设均为该助理专属。"


def register(registry) -> None:
    try:
        CFG.update(registry.config or {})
    except Exception:  # noqa: BLE001
        log.debug("[assistant] config merge skipped", exc_info=True)
    registry.register_chat_command("assistant", _handle_command)
    registry.register_middleware(lambda config: AssistantMiddleware())
    try:
        _connect().close()
    except Exception:  # noqa: BLE001
        log.warning("[assistant] db init failed", exc_info=True)
    log.info(
        "[assistant] registered /assistant (personas=%s db=%s inject_limit=%s)",
        _persona_dir(),
        _db_path(),
        CFG.get("inject_limit"),
    )
