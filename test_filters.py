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

    print("\n--- exclude.txt（持久配置，计划任务也会读）---")
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="wh-excl-"))
    try:
        eq(ha.load_exclude_patterns(tmp), [], "没有配置文件时返回空")
        eq(ha.exclude_file(tmp).name, "exclude.txt", "配置文件名")

        ha.save_exclude_patterns(tmp, ["bili-history.html"])
        eq(ha.load_exclude_patterns(tmp), ["bili-history.html"], "写得进也读得出")

        # 注释和空行必须被忽略
        ha.exclude_file(tmp).write_text(
            "# 这是注释\n\n  bili-history.html  \n\n# 又一条注释\n其它关键字\n",
            encoding="utf-8")
        eq(ha.load_exclude_patterns(tmp), ["bili-history.html", "其它关键字"],
           "注释、空行、首尾空格都被正确忽略")

        # 过滤函数要自动读这份配置，不需要命令行参数
        keep = ha.make_url_filter(tmp)
        bili_url = ("file:///C:/Users/%E6%9B%BE%E5%AD%90%E7%91%9C/AppData/Local/"
                    "bili-history-archive/exports/bili-history.html")
        eq(keep(bili_url), False, "exclude.txt 里的关键字自动生效")
        eq(keep("https://www.bilibili.com/"), True, "没被关键字命中的照常归档")

        # 大小写不敏感
        ha.save_exclude_patterns(tmp, ["BILI-History.HTML"])
        eq(ha.make_url_filter(tmp)(bili_url), False, "关键字匹配不区分大小写")

        # 和命令行 --exclude 叠加，互不覆盖
        ha.save_exclude_patterns(tmp, ["bili-history.html"])
        keep2 = ha.make_url_filter(tmp, ["另一个关键字"])
        eq(keep2(bili_url), False, "配置文件里的关键字仍然生效")
        eq(keep2("https://x.com/另一个关键字/y"), False, "命令行的关键字也生效")
        eq(keep2("https://normal.example/"), True, "两者都不命中时照常归档")

        # 清空
        ha.save_exclude_patterns(tmp, [])
        eq(ha.load_exclude_patterns(tmp), [], "清空之后没有关键字")
        eq(ha.make_url_filter(tmp)(bili_url), True, "清空后该链接不再被排除")

        # 归档目录本身还在不在（tmp 下没有 self 目录，所以全都该放行）
        eq(ha.make_url_filter(tmp)("file:///E:/anywhere/x.html"), True,
           "没配关键字时，非归档目录的本地文件照常归档")
    finally:
        import shutil as _sh
        _sh.rmtree(tmp, ignore_errors=True)

    print("\n--- 服务模式页面（http://127.0.0.1:端口/history/）---")
    # 这是真实踩过的坑：静态快照是 file:// 能认出来，但改成服务模式之后
    # 页面地址变成 http://127.0.0.1:端口/?t=令牌，同样会进浏览器历史，
    # 而端口和令牌每次都变，光靠它们认不出来——所以固定了一个 /history/ 路径。
    eq(ha.is_viewer_url("http://127.0.0.1:50070/history/?t=84zx11UpxHRzj2npVrhuxyqw"),
       True, "新版固定路径")
    eq(ha.is_viewer_url("http://127.0.0.1:50070/history/"), True, "不带令牌也认")
    eq(ha.is_viewer_url("http://localhost:8080/history/?x=1"), True, "localhost 同样算")
    eq(ha.is_viewer_url("http://[::1]:9000/history/"), True, "IPv6 回环也算")
    eq(ha.is_viewer_url("http://127.0.0.1:8731/?t=bDDgcmUjYxcBN-DY9vxsu_n-"), True,
       "旧版形式（根路径 + 令牌）也认，免得升级前的记录留在库里")
    eq(ha.is_viewer_url("https://127.0.0.1:8731/?t=bDDgcmUjYxcBN-DY9vxsu_n-"), True,
       "https 的旧版形式")

    print("\n--- 这些本地服务不能被误伤（重要）---")
    for url, why in [
        ("http://127.0.0.1:3080/", "DSH GUI"),
        ("http://localhost:3000/", "Open WebUI"),
        ("http://localhost:3000/auth", "Open WebUI 登录页"),
        ("http://localhost:5173/learn", "CET-Learn"),
        ("http://localhost:8000/reader.html?ch=21", "阅读器"),
        ("http://127.0.0.1:55101/?redirect_uri=vscode%3A%2F%2F", "VS Code 登录回调"),
        ("http://127.0.0.1:8081/health", "健康检查端点"),
        ("http://localhost:3000/?t=abc", "t 太短，不是本工具的令牌"),
        ("https://127.0.0.1.evil.com/history/", "域名伪装"),
        ("https://example.com/history/", "外网的 /history/"),
        ("http://192.168.1.5:5000/history/", "局域网地址"),
        ("file:///E:/x/history/index.html", "file 协议另走一条规则"),
        ("", "空串"),
    ]:
        eq(ha.is_viewer_url(url), False, f"不误伤：{why}")

    print("\n--- 过滤器把服务页面也排掉 ---")
    keep3 = ha.make_url_filter(ARCHIVE)
    eq(keep3("http://127.0.0.1:50070/history/?t=abc123456789012345678"), False,
       "服务页面默认被排除")
    eq(keep3("http://localhost:5173/learn"), True, "别人的本地服务照常归档")
    eq(ha.make_url_filter(ARCHIVE, exclude_self=False)(
        "http://127.0.0.1:50070/history/"), True,
       "--no-self-exclude 时不再排除服务页面")

    print(f"\n{'失败 %d 项' % FAILS if FAILS else '全部通过'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
