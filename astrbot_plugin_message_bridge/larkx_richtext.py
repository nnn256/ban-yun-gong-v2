"""Expand Feishu rich-text trees into ordered message parts.

The public ``content`` field is a display summary and may contain placeholders.
Only the POST body and decoded node properties are message content here.
"""
from __future__ import annotations

from .build_identity import capture_module_build

MODULE_BUILD = capture_module_build(__file__)

import hashlib
import re
from bisect import bisect_right
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
_MARKDOWN_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})[^\n]*(?:\n|$)")
_MARKDOWN_IMAGE_HTML = re.compile(r"<(?:img|picture|source)\b", re.IGNORECASE)
_MARKDOWN_REFERENCE_LINE = re.compile(r" {0,3}\[[^\]\n]+\]:[ \t]*")
_EMOTION_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
# Visible equivalents for names in Feishu's published emoji list. The private
# EMOTION node gives a name, not a resource to download.
_EMOTIONS = {
    "OK": "👌", "THUMBSUP": "👍", "THANKS": "🙏", "MUSCLE": "💪",
    "FINGERHEART": "🫰", "APPLAUSE": "👏", "FISTBUMP": "👊", "JIAYI": "➕1",
    "DONE": "✅", "YES": "✅", "NO": "❌", "CHECKMARK": "✔️", "CROSSMARK": "✖️",
    "ONIT": "🙋", "GET": "🫡", "SMILE": "🙂", "BLUSH": "😊", "LAUGH": "😄",
    "SMIRK": "😏", "LOL": "😂", "FACEPALM": "🤦", "LOVE": "❤️",
    "WINK": "😉", "PROUD": "😎", "SCOWL": "😠", "THINKING": "🤔",
    "SOB": "😭", "CRY": "😢", "WOW": "😮", "YEAH": "✌️", "WITTY": "😏",
    "TEARS": "🥲", "KISS": "😘", "DROOL": "🤤", "CLAP": "👏",
}


class RichPostError(ValueError):
    """A malformed POST; messages deliberately omit resource identifiers."""


class RichPostMediaIncomplete(RichPostError):
    """A media index exists, but no matching media body can be expanded.

    A caller may read this same visible message once to obtain its full body.
    Index values are not download keys and do not authorize a history read.
    """


def _media_indexes(rich: dict[str, Any]) -> tuple[list[str] | tuple[str, ...], list[str] | tuple[str, ...]]:
    indexes = []
    characters = 0
    for name in ("imageIds", "mediaIds"):
        values = rich.get(name, [])
        if not isinstance(values, (list, tuple)) or len(values) > MAX_NODES:
            raise RichPostError("飞书富文本媒体索引格式无效或超过上限")
        for value in values:
            if not isinstance(value, str) or not value:
                raise RichPostError("飞书富文本媒体索引标识格式无效")
            characters += len(value)
            if characters > MAX_TEXT_CHARS:
                raise RichPostError("飞书富文本媒体索引长度超过上限")
        indexes.append(values)
    return indexes[0], indexes[1]


def _rich_tree(body: dict[str, Any]) -> Any:
    """Select one representation, never append the legacy tree to the Md tree.

    The desktop protocol can keep a reduced legacy preview beside the complete
    richTextWithMd tree. Empty Md fields are common and must not hide legacy
    content. Only a structurally valid, empty Md tree permits the fallback;
    malformed Md must not hide lost media behind a valid legacy preview.
    """
    md = body.get("richTextWithMd")
    if md is not None:
        if not isinstance(md, dict):
            raise RichPostError("飞书新版富文本元素格式无效")
        roots = md.get("elementIds", [])
        elements = md.get("elements", {})
        if (not isinstance(roots, (list, tuple)) or len(roots) > MAX_NODES or
                any(not isinstance(root, str) for root in roots)):
            raise RichPostError("飞书新版富文本根节点格式无效或超过上限")
        if not isinstance(elements, dict):
            raise RichPostError("飞书新版富文本元素字典格式无效")
        nodes = elements.get("dictionary", {})
        if not isinstance(nodes, dict) or len(nodes) > MAX_NODES:
            raise RichPostError("飞书新版富文本节点字典无效或超过上限")
        if roots and any(root not in nodes for root in roots):
            raise RichPostError("飞书新版富文本引用的节点不存在")
        if any(not isinstance(key, str) or not isinstance(node, dict) for key, node in nodes.items()):
            raise RichPostError("飞书新版富文本节点格式无效")
        image_ids, media_ids = _media_indexes(md)
        if roots or nodes or image_ids or media_ids:
            return md
    legacy = body.get("richText")
    return legacy if legacy is not None else md if md is not None else {}


def is_rich_post(message: dict[str, Any]) -> bool:
    """Detect structured content, including TEXT envelopes with rich nodes.

    Lark's TEXT and POST schemas both contain ``richText``. Their display
    summaries replace media with labels, so a TEXT body with media nodes must
    go through the same expansion as POST instead of forwarding its summary.
    Ordinary TEXT with inline emoji keeps its single-message receipt path.
    """
    value = message.get("msg_type", message.get("message_type", ""))
    kind = str(value).lower()
    if kind in {"2", "post"}:
        return True
    body = message.get("content_data")
    if kind not in {"4", "text"} or not isinstance(body, dict):
        return False
    rich = _rich_tree(body)
    if isinstance(rich, dict):
        image_ids, media_ids = _media_indexes(rich)
        if image_ids or media_ids:
            return True
    elements = rich.get("elements") if isinstance(rich, dict) else None
    nodes = elements.get("dictionary") if isinstance(elements, dict) else None
    if not isinstance(nodes, dict):
        return False
    return any(isinstance(node, dict) and str(node.get("tag", "")).lower() in {
                   "2", "img", "18", "media", "49", "markdown"}
               for node in nodes.values())


def has_inline_emotion(message: dict[str, Any]) -> bool:
    """Recognize structured emoji inside TEXT, independently of sticker messages."""
    kind = str(message.get("msg_type", message.get("message_type", ""))).lower()
    body = message.get("content_data")
    if kind not in {"4", "text"} or not isinstance(body, dict):
        return False
    rich = _rich_tree(body)
    elements = rich.get("elements") if isinstance(rich, dict) else None
    nodes = elements.get("dictionary") if isinstance(elements, dict) else None
    return isinstance(nodes, dict) and any(
        isinstance(node, dict) and str(node.get("tag", "")).lower() in {"10", "emotion"}
        for node in nodes.values()
    )


def rich_text_content(part: dict[str, Any], content_types: Any) -> str:
    """Apply text and emoji route switches without changing the part's receipt key."""
    runs = part.get("_rich_text_runs")
    if runs is None:
        return str(part.get("content") or "") if "text" in content_types else ""
    return "".join(value for kind, value in runs if kind in content_types).strip("\n")


def has_unresolved_media(message: dict[str, Any]) -> bool:
    """Signal one safe, same-message completion read for index-only media.

    Structural corruption and unsupported properties are not completion hints.
    The receiver must retain a failed read/parse and apply its existing bounded
    retry policy; a still-incomplete response is not permission to send a summary.
    """
    try:
        if not is_rich_post(message):
            return False
        body = message.get("content_data")
        rich = _rich_tree(body) if isinstance(body, dict) else None
        if not isinstance(rich, dict) or not any(_media_indexes(rich)):
            return False
        rich_post_parts(message)
    except RichPostMediaIncomplete:
        return True
    except RichPostError:
        return False
    return False


def indexed_media_types(message: dict[str, Any]) -> frozenset[int]:
    """Return declared media categories for a same-message completion check.

    These indexes do not establish visible media count, ordering, or resource
    keys. A completion response must still parse and contain these categories;
    merely returning a normal TEXT envelope is not a successful media read.
    Malformed structures raise instead of weakening the required categories.
    """
    body = message.get("content_data")
    if not isinstance(body, dict):
        return frozenset()
    rich = _rich_tree(body)
    if rich is None:
        return frozenset()
    if not isinstance(rich, dict):
        raise RichPostError("飞书富文本元素格式无效")
    image_ids, media_ids = _media_indexes(rich)
    return frozenset(kind for kind, indexes in ((5, image_ids), (15, media_ids)) if indexes)


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


def _markdown_images(content: str) -> list[tuple[int, int, str]]:
    """Locate supported inline image references, without fetching destinations.

    Private Markdown map syntax is undocumented. Only an explicit standard
    ``![alt](destination)`` is resolved by the caller. Code and escaped image
    markers stay literal text; indirect references and HTML images fail rather
    than silently losing media. Other Markdown remains source text.
    """
    spans: list[tuple[int, int, str]] = []
    # Index complete backtick runs once. Repeated unmatched code delimiters
    # must not turn a bounded property into a quadratic scan.
    ticks: dict[int, list[int]] = {}
    for match in re.finditer(r"`+", content):
        ticks.setdefault(match.end() - match.start(), []).append(match.start())
    size = len(content)
    whitespace = " \t\r\n"

    def skip_space(pos: int) -> int:
        while pos < size and content[pos] in whitespace:
            pos += 1
        return pos

    def unescape(value: str) -> str:
        return re.sub(r"\\([!\"#$%&'()*+,\-./:;<=>?@\[\\\]^_`{|}~])", r"\1", value)

    def image(start: int) -> tuple[int, int, str]:
        pos, depth = start + 2, 1
        while pos < size and depth:
            if content[pos] == "\\" and pos + 1 < size:
                pos += 2
                continue
            if content[pos] == "[":
                depth += 1
                if depth > MAX_DEPTH:
                    raise RichPostError("飞书 Markdown 图片说明嵌套过深")
            elif content[pos] == "]":
                depth -= 1
            pos += 1
        if depth or pos >= size or content[pos] != "(":
            raise RichPostError("飞书 Markdown 图片引用格式暂不支持或不完整")
        pos = skip_space(pos + 1)
        begin = pos
        if pos < size and content[pos] == "<":
            begin = pos = pos + 1
            while pos < size and content[pos] != ">":
                if content[pos] in "\r\n<":
                    raise RichPostError("飞书 Markdown 图片地址格式无效")
                if content[pos] == "\\" and pos + 1 < size:
                    pos += 2
                else:
                    pos += 1
            if pos >= size:
                raise RichPostError("飞书 Markdown 图片地址未闭合")
            destination = unescape(content[begin:pos])
            pos += 1
        else:
            depth = 0
            while pos < size:
                char = content[pos]
                if char == "\\" and pos + 1 < size:
                    pos += 2
                    continue
                if char == "(" :
                    depth += 1
                    if depth > MAX_DEPTH:
                        raise RichPostError("飞书 Markdown 图片地址嵌套过深")
                elif char == ")":
                    if not depth:
                        break
                    depth -= 1
                elif char in whitespace:
                    break
                elif char in "<>":
                    raise RichPostError("飞书 Markdown 图片地址格式无效")
                pos += 1
            if depth:
                raise RichPostError("飞书 Markdown 图片地址未闭合")
            destination = unescape(content[begin:pos])
        if not destination:
            raise RichPostError("飞书 Markdown 图片缺少资源引用")
        # A matching alias is still not authority to open a URL or a local
        # path. Destinations are identifiers only, never requests or paths.
        if any(char.isspace() or ord(char) < 32 or char in ":/\\%?#&<>" for char in destination):
            raise RichPostError("飞书 Markdown 外部图片或路径引用暂不支持")
        if pos < size and content[pos] != ")":
            before_space = pos
            pos = skip_space(pos)
            if pos == before_space:
                raise RichPostError("飞书 Markdown 图片地址与标题格式无效")
            if pos < size and content[pos] != ")":
                if content[pos] not in "\"'(":
                    raise RichPostError("飞书 Markdown 图片标题格式无效")
                closing = ")" if content[pos] == "(" else content[pos]
                pos += 1
                while pos < size and content[pos] != closing:
                    if content[pos] in "\r\n":
                        raise RichPostError("飞书 Markdown 图片标题格式无效")
                    pos += 2 if content[pos] == "\\" and pos + 1 < size else 1
                if pos >= size:
                    raise RichPostError("飞书 Markdown 图片标题未闭合")
                pos = skip_space(pos + 1)
        if pos >= size or content[pos] != ")":
            raise RichPostError("飞书 Markdown 图片引用未闭合")
        return start, pos + 1, destination

    def link_end(start: int) -> int:
        # The destination/title of an ordinary link is literal source text,
        # even if it happens to contain the characters ``![``. Image syntax
        # in the label was already visited before reaching this delimiter.
        pos, depth = start + 1, 1
        while pos < size:
            if content[pos] == "\\" and pos + 1 < size:
                pos += 2
                continue
            if content[pos] == "<":
                end = content.find(">", pos + 1)
                if end < 0:
                    break
                pos = end + 1
                continue
            if (depth == 1 and content[pos] in "\"'" and pos > start + 1 and
                    content[pos - 1] in whitespace):
                quote = content[pos]
                pos += 1
                while pos < size and content[pos] != quote:
                    pos += 2 if content[pos] == "\\" and pos + 1 < size else 1
                if pos >= size:
                    break
                pos += 1
                continue
            if content[pos] == "(":
                depth += 1
                if depth > MAX_DEPTH:
                    raise RichPostError("飞书 Markdown 链接嵌套过深")
            elif content[pos] == ")":
                depth -= 1
                if not depth:
                    return pos + 1
            pos += 1
        # A malformed ordinary link stays source text. Its ambiguous image
        # markers must not be promoted into authenticated media requests.
        if content.find("![", start) >= 0:
            raise RichPostError("飞书 Markdown 链接中的图片引用格式不明确")
        return size

    pos = 0
    labels: list[int] = []
    no_angle_close = False
    while pos < size:
        # Fenced blocks are recognized only in their standard line position.
        if pos == 0 or content[pos - 1] == "\n":
            fence = _MARKDOWN_FENCE.match(content, pos)
            if fence:
                marker = fence.group(1)
                end = fence.end()
                closing = re.compile(r"(?m)^ {0,3}" + re.escape(marker[0]) +
                                     "{" + str(len(marker)) + r",}[ \t]*\r?$").search(content, end)
                pos = closing.end() if closing else size
                continue
            if _MARKDOWN_REFERENCE_LINE.match(content, pos):
                # Reference definitions are not visible media. Referenced
                # images themselves are rejected by image() above.
                end = content.find("\n", pos)
                pos = end if end >= 0 else size
                continue
            if content.startswith(("    ", "\t"), pos):
                # Four spaces may mean an indented code block or a nested
                # list. Without interpreting the block grammar, treating an
                # image here as either text or media could silently lose it.
                end = content.find("\n", pos)
                end = end if end >= 0 else size
                if "![" in content[pos:end] or _MARKDOWN_IMAGE_HTML.search(content, pos, end):
                    raise RichPostError("飞书 Markdown 缩进结构中的图片引用暂不支持")
        if content[pos] == "\\" and pos + 1 < size:
            pos += 2
            continue
        if content[pos] == "`":
            end = pos + 1
            while end < size and content[end] == "`":
                end += 1
            candidates = ticks[end - pos]
            following = bisect_right(candidates, pos)
            pos = candidates[following] + end - pos if following < len(candidates) else end
            continue
        if content.startswith("![", pos):
            span = image(pos)
            spans.append(span)
            if len(spans) > MAX_PARTS:
                raise RichPostError("飞书 Markdown 图片数量超过上限")
            pos = span[1]
            continue
        if content[pos] == "<":
            if _MARKDOWN_IMAGE_HTML.match(content, pos):
                raise RichPostError("飞书 Markdown HTML 图片格式暂不支持")
            # An autolink or HTML tag is source text, not an image request.
            end = -1 if no_angle_close else content.find(">", pos + 1)
            if end >= 0:
                pos = end + 1
                continue
            no_angle_close = True
        if content[pos] == "[":
            labels.append(pos)
            if len(labels) > MAX_DEPTH:
                raise RichPostError("飞书 Markdown 链接说明嵌套过深")
        elif content[pos] == "]" and labels:
            labels.pop()
            if pos + 1 < size and content[pos + 1] == "(":
                end = link_end(pos + 1)
                pos = end
                continue
        pos += 1
    return spans


def rich_post_parts(message: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize POST leaves without networking or modifying the input.

    Original message/chat IDs must survive: authenticated media URLs refer to
    the POST envelope, even when a nested leaf is normalized to IMAGE or MEDIA.
    A rootless *flat* legacy body is ordered by numeric element ID. Tree layouts
    require explicit roots; unrelated dictionary entries are never appended.
    """
    if not is_rich_post(message) and not has_inline_emotion(message):
        raise RichPostError("消息不是飞书富文本")
    body = message.get("content_data")
    if not isinstance(body, dict):
        raise RichPostError("飞书富文本缺少可解析的正文；未发送摘要文字")
    parts: list[dict[str, Any]] = []
    text_chunks: list[str] = []
    text_ids: list[str] = []
    text_kinds: list[str] = []
    text_length = 0
    property_bytes = 0
    types = None

    def append_part(kind: int, content: str, data: dict[str, Any], part_id: str) -> None:
        if len(parts) >= MAX_PARTS:
            raise RichPostError("飞书富文本内容段数超过上限")
        parts.append({**message, "msg_type": kind, "message_type": kind,
                      "content": content, "content_data": data, "_rich_part_id": part_id})

    def text(value: Any, node_id: str, *, count: bool = True, kind: str = "text") -> None:
        nonlocal text_length
        if not isinstance(value, str):
            raise RichPostError("飞书富文本文字格式无效")
        if count:
            text_length += len(value)
        if text_length > MAX_TEXT_CHARS:
            raise RichPostError("飞书富文本长度超过上限")
        if value:
            text_chunks.append(value)
            text_ids.append(node_id)
            text_kinds.append(kind)

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
                if "emoji" in text_kinds:
                    parts[-1]["_rich_text_runs"] = tuple(zip(text_kinds, text_chunks))
            text_chunks.clear()
            text_ids.clear()
            text_kinds.clear()

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

    def markdown(node: dict[str, Any], node_id: str) -> None:
        nonlocal text_length
        prop = decode(node, "MarkdownProperty")
        content = prop.content
        text_length += len(content)
        if text_length > MAX_TEXT_CHARS:
            raise RichPostError("飞书富文本长度超过上限")
        if len(prop.images) > MAX_NODES:
            raise RichPostError("飞书 Markdown 图片资源数量超过上限")
        spans = _markdown_images(content)
        if prop.images and not spans:
            raise RichPostError("飞书 Markdown 含图片资源但没有可靠的可见引用")
        references: dict[str, list[Any]] = {}
        for alias, image in prop.images.items():
            references.setdefault(alias, []).append(image)
            if image.imageKey and image.imageKey != alias:
                references.setdefault(image.imageKey, []).append(image)
        offset = 0
        for index, (start, end, destination) in enumerate(spans):
            candidates = references.get(destination, [])
            if not candidates or len({value.SerializeToString(deterministic=True) for value in candidates}) != 1:
                raise RichPostError("飞书 Markdown 图片引用不存在或存在歧义")
            image = candidates[0]
            if (not re.fullmatch(r"[\w.-]{1,200}", image.imageKey) or image.imageKey in {".", ".."}):
                raise RichPostError("飞书 Markdown 图片缺少有效的资源标识")
            text(content[offset:start], f"{node_id}:markdown:text:{index}", count=False)
            flush()
            append_part(5, "", {"imageV2": _proto_dict(image)}, f"{node_id}:markdown:image:{index}")
            offset = end
        text(content[offset:], f"{node_id}:markdown:text:{len(spans)}", count=False)

    title = body.get("title", "")
    if title:
        text(title, "title")
        flush()

    rich = _rich_tree(body)
    if rich is None:
        rich = {}
    if not isinstance(rich, dict):
        raise RichPostError("飞书富文本元素格式无效")
    image_ids, media_ids = _media_indexes(rich)
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
        if image_ids or media_ids:
            raise RichPostMediaIncomplete("飞书富文本含媒体索引但缺少可解析的媒体正文；未发送摘要文字")
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
        elif tag == 49:
            if children:
                raise RichPostError("飞书 Markdown 节点不能同时包含子节点")
            markdown(node, node_id)
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
            key = decode(node, "EmotionProperty").key.strip()
            display = (_EMOTIONS.get(key.upper(), f"[{key}]")
                       if _EMOTION_NAME.fullmatch(key) else "[未识别表情]")
            text(display, node_id, kind="emoji")
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
    if ((image_ids and not any(part["msg_type"] == 5 for part in parts)) or
            (media_ids and not any(part["msg_type"] == 15 for part in parts))):
        raise RichPostMediaIncomplete("飞书富文本含媒体索引但缺少对应媒体正文；未发送摘要文字")
    if not parts:
        raise RichPostError("飞书富文本没有可转发的正文或媒体；未发送摘要文字")
    return parts
