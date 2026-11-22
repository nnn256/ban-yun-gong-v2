"""Personal Feishu image and attachment transport without resource-key text."""
from __future__ import annotations

from .build_identity import capture_module_build

MODULE_BUILD = capture_module_build(__file__)

import base64
import binascii
import ipaddress
import importlib
import math
import mimetypes
import os
import re
import shutil
import socket
import struct
import subprocess
import tempfile
import zlib
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit
from urllib.request import url2pathname


MAX_IMAGE_BYTES = 25 * 1024 * 1024
MAX_ATTACHMENT_BYTES = 100 * 1024 * 1024
MAX_MEDIA_REDIRECTS = 4
MAX_NATIVE_DURATION_MS = 2_147_483_647
NATIVE_UPLOAD_PART_SIZE = 4 * 1024 * 1024
_IMAGE_RESOURCE_KEY = re.compile(r"[A-Za-z0-9_.-]{1,200}")


class ImageTransferError(ValueError):
    """A media failure whose message does not expose URLs or credentials."""


class NativeMediaDeliveryUncertain(ImageTransferError):
    """The gateway may have accepted the message; do not retry it blindly."""


def _validate_media_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ImageTransferError("媒体地址格式无效")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise ImageTransferError("媒体地址端口无效") from exc
    if port not in {80, 443}:
        raise ImageTransferError("媒体地址端口不受支持")
    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise ImageTransferError("媒体地址不能指向本机或内部域名")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        if address.is_loopback or address.is_private or address.is_link_local or address.is_reserved:
            raise ImageTransferError("媒体地址不能指向本机或内部地址")
        addresses = {address}
    else:
        try:
            resolved = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            addresses = {ipaddress.ip_address(item[4][0].split("%", 1)[0]) for item in resolved}
        except (OSError, ValueError) as exc:
            raise ImageTransferError("媒体地址解析失败") from exc
    if not addresses or any(not address.is_global for address in addresses):
        raise ImageTransferError("媒体地址解析到非公网 IP")


def _download_http_bytes(url: str, max_bytes: int, timeout: tuple[int, int]) -> bytes:
    import requests
    current = url
    for redirect_count in range(MAX_MEDIA_REDIRECTS + 1):
        _validate_media_url(current)
        with requests.get(current, timeout=timeout, stream=True, allow_redirects=False) as response:
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location or redirect_count >= MAX_MEDIA_REDIRECTS:
                    raise ImageTransferError("媒体重定向无效或次数过多")
                current = urljoin(current, location)
                continue
            response.raise_for_status()
            declared = response.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > max_bytes:
                raise ImageTransferError("媒体文件超过大小上限")
            data = bytearray()
            for chunk in response.iter_content(65536):
                data.extend(chunk)
                if len(data) > max_bytes:
                    raise ImageTransferError("媒体文件超过大小上限")
            return bytes(data)
    raise ImageTransferError("媒体重定向无效或次数过多")


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
        return _checked(_download_http_bytes(source, MAX_IMAGE_BYTES, (10, 45)))
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
        try:
            message_id = client.send(str(chat_id), image_key=key)
        except Exception as exc:
            raise NativeMediaDeliveryUncertain("飞书图片发送结果不确定，未自动重发") from exc
    if not message_id:
        raise NativeMediaDeliveryUncertain("飞书图片发送未返回回执，未自动重发")
    return str(message_id)


def is_image_message(message: dict[str, Any]) -> bool:
    value = message.get("msg_type", message.get("message_type"))
    return value in (5, "5", "image", "IMAGE")


def image_resource_key(message: dict[str, Any]) -> str | None:
    """Read the pinned LarkAgentX IMAGE fields without auth or networking."""
    body = message.get("content_data")
    if not is_image_message(message) or not isinstance(body, dict):
        return None
    v2 = body.get("imageV2")
    legacy = body.get("image")
    origin = legacy.get("origin") if isinstance(legacy, dict) else None
    keys = (
        v2.get("imageKey") if isinstance(v2, dict) else None,
        origin.get("key") if isinstance(origin, dict) else None,
        legacy.get("imageKey") if isinstance(legacy, dict) else None,
    )
    return next((key for key in keys if isinstance(key, str) and key not in {".", ".."}
                 and _IMAGE_RESOURCE_KEY.fullmatch(key)), None)


def download_image(client: Any, message: dict[str, Any]) -> bytes:
    """Download an authenticated Feishu IMAGE as bytes for Image.fromBytes()."""
    if client is None or not is_image_message(message):
        raise ImageTransferError("飞书图片消息或客户端不可用")
    key = image_resource_key(message)
    message_id = str(message.get("msg_id") or message.get("message_id") or "")
    chat_id = str(message.get("chat_id") or "")
    if not key or not message_id or not chat_id:
        raise ImageTransferError("飞书图片缺少下载所需的资源标识")
    media = _lark_media()
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
        return _attachment_checked(_download_http_bytes(reference, MAX_ATTACHMENT_BYTES, (10, 90)))
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


def _ffmpeg_executable() -> str:
    configured = os.environ.get("MOVER_FFMPEG", "").strip()
    if configured:
        if not Path(configured).is_file():
            raise ImageTransferError("配置的 FFmpeg 程序不存在")
        return str(Path(configured).resolve())
    try:
        imageio_ffmpeg = importlib.import_module("imageio_ffmpeg")
        executable = imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError, OSError):
        executable = shutil.which("ffmpeg")
    if not executable or not Path(executable).is_file():
        raise ImageTransferError("原生语音和视频需要本机 FFmpeg，请安装 imageio-ffmpeg")
    return str(executable)


def _run_ffmpeg(arguments: list[str]) -> None:
    try:
        result = subprocess.run(
            [_ffmpeg_executable(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *arguments],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            timeout=300, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ImageTransferError("本机媒体转码失败或超时，未上传原生媒体") from exc
    if result.returncode:
        raise ImageTransferError("媒体格式无法解码或转码，未上传原生媒体")


def _validated_duration(milliseconds: int) -> int:
    if isinstance(milliseconds, bool) or not isinstance(milliseconds, int) or not 1 <= milliseconds <= MAX_NATIVE_DURATION_MS:
        raise ImageTransferError("媒体时长无效或超出原生消息范围")
    return milliseconds


def _opus_duration_ms(data: bytes) -> int:
    """Read the final Ogg granule in 48 kHz Opus samples, excluding pre-skip."""
    offset, serial, preskip, final_granule = 0, None, None, None
    while offset < len(data):
        if len(data) - offset < 27 or data[offset:offset + 4] != b"OggS" or data[offset + 4] != 0:
            raise ImageTransferError("原生语音不是有效的 Ogg Opus 文件")
        segments = data[offset + 26]
        header_end = offset + 27 + segments
        if header_end > len(data):
            raise ImageTransferError("原生语音 Ogg 页被截断")
        page_end = header_end + sum(data[offset + 27:header_end])
        if page_end > len(data):
            raise ImageTransferError("原生语音 Ogg 数据被截断")
        page_serial = struct.unpack_from("<I", data, offset + 14)[0]
        payload = data[header_end:page_end]
        if serial is None:
            if not payload.startswith(b"OpusHead") or len(payload) < 19:
                raise ImageTransferError("原生语音需要 Opus 编码")
            serial = page_serial
            preskip = struct.unpack_from("<H", payload, 10)[0]
        elif page_serial != serial:
            raise ImageTransferError("原生语音不接受多个 Ogg 流")
        granule = struct.unpack_from("<Q", data, offset + 6)[0]
        if granule != 0xFFFFFFFFFFFFFFFF:
            final_granule = granule
        offset = page_end
    if preskip is None or final_granule is None or final_granule <= preskip:
        raise ImageTransferError("原生语音没有有效时长")
    return _validated_duration(math.ceil((final_granule - preskip) * 1000 / 48_000))


def _mp4_duration_ms(data: bytes) -> int:
    """Read the normalized MP4 movie timescale rather than trusting a filename."""
    def boxes(start: int, end: int):
        position = start
        while position < end:
            if end - position < 8:
                raise ImageTransferError("原生视频 MP4 数据被截断")
            size, kind = struct.unpack_from(">I4s", data, position)
            header = 8
            if size == 1:
                if end - position < 16:
                    raise ImageTransferError("原生视频 MP4 数据被截断")
                size, header = struct.unpack_from(">Q", data, position + 8)[0], 16
            elif size == 0:
                size = end - position
            if size < header or position + size > end:
                raise ImageTransferError("原生视频 MP4 文件结构无效")
            yield kind, position + header, position + size
            position += size

    for kind, start, end in boxes(0, len(data)):
        if kind != b"moov":
            continue
        for child, payload, child_end in boxes(start, end):
            if child != b"mvhd":
                continue
            version = data[payload] if payload < child_end else -1
            if version == 0 and child_end - payload >= 20:
                scale, duration = struct.unpack_from(">II", data, payload + 12)
            elif version == 1 and child_end - payload >= 32:
                scale = struct.unpack_from(">I", data, payload + 20)[0]
                duration = struct.unpack_from(">Q", data, payload + 24)[0]
            else:
                raise ImageTransferError("原生视频时长字段无效")
            if scale <= 0 or duration <= 0:
                raise ImageTransferError("原生视频没有有效时长")
            return _validated_duration(math.ceil(duration * 1000 / scale))
    raise ImageTransferError("原生视频没有 MP4 时长元数据")


def _normalize_native_media(source: Any, kind: str, directory: Path) -> dict[str, Any]:
    data = attachment_bytes(source)
    if kind == "audio" and data.startswith((b"#!SILK_V3", b"\x02#!SILK_V3")):
        decoded = _silk_to_wav(data)
        if decoded is data or not decoded.startswith(b"RIFF"):
            raise ImageTransferError("QQ SILK 语音解码失败，未改发普通文件")
        data = decoded
    extension = _media_extension(data, kind)
    allowed = {"audio": {".wav", ".mp3", ".ogg", ".flac", ".amr", ".m4a"},
               "video": {".mp4", ".webm", ".avi"}}
    if extension not in allowed[kind]:
        raise ImageTransferError("不支持的原生媒体容器，未改发普通文件")
    input_path = directory / ("source" + extension)
    input_path.write_bytes(data)
    output_path = directory / ("audio.ogg" if kind == "audio" else "video.mp4")
    common = ["-protocol_whitelist", "file,pipe", "-i", str(input_path)]
    if kind == "audio":
        _run_ffmpeg([*common, "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "48000",
                     "-c:a", "libopus", "-b:a", "32k", "-fs", str(MAX_ATTACHMENT_BYTES + 1), str(output_path)])
        normalized = _attachment_checked(output_path.read_bytes())
        return {"path": output_path, "mime": "audio/ogg", "duration_ms": _opus_duration_ms(normalized), "preview": None}
    _run_ffmpeg([*common, "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libx264",
                 "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p", "-c:a", "aac",
                 "-b:a", "128k", "-movflags", "+faststart", "-fs", str(MAX_ATTACHMENT_BYTES + 1), str(output_path)])
    normalized = _attachment_checked(output_path.read_bytes())
    duration = _mp4_duration_ms(normalized)
    preview = directory / "cover.jpg"
    _run_ffmpeg(["-protocol_whitelist", "file,pipe", "-i", str(output_path),
                 "-map", "0:v:0", "-frames:v", "1", "-q:v", "3", str(preview)])
    _checked(preview.read_bytes())
    return {"path": output_path, "mime": "video/mp4", "duration_ms": duration, "preview": preview}


def _upload_native_media(auth: Any, path: Path, kind: str, mime: str, preview_key: str = "") -> dict[str, Any]:
    media = _lark_media()
    pan = media._pan()
    file_type = pan.CompleteFileUploadRequest.FileType.Value("AUDIO" if kind == "audio" else "MEDIA")
    size = path.stat().st_size
    if not 0 < size <= MAX_ATTACHMENT_BYTES:
        raise ImageTransferError("原生媒体内容为空或超过 100MB 上限")
    created = pan.CreateFileUploadResponse.FromString(media._post(
        auth, "/upload/create", "创建原生媒体上传", "application/x-protobuf",
        data=pan.CreateFileUploadRequest(fileType=file_type, fileName=path.name, fileSize=size).SerializeToString()))
    key = created.uploadId
    if not re.fullmatch(r"[\w.-]{1,200}", key or ""):
        raise ImageTransferError("原生媒体上传未返回有效资源标识")
    complete = pan.CompleteFileUploadRequest(uploadId=key, fileType=file_type, fileName=path.name, mime=mime)
    if preview_key:
        complete.imageKeys.coverKey = preview_key
    with path.open("rb") as stream:
        part_id = 0
        while chunk := stream.read(NATIVE_UPLOAD_PART_SIZE):
            part_id += 1
            part = pan.UploadFilePartResponse.FromString(media._post(
                auth, "/upload/part", "上传原生媒体分块", "application/octet-stream",
                params={"upload_id": key, "part_id": part_id}, data=chunk))
            crc = zlib.crc32(chunk)
            if part.partId != part_id or part.crc32 != crc:
                raise ImageTransferError("原生媒体上传分块校验失败")
            complete.partToCrc32[part_id] = crc
    result = pan.CompleteFileUploadResponse.FromString(media._post(
        auth, "/upload/complete", "完成原生媒体上传", "application/x-protobuf", data=complete.SerializeToString()))
    if result.code != 0:
        raise ImageTransferError("原生媒体上传完成校验失败")
    return {"key": key, "name": path.name, "size": size, "mime": mime,
            "crypto_token": result.cryptoToken, "preview_key": preview_key}


def _native_message_request(chat_id: str, uploaded: dict[str, Any], kind: str, duration_ms: int):
    gateway = importlib.import_module("larkx.proto.gateway")
    ids = importlib.import_module("larkx.proto.ids")
    request = gateway._lark_all().messages.PutMessageRequest()
    request.chatId, request.cid = str(chat_id), ids.generate_request_cid()
    request.isNotified, request.version = True, 1
    duration_ms = _validated_duration(duration_ms)
    # Both receive decoders divide these fields by 1000 when displaying seconds.
    if kind == "audio":
        request.type = 7
        request.content.audioKey = str(uploaded["key"])
        request.content.audioDuration = duration_ms
    elif kind == "video":
        request.type = 15
        request.content.fileKey = str(uploaded["key"])
        request.content.fileName = str(uploaded["name"])
        request.content.fileSize = int(uploaded["size"])
        request.content.fileMime = str(uploaded["mime"])
        request.content.duration = duration_ms
        if uploaded.get("preview_key"):
            request.content.imageKey = str(uploaded["preview_key"])
    else:
        raise ImageTransferError("原生媒体类型必须是 audio 或 video")
    if kind == "video" and uploaded.get("preview_key"):
        # MEDIA follows the same cover binding as IMAGE; a completed file
        # upload's cryptoToken is not accepted as the video's cover token.
        request.content.cryptoToken = str(uploaded["preview_key"])
    elif uploaded.get("crypto_token"):
        request.content.cryptoToken = str(uploaded["crypto_token"])
    return request


def send_native_media(client: Any, chat_id: str, source: Any, kind: str) -> str:
    """Normalize, upload as AUDIO/MEDIA, and require a server message receipt.

    Platform acceptance still requires a controlled live test. This path never
    falls back to FILE if decoding, native upload, or native sending fails.
    """
    if kind not in {"audio", "video"}:
        raise ImageTransferError("原生媒体类型必须是 audio 或 video")
    if client is None or not str(chat_id).strip() or not callable(getattr(client, "_gateway_post", None)):
        raise ImageTransferError("飞书原生媒体客户端或目标会话不可用")
    media = _lark_media()
    with tempfile.TemporaryDirectory(prefix="mover-native-") as directory:
        normalized = _normalize_native_media(source, kind, Path(directory))
        duration = _validated_duration(normalized["duration_ms"])
        preview_key = ""
        if normalized.get("preview"):
            preview_key = media.upload_image(client.auth, str(normalized["preview"]))
            if not preview_key:
                raise ImageTransferError("视频封面上传未返回资源标识")
        uploaded = _upload_native_media(client.auth, normalized["path"], kind, normalized["mime"], preview_key)
        request = _native_message_request(chat_id, uploaded, kind, duration)
        builders = importlib.import_module("larkx.proto.builders")
        ids = importlib.import_module("larkx.proto.ids")
        packet = builders.wrap_packet(5, request, ids.generate_long_request_id())
        try:
            response = client._gateway_post(packet)
        except Exception as exc:
            raise NativeMediaDeliveryUncertain("原生媒体发送结果未知，请核对目标会话后再决定是否重发") from exc
        try:
            message_id = builders.decode_put_message_response(response)
        except Exception as exc:
            raise NativeMediaDeliveryUncertain("原生媒体发送响应无法确认，请核对目标会话后再决定是否重发") from exc
        if not message_id:
            raise NativeMediaDeliveryUncertain("飞书原生媒体发送未返回回执，未自动重发")
        if str(message_id).startswith("-"):
            raise ImageTransferError("飞书没有返回有效的原生媒体消息回执")
        return str(message_id)


def send_attachment(client: Any, chat_id: str, source: Any, filename: str = "", kind: str = "file") -> str | None:
    """Explicitly send a FILE attachment, even when its bytes contain audio/video.

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
        try:
            message_id = client.send(str(chat_id), file=uploaded)
        except Exception as exc:
            raise NativeMediaDeliveryUncertain("飞书附件发送结果不确定，未自动重发") from exc
    if not message_id:
        raise NativeMediaDeliveryUncertain("飞书附件发送未返回回执，未自动重发")
    return str(message_id)


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
