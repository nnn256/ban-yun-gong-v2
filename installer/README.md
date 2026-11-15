# 搬运工V2 安装器

若不需要安装向导，可解压 `artifacts/搬运工V2.zip` 并运行根目录 `搬运工V2.exe`。

在仓库根目录运行 `./installer/build-installer.ps1`，生成 `artifacts/搬运工V2-安装程序.exe`。

安装器使用 WiX Toolset 7 中文向导，默认安装位置为 `D:/搬运工V2`，可在“选项”中修改。它会创建桌面及开始菜单快捷方式。首次启动需要联网安装 AstrBot 4.29.4 与固定提交的 LarkAgentX。

安装包仅包含程序文件和官方 uv 工具，不包含 `runtime`、QQ 登录数据、飞书登录凭据、`bridge_rules.json`、`cmd_config.json`、工作台本机设置或访问口令。随包的 `bridge_rules.example.json` 为空；在新目录安装会从空映射开始，安装到已有目录则会保留该目录的本机登录与映射数据。若需全新设置，请在“选项”选择空目录，并先停止另一目录的工作台/AstrBot，避免端口冲突。
