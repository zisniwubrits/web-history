#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地服务的测试：真的把服务起起来，用 urllib 打进去验。

重点不是"能返回 200"，而是几件容易出事的事：
  * 没令牌必须打不开（服务只监听 127.0.0.1，但同机别的程序也能连）
  * 跨站请求必须被拒（浏览器会带 Origin）
  * TODO 必须真的落到归档库里，而不只是内存里
  * 接口不认识的操作、坏 JSON 不能把服务搞崩

    python test_server.py
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import tempfile
import threading
import urllib.error
import urllib.request
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


class Client:
    def __init__(self, port, token):
        self.base = f"http://127.0.0.1:{port}"
        self.token = token

    def get(self, path, token=True, headers=None):
        url = f"{self.base}{path}"
        if token:
            url += ("&" if "?" in url else "?") + f"t={self.token}"
        req = urllib.request.Request(url, headers=headers or {})
        return self._do(req)

    def post(self, path, body, token=True, headers=None, raw=None):
        url = f"{self.base}{path}"
        if token:
            url += ("&" if "?" in url else "?") + f"t={self.token}"
        data = raw if raw is not None else json.dumps(body).encode("utf-8")
        h = {"Content-Type": "application/json"}
        h.update(headers or {})
        req = urllib.request.Request(url, data=data, headers=h, method="POST")
        return self._do(req)

    @staticmethod
    def _do(req):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read().decode("utf-8", "replace")
                ctype = resp.headers.get("Content-Type", "")
                if "json" in ctype:
                    return resp.status, json.loads(raw or "{}")
                return resp.status, raw          # 页面就是 HTML，别去解 JSON
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                return exc.code, json.loads(raw)
            except ValueError:
                return exc.code, {"raw": raw}


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="wh-serve-"))
    archive_dir = tmp / "archive"
    archive_dir.mkdir()

    conn = ha.open_archive(archive_dir)
    src_id = ha.get_source_id(conn, {"kind": "chromium", "browser": "Edge",
                                     "profile": "Default", "path": tmp / "History"})
    conn.execute(
        "INSERT INTO urls(url, url_hash, scheme, host, latest_title)"
        " VALUES('https://example.com/a','h','https','example.com','示例页面')")
    uid = conn.execute("SELECT id FROM urls").fetchone()[0]
    conn.execute(
        "INSERT INTO visits(source_id,url_id,visit_time_raw,visit_time_utc,"
        "visit_time_local,day,transition,transition_name,visit_duration_ms,typed)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (src_id, uid, 13300000000000000, "2026-09-11T10:00:00.000000Z",
         "2026-09-11 18:00:00", "2026-09-11", 1, "TYPED", 0, 1))
    conn.close()

    token = "test-token-abc123"
    state = {"conn": ha.open_archive(archive_dir, threaded=True), "lock": threading.Lock(),
             "verbose": False}
    handler = ha.build_handler(archive_dir, token, state)
    httpd = ha.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    c = Client(port, token)
    print(f"测试服务起在 127.0.0.1:{port}\n")

    try:
        print("--- 鉴权 ---")
        st, body = c.get("/api/meta", token=False)
        eq(st, 403, "不带令牌访问接口被拒绝")
        st, body = c.get("/", token=False)
        eq(st, 403, "不带令牌打不开页面")
        st, body = c.get("/api/meta", token=False, headers={"X-Token": token})
        eq(st, 200, "用 X-Token 头也能通过")
        st, body = c.get("/api/meta", token=False, headers={"Cookie": f"wh_token={token}"})
        eq(st, 200, "用 Cookie 也能通过（刷新页面时不带 ?t= 也行）")

        print("\n--- 页面与数据 ---")
        st, page = c.get("/")
        eq(st, 200, "带令牌能打开页面")
        check(isinstance(page, str) and "<!doctype html>" in page.lower(),
              "返回的是 HTML 页面", str(page)[:80])
        check('const MODE = "server"' in page, "页面处于 server 模式（数据走接口）")
        check("__DATA__" not in page, "占位符已被替换")
        check("localStorage" not in page, "不再依赖浏览器本地存储")
        st, body = c.get("/api/meta")
        eq(st, 200, "meta 接口正常")
        eq(body["meta"]["rows"], 1, "meta 里的记录数与库一致")
        eq(body["meta"]["first_day"], "2026-09-11", "meta 带出日期范围")

        st, body = c.get("/api/rows")
        eq(st, 200, "rows 接口正常")
        eq(len(body["rows"]), 1, "返回 1 条记录")
        eq(len(body["rows"][0]), 12, "每行 12 列，与静态导出的 DATA 结构一致")
        eq(body["rows"][0][5], "https://example.com/a", "第 6 列是 URL")
        eq(body["rows"][0][3], "Default", "第 4 列是配置名")

        print("\n--- 页面模板是独立文件 ---")
        # 以前模板内嵌在 .py 里（占 42% 的篇幅），拆出来后编辑器才有高亮/补全，
        # 也不会再有 Python 字符串转义吃掉模板反斜杠的问题。
        eq(ha.VIEWER_TEMPLATE_NAME, "viewer.html", "模板文件名")
        tpl_path = ha.viewer_template_path()
        eq(tpl_path.parent, Path(ha.__file__).resolve().parent,
           "模板必须和 history_archive.py 同目录")
        check(tpl_path.is_file(), "模板文件存在", str(tpl_path))
        tpl = ha.load_viewer_template()
        check("<!doctype html>" in tpl.lower(), "模板是完整的 HTML")
        check("__DATA__" in tpl, "模板里有数据占位符 __DATA__")
        check("__MODE__" in tpl, "模板里有模式占位符 __MODE__")
        eq("HTML_TEMPLATE" in dir(ha), False, "Python 里不再内嵌 HTML_TEMPLATE")
        check(len(tpl.splitlines()) > 1000,
              "模板确实被完整搬出去了（%d 行）" % len(tpl.splitlines()))

        # 不做缓存：改完模板刷新页面就生效，服务不用重启
        original = tpl_path.read_bytes()
        try:
            tpl_path.write_bytes(original.replace(b"<title>", b"<title>[hot]"))
            check("[hot]" in ha.load_viewer_template(),
                  "重新读取拿到的是磁盘上的最新内容（没有缓存）")
        finally:
            tpl_path.write_bytes(original)
        check("[hot]" not in ha.load_viewer_template(), "测试后模板已还原")

        print("\n--- 未知地址 ---")
        st, body = c.get("/api/nope")
        eq(st, 404, "未知接口返回 404")
        st, body = c.get("/favicon.ico")
        eq(st, 204, "favicon 返回 204 而不是报错")

        print("\n--- TODO 增删改查 ---")
        st, body = c.get("/api/todos")
        eq(body["todos"], [], "一开始没有 TODO")

        st, body = c.post("/api/todos", {"action": "add", "item": {
            "kind": "text", "text": "看完原神 7.0 剧情", "tag": "周末"}})
        eq(st, 200, "加一条文字 TODO")
        eq(len(body["todos"]), 1, "返回的列表里有 1 条")
        todo_id = body["todos"][0]["id"]
        eq(body["todos"][0]["tag"], "周末", "标签写进去了")

        st, body = c.post("/api/todos", {"action": "add", "item": {
            "kind": "url", "url": "https://www.bilibili.com/video/BV1", "title": "视频"}})
        eq(len(body["todos"]), 2, "加一条网址 TODO")
        st, body = c.post("/api/todos", {"action": "add", "item": {
            "kind": "url", "url": "https://www.bilibili.com/video/BV1"}})
        eq(body["changed"], False, "同一条 URL 未完成时不再重复添加")
        eq(len(body["todos"]), 2, "列表没变")
        check("已" in body.get("message", "") or "在" in body.get("message", ""),
              "重复时给出了提示文字", body.get("message"))

        st, body = c.post("/api/todos", {"action": "add", "item": {"kind": "text", "text": " "}})
        eq(body["changed"], False, "空内容不添加")

        st, body = c.post("/api/todos", {"action": "toggle", "id": todo_id})
        eq([t for t in body["todos"] if t["id"] == todo_id][0]["done"], True, "能标记完成")
        st, body = c.post("/api/todos", {"action": "toggle", "id": todo_id})
        eq([t for t in body["todos"] if t["id"] == todo_id][0]["done"], False, "能取消完成")

        st, body = c.post("/api/todos", {"action": "update", "id": todo_id,
                                         "patch": {"text": "改过了", "tag": "学习"}})
        row = [t for t in body["todos"] if t["id"] == todo_id][0]
        eq(row["text"], "改过了", "能改文字")
        eq(row["tag"], "学习", "能改标签")

        print("\n--- 落库（不是只存在内存里）---")
        raw = sqlite3.connect(archive_dir / "archive.sqlite")
        n = raw.execute("SELECT COUNT(*) FROM todos").fetchone()[0]
        raw.close()
        eq(n, 2, "TODO 确实写进了 archive.sqlite")

        print("\n--- 导入与清理 ---")
        st, body = c.post("/api/todos", {"action": "import", "items": [
            {"id": "x1", "kind": "text", "text": "导入的 A"},
            {"id": todo_id, "kind": "text", "text": "会被忽略（id 已存在）"},
            {"kind": "text", "text": ""},
        ]})
        eq(len(body["todos"]), 3, "导入只加了新的那条")
        check("跳过 1" in (body.get("message") or ""), "如实报告跳过了几条",
              body.get("message"))

        st, body = c.post("/api/todos", {"action": "toggle", "id": "x1"})
        st, body = c.post("/api/todos", {"action": "clear_done"})
        eq(len([t for t in body["todos"] if t["done"]]), 0, "清空已完成生效")

        st, body = c.post("/api/todos", {"action": "remove", "id": todo_id})
        eq(len(body["todos"]), 1, "能删除单条")
        st, body = c.post("/api/todos", {"action": "remove", "id": "不存在的"})
        eq(body["changed"], False, "删不存在的 id 不报错，只是没有变化")

        print("\n--- 坏输入不能把服务搞崩 ---")
        st, body = c.post("/api/todos", {"action": "乱写的操作"})
        eq(st, 400, "不认识的操作返回 400")
        st, body = c.post("/api/todos", None, raw="{ 这不是 json".encode("utf-8"))
        eq(st, 400, "坏 JSON 返回 400")
        st, body = c.post("/api/todos", {"action": "add"}, headers={"Content-Type": "text/plain"})
        eq(st, 415, "非 JSON 的 Content-Type 被拒（能挡掉跨站表单提交）")
        st, body = c.get("/api/meta")
        eq(st, 200, "折腾之后服务仍然正常")

        print("\n--- 跨站防护 ---")
        st, body = c.post("/api/todos", {"action": "add", "item": {"kind": "text", "text": "x"}},
                          headers={"Origin": "https://evil.example"})
        eq(st, 403, "别的站点的 Origin 被拒绝")
        st, body = c.post("/api/todos", {"action": "add", "item": {"kind": "text", "text": "y"}},
                          headers={"Origin": f"http://127.0.0.1:{port}"})
        eq(st, 200, "自己的 Origin 放行")

        print("\n--- 重启之后数据还在 ---")
        state["conn"].close()
        state["conn"] = ha.open_archive(archive_dir, threaded=True)
        st, body = c.get("/api/todos")
        eq(len(body["todos"]) >= 1, True, "重启读取仍能拿到 TODO")

    finally:
        httpd.shutdown()
        httpd.server_close()
        try:
            state["conn"].close()
        except sqlite3.Error:
            pass
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'失败 %d 项' % FAILS if FAILS else '全部通过'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
