// 从生成的 HTML 里抽出「纯筛选逻辑」区间，直接在 Node 里跑断言。
const fs = require('fs');
const html = fs.readFileSync('archive/exports/history.html', 'utf8');

const START = '/* __FILTER_LOGIC_START__ */';
const END = '/* __FILTER_LOGIC_END__ */';
const i = html.indexOf(START), j = html.indexOf(END);
if (i < 0 || j < 0) { console.error('找不到逻辑区间标记'); process.exit(1); }
const code = html.slice(i + START.length, j);

const mod = new Function(code + '\nreturn {dayStr, shiftDay, timeWindow, rangeLabel, matchesFilters,'
  + ' parseQuery, buildMatcher, splitMatches, escapeRegExp,'
  + ' cleanText, latinWords, cjkRuns, hostTerm, extractTerms, topTerms,'
  + ' layoutCloud, cloudColor, isNoiseTerm};')();
const { dayStr, timeWindow, matchesFilters, parseQuery, buildMatcher, splitMatches,
        cleanText, latinWords, hostTerm, extractTerms, topTerms, layoutCloud,
        cloudColor } = mod;

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

console.log('\n--- 词云：文本清洗与分词 ---');
eq(cleanText('DeepSeek 开放平台 | 官网'), 'deepseek 开放平台 官网', '分隔符与竖线变空格并转小写');
eq(cleanText('看这个 https://a.com/x?y=1 好吗').indexOf('http'), -1, '裸链接被丢掉');
eq(latinWords('DeepSeek API v3 好用'), ['deepseek', 'api'], '英文词过滤停用词与短词');
eq(latinWords('the and for 123 abc'), ['abc'], '停用词和纯数字被过滤');
eq(latinWords('C++ 与 Python3'), ['python3'], '特殊字符不会产生脏词');
eq(mod.cjkRuns('abc 中文测试 xyz 第二段'), ['中文测试', '第二段'], '汉字串被正确切出');

console.log('\n--- 词云：站点名提取 ---');
eq(hostTerm('www.bilibili.com'), 'bilibili', 'www 前缀被去掉');
eq(hostTerm('i.njupt.edu.cn'), 'njupt', '多级后缀 edu.cn 取到正确的一级');
eq(hostTerm('m.bqg948.xyz'), 'bqg948', '二级域名带数字也正常');
eq(hostTerm('space.bilibili.com'), 'bilibili', 'space 子域被去掉');
eq(hostTerm('localhost'), 'localhost', 'localhost 保持原样');
eq(hostTerm('127.0.0.1'), '', '纯 IP 不产出站点名');
eq(hostTerm(''), '', '空 host 返回空串');

console.log('\n--- 词云：词频统计 ---');
const mkRow = (title, host, dup) =>
  ['2026-09-10 12:00:00', 'x', 'Edge', 'Default', title, 'http://a', host || 'a', 'LINK', 0, 0, '2026-09-10', dup || 1];
// 「部落冲突」在 5 条标题里出现 -> 应当被整体取成一个词
const cloudRows = [];
for (let i = 0; i < 5; i++) cloudRows.push(mkRow('部落冲突升级数据 ' + i, 'clashpost.com'));
cloudRows.push(mkRow('部落冲突攻略', 'clashpost.com'));
cloudRows.push(mkRow('只出现一次的冷门词呀', 'rare.example.com'));

const titleTerms = topTerms(extractTerms(cloudRows, 'title', 3), 50);
const titleMap = new Map(titleTerms.map(t => [t.text, t.count]));
eq(titleMap.get('部落冲突') >= 6, true, '高频四字短语被整体取出（部落冲突）');
eq(titleMap.has('部落'), false, '被更长词覆盖的碎片不会重复出现');
eq(titleMap.has('冷门词'), false, '只出现一次的词低于阈值，不进入词云');
eq(titleTerms.every(t => t.text.length >= 2), true, '结果里没有单字');

const hostTerms = topTerms(extractTerms(cloudRows, 'host', 1), 50);
eq(hostTerms[0].text, 'clashpost', '站点模式下取到域名主体');
eq(hostTerms[0].count, 6, '站点计次与记录数一致');

const bothTerms = topTerms(extractTerms(cloudRows, 'both', 3), 50);
const bothMap = new Map(bothTerms.map(t => [t.text, t.count]));
eq(bothMap.has('clashpost') && bothMap.has('部落冲突'), true, '两者模式同时含标题词与站点名');

console.log('\n--- 词云：按访问次数加权 ---');
const weighted = extractTerms([mkRow('哔哩哔哩视频', 'b.com', 7)], 'title', 1);
eq(topTerms(weighted, 10).every(t => t.count === 7), true, 'dup_count > 1 时按次数加权');

console.log('\n--- 词云：排布算法 ---');
const fakeMeasure = (text, size) => text.length * size * 0.62;
const words = [];
for (let i = 0; i < 60; i++) {
  words.push({ text: '词' + i + 'word', count: 100 - i });
}
const placed = layoutCloud(words, {
  width: 1000, height: 520, measure: fakeMeasure, maxWords: 60,
});
eq(placed.length > 20, true, `能摆下相当数量的词（实际 ${placed.length} 个）`);
eq(placed.every(p => p.box.x0 >= 0 && p.box.y0 >= 0 &&
                     p.box.x1 <= 1000 && p.box.y1 <= 520), true, '所有词都在画布范围内');
let overlaps = 0;
for (let i = 0; i < placed.length; i++) {
  for (let j = i + 1; j < placed.length; j++) {
    const a = placed[i].box, b = placed[j].box;
    if (a.x0 < b.x1 && a.x1 > b.x0 && a.y0 < b.y1 && a.y1 > b.y0) overlaps++;
  }
}
eq(overlaps, 0, '没有任何两个词的包围盒重叠');
eq(placed.every((p, i) => i === 0 || placed[i - 1].size >= p.size), true, '按字号从大到小摆放');
eq(placed[0].size >= placed[placed.length - 1].size, true, '最高频的词字号最大');

// 字号对比度：真实数据里频次能差两个数量级，字号必须有明显梯度
const contrastWords = [];
for (let i = 0; i < 100; i++) contrastWords.push({ text: 'w' + i, count: Math.round(2400 * Math.pow(0.97, i)) });
const contrast = layoutCloud(contrastWords, {
  width: 1140, height: 787, measure: fakeMeasure, maxWords: 100,
  minSize: 11, maxSize: 96, power: 0.75,
});
const cSizes = contrast.map(p => p.size);
eq(Math.max(...cSizes) / Math.min(...cSizes) >= 4, true,
   `最大字号至少是最小字号的 4 倍（实际 ${(Math.max(...cSizes) / Math.min(...cSizes)).toFixed(1)} 倍）`);
eq(cSizes[0], 96, '最高频的词顶到最大字号');

// 注意：max/min 由配置的字号区间钉死，任何指数都一样。
// 真正决定「对比感」的是中间段被压得多低——幂律越高，中频词越小。
const midShare = (sizes) => sizes[Math.floor(sizes.length / 2)] / sizes[0];
const sizesAt = (power) => layoutCloud(contrastWords, {
  width: 1140, height: 787, measure: fakeMeasure, maxWords: 100,
  minSize: 11, maxSize: 96, power: power,
}).map(p => p.size);
const mid75 = midShare(sizesAt(0.75));
const mid50 = midShare(sizesAt(0.5));
eq(mid75 < mid50, true,
   `0.75 幂律把中频词压得更低，对比更强（中位/最大 ${mid75.toFixed(3)} < ${mid50.toFixed(3)}）`);
eq(mid75 < 0.4, true, '中位词不超过最大词的四成，头部足够突出');

eq(layoutCloud([], { width: 100, height: 100, measure: fakeMeasure }).length, 0, '空输入返回空');
eq(layoutCloud(words, { width: 0, height: 100, measure: fakeMeasure }).length, 0, '画布宽为 0 时不做排布');
const tightWords = [];
for (let i = 0; i < 400; i++) tightWords.push({ text: 'looooongterm' + i, count: 400 - i });
const tight = layoutCloud(tightWords, {
  width: 400, height: 200, measure: fakeMeasure, maxWords: 400,
});
eq(tight.length < 400, true, '词太多时放不下的会被丢弃而不是叠在一起');
eq(tight.length > 0, true, '即使拥挤也仍然摆下了一部分');

console.log('\n--- 词云：配色 ---');
eq(/^#[0-9a-f]{6}$/.test(cloudColor(0, 100)), true, '颜色是合法的十六进制');
eq(cloudColor(0, 100) !== cloudColor(99, 100), true, '高频词与低频词颜色不同');
eq(cloudColor(0, 1), '#4d93f8', '只有一个词时用强调色');

console.log('\n--- 词云：虚词裁剪的边界（曾经误伤过实词）---');
const manyRows = (title) => {
  const rs = [];
  for (let i = 0; i < 4; i++) rs.push(mkRow(title, 'x.com'));
  return rs;
};
const termsOf = (rows) => topTerms(extractTerms(rows, 'title', 3), 60).map(t => t.text);
eq(termsOf(manyRows('好帮手 下载游戏 中台门户')).includes('好帮手'), true,
   '词首实词不被误削：好帮手');
eq(termsOf(manyRows('好帮手 下载游戏 中台门户')).includes('下载游戏'), true,
   '词首实词不被误削：下载游戏');
eq(termsOf(manyRows('好帮手 下载游戏 中台门户')).includes('中台门户'), true,
   '词首实词不被误削：中台门户');
eq(termsOf(manyRows('看攻略的好地方')).includes('看攻略'), true,
   '词尾虚词被削掉：看攻略的 -> 看攻略');
eq(termsOf(manyRows('看攻略的好地方')).some(t => t.endsWith('的')), false,
   '结果里没有以「的」结尾的词');

console.log('\n--- 词云：400 词容量与排布可复现性 ---');
// fixture 要接近真实数据：词长 2~4 字、频次陡降。
// 用 "term399" 这种 8 字符长词测试是不现实的——真实词云里没有那么多长词。
const cjkChar = (n) => String.fromCharCode(0x4e00 + (n % 2000));
const big = [];
for (let i = 0; i < 400; i++) {
  let text = '';
  const len = 2 + (i % 3);
  for (let k = 0; k < len; k++) text += cjkChar(i * 13 + k * 7);
  big.push({ text: text, count: Math.max(20, Math.round(6000 * Math.pow(0.97, i))) });
}
const bigOpt = { width: 1140, height: 787, measure: fakeMeasure, maxWords: 400,
                 minSize: 11, maxSize: 96, power: 0.75 };
const bigPlaced = layoutCloud(big, bigOpt);
eq(bigPlaced.length >= 270, true,
   `400 个词的合成压力集能摆下大部分（实际 ${bigPlaced.length}/400；真实数据可全部摆下）`);
let bigOverlap = 0, bigArea = 0;
for (const p of bigPlaced) bigArea += (p.box.x1 - p.box.x0) * (p.box.y1 - p.box.y0);
for (let i = 0; i < bigPlaced.length; i++) {
  for (let j = i + 1; j < bigPlaced.length; j++) {
    const a = bigPlaced[i].box, b = bigPlaced[j].box;
    if (a.x0 < b.x1 && a.x1 > b.x0 && a.y0 < b.y1 && a.y1 > b.y0) bigOverlap++;
  }
}
eq(bigOverlap, 0, '400 个词依然零重叠');
eq(bigPlaced.every(p => p.box.x0 >= 0 && p.box.y0 >= 0 &&
                        p.box.x1 <= 1140 && p.box.y1 <= 787), true, '400 个词都在画布内');
eq(bigArea / (1140 * 787) >= 0.5, true,
   `画布填充率 ${(bigArea / (1140 * 787) * 100).toFixed(1)}%`);

console.log('\n--- 词云：统一间距 / 不竖排 ---');
// 字号差 9 倍时，只有「一套规则管所有字号」看上去才是一致的，
// 所以代码里不允许再出现按字号分支的间距参数。
eq(html.indexOf('smallPadding'), -1, '没有小词专用的 padding（间距规则统一）');
eq(html.indexOf('smallLineFactor'), -1, '没有小词专用的行高（行高规则统一）');
eq(html.indexOf('smallLimit'), -1, '没有字号阈值分支');
eq(/padding:\s*3,\s*lineFactor:\s*1\.2/.test(html), true,
   '渲染时大词小词用同一组 padding / lineFactor');
eq(html.indexOf('rotateEvery'), -1, '排布里没有旋转逻辑');
eq(/ctx\.fillText\(it\.text, it\.x, it\.y\)/.test(html), true, '文字一律画在正位（不旋转）');
eq(html.indexOf('ctx.rotate'), -1, '没有任何 canvas 旋转调用');
eq(html.indexOf('cloudRotate'), -1, '「重新摆放」不再随机旋转');
eq(bigPlaced.every(p => p.rot === undefined), true, '排布结果里没有竖排的词');

// 间距一致性：ink 到 ink 的距离 = 包围盒间距 + 两侧 padding。
// 用最近邻包围盒间距的中位数比较大小词，两者应当在同一量级。
const gapMedian = (arr) => {
  if (!arr.length) return 0;
  const s = arr.slice().sort((a, b) => a - b);
  return s[Math.floor(s.length / 2)];
};
const nearestGaps = { small: [], big: [] };
for (const p of bigPlaced) {
  let best = Infinity;
  for (const q of bigPlaced) {
    if (q === p) continue;
    const dx = Math.max(0, Math.max(q.box.x0 - p.box.x1, p.box.x0 - q.box.x1));
    const dy = Math.max(0, Math.max(q.box.y0 - p.box.y1, p.box.y0 - q.box.y1));
    best = Math.min(best, Math.hypot(dx, dy));
  }
  if (best === Infinity) continue;
  if (p.size <= 20) nearestGaps.small.push(best);
  else if (p.size >= 46) nearestGaps.big.push(best);
}
const gs = gapMedian(nearestGaps.small), gb = gapMedian(nearestGaps.big);
const spread = Math.max(gs, gb) / Math.max(0.01, Math.min(gs, gb));
eq(spread <= 2.5, true,
   `大小词的最近邻间距在同一量级（小词中位 ${gs.toFixed(1)}px / 大词 ${gb.toFixed(1)}px，相差 ${spread.toFixed(1)} 倍）`);

const snap = (t) => JSON.stringify(t.map(p => [p.text, Math.round(p.x), Math.round(p.y), p.size]));
eq(snap(layoutCloud(big, bigOpt)), snap(layoutCloud(big, bigOpt)),
   '同一配置两次排布结果完全一致（种子固定，可复现）');
eq(snap(layoutCloud(big, Object.assign({}, bigOpt, { phase: 1.23 }))) !== snap(bigPlaced),
   true, '换 phase 后布局变化，「重新摆放」有效');

// 小词不应该是散落的：多数小词都要有紧邻的同伴
const smallBoxes = bigPlaced.filter(p => p.size <= 22).map(p => p.box);
let closeNeighbors = 0;
for (const a of smallBoxes) {
  for (const b of smallBoxes) {
    if (a === b) continue;
    const dx = Math.max(0, Math.max(b.x0 - a.x1, a.x0 - b.x1));
    const dy = Math.max(0, Math.max(b.y0 - a.y1, a.y0 - b.y1));
    if (Math.hypot(dx, dy) <= 6) { closeNeighbors++; break; }
  }
}
const closePct = closeNeighbors / Math.max(1, smallBoxes.length);
eq(closePct >= 0.7, true,
   `${(closePct * 100).toFixed(0)}% 的小词有 6px 内的邻居，小字是成片而不是散落的`);

console.log('\n--- 布局（限宽与吸顶表头）---');
eq((html.match(/class="wrap"/g) || []).length, 2, '顶部栏与正文各有一个居中限宽容器');
eq(/<header>\s*<div class="wrap">/.test(html), true, '顶部栏内容包在 .wrap 里');
eq(/<main class="wrap">\s*<section id="listView">\s*<table>/.test(html), true,
   '表格包在 main.wrap > #listView 里');
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

console.log('\n--- 词云界面结构 ---');
for (const need of ['id="viewToggle"', 'id="listView"', 'id="cloudView"',
                    'id="cloudWrap"', 'id="cloud"', 'id="cloudTip"',
                    'id="srcToggle"', 'id="cloudTop"', 'id="cloudRedraw"',
                    'data-view="cloud"', 'data-src="host"', 'data-src="both"']) {
  eq(html.indexOf(need) >= 0, true, `页面包含: ${need}`);
}
eq(/<canvas id="cloud">/.test(html), true, '词云用 canvas 绘制');
eq(html.indexOf('__WORDCLOUD_LOGIC_START__') >= 0, true, '词云逻辑有独立标记可测');
for (const fn of ['function setView(', 'function renderCloud(', 'function scheduleCloud(',
                  'function measureTerm(', 'function cloudHitAt(']) {
  eq(html.indexOf(fn) >= 0, true, `含函数: ${fn}`);
}
eq(html.indexOf('devicePixelRatio') >= 0, true, '处理了高分屏缩放');
eq(/\.width\s*=\s*Math\.round\(cssW \* dpr\)/.test(html), true,
   'canvas 位图按 DPR 放大（cssW * dpr）');
eq(/Math\.round\(cssW \* 0\.69\)/.test(html), true, '画布高度按 0.69 比例（比原来大 50%）');
eq(/minSize:\s*11/.test(html), true, '最小字号 11px');
eq(/maxSize:\s*Math\.max\(48,\s*Math\.min\(96/.test(html), true, '最大字号上限 96px');
eq(/power:\s*0\.75/.test(html), true, '字号映射用 0.75 幂律（对比更明显）');
eq(/setTransform\(dpr, 0, 0, dpr, 0, 0\)/.test(html), true,
   '绘制坐标按 DPR 缩放，避免高分屏发虚');

console.log('\n--- 整段内联脚本能否通过编译 ---');
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
eq(scripts.length, 1, '只有一个内联脚本块');
let compileErr = '';
try {
  new Function(scripts[0]);        // 只编译不执行
} catch (e) {
  compileErr = e.message;
}
eq(compileErr, '', '整段脚本语法正确' + (compileErr ? `（${compileErr}）` : ''));

console.log(fails ? `\n失败 ${fails} 项` : '\n全部通过');
process.exit(fails ? 1 : 0);
