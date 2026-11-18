# 搬运工V2

> **源码备份状态（2026-11-18）**：本仓库已保存截至本次备份的源码，包含下述 2.0.9 相关更新。GitHub Releases 当前最新已发布版本为 **v2.0.2**；这次源码备份没有构建或发布新的 Windows 程序，也不代表已完成真实 QQ / 飞书收发验证。备份不包含本机运行环境、QQ / 飞书登录数据、聊天记录、本机设置或实际转发规则；示例规则为空，重新部署后须自行登录和设置。

Windows 推荐入口：完整解压 ZIP 后，双击 **后台运行.vbs** 或 **搬运工V2.exe**。程序会无命令窗口启动工作台、AstrBot 和后台守护，日志保存在 `runtime/logs`。需要扫码、配置规则或查看日志时，双击 **打开工作台.vbs**，或访问 `http://127.0.0.1:18766/`。首次下载需要联网，会在后台准备 Python、AstrBot 与依赖，无需手工配置 Python。

首次使用双击 `打开工作台.vbs`，按页面完成 QQ / 飞书扫码与必要的协议确认，并选择要转发的群。已有本机登录和群规则会保留；再次双击会复用服务。同目录重复点击不会并发安装，日志每次独立保存到 `runtime/logs/bootstrap-*.log`。`launch-workbench.cmd` 和 EXE 启动器都调用同一套引导流程；请勿单独运行安装辅助脚本。

## 2.0.9 修复 Windows PowerShell 5.1 更新

如果更新时出现 `NameError: name 'update' is not defined`，完整解压新版 ZIP 到新文件夹，然后双击 `打开工作台.vbs`。新版会先定位正在运行的工作台，安全退出旧进程后再更新原目录，保留本机映射、登录和后台设置。新版启动后，再到「本地设置 → 后台保活」保存。即使自动恢复已关闭，也可以手动打开工作台。

## 2.0.8 原生视频与映射编辑

已保存的规则可以在编辑窗口改为单向或双向。改为单向只移除对应的反向规则。列表里的“转发本人：开/关”会立即保存当前方向；这个开关仍默认为关闭。升级同时修复了 QQ 账号专用配置覆盖全局本人消息上报的问题。开启映射的本人开关后可以转发本人手动发送的新消息，程序自己的转发回声仍会过滤。

飞书富文本或合并记录内的视频会提取真实视频内容。发到飞书使用原生视频消息，发到 QQ 使用原生视频段。合并记录中的文字和图片按原顺序转发，省去合并标题、发送者编号和资源标识。媒体读取或发送失败会写入转发日志。旧版已发出的摘要不会自动替换，请重新发送原消息测试。

## 2.0.7 本人消息与隐藏后台

本人消息开关仍默认关闭，在每条映射的“转发本人手动发送的消息”中单独开启。本版补上 QQ 节点本人事件上报，并修复本人向好友发消息时被错误归入本账号会话的问题。关闭时不转本人消息，打开后转发，重复上报和程序自己的转发回声仍会被过滤。升级只修改本项目的 QQ WS 节点，不会替你打开规则开关。

`后台运行.vbs` 完全不创建命令窗口；`一键启动.cmd` / `更新并启动.cmd` 会立即转交无窗口 EXE 后退出，CMD 本身可能短暂闪现。后台入口不自动打开浏览器和接入向导；启动失败写日志，避免弹框打断无人值守。`打开工作台.vbs` 明确打开浏览器，后台保活继续工作。安装版开始菜单分别提供“后台运行搬运工V2”和“打开搬运工V2工作台”；桌面的工作台快捷方式打开设置页面。QQ/飞书的扫码或重新登录仍需人工完成，平台登录窗口按需显示；保持会话在线即可，无需一直打开工作台页面。

更新时完整解压新版 ZIP，双击 **一键启动.cmd** 或 **更新并启动.cmd**。程序先校验新版每个文件的 SHA-256，再自动定位已登记或正在运行的旧安装目录，只覆盖程序文件；`runtime`、QQ/飞书登录信息、转发规则和本机设置保持原样。旧程序文件备份保存在原目录的 `runtime/update-backups`；覆盖失败会回滚。若旧安装已被删除，新包不能恢复此前已删除的登录数据，需要重新扫码。开发目录没有发行清单时可本地启动，跨目录更新必须提供完整校验清单。

一键启动后，工作台与 AstrBot 在后台运行，并由守护进程在异常退出后自动恢复。SnowLuma 原生连接器在已启动后意外退出时也会按退避策略恢复；手动停止和等待扫码会被保留。飞书连接随 AstrBot 加载已有凭证恢复，凭证失效时需重新扫码。在“本地设置 → 后台保活”中，可分别设置自动恢复、防空闲睡眠与 **Windows 登录后自动运行**。关闭浏览器、锁屏、显示器息屏均不影响后台进程；保持电脑联网且 Windows 用户已登录。手动睡眠、休眠、关机及退出用户会中断转发。

“运行总览 → 实时日志与查询”可以按组件、级别、关键词、时间和日志文件筛选，并查看更早记录。覆盖 AstrBot、SnowLuma、飞书连接、转发插件、后台守护、工作台与启动配置；实时刷新可随时关闭。新产生的工作台、守护、转发和 AstrBot 收集日志按大小轮转；查询结果会隐藏常见凭证字段。

本版还修复普通文本信封内嵌富文本视频的解析，覆盖本人消息与 QQ 私聊。无效媒体会记录失败，不会把 `[富文本] [视频]` 摘要当正文发送。旧版已经发出的占位文字属于历史消息，需要重新转发。

发布包包含固定版本 SnowLuma Go 插件源码归档，来源、提交、校验值和许可证见 `vendor/snowluma-go.json` 及归档内 `LICENSE`；SnowLuma 本体首次启动按官方发布包 SHA-256 校验后下载。LarkAgentX 的完整扫码依赖同时按 AstrBot 宿主版本校验，避免首次扫码缺包。

搬运工V2 包含独立运行的本地工作台和 AstrBot 插件。每条映射可以独立选择 QQ 或飞书作为来源和目标，因此支持 QQ→QQ、QQ→飞书、飞书→QQ、飞书→飞书，也支持一个来源转发到多个目标。LarkAgentX 的 agent backend 固定为 `none`，桥接插件会截住受管平台事件，不触发 AstrBot AI 回复。

## 下载与源码

中文 Windows 安装程序和便携 ZIP 请从 [GitHub Releases](https://github.com/nnn256/ban-yun-gong-v2/releases/latest) 下载。公开源代码仓库：[搬运工V2](https://github.com/nnn256/ban-yun-gong-v2)。发行包不包含任何 QQ/飞书登录数据或用户群映射；首次使用时请在本机扫码登录并自行创建转发规则。

## 运行工作台

便携版 ZIP：下载 ZIP，解压全部文件到同一目录，双击根目录里的 `搬运工V2.exe` 即可隐藏启动后台；需要浏览器时双击 `打开工作台.vbs`；首次启动需要联网下载 AstrBot/LarkAgentX 运行环境。该 ZIP 不含现有账号或映射规则，首次运行创建空映射。

中文版 Windows 安装程序也可从上述 GitHub Releases 页面下载并双击运行。安装器默认使用当前用户的 `%LOCALAPPDATA%\Programs\搬运工V2`，可在“选项”修改目录；安装结束后可从桌面或开始菜单启动。EXE/ZIP 均不包含 QQ 登录数据、飞书登录凭据、`bridge_rules.json`、`cmd_config.json` 或 `runtime` 数据。随包示例规则为空；在新目录安装会从空映射开始，安装到已有目录则保留该目录的本机登录与映射数据。若需要全新设置，请在“选项”选一个空目录，并先停止另一目录中正在运行的工作台/AstrBot，避免固定端口冲突。首次启动需要联网下载 AstrBot 运行环境和固定提交的 LarkAgentX。

在 Windows PowerShell 中运行：

~~~powershell
Set-ExecutionPolicy -Scope Process Bypass
.\start-workbench.ps1
~~~

工作台地址为 `http://127.0.0.1:18766`。首次运行会在 `runtime` 下准备 AstrBot 4.29.4、固定提交的 LarkAgentX 消息客户端和桥接插件，启动本机工作台与 AstrBot；数据与 D 盘其他项目隔离。AstrBot 管理页为 `http://127.0.0.1:6185/`。首次密码会显示在工作台；登录并按提示修改后，把新密码粘贴回工作台保存，密码副本使用 Windows DPAPI 加密。

在“群映射”中分别选择来源和目标平台、账号、会话类型与群/好友；支持 QQ→QQ、QQ→飞书、飞书→QQ、飞书→飞书。同一来源也可配置多个目标。选择双向会创建两条独立映射。每个 QQ 账号分别显示登录与 OneBot 状态，群归属由已验证的群列表确认。会话选择器优先显示经元数据核验的群名或联系人名，并保留会话 ID。本人手动消息默认关闭，可在新建规则时勾选或在已保存的规则表逐条切换；顶栏全局开关可暂停或恢复所有映射。桥接回声按发送回执过滤。LarkAgentX 扫码凭证存放在 `runtime\LarkAgentX-data`。工作台启动时会下载固定提交的源码包并校验 SHA-256，然后仅安装它运行时缺少的 protobuf 依赖。

AstrBot OneBot v11 反向 WebSocket 实例使用本机地址 `127.0.0.1`、端口 `6199`，OneBot Access Token 留空。LarkAgentX 用飞书 App 扫码登录个人账号，不需要创建飞书机器人 App ID / App Secret；其凭证仅保存在本机数据目录。

停止 AstrBot 使用工作台按钮，守护进程会尊重手动停止。停止本安装的工作台与 AstrBot 运行 `.\stop-workbench.ps1`；本机登录和规则会保留。只关闭浏览器标签不会停止后台运行。

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

每条映射可分别勾选文字、表情包、图片、视频、语音和文件；“编辑”可修改现有单向或双向映射。旧映射未设置 `content_types` 时默认允许全部类型，禁止空选择。本人手动消息由 `include_self` 控制，默认关闭；桥接自己发送的消息按回执过滤，始终防止回声循环。

飞书视频同时支持独立 MEDIA 消息和嵌在 POST 富文本中的视频。富文本按元素顺序解析真实文字、图片和媒体资源，各节点分别经过类型筛选；视频不再变成 `[富文本] [视频]` 摘要。缺少资源或下载失败会记录失败，不把内部媒体占位符当正文发送。全部节点允许时，飞书同平台转发保留原富文本；只允许部分类型时仅发送选中的节点。

转发文字只保留正文。图片和 QQ 数字表情按图片发送，表情包单独筛选。QQ→飞书语音转换为 Opus 并发送原生 AUDIO；视频转换为 H.264/AAC MP4、提取封面并发送原生 MEDIA。飞书→QQ先下载实际媒体，再使用 SnowLuma 管理器的 Record、Video 原生消息段；飞书→飞书媒体使用平台原生转发。音视频发送不会自动降级为普通文件；错误会计入失败，已发送成功的消息片段在重试时不重复发送。实际服务端已验证 AUDIO=7、MEDIA=15、QQ record/video 回执及媒体资源下载。首装脚本自动安装固定版本的本机转码组件，无需配置 AI 或手工填写平台 Token。

扫码是登录账号；监听还需要本项目的连接服务保持运行。关闭工作台浏览器标签或飞书桌面窗口不会主动注销 LarkAgentX；退出 LarkAgentX 登录、凭证失效、停止服务或电脑关机后不能继续监听。QQ 依赖 SnowLuma 对应账号会话保持在线，窗口可以最小化，退出该 QQ 会话会断开连接。工作台、AstrBot、OneBot 和 SnowLuma 使用本机不同端口；`127.0.0.1` 代表本机，不是另一项目的公网 IP。失效临时 AstrBot 路径会在本项目运行目录验证有效后修复，有效自定义路径不会被替换。

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
