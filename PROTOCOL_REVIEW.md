# 消息协议与多图转发审计

核查日期：2026-11-19。

本文件记录开发文档和固定上游源码能够证明的行为、实现要求及尚未证实的边界。核查仅包含公开文档和代码读取，没有读取个人登录数据，也没有向真实 QQ 或飞书会话发送测试消息。最终回归结果以 `VERIFICATION.txt` 为准；本文不将文档核查当作真实账号收发验收。

## 1. 本项目使用的通道

| 通道 | 实际接口 | 本次核查的用途 |
| --- | --- | --- |
| QQ 个人账号 | SnowLuma v2.4.2 / OneBot v11 | 消息段数组、图片来源格式、发送回执及已有合并记录读取 |
| 飞书个人账号 | 固定版本 LarkAgentX 个人会话协议 | POST 元素树、资源下载上下文、接收确认及补拉 |
| QQ 官方机器人文档 | QQ Bot v2 OpenAPI | 对照官方消息类型和限制，不能直接套用给 OneBot |
| 飞书开放平台文档 | 飞书 IM OpenAPI | 对照消息内容模型，不能断言公开字段等同于个人会话协议字段 |

本项目不因本次审计切换为官方机器人接入，也不需要接入大模型。官方 API 的令牌、机器人权限、OpenID 和请求结构与当前个人账号通道不同。

## 2. 一条消息内多张图片与合并聊天记录不同

飞书[发送消息内容结构](https://open.feishu.cn/document/server-docs/im-v1/message-content-description/create_json?lang=zh-CN)明确说明：一条 `post` 富文本可以分多个段落，每段包含节点；图片和视频必须各自独占段落。`img.image_key` 指向图片，视频使用 `media.file_key`，可另带封面 `image_key`。因此，同一条消息包含五张图片是合法内容，不能仅转发顶层显示摘要或只取第一张图片。

飞书[接收消息内容结构](https://open.feishu.cn/document/server-docs/im-v1/message-content-description/message_content?lang=zh-CN)将 `merge_forward` 的正文描述为固定的 `Merged and Forwarded Message` 占位，需要继续获取子消息。占位文本并不是聊天记录正文。

飞书[消息管理概述](https://open.feishu.cn/document/server-docs/im-v1/message/intro?lang=zh-CN)还区分：

- `root_id`：回复树的根消息。
- `parent_id`：直接被回复的消息；话题内回复有专门规则。
- `upper_message_id`：合并转发子消息所在的外层合并消息。

用户截图能说明“客户端显示多张图片，而目标端出现了摘要或缺图”，不能仅凭截图确定原始消息一定属于 Markdown、POST、批量逐条转发或合并转发。排查必须依据实际结构和可匿名化的消息类型、节点数量、下载结果、发送回执，不能把截图当作 `MARKDOWN` 节点的协议证据。

实现要求：普通 POST 按原树顺序提取正文及所有媒体；连续图片可以组成 QQ 的同条多图消息；文字、视频等原始边界不能因过滤或去重而消失。真正的合并转发继续按子消息处理，本轮不据此新增未经验证的 QQ 原生合并聊天卡片。

## 3. 官方 content_v2 与私有 richTextWithMd 的证据边界

飞书[接收消息内容结构](https://open.feishu.cn/document/server-docs/im-v1/message-content-description/message_content?lang=zh-CN)和[发送消息内容结构](https://open.feishu.cn/document/server-docs/im-v1/message-content-description/create_json?lang=zh-CN)说明：`md` 标签仅在 `content_v2` 保留原始 Markdown；旧 `content` 会转换或简化部分样式，官方推荐优先读取 `content_v2`。没有 `md` 时，两者内容相同。

以上描述针对飞书 OpenAPI，不能据此宣称 `content_v2 == richTextWithMd`，也不能宣称私有 `richText` 必定不完整。

固定 LarkAgentX 版本为 `d288ef4df8a42f6286c5506b7185f64430071490`。读取该版本的 [protobuf 定义](https://github.com/cv-cat/LarkAgentX/blob/d288ef4df8a42f6286c5506b7185f64430071490/larkx/proto/lark_all_pb2.py)的序列化 descriptor，可确认：

| 私有结构 | 已确认字段 |
| --- | --- |
| `entities.PostContent` | `richText` 第 6 字段；`richTextWithMd` 第 16 字段，二者均为 `RichText` |
| `entities.RichTextElement` | `tag`、二进制 `property`、有序 `childIds`；存在 `MARKDOWN` 类型 |
| `RichTextElement.MarkdownProperty` | `content` 第 1 字段；`images` 第 2 字段，类型为 `map<string, ImageSetV2>` |
| `ImageSetV2` | `imageKey`、`fsUnit`、`crypto`、图片规格等 |
| `entities.Message` | `isTruncated` 第 22 字段，布尔型 |

准确字段名是 `richTextWithMd`；本文没有找到名为 `richWithMd` 的对应字段证据。

选取某一种非空树时必须完整验证，不能将新旧树简单拼接，否则同一张图片或同一段文字可能重复发送。新表示为空时应允许使用旧表示；新表示存在但含不支持或损坏内容时不能偷偷当成已完整转发。

### Markdown 内五张图片的引用语法

官方 POST `md` 文档说明：Markdown 图片的链接地址可以直接使用图片 Key，例如 `img_v3_xxx`。该文档支持公开 API 中的图片扩展语法，但没有描述个人会话 `MarkdownProperty.images` 的 map key 如何与 `content` 内引用关联。

固定 LarkAgentX [compose.py](https://github.com/cv-cat/LarkAgentX/blob/d288ef4df8a42f6286c5506b7185f64430071490/larkx/proto/compose.py)的 `fill_markdown` 将 Markdown 转换为传统 `richText` 的段落、文字、链接、引用等节点；`message_request` 调用 `fill_markdown(req.content.richText, ...)`。它没有构建 `MarkdownProperty.images`，也没有提供该 map 与 Markdown 图片引用的解析器。[decoders.py](https://github.com/cv-cat/LarkAgentX/blob/d288ef4df8a42f6286c5506b7185f64430071490/larkx/proto/decoders.py)也没有提供该关联契约。

本次未获得“真实个人会话 MARKDOWN 节点内五张图片”的脱敏协议样本或上游解析源码来证明其引用匹配规则。因此不能凭 map 的遍历顺序配图，也不能用图片数量相同来证明对应关系。安全实现边界如下：

1. 正常 IMG 节点按明确的 `ImageProperty` 资源字段下载并转发。
2. MARKDOWN 仅文字时可保留正文；涉及图片时必须有经过验证的、精确的引用到资源映射规则，并保留原文顺序。
3. 图片资源无法可靠关联时，明确记录“不支持的图片引用结构”并保留待处理状态；不能只发送 Markdown 字符串、图片占位文字或部分图片后将整条标记完成。
4. 不下载任意 Markdown 外网 URL 来假装获得同一资源，不把 map 中所有图片无条件追加到末尾。

2.0.12 实现受限的标准 inline 图片语法 `![说明](引用)`：仅在正文引用精确匹配当前 `images` map 的别名或 `ImageSetV2.imageKey`，且资源标识有效时提取图片。支持尖括号包裹的标识及可选标题，按正文顺序展开；代码块、行内代码和转义标记保留文字。未引用缓存不会追加，重复引用保留独立的稳定叶标识。外部 URL、本地路径、引用式图片、HTML 图片、歧义匹配、缺失资源及不明确的缩进结构会明确失败。真实 protobuf 夹具验证该受限语法，不代表已经获得用户那条消息的私有结构或覆盖全部 Markdown 语法。

## 4. 图片必须使用真实资源和正确上下文

飞书[获取消息中的资源文件](https://open.feishu.cn/document/server-docs/im-v1/message/get-2?lang=zh-CN)明确：`message_id` 和 `file_key` 必须匹配；图片和富文本中的图片使用 `type=image`，文件、音频、视频使用 `type=file`。公开接口错误码 `234043` 明列不支持合并转发消息（含子消息）及消息卡片。因此，不能承诺靠该公开接口兜底下载所有合并记录、卡片或表情包。

公开接口还规定：图片整体下载需小于 100 MB；大文件下载需要 Range，单次分片不超过 32 MB。这些是该 OpenAPI 的条件，不是本项目个人通道或 QQ 的发送上限。

本项目个人通道以 LarkAgentX 的鉴权下载器为准，必须保留当前账号以及原始消息、会话、资源标识和所需加密信息，校验实际字节和大小后交给 QQ。资源 Key 不是可直接发送到 QQ 的图片 URL。下载失败必须记为可重试失败并继续处理后续图片；不能变成成功发送一段 `[图片]`。

本项目的图片、分组数量及总字节上限属于本地资源保护策略，不能描述为官方平台限制。分组还需考虑 Base64 约增加三分之一的请求体开销。达到本地上限应明确拆分或报错，不能无提示截断。

## 5. OneBot 与 SnowLuma 的多图和回执

[OneBot v11 API](https://11.onebot.dev/api)允许 `message` 使用消息段数组。[消息段规范](https://11.onebot.dev/message/segment)定义 `image.data.file` 可使用文件 URI、HTTP URL 或 `base64://`；`reply.data.id` 使用 QQ 消息 ID。

SnowLuma [v2.4.2 message-parser.ts](https://github.com/TomyJan/SnowLuma/blob/v2.4.2/packages/onebot/src/message-parser.ts)的 `parseMessage` 对数组逐项调用 `segmentToElement` 并按序加入结果；并没有在该解析器中发现“最多四张图片”的固定限制。[actions/message.ts](https://github.com/TomyJan/SnowLuma/blob/v2.4.2/packages/onebot/src/actions/message.ts)的 `send_group_msg` 接收 `message`，交给 `ctx.sendGroupMessage`，成功返回 `message_id`，该文件明确注明成功 ID 可以为负数。

这些源码为“同次发送多个 image 段”提供直接依据，但不证明任何图片数量、字节大小、网络和账号状态下都能送达。应坚持：

- 下载后再预留发送去重记录，避免在尚未取得图片时就永久压掉重试。
- 分组发送沿用原始图片叶的身份；重启后仅重试确定失败的叶，成功叶不重新发送。
- 超时、连接断开或缺少可确认回执时，结果属于“不确定”，不能再拆成单图盲目重发。
- 一次分组结果不确定时，该组所有参与叶都需保留不确定状态，并提示核对目标。
- 不同目标、不同发送账号独立记录结果，一个目标失败不能抹掉其他目标的成功记录。

[OneBot WebSocket 规范](https://11.onebot.dev/communication/ws.html)定义 API `echo` 用于关联请求响应，事件侧不处理响应数据；不能把它与 LarkAgentX 私有网关的 ACK 混为同一机制。

[OneBot 公开 API](https://11.onebot.dev/api/public)中 `get_forward_msg` 返回的标准字段是单数 `message`，数组内为 `node`。部分兼容实现使用 `messages`，读取适配应兼容已有实际结构并验证类型，不能仅因字段名不同静默丢掉合并记录。该能力与本轮 POST 多图发送是不同路径。

## 6. QQ 官方机器人能力：只作对照

[QQ 官方发送群聊消息](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_messages.post.html)使用 `/v2/groups/{group_openid}/messages`：`msg_type` 0 为文本、2 为 Markdown、7 为富媒体、8 为图文卡片；富媒体的 `media.file_info` 来自上传接口。`message_reference` 表示引用；`msg_id` 是被动回复上下文，文档给出的有效期为 5 分钟。`msg_seq` 与 `msg_id` 联合防重复。

[QQ 全量群消息事件](https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/group_message_create.html)要求开启“接收所有消息”，支持附件及递归 `msg_elements`，其中包含普通、卡片、并行、聊天记录和引用等类型。

[QQ 官方富媒体](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/rich-media.html)描述上传、取得 `file_info`、再发消息的流程，并给出软硬大小限制及过期规则。上述 `group_openid`、`msg_type=7`、`file_info`、授权和大小规则均不能直接改写为 SnowLuma 的数字群号、OneBot 图片段或个人账号的限制。

本轮没有把 QQ Bot v2 的图文卡片、Markdown、聊天记录模型强行塞进 OneBot，也不声称跨平台引用能够在没有源到目标消息 ID 映射的情况下完整保留跳转关系。

## 7. 接收确认、截断及防丢要求

飞书[事件概述](https://open.feishu.cn/document/server-docs/event-subscription-guide/overview?lang=zh-CN)描述官方开放平台需在 3 秒内完成成功确认，失败按 15 秒、5 分钟、1 小时、6 小时重试，最多 4 次；即使成功也可能重复，建议用 `uuid` 或 `event_id` 去重。这证明官方事件也需要幂等处理，但时间和 ACK 格式不能直接套到 LarkAgentX 私有网关。

个人通道实现必须遵守它自身固定协议：先将有效接收工作持久化再确认；解析或磁盘提交失败不能确认后静默丢弃；SID、批量 ID 的补拉允许部分失败且可恢复。收到消息、下载成功、目标发送成功是三个不同阶段，日志计数应能区分。

固定 LarkAgentX descriptor 存在 `Message.isTruncated`，但审计时上游 `decoders.message_dict` 转换没有保留该字段。此发现不能证明用户此次五图必定因截断丢失，但属于需要处理的真实风险：截断消息必须保留标记并按消息 ID 补拉完整正文；补拉后仍截断、未返回或结构不完整时，明确等待重试或显示错误，不能将少量已解析叶代表整条完成。

### 2.0.13 系统通知

固定版本的 `proto.proto` 将 `Message.Type.SYSTEM` 定义为 6，群分享类型为 9。`decoders.py` 对外层类型 6 解码 `SystemContent`，用 `typeNum` 生成 `[系统消息] type=…` 摘要。摘要中的 9 不是外层消息类型，也不足以判断通知的具体事件。桥接按外层类型过滤，保留同样字样的普通聊天文字。通知可确认接收；批量读取跳过通知时仍记其消息 ID 已读取，避免重复补拉。

### 2.0.14 独立图片、位置补收与内嵌表情

连续独立图片是多条消息，不能只用一条富文本的图片节点数判断是否收齐。已映射的飞书群首次检查时保存当前会话位置作为起点；后续定时、重连时按位置补读。查到的消息、仍缺失的位置和读取进度一起落盘。按账号、会话与规则版本隔离旧结果，不因重新连接或修改映射向新目标重放旧内容。缺失位置或内容未返回时保留待查；平台历史只能作为可恢复来源，不能保证取回所有消息。

固定版协议的 `EmotionProperty` 提供内嵌表情的名称，普通 TEXT、POST 和嵌套合并记录均从原始节点解析。可识别的名称转为可见符号，保留重复节点和图文顺序；用户自己输入的占位字样仍是普通文字。独立贴纸的资源传输与内嵌表情分开处理。SID 请求的 `sids` 按真实描述符使用字符串列表。图片诊断不阻塞接收确认，确认仍在 SQLite 写入成功后发送。

## 8. 本轮必须覆盖的离线回归

下列为实现和验收要求，不代表本文件已经执行或通过这些测试：

- 同一 POST 五张正常 IMG 节点：取得五份真实图片数据；QQ 同条多图，顺序正确，无摘要占位。
- 嵌套容器、普通树与非空 `richTextWithMd` 树：只选一份有效表示，避免遗漏或重复；不能据截图伪造其节点类型。
- MARKDOWN 标准 inline 图片精确匹配资源时按原序展开；引用结构不支持或不可靠时明确失败，不假成功；纯文字、代码与转义独立覆盖。
- 第三张下载确定失败：其余图片和后续正文继续；重试只补失败叶。
- QQ 确定拒绝与回执超时：前者可恢复，后者整组保留不确定且不拆分重发。
- 多目标、账号切换、重启恢复、用户过滤规则：相互隔离，不重复成功叶，不跨原始文本或视频边界组合图片。
- 推送 `isTruncated`、批量只带最后一条、SID 补拉部分返回：完整补拉前不能标完成。
- 接收前入库失败、发送成功后诊断写失败及取消任务：不制造已确认却无待处理记录，也不把已发送结果变成可盲重试。

## 9. 取证限制

上述飞书、QQ 官方文档和 OneBot 页面均已抓取正文确认；部分飞书 JSON 数组在网页提取结果中排版缺失，因此本文引用字段说明和原文规则，不复制损坏的 JSON 示例。

SnowLuma 当前文档地址 `https://snowluma.github.io/zh/docs/api` 本次返回 404；原 SnowLuma 仓库/部分旧文档链接也不可用。本次改用用户指定的 TomyJan 镜像固定 `v2.4.2` 源码确认解析和动作合同，不把 Release 的“Sync Official”说明当成完整功能保证。

未找到能公开证明私有 `MarkdownProperty.images` 引用匹配的文档或上游解析器；未进行真实账号发消息验证。新增支持应以脱敏实际结构、离线固定样本和明确发送回执补足证据，不以猜测扩展协议范围。
