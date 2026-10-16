#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bmsync 自测：在临时沙盒里用真实的 Chrome 书签文件 + 合成的 Safari 书签做端到端验证。

用真实的 Chrome Bookmarks 有两个好处：
  1. 覆盖真实的字段、时间戳、特殊书签（chrome://、file://、javascript: 等）；
  2. 能验证"读进来再写回去"不会破坏 Chrome 的书签库（无损往返）。

用法：/usr/bin/python3 tests/test_bmsync.py
"""

import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
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

    def run(self, *args, expect=0):
        argv = [str(a) for a in args]
        globals_, rest = [], []
        for a in argv:  # 只有真正的全局开关需要提到子命令之前
            (globals_ if a in self.GLOBAL_FLAGS else rest).append(a)
        cmd = [sys.executable, str(SCRIPT), "--config", str(self.config)] + globals_ + rest
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
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
    scenario_missing_permission(base)

    print("\n" + "=" * 70)
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    for name in FAIL:
        print("  失败：%s" % name)
    shutil.rmtree(base, ignore_errors=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
