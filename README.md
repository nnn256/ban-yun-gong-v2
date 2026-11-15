# 搬运工V2

搬运工V2 包含独立运行的本地工作台和 AstrBot 插件。每条映射可以独立选择 QQ 或飞书作为来源和目标，因此支持 QQ→QQ、QQ→飞书、飞书→QQ、飞书→飞书，也支持一个来源转发到多个目标。LarkAgentX 的 agent backend 固定为 `none`，桥接插件会截住受管平台事件，不触发 AstrBot AI 回复。

## 下载与源码

中文 Windows 安装程序和便携 ZIP 请从 [GitHub Releases](https://github.com/nnn256/ban-yun-gong-v2/releases/latest) 下载。公开源代码仓库：[搬运工V2](https://github.com/nnn256/ban-yun-gong-v2)。发行包不包含任何 QQ/飞书登录数据或用户群映射；首次使用时请在本机扫码登录并自行创建转发规则。

## 运行工作台

便携版 ZIP：从 GitHub Releases 下载 ZIP，解压全部文件到同一目录，双击根目录里的 `搬运工V2.exe` 即可启动工作台并打开浏览器；首次启动需要联网下载 AstrBot/LarkAgentX 运行环境。该 ZIP 不含现有账号或映射规则，首次运行创建空映射。

中文版 Windows 安装程序也可从上述 GitHub Releases 页面下载并双击运行。安装器默认使用 `D:\搬运工V2`，可在“选项”修改目录；安装结束后可从桌面或开始菜单启动。EXE/ZIP 均不包含 QQ 登录数据、飞书登录凭据、`bridge_rules.json`、`cmd_config.json` 或 `runtime` 数据。随包示例规则为空；在新目录安装会从空映射开始，安装到已有目录则保留该目录的本机登录与映射数据。若需要全新设置，请在“选项”选一个空目录，并先停止另一目录中正在运行的工作台/AstrBot，避免固定端口冲突。首次启动需要联网下载 AstrBot 运行环境和固定提交的 LarkAgentX。

在 Windows PowerShell 中运行：

~~~powershell
Set-ExecutionPolicy -Scope Process Bypass
.\start-workbench.ps1
~~~

工作台地址为 `http://127.0.0.1:18766`。首次运行会在 `runtime` 下准备 AstrBot 4.29.4、固定提交的 LarkAgentX 消息客户端和桥接插件，启动本机工作台与 AstrBot；数据与 D 盘其他项目隔离。AstrBot 管理页为 `http://127.0.0.1:6185/`。首次密码会显示在工作台；登录并按提示修改后，把新密码粘贴回工作台保存，密码副本使用 Windows DPAPI 加密。

在“群映射”中分别选择来源和目标平台、账号、会话类型与群/好友；支持 QQ→QQ、QQ→飞书、飞书→QQ、飞书→飞书。同一来源也可配置多个目标。选择双向会创建两条独立映射。每个 QQ 账号分别显示登录与 OneBot 状态，群归属由已验证的群列表确认。会话选择器优先显示经元数据核验的群名或联系人名，并保留会话 ID。本人手动消息默认关闭，可在新建规则时勾选或在已保存的规则表逐条切换；顶栏全局开关可暂停或恢复所有映射。桥接回声按发送回执过滤。LarkAgentX 扫码凭证存放在 `runtime\LarkAgentX-data`。工作台启动时会下载固定提交的源码包并校验 SHA-256，然后仅安装它运行时缺少的 protobuf 依赖。

AstrBot OneBot v11 反向 WebSocket 实例使用本机地址 `127.0.0.1`、端口 `6199`，OneBot Access Token 留空。LarkAgentX 用飞书 App 扫码登录个人账号，不需要创建飞书机器人 App ID / App Secret；其凭证仅保存在本机数据目录。

停止 AstrBot 使用工作台按钮；停止工作台运行 `.\stop-workbench.ps1`。停止工作台不会关闭 AstrBot 或 Snowluma Go 管理的 SnowLuma。

本地安装 AstrBot 与插件也可单独运行 `.\install_astrbot.ps1`。打包项目运行 `.\package.ps1`；ZIP 不包含 AstrBot 虚拟环境、运行日志或本地平台数据。

## 本机部署

1. 运行 `.\start-workbench.ps1`，打开 AstrBot 管理页，首次登录后设置新密码。
2. 桥接插件只处理规则转发；无需配置大模型。
3. 在“机器人”添加 OneBot v11，ID `qq_bridge`，主机 `127.0.0.1`，端口 `6199`，Token 留空，保存。
4. 在“扩展功能 → SnowLuma 控制台”按“部署步骤”安装并启动 SnowLuma；阅读后手动确认用户协议/隐私政策，启动 QQ、扫码登录，再在“节点配置 → WS 客户端”添加 AstrBot 地址 `ws://127.0.0.1:6199/ws`，端口 6199，Token 留空。
5. 在 SnowLuma 扩展页点“同步连接”，确认在线状态。
6. 在工作台“接入向导”生成飞书二维码，用飞书 App 扫码登录个人账号。LarkAgentX 凭证写入本机 `runtime\LarkAgentX-data`；LarkAgentX agent backend 固定为 `none`。
7. 在“群映射”分别选择来源和目标，可建立 QQ→QQ、QQ→飞书、飞书→QQ 或飞书→飞书；选择双向可自动生成反方向规则。本人消息默认关闭，可在新增映射时勾选或在规则表逐条切换。顶栏“停止全部转发”会暂停所有映射但保持监听；点击“恢复全部转发”可重新开始。
8. “打开本机 QQ 扫码”会复用当前 Windows QQ 客户端；如果账号尚未登录，请在 QQ 窗口用手机 QQ 扫码。本机飞书桌面客户端可单独打开用于查看；消息监听状态以 LarkAgentX 连接和工作台实时状态为准。

若需手动安装插件到另一 AstrBot 根目录，运行：

~~~powershell
Set-ExecutionPolicy -Scope Process Bypass
.\install.ps1 -AstrBotRoot 'D:\AstrBot'
~~~

规则文件位于：

~~~text
<AstrBot根目录>\data\plugin_data\astrbot_plugin_message_bridge\bridge_rules.json
~~~

工作台保存后，下一条消息会使用新映射；顶栏全局开关即时暂停或恢复转发。修改插件代码后需重载插件或重启 AstrBot。

## 配置规则

每条规则的 `source_platform` 可为 `aiocqhttp` 或 `larkx`，目标也可为这两个平台，因此覆盖四种组合。QQ 的 `source_platform_id` 使用 OneBot 实例 ID；LarkAgentX 账号 ID 固定为 `default`。QQ 目标 UMO 为 `qq_bridge:GroupMessage:群号` 或 `qq_bridge:FriendMessage:好友QQ号`；飞书目标为 `larkx:GroupMessage:chat_id` 或 `larkx:FriendMessage:chat_id`。通过 `source_account_id` 和 `target_account_id` 指定 QQ 账号。`include_self` 控制是否转发本人手动发送的消息，默认 `false`；顶层 `bridge_enabled` 控制所有规则的全局暂停与恢复。

例如：

~~~json
{
  "name": "QQ 群转飞书群",
  "enabled": true,
  "source_platform": "aiocqhttp",
  "source_platform_id": "qq_bridge",
  "source_message_type": "GroupMessage",
  "source_session_id": "123456789",
  "target_umo": "larkx:GroupMessage:FEISHU_CHAT_ID"
}
~~~

飞书群反向到 QQ 时来源平台设为 `larkx`、实例 ID `default`，来源会话填写对应 `chat_id`，目标会话填 `qq_bridge:GroupMessage:QQ群号`。首条飞书消息会把会话 ID 记录在工作台可选列表中。样例文件在插件目录的 `bridge_rules.example.json`。

转发文字只保留消息正文，不添加项目名、平台名或发送者姓名。图片和 QQ 数字系统表情按图片传输；无法取得实际贴图资源时会保留为简短表情标签，不转发内部资源 key。文本、图片、语音、视频和文件均进入同一条路由链。飞书→QQ使用 QQ 图片、Record、Video 或 File 消息段；QQ→飞书图片作为原生图片发送，语音、视频和文件发送为真实文件附件。LarkAgentX 当前可验证上传接口没有原生语音气泡/视频播放器的发送契约，因此音视频在飞书显示为可下载附件。飞书→飞书重新发送正文与媒体，不添加原发送者信息。

多 QQ 账号的规则可使用 `source_account_id` 限定来源 QQ，使用 `target_account_id` 指定发送 QQ。旧规则未指定账号时，只有目标群在已验证在线账号中归属唯一，系统才会自动选择；共有群或多账号私聊需明确选择账号。实时登录身份来自已连接 OneBot 的 `get_login_info`，不是只看 QQ 进程是否存在。飞书桌面进程和 LarkAgentX 账号连接状态分别显示；已有有效凭证时自动恢复监听，无需反复扫码。

## 本地验证与打包

运行测试：

~~~powershell
.\verify.ps1
~~~

生成完整项目 ZIP：

~~~powershell
.\package.ps1
~~~

成品位于 `artifacts\搬运工V2.zip`。测试覆盖四种平台路由、QQ 多账号选择、同来源多目标、本人消息开关、全局暂停/恢复、图片与音视频附件传输、安装脚本、AstrBot 启停和工作台表单。单元测试不会发送真实群消息；需在已映射的会话发送测试内容后确认线上收发。

## 参考

- [AstrBot 安装与启动](https://docs.astrbot.app/platform/start.html)
- [AstrBot OneBot v11 接入](https://docs.astrbot.app/platform/aiocqhttp.html)
- [Snowluma Go AstrBot 扩展](https://github.com/Shou-Lu/Snowluma-Go)
- [LarkAgentX](https://github.com/cv-cat/LarkAgentX)
