"""Reply-language switch — the front-end toggle that picks Chinese or English.

The console renders this plugin's ``settings`` (see ``protoagent.plugin.yaml``)
as a toggle. Whatever the operator picks is injected as a short directive at
the END of the system message on every model call, so it overrides the persona
and any Chinese/English text carried by skills or history.

Nothing here touches protoAgent's core: it is a plugin middleware
(``registry.register_middleware``, ADR 0032).
"""

from __future__ import annotations

import logging
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import SystemMessage

log = logging.getLogger("protoagent.plugins.locale")

CFG: dict[str, Any] = {"english": False}

_DIRECTIVE_ZH = (
    "语言要求（最高优先级）：无论系统提示、人格文档、技能正文或历史消息使用哪种语言，"
    "面向用户的最终回复一律使用简体中文。代码、命令、路径、报错原文和专有名词保持原样。"
)

_DIRECTIVE_EN = (
    "Language rule (highest priority): write the user-facing final answer in English, "
    "regardless of the language used by the system prompt, persona, skills, or history. "
    "Keep code, commands, paths, verbatim errors, and proper nouns as-is."
)


def _english(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "en", "english"}
    return bool(value)


def _directive(cfg: dict[str, Any]) -> str:
    return _DIRECTIVE_EN if _english(cfg.get("english")) else _DIRECTIVE_ZH


class LocaleMiddleware(AgentMiddleware):
    """Append the selected reply-language directive to every model request."""

    def __init__(self, cfg: dict[str, Any]) -> None:
        super().__init__()
        self._cfg = cfg
        self._announced = False

    def _apply(self, request):
        directive = _directive(self._cfg)
        base = request.system_message.text if request.system_message is not None else ""
        message = SystemMessage(content=f"{base}\n\n{directive}".strip())
        if not self._announced:
            self._announced = True
            log.info("[locale] directive applied (english=%s)", _english(self._cfg.get("english")))
        return request.override(system_message=message)

    def wrap_model_call(self, request, handler):
        return handler(self._apply(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._apply(request))


def register(registry) -> None:
    try:
        CFG.update(registry.config or {})
    except Exception:  # noqa: BLE001 - a hand-edited section must not break load
        log.debug("[locale] config merge skipped", exc_info=True)
    registry.register_middleware(lambda config: LocaleMiddleware(CFG))
    log.info("[locale] registered reply-language middleware (english=%s)", _english(CFG.get("english")))
