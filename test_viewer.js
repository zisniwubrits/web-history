// 从生成的 HTML 里抽出「纯筛选逻辑」区间，直接在 Node 里跑断言。
const fs = require('fs');
const html = fs.readFileSync('archive/exports/history.html', 'utf8');

const START = '/* __FILTER_LOGIC_START__ */';
const END = '/* __FILTER_LOGIC_END__ */';
const i = html.indexOf(START), j = html.indexOf(END);
if (i < 0 || j < 0) { console.error('找不到逻辑区间标记'); process.exit(1); }
const code = html.slice(i + START.length, j);

const mod = new Function(code + '\nreturn {dayStr, shiftDay, timeWindow, rangeLabel, matchesFilters,'
  + ' parseQuery, buildMatcher, splitMatches, escapeRegExp};')();
const { dayStr, timeWindow, matchesFilters, parseQuery, buildMatcher, splitMatches } = mod;

const TODAY = new Date(2026, 8, 10, 14, 30, 0);   // 2026-09-10 本地时间
let fails = 0;
function eq(actual, expected, label) {
  const a = JSON.stringify(actual), e = JSON.stringify(expected);
  const ok = a === e;
  if (!ok) fails++;
  console.log(`[${ok ? 'PASS' : 'FAIL'}] ${label}` + (ok ? '' : `\n        期望 ${e}\n        实际 ${a}`));
}
const W = (st) => { const w = timeWindow(st, TODAY); return { from: w.from, to: w.to }; };

console.log('--- timeWindow 边界 ---');
eq(W({ time: 'all' }),        { from: '', to: '' }, '全部时间 -> 不加限制');
eq(W({ time: 'today' }),      { from: '2026-09-10', to: '2026-09-10' }, '今天');
eq(W({ time: 'yesterday' }),  { from: '2026-09-09', to: '2026-09-09' }, '昨天');
eq(W({ time: '7' }),          { from: '2026-09-04', to: '2026-09-10' }, '最近 7 天 = 今天 + 前 6 天');
eq(W({ time: '30' }),         { from: '2026-08-12', to: '2026-09-10' }, '最近 30 天');
eq(W({ time: '365' }),        { from: '2025-09-11', to: '2026-09-10' }, '最近一年');
eq(W({ time: 'day', day: '2026-01-02' }), { from: '2026-01-02', to: '2026-01-02' }, '指定某一天');
eq(W({ time: 'custom', from: '2026-01-01', to: '2026-03-01' }),
   { from: '2026-01-01', to: '2026-03-01' }, '自定义范围');
eq(W({ time: 'custom', from: '2026-01-01', to: '' }), { from: '2026-01-01', to: '' }, '只填起始');
eq(W({ time: 'custom', from: '', to: '2026-01-01' }), { from: '', to: '2026-01-01' }, '只填结束');

console.log('\n--- 跨月/跨年边界 ---');
const JAN1 = new Date(2026, 0, 1, 9, 0, 0);   // 2026-01-01
eq(timeWindow({ time: 'yesterday' }, JAN1).from, '2025-12-31', '昨天跨年');
eq(timeWindow({ time: '7' }, JAN1).from, '2025-12-26', '最近 7 天跨年');
const MAR1 = new Date(2026, 2, 1, 9, 0, 0);
eq(timeWindow({ time: 'yesterday' }, MAR1).from, '2026-02-28', '昨天跨月（非闰年 2 月）');

console.log('\n--- UTC 陷阱：东八区凌晨必须还是"今天" ---');
eq(dayStr(new Date(2026, 8, 10, 0, 5, 0)), '2026-09-10', '凌晨 00:05 算今天');
eq(dayStr(new Date(2026, 8, 10, 7, 59, 0)), '2026-09-10', '早上 07:59 算今天');
eq(timeWindow({ time: 'today' }, new Date(2026, 8, 10, 0, 5, 0)).from, '2026-09-10',
   '凌晨选"今天"不会退到昨天');

console.log('\n--- matchesFilters 含首尾端点 ---');
const row = (day, browser, title, url) =>
  ['2026-09-10 12:00:00', 'x', browser || 'Edge', 'Default', title || '', url || 'http://a', 'a', 'LINK', 0, 0, day, 1];
eq(matchesFilters(row('2026-09-10'), { time: '7' }, TODAY), true,  '区间右端点保留');
eq(matchesFilters(row('2026-09-04'), { time: '7' }, TODAY), true,  '区间左端点保留');
eq(matchesFilters(row('2026-09-03'), { time: '7' }, TODAY), false, '左端点再往前一天排除');
eq(matchesFilters(row('2026-09-11'), { time: '7' }, TODAY), false, '右端点之后排除');
eq(matchesFilters(row('2026-09-10'), { time: 'day', day: '2026-09-10' }, TODAY), true, '指定某一天命中');
eq(matchesFilters(row('2026-09-09'), { time: 'day', day: '2026-09-10' }, TODAY), false, '指定某一天不命中');

console.log('\n--- 组合条件 ---');
const s1 = { time: '7', browser: 'Chrome', q: 'bili' };
eq(matchesFilters(row('2026-09-09', 'Chrome', 'bilibili 视频', 'https://x'), s1, TODAY), true,
   '时间+浏览器+关键词 全中');
eq(matchesFilters(row('2026-09-09', 'Edge', 'bilibili 视频', 'https://x'), s1, TODAY), false,
   '浏览器不符被排除');
eq(matchesFilters(row('2026-09-09', 'Chrome', '别的标题', 'https://x'), s1, TODAY), false,
   '关键词不符被排除');
eq(matchesFilters(row('2026-09-09', 'Chrome', 'x', 'https://bilibili.com/v'), s1, TODAY), true,
   '关键词匹配 URL');
eq(matchesFilters(row('2026-09-09', 'Chrome', 'BILIBILI 大写', 'https://x'), s1, TODAY), true,
   '关键词大小写不敏感');
eq(matchesFilters(row('2026-09-09'), { time: 'all' }, TODAY), true, '无任何条件时全部保留');

console.log('\n--- 标签文案 ---');
eq(timeWindow({ time: 'today' }, TODAY).label, '今天 2026-09-10', '今天标签带具体日期');
eq(timeWindow({ time: 'day', day: '2026-09-09' }, TODAY).label, '仅 2026-09-09', '某一天标签');
eq(timeWindow({ time: 'custom', from: '2026-01-01', to: '2026-02-01' }, TODAY).label,
   '2026-01-01 ~ 2026-02-01', '范围标签');
eq(timeWindow({ time: 'custom', from: '', to: '2026-02-01' }, TODAY).label,
   '2026-02-01 之前（含当天）', '只有结束端');
eq(timeWindow({ time: 'day', day: '' }, TODAY).label, '未选择日期', '未选日期时的提示');

console.log('\n--- 搜索词解析 ---');
eq(parseQuery('bilibili', false), { mode:'plain', text:'bilibili', flags:'', error:'' },
   '普通模式原样保留');
eq(parseQuery('deepseek|openai', true).text, 'deepseek|openai', '正则模式默认整串作为表达式');
eq(parseQuery('deepseek|openai', true).flags, 'i', '正则默认忽略大小写');
eq(parseQuery('/^https:\\/\\/git/i', true).text, '^https:\\/\\/git', '/pattern/flags 写法解析出 pattern');
eq(parseQuery('/^https:\\/\\/git/i', true).flags, 'i', '/pattern/flags 写法解析出 flags');
eq(parseQuery('/abc/', true).flags, 'i', '/abc/ 不带 flags 时补上 i');
eq(parseQuery('/abc/gm', true).flags, 'gm', '显式写了 flags 就完全尊重（可借此区分大小写）');
eq(buildMatcher(parseQuery('/DeepSeek/', true)).test('deepseek'), true,
   '/…/ 默认忽略大小写');
eq(buildMatcher(parseQuery('/DeepSeek/g', true)).test('deepseek'), false,
   '显式写 flags 时能做到区分大小写');

console.log('\n--- 匹配器编译 ---');
eq(buildMatcher(parseQuery('', true)).empty, true, '空表达式视为不筛选');
eq(buildMatcher(parseQuery('deepseek', false)).ok, true, '普通模式永远编译成功');
eq(buildMatcher(parseQuery('(', true)).ok, false, '错误的正则 ok=false');
eq(buildMatcher(parseQuery('(', true)).error.length > 0, true, '错误的正则带出错误原因');
eq(buildMatcher(parseQuery('deepseek|openai', true)).test('用 openai 试试'), true, '或运算生效');
eq(buildMatcher(parseQuery('^https', true)).test('http://x'), false, '锚点 ^ 生效');
eq(buildMatcher(parseQuery('\\.com$', true)).test('a.com'), true, '转义点 + 结尾锚点生效');

console.log('\n--- g 标志的状态污染（经典坑）---');
const gMatcher = buildMatcher(parseQuery('/bili/g', true));
const gResults = [1,2,3,4,5].map(() => gMatcher.test('bilibili.com'));
eq(gResults, [true,true,true,true,true], '带 g 标志时 test() 反复调用结果必须一致');
eq(buildMatcher(parseQuery('/bili/y', true)).test('bilibili'), true, '带 y 标志也不受 lastIndex 影响');

console.log('\n--- 普通模式下特殊字符必须是字面量 ---');
eq(buildMatcher(parseQuery('a.b', false)).test('axb'), false, '普通模式 a.b 不匹配 axb');
eq(buildMatcher(parseQuery('a.b', false)).test('a.b'), true, '普通模式 a.b 匹配 a.b');
eq(buildMatcher(parseQuery('a.b', true)).test('axb'), true, '正则模式 a.b 匹配 axb');
eq(buildMatcher(parseQuery('a.b', true)).test('a.b'), true, '正则模式 a.b 也匹配 a.b');
eq(buildMatcher(parseQuery('c++', false)).test('c++ 教程'), true, '普通模式受得住 c++ 这种词');
eq(buildMatcher(parseQuery('c++', true)).ok, false, '正则模式 c++ 是非法表达式（暴露给用户）');
eq(buildMatcher(parseQuery('c\\+\\+', true)).test('c++ 教程'), true, '转义后可用');

console.log('\n--- 命中片段切分（高亮用）---');
const segs = (t, q, rx) => splitMatches(t, buildMatcher(parseQuery(q, !!rx)));
eq(segs('bilibili 视频', 'bili').map(s => s.hit ? '[' + s.text + ']' : s.text).join(''),
   '[bili][bili] 视频', '普通模式多段命中都被切出来');
eq(segs('abc', 'zzz'), [{ text:'abc', hit:false }], '没命中时整段返回');
eq(segs('', 'abc'), [], '空文本返回空数组');
eq(segs('axb', 'a.b', false).map(s => s.hit ? '[' + s.text + ']' : s.text).join(''),
   'axb', '普通模式不会把 axb 的 a/b 点亮');
eq(segs('axb', 'a.b', true).map(s => s.hit ? '[' + s.text + ']' : s.text).join(''),
   '[axb]', '正则模式正确点亮 axb');
eq(segs('aaa', 'a*', true).length > 0, true, '空匹配正则不会死循环');
eq(segs('https://x.com', '\\w+', true).filter(s => s.hit).length > 0, true, '正则高亮正常');

console.log('\n--- 正则 + 时间 + 浏览器 组合 ---');
const mk = (q) => buildMatcher(parseQuery(q, true));
eq(matchesFilters(row('2026-09-09', 'Edge', 'x', 'https://www.bilibili.com/v/1'),
   { time:'7', browser:'Edge' }, TODAY, mk('^https://www\\.bilibili\\.com')), true,
   '正则命中 URL 前缀');
eq(matchesFilters(row('2026-09-09', 'Edge', 'x', 'https://www.bilibili.com/v/1'),
   { time:'7', browser:'Edge' }, TODAY, mk('^http://')), false,
   '正则不命中时不通过');
eq(matchesFilters(row('2026-08-01', 'Edge', 'x', 'https://www.bilibili.com/v/1'),
   { time:'7', browser:'Edge' }, TODAY, mk('bilibili')), false,
   '正则不会绕过时间条件');
eq(matchesFilters(row('2026-09-09', 'Edge', 'x', 'y'),
   { time:'all' }, TODAY, buildMatcher(parseQuery('(', true))), false,
   '非法正则时不会把行放过去');
eq(matchesFilters(row('2026-09-09', 'Edge', 'DeepSeek 开放平台', 'y'),
   { time:'all', q:'deepseek', useRegex:false }, TODAY), true,
   '不传 matcher 时按 state.q 自动编译（向后兼容）');

console.log('\n--- 页面结构一致性 ---');
const ids = new Set([...html.matchAll(/\bid="([^"]+)"/g)].map(m => m[1]));
const used = [...html.matchAll(/getElementById\('([^']+)'\)/g)].map(m => m[1]);
const missing = [...new Set(used)].filter(id => !ids.has(id));
eq(missing, [], '所有 getElementById 都能在 HTML 里找到对应元素');

for (const gone of ['applyFilters', 'rgEl', 'initBrowsers', "getElementById('range')"]) {
  eq(html.indexOf(gone) < 0, true, `已移除旧实现残留: ${gone}`);
}

const wantOptions = ['all', 'today', 'yesterday', '7', '30', '365', 'day', 'custom'];
const haveOptions = [...html.matchAll(/<option value="([^"]*)"/g)].map(m => m[1]);
eq(wantOptions.every(v => haveOptions.includes(v)), true,
   '时间下拉包含全部 8 个选项: ' + haveOptions.join(','));
eq(html.indexOf('__DATA__') < 0, true, '数据占位符已被替换');
eq((html.match(/const DATA = \[/g) || []).length, 1, 'DATA 只注入一次');

for (const need of ['id="reToggle"', 'id="err"', 'class="toggle"', 'mark {', 'setRich(',
                    'setRegexMode(', 'readStateFromUI()']) {
  eq(html.indexOf(need) >= 0, true, `页面包含: ${need}`);
}
eq(html.indexOf("createElement('mark')") >= 0, true, '高亮用 createElement 生成，不拼 HTML 字符串');
const ihAssigns = [...html.matchAll(/\.innerHTML\s*=\s*([^;]+);/g)].map(m => m[1].trim());
eq(ihAssigns.length > 0 && ihAssigns.every(v => v === "''"), true,
   "innerHTML 只被赋值为空串，内容一律走 DOM 节点（" + ihAssigns.length + " 处）");

console.log('\n--- 布局（限宽与吸顶表头）---');
eq((html.match(/class="wrap"/g) || []).length, 2, '顶部栏与正文各有一个居中限宽容器');
eq(/<header>\s*<div class="wrap">/.test(html), true, '顶部栏内容包在 .wrap 里');
eq(/<main class="wrap">\s*<table>/.test(html), true, '表格包在 main.wrap 里');
eq(/--maxw:\s*\d+px/.test(html), true, '定义了内容最大宽度 --maxw');
eq(/th\s*{[^}]*top:\s*var\(--headh\)/s.test(html), true, '表头 sticky 用 --headh 而不是 top:0');
eq(html.indexOf('syncHeaderHeight()') >= 0, true, '有回填顶部栏高度的逻辑');
eq(/table-layout:\s*fixed/.test(html), true, '表格用固定布局，列宽可控');
eq(html.indexOf('@media') >= 0, true, '有窄屏适配');
eq(/td\.br\s*{[^}]*word-break/.test(html), true, '浏览器列允许换行，不会撑宽表格');
eq(/td\.br\s*{[^}]*white-space:\s*nowrap/.test(html), false, '浏览器列不再强制不换行');

console.log('\n--- 主题令牌（对齐 DSH 暗色主题）---');
const DSH = {
  '#151517': 'bg-base',
  '#232324': 'bg-layer-1',
  '#2c2c2e': 'bg-layer-2',
  '#353638': 'bg-layer-3',
  '#f9fafb': 'label-primary',
  '#cfd3d6': 'label-secondary',
  '#adb2b8': 'label-tertiary',
  '#81858c': 'label-caption',
  '#4d93f8': 'accent (static-blue-450)',
  '#f25a5a': 'danger (static-red-400)',
  '#f59e0b': 'warn (static-amber-500)',
  '#ffffff1f': 'border-l2',
  '#ffffff29': 'border-l3',
};
for (const [hex, name] of Object.entries(DSH)) {
  eq(html.indexOf(hex) >= 0, true, `含 DSH 令牌色 ${name} ${hex}`);
}
eq(/--radius-pill:\s*18px/.test(html), true, '按钮胶囊圆角 18px（DSH 按钮规范）');
eq(/--radius-md:\s*12px/.test(html), true, '12px 圆角（DSL 卡片规范）');
eq(/--radius-sm:\s*8px/.test(html), true, '8px 圆角（DSH 输入框规范）');
eq(html.indexOf('PingFang SC') >= 0, true, '字体栈含 PingFang SC');
eq(html.indexOf('JetBrains Mono') >= 0, true, '等宽字体栈含 JetBrains Mono');
eq(/cubic-bezier\(\.4,\s*0,\s*\.2,\s*1\)/.test(html), true,
   '动效曲线用 DSH 的 --ds-ease-in-out');

// 旧配色必须清干净，避免两套色系混用
for (const stale of ['#161a22', '#69a7ff', '#262c38', '#1e232d', '#98a2b5',
                     '#8b95a7', '#2c3342', '#12161d', '#1d3a70', '#cfe0ff']) {
  eq(html.indexOf(stale), -1, `旧配色已清除: ${stale}`);
}
eq(html.indexOf('color-mix('), -1, '没有用 color-mix（改用与 DSH 一致的 8 位 hex 透明度）');
eq(/(^|[^-])#0f1115/.test(html), true, '#0f1115 仍作为浅底深字的前景色保留');

console.log(fails ? `\n失败 ${fails} 项` : '\n全部通过');
process.exit(fails ? 1 : 0);
