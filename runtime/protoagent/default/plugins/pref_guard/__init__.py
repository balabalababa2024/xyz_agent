"""Deterministic hot-memory guard (plan B).

Every model call, this middleware scans the latest user message with fixed
rules. A sentence that reads like a *stable* preference ("以后都…", "我习惯…",
"别再…") is written straight into the knowledge store as always-on memory
(``domain="hot"``, ``memory_kind="profile"``, ``delivery_policy="always"``),
which the projection re-injects every turn and never sheds for budget.

Why a middleware instead of prompt instructions: the prompt-level path (SOUL +
``preference-notes``) is probabilistic — a model can miss a preference or
refuse to call the tool. This path is deterministic: keyword rules run locally,
no extra model call, no token cost.

Dedupe is content-based: a new sentence is skipped when a hot chunk already
contains it (or vice-versa, after whitespace/punctuation normalisation).

Nothing here edits protoAgent core: it is ``registry.register_middleware``
(ADR 0032); the store is reached through the documented ``STATE.knowledge_store``
seam (ADR 0031).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage

log = logging.getLogger("protoagent.plugins.pref_guard")

CFG: dict[str, Any] = {
    "enabled": True,
    "dry_run": False,
    "max_chars": 300,
    "dedupe": True,
    "extra_triggers": "",
}

# Stable-preference signals. Kept deliberately narrow: a false negative only
# leaves a preference to the prompt path, while a false positive puts a one-off
# instruction in front of the model every turn.
_TRIGGER = re.compile(
    r"(以后|今后|往后|从此|每次|一律|总是|永远|一直|"
    r"我习惯|我偏好|我更喜欢|我不喜欢|我讨厌|"
    r"不要再|别再|不要用|不准|不许|"
    r"记住|记一下|统一用|默认用|都用|都用上)"
)

# One-off markers: presence cancels a trigger ("这次先用英文" is not a preference).
_ONEOFF = re.compile(r"(这次|这一回|这一次|今天|今晚|临时|暂时|就这一次|仅此一次|先这样)")

_STRIP = re.compile(r"[\s，。！？、；：,.!?;:()（）\[\]“”\"'‘’]+")

# Attached-file refs from the console are host data, not user speech.
_ATTACHED = re.compile(r"^\[attached-image refs\][^\n]*\n?", re.I)

MAX_SCAN = 400


def _clean_user_text(text: str) -> str:
    """Drop host-injected envelopes, keep only what the user actually typed."""
    text = text or ""
    while True:
        start = text.find("<injected_context>")
        if start < 0:
            break
        end = text.find("</injected_context>", start)
        if end < 0:
            text = text[:start]
            break
        text = text[:start] + text[end + len("</injected_context>"):]
    text = _ATTACHED.sub("", text.strip())
    return text.strip()



def _norm(text: str) -> str:
    return _STRIP.sub("", text or "").lower()


def _triggers() -> re.Pattern[str]:
    extra = str(CFG.get("extra_triggers") or "").strip()
    if not extra:
        return _TRIGGER
    try:
        return re.compile(f"{_TRIGGER.pattern}|{extra}")
    except re.error:
        log.warning("[pref_guard] bad extra_triggers, ignored: %r", extra)
        return _TRIGGER


def _is_context_frame(msg) -> bool:
    """True for the per-turn ``<injected_context>`` frame (host data, not speech).

    protoAgent appends that frame as its OWN tagged HumanMessage after the real
    user message (``graph.context_frame``), so scanning for the latest human
    message would otherwise land on recalled memory + skills index instead.
    """
    kwargs = getattr(msg, "additional_kwargs", None)
    if kwargs is None and isinstance(msg, dict):
        kwargs = msg.get("additional_kwargs")
    return isinstance(kwargs, dict) and bool(kwargs.get("protoagent_injected_context"))


def _latest_user_text(request) -> str:
    msgs = getattr(request, "messages", None) or []
    for msg in reversed(msgs):
        is_human = isinstance(msg, HumanMessage) or getattr(msg, "type", "") == "human"
        if not is_human or _is_context_frame(msg):
            continue
        content = getattr(msg, "content", None)
        if content is None and isinstance(msg, dict):
            content = msg.get("content")
        if isinstance(content, list):  # multimodal blocks
            content = " ".join(
                str(part.get("text", "")) if isinstance(part, dict) else str(part)
                for part in content
            )
        return str(content or "")
    return ""


def _classify(text: str) -> str | None:
    """Return the preference sentence to store, or None when it isn't one."""
    text = _clean_user_text(text)
    if not text or len(text) > MAX_SCAN:
        return None
    if text.startswith("/"):  # slash commands are not preferences
        return None
    if _ONEOFF.search(text) or not _triggers().search(text):
        return None
    # A pure question ("以后都这样吗？") is a question, not a statement.
    if text.rstrip().endswith(("?", "？")):
        return None
    return text[: int(CFG.get("max_chars") or 300)]


def _store():
    from runtime.state import STATE

    return getattr(STATE, "knowledge_store", None)


def _assistant_db() -> Path:
    root = os.environ.get("PROTOAGENT_BOX_ROOT")
    repo = Path(root).parent.parent if root else Path(__file__).resolve().parents[5]
    return repo / "runtime" / "data" / "assistants.db"


def _session_of(request) -> str:
    state = getattr(request, "state", None)
    if isinstance(state, dict):
        return str(state.get("session_id") or "").strip()
    return str(getattr(state, "session_id", "") or "").strip()


def _bound_assistant(session_id: str) -> str:
    """Current assistant for this session ('' = unbound / legacy global mode)."""
    if not session_id:
        return ""
    path = _assistant_db()
    if not path.is_file():
        return ""
    try:
        con = sqlite3.connect(path, timeout=5)
        try:
            row = con.execute(
                "SELECT assistant_id FROM session_bindings WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        finally:
            con.close()
        return str(row[0]) if row else ""
    except Exception:  # noqa: BLE001 - routing must not break preference capture
        log.debug("[pref_guard] assistant lookup failed", exc_info=True)
        return ""


def _scope(assistant_id: str) -> tuple[str, str, str | None]:
    """(domain, delivery_policy, namespace) for the current routing mode."""
    if assistant_id:
        return "assistant_hot", "on_demand", assistant_id
    return "hot", "always", None


def _already_stored(store, content: str, assistant_id: str = "") -> bool:
    if not bool(CFG.get("dedupe", True)):
        return False
    target = _norm(content)
    if len(target) < 4:
        return False
    domain, _, namespace = _scope(assistant_id)
    try:
        if namespace:
            rows = store.list_chunks(domain=domain, namespace=namespace, limit=100)
        else:
            rows = store.list_chunks(domain=domain, limit=100)
    except TypeError:  # backend without the domain filter
        rows = store.list_chunks(limit=100)
    except Exception:  # noqa: BLE001 - a lookup failure must not drop the write
        log.debug("[pref_guard] dedupe scan failed", exc_info=True)
        return False
    for row in rows or []:
        existing = row.get("content", "") if isinstance(row, dict) else getattr(row, "content", "")
        known = _norm(str(existing or ""))
        if not known:
            continue
        if target in known or known in target:
            return True
    return False


def _write(store, content: str, assistant_id: str = "") -> int | None:
    subject = "pref-" + hashlib.sha1(_norm(content).encode()).hexdigest()[:10]
    domain, policy, namespace = _scope(assistant_id)
    return store.add_chunk(
        content,
        domain=domain,
        heading="用户偏好",
        memory_kind="profile",
        delivery_policy=policy,
        namespace=namespace,
        subject=subject,
        source="pref_guard",
        source_type="conversation",
    )


def _scan(request) -> None:
    if not bool(CFG.get("enabled", True)):
        return
    text = _latest_user_text(request)
    content = _classify(text)
    if not content:
        if text:
            log.debug("[pref_guard] skipped (no stable-preference signal): %s", text[:60])
        else:
            log.debug("[pref_guard] skipped (no user message found)")
        return
    store = _store()
    if store is None:
        log.debug("[pref_guard] knowledge store unavailable, skip")
        return
    assistant_id = _bound_assistant(_session_of(request))
    if _already_stored(store, content, assistant_id):
        log.debug("[pref_guard] duplicate preference skipped")
        return
    if bool(CFG.get("dry_run", False)):
        log.info("[pref_guard] DRY-RUN would store: %s", content[:80])
        return
    chunk_id = _write(store, content, assistant_id)
    if assistant_id:
        log.info(
            "[pref_guard] stored preference assistant=%s domain=assistant_hot id=%s chars=%d",
            assistant_id,
            chunk_id,
            len(content),
        )
    else:
        log.info("[pref_guard] stored preference hot/always id=%s chars=%d", chunk_id, len(content))


class PrefGuardMiddleware(AgentMiddleware):
    """Rule-based preference capture, run before every model call."""

    def wrap_model_call(self, request, handler):
        try:
            _scan(request)
        except Exception:  # noqa: BLE001 - never break the turn
            log.warning("[pref_guard] scan failed", exc_info=True)
        return handler(request)

    async def awrap_model_call(self, request, handler):
        try:
            if bool(CFG.get("enabled", True)):
                content = _classify(_latest_user_text(request))
                store = _store() if content else None
                assistant_id = _bound_assistant(_session_of(request)) if content else ""
                if content and store is not None and not _already_stored(store, content, assistant_id):
                    if bool(CFG.get("dry_run", False)):
                        log.info("[pref_guard] DRY-RUN would store: %s", content[:80])
                    else:
                        chunk_id = await asyncio.to_thread(_write, store, content, assistant_id)
                        if assistant_id:
                            log.info(
                                "[pref_guard] stored preference assistant=%s domain=assistant_hot id=%s chars=%d",
                                assistant_id,
                                chunk_id,
                                len(content),
                            )
                        else:
                            log.info("[pref_guard] stored preference hot/always id=%s chars=%d", chunk_id, len(content))
        except Exception:  # noqa: BLE001
            log.warning("[pref_guard] scan failed", exc_info=True)
        return await handler(request)


def register(registry) -> None:
    try:
        CFG.update(registry.config or {})
    except Exception:  # noqa: BLE001 - a hand-edited section must not break load
        log.debug("[pref_guard] config merge skipped", exc_info=True)
    registry.register_middleware(lambda config: PrefGuardMiddleware())
    log.info(
        "[pref_guard] registered preference guard (enabled=%s dry_run=%s dedupe=%s)",
        CFG.get("enabled"),
        CFG.get("dry_run"),
        CFG.get("dedupe"),
    )
