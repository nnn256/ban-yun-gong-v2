import base64
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from astrbot_plugin_message_bridge import larkx_media as media


PNG = b"\x89PNG\r\n\x1a\nfixture-image-data"


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
        with patch("requests.get", return_value=response) as get:
            self.assertEqual(media.image_bytes(component), PNG)
        get.assert_called_once_with(component.url, timeout=(10, 45), stream=True)
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
        with patch("requests.get", return_value=response):
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
        with patch("requests.get", return_value=response) as get:
            with self.assertRaises(media.ImageTransferError):
                media.attachment_bytes("https://fixture.invalid/file")
        response.iter_content.assert_not_called()
        self.assertNotIn("cookies", get.call_args.kwargs)

    def test_missing_media_key_does_not_send_resource_summary_or_download(self):
        lark = SimpleNamespace(extract_resource_key=Mock(return_value=""), download=Mock())
        with patch.object(media, "_lark_media", return_value=lark):
            with self.assertRaises(media.ImageTransferError):
                media.download_attachment(object(), {"msg_type": 15, "msg_id": "m", "chat_id": "c", "content_data": {}})
        lark.download.assert_not_called()


if __name__ == "__main__":
    unittest.main()
