import { sleepGuidance } from './sleep-guidance.mjs'
import { queryWindow, recentWindow } from './date-window.mjs?v=4'
import { todayActionReply } from './today-action.mjs'

const number = new Intl.NumberFormat('zh-CN')
const average = values => values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : 0
const finite = values => values.filter(Number.isFinite)
const duration = minutes => {
  const rounded = Math.round(minutes)
  const hours = Math.floor(rounded / 60)
  const remainder = rounded % 60
  return hours ? hours + ' 小时' + (remainder ? ' ' + remainder + ' 分' : '') : remainder + ' 分钟'
}

export function dayLabel(date) {
  return date.slice(5).replace('-', '/').replace(/^0/, '').replace('/0', '/')
}

function sleepComparison(rows, allRows, field) {
  const sleep = new Map(allRows.filter(row => row.bedtime && Number.isFinite(row[field]))
    .map(row => [row.bedtime, row[field]]))
  const pairs = rows.filter(row => Number.isFinite(row.steps) && sleep.has(row.date))
    .map(row => ({ steps: row.steps, minutes: sleep.get(row.date) }))
  if (pairs.length < 2) return null
  const ranked = pairs.map(row => row.steps).sort((a, b) => a - b)
  const mid = Math.floor(ranked.length / 2)
  const threshold = ranked.length % 2 ? ranked[mid] : (ranked[mid] + ranked[mid - 1]) / 2
  const high = pairs.filter(row => row.steps > threshold).map(row => row.minutes)
  const rest = pairs.filter(row => row.steps <= threshold).map(row => row.minutes)
  if (high.length < 2 || rest.length < 2) return null
  return { high, rest, delta: average(high) - average(rest), days: pairs.length }
}

export function createHealthData(dataset) {
  const last = dataset.last
  const allRows = dataset.rows

  function records(window, dateField = 'date') {
    return allRows.filter(row => row[dateField] && row[dateField] >= window.start && row[dateField] <= window.end)
  }

  function summary(days) {
    const window = recentWindow(last, days)
    const rows = records(window)
    const values = metric => finite(rows.map(item => item[metric]))
    return { rows, first: window.start, last, metrics: { steps: values('steps'), sleep: values('sleep'), deep: values('deep') } }
  }

  function measurementReply(question, kind, window) {
    const measurement = { kind, window }
    const single = window.start === window.end
    const label = { steps: '步数', sleep: '睡眠', deep: '深睡' }[kind]
    const rangeLabel = single ? window.start : window.start + ' 至 ' + window.end
    if (kind === 'steps' && /昨晚|昨夜|前晚|晚上|当晚|夜间/.test(question)) {
      return { title: '步数记录按整天汇总', lines: ['可以查询 ' + rangeLabel + ' 全天的步数。'], measurement }
    }
    const rows = records(window, kind === 'steps' ? 'date' : 'bedtime')
    const values = finite(rows.map(row => row[kind]))
    if (!values.length) {
      return { title: '这个时间范围没有' + label + '记录',
        lines: ['查询日期：' + rangeLabel + '。样例日记录覆盖 ' + allRows[0].date + ' 至 ' + last + '。'], measurement }
    }
    if (single && values.length > 1) {
      return { title: '该日有多条' + label + '记录', lines: ['样例日期：' + rangeLabel + '。请先确认要查看哪条记录。'], measurement }
    }
    const total = /总共|合计|一共|总步数/.test(question) ||
      (kind === 'steps' && /走了多少步|走了几步/.test(question) && !/平均|日均/.test(question))
    const value = total ? values.reduce((sum, item) => sum + item, 0) : average(values)
    const result = kind === 'steps' ? number.format(Math.round(value)) + ' 步' : duration(value)
    if (single) {
      const row = rows.find(item => Number.isFinite(item[kind]))
      const lines = ['样例日期：' + rangeLabel + (kind === 'steps' ? '。' : '（按入睡日）。')]
      if (kind !== 'steps') {
        const detail = []
        if (row.bedAt) detail.push('入睡 ' + row.bedAt.slice(11, 16))
        if (row.wakeAt) detail.push('醒来 ' + (row.wakeAt.slice(0, 10) > rangeLabel ? '次日 ' : '') + row.wakeAt.slice(11, 16))
        if (kind === 'sleep' && Number.isFinite(row.deep)) detail.push('深睡 ' + duration(row.deep))
        if (detail.length) lines.push(detail.join('，') + '。')
      }
      return { title: (kind === 'steps' ? '全天步数 ' : '当晚' + label + ' ') + result, lines, measurement }
    }
    return { title: label + (total ? '合计 ' : '日均 ') + result,
      lines: ['样例日期：' + rangeLabel + '，共 ' + values.length + ' 天有记录。'], measurement }
  }

  function reply(query, fallbackDays, { awaitingSleepAge = false, lastMeasurement, pendingTodayAction } = {}) {
    const question = query.normalize('NFKC').trim()
    if (/^(?:(?:没什么|没事|没有了|不用了|先这样|先到这(?:里)?)[。！!\s]*|谢谢[。！!\s]*)$/.test(question)) {
      return { title: '好的，先聊到这里。', lines: [] }
    }
    const insight = /步数|走路|走了|走得|走多少|步行|散步|活动/.test(question)
    const deep = /深睡|深度睡眠/.test(question)
    const sleeping = deep || /睡眠|睡了|睡得|睡觉|几点睡|入睡|醒来|起床|睡前|睡多久|睡几|睡多长|睡多少/.test(question)

    if (/诊断|确诊|检验结果|用药|处方|安眠药|开药|急救/.test(question)) {
      return { title: '这个问题适合咨询专业人士', lines: ['我可以帮你读懂演示中的步数、睡眠记录，或保存一个日常习惯计划。'] }
    }
    const guidance = sleepGuidance(question, awaitingSleepAge)
    if (guidance) return guidance
    const action = todayActionReply(question, pendingTodayAction)
    if (action) return action
    if (/计划|目标|打卡|习惯|行动|我想(?:要)?(?:多走|早睡|早点睡|多活动)/.test(question)) {
      const plan = sleeping ? '睡前留十分钟放松' : '午饭后散步十分钟'
      return { title: '先选一个小行动', lines: ['可以试试「' + plan + '」。你可以调整时间和内容，然后在接下来的 7 天里记录自己完成了几次。'], plan }
    }
    if (deep && /什么|区别|意思|含义|是指/.test(question) && !/数据|多久|多少|几小时/.test(question)) {
      return { title: '深睡是睡眠的一部分', lines: ['深睡通常指非快速眼动睡眠的第三阶段。总睡眠时间包含多个睡眠阶段，两者适合分开记录与比较。'],
        link: { url: 'https://www.nhlbi.nih.gov/health/sleep/stages-of-sleep', name: '查看 NHLBI 的睡眠分期说明' } }
    }

    const dates = queryWindow(question, last)
    const followup = /^(那|还有|再看|也看|换成)|(?:呢|的数据|的记录|具体几点)[。！？!?]*$/.test(question)
    const asksNight = /昨晚|昨夜|前晚|晚上|那一晚|那晚/.test(question)
    const requestsData = /数据|记录|多少|几小时|多久|几点/.test(question)
    const shortDate = /^(?:那|就查)?\s*(?:前天晚上|昨天晚上|前晚|前天|昨晚|昨夜|昨天|今天|上周|本周|这周)\s*(?:呢|吧)?[。！？!?]*$/.test(question)
    const requested = insight ? 'steps' : deep ? 'deep' : sleeping ? 'sleep' : null
    const inherited = shortDate || (dates || followup) && requestsData ? lastMeasurement?.kind : null
    const kind = requested ?? inherited ?? (asksNight && (requestsData || shortDate) ? 'sleep' : null)
    if (kind && dates?.error) return { title: '需要确认查询日期', lines: [dates.error] }
    const window = dates?.start ? dates : (followup ? lastMeasurement?.window : null) ?? recentWindow(last, fallbackDays)

    if (insight && sleeping && /走得|步数高|步数低|配对|相关|关联|关系|对睡眠/.test(question)) {
      const pair = sleepComparison(records(window), allRows, deep ? 'deep' : 'sleep')
      const metric = deep ? '深睡' : '睡眠'
      if (!pair) return { title: '配对记录不足', lines: ['这个时间范围无法分成两个有效组。可以试试询问近 30 天。'] }
      const delta = Math.round(Math.abs(pair.delta) * 10) / 10
      return {
        title: delta ? '高步数组平均' + (pair.delta > 0 ? '多' : '少') + '睡 ' + delta + ' 分钟' : '两组均值接近',
        lines: [
          window.start + ' 至 ' + window.end + ' 有 ' + pair.days + ' 个可配对日期。按步数中位数分组，高步数组平均' + metric + ' ' + duration(average(pair.high)) + '（' + pair.high.length + ' 天），其余组 ' + duration(average(pair.rest)) + '（' + pair.rest.length + ' 天）。',
          '这是样例记录中的分组差异，不能据此判断步数变化导致了睡眠变化。',
        ],
      }
    }
    if (insight && sleeping) {
      return { title: '你想先查步数还是睡眠？', lines: ['可以分别查询同一个日期范围。'], measurement: { window } }
    }
    if (kind) return measurementReply(question, kind, window)
    if (dates && requestsData) {
      if (dates.error) return { title: '需要确认查询日期', lines: [dates.error] }
      return { title: '想查看步数还是睡眠？', lines: ['已确定样例日期范围。'], measurement: { window: dates } }
    }
    return { title: '这类问题暂不在演示范围内', lines: ['你可以查样例中的步数和睡眠、按年龄查睡多久合适，或者制定一周行动计划。'] }
  }
  return { summary, reply, asOf: last }
}

export { average, duration }
