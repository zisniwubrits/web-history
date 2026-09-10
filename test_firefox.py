#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Firefox 支持路径的验证测试。

Firefox 和 Chromium 的历史库结构完全不同（moz_places / moz_historyvisits，
时间戳是 1970 起算的微秒），而开发机上没有装 Firefox，所以这条路径容易一直是
"写了但没跑过"。这个脚本用真实 Firefox schema 造一个历史库，把发现和归档
两条路径都跑一遍。

    python test_firefox.py
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import history_archive as ha

FAILS = 0


def check(ok: bool, label: str, detail: str = ""):
    global FAILS
    if not ok:
        FAILS += 1
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"\n        {detail}"))


def eq(actual, expected, label):
    check(actual == expected, label, f"期望 {expected!r}，实际 {actual!r}")


# ---------------------------------------------------------------- 构造测试库
# 取自真实 Firefox 的 places.sqlite 结构（只保留归档会用到的列，外加几个
# 无关列用来确认代码不会因为多出来的列而出问题）
PLACES_DDL = """
CREATE TABLE moz_places (
    id INTEGER PRIMARY KEY,
    url LONGVARCHAR,
    title LONGVARCHAR,
    rev_host LONGVARCHAR,
    visit_count INTEGER DEFAULT 0,
    hidden INTEGER DEFAULT 0 NOT NULL,
    typed INTEGER DEFAULT 0 NOT NULL,
    frecency INTEGER DEFAULT -1 NOT NULL,
    last_visit_date INTEGER,
    guid TEXT,
    foreign_count INTEGER DEFAULT 0 NOT NULL,
    url_hash INTEGER DEFAULT 0 NOT NULL,
    description TEXT,
    site_name TEXT
);
CREATE TABLE moz_historyvisits (
    id INTEGER PRIMARY KEY,
    from_visit INTEGER,
    place_id INTEGER,
    visit_date INTEGER,
    visit_type INTEGER,
    session INTEGER,
    source INTEGER DEFAULT 0 NOT NULL
);
CREATE TABLE moz_origins (id INTEGER PRIMARY KEY, prefix TEXT, host TEXT, frecency INTEGER);
"""

EPOCH_2023_11_14 = 1_700_000_000_000_000   # 2023-11-14T22:13:20Z


def build_places_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(PLACES_DDL)
    conn.executemany(
        "INSERT INTO moz_places(id,url,title,visit_count,hidden,typed,frecency,last_visit_date,guid)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        [
            (1, "https://www.mozilla.org/zh-CN/", "Mozilla 中国", 3, 0, 1, 100,
             EPOCH_2023_11_14, "aaa"),
            (2, "https://github.com/", "GitHub: Let's build from here", 1, 0, 0, 90,
             EPOCH_2023_11_14 + 60_000_000, "bbb"),
            (3, "https://example.com/no-title", None, 1, 0, 0, 50, None, "ccc"),
            (4, "https://hidden.example/", "隐藏的占位记录", 1, 1, 0, 10, None, "ddd"),
        ],
    )
    conn.executemany(
        "INSERT INTO moz_historyvisits(id,from_visit,place_id,visit_date,visit_type,session)"
        " VALUES(?,?,?,?,?,?)",
        [
            (1, 0, 1, EPOCH_2023_11_14, 2, 1),                       # 手输
            (2, 0, 1, EPOCH_2023_11_14 + 1_000_000, 1, 1),           # 链接
            (3, 0, 1, EPOCH_2023_11_14 + 2_000_000, 1, 1),           # 同一条链接再来一次
            (4, 0, 2, EPOCH_2023_11_14 + 60_000_000, 1, 1),
            (5, 0, 3, EPOCH_2023_11_14 + 120_000_000, 5, 1),         # 永久重定向
            (6, 0, 4, EPOCH_2023_11_14 + 180_000_000, 1, 2),
            (7, 0, 99, EPOCH_2023_11_14 + 240_000_000, 1, 2),        # 指向不存在的 place
            (8, 0, 1, 0, 1, 2),                                      # 时间戳非法
            (9, 0, 1, EPOCH_2023_11_14 + 1_000_000, 1, 1),           # 与第 2 条完全重复
        ],
    )
    conn.commit()
    conn.close()


def build_profile(root: Path) -> Path:
    """造一个带 profiles.ini 的 Firefox 根目录，返回 places.sqlite 路径。"""
    prof = root / "Profiles" / "abc123.default-release"
    prof.mkdir(parents=True)
    places = prof / "places.sqlite"
    build_places_db(places)
    (root / "profiles.ini").write_text(
        "[General]\nStartWithLastProfile=1\nVersion=2\n\n"
        "[Profile0]\nName=default-release\nIsRelative=1\n"
        "Path=Profiles/abc123.default-release\nDefault=1\n",
        encoding="utf-8",
    )
    return places


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="wh-firefox-"))
    try:
        # ---------------------------------------------------- 1) 发现路径
        print("--- 发现路径（profiles.ini 解析）---")
        ff_root = tmp / "Firefox"
        places = build_profile(ff_root)

        original_roots = ha.FIREFOX_ROOTS
        ha.FIREFOX_ROOTS = [str(ff_root)]          # 指向临时目录，不碰真实配置
        try:
            found = ha.discover_firefox()
        finally:
            ha.FIREFOX_ROOTS = original_roots

        eq(len(found), 1, "从 profiles.ini 里发现 1 个 Firefox 配置")
        if found:
            eq(found[0]["kind"], "firefox", "类型标记为 firefox")
            eq(found[0]["browser"], "Firefox", "浏览器名")
            eq(found[0]["profile"], "default-release", "配置名取自 profiles.ini")
            eq(Path(found[0]["path"]).resolve(), places.resolve(), "places.sqlite 路径解析正确")

        # ---------------------------------------------------- 2) 归档路径
        print("\n--- 归档路径（ingest_firefox）---")
        archive_dir = tmp / "archive"
        conn = ha.open_archive(archive_dir)
        log = ha.Logger(None, False)
        stats = {"new_visits": 0, "new_urls": 0, "new_titles": 0}
        src = {"kind": "firefox", "browser": "Firefox", "profile": "default-release",
               "path": places}
        source_id = ha.get_source_id(conn, src)
        url_id_map = ha.load_url_id_map(conn)

        def run_ingest():
            conn.execute("BEGIN IMMEDIATE")
            for ddl in ha.TEMP_TABLES_DDL.split(";"):
                if ddl.strip():
                    conn.execute(ddl)
            conn.execute("DELETE FROM _touched")
            conn.execute("DELETE FROM _raw")
            ha.ingest_firefox(conn, places, source_id, url_id_map, log, stats)
            ha.refresh_url_times(conn)
            conn.execute("COMMIT")

        run_ingest()
        ha.backfill_visit_titles(conn)

        # 9 条里有 2 条应当被跳过：place 不存在、时间戳为 0
        eq(conn.execute("SELECT COUNT(*) FROM visits").fetchone()[0], 6,
           "6 条唯一访问入库（两条完全重复的合并成一行）")
        eq(conn.execute("SELECT SUM(dup_count) FROM visits").fetchone()[0], 7,
           "重数合计 7，重复的那条没有被丢掉")
        eq(conn.execute("SELECT COUNT(*) FROM urls").fetchone()[0], 4, "4 个 URL 入库")
        eq(stats["new_urls"], 4, "新增 URL 计数")
        # 注意：new_visits 由 cmd_sync 汇总（按 source 前后快照取差值），
        # ingest_* 本身不累加这个计数，所以这里直接查库验证。

        # 时间戳换算：Firefox 是 1970 起算的微秒
        row = conn.execute(
            "SELECT visit_time_utc, visit_time_local, day FROM visits ORDER BY visit_time_raw LIMIT 1"
        ).fetchone()
        eq(row[0], "2023-11-14T22:13:20.000000Z", "UTC 时间换算正确（1970 起算的微秒）")
        eq(len(row[1]), 19, "本地时间字段格式正常")
        local_expected = datetime.fromtimestamp(EPOCH_2023_11_14 / 1e6, tz=timezone.utc) \
            .astimezone().strftime("%Y-%m-%d %H:%M:%S")
        eq(row[1], local_expected, "本地时间与本机时区一致")
        eq(row[2], local_expected[:10], "day 字段取自本地时区")

        # visit_type 映射与 typed 标记
        eq(conn.execute(
            "SELECT transition_name FROM visits WHERE visit_time_raw=?",
            (EPOCH_2023_11_14,)).fetchone()[0], "TYPED", "visit_type=2 映射成 TYPED")
        eq(conn.execute(
            "SELECT typed FROM visits WHERE visit_time_raw=?",
            (EPOCH_2023_11_14,)).fetchone()[0], 1, "visit_type=2 标记为手动输入")
        eq(conn.execute(
            "SELECT transition_name FROM visits WHERE visit_time_raw=?",
            (EPOCH_2023_11_14 + 120_000_000,)).fetchone()[0],
           "REDIRECT_PERMANENT", "visit_type=5 映射成 REDIRECT_PERMANENT")
        eq(conn.execute(
            "SELECT typed FROM visits WHERE visit_time_raw=?",
            (EPOCH_2023_11_14 + 60_000_000,)).fetchone()[0], 0, "visit_type=1 不算手输")

        # Firefox 不记录停留时长
        eq(conn.execute("SELECT MAX(visit_duration_ms) FROM visits").fetchone()[0], 0,
           "Firefox 停留时长统一为 0")

        # 标题：有标题的记住，没标题的不写脏数据
        eq(conn.execute(
            "SELECT latest_title FROM urls WHERE url='https://www.mozilla.org/zh-CN/'"
        ).fetchone()[0], "Mozilla 中国", "带标题的 URL 标题正确")
        eq(conn.execute(
            "SELECT latest_title FROM urls WHERE url='https://example.com/no-title'"
        ).fetchone()[0], None, "无标题 URL 保持 NULL")
        eq(conn.execute("SELECT COUNT(*) FROM titles").fetchone()[0], 3,
           "3 个有标题的 URL 进了 titles 表")

        # hidden 的 place 也应该照常归档（永久归档不丢东西）
        eq(conn.execute(
            "SELECT COUNT(*) FROM visits v JOIN urls u ON u.id=v.url_id"
            " WHERE u.url='https://hidden.example/'").fetchone()[0], 1,
           "hidden 的浏览记录照样归档")

        # ---------------------------------------------------- 3) 幂等
        print("\n--- 重复归档必须幂等 ---")
        before = conn.execute("SELECT COUNT(*), SUM(dup_count) FROM visits").fetchone()
        run_ingest()
        after = conn.execute("SELECT COUNT(*), SUM(dup_count) FROM visits").fetchone()
        eq(after, before, "再跑一次，行数与重数都不变")

        # ---------------------------------------------------- 4) 完整性
        print("\n--- 归档库校验 ---")
        eq(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok", "integrity_check")
        eq(conn.execute("PRAGMA foreign_key_check").fetchall(), [], "foreign_key_check")
        eq(conn.execute(
            "SELECT COUNT(*) FROM visits v LEFT JOIN urls u ON u.id=v.url_id"
            " WHERE u.id IS NULL").fetchone()[0], 0, "没有孤立访问记录")

        conn.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'失败 %d 项' % FAILS if FAILS else '全部通过'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
