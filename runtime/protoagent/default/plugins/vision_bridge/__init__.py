"""视觉桥接插件：把一张图片变成文本，供纯文本技能消费。

设计要点：
- 走 OpenAI 兼容的 ``/chat/completions`` + ``image_url``（base64 data URI），
  实测于智谱 ``glm-4v-flash``（见 ``runtime/vision-probe.py``）。
- 不改变主对话模型：只把图片转成文本描述，主模型继续按文本推理，
  属于「非原生多模态输入 → 文本对话流」的桥接。
- 输入优先级：``data:`` URI → http(s) URL → 宿主文件路径；沙箱内图片请先用
  ``sbx_download_file`` 取回宿主，再交给本工具（本工具不直连沙箱）。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

log = logging.getLogger("protoagent.plugins.vision_bridge")

CFG: dict[str, Any] = {
    "base_url": "https://open.bigmodel.cn/api/paas/v4",
    "model": "glm-4v-flash",
    "max_tokens": 1024,
    "timeout_s": 90,
    "max_image_mb": 12,
    "max_output_chars": 8000,
    "media_dir": "",
    "api_key": "",
}

DEFAULT_QUESTION = "请详细描述这张图片的内容；若含文字，请完整转写。"

_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
}


def _int(key: str, fallback: int) -> int:
    try:
        return int(CFG[key])
    except (TypeError, ValueError):
        return fallback


def _clip(text: str) -> str:
    limit = _int("max_output_chars", 8000)
    return text if len(text) <= limit else text[:limit] + "\n…（输出已截断）"


def _mime_for(name: str, data: bytes) -> str:
    suffix = Path(name).suffix.lower()
    if suffix in _MIME:
        return _MIME[suffix]
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _fetch_url(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "protoagent-vision-bridge"})
    with urllib.request.urlopen(req, timeout=_int("timeout_s", 90)) as resp:
        return resp.read()


_MEDIA_ID = re.compile(r"^[0-9a-fA-F]{16,64}$")


def _media_dir() -> Path:
    """protoAgent 的媒体目录：聊天里贴的图落在这里，模型只拿到 media id。"""
    if CFG.get("media_dir"):
        return Path(CFG["media_dir"])
    box = os.environ.get("PROTOAGENT_BOX_ROOT") or str(Path.home() / ".protoagent")
    instance = os.environ.get("PROTOAGENT_INSTANCE") or "default"
    return Path(box) / instance / "media"


def _media_file(media_id: str) -> Path | None:
    root = _media_dir()
    if not root.is_dir():
        return None
    for candidate in root.glob(f"{media_id}.*"):
        if candidate.suffix.lower() != ".json" and candidate.is_file():
            return candidate
    return None


def _load_image(image: str) -> tuple[bytes, str, str]:
    """Returns (bytes, mime, source_label)."""
    src = (image or "").strip()
    if not src:
        raise ValueError("image 为空：请传宿主图片路径、http(s) URL、data: URI 或 media id")
    if src.startswith("data:"):
        header, _, b64 = src.partition(",")
        mime = header[5:].split(";")[0] or "image/png"
        return base64.b64decode(b64), mime, "data-uri"
    if src.lower().startswith(("http://", "https://")):
        data = _fetch_url(src)
        return data, _mime_for(src, data), src
    if _MEDIA_ID.match(src):
        path = _media_file(src)
        if path is not None:
            data = path.read_bytes()
            return data, _mime_for(path.name, data), f"media:{src}"
    path = Path(src)
    if not path.is_file():
        raise FileNotFoundError(
            f"找不到图片：{src}（支持宿主路径 / http(s) URL / data: URI / 聊天附件 media id；"
            f"media 目录：{_media_dir()}）"
        )
    data = path.read_bytes()
    return data, _mime_for(path.name, data), str(path)


def _post(payload: dict) -> dict:
    req = urllib.request.Request(
        f"{CFG['base_url'].rstrip('/')}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {CFG['api_key']}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=_int("timeout_s", 90)) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


# ── 饮食热量（视觉只识别，算热量在本地） ────────────────────────────────────

_FOOD_CACHE: dict[str, Any] | None = None


def _food_db_path() -> Path:
    env = os.environ.get("DIET_FOOD_DB")
    if env:
        return Path(env)
    box = os.environ.get("PROTOAGENT_BOX_ROOT")
    repo = Path(box).parent.parent if box else Path(__file__).resolve().parents[5]
    return repo / "data" / "food_calories.json"


def _food_data() -> dict[str, Any]:
    global _FOOD_CACHE
    if _FOOD_CACHE is not None:
        return _FOOD_CACHE
    path = _food_db_path()
    try:
        _FOOD_CACHE = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - missing DB degrades to "unknown" calories
        log.warning("[vision] food db unavailable at %s", path, exc_info=True)
        _FOOD_CACHE = {"foods": {}, "aliases": {}}
    return _FOOD_CACHE


def _food_key(name: str) -> str:
    text = str(name or "").strip().lower()
    text = re.sub(r"[（(][^）)]*[）)]", "", text)
    text = re.sub(r"[\s、,，。.;；:：/]+", "", text)
    text = re.sub(r"^(一份|一个|一块|一碗|一杯|一碟|少许|适量)", "", text)
    return text


def _match_food(name: str) -> tuple[str, float] | None:
    data = _food_data()
    foods: dict[str, float] = data.get("foods") or {}
    aliases: dict[str, str] = data.get("aliases") or {}
    key = _food_key(name)
    if not key:
        return None
    if key in foods:
        return key, float(foods[key])
    if key in aliases and aliases[key] in foods:
        return aliases[key], float(foods[aliases[key]])
    for alias, target in aliases.items():
        if _food_key(alias) == key and target in foods:
            return target, float(foods[target])
    # 最长子串兜底： "香煎鸡胸肉" → 鸡胸肉
    for known in sorted(foods, key=len, reverse=True):
        if _food_key(known) and _food_key(known) in key:
            return known, float(foods[known])
    return None


def _extract_json_array(text: str) -> list[Any]:
    cleaned = re.sub(r"```(?:json)?", "", text or "").replace("```", "")
    start, end = cleaned.find("["), cleaned.rfind("]")
    if start < 0 or end <= start:
        raise ValueError("视觉输出中没有 JSON 数组")
    body = json.loads(cleaned[start : end + 1])
    if isinstance(body, dict):
        body = body.get("items") or body.get("foods") or []
    if not isinstance(body, list):
        raise ValueError("视觉输出的 JSON 不是数组")
    return body


@tool
async def vision_diet_analyze(image: str, meal: str = "") -> str:
    """识别一张饮食照片里的食物与估算重量，并用本地字典计算总热量。

    Use this for 饮食记录 / 拍照估热量. The vision model ONLY identifies food
    names and weights; calorie numbers come from data/food_calories.json locally,
    never from the model.

    Args:
        image: 聊天附件 media id、宿主文件路径、http(s) URL，或 data: URI。
        meal: 可选的餐次标签（早餐/午餐/晚餐/加餐），用于记录文案。

    Returns:
        JSON 字符串：{"date": "...", "meal": "...", "items": [...], "total_kcal": N,
        "unknown_foods": [...]}；出错时返回以 "ERROR:" 开头的文本。
    """
    if not CFG["api_key"]:
        return "ERROR: vision_bridge 未配置 api_key（Settings ▸ Plugins ▸ Vision Bridge，或 secrets.yaml）"
    prompt = (
        "识别图中所有食物种类和可食用重量（克）。"
        "只返回 JSON 数组，格式为 [{\"food\": \"米饭\", \"weight_g\": 150}]；"
        "不要解释、不要 markdown、不要额外字段。"
    )
    try:
        data, mime, label = await asyncio.to_thread(_load_image, image)
    except Exception as exc:  # noqa: BLE001
        return f"ERROR: 读取图片失败 — {type(exc).__name__}: {exc}"
    limit = _int("max_image_mb", 12) * 1024 * 1024
    if len(data) > limit:
        return f"ERROR: 图片过大（{len(data) / 1024 / 1024:.1f} MB > {limit / 1024 / 1024:.0f} MB），请先压缩"
    b64 = base64.b64encode(data).decode()
    payload = {
        "model": CFG["model"],
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                    {"type": "text", "text": prompt},
                ],
            }
        ],
        "max_tokens": _int("max_tokens", 1024),
    }
    try:
        body = await asyncio.to_thread(_post, payload)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        return f"ERROR: 视觉端点 HTTP {exc.code} — {detail}"
    except Exception as exc:  # noqa: BLE001
        return f"ERROR: 视觉端点不可达 — {type(exc).__name__}: {exc}"
    try:
        content = (body["choices"][0]["message"].get("content") or "").strip()
    except (KeyError, IndexError, TypeError):
        return f"ERROR: 视觉端点返回结构异常 — {json.dumps(body, ensure_ascii=False)[:300]}"
    try:
        raw_items = _extract_json_array(content)
    except Exception as exc:  # noqa: BLE001
        return f"ERROR: 无法解析食物 JSON — {exc}; 原始输出: {content[:200]}"

    items: list[dict[str, Any]] = []
    unknown: list[str] = []
    total = 0.0
    for raw in raw_items[:30]:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("food") or raw.get("name") or "").strip()
        try:
            grams = float(raw.get("weight_g") or raw.get("weight") or 0)
        except (TypeError, ValueError):
            grams = 0.0
        if not name or grams <= 0:
            continue
        grams = min(grams, 2000.0)
        match = _match_food(name)
        if match:
            canonical, per100 = match
            kcal = round(per100 * grams / 100.0, 1)
            total += kcal
            items.append(
                {
                    "food": canonical,
                    "weight_g": round(grams, 1),
                    "kcal_per_100g": per100,
                    "kcal": kcal,
                }
            )
        else:
            unknown.append(name)
            items.append({"food": name, "weight_g": round(grams, 1), "kcal_per_100g": None, "kcal": None})
    if not items:
        return f"ERROR: 没有识别到可计量的食物；原始输出: {content[:200]}"
    today = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
    return json.dumps(
        {
            "date": today,
            "meal": (meal or "").strip(),
            "items": items,
            "total_kcal": round(total, 1),
            "unknown_foods": unknown,
            "source": f"vision:{CFG['model']} src={label}; calories=data/food_calories.json",
        },
        ensure_ascii=False,
    )


@tool
async def vision_analyze(image: str, question: str = "", model: str = "") -> str:
    """Describe or answer a question about a picture, returning plain text.

    Use this for 拍照问答: the picture is converted to text here so the
    text-only chat model can reason about it.

    Args:
        image: 聊天附件 media id（形如 "733b781f380140b1a7afc5abab9f92b4"，用户直接贴图时用它）、
            宿主文件路径（如 "D:\\\\...\\\\photo.jpg"）、http(s) URL，或 data: URI。
            沙箱内图片请先用 sbx_download_file 取回宿主。
        question: what to ask about the picture; empty = full description + OCR.
        model: vision model override; empty = configured default (glm-4v-flash).
    """
    if not CFG["api_key"]:
        return "ERROR: vision_bridge 未配置 api_key（Settings ▸ Plugins ▸ Vision Bridge，或 secrets.yaml）"
    q = (question or "").strip() or DEFAULT_QUESTION
    use_model = (model or "").strip() or CFG["model"]
    try:
        data, mime, label = await asyncio.to_thread(_load_image, image)
    except Exception as exc:  # noqa: BLE001
        return f"ERROR: 读取图片失败 — {type(exc).__name__}: {exc}"
    limit = _int("max_image_mb", 12) * 1024 * 1024
    if len(data) > limit:
        return f"ERROR: 图片过大（{len(data) / 1024 / 1024:.1f} MB > {limit / 1024 / 1024:.0f} MB），请先压缩"
    b64 = base64.b64encode(data).decode()
    payload = {
        "model": use_model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                    {"type": "text", "text": q},
                ],
            }
        ],
        "max_tokens": _int("max_tokens", 1024),
    }
    try:
        body = await asyncio.to_thread(_post, payload)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        return f"ERROR: 视觉端点 HTTP {exc.code} — {detail}"
    except Exception as exc:  # noqa: BLE001
        return f"ERROR: 视觉端点不可达 — {type(exc).__name__}: {exc}"
    try:
        content = (body["choices"][0]["message"].get("content") or "").strip()
    except (KeyError, IndexError, TypeError):
        return f"ERROR: 视觉端点返回结构异常 — {json.dumps(body, ensure_ascii=False)[:300]}"
    if not content:
        return "ERROR: 视觉端点返回空内容（可能是 max_tokens 过小或图片无法解析）"
    usage = body.get("usage") or {}
    head = f"[vision:{use_model} src={label} tokens={usage.get('total_tokens', '?')}]\n"
    return _clip(head + content)


def register(registry) -> None:
    cfg = getattr(registry, "config", None) or {}
    for key in list(CFG):
        value = cfg.get(key)
        if value not in (None, ""):
            CFG[key] = value
    registry.register_tool(vision_analyze)
    registry.register_tool(vision_diet_analyze)
    log.info(
        "[vision] registered vision_analyze + vision_diet_analyze (endpoint=%s model=%s key=%s food_db=%s)",
        CFG["base_url"],
        CFG["model"],
        "set" if CFG["api_key"] else "MISSING",
        _food_db_path(),
    )
