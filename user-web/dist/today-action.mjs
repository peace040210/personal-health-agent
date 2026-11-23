import { count } from './date-window.mjs?v=4'

export const actionLabels = { nap: '午休', walk: '散步', break: '休息' }
const durationPattern = /(\d{1,4}|[零一二两三四五六七八九十]{1,3})\s*分钟|半\s*(?:个\s*)?小时|一刻钟/

function findMinutes(text) {
  const match = text.match(durationPattern)
  if (!match) return null
  return match[1] ? count(match[1]) : (match[0] === '一刻钟' ? 15 : 30)
}

export function todayActionReply(text, previous) {
  const kind = /午休|午睡|小睡|打个盹|眯一会/.test(text) ? 'nap'
    : /散步|走一走|步行/.test(text) ? 'walk' : /休息/.test(text) ? 'break' : null
  const change = /^(?:那|就|好[，,]?)?\s*(?:改成|改为|换成|只留|调到|就留|还是)\s*/.test(text)
  const durationFollowup = /^(?:那就|就)?\s*(?:\d+|[一二两三四五六七八九十]+)\s*分钟[。！!\s]*$/.test(text)
  const today = /今天|今日|中午|下午|今晚|午饭后|待会儿|一会儿|现在/.test(text)
  const intention = /需要|想|打算|准备|只有|只能|安排|帮我记|留出|提醒|闹钟/.test(text)
  const recurring = /每天|每周|工作日|一周|七天|7\s*天|长期/.test(text)

  const revisesToday = previous && (change || durationFollowup)
  if (recurring || (!kind && !revisesToday)) return null
  if (/(?:昨天|昨晚|前天|前晚)|已经|刚才|睡了|休息了/.test(text)) {
    if (kind === 'nap' && /几分钟|多久|多长|多少|记录|数据/.test(text)) {
      return { title: '这里没有单独的午休记录', lines: ['可以查看样例的夜间睡眠，或在「我的计划」安排今天的午休。'] }
    }
    return null
  }
  if (/不想|不需要|不用|不要|取消|别安排/.test(text) && (today || previous)) {
    return { title: '今天可以先不安排', lines: ['如果已经加入今日行动，可以到「我的计划」里移除。'] }
  }
  if (/明天|明早|明晚|后天|下周/.test(text) && intention) {
    return { title: '这项行动的日期在未来', lines: ['今日行动会保存在你添加当天。也可以在「我的计划」设置一周目标。'] }
  }
  if (!(today && intention) && !revisesToday) return null
  const action = kind ?? previous?.kind
  if (!action) return null
  const minutes = findMinutes(text)
  if (!minutes || minutes > 1440) {
    return { title: '你想安排多长时间？', lines: ['可以直接告诉我分钟数。'], pendingTodayAction: { kind: action } }
  }
  const label = actionLabels[action]
  const lines = ['可以把这 ' + minutes + ' 分钟记为今天的一次' + label + '，完成后在「我的计划」打卡。']
  if (/提醒|闹钟/.test(text)) lines.push('如果需要到点提醒，可以另外设置设备闹钟。')
  return { title: '今天' + label + ' ' + minutes + ' 分钟', lines,
    todayAction: { kind: action, minutes }, pendingTodayAction: { kind: action } }
}
