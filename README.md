# 浏览器历史永久归档器

把本机所有浏览器的历史记录**增量**归档到一个本地 SQLite 数据库，永久保存。

浏览器自己只保留约 90 天历史，删掉就没了。这个工具每次运行都把新记录**追加**进归档库，
已经归档的记录永远不会被删除或覆盖——只要定期运行，历史就是永久留存的。

- 只依赖 Python 标准库，不需要 `pip install` 任何东西
- 不需要关闭浏览器：工具会先把被锁住的数据库整份复制出来再读
- 全部数据留在本地，不联网、不上传

---

## 快速开始

```powershell
# 1) 先看看能探测到哪些浏览器历史库
python history_archive.py detect

# 2) 归档一次（第一次会导入全部现有历史）
python history_archive.py sync

# 3) 查看历史 —— 生成网页版并用浏览器打开
python history_archive.py view
```

`view` 打开的页面自带搜索框、浏览器筛选、日期筛选，双击任何一条就能跳回原网页。
想导出成别的格式：

```powershell
python history_archive.py export          # 同时生成下面三个文件
```

`export` 会在 `archive/exports/` 下生成：

| 文件 | 说明 |
| --- | --- |
| `history.html` | 网页版历史记录，`view` 命令打开的就是它 |
| `history.csv` | 带 BOM 的 UTF-8，Excel 直接双击打开不乱码 |
| `history.jsonl` | 每行一个 JSON 对象，方便喂给别的程序 |

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

> **建议**：归档库放在 `E:\WorkStation\AI\web-history\archive\archive.sqlite`，
> 记得把它纳入你平时的备份（网盘 / 移动硬盘）。本工具保证不丢记录，但保证不了硬盘不坏。

---

## 命令一览

```
python history_archive.py [sync]            归档一次（不带参数时的默认命令）
python history_archive.py view              生成网页版历史记录并用浏览器打开
python history_archive.py detect            列出探测到的浏览器历史库
python history_archive.py stats             归档统计（按浏览器/年份/站点/页面）
python history_archive.py verify            校验归档库完整性
python history_archive.py export [选项]     导出 CSV / JSONL / HTML
python history_archive.py backup            一致性备份（可滚动保留 N 份）

全局选项:
  --archive DIR        归档目录，默认 <脚本目录>/archive
                       也可用环境变量 WEB_HISTORY_ARCHIVE 指定
                       （写在子命令前面或后面都可以）

sync 选项:
  -v, --verbose        打印详细日志
  --source chrome,edge 只归档指定浏览器
  --extra-root DIR     追加自定义的用户数据目录（浏览器装在非默认位置时用）

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

---

## 数据存在哪、长什么样

所有数据都在一个文件里：`archive/archive.sqlite`。

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

1. **网页版（推荐）**：`python history_archive.py view`
   会生成 `archive/exports/history.html` 并用默认浏览器打开。这个文件是自包含的，
   双击 `archive\exports\history.html` 也能直接打开，不依赖这个脚本。

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

## 文件清单

| 文件 | 作用 |
| --- | --- |
| `history_archive.py` | 主程序，全部功能都在这里 |
| `install-task.ps1` | 注册 / 卸载 Windows 计划任务 |
| `test_viewer.js` | 网页版筛选逻辑的回归测试（`node test_viewer.js`，需先跑过 `view`） |
| `archive/archive.sqlite` | **归档数据库本体，这就是你的永久历史** |
| `archive/exports/` | 导出的 CSV / JSONL / HTML |
| `archive/logs/` | 每次归档的日志 |
| `archive/backups/` | `backup` 命令产生的备份 |
