const source = {
  url: 'https://www.cdc.gov/sleep/about/index.html',
  name: '查看 CDC 按年龄划分的睡眠建议',
}

// CDC's daily recommendations. Infant and preschool ranges count naps.
const groups = [
  { age: '0–3 个月', hours: '14–17 小时' },
  { age: '4–12 个月', hours: '12–16 小时（含小睡）' },
  { age: '1–2 岁', hours: '11–14 小时（含小睡）' },
  { age: '3–5 岁', hours: '10–13 小时（含小睡）' },
  { age: '6–12 岁', hours: '9–12 小时' },
  { age: '13–17 岁', hours: '8–10 小时' },
  { age: '18–60 岁', hours: '至少 7 小时' },
  { age: '61–64 岁', hours: '7–9 小时' },
  { age: '65 岁及以上', hours: '7–8 小时' },
]

const digits = { 零: 0, 〇: 0, 一: 1, 二: 2, 两: 2, 三: 3, 四: 4, 五: 5, 六: 6, 七: 7, 八: 8, 九: 9 }
const count = text => {
  if (!text.includes('十')) return digits[text] ?? Number(text)
  const [tens, ones] = text.split('十')
  return 10 * (digits[tens] ?? 1) + (digits[ones] ?? 0)
}

function identifyGroup(question) {
  const match = question.match(/(\d{1,3}(?:\.\d+)?|[零〇一二两三四五六七八九]?(?:十[一二三四五六七八九]?)|[零〇一二两三四五六七八九])\s*(个\s*)?(半)?\s*(月|岁)/)
  if (!match) return null
  const age = count(match[1]) + (match[3] ? .5 : 0)
  if (match[4] === '月' && age <= 12) return groups[age < 4 ? 0 : 1]
  const years = match[4] === '月' ? Math.floor(age / 12) : Math.floor(age)
  if (years < 1) return groups[age * 12 < 4 ? 0 : 1]
  if (years > 120) return null
  return groups[[2, 5, 12, 17, 60, 64, 120].findIndex(max => years <= max) + 2]
}

export function sleepGuidance(question, awaitingAge) {
  const infant = /新生儿|婴儿|婴幼儿|宝宝|小婴儿/.test(question)
  const audience = infant || /小孩|孩子|儿童|幼儿|青少年|成人|成年人|老年人|老人|\d+\s*岁/.test(question)
  const asksRecommendation = /建议|应该|需要|合适|适合|正常|够吗|要睡|多睡/.test(question)
  const directAge = /^\s*(?:(?:我家)?(?:宝宝|孩子|小孩)\s*)?(?:\d{1,3}(?:\.\d+)?|[零〇一二两三四五六七八九十]+)\s*(?:个\s*)?(?:半\s*)?(?:月|岁)(?:大|龄|了|呢)?[。！!？?\s]*$/.test(question)
  const age = identifyGroup(question)
  const sleep = /睡/.test(question)
  const namesAnAge = age && !/过去|最近|近\d|近[一二两三四五六七八九十]/.test(question)
  const wantsDuration = /时长|多久|几\s*(?:个\s*)?小时|几\s*个钟|多少|分钟|小时|应该|合适|正常|够吗/.test(question)

  if ((awaitingAge && directAge && age) || (sleep && wantsDuration && namesAnAge)) {
    return { title: age.age + '的睡眠建议', lines: ['每 24 小时建议睡 ' + age.hours + '。'], link: source }
  }
  if (!sleep || !wantsDuration || !(asksRecommendation || audience)) return null

  if (infant) {
    const ranges = /新生儿/.test(question) ? groups.slice(0, 1) : groups.slice(0, 2)
    return {
      title: '婴儿的建议睡眠时长随月龄变化',
      lines: [...ranges.map(group => group.age + '：每天 ' + group.hours + '。'),
        ...(ranges.length > 1 ? ['宝宝几个月大？'] : [])],
      link: source,
      awaitingSleepAge: ranges.length > 1,
    }
  }
  if (/成人|成年人/.test(question)) {
    return { title: '成人的睡眠建议', lines: groups.slice(6).map(group => group.age + '：每天 ' + group.hours + '。'), link: source }
  }
  if (/老年人|老人/.test(question)) {
    return { title: '老年人的睡眠建议', lines: ['65 岁及以上：每天建议睡 7–8 小时。'], link: source }
  }
  if (/青少年/.test(question)) {
    return { title: '青少年的睡眠建议', lines: ['13–17 岁：每天建议睡 8–10 小时。'], link: source }
  }
  return { title: '建议睡多久取决于年龄', lines: ['你问的是几个月或几岁的人？'], awaitingSleepAge: true }
}
