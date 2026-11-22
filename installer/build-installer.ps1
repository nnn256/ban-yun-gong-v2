param(
    [string]$OutputPath,
    [string]$PortablePath,
    [switch]$PortableOnly
)

$ErrorActionPreference = 'Stop'
$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$artifactRoot = Join-Path $projectRoot 'artifacts'
$buildRoot = Join-Path $artifactRoot ('.installer-build-' + [guid]::NewGuid().ToString('N'))
$cacheRoot = Join-Path $artifactRoot '.installer-cache'
$payloadRoot = Join-Path $buildRoot 'payload'
$metadata = Get-Content -LiteralPath (Join-Path $projectRoot 'astrbot_plugin_message_bridge/metadata.yaml') -Raw -Encoding UTF8
$versionMatch = [regex]::Match($metadata, '(?m)^version:\s*v(\d+\.\d+\.\d+)\s*$')
if (-not $versionMatch.Success) { throw 'Bridge metadata must specify a three-part release version.' }
$releaseVersion = $versionMatch.Groups[1].Value
if (-not $OutputPath) { $OutputPath = Join-Path $artifactRoot "搬运工V2-Setup-$releaseVersion-x64.exe" }
if ($PortableOnly -and -not $PortablePath) { $PortablePath = Join-Path $artifactRoot "搬运工V2-$releaseVersion.zip" }
if (-not $PortableOnly) {
    if ($PSVersionTable.PSVersion.Major -lt 7) { throw 'Use PowerShell 7 to build the WiX installer.' }
    $wixExe = (Get-Command wix.exe -ErrorAction Stop).Source
}
$uvVersion = '0.13.8'
$uvExecutableHash = '03FA174894B9D0CBE08762D40D1C2C64D10E875BD56F1A1E334EE1E00D60CDBE'

& (Join-Path $projectRoot 'verify.ps1')
if ($LASTEXITCODE -ne 0) { throw 'Project verification failed; installer was not built.' }
Write-Output '[1/5] Project tests passed.'
$portableLauncher = Join-Path $buildRoot 'launcher/搬运工V2.exe'
$portableUv = Join-Path $buildRoot 'launcher/tools/uv.exe'
& (Join-Path $projectRoot 'build-portable.ps1') -OutputExe $portableLauncher -UvOutput $portableUv
if ($LASTEXITCODE -ne 0) { throw 'Portable launcher build failed; installer was not built.' }
New-Item -ItemType Directory -Force -Path $payloadRoot,$artifactRoot,$cacheRoot | Out-Null
$rootFiles = @(
    'README.md', 'VERIFICATION.txt', 'PROTOCOL_REVIEW.md', 'launch-workbench.cmd', '一键启动.cmd',
    '后台运行.vbs', '打开工作台.vbs',
    'updater.py', 'update-and-start.ps1', '更新并启动.cmd', 'log_store.py',
    'install.ps1', 'install_astrbot.ps1', 'install_larkagentx.ps1',
    'install_snowluma.ps1', 'start_snowluma.ps1', 'start-workbench.ps1',
    'stop-workbench.ps1', 'background_runtime.py', 'background_supervisor.py', 'astrbot_runner.py',
    'workbench_server.py', 'workbench.settings.example.json',
    'astrbot_plugin_message_bridge/__init__.py', 'astrbot_plugin_message_bridge/bridge_core.py',
    'astrbot_plugin_message_bridge/bridge_rules.example.json', 'astrbot_plugin_message_bridge/build_identity.py',
    'astrbot_plugin_message_bridge/event_log.py', 'astrbot_plugin_message_bridge/larkx_bridge.py',
    'astrbot_plugin_message_bridge/larkx_history.py', 'astrbot_plugin_message_bridge/larkx_inbox.py',
    'astrbot_plugin_message_bridge/larkx_media.py', 'astrbot_plugin_message_bridge/larkx_merge.py',
    'astrbot_plugin_message_bridge/larkx_receive.py', 'astrbot_plugin_message_bridge/larkx_richtext.py',
    'astrbot_plugin_message_bridge/main.py', 'astrbot_plugin_message_bridge/metadata.yaml',
    'astrbot_plugin_message_bridge/qq_emojis.py', 'astrbot_plugin_message_bridge/qq_transport.py',
    'astrbot_plugin_message_bridge/runtime_status.py', 'astrbot_plugin_message_bridge/snowluma_watchdog.py',
    'workbench/app.js', 'workbench/index.html', 'workbench/style.css',
    'vendor/snowluma-go-1.0.9.zip', 'vendor/snowluma-go.json'
)
foreach ($file in $rootFiles) {
    $target = Join-Path $payloadRoot $file
    New-Item -ItemType Directory -Force -Path (Split-Path $target -Parent) | Out-Null
    Copy-Item -LiteralPath (Join-Path $projectRoot $file) -Destination $target
}
Copy-Item -LiteralPath $portableLauncher -Destination (Join-Path $payloadRoot 'mover-v2-launcher.exe')

$forbiddenNames = @('credentials.json', 'bridge_rules.json', 'cmd_config.json', 'onebot.json',
                    'workbench.settings.json', '.workbench-bootstrap.dpapi', '.mover-v2-astrbot.pid')
$forbiddenPayload = Get-ChildItem -LiteralPath $payloadRoot -Recurse -File | Where-Object {
    $_.Name -in $forbiddenNames -or $_.FullName.Substring($payloadRoot.Length + 1) -match '(^|\\)runtime(\\|$)'
}
if ($forbiddenPayload) {
    throw "Installer payload contains local data or credentials: $($forbiddenPayload.Name -join ', ')"
}
if (-not (Test-Path -LiteralPath (Join-Path $payloadRoot 'mover-v2-launcher.exe') -PathType Leaf)) {
    throw 'Installer payload is missing its native launcher executable.'
}
$ruleTemplate = Get-Content -LiteralPath (Join-Path $payloadRoot 'astrbot_plugin_message_bridge/bridge_rules.example.json') -Raw | ConvertFrom-Json
if (@($ruleTemplate.rules).Count -ne 0) {
    throw 'Installer template must contain zero user/group routing rules.'
}
Write-Output '[2/5] Application source staged; local QQ/Feishu login data and mappings are excluded.'

# build-portable.ps1 has already verified the pinned uv executable. Reuse
# that exact output rather than downloading or trusting another archive cache.
$actualUvHash = (Get-FileHash -LiteralPath $portableUv -Algorithm SHA256).Hash
if ($actualUvHash -ne $uvExecutableHash) { throw "uv executable SHA-256 mismatch: $actualUvHash" }
$tools = Join-Path $payloadRoot 'tools'
New-Item -ItemType Directory -Force -Path $tools | Out-Null
$bundledUv = Join-Path $tools 'uv.exe'
Copy-Item -LiteralPath $portableUv -Destination $bundledUv
$uvVersionOutput = (& $bundledUv --version | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $uvVersionOutput -notmatch "^uv $([regex]::Escape($uvVersion)) ") {
    throw "Bundled uv version check failed: $uvVersionOutput"
}
Write-Output '[3/5] Verified pinned uv executable staged from portable build.'
foreach ($license in @('LICENSE-APACHE', 'LICENSE-MIT')) {
    $notice = Join-Path (Split-Path $portableUv -Parent) "$license.txt"
    if (-not (Test-Path -LiteralPath $notice -PathType Leaf)) { throw "Missing uv $license notice." }
    Copy-Item -LiteralPath $notice -Destination (Join-Path $tools "$license.txt")
}
Write-Output '[4/5] uv license notices staged from portable build.'

$manifestPython = $env:MOVER_VERIFY_PYTHON
if (-not $manifestPython) { $manifestPython = Join-Path $projectRoot 'runtime\.venv\Scripts\python.exe' }
$manifestPythonArgs = @()
if (-not (Test-Path -LiteralPath $manifestPython -PathType Leaf)) {
    $pythonLauncher = Get-Command py -ErrorAction SilentlyContinue
    if (-not $pythonLauncher) { throw 'Installer build requires the project Python runtime or py.exe.' }
    $manifestPython = $pythonLauncher.Source
    $manifestPythonArgs = @('-3')
}
& $manifestPython @manifestPythonArgs -B (Join-Path $projectRoot 'updater.py') manifest $payloadRoot --version $releaseVersion
if ($LASTEXITCODE -ne 0) { throw 'Could not seal the installer application payload.' }

function Build-PortableZip {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zipStage = Join-Path $buildRoot 'portable'
    $tempZip = Join-Path $buildRoot 'portable.zip'
    $zipCheck = Join-Path $buildRoot '便携版 验证目录'
    Copy-Item -LiteralPath $payloadRoot -Destination $zipStage -Recurse
    Copy-Item -LiteralPath $portableLauncher -Destination (Join-Path $zipStage '搬运工V2.exe')
    [IO.Compression.ZipFile]::CreateFromDirectory($zipStage, $tempZip)
    & $manifestPython @manifestPythonArgs -B (Join-Path $projectRoot 'updater.py') seal $tempZip --version $releaseVersion
    if ($LASTEXITCODE -ne 0) { throw 'Could not seal the exact portable ZIP.' }
    Expand-Archive -LiteralPath $tempZip -DestinationPath $zipCheck
    $check = Start-Process -FilePath (Join-Path $zipCheck '搬运工V2.exe') -ArgumentList '--check' -WorkingDirectory $buildRoot -WindowStyle Hidden -Wait -PassThru
    if ($check.ExitCode -ne 0) { throw "Portable ZIP launcher self-check failed: $($check.ExitCode)" }
    $zipPath = [IO.Path]::GetFullPath($PortablePath)
    New-Item -ItemType Directory -Force -Path (Split-Path $zipPath -Parent) | Out-Null
    Move-Item -LiteralPath $tempZip -Destination $zipPath -Force
    Get-FileHash -LiteralPath $zipPath -Algorithm SHA256 | Format-List
}

if ($PortableOnly) {
    Build-PortableZip
    return
}

$extensions = (& $wixExe extension list -g | Out-String)
if ($extensions -notmatch 'WixToolset\.BootstrapperApplications\.wixext\s+7\.0\.1') {
    & $wixExe extension add -g 'WixToolset.BootstrapperApplications.wixext/7.0.1'
    if ($LASTEXITCODE -ne 0) { throw 'Could not install the WiX Burn bootstrapper extension.' }
}

$wixNamespace = [System.Xml.Linq.XNamespace]'http://wixtoolset.org/schemas/v4/wxs'
$wixDocument = [System.Xml.Linq.XDocument]::new(
    [System.Xml.Linq.XDeclaration]::new('1.0', 'utf-8', 'yes'),
    [System.Xml.Linq.XElement]::new($wixNamespace + 'Wix')
)
$fragment = [System.Xml.Linq.XElement]::new($wixNamespace + 'Fragment')
$wixDocument.Root.Add($fragment)
$directoryRef = [System.Xml.Linq.XElement]::new($wixNamespace + 'DirectoryRef', [System.Xml.Linq.XAttribute]::new('Id', 'INSTALLFOLDER'))
$fragment.Add($directoryRef)
$componentGroup = [System.Xml.Linq.XElement]::new($wixNamespace + 'ComponentGroup', [System.Xml.Linq.XAttribute]::new('Id', 'PayloadFiles'))
$fragment.Add($componentGroup)
$directories = @{}
$directories[''] = $directoryRef

function Add-DirectoryCleanupComponent {
    param(
        [System.Xml.Linq.XElement]$Parent,
        [System.Xml.Linq.XElement]$Group,
        [string]$DirectoryKey
    )
    $normalizedKey = if ($DirectoryKey) { $DirectoryKey.Replace('\','/').ToLowerInvariant() } else { 'install-root' }
    $hash = [Security.Cryptography.SHA256]::HashData([Text.Encoding]::UTF8.GetBytes("cleanup:$normalizedKey"))
    $suffix = [Convert]::ToHexString($hash).Substring(0,24)
    [byte[]]$guidBytes = $hash[0..15]
    $guidBytes[6] = ($guidBytes[6] -band 0x0F) -bor 0x50
    $guidBytes[8] = ($guidBytes[8] -band 0x3F) -bor 0x80
    $componentId = "cleanup_$suffix"
    $component = [System.Xml.Linq.XElement]::new($wixNamespace + 'Component')
    $component.SetAttributeValue('Id', $componentId)
    $component.SetAttributeValue('Guid', ([guid]::new($guidBytes)).ToString('B').ToUpperInvariant())
    $remove = [System.Xml.Linq.XElement]::new($wixNamespace + 'RemoveFolder')
    $remove.SetAttributeValue('Id', "remove_$suffix")
    $remove.SetAttributeValue('On', 'uninstall')
    $registry = [System.Xml.Linq.XElement]::new($wixNamespace + 'RegistryValue')
    $registry.SetAttributeValue('Root', 'HKCU')
    $registry.SetAttributeValue('Key', 'Software\搬运工V2\Components')
    $registry.SetAttributeValue('Name', $componentId)
    $registry.SetAttributeValue('Type', 'integer')
    $registry.SetAttributeValue('Value', '1')
    $registry.SetAttributeValue('KeyPath', 'yes')
    $component.Add($remove)
    $component.Add($registry)
    $Parent.Add($component)
    $componentRef = [System.Xml.Linq.XElement]::new($wixNamespace + 'ComponentRef')
    $componentRef.SetAttributeValue('Id', $componentId)
    $Group.Add($componentRef)
}

Add-DirectoryCleanupComponent $directoryRef $componentGroup ''
$allPayloadFiles = Get-ChildItem -LiteralPath $payloadRoot -Recurse -File
foreach ($file in $allPayloadFiles) {
    $relative = $file.FullName.Substring($payloadRoot.Length + 1)
    $parts = $relative -split '[\\/]'
    $parentKey = ''
    $parent = $directoryRef
    for ($index = 0; $index -lt ($parts.Length - 1); $index++) {
        $parentKey = if ($parentKey) { Join-Path $parentKey $parts[$index] } else { $parts[$index] }
        if (-not $directories.ContainsKey($parentKey)) {
            $folderHash = [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData([Text.Encoding]::UTF8.GetBytes($parentKey))).Substring(0,16)
            $directory = [System.Xml.Linq.XElement]::new($wixNamespace + 'Directory',
                [System.Xml.Linq.XAttribute]::new('Id', "dir_$folderHash"),
                [System.Xml.Linq.XAttribute]::new('Name', $parts[$index]))
            $parent.Add($directory)
            Add-DirectoryCleanupComponent $directory $componentGroup $parentKey
            $directories[$parentKey] = $directory
        }
        $parent = $directories[$parentKey]
    }

    $normalized = $relative.Replace('\','/').ToLowerInvariant()
    $hash = [Security.Cryptography.SHA256]::HashData([Text.Encoding]::UTF8.GetBytes($normalized))
    $id = 'file_' + [Convert]::ToHexString($hash).Substring(0,24)
    [byte[]]$guidBytes = $hash[0..15]
    $guidBytes[6] = ($guidBytes[6] -band 0x0F) -bor 0x50
    $guidBytes[8] = ($guidBytes[8] -band 0x3F) -bor 0x80
    $guid = ([guid]::new($guidBytes)).ToString('B').ToUpperInvariant()
    $component = [System.Xml.Linq.XElement]::new($wixNamespace + 'Component',
        [System.Xml.Linq.XAttribute]::new('Id', "cmp_$($id.Substring(5))"),
        [System.Xml.Linq.XAttribute]::new('Guid', $guid))
    $registry = [System.Xml.Linq.XElement]::new($wixNamespace + 'RegistryValue',
        [System.Xml.Linq.XAttribute]::new('Root','HKCU'),
        [System.Xml.Linq.XAttribute]::new('Key','Software\搬运工V2\Components'),
        [System.Xml.Linq.XAttribute]::new('Name',"cmp_$($id.Substring(5))"),
        [System.Xml.Linq.XAttribute]::new('Type','integer'),
        [System.Xml.Linq.XAttribute]::new('Value','1'),
        [System.Xml.Linq.XAttribute]::new('KeyPath','yes'))
    $fileElement = [System.Xml.Linq.XElement]::new($wixNamespace + 'File',
        [System.Xml.Linq.XAttribute]::new('Id',$id),
        [System.Xml.Linq.XAttribute]::new('Name',$file.Name),
        [System.Xml.Linq.XAttribute]::new('Source',$file.FullName))
    $component.Add($registry)
    $component.Add($fileElement)
    $parent.Add($component)
    $componentGroup.Add([System.Xml.Linq.XElement]::new($wixNamespace + 'ComponentRef',
        [System.Xml.Linq.XAttribute]::new('Id',"cmp_$($id.Substring(5))")))
}
$generatedWxs = Join-Path $buildRoot 'GeneratedPayload.wxs'
$perUserDirectoryRef = [System.Xml.Linq.XElement]::new($wixNamespace + 'DirectoryRef')
$perUserDirectoryRef.SetAttributeValue('Id', 'PerUserProgramFilesFolder')
$fragment.Add($perUserDirectoryRef)
Add-DirectoryCleanupComponent $perUserDirectoryRef $componentGroup 'per-user-programs-root'
$wixDocument.Save($generatedWxs)

$msi = Join-Path $buildRoot 'MoverV2.msi'
Write-Output '[5/5] Compiling MSI and Chinese EXE installer.'
& $wixExe build (Join-Path $PSScriptRoot 'Product.wxs') $generatedWxs -arch x64 -d "ReleaseVersion=$releaseVersion" -o $msi -pdbtype none
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $msi -PathType Leaf)) {
    throw 'WiX failed to build the MSI package.'
}

$finalPath = [System.IO.Path]::GetFullPath($OutputPath)
$temporaryExe = Join-Path $buildRoot '搬运工V2-安装程序.exe'
& $wixExe build (Join-Path $PSScriptRoot 'Bundle.wxs') `
    -ext 'WixToolset.BootstrapperApplications.wixext/7.0.1' `
    -arch x64 `
    -d "ReleaseVersion=$releaseVersion" `
    -d "MsiPath=$msi" `
    -d "InstallerSource=$PSScriptRoot" `
    -loc (Join-Path $PSScriptRoot 'zh-CN.wxl') `
    -o $temporaryExe `
    -pdbtype none
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $temporaryExe -PathType Leaf)) {
    throw 'WiX failed to build the EXE bootstrapper.'
}

New-Item -ItemType Directory -Force -Path (Split-Path $finalPath -Parent) | Out-Null
Move-Item -LiteralPath $temporaryExe -Destination $finalPath -Force
$installerHash = (Get-FileHash -LiteralPath $finalPath -Algorithm SHA256).Hash
[PSCustomObject]@{
    Installer = $finalPath
    SizeBytes = (Get-Item -LiteralPath $finalPath).Length
    SHA256 = $installerHash
    BundledUv = $uvVersionOutput
    VerifiedUvExecutableSHA256 = $actualUvHash
} | Format-List
if ($PortablePath) { Build-PortableZip }
