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
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

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


def open_archive(archive_dir: Path, *, create: bool = True) -> sqlite3.Connection:
    archive_dir.mkdir(parents=True, exist_ok=True)
    db_path = archive_dir / "archive.sqlite"
    if not create and not db_path.exists():
        raise SystemExit(f"归档库还不存在: {db_path}\n请先运行: python history_archive.py sync")
    conn = sqlite3.connect(db_path, isolation_level=None)
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
        select_urls = (
            f"SELECT u.id, u.url, {title_expr}, {typed_expr} FROM urls u WHERE u.url IS NOT NULL"
        )
        for _raw_id, url, title, typed_count in src.execute(select_urls):
            if not url:
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
        select_places = (
            f"SELECT p.id, p.url, {title_expr}, {typed_expr} FROM moz_places p "
            "WHERE p.url IS NOT NULL"
        )
        for _raw_id, url, title, typed in src.execute(select_places):
            if not url:
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

    stats = {"new_visits": 0, "new_urls": 0, "new_titles": 0, "dup_merged": 0}
    ok = fail = 0
    stage_root = archive_dir / "_staging"

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
                    ingest_chromium(conn, staged, source_id, url_id_map, log, stats)
                else:
                    ingest_firefox(conn, staged, source_id, url_id_map, log, stats)

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


HTML_TEMPLATE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>浏览历史归档</title>
<style>
  /* ======================================================================
     配色与排版令牌取自 DSH Web GUI 的暗色主题（--dsw-static-* / --dsw-alias-* /
     --dsw-font-* / --dsl-*-radius），左侧注释标明对应关系，方便将来重新对齐。
     ====================================================================== */
  :root {
    color-scheme: dark;

    /* 背景层（DSH: bg-base / bg-layer-1..3） */
    --bg-base:    #151517;
    --bg-layer-1: #232324;
    --bg-layer-2: #2c2c2e;
    --bg-layer-3: #353638;

    /* 描边（DSH: border-l1 / l2 / l3） */
    --border-l1: #ffffff0f;
    --border-l2: #ffffff1f;
    --border-l3: #ffffff29;

    /* 文字（DSH: label-primary / secondary / tertiary / caption） */
    --label-primary:   #f9fafb;
    --label-secondary: #cfd3d6;
    --label-tertiary:  #adb2b8;
    --label-caption:   #81858c;
    --on-light:        #0f1115;

    /* 交互态（DSH: interactive-bg-hover / active） */
    --hover:  #ffffff14;
    --active: #ffffff24;

    /* 强调与状态色（DSH: static-blue-450 / deepseek-450 / red-400 / amber-500 / amber-400） */
    --accent:        #4d93f8;
    --accent-strong: #5686fe;
    --danger:        #f25a5a;
    --warn:          #f59e0b;
    --warn-text:     #f7ad31;

    /* 字体（DSH: --dsw-font-family / --ds-font-family-code） */
    --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
                 "Hiragino Sans GB", "Microsoft YaHei", "Helvetica Neue",
                 Helvetica, Arial, sans-serif;
    --font-mono: "SF Mono", "JetBrains Mono", "Fira Code", Consolas,
                 "Liberation Mono", Menlo, Courier, "PingFang SC", "Microsoft YaHei";

    /* 圆角与动效（DSH: 输入框 8px / 卡片 12px / 按钮胶囊 18px；--ds-ease-in-out） */
    --radius-sm: 8px;
    --radius-md: 12px;
    --radius-pill: 18px;
    --dur: .2s;
    --ease: cubic-bezier(.4, 0, .2, 1);

    /* 布局 */
    --maxw: 1180px;
    --pad: 20px;
    --headh: 0px;          /* 顶部筛选栏实测高度，由 JS 回填 */
  }
  * { box-sizing: border-box; }
  [hidden] { display:none !important; }
  body { margin:0; background:var(--bg-base); color:var(--label-primary);
         font:14px/22px var(--font-sans);
         -webkit-font-smoothing:antialiased; }

  /* DSH 的滚动条：8px 宽、圆角、hover 变亮 */
  ::-webkit-scrollbar { width:8px; height:8px; }
  ::-webkit-scrollbar-thumb { background:#3c3c3d; border-radius:4px; }
  ::-webkit-scrollbar-thumb:hover { background:#65676b; }
  ::-webkit-scrollbar-track { background:transparent; }

  /* 居中限宽容器：背景仍然通栏，内容收在中间 */
  .wrap { max-width:var(--maxw); margin:0 auto; padding:0 var(--pad); }

  header { position:sticky; top:0; z-index:5; background:var(--bg-base);
           border-bottom:1px solid var(--border-l1); padding:14px 0 12px; }
  header .wrap { display:flex; flex-direction:column; }
  h1 { margin:0 0 12px; font-size:16px; line-height:24px; font-weight:600;
       display:flex; align-items:baseline; gap:10px; flex-wrap:wrap; }
  h1 .count { font-size:13px; line-height:20px; font-weight:400;
              color:var(--label-caption); }
  .row { display:flex; gap:8px; flex-wrap:wrap; align-items:center; }

  /* 输入框：对齐 DSH 的 32px 高 + 8px 圆角 + layer-1 底色 */
  input, select { height:32px; padding:0 10px; font:14px/22px var(--font-sans);
                  color:var(--label-primary); background:var(--bg-layer-1);
                  border:1px solid var(--border-l2); border-radius:var(--radius-sm);
                  transition:border-color var(--dur) var(--ease),
                             background-color var(--dur) var(--ease); }
  input:hover, select:hover { background:var(--bg-layer-2); }
  input:focus, select:focus { outline:none; border-color:var(--accent); }
  input#q { flex:1 1 240px; min-width:180px; }
  input#q.invalid { border-color:var(--danger); }
  input[type=date] { color-scheme:dark; }

  /* 按钮：DSH 的胶囊形（18px 圆角、36px 高，这里压到 32px 与输入框齐平） */
  button { height:32px; padding:0 14px; font:14px/22px var(--font-sans);
           color:var(--label-primary); background:transparent;
           border:1px solid var(--border-l2); border-radius:var(--radius-pill);
           cursor:pointer; white-space:nowrap;
           transition:background-color var(--dur) var(--ease),
                      border-color var(--dur) var(--ease),
                      color var(--dur) var(--ease); }
  button:hover { background:var(--hover); }
  button:active { background:var(--active); }
  button:disabled { opacity:.4; cursor:not-allowed; }

  /* 正则开关：选中态用 DSH 的 ghost-active（layer-3 填充 + 1px 内描边） */
  .toggle { font-family:var(--font-mono); letter-spacing:1px; padding:0 12px; }
  .toggle.on { color:var(--accent); background:var(--bg-layer-3);
               box-shadow:inset 0 0 0 1px var(--border-l3); }

  .err { margin-top:10px; padding:8px 12px; border-radius:var(--radius-sm);
         font-size:13px; line-height:20px;
         background:#f25a5a24; border:1px solid #f25a5a66; color:var(--danger); }
  mark { background:#f59e0b3d; color:var(--warn-text);
         border-radius:3px; padding:0 1px; }

  /* ---- 时间条件区：选了「指定某一天 / 自定义范围」才出现 ---- */
  .cond { margin-top:10px; padding:10px 12px; border-radius:var(--radius-sm);
          background:var(--bg-layer-1); border:1px solid var(--border-l1); }
  .condLabel { color:var(--label-secondary); font-size:13px; line-height:20px; }
  .hint { color:var(--label-caption); font-size:13px; line-height:20px; }

  /* ---- 已生效的筛选条件：DSH 的 pill（24px 高 / 12px 圆角）---- */
  .chips { margin-top:10px; display:flex; gap:6px; flex-wrap:wrap; align-items:center; }
  .chip { display:inline-flex; align-items:center; gap:6px; height:24px;
          padding:0 6px 0 10px; font-size:12px; line-height:18px;
          color:var(--label-secondary); background:var(--bg-layer-2);
          border:1px solid var(--border-l1); border-radius:var(--radius-md); }
  .chip b { font-weight:500; color:var(--label-primary); }
  .chip button { height:18px; width:18px; padding:0; font-size:13px; line-height:1;
                 color:var(--label-caption); background:transparent; border:none;
                 border-radius:50%; }
  .chip button:hover { background:var(--hover); color:var(--label-primary); }
  .stat { color:var(--label-caption); font-size:13px; line-height:20px; margin-top:10px; }

  table { width:100%; border-collapse:collapse; table-layout:fixed; }
  th, td { text-align:left; padding:9px 12px; border-bottom:1px solid var(--border-l1);
           vertical-align:top; }
  /* top 用实测的筛选栏高度，否则表头会被顶部栏挡住 */
  th { position:sticky; top:var(--headh); z-index:4; background:var(--bg-base);
       color:var(--label-caption); font-size:12px; line-height:18px; font-weight:500;
       border-bottom:1px solid var(--border-l2); }
  tbody tr { transition:background-color var(--dur) var(--ease); }
  tbody tr:hover { background:var(--hover); }
  td.time { white-space:nowrap; color:var(--label-tertiary); font-size:13px;
            font-variant-numeric:tabular-nums; }
  td.br { color:var(--label-caption); font-size:13px; word-break:break-word; }
  td.empty { padding:44px 12px; text-align:center; color:var(--label-caption); }
  td.empty button { margin-left:8px; }
  a { color:var(--accent); text-decoration:none; word-break:break-all; }
  a:hover { text-decoration:underline; }
  .title { color:var(--label-primary); }
  .host { color:var(--label-caption); font-size:12px; line-height:18px; }

  /* 主按钮：DSH 的 primary（浅底深字），用在「加载更多」上 */
  #more { display:block; margin:20px auto 48px; height:36px; padding:0 20px;
          color:var(--on-light); background:var(--label-primary);
          border-color:transparent; font-weight:500; }
  #more:hover { background:#ebeef2; }

  /* ---- 分段控件（DSH 的 ghost-active 选中态）---- */
  .seg { display:inline-flex; gap:2px; padding:2px; background:var(--bg-layer-1);
         border:1px solid var(--border-l1); border-radius:var(--radius-pill); }
  .seg button { height:26px; padding:0 12px; font-size:13px;
                color:var(--label-tertiary); background:transparent;
                border:none; border-radius:14px; }
  .seg button:hover { background:var(--hover); color:var(--label-primary); }
  .seg button.on { color:var(--label-primary); background:var(--bg-layer-3);
                   box-shadow:inset 0 0 0 1px var(--border-l3); }
  h1 .seg { margin-left:auto; }

  /* ---- 词云 ---- */
  #cloudView { padding:20px 0 48px; }
  .cloudBar { display:flex; gap:8px; flex-wrap:wrap; align-items:center;
              margin-bottom:12px; }
  .cloudBar .hint { margin-left:auto; }
  #cloudWrap { position:relative; overflow:hidden;
               background:var(--bg-layer-1);
               border:1px solid var(--border-l1); border-radius:var(--radius-md); }
  #cloud { display:block; width:100%; cursor:pointer; }
  #cloudTip { position:absolute; left:0; top:0; opacity:0; pointer-events:none;
              padding:4px 8px; font-size:12px; line-height:18px; white-space:nowrap;
              color:var(--label-primary); background:#43454a;
              border:1px solid var(--border-l2); border-radius:var(--radius-sm);
              transition:opacity var(--dur) var(--ease); }

  /* 窄屏：收紧留白，并让固定列让出空间 */
  @media (max-width: 860px) {
    :root { --pad: 12px; }
    h1, td, td.br, td.time { font-size:13px; }
    th, td { padding:8px 8px; }
    th:nth-child(1), td:nth-child(1) { width:104px !important; }
    th:nth-child(2), td:nth-child(2) { width:84px !important; }
    th:nth-child(4), td:nth-child(4) { width:78px !important; }
    h1 { flex-direction:column; align-items:flex-start; }
    h1 .seg { margin-left:0; }
  }
</style>
</head>
<body>
<header>
 <div class="wrap">
  <h1>浏览历史归档 <span class="count" id="total"></span>
    <span class="seg" id="viewToggle">
      <button type="button" data-view="list" class="on">列表</button>
      <button type="button" data-view="cloud">词云</button>
    </span>
  </h1>

  <div class="row">
    <input id="q" placeholder="搜索 URL 或标题…" autocomplete="off" spellcheck="false">
    <button id="reToggle" class="toggle" type="button"
            title="开启正则表达式搜索（点一下切换）">.*</button>
    <select id="browser"><option value="">全部浏览器</option></select>
    <select id="time" title="按时间筛选">
      <optgroup label="快捷范围">
        <option value="all">全部时间</option>
        <option value="today">今天</option>
        <option value="yesterday">昨天</option>
        <option value="7">最近 7 天</option>
        <option value="30">最近 30 天</option>
        <option value="365">最近一年</option>
      </optgroup>
      <optgroup label="指定时间">
        <option value="day">指定某一天…</option>
        <option value="custom">自定义范围…</option>
      </optgroup>
    </select>
    <button id="clear">清空筛选</button>
  </div>

  <div class="err" id="err" hidden></div>

  <div class="row cond" id="condRow" hidden>
    <span class="condLabel" id="condLabel"></span>
    <span id="dayBox" hidden>
      <input type="date" id="day" aria-label="选择日期">
    </span>
    <span id="rangeBox" hidden class="row">
      <input type="date" id="from" aria-label="起始日期">
      <span class="hint">至</span>
      <input type="date" id="to" aria-label="结束日期">
      <button id="fillAll" type="button">填满全部范围</button>
    </span>
    <span class="hint" id="condHint"></span>
  </div>

  <div class="chips" id="chips"></div>
  <div class="stat" id="stat"></div>
 </div>
</header>
<main class="wrap">
 <section id="listView">
  <table>
    <thead><tr>
      <th style="width:148px">时间</th><th style="width:116px">浏览器</th>
      <th>标题 / 链接</th><th style="width:104px">类型</th>
    </tr></thead>
    <tbody id="tbody"></tbody>
  </table>
  <button id="more" style="display:none">加载更多</button>
 </section>

 <section id="cloudView" hidden>
  <div class="cloudBar">
    <span class="seg" id="srcToggle">
      <button type="button" data-src="title" class="on">标题</button>
      <button type="button" data-src="host">站点</button>
      <button type="button" data-src="both">两者</button>
    </span>
    <select id="cloudTop" title="显示多少个词">
      <option value="50">前 50 个词</option>
      <option value="100" selected>前 100 个词</option>
      <option value="200">前 200 个词</option>
    </select>
    <button id="cloudRedraw" type="button">重新摆放</button>
    <span class="hint" id="cloudHint"></span>
  </div>
  <div id="cloudWrap">
    <canvas id="cloud"></canvas>
    <div id="cloudTip"></div>
  </div>
 </section>
</main>
<script>
const DATA = __DATA__;
const PAGE = 400;

const tbody   = document.getElementById('tbody');
const statEl  = document.getElementById('stat');
const totalEl = document.getElementById('total');
const moreBtn = document.getElementById('more');
const qEl     = document.getElementById('q');
const brEl    = document.getElementById('browser');
const timeEl  = document.getElementById('time');
const dayEl   = document.getElementById('day');
const fromEl  = document.getElementById('from');
const toEl    = document.getElementById('to');
const clearEl = document.getElementById('clear');
const reToggle = document.getElementById('reToggle');
const errEl    = document.getElementById('err');
const viewToggle = document.getElementById('viewToggle');
const listView   = document.getElementById('listView');
const cloudView  = document.getElementById('cloudView');
const srcToggle  = document.getElementById('srcToggle');
const cloudTop   = document.getElementById('cloudTop');
const cloudRedraw = document.getElementById('cloudRedraw');
const cloudHint  = document.getElementById('cloudHint');
const cloudWrap  = document.getElementById('cloudWrap');
const cloud      = document.getElementById('cloud');
const cloudTip   = document.getElementById('cloudTip');
const condRow = document.getElementById('condRow');
const condLabel = document.getElementById('condLabel');
const condHint  = document.getElementById('condHint');
const dayBox    = document.getElementById('dayBox');
const rangeBox  = document.getElementById('rangeBox');
const fillAll   = document.getElementById('fillAll');
const chipsEl   = document.getElementById('chips');

// 当前筛选条件，UI 永远从这里读、往这里写
const state = { q:'', browser:'', time:'all', day:'', from:'', to:'', useRegex:false,
                view:'list', cloudSource:'title', cloudTop:100,
                cloudPhase:0, cloudRotate:3 };
let filtered = DATA, shown = 0, currentMatcher = null;

// ==========================================================================
// 纯筛选逻辑（不碰 DOM，方便单独测试）
// ==========================================================================
/* __FILTER_LOGIC_START__ */
function pad2(n){ return n < 10 ? '0' + n : '' + n; }

// 本地日期字符串。注意不能用 toISOString()，那是 UTC，
// 东八区凌晨 0-8 点会算成前一天。
function dayStr(d){
  return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate());
}
function shiftDay(base, delta){
  const d = new Date(base.getFullYear(), base.getMonth(), base.getDate());
  d.setDate(d.getDate() + delta);
  return d;
}

// 把「时间模式」翻译成一个闭区间的日期窗口；空串表示这一端不限制
function timeWindow(state, today){
  const t = today || new Date();
  switch (state.time) {
    case 'today':     return { from: dayStr(t), to: dayStr(t), label: '今天 ' + dayStr(t) };
    case 'yesterday': { const y = shiftDay(t, -1);
                        return { from: dayStr(y), to: dayStr(y), label: '昨天 ' + dayStr(y) }; }
    case '7':         return { from: dayStr(shiftDay(t, -6)),  to: dayStr(t), label: '最近 7 天' };
    case '30':        return { from: dayStr(shiftDay(t, -29)), to: dayStr(t), label: '最近 30 天' };
    case '365':       return { from: dayStr(shiftDay(t, -364)), to: dayStr(t), label: '最近一年' };
    case 'day':       return { from: state.day, to: state.day,
                               label: state.day ? ('仅 ' + state.day) : '未选择日期' };
    case 'custom':    return { from: state.from, to: state.to,
                               label: rangeLabel(state.from, state.to) };
    default:          return { from: '', to: '', label: '' };
  }
}
function rangeLabel(from, to){
  if (from && to) return from === to ? ('仅 ' + from) : (from + ' ~ ' + to);
  if (from) return from + ' 之后（含当天）';
  if (to)   return to + ' 之前（含当天）';
  return '未设置范围';
}

// row: [local, utc, browser, profile, title, url, host, transition, ms, typed, day, dup]
function matchesFilters(row, state, today, matcher){
  if (state.browser && row[2] !== state.browser) return false;
  const w = timeWindow(state, today);
  if (w.from && row[10] < w.from) return false;
  if (w.to   && row[10] > w.to)   return false;
  if (matcher === undefined) matcher = buildMatcher(parseQuery(state.q, state.useRegex));
  if (!matcher.ok) return false;
  if (matcher.empty) return true;
  return matcher.test(row[5] + ' ' + (row[4] || ''));
}

// --- 搜索词解析：普通模式 / 正则模式 ------------------------------------
// 正则模式支持 /pattern/flags 写法。
//   bare 写法（如 deepseek|openai）  -> 自动补 i，忽略大小写
//   /pattern/ 写法                  -> 同样补 i
//   /pattern/gm 写法                -> 完全按你写的 flags 来（这样才有办法区分大小写）
function parseQuery(raw, useRegex){
  raw = raw || '';
  if (!useRegex) {
    return { mode: 'plain', text: raw, flags: '', error: '' };
  }
  let body = raw.trim();
  let flags = 'i';
  const m = /^\/(.*)\/([gimsuy]*)$/.exec(body);
  if (m) {
    body = m[1];
    flags = m[2] === '' ? 'i' : m[2];
  }
  return { mode: 'regex', text: body, flags: flags, error: '' };
}

function escapeRegExp(s){ return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }

// 编译成匹配器。ok=false 表示正则写错了，error 是给用户看的原因。
function buildMatcher(query){
  if (query.mode === 'plain') {
    const needle = query.text.toLowerCase();
    if (!needle) return { ok:true, empty:true, error:'' };
    return { ok:true, empty:false, error:'',
             highlight: new RegExp(escapeRegExp(query.text), 'gi'),
             test: s => (s || '').toLowerCase().indexOf(needle) >= 0 };
  }
  if (!query.text) return { ok:true, empty:true, error:'' };

  // 关键：test() 用的正则必须去掉 g / y，否则 lastIndex 会在多次调用之间累积，
  // 同一个字符串反复 test 会时而 true 时而 false。
  const testFlags = query.flags.replace(/[gy]/g, '');
  let re;
  try {
    re = new RegExp(query.text, testFlags);
  } catch (e) {
    return { ok:false, empty:false, error:(e && e.message) ? e.message : String(e),
             test: () => false };
  }
  let hl;
  try {
    hl = new RegExp(query.text, testFlags.indexOf('g') < 0 ? testFlags + 'g' : testFlags);
  } catch (e) {
    hl = null;
  }
  return { ok:true, empty:false, error:'', regex:re, highlight:hl, test: s => re.test(s || '') };
}

// 把一段文字切成「命中 / 未命中」的片段，交给界面做高亮。纯函数，方便测试。
function splitMatches(text, matcher){
  text = text || '';
  if (!matcher || matcher.empty || !matcher.ok || !matcher.highlight) {
    return text ? [{ text: text, hit: false }] : [];
  }
  // 普通模式的高亮也要先转义，否则搜 “a.b” 会把 “axb” 也点亮
  const re = matcher.highlight;
  re.lastIndex = 0;
  const out = [];
  let last = 0, m, guard = 0;
  while ((m = re.exec(text)) !== null) {
    if (++guard > 200) break;                       // 防御病态正则卡死页面
    if (m.index > last) out.push({ text: text.slice(last, m.index), hit: false });
    if (m[0]) out.push({ text: m[0], hit: true });
    last = m.index + m[0].length;
    if (!m[0]) re.lastIndex++;                      // 空匹配必须手动前进，否则死循环
    if (re.lastIndex > text.length) break;
  }
  if (last < text.length) out.push({ text: text.slice(last), hit: false });
  return out;
}
// ==========================================================================
// 词云：分词、统计、排布（纯函数，可单独测试）
// ==========================================================================
/* __WORDCLOUD_LOGIC_START__ */
// 中文没有空格，纯标准库做不到词性标注，这里用「n-gram + 频次挑选」：
// 先统计 2/3/4 元组的出现次数，再在每个汉字串里从左往右贪心取
// 「够高频的最长片段」。高频短语（如「部落冲突」）会被整体取出，
// 低频的碎片则被丢弃——这正是词云想要的效果。
const STOPWORDS = new Set([
  // 英文
  'the','and','for','from','with','that','this','you','your','are','was','were',
  'have','has','had','not','but','all','can','will','would','there','their','what',
  'when','where','which','who','how','why','his','her','its','our','out','one',
  'two','new','get','got','use','used','using','via','com','www','http','https',
  'html','index','page','home','login','sign','search','google','about','more',
  'cc','tv','xyz','info','net','org','edu','gov','io','cn','app','dev','site',
  'user','view','list','item','post','news','main','null','undefined','true','false',
  // 中文（单字与高频虚词，出现即无信息量）
  '的','了','和','是','在','我','有','就','不','人','都','一','上','也','很',
  '到','说','要','去','你','会','着','没','看','好','自','己','这','那','他',
  '她','它','们','个','之','与','及','或','而','但','被','把','让','从','对',
  '向','于','为','以','所','能','可','该','此','其','中','等','各','又','再',
  '才','只','更','最','呢','吗','啊','吧','呀','是','地','得','过','下','来',
  '我们','他们','你们','这个','那个','什么','怎么','可以','一个','就是','不是',
  '没有','自己','现在','已经','但是','因为','所以','如果','还是','这么','那么',
  '之后','之前','时候','一些','一样','大家','需要','使用','进行','通过','关于',
  '以及','或者','并且','而且','然后','只是','还有','全部','所有','每个','各种',
  '一下','一直','一起','开始','结束','更多','相关','内容','页面','网站','首页',
]);

const CJK_CLASS = '[\\u3400-\\u4dbf\\u4e00-\\u9fff\\uf900-\\ufaff]';
const CJK_RUN_RE = new RegExp(CJK_CLASS + '+', 'g');

// 去掉链接、把各种分隔符与全角标点换成空格、折叠空白，再转小写
function cleanText(s){
  return (s || '')
    .replace(/https?:\/\/\S+/gi, ' ')
    .replace(/[\u2000-\u206f\u3001-\u303f\uff01-\uff5e|丨·—–_/\\>»«[\](){}<>《》，。！？、；：,.!?;:"'`~@#$%^&*+=]+/g, ' ')
    .replace(/[\u3000\s]+/g, ' ')      // 必须在标点替换之后，否则会留下连续空格
    .trim()
    .toLowerCase();
}

// 英文与数字词：至少以两个字母开头（挡掉 v3、x2 这类碎片），纯数字和停用词丢掉
function latinWords(text){
  const out = [];
  const re = /[a-z]{2}[a-z0-9]*/g;
  const lower = String(text || '').toLowerCase();
  let m;
  while ((m = re.exec(lower)) !== null) {
    if (!STOPWORDS.has(m[0])) out.push(m[0]);
  }
  return out;
}

function cjkRuns(text){
  return text.match(CJK_RUN_RE) || [];
}

// 只去掉词尾的真虚词字：「看攻略的」->「看攻略」。
// 注意别拿整张停用词表来削——「好帮手」的「好」、「下载游戏」的「下」都是实词的一部分。
const CJK_PARTICLES = new Set(['的','了','着','呢','吗','吧','啊','呀','哦','嗯',
                               '嘛','啦','咯','哇','喔','噢']);
function trimParticles(term){
  let s = term;
  while (s.length > 2 && CJK_PARTICLES.has(s[s.length - 1])) s = s.slice(0, -1);
  return s;
}

// 整个词都是停用字（例如「的我」「了他」）就丢掉
function isNoiseTerm(term){
  if (STOPWORDS.has(term)) return true;
  if (term.length < 2) return true;
  let stop = 0;
  for (const ch of term) if (STOPWORDS.has(ch)) stop++;
  return stop === term.length;
}

// 站点名：www.bilibili.com -> bilibili，i.njupt.edu.cn -> njupt
const HOST_PREFIX = new Set(['www','m','i','space','search','api','static','cdn',
  'img','v','t','bbs','blog','new','beta','docs','wiki','en','cn','mail','web',
  'passport','account','login','shop','pan','live','www2']);
const SECOND_LEVEL = new Set(['com','net','org','edu','gov','co','ac','gob','mil',
  'or','ne','go','info','biz','tv','cc']);

function hostTerm(host){
  if (!host) return '';
  const h = String(host).toLowerCase();
  if (/^[\d.]+$/.test(h)) return '';          // 纯 IP 没有站点名可言
  const parts = h.split('.').filter(Boolean);
  if (parts.length < 2) return parts[0] || '';
  let mid = parts.slice(0, -1);
  while (mid.length > 1 && HOST_PREFIX.has(mid[0])) mid = mid.slice(1);
  let name = mid[mid.length - 1] || '';
  if (SECOND_LEVEL.has(name) && mid.length > 1) name = mid[mid.length - 2];
  return name.replace(/[^a-z0-9-]/g, '');
}

// 主入口：把若干行记录聚合成 {词 -> 次数}
// source: 'title' | 'host' | 'both'
function extractTerms(rows, source, minCount){
  const count = new Map();
  const bump = (k, w) => { if (k) count.set(k, (count.get(k) || 0) + w); };
  const runs = [];            // 汉字串，稍后统一做 n-gram 挑选

  for (const r of rows) {
    const w = Math.max(1, r[11] || 1);      // 按实际访问次数加权
    if (source === 'host' || source === 'both') bump(hostTerm(r[6]), w);
    if (source === 'host') continue;

    const text = cleanText(r[4] || '');
    for (const word of latinWords(text)) bump(word, w);
    for (const run of cjkRuns(text)) runs.push([run, w]);
  }

  if (source !== 'host') {
    const need = minCount === undefined ? 3 : minCount;
    const grams = [new Map(), new Map(), new Map()];   // 2 / 3 / 4 元组
    for (const [run, w] of runs) {
      for (let n = 2; n <= 4; n++) {
        for (let i = 0; i + n <= run.length; i++) {
          const g = run.substr(i, n);
          grams[n - 2].set(g, (grams[n - 2].get(g) || 0) + w);
        }
      }
    }
    for (const [run, w] of runs) {
      let i = 0;
      while (i < run.length) {
        let taken = 0;
        for (let n = 4; n >= 2; n--) {                 // 优先取最长的
          if (i + n > run.length) continue;
          const g = run.substr(i, n);
          if ((grams[n - 2].get(g) || 0) >= need) { bump(trimParticles(g), w); taken = n; break; }
        }
        i += taken || 1;                                // 都不够高频就跳过这个字
      }
    }
  }

  for (const k of [...count.keys()]) if (isNoiseTerm(k)) count.delete(k);
  return count;
}

// 把 {词:次数} 排序并截断
function topTerms(counts, limit){
  const arr = [];
  for (const [text, n] of counts) arr.push({ text: text, count: n });
  arr.sort((a, b) => b.count - a.count || (a.text < b.text ? -1 : 1));
  return limit ? arr.slice(0, limit) : arr;
}

// 词云排布：从中心向外沿螺线找空位，放不下就跳过。
// measure(text, size) 由调用方注入，这样这个函数不依赖 canvas，可以单独测。
function layoutCloud(terms, opts){
  const o = Object.assign({
    width: 1000, height: 520, minSize: 13, maxSize: 56,
    padding: 3, maxWords: 100, measure: (t, s) => t.length * s * 0.62,
    rotateEvery: 3, phase: 0,
  }, opts || {});

  const placed = [];
  if (!terms.length || o.width <= 0 || o.height <= 0) return placed;

  const list = terms.slice(0, o.maxWords);
  const maxC = list[0].count;
  const minC = list[list.length - 1].count;
  const sMax = Math.sqrt(maxC), sMin = Math.sqrt(minC);
  const span = sMax - sMin;

  const boxes = [];
  const cx = o.width / 2, cy = o.height / 2;
  const pad = o.padding;

  for (let idx = 0; idx < list.length; idx++) {
    const term = list[idx];
    const t = span > 0 ? (Math.sqrt(term.count) - sMin) / span : 1;
    const size = Math.round(o.minSize + t * (o.maxSize - o.minSize));
    const textW = Math.max(1, o.measure(term.text, size));
    const textH = size * 1.2;
    // 每 rotateEvery 个词允许竖排一个，让画面不至于全是横条
    const rotations = (o.rotateEvery > 0 && idx % o.rotateEvery === o.rotateEvery - 1)
      ? [0, 90] : [0];

    let hit = null;
    for (let step = 0; step < 2600 && !hit; step++) {
      const angle = o.phase + step * 0.32;
      const r = 1.5 * angle;
      const x = cx + r * Math.cos(angle);
      const y = cy + r * Math.sin(angle) * 0.62;   // 竖向压扁，贴合宽扁的画布
      for (const rot of rotations) {
        const halfW = (rot ? textH : textW) / 2 + pad;
        const halfH = (rot ? textW : textH) / 2 + pad;
        const box = { x0: x - halfW, y0: y - halfH, x1: x + halfW, y1: y + halfH };
        if (box.x0 < 0 || box.y0 < 0 || box.x1 > o.width || box.y1 > o.height) continue;
        let clash = false;
        for (const b of boxes) {
          if (box.x0 < b.x1 && box.x1 > b.x0 && box.y0 < b.y1 && box.y1 > b.y0) {
            clash = true; break;
          }
        }
        if (clash) continue;
        hit = { text: term.text, count: term.count, size: size,
                x: x, y: y, rot: rot, box: box, rank: idx };
        break;
      }
    }
    if (hit) { placed.push(hit); boxes.push(hit.box); }
  }
  return placed;
}

// 按排名给颜色，从强调蓝过渡到次级灰（都取自 DSH 令牌）
function cloudColor(rank, total){
  const p = total > 1 ? rank / (total - 1) : 0;
  if (p < 0.06) return '#4d93f8';
  if (p < 0.18) return '#679efe';
  if (p < 0.42) return '#cfd3d6';
  if (p < 0.70) return '#adb2b8';
  return '#81858c';
}
/* __WORDCLOUD_LOGIC_END__ */

/* __FILTER_LOGIC_END__ */
// ==========================================================================
// 界面
// ==========================================================================
const DAY_MIN = DATA.reduce((m, r) => (r[10] && r[10] < m ? r[10] : m), '9999-99-99');
const DAY_MAX = DATA.reduce((m, r) => (r[10] && r[10] > m ? r[10] : m), '0000-00-00');

(function init(){
  // 浏览器下拉
  const set = [...new Set(DATA.map(r => r[2]))].sort();
  for (const b of set) {
    const o = document.createElement('option');
    o.value = b; o.textContent = b; brEl.appendChild(o);
  }
  // 日期输入框限制在归档实际覆盖的范围内，省得选到没数据的日子
  for (const el of [dayEl, fromEl, toEl]) {
    el.min = DAY_MIN; el.max = DAY_MAX;
  }
  dayEl.value = DAY_MAX;   // 默认落在最近有记录的一天
  fromEl.value = DAY_MIN;
  toEl.value = DAY_MAX;
  totalEl.textContent = '共 ' + DATA.length.toLocaleString() + ' 条 · 数据范围 ' +
                        DAY_MIN + ' ~ ' + DAY_MAX;
})();

// 根据当前时间模式，显示对应的输入框，并给出说明文字
function syncTimeControls(){
  const isDay = state.time === 'day';
  const isCustom = state.time === 'custom';
  dayBox.hidden = !isDay;
  rangeBox.hidden = !isCustom;
  condRow.hidden = !(isDay || isCustom);

  if (isDay) {
    condLabel.textContent = '选定一天：';
    condHint.textContent = '（只会显示这一天的记录，可直接键入日期）';
  } else if (isCustom) {
    condLabel.textContent = '时间范围：';
    condHint.textContent = '（首尾两天都包含；只填一端表示这一端不限制）';
  } else {
    condLabel.textContent = '';
    condHint.textContent = '';
  }
}

// 已生效的条件做成可单独删除的标签
function renderChips(){
  chipsEl.innerHTML = '';
  const add = (label, value, onClear) => {
    const chip = document.createElement('span');
    chip.className = 'chip';
    chip.append(label + ' ');
    const b = document.createElement('b');
    b.textContent = value;
    chip.appendChild(b);
    const x = document.createElement('button');
    x.type = 'button'; x.textContent = '×'; x.title = '移除这个条件';
    x.onclick = onClear;
    chip.appendChild(x);
    chipsEl.appendChild(chip);
  };

  if (state.q) {
    if (state.useRegex) {
      const q = parseQuery(state.q, true);
      add('正则', '/' + q.text + '/' + q.flags,
          () => { state.q = ''; qEl.value = ''; update(); qEl.focus(); });
    } else {
      add('关键词', state.q, () => { state.q = ''; qEl.value = ''; update(); qEl.focus(); });
    }
  }
  if (state.browser) add('浏览器', state.browser, () => { state.browser = ''; brEl.value = ''; update(); });

  const w = timeWindow(state);
  if (state.time !== 'all' && w.label) {
    add('时间', w.label, () => {
      state.time = 'all'; timeEl.value = 'all';
      state.day = ''; state.from = DAY_MIN; state.to = DAY_MAX;
      update();
    });
  }

  if (chipsEl.children.length > 1) {
    const all = document.createElement('button');
    all.type = 'button'; all.textContent = '全部清除';
    all.onclick = clearAll;
    chipsEl.appendChild(all);
  }
}

// 把命中的片段包成 <mark>。全程用 DOM 节点拼，不碰 innerHTML，避免注入风险。
function setRich(el, text, matcher){
  el.textContent = '';
  for (const seg of splitMatches(text, matcher)) {
    if (seg.hit) {
      const m = document.createElement('mark');
      m.textContent = seg.text;
      el.appendChild(m);
    } else if (seg.text) {
      el.appendChild(document.createTextNode(seg.text));
    }
  }
}

function emptyRow(message){
  const tr = document.createElement('tr');
  const td = document.createElement('td');
  td.className = 'empty'; td.colSpan = 4;
  td.append(message || '没有符合这些条件的记录。');
  const btn = document.createElement('button');
  btn.type = 'button'; btn.textContent = '清空筛选，看全部';
  btn.onclick = clearAll;
  td.appendChild(btn);
  tr.appendChild(td);
  return tr;
}

function renderMore(){
  const slice = filtered.slice(shown, shown + PAGE);
  const frag = document.createDocumentFragment();
  for (const r of slice) {
    const tr = document.createElement('tr');
    const tdT = document.createElement('td');
    tdT.className = 'time'; tdT.textContent = r[0];

    const tdB = document.createElement('td');
    tdB.className = 'br'; tdB.textContent = r[3] ? (r[2] + ' / ' + r[3]) : r[2];

    const tdU = document.createElement('td');
    const a = document.createElement('a');
    a.href = r[5]; a.target = '_blank'; a.rel = 'noreferrer noopener';
    setRich(a, r[5], currentMatcher);
    const div1 = document.createElement('div');
    div1.className = 'title'; setRich(div1, r[4] || '(无标题)', currentMatcher);
    const div2 = document.createElement('div');
    div2.appendChild(a);
    const div3 = document.createElement('div');
    div3.className = 'host'; div3.textContent = r[6] || '';
    tdU.append(div1, div2, div3);

    const tdX = document.createElement('td');
    tdX.className = 'br';
    tdX.textContent = (r[7] || '') + (r[9] ? ' · 输入' : '') +
                      (r[8] ? ' · ' + (r[8] / 1000).toFixed(1) + 's' : '') +
                      (r[11] > 1 ? ' · ×' + r[11] : '');

    tr.append(tdT, tdB, tdU, tdX);
    frag.appendChild(tr);
  }
  tbody.appendChild(frag);
  shown += slice.length;

  const pct = DATA.length ? Math.round(filtered.length / DATA.length * 100) : 0;
  statEl.textContent = '显示 ' + shown.toLocaleString() + ' / 命中 ' +
                       filtered.length.toLocaleString() + ' 条' +
                       (filtered.length === DATA.length ? '（全部）' : '（占全部 ' + pct + '%）');
  moreBtn.style.display = shown < filtered.length ? 'block' : 'none';
  moreBtn.textContent = '加载更多（还有 ' + (filtered.length - shown).toLocaleString() + ' 条）';
}

function update(){
  syncTimeControls();

  const query = parseQuery(state.q, state.useRegex);
  currentMatcher = buildMatcher(query);

  // 正则写错时明确报出来，并把筛选停掉，而不是悄悄返回空结果
  const bad = !currentMatcher.ok;
  errEl.hidden = !bad;
  errEl.textContent = bad ? ('正则表达式无效：' + currentMatcher.error) : '';
  qEl.classList.toggle('invalid', bad);

  filtered = bad ? [] : DATA.filter(r => matchesFilters(r, state, undefined, currentMatcher));

  tbody.innerHTML = '';
  shown = 0;
  if (!filtered.length) {
    tbody.appendChild(bad
      ? emptyRow('正则表达式无效，已停止筛选。改好表达式或关掉 .* 开关即可。')
      : emptyRow());
    statEl.textContent = '命中 0 条 / 归档共 ' + DATA.length.toLocaleString() + ' 条';
    moreBtn.style.display = 'none';
  } else {
    renderMore();
  }
  renderChips();
  syncHeaderHeight();   // 条件行/标签行会改变顶部栏高度，表头偏移要跟着更新
  scheduleCloud();      // 词云视图下才会真正重算
}

function clearAll(){
  state.q = ''; state.browser = ''; state.time = 'all';
  state.day = ''; state.from = DAY_MIN; state.to = DAY_MAX; state.useRegex = false;
  qEl.value = ''; brEl.value = ''; timeEl.value = 'all';
  dayEl.value = DAY_MAX; fromEl.value = DAY_MIN; toEl.value = DAY_MAX;
  reToggle.classList.remove('on');
  qEl.placeholder = '搜索 URL 或标题…';
  update();
  qEl.focus();
}

function setRegexMode(on, refresh){
  state.useRegex = on;
  reToggle.classList.toggle('on', on);
  reToggle.title = on ? '正在用正则表达式搜索（点一下切回普通搜索）'
                      : '开启正则表达式搜索（点一下切换）';
  qEl.placeholder = on
    ? '正则搜索，例如 deepseek|openai\\.com ，或 /^https:\\/\\/github/i'
    : '搜索 URL 或标题…';
  if (refresh !== false) update();
}

function readStateFromUI(){
  state.q = qEl.value.trim();
  state.browser = brEl.value;
  state.time = timeEl.value;
  state.day = dayEl.value;
  state.from = fromEl.value;
  state.to = toEl.value;
}

qEl.addEventListener('input', () => { readStateFromUI(); update(); });
brEl.addEventListener('change', () => { readStateFromUI(); update(); });
timeEl.addEventListener('change', () => { readStateFromUI(); update(); });
dayEl.addEventListener('change', () => { readStateFromUI(); update(); });
fromEl.addEventListener('change', () => { readStateFromUI(); update(); });
toEl.addEventListener('change', () => { readStateFromUI(); update(); });
clearEl.addEventListener('click', clearAll);
reToggle.addEventListener('click', () => setRegexMode(!state.useRegex));
fillAll.addEventListener('click', () => {
  fromEl.value = DAY_MIN; toEl.value = DAY_MAX;
  readStateFromUI(); update();
});
// ==========================================================================
// 词云：渲染层（依赖 canvas / DOM）
// ==========================================================================
const CLOUD_FONT = (getComputedStyle(document.documentElement)
  .getPropertyValue('--font-sans') || 'sans-serif').trim() || 'sans-serif';
const measureCtx = document.createElement('canvas').getContext('2d');
let cloudItems = [];
let cloudTimer = 0;

function measureTerm(text, size){
  measureCtx.font = '600 ' + size + 'px ' + CLOUD_FONT;
  return measureCtx.measureText(text).width;
}

function renderCloud(){
  cloudTimer = 0;
  if (state.view !== 'cloud') return;

  const cssW = Math.max(320, cloudWrap.clientWidth || 1000);
  const cssH = Math.max(300, Math.min(620, Math.round(cssW * 0.46)));
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  cloud.width = Math.round(cssW * dpr);
  cloud.height = Math.round(cssH * dpr);
  cloud.style.height = cssH + 'px';
  const ctx = cloud.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssW, cssH);

  const counts = extractTerms(filtered, state.cloudSource,
                              state.cloudTop >= 200 ? 4 : 3);
  const terms = topTerms(counts, state.cloudTop);
  cloudItems = layoutCloud(terms, {
    width: cssW, height: cssH, measure: measureTerm, padding: 3,
    minSize: 13, maxSize: Math.max(26, Math.min(54, Math.round(cssH / 9))),
    maxWords: state.cloudTop,
    phase: state.cloudPhase, rotateEvery: state.cloudRotate,
  });

  const total = cloudItems.length;
  for (const it of cloudItems) {
    ctx.save();
    ctx.translate(it.x, it.y);
    if (it.rot) ctx.rotate(-Math.PI / 2);
    ctx.font = '600 ' + it.size + 'px ' + CLOUD_FONT;
    ctx.fillStyle = cloudColor(it.rank, total);
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(it.text, 0, 0);
    ctx.restore();
  }

  const dropped = terms.length - total;
  cloudHint.textContent = '基于当前筛选的 ' + filtered.length.toLocaleString() +
    ' 条记录，取出 ' + terms.length + ' 个词，放得下 ' + total + ' 个' +
    (dropped > 0 ? '（' + dropped + ' 个太挤没排进去）' : '') +
    '　·　点词语可回到列表并搜索它';
}

// 输入时防抖，避免每敲一个字就重算一遍分词
function scheduleCloud(){
  if (state.view !== 'cloud') return;
  clearTimeout(cloudTimer);
  cloudTimer = setTimeout(renderCloud, 90);
}

function cloudHitAt(clientX, clientY){
  const rect = cloud.getBoundingClientRect();
  const x = clientX - rect.left, y = clientY - rect.top;
  for (const it of cloudItems) {
    if (x >= it.box.x0 && x <= it.box.x1 && y >= it.box.y0 && y <= it.box.y1) {
      return { item: it, x: x, y: y, rect: rect };
    }
  }
  return null;
}

cloud.addEventListener('mousemove', (e) => {
  const hit = cloudHitAt(e.clientX, e.clientY);
  if (!hit) {
    cloudTip.style.opacity = '0';
    cloud.style.cursor = 'default';
    return;
  }
  cloudTip.textContent = hit.item.text + ' · ' + hit.item.count + ' 次';
  cloudTip.style.opacity = '1';
  const tw = cloudTip.offsetWidth, th = cloudTip.offsetHeight;
  cloudTip.style.left = Math.max(4, Math.min(hit.x + 12, hit.rect.width - tw - 4)) + 'px';
  cloudTip.style.top = Math.max(4, hit.y - th - 8) + 'px';
  cloud.style.cursor = 'pointer';
});
cloud.addEventListener('mouseleave', () => { cloudTip.style.opacity = '0'; });

// 点词 -> 切回列表并按这个词搜索（顺手关掉正则，免得词里有特殊字符）
cloud.addEventListener('click', (e) => {
  const hit = cloudHitAt(e.clientX, e.clientY);
  if (!hit) return;
  setRegexMode(false, false);
  qEl.value = hit.item.text;
  state.q = hit.item.text;
  setView('list');
});

function setView(view){
  state.view = view;
  for (const b of viewToggle.querySelectorAll('button')) {
    b.classList.toggle('on', b.dataset.view === view);
  }
  listView.hidden = view !== 'list';
  cloudView.hidden = view !== 'cloud';
  cloudTip.style.opacity = '0';
  update();
}

viewToggle.addEventListener('click', (e) => {
  const b = e.target.closest('button');
  if (b) setView(b.dataset.view);
});
srcToggle.addEventListener('click', (e) => {
  const b = e.target.closest('button');
  if (!b) return;
  state.cloudSource = b.dataset.src;
  for (const x of srcToggle.querySelectorAll('button')) x.classList.toggle('on', x === b);
  renderCloud();
});
cloudTop.addEventListener('change', () => {
  state.cloudTop = parseInt(cloudTop.value, 10) || 100;
  renderCloud();
});
cloudRedraw.addEventListener('click', () => {
  state.cloudPhase = Math.random() * Math.PI * 2;
  state.cloudRotate = 2 + Math.floor(Math.random() * 3);
  renderCloud();
});

moreBtn.addEventListener('click', renderMore);

// 顶部筛选栏的高度会随条件行、标签行出现而变化，表头 sticky 的偏移量得跟着更新，
// 否则滚动时列标题会被顶部栏盖住。
function syncHeaderHeight(){
  const h = document.querySelector('header');
  if (!h) return;
  // 减去边框，让表头严丝合缝贴在筛选栏下沿
  const px = Math.max(0, h.getBoundingClientRect().height - 1);
  document.documentElement.style.setProperty('--headh', px + 'px');
}
window.addEventListener('resize', () => {
  syncHeaderHeight();
  if (state.view === 'cloud') scheduleCloud();
});

readStateFromUI();
update();
</script>
</body>
</html>
"""


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
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        path.write_text(HTML_TEMPLATE.replace("__DATA__", payload), encoding="utf-8")
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


def cmd_view(args) -> int:
    out_dir: Path = args.out or (args.archive / "exports")
    ns = argparse.Namespace(
        archive=args.archive,
        out=out_dir,
        format="html",
        since=args.since,
        until=args.until,
        browser=args.browser,
        contains=args.contains,
        limit=args.limit,
        html_limit=args.html_limit,
        snapshot=False,
    )
    rc = cmd_export(ns)
    if rc != 0:
        return rc

    page = out_dir / "history.html"
    if not page.exists():
        print(f"[x] 没有生成 {page}（--format 里需要包含 html）")
        return 1

    if args.no_open:
        print(f"\n用浏览器打开这个文件即可查看: {page}")
        return 0

    print(f"\n正在用默认浏览器打开: {page}")
    if not open_in_browser(page):
        print("[!] 自动打开失败，手动双击这个文件即可:")
        print(f"    {page}")
        return 1
    print("在页面里可以搜索关键词（点 .* 切成正则表达式）、按浏览器筛选；")
    print("「时间」下拉里有今天/昨天/最近 7 天等快捷范围，也可以选「指定某一天」或「自定义范围」；")
    print("生效的条件会显示成标签，点 × 单独去掉。")
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

    p_view = sub.add_parser("view", parents=[common], help="生成网页版历史记录并用浏览器打开")
    p_view.add_argument("--out", type=Path, default=None, help="导出目录（默认 <归档>/exports）")
    p_view.add_argument("--since", default=None, help="只看该日期之后的记录 YYYY-MM-DD")
    p_view.add_argument("--until", default=None, help="只看该日期之前的记录 YYYY-MM-DD")
    p_view.add_argument("--browser", default=None, help="只看指定浏览器，逗号分隔")
    p_view.add_argument("--contains", default=None, help="只看 URL/标题包含该文本的记录")
    p_view.add_argument("--limit", type=int, default=None, help="最多放进去多少条")
    p_view.add_argument("--html-limit", type=int, default=None, help="内嵌条数上限（默认 100000）")
    p_view.add_argument("--no-open", action="store_true", help="只生成文件，不自动打开浏览器")

    p_backup = sub.add_parser("backup", parents=[common], help="备份归档数据库")
    p_backup.add_argument("--keep", type=int, default=30, help="保留最近多少份备份（默认 30，0=不清理）")
    p_backup.add_argument("--backup-dir", type=Path, default=None, help="备份目录")

    commands = {"sync", "detect", "stats", "verify", "export", "view", "backup"}
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
        if args.command == "backup":
            return cmd_backup(args)
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
