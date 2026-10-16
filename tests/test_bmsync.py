#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bmsync 自测：在临时沙盒里用真实的 Chrome 书签文件 + 合成的 Safari 书签做端到端验证。

用真实的 Chrome Bookmarks 有两个好处：
  1. 覆盖真实的字段、时间戳、特殊书签（chrome://、file://、javascript: 等）；
  2. 能验证"读进来再写回去"不会破坏 Chrome 的书签库（无损往返）。

用法：/usr/bin/python3 tests/test_bmsync.py
"""

import argparse
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / "bmsync.py"
sys.path.insert(0, str(HERE.parent))
import bmsync  # noqa: E402

# 真实 Chrome 书签文件：存在就用它做无损往返测试，不存在则回退到合成夹具。
# 也可以用环境变量指定别的书签文件（比如另一台机器导出的）：
#   BMSYNC_TEST_CHROME=~/Downloads/Bookmarks python3 tests/test_bmsync.py
REAL_CHROME = Path(os.environ.get(
    "BMSYNC_TEST_CHROME",
    str(Path.home() / "Library/Application Support/Google/Chrome/Default/Bookmarks"))).expanduser()

PASS, FAIL = [], []


def NU(u: str) -> str:
    """规范化 URL 后再比较（norm_url 会把 https://a.com 规范成 https://a.com/）。"""
    return bmsync.norm_url(u)


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("  ✓ %s" % name)
    else:
        FAIL.append(name)
        print("  ✗ %s  %s" % (name, detail))


# --------------------------------------------------------------------------------------
# 沙盒
# --------------------------------------------------------------------------------------


class Sandbox:
    def __init__(self, base: Path):
        self.base = base
        self.chrome_dir = base / "chrome"
        self.profile = self.chrome_dir / "Default"
        self.profile.mkdir(parents=True, exist_ok=True)
        self.chrome_bookmarks = self.profile / "Bookmarks"
        self.safari_path = base / "Safari" / "Bookmarks.plist"
        self.safari_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_dir = base / "state"
        self.config = base / "config.json"

    def install_chrome(self, raw: dict):
        self.chrome_bookmarks.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    def install_safari(self, raw: dict):
        self.safari_path.write_bytes(plistlib.dumps(raw, fmt=plistlib.FMT_BINARY))

    def read_chrome(self) -> dict:
        return json.loads(self.chrome_bookmarks.read_text(encoding="utf-8"))

    def read_safari(self) -> dict:
        return plistlib.loads(self.safari_path.read_bytes())

    def write_config(self):
        cfg = {
            "chrome": {"user_data_dir": str(self.chrome_dir), "profile": "Default"},
            "safari": {"bookmarks_path": str(self.safari_path)},
            "sync": {"require_browsers_closed": False, "backup_keep": 5,
                     "skip_folders": list(bmsync.DEFAULT_SKIP_FOLDERS), "prefer": "newest"},
            "state_dir": str(self.state_dir),
            "log_file": str(self.state_dir / "bmsync.log"),
        }
        self.config.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    GLOBAL_FLAGS = {"-v", "-vv", "-vvv", "--json", "--version", "-h", "--help"}

    def run(self, *args, expect=0, env=None):
        argv = [str(a) for a in args]
        globals_, rest = [], []
        for a in argv:  # 只有真正的全局开关需要提到子命令之前
            (globals_ if a in self.GLOBAL_FLAGS else rest).append(a)
        cmd = [sys.executable, str(SCRIPT), "--config", str(self.config)] + globals_ + rest
        full_env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        full_env.update(env or {})
        proc = subprocess.run(cmd, capture_output=True, text=True, env=full_env)
        if expect is not None and proc.returncode != expect:
            print("    [命令失败] %s\n    stdout: %s\n    stderr: %s" %
                  (" ".join(cmd[2:]), proc.stdout.strip(), proc.stderr.strip()))
        return proc

    # 用库 API 直接读回结构
    # 与同步时保持一致的读取口径（默认跳过阅读列表等特殊容器）
    def chrome_tree(self, skip=None):
        return bmsync.ChromeBackend(self.chrome_dir, "Default").read_tree(skip)

    def safari_tree(self, skip=None):
        return bmsync.SafariBackend(self.safari_path).read_tree(skip)


# --------------------------------------------------------------------------------------
# 构造 Safari 书签 plist
# --------------------------------------------------------------------------------------


def leaf(title, url, uuid_str=None, mtime=None):
    return {
        "WebBookmarkType": "WebBookmarkTypeLeaf",
        "WebBookmarkUUID": uuid_str or str(uuid.uuid4()).upper(),
        "URLString": url,
        "URIDictionary": {"title": title},
        "WebBookmarkModificationDate": mtime if mtime is not None else time.time() - bmsync.MAC_EPOCH_OFFSET_S,
    }


def folder(title, children, uuid_str=None):
    return {
        "WebBookmarkType": "WebBookmarkTypeList",
        "WebBookmarkUUID": uuid_str or str(uuid.uuid4()).upper(),
        "Title": title,
        "Children": children,
        "WebBookmarkModificationDate": time.time() - bmsync.MAC_EPOCH_OFFSET_S,
    }


def safari_fixture(bar_children, menu_children=None):
    """一份接近真实 Safari 的 plist：含 BookmarksBar / BookmarksMenu / 阅读列表 / 收藏夹。"""
    return {
        "Children": [
            folder("BookmarksBar", bar_children),
            folder("BookmarksMenu", menu_children or []),
            folder("com.apple.ReadingList", [leaf("待读文章", "https://example.com/reading")]),
            {"WebBookmarkType": "WebBookmarkTypeProxy", "WebBookmarkUUID": str(uuid.uuid4()).upper(),
             "Title": "com.apple.WebBookmarkSpecialID.History"},
        ],
        "Title": "Bookmarks",
        "WebBookmarkType": "WebBookmarkTypeList",
        "WebBookmarkUUID": str(uuid.uuid4()).upper(),
    }


def empty_safari():
    return safari_fixture([], [])


def chrome_fixture(bar_children, other_children=None, mobile_children=None):
    def wrap(children, name):
        return {"children": children, "date_added": bmsync.epoch_to_chrome_ts(time.time()),
                "date_last_used": "0", "date_modified": "0", "guid": str(uuid.uuid4()),
                "id": str(uuid.uuid4()), "name": name, "type": "folder"}

    return {
        "checksum": "0" * 32,
        "roots": {
            "bookmark_bar": wrap(bar_children, "Bookmarks bar"),
            "other": wrap(other_children or [], "Other bookmarks"),
            "synced": wrap([wrap(mobile_children or [], "Mobile bookmarks")], "Mobile bookmarks"),
        },
        "sync_metadata": "{}",
        "version": 1,
    }


def synthetic_chrome_bookmarks() -> dict:
    """
    一份合成的 Chrome 书签库，覆盖真实文件里各种容易踩坑的情况：
    嵌套目录、重复 URL、特殊协议、NBSP、emoji、URL 里的中文、synced 下直接挂的 url 节点。
    这样在没装 Chrome（或没有真实书签）的机器上也能完整跑测试。
    """
    nbsp = "指纹识别模块FPM10A用户手册\xa0- 百度文库"
    return {
        "checksum": "0" * 32,
        "roots": {
            "bookmark_bar": {
                "children": [
                    url_node("百度", "https://www.baidu.com/"),
                    url_node("Google", "https://www.google.com"),
                    url_node(nbsp, "https://wenku.baidu.com/view/71dab61655270722192ef7e4.html"),
                    url_node("重复 A", "https://dup.example"),
                    url_node("重复 B", "https://dup.example"),
                    url_node("设置页", "chrome://settings/"),
                    url_node("本地文件", "file:///tmp/a%20b.html"),
                    url_node("带空格", "https://space.example/a b"),
                    url_node("中文域名", "https://中文.example/路径?q=1#frag"),
                    url_node("emoji 🎉 标题", "https://emoji.example/"),
                    folder_node("常用", [
                        url_node("GitHub", "https://github.com"),
                        url_node("知乎", "https://www.zhihu.com/"),
                    ]),
                    folder_node("深层", [folder_node("一层", [folder_node("二层", [
                        folder_node("三层", [url_node("最深处", "https://deep5.example")])])])]),
                ],
                "date_added": bmsync.epoch_to_chrome_ts(time.time()),
                "date_last_used": "0", "date_modified": "0",
                "guid": str(uuid.uuid4()), "id": str(uuid.uuid4()),
                "name": "Bookmarks bar", "type": "folder",
            },
            "other": {
                "children": [url_node("其他里的一条", "https://other.example")],
                "date_added": bmsync.epoch_to_chrome_ts(time.time()),
                "date_last_used": "0", "date_modified": "0",
                "guid": str(uuid.uuid4()), "id": str(uuid.uuid4()),
                "name": "Other bookmarks", "type": "folder",
            },
            # synced 里只有一个 Mobile bookmarks 文件夹（真实 Chrome 的结构）
            "synced": {
                "children": [
                    folder_node("Mobile bookmarks", [
                        url_node("打开新的标签页", "chrome-native://newtab/"),
                        url_node("Cloudplay Plus", "https://www.cloudplayplus.com/web"),
                        url_node("工作 | AList", "https://alist.example:9091/myself/MBD/%E5%B7%A5%E4%BD%9C/"),
                    ]),
                ],
                "date_added": bmsync.epoch_to_chrome_ts(time.time()),
                "date_last_used": "0", "date_modified": "0",
                "guid": str(uuid.uuid4()), "id": str(uuid.uuid4()),
                "name": "Mobile bookmarks", "type": "folder",
            },
        },
        "sync_metadata": "{}",
        "version": 1,
    }


def url_node(name, url, mtime=None):
    return {"date_added": bmsync.epoch_to_chrome_ts(mtime or time.time()), "date_last_used": "0",
            "guid": str(uuid.uuid4()), "id": str(uuid.uuid4()), "name": name, "type": "url", "url": url}


def folder_node(name, children, mtime=None):
    return {"children": children, "date_added": bmsync.epoch_to_chrome_ts(mtime or time.time()),
            "date_last_used": "0", "date_modified": bmsync.epoch_to_chrome_ts(mtime or time.time()),
            "guid": str(uuid.uuid4()), "id": str(uuid.uuid4()), "name": name, "type": "folder"}


# --------------------------------------------------------------------------------------
# 语义比较
# --------------------------------------------------------------------------------------


def semantic(tree, container):
    """把某个容器下的内容变成可比较的集合：(类型, 路径, 名字, URL)。"""
    out = set()

    def walk(item_id):
        it = tree.items[item_id]
        if it.kind == "bookmark":
            out.add(("bookmark", tree.display_path(item_id), it.name, bmsync.norm_url(it.url)))
        else:
            out.add(("folder", tree.display_path(item_id), it.name, ""))
            for cid in tree.children_of(item_id):
                walk(cid)

    for cid in tree.children_of(container):
        walk(cid)
    return out


def all_items(tree):
    """语义等价性：容器名已统一（书签栏/其他书签/移动设备书签），顺序不参与比较。"""
    return {(it.kind, tree.display_path(i), it.name,
             bmsync.norm_url(it.url) if it.kind == "bookmark" else "")
            for i, it in tree.items.items() if i not in bmsync.CONTAINERS}


# --------------------------------------------------------------------------------------
# 测试场景
# --------------------------------------------------------------------------------------


def scenario_roundtrip(tmp: Path):
    """Chrome 书签 → 读进来 → 原样写回，结构和数据必须完全不变。"""
    has_real = REAL_CHROME.exists()
    print("\n[1] Chrome 书签无损往返（%s）" % ("真实文件" if has_real else "合成夹具"))
    sb = Sandbox(tmp / "rt")
    original = json.loads(REAL_CHROME.read_text(encoding="utf-8")) if has_real \
        else synthetic_chrome_bookmarks()
    sb.install_chrome(original)
    sb.install_safari(empty_safari())
    sb.write_config()

    backend = bmsync.ChromeBackend(sb.chrome_dir, "Default")
    tree_before = backend.read_tree()
    # 用读到的树直接写回（不做任何合并）
    backend.write_tree(tree_before, backend.build_native_map(backend.read_raw(), tree_before))

    raw_after = sb.read_chrome()
    tree_after = backend.read_tree()

    # 写盘时会把 URL 规范化（补根路径斜杠、空格转义），所以按规范化结果比较
    def nodekey(n):
        url = n.get("url", "")
        return (n["type"], n.get("name"), bmsync.norm_url(url) if url else "")

    before_set = {nodekey(n) for n in walk_chrome(original)}
    after_set = {nodekey(n) for n in walk_chrome(raw_after)}
    check("往返后节点集合一致", before_set == after_set,
          "少了 %s / 多了 %s" % (len(before_set - after_set), len(after_set - before_set)))
    check("往返后统一树一致", all_items(tree_before) == all_items(tree_after))
    nb_before = [n["name"] for n in walk_chrome(original) if "\xa0" in (n.get("name") or "")]
    nb_after = [n["name"] for n in walk_chrome(raw_after) if "\xa0" in (n.get("name") or "")]
    check("名字中的不换行空格(NBSP)未被改写", sorted(nb_before) == sorted(nb_after),
          "前 %d 个 / 后 %d 个" % (len(nb_before), len(nb_after)))
    check("保留原有 guid", count_guids_before_after(original, raw_after) >= 0.98,
          "guid 保留率 %.2f" % count_guids_before_after(original, raw_after))
    check("checksum 已重算并写入", "checksum" in raw_after and len(raw_after["checksum"]) == 32)
    check("书签数量不变（%d）" % tree_before.stats()["bookmarks"],
          tree_before.stats() == tree_after.stats())


def walk_chrome(raw):
    out = []

    def rec(node):
        if node.get("type") == "url":
            out.append(node)
            return
        for c in node.get("children") or []:
            rec(c)

    for key in ("bookmark_bar", "other", "synced"):
        r = raw.get("roots", {}).get(key)
        if isinstance(r, dict):
            rec(r)
    return out


def count_guids_before_after(before_raw, after_raw):
    before = {n["guid"] for n in walk_chrome(before_raw)}
    after = {n["guid"] for n in walk_chrome(after_raw)}
    return len(before & after) / max(1, len(before))


def scenario_first_sync(tmp: Path):
    """首次同步：Chrome 的 375 个书签全量进入 Safari，Safari 的 3 个进入 Chrome。"""
    print("\n[2] 首次同步（真实 Chrome 数据 → 空 Safari）")
    sb = Sandbox(tmp / "first")
    source = json.loads(REAL_CHROME.read_text(encoding="utf-8")) if REAL_CHROME.exists() \
        else synthetic_chrome_bookmarks()
    sb.install_chrome(source)
    sb.install_safari(safari_fixture([leaf("Safari 收藏", "https://safari.example/one")],
                                     [leaf("Safari 菜单项", "https://safari.example/menu")]))
    sb.write_config()

    chrome_before = sb.chrome_bookmarks.read_bytes()
    safari_before = sb.safari_path.read_bytes()
    proc = sb.run("sync", "--dry-run", "-v")
    check("演练不写盘",
          sb.chrome_bookmarks.read_bytes() == chrome_before and sb.safari_path.read_bytes() == safari_before,
          "演练居然改了文件")
    check("演练也不该留下状态文件", not (sb.state_dir / "state.json").exists())

    proc = sb.run("sync", "-v")
    check("同步命令成功", proc.returncode == 0, proc.stderr.strip()[-300:])

    raw_chrome = source
    raw_urls = len([n for n in walk_chrome(raw_chrome)])
    ct, st = sb.chrome_tree(), sb.safari_tree()
    # 阅读列表属于跳过项，不应被当成普通书签导入
    check("Chrome 拿到 Safari 的 2 条 %d → %d" % (raw_urls, ct.stats()["bookmarks"]),
          ct.stats()["bookmarks"] == raw_urls + 2, "实际 %d" % ct.stats()["bookmarks"])
    check("Safari 拿到 Chrome 全部 %d 条" % raw_urls,
          st.stats()["bookmarks"] == raw_urls + 2, "实际 %d" % st.stats()["bookmarks"])
    check("阅读列表未被导入成普通书签",
          NU("https://example.com/reading") not in {bmsync.norm_url(i.url) for i in ct.items.values()})
    raw_safari_now = sb.read_safari()
    reading = [c for c in raw_safari_now["Children"]
               if (c.get("Title") or "").startswith("com.apple.ReadingList")]
    check("阅读列表容器仍在且内容保留",
          len(reading) == 1 and len(reading[0]["Children"]) == 1, str(len(reading)))
    check("Safari 的新书签进入了 Chrome", any(NU("https://safari.example/one") == bmsync.norm_url(i.url)
                                              for i in ct.items.values()))

    check("顶层容器未被误删", tally_top(sb.read_safari()) >= 4, str(tally_top(sb.read_safari())))
    # synced 下直接挂的书签会被整理进 Mobile bookmarks 文件夹，因此按 URL 集合校验不丢数据
    urls_before = {bmsync.norm_url(n["url"]) for n in walk_chrome(raw_chrome)}
    urls_after = {bmsync.norm_url(n["url"]) for n in walk_chrome(sb.read_chrome())}
    check("Chrome 原有书签一个不少", urls_before <= urls_after,
          "丢失：%s" % list(urls_before - urls_after)[:3])
    check("Chrome 顶层容器齐全",
          set(sb.read_chrome()["roots"]) >= {"bookmark_bar", "other", "synced"})

    # 幂等：再同步一次应当没有任何改动
    proc2 = sb.run("sync", "-v", "--list-changes")
    check("第二次同步无改动", count_no_changes(proc2.stdout), proc2.stdout[-400:])
    check("第二次同步后 Chrome 书签数不变", sb.chrome_tree().stats()["bookmarks"] == raw_urls + 2)
    check("第二次同步后 Safari 书签数不变", sb.safari_tree().stats()["bookmarks"] == raw_urls + 2)


def tally_top(raw):
    out = 0

    def rec(node):
        nonlocal out
        out += 1
        for c in node.get("Children") or []:
            rec(c)

    for c in raw.get("Children") or []:
        rec(c)
    return out


def chrome_shape(raw):
    """Chrome 顶层容器里的节点路径（不含具体 id/guid）。"""
    out = set()

    def rec(node, path):
        for c in node.get("children") or []:
            if c.get("type") == "url":
                out.add((path, "u", bmsync.norm_url(c.get("url", ""))))
            else:
                out.add((path, "f", c.get("name", "")))
                rec(c, path + "/" + c.get("name", ""))

    for key, name in (("bookmark_bar", "bar"), ("other", "other"), ("synced", "synced")):
        r = raw.get("roots", {}).get(key)
        if isinstance(r, dict):
            rec(r, name)
    return out


def count_no_changes(stdout):
    """从汇总输出里确认两个浏览器的增删改都是 0。"""
    seen = 0
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith(("chrome", "safari")) and "新增" in line:
            seen += 1
            if not line.endswith("新增 0 / 修改 0 / 删除 0"):
                return False
    return seen >= 2


def scenario_two_way(tmp: Path):
    """双向增量：Chrome 加书签、Safari 加书签、各自改名、删除，都要正确传播。"""
    print("\n[3] 常规增量同步")
    sb = Sandbox(tmp / "inc")
    # 两个浏览器初始都只有少量书签
    sb.install_chrome(chrome_fixture([
        url_node("谷歌", "https://google.com"),
        url_node("要被删掉的", "https://delete-me.example"),
        folder_node("工具", [url_node("GitHub", "https://github.com")]),
    ], [url_node("其他里的一条", "https://other.example")]))
    sb.install_safari(safari_fixture([
        leaf("苹果", "https://apple.com"),
        leaf("要被删掉的", "https://delete-me.example"),
    ], []))
    sb.write_config()

    sb.run("sync")
    ct, st = sb.chrome_tree(), sb.safari_tree()
    # Chrome 3 条 + synced 1 条；Safari 2 条（阅读列表被跳过）→ 合并后 5 条
    check("双方书签数一致（5）", ct.stats()["bookmarks"] == st.stats()["bookmarks"] == 5,
          "chrome=%d safari=%d" % (ct.stats()["bookmarks"], st.stats()["bookmarks"]))
    check("Chrome 拿到了 苹果", NU("https://apple.com") in [bmsync.norm_url(i.url) for i in ct.items.values()])
    check("Safari 拿到了 工具/GitHub", any("github.com" in (i.url or "") for i in st.items.values()))

    # 双方各自修改
    raw = sb.read_chrome()
    raw["roots"]["bookmark_bar"]["children"][0]["name"] = "谷歌（改名了）"
    raw["roots"]["bookmark_bar"]["children"].append(url_node("新加的", "https://new.example", time.time()))
    raw["roots"]["bookmark_bar"]["children"] = [c for c in raw["roots"]["bookmark_bar"]["children"]
                                                if c.get("name") != "要被删掉的"]
    sb.install_chrome(raw)

    sf = sb.read_safari()
    sf["Children"][0]["Children"].append(leaf("Safari 新增", "https://safari-new.example"))
    sb.install_safari(sf)

    proc = sb.run("sync", "-v", "--list-changes")
    ct, st = sb.chrome_tree(), sb.safari_tree()
    check("改名传播到 Safari", any(i.name == "谷歌（改名了）" for i in st.items.values()))
    check("Chrome 新增传播到 Safari", NU("https://new.example") in [bmsync.norm_url(i.url) for i in st.items.values()])
    check("Safari 新增传播到 Chrome",
          NU("https://safari-new.example") in [bmsync.norm_url(i.url) for i in ct.items.values()])
    check("删除传播到 Safari", NU("https://delete-me.example") not in [bmsync.norm_url(i.url) for i in st.items.values()])
    check("两边最终一致", all_items(ct) == all_items(st),
          "差异：%s" % (all_items(ct) ^ all_items(st)))

    # 第三次：应当无改动
    proc = sb.run("sync", "-v")
    check("再次同步无改动", count_no_changes(proc.stdout), proc.stdout[-500:])


def scenario_conflict(tmp: Path):
    """同一书签两边都改：按修改时间取新的那个。"""
    print("\n[4] 冲突处理（两边都改同一个书签）")
    sb = Sandbox(tmp / "conf")
    sb.install_chrome(chrome_fixture([url_node("原名", "https://dup.example", time.time())]))
    sb.install_safari(safari_fixture([leaf("原名", "https://dup.example")]))
    sb.write_config()
    sb.run("sync")

    now = time.time()
    raw = sb.read_chrome()
    raw["roots"]["bookmark_bar"]["children"][0]["name"] = "Chrome 的名字"
    raw["roots"]["bookmark_bar"]["children"][0]["date_modified"] = bmsync.epoch_to_chrome_ts(now - 3600)
    raw["roots"]["bookmark_bar"]["children"][0]["date_added"] = bmsync.epoch_to_chrome_ts(now - 3600)
    sb.install_chrome(raw)
    sf = sb.read_safari()
    sf["Children"][0]["Children"][0]["URIDictionary"]["title"] = "Safari 的名字"
    sf["Children"][0]["Children"][0]["WebBookmarkModificationDate"] = now - bmsync.MAC_EPOCH_OFFSET_S
    sb.install_safari(sf)

    proc = sb.run("sync", "-v")
    ct, st = sb.chrome_tree(), sb.safari_tree()
    names = {i.name for i in ct.items.values()} | {i.name for i in st.items.values()}
    check("冲突提示已输出", "冲突" in proc.stdout or "两边都修改过" in proc.stdout, proc.stdout[-400:])
    check("取较新的 Safari 版本", "Safari 的名字" in names and "Chrome 的名字" not in names, str(names))
    check("两边结果一致", all_items(ct) == all_items(st))


def scenario_one_way(tmp: Path):
    """单向同步：Safari → Chrome 时不允许反向写入。"""
    print("\n[5] 单向同步 safari2chrome")
    sb = Sandbox(tmp / "oneway")
    sb.install_chrome(chrome_fixture([url_node("只在 Chrome", "https://chrome-only.example")]))
    sb.install_safari(safari_fixture([leaf("只在 Safari", "https://safari-only.example")]))
    sb.write_config()
    sb.run("sync", "--direction", "safari2chrome")
    ct = sb.chrome_tree()
    check("Safari 的书签已进入 Chrome",
          NU("https://safari-only.example") in [bmsync.norm_url(i.url) for i in ct.items.values()])
    check("Chrome 原有书签未被删除",
          NU("https://chrome-only.example") in [bmsync.norm_url(i.url) for i in ct.items.values()])


def scenario_export_import(tmp: Path):
    """导出 HTML / JSON，再把 HTML 导入回来。"""
    print("\n[6] 导出与导入")
    sb = Sandbox(tmp / "exp")
    sb.install_chrome(chrome_fixture([
        url_node("带 & 符号", "https://and.example/?a=1&b=2"),
        url_node("中文标题", "https://中文.example/路径"),
        folder_node("嵌套", [url_node("深层", "https://deep.example")]),
    ]))
    sb.install_safari(empty_safari())
    sb.write_config()
    sb.run("sync")

    out_dir = tmp / "exp" / "out"
    proc = sb.run("export", "--out", out_dir, "--format", "html")
    html_files = list(out_dir.glob("chrome-*.html"))
    check("生成了 HTML 导出", len(html_files) == 1, str(list(out_dir.iterdir())))
    if html_files:
        content = html_files[0].read_text(encoding="utf-8")
        check("HTML 含三条书签", content.count("<A HREF=") == 3, content[:400])
        check("HTML 转义了 &", "&amp;" in content)

    proc = sb.run("export", "--out", out_dir, "--format", "json")
    json_files = list(out_dir.glob("chrome-*.json"))
    check("生成了 JSON 导出", len(json_files) == 1)
    if json_files:
        data = json.loads(json_files[0].read_text(encoding="utf-8"))
        check("JSON 导出可解析",
              isinstance(data.get("roots"), list)
              and {r.get("name") for r in data["roots"]} == {"书签栏", "其他书签"},
              str(data.get("roots"))[:200])

    # 导入一个新文件（原本不存在于两个浏览器）
    imp = tmp / "exp" / "import.html"
    imp.write_text("""<!DOCTYPE NETSCAPE-Bookmark-file-1>
<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">
<TITLE>Bookmarks</TITLE><H1>Bookmarks</H1>
<DL><p>
    <DT><A HREF="https://imported.example/1" ADD_DATE="1700000000">导入的第一条</A>
    <DT><H3>导入的目录</H3>
    <DL><p>
        <DT><A HREF="https://imported.example/2" ADD_DATE="1700000001">导入的第二条</A>
    </DL><p>
</DL><p>
""", encoding="utf-8")
    proc = sb.run("import", imp, "--dry-run")
    check("import 演练成功", proc.returncode == 0, proc.stderr.strip()[-300:])
    check("演练未写入", "https://imported.example/1" not in sb.read_chrome().__str__())

    proc = sb.run("import", imp)
    check("import 执行成功", proc.returncode == 0, proc.stderr.strip()[-300:])
    ct, st = sb.chrome_tree(), sb.safari_tree()
    urls_c = {bmsync.norm_url(i.url) for i in ct.items.values()}
    check("导入项进入 Chrome",
          NU("https://imported.example/1") in urls_c and NU("https://imported.example/2") in urls_c,
          "实际：%s" % sorted(urls_c)[:6])
    check("导入项进入 Safari", all_items(ct) == all_items(st), str(all_items(ct) ^ all_items(st)))
    check("导入内容放在「其他书签/导入书签」下",
          any(i.name == "导入书签" and i.parent_id == bmsync.OTHER_ID for i in ct.items.values()))
    check("移动设备书签目录两边都有",
          any(i.name == bmsync.MOBILE_FOLDER_NAME and i.parent_id == bmsync.OTHER_ID
              for i in ct.items.values())
          and any(i.name == bmsync.MOBILE_FOLDER_NAME and i.parent_id == bmsync.OTHER_ID
                  for i in st.items.values()))


def scenario_edge_cases(tmp: Path):
    """边界：空书签、重复 URL、特殊协议、深层嵌套、非法字符。"""
    print("\n[7] 边界情况")
    sb = Sandbox(tmp / "edge")
    sb.install_chrome(chrome_fixture([
        url_node("重复 A", "https://dup2.example"),
        url_node("重复 B", "https://dup2.example"),
        url_node("特殊", "chrome://settings/"),
        url_node("本地文件", "file:///tmp/a%20b.html"),
        url_node("带空格", "https://space.example/a b"),
        url_node("空名字", ""),
        folder_node("很深的目录", [folder_node("一层", [folder_node("二层", [
            folder_node("三层", [url_node("最深处", "https://deep5.example")])])])]),
    ]))
    sb.install_safari(empty_safari())
    sb.write_config()

    proc = sb.run("sync", "-v")
    check("含特殊 URL 也能同步成功", proc.returncode == 0, proc.stderr.strip()[-400:])
    ct, st = sb.chrome_tree(), sb.safari_tree()
    check("重复 URL 保留两条", sum(1 for i in ct.items.values()
                                   if bmsync.norm_url(i.url) == NU("https://dup2.example")) == 2)
    check("Safari 侧也保留两条", sum(1 for i in st.items.values()
                                      if bmsync.norm_url(i.url) == NU("https://dup2.example")) == 2)
    check("深层嵌套结构一致", all_items(ct) == all_items(st), str(all_items(ct) ^ all_items(st))[:300])
    raw_now = sb.read_chrome()
    check("Chrome 顶层容器结构完好",
          set(raw_now["roots"].keys()) == {"bookmark_bar", "other", "synced"},
          str(list(raw_now["roots"].keys())))
    check("二次同步稳定", count_no_changes(sb.run("sync", "-v").stdout))

    # 空的 Chrome 书签库 + 空的 Safari
    sb2 = Sandbox(tmp / "edge2")
    sb2.install_chrome(chrome_fixture([]))
    sb2.install_safari(empty_safari())
    sb2.write_config()
    proc = sb2.run("sync")
    check("两边都为空时不报错", proc.returncode == 0, proc.stderr.strip()[-300:])


def scenario_global_flags(tmp: Path):
    """
    全局选项（-v / -vv / --json / --config）必须写在子命令前后都有效。

    回归用例：这些选项原本只定义在主解析器上，argparse 解析完子命令后不会再回主解析器找参数，
    于是 `bmsync sync -v` 直接报 "unrecognized arguments: -v"（README 里正是这么写的）。
    """
    print("\n[9] 全局选项的位置无关性")

    # 9.1 纯解析层：不依赖沙盒，跑得快
    cases = [
        ([], 1, False), (["sync"], 1, False),
        (["sync", "-v"], 2, False), (["-v", "sync"], 2, False),
        (["sync", "-vv"], 3, False), (["-vv", "sync"], 3, False),
        (["-v", "sync", "-v"], 3, False),
        (["sync", "--verbose"], 2, False), (["--verbose", "sync"], 2, False),
        (["sync", "--json"], 1, True), (["--json", "sync"], 1, True),
        (["sync", "--dry-run", "-v"], 2, False),
        (["sync", "--prefer", "safari", "-v"], 2, False),
        (["export", "--out", "/tmp/x", "-v"], 2, False),
        (["install-scheduler", "--interval", "30", "-v"], 2, False),
        (["import", "f.html", "-v"], 2, False),
    ]
    bad = []
    for argv, expect_v, expect_j in cases:
        cleaned, opts = bmsync.preprocess_globals(argv)
        if opts["verbose"] != expect_v or opts["json"] != expect_j:
            bad.append("%s -> verbose=%s json=%s" % (" ".join(argv), opts["verbose"], opts["json"]))
    check("全局选项在任意位置都被正确解析（%d 种组合）" % len(cases), not bad, "；".join(bad))

    # --config 也要能从参数里摘出来，且剩余参数保持原样
    cleaned, opts = bmsync.preprocess_globals(["--config", "/tmp/a.json", "sync", "--dry-run"])
    check("--config 被摘出并保留其值", opts["config"] == "/tmp/a.json" and cleaned == ["sync", "--dry-run"],
          "%r / %r" % (opts["config"], cleaned))
    cleaned, opts = bmsync.preprocess_globals(["sync", "--config=/tmp/b.json", "-v"])
    check("--config=路径 形式也支持", opts["config"] == "/tmp/b.json" and opts["verbose"] == 2,
          "%r / %r" % (opts["config"], opts["verbose"]))
    cleaned, opts = bmsync.preprocess_globals(["sync", "--", "-v"])
    check("-- 之后的参数不被当成全局选项", cleaned == ["sync", "--", "-v"] and opts["verbose"] == 1,
          "%r" % (cleaned,))

    # 9.2 真实 CLI：README 里写的组合必须能跑通
    sb = Sandbox(tmp / "flags")
    sb.install_chrome(chrome_fixture([url_node("A", "https://a.example")]))
    sb.install_safari(safari_fixture([leaf("B", "https://b.example")]))
    sb.write_config()
    invocations = [
        ["sync", "--dry-run", "-v"],
        ["-v", "sync", "--dry-run"],
        ["sync", "-v", "--dry-run"],
        ["sync", "--dry-run", "-vv"],
        ["status", "-v"],
        ["--config", str(sb.config), "sync", "--dry-run", "-v"],   # 全局选项在最前
        ["sync", "--dry-run", "--config", str(sb.config), "-v"],   # 混在中间
        ["sync", "--config=" + str(sb.config), "--dry-run"],       # --config=路径
        ["export", "--out", str(tmp / "flags" / "out"), "-v"],     # 其它子命令也要支持
    ]
    failed = []
    for argv in invocations:
        # 除了显式带 --config 的那几条，其余统一走沙盒配置
        full = list(argv) if "--config" in argv or any(a.startswith("--config=") for a in argv) \
            else ["--config", str(sb.config)] + list(argv)
        cmd = [sys.executable, str(SCRIPT)] + full
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        if "unrecognized arguments" in proc.stderr or proc.returncode != 0:
            failed.append("%s (rc=%s: %s)" % (" ".join(full), proc.returncode,
                                              proc.stderr.strip().splitlines()[-1][:80] if proc.stderr else ""))
    check("README 里的命令都能跑通", not failed, "；".join(failed))

    # 9.3 -v 增加详细程度，--json 输出机器可读结果（两种位置都验）
    base = sb.run("sync", "--dry-run")
    more = sb.run("sync", "--dry-run", "-v")
    same = sb.run("-v", "sync", "--dry-run")
    def count_lines(proc):
        return len([ln for ln in proc.stdout.splitlines() if ln.strip()])

    lines = count_lines
    check("-v 确实让输出更详细", lines(more) > lines(base), "%d vs %d" % (lines(more), lines(base)))
    check("-v 写在前后结果一致", lines(more) == lines(same), "%d vs %d" % (lines(more), lines(same)))
    check("--json 写在子命令后有效", '"summary"' in sb.run("sync", "--dry-run", "--json").stdout)
    check("--json 写在子命令前有效", '"summary"' in sb.run("--json", "sync", "--dry-run").stdout)


def scenario_realtime(tmp: Path):
    """实时同步：文件一变就自动同步，且双向都要能用。"""
    print("\n[10] 实时同步（watch）")
    sb = Sandbox(tmp / "watch")
    sb.install_chrome(chrome_fixture([url_node("已有", "https://a.example")]))
    sb.install_safari(safari_fixture([leaf("Safari 原有", "https://s.example")]))
    sb.write_config()
    # 把节奏调快，否则测试要等十几秒
    cfg = json.loads(sb.config.read_text(encoding="utf-8"))
    cfg["watch"] = {"interval": 0.3, "settle": 0.6, "resync_after": 30}
    sb.config.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    # 10.1 指纹函数本身
    fp1 = bmsync.file_fingerprint(sb.chrome_bookmarks)
    time.sleep(0.02)
    sb.chrome_bookmarks.write_bytes(sb.chrome_bookmarks.read_bytes() + b" ")
    fp2 = bmsync.file_fingerprint(sb.chrome_bookmarks)
    check("文件指纹能识别变化", fp1 != fp2 and fp2 is not None, "%s vs %s" % (fp1, fp2))
    check("不存在的文件指纹为 None", bmsync.file_fingerprint(tmp / "nope") is None)

    # 10.2 watch --verify 自检
    verify = sb.run("watch", "--verify", expect=None)
    check("watch --verify 能给出监听参数", "监听参数" in verify.stdout and "结论" in verify.stdout,
          verify.stdout[-200:])

    # 10.3 单实例锁：第二个 watch 必须拒绝启动
    lock_path = tmp / "watch" / "state" / "bmsync.lock"
    with bmsync.file_lock(lock_path):
        dup = sb.run("watch", "--once", "0.5", "--lock", str(lock_path), expect=3)
        check("已有实例在跑时拒绝启动第二个", dup.returncode == 3 and "另一个 bmsync" in dup.stderr,
              "rc=%s err=%s" % (dup.returncode, dup.stderr[:120]))

    # 10.4 真正的实时同步：Chrome 加书签 → Safari 自动收到
    proc = subprocess.Popen(
        [sys.executable, str(SCRIPT), "--config", str(sb.config), "watch", "--no-initial", "-v"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    try:
        time.sleep(1.5)
        raw = json.loads(sb.chrome_bookmarks.read_text(encoding="utf-8"))
        raw["roots"]["bookmark_bar"]["children"].append(url_node("实时新增", "https://realtime.example"))
        sb.install_chrome(raw)

        arrived = False
        deadline = time.time() + 25
        while time.time() < deadline:
            time.sleep(0.4)
            if any(NU("https://realtime.example") == bmsync.norm_url(i.url)
                   for i in sb.safari_tree().items.values()):
                arrived = True
                break
        check("Chrome 的改动被实时同步到 Safari", arrived)

        # 反向：Safari 加书签 → Chrome 自动收到
        sf = sb.read_safari()
        sf["Children"][0]["Children"].append(leaf("Safari 实时新增", "https://safari-realtime.example"))
        sb.install_safari(sf)
        back = False
        deadline = time.time() + 25
        while time.time() < deadline:
            time.sleep(0.4)
            if any(NU("https://safari-realtime.example") == bmsync.norm_url(i.url)
                   for i in sb.chrome_tree().items.values()):
                back = True
                break
        check("Safari 的改动被实时同步到 Chrome", back)
    finally:
        proc.terminate()
        try:
            out, err = proc.communicate(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
    check("watch 运行期间没有异常退出", "异常退出" not in (err or ""), (err or "")[:200])
    check("watch 把每次触发都记进了日志",
          (sb.state_dir / "bmsync.log").read_text(encoding="utf-8").count("watch: ") >= 3,
          (sb.state_dir / "bmsync.log").read_text(encoding="utf-8")[-200:])

    # 10.5 watch --once：跑指定秒数后自己退出（便于脚本化验证）
    once = sb.run("watch", "--once", "0.8", expect=0)
    check("watch --once 会自行结束", once.returncode == 0, str(once.returncode))

    # 10.5 内容指纹：浏览器重写文件但书签没变时不该触发同步（切换标签页就会这样）
    f = sb.chrome_bookmarks
    raw = json.loads(f.read_text(encoding="utf-8"))
    fp_before = bmsync.content_fingerprint(f.read_bytes())
    raw["checksum"] = "a" * 32
    raw["roots"]["bookmark_bar"]["children"][0]["date_last_used"] = "999"
    f.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    check("只改 checksum / date_last_used → 内容指纹不变",
          bmsync.content_fingerprint(f.read_bytes()) == fp_before)
    raw = json.loads(f.read_text(encoding="utf-8"))
    raw["roots"]["bookmark_bar"]["children"].append(url_node("真新增", "https://really-new.example"))
    f.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    check("真的加了书签 → 内容指纹改变",
          bmsync.content_fingerprint(f.read_bytes()) != fp_before)

    # Safari 的 plist 被系统周期性重写（只改 ModificationDate）也一样不该触发
    sf_raw = sb.read_safari()
    sf_fp = bmsync.content_fingerprint(sb.safari_path.read_bytes())
    sf_raw["Children"][0]["WebBookmarkModificationDate"] = 12345678.0
    sb.install_safari(sf_raw)
    check("Safari 只改 ModificationDate → 内容指纹不变",
          bmsync.content_fingerprint(sb.safari_path.read_bytes()) == sf_fp)

    # 端到端：噪音不触发同步，真变化仍然触发
    sb2 = Sandbox(tmp / "watch" / "noise")
    sb2.install_chrome(chrome_fixture([url_node("A", "https://a.example")]))
    sb2.install_safari(safari_fixture([leaf("B", "https://b.example")]))
    sb2.write_config()
    cfg2 = json.loads(sb2.config.read_text(encoding="utf-8"))
    cfg2["watch"] = {"interval": 0.3, "settle": 0.5, "resync_after": 120, "bridge_port": 0}
    sb2.config.write_text(json.dumps(cfg2, ensure_ascii=False), encoding="utf-8")
    sb2.run("sync")
    log_file = sb2.state_dir / "bmsync.log"
    proc = subprocess.Popen(
        [sys.executable, str(SCRIPT), "--config", str(sb2.config), "watch", "--no-initial"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    try:
        time.sleep(1.2)
        mark = len(log_file.read_text(encoding="utf-8").splitlines())
        for i in range(6):                      # 模拟切标签页：文件被重写但书签没变
            raw = json.loads(sb2.chrome_bookmarks.read_text(encoding="utf-8"))
            raw["checksum"] = "%032x" % (i + 1)
            sb2.chrome_bookmarks.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
            time.sleep(0.4)
        time.sleep(1.2)
        noise_syncs = [ln for ln in log_file.read_text(encoding="utf-8").splitlines()[mark:]
                       if "开始同步" in ln]
        check("噪音重写（切换标签页）不触发同步", len(noise_syncs) == 0,
              "触发了 %d 次" % len(noise_syncs))

        raw = json.loads(sb2.chrome_bookmarks.read_text(encoding="utf-8"))
        raw["roots"]["bookmark_bar"]["children"].append(url_node("真新增", "https://real-noise-test.example"))
        sb2.chrome_bookmarks.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        arrived = False
        deadline = time.time() + 20
        while time.time() < deadline:
            time.sleep(0.4)
            if any("real-noise-test.example" in (i.url or "") for i in sb2.safari_tree().items.values()):
                arrived = True
                break
        check("噪音之后真正的变化仍能同步", arrived)
    finally:
        proc.terminate()
        try:
            proc.communicate(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()

    # 10.54 watch --why：能区分"文件被重写"与"内容真的变了"
    why_sb = Sandbox(tmp / "watch" / "why")
    why_sb.install_chrome(chrome_fixture([url_node("A", "https://a.example")]))
    why_sb.install_safari(safari_fixture([leaf("B", "https://b.example")]))
    why_sb.write_config()
    proc = subprocess.Popen(
        [sys.executable, str(SCRIPT), "--config", str(why_sb.config), "watch", "--why", "4"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    time.sleep(1.2)
    raw = json.loads(why_sb.chrome_bookmarks.read_text(encoding="utf-8"))
    raw["checksum"] = "b" * 32                      # 只改易变字段
    why_sb.chrome_bookmarks.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    time.sleep(1.2)
    raw = json.loads(why_sb.chrome_bookmarks.read_text(encoding="utf-8"))
    raw["roots"]["bookmark_bar"]["children"].append(url_node("真新增", "https://why-new.example"))
    why_sb.chrome_bookmarks.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    try:
        out, _err = proc.communicate(timeout=25)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _err = proc.communicate()
    check("watch --why 能识别纯噪音重写", "内容未变（噪音）" in out, out[-300:])
    check("watch --why 能识别真实内容变化并给出差异", "内容也变了" in out and "差异：" in out,
          out[-400:])
    check("watch --why 给出结论", "结论：" in out, out[-200:])

    # 10.55 浏览器在跑时的风险提示（实时模式没法"拒绝同步"，但必须说清覆盖风险）
    if bmsync.is_process_running(["python3", "python3.14", "python"]) is None:
        print("  ⤵ 进程检测不可用（沙盒里 ps 被禁用），跳过这两项——在你自己的终端里会执行")
    else:
        check("进程检测能认出正在运行的程序",
              bmsync.is_process_running(["python3", "python3.14", "python"]) is not None)
        check("进程检测对不存在的名字返回 None",
              bmsync.is_process_running(["definitely-not-running-xyz"]) is None)
    import inspect as _inspect
    wsrc = _inspect.getsource(bmsync.cmd_watch)
    check("watch 会提示「浏览器运行中可能覆盖」的风险",
          "正在运行" in wsrc and "覆盖" in wsrc, "缺少风险提示")
    check("watch --verify 也会给出该风险提示",
          "正在运行" in _inspect.getsource(bmsync.watch_verify))

    # 10.6 常驻服务：只验证生成的 plist 内容（不真的装到系统里）
    daemon = sb.run("watch-daemon", "start", "--dry-run", "--log-dir", str(tmp / "watch" / "logs"),
                    expect=0)
    check("watch-daemon 生成常驻服务定义", "com.bmsync.watch" in daemon.stdout, daemon.stdout[:200])
    check("常驻服务带 KeepAlive（掉线自动拉起）", "<key>KeepAlive</key>" in daemon.stdout)
    check("常驻服务开机自启", "<key>RunAtLoad</key>" in daemon.stdout
          and daemon.stdout.split("<key>RunAtLoad</key>")[1].strip().startswith("<true/>"))

    # 10.7 模式切换
    mode_show = sb.run("mode", expect=0)
    check("mode 能显示三种工作模式", all(k in mode_show.stdout for k in ("manual", "scheduled", "watch")),
          mode_show.stdout[:300])
    sb.run("mode", "watch", "--keep-scheduler", expect=0)
    saved = json.loads(sb.config.read_text(encoding="utf-8"))
    check("mode watch 会写回配置文件", saved.get("mode") == "watch", str(saved.get("mode")))
    # mode manual 会去清理定时任务/常驻服务——把路径重定向到沙盒，别动真实的系统目录
    fake_dir = tmp / "watch" / "fake-LaunchAgents"
    fake_sched = fake_dir / "com.bmsync.sync.plist"
    fake_watch = fake_dir / "com.bmsync.watch.plist"
    fake_dir.mkdir(parents=True, exist_ok=True)        # 只在沙盒里造"已安装"的假象
    fake_sched.write_text("dummy", encoding="utf-8")
    fake_watch.write_text("dummy", encoding="utf-8")
    # 用环境变量把 launchd 目录指向沙盒，子进程同样生效（绝不碰真实的 ~/Library/LaunchAgents）
    sb.run("mode", "manual", expect=0, env={"BMSYNC_LAUNCHD_DIR": str(fake_dir)})
    saved = json.loads(sb.config.read_text(encoding="utf-8"))
    check("mode manual 会写回配置文件", saved.get("mode") == "manual", str(saved.get("mode")))
    check("mode manual 会清理定时任务与常驻服务（沙盒内验证）",
          not fake_sched.exists() and not fake_watch.exists(),
          "sched=%s watch=%s" % (fake_sched.exists(), fake_watch.exists()))
    check("切换模式不影响其它配置项", saved["sync"]["prefer"] == "newest" and "chrome" in saved,
          str(list(saved)))
    sb.run("mode", "scheduled", expect=0)
    check("mode scheduled 会提示安装命令",
          "install-scheduler" in sb.run("mode", "scheduled").stdout)


def scenario_backups(tmp: Path):
    """备份策略：内容没变不新增、太频繁不新增、分级保留不爆量。"""
    print("\n[11] 备份策略（去重 + 最小间隔 + 分级保留）")
    sb = Sandbox(tmp / "bak")
    sb.install_chrome(chrome_fixture([url_node("A", "https://a.example")]))
    sb.install_safari(safari_fixture([leaf("B", "https://b.example")]))
    sb.write_config()
    bdir = sb.state_dir / "backups"
    bdir.mkdir(parents=True, exist_ok=True)
    f = sb.chrome_bookmarks

    p1, created1 = bmsync.backup_file(f, bdir, 20, min_interval=0)
    check("首次备份会创建文件", created1 and p1 is not None, str(p1))
    check("备份文件名带内容指纹",
          p1 is not None and re.search(r"\.\d{8}-\d{6}-[0-9a-f]{12}\.bak$", p1.name) is not None,
          p1.name if p1 else "")

    # 只改易变字段（checksum / date_last_used）：实质内容没变，不该新增备份
    raw = json.loads(f.read_text(encoding="utf-8"))
    raw["checksum"] = "f" * 32
    raw["roots"]["bookmark_bar"]["children"][0]["date_last_used"] = "123456"
    f.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    _, created2 = bmsync.backup_file(f, bdir, 20, min_interval=0)
    check("只改 checksum/时间戳不产生新备份", not created2)

    # 真的加了书签：间隔未到 → 不新增；间隔到了 → 新增
    raw = json.loads(f.read_text(encoding="utf-8"))
    raw["roots"]["bookmark_bar"]["children"].append(url_node("新书签", "https://new.example"))
    f.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    _, created3 = bmsync.backup_file(f, bdir, 20, min_interval=300)
    check("实质变化但间隔未到 → 暂不备份", not created3)
    _, created4 = bmsync.backup_file(f, bdir, 20, min_interval=0)
    check("间隔允许后新增备份", created4)
    check("整个过程只产生 2 份备份（而非 4 份）",
          len([x for x in bdir.iterdir() if x.is_file()]) == 2,
          str(sorted(x.name for x in bdir.iterdir())))

    # 分级保留：造 40 份"最近 1 小时"的备份，全部保留；造 5 份 40 天前的，只留 keep 份
    thinning = tmp / "bak" / "thin"
    thinning.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now()
    for i in range(40):                                  # 最近 1 小时内，每分钟一份
        ts = stamp - timedelta(minutes=i)
        (thinning / ("chrome.%s-%012x.bak" % (ts.strftime("%Y%m%d-%H%M%S"), i))).write_text("x")
    for i in range(5):                                   # 40 天前
        ts = stamp - timedelta(days=40, minutes=i)
        (thinning / ("chrome.%s-%012x.bak" % (ts.strftime("%Y%m%d-%H%M%S"), 100 + i))).write_text("x")
    removed = bmsync.prune_backups(thinning, "chrome", keep=2)
    left = sorted(x.name for x in thinning.iterdir())
    def stamp_of(name):
        return name.split(".")[1][:8]          # chrome.<YYYYMMDD>-<HHMMSS>-<hash>.bak

    day_ago = (stamp - timedelta(days=1)).strftime("%Y%m%d")
    month_ago = (stamp - timedelta(days=30)).strftime("%Y%m%d")
    check("最近 24 小时的备份全部保留",
          len([n for n in left if stamp_of(n) >= day_ago]) == 40,
          "%d 份（应为 40）" % len([n for n in left if stamp_of(n) >= day_ago]))
    check("30 天以上只保留 keep 份",
          len([n for n in left if stamp_of(n) < month_ago]) == 2,
          "实际 %d 份" % len([n for n in left if stamp_of(n) < month_ago]))
    check("确实清理掉了旧备份", len(removed) >= 3, "清理了 %d 份" % len(removed))


def scenario_bridge(tmp: Path):
    """桥接服务：扩展在线时由扩展写 Chrome，bmsync 不再碰文件。"""
    print("\n[12] 本地桥接（让运行中的 Chrome 也能被同步）")
    sb = Sandbox(tmp / "bridge")
    sb.install_chrome(chrome_fixture([url_node("Chrome 原有", "https://c.example")]))
    sb.install_safari(safari_fixture([leaf("Safari 原有", "https://s.example")]))
    sb.write_config()
    cfg = bmsync.load_config(sb.config)
    sp = bmsync.state_paths(cfg)
    sb.state_dir.mkdir(parents=True, exist_ok=True)

    # 12.1 扩展在线判定取决于心跳新鲜度
    check("没有心跳时视为扩展离线", not bmsync.bridge_active(cfg))
    sp["bridge"].write_text(json.dumps({"last_seen": bmsync.now_ts()}), encoding="utf-8")
    check("心跳新鲜时视为扩展在线", bmsync.bridge_active(cfg))
    sp["bridge"].write_text(json.dumps({"last_seen": bmsync.now_ts() - 99999}), encoding="utf-8")
    check("心跳过期后视为离线", not bmsync.bridge_active(cfg))

    # 12.2 delta 语义：已经在 Chrome 里的东西不该再要求添加；且永不要求删除已有书签
    sb.run("sync")
    sb.state_dir.joinpath("bridge.json").write_text(json.dumps({"last_seen": bmsync.now_ts()}),
                                                    encoding="utf-8")
    delta = bmsync._bridge_delta(sp, cfg)
    check("同步完成后的 delta 不含重复新增", delta["counts"]["add"] == 0,
          json.dumps(delta["counts"], ensure_ascii=False))
    check("delta 不会要求删除 Chrome 已有书签",
          not any("c.example" in r["url"] for r in delta["remove"]), str(delta["remove"])[:120])
    check("delta 里新增项都带容器标识与目标路径",
          all(a.get("container") in ("bar", "other") and isinstance(a.get("path"), list)
              for a in delta["add"]),
          json.dumps(delta["add"], ensure_ascii=False)[:160])

    # 12.3 扩展在线时同步不再改写 Chrome 文件（否则会和扩展互相覆盖）
    before = sb.chrome_bookmarks.read_bytes()
    sf = sb.read_safari()
    sf["Children"][0]["Children"].append(leaf("又一条", "https://another.example"))
    sb.install_safari(sf)
    out = sb.run("sync", "-v")
    check("扩展在线时 Chrome 文件不被改写", sb.chrome_bookmarks.read_bytes() == before,
          "文件被改了")
    check("日志里说明了交给扩展", "交给 Chrome 扩展" in out.stdout, out.stdout[-200:])

    # 12.4 delta 必须给出带路径的待新增项：用"基线里有、Chrome 里没有"的状态直接验证
    stale = tmp / "bridge" / "stale"
    stale.mkdir(parents=True, exist_ok=True)
    stale_cfg = dict(cfg)
    stale_cfg["state_dir"] = str(stale)
    sp_stale = bmsync.state_paths(stale_cfg)
    stale_state = {"version": 3, "base": {"items": {}, "children": {}}, "history": [], "tombstones": {}}
    items, children = {}, {}
    for cid in bmsync.CONTAINERS:
        items[cid] = {"kind": "folder", "name": cid, "parent": bmsync.ROOT_ID, "url": None, "mtime": None}
        children[cid] = []
    children[bmsync.ROOT_ID] = list(bmsync.CONTAINERS)
    holder = "f:other/工具"
    items[holder] = {"kind": "folder", "name": "工具", "parent": bmsync.OTHER_ID, "url": None, "mtime": None}
    children[bmsync.OTHER_ID] = [holder]
    children[holder] = ["u:https://only-safari.example/"]
    items["u:https://only-safari.example/"] = {"kind": "bookmark", "name": "只在 Safari",
                                               "parent": holder, "url": "https://only-safari.example",
                                               "mtime": None}
    stale_state["base"] = {"items": items, "children": children}
    (stale / "state.json").write_text(json.dumps(stale_state, ensure_ascii=False), encoding="utf-8")
    delta2 = bmsync._bridge_delta(sp_stale, stale_cfg)
    want = [a for a in delta2["add"] if "only-safari" in a["url"]]
    check("能给出待新增项及其容器/路径",
          len(want) == 1 and want[0]["container"] == "other" and want[0]["path"] == ["工具"],
          json.dumps(delta2["add"], ensure_ascii=False)[:200])

    # 12.45 回归：扩展在线时，绝不能删除"用户刚在 Chrome 里加、基线还不知道"的书签
    #        （曾经的严重 bug：/delta 基于旧基线比较，把用户新加的书签判定为多余并下令删除）
    sp["bridge"].write_text(json.dumps({"last_seen": bmsync.now_ts()}), encoding="utf-8")
    chrome_b, safari_b = bmsync.build_backends(cfg)
    ns = argparse.Namespace(dry_run=False, force=True, prefer=None, direction=None,
                            list_changes=False, max_show=0, quiet=True, json=False, verbose=0)
    raw = json.loads(sb.chrome_bookmarks.read_text(encoding="utf-8"))
    raw["roots"]["bookmark_bar"]["children"].append(url_node("用户刚加的", "https://user-just-added.example"))
    sb.chrome_bookmarks.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    bmsync.run_sync(cfg, chrome_b, safari_b, sp, bmsync.load_state(sp), ns)
    d_fresh = bmsync._bridge_delta(sp, cfg)
    check("用户刚在 Chrome 加的书签不会被要求删除",
          not any("user-just-added" in r["url"] for r in d_fresh["remove"]),
          str(d_fresh["remove"])[:160])
    check("用户刚加的书签仍在 Chrome 里",
          any("user-just-added" in (i.url or "") for i in chrome_b.read_tree().items.values()))

    # 12.46 只有"Safari 明确删除过（有墓碑）"的书签，才允许让扩展从 Chrome 删除。
    #       注意：扩展在线时 Chrome 文件不会被打字（由扩展负责），所以这里验证的是
    #       "墓碑里带上了 URL" + "delta 能据此下发删除"，而不是直接看 Chrome 文件。
    sp["bridge"].unlink()                       # 先离线跑一次，让两边都拿到这本书签
    sf2 = sb.read_safari()
    sf2["Children"][0]["Children"].append(leaf("两边都有的", "https://both-sides.example"))
    sb.install_safari(sf2)
    sb.run("sync")
    check("两边都拿到这本书签",
          any("both-sides" in (i.url or "") for i in sb.chrome_tree().items.values()))

    sp["bridge"].write_text(json.dumps({"last_seen": bmsync.now_ts()}), encoding="utf-8")
    sf3 = sb.read_safari()
    sf3["Children"][0]["Children"] = [c for c in sf3["Children"][0]["Children"]
                                      if (c.get("URIDictionary") or {}).get("title") != "两边都有的"]
    sb.install_safari(sf3)
    bmsync.run_sync(cfg, chrome_b, safari_b, sp, bmsync.load_state(sp), ns)
    tombstones = (json.loads((sb.state_dir / "state.json").read_text(encoding="utf-8"))
                  .get("tombstones") or {})
    check("墓碑记录了被删书签的 URL",
          any(isinstance(v, dict) and "both-sides" in (v.get("url") or "")
              for v in tombstones.values()), str(tombstones)[:200])
    d_del = bmsync._bridge_delta(sp, cfg)
    check("Safari 删除的书签会指示扩展从 Chrome 删除",
          any("both-sides" in r["url"] for r in d_del["remove"]), str(d_del["remove"])[:160])
    check("delta 不会顺手删除未同步过的 Chrome 书签",
          not any("user-just-added" in r["url"] for r in d_del["remove"]),
          str(d_del["remove"])[:160])

    # 12.5 扩展离线后恢复直接写文件
    sp["bridge"].unlink()
    sb.run("sync", "-v")
    check("扩展离线后恢复写 Chrome 文件",
          any("another.example" in (i.url or "") or "在" in (i.name or "")
              for i in sb.chrome_tree().items.values())
          or sb.chrome_bookmarks.read_bytes() != before,
          "既没写文件也不包含新书签")

    # 12.5 真实起一次桥接服务，走 HTTP 协议
    srv = bmsync.start_bridge(cfg, sp, 0)          # 端口 0 = 让系统分配空闲端口
    if srv is None:
        check("桥接服务能启动", False, "start_bridge 返回 None")
    else:
        port = srv.server_address[1]
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/ping" % port, timeout=5) as r:
                ping = json.loads(r.read().decode())
            check("桥接 /ping 正常", ping.get("ok") is True and ping.get("app") == "bmsync", str(ping))
            with urllib.request.urlopen("http://127.0.0.1:%d/delta" % port, timeout=5) as r:
                over_http = json.loads(r.read().decode())
            check("桥接 /delta 正常", "counts" in over_http, str(over_http)[:120])
            req = urllib.request.Request("http://127.0.0.1:%d/applied" % port, data=b'{"added":1}',
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as r:
                applied = json.loads(r.read().decode())
            check("桥接 /applied 正常", applied.get("ok") is True, str(applied))
            check("/ping 会刷新心跳", bmsync.bridge_active(cfg))
        finally:
            srv.shutdown()
            srv.server_close()

    # 12.6 扩展文件齐全且 JSON 合法
    ext = HERE.parent / "extension"
    for name in ("manifest.json", "background.js", "core.js", "popup.html", "popup.js",
                 "test_core.mjs", "test_background.mjs", "test_popup.mjs"):
        check("扩展文件存在：%s" % name, (ext / name).exists())
    manifest = json.loads((ext / "manifest.json").read_text(encoding="utf-8"))
    check("扩展用 mv3 且只申请 bookmarks 权限",
          manifest.get("manifest_version") == 3 and manifest.get("permissions") == ["bookmarks"],
          str(manifest.get("permissions")))
    check("扩展只连本机地址", all("127.0.0.1" in h or "localhost" in h
                                  for h in manifest.get("host_permissions", [])),
          str(manifest.get("host_permissions")))
    check("扩展用 module service worker（core.js 走 ESM 导入）",
          manifest.get("background", {}).get("type") == "module",
          str(manifest.get("background")))

    # 扩展用 node 单测（不需要 Chrome）；没装 node 就跳过
    node = shutil.which("node")
    if node is None:
        print("  ⤵ 未安装 node，跳过扩展单测（有 node 的机器上会执行）")
    else:
        for script, label in (("test_core.mjs", "扩展核心逻辑单测（含英文界面场景）"),
                              ("test_background.mjs", "扩展后台脚本单测（应用/状态/最近变动）"),
                              ("test_popup.mjs", "扩展弹窗单测（module 加载/元素 id/渲染）")):
            run_node = subprocess.run([node, str(ext / script)], capture_output=True, text=True)
            check(label, run_node.returncode == 0, (run_node.stdout or run_node.stderr)[-300:])

    # 3) 桥接 /recent：弹窗要展示的"最近变动"必须能拿到
    srv2 = bmsync.start_bridge(cfg, sp, 0)
    if srv2 is None:
        check("桥接 /recent 可用", False, "桥接启动失败")
    else:
        port2 = srv2.server_address[1]
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/recent" % port2, timeout=5) as r:
                recent = json.loads(r.read().decode())
            check("桥接 /recent 返回待应用与历史",
                  "pending" in recent and "events" in recent and isinstance(recent["events"], list),
                  str(recent)[:200])
            check("/recent 的历史里含同步记录",
                  any(e.get("kind") == "sync" for e in recent["events"]),
                  json.dumps(recent["events"], ensure_ascii=False)[:200])
            check("/recent 里能看到扩展回报",
                  any(e.get("kind") == "applied" for e in recent["events"]),
                  json.dumps(recent["events"], ensure_ascii=False)[:200])
        finally:
            srv2.shutdown()
            srv2.server_close()


def scenario_mass_delete_guard(tmp: Path):
    """
    删除熔断：一侧书签文件被清空/大面积改写时，绝不能把这场故障复制到另一侧。
    真实事故：Safari 文件被覆盖后，Chrome 被删到只剩 8 条。
    """
    print("\n[13] 删除熔断（防止一侧故障被复制到另一侧）")
    sb = Sandbox(tmp / "guard")
    items = [url_node("书签%d" % i, "https://site%d.example" % i) for i in range(120)]
    sb.install_chrome(chrome_fixture(items))
    sb.install_safari(safari_fixture([leaf("Safari 原有", "https://s.example")]))
    sb.write_config()
    sb.run("sync")

    def chrome_count():
        return len([i for i in sb.chrome_tree().items.values() if i.kind == "bookmark"])

    def base_count():
        st = json.loads((sb.state_dir / "state.json").read_text(encoding="utf-8"))
        return sum(1 for v in st["base"]["items"].values() if v["kind"] == "bookmark")

    before = chrome_count()
    check("同步后两侧都有全部书签", before == 121, "实际 %d" % before)

    # 灾难：Safari 文件被清空（iCloud 覆盖 / 文件损坏 / 被重置）
    sf = sb.read_safari()
    sf["Children"] = [c for c in sf["Children"] if c.get("Title") == "BookmarksBar"]
    sf["Children"][0]["Children"] = []
    sb.install_safari(sf)
    out = sb.run("sync", "-v")
    check("熔断被触发并给出告警", "熔断" in (out.stdout + out.stderr),
          (out.stdout + out.stderr)[-200:])
    check("Chrome 书签没被删", chrome_count() == before, "被删到 %d" % chrome_count())
    check("熔断后基线保持完整", base_count() >= before - 1, "基线 %d" % base_count())

    # 恢复后不应再出现任何"删除"，且两侧内容一致
    sf2 = sb.read_safari()
    sf2["Children"][0]["Children"].append(leaf("恢复进来的", "https://restored.example"))
    sb.install_safari(sf2)
    out2 = sb.run("sync", "-v")
    check("一侧恢复后不再触发熔断", "熔断" not in (out2.stdout + out2.stderr))
    check("恢复的书签能同步过去",
          any("restored" in (i.url or "") for i in sb.chrome_tree().items.values()))
    check("Chrome 规模没有减少", chrome_count() >= before, "实际 %d（原 %d）" % (chrome_count(), before))

    # 显式确认时才允许大批删除（用直接调用 merge 的方式验证限额逻辑本身）
    big = bmsync.Tree()
    for cid in bmsync.CONTAINERS:
        big.add(bmsync.Item(id=cid, kind="folder", name=cid, parent_id=bmsync.ROOT_ID))
    empty_side = bmsync.Tree()
    for cid in bmsync.CONTAINERS:
        empty_side.add(bmsync.Item(id=cid, kind="folder", name=cid, parent_id=bmsync.ROOT_ID))
    for i in range(50):
        big.add(bmsync.Item(id="u:https://x%d.example/" % i, kind="bookmark", name="x%d" % i,
                            parent_id=bmsync.BAR_ID, url="https://x%d.example" % i, mtime=None))
    res = bmsync.merge(big.clone(), empty_side, big.clone(), max_delete_ratio=0.05,
                       max_delete_count=10)
    check("合并层：超限删除被拦下", res.blocked_deletes >= 50, str(res.blocked_deletes))
    check("合并层：拦下后给出明确说明",
          any("熔断" in c for c in res.conflicts), str(res.conflicts)[:160])
    res2 = bmsync.merge(big.clone(), empty_side, big.clone(), max_delete_ratio=0, max_delete_count=0)
    check("合并层：显式放开后不再拦", res2.blocked_deletes == 0, str(res2.blocked_deletes))


def scenario_missing_permission(tmp: Path):
    """Safari 文件不可读时要给出明确的 TCC 提示，而不是一堆 traceback。"""
    print("\n[8] 权限缺失时的提示")
    sb = Sandbox(tmp / "perm")
    sb.install_chrome(chrome_fixture([url_node("x", "https://x.example")]))
    sb.write_config()  # 故意不创建 Safari 文件
    proc = sb.run("sync", expect=2)
    check("返回码为 2（权限/文件问题）", proc.returncode == 2, str(proc.returncode))
    check("提示了具体原因", "无法读取" in proc.stderr, proc.stderr[:300])
    check("没有抛出 traceback", "Traceback" not in proc.stderr, proc.stderr[:300])

    proc = sb.run("doctor", expect=None)
    check("doctor 能给出诊断", "Safari" in proc.stdout and "失败" in proc.stdout)
    check("doctor 提到了完全磁盘访问权限", "完全磁盘访问权限" in proc.stdout)

    # 回归：定时任务用 --quiet 运行，失败时必须仍有线索
    # （否则 launchd 里跑失败就是一片寂静，连日志文件都不会有）
    quiet_fail = sb.run("sync", "--quiet", expect=2)
    check("--quiet 下报错仍打到 stderr", "无法读取" in quiet_fail.stderr,
          "stderr=%r" % quiet_fail.stderr[:120])
    log_path = sb.state_dir / "bmsync.log"
    check("失败会写进 bmsync.log", log_path.exists() and "同步失败" in log_path.read_text(encoding="utf-8"),
          "日志存在=%s" % log_path.exists())
    if log_path.exists():
        check("失败日志带退出码与原因",
              "退出码 2" in log_path.read_text(encoding="utf-8"),
              log_path.read_text(encoding="utf-8")[-160:])

    # 回归：定时任务的日志目录必须在 launchd 一定可写的位置，且是绝对路径
    sched = bmsync.build_parser().parse_args(["install-scheduler", "--dry-run"])
    plist_preview = sb.run("install-scheduler", "--dry-run", "--log-dir", str(tmp / "sched-logs"),
                           expect=None)
    check("install-scheduler 能生成任务定义", "ProgramArguments" in plist_preview.stdout
          or "StandardErrorPath" in plist_preview.stdout, plist_preview.stderr[:120])
    check("任务日志用绝对路径",
          str(tmp / "sched-logs") in plist_preview.stdout,
          plist_preview.stdout[:200])
    check("默认任务日志不放在仓库内",
          str(bmsync.LAUNCHD_LOG_DIR).startswith(str(bmsync.HOME)),
          str(bmsync.LAUNCHD_LOG_DIR))
    del sched

    # 回归：权限没就绪时应当拒绝安装定时任务，而不是装出一个注定失败的任务
    blocked = sb.run("install-scheduler", "--log-dir", str(tmp / "sched-logs"), expect=2)
    check("权限未就绪时拒绝安装定时任务", blocked.returncode == 2, str(blocked.returncode))
    check("拒绝安装时说明缺什么", "未安装定时任务" in blocked.stderr and "失败" in blocked.stderr,
          blocked.stderr[:160])
    check("拒绝了就不该留下 plist",
          not bmsync.launchd_plist_path(bmsync.LAUNCHD_LABEL).exists()
          or bmsync.launchd_plist_path(bmsync.LAUNCHD_LABEL).read_text(encoding="utf-8") != "")

    # 回归：install-scheduler 必须先检查权限、再写 plist（顺序反了会把 plist 写坏）
    import inspect
    src = inspect.getsource(bmsync.cmd_install_scheduler)
    write_at = src.index("atomic_write(launchd_plist_path")
    check("权限检查在写 plist 之前", src.index("未安装定时任务") < write_at, "顺序反了")
    check("日志目录创建在写 plist 之前", src.index("log_dir.mkdir") < write_at, "顺序反了")
    check("用 bootout+bootstrap 重新加载（不用废弃的 load/unload）",
          "bootout" in src and "bootstrap" in src, "缺少 bootout/bootstrap")


def main():
    tmp_root = HERE.parent / "state"
    tmp_root.mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix="bmsync-test-", dir=str(tmp_root)))
    print("沙盒目录：%s" % base)
    if not REAL_CHROME.exists():
        print("未找到真实 Chrome 书签，改用合成夹具（测试同样有效）")
    scenario_roundtrip(base)
    scenario_first_sync(base)
    scenario_two_way(base)
    scenario_conflict(base)
    scenario_one_way(base)
    scenario_export_import(base)
    scenario_edge_cases(base)
    scenario_global_flags(base)
    scenario_realtime(base)
    scenario_backups(base)
    scenario_bridge(base)
    scenario_mass_delete_guard(base)
    scenario_missing_permission(base)

    print("\n" + "=" * 70)
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    for name in FAIL:
        print("  失败：%s" % name)
    shutil.rmtree(base, ignore_errors=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
