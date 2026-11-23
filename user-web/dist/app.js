import { average, createHealthData, dayLabel, duration } from './health.mjs?v=4'
import { actionLabels } from './today-action.mjs'
import { createAgentClient } from './agent-client.mjs?v=7'

const $ = id => document.getElementById(id)
const number = new Intl.NumberFormat('zh-CN')
const icon = name => '<svg aria-hidden="true"><use href="#' + name + '"></use></svg>'
const escape = value => String(value).replace(/[&<>"']/g, char => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
})[char])
const pages = { overview: '健康概览', chat: '问健康助手', plan: '我的计划' }
let health
let range = 7
let metric = 'steps'
let points = []
let plan = readPlan()
let chatContext = {}
let daily = readToday(localDate())
const agent = location.pathname.startsWith('/qibu/') ? createAgentClient() : null
let agentState
let sending = false
let switching = false
let writing = false
let retryTurn
const greeting = $('messages').innerHTML

function setBusy() {
  const busy = !!agent && (sending || switching || writing || !agentState)
  $('chat-form').querySelector('button').disabled = busy
  $('new-conversation').disabled = busy
  $('conversation-list').disabled = busy
}

function activePage() {
  const value = location.hash.slice(1)
  return Object.hasOwn(pages, value) ? value : 'overview'
}

function showPage() {
  const current = activePage()
  $('breadcrumb').textContent = pages[current]
  document.title = '栖步 · ' + pages[current]
  for (const name of Object.keys(pages)) {
    $('view-' + name).hidden = name !== current
    const link = document.querySelector('[data-view="' + name + '"]')
    if (name === current) link.setAttribute('aria-current', 'page')
    else link.removeAttribute('aria-current')
  }
  if (current === 'chat') $('messages').scrollTop = $('messages').scrollHeight
  if (current === 'plan') {
    renderToday()
    renderPlan()
    if (agent) renderAgentActions()
  }
}

function navigate(page) {
  if (location.hash !== '#' + page) location.hash = page
  else showPage()
}

function metricCard(name, value, detail, theme, symbol) {
  return '<article class="metric card-' + theme + '"><span class="metric-icon">' + icon(symbol) +
    '</span><div><h2>' + name + '</h2><strong>' + value + '</strong><p>' + detail + '</p></div></article>'
}

function renderOverview() {
  if (!health) return
  const { rows, first, last, metrics } = health.summary(range)
  $('dataset-date').textContent = '样例数据截至 ' + last
  const recordCount = values => '近 ' + range + ' 天 · ' + values.length + ' 天有记录'
  $('metrics').innerHTML = [
    metricCard('日均步数', metrics.steps.length ? number.format(Math.round(average(metrics.steps))) + '<small> 步</small>' : '—', recordCount(metrics.steps), 'steps', 'steps'),
    metricCard('平均睡眠', metrics.sleep.length ? duration(average(metrics.sleep)) : '—', recordCount(metrics.sleep), 'sleep', 'moon'),
    metricCard('平均深睡', metrics.deep.length ? Math.round(average(metrics.deep)) + '<small> 分钟</small>' : '—', recordCount(metrics.deep), 'deep', 'timer'),
  ].join('')
  $('chart-range').textContent = dayLabel(first) + ' 至 ' + dayLabel(last) + ' · 每日记录'
  points = rows
  const values = metrics[metric]
  if (!values.length) {
    $('chart').innerHTML = '<p class="fine-print">这个范围内没有记录。</p>'
    return
  }
  const unit = metric === 'steps' ? 2000 : 120
  const ceiling = Math.max(unit * 4, Math.ceil(Math.max(...values) / unit) * unit)
  const format = metric === 'steps' ? value => number.format(value) + ' 步' : duration
  const ticks = [ceiling, ceiling * .75, ceiling / 2, ceiling / 4, 0].map(value =>
    '<span>' + (metric === 'steps' ? number.format(value) : Number((value / 60).toFixed(1)) + 'h') + '</span>').join('')
  const columns = rows.map((row, index) => {
    const value = row[metric]
    const valid = Number.isFinite(value)
    const label = dayLabel(row.date)
    const showLabel = rows.length < 10 || index % Math.ceil(rows.length / 6) === 0 || index === rows.length - 1
    return '<div class="bar-column"><button type="button" data-point="' + index + '" class="bar ' + (metric === 'steps' ? 'green-bar' : 'blue-bar') + '" ' +
      'style="--height:' + (valid ? Math.max(2, value / ceiling * 100) : 0) + '%" ' +
      'aria-label="' + label + ' ' + (valid ? format(value) : '无记录') + '" title="' + label + ' ' + (valid ? format(value) : '无记录') +
      '"' + (valid ? ' aria-pressed="false"' : ' disabled') + '></button><small class="bar-label' + (showLabel ? '' : ' concealed') + '">' + label + '</small></div>'
  }).join('')
  $('chart').innerHTML = '<div class="chart-layout"><div class="axis" aria-hidden="true">' + ticks +
    '</div><div class="plot">' + columns + '</div></div><p id="point-detail" class="point-detail"></p>'
  const selected = rows.findLastIndex(row => Number.isFinite(row[metric]))
  if (selected >= 0) selectPoint(selected)
}

function selectPoint(index) {
  const point = points[index]
  if (!point || !Number.isFinite(point[metric])) return
  $('chart').querySelectorAll('[data-point]').forEach((button, i) => {
    if (!button.disabled) button.setAttribute('aria-pressed', i === index ? 'true' : 'false')
  })
  $('point-detail').textContent = dayLabel(point.date) + ' · ' +
    (metric === 'steps' ? number.format(point.steps) + ' 步' : duration(point.sleep))
}

function userBubble(text) {
  return '<div class="bubble user"><p>' + escape(text) + '</p></div>'
}

async function ask(question) {
  const value = question.trim()
  if (!value || sending || switching || writing || (agent ? !agentState : !health)) return
  navigate('chat')
  const messages = $('messages')
  messages.insertAdjacentHTML('beforeend', userBubble(value))
  const pendingBubble = messages.lastElementChild
  $('question').value = ''
  $('chat-status').textContent = ''
  let result
  if (agent) {
    sending = true
    setBusy()
    $('chat-status').textContent = '正在理解并处理这条消息…'
    messages.scrollTop = messages.scrollHeight
    const session = agent.current()
    if (retryTurn?.session !== session || retryTurn.value !== value) {
      retryTurn = { session, value, id: crypto.randomUUID() }
    }
    try {
      result = await agent.chat(value, retryTurn.id)
      retryTurn = undefined
      $('chat-status').textContent = ''
    } catch (error) {
      pendingBubble.remove()
      $('chat-status').textContent = error.message
      if (!$('question').value) $('question').value = value
      return
    } finally {
      sending = false
      setBusy()
    }
  } else {
    result = health.reply(value, range, chatContext)
    chatContext = { awaitingSleepAge: result.awaitingSleepAge, lastMeasurement: result.measurement,
      pendingTodayAction: result.pendingTodayAction }
  }
  if (agent && Object.hasOwn(result, 'pending_draft_id')) {
    messages.querySelectorAll('[data-confirm-agent]:not(:disabled)').forEach(button => {
      if (button.dataset.confirmAgent !== result.pending_draft_id) {
        button.disabled = true
        button.textContent = '这项提议已失效'
      }
    })
  }
  if (result.memory_draft) {
    messages.querySelectorAll('.memory-offer:has([data-memory-draft])').forEach(offer => {
      offer.textContent = '已由新的记忆提议替代'
    })
  }
  messages.insertAdjacentHTML('beforeend', assistantBubble(result))
  messages.scrollTop = messages.scrollHeight
  if (agent) void refreshConversationList().catch(() => {})
}

function assistantBubble(result) {
  const link = result.link ? '<a href="' + escape(result.link.url) + '" target="_blank" rel="noreferrer">' + escape(result.link.name) + '</a>' : ''
  const sources = (result.sources ?? []).filter(item => item.url.startsWith('https://')).map(item =>
    '<p><a href="' + escape(item.url) + '" target="_blank" rel="noreferrer">' + escape(item.title) + '</a></p>').join('')
  const action = result.plan ? '<button class="button secondary" type="button" data-use-plan="' + escape(result.plan) + '">用这个行动</button>' : ''
  const today = result.todayAction
  const addToday = today ? '<button class="button secondary" type="button" data-add-today="' + today.kind +
    '" data-minutes="' + today.minutes + '" data-action-date="' + localDate() + '">加入今日行动</button>' : ''
  const confirm = result.draft && (!result.draft_status || result.draft_status === 'draft')
    ? '<button class="button secondary" type="button" data-confirm-agent="' + escape(result.draft.id) +
      '">' + (result.draft.days === 7 ? '确认一周计划' : '确认今日行动') + '</button>'
    : result.draft_status === 'active' ? '<p class="fine-print">已保存</p>' : ''
  let memory = ''
  if (result.memory_draft) {
    const status = result.memory_status
    if (!status || status === 'pending') {
      const id = escape(result.memory_draft.id)
      memory = '<div class="memory-offer"><button class="button secondary" type="button" data-memory-draft="' + id +
        '" data-memory-operation="confirm">确认记住</button><button class="button remove-action" type="button" data-memory-draft="' + id +
        '" data-memory-operation="decline">暂不记录</button></div>'
    } else {
      memory = '<p class="fine-print">' + (status === 'confirmed' ? '曾确认记住' : '这条提议已处理') + '</p>'
    }
  }
  return '<div class="bubble assistant"><strong>' + escape(result.title) + '</strong>' +
    result.lines.map(line => '<p style="white-space:pre-wrap">' + escape(line) + '</p>').join('') +
    (result.understanding === 'fallback' ? '<p class="fine-print">语言模型未返回可用结果，本次使用基础规则。</p>' : '') +
    link + sources + action + addToday + confirm + memory + '</div>'
}

function renderMemory() {
  const item = agentState?.memory
  $('memory-value').textContent = item ? '工作日午休最多 ' + item.minutes + ' 分钟' : '还没有已确认的用户记忆。'
  $('forget-memory').hidden = !item
}

async function refreshConversationList() {
  const rows = await agent.conversations()
  const current = agent.current()
  $('conversation-list').innerHTML = rows.map(row => '<option value="' + escape(row.id) + '">' +
    escape(row.title) + '</option>').join('')
  $('conversation-list').value = current
}

async function loadConversation(next, fresh = false) {
  if (switching || writing || sending) return
  switching = true
  setBusy()
  $('retry-conversation').hidden = true
  $('chat-status').textContent = '正在恢复会话…'
  const previous = agent.current()
  try {
    if (fresh) next = (await agent.newConversation()).id
    if (next) agent.select(next)
    let state
    try {
      state = await agent.read()
    } catch (error) {
      if (error.status !== 404 || next) throw error
      const list = await agent.conversations()
      agent.select(list[0]?.id ?? (await agent.newConversation()).id)
      state = await agent.read()
    }
    agentState = state
    retryTurn = undefined
    const messages = $('messages')
    messages.innerHTML = greeting + state.history.map(row => userBubble(row.message) + assistantBubble(row.reply)).join('')
    messages.scrollTop = messages.scrollHeight
    $('question').value = ''
    renderMemory()
    $('memory-status').textContent = ''
    if (activePage() === 'plan') renderAgentActions()
    $('chat-status').textContent = ''
    $('chat-mode').textContent = state.model_configured ? 'Agent 在线 · 已配置语言模型' : 'Agent 在线 · 本地规则'
    if (state.model_configured) $('chat-runtime-note').textContent = '提问由已配置的语言模型理解，会话与确认后的行动由后端保存。'
    await refreshConversationList().catch(() => {})
  } catch (error) {
    agent.select(previous)
    $('conversation-list').value = previous
    $('chat-status').textContent = error.message
    $('retry-conversation').hidden = !!agentState
  } finally {
    switching = false
    setBusy()
  }
}

async function changeMemory(button, operation, draft) {
  if (writing || switching || sending) return
  writing = true
  setBusy()
  button.disabled = true
  const status = operation === 'forget' ? $('memory-status') : $('chat-status')
  try {
    const result = await agent.memory(operation, draft)
    agentState.memory = result.memory
    renderMemory()
    if (operation === 'forget') {
      $('messages').querySelectorAll('.memory-offer').forEach(offer => { offer.textContent = '提议已清除' })
      status.textContent = '已忘记这条记忆。'
    } else {
      button.closest('.memory-offer').textContent = operation === 'confirm' ? '已记住' : '已跳过'
      status.textContent = ''
    }
  } catch (error) {
    status.textContent = error.message
    button.disabled = false
  } finally {
    writing = false
    if (operation === 'forget') button.disabled = false
    setBusy()
  }
}

function localDate(dayOffset = 0) {
  const now = new Date()
  now.setDate(now.getDate() + dayOffset)
  return [now.getFullYear(), String(now.getMonth() + 1).padStart(2, '0'), String(now.getDate()).padStart(2, '0')].join('-')
}

function planDays(start) {
  return Array.from({ length: 7 }, (_, i) => {
    const day = new Date(start + 'T12:00:00Z')
    day.setUTCDate(day.getUTCDate() + i)
    return day.toISOString().slice(0, 10)
  })
}

function validToday(action) {
  return action && Object.hasOwn(actionLabels, action.kind) && Number.isInteger(action.minutes) &&
    action.minutes > 0 && action.minutes <= 1440
}

function readToday(date) {
  try {
    const rows = JSON.parse(localStorage.getItem('qibu-actions:' + date))
    return { date, actions: Array.isArray(rows) ? rows.filter(validToday) : [] }
  } catch {
    return { date, actions: [] }
  }
}

function currentToday() {
  const date = localDate()
  if (daily.date !== date) daily = readToday(date)
  return daily
}

function saveToday(status) {
  try {
    localStorage.setItem('qibu-actions:' + daily.date, JSON.stringify(daily.actions))
  } catch {
    status = '当前浏览器存储不可用，刷新后今日行动会清空。'
  }
  renderToday()
  $('today-status').textContent = status
}

function renderToday() {
  const { date, actions } = currentToday()
  $('today-date').textContent = date
  $('today-actions').innerHTML = actions.length ? actions.map(action =>
    '<div class="today-action"><strong>' + actionLabels[action.kind] + ' ' + action.minutes + ' 分钟</strong>' +
    '<div><button type="button" class="button secondary" data-toggle-today="' + action.kind + '" aria-pressed="' +
    (action.done === true) + '">' + (action.done === true ? '已完成' : '标记完成') + '</button>' +
    '<button type="button" class="button remove-action" data-remove-today="' + action.kind + '">移除</button></div></div>'
  ).join('') : '<p class="empty-plan">在对话中安排今天的一件事，就可以加到这里。</p>'
}

function addTodayAction(button) {
  const state = currentToday()
  if (button.dataset.actionDate !== state.date) {
    button.textContent = '这项建议已过期'
    button.disabled = true
    return
  }
  const action = { kind: button.dataset.addToday, minutes: Number(button.dataset.minutes), done: false }
  if (!validToday(action)) return
  const existing = state.actions.find(item => item.kind === action.kind)
  if (existing?.minutes !== action.minutes) {
    state.actions = state.actions.filter(item => item.kind !== action.kind).concat(action)
  }
  saveToday('已加入今日行动。')
  navigate('plan')
}

function renderAgentActions() {
  const panel = $('agent-actions-panel')
  panel.hidden = false
  const clock = agentState?.today
  $('agent-today').textContent = clock ?? ''
  const saved = agentState?.actions ?? []
  $('agent-actions').innerHTML = saved.length ? saved.map(item => {
    const days = planDays(item.day).slice(0, item.days)
    const buttons = days.map(day => {
      const done = item.checked.includes(day)
      return '<button class="button secondary" type="button" data-agent-check="' + escape(item.id) +
        '" data-day="' + day + '" data-operation="' + (done ? 'undo' : 'complete') +
        '" aria-pressed="' + done + '"' + (day > clock ? ' disabled' : '') + '>' +
        (item.days === 7 ? dayLabel(day) + ' ' : '') + (done ? '已完成' : '标记完成') + '</button>'
    }).join('')
    return '<div class="today-action"><strong>' + escape(item.label) + ' · ' + (item.days === 7 ? '一周计划' : '今日行动') +
      '</strong><div style="flex-wrap:wrap">' + buttons + '<button class="button remove-action" type="button" data-agent-remove="' +
      escape(item.id) + '">移除</button></div></div>'
  }).join('') : '<p class="empty-plan">已确认的行动会显示在这里。</p>'
}

async function changeAgentAction(button, id, operation, day) {
  if (switching || writing || sending) return
  writing = true
  setBusy()
  button.disabled = true
  const status = operation === 'confirm' ? $('chat-status') : $('agent-status')
  status.textContent = '正在更新…'
  try {
    const next = await agent.change(id, operation, day)
    agentState = { ...agentState, actions: next.actions, today: next.today }
    status.textContent = ''
    renderAgentActions()
    if (operation === 'confirm') {
      button.textContent = '已保存'
      navigate('plan')
      $('agent-status').textContent = '行动已保存。'
    }
  } catch (error) {
    status.textContent = error.message
    button.disabled = false
  } finally {
    writing = false
    setBusy()
  }
}

function readPlan() {
  try {
    const stored = JSON.parse(localStorage.getItem('qibu-plan-v1'))
    if (stored && typeof stored.action === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(stored.start)
        && Number.isFinite(Date.parse(stored.start)) && Array.isArray(stored.checked)) return stored
  } catch {
    return null
  }
  return null
}

function savePlan(status) {
  try {
    localStorage.setItem('qibu-plan-v1', JSON.stringify(plan))
    $('plan-message').textContent = status
  } catch {
    $('plan-message').textContent = '当前浏览器无法保存，计划会在关闭页面后清除。'
  }
  renderPlan()
}

function renderPlan() {
  if (!plan) {
    $('plan-state').innerHTML = '<span class="try-icon">' + icon('calendar') + '</span><h2>本周打卡</h2>' +
      '<p class="empty-plan">保存一个行动后，就可以开始记录。</p>'
    return
  }
  const days = planDays(plan.start)
  const today = localDate()
  const done = days.filter(day => plan.checked.includes(day)).length
  $('plan-state').innerHTML = '<span class="try-icon">' + icon('calendar') + '</span><h2>本周打卡</h2><p class="action-title">' +
    escape(plan.action) + '</p><p class="plan-dates">' + dayLabel(days[0]) + ' 至 ' + dayLabel(days.at(-1)) +
    ' · 已完成 <strong>' + done + ' / 7</strong> 天</p><progress value="' + done + '" max="7" aria-label="一周计划完成进度"></progress>' +
    '<div class="checkin-grid">' + days.map((day, index) => {
      const checked = plan.checked.includes(day)
      return '<button type="button" class="checkin" data-checkin="' + day + '" aria-pressed="' + checked + '" aria-label="第 ' +
        (index + 1) + ' 天 ' + dayLabel(day) + ' 打卡"' + (day > today ? ' disabled' : '') + '><span>' +
        (checked ? icon('check') : index + 1) + '</span><small>' + dayLabel(day) + '</small></button>'
    }).join('') + '</div>'
  if (!$('plan-action').value) $('plan-action').value = plan.action
  $('plan-form-title').textContent = '调整行动'
}

$('range').addEventListener('click', event => {
  const button = event.target.closest('[data-days]')
  if (!button) return
  range = Number(button.dataset.days)
  $('range').querySelectorAll('button').forEach(item => item.setAttribute('aria-pressed', String(item === button)))
  renderOverview()
})

$('metric-choice').addEventListener('click', event => {
  const button = event.target.closest('[data-metric]')
  if (!button) return
  metric = button.dataset.metric
  $('metric-choice').querySelectorAll('button').forEach(item => item.setAttribute('aria-pressed', String(item === button)))
  renderOverview()
})

$('chart').addEventListener('click', event => {
  const button = event.target.closest('[data-point]')
  if (button) selectPoint(Number(button.dataset.point))
})

$('chat-form').addEventListener('submit', event => {
  event.preventDefault()
  ask($('question').value)
})

$('new-conversation').addEventListener('click', () => { if (agent) void loadConversation(null, true) })
$('retry-conversation').addEventListener('click', () => { if (agent) void loadConversation() })
$('conversation-list').addEventListener('change', event => { if (agent) void loadConversation(event.target.value) })
$('forget-memory').addEventListener('click', event => { if (agent) void changeMemory(event.currentTarget, 'forget') })

$('plan-form').addEventListener('submit', event => {
  event.preventDefault()
  const action = $('plan-action').value.trim()
  if (!action) {
    $('plan-message').textContent = '请输入一个行动。'
    $('plan-action').focus()
    return
  }
  const keep = plan && plan.action === action && planDays(plan.start).at(-1) >= localDate()
  plan = keep ? plan : { action, start: localDate(), checked: [] }
  savePlan('计划已保存。')
})

document.addEventListener('click', event => {
  const button = event.target.closest('button')
  if (!button) return
  const { ask: prompt, suggestion, usePlan, checkin, addToday, toggleToday, removeToday } = button.dataset
  if (prompt) ask(prompt)
  if (suggestion) {
    $('plan-action').value = suggestion
    $('plan-action').focus()
  }
  if (usePlan) {
    $('plan-action').value = usePlan
    navigate('plan')
  }
  if (addToday) addTodayAction(button)
  if (agent && button.dataset.confirmAgent) void changeAgentAction(button, button.dataset.confirmAgent, 'confirm')
  if (agent && button.dataset.agentCheck) void changeAgentAction(button, button.dataset.agentCheck, button.dataset.operation, button.dataset.day)
  if (agent && button.dataset.agentRemove) void changeAgentAction(button, button.dataset.agentRemove, 'remove')
  if (agent && button.dataset.memoryDraft) void changeMemory(button, button.dataset.memoryOperation, button.dataset.memoryDraft)
  if (toggleToday) {
    const action = currentToday().actions.find(item => item.kind === toggleToday)
    if (action) {
      action.done = !action.done
      saveToday(action.done ? '已完成今日行动。' : '已撤回完成标记。')
      $('today-actions').querySelector('[data-toggle-today="' + toggleToday + '"]')?.focus()
    }
  }
  if (removeToday) {
    const state = currentToday()
    state.actions = state.actions.filter(item => item.kind !== removeToday)
    saveToday('已移除今日行动。')
  }
  if (checkin && plan && planDays(plan.start).includes(checkin) && checkin <= localDate()) {
    const checked = plan.checked.includes(checkin)
    plan.checked = checked ? plan.checked.filter(day => day !== checkin) : [...plan.checked, checkin]
    savePlan(checked ? '已取消此日打卡。' : '已记录完成。')
    $('plan-state').querySelector('[data-checkin="' + checkin + '"]').focus()
  }
})

addEventListener('hashchange', showPage)
showPage()

if (agent) {
  $('conversation-controls').hidden = false
  $('chat-mode').textContent = '连接 FastAPI · 可查询样例和保存行动'
  $('chat-runtime-note').textContent = '已接入 Agent，确认后的行动由后端保存。'
  void loadConversation()
}

try {
  const response = await fetch('./data.json?v=3')
  if (!response.ok) throw new Error('Data unavailable')
  health = createHealthData(await response.json())
  $('chat-reference-date').textContent = '查询记录时，相对日期以样例 ' + health.asOf + ' 为参照。'
  renderOverview()
} catch {
  $('app-error').hidden = false
  $('question').disabled = true
  $('chat-form').querySelector('button').disabled = true
  document.querySelectorAll('[data-ask]').forEach(button => { button.disabled = true })
}
