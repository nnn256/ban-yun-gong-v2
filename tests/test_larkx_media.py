import base64
import importlib
import io
import struct
import sys
import tempfile
import unittest
import wave
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from astrbot_plugin_message_bridge import larkx_media as media


PNG = b"\x89PNG\r\n\x1a\nfixture-image-data"
PROJECT = Path(__file__).resolve().parents[1]
LARKX_SOURCE = PROJECT / "runtime" / "LarkAgentX-runtime" / "LarkAgentX-d288ef4df8a42f6286c5506b7185f64430071490"


class ImageTransportTests(unittest.TestCase):
    def test_upload_then_native_image_send_and_temp_cleanup(self):
        paths = []

        def upload(auth, path):
            paths.append(Path(path))
            self.assertEqual(Path(path).read_bytes(), PNG)
            self.assertEqual(Path(path).suffix, ".png")
            return "fixture-image-key"

        client = SimpleNamespace(auth=object(), send=Mock(return_value="fixture-msg-id"))
        with patch.object(media, "_lark_media", return_value=SimpleNamespace(upload_image=upload)):
            result = media.send_image(client, "fixture-chat", PNG)

        self.assertEqual(result, "fixture-msg-id")
        client.send.assert_called_once_with("fixture-chat", image_key="fixture-image-key")
        self.assertFalse(paths[0].exists())

    def test_qq_image_component_prefers_url_over_nonpath_file_identifier(self):
        component = SimpleNamespace(url="https://fixture.invalid/image", file="opaque-qq-file-id", path="")
        response = Mock()
        response.headers = {"content-length": str(len(PNG))}
        response.iter_content.return_value = iter([PNG[:8], PNG[8:]])
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch.object(media.socket, "getaddrinfo", return_value=[(0, 0, 0, "", ("93.184.216.34", 443))]), patch("requests.get", return_value=response) as get:
            self.assertEqual(media.image_bytes(component), PNG)
        get.assert_called_once_with(component.url, timeout=(10, 45), stream=True, allow_redirects=False)
        self.assertNotIn("cookies", get.call_args.kwargs)

    def test_base64_and_local_file_images_preserve_bytes(self):
        encoded = base64.b64encode(PNG).decode("ascii")
        self.assertEqual(media.image_bytes("base64://" + encoded), PNG)
        self.assertEqual(media.image_bytes("data:image/png;base64," + encoded), PNG)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture image.png"
            path.write_bytes(PNG)
            self.assertEqual(media.image_bytes(path), PNG)
            self.assertEqual(media.image_bytes(path.as_uri()), PNG)

    def test_nonimage_and_oversized_response_are_rejected_before_upload(self):
        with self.assertRaises(media.ImageTransferError):
            media.image_bytes(b"<html>login page</html>")
        response = Mock()
        response.headers = {"content-length": str(media.MAX_IMAGE_BYTES + 1)}
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch.object(media.socket, "getaddrinfo", return_value=[(0, 0, 0, "", ("93.184.216.34", 443))]), patch("requests.get", return_value=response):
            with self.assertRaises(media.ImageTransferError):
                media.image_bytes("https://fixture.invalid/large")
        response.iter_content.assert_not_called()

    def test_feishu_image_download_uses_authenticated_resource_and_byte_limit(self):
        auth = SimpleNamespace(region=SimpleNamespace(file_host="https://fixture.invalid"))
        client = SimpleNamespace(auth=auth)
        resource_url = "https://fixture.invalid/download/messages/msg/keys/key?chat_id=chat"
        lark = SimpleNamespace(
            extract_resource_key=Mock(return_value="fixture-key"),
            message_resource_url=Mock(return_value=resource_url),
            download=Mock(return_value=(PNG, "image/png")),
        )
        message = {"msg_type": "5", "content_data": {"imageV2": {"imageKey": "fixture-key"}}, "msg_id": "msg", "chat_id": "chat"}
        with patch.object(media, "_lark_media", return_value=lark):
            self.assertEqual(media.download_image(client, message), PNG)
        lark.extract_resource_key.assert_called_once_with(5, message["content_data"])
        lark.message_resource_url.assert_called_once_with("msg", "fixture-key", "chat", host=auth.region.file_host)
        lark.download.assert_called_once_with(auth, resource_url, max_bytes=media.MAX_IMAGE_BYTES)

    def test_failed_native_send_does_not_report_delivery(self):
        client = SimpleNamespace(auth=object(), send=Mock(return_value=None))
        lark = SimpleNamespace(upload_image=Mock(return_value="fixture-key"))
        with patch.object(media, "_lark_media", return_value=lark):
            self.assertIsNone(media.send_image(client, "fixture-chat", PNG))


class AttachmentTransportTests(unittest.TestCase):
    def test_qq_silk_upload_is_decoded_to_playable_wav_when_codec_is_available(self):
        silk = b"#!SILK_V3fixture-audio"
        uploaded = {}

        class FakeSilk:
            @staticmethod
            def decode(_source, output, sample_rate):
                self.assertEqual(sample_rate, 24000)
                output.write(b"\x00\x00" * 240)

        def upload(_auth, path):
            uploaded["name"] = Path(path).name
            uploaded["bytes"] = Path(path).read_bytes()
            return {"key": "fixture-voice-key", "name": Path(path).name}

        client = SimpleNamespace(auth=object(), send=Mock(return_value="voice-message-id"))
        with patch.dict("sys.modules", {"pysilk": FakeSilk}), patch.object(media, "_lark_media", return_value=SimpleNamespace(upload_file=upload)):
            self.assertEqual(media.send_attachment(client, "chat", silk, filename="voice.silk", kind="audio"), "voice-message-id")
        self.assertEqual(uploaded["name"], "voice.wav")
        self.assertTrue(uploaded["bytes"].startswith(b"RIFF"))

    def test_send_video_uploads_real_file_and_sends_file_type(self):
        payload = b"\x00\x00\x00\x18ftypisomfixture-video"
        paths = []

        def upload(auth, path):
            paths.append(Path(path))
            self.assertEqual(Path(path).name, "video.mp4")
            self.assertEqual(Path(path).read_bytes(), payload)
            return {"key": "fixture-file-key", "name": "video.mp4", "size": len(payload), "mime": "video/mp4"}

        client = SimpleNamespace(auth=object(), send=Mock(return_value="fixture-msg-id"))
        with patch.object(media, "_lark_media", return_value=SimpleNamespace(upload_file=upload)):
            self.assertEqual(media.send_attachment(client, "fixture-chat", payload, kind="video"), "fixture-msg-id")
        self.assertEqual(client.send.call_args.kwargs["file"]["name"], "video.mp4")
        self.assertNotIn("text", client.send.call_args.kwargs)
        self.assertFalse(paths[0].exists())

    def test_download_voice_preserves_bytes_and_identifies_ogg_container(self):
        payload = b"OggSfixture-voice"
        auth = SimpleNamespace(region=SimpleNamespace(file_host="https://fixture.invalid"))
        lark = SimpleNamespace(extract_resource_key=Mock(return_value="fixture-key"),
                               message_resource_url=Mock(return_value="https://fixture.invalid/file"),
                               download=Mock(return_value=(payload, "application/octet-stream")))
        message = {"msg_type": "7", "msg_id": "message", "chat_id": "chat", "content_data": {"key": "fixture-key"}}
        with patch.object(media, "_lark_media", return_value=lark):
            self.assertEqual(media.download_attachment(SimpleNamespace(auth=auth), message), (payload, "audio.ogg", "audio"))
        lark.extract_resource_key.assert_called_once_with(7, message["content_data"])
        lark.download.assert_called_once_with(auth, "https://fixture.invalid/file", max_bytes=media.MAX_ATTACHMENT_BYTES)

    def test_filename_cannot_escape_directory_or_include_windows_reserved_characters(self):
        self.assertEqual(media.attachment_filename("../../a:test?.mp4", b"file", "video"), "a_test_.mp4")
        self.assertEqual(media.attachment_filename("C:\\dir\\record", b"#!SILK_V3fixture", "audio"), "record.silk")
        self.assertEqual(media.attachment_filename("", b"opaque", "video"), "video.bin")
        self.assertEqual(media.attachment_filename("CON.mp4", b"opaque", "video"), "_CON.mp4")

    def test_file_component_url_beats_opaque_record_file_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "voice.amr"
            path.write_bytes(b"#!AMR\nfixture")
            component = SimpleNamespace(file="opaque-id.amr", url=path.as_uri(), path="")
            self.assertEqual(media.attachment_bytes(component), b"#!AMR\nfixture")

    def test_attachment_http_declared_size_is_enforced_before_body(self):
        response = Mock()
        response.headers = {"content-length": str(media.MAX_ATTACHMENT_BYTES + 1)}
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch.object(media.socket, "getaddrinfo", return_value=[(0, 0, 0, "", ("93.184.216.34", 443))]), patch("requests.get", return_value=response) as get:
            with self.assertRaises(media.ImageTransferError):
                media.attachment_bytes("https://fixture.invalid/file")
        response.iter_content.assert_not_called()
        self.assertNotIn("cookies", get.call_args.kwargs)

    def test_remote_media_rejects_private_addresses_before_request(self):
        with patch.object(media.socket, "getaddrinfo", return_value=[(0, 0, 0, "", ("192.168.1.9", 443))]), patch("requests.get") as get:
            with self.assertRaisesRegex(media.ImageTransferError, "非公网 IP"):
                media.attachment_bytes("https://fixture.invalid/file")
        get.assert_not_called()

    def test_remote_media_revalidates_redirect_destination(self):
        response = Mock()
        response.status_code = 302
        response.headers = {"location": "http://127.0.0.1/private"}
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch.object(media.socket, "getaddrinfo", return_value=[(0, 0, 0, "", ("93.184.216.34", 443))]), patch("requests.get", return_value=response) as get:
            with self.assertRaisesRegex(media.ImageTransferError, "不能指向本机"):
                media.attachment_bytes("https://fixture.invalid/redirect")
        get.assert_called_once_with("https://fixture.invalid/redirect", timeout=(10, 90), stream=True, allow_redirects=False)

    def test_missing_media_key_does_not_send_resource_summary_or_download(self):
        lark = SimpleNamespace(extract_resource_key=Mock(return_value=""), download=Mock())
        with patch.object(media, "_lark_media", return_value=lark):
            with self.assertRaises(media.ImageTransferError):
                media.download_attachment(object(), {"msg_type": 15, "msg_id": "m", "chat_id": "c", "content_data": {}})
        lark.download.assert_not_called()


@unittest.skipUnless((LARKX_SOURCE / "larkx" / "proto" / "gateway.py").is_file(), "Pinned LarkAgentX source is not installed")
class NativeMediaTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inserted_source = str(LARKX_SOURCE) not in sys.path
        if cls.inserted_source:
            sys.path.insert(0, str(LARKX_SOURCE))
        cls.gateway = importlib.import_module("larkx.proto.gateway")
        cls.builders = importlib.import_module("larkx.proto.builders")
        cls.types = cls.gateway._lark_all()

    @classmethod
    def tearDownClass(cls):
        if cls.inserted_source:
            sys.path.remove(str(LARKX_SOURCE))

    def _upload_transport(self, expected_kind, *, corrupt_crc=False):
        pan = self.types.pan
        requests = []

        def post(auth, path, what, content_type="", **kwargs):
            self.assertEqual(auth, "fixture-auth")
            if path == "/upload/create":
                request = pan.CreateFileUploadRequest.FromString(kwargs["data"])
                self.assertEqual(request.fileType, 4 if expected_kind == "audio" else 5)
                requests.append((path, request))
                return pan.CreateFileUploadResponse(uploadId="fixture-native-key").SerializeToString()
            if path == "/upload/part":
                self.assertEqual(kwargs["params"]["upload_id"], "fixture-native-key")
                requests.append((path, kwargs["data"]))
                crc = zlib.crc32(kwargs["data"])
                return pan.UploadFilePartResponse(partId=kwargs["params"]["part_id"], crc32=crc + 1 if corrupt_crc else crc).SerializeToString()
            self.assertEqual(path, "/upload/complete")
            request = pan.CompleteFileUploadRequest.FromString(kwargs["data"])
            self.assertEqual(request.fileType, 4 if expected_kind == "audio" else 5)
            requests.append((path, request))
            return pan.CompleteFileUploadResponse(code=0, cryptoToken="fixture-crypto-value").SerializeToString()

        return SimpleNamespace(_pan=lambda: pan, _post=post), requests

    def test_native_uploader_uses_audio_media_types_and_checks_every_chunk_crc(self):
        for kind, mime in (("audio", "audio/ogg"), ("video", "video/mp4")):
            transport, calls = self._upload_transport(kind)
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / ("audio.ogg" if kind == "audio" else "video.mp4")
                payload = b"first-chunk-next-chunk"
                path.write_bytes(payload)
                with patch.object(media, "_lark_media", return_value=transport), patch.object(media, "NATIVE_UPLOAD_PART_SIZE", 8):
                    result = media._upload_native_media("fixture-auth", path, kind, mime, "fixture-cover" if kind == "video" else "")
                self.assertEqual(result["key"], "fixture-native-key")
                self.assertEqual(result["size"], len(payload))
                self.assertEqual(result["mime"], mime)
                self.assertEqual(result["preview_key"], "fixture-cover" if kind == "video" else "")
                self.assertEqual(b"".join(data for route, data in calls if route == "/upload/part"), payload)
                complete = calls[-1][1]
                self.assertEqual(len(complete.partToCrc32), 3)
                self.assertEqual(complete.imageKeys.coverKey, "fixture-cover" if kind == "video" else "")

    def test_native_uploader_stops_before_complete_when_part_crc_is_wrong(self):
        transport, calls = self._upload_transport("audio", corrupt_crc=True)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audio.ogg"
            path.write_bytes(b"content")
            with patch.object(media, "_lark_media", return_value=transport):
                with self.assertRaisesRegex(media.ImageTransferError, "分块校验"):
                    media._upload_native_media("fixture-auth", path, "audio", "audio/ogg")
        self.assertEqual([route for route, _ in calls], ["/upload/create", "/upload/part"])

    def test_actual_serialized_requests_are_audio_or_media_not_file(self):
        uploaded = {"key": "fixture-native-key", "name": "clip.mp4", "size": 1234, "mime": "video/mp4", "crypto_token": "fixture-crypto-value", "preview_key": "fixture-cover"}
        for kind, expected_type in (("audio", 7), ("video", 15)):
            request = media._native_message_request("fixture-chat", uploaded, kind, 1250)
            packet = self.builders.wrap_packet(5, request, "fixture-request")
            parsed = self.types.messages.PutMessageRequest.FromString(packet.payload)
            self.assertEqual(packet.cmd, 5)
            self.assertEqual(parsed.chatId, "fixture-chat")
            self.assertEqual(parsed.type, expected_type)
            if kind == "audio":
                self.assertEqual(parsed.content.cryptoToken, "fixture-crypto-value")
                self.assertEqual(parsed.content.audioKey, "fixture-native-key")
                self.assertEqual(parsed.content.audioDuration, 1250)
                self.assertFalse(parsed.content.HasField("fileKey"))
            else:
                self.assertEqual(parsed.content.fileKey, "fixture-native-key")
                self.assertEqual(parsed.content.fileName, "clip.mp4")
                self.assertEqual(parsed.content.fileSize, 1234)
                self.assertEqual(parsed.content.fileMime, "video/mp4")
                self.assertEqual(parsed.content.duration, 1250)
                self.assertEqual(parsed.content.imageKey, "fixture-cover")
                self.assertEqual(parsed.content.cryptoToken, "fixture-cover")
                self.assertNotEqual(parsed.content.cryptoToken, uploaded["crypto_token"])

    def _normalize_stub(self, source, kind, folder):
        path = folder / ("audio.ogg" if kind == "audio" else "video.mp4")
        path.write_bytes(b"normalized-media")
        return {"path": path, "mime": "audio/ogg" if kind == "audio" else "video/mp4", "duration_ms": 1000, "preview": None}

    def test_native_send_requires_server_receipt_and_never_uses_generic_file_send(self):
        sent = []

        def gateway_send(packet):
            sent.append(self.types.messages.PutMessageRequest.FromString(packet.payload))
            response = self.builders.P.PutMessageResponse()
            response.message.id = "123456789"
            return self.builders.wrap_packet(5, response, "fixture-response").SerializeToString()

        client = SimpleNamespace(auth="fixture-auth", _gateway_post=gateway_send, send=Mock())
        uploaded = {"key": "fixture-native-key", "name": "audio.ogg", "size": 16, "mime": "audio/ogg"}
        with patch.object(media, "_normalize_native_media", side_effect=self._normalize_stub), patch.object(media, "_upload_native_media", return_value=uploaded):
            self.assertEqual(media.send_native_media(client, "fixture-chat", b"source", "audio"), "123456789")
        self.assertEqual(sent[0].type, 7)
        client.send.assert_not_called()

        client._gateway_post = Mock(side_effect=OSError("fixture connection dropped"))
        with patch.object(media, "_normalize_native_media", side_effect=self._normalize_stub), patch.object(media, "_upload_native_media", return_value=uploaded):
            with self.assertRaises(media.NativeMediaDeliveryUncertain):
                media.send_native_media(client, "fixture-chat", b"source", "audio")
        client.send.assert_not_called()

    def test_invalid_duration_stops_before_upload_and_cannot_be_reported_as_native_send(self):
        client = SimpleNamespace(auth="fixture-auth", _gateway_post=Mock())
        def invalid(source, kind, folder):
            result = self._normalize_stub(source, kind, folder)
            result["duration_ms"] = 0
            return result
        with patch.object(media, "_normalize_native_media", side_effect=invalid), patch.object(media, "_upload_native_media") as upload:
            with self.assertRaisesRegex(media.ImageTransferError, "时长无效"):
                media.send_native_media(client, "fixture-chat", b"source", "audio")
        upload.assert_not_called()
        client._gateway_post.assert_not_called()

    def test_native_duration_readers_reject_non_media_and_truncated_containers(self):
        for payload in (b"", b"not-an-opus-stream", b"OggS" + b"\0" * 12):
            with self.assertRaises(media.ImageTransferError):
                media._opus_duration_ms(payload)
        for payload in (b"", b"not-an-mp4", struct.pack(">I4s", 64, b"moov")):
            with self.assertRaises(media.ImageTransferError):
                media._mp4_duration_ms(payload)

    def test_real_local_ffmpeg_normalizes_wav_to_opus_and_reads_measured_milliseconds(self):
        output = io.BytesIO()
        with wave.open(output, "wb") as writer:
            writer.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
            writer.writeframes(b"\x00\x00" * 2000)
        with tempfile.TemporaryDirectory() as directory:
            result = media._normalize_native_media(output.getvalue(), "audio", Path(directory))
            self.assertEqual(result["mime"], "audio/ogg")
            self.assertEqual(result["duration_ms"], 250)
            self.assertIn(b"OpusHead", result["path"].read_bytes())
            self.assertIsNone(result["preview"])

    def test_actual_short_video_is_h264_mp4_with_cover_and_measured_duration(self):
        fixture = PROJECT / "artifacts" / "media-forwarding-controls-20261116" / "local-codec-fixtures-audit" / "solid-with-audio.mp4"
        if not fixture.is_file():
            self.skipTest("Controlled local video fixture is not installed")
        with tempfile.TemporaryDirectory() as directory:
            result = media._normalize_native_media(fixture, "video", Path(directory))
            self.assertEqual(result["mime"], "video/mp4")
            self.assertTrue(1000 <= result["duration_ms"] <= 1100)
            self.assertEqual(media.image_extension(result["preview"].read_bytes()), ".jpg")
            self.assertIn(b"avc1", result["path"].read_bytes())


if __name__ == "__main__":
    unittest.main()
