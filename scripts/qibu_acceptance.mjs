import assert from 'node:assert/strict'

const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright')
const baseURL = new URL(process.env.QIBU_BASE_URL || 'http://127.0.0.1:8876')
assert.ok(['127.0.0.1', 'localhost', '[::1]'].includes(baseURL.hostname), 'Use an isolated local Qibu instance.')
const browser = await chromium.launch({ executablePath: process.env.CHROME_EXECUTABLE || undefined })
const pass = []
const ready = page => page.waitForFunction(() => !document.querySelector('#conversation-list').disabled)
const turns = page => page.locator('#messages')
const lastReply = page => turns(page).locator('.bubble.assistant').last()
const select = page => page.locator('#conversation-list')
const snapshot = async (page, id) => {
  const response = await page.request.get('/api/qibu/session', { params: { session_id: id ?? await select(page).inputValue() } })
  assert.equal(response.status(), 200)
  return response.json()
}
const chat = async (page, message) => {
  await ready(page)
  const sent = page.waitForResponse(response => response.url().endsWith('/api/qibu/chat'))
  await page.locator('#question').fill(message)
  await page.locator('#question').press('Enter')
  const response = await sent
  assert.equal(response.status(), 200)
  const result = await response.json()
  await lastReply(page).getByText(result.title, { exact: true }).waitFor()
  return result
}
const newConversation = async page => {
  const old = await select(page).inputValue()
  await page.getByRole('button', { name: '新会话' }).click()
  await page.waitForFunction(id => {
    const list = document.querySelector('#conversation-list')
    return !!list.value && list.value !== id && !list.disabled
  }, old)
}

async function check(name, run, { prepare } = {}) {
  const context = await browser.newContext({ baseURL: baseURL.origin })
  const page = await context.newPage()
  page.setDefaultTimeout(85_000)
  const errors = []
  page.on('pageerror', error => errors.push(error.message))
  try {
    if (prepare) await prepare(page)
    await page.goto('/qibu/#chat')
    await run(page)
    assert.deepEqual(errors, [])
    pass.push(name)
    console.log('PASS', name)
  } finally {
    await context.close()
  }
}

const cases = [
  ['问睡眠、补日期、刷新追问、改查前晚', async page => {
    const question = '帮我瞅一眼我睡了多久'
    assert.match((await chat(page, question)).title, /哪一天/)
    const night = await chat(page, '就昨晚吧')
    assert.match(night.title, /当晚睡眠/)
    assert.match(night.lines[0], /2024-12-14/)
    assert.equal(night.understanding, 'model')
    const id = await select(page).inputValue()
    await page.reload()
    await ready(page)
    assert.equal(await select(page).inputValue(), id)
    await turns(page).getByText(question, { exact: true }).waitFor()
    const deep = await chat(page, '那深睡呢')
    assert.match(deep.title, /当晚深睡/)
    assert.match(deep.lines[0], /2024-12-14/)
    const revised = await chat(page, '不是，我想看前晚的')
    assert.match(revised.title, /当晚深睡/)
    assert.match(revised.lines[0], /2024-12-13/)
  }],
  ['缺真实日期的记录时直接说明，追问保持同一日期', async page => {
    const missing = await chat(page, '给我看看 2026-11-20 那晚，总共睡了多久')
    assert.match(missing.title, /没有相关记录/)
    const followUp = await chat(page, '那深睡有吗')
    assert.match(followUp.title, /没有相关记录/)
    assert.match(followUp.lines[0], /2026-11-20/)
    assert.match((await chat(page, '昨晚的呢')).lines[0], /2024-12-14/)
    assert.deepEqual((await snapshot(page)).actions, [])
  }],
  ['健康问答只追问缺少的年龄，随后给来源', async page => {
    const question = await chat(page, '我家娃每天得睡多久')
    assert.match([question.title, ...question.lines].join(' '), /几个月/)
    const age = await chat(page, '刚满六个月')
    assert.match(age.lines.join(' '), /12–16/)
    assert.ok(age.sources.some(source => new URL(source.url).hostname === 'www.cdc.gov'))
    await lastReply(page).getByRole('link').first().waitFor()
  }],
  ['变更行动时长与周期、确认、打卡、刷新恢复', async page => {
    const clarify = await chat(page, '帮我排个一周的散步计划')
    assert.match(clarify.title, /几分钟/)
    const week = (await chat(page, '每天十五分钟吧')).draft
    assert.deepEqual([week.kind, week.minutes, week.days], ['walk', 15, 7])
    const revised = await chat(page, '今天就先午休十分钟')
    assert.deepEqual([revised.draft.kind, revised.draft.minutes, revised.draft.days], ['nap', 10, 1])
    assert.equal(revised.memory_draft, null)
    assert.equal(await turns(page).locator('[data-confirm-agent]:disabled').count(), 1)
    const sid = await select(page).inputValue()
    const stale = await page.request.post('/api/qibu/actions', {
      data: { session_id: sid, action_id: week.id, operation: 'confirm' },
    })
    assert.equal(stale.status(), 409)
    assert.deepEqual((await snapshot(page, sid)).actions, [])
    await lastReply(page).getByRole('button', { name: '确认今日行动' }).click()
    await page.getByText('行动已保存。', { exact: true }).waitFor()
    await page.locator('#agent-actions').getByRole('button', { name: '标记完成' }).click()
    await page.locator('#agent-actions').getByRole('button', { name: '已完成' }).waitFor()
    await page.reload()
    await page.locator('#agent-actions').getByRole('button', { name: '已完成' }).waitFor()
    const saved = (await snapshot(page, sid)).actions
    assert.deepEqual(saved.map(action => [action.minutes, action.days, action.checked.length]), [[10, 1, 1]])
    await page.locator('[data-view="chat"]').click()
    const another = await chat(page, '改成十五分钟')
    assert.equal(another.draft?.minutes, 15)
    await chat(page, '还是先不安排了')
    assert.equal(await turns(page).locator('[data-confirm-agent]:enabled').count(), 0)
    assert.equal((await snapshot(page, sid)).actions.length, 1)
  }],
  ['已确认记忆跨会话、独立浏览器隔离、撤除记忆', async page => {
    const proposed = await chat(page, '我工作日午休只有十分钟')
    assert.ok(proposed.memory_draft)
    assert.equal((await snapshot(page)).memory, null)
    const origin = await select(page).inputValue()
    await newConversation(page)
    assert.equal((await chat(page, '按我平时午休的时间帮我安排午休')).draft, null)
    await select(page).selectOption(origin)
    await lastReply(page).getByRole('button', { name: '确认记住' }).click()
    await lastReply(page).getByText('已记住', { exact: true }).waitFor()
    await newConversation(page)
    const state = await snapshot(page)
    assert.equal(state.memory.minutes, 10)
    const weekday = new Date(state.today + 'T12:00:00Z').getUTCDay()
    const recalled = await chat(page, '按我平时午休的时间帮我安排午休')
    if (weekday > 0 && weekday < 6) {
      assert.equal(recalled.draft?.minutes, 10)
      assert.match(recalled.lines.join(' '), /已使用你确认过的/)
    } else {
      assert.equal(recalled.draft, null)
    }
    const other = await browser.newContext({ baseURL: baseURL.origin })
    try {
      const response = await other.request.get('/api/qibu/session', { params: { session_id: origin } })
      assert.equal(response.status(), 404)
      assert.deepEqual(await (await other.request.get('/api/qibu/conversations')).json(), [])
    } finally {
      await other.close()
    }
    await page.getByText('我的记忆', { exact: true }).click()
    await page.getByText('工作日午休最多 10 分钟', { exact: true }).waitFor()
    await page.locator('#forget-memory').click()
    await page.getByText('已忘记这条记忆。', { exact: true }).waitFor()
    assert.equal((await snapshot(page)).memory, null)
  }],
  ['启动断连、503、成功响应丢失以及确认重试', async page => {
    await page.getByText('后端暂时连不上。你可以再试一次。', { exact: true }).waitFor()
    await page.getByRole('button', { name: '重试加载会话' }).click()
    await ready(page)
    await page.route('**/api/qibu/chat', route => route.fulfill({ status: 503 }), { times: 1 })
    const text = '今天我想抽十分钟午休'
    await page.locator('#question').fill(text)
    await page.locator('#question').press('Enter')
    await page.getByText('请求未完成，请再试一次。', { exact: true }).waitFor()
    assert.equal(await page.locator('#question').inputValue(), text)
    assert.equal(await turns(page).getByText(text, { exact: true }).count(), 0)
    assert.equal((await snapshot(page)).history.length, 0)
    let completed
    await page.route('**/api/qibu/chat', async route => {
      completed = await (await route.fetch()).json()
      await route.abort()
    }, { times: 1 })
    await page.locator('#question').press('Enter')
    await page.getByText('后端暂时连不上。你可以再试一次。', { exact: true }).waitFor()
    assert.equal((await snapshot(page)).history.length, 1)
    const retry = await chat(page, text)
    assert.equal(retry.draft.id, completed.draft.id)
    assert.equal(await turns(page).getByText(text, { exact: true }).count(), 1)
    const confirm = lastReply(page).getByRole('button', { name: '确认今日行动' })
    await page.route('**/api/qibu/actions', async route => {
      await route.fetch()
      await route.abort()
    }, { times: 1 })
    await confirm.click()
    await page.getByText('后端暂时连不上。你可以再试一次。', { exact: true }).waitFor()
    await confirm.click()
    await page.getByText('行动已保存。', { exact: true }).waitFor()
    const persisted = await snapshot(page)
    assert.equal(persisted.history.length, 1)
    assert.deepEqual(persisted.actions.map(action => action.id), [completed.draft.id])
  }, { prepare: page => page.route('**/api/qibu/session?*', route => route.abort(), { times: 1 }) }],
]

try {
  const results = await Promise.allSettled(cases.map(args => check(...args)))
  results.forEach((result, index) => {
    if (result.status === 'rejected') console.error('FAIL', cases[index][0], result.reason)
  })
  console.log(`${pass.length}/${cases.length} browser scenarios passed`)
  if (pass.length !== cases.length) process.exitCode = 1
} finally {
  await browser.close()
}
