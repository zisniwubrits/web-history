#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
web-history-archive —— 浏览器历史永久归档器
=============================================

把本机各个浏览器的历史记录 **增量** 归档到一个本地 SQLite 数据库，永久保存。
浏览器自己会在 90 天左右清理历史，本工具每次运行都把新记录追加进归档，
已经归档的记录永远不会被删除，所以只要定期运行，历史就是永久留存的。

支持的浏览器
------------
  Chromium 系: Chrome / Chrome Beta / Dev / Canary、Edge (含 Beta/Dev/Canary)、
               Brave、Vivaldi、Chromium、Yandex、Opera、Opera GX、
               360 极速浏览器、QQ 浏览器、搜狗浏览器、CocCoc …
  Firefox   : 自动读取 profiles.ini 里的所有配置文件

只依赖 Python 标准库，不需要 pip 安装任何东西。

常用命令
--------
  python history_archive.py                  # 归档一次（默认命令）
  python history_archive.py detect           # 只探测，列出发现的浏览器历史库
  python history_archive.py sync -v          # 归档并输出详细日志
  python history_archive.py export           # 导出 CSV / JSONL / HTML
  python history_archive.py stats            # 看归档统计
  python history_archive.py verify           # 校验归档完整性
  python history_archive.py backup --keep 30 # 备份归档数据库

归档位置默认是脚本同目录下的 archive/，可以用 --archive 指定，
也可以用环境变量 WEB_HISTORY_ARCHIVE 指定。
"""

from __future__ import annotations

import argparse
import configparser
import csv
import gzip
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, unquote, urlsplit

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_ARCHIVE = Path(os.environ.get("WEB_HISTORY_ARCHIVE") or (SCRIPT_DIR / "archive"))

# Chromium 时间戳: 1601-01-01 起算的微秒数
WEBKIT_EPOCH_OFFSET_US = 11_644_473_600 * 1_000_000

STAGE_RETRIES = 3

# 合理的访问时间范围 (UTC)，超出范围的当作脏数据丢弃
MIN_VALID_TS = datetime(1995, 1, 1, tzinfo=timezone.utc)

# Chromium 的 transition 类型（低 8 位为核心类型）
CHROMIUM_TRANSITIONS = {
    0: "LINK",
    1: "TYPED",
    2: "AUTO_BOOKMARK",
    3: "AUTO_SUBFRAME",
    4: "MANUAL_SUBFRAME",
    5: "GENERATED",
    6: "START_PAGE",
    7: "FORM_SUBMIT",
    8: "RELOAD",
    9: "KEYWORD",
    10: "KEYWORD_GENERATED",
}

# Firefox 的 visit_type
FIREFOX_TRANSITIONS = {
    1: "LINK",
    2: "TYPED",
    3: "BOOKMARK",
    4: "EMBED",
    5: "REDIRECT_PERMANENT",
    6: "REDIRECT_TEMPORARY",
    7: "DOWNLOAD",
    8: "FRAMED_LINK",
    9: "RELOAD",
}

# (显示名, 用户数据目录模板) —— Chromium 系
CHROMIUM_ROOTS = [
    ("Chrome", r"%LOCALAPPDATA%\Google\Chrome\User Data"),
    ("Chrome Beta", r"%LOCALAPPDATA%\Google\Chrome Beta\User Data"),
    ("Chrome Dev", r"%LOCALAPPDATA%\Google\Chrome Dev\User Data"),
    ("Chrome Canary", r"%LOCALAPPDATA%\Google\Chrome SxS\User Data"),
    ("Edge", r"%LOCALAPPDATA%\Microsoft\Edge\User Data"),
    ("Edge Beta", r"%LOCALAPPDATA%\Microsoft\Edge Beta\User Data"),
    ("Edge Dev", r"%LOCALAPPDATA%\Microsoft\Edge Dev\User Data"),
    ("Edge Canary", r"%LOCALAPPDATA%\Microsoft\Edge SxS\User Data"),
    ("Brave", r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\User Data"),
    ("Brave Beta", r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser-Beta\User Data"),
    ("Brave Nightly", r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser-Nightly\User Data"),
    ("Vivaldi", r"%LOCALAPPDATA%\Vivaldi\User Data"),
    ("Chromium", r"%LOCALAPPDATA%\Chromium\User Data"),
    ("Yandex", r"%LOCALAPPDATA%\Yandex\YandexBrowser\User Data"),
    ("Opera", r"%APPDATA%\Opera Software\Opera Stable"),
    ("Opera GX", r"%APPDATA%\Opera Software\Opera GX Stable"),
    ("360 Chrome", r"%LOCALAPPDATA%\360Chrome\Chrome\User Data"),
    ("360 Chrome X", r"%LOCALAPPDATA%\360ChromeX\Chrome\User Data"),
    ("QQ Browser", r"%LOCALAPPDATA%\Tencent\QQBrowser\User Data"),
    ("Sogou Explorer", r"%LOCALAPPDATA%\SogouExplorer\Webkit"),
    ("CocCoc", r"%LOCALAPPDATA%\CocCoc\Browser\User Data"),
]

FIREFOX_ROOTS = [
    r"%APPDATA%\Mozilla\Firefox",
    r"%LOCALAPPDATA%\Mozilla\Firefox",
]

# 归档库表结构
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS sources (
    id             INTEGER PRIMARY KEY,
    source_key     TEXT NOT NULL UNIQUE,   -- 浏览器|配置|路径 的稳定标识
    browser        TEXT NOT NULL,
    profile        TEXT NOT NULL,
    history_path   TEXT NOT NULL,
    first_seen_utc TEXT NOT NULL,
    last_sync_utc  TEXT,
    last_status    TEXT,
    last_message   TEXT
);

CREATE TABLE IF NOT EXISTS urls (
    id               INTEGER PRIMARY KEY,
    url              TEXT NOT NULL UNIQUE,
    url_hash         TEXT NOT NULL,
    scheme           TEXT,
    host             TEXT,
    first_visit_utc  TEXT,
    last_visit_utc   TEXT,
    latest_title     TEXT,
    best_typed_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_urls_host ON urls(host);
CREATE INDEX IF NOT EXISTS idx_urls_last ON urls(last_visit_utc);

-- 同一个 URL 历史上出现过的所有标题，永久保留
CREATE TABLE IF NOT EXISTS titles (
    id            INTEGER PRIMARY KEY,
    url_id        INTEGER NOT NULL REFERENCES urls(id) ON DELETE CASCADE,
    title         TEXT NOT NULL,
    first_seen_utc TEXT NOT NULL,
    last_seen_utc  TEXT NOT NULL,
    UNIQUE(url_id, title)
);

CREATE TABLE IF NOT EXISTS visits (
    id               INTEGER PRIMARY KEY,
    source_id        INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    url_id           INTEGER NOT NULL REFERENCES urls(id) ON DELETE CASCADE,
    visit_time_raw   INTEGER NOT NULL,          -- 浏览器原始时间戳(微秒)
    visit_time_utc   TEXT NOT NULL,
    visit_time_local TEXT NOT NULL,
    day              TEXT NOT NULL,             -- YYYY-MM-DD (本地时区)
    title            TEXT,                      -- 首次归档时抓到的标题
    transition       INTEGER,
    transition_name  TEXT,
    visit_duration_ms INTEGER NOT NULL DEFAULT 0,
    typed            INTEGER NOT NULL DEFAULT 0,
    -- 浏览器里偶尔存在 URL、时间戳、类型完全一致的重复行。dup_count 记录归档时
    -- 观察到的重数（取最大值合并），既不丢数据，反复归档也不会重复累加。
    dup_count        INTEGER NOT NULL DEFAULT 1,
    UNIQUE(source_id, visit_time_raw, url_id, transition, visit_duration_ms, typed)
);
CREATE INDEX IF NOT EXISTS idx_visits_time  ON visits(visit_time_raw);
CREATE INDEX IF NOT EXISTS idx_visits_day   ON visits(day);
CREATE INDEX IF NOT EXISTS idx_visits_url   ON visits(url_id);
CREATE INDEX IF NOT EXISTS idx_visits_src   ON visits(source_id);

CREATE TABLE IF NOT EXISTS sync_runs (
    id            INTEGER PRIMARY KEY,
    started_utc   TEXT NOT NULL,
    finished_utc  TEXT,
    status        TEXT,
    sources_ok    INTEGER DEFAULT 0,
    sources_fail  INTEGER DEFAULT 0,
    new_visits    INTEGER DEFAULT 0,
    new_urls      INTEGER DEFAULT 0,
    new_titles    INTEGER DEFAULT 0,
    message       TEXT
);

-- TODO 列表。存在归档库里而不是浏览器里：跟着 backup 一起备份，
-- 换浏览器/清缓存也不会丢，静态导出的页面只是只读快照。
CREATE TABLE IF NOT EXISTS todos (
    id       TEXT PRIMARY KEY,
    kind     TEXT NOT NULL DEFAULT 'text',   -- 'url' 记一条网页，'text' 是自由文字
    text     TEXT NOT NULL DEFAULT '',
    url      TEXT NOT NULL DEFAULT '',
    title    TEXT NOT NULL DEFAULT '',
    tag      TEXT NOT NULL DEFAULT '',
    done     INTEGER NOT NULL DEFAULT 0,
    created  TEXT NOT NULL,
    done_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_todos_done ON todos(done);
"""

INSERT_RAW_SQL = """
INSERT INTO _raw
    (source_id, url_id, visit_time_raw, visit_time_utc, visit_time_local,
     day, title, transition, transition_name, visit_duration_ms, typed)
VALUES (?,?,?,?,?,?,?,?,?,?,?)
"""

# 先在临时表里对本次扫描做批内聚合（同一个 URL、同一微秒、同类型算同一条），
# 再用 MAX 合并进归档：既保留浏览器里的重复计数，又保证反复归档不会重复累加。
MERGE_RAW_SQL = """
INSERT INTO visits
    (source_id, url_id, visit_time_raw, visit_time_utc, visit_time_local, day, title,
     transition, transition_name, visit_duration_ms, typed, dup_count)
SELECT source_id, url_id, visit_time_raw, visit_time_utc, visit_time_local, day, title,
       transition, transition_name, visit_duration_ms, typed, COUNT(*)
FROM _raw WHERE 1
GROUP BY url_id, visit_time_raw, transition, visit_duration_ms, typed
ON CONFLICT(source_id, visit_time_raw, url_id, transition, visit_duration_ms, typed)
DO UPDATE SET dup_count = MAX(dup_count, excluded.dup_count)
"""

UPSERT_URL_SQL = """
INSERT INTO urls
    (url, url_hash, scheme, host, first_visit_utc, last_visit_utc,
     latest_title, best_typed_count)
VALUES (?,?,?,?,?,?,?,?)
ON CONFLICT(url) DO UPDATE SET
    latest_title = CASE
        WHEN excluded.latest_title IS NOT NULL AND excluded.latest_title <> ''
        THEN excluded.latest_title ELSE urls.latest_title END,
    best_typed_count = MAX(urls.best_typed_count, excluded.best_typed_count)
"""

UPSERT_TITLE_SQL = """
INSERT INTO titles (url_id, title, first_seen_utc, last_seen_utc)
VALUES (?,?,?,?)
ON CONFLICT(url_id, title) DO UPDATE SET last_seen_utc = excluded.last_seen_utc
"""

# 每次 flush 用的临时表：本次扫描到的原始行先落在这里
TEMP_TABLES_DDL = """
CREATE TEMP TABLE IF NOT EXISTS _raw (
    source_id         INTEGER,
    url_id            INTEGER,
    visit_time_raw    INTEGER,
    visit_time_utc    TEXT,
    visit_time_local  TEXT,
    day               TEXT,
    title             TEXT,
    transition        INTEGER,
    transition_name   TEXT,
    visit_duration_ms INTEGER,
    typed             INTEGER
);
CREATE TEMP TABLE IF NOT EXISTS _touched (url_id INTEGER PRIMARY KEY);
"""


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------

class Logger:
    """同时往控制台和日志文件写。"""

    def __init__(self, path: Path | None = None, verbose: bool = False):
        self.verbose = verbose
        self.fh = None
        if path is not None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                self.fh = path.open("a", encoding="utf-8")
                self.fh.write(f"\n===== {now_iso()} =====\n")
            except OSError:
                self.fh = None

    def __call__(self, msg: str, *, level: str = "info", always: bool = False):
        if level == "debug" and not self.verbose:
            return
        prefix = {"info": "", "ok": "[OK] ", "warn": "[!] ", "err": "[x] ", "debug": "    "}
        line = prefix.get(level, "") + msg
        if level != "debug" or always:
            print(line, flush=True)
        if self.fh:
            try:
                self.fh.write(line + "\n")
                self.fh.flush()
            except OSError:
                pass

    def close(self):
        if self.fh:
            try:
                self.fh.close()
            except OSError:
                pass
            self.fh = None


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def fmt_dt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def fmt_local(dt: datetime) -> str:
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def webkit_us_to_dt(value) -> datetime | None:
    """Chromium: 1601 起算的微秒 -> UTC datetime"""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    try:
        dt = datetime.fromtimestamp((v - WEBKIT_EPOCH_OFFSET_US) / 1_000_000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return dt if dt >= MIN_VALID_TS else None


def unix_us_to_dt(value) -> datetime | None:
    """Firefox: 1970 起算的微秒 -> UTC datetime"""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    try:
        dt = datetime.fromtimestamp(v / 1_000_000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return dt if dt >= MIN_VALID_TS else None


def split_url(url: str) -> tuple[str, str]:
    try:
        parts = urlsplit(url)
        scheme = (parts.scheme or "").lower()
        host = (parts.hostname or "").lower()
        return scheme, host
    except ValueError:
        return "", ""


def file_url_to_path(url: str) -> str:
    """file:// 链接还原成本地路径（统一小写、反斜杠、去掉百分号转义）"""
    if not url or not url.lower().startswith("file:"):
        return ""
    try:
        raw = urlsplit(url).path
    except ValueError:
        return ""
    path = unquote(raw)
    # Windows 上 file:///E:/x 的 path 是 /E:/x，去掉开头的斜杠
    if len(path) > 2 and path[0] == "/" and path[2] == ":":
        path = path[1:]
    return path.replace("/", "\\").lower().rstrip("\\")


def is_self_url(url: str, archive_dir: Path) -> bool:
    """判断这条记录是不是归档工具自己产生的。

    最典型的就是用 view 打开的那张 history.html——它一被打开就进了浏览器历史，
    下次 sync 又把它归档进来，越滚越多，而且没有任何信息量。
    """
    path = file_url_to_path(url)
    if not path:
        return False
    base = str(archive_dir).replace("/", "\\").lower().rstrip("\\")
    return path == base or path.startswith(base + "\\")


EXCLUDE_FILE_NAME = "exclude.txt"


def exclude_file(archive_dir: Path) -> Path:
    return archive_dir / EXCLUDE_FILE_NAME


def load_exclude_patterns(archive_dir: Path) -> list[str]:
    """读 <归档目录>/exclude.txt 里的额外排除关键字。

    存在的意义：计划任务跑的是不带参数的 sync，命令行上的 --exclude 用不上，
    所以需要一份会被自动读取的持久配置。一行一个关键字，空行和 # 开头的行忽略。
    """
    path = exclude_file(archive_dir)
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def save_exclude_patterns(archive_dir: Path, patterns: list[str]) -> Path:
    archive_dir.mkdir(parents=True, exist_ok=True)
    path = exclude_file(archive_dir)
    body = [
        "# 归档时额外排除的链接关键字，一行一个，包含即排除（不区分大小写）。",
        "# 用 `python history_archive.py exclude 关键字` 增删，别手改也行。",
        "# 归档目录下的链接（本工具自己的导出页面）已经默认排除，不用写在这里。",
        "",
    ]
    body.extend(patterns)
    path.write_text("\n".join(body) + "\n", encoding="utf-8")
    return path


VIEWER_PATH = "/history/"
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,40}$")


def is_viewer_url(url: str) -> bool:
    """判断链接是不是本工具**服务模式**自己的页面。

    服务模式的地址形如 http://127.0.0.1:50070/history/?t=<令牌>，
    它被打开后同样会进浏览器历史——这点跟 file:// 的静态快照一样，
    所以也要排除。用固定路径 /history/ 来认，端口和令牌每次都变，认不得。
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if (parts.scheme or "").lower() not in ("http", "https"):
        return False
    if (parts.hostname or "").lower() not in ("127.0.0.1", "localhost", "::1"):
        return False
    path = parts.path or "/"
    if path.startswith(VIEWER_PATH):
        return True
    # 早期版本把页面放在根路径，只带一个 ?t=<令牌>。令牌形状够特别，
    # 用来兜底识别，免得升级前的记录一直留在库里。
    if path != "/":
        return False
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key == "t" and _TOKEN_RE.match(value or ""):
            return True
    return False


def make_url_filter(archive_dir: Path, extra_patterns: list[str] | None = None,
                    exclude_self: bool = True):
    """返回 keep_url(url) —— True 表示这条记录要归档。

    排除规则 = 归档目录下的链接 + 本工具服务页面（默认）
             + exclude.txt 里的关键字 + 命令行传进来的关键字。
    """
    patterns = [p.lower() for p in (extra_patterns or []) if p]
    patterns.extend(p.lower() for p in load_exclude_patterns(archive_dir))

    def keep_url(url: str) -> bool:
        if exclude_self and is_self_url(url, archive_dir):
            return False
        if exclude_self and is_viewer_url(url):
            return False
        if patterns:
            low = url.lower()
            for pat in patterns:
                if pat in low:
                    return False
        return True

    return keep_url


def source_key(browser: str, profile: str, path: Path) -> str:
    raw = f"{browser}|{profile}|{str(path).lower()}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16] + ":" + browser


def expand(path_template: str) -> Path:
    return Path(os.path.expandvars(path_template))


def human(n: int) -> str:
    return f"{n:,}"


def fsize(path: Path) -> str:
    try:
        n = path.stat().st_size
    except OSError:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024.0
    return f"{n:.1f} GB"


# --------------------------------------------------------------------------
# 探测浏览器历史库
# --------------------------------------------------------------------------

def discover_chromium(extra_roots: list[Path] | None = None) -> list[dict]:
    roots = [(name, expand(tpl)) for name, tpl in CHROMIUM_ROOTS]
    for p in extra_roots or []:
        roots.append((p.name, p))

    found: list[dict] = []
    seen: set[str] = set()

    for browser, root in roots:
        try:
            if not root.is_dir():
                continue
        except OSError:
            continue

        # 情况一：root 本身就是配置目录（Opera、搜狗这样）
        candidates: list[tuple[str, Path]] = []
        if (root / "History").is_file():
            candidates.append(("Default", root / "History"))
        # 情况二：root 下面每个含 History 的子目录都是一个配置
        try:
            for child in sorted(root.iterdir()):
                if not child.is_dir():
                    continue
                h = child / "History"
                if h.is_file():
                    candidates.append((child.name, h))
        except (OSError, PermissionError):
            pass

        for profile, hist in candidates:
            key = str(hist).lower()
            if key in seen:
                continue
            seen.add(key)
            found.append(
                {
                    "kind": "chromium",
                    "browser": browser,
                    "profile": profile,
                    "path": hist,
                }
            )
    return found


def discover_firefox() -> list[dict]:
    found: list[dict] = []
    seen: set[str] = set()

    for tpl in FIREFOX_ROOTS:
        root = expand(tpl)
        if not root.is_dir():
            continue

        entries: list[tuple[str, Path]] = []
        ini = root / "profiles.ini"
        if ini.is_file():
            cp = configparser.ConfigParser(strict=False)
            try:
                cp.read(ini, encoding="utf-8")
            except (OSError, configparser.Error):
                cp = configparser.ConfigParser(strict=False)
            for section in cp.sections():
                if not section.lower().startswith("profile"):
                    continue
                if "path" not in cp[section]:
                    continue
                name = cp[section].get("name", section)
                raw = cp[section]["path"]
                if cp[section].get("isrelative", "1") == "1":
                    p = (root / raw).resolve()
                else:
                    p = Path(raw)
                entries.append((name, p / "places.sqlite"))

        # 兜底：直接扫 Profiles 目录
        prof_dir = root / "Profiles"
        if prof_dir.is_dir():
            try:
                for child in sorted(prof_dir.iterdir()):
                    if child.is_dir() and (child / "places.sqlite").is_file():
                        entries.append((child.name, child / "places.sqlite"))
            except OSError:
                pass

        for name, places in entries:
            if not places.is_file():
                continue
            key = str(places).lower()
            if key in seen:
                continue
            seen.add(key)
            found.append(
                {
                    "kind": "firefox",
                    "browser": "Firefox",
                    "profile": name,
                    "path": places,
                }
            )
    return found


def discover_all(extra_roots: list[Path] | None = None) -> list[dict]:
    return discover_chromium(extra_roots) + discover_firefox()


# --------------------------------------------------------------------------
# 把被浏览器锁住的数据库复制出来
# --------------------------------------------------------------------------

def stage_sqlite(src: Path, stage_dir: Path, log: Logger) -> Path:
    """浏览器运行时会锁住 History / places.sqlite，所以先整份复制（含 -wal/-shm）。"""
    stage_dir.mkdir(parents=True, exist_ok=True)
    dst = stage_dir / src.name
    last_err: Exception | None = None

    for attempt in range(1, STAGE_RETRIES + 1):
        try:
            for suffix in ("", "-wal", "-shm", "-journal"):
                s = Path(str(src) + suffix)
                d = Path(str(dst) + suffix)
                if d.exists():
                    d.unlink()
                if s.exists():
                    shutil.copy2(s, d)
            return dst
        except (PermissionError, OSError) as exc:
            last_err = exc
            log(f"复制 {src.name} 第 {attempt} 次失败: {exc}", level="debug")
            time.sleep(0.5 * attempt)

    raise RuntimeError(f"无法复制 {src}（可能被独占锁定）: {last_err}")


def cleanup_stage(stage_dir: Path):
    try:
        if stage_dir.exists():
            shutil.rmtree(stage_dir, ignore_errors=True)
    except OSError:
        pass


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.DatabaseError:
        return set()


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


# --------------------------------------------------------------------------
# 归档库
# --------------------------------------------------------------------------

def has_expected_visit_key(conn: sqlite3.Connection) -> bool:
    want = ["source_id", "visit_time_raw", "url_id", "transition", "visit_duration_ms", "typed"]
    try:
        for _seq, name, unique, _origin, _partial in conn.execute("PRAGMA index_list(visits)"):
            if not unique:
                continue
            cols = [r[2] for r in conn.execute(f"PRAGMA index_info({name})")]
            if cols == want:
                return True
    except sqlite3.DatabaseError:
        return False
    return False


def open_archive(archive_dir: Path, *, create: bool = True,
                 threaded: bool = False) -> sqlite3.Connection:
    archive_dir.mkdir(parents=True, exist_ok=True)
    db_path = archive_dir / "archive.sqlite"
    if not create and not db_path.exists():
        raise SystemExit(f"归档库还不存在: {db_path}\n请先运行: python history_archive.py sync")
    # 服务模式下 ThreadingHTTPServer 每个请求一个线程，连接必须允许跨线程；
    # 所有访问都在同一把锁里串行化，所以是安全的。
    conn = sqlite3.connect(db_path, isolation_level=None,
                           check_same_thread=not threaded)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(SCHEMA)
    if not has_expected_visit_key(conn):
        conn.close()
        raise SystemExit(
            f"归档库 {db_path} 的表结构来自旧版本，无法直接升级。\n"
            f"请先备份并删除该文件（或整个 {archive_dir} 目录）后重新运行 sync。"
        )
    conn.execute(
        "INSERT INTO meta(key,value) VALUES('schema_version','2') "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
    )
    return conn


def get_source_id(conn: sqlite3.Connection, src: dict) -> int:
    key = source_key(src["browser"], src["profile"], src["path"])
    row = conn.execute("SELECT id FROM sources WHERE source_key=?", (key,)).fetchone()
    if row:
        conn.execute(
            "UPDATE sources SET browser=?, profile=?, history_path=? WHERE id=?",
            (src["browser"], src["profile"], str(src["path"]), row[0]),
        )
        return int(row[0])
    cur = conn.execute(
        "INSERT INTO sources(source_key,browser,profile,history_path,first_seen_utc)"
        " VALUES(?,?,?,?,?)",
        (key, src["browser"], src["profile"], str(src["path"]), now_iso()),
    )
    return int(cur.lastrowid)


def load_url_id_map(conn: sqlite3.Connection) -> dict[str, int]:
    return {url: uid for uid, url in conn.execute("SELECT id, url FROM urls")}


def register_urls(
    conn: sqlite3.Connection,
    url_id_map: dict[str, int],
    rows: list[tuple],
    log: Logger,
    stats: dict,
) -> None:
    """把新 URL 写进 urls 表，并把 id 补进 url_id_map。"""
    fresh = [r for r in rows if r[0] not in url_id_map]
    if not fresh:
        return
    conn.executemany(UPSERT_URL_SQL, fresh)
    stats["new_urls"] += len(fresh)
    # 只回查这一批新 URL 的 id
    chunk = 400
    for i in range(0, len(fresh), chunk):
        part = [r[0] for r in fresh[i : i + chunk]]
        ph = ",".join("?" * len(part))
        for uid, url in conn.execute(f"SELECT id, url FROM urls WHERE url IN ({ph})", part):
            url_id_map[url] = uid


def record_titles(
    conn: sqlite3.Connection,
    url_id_map: dict[str, int],
    pairs: list[tuple[str, str]],
    stats: dict,
) -> None:
    if not pairs:
        return
    ts = now_iso()
    rows = [(url_id_map[u], t, ts, ts) for u, t in pairs if u in url_id_map and t]
    if not rows:
        return
    before = conn.total_changes
    conn.executemany(UPSERT_TITLE_SQL, rows)
    stats["new_titles"] += conn.total_changes - before


def refresh_url_times(conn: sqlite3.Connection) -> None:
    """根据 visits 回填 urls 的首次/最近访问时间（只处理本次碰到过的 URL）。"""
    conn.execute(
        """
        UPDATE urls SET
            first_visit_utc = (
                SELECT MIN(v.visit_time_utc) FROM visits v WHERE v.url_id = urls.id),
            last_visit_utc = (
                SELECT MAX(v.visit_time_utc) FROM visits v WHERE v.url_id = urls.id)
        WHERE id IN (SELECT url_id FROM _touched)
        """
    )


# --------------------------------------------------------------------------
# 读取各个浏览器的历史
# --------------------------------------------------------------------------

def ingest_chromium(
    conn: sqlite3.Connection,
    db_path: Path,
    source_id: int,
    url_id_map: dict[str, int],
    log: Logger,
    stats: dict,
    keep_url=None,
) -> None:
    src = sqlite3.connect(str(db_path))
    try:
        if not table_exists(src, "urls") or not table_exists(src, "visits"):
            raise RuntimeError("不是有效的 Chromium 历史库（缺少 urls/visits 表）")

        ucols = table_columns(src, "urls")
        vcols = table_columns(src, "visits")
        if "url" not in ucols or "id" not in ucols:
            raise RuntimeError("urls 表结构异常")

        title_expr = "COALESCE(u.title,'')" if "title" in ucols else "''"
        typed_expr = "COALESCE(u.typed_count,0)" if "typed_count" in ucols else "0"

        # ---- 1) URL 元数据 ----
        url_rows = []
        url_titles: list[tuple[str, str]] = []
        excluded_ids: set[int] = set()
        select_urls = (
            f"SELECT u.id, u.url, {title_expr}, {typed_expr} FROM urls u WHERE u.url IS NOT NULL"
        )
        for _raw_id, url, title, typed_count in src.execute(select_urls):
            if not url:
                continue
            if keep_url is not None and not keep_url(url):
                excluded_ids.add(_raw_id)
                continue
            scheme, host = split_url(url)
            url_rows.append(
                (
                    url,
                    hashlib.sha1(url.encode("utf-8", "replace")).hexdigest(),
                    scheme,
                    host,
                    None,
                    None,
                    title or None,
                    int(typed_count or 0),
                )
            )
            if title:
                url_titles.append((url, title))
        register_urls(conn, url_id_map, url_rows, log, stats)
        record_titles(conn, url_id_map, url_titles, stats)
        if excluded_ids:
            log(f"  排除 {human(len(excluded_ids))} 个链接（工具自身产生的页面）", level="debug")
        log(f"  读取 {human(len(url_rows))} 个 URL", level="debug")

        # ---- 2) 访问记录 ----
        url_ref = "v.url" if "url" in vcols else None
        if url_ref is None:
            raise RuntimeError("visits 表缺少 url 列")
        trans_expr = "v.transition" if "transition" in vcols else "0"
        dur_expr = "v.visit_duration" if "visit_duration" in vcols else "0"
        time_expr = "v.visit_time" if "visit_time" in vcols else None
        if time_expr is None:
            raise RuntimeError("visits 表缺少 visit_time 列")

        # urls.id -> 归档 url_id
        local_map: dict[int, int] = {}
        for raw_id, url in src.execute("SELECT id, url FROM urls WHERE url IS NOT NULL"):
            uid = url_id_map.get(url)
            if uid is not None:
                local_map[raw_id] = uid

        select_visits = (
            f"SELECT {url_ref}, {time_expr}, {trans_expr}, {dur_expr} FROM visits v"
        )

        batch: list[tuple] = []
        skipped = 0
        considered = 0

        def flush():
            nonlocal batch
            if not batch:
                return
            conn.execute("DELETE FROM _raw")
            conn.executemany(INSERT_RAW_SQL, batch)
            conn.execute(MERGE_RAW_SQL)
            conn.execute(
                "INSERT OR IGNORE INTO _touched(url_id) SELECT DISTINCT url_id FROM _raw"
            )
            batch = []

        for raw_url_id, raw_time, transition, duration in src.execute(select_visits):
            considered += 1
            if raw_url_id in excluded_ids:
                stats["skipped_self"] = stats.get("skipped_self", 0) + 1
                continue
            uid = local_map.get(raw_url_id)
            if uid is None:
                skipped += 1
                continue
            dt = webkit_us_to_dt(raw_time)
            if dt is None:
                skipped += 1
                continue
            tr = int(transition or 0)
            core = tr & 0xFF
            dur_ms = int(duration or 0) // 1000
            if dur_ms < 0:
                dur_ms = 0
            local = dt.astimezone()
            batch.append(
                (
                    source_id,
                    uid,
                    int(raw_time),
                    fmt_dt(dt),
                    fmt_local(dt),
                    local.strftime("%Y-%m-%d"),
                    None,  # 标题稍后按 URL 统一回填
                    tr,
                    CHROMIUM_TRANSITIONS.get(core, f"OTHER({core})"),
                    dur_ms,
                    1 if core == 1 else 0,
                )
            )
            if len(batch) >= 1000:
                flush()
        flush()

        if skipped:
            log(f"  跳过 {human(skipped)} 条无效记录", level="debug")
        log(f"  扫描 {human(considered)} 条访问记录", level="debug")
    finally:
        src.close()


def ingest_firefox(
    conn: sqlite3.Connection,
    db_path: Path,
    source_id: int,
    url_id_map: dict[str, int],
    log: Logger,
    stats: dict,
    keep_url=None,
) -> None:
    src = sqlite3.connect(str(db_path))
    try:
        if not table_exists(src, "moz_places") or not table_exists(src, "moz_historyvisits"):
            raise RuntimeError("不是有效的 Firefox 历史库（缺少 moz_places 表）")

        pcols = table_columns(src, "moz_places")
        vcols = table_columns(src, "moz_historyvisits")
        title_expr = "COALESCE(p.title,'')" if "title" in pcols else "''"
        typed_expr = "COALESCE(p.typed,0)" if "typed" in pcols else "0"

        url_rows = []
        url_titles: list[tuple[str, str]] = []
        excluded_ids: set[int] = set()
        select_places = (
            f"SELECT p.id, p.url, {title_expr}, {typed_expr} FROM moz_places p "
            "WHERE p.url IS NOT NULL"
        )
        for _raw_id, url, title, typed in src.execute(select_places):
            if not url:
                continue
            if keep_url is not None and not keep_url(url):
                excluded_ids.add(_raw_id)
                continue
            scheme, host = split_url(url)
            url_rows.append(
                (
                    url,
                    hashlib.sha1(url.encode("utf-8", "replace")).hexdigest(),
                    scheme,
                    host,
                    None,
                    None,
                    title or None,
                    int(typed or 0),
                )
            )
            if title:
                url_titles.append((url, title))
        register_urls(conn, url_id_map, url_rows, log, stats)
        record_titles(conn, url_id_map, url_titles, stats)
        log(f"  读取 {human(len(url_rows))} 个 URL", level="debug")

        local_map: dict[int, int] = {}
        for raw_id, url in src.execute("SELECT id, url FROM moz_places WHERE url IS NOT NULL"):
            uid = url_id_map.get(url)
            if uid is not None:
                local_map[raw_id] = uid

        place_ref = "v.place_id"
        type_expr = "v.visit_type" if "visit_type" in vcols else "0"

        batch: list[tuple] = []
        skipped = 0
        considered = 0

        def flush():
            nonlocal batch
            if not batch:
                return
            conn.execute("DELETE FROM _raw")
            conn.executemany(INSERT_RAW_SQL, batch)
            conn.execute(MERGE_RAW_SQL)
            conn.execute(
                "INSERT OR IGNORE INTO _touched(url_id) SELECT DISTINCT url_id FROM _raw"
            )
            batch = []

        for raw_place_id, raw_time, visit_type in src.execute(
            f"SELECT {place_ref}, v.visit_date, {type_expr} FROM moz_historyvisits v"
        ):
            considered += 1
            if raw_place_id in excluded_ids:
                stats["skipped_self"] = stats.get("skipped_self", 0) + 1
                continue
            uid = local_map.get(raw_place_id)
            if uid is None:
                skipped += 1
                continue
            dt = unix_us_to_dt(raw_time)
            if dt is None:
                skipped += 1
                continue
            vt = int(visit_type or 0)
            local = dt.astimezone()
            batch.append(
                (
                    source_id,
                    uid,
                    int(raw_time),
                    fmt_dt(dt),
                    fmt_local(dt),
                    local.strftime("%Y-%m-%d"),
                    None,
                    vt,
                    FIREFOX_TRANSITIONS.get(vt, f"OTHER({vt})"),
                    0,
                    1 if vt == 2 else 0,
                )
            )
            if len(batch) >= 1000:
                flush()
        flush()

        if skipped:
            log(f"  跳过 {human(skipped)} 条无效记录", level="debug")
        log(f"  扫描 {human(considered)} 条访问记录", level="debug")
    finally:
        src.close()


def backfill_visit_titles(conn: sqlite3.Connection) -> None:
    """把 URLs 表里已知的标题回填到还没标题的访问记录上。"""
    conn.execute(
        """
        UPDATE visits SET title = (
            SELECT u.latest_title FROM urls u WHERE u.id = visits.url_id
        )
        WHERE title IS NULL
          AND EXISTS (SELECT 1 FROM urls u WHERE u.id = visits.url_id AND u.latest_title IS NOT NULL)
        """
    )


# --------------------------------------------------------------------------
# 命令: sync
# --------------------------------------------------------------------------

def cmd_sync(args) -> int:
    archive_dir: Path = args.archive
    log = Logger(archive_dir / "logs" / f"sync-{datetime.now():%Y%m%d}.log", args.verbose)

    sources = discover_all([Path(p) for p in (args.extra_root or [])])
    if args.source and args.source != "all":
        wanted = {s.strip().lower() for s in args.source.split(",") if s.strip()}
        sources = [s for s in sources if s["browser"].lower() in wanted]

    log(f"归档目录: {archive_dir}")
    log(f"发现 {len(sources)} 个浏览器历史库")

    if not sources:
        log("没有找到任何浏览器历史库。用 detect 命令看看探测结果。", level="warn")
        log.close()
        return 1

    conn = open_archive(archive_dir)
    started = now_iso()
    run_id = conn.execute(
        "INSERT INTO sync_runs(started_utc,status) VALUES(?,'running')", (started,)
    ).lastrowid

    stats = {"new_visits": 0, "new_urls": 0, "new_titles": 0, "dup_merged": 0,
             "skipped_self": 0}
    ok = fail = 0
    stage_root = archive_dir / "_staging"
    keep_url = make_url_filter(archive_dir, getattr(args, "exclude", None),
                               not getattr(args, "no_self_exclude", False))

    def source_snapshot(source_id: int) -> tuple[int, int]:
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(dup_count),0) FROM visits WHERE source_id=?",
            (source_id,),
        ).fetchone()
        return int(row[0]), int(row[1])

    try:
        for src in sources:
            label = f"{src['browser']} / {src['profile']}"
            stage_dir = stage_root / hashlib.sha1(label.encode()).hexdigest()[:10]
            try:
                conn.execute("BEGIN IMMEDIATE")
                source_id = get_source_id(conn, src)
                url_id_map = load_url_id_map(conn)
                # 注意：executescript 会隐式 COMMIT，这里必须逐条执行
                for ddl in TEMP_TABLES_DDL.split(";"):
                    if ddl.strip():
                        conn.execute(ddl)
                conn.execute("DELETE FROM _touched")
                conn.execute("DELETE FROM _raw")
                rows_before, visits_before = source_snapshot(source_id)
                staged = stage_sqlite(src["path"], stage_dir, log)
                if src["kind"] == "chromium":
                    ingest_chromium(conn, staged, source_id, url_id_map, log, stats, keep_url)
                else:
                    ingest_firefox(conn, staged, source_id, url_id_map, log, stats, keep_url)

                rows_after, visits_after = source_snapshot(source_id)
                new_rows = rows_after - rows_before
                new_visits = visits_after - visits_before
                stats["new_visits"] += new_visits
                stats["dup_merged"] += max(0, new_visits - new_rows)

                refresh_url_times(conn)
                conn.execute(
                    "UPDATE sources SET last_sync_utc=?, last_status='ok', last_message=? WHERE id=?",
                    (now_iso(), f"new_visits={new_visits}", source_id),
                )
                conn.execute("COMMIT")
                ok += 1
                extra = ""
                if new_visits and new_rows < new_visits:
                    extra = f"  (其中 {human(new_visits - new_rows)} 条与已有记录合并计数)"
                log(
                    f"{label}: 新增 {human(new_visits)} 条访问  ({fsize(src['path'])}){extra}",
                    level="ok",
                )
            except Exception as exc:  # noqa: BLE001 — 单个浏览器失败不能影响其它
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                fail += 1
                log(f"{label}: 失败 -> {exc}", level="err")
                try:
                    conn.execute(
                        "UPDATE sources SET last_status='error', last_message=?, last_sync_utc=?"
                        " WHERE source_key=?",
                        (
                            str(exc)[:500],
                            now_iso(),
                            source_key(src["browser"], src["profile"], src["path"]),
                        ),
                    )
                except sqlite3.Error:
                    pass
            finally:
                cleanup_stage(stage_dir)

        backfill_visit_titles(conn)
        conn.execute(
            "UPDATE sync_runs SET finished_utc=?, status=?, sources_ok=?, sources_fail=?,"
            " new_visits=?, new_urls=?, new_titles=? WHERE id=?",
            (
                now_iso(),
                "ok" if fail == 0 else "partial",
                ok,
                fail,
                stats["new_visits"],
                stats["new_urls"],
                stats["new_titles"],
                run_id,
            ),
        )

        total_v = conn.execute("SELECT COALESCE(SUM(dup_count),0) FROM visits").fetchone()[0]
        total_rows = conn.execute("SELECT COUNT(*) FROM visits").fetchone()[0]
        total_u = conn.execute("SELECT COUNT(*) FROM urls").fetchone()[0]
        log("")
        log(
            f"本次新增: 访问 {human(stats['new_visits'])} 条 / 新 URL {human(stats['new_urls'])} 个",
            level="ok",
        )
        log(
            f"归档总计: 访问 {human(total_v)} 条"
            f"（去重后 {human(total_rows)} 行）/ URL {human(total_u)} 个"
        )
        if stats["skipped_self"]:
            log(f"已排除 {human(stats['skipped_self'])} 条自身产生的记录"
                f"（归档页面被自己打开产生的访问）")
        span = conn.execute(
            "SELECT MIN(visit_time_local), MAX(visit_time_local) FROM visits"
        ).fetchone()
        if span and span[0]:
            log(f"覆盖时间: {span[0]}  ~  {span[1]}")
        log(f"成功 {ok} 个 / 失败 {fail} 个")
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        log.close()
        return 0 if fail == 0 else 2
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass
        cleanup_stage(stage_root)


# --------------------------------------------------------------------------
# 命令: detect / stats / verify
# --------------------------------------------------------------------------

def cmd_detect(args) -> int:
    sources = discover_all([Path(p) for p in (args.extra_root or [])])
    if not sources:
        print("没有发现任何浏览器历史库。")
        print("如果浏览器装在非默认位置，可以用 --extra-root 指定用户数据目录。")
        return 1
    print(f"发现 {len(sources)} 个历史库:\n")
    print(f"{'浏览器':<18}{'配置':<22}{'大小':>10}  路径")
    print("-" * 100)
    for s in sorted(sources, key=lambda x: (x["browser"], x["profile"])):
        print(f"{s['browser']:<18}{s['profile']:<22}{fsize(s['path']):>10}  {s['path']}")
    return 0


def cmd_stats(args) -> int:
    conn = open_archive(args.archive, create=False)

    def scalar(sql, params=()):
        row = conn.execute(sql, params).fetchone()
        return row[0] if row else 0

    total_v = scalar("SELECT COALESCE(SUM(dup_count),0) FROM visits")
    total_rows = scalar("SELECT COUNT(*) FROM visits")
    total_u = scalar("SELECT COUNT(*) FROM urls")
    total_t = scalar("SELECT COUNT(*) FROM titles")
    hosts = scalar("SELECT COUNT(DISTINCT host) FROM urls WHERE host <> ''")
    span = conn.execute("SELECT MIN(visit_time_local), MAX(visit_time_local) FROM visits").fetchone()

    print("=" * 62)
    print("  浏览历史归档统计")
    print("=" * 62)
    print(f"  归档库      : {args.archive / 'archive.sqlite'}  ({fsize(args.archive / 'archive.sqlite')})")
    print(f"  访问记录    : {human(total_v)}   (数据行 {human(total_rows)})")
    print(f"  独立 URL    : {human(total_u)}")
    print(f"  历史标题    : {human(total_t)}")
    print(f"  站点数      : {human(hosts)}")
    if span and span[0]:
        print(f"  时间跨度    : {span[0]}  ~  {span[1]}")
    runs = conn.execute(
        "SELECT finished_utc, status, new_visits FROM sync_runs"
        " WHERE finished_utc IS NOT NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if runs:
        print(f"  上次归档    : {runs[0]}  ({runs[1]}, 新增 {human(runs[2] or 0)} 条)")

    print("\n-- 按浏览器 --")
    rows = conn.execute(
        """
        SELECT s.browser, s.profile, COALESCE(SUM(v.dup_count),0) c, MAX(v.visit_time_local) last
        FROM sources s LEFT JOIN visits v ON v.source_id = s.id
        GROUP BY s.id ORDER BY c DESC
        """
    ).fetchall()
    for browser, profile, c, last in rows:
        print(f"  {browser:<16}{profile:<20}{human(c):>12}   最近: {last or '-'}")

    print("\n-- 按年份 --")
    for day, c in conn.execute(
        "SELECT substr(day,1,4) y, COALESCE(SUM(dup_count),0) c FROM visits"
        " GROUP BY y ORDER BY y DESC LIMIT 15"
    ):
        print(f"  {day}  {human(c):>12}")

    print("\n-- 访问最多的站点 --")
    for host, c in conn.execute(
        "SELECT host, COALESCE(SUM(v.dup_count),0) c FROM visits v JOIN urls u ON u.id=v.url_id"
        " WHERE u.host <> '' GROUP BY u.host ORDER BY c DESC LIMIT 15"
    ):
        print(f"  {host:<45}{human(c):>10}")

    print("\n-- 访问最多的页面 --")
    for url, title, c in conn.execute(
        """
        SELECT u.url, COALESCE(u.latest_title,''), COALESCE(SUM(v.dup_count),0) c
        FROM visits v JOIN urls u ON u.id = v.url_id
        GROUP BY u.id ORDER BY c DESC LIMIT 10
        """
    ):
        t = (title or "")[:38]
        print(f"  {human(c):>8}  {t:<40} {url[:70]}")
    print()
    conn.close()
    return 0


def cmd_verify(args) -> int:
    conn = open_archive(args.archive, create=False)
    problems = 0

    print("检查归档库完整性 …")
    for row in conn.execute("PRAGMA integrity_check"):
        print(f"  integrity_check: {row[0]}")
        if row[0] != "ok":
            problems += 1

    fk = conn.execute("PRAGMA foreign_key_check").fetchall()
    if fk:
        problems += 1
        print(f"  外键错误: {len(fk)} 处")
    else:
        print("  foreign_key_check: ok")

    orphan_v = conn.execute(
        "SELECT COUNT(*) FROM visits v LEFT JOIN urls u ON u.id=v.url_id WHERE u.id IS NULL"
    ).fetchone()[0]
    orphan_s = conn.execute(
        "SELECT COUNT(*) FROM visits v LEFT JOIN sources s ON s.id=v.source_id WHERE s.id IS NULL"
    ).fetchone()[0]
    if orphan_v or orphan_s:
        problems += 1
    print(f"  孤立访问记录: url={orphan_v}, source={orphan_s}")

    dupes = conn.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT source_id, visit_time_raw, url_id, transition, visit_duration_ms,
                   typed, COUNT(*) c
            FROM visits
            GROUP BY source_id, visit_time_raw, url_id, transition, visit_duration_ms, typed
            HAVING c > 1
        )
        """
    ).fetchone()[0]
    print(f"  重复访问记录: {dupes}")
    problems += 1 if dupes else 0

    bad_dup = conn.execute(
        "SELECT COUNT(*) FROM visits WHERE dup_count IS NULL OR dup_count < 1"
    ).fetchone()[0]
    print(f"  重复计数异常: {bad_dup}")
    problems += 1 if bad_dup else 0

    bad_time = conn.execute(
        "SELECT COUNT(*) FROM visits WHERE visit_time_utc IS NULL OR visit_time_local IS NULL"
    ).fetchone()[0]
    print(f"  时间字段缺失: {bad_time}")
    problems += 1 if bad_time else 0

    counts = conn.execute(
        "SELECT (SELECT COALESCE(SUM(dup_count),0) FROM visits),"
        " (SELECT COUNT(*) FROM urls),"
        " (SELECT COUNT(*) FROM titles), (SELECT COUNT(*) FROM sources),"
        " (SELECT COUNT(*) FROM visits)"
    ).fetchone()
    print(
        f"\n  访问记录={human(counts[0])} (数据行 {human(counts[4])})"
        f"  urls={human(counts[1])}  titles={human(counts[2])}  sources={human(counts[3])}"
    )
    print("\n结果: " + ("发现 %d 个问题" % problems if problems else "一切正常"))
    conn.close()
    return 1 if problems else 0


# --------------------------------------------------------------------------
# 命令: export
# --------------------------------------------------------------------------

EXPORT_SQL = """
SELECT
    v.visit_time_local,
    v.visit_time_utc,
    s.browser,
    s.profile,
    v.title,
    u.url,
    u.host,
    v.transition_name,
    v.visit_duration_ms,
    v.typed,
    v.day,
    v.dup_count
FROM visits v
JOIN urls u    ON u.id = v.url_id
JOIN sources s ON s.id = v.source_id
WHERE 1=1
"""


def build_export_query(args) -> tuple[str, list]:
    sql = EXPORT_SQL
    params: list = []
    if args.since:
        sql += " AND v.day >= ?"
        params.append(args.since)
    if args.until:
        sql += " AND v.day <= ?"
        params.append(args.until)
    if args.browser:
        names = [b.strip().lower() for b in args.browser.split(",") if b.strip()]
        sql += " AND lower(s.browser) IN (%s)" % ",".join("?" * len(names))
        params.extend(names)
    if args.contains:
        sql += " AND (u.url LIKE ? OR COALESCE(v.title,'') LIKE ?)"
        like = f"%{args.contains}%"
        params.extend([like, like])
    sql += " ORDER BY v.visit_time_raw DESC"
    if args.limit:
        sql += " LIMIT ?"
        params.append(int(args.limit))
    return sql, params


# 页面模板放在同目录的 viewer.html 里，不内嵌进 Python 源码。原因：
#   * 内嵌在字符串里，HTML/CSS/JS 没有语法高亮、补全、格式化
#   * 改样式不用动 .py，也不会因为模板里一个引号写错就报 Python 语法错误
#   * 不用再担心 Python 的转义把模板里的反斜杠吃掉
#     （踩过：JS 正则被吃掉导致整页白屏）
# 不缓存：每次读一遍，改完 viewer.html 刷新页面就生效，服务不用重启。
VIEWER_TEMPLATE_NAME = "viewer.html"


def viewer_template_path() -> Path:
    return SCRIPT_DIR / VIEWER_TEMPLATE_NAME


def load_viewer_template() -> str:
    path = viewer_template_path()
    if not path.is_file():
        raise SystemExit(
            f"缺少页面模板: {path}\n"
            f"它是网页本体，必须和 history_archive.py 放在同一个目录里。"
        )
    return path.read_text(encoding="utf-8")


def cmd_export(args) -> int:
    conn = open_archive(args.archive, create=False)
    out_dir: Path = args.out or (args.archive / "exports")
    out_dir.mkdir(parents=True, exist_ok=True)

    sql, params = build_export_query(args)
    formats = [f.strip().lower() for f in (args.format or "csv,jsonl,html").split(",") if f.strip()]
    cur = conn.execute(sql, params)

    written: list[Path] = []
    stamps = "".join(f"{datetime.now():%Y%m%d-%H%M%S}")

    # ---- CSV ----
    if "csv" in formats:
        path = out_dir / "history.csv"
        n = 0
        with path.open("w", encoding="utf-8-sig", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(
                ["访问时间(本地)", "访问时间(UTC)", "浏览器", "配置", "标题",
                 "URL", "站点", "类型", "停留(ms)", "手动输入", "日期", "重复次数"]
            )
            for row in cur:
                w.writerow(row)
                n += 1
        written.append(path)
        print(f"[OK] CSV   {human(n)} 条 -> {path}")

    # ---- JSONL ----
    if "jsonl" in formats:
        cur = conn.execute(sql, params)
        path = out_dir / "history.jsonl"
        n = 0
        with path.open("w", encoding="utf-8") as fh:
            for row in cur:
                fh.write(
                    json.dumps(
                        {
                            "visited_local": row[0],
                            "visited_utc": row[1],
                            "browser": row[2],
                            "profile": row[3],
                            "title": row[4],
                            "url": row[5],
                            "host": row[6],
                            "transition": row[7],
                            "duration_ms": row[8],
                            "typed": bool(row[9]),
                            "day": row[10],
                            "dup_count": row[11],
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    + "\n"
                )
                n += 1
        written.append(path)
        print(f"[OK] JSONL {human(n)} 条 -> {path}")

    # ---- HTML ----
    if "html" in formats:
        html_limit = args.html_limit if args.html_limit is not None else 100000
        cur = conn.execute(sql, params)
        data = []
        for row in cur:
            if len(data) >= html_limit:
                break
            data.append(list(row))
        path = out_dir / "history.html"
        path.write_text(render_viewer_html(args.archive, mode="static", rows=data),
                        encoding="utf-8")
        written.append(path)
        extra = "" if len(data) < html_limit else f"（已截断到 {human(html_limit)} 条，完整数据看 CSV/JSONL）"
        print(f"[OK] HTML  {human(len(data))} 条 -> {path}{extra}")

    if args.snapshot:
        snap_dir = out_dir / "snapshots" / stamps
        snap_dir.mkdir(parents=True, exist_ok=True)
        for p in written:
            shutil.copy2(p, snap_dir / p.name)
        print(f"[OK] 快照已保存 -> {snap_dir}")

    conn.close()
    return 0


# --------------------------------------------------------------------------
# 命令: view
# --------------------------------------------------------------------------

def open_in_browser(path: Path) -> bool:
    """用系统默认程序打开文件（Windows 用 startfile，其它平台退回 webbrowser）。"""
    try:
        if hasattr(os, "startfile"):
            os.startfile(str(path))  # type: ignore[attr-defined]
            return True
    except OSError:
        pass
    try:
        import webbrowser

        return webbrowser.open(path.resolve().as_uri())
    except Exception:  # noqa: BLE001
        return False


def render_viewer_html(archive_dir: Path, mode: str = "static",
                       rows: list | None = None) -> str:
    """生成查看页面。

    mode=server：不内嵌数据，由页面自己去 /api/rows 拉，TODO 可读写。
    mode=static：把 rows 内嵌进去，得到一个自包含、可离线、只读的快照。
    """
    payload = json.dumps(rows if rows is not None else [],
                         ensure_ascii=False, separators=(",", ":"))
    return (load_viewer_template()
            .replace("__DATA__", payload)
            .replace("__MODE__", "server" if mode == "server" else "static"))


def cmd_view(args) -> int:
    """默认起本地服务（可读写 TODO）；--static 退回生成自包含的只读快照。"""
    if getattr(args, "static", False):
        out_dir: Path = args.out or (args.archive / "exports")
        ns = argparse.Namespace(
            archive=args.archive, out=out_dir, format="html",
            since=args.since, until=args.until, browser=args.browser,
            contains=args.contains, limit=args.limit,
            html_limit=args.html_limit, snapshot=False,
        )
        rc = cmd_export(ns)
        if rc != 0:
            return rc
        page = out_dir / "history.html"
        if not page.exists():
            print(f"[x] 没有生成 {page}")
            return 1
        if args.no_open:
            print(f"\n静态快照已生成（只读）: {page}")
            return 0
        print(f"\n正在用默认浏览器打开静态快照: {page}")
        print("（这是只读快照，TODO 不可用；需要 TODO 就用不带 --static 的 serve）")
        return 0 if open_in_browser(page) else 1

    return cmd_serve(argparse.Namespace(
        archive=args.archive,
        port=getattr(args, "port", 0),
        no_open=args.no_open,
        verbose=getattr(args, "verbose", False),
    ))


# --------------------------------------------------------------------------
# 命令: purge —— 清掉已经归档进去的自我引用记录
# --------------------------------------------------------------------------

def cmd_purge(args) -> int:
    archive_dir: Path = args.archive
    conn = open_archive(archive_dir, create=False)
    keep_url = make_url_filter(archive_dir, getattr(args, "exclude", None),
                               not getattr(args, "no_self_exclude", False))

    rows = conn.execute(
        """
        SELECT u.id, u.url, COUNT(v.id), COALESCE(SUM(v.dup_count),0),
               MIN(v.visit_time_local), MAX(v.visit_time_local)
        FROM urls u LEFT JOIN visits v ON v.url_id = u.id
        GROUP BY u.id
        """
    ).fetchall()
    targets = [r for r in rows if not keep_url(r[1])]

    if not targets:
        print("归档库里没有需要清理的自我引用记录。")
        conn.close()
        return 0

    print("以下记录会被清除（它们都是工具自己产生的，没有信息量）：\n")
    total_visits = 0
    for _uid, url, n, dup, first, last in targets:
        total_visits += dup
        print(f"  {human(dup):>8} 次访问 · {human(n):>6} 行")
        print(f"           {url}")
        print(f"           {first} ~ {last}")

    print(f"\n共 {len(targets)} 个 URL、{human(total_visits)} 条访问记录。")

    if not args.yes:
        print("\n这是预演，什么都没有删除。确认无误后加 --yes 真正执行。")
        print("（执行前会自动备份一份归档库）")
        conn.close()
        return 0

    # 先备份：这是对「只增不减」的归档做删除，必须留退路
    if not args.no_backup:
        src_path = archive_dir / "archive.sqlite"
        bk_dir = archive_dir / "backups"
        bk_dir.mkdir(parents=True, exist_ok=True)
        dst = bk_dir / f"archive-before-purge-{datetime.now():%Y%m%d-%H%M%S}.sqlite"
        raw = sqlite3.connect(str(src_path))
        out = sqlite3.connect(str(dst))
        try:
            raw.backup(out)
        finally:
            out.close()
            raw.close()
        print(f"\n[OK] 已备份 -> {dst}")

    try:
        conn.execute("BEGIN IMMEDIATE")
        for uid, _url, _n, _dup, _f, _l in targets:
            conn.execute("DELETE FROM visits WHERE url_id=?", (uid,))
            conn.execute("DELETE FROM titles WHERE url_id=?", (uid,))
            conn.execute("DELETE FROM urls WHERE id=?", (uid,))
        conn.execute("COMMIT")
    except sqlite3.Error as exc:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        print(f"[x] 删除失败，已回滚: {exc}")
        conn.close()
        return 1

    left_v = conn.execute("SELECT COALESCE(SUM(dup_count),0) FROM visits").fetchone()[0]
    left_u = conn.execute("SELECT COUNT(*) FROM urls").fetchone()[0]
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error:
        pass
    print(f"[OK] 清理完成。归档现在剩 {human(left_v)} 条访问 / {human(left_u)} 个 URL。")
    conn.close()
    return 0


# --------------------------------------------------------------------------
# TODO 存储（服务端）
# --------------------------------------------------------------------------

def todo_list(conn: sqlite3.Connection) -> list[dict]:
    """未完成在前，同组内按创建时间倒序。"""
    rows = conn.execute(
        "SELECT id, kind, text, url, title, tag, done, created, done_at FROM todos"
        " ORDER BY done ASC, created DESC"
    ).fetchall()
    return [
        {
            "id": r[0], "kind": r[1], "text": r[2], "url": r[3], "title": r[4],
            "tag": r[5], "done": bool(r[6]), "created": r[7], "doneAt": r[8],
        }
        for r in rows
    ]


def todo_add(conn: sqlite3.Connection, item: dict) -> tuple[bool, str]:
    kind = "url" if item.get("kind") == "url" else "text"
    text = str(item.get("text") or "").strip()
    url = str(item.get("url") or "").strip()
    if kind == "url":
        if not url:
            return False, "url 为空"
        # 同一条链接已有未完成的记录就不重复添加
        dup = conn.execute(
            "SELECT 1 FROM todos WHERE kind='url' AND url=? AND done=0", (url,)
        ).fetchone()
        if dup:
            return False, "dup"
    elif not text:
        return False, "empty"

    todo_id = str(item.get("id") or "").strip() or (
        "t" + format(int(time.time() * 1000), "x") + "-" + secrets.token_hex(3))
    conn.execute(
        "INSERT INTO todos(id, kind, text, url, title, tag, done, created, done_at)"
        " VALUES(?,?,?,?,?,?,0,?,NULL)",
        (todo_id, kind, text, url, str(item.get("title") or ""),
         str(item.get("tag") or "").strip(), now_iso()),
    )
    return True, ""


def todo_toggle(conn: sqlite3.Connection, todo_id: str) -> bool:
    row = conn.execute("SELECT done FROM todos WHERE id=?", (todo_id,)).fetchone()
    if row is None:
        return False
    if row[0]:
        conn.execute("UPDATE todos SET done=0, done_at=NULL WHERE id=?", (todo_id,))
    else:
        conn.execute("UPDATE todos SET done=1, done_at=? WHERE id=?", (now_iso(), todo_id))
    return True


def todo_update(conn: sqlite3.Connection, todo_id: str, patch: dict) -> bool:
    fields, params = [], []
    if "text" in patch:
        fields.append("text=?"); params.append(str(patch["text"]).strip())
    if "tag" in patch:
        fields.append("tag=?"); params.append(str(patch["tag"]).strip())
    if not fields:
        return False
    params.append(todo_id)
    cur = conn.execute(f"UPDATE todos SET {', '.join(fields)} WHERE id=?", params)
    return cur.rowcount > 0


def todo_remove(conn: sqlite3.Connection, todo_id: str) -> bool:
    return conn.execute("DELETE FROM todos WHERE id=?", (todo_id,)).rowcount > 0


def todo_clear_done(conn: sqlite3.Connection) -> int:
    return conn.execute("DELETE FROM todos WHERE done=1").rowcount


def todo_import(conn: sqlite3.Connection, items: list) -> dict:
    """合并导入：按 id 去重，已存在的 id 保留库里的版本（本地可能刚改过）。"""
    added = skipped = 0
    for it in items or []:
        if not isinstance(it, dict):
            skipped += 1
            continue
        kind = "url" if it.get("kind") == "url" else "text"
        text = str(it.get("text") or "").strip()
        url = str(it.get("url") or "").strip()
        if (kind == "url" and not url) or (kind == "text" and not text):
            skipped += 1
            continue
        todo_id = str(it.get("id") or "").strip()
        if todo_id and conn.execute("SELECT 1 FROM todos WHERE id=?", (todo_id,)).fetchone():
            continue
        if not todo_id:
            todo_id = "t" + format(int(time.time() * 1000), "x") + "-" + secrets.token_hex(3)
        conn.execute(
            "INSERT OR IGNORE INTO todos(id, kind, text, url, title, tag, done, created, done_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (todo_id, kind, text, url, str(it.get("title") or ""),
             str(it.get("tag") or "").strip(), 1 if it.get("done") else 0,
             str(it.get("created") or now_iso()), it.get("doneAt")),
        )
        added += 1
    return {"added": added, "skipped": skipped}


# --------------------------------------------------------------------------
# 命令: exclude —— 管理额外的排除关键字
# --------------------------------------------------------------------------

def cmd_exclude(args) -> int:
    archive_dir: Path = args.archive
    existing = load_exclude_patterns(archive_dir)
    words = list(getattr(args, "words", None) or [])

    if args.remove_all:
        save_exclude_patterns(archive_dir, [])
        print("[OK] 已清空 exclude.txt")
        words = []

    if not words:
        print(f"排除配置: {exclude_file(archive_dir)}")
        if not existing:
            print("  （没有额外关键字；归档目录下的链接本来就默认排除）")
        else:
            for p in existing:
                print(f"  {p}")
        print()
        print("加一条:   python history_archive.py exclude bili-history.html")
        print("删一条:   python history_archive.py exclude --remove bili-history.html")
        return 0

    if args.remove:
        removed = [w for w in words if w.lower() in [p.lower() for p in existing]]
        kept = [p for p in existing if p.lower() not in [w.lower() for w in words]]
        save_exclude_patterns(archive_dir, kept)
        for w in removed:
            print(f"[OK] 已移除: {w}")
        missing = [w for w in words if w.lower() not in [p.lower() for p in existing]]
        for w in missing:
            print(f"[!] 本来就没有: {w}")
        return 0

    merged = list(existing)
    for w in words:
        if w.lower() in [p.lower() for p in merged]:
            print(f"[!] 已经在里面了: {w}")
        else:
            merged.append(w)
            print(f"[OK] 已添加: {w}")
    path = save_exclude_patterns(archive_dir, merged)
    print(f"\n配置写在 {path}，下次 sync（包括计划任务）会自动生效。")
    print("已经归档进去的记录用 purge 清掉：")
    print("  python history_archive.py purge        # 先预演")
    print("  python history_archive.py purge --yes  # 真删（会先自动备份）")
    return 0


# --------------------------------------------------------------------------
# 命令: serve —— 本地 HTTP 服务
# --------------------------------------------------------------------------

def rows_payload(conn: sqlite3.Connection, limit: int | None = None) -> list:
    """导出给前端的记录数组，列顺序与静态导出的 DATA 完全一致。"""
    sql = EXPORT_SQL + " ORDER BY v.visit_time_raw DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [list(r) for r in conn.execute(sql)]


def build_handler(archive_dir: Path, token: str, state: dict):
    """生成请求处理器。state 里放共享的连接和锁（服务是常驻的，不每次重开库）。"""
    lock: threading.Lock = state["lock"]

    class Handler(BaseHTTPRequestHandler):
        server_version = "web-history-archive"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):    # 默认会往 stderr 刷一堆，压掉
            if state.get("verbose"):
                sys.stderr.write("  %s\n" % (fmt % args))

        def _send(self, code: int, body: bytes, ctype: str, extra=None):
            if len(body) > 1024 and "gzip" in (self.headers.get("Accept-Encoding") or ""):
                body = gzip.compress(body, 6)
                gz = True
            else:
                gz = False
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")     # 禁止被别的页面内嵌
            self.send_header("Referrer-Policy", "no-referrer")
            if gz:
                self.send_header("Content-Encoding", "gzip")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _err(self, code, msg):
            self._json({"ok": False, "error": msg}, code)

        def _check_token(self):
            """令牌每次启动随机生成、写在打开的 URL 里。
            别的网页猜不到它，所以拿不到令牌就等于这个服务不存在。"""
            q = parse_qs(urlsplit(self.path).query)
            got = self.headers.get("X-Token") or (q.get("t") or [""])[0]
            if not got:            # 也接受 Cookie，刷新页面时不用一直带着 ?t=
                for part in (self.headers.get("Cookie") or "").split(";"):
                    k, _, v = part.strip().partition("=")
                    if k == "wh_token":
                        got = v
                        break
            if not got or not hmac.compare_digest(str(got), token):
                self._err(403, "缺少或错误的访问令牌，请用 serve 命令打印的地址打开。")
                return False
            return True

        def _check_origin(self):
            """跨站页面即使猜到端口也发不出请求：浏览器会带 Origin，对不上就拒。"""
            origin = self.headers.get("Origin")
            if not origin:
                return True
            host = self.headers.get("Host") or ""
            return origin in (f"http://{host}", f"https://{host}")

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/favicon.ico":
                self._send(204, b"", "image/x-icon")
                return
            if not self._check_token():
                return
            if path in (VIEWER_PATH, VIEWER_PATH.rstrip("/"), "/index.html"):
                page = render_viewer_html(archive_dir, mode="server")
                self._send(200, page.encode("utf-8"), "text/html; charset=utf-8",
                           {"Set-Cookie": f"wh_token={token}; Path=/; SameSite=Strict"})
            elif path == "/":
                # 只把页面放在固定的 /history/ 下：这样归档时能靠路径认出
                # 「这是工具自己的页面」，端口和令牌每次都在变，认不得。
                self._send(302, b"", "text/plain; charset=utf-8",
                           {"Location": VIEWER_PATH + "?" + (urlsplit(self.path).query or "")})
            elif path == "/api/rows":
                with lock:
                    data = rows_payload(state["conn"])
                self._json({"ok": True, "rows": data})
            elif path == "/api/meta":
                with lock:
                    conn = state["conn"]
                    meta = {
                        "rows": conn.execute(
                            "SELECT COALESCE(SUM(dup_count),0) FROM visits").fetchone()[0],
                        "urls": conn.execute("SELECT COUNT(*) FROM urls").fetchone()[0],
                        "titles": conn.execute("SELECT COUNT(*) FROM titles").fetchone()[0],
                        "sources": conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0],
                        "generated": now_iso(),
                    }
                    span = conn.execute("SELECT MIN(day), MAX(day) FROM visits").fetchone()
                    meta["first_day"], meta["last_day"] = span[0], span[1]
                    run = conn.execute(
                        "SELECT finished_utc, new_visits FROM sync_runs"
                        " WHERE finished_utc IS NOT NULL ORDER BY id DESC LIMIT 1"
                    ).fetchone()
                    if run:
                        meta["last_sync"], meta["last_sync_new"] = run[0], run[1]
                self._json({"ok": True, "meta": meta})
            elif path == "/api/todos":
                with lock:
                    self._json({"ok": True, "todos": todo_list(state["conn"])})
            else:
                self._err(404, "没有这个地址")

        def do_POST(self):
            path = urlsplit(self.path).path
            if not self._check_token():
                return
            if not self._check_origin():
                self._err(403, "跨站请求被拒绝")
                return
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
            if ctype != "application/json":
                # 强制 JSON 也挡掉一批简单的跨站表单提交
                self._err(415, "只接受 application/json")
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length > 8 * 1024 * 1024:
                    self._err(413, "请求体过大")
                    return
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except (ValueError, UnicodeDecodeError) as exc:
                self._err(400, f"请求体不是合法 JSON: {exc}")
                return

            if path == "/api/todos":
                action = payload.get("action")
                with lock:
                    conn = state["conn"]
                    try:
                        conn.execute("BEGIN IMMEDIATE")
                        msg = ""
                        if action == "add":
                            changed, why = todo_add(conn, payload.get("item") or {})
                            msg = {"dup": "这条链接已经在未完成的 TODO 里了",
                                   "empty": "内容不能为空"}.get(why, why)
                        elif action == "toggle":
                            changed = todo_toggle(conn, payload.get("id") or "")
                        elif action == "update":
                            changed = todo_update(conn, payload.get("id") or "",
                                                  payload.get("patch") or {})
                        elif action == "remove":
                            changed = todo_remove(conn, payload.get("id") or "")
                        elif action == "clear_done":
                            changed = True
                            msg = f"已清除 {todo_clear_done(conn)} 条"
                        elif action == "import":
                            res = todo_import(conn, payload.get("items") or [])
                            changed = True
                            msg = (f"新增 {res['added']} 条"
                                   + (f"，跳过 {res['skipped']} 条" if res["skipped"] else ""))
                        else:
                            conn.execute("ROLLBACK")
                            self._err(400, f"不认识的操作: {action}")
                            return
                        conn.execute("COMMIT")
                    except sqlite3.Error as exc:
                        try:
                            conn.execute("ROLLBACK")
                        except sqlite3.Error:
                            pass
                        self._err(500, f"写库失败: {exc}")
                        return
                    self._json({"ok": True, "changed": bool(changed), "message": msg,
                                "todos": todo_list(conn)})
            elif path == "/api/sync":
                try:
                    new = run_sync_once(archive_dir, state)
                except Exception as exc:  # noqa: BLE001
                    self._err(500, f"同步失败: {exc}")
                    return
                with lock:
                    rows = rows_payload(state["conn"])
                self._json({"ok": True, "new_visits": new, "rows": rows})
            else:
                self._err(404, "没有这个地址")

        do_HEAD = do_GET

    return Handler


def run_sync_once(archive_dir: Path, state: dict) -> int:
    """在服务进程里跑一次 sync，返回本次新增条数。"""
    args = argparse.Namespace(
        archive=archive_dir, verbose=False, source="all", extra_root=None,
        exclude=None, no_self_exclude=False,
    )
    lock: threading.Lock = state["lock"]
    with lock:
        # sync 会自己开库写，先把服务这边的连接让开，跑完再重开
        state["conn"].close()
        try:
            cmd_sync(args)
        finally:
            state["conn"] = open_archive(archive_dir, threaded=True)
        row = state["conn"].execute(
            "SELECT new_visits FROM sync_runs WHERE finished_utc IS NOT NULL"
            " ORDER BY id DESC LIMIT 1").fetchone()
    return int(row[0]) if row else 0


def cmd_serve(args) -> int:
    archive_dir: Path = args.archive
    conn = open_archive(archive_dir, create=False, threaded=True)
    token = secrets.token_urlsafe(18)
    state = {"conn": conn, "lock": threading.Lock(), "verbose": args.verbose}

    handler = build_handler(archive_dir, token, state)
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", int(args.port)), handler)
    except OSError as exc:
        print(f"[x] 端口 {args.port} 起不来: {exc}")
        conn.close()
        return 1
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    url = f"http://127.0.0.1:{port}{VIEWER_PATH}?t={token}"

    # 注意 flush：输出被重定向到管道/文件时，print 默认是块缓冲，
    # 用户会看着一片空白等不到那行地址。
    print("=" * 62, flush=True)
    print("  浏览历史归档 · 本地服务已启动", flush=True)
    print("=" * 62, flush=True)
    print(f"  地址   : {url}", flush=True)
    print(f"  归档库 : {archive_dir / 'archive.sqlite'}", flush=True)
    print(f"  监听   : 127.0.0.1:{port}（只有本机能访问，局域网和外网都到不了）", flush=True)
    print(f"  令牌   : 已写在地址里，每次启动都不一样", flush=True)
    print(flush=True)
    print("  按 Ctrl+C 停止", flush=True)
    print(flush=True)

    if not args.no_open:
        open_in_browser(url)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止…")
    finally:
        httpd.shutdown()
        httpd.server_close()
        conn.close()
    return 0


# --------------------------------------------------------------------------
# 命令: backup
# --------------------------------------------------------------------------

def cmd_backup(args) -> int:
    src_path = args.archive / "archive.sqlite"
    if not src_path.exists():
        print(f"归档库不存在: {src_path}")
        return 1
    bk_dir = args.backup_dir or (args.archive / "backups")
    bk_dir.mkdir(parents=True, exist_ok=True)

    dst = bk_dir / f"archive-{datetime.now():%Y%m%d-%H%M%S}.sqlite"
    src = sqlite3.connect(str(src_path))
    dst_conn = sqlite3.connect(str(dst))
    try:
        src.backup(dst_conn)  # 一致性快照，不怕运行中被复制
    finally:
        dst_conn.close()
        src.close()
    print(f"[OK] 备份 -> {dst}  ({fsize(dst)})")

    keep = int(args.keep)
    if keep > 0:
        files = sorted(bk_dir.glob("archive-*.sqlite"))
        for old in files[:-keep]:
            try:
                old.unlink()
                print(f"     清理旧备份 {old.name}")
            except OSError as exc:
                print(f"     清理失败 {old.name}: {exc}")
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="history_archive.py",
        description="浏览器历史永久归档器（增量写入本地 SQLite，永不丢失）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python history_archive.py                    # 归档一次\n"
            "  python history_archive.py view               # 直接打开网页版历史记录\n"
            "  python history_archive.py sync -v            # 归档并打印细节\n"
            "  python history_archive.py detect             # 只探测有哪些历史库\n"
            "  python history_archive.py export --format html,csv\n"
            "  python history_archive.py stats\n"
            "  python history_archive.py backup --keep 30\n"
        ),
    )
    parser.add_argument(
        "--archive",
        type=Path,
        default=DEFAULT_ARCHIVE,
        help=f"归档目录（默认 {DEFAULT_ARCHIVE}）",
    )
    # 子命令里也要能写 --archive（例如 sync --archive D:\x）。
    # 用 SUPPRESS 做默认值，避免子解析器把主解析器已经解析到的值覆盖成 None。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--archive",
        type=Path,
        default=argparse.SUPPRESS,
        help="归档目录（默认 <脚本目录>/archive）",
    )
    sub = parser.add_subparsers(dest="command")

    p_sync = sub.add_parser("sync", parents=[common], help="把浏览器历史增量归档（默认命令）")
    p_sync.add_argument("-v", "--verbose", action="store_true", help="输出详细日志")
    p_sync.add_argument("--source", default="all", help="只归档指定浏览器，逗号分隔，如 chrome,edge")
    p_sync.add_argument("--extra-root", action="append", help="额外的用户数据目录（可重复）")
    p_sync.add_argument("--exclude", action="append",
                        help="额外排除包含该关键字的链接（可重复），如 --exclude bili-history.html")
    p_sync.add_argument("--no-self-exclude", action="store_true",
                        help="不排除归档目录下的链接（默认会排除，避免把自己的页面收进来）")

    p_detect = sub.add_parser("detect", parents=[common], help="列出探测到的浏览器历史库")
    p_detect.add_argument("--extra-root", action="append", help="额外的用户数据目录（可重复）")

    sub.add_parser("stats", parents=[common], help="显示归档统计")

    sub.add_parser("verify", parents=[common], help="校验归档库完整性")

    p_export = sub.add_parser("export", parents=[common], help="导出 CSV / JSONL / HTML")
    p_export.add_argument("--format", default="csv,jsonl,html", help="导出格式，逗号分隔")
    p_export.add_argument("--out", type=Path, default=None, help="导出目录（默认 <归档>/exports）")
    p_export.add_argument("--since", default=None, help="起始日期 YYYY-MM-DD")
    p_export.add_argument("--until", default=None, help="结束日期 YYYY-MM-DD")
    p_export.add_argument("--browser", default=None, help="只导出指定浏览器，逗号分隔")
    p_export.add_argument("--contains", default=None, help="只导出 URL/标题包含该文本的记录")
    p_export.add_argument("--limit", type=int, default=None, help="最多导出多少条")
    p_export.add_argument("--html-limit", type=int, default=None, help="HTML 内嵌条数上限（默认 100000）")
    p_export.add_argument("--snapshot", action="store_true", help="同时保存一份带时间戳的快照")

    p_view = sub.add_parser("view", parents=[common], help="启动本地服务并打开（默认）")
    p_view.add_argument("--out", type=Path, default=None, help="静态模式的导出目录")
    p_view.add_argument("--since", default=None, help="静态模式：起始日期 YYYY-MM-DD")
    p_view.add_argument("--until", default=None, help="静态模式：结束日期 YYYY-MM-DD")
    p_view.add_argument("--browser", default=None, help="静态模式：只导出指定浏览器")
    p_view.add_argument("--contains", default=None, help="静态模式：只导出含该文本的记录")
    p_view.add_argument("--limit", type=int, default=None, help="静态模式：最多多少条")
    p_view.add_argument("--html-limit", type=int, default=None, help="静态模式：内嵌条数上限")
    p_view.add_argument("--static", action="store_true",
                        help="不起服务，生成自包含的只读快照文件")
    p_view.add_argument("--port", type=int, default=0, help="服务端口（默认自动挑一个空闲的）")
    p_view.add_argument("-v", "--verbose", action="store_true", help="打印每个请求")
    p_view.add_argument("--no-open", action="store_true", help="只启动，不自动打开浏览器")

    p_serve = sub.add_parser("serve", parents=[common], help="启动本地服务（不自动打开浏览器）")
    p_serve.add_argument("--port", type=int, default=0, help="服务端口（默认自动挑一个空闲的）")
    p_serve.add_argument("-v", "--verbose", action="store_true", help="打印每个请求")
    p_serve.add_argument("--no-open", action="store_true", help="不自动打开浏览器")

    p_backup = sub.add_parser("backup", parents=[common], help="备份归档数据库")
    p_backup.add_argument("--keep", type=int, default=30, help="保留最近多少份备份（默认 30，0=不清理）")
    p_backup.add_argument("--backup-dir", type=Path, default=None, help="备份目录")

    p_purge = sub.add_parser("purge", parents=[common], help="清除归档里工具自身产生的记录")
    p_purge.add_argument("--yes", action="store_true", help="真正执行删除（默认只预演）")
    p_purge.add_argument("--no-backup", action="store_true", help="删除前不备份（不建议）")
    p_purge.add_argument("--exclude", action="append", help="额外排除包含该关键字的链接（可重复）")
    p_purge.add_argument("--no-self-exclude", action="store_true", help="不把归档目录算作自我引用")

    p_excl = sub.add_parser("exclude", parents=[common],
                            help="管理额外排除的链接关键字（持久生效，计划任务也会用）")
    p_excl.add_argument("words", nargs="*", help="要添加（或配合 --remove 删除）的关键字")
    p_excl.add_argument("--remove", action="store_true", help="删除这些关键字而不是添加")
    p_excl.add_argument("--clear", dest="remove_all", action="store_true", help="清空全部关键字")

    commands = {"sync", "detect", "stats", "verify", "export", "view", "serve",
                "backup", "purge", "exclude"}
    if not argv:
        argv = ["sync"]
    elif argv[0] in ("-h", "--help"):
        pass  # 显示总帮助
    elif not any(a in commands for a in argv):
        # 没写子命令时默认执行 sync（此时全局选项仍然可用）
        argv = ["sync"] + list(argv)

    args = parser.parse_args(argv)
    if args.archive is None:
        args.archive = DEFAULT_ARCHIVE
    args.archive = Path(args.archive).expanduser()
    return args


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            # 输出重定向到管道时 Windows 会用 ANSI 代码页，统一成 UTF-8 免得中文乱码
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, OSError):
            pass

    args = parse_args(list(sys.argv[1:] if argv is None else argv))

    try:
        if args.command == "sync":
            return cmd_sync(args)
        if args.command == "detect":
            return cmd_detect(args)
        if args.command == "stats":
            return cmd_stats(args)
        if args.command == "verify":
            return cmd_verify(args)
        if args.command == "export":
            return cmd_export(args)
        if args.command == "view":
            return cmd_view(args)
        if args.command == "serve":
            return cmd_serve(args)
        if args.command == "backup":
            return cmd_backup(args)
        if args.command == "purge":
            return cmd_purge(args)
        if args.command == "exclude":
            return cmd_exclude(args)
    except KeyboardInterrupt:
        print("\n已中断")
        return 130
    except sqlite3.DatabaseError as exc:
        print(f"[x] 数据库错误: {exc}")
        return 1
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"[x] 出错: {exc}")
        return 1

    print("未知命令，用 -h 查看帮助")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
