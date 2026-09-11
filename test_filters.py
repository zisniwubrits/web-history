#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
链接过滤的测试：判断一条记录是不是「工具自己产生的」。

最典型的是用 view 打开的 history.html——它被打开就进了浏览器历史，
下次 sync 又会把它归档进来，越滚越多且毫无信息量。

路径比较是这里最容易静默出错的地方（盘符大小写、正斜杠反斜杠、
百分号转义、以及 archive 与 archive2 这种前缀陷阱），所以单独测。

    python test_filters.py
"""

from __future__ import annotations

import sys
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


ARCHIVE = Path(r"E:\WorkStation\AI\web-history\archive")


def main() -> int:
    print("--- file:// 路径还原 ---")
    eq(ha.file_url_to_path("file:///E:/a/b.html"), r"e:\a\b.html", "普通 Windows 路径")
    eq(ha.file_url_to_path("file:///e:/A/B.HTML"), r"e:\a\b.html", "统一小写")
    eq(ha.file_url_to_path("file:///E:/a/b%20c.html"), r"e:\a\b c.html", "百分号转义被还原")
    # 用「张三」这种明显的占位名，别用本机真实用户名——这是要推到公开仓库的
    eq(ha.file_url_to_path(
        "file:///C:/Users/%E5%BC%A0%E4%B8%89/x.html"),
        "c:\\users\\张三\\x.html", "中文用户名能正确解码")
    eq(ha.file_url_to_path("file:///E:/a/"), r"e:\a", "结尾斜杠被去掉")
    eq(ha.file_url_to_path("https://example.com/x"), "", "非 file 协议返回空")
    eq(ha.file_url_to_path(""), "", "空串返回空")
    eq(ha.file_url_to_path("file:///"), "", "根路径返回空（不能匹配一切）")

    print("\n--- 判断是否自我引用 ---")
    eq(ha.is_self_url("file:///E:/WorkStation/AI/web-history/archive/exports/history.html",
                      ARCHIVE), True, "归档目录里的导出页面")
    eq(ha.is_self_url("file:///E:/WorkStation/AI/web-history/archive/archive.sqlite",
                      ARCHIVE), True, "归档库文件本身")
    eq(ha.is_self_url("file:///E:/WorkStation/AI/web-history/archive", ARCHIVE), True,
       "归档目录本身")
    eq(ha.is_self_url("file:///e:/workstation/ai/WEB-HISTORY/ARCHIVE/exports/x.html",
                      ARCHIVE), True, "大小写不同也算同一个位置")
    eq(ha.is_self_url("file:///E:/WorkStation/AI/web-history/archive2/x.html",
                      ARCHIVE), False, "archive2 不能被 archive 前缀误伤")
    eq(ha.is_self_url("file:///E:/WorkStation/AI/web-history/other.html", ARCHIVE), False,
       "归档目录之外的本地文件要保留")
    eq(ha.is_self_url("file:///E:/WorkStation/AI/pixiaoshuo/reader.html", ARCHIVE), False,
       "别的项目的本地页面要保留")
    eq(ha.is_self_url("https://example.com/archive/exports/history.html", ARCHIVE), False,
       "网址里带 archive 字样不受影响（非 file 协议）")
    eq(ha.is_self_url("", ARCHIVE), False, "空 URL")

    print("\n--- 过滤器 ---")
    keep = ha.make_url_filter(ARCHIVE)
    eq(keep("file:///E:/WorkStation/AI/web-history/archive/exports/history.html"), False,
       "默认排除归档目录下的页面")
    eq(keep("https://www.bilibili.com/"), True, "普通网址照常归档")
    eq(keep("file:///E:/other/notes.html"), True, "其他本地文件照常归档")

    keep_off = ha.make_url_filter(ARCHIVE, exclude_self=False)
    eq(keep_off("file:///E:/WorkStation/AI/web-history/archive/exports/history.html"), True,
       "--no-self-exclude 时不再排除")

    keep_extra = ha.make_url_filter(ARCHIVE, ["bili-history.html"])
    eq(keep_extra("file:///C:/Users/x/AppData/Local/bili-history-archive/exports/bili-history.html"),
       False, "--exclude 关键字能排除归档目录之外的同类页面")
    eq(keep_extra("file:///E:/WorkStation/AI/web-history/archive/exports/history.html"),
       False, "加了 --exclude 之后，默认的自我引用排除依然生效")
    eq(keep_extra("https://www.bilibili.com/"), True, "无关网址不受 --exclude 影响")

    keep_upper = ha.make_url_filter(ARCHIVE, ["BILI-HISTORY.HTML"])
    eq(keep_upper("file:///x/bili-history.html"), False, "关键字匹配不区分大小写")

    print("\n--- 归档目录可以用相对路径 / 带斜杠 ---")
    for variant in [Path("archive"), Path("archive/"), Path("./archive")]:
        eq(ha.is_self_url("file:///" + str(ARCHIVE).replace("\\", "/") + "/exports/history.html",
                          variant.resolve()), True,
           f"归档目录写成 {variant} 也能正确判断")

    print(f"\n{'失败 %d 项' % FAILS if FAILS else '全部通过'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
