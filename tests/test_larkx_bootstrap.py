"""Dependency-complete pinned bootstrap, archive recovery, and clean-venv checks."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "install_larkagentx.ps1"
COMMIT = "d288ef4df8a42f6286c5506b7185f64430071490"
ARCHIVE_SHA = "a3033114963d98e6a1fcd8d0904ade8f4deb3ed34c0e2b2637986762c85f1f1b"


def embedded_python(name):
    text = SCRIPT.read_text(encoding="utf-8-sig")
    match = re.search(r"\$" + re.escape(name) + r'\s*=\s*@"\r?\n(.*?)\r?\n"@', text, re.S)
    if not match:
        raise AssertionError("Embedded Python block not found: " + name)
    return match[1]


def archive_fixture(path, *, malicious=False):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Pinned/pyproject.toml", '[project]\nname="larkx"\nversion="2.1.0"\n')
        archive.writestr("Pinned/larkx/auth.py", "# fixture auth\n")
        archive.writestr("Pinned/larkx/client.py", "# fixture client\n")
        archive.writestr("Pinned/larkx/__init__.py", "# fixture init\n")
        if malicious:
            archive.writestr("Pinned/../../escape.txt", "not allowed")
    return hashlib.sha256(path.read_bytes()).hexdigest()


class LarkXBootstrapTests(unittest.TestCase):
    def run_prepare(self, archive, destination, sha):
        return subprocess.run(
            [sys.executable, "-I", "-B", "-c", embedded_python("prepareSourceCode"), str(archive), str(destination), sha],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
        )

    def test_installer_is_bom_encoded_noninteractive_and_installs_project_dependencies(self):
        self.assertTrue(SCRIPT.read_bytes().startswith(b"\xef\xbb\xbf"))
        text = SCRIPT.read_text(encoding="utf-8-sig")
        self.assertIn("pip install --python $astrbotPython @requirements", text)
        self.assertIn('dependencies.append("astrbot==4.29.4")', embedded_python("dependencyCode"))
        self.assertNotIn("Mandatory", text)
        self.assertNotIn("$env:PYTHONPATH", text)
        self.assertIn("for ($attempt=1; $attempt -le 3; $attempt++)", text)
        self.assertIn(".LarkAgentX-download-", text)
        self.assertIn(".LarkAgentX-extract-", text)
        self.assertIn(".invalid-", text)
        self.assertIn(".incomplete-", text)
        self.assertIn("-I -B $importFile", text)
        self.assertIn("pinned_dependencies_checked", embedded_python("importCheckCode"))

    @unittest.skipUnless(os.name == "nt", "Windows PowerShell 5.1 validation")
    def test_powershell_51_parser_accepts_bootstrap(self):
        powershell = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        code = "$e=$null;$t=$null;[Management.Automation.Language.Parser]::ParseFile($args[0],[ref]$t,[ref]$e)|Out-Null;if($e.Count){$e|ForEach-Object{$_.Message};exit 1}"
        # Use a temporary parser wrapper so PS5.1 does not reinterpret native -Command args.
        with tempfile.TemporaryDirectory(prefix="larkx-ps-parser-") as temp:
            wrapper = Path(temp) / "parse.ps1"
            wrapper.write_text(code, encoding="utf-8-sig")
            result = subprocess.run([str(powershell), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(wrapper), str(SCRIPT)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_source_extract_is_idempotent_and_recovers_partial_extract_without_fixed_directory(self):
        with tempfile.TemporaryDirectory(prefix="larkx-source-test-") as temp:
            root = Path(temp)
            archive = root / "source.zip"
            sha = archive_fixture(archive)
            destination = root / "runtime" / "Pinned"
            destination.mkdir(parents=True)
            (destination / "pyproject.toml").write_text("partial", encoding="utf-8")
            old_fixed = root / "LarkAgentX-extract-pinned-commit"
            old_fixed.mkdir()
            (old_fixed / "old-partial.txt").write_text("leave unchanged", encoding="utf-8")
            result = self.run_prepare(archive, destination, sha)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue((destination / "larkx/auth.py").is_file())
            self.assertEqual(len(list(destination.parent.glob("Pinned.incomplete-*"))), 1)
            self.assertEqual((old_fixed / "old-partial.txt").read_text(), "leave unchanged")
            before = (destination / "larkx/auth.py").stat().st_mtime_ns
            result = self.run_prepare(archive, destination, sha)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual((destination / "larkx/auth.py").stat().st_mtime_ns, before)
            self.assertFalse(list(destination.parent.glob(".LarkAgentX-extract-*")))

    def test_archive_digest_mismatch_never_replaces_existing_source(self):
        with tempfile.TemporaryDirectory(prefix="larkx-hash-test-") as temp:
            root = Path(temp)
            archive = root / "source.zip"
            archive_fixture(archive)
            destination = root / "existing"
            destination.mkdir()
            sentinel = destination / "sentinel.txt"
            sentinel.write_text("keep", encoding="utf-8")
            result = self.run_prepare(archive, destination, "0" * 64)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("SHA-256 mismatch", result.stderr)
            self.assertEqual(sentinel.read_text(), "keep")
            self.assertFalse((destination / "larkx").exists())

    def test_unsafe_archive_member_never_leaves_staging_directory(self):
        with tempfile.TemporaryDirectory(prefix="larkx-traversal-test-") as temp:
            root = Path(temp)
            archive = root / "source.zip"
            sha = archive_fixture(archive, malicious=True)
            result = self.run_prepare(archive, root / "destination", sha)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Unsafe archive member", result.stderr)
            self.assertFalse((root / "destination").exists())
            self.assertFalse((root / "escape.txt").exists())

    def test_hosted_plan_uses_actual_astrbot_requirements_and_preserves_other_pins(self):
        source = ROOT / "runtime" / "LarkAgentX-runtime" / ("LarkAgentX-" + COMMIT)
        python = Path(os.environ.get("MOVER_VERIFY_PYTHON") or ROOT / "runtime" / ".venv" / "Scripts" / "python.exe")
        if not source.is_dir() or not python.is_file():
            self.skipTest("Installed AstrBot runtime not available")
        result = subprocess.run([str(python), "-I", "-B", "-c", embedded_python("dependencyCode"), str(source)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        plan = json.loads(result.stdout)
        if not plan["hosted"]:
            self.skipTest("Selected runtime does not have AstrBot metadata")
        self.assertIn("astrbot==4.29.4", plan["requirements"])
        self.assertEqual(set(plan["overrides"]), {"fastapi", "sqlalchemy"})
        self.assertIn("fastapi>=0.124.0", plan["requirements"])
        self.assertIn("sqlalchemy[asyncio]>=2.0.41", plan["requirements"])
        self.assertIn("requests==2.31.0", plan["requirements"])
        self.assertIn("qrcode[pil]==8.2", plan["requirements"])
        self.assertNotIn("fastapi==0.115.12", plan["requirements"])
        self.assertNotIn("SQLAlchemy==2.0.30", plan["requirements"])

    @unittest.skipUnless(os.environ.get("MOVER_RUN_CLEAN_LARKX_BOOTSTRAP") == "1", "opt-in clean environment dependency installation")
    def test_real_clean_venv_full_bootstrap_and_isolated_qr_import(self):
        uv = ROOT / "tools/uv.exe"
        uv = str(uv) if uv.is_file() else shutil.which("uv")
        if not uv:
            self.skipTest("uv.exe not installed")
        cached = ROOT / "runtime" / ("LarkAgentX-" + COMMIT + ".zip")
        if not cached.is_file():
            self.skipTest("Pinned source archive not cached")
        self.assertEqual(hashlib.sha256(cached.read_bytes()).hexdigest(), ARCHIVE_SHA)
        powershell = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        with tempfile.TemporaryDirectory(prefix="larkx-clean-bootstrap-") as temp:
            root = Path(temp)
            runtime = root / "fresh-runtime"
            runtime.mkdir()
            shutil.copyfile(cached, runtime / cached.name)
            venv = runtime / ".venv"
            create = subprocess.run([uv, "venv", "--python", "3.12", str(venv)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
            self.assertEqual(create.returncode, 0, create.stdout + create.stderr)
            python = venv / "Scripts/python.exe"
            before = subprocess.run([str(python), "-I", "-B", "-c", "from larkx.auth import QrLogin"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
            self.assertNotEqual(before.returncode, 0)
            self.assertIn("ModuleNotFoundError", before.stderr)
            env = {**os.environ, "PYTHONPATH": str(root / "poisoned-path"), "LARKX_HOME": str(root / "no-login-data"), "LARKX_NO_DOTENV": "1", "LARKX_AGENT_BACKEND": "none"}
            result = subprocess.run([str(powershell), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT), "-RuntimeDirectory", str(runtime), "-PythonPath", str(python)], cwd=root, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            summary = next(json.loads(line) for line in result.stdout.splitlines() if line.startswith('{"larkx_version"'))
            self.assertTrue(summary["isolated_import_ok"])
            self.assertTrue(summary["local_qr_png_ok"])
            self.assertEqual(summary["pinned_dependencies_checked"], 13)
            self.assertFalse(summary["network_or_login_used"])
            self.assertFalse(list(root.rglob("credentials*.json")))
            second = subprocess.run([str(powershell), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT), "-RuntimeDirectory", str(runtime), "-PythonPath", str(python)], cwd=root, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertIn('"isolated_import_ok": true', second.stdout)


if __name__ == "__main__":
    unittest.main()
