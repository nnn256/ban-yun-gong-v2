const UI_VERSION = '20261118-mapping-17'

const state = {
  status: null,
  rules: [],
  savedRules: [],
  savingRules: false,
  filter: 'all',
  page: new URLSearchParams(location.search).get('page') || localStorage.getItem('mover-v2-page') || 'overview',
  toastTimer: null,
  larkxQrTimer: null,
  mappingDiscovery: null,
  mappingDiscoveryPromise: null,
  mappingEditingIndices: [],
  mappingSelfEdited: false,
  bridgeEnabled: null,
}

const contentTypeOptions = [
  { value: 'text', name: '文字' },
  { value: 'emoji', name: '表情包' },
  { value: 'image', name: '图片' },
  { value: 'video', name: '视频' },
  { value: 'audio', name: '语音' },
  { value: 'file', name: '文件' },
]
const defaultContentTypes = contentTypeOptions.map(option => option.value)

const $ = (selector, root = document) => root.querySelector(selector)
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)]
const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, char => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
})[char])

function ensureMatchingPage() {
  if (document.documentElement.dataset.workbenchUi === UI_VERSION) return true
  const url = new URL(location.href)
  // An existing tab may still have the previous HTML while loading new JS.
  // Refresh the complete document once before binding to the current fields.
  if (url.searchParams.get('ui') !== UI_VERSION) {
    url.searchParams.set('ui', UI_VERSION)
    location.replace(url.href)
  } else {
    const notice = $('#toast') || document.body.appendChild(document.createElement('div'))
    notice.textContent = '工作台页面与程序版本不一致，请关闭此页后重新打开工作台。'
    notice.className = 'toast error'
    notice.hidden = false
  }
  return false
}

async function request(path, options = {}) {
  const response = await fetch(path, {
    cache: 'no-store',
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options,
  })
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(payload.error || `请求失败 (${response.status})`)
  return payload
}

function toast(message, isError = false) {
  const node = $('#toast')
  node.textContent = message
  node.classList.toggle('error', isError)
  node.hidden = false
  clearTimeout(state.toastTimer)
  state.toastTimer = setTimeout(() => { node.hidden = true }, 3400)
}

function routeTarget(route) {
  const parts = String(route.target_umo || '').split(':')
  const platform = parts[0] === 'larkx'
    ? 'larkx'
    : 'aiocqhttp'
  return {
    platformId: parts[0] === 'larkx' ? 'default' : (parts[0] || '—'),
    messageType: parts[1] || '',
    sessionId: parts.slice(2).join(':') || '—',
    platform,
  }
}

function platformLabel(platform) {
  return platform === 'aiocqhttp' ? 'QQ' : platform === 'larkx' ? '飞书个人号' : platform === 'lark' ? '飞书机器人' : String(platform || '平台')
}

function routeIdentity(route) {
  return [route.source_platform, route.source_platform_id, route.source_account_id || '', route.source_session_id, route.source_message_type, route.target_umo, route.target_account_id || ''].join('|')
}

function setPage(page) {
  const allowed = ['overview', 'setup', 'rules', 'settings']
  state.page = allowed.includes(page) ? page : 'overview'
  localStorage.setItem('mover-v2-page', state.page)
  $$('.view').forEach(node => {
    const active = node.id === `page-${state.page}`
    node.classList.toggle('active', active)
    node.hidden = !active
  })
  $$('.nav-item').forEach(node => {
    const active = node.dataset.page === state.page
    node.classList.toggle('active', active)
    if (active) node.setAttribute('aria-current', 'page')
    else node.removeAttribute('aria-current')
  })
  $('#currentPageLabel').textContent = { overview: '运行总览', setup: '接入向导', rules: '群映射', settings: '本地设置' }[state.page]
}

function setPill(status, text) {
  const pill = $('#astrbotStatus')
  pill.className = `status-pill ${status}`
  $('span:last-child', pill).textContent = text
}

function setAdapterState(key, value) {
  const map = {
    connected: { label: '已连接', cls: 'good' },
    verifying: { label: '已连接 · 身份待确认', cls: 'warn' },
    identity_unverified: { label: '账号身份待确认', cls: 'warn' },
    disconnected: { label: '未连接', cls: 'warn' },
    unavailable: { label: '当前不可用', cls: 'bad' },
    not_configured: { label: '未配置', cls: 'warn' },
    waiting: { label: key === 'qq' ? '等待 OneBot 连接' : '等待飞书扫码', cls: 'warn' },
    stopped: { label: '未启动', cls: '' },
  }
  const descriptor = map[value] || { label: '待确认', cls: '' }
  $(`#${key}State`).textContent = descriptor.label
  $(`#${key}Dot`).className = `state-dot ${descriptor.cls}`.trim()
  if (key === 'feishu' && $('#feishuAuth')) $('#feishuAuth').textContent = value === 'connected' ? 'LarkAgentX 监听中' : value === 'waiting' ? '需要扫码登录' : '未运行'
}

function localHttpUrl(value) {
  try {
    const url = new URL(String(value || ''))
    if (url.protocol === 'http:' && ['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname)) return url.href
  } catch (_) { /* Only export verified loopback links. */ }
  return ''
}

function setDataList(id, values) {
  const list = $(`#${id}`)
  if (!list) return
  list.replaceChildren(...values.map(({ value, label }) => {
    const option = document.createElement('option')
    option.value = String(value || '')
    option.label = String(label || value || '')
    return option
  }).filter(option => option.value))
}

function renderQqRuntime(status) {
  const runtime = status.runtime || {}
  const qq = runtime.qq || {}
  const snow = runtime.snowluma || {}
  const qqAccounts = Array.isArray(qq.accounts) ? qq.accounts : []
  const snowAccounts = Array.isArray(snow.accounts) ? snow.accounts : []
  const accounts = new Map()
  for (const account of qqAccounts) {
    const id = String(account.user_id || '')
    if (id) accounts.set(id, { ...account, snow: snowAccounts.find(item => String(item.uin) === id) })
  }
  for (const account of snowAccounts) {
    const id = String(account.uin || '')
    if (id && !accounts.has(id)) accounts.set(id, { user_id: id, nickname: account.nickname, state: 'waiting', connected: false, identity_verified: false, groups: [], snow: account })
  }
  const accountRows = [...accounts.values()]
  const verified = accountRows.filter(account => account.identity_verified === true)
  const groupCount = verified.reduce((count, account) => count + (Array.isArray(account.groups) ? account.groups.length : 0), 0)
  const title = accountRows.length
    ? `本机识别到 ${accountRows.length} 个 QQ 账号 · ${verified.length} 个 OneBot 已验证`
    : '尚未取得当前 QQ 登录账号'
  $('#qqAccountSummary').textContent = `${title}${verified.length ? ` · 已读取 ${groupCount} 个群` : ''}`
  $('#qqIdentityState').textContent = verified.length ? `${verified.length}/${accountRows.length} 个已验证` : qq.connected === true ? '待验证' : '未确认'
  const accountList = $('#qqAccountsList')
  if (accountList) accountList.replaceChildren(...accountRows.map(account => {
    const row = document.createElement('div')
    row.className = 'qq-account-row'
    const identity = document.createElement('span')
    identity.textContent = `${account.nickname || 'QQ'} · ${account.user_id}`
    const status = document.createElement('span')
    status.className = account.identity_verified ? 'account-state good' : 'account-state warn'
    status.textContent = account.identity_verified
      ? `OneBot 已验证 · ${account.groups?.length || 0} 群`
      : account.snow ? 'SnowLuma 已登录 · 等待 OneBot' : '等待账号连接'
    row.append(identity, status)
    return row
  }))
  const qqActivity = qq
  $('#qqForwardActivity').textContent = `QQ 群事件 ${qqActivity.received_count || 0} · 发送接口接受 ${qqActivity.sent_count || 0} · 失败 ${qqActivity.failed_count || 0}`
  setAdapterState('qq', qq.state || status.platforms?.qq)
  const snowUrl = localHttpUrl(snow.url || snow.webui_url || status.snowluma?.url)
  const snowLink = $('#openSnowLumaLocal')
  snowLink.href = snowUrl || 'http://127.0.0.1:5100/'
  snowLink.textContent = snowUrl ? `${snowUrl} ↗` : 'SnowLuma 本机地址待确认'
  snowLink.setAttribute('aria-disabled', snowUrl ? 'false' : 'true')
  snowLink.title = snowUrl ? '在本机浏览器打开当前 SnowLuma WebUI' : '等待插件导出当前实际端口'
  $('#qqOneBotAddress').textContent = 'ws://127.0.0.1:6199/ws'
  $('#launchLocalQq').disabled = Boolean(state.launchingQq)
}

function renderStatus(status) {
  state.status = status
  const app = status.astrbot || {}
  const running = Boolean(app.running)
  const rootReady = Boolean(app.root_ready)
  const managed = Boolean(app.managed_here)

  if (running) setPill('status-good', 'AstrBot 正在运行')
  else if (app.installed && rootReady) setPill('status-warn', 'AstrBot 已安装 · 未启动')
  else setPill('status-muted', 'AstrBot 待初始化')

  $('#runtimeTag').textContent = running ? '正在运行' : app.installed ? '已安装' : '未安装'
  $('#runtimeTag').className = `mini-tag ${running ? 'tag-good' : app.installed ? 'tag-warning' : 'tag-neutral'}`
  $('#runtimePath').textContent = status.settings.astrbot_root
  $('#runtimeDetail').textContent = running
    ? `本机管理页已启动。${managed ? '由搬运工V2工作台启动。' : '该实例由其他进程启动。'}`
    : app.installed ? 'AstrBot 已安装，平台消息配置完成后即可启动。' : '运行环境尚未就绪，请检查本机 AstrBot 安装目录。'

  $('#topStart').disabled = running
  $('#runtimeStart').disabled = running || !app.installed || !rootReady
  $('#runtimeStop').disabled = !running || !managed
  $('#openAstrbot').href = app.url || 'http://127.0.0.1:6185/'
  $('#openAstrbot').setAttribute('aria-disabled', running ? 'false' : 'true')
  $('#installPlugin').disabled = !rootReady
  $('#installPluginSettings').disabled = !rootReady

  state.bridgeEnabled = typeof status.bridge_enabled === 'boolean' ? status.bridge_enabled : null
  const bridgeToggle = $('#bridgeToggle')
  bridgeToggle.disabled = state.bridgeEnabled === null || !running
  bridgeToggle.setAttribute('aria-pressed', String(state.bridgeEnabled === true))
  bridgeToggle.textContent = state.bridgeEnabled === null ? '转发状态未知' : state.bridgeEnabled ? '停止全部转发' : '恢复全部转发'
  bridgeToggle.title = state.bridgeEnabled === null ? '等待桥接插件状态' : state.bridgeEnabled ? '暂停所有群映射的消息转发' : '恢复所有已启用群映射'

  renderQqRuntime(status)
  const larkx = status.larkagentx || {}
  const feishuRuntime = status.runtime?.feishu || {}
  const feishuConnected = feishuRuntime.connected === true || larkx.bridge_state === 'running'
  setAdapterState('feishu', feishuConnected ? 'connected' : 'waiting')
  $('#feishuDesktopState').textContent = larkx.desktop?.running
    ? `客户端运行中 (${larkx.desktop.process_count})`
    : '客户端未运行'
  const receivedCount = Number(larkx.monitor?.received_count || 0)
  const acceptedCount = Number(larkx.monitor?.sent_count || 0)
  const failedCount = Number(larkx.monitor?.failed_count || 0)
  const lastReceived = larkx.monitor?.last_received_at
    ? new Date(larkx.monitor.last_received_at).toLocaleString('zh-CN', { hour12: false })
    : ''
  $('#feishuMonitorActivity').textContent = larkx.bridge_state === 'running'
    ? `实时监听已连接 · 收到 ${receivedCount} 条${lastReceived ? ` · 最近 ${lastReceived}` : ''} · 发送接口接受 ${acceptedCount} 条 · 失败 ${failedCount} 条。agent backend=none。`
    : `本机飞书客户端${larkx.desktop?.running ? '已识别' : '未运行'}；LarkAgentX 监听状态：${larkx.bridge_message || larkx.bridge_state || '等待扫码'}。`
  const snowlumaPlugin = status.snowluma_plugin || {}
  $('#openSnowLumaExtension').href = snowlumaPlugin.astrbot_url || 'http://127.0.0.1:6185/#/extension/plugins'
  $('#openSnowLumaExtensionWizard').href = snowlumaPlugin.astrbot_url || 'http://127.0.0.1:6185/#/extension/plugins'
  $('#navRuleCount').textContent = String(status.rules?.enabled ?? 0)
  $('#enabledRouteSummary').textContent = `${status.rules?.enabled ?? 0} 条启用`
  $('#configPath').textContent = status.plugin?.config_path || '等待 AstrBot 目录'

  const setupStates = [
    Boolean(app.running && status.plugin?.installed),
    status.runtime?.qq?.identity_verified === true,
    feishuConnected && receivedCount > 0,
    (status.rules?.enabled ?? 0) > 0,
  ]
  $('#setupProgress').textContent = `${setupStates.filter(Boolean).length} / 4 已完成`
  setWizardStep('wizardRuntimeState', setupStates[0], '运行中', '待启动')
  setWizardStep('wizardQqState', setupStates[1], '已连接', snowlumaPlugin.installed ? '已安装 · 等待部署' : '待安装扩展')
  const qrPending = ['starting', 'waiting_scan', 'awaiting_confirm'].includes(state.larkxQrStatus || '')
  setWizardStep('wizardFeishuState', setupStates[2], `监听中 · 收到 ${receivedCount}`, qrPending ? (state.larkxQrStatus === 'awaiting_confirm' ? '手机确认中' : '等待扫码') : larkx.bridge_state === 'running' ? '监听中 · 等待新消息' : (larkx.has_credentials ? '已登录 · 正在连接' : '待扫码'))
  $('#startLarkXQr').disabled = qrPending
  $('#startLarkXQr').textContent = qrPending ? '等待扫码完成' : feishuConnected ? '重新扫码登录' : '生成登录二维码'
  if (feishuConnected && !qrPending) {
    $('#larkxQrPanel').hidden = false
    $('#larkxQrImage').hidden = true
    $('#larkxQrStatus').textContent = '飞书账号已登录'
    $('#larkxQrMessage').textContent = larkx.bridge_state === 'running' ? 'LarkAgentX 监听已连接；agent backend=none。' : (larkx.bridge_message || '本地消息监听正在连接；agent backend=none。')
    $('#larkxQrCountdown').textContent = larkx.user_id ? `账号 ID：${larkx.user_id}` : ''
  }
  setWizardStep('wizardMappingState', setupStates[3], '已设置', '未设置')

  const notice = $('#bootstrapNotice')
  const pass = status.bootstrap?.password
  notice.hidden = !pass
  if (pass) {
    $('#bootstrapUsername').value = status.bootstrap.username || 'astrbot'
    if (document.activeElement !== $('#bootstrapPassword')) $('#bootstrapPassword').value = pass
  }

  const rootInput = $('#astrbotRoot')
  if (document.activeElement !== rootInput) rootInput.value = status.settings.astrbot_root
  const portInput = $('#astrbotPort')
  if (document.activeElement !== portInput) portInput.value = status.settings.astrbot_port
  renderBackground(status.background || {})
  $('#lastUpdated').textContent = `刚刚刷新 · ${new Date().toLocaleTimeString('zh-CN', { hour12: false })}`
}

function setWizardStep(id, done, doneLabel, pendingLabel) {
  const node = $(`#${id}`)
  node.textContent = done ? doneLabel : pendingLabel
  node.className = `mini-tag ${done ? 'tag-good' : 'tag-warning'}`
}

async function pollLarkXQr() {
  try {
    const qr = await request('/api/larkagentx/qr/status')
    state.larkxQrStatus = qr.status
    if (['starting', 'waiting_scan', 'awaiting_confirm', 'error'].includes(qr.status)) $('#larkxQrPanel').hidden = false
    if (qr.status === 'authenticated' || qr.status === 'error' || qr.status === 'expired' || qr.status === 'cancelled' || qr.status === 'not_started') {
      clearInterval(state.larkxQrTimer)
      state.larkxQrTimer = null
    }
    $('#larkxQrStatus').textContent = qr.status === 'awaiting_confirm' ? '已扫码，请在手机确认' : qr.status === 'authenticated' ? '登录成功' : qr.status === 'waiting_scan' ? '等待扫码' : qr.status === 'starting' ? '正在生成二维码' : qr.status === 'expired' ? '二维码已过期' : qr.status === 'error' ? '登录失败' : qr.status === 'cancelled' ? '扫码已取消' : '等待扫码'
    $('#larkxQrMessage').textContent = qr.message || '使用飞书 App 扫描二维码。'
    const image = $('#larkxQrImage')
    if (qr.image_data) {
      const source = `data:image/png;base64,${qr.image_data}`
      if (image.getAttribute('src') !== source) image.setAttribute('src', source)
      image.hidden = false
    } else if (['expired', 'error', 'cancelled'].includes(qr.status)) {
      image.hidden = true
    }
    if (qr.status === 'authenticated') await refresh(false)
  } catch (error) {
    toast(error.message, true)
  }
}

function renderLogs(logs) {
  const list = $('#logList')
  if (!logs.length) {
    list.innerHTML = '<p class="empty-inline">AstrBot 启动后会在这里显示最近日志。</p>'
    return
  }
  list.replaceChildren(...logs.slice(-12).reverse().map(line => {
    const entry = document.createElement('div')
    entry.className = 'log-entry'
    const time = document.createElement('time')
    const found = String(line).match(/\b(\d{2}:\d{2}:\d{2})\b/)
    time.textContent = found?.[1] || '日志'
    const text = document.createElement('span')
    text.textContent = String(line).trim().slice(-340)
    entry.append(time, text)
    return entry
  }))
}

function renderBackground(background) {
  const text = background.running ? '守护进程运行中，关闭浏览器仍会监控。' : background.enabled ? '后台恢复已启用，守护进程尚未就绪。' : '后台恢复已关闭。'
  if ($('#backgroundSummary')) $('#backgroundSummary').textContent = text
  if (!state.backgroundEditing) {
    if ($('#backgroundEnabled')) $('#backgroundEnabled').checked = background.enabled === true
    if ($('#backgroundSleep')) $('#backgroundSleep').checked = background.keep_awake === true
    if ($('#backgroundAutostart')) $('#backgroundAutostart').checked = background.autostart === true
  }
  if ($('#backgroundDetail')) $('#backgroundDetail').textContent = `${text} ${background.keep_awake_active ? '当前已请求系统保持唤醒，显示器仍可关闭。' : '系统唤醒状态以守护进程为准。'}`
}

async function refreshLogs(older = false) {
  if (state.logsLoading || !$('#logList')) return
  state.logsLoading = true
  try {
    const selectedFile = $('#logFile').value
    const params = new URLSearchParams({ component: $('#logComponent').value, level: $('#logLevel').value, q: $('#logSearch').value, limit: '1000', file: selectedFile })
    if (older === true && state.logBefore != null) params.set('before', String(state.logBefore))
    for (const [key, id] of [['since', 'logSince'], ['until', 'logUntil']]) {
      const value = $(`#${id}`).value
      if (value) params.set(key, new Date(value).toISOString())
    }
    const result = await request(`/api/logs?${params}`)
    const fileSelect = $('#logFile')
    const defaultOption = document.createElement('option')
    defaultOption.value = ''; defaultOption.textContent = '所有组件最新日志'
    fileSelect.replaceChildren(defaultOption, ...(result.files || []).map(file => {
      const option = document.createElement('option')
      option.value = file.id; option.textContent = `[${file.component}] ${file.name}`
      return option
    }))
    if ([...fileSelect.options].some(option => option.value === selectedFile)) fileSelect.value = selectedFile
    state.logBefore = result.next_before
    $('#olderLogs').disabled = !selectedFile || result.next_before == null
    state.logEntries = result.entries || []
    const list = $('#logList')
    const atBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 50
    list.replaceChildren(...state.logEntries.map(entry => {
      const node = document.createElement('div')
      node.className = `log-entry log-${String(entry.level).toLowerCase()}`
      const time = document.createElement('time')
      time.textContent = new Date(entry.time).toLocaleString()
      const text = document.createElement('span')
      text.textContent = `[${entry.component} · ${entry.level}] ${entry.text}`
      node.append(time, text)
      return node
    }))
    if (!state.logEntries.length) {
      const empty = document.createElement('p')
      empty.className = 'empty-inline'
      empty.textContent = '没有匹配的日志；组件启动并产生日志后会显示在这里。'
      list.append(empty)
    }
    if (atBottom) list.scrollTop = list.scrollHeight
    $('#logQueryStatus').textContent = `${new Date().toLocaleTimeString()} · ${state.logEntries.length} 条 · ${result.files?.length || 0} 个日志文件${result.bounded ? ' · 选择文件后可查询更早日志' : ''}`
  } catch (error) {
    $('#logQueryStatus').textContent = `日志读取失败：${error.message}`
  } finally {
    state.logsLoading = false
  }
}

function endpointDisplayName(platform, instance, accountId, sessionId) {
  if (platform === 'aiocqhttp') {
    const accounts = (state.status?.runtime?.qq?.accounts || []).filter(account => (!instance || account.platform_id === instance) && (!accountId || String(account.user_id) === String(accountId)))
    const group = accounts.flatMap(account => account.groups || []).find(item => String(item.group_id) === String(sessionId))
    return group?.group_name || '名称未读取'
  }
  const chats = [...(state.status?.larkagentx?.chats || []), ...(state.mappingDiscovery?.feishu_chats || [])]
  return chats.find(chat => String(chat.chat_id) === String(sessionId) && chat.name)?.name || '名称未读取'
}

function routeEndpoint(platform, id, sessionId, accountId = '') {
  const name = platformLabel(platform)
  const glyph = platform === 'aiocqhttp' ? 'Q' : '飞'
  const brand = platform === 'aiocqhttp' ? 'qq-brand' : 'feishu-brand'
  const identity = accountId ? `${id} · 账号 ${accountId}` : id
  return `<div class="route-endpoint"><span class="platform-brand ${brand}">${glyph}</span><span class="route-endpoint-copy"><strong>${escapeHtml(name)} · ${escapeHtml(endpointDisplayName(platform, id, accountId, sessionId))}</strong><code>${escapeHtml(identity || '未指定实例')} · ${escapeHtml(sessionId || '未指定会话')}</code></span></div>`
}

function renderOverviewRoutes() {
  const host = $('#overviewRoutes')
  const routes = state.rules.filter(rule => rule.enabled !== false).slice(0, 5)
  if (!routes.length) {
    host.innerHTML = '<div class="empty-state"><span class="empty-symbol" aria-hidden="true">+</span><strong>还没有启用的映射</strong><p>独立选择来源和目标平台与会话；双向会生成两个方向。</p></div>'
    return
  }
  host.innerHTML = routes.map(route => {
    const target = routeTarget(route)
    return `<div class="route-row">${routeEndpoint(route.source_platform, route.source_platform_id, route.source_session_id, route.source_account_id)}<span class="route-arrow" aria-label="转发到">→</span>${routeEndpoint(target.platform, target.platformId, target.sessionId, route.target_account_id)}<span class="mini-tag tag-good route-enabled">已启用</span></div>`
  }).join('')
}

function renderRules() {
  const list = $('#rulesTable')
  const all = state.rules
  const enabled = all.filter(route => route.enabled !== false)
  $('#allCount').textContent = String(all.length)
  $('#enabledCount').textContent = String(enabled.length)
  $('#disabledCount').textContent = String(all.length - enabled.length)
  $('#navRuleCount').textContent = String(enabled.length)
  $('#enabledRouteSummary').textContent = `${enabled.length} 条启用`
  const filtered = all.map((route, index) => ({ route, index })).filter(({ route }) => {
    return state.filter === 'all' || (state.filter === 'enabled' ? route.enabled !== false : route.enabled === false)
  })
  if (!filtered.length) {
    list.innerHTML = '<div class="empty-state"><span class="empty-symbol" aria-hidden="true">↔</span><strong>没有符合条件的映射</strong><p>选择任意两个 QQ 或飞书会话，添加单向或双向规则。</p></div>'
    renderOverviewRoutes()
    return
  }
  const rows = filtered.map(({ route, index }) => {
    const target = routeTarget(route)
    const enabledState = route.enabled !== false
    return `<tr>
      <td><div class="table-platform"><span class="platform-brand ${route.source_platform === 'aiocqhttp' ? 'qq-brand' : 'feishu-brand'}">${route.source_platform === 'aiocqhttp' ? 'Q' : '飞'}</span><span>${escapeHtml(platformLabel(route.source_platform))}<code class="table-id">${escapeHtml(route.source_platform_id)}${route.source_account_id ? ` · QQ ${escapeHtml(route.source_account_id)}` : ''}</code></span></div></td>
      <td>${escapeHtml(endpointDisplayName(route.source_platform, route.source_platform_id, route.source_account_id, route.source_session_id))}<code class="table-id">${escapeHtml(route.source_session_id)}</code></td>
      <td class="table-direction">→</td>
      <td><div class="table-platform"><span class="platform-brand ${target.platform === 'aiocqhttp' ? 'qq-brand' : 'feishu-brand'}">${target.platform === 'aiocqhttp' ? 'Q' : '飞'}</span><span>${escapeHtml(platformLabel(target.platform))}<code class="table-id">${escapeHtml(target.platformId)}${route.target_account_id ? ` · QQ ${escapeHtml(route.target_account_id)}` : ''}</code></span></div></td>
      <td>${escapeHtml(endpointDisplayName(target.platform, target.platformId, route.target_account_id, target.sessionId))}<code class="table-id">${escapeHtml(target.sessionId)}</code></td>
      <td class="route-content-types">${escapeHtml(routeContentTypeLabel(route))}</td>
      <td><button class="toggle" type="button" data-toggle-route="${index}" aria-pressed="${enabledState}" aria-label="${enabledState ? '停用' : '启用'} ${escapeHtml(route.name || '映射')}"></button></td>
      <td><button class="table-action edit" type="button" data-edit-route="${index}">编辑</button><button class="table-action" type="button" data-toggle-self="${index}" aria-pressed="${route.include_self === true}">${route.include_self === true ? '转发本人：开' : '转发本人：关'}</button><button class="table-action" type="button" data-remove-route="${index}">删除</button></td>
    </tr>`
  }).join('')
  list.innerHTML = `<table class="rules-table"><thead><tr><th>来源平台</th><th>来源会话</th><th aria-label="方向"></th><th>目标平台</th><th>目标会话</th><th>转发类型</th><th>状态</th><th>本人消息 / 操作</th></tr></thead><tbody>${rows}</tbody></table>`
  renderOverviewRoutes()
}

async function refresh(loadRules = false) {
  try {
    const status = await request('/api/status')
    renderStatus(status)
    if (loadRules) {
      const config = await request('/api/rules')
      state.savedRules = state.rules = Array.isArray(config.rules) ? config.rules : []
      renderRules()
    }
  } catch (error) {
    setPill('status-bad', '工作台连接异常')
    toast(error.message, true)
  }
}

async function postAction(path, payload = {}, successText = '') {
  document.body.classList.add('loading')
  try {
    const result = await request(path, { method: 'POST', body: JSON.stringify(payload) })
    if (successText) toast(successText)
    await refresh(path === '/api/rules')
    return result
  } catch (error) {
    toast(error.message, true)
    return null
  } finally {
    document.body.classList.remove('loading')
  }
}

async function saveMappings(rules, successText, { keepTableEdits = false } = {}) {
  state.savingRules = true
  $('#submitMapping').disabled = true
  $('#saveRules').disabled = true
  $('#rulesTable').setAttribute('aria-busy', 'true')
  try {
    const saved = await request('/api/rules', { method: 'POST', body: JSON.stringify({ rules }) })
    if (keepTableEdits && state.rules !== state.savedRules) {
      // The row switch saves only the self flag. Keep staged status/deletion edits in the table.
      const flags = new Map(saved.rules.map(route => [routeIdentity(route), route.include_self]))
      state.rules = state.rules.map(route => ({ ...route, include_self: flags.get(routeIdentity(route)) === true }))
    } else {
      state.rules = saved.rules
    }
    state.savedRules = saved.rules
    renderRules()
    toast(successText)
  } finally {
    state.savingRules = false
    $('#submitMapping').disabled = false
    $('#saveRules').disabled = false
    $('#rulesTable').setAttribute('aria-busy', 'false')
  }
}

function endpoint(side) {
  return {
    platform: $(`#${side}Platform`).value,
    instance: $(`#${side}Instance`).value.trim(),
    account: $(`#${side}Account`).value,
    messageType: $(`#${side}MessageType`).value,
    session: $(`#${side}Session`).value.trim(),
  }
}

function endpointIdentity(value) {
  return [value.platform, value.instance, value.platform === 'aiocqhttp' ? value.account : '', value.messageType, value.session].join('|')
}

function buildRouteFromEndpoints(source, target, includeSelf = false, contentTypes = defaultContentTypes) {
  return {
    name: `${platformLabel(source.platform)} ${source.session} → ${platformLabel(target.platform)} ${target.session}`,
    enabled: true,
    include_self: includeSelf === true,
    source_platform: source.platform,
    source_platform_id: source.instance,
    source_message_type: source.messageType,
    source_session_id: source.session,
    source_account_id: source.platform === 'aiocqhttp' ? source.account : '',
    target_account_id: target.platform === 'aiocqhttp' ? target.account : '',
    target_umo: `${target.platform === 'larkx' ? 'larkx' : target.instance}:${target.messageType}:${target.session}`,
    content_types: contentTypeOptions.filter(option => contentTypes.includes(option.value)).map(option => option.value),
  }
}

function routeEndpoints(route) {
  const target = routeTarget(route)
  return [
    { platform: route.source_platform, instance: route.source_platform_id, account: route.source_account_id || '', messageType: route.source_message_type, session: route.source_session_id },
    { platform: target.platform, instance: target.platformId, account: route.target_account_id || '', messageType: target.messageType, session: target.sessionId },
  ]
}

function routeContentTypeLabel(route) {
  const selected = Array.isArray(route.content_types) ? route.content_types : defaultContentTypes
  return contentTypeOptions.filter(option => selected.includes(option.value)).map(option => option.name).join('、') || '未选择'
}

function endpointQqAccounts(side, discovery = state.mappingDiscovery) {
  const instance = $(`#${side}Instance`).value.trim()
  return (discovery?.qq_instances || []).filter(item => !instance || String(item.id) === instance).flatMap(item =>
    (item.accounts || []).map(account => ({ ...account, platform_id: item.id }))
  )
}

function renderSessionPicker(side, values) {
  const query = $(`#${side}SessionSearch`).value.trim().toLocaleLowerCase()
  const selected = $(`#${side}Session`).value.trim()
  const picker = $(`#${side}SessionPicker`)
  const placeholder = document.createElement('option')
  placeholder.value = ''
  placeholder.textContent = '按群名选择；完整 ID 显示在下方'
  const unique = [...new Map(values.map(value => [String(value.value), value])).values()]
  const nameKey = value => `${String(value.name || '').trim().toLocaleLowerCase()}|${value.kind || ''}`
  const nameCounts = new Map()
  for (const value of unique) nameCounts.set(nameKey(value), (nameCounts.get(nameKey(value)) || 0) + 1)
  const matches = unique.filter(value => `${value.name || ''} ${value.kind || ''} ${value.value}`.toLocaleLowerCase().includes(query))
  const options = matches.map(value => {
    const option = document.createElement('option')
    option.value = String(value.value)
    const name = String(value.name || '').trim()
    const displayName = name && name !== option.value ? name : '名称未读取'
    const parts = [displayName, value.kind || '会话']
    if (!name || name === option.value || nameCounts.get(nameKey(value)) > 1) parts.push(`…${option.value.slice(-6)}`)
    option.textContent = parts.join(' · ')
    return option
  })
  const selectedItem = unique.find(value => String(value.value) === selected)
  if (selectedItem && !matches.some(value => String(value.value) === selected)) {
    const selectedOption = document.createElement('option')
    selectedOption.value = selected
    const name = String(selectedItem.name || '').trim()
    const displayName = name && name !== selected ? name : '名称未读取'
    selectedOption.textContent = `当前已选：${displayName} · ${selectedItem.kind || '会话'}`
    options.unshift(selectedOption)
  }
  picker.replaceChildren(placeholder, ...options)
  picker.value = options.some(option => option.value === selected) ? selected : ''
}

function renderEndpoint(side, savedAccount = '') {
  const discovery = state.mappingDiscovery || {}
  const value = endpoint(side)
  const isQq = value.platform === 'aiocqhttp'
  const accountSelect = $(`#${side}Account`)
  for (const suffix of ['Account', 'AccountLabel', 'AccountHelp']) $(`#${side}${suffix}`).hidden = !isQq
  accountSelect.required = isQq
  const instances = isQq ? discovery.qq_instances || [] : (discovery.feishu_accounts || [{id:'default'}])
  setDataList(`${side}InstanceOptions`, instances.map(item => ({ value: item.id, label: [item.name, item.id, item.user_id].filter(Boolean).join(' · ') })))
  if (!$(`#${side}Instance`).value.trim() && instances.length === 1) $(`#${side}Instance`).value = String(instances[0].id)
  if (!isQq && !$(`#${side}Instance`).value.trim()) $(`#${side}Instance`).value = 'default'
  if (isQq) {
    const selected = savedAccount || accountSelect.value
    const accounts = endpointQqAccounts(side, discovery)
    const option = document.createElement('option')
    option.value = ''
    option.textContent = accounts.length ? '选择用于转发的 QQ 账号' : '尚未发现在线 QQ 账号'
    const options = accounts.filter(account => account.user_id).map(account => {
      const node = document.createElement('option')
      node.value = String(account.user_id)
      const online = account.connected === true && account.identity_verified === true
      node.textContent = `${account.nickname || 'QQ'} (${account.user_id}) · ${online ? '在线 · 已验证' : '离线/未验证'}`
      node.disabled = !online && node.value !== selected
      return node
    })
    if (selected && !options.some(node => node.value === selected)) {
      const savedOption = document.createElement('option')
      savedOption.value = selected
      savedOption.textContent = `QQ (${selected}) · 当前选择，尚未验证`
      options.push(savedOption)
    }
    accountSelect.replaceChildren(option, ...options)
    accountSelect.value = options.some(node => node.value === selected && !node.disabled) ? selected : ''
    const allGroups = accounts.filter(account => account.connected === true && account.identity_verified === true).flatMap(account =>
      (account.groups || []).map(group => ({ ...group, account_id: String(account.user_id) }))
    )
    const owners = [...new Set(allGroups.filter(group => String(group.group_id) === value.session).map(group => group.account_id))]
    if (!accountSelect.value && owners.length === 1) accountSelect.value = owners[0]
    const groups = allGroups.filter(group => !accountSelect.value || group.account_id === accountSelect.value)
    $(`#${side}AccountHelp`).textContent = accountSelect.value ? `使用 QQ ${accountSelect.value}` : owners.length > 1 ? '此群有多个 QQ 账号，请选择账号。' : '选择登录账号；群归属唯一时自动选择。'
    $(`#${side}SessionLabel`).textContent = value.messageType === 'GroupMessage' ? 'QQ 群号' : 'QQ 好友号'
    renderSessionPicker(side, value.messageType === 'GroupMessage' ? groups.map(group => ({value:group.group_id,name:group.group_name,kind:'QQ群'})) : [])
    const group = groups.find(item => String(item.group_id) === value.session)
    $(`#${side}SessionHelp`).textContent = value.messageType === 'FriendMessage' ? '填写对方 QQ 号；从所选本机账号收发。' : group ? `已确认 QQ 群“${group.group_name || '名称未读取'}”；完整群号显示在上方。` : value.session ? '尚未在所选账号群列表确认此群，请核对账号与群号。' : `可选 ${groups.length} 个已验证群；先按群名筛选。`
  } else {
    const chats = (discovery.feishu_chats || []).filter(chat => !chat.message_type || chat.message_type === value.messageType)
    renderSessionPicker(side, chats.map(chat => ({value:chat.chat_id,name:chat.name,kind:value.messageType === 'GroupMessage' ? '飞书群' : '飞书私聊'})))
    $(`#${side}SessionLabel`).textContent = value.messageType === 'GroupMessage' ? '飞书群会话 ID（自动填入，可编辑）' : '飞书私聊会话 ID（自动填入，可编辑）'
    const chat = chats.find(item => String(item.chat_id) === value.session)
    $(`#${side}SessionHelp`).textContent = chat ? `已选择飞书会话“${chat.name || '名称未读取'}”；完整会话 ID 显示在上方。` : value.session ? '此 ID 不在当前账号已发现列表中，请确认账号可访问。' : `可选 ${chats.length} 个已发现会话；先按群名筛选。`
  }
}

function renderMappingSources() {
  renderEndpoint('source')
  renderEndpoint('target')
}

function discoveryCoverage(value) {
  if (typeof value === 'string') return value
  if (!value || typeof value !== 'object') return '待验证'
  return String(value.detail || value.status || value.state || '待验证')
}

async function loadMappingDiscovery({ refresh = false } = {}) {
  if (state.mappingDiscoveryPromise) return state.mappingDiscoveryPromise
  const button = $('#refreshMappingDiscovery')
  button.disabled = true
  $('#mappingDiscoveryStatus').textContent = '正在读取当前 QQ 账号、群列表与飞书会话…'
  state.mappingDiscoveryPromise = (async () => {
    try {
      const discovery = await request('/api/mapping/discovery', refresh ? { method: 'POST', body: '{}' } : {})
      if (!$('#mappingDialog').open) return
      state.mappingDiscovery = discovery
      const suggested = state.mappingEditingIndices.length ? {} : (discovery.suggested || {})
      for (const side of ['source', 'target']) {
        const isQq = $(`#${side}Platform`).value === 'aiocqhttp'
        const session = $(`#${side}Session`)
        const suggestion = isQq ? suggested.qq_group : suggested.feishu_chat
        if (!session.value.trim() && suggestion) session.value = String(suggestion)
      }
      renderMappingSources()
      if (suggested.qq_account) {
        for (const side of ['source', 'target']) {
          if ($(`#${side}Platform`).value !== 'aiocqhttp') continue
          const account = $(`#${side}Account`)
          if (!account.value && [...account.options].some(option => option.value === String(suggested.qq_account) && !option.disabled)) {
            account.value = String(suggested.qq_account)
            renderEndpoint(side)
          }
        }
      }
      $('#mappingDiscoveryStatus').textContent = `QQ：${discoveryCoverage(discovery.coverage?.qq)}；飞书：${discoveryCoverage(discovery.coverage?.feishu)}。${state.mappingEditingIndices.length ? '已保留当前表单设置。' : '自动读取仅填充空白字段。'}`
      const warnings = Array.isArray(discovery.warnings) ? discovery.warnings.map(String) : []
      $('#mappingDiscoveryWarnings').hidden = !warnings.length
      $('#mappingDiscoveryWarnings').textContent = warnings.join('；')
    } catch (error) {
      if ($('#mappingDialog').open) {
        $('#mappingDiscoveryStatus').textContent = `自动读取未完成：${error.message}。可保留现有输入并重试。`
        $('#mappingDiscoveryWarnings').hidden = false
        $('#mappingDiscoveryWarnings').textContent = '没有取得可验证的完整列表；手工输入不会被标为已验证。'
      }
    } finally {
      button.disabled = false
      state.mappingDiscoveryPromise = null
    }
  })()
  return state.mappingDiscoveryPromise
}

function routeReciprocalIndex(index) {
  const route = state.rules[index]
  if (!route) return -1
  const [source, target] = routeEndpoints(route)
  const reverseIdentity = routeIdentity(buildRouteFromEndpoints(target, source))
  return state.rules.findIndex((candidate, candidateIndex) => candidateIndex !== index && routeIdentity(candidate) === reverseIdentity)
}

function setMappingEndpoint(side, endpointValue) {
  $(`#${side}Platform`).value = endpointValue.platform
  $(`#${side}Instance`).value = endpointValue.instance
  $(`#${side}MessageType`).value = endpointValue.messageType
  $(`#${side}Session`).value = endpointValue.session
  $(`#${side}SessionSearch`).value = ''
  $(`#${side}Account`).value = ''
  renderEndpoint(side, endpointValue.account)
}

function showMappingSelfState() {
  const [forwardIndex, reverseIndex] = state.mappingEditingIndices
  const checkbox = $('#mappingIncludeSelf')
  const forwardSelf = state.rules[forwardIndex]?.include_self === true
  const reverse = $('#mappingDirection').value === 'both' ? state.rules[reverseIndex] : null
  checkbox.indeterminate = Boolean(reverse && forwardSelf !== (reverse.include_self === true))
  checkbox.checked = !checkbox.indeterminate && forwardSelf
}

function populateMappingEditor() {
  const indices = state.mappingEditingIndices
  const route = state.rules[indices[0]]
  if (!route) return
  const [source, target] = routeEndpoints(route)
  setMappingEndpoint('source', source)
  setMappingEndpoint('target', target)
  const selectedTypes = new Set(Array.isArray(route.content_types) ? route.content_types : defaultContentTypes)
  $$('#contentTypeOptions input[name="contentType"]').forEach(input => { input.checked = selectedTypes.has(input.value) })
  $('#mappingDirection').value = indices.length > 1 ? 'both' : 'forward'
  showMappingSelfState()
  $('#mappingDialogTitle').textContent = indices.length > 1 ? '编辑双向映射' : '编辑转发规则'
  $('#submitMapping').textContent = '保存修改'
}

function openMappingDialog(routeIndex = null) {
  if (state.savingRules) return
  $('#dialogError').hidden = true
  $('#mappingDiscoveryWarnings').hidden = true
  state.mappingEditingIndices = []
  state.mappingSelfEdited = false
  if (Number.isInteger(routeIndex) && state.rules[routeIndex]) {
    const reciprocal = routeReciprocalIndex(routeIndex)
    state.mappingEditingIndices = reciprocal < 0 ? [routeIndex] : [routeIndex, reciprocal]
  }
  if (state.mappingEditingIndices.length) populateMappingEditor()
  else renderMappingSources()
  $('#mappingDialog').showModal()
  $('#sourcePlatform').focus()
  loadMappingDiscovery()
}

function closeMappingDialog() {
  if (state.savingRules) return
  $('#mappingDialog').close()
  $('#mappingForm').reset()
  state.mappingEditingIndices = []
  $('#mappingIncludeSelf').indeterminate = false
  $('#mappingDialogTitle').textContent = '新增群映射'
  $('#submitMapping').textContent = '创建映射'
}

function proposedMappingRules(source, target, contentTypes) {
  const [forwardIndex, reverseIndex] = state.mappingEditingIndices
  const both = $('#mappingDirection').value === 'both'
  const checkbox = $('#mappingIncludeSelf')
  const previousForward = state.rules[forwardIndex]
  const previousReverse = state.rules[reverseIndex]
  const originalTypes = previousForward?.content_types || defaultContentTypes
  const typesUnchanged = originalTypes.length === contentTypes.length && contentTypes.every(type => originalTypes.includes(type))
  const build = (from, to, previous, types) => {
    const includeSelf = checkbox.indeterminate ? previous?.include_self === true : checkbox.checked
    const route = buildRouteFromEndpoints(from, to, includeSelf, types)
    if (previous) {
      route.enabled = previous.enabled !== false
      route.name = previous.name || route.name
    }
    return route
  }
  const forward = build(source, target, previousForward, contentTypes)
  const reverse = both ? build(target, source, previousReverse, typesUnchanged && previousReverse ? (previousReverse.content_types || defaultContentTypes) : contentTypes) : null
  const keys = new Set([forward, reverse].filter(Boolean).map(routeIdentity))
  if (state.rules.some((route, index) => index !== forwardIndex && index !== reverseIndex && keys.has(routeIdentity(route)))) {
    throw new Error('已有相同方向的账号会话映射。请编辑现有规则或选择其他会话。')
  }
  const rules = state.rules.flatMap((route, index) => index === forwardIndex ? [forward] : index === reverseIndex ? (reverse ? [reverse] : []) : [route])
  if (forwardIndex === undefined) rules.push(forward)
  if (reverse && reverseIndex === undefined) rules.push(reverse)
  return rules
}

function bindEvents() {
  $('#logQuery').addEventListener('submit', event => { event.preventDefault(); refreshLogs() })
  for (const id of ['logComponent', 'logLevel', 'logFile']) $(`#${id}`).addEventListener('change', () => refreshLogs())
  $('#olderLogs').addEventListener('click', () => { $('#logLive').checked = false; refreshLogs(true) })
  $('#exportLogs').addEventListener('click', () => {
    const blob = new Blob([(state.logEntries || []).map(entry => `${entry.time} [${entry.component}] ${entry.text}`).join('\n')], { type: 'text/plain;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url; link.download = `mover-logs-${Date.now()}.txt`; link.click()
    URL.revokeObjectURL(url)
  })
  $('#backgroundForm').addEventListener('change', () => { state.backgroundEditing = true })
  $('#backgroundForm').addEventListener('submit', async event => {
    event.preventDefault()
    const saved = await postAction('/api/background/config', { enabled: $('#backgroundEnabled').checked, keep_awake: $('#backgroundSleep').checked, autostart: $('#backgroundAutostart').checked }, '后台设置已保存')
    if (saved) { state.backgroundEditing = false; await refresh(false) }
  })
  $$('.nav-item').forEach(button => button.addEventListener('click', () => setPage(button.dataset.page)))
  $$('[data-page-link]').forEach(button => button.addEventListener('click', () => setPage(button.dataset.pageLink)))
  $$('[data-open-mapping]').forEach(button => button.addEventListener('click', openMappingDialog))
  $('#closeDialog').addEventListener('click', closeMappingDialog)
  $('#cancelDialog').addEventListener('click', closeMappingDialog)
  $('#refreshMappingDiscovery').addEventListener('click', () => loadMappingDiscovery({ refresh: true }))
  $('#mappingDirection').addEventListener('change', () => { if (!state.mappingSelfEdited) showMappingSelfState() })
  $('#mappingIncludeSelf').addEventListener('change', () => { state.mappingSelfEdited = true })
  for (const side of ['source', 'target']) {
    $(`#${side}Platform`).addEventListener('change', () => {
      $(`#${side}Instance`).value = ''
      $(`#${side}Account`).value = ''
      $(`#${side}Session`).value = ''
      $(`#${side}SessionSearch`).value = ''
      renderEndpoint(side)
    })
    $(`#${side}Instance`).addEventListener('input', () => {
      $(`#${side}Account`).value = ''
      renderEndpoint(side)
    })
    for (const suffix of ['Account', 'Session']) $(`#${side}${suffix}`).addEventListener('input', () => renderEndpoint(side))
    $(`#${side}MessageType`).addEventListener('change', () => {
      $(`#${side}Session`).value = ''
      $(`#${side}SessionSearch`).value = ''
      renderEndpoint(side)
    })
    $(`#${side}SessionSearch`).addEventListener('input', () => renderEndpoint(side))
    $(`#${side}SessionPicker`).addEventListener('change', () => {
      $(`#${side}Session`).value = $(`#${side}SessionPicker`).value
      renderEndpoint(side)
    })
  }
  $('#mappingDialog').addEventListener('click', event => {
    if (event.target === $('#mappingDialog')) closeMappingDialog()
  })
  $('#mappingDialog').addEventListener('cancel', event => { event.preventDefault(); closeMappingDialog() })
  $('#mappingForm').addEventListener('submit', async event => {
    event.preventDefault()
    if (state.savingRules) return
    const errorNode = $('#dialogError')
    errorNode.hidden = true
    const source = endpoint('source')
    const target = endpoint('target')
    const contentTypes = $$('#contentTypeOptions input[name="contentType"]:checked').map(input => input.value)
    const error = !contentTypes.length
      ? '至少选择一种转发内容类型。'
      : [source, target].some(value => !value.instance || !value.session || (value.platform === 'aiocqhttp' && !value.account))
        ? '请选择两端平台、实例、QQ 账号和会话 ID。'
        : endpointIdentity(source) === endpointIdentity(target) ? '来源和目标不能是同一个账号会话。' : ''
    try {
      if (error) throw new Error(error)
      await saveMappings(proposedMappingRules(source, target, contentTypes), '群映射已保存')
      closeMappingDialog()
    } catch (error) {
      errorNode.textContent = error.message
      errorNode.hidden = false
    }
  })

  $('#rulesTable').addEventListener('click', async event => {
    if (state.savingRules) return
    const edit = event.target.closest('[data-edit-route]')
    if (edit) {
      openMappingDialog(Number(edit.dataset.editRoute))
      return
    }
    const toggle = event.target.closest('[data-toggle-route]')
    if (toggle) {
      const selected = Number(toggle.dataset.toggleRoute)
      state.rules = state.rules.map((route, index) => index === selected ? { ...route, enabled: route.enabled === false } : route)
      renderRules()
      return
    }
    const selfToggle = event.target.closest('[data-toggle-self]')
    if (selfToggle) {
      const route = state.rules[Number(selfToggle.dataset.toggleSelf)]
      if (!route) return
      const savedIndex = state.savedRules.findIndex(item => routeIdentity(item) === routeIdentity(route))
      if (savedIndex < 0) return
      const includeSelf = route.include_self !== true
      const rules = state.savedRules.map((item, index) => index === savedIndex ? { ...item, include_self: includeSelf } : item)
      selfToggle.disabled = true
      try {
        await saveMappings(rules, includeSelf ? '此方向已开启本人消息转发' : '此方向已关闭本人消息转发', { keepTableEdits: true })
      } catch (error) {
        toast(`本人消息设置未保存：${error.message}`, true)
      } finally {
        selfToggle.disabled = false
      }
      return
    }
    const remove = event.target.closest('[data-remove-route]')
    if (remove) {
      const index = Number(remove.dataset.removeRoute)
      state.rules = state.rules.filter((_, selected) => selected !== index)
      renderRules()
    }
  })
  $$('.filter-tab').forEach(button => button.addEventListener('click', () => {
    state.filter = button.dataset.filter
    $$('.filter-tab').forEach(tab => tab.classList.toggle('active', tab === button))
    renderRules()
  }))
  $('#saveRules').addEventListener('click', async () => {
    if (state.savingRules) return
    try { await saveMappings(state.rules, '群映射已保存') }
    catch (error) { toast(error.message, true) }
  })

  $('#bridgeToggle').addEventListener('click', async () => {
    if (typeof state.bridgeEnabled !== 'boolean') return
    const enabled = !state.bridgeEnabled
    const result = await postAction('/api/bridge/enable', { enabled }, enabled ? '所有已启用映射已恢复' : '所有消息转发已暂停')
    if (result) state.bridgeEnabled = enabled
  })

  const startAstrbot = reset => postAction('/api/astrbot/start', { reset_password: reset }, 'AstrBot 正在启动')
  $('#topStart').addEventListener('click', () => startAstrbot(false))
  $('#runtimeStart').addEventListener('click', () => startAstrbot(false))
  $('#runtimeStop').addEventListener('click', () => postAction('/api/astrbot/stop', {}, 'AstrBot 已停止'))
  $('#installPlugin').addEventListener('click', () => postAction('/api/plugin/install', {}, '桥接插件已安装'))
  $('#installPluginSettings').addEventListener('click', () => postAction('/api/plugin/install', {}, '桥接插件已安装'))
  $('#refreshButton').addEventListener('click', () => refresh(false))
  $('#launchLocalQq').addEventListener('click', async () => {
    if (state.launchingQq) return
    state.launchingQq = true
    $('#launchLocalQq').disabled = true
    try {
      const result = await request('/api/qq/launch', { method: 'POST', body: '{}' })
      if (result.ok === false) throw new Error(result.message || result.error || '本机 QQ 未能打开')
      toast(result.message || '已请求打开本机 QQ，请在 QQ 窗口扫码登录。')
      await refresh(false)
    } catch (error) {
      toast(error.message, true)
    } finally {
      state.launchingQq = false
      $('#launchLocalQq').disabled = false
    }
  })

  $('#settingsForm').addEventListener('submit', async event => {
    event.preventDefault()
    const result = await postAction('/api/settings', {
      astrbot_root: $('#astrbotRoot').value.trim(),
      astrbot_port: Number($('#astrbotPort').value),
    }, '本地设置已保存')
    if (result) await refresh(true)
  })

  $('#startLarkXQr').addEventListener('click', async () => {
    try {
      if (['starting', 'waiting_scan', 'awaiting_confirm'].includes(state.larkxQrStatus || '')) return
      $('#startLarkXQr').disabled = true
      clearInterval(state.larkxQrTimer)
      const qr = await request('/api/larkagentx/qr/start', { method: 'POST', body: '{}' })
      state.larkxQrStatus = qr.status
      $('#larkxQrPanel').hidden = false
      $('#larkxQrImage').src = qr.image_data ? `data:image/png;base64,${qr.image_data}` : ''
      $('#larkxQrImage').hidden = !qr.image_data
      $('#larkxQrStatus').textContent = qr.status === 'waiting_scan' ? '等待扫码' : '正在生成二维码'
      $('#larkxQrMessage').textContent = qr.message || '使用飞书 App 扫描二维码。'
      state.larkxQrTimer = setInterval(pollLarkXQr, 2000)
      await pollLarkXQr()
    } catch (error) {
      toast(error.message, true)
      state.larkxQrStatus = 'error'
      $('#startLarkXQr').disabled = false
    }
  })

  $('#toggleBootstrapPassword').addEventListener('click', () => {
    const input = $('#bootstrapPassword')
    const showing = input.type === 'text'
    input.type = showing ? 'password' : 'text'
    $('#toggleBootstrapPassword').setAttribute('aria-label', showing ? '显示初始密码' : '隐藏初始密码')
    $('#toggleBootstrapPassword').title = showing ? '显示初始密码' : '隐藏初始密码'
  })
  $('#copyBootstrapPassword').addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText($('#bootstrapPassword').value)
      toast('初始密码已复制到剪贴板')
    } catch {
      $('#bootstrapPassword').type = 'text'
      $('#bootstrapPassword').select()
      toast('选中密码后按 Ctrl+C 复制')
    }
  })
  $('#clearBootstrapPassword').addEventListener('click', async () => {
    await postAction('/api/bootstrap/clear', {}, '首次登录提示已清除')
  })
  $('#saveBootstrapPassword').addEventListener('click', async () => {
    const password = $('#bootstrapPassword').value
    if (!password) {
      toast('请先输入 AstrBot 当前登录密码', true)
      return
    }
    const saved = await postAction('/api/bootstrap/set', { password }, '管理密码已加密保存')
    if (saved) $('#bootstrapPassword').type = 'password'
  })
}

if (ensureMatchingPage()) {
  bindEvents()
  setPage(state.page)
  refresh(true)
  refreshLogs()
  setInterval(() => { if ($('#logLive').checked && state.page === 'overview') refreshLogs() }, 1200)
  if (new URLSearchParams(location.search).get('autostart') === '1') {
    pollLarkXQr()
    state.larkxQrTimer = setInterval(pollLarkXQr, 2000)
  }
  setInterval(() => refresh(false), 4500)
}
