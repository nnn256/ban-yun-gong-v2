"""Personal Feishu image and attachment transport without resource-key text."""
from __future__ import annotations

import base64
import binascii
import importlib
import mimetypes
import re
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname


MAX_IMAGE_BYTES = 25 * 1024 * 1024
MAX_ATTACHMENT_BYTES = 100 * 1024 * 1024


class ImageTransferError(ValueError):
    """A media failure whose message does not expose URLs or credentials."""


def _lark_media() -> Any:
    return importlib.import_module("larkx.media")


def image_extension(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    if data.startswith(b"BM"):
        return ".bmp"
    if data.startswith((b"II*\x00", b"MM\x00*")):
        return ".tiff"
    raise ImageTransferError("图片格式无法识别")


def _checked(data: bytes) -> bytes:
    if not data:
        raise ImageTransferError("图片内容为空")
    if len(data) > MAX_IMAGE_BYTES:
        raise ImageTransferError("图片超过 25MB 上限")
    image_extension(data)
    return data


def _read_file(path: Path) -> bytes:
    if not path.is_file():
        raise ImageTransferError("图片文件不存在")
    if path.stat().st_size > MAX_IMAGE_BYTES:
        raise ImageTransferError("图片超过 25MB 上限")
    with path.open("rb") as stream:
        return _checked(stream.read(MAX_IMAGE_BYTES + 1))


def image_bytes(source: Any) -> bytes:
    """Read bytes, a path, URL/base64 reference, or an AstrBot Image component.

    Blocking network/file work belongs in asyncio.to_thread at the caller.
    QQ image URLs are fetched without Feishu authentication cookies.
    """
    if isinstance(source, (bytes, bytearray, memoryview)):
        return _checked(bytes(source))
    if isinstance(source, Path):
        return _read_file(source)
    if not isinstance(source, str):
        candidates = [getattr(source, name, None) for name in ("url", "path", "file")]
        source = next((str(value) for value in candidates if value), "")
    if not source:
        raise ImageTransferError("图片没有可用的文件或 URL")
    if source.startswith("base64://") or source.startswith("data:image/"):
        encoded = source[len("base64://"):] if source.startswith("base64://") else source.partition(",")[2]
        if len(encoded) > (MAX_IMAGE_BYTES * 4 // 3 + 8):
            raise ImageTransferError("图片超过 25MB 上限")
        try:
            return _checked(base64.b64decode(encoded, validate=True))
        except (ValueError, binascii.Error) as exc:
            raise ImageTransferError("图片 base64 数据无效") from exc
    if source.startswith(("http://", "https://")):
        import requests

        chunks = []
        total = 0
        with requests.get(source, timeout=(10, 45), stream=True) as response:
            response.raise_for_status()
            declared_size = response.headers.get("content-length", "")
            if declared_size.isdigit() and int(declared_size) > MAX_IMAGE_BYTES:
                raise ImageTransferError("图片超过 25MB 上限")
            for chunk in response.iter_content(65536):
                total += len(chunk)
                if total > MAX_IMAGE_BYTES:
                    raise ImageTransferError("图片超过 25MB 上限")
                chunks.append(chunk)
        return _checked(b"".join(chunks))
    if source.startswith("file:"):
        parsed = urlsplit(source)
        path = url2pathname(parsed.path)
        if parsed.netloc and parsed.netloc != "localhost":
            path = "//" + parsed.netloc + path
        return _read_file(Path(path))
    return _read_file(Path(source))


def send_image(client: Any, chat_id: str, source: Any) -> str | None:
    """Upload once and send a native IMAGE message, returning its server ID."""
    if client is None or not str(chat_id):
        raise ImageTransferError("飞书客户端或目标会话不可用")
    data = image_bytes(source)
    media = _lark_media()
    # LarkAgentX's official uploader accepts a filename. Keeping it in a scoped
    # temp directory also closes the file before Windows opens it for upload.
    with tempfile.TemporaryDirectory(prefix="mover-image-") as directory:
        path = Path(directory) / ("image" + image_extension(data))
        path.write_bytes(data)
        key = media.upload_image(client.auth, str(path))
        if not key:
            raise ImageTransferError("飞书图片上传未返回资源标识")
        message_id = client.send(str(chat_id), image_key=key)
    return str(message_id) if message_id else None


def is_image_message(message: dict[str, Any]) -> bool:
    value = message.get("msg_type", message.get("message_type"))
    return value in (5, "5", "image", "IMAGE")


def download_image(client: Any, message: dict[str, Any]) -> bytes:
    """Download an authenticated Feishu IMAGE as bytes for Image.fromBytes()."""
    if client is None or not is_image_message(message):
        raise ImageTransferError("飞书图片消息或客户端不可用")
    media = _lark_media()
    content_data = message.get("content_data") or {}
    if not isinstance(content_data, dict):
        raise ImageTransferError("飞书图片消息格式无效")
    key = media.extract_resource_key(5, content_data)
    message_id = str(message.get("msg_id") or message.get("message_id") or "")
    chat_id = str(message.get("chat_id") or "")
    if not key or not message_id or not chat_id:
        raise ImageTransferError("飞书图片缺少下载所需的资源标识")
    auth = client.auth
    resource = media.message_resource_url(message_id, key, chat_id, host=auth.region.file_host)
    data, _content_type = media.download(auth, resource, max_bytes=MAX_IMAGE_BYTES)
    return _checked(data)


def attachment_kind(message: dict[str, Any]) -> str | None:
    value = str(message.get("msg_type", message.get("message_type", ""))).lower()
    return {"3": "file", "file": "file", "15": "video", "video": "video", "media": "video",
            "7": "audio", "audio": "audio", "record": "audio", "10": "sticker", "sticker": "sticker"}.get(value)


def _attachment_checked(data: bytes) -> bytes:
    if not data:
        raise ImageTransferError("附件内容为空")
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise ImageTransferError("附件超过 100MB 上限")
    return data


def _source_reference(source: Any) -> str:
    if isinstance(source, (str, Path)):
        return str(source)
    candidates = [getattr(source, name, None) for name in ("url", "path", "file")]
    for candidate in candidates:
        if not candidate:
            continue
        value = str(candidate)
        if value.startswith(("http://", "https://", "base64://", "data:", "file:")) or Path(value).is_file():
            return value
    return ""


def attachment_bytes(source: Any) -> bytes:
    """Read an attachment source without forwarding authenticated URLs to QQ."""
    if isinstance(source, (bytes, bytearray, memoryview)):
        return _attachment_checked(bytes(source))
    reference = _source_reference(source)
    if not reference:
        raise ImageTransferError("附件没有可用的文件或 URL")
    if reference.startswith("base64://") or reference.startswith("data:"):
        encoded = reference[len("base64://"):] if reference.startswith("base64://") else reference.partition(",")[2]
        if len(encoded) > (MAX_ATTACHMENT_BYTES * 4 // 3 + 8):
            raise ImageTransferError("附件超过 100MB 上限")
        try:
            return _attachment_checked(base64.b64decode(encoded, validate=True))
        except (ValueError, binascii.Error) as exc:
            raise ImageTransferError("附件 base64 数据无效") from exc
    if reference.startswith(("http://", "https://")):
        import requests

        data = bytearray()
        with requests.get(reference, timeout=(10, 90), stream=True) as response:
            response.raise_for_status()
            declared = response.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > MAX_ATTACHMENT_BYTES:
                raise ImageTransferError("附件超过 100MB 上限")
            for chunk in response.iter_content(65536):
                data.extend(chunk)
                if len(data) > MAX_ATTACHMENT_BYTES:
                    raise ImageTransferError("附件超过 100MB 上限")
        return _attachment_checked(bytes(data))
    if reference.startswith("file:"):
        parsed = urlsplit(reference)
        reference = url2pathname(parsed.path)
        if parsed.netloc and parsed.netloc != "localhost":
            reference = "//" + parsed.netloc + reference
    path = Path(reference)
    if not path.is_file():
        raise ImageTransferError("附件文件不存在")
    if path.stat().st_size > MAX_ATTACHMENT_BYTES:
        raise ImageTransferError("附件超过 100MB 上限")
    with path.open("rb") as stream:
        return _attachment_checked(stream.read(MAX_ATTACHMENT_BYTES + 1))


def _media_extension(data: bytes, kind: str, mime: str = "") -> str:
    if data.startswith((b"#!SILK_V3", b"\x02#!SILK_V3")):
        return ".silk"
    if data.startswith(b"#!AMR"):
        return ".amr"
    if data.startswith(b"OggS"):
        return ".ogg"
    if data.startswith(b"fLaC"):
        return ".flac"
    if data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return ".wav"
    if data.startswith(b"RIFF") and data[8:12] == b"AVI ":
        return ".avi"
    if data.startswith(b"ID3") or data[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return ".mp3"
    if len(data) > 12 and data[4:8] == b"ftyp":
        return ".m4a" if kind == "audio" else ".mp4"
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        return ".webm"
    extension = mimetypes.guess_extension(mime.partition(";")[0].strip()) if mime else ""
    if extension:
        return extension
    # A missing codec is kept opaque; assigning an MP4/WAV suffix would make
    # an unconverted file appear playable when no format has been measured.
    return ".bin"


def _silk_to_wav(data: bytes) -> bytes:
    """Decode QQ SILK to a widely playable WAV when the local codec is present."""
    if not data.startswith((b"#!SILK_V3", b"\x02#!SILK_V3")):
        return data
    try:
        import io
        import wave
        import pysilk

        pcm = io.BytesIO()
        pysilk.decode(io.BytesIO(data), pcm, 24000)
        frames = pcm.getvalue()
        if not frames:
            return data
        output = io.BytesIO()
        with wave.open(output, "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(24000)
            writer.writeframes(frames)
        return output.getvalue()
    except Exception:
        return data


def attachment_filename(filename: str, data: bytes, kind: str = "file", mime: str = "") -> str:
    raw = unquote(str(filename or "")).replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", raw).strip(" .")[:180]
    if not name:
        name = {"audio": "audio", "video": "video"}.get(kind, "attachment")
    if Path(name).stem.upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        name = "_" + name
    if not Path(name).suffix:
        name += _media_extension(data, kind, mime)
    return name


def send_attachment(client: Any, chat_id: str, source: Any, filename: str = "", kind: str = "file") -> str | None:
    """Send a real FILE attachment, including audio/video when native upload is unavailable.

    The pinned LarkAgentX compose API supports IMAGE and FILE uploads. It has
    no measured audio/video upload contract, so this method never claims that
    an uploaded FILE is a native voice bubble or video player.
    """
    if client is None or not str(chat_id):
        raise ImageTransferError("飞书客户端或目标会话不可用")
    data = attachment_bytes(source)
    original_data = data
    data = _silk_to_wav(data) if kind == "audio" else data
    reference = _source_reference(source)
    supplied_name = filename or str(getattr(source, "name", "") or "")
    if not supplied_name and reference and not reference.startswith(("base64://", "data:")):
        supplied_name = urlsplit(reference).path if "://" in reference else reference
    name = attachment_filename(supplied_name, data, kind)
    if data is not original_data and name.casefold().endswith(".silk"):
        name = str(Path(name).with_suffix(".wav"))
    media = _lark_media()
    with tempfile.TemporaryDirectory(prefix="mover-attachment-") as directory:
        path = Path(directory) / name
        path.write_bytes(data)
        uploaded = media.upload_file(client.auth, str(path))
        if not isinstance(uploaded, dict) or not uploaded.get("key"):
            raise ImageTransferError("飞书附件上传未返回资源标识")
        message_id = client.send(str(chat_id), file=uploaded)
    return str(message_id) if message_id else None


def download_attachment(client: Any, message: dict[str, Any]) -> tuple[bytes, str, str]:
    """Return (bytes, sanitized filename, audio/video/file kind) for a Feishu attachment."""
    kind = attachment_kind(message)
    if client is None or kind is None:
        raise ImageTransferError("飞书附件消息或客户端不可用")
    content = message.get("content_data") or {}
    if not isinstance(content, dict):
        raise ImageTransferError("飞书附件消息格式无效")
    media = _lark_media()
    msg_type = {"file": 3, "video": 15, "audio": 7, "sticker": 10}[kind]
    key = media.extract_resource_key(msg_type, content)
    message_id = str(message.get("msg_id") or message.get("message_id") or "")
    chat_id = str(message.get("chat_id") or "")
    if not key or not message_id or not chat_id:
        raise ImageTransferError("飞书附件缺少下载所需的资源标识")
    auth = client.auth
    resource = media.message_resource_url(message_id, key, chat_id, host=auth.region.file_host)
    data, content_type = media.download(auth, resource, max_bytes=MAX_ATTACHMENT_BYTES)
    data = _attachment_checked(data)
    mime = str(content.get("mime") or content_type or "")
    if kind == "sticker":
        extension = image_extension(data)
        return data, f"sticker{extension}", kind
    filename = attachment_filename(str(content.get("name") or content.get("fileName") or ""), data, kind, mime)
    return data, filename, kind
