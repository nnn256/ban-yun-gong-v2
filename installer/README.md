# 搬运工V2 安装器

在 Windows x64 上准备 PowerShell 7、WiX Toolset 7.0.1、.NET Framework C# 编译器，以及带测试依赖的 Python。建议使用项目初始化后的 AstrBot Python 3.12 环境。也可以将 `MOVER_VERIFY_PYTHON` 设为独立环境的 `python.exe` 路径。Node.js 用于运行工作台 JavaScript 测试。

在仓库根目录运行以下命令，生成当前版本的安装器和便携 ZIP。

```powershell
.\installer\build-installer.ps1 -PortablePath '.\artifacts\搬运工V2-2.0.16.zip'
```

安装器默认命名为 `artifacts/搬运工V2-Setup-2.0.16-x64.exe`。MSI、向导和发行清单从桥接插件的 `metadata.yaml` 读取同一版本号。也可用 `./package.ps1` 单独生成便携版。

安装器默认装到当前用户的 `%LOCALAPPDATA%\Programs\搬运工V2`，可在“选项”中更改目录。升级自定义位置时请选择原安装目录。向导完成页和桌面“搬运工V2工作台”快捷方式会打开该安装的工作台。首次启动需要联网准备 Python、AstrBot 4.29.4 及平台组件。

两种成品使用同一份明确的程序文件清单。`bridge_rules.example.json` 中没有预设规则。账号、群映射、运行数据和日志均留在本机。Windows 设置中的卸载项管理程序和快捷方式，保留本机设置和 `runtime` 数据。
