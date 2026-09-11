# 浏览器历史永久归档器

把本机所有浏览器的历史记录**增量**归档到一个本地 SQLite 数据库，永久保存。

浏览器自己只保留约 90 天历史，删掉就没了。这个工具每次运行都把新记录**追加**进归档库，
已经归档的记录永远不会被删除或覆盖——只要定期运行，历史就是永久留存的。

- 只依赖 Python 标准库，不需要 `pip install` 任何东西
- 不需要关闭浏览器：工具会先把被锁住的数据库整份复制出来再读
- 全部数据留在本地，不联网、不上传

## 环境要求

| 项 | 要求 | 说明 |
| --- | --- | --- |
| 操作系统 | **仅 Windows 10 / 11** | 浏览器的用户数据目录是按 Windows 的路径规则写死的，macOS / Linux 上 `detect` 会一个历史库都找不到 |
| Python | **3.8 或更高**，且要在 `PATH` 里 | 只用到标准库。在命令行里敲 `python --version` 能出版本号才算合格。已在 3.12 和 3.13 上实跑验证，语法层面兼容到 3.7 |
| Node.js | 可选 | 只有跑 `test_viewer.js` 这个前端筛选逻辑的测试才需要，日常使用完全不需要 |

**其它平台能不能用？** 归档核心逻辑（SQLite 读写、时间换算、导出）是与平台无关的，
要支持 macOS / Linux 主要改两处：各浏览器用户数据目录的路径表（`CHROMIUM_ROOTS`、
`FIREFOX_ROOTS`），以及 `install-task.ps1` 换成 cron / launchd。欢迎 PR。

---

## 快速开始

```powershell
# 1) 先看看能探测到哪些浏览器历史库
python history_archive.py detect

# 2) 归档一次（第一次会导入全部现有历史）
python history_archive.py sync

# 3) 查看历史 —— 起一个本地服务并打开
python history_archive.py view
```

`view` 会在 `127.0.0.1` 上起一个本地服务，用浏览器打开。按 `Ctrl+C` 停止。

**为什么用服务而不是静态文件**：页面要能写东西（TODO），静态 HTML 做不到。
存到浏览器 localStorage 又会跟浏览器绑定、清了缓存就没，也不在备份范围内。

### 两种模式

| | `serve` / `view`（默认） | `view --static` / `export` |
| --- | --- | --- |
| 数据 | 页面启动时从 `/api/rows` 拉 | 生成时内嵌进 HTML |
| 页面体积 | **67 KB** | 约 3 MB（内嵌 13,000 条） |
| 刷新 | 有「刷新」按钮，可顺带先同步一次 | 无，要重新生成 |
| TODO | 可读写，存进 `archive.sqlite` | 只读（页面会明确说明） |
| 依赖 | 要跑着服务 | 双击就能看，能发给别人 |

静态快照适合归档留档和分享；日常看用服务模式。

### 服务的安全边界

它只监听 `127.0.0.1`，局域网和外网都到不了。但同机的其它程序能连，
所以还有两道：

1. **每次启动生成一次性令牌**，写在打开的 URL 里。没有令牌一律 403
2. **校验 `Origin` + 强制 `Content-Type: application/json`**——
   别的网页即使猜到端口也发不出写请求（浏览器跨站只能发简单请求）

另外返回头里带了 `X-Frame-Options: DENY` 和 `nosniff`，令牌用 `hmac.compare_digest`
比对，避免时序侧信道。

> 归档目录默认是**脚本所在目录**下的 `archive/`，与你的用户名、盘符都无关，
> clone 到哪里就在哪里生成。需要换位置见下面「命令一览」里的 `--archive`。

### 词云

页面右上角可以在**列表**和**词云**之间切换。词云同样跟随当前筛选条件——
比如「时间选最近一年、浏览器选 Edge」再切到词云，看到的就是这一年的关注点。

- **来源**可选 `标题` / `站点` / `两者`：标题取页面标题里的词，站点取域名主体（`www.bilibili.com` → `bilibili`）
- **前 50 / 100 / 200（默认）/ 400 个词**，字号 11 ~ 96px 按频次做 0.75 次幂缩放，头尾差约 9 倍
  （幂指数是刻意选的：0.5 也就是开方会把中频词抬高、差距被压缩；1.0 线性又让长尾全挤到最小号）
- 画布高度取容器宽度的 0.69 倍（约 1140 × 790），切换窗口大小时自动重排
- **所有词一律横排**，不做竖排/旋转
- 大词小词共用同一套间距规则（padding 3px、行高 1.2）——字号相差 9 倍时，
  只有规则统一看上去才是一致的；按字号分支给不同间距反而会一边松一边挤
- **排布带随机性但结果可复现**：每个词从自己的随机方向开始找位置，落位后再抖动一下
  （挑四周最空的地方），所以整片不会顺着螺旋排成一圈一圈的死板结构。
  随机流是带种子的：**每次打开页面都会换一个初始相位**，所以不会每次都长一样；
  而同一次会话里相位保持不变，切筛选、搜关键词时词云不会乱跳。想手动换一个就点「重新摆放」
- **重新摆放**换个螺旋起点重排一次
- 鼠标悬停显示该词出现次数，**点击词语会切回列表并搜索它**
- 排布用螺线搜索 + 包围盒碰撞检测，放不下的词会被丢弃并在下方说明丢了多少个，绝不会叠在一起
  - 碰撞检测走**均匀网格索引**，否则 400 个词时每个候选点都要跟已放置的全部词比一遍，会卡到不能用
  - 螺线在大半径处的采样点间距会拉到上百像素，边缘的小空隙靠**随机补位**兜底（种子固定，结果可复现）
中文分词没有用任何第三方库（整个项目零依赖），走的是「n-gram + 频次挑选」：
先统计 2/3/4 字片段在全语料里的出现次数，再在每个汉字串里从左往右贪心取够高频的最长片段。
高频短语会被整体取出（`部落冲突`、`艾宾浩斯`、`星穹铁道`），低频碎片直接丢弃。
实测 13,000 条记录分词约 40ms，所以切换筛选条件时是实时的。

### TODO

页面右上角第三个标签页。两种加法：

- **从历史里加**：在「列表」里点某一行的 `＋`，这条链接就进了 TODO（标题一起带过去）
- **直接写**：在 TODO 页的输入框里敲，回车即可；以 `http` 开头的会被当成链接，其余当纯文字

其它：

- **标签**：输入框旁边可以填标签，之后点 `＋` 加进来的记录都会带上这个标签。
  已有标签会出现在下拉提示里，方便复用
- **未完成 / 全部 / 已完成** 三个视图切换，标签页上有个数字角标显示未完成数
- 每条可以勾选完成、就地编辑文字和标签、删除；「清空已完成」批量删除
- **导出 / 导入 JSON**：导出的 `todos.json` 可以再导回来，不会产生重复
  （按 id 去重，冲突时保留本地版本）

**数据存在哪**：`archive.sqlite` 里的 `todos` 表。所以它跟着 `backup` 一起被备份，
换浏览器、清缓存都不影响。静态快照里 TODO 是只读的（页面会明确说明）。

---

## 让「永久」真的成立

光跑一次脚本不算永久——浏览器的历史每天都在变。注册一个计划任务让它自动跑：

```powershell
powershell -ExecutionPolicy Bypass -File install-task.ps1 -IntervalHours 6 -RunNow
```

- 登录后 3 分钟跑一次，之后每 6 小时一次
- 用 `pythonw.exe` 运行，不弹黑窗口，不需要管理员权限
- 日志写在 `archive/logs/sync-YYYYMMDD.log`

其他管理命令：

```powershell
Get-ScheduledTask -TaskName WebHistoryArchive | Get-ScheduledTaskInfo   # 看上次运行结果
Start-ScheduledTask -TaskName WebHistoryArchive                         # 立刻跑一次
powershell -ExecutionPolicy Bypass -File install-task.ps1 -Uninstall   # 删除任务
```

> **建议**：归档库是**单个文件** `<脚本目录>\archive\archive.sqlite`，
> 记得把它纳入你平时的备份（网盘 / 移动硬盘）。本工具保证不丢记录，但保证不了硬盘不坏。

### 自我引用会被自动排除

工具自己的页面被打开时，**浏览器也会把这次打开记进历史**，下次 `sync` 又把它归档回来——
一条没有任何信息量的记录，还会越滚越多。两种模式都要管：

| 模式 | 页面地址 | 怎么认出来 |
| --- | --- | --- |
| 静态快照 | `file:///…/archive/exports/history.html` | `file:` 协议 + 路径在归档目录下 |
| 服务模式 | `http://127.0.0.1:<端口>/history/?t=<令牌>` | 回环地址 + 固定路径 `/history/` |

服务模式这条是踩过坑才补上的：**端口和令牌每次启动都不一样**，光靠它们认不出来。
所以页面被固定在 `/history/` 路径下；访问根路径会 302 跳过去。

sync 结束时会告诉你排除了几条：

```
已排除 77 条自身产生的记录（归档页面被自己打开产生的访问）
```

**别人的本地服务不会被误伤**：`localhost:3000`、`localhost:5173`、`127.0.0.1:3080`
这些都不带 `/history/`，照常归档。`test_filters.py` 里有 13 条专门盯着这件事的断言，
包括 `https://127.0.0.1.evil.com/history/` 这种域名伪装。

如果归档目录之外还有同类页面（比如别的工具生成的导出页），**加一条持久排除关键字**：

```powershell
python history_archive.py exclude                              # 看当前有哪些
python history_archive.py exclude bili-history.html            # 加一条
python history_archive.py exclude --remove bili-history.html   # 删一条
python history_archive.py exclude --clear                      # 清空
```

配置写在 `<归档目录>/exclude.txt`（一行一个关键字，包含即排除，不区分大小写）。

> **这一点很关键**：计划任务跑的是不带参数的 `sync`，命令行上的 `--exclude` 根本传不进去，
> 只有这份文件会被自动读取。`--exclude` 参数仍然可用，它和文件里的关键字**叠加**，不互相覆盖。

已经归档进去的存量记录，用 `purge` 清掉——**默认只预演**，确认后才加 `--yes`，
而且删除前会自动备份一份：

```powershell
python history_archive.py purge          # 列出会删什么，什么都不动
python history_archive.py purge --yes    # 真正执行（先自动备份）
```

---

## 命令一览

```
python history_archive.py [sync]            归档一次（不带参数时的默认命令）
python history_archive.py serve             启动本地服务（不自动开浏览器）
python history_archive.py view              启动本地服务并打开浏览器（默认）
python history_archive.py detect            列出探测到的浏览器历史库
python history_archive.py stats             归档统计（按浏览器/年份/站点/页面）
python history_archive.py verify            校验归档库完整性
python history_archive.py export [选项]     导出 CSV / JSONL / HTML
python history_archive.py backup            一致性备份（可滚动保留 N 份）
python history_archive.py purge             清除归档里工具自身产生的记录（默认只预演）
python history_archive.py exclude [关键字]   管理额外排除的关键字（持久生效，计划任务会用）

全局选项:
  --archive DIR        归档目录，默认 <脚本目录>/archive
                       也可用环境变量 WEB_HISTORY_ARCHIVE 指定
                       （写在子命令前面或后面都可以）

serve / view 选项:
  --port N             服务端口，默认自动挑一个空闲的
  -v, --verbose        打印每个请求
  --no-open            只启动服务，不打开浏览器
  --static             （仅 view）不起服务，生成自包含的只读快照

sync 选项:
  -v, --verbose        打印详细日志
  --source chrome,edge 只归档指定浏览器
  --extra-root DIR     追加自定义的用户数据目录（浏览器装在非默认位置时用）
  --exclude 关键字     额外排除包含该关键字的链接（可重复）
  --no-self-exclude    不排除归档目录下的链接（默认会排除，见下）

purge 选项:
  --yes                真正执行删除（默认只预演，什么都不删）
  --no-backup          删除前不备份（不建议）
  --exclude / --no-self-exclude   含义同 sync

exclude 选项:
  关键字…              要添加的关键字（一行一个，包含即排除，不区分大小写）
  --remove             删除这些关键字而不是添加
  --clear              清空全部关键字

view 选项:
  --since / --until YYYY-MM-DD   只放进这个时间范围的记录
  --browser Edge,Chrome          只放指定浏览器
  --contains 关键词              只放 URL/标题含该关键词的记录
  --limit N                      最多放进去 N 条
  --no-open                      只生成文件，不自动打开浏览器

export 选项:
  --format csv,jsonl,html   导出格式（默认全部）
  --out DIR                 导出目录，默认 <归档>/exports
  --since / --until YYYY-MM-DD   时间范围
  --browser Edge,Chrome          只导出指定浏览器
  --contains 关键词              只导出 URL/标题含该关键词的记录
  --limit N                      最多导出 N 条
  --snapshot                     额外存一份带时间戳的快照，不覆盖旧文件

backup 选项:
  --keep N             保留最近 N 份备份（默认 30，0 表示不清理）
```

---

## 支持的浏览器

**Chromium 系**：Chrome（含 Beta / Dev / Canary）、Edge（含 Beta / Dev / Canary）、
Brave、Vivaldi、Chromium、Yandex、Opera / Opera GX、360 极速浏览器、
QQ 浏览器、搜狗浏览器、CocCoc。

**Firefox**：自动读取 `profiles.ini`，所有配置文件都会被归档。

探测不到的浏览器用 `--extra-root` 指定它的用户数据目录即可，例如：

```powershell
python history_archive.py sync --extra-root "D:\Portable\Chrome\User Data"
```

### 实测覆盖情况

这个表是诚实的现状，不是宣传语：

| 浏览器 | 验证方式 |
| --- | --- |
| Chrome、Edge | **在真实机器上实机验证**（含浏览器正在运行、数据库被锁的情况） |
| Firefox | 用真实 `places.sqlite` schema 造的合成库跑通全流程（`test_firefox.py`，26 项断言）。**没有在真正装过 Firefox 的机器上实机跑过**，遇到问题欢迎提 issue |
| 其余 Chromium 系 | 走的是与 Chrome / Edge 完全相同的代码路径，但未逐一实机验证 |

---

## 数据存在哪、长什么样

所有数据都在一个文件里：`<归档目录>/archive.sqlite`（默认 `archive/archive.sqlite`）。

| 表 | 内容 |
| --- | --- |
| `visits` | 每条访问记录：时间（原始微秒 / UTC / 本地 / 日期）、类型、停留时长、是否手输、重复次数 |
| `urls` | 每个 URL 的首次/最近访问时间、最新标题、站点域名 |
| `titles` | 同一个 URL **历史上出现过的所有标题**，永久保留 |
| `sources` | 每个被归档的浏览器配置及其最近一次归档状态 |
| `sync_runs` | 每次归档的运行记录 |

自己用 SQL 查也很方便：

```sql
-- 最近 7 天访问最多的站点
SELECT u.host, SUM(v.dup_count) AS n
FROM visits v JOIN urls u ON u.id = v.url_id
WHERE v.day >= date('now','-7 day')
GROUP BY u.host ORDER BY n DESC LIMIT 20;

-- 凌晨 1-5 点到底在刷什么
SELECT substr(v.visit_time_local,12,2) AS hh, u.host, COUNT(*) n
FROM visits v JOIN urls u ON u.id = v.url_id
WHERE hh BETWEEN '01' AND '05'
GROUP BY hh, u.host ORDER BY n DESC LIMIT 20;

-- 某个页面历史上改过哪些标题
SELECT t.title, t.first_seen_utc, t.last_seen_utc
FROM titles t JOIN urls u ON u.id = t.url_id
WHERE u.url LIKE '%github.com/yourname%';
```

---

## 工作原理与几个设计取舍

1. **先复制再读**。浏览器运行时会对 `History` 加锁，所以工具把它（连同 `-wal` / `-shm`）
   复制到 `archive/_staging/` 再打开，读完即删。不碰原始文件，零风险。
2. **增量 + 幂等**。去重键是「浏览器配置 + 原始时间戳 + URL + 访问类型」。
   反复运行不会产生重复记录，也不会丢记录。
3. **重复访问计数**。浏览器里偶尔存在 URL、时间戳、类型完全一样的多行（Edge 上实测有），
   `visits.dup_count` 记录归档时观察到的重数，取最大值合并。所以
   `SUM(dup_count)` 才是真实访问次数，`COUNT(*)` 是数据行数。
4. **单个浏览器失败不影响其他**。每个浏览器一个独立事务，Edge 出了错 Chrome 照常归档，
   失败原因写进 `sources.last_message`。
5. **删掉的记录不会被归档删除**。归档只做插入和更新，从不做删除（`backup --keep` 除外，
   那只清理备份文件）。

---

## 常见问题

**Q: 我要怎么查看自己的浏览记录？**
三种方式，按推荐顺序：

1. **服务模式（推荐）**：`python history_archive.py view`
   起本地服务并用浏览器打开，按 `Ctrl+C` 停止。数据实时从归档库读，
   有「刷新」按钮（勾上「先同步」可以顺带跑一次 sync 再刷新）。

   页面上的筛选：
   - **搜索框**：URL 和标题一起搜，大小写不敏感
   - **`.*` 按钮**：点一下切换成**正则表达式**搜索（见下）
   - **浏览器**：只在多个浏览器都有记录时才需要
   - **时间**：一个下拉搞定所有时间筛选
     - 快捷范围：今天 / 昨天 / 最近 7 天 / 最近 30 天 / 最近一年
     - `指定某一天…`：选完会在下面展开一个日期框，默认停在你最近有记录的那天
     - `自定义范围…`：展开起止两个日期框，只填一端就是"从某天起 / 到某天止"
     - 日期框的可选范围被限制在归档实际覆盖的日期内，选不到空日子
   - **条件标签**：凡是生效的条件都会在下方显示成标签，点标签上的 `×` 可以单独去掉，
     也可以点「全部清除」。这样不会有"明明选了条件却查不到东西还不知道为什么"的情况
   - 命中 0 条时页面会直接提示并给一个「清空筛选，看全部」的按钮

### 正则表达式搜索

点搜索框右边的 `.*` 按钮切换。开启后输入框会变蓝、提示文字也会变，一眼能看出当前是正则模式。

被匹配的文本是 `URL + 空格 + 标题` 拼起来的一整串，所以两种写法都成立：

| 输入 | 含义 |
| --- | --- |
| `deepseek\|openai` | 命中任意一个 |
| `^https://github\.com` | 只匹配 URL 以它开头（URL 在串首，`^` 可用） |
| `\.pdf$` | 以 .pdf 结尾（注意点在正则里要转义） |
| `(bilibili\|b23)\.tv` | 分组 |
| `/DeepSeek/g` | 写成 `/模式/flags` 形式时，flags 完全按你写的来——这个例子里没写 `i`，所以**区分大小写** |
| `/2026-09-\d\d/g` | 只匹配 2026 年 9 月的日期字符串 |

规则说明：

- 直接写模式（不带斜杠）时自动忽略大小写；写 `/模式/flags` 时 flags 完全由你决定，
  这也是需要区分大小写时的办法
- **正则写错时会红字提示具体原因**，并停止筛选（不会悄悄给你一个空列表），
  改好或者关掉 `.*` 开关即可恢复
- 命中的片段会在标题和链接里**高亮**显示
- 普通模式下的 `.` 是普通字符（搜 `a.b` 不会命中 `axb`），只有开了正则才当元字符用

想验证筛选逻辑有没有被改坏，跑 `node test_viewer.js`（80+ 项断言，含时区、跨年、
`g` 标志状态污染、空匹配死循环等边界）。
2. **命令行统计**：`python history_archive.py stats`
   看总条数、时间跨度、按年份分布、访问最多的站点和页面。
3. **自己写 SQL**：用任何 SQLite 工具打开 `archive/archive.sqlite`，
   表结构和示例查询见上面「数据存在哪、长什么样」一节。

注意 `view` 和 `export` 生成的是**生成那一刻的快照**。归档库本身每天自动更新，
但 HTML 页面不会自动跟着变——想看最新记录，重新跑一次 `view` 就行。

**Q: 需要管理员权限吗？**
不需要。所有路径都在当前用户目录下，计划任务也是当前用户级别的。

**Q: 会不会被浏览器或杀毒软件当成恶意软件？**
它就是普通的 Python 脚本读取你自己的历史库。不过某些安全软件会盯着「读取浏览器数据库」
这个行为，如果被拦了，把脚本目录加进白名单即可。

**Q: 归档库会不会越来越大？**
访问记录本身很小。实测 Edge 两万多条记录约 7 MB。按这个量级，几十万条也就几十 MB。

**Q: 我删掉了浏览器里的历史，归档里还会有吗？**
会有。归档只增不减，这正是它的意义。

**Q: 想换归档位置？**
`python history_archive.py sync --archive D:\history-archive`，
或者设环境变量 `WEB_HISTORY_ARCHIVE=D:\history-archive`。

**Q: 提示「表结构来自旧版本」？**
归档库自带的版本号与当前脚本不匹配。备份后删除 `archive/archive.sqlite`
（或整个 `archive` 目录），重新 `sync` 即可；浏览器里的历史还在，能重新导入。

---

## 给开发者：怎么跑测试

改动代码后跑这两个脚本，别把已经修好的坑再踩回去：

```powershell
python test_filters.py    # 链接过滤（自我引用判定、路径归一化，26 项断言）
python test_firefox.py    # Firefox 归档路径（合成 places.sqlite，26 项断言）
python test_server.py     # 本地服务（起真服务打接口：鉴权、跨站、落库，44 项断言）
python history_archive.py view --static --no-open
node test_viewer.js       # 页面纯逻辑（筛选/正则/词云/TODO 展示，253 项断言）
```

`test_server.py` 会真的把服务起在随机端口上，用 `urllib` 打进去验——
包括"不带令牌必须 403""跨站 Origin 必须被拒""TODO 必须真的落到 archive.sqlite 而不是只在内存里"。它已经抓到过一个真 bug：`ThreadingHTTPServer` 每个请求一个线程，而 SQLite 连接默认不允许跨线程使用。

`test_viewer.js` 从生成的 HTML 里抽出标记为 `__FILTER_LOGIC_START__` /
`__FILTER_LOGIC_END__` 的纯函数区间来测，所以它验证的是**页面里真正在跑的那份代码**，
不是副本。

覆盖到的坑包括：东八区下 `toISOString()` 取 UTC 日期导致「今天」错一天、
带 `g` 标志的正则 `test()` 状态污染、`a*` 类正则的空匹配死循环、
普通模式下 `.` 必须当字面量、Firefox 的 1970 微秒时间基准等。

---

## 文件清单

| 文件 | 作用 |
| --- | --- |
| `history_archive.py` | 主程序：探测、归档、统计、服务 |
| `viewer.html` | **网页本体**（HTML/CSS/JS），由 Python 读取后填充数据 |
| `install-task.ps1` | 注册 / 卸载 Windows 计划任务 |
| `test_filters.py` | 链接过滤测试（`python test_filters.py`） |
| `test_firefox.py` | Firefox 归档路径的测试（`python test_firefox.py`） |
| `test_server.py` | 本地服务的测试（`python test_server.py`） |
| `test_viewer.js` | 页面纯逻辑的测试（`node test_viewer.js`） |
| `LICENSE` | MIT |
| `<归档目录>/archive.sqlite` | **归档数据库本体，这就是你的永久历史** |
| `<归档目录>/exclude.txt` | 额外排除关键字 |
| `<归档目录>/exports/` | 导出的 CSV / JSONL / HTML |
| `<归档目录>/logs/` | 每次归档的日志 |
| `<归档目录>/backups/` | `backup` 命令产生的备份 |

> `viewer.html` 是网页本体，必须和 `history_archive.py` 放在同一目录。
> 它不被打包进 Python 源码，改完**刷新页面就生效，服务不用重启**。

> 归档目录默认是脚本同级的 `archive/`，已被 `.gitignore` 排除——
> **你的浏览记录不会被提交进 git**，clone 和推送的永远只有源码。

---

## 许可证

[MIT](LICENSE)。随便用、随便改、随便分发，保留版权声明即可。

