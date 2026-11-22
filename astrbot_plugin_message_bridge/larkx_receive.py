"""Intercept upstream's lossy one-shot batch/SID reads before forwarding."""
from __future__ import annotations

from .build_identity import capture_module_build

MODULE_BUILD = capture_module_build(__file__)

import asyncio
import importlib
from typing import Any, Callable

from .larkx_media import image_resource_key
from .larkx_merge import is_merged_forward, is_system_message
from .larkx_richtext import has_unresolved_media, indexed_media_types, is_rich_post, rich_post_parts


READ_BATCH_SIZE = 50
MAX_NESTED_SID_DEPTH = 4


def normalize_entity(entity: dict[str, Any], *, at_me: bool = False) -> dict[str, Any]:
    """Keep completion flags that the pinned display normalizer discards.

    No global upstream decoder is patched. Rich node properties and merged
    child content/source metadata remain inside the decoded content_data.
    """
    from larkx.proto import decoders

    if not isinstance(entity, dict):
        raise ValueError("Invalid Feishu message entity")
    for name in ("isTruncated", "isRemoved"):
        if name in entity and not isinstance(entity[name], bool):
            raise ValueError("Invalid Feishu message completion flag")
    message = decoders.message_dict(entity, at_me=at_me)
    message.update(
        is_truncated=entity.get("isTruncated", False),
        is_removed=entity.get("isRemoved", False),
        parent_source_message_id=entity.get("parentSourceMessageId") or "",
        root_source_message_id=entity.get("rootSourceMessageId") or "",
    )
    return message


def decode_push_messages(payload: bytes) -> list[dict[str, Any]]:
    """Decode a push without dropping truncation before its durable ACK."""
    from larkx.proto import proto_pb2 as P
    from protobuf_to_dict import protobuf_to_dict

    request = protobuf_to_dict(P.PushMessagesRequest.FromString(payload))
    at_me = request.get("messagesAtMe") or {}
    return [normalize_entity(entity, at_me=bool(at_me.get(entity.get("id")) or at_me.get(key)))
            for key, entity in (request.get("messages") or {}).items()]


def decode_batch_copy_messages(payload: bytes) -> tuple[list[str], list[dict[str, Any]]]:
    """Preserve the raw lastMessage flags while retaining every batch ID."""
    from larkx.proto.gateway import _lark_all
    from protobuf_to_dict import protobuf_to_dict

    request = _lark_all().messages.PushBatchCopyMessages.FromString(payload)
    ids = list(dict.fromkeys(str(value) for value in request.messageIds if value))
    messages = []
    if request.HasField("lastMessage") and request.lastMessage.id:
        message = normalize_entity(protobuf_to_dict(request.lastMessage))
        messages.append(message)
        if message["msg_id"] not in ids:
            ids.append(message["msg_id"])
    return ids, messages


def batch_copy_work(payload: bytes) -> dict[str, Any]:
    ids, messages = decode_batch_copy_messages(payload)
    messages = [message for message in messages if message.get("from_id")
                or is_system_message(message) or message.get("is_removed") is True]
    embedded_ids = [message.get("msg_id") or message.get("message_id") for message in messages]
    wanted = list(dict.fromkeys(str(value) for value in [*ids, *embedded_ids] if value))
    return {"ids": wanted, "embedded": messages}


def normalized_sids(values: Any) -> list[str]:
    """SID requests and response map keys are strings on the pinned wire."""
    if not isinstance(values, (list, tuple)):
        raise ValueError("Invalid Feishu SID list")
    if any(type(value) not in (int, str) or not value for value in values):
        raise ValueError("Invalid Feishu SID")
    return list(dict.fromkeys(map(str, values)))


def _merged_completion_leaves(message: dict[str, Any]):
    """Read embedded metadata with the same budgets as the delivery parser.

    Explicitly truncated children are yielded without parsing their partial
    bytes. Every other rich leaf is decoded directly, never through the
    upstream recursive summary decoder.
    """
    from larkx.proto.decoders import enum_to_int
    from larkx.proto.proto_pb2 import MergeForwardContent
    from protobuf_to_dict import protobuf_to_dict

    from .larkx_merge import MAX_CONTENT_BYTES, MAX_DEPTH, MAX_MESSAGES, MERGE_TYPE, SUPPORTED_TYPES, SYSTEM_TYPE

    seen = 0
    content_bytes = 0

    def scan(body: Any, depth: int = 0):
        nonlocal seen, content_bytes
        children = body.get("messages") if isinstance(body, dict) else None
        if not isinstance(children, (list, tuple)) or depth > MAX_DEPTH:
            raise ValueError("Invalid Feishu merged completion metadata")
        for child in children:
            seen += 1
            if not isinstance(child, dict) or seen > MAX_MESSAGES:
                raise ValueError("Invalid Feishu merged completion metadata")
            if child.get("isRemoved") is True:
                continue
            kind = enum_to_int(child.get("type", 0))
            if kind == SYSTEM_TYPE:
                continue
            if child.get("isTruncated") is True:
                yield child, None, None
                continue
            if kind not in SUPPORTED_TYPES and kind != MERGE_TYPE:
                continue
            raw = child.get("content", b"")
            if not isinstance(raw, bytes):
                raise ValueError("Invalid Feishu merged completion content")
            content_bytes += len(raw)
            if content_bytes > MAX_CONTENT_BYTES:
                raise ValueError("Feishu merged completion content exceeds limit")
            if kind == MERGE_TYPE:
                if depth >= MAX_DEPTH:
                    raise ValueError("Invalid Feishu merged completion metadata")
                # The upstream summary decoder recursively expands children
                # before returning. Parse only this container so our shared
                # depth/count/byte limits apply before each further descent.
                nested = protobuf_to_dict(MergeForwardContent.FromString(raw))
                yield from scan(nested, depth + 1)
            else:
                yield child, kind, raw

    yield from scan(message.get("content_data"))


def _rich_completion_leaf(kind: int, raw: bytes) -> dict[str, Any]:
    from larkx.proto.proto_pb2 import PostContent, TextContent
    from protobuf_to_dict import protobuf_to_dict

    schema = PostContent if kind == 2 else TextContent
    return {"msg_type": kind, "content_data": protobuf_to_dict(schema.FromString(raw))}


def needs_message_completion(message: dict[str, Any]) -> bool:
    """Read this visible message when its metadata proves it is incomplete.

    Explicit truncation or media without a usable body requests one same-ID
    read. Reply/quote identifiers and display placeholders alone
    never request conversation history or invent download keys.
    """
    if is_system_message(message) or message.get("is_removed") is True:
        return False
    if message.get("msg_type") == 5 and not image_resource_key(message):
        return True
    if message.get("is_truncated") is True or has_unresolved_media(message):
        return True

    if is_merged_forward(message):
        for child, kind, raw in _merged_completion_leaves(message):
            if child.get("isTruncated") is True:
                return True
            if kind in {2, 4} and has_unresolved_media(_rich_completion_leaf(kind, raw)):
                return True
    return False


def completion_preserves_media(original: dict[str, Any], complete: dict[str, Any]) -> bool:
    """Reject a same-ID response that replaces declared media with a summary.

    Media indexes establish categories, not image counts or ordering. A valid
    completion must parse normally and retain every category declared by the
    available original rich bodies. Truncated embedded bytes are not trusted
    as a complete body; the response still passes the full delivery parser.
    """
    from .larkx_merge import merged_forward_parts

    if original.get("msg_type") == 5:
        return bool(image_resource_key(complete))
    required = set(indexed_media_types(original)) if is_rich_post(original) else set()
    original_merged = is_merged_forward(original)
    if original_merged and original.get("is_truncated") is not True:
        for _child, kind, raw in _merged_completion_leaves(original):
            if kind in {2, 4}:
                required.update(indexed_media_types(_rich_completion_leaf(kind, raw)))
    if required:
        if original_merged != is_merged_forward(complete):
            return False
        if not original_merged and not is_rich_post(complete):
            return False
    if is_merged_forward(complete):
        parts = merged_forward_parts(complete)
    elif is_rich_post(complete):
        parts = rich_post_parts(complete)
    else:
        return not required
    return required.issubset({part.get("msg_type") for part in parts})


def reliable_client_class(base: type, enqueue: Callable[..., Any]) -> type:
    """Use the pinned client's websocket/decoder, but persist pending reads.

    Upstream acknowledges a push before a read. Its one-shot _batch_copy drops
    even lastMessage when messages_by_ids raises, and never retries a partial
    response. These overrides only capture IDs and already decoded messages;
    the plugin's receive worker performs the network reads and recovery.
    """
    class ReliableLarkClient(base):
        def messages_by_ids(self, msg_ids: Any) -> list[dict[str, Any]]:
            wanted = list(dict.fromkeys(str(value) for value in msg_ids))
            response = self.api("messages.PullMessagesByIdsRequest", {"messageIds": wanted})
            return [normalize_entity(entity) for entity in ((response or {}).get("messages") or {}).values()
                    if str(entity.get("id")) in wanted]

        async def connect_websocket(self, on_message: Any, on_connected: Any = None) -> None:
            """Persist a valid message push before sending its acknowledgement.

            The stock client ACKs first and dispatches in a detached future;
            process interruption between those actions loses an acknowledged
            picture. This path only decodes and captures local work before ACK.
            Media reads and actual sends are performed by the receive workers.
            """
            from larkx.proto import decoders

            module = importlib.import_module("larkx.client")
            self._ensure_loop()
            async with module.websockets.connect(self.build_ws_url()) as ws:
                if on_connected:
                    on_connected()
                heartbeat = asyncio.create_task(self.heartbeat_loop(ws))
                try:
                    async for raw in ws:
                        try:
                            _, packet = decoders.parse_ws_frame(raw)
                            if "json" in packet:
                                continue
                            cmd = packet.get("cmd")
                            if cmd == module.CMD_PUSH_MESSAGES:
                                for message in decode_push_messages(packet.get("payload") or b""):
                                    if message.get("from_id") and not is_system_message(message):
                                        await on_message(message)
                            elif cmd == module.CMD_BATCH_COPY:
                                await self._batch_copy(packet.get("payload") or b"", on_message)
                            elif cmd == module.CMD_PUSH_SID:
                                await self._pull_sids(decoders.push_sids(raw), on_message)
                            sid = packet.get("sid")
                            if sid is not None:
                                await self.send_ack(ws, sid)
                        except Exception as exc:
                            # No ACK on failed parsing/local disk commit: the
                            # server can replay, and durable receipts dedupe it.
                            self._warn_bad_frame(exc)
                            raise
                finally:
                    heartbeat.cancel()
                    await asyncio.gather(heartbeat, return_exceptions=True)

        async def _batch_copy(self, payload: bytes, on_message: Any) -> None:
            await enqueue("batch", batch_copy_work(payload))

        async def _pull_sids(self, sids: Any, on_message: Any) -> None:
            if sids:
                values = normalized_sids(sids)
                for offset in range(0, len(values), READ_BATCH_SIZE):
                    await enqueue("sid", {"sids": values[offset:offset + READ_BATCH_SIZE]})

    return ReliableLarkClient
