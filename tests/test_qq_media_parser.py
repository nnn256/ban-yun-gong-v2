"""Exercise the installed AstrBot parser outside the plugin's test stubs."""
import subprocess
import unittest
from pathlib import Path


class InstalledQQMediaParserTests(unittest.TestCase):
    def test_local_audio_video_and_attachment_keep_native_onebot_segments(self):
        project = Path(__file__).resolve().parents[1]
        python = project / "runtime" / ".venv" / "Scripts" / "python.exe"
        if not python.is_file():
            self.skipTest("Local AstrBot runtime is not installed")
        script = r'''
import asyncio
import base64
import tempfile
import wave
from pathlib import Path

from astrbot.api.event import MessageChain
from astrbot.api.message_components import File, Record, Video
from astrbot_plugin_message_bridge.qq_transport import _onebot_segments

async def main():
    with tempfile.TemporaryDirectory(prefix="mover-media-fixture-") as directory:
        folder = Path(directory)
        wav = folder / "voice fixture.wav"
        with wave.open(str(wav), "wb") as stream:
            stream.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
            stream.writeframes(b"\x00\x00" * 800)
        # Only parser serialization is under test; QQ codec decoding is external.
        video = folder / "video fixture.mp4"
        video.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 16)
        document = folder / "document fixture.txt"
        document.write_text("fixture attachment", encoding="utf-8")
        chain = MessageChain()
        chain.chain.extend([
            Record.fromFileSystem(str(wav)),
            Video.fromFileSystem(str(video)),
            File(name=document.name, file=str(document)),
        ])
        segments = await _onebot_segments(chain)
        assert [segment["type"] for segment in segments] == ["record", "video", "file"]
        audio = segments[0]["data"]["file"]
        assert audio.startswith("base64://")
        assert base64.b64decode(audio[9:]) == wav.read_bytes()
        assert segments[1]["data"]["file"] == video.as_uri()
        assert segments[2]["data"]["file"] == document.as_uri()
        assert segments[2]["data"]["name"] == document.name
        print("native record/video/file parser smoke passed")

asyncio.run(main())
'''
        result = subprocess.run(
            [str(python), "-B", "-c", script], cwd=project,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("native record/video/file parser smoke passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
