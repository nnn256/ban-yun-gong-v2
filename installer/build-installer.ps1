param(
    [string]$OutputPath = (Join-Path (Split-Path $PSScriptRoot -Parent) 'artifacts/搬运工V2-安装程序.exe')
)

$ErrorActionPreference = 'Stop'
$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$artifactRoot = Join-Path $projectRoot 'artifacts'
$buildRoot = Join-Path $artifactRoot ('.installer-build-' + [guid]::NewGuid().ToString('N'))
$cacheRoot = Join-Path $artifactRoot '.installer-cache'
$payloadRoot = Join-Path $buildRoot 'payload'
$downloads = Join-Path $buildRoot 'downloads'
$wixExe = 'C:/Program Files/WiX Toolset v7.0/bin/wix.exe'
$uvVersion = '0.13.8'
$uvArchiveHash = '529E78A9CEC6CD7E3E014F7ECD8BDA0C113969D4ED0F2EFEEDB85C30943325B8'
$uvArchiveName = 'uv-x86_64-pc-windows-msvc.zip'
$uvArchiveUrl = "https://github.com/astral-sh/uv/releases/download/$uvVersion/$uvArchiveName"

& (Join-Path $projectRoot 'verify.ps1')
if ($LASTEXITCODE -ne 0) { throw 'Project verification failed; installer was not built.' }
Write-Output '[1/5] Project tests passed.'
$portableLauncher = Join-Path $artifactRoot 'portable/搬运工V2.exe'
$portableUv = Join-Path $artifactRoot 'portable/tools/uv.exe'
& (Join-Path $projectRoot 'build-portable.ps1') -OutputExe $portableLauncher -UvOutput $portableUv
if ($LASTEXITCODE -ne 0) { throw 'Portable launcher build failed; installer was not built.' }
if (-not (Test-Path -LiteralPath $wixExe -PathType Leaf)) {
    throw "WiX Toolset v7 was not found: $wixExe"
}

New-Item -ItemType Directory -Force -Path $payloadRoot,$downloads,$artifactRoot,$cacheRoot | Out-Null
$rootFiles = @(
    'README.md', 'VERIFICATION.txt', 'launch-workbench.cmd',
    'install.ps1', 'install_astrbot.ps1', 'install_larkagentx.ps1',
    'install_snowluma.ps1', 'start_snowluma.ps1', 'start-workbench.ps1',
    'stop-workbench.ps1', 'workbench_server.py', 'workbench.settings.example.json'
)
foreach ($file in $rootFiles) {
    Copy-Item -LiteralPath (Join-Path $projectRoot $file) -Destination (Join-Path $payloadRoot $file)
}
Copy-Item -LiteralPath $portableLauncher -Destination (Join-Path $payloadRoot 'mover-v2-launcher.exe')

foreach ($directory in @('astrbot_plugin_message_bridge', 'workbench')) {
    $source = Join-Path $projectRoot $directory
    $destination = Join-Path $payloadRoot $directory
    New-Item -ItemType Directory -Force -Path $destination | Out-Null
    Get-ChildItem -LiteralPath $source -Recurse -File |
        Where-Object { $_.FullName -notmatch '\\(__pycache__|node_modules)\\' -and $_.Extension -ne '.pyc' } |
        ForEach-Object {
            $relative = $_.FullName.Substring($source.Length + 1)
            $target = Join-Path $destination $relative
            New-Item -ItemType Directory -Force -Path (Split-Path $target -Parent) | Out-Null
            Copy-Item -LiteralPath $_.FullName -Destination $target
        }
}

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

$uvZip = Join-Path $downloads $uvArchiveName
$cachedUvZip = Join-Path $cacheRoot $uvArchiveName
if (-not (Test-Path -LiteralPath $cachedUvZip -PathType Leaf)) {
    $previousBuilds = Get-ChildItem -LiteralPath $artifactRoot -Directory -Filter '.installer-build-*' -ErrorAction SilentlyContinue
    $previousArchive = $previousBuilds | ForEach-Object { Join-Path (Join-Path $_.FullName 'downloads') $uvArchiveName } |
        Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
    if ($previousArchive) {
        Copy-Item -LiteralPath $previousArchive -Destination $cachedUvZip
    }
    else {
        Write-Output '[3/5] Downloading the official uv package (15 MB) for the installer.'
        & curl.exe -fLsS --retry 2 --connect-timeout 15 --max-time 180 -o $cachedUvZip $uvArchiveUrl
        if ($LASTEXITCODE -ne 0) { throw "Official uv download failed (curl exit $LASTEXITCODE)." }
    }
}
$cachedUvHash = (Get-FileHash -LiteralPath $cachedUvZip -Algorithm SHA256).Hash
if ($cachedUvHash -ne $uvArchiveHash) { throw "uv cache SHA-256 mismatch: $cachedUvHash" }
Copy-Item -LiteralPath $cachedUvZip -Destination $uvZip -Force
$actualUvHash = (Get-FileHash -LiteralPath $uvZip -Algorithm SHA256).Hash
if ($actualUvHash -ne $uvArchiveHash) { throw "uv archive SHA-256 mismatch: $actualUvHash" }
$uvExtract = Join-Path $downloads 'uv-extract'
Expand-Archive -LiteralPath $uvZip -DestinationPath $uvExtract
$uvSource = Get-ChildItem -LiteralPath $uvExtract -Filter 'uv.exe' -Recurse -File | Select-Object -First 1
if (-not $uvSource) { throw 'Verified uv archive did not contain uv.exe.' }
$tools = Join-Path $payloadRoot 'tools'
New-Item -ItemType Directory -Force -Path $tools | Out-Null
$bundledUv = Join-Path $tools 'uv.exe'
Copy-Item -LiteralPath $uvSource.FullName -Destination $bundledUv
$uvVersionOutput = (& $bundledUv --version | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $uvVersionOutput -notmatch "^uv $([regex]::Escape($uvVersion)) ") {
    throw "Bundled uv version check failed: $uvVersionOutput"
}

foreach ($license in @('LICENSE-APACHE', 'LICENSE-MIT')) {
    Write-Output "[4/5] Retrieving uv $license notice."
    $uri = "https://api.github.com/repos/astral-sh/uv/contents/$license"
    $response = Invoke-RestMethod -Uri $uri -Headers @{ 'User-Agent' = 'MoverV2-Installer' } -TimeoutSec 30
    if (-not $response.content) { throw "Could not retrieve the uv $license notice." }
    $bytes = [Convert]::FromBase64String(($response.content -replace '\s', ''))
    [System.IO.File]::WriteAllBytes((Join-Path $tools "$license.txt"), $bytes)
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
& $wixExe build (Join-Path $PSScriptRoot 'Product.wxs') $generatedWxs -arch x64 -o $msi -pdbtype none
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $msi -PathType Leaf)) {
    throw 'WiX failed to build the MSI package.'
}

$finalPath = [System.IO.Path]::GetFullPath($OutputPath)
$temporaryExe = Join-Path $buildRoot '搬运工V2-安装程序.exe'
& $wixExe build (Join-Path $PSScriptRoot 'Bundle.wxs') `
    -ext 'WixToolset.BootstrapperApplications.wixext/7.0.1' `
    -arch x64 `
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
    VerifiedUvArchiveSHA256 = $actualUvHash
} | Format-List
