param([string]$RuntimeDirectory, [string]$PythonPath)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if (-not $RuntimeDirectory) { $RuntimeDirectory = Join-Path $PSScriptRoot 'runtime' }
$runtimeDir = [IO.Path]::GetFullPath($RuntimeDirectory)
$toolsDir = Join-Path $PSScriptRoot 'tools'
$localUv = Join-Path $toolsDir 'uv.exe'
if (Test-Path -LiteralPath $localUv -PathType Leaf) { $uvCommand = $localUv }
else {
    $uvFound = Get-Command uv.exe -ErrorAction SilentlyContinue
    if (-not $uvFound) { throw 'LarkAgentX requires uv.exe. Run the CMD setup entry first.' }
    $uvCommand = $uvFound.Source
}
$commit = 'd288ef4df8a42f6286c5506b7185f64430071490'
$sha256 = 'A3033114963D98E6A1FCD8D0904ADE8F4DEB3ED34C0E2B2637986762C85F1F1B'
$sourceDir = Join-Path $runtimeDir "LarkAgentX-runtime\LarkAgentX-$commit"
$archive = Join-Path $runtimeDir "LarkAgentX-$commit.zip"
$url = "https://codeload.github.com/cv-cat/LarkAgentX/zip/$commit"
$astrbotPython = if ($PythonPath) { [IO.Path]::GetFullPath($PythonPath) } else { Join-Path $runtimeDir '.venv\Scripts\python.exe' }
if (-not (Test-Path -LiteralPath $astrbotPython -PathType Leaf)) { throw "AstrBot Python environment not found: $astrbotPython" }
New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null
function Get-LarkArchiveHash([string]$Path) {
    $hasher = [Security.Cryptography.SHA256]::Create()
    $stream = [IO.File]::OpenRead($Path)
    try { return ([BitConverter]::ToString($hasher.ComputeHash($stream))).Replace('-', '') }
    finally { $stream.Dispose(); $hasher.Dispose() }
}
if (Test-Path -LiteralPath $archive -PathType Leaf) {
    $actual = Get-LarkArchiveHash $archive
    if ($actual -ne $sha256) {
        Move-Item -LiteralPath $archive -Destination "$archive.invalid-$([Guid]::NewGuid().ToString('N'))"
        Write-Output 'Invalid cached archive retained; downloading a fresh pinned source.'
    }
}
if (-not (Test-Path -LiteralPath $archive -PathType Leaf)) {
    $curl = Join-Path $env:SystemRoot 'System32\curl.exe'
    if (-not (Test-Path -LiteralPath $curl -PathType Leaf)) { $curl = (Get-Command curl.exe -ErrorAction Stop).Source }
    $downloadError = $null
    for ($attempt=1; $attempt -le 3; $attempt++) {
        $download = Join-Path $runtimeDir ('.LarkAgentX-download-' + [Guid]::NewGuid().ToString('N') + '.zip')
        try {
            & $curl --fail --location --silent --show-error --retry 2 --connect-timeout 30 --max-time 900 --output $download $url
            if ($LASTEXITCODE -ne 0) { throw "Pinned source download failed (attempt $attempt)." }
            $actual = Get-LarkArchiveHash $download
            if ($actual -ne $sha256) { throw "Pinned source SHA-256 validation failed (attempt $attempt)." }
            Move-Item -LiteralPath $download -Destination $archive
            $downloadError = $null
            break
        } catch { $downloadError = $_; if ($attempt -lt 3) { Start-Sleep -Seconds ($attempt*2) } }
        finally { if (Test-Path -LiteralPath $download -PathType Leaf) { Remove-Item -LiteralPath $download -Force } }
    }
    if ($downloadError) { throw $downloadError }
}
$prepareSourceCode = @"
import hashlib, json, os, shutil, stat, sys, uuid, zipfile
from pathlib import Path, PurePosixPath
archive, destination, expected = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3].lower()
with archive.open("rb") as stream:
    actual = hashlib.file_digest(stream, "sha256").hexdigest()
if actual != expected:
    raise SystemExit("Archive SHA-256 mismatch")
with zipfile.ZipFile(archive) as source:
    entries = source.infolist()
    roots, total = set(), 0
    for entry in entries:
        name = PurePosixPath(entry.filename)
        if name.is_absolute() or not name.parts or any(part in ("..", "") or ":" in part or "\\" in part for part in name.parts):
            raise SystemExit("Unsafe archive member")
        if stat.S_ISLNK(entry.external_attr >> 16):
            raise SystemExit("Archive links are not supported")
        roots.add(name.parts[0])
        total += entry.file_size
    if len(roots) != 1 or total > 128 * 1024 * 1024:
        raise SystemExit("Unexpected archive layout or size")
    top = next(iter(roots))
    files = {PurePosixPath(item.filename).relative_to(top).as_posix(): item for item in entries if not item.is_dir() and len(PurePosixPath(item.filename).parts) > 1}
    if not {"pyproject.toml", "larkx/auth.py", "larkx/client.py"}.issubset(files):
        raise SystemExit("Pinned archive is missing runtime modules")
    source_files = {name: item for name, item in files.items() if name == "pyproject.toml" or name.startswith("larkx/")}
    valid = destination.is_dir() and not destination.is_symlink()
    if valid:
        valid = all((destination / name).is_file() and not (destination / name).is_symlink() and (destination / name).read_bytes() == source.read(item) for name, item in source_files.items())
    if not valid:
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = destination.parent / (".LarkAgentX-extract-" + uuid.uuid4().hex)
        staged, quarantine = staging / top, None
        staging.mkdir()
        try:
            for name, item in files.items():
                target = staged / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read(item))
            if destination.exists():
                quarantine = destination.with_name(destination.name + ".incomplete-" + uuid.uuid4().hex)
                os.replace(destination, quarantine)
            try:
                os.replace(staged, destination)
            except BaseException:
                if quarantine is not None and not destination.exists():
                    os.replace(quarantine, destination)
                raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)
print(json.dumps({"source_ready": True, "archive_sha256": actual, "source_files_checked": len(source_files)}))
"@
# Write script text instead of passing multiline Python through PS5.1's native
# argument quoting, which can strip quotes in paths and Python string literals.
$prepareFile = Join-Path $runtimeDir ('.larkx-prepare-' + [Guid]::NewGuid().ToString('N') + '.py')
try {
    [IO.File]::WriteAllText($prepareFile, $prepareSourceCode, [Text.UTF8Encoding]::new($false))
    & $astrbotPython -I -B $prepareFile $archive $sourceDir $sha256
    if ($LASTEXITCODE -ne 0) { throw 'Pinned LarkAgentX source verification/extraction failed.' }
} finally { if (Test-Path -LiteralPath $prepareFile -PathType Leaf) { Remove-Item -LiteralPath $prepareFile -Force } }

# The pinned source's old web/storage pins conflict with AstrBot 4.29.4's
# FastAPI>=0.124.0 / SQLAlchemy>=2.0.41 requirements. Resolve the full runtime
# together with AstrBot when hosted; keep all other upstream dependency pins.
$dependencyCode = @"
import importlib.metadata, json, re, sys, tomllib
from pathlib import Path
project = tomllib.loads((Path(sys.argv[1]) / "pyproject.toml").read_text(encoding="utf-8"))["project"]
try:
    host_version = importlib.metadata.version("astrbot")
except importlib.metadata.PackageNotFoundError:
    host_version = None
if host_version is not None and host_version != "4.29.4":
    raise SystemExit("Unsupported AstrBot runtime version; expected 4.29.4")
def base_name(requirement):
    return re.split(r"[<>=!~\[ ;]", requirement, maxsplit=1)[0].lower().replace("_", "-")
host_requirements = importlib.metadata.requires("astrbot") if host_version else []
dependencies, overrides = [], {}
for requirement in project["dependencies"]:
    key = base_name(requirement)
    if host_version and key in ("fastapi", "sqlalchemy"):
        matches = [item for item in host_requirements if base_name(item) == key and ";" not in item]
        if len(matches) != 1:
            raise SystemExit("AstrBot hosted compatibility requirement is missing or ambiguous")
        requirement = matches[0]
        overrides[key] = requirement
    dependencies.append(requirement)
dependencies += ["silk-python==0.2.9", "imageio-ffmpeg==0.6.0"]
if host_version:
    dependencies.append("astrbot==4.29.4")
print(json.dumps({"requirements": dependencies, "hosted": bool(host_version), "overrides": overrides}))
"@
$dependencyFile = Join-Path $runtimeDir ('.larkx-dependencies-' + [Guid]::NewGuid().ToString('N') + '.py')
try {
    [IO.File]::WriteAllText($dependencyFile, $dependencyCode, [Text.UTF8Encoding]::new($false))
    $dependencyOutput = & $astrbotPython -I -B $dependencyFile $sourceDir
    if ($LASTEXITCODE -ne 0) { throw 'Unable to resolve the pinned LarkAgentX hosted runtime requirements.' }
    $dependencyPlan = $dependencyOutput | ConvertFrom-Json
} finally { if (Test-Path -LiteralPath $dependencyFile -PathType Leaf) { Remove-Item -LiteralPath $dependencyFile -Force } }
$requirements = @($dependencyPlan.requirements)
$installFailed = $false
for ($attempt=1; $attempt -le 3; $attempt++) {
    & $uvCommand pip install --python $astrbotPython @requirements
    if ($LASTEXITCODE -eq 0) { $installFailed = $false; break }
    $installFailed = $true
    if ($attempt -lt 3) { Start-Sleep -Seconds ($attempt*2) }
}
if ($installFailed) { throw 'LarkAgentX runtime dependency installation failed; rerun after checking network access.' }
$importCheckCode = @"
import importlib, importlib.metadata, io, json, os, re, socket, sys, tomllib
from pathlib import Path
os.environ["LARKX_NO_DOTENV"] = "1"
os.environ["LARKX_AGENT_BACKEND"] = "none"
def no_network(*args, **kwargs):
    raise RuntimeError("Bootstrap check must not use network or login")
socket.create_connection = no_network
socket.socket.connect = no_network
source = Path(sys.argv[1]).resolve()
project = tomllib.loads((source / "pyproject.toml").read_text(encoding="utf-8"))["project"]
try:
    host_version = importlib.metadata.version("astrbot")
except importlib.metadata.PackageNotFoundError:
    host_version = None
host_requirements = importlib.metadata.requires("astrbot") if host_version else []
checked = {}
for requirement in project["dependencies"] + ["silk-python==0.2.9", "imageio-ffmpeg==0.6.0"]:
    match = re.fullmatch(r"([A-Za-z0-9_.-]+)(?:\[[^\]]+\])?==([^; ]+)", requirement)
    if not match:
        raise SystemExit("Pinned runtime dependency declaration is invalid")
    key = match[1].lower().replace("_", "-")
    if host_version and key in ("fastapi", "sqlalchemy"):
        from packaging.requirements import Requirement
        rules = [Requirement(value) for value in host_requirements if Requirement(value).name.lower().replace("_", "-") == key and Requirement(value).marker is None]
        if len(rules) != 1 or importlib.metadata.version(match[1]) not in rules[0].specifier:
            raise SystemExit("Hosted runtime dependency compatibility check failed")
    elif importlib.metadata.version(match[1]) != match[2]:
        raise SystemExit("Pinned runtime dependency verification failed")
    checked[match[1]] = importlib.metadata.version(match[1])
sys.path.insert(0, str(source))
for name in ("larkx", "larkx.auth", "larkx.client", "larkx.storage", "larkx.web.login", "larkx.cli", "loguru", "requests", "websockets", "google.protobuf", "protobuf_to_dict", "sqlalchemy", "qrcode", "PIL.Image", "dotenv", "questionary", "fastapi", "uvicorn", "pysilk", "imageio_ffmpeg"):
    importlib.import_module(name)
import larkx, qrcode
from larkx.auth import LarkAuth, QrLogin
from larkx.client import LarkClient
assert Path(larkx.__file__).resolve().is_relative_to(source), "larkx must load from the verified pinned source"
image = io.BytesIO()
qrcode.make("mover-local-bootstrap-fixture").save(image, format="PNG")
assert image.getvalue().startswith(b"\x89PNG\r\n\x1a\n")
print(json.dumps({"larkx_version": project["version"], "pinned_dependencies_checked": len(checked), "hosted_astrbot": bool(host_version), "host_compatibility_overrides": ["fastapi", "sqlalchemy"] if host_version else [], "isolated_import_ok": True, "local_qr_png_ok": True, "network_or_login_used": False}))
"@
$importFile = Join-Path $runtimeDir ('.larkx-import-' + [Guid]::NewGuid().ToString('N') + '.py')
try {
    [IO.File]::WriteAllText($importFile, $importCheckCode, [Text.UTF8Encoding]::new($false))
    & $astrbotPython -I -B $importFile $sourceDir
    if ($LASTEXITCODE -ne 0) { throw 'LarkAgentX clean import/QR dependency check failed; installation is not complete.' }
} finally { if (Test-Path -LiteralPath $importFile -PathType Leaf) { Remove-Item -LiteralPath $importFile -Force } }
Write-Output "LarkAgentX pinned source and runtime dependencies ready: $sourceDir"
