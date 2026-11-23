export function dateOffset(day, days) {
  const date = new Date(day + 'T12:00:00Z')
  date.setUTCDate(date.getUTCDate() + days)
  return date.toISOString().slice(0, 10)
}

export function recentWindow(last, days) {
  return { start: dateOffset(last, 1 - days), end: last }
}

const chineseDigits = { 零: 0, 一: 1, 二: 2, 两: 2, 三: 3, 四: 4, 五: 5, 六: 6, 七: 7, 八: 8, 九: 9 }

export function count(text) {
  if (text.includes('十')) {
    const [tens, ones] = text.split('十')
    return (chineseDigits[tens] ?? 1) * 10 + (chineseDigits[ones] ?? 0)
  }
  return chineseDigits[text] ?? Number(text)
}

function calendarDay(year, month, day) {
  const value = [year, month.padStart(2, '0'), day.padStart(2, '0')].join('-')
  const date = new Date(value + 'T12:00:00Z')
  return Number.isFinite(date.valueOf()) && date.toISOString().slice(0, 10) === value ? value : null
}

export function queryWindow(question, last) {
  const calendar = /(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)|(?:(\d{4})年)?(\d{1,2})[月/](\d{1,2})(?:日|号)?(?!\d)/g
  const dates = [...question.matchAll(calendar)]
  if (dates.length) {
    const known = dates.map(([, isoYear, isoMonth, isoDay, year, month, day]) => isoYear
      ? calendarDay(isoYear, isoMonth, isoDay) : calendarDay(year ?? last.slice(0, 4), month, day))
    if (known.some(day => !day)) return { error: '请核对日期，例如 2024-12-14。' }
    if (known.length === 1) return { start: known[0], end: known[0] }
    if (known.length === 2 && /至|到|~|～/.test(question) && known[0] <= known[1]) return { start: known[0], end: known[1] }
    return { error: '请给出一个日期，或按先后顺序写出起止日期。' }
  }

  const relative = question.match(/前晚|前天|昨晚|昨夜|昨天|今日|今天|最新一天|明晚|明天/g) ?? []
  const relativeOffsets = { 前晚: -2, 前天: -2, 昨晚: -1, 昨夜: -1, 昨天: -1, 明晚: 1, 明天: 1 }
  const days = [...new Set(relative.map(label => dateOffset(last, relativeOffsets[label] ?? 0)))]
  if (days.length === 1) return { start: days[0], end: days[0] }
  if (days.length === 2 && /至|到/.test(question) && days[0] <= days[1]) return { start: days[0], end: days[1] }
  if (days.length > 1) return { error: '请每次查一个日期，或写明起止日期。' }
  const daysAgo = question.match(/(\d{1,4}|[零一二两三四五六七八九十]{1,3})\s*天前/)
  if (daysAgo) {
    const date = dateOffset(last, -count(daysAgo[1]))
    return { start: date, end: date }
  }
  const duration = question.match(/(?:最近|过去|近|这)\s*(\d{1,4}|[零一二两三四五六七八九十]{1,3})\s*(天|日|周|个星期|星期)/)
  if (duration) {
    const days = count(duration[1]) * (/周|星期/.test(duration[2]) ? 7 : 1)
    if (!Number.isInteger(days) || days < 1) return { error: '请给出有效的天数或周数。' }
    return recentWindow(last, days)
  }
  if (/最近一个月|过去一个月/.test(question)) return recentWindow(last, 30)
  const weeks = question.match(/上上周|上一周|上周|本周|这周|上个星期|本星期|这个星期/g)
  if (weeks?.length > 1) return { error: '请每次查一周，或写明起止日期。' }
  if (weeks) {
    const weekday = new Date(last + 'T12:00:00Z').getUTCDay()
    const monday = dateOffset(last, -((weekday + 6) % 7))
    if (/^(本|这)/.test(weeks[0])) return { start: monday, end: last }
    const shift = weeks[0] === '上上周' ? -14 : -7
    return { start: dateOffset(monday, shift), end: dateOffset(monday, shift + 6) }
  }
  if (/上个月|这个月|本月|去年|今年|月初|月末|\d{4}年|\d{1,2}月/.test(question)) {
    return { error: '请给出具体日期或起止日期，例如 2024-12-01 至 2024-12-15。' }
  }
  return null
}
