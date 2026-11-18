"""Expand Feishu POST trees into ordered, downloadable message parts.

The public ``content`` field is a display summary and may contain placeholders.
Only the POST body and decoded node properties are message content here.
"""
from __future__ import annotations

import hashlib
from typing import Any


MAX_NODES = 4096
MAX_DEPTH = 64
MAX_PARTS = 1024
MAX_PROPERTY_BYTES = 1024 * 1024
MAX_TOTAL_PROPERTY_BYTES = 8 * 1024 * 1024
MAX_TEXT_CHARS = 2 * 1024 * 1024

_TAG_NAMES = {
    "TEXT": 1, "IMG": 2, "P": 3, "FIGURE": 4, "AT": 5, "A": 6,
    "B": 7, "I": 8, "U": 9, "EMOTION": 10, "DIV": 14,
    "TEXTABLE_AREA": 15, "LINK": 17, "MEDIA": 18, "DOCS": 22,
    "H1": 23, "H2": 24, "H3": 25, "UL": 26, "OL": 27, "LI": 28,
    "QUOTE": 29, "CODE": 30, "CODE_BLOCK": 31, "HR": 32,
    "CODE_BLOCK_V2": 37, "TABLE": 39, "TABLE_ROW": 40,
    "TABLE_CELL": 41, "COLUMNS": 42, "COLUMN": 43,
    "H4": 44, "H5": 45, "H6": 46, "AT_USER_GROUP": 47, "MARKDOWN": 49,
}
_TEXT_TAGS = {1, 3, 7, 8, 9, 17, 23, 24, 25, 30, 44, 45, 46}
_BLOCK_TAGS = {3, 14, 23, 24, 25, 26, 27, 28, 29, 31, 37, 39, 40, 42, 43, 44, 45, 46}
_CONTAINER_TAGS = _BLOCK_TAGS | {4, 15, 22, 41}


class RichPostError(ValueError):
    """A malformed POST; messages deliberately omit resource identifiers."""


def is_rich_post(message: dict[str, Any]) -> bool:
    """Detect structured content, including TEXT envelopes with rich nodes.

    Lark's TEXT and POST schemas both contain ``richText``. Their display
    summaries replace media with labels, so a TEXT body with media nodes must
    go through the same expansion as POST instead of forwarding its summary.
    Ordinary TEXT formatting and emoji keep their existing text semantics.
    """
    value = message.get("msg_type", message.get("message_type", ""))
    kind = str(value).lower()
    if kind in {"2", "post"}:
        return True
    body = message.get("content_data")
    if kind not in {"4", "text"} or not isinstance(body, dict):
        return False
    rich = body.get("richText")
    elements = rich.get("elements") if isinstance(rich, dict) else None
    nodes = elements.get("dictionary") if isinstance(elements, dict) else None
    if not isinstance(nodes, dict):
        return False
    return any(isinstance(node, dict) and str(node.get("tag", "")).lower() in {"2", "img", "18", "media"}
               for node in nodes.values())


def _element_types() -> Any:
    # Most installations load the plugin before adding LarkAgentX to sys.path.
    from larkx.proto.gateway import _lark_all
    return _lark_all().entities.RichTextElement


def _tag(value: Any) -> int:
    if isinstance(value, str) and value.upper() in _TAG_NAMES:
        return _TAG_NAMES[value.upper()]
    try:
        return int(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise RichPostError("飞书富文本节点类型无效") from exc


def _proto_dict(value: Any) -> dict[str, Any]:
    from protobuf_to_dict import protobuf_to_dict
    return protobuf_to_dict(value)


def rich_post_parts(message: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize POST leaves without networking or modifying the input.

    Original message/chat IDs must survive: authenticated media URLs refer to
    the POST envelope, even when a nested leaf is normalized to IMAGE or MEDIA.
    A rootless *flat* legacy body is ordered by numeric element ID. Tree layouts
    require explicit roots; unrelated dictionary entries are never appended.
    """
    if not is_rich_post(message):
        raise RichPostError("消息不是飞书富文本")
    body = message.get("content_data")
    if not isinstance(body, dict):
        raise RichPostError("飞书富文本缺少可解析的正文；未发送摘要文字")
    parts: list[dict[str, Any]] = []
    text_chunks: list[str] = []
    text_ids: list[str] = []
    text_length = 0
    property_bytes = 0
    types = None

    def append_part(kind: int, content: str, data: dict[str, Any], part_id: str) -> None:
        if len(parts) >= MAX_PARTS:
            raise RichPostError("飞书富文本内容段数超过上限")
        parts.append({**message, "msg_type": kind, "message_type": kind,
                      "content": content, "content_data": data, "_rich_part_id": part_id})

    def text(value: Any, node_id: str) -> None:
        nonlocal text_length
        if not isinstance(value, str):
            raise RichPostError("飞书富文本文字格式无效")
        text_length += len(value)
        if text_length > MAX_TEXT_CHARS:
            raise RichPostError("飞书富文本长度超过上限")
        if value:
            text_chunks.append(value)
            text_ids.append(node_id)

    def newline() -> None:
        if text_chunks and not text_chunks[-1].endswith("\n"):
            text("\n", "")

    def flush() -> None:
        if text_chunks:
            value = "".join(text_chunks).strip("\n")
            if value:
                identity = "\0".join(text_ids).encode("utf-8")
                part_id = "text:" + hashlib.sha256(identity).hexdigest()[:24]
                append_part(4, value, {"text": value}, part_id)
            text_chunks.clear()
            text_ids.clear()

    def decode(node: dict[str, Any], name: str) -> Any:
        nonlocal types, property_bytes
        raw = node.get("property", b"")
        if not isinstance(raw, (bytes, bytearray, memoryview)):
            raise RichPostError("飞书富文本节点属性格式无效")
        property_bytes += len(raw)
        if len(raw) > MAX_PROPERTY_BYTES or property_bytes > MAX_TOTAL_PROPERTY_BYTES:
            raise RichPostError("飞书富文本节点属性超过上限")
        try:
            if types is None:
                types = _element_types()
            return getattr(types, name).FromString(bytes(raw))
        except Exception as exc:
            raise RichPostError("飞书富文本节点属性无法解析") from exc

    title = body.get("title", "")
    if title:
        text(title, "title")
        flush()

    rich = body.get("richText")
    if rich is None:
        rich = {}
    if not isinstance(rich, dict):
        raise RichPostError("飞书富文本元素格式无效")
    elements = rich.get("elements")
    if elements is None:
        elements = {}
    if not isinstance(elements, dict):
        raise RichPostError("飞书富文本元素字典格式无效")
    dictionary = elements.get("dictionary")
    if dictionary is None:
        dictionary = {}
    if not isinstance(dictionary, dict) or len(dictionary) > MAX_NODES:
        raise RichPostError("飞书富文本节点字典无效或超过上限")
    roots = rich.get("elementIds")
    if roots is None:
        roots = []
    if not isinstance(roots, (list, tuple)) or len(roots) > MAX_NODES:
        raise RichPostError("飞书富文本根节点格式无效")
    if not dictionary:
        if roots:
            raise RichPostError("飞书富文本引用的节点不存在")
        text(body.get("text", ""), "body")
        flush()
        if not parts:
            raise RichPostError("飞书富文本正文为空；未发送摘要文字")
        return parts
    if any(not isinstance(key, str) or not isinstance(node, dict) for key, node in dictionary.items()):
        raise RichPostError("飞书富文本节点格式无效")
    if not roots:
        if any(node.get("childIds") for node in dictionary.values()):
            raise RichPostError("飞书富文本树缺少根节点")
        roots = sorted(dictionary, key=lambda key: (0, int(key)) if key.isdecimal() else (1, key))

    seen: set[str] = set()
    active: set[str] = set()
    edges = 0

    def walk(node_id: str, depth: int = 0, ordered: bool = False, index: int = 0) -> None:
        nonlocal edges
        if not isinstance(node_id, str) or node_id not in dictionary:
            raise RichPostError("飞书富文本引用的节点不存在")
        if depth > MAX_DEPTH or node_id in active:
            raise RichPostError("飞书富文本节点循环或层级过深")
        if node_id in seen:
            return
        seen.add(node_id)
        active.add(node_id)
        node = dictionary[node_id]
        tag = _tag(node.get("tag", 1))
        children = node.get("childIds")
        if children is None:
            children = []
        if not isinstance(children, (list, tuple)):
            raise RichPostError("飞书富文本子节点格式无效")
        edges += len(children)
        if edges > MAX_NODES * 4:
            raise RichPostError("飞书富文本节点引用超过上限")
        if tag in _BLOCK_TAGS:
            newline()
        if tag == 28:
            text(f"{index + 1}. " if ordered else "- ", node_id)
        if tag == 18:
            prop = decode(node, "MediaProperty")
            if not prop.key:
                raise RichPostError("飞书富文本视频缺少下载所需的资源标识")
            flush()
            append_part(15, "", _proto_dict(prop), node_id)
        elif tag == 2:
            prop = decode(node, "ImageProperty")
            raw = _proto_dict(prop)
            v2 = raw.get("image") or {}
            key = v2.get("imageKey") or prop.originKey or prop.token
            if not key:
                raise RichPostError("飞书富文本图片缺少下载所需的资源标识")
            data = {"imageV2": {**v2, "imageKey": key}}
            if prop.fsUnit:
                data["fsUnit"] = prop.fsUnit
            flush()
            append_part(5, "", data, node_id)
        elif children:
            # Container properties describe layout; the ordered descendants
            # contain the text, so decoding both would duplicate the body.
            pass
        elif tag in _TEXT_TAGS:
            text(decode(node, "TextProperty").content, node_id)
        elif tag == 5:
            text(decode(node, "AtProperty").content, node_id)
        elif tag == 6:
            prop = decode(node, "AnchorProperty")
            href = prop.href
            label = prop.content or prop.textContent
            text(f"{label}({href})" if label and href and label != href else label or href, node_id)
        elif tag == 31:
            text(decode(node, "CodeBlockProperty").content, node_id)
        elif tag == 37:
            prop = decode(node, "CodeBlockV2Property")
            text("\n".join("".join(span.content for span in line.contents) for line in prop.contents), node_id)
        elif tag == 10:
            # EMOTION.key is an emoji name, not an authenticated image key.
            key = decode(node, "EmotionProperty").key
            if key:
                text(f"[{key}]", node_id)
        elif tag == 32:
            newline()
        elif tag not in _CONTAINER_TAGS:
            raise RichPostError("飞书富文本包含暂不支持的内容类型")
        for child_index, child in enumerate(children):
            walk(child, depth + 1, ordered=tag == 27, index=child_index)
        if tag in _BLOCK_TAGS:
            newline()
        active.remove(node_id)

    for root in roots:
        walk(root)
    flush()
    if not parts:
        raise RichPostError("飞书富文本没有可转发的正文或媒体；未发送摘要文字")
    return parts
