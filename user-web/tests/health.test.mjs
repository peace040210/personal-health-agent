import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import { createHealthData } from '../dist/health.mjs'

const dataset = JSON.parse(readFileSync(new URL('../dist/data.json', import.meta.url)))
const health = createHealthData(dataset)

test('the overview and questions use the requested date window', () => {
  assert.equal(health.summary(7).rows.length, 7)
  assert.equal(health.summary(30).first, '2024-11-16')
  assert.match(health.reply('过去7天一共走了多少步？', 30).title, /62,058/)
  assert.match(health.reply('我想知道过去七天平均走了多少步', 30).title, /8,865/)
})

test('the comparison groups by median and pairs sleep by bedtime date', () => {
  const answer = health.reply('过去30天，步数高的日子当晚深睡更长吗？', 7)
  assert.match(answer.title, /多睡 1.3 分钟/)
  assert.match(answer.lines[0], /30 个可配对日期/)
  assert.match(answer.lines[1], /不能据此判断/)
})

test('definitions and coach actions stay within the demo scope', () => {
  assert.match(health.reply('深睡和总睡眠有什么区别？', 7).link.url, /^https:/)
  assert.equal(health.reply('帮我定个步行计划', 7).plan, '午饭后散步十分钟')
  assert.match(health.reply('帮我诊断一下', 7).title, /咨询专业人士/)
})

test('infant sleep guidance is age-specific and accepts an age-only follow-up', () => {
  const reply = health.reply('婴儿每天睡几小时合适', 7)
  assert.match(reply.lines[0], /0–3 个月.*14–17 小时/)
  assert.match(reply.lines[1], /4–12 个月.*12–16 小时.*含小睡/)
  assert.equal(reply.awaitingSleepAge, true)
  assert.equal(new URL(reply.link.url).hostname, 'www.cdc.gov')
  const followup = health.reply('六个月', 7, { awaitingSleepAge: reply.awaitingSleepAge })
  assert.match(followup.lines[0], /每 24 小时.*12–16 小时/)
  assert.match(health.reply('2个月大的宝宝每天睡多久？', 7).lines[0], /14–17 小时/)
  assert.match(health.reply('1岁孩子每天睡几小时合适？', 7).lines[0], /11–14 小时/)
})

test('personal statistics and a turn ending never inherit an age suggestion', () => {
  const context = { awaitingSleepAge: true }
  assert.match(health.reply('过去七天我平均睡多久', 7, context).title, /睡眠日均/)
  assert.equal(health.reply('没什么', 7, context).title, '好的，先聊到这里。')
  assert.equal(health.reply('没什么', 7, context).awaitingSleepAge, undefined)
  assert.equal(health.reply('六个月', 7).awaitingSleepAge, undefined)
  assert.equal(health.reply('那今天几号？', 7).title, '这类问题暂不在演示范围内')
})

test('last-night requests use the date of going to bed and retain it across follow-ups', () => {
  const first = health.reply('我昨天晚上睡了几小时', 7)
  assert.equal(first.title, '当晚睡眠 6 小时 35 分')
  assert.match(first.lines[0], /2024-12-14.*入睡日/)
  assert.match(first.lines[1], /入睡 23:07.*次日 05:42.*深睡 1 小时 13 分/)
  const context = { lastMeasurement: first.measurement }
  const next = health.reply('昨天晚上的数据', 30, context)
  assert.equal(next.title, first.title)
  assert.deepEqual(next.measurement, first.measurement)
  assert.equal(health.reply('那深睡呢？', 30, context).title, '当晚深睡 1 小时 13 分')
  assert.equal(health.reply('那今天几号？', 7, context).title, '这类问题暂不在演示范围内')
})

test('supports explicit dates and reuses the metric when the user changes the window', () => {
  const first = health.reply('2024-12-12 晚上睡了多久', 30)
  assert.equal(first.title, '当晚睡眠 7 小时')
  const revised = health.reply('那前晚呢？', 7, { lastMeasurement: first.measurement })
  assert.equal(revised.title, '当晚睡眠 7 小时 15 分')
  assert.equal(health.reply('两天前的睡眠数据', 7).title, revised.title)
  const priorWeek = health.reply('上周呢？', 30, { lastMeasurement: first.measurement })
  assert.match(priorWeek.lines[0], /2024-12-02 至 2024-12-08/)
})

test('invalid and missing dates never return a silent default average', () => {
  const missing = health.reply('2026-11-22 晚上睡了多久', 7)
  assert.equal(missing.title, '这个时间范围没有睡眠记录')
  assert.match(missing.lines[0], /2026-11-22/)
  assert.equal(health.reply('2024-02-30 睡眠记录', 7).title, '需要确认查询日期')
  assert.equal(health.reply('昨天和今天的睡眠记录', 7).title, '需要确认查询日期')
  assert.equal(health.reply('昨天晚上走了多少步？', 7).title, '步数记录按整天汇总')
})

test('a nap request proposes one action today and preserves the stated duration', () => {
  const reply = health.reply('我今天需要午休十分钟', 7)
  assert.equal(reply.title, '今天午休 10 分钟')
  assert.deepEqual(reply.todayAction, { kind: 'nap', minutes: 10 })
  assert.equal(reply.plan, undefined)
  assert.deepEqual(health.reply('我中午只有一刻钟可以午休', 7).todayAction, { kind: 'nap', minutes: 15 })
  assert.deepEqual(health.reply('今天下午想散步 20 分钟', 7).todayAction, { kind: 'walk', minutes: 20 })
  assert.equal(health.reply('今天不想安排午休十分钟', 7).todayAction, undefined)
  assert.equal(health.reply('帮我制定一周走路计划', 7).todayAction, undefined)
})

test('a draft action can be clarified or revised and is never proposed for another date', () => {
  const draft = health.reply('我今天想午休', 7)
  assert.equal(draft.title, '你想安排多长时间？')
  assert.equal(draft.todayAction, undefined)
  const context = { pendingTodayAction: draft.pendingTodayAction }
  assert.deepEqual(health.reply('那就十五分钟', 7, context).todayAction, { kind: 'nap', minutes: 15 })
  assert.deepEqual(health.reply('改成五分钟', 7, context).todayAction, { kind: 'nap', minutes: 5 })
  assert.equal(health.reply('明天想午休十分钟', 7, context).todayAction, undefined)
  assert.equal(health.reply('我今天已经午休了十分钟', 7).todayAction, undefined)
  assert.equal(health.reply('昨天午休了几分钟', 7).title, '这里没有单独的午休记录')
})
