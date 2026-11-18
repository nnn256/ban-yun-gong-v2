"""Expand a Feishu merged chat record without relaying its display summary."""
from __future__ import annotations

from typing import Any

from .larkx_richtext import is_rich_post, rich_post_parts


MERGE_TYPE = 11
MAX_DEPTH = 8
MAX_MESSAGES = 512
MAX_CONTENT_BYTES = 16 * 1024 * 1024
SUPPORTED_TYPES = {2, 3, 4, 5, 7, 10, 15}


def is_merged_forward(message: dict[str, Any]) -> bool:
    value = message.get("msg_type", message.get("message_type", ""))
    return str(value).lower() in {str(MERGE_TYPE), "merge_forward"}


def merged_forward_parts(message: dict[str, Any]) -> list[dict[str, Any]]:
    from larkx.proto.decoders import decode_message_content, enum_to_int
    from larkx.proto.proto_pb2 import MergeForwardContent
    from protobuf_to_dict import protobuf_to_dict

    if not is_merged_forward(message):
        raise ValueError("消息不是飞书合并转发")
    # The copies are accessible through the visible enclosing message, even
    # if forwarded children refer to a different chat or original author.
    address = {"msg_id": message.get("msg_id") or message.get("message_id"), "chat_id": message.get("chat_id")}
    contents: list[dict[str, Any]] = []
    seen_messages = 0
    content_bytes = 0

    def unpack(body, parent: str = "", depth: int = 0) -> None:
        nonlocal seen_messages, content_bytes
        children = body.get("messages") if isinstance(body, dict) else None
        if not isinstance(children, (list, tuple)) or depth > MAX_DEPTH:
            raise ValueError("飞书合并转发内容无效或嵌套过深")
        for index, child in enumerate(children):
            seen_messages += 1
            if seen_messages > MAX_MESSAGES or not isinstance(child, dict):
                raise ValueError("飞书合并转发消息数量或格式无效")
            kind = enum_to_int(child.get("type", 0))
            if kind not in SUPPORTED_TYPES and kind != MERGE_TYPE:
                continue
            path = f"{parent}{index}"
            raw = child.get("content", b"")
            if not isinstance(raw, bytes):
                raise ValueError("飞书合并转发正文格式无效")
            content_bytes += len(raw)
            if content_bytes > MAX_CONTENT_BYTES:
                raise ValueError("飞书合并转发正文超过上限")
            if kind == MERGE_TYPE:
                unpack(protobuf_to_dict(MergeForwardContent.FromString(raw)), path + ".", depth + 1)
                continue
            summary, data = decode_message_content(kind, raw)
            if not isinstance(data, dict) or not data:
                raise ValueError("飞书合并转发子消息无法解析")
            part = {**address, "msg_type": kind, "content": summary if kind == 4 else "",
                    "content_data": data, "_rich_part_id": path}
            if is_rich_post(part):
                for leaf in rich_post_parts(part):
                    contents.append({**leaf, "_rich_part_id": f"{path}:{leaf['_rich_part_id']}"})
            else:
                contents.append(part)
            if len(contents) > MAX_MESSAGES:
                raise ValueError("飞书合并转发内容段数超过上限")

    unpack(message.get("content_data"))
    return contents
