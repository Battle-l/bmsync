#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bmsync —— Chrome / Safari 书签双向同步工具（macOS，仅依赖 Python 标准库）

思路：
  1. 直接读取两个浏览器的书签库：Chrome 用 Bookmarks(JSON)，Safari 用 Bookmarks.plist。
  2. 与"上次同步后的基线"做三方比对，得出各自的新增 / 修改 / 删除。
  3. 合并成一份统一结果（冲突按修改时间或 --prefer 判定），再分别写回两个浏览器。
  4. 状态记录在 state/ 目录，所以第二次同步只处理增量，不会重复导入、也不会反复改写。
  5. 顺序不参与同步（两个浏览器排序机制不同），只保证内容与层级一致。

常用命令：
  bmsync doctor                 # 自检：权限、路径、浏览器是否在运行
  bmsync status                 # 查看两边数量与上次同步情况
  bmsync sync --dry-run -v      # 演练：只打印将要做的改动，不落盘
  bmsync sync                   # 真正同步（自动备份两边原文件到 state/backups/）
  bmsync sync --list-changes -v # 显示每一条改动
  bmsync export --format html   # 导出可手动导入的书签文件
  bmsync install-scheduler      # 安装 launchd 定时任务（默认每 60 分钟）

注意：
  * Safari 的 Bookmarks.plist 受 macOS TCC 保护，运行本脚本的进程需要"完全磁盘访问权限"。
  * 写文件前请退出对应浏览器，否则浏览器可能用内存中的旧数据覆盖改动。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import uuid
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime
from html import escape as html_escape
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import unquote, urlsplit, urlunsplit

__version__ = "1.0.0"

# --------------------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------------------

APP_DIR = Path(__file__).resolve().parent
HOME = Path.home()

# Chrome / Chromium 系浏览器（用于自检提示与进程检测）
CHROMIUM_PROCS = ["Google Chrome", "Google Chrome Beta", "Google Chrome Canary", "Chromium",
                  "Microsoft Edge", "Brave Browser", "Vivaldi", "Arc"]

SAFARI_BOOKMARK_PATHS = [
    "Library/Safari/Bookmarks.plist",
    "Library/Containers/com.apple.Safari/Data/Library/Safari/Bookmarks.plist",
]

CHROME_EPOCH_OFFSET_S = 11644473600.0   # 1601-01-01 → 1970-01-01
MAC_EPOCH_OFFSET_S = 978307200.0        # 2001-01-01 → 1970-01-01

# 默认不参与同步的容器（Safari 自己管理的特殊目录，如阅读列表、历史记录等）
DEFAULT_SKIP_FOLDERS = [
    "com.apple.ReadingList", "Reading List", "阅读列表",
    "com.apple.WebBookmarkSpecialID.ReadingList",
]
MOBILE_FOLDER_NAME = "移动设备书签"

STATE_VERSION = 3
TOMBSTONE_TTL = 180 * 86400

# 内部容器 id
ROOT_ID = "root"
BAR_ID = "bar"        # Chrome 书签栏 / Safari 个人收藏
OTHER_ID = "other"    # Chrome 其他书签 / Safari 书签菜单
MOBILE_ID = "mobile"  # 仅作为 Chrome synced 的读写锚点，不作为统一树里的容器
CONTAINERS = (BAR_ID, OTHER_ID)
CONTAINER_LABELS = {BAR_ID: "书签栏", OTHER_ID: "其他书签"}

# Chrome 顶层容器名
CHROME_BAR_NAME = "Bookmarks bar"
CHROME_OTHER_NAME = "Other bookmarks"
CHROME_MOBILE_NAME = "Mobile bookmarks"
# Safari 顶层容器名
SAFARI_BAR_TITLES = ["BookmarksBar", "Bookmarks Bar", "个人收藏", "喜好项目"]
SAFARI_MENU_TITLES = ["BookmarksMenu", "Bookmarks Menu", "书签菜单"]


# --------------------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------------------


def eprint(*args: Any) -> None:
    print(*args, file=sys.stderr)


def log(msg: str, verbosity: int, need: int = 1) -> None:
    if verbosity >= need:
        print(msg, flush=True)


def now_ts() -> float:
    return time.time()


def fmt_ts(ts: Optional[float]) -> str:
    if ts is None:
        return "-"
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return "-"


def atomic_write(path: Path, data: bytes, keep_mode_from: Optional[Path] = None) -> None:
    """先写临时文件再 rename，避免中途失败写坏原文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("%s.bmsync-tmp-%d" % (path.name, os.getpid()))
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    if keep_mode_from is not None and keep_mode_from.exists():
        try:
            shutil.copymode(keep_mode_from, tmp)
        except OSError:
            pass
    os.replace(tmp, path)


def json_dumps_compact(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str)


def mid_digest(obj: Any) -> str:
    """用于比较"结构是否真的变了"的摘要（对 plist/JSON 都适用）。"""
    return hashlib.sha1(json_dumps_compact(_plain(obj)).encode("utf-8")).hexdigest()


def _plain(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    if isinstance(obj, (bytes, bytearray)):
        return {"__bytes__": len(obj)}
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


_MULTI_SPACE = re.compile(r"\s+")


def norm_name(name: Any) -> str:
    if name is None:
        return ""
    return _MULTI_SPACE.sub(" ", unicodedata.normalize("NFC", str(name).strip()))


def norm_url(url: Any) -> str:
    """规范化 URL，仅用于判定"是不是同一个书签"，不用于写盘。

    规则：小写 scheme/host、去默认端口、去末尾斜杠（根路径除外）、百分号解码、
    丢弃 fragment。注意根路径必须保留成 "/"，否则 "https://a.com" 会被规范化成空串，
    导致所有无路径的 URL 被当成同一个书签。
    """
    if url is None:
        return ""
    s = unicodedata.normalize("NFC", str(url).strip())
    if not s:
        return ""
    try:
        parts = urlsplit(s)
    except ValueError:
        return s
    if not parts.scheme and not parts.netloc:
        return s.rstrip("/")
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    if (scheme == "http" and netloc.endswith(":80")) or (scheme == "https" and netloc.endswith(":443")):
        netloc = netloc.rsplit(":", 1)[0]
    path = unquote(parts.path)
    if not path:
        path = "/"
    elif path != "/":
        path = path.rstrip("/") or "/"
    return urlunsplit((scheme, netloc, path, unquote(parts.query), "")) or s


def clean_url_for_write(url: Any) -> str:
    """写盘时的最小规整：去换行/空白、空格转义、http(s) 无路径时补斜杠。"""
    s = str(url or "").strip().replace("\n", "").replace("\r", "").replace("\t", "")
    s = s.replace(" ", "%20")
    try:
        parts = urlsplit(s)
    except ValueError:
        return s
    if parts.scheme in ("http", "https") and not parts.path:
        return urlunsplit((parts.scheme, parts.netloc, "/", parts.query, parts.fragment))
    return s


def safe_filename(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:80] or "bookmarks"


def chrome_ts_to_epoch(v: Any) -> Optional[float]:
    if v in (None, ""):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f <= 0:
        return None
    if f > 1e15:
        f /= 1e6
    elif f > 1e12:
        f /= 1e3
    return f - CHROME_EPOCH_OFFSET_S


def epoch_to_chrome_ts(ts: Optional[float]) -> str:
    base = ts if ts is not None else now_ts()
    return str(int(round((base + CHROME_EPOCH_OFFSET_S) * 1_000_000)))


def safari_ts_to_epoch(v: Any) -> Optional[float]:
    if v in (None, ""):
        return None
    if isinstance(v, datetime):
        return v.timestamp()
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f <= 0:
        return None
    if f > 1e11:  # 已经是 POSIX 时间
        return f
    return f + MAC_EPOCH_OFFSET_S


def epoch_to_safari_ts(ts: Optional[float]) -> float:
    base = ts if ts is not None else now_ts()
    return base - MAC_EPOCH_OFFSET_S


# --------------------------------------------------------------------------------------
# 统一书签树
# --------------------------------------------------------------------------------------


@dataclass
class Item:
    id: str
    kind: str                        # 'folder' | 'bookmark'
    name: str
    parent_id: Optional[str] = None
    url: Optional[str] = None
    mtime: Optional[float] = None    # POSIX 秒

    def fingerprint(self) -> Tuple[Any, ...]:
        """判断"内容/位置有没有变"（不含顺序、不含时间）。"""
        return (self.kind, norm_name(self.name),
                norm_url(self.url) if self.kind == "bookmark" else None,
                self.parent_id)

    def to_state(self) -> Dict[str, Any]:
        return {"kind": self.kind, "name": self.name, "parent": self.parent_id,
                "url": self.url, "mtime": self.mtime}

    @staticmethod
    def from_state(item_id: str, d: Dict[str, Any]) -> "Item":
        return Item(id=item_id, kind=d.get("kind", "bookmark"), name=d.get("name", ""),
                    parent_id=d.get("parent"), url=d.get("url"), mtime=d.get("mtime"))


class Tree:
    """书签树的逻辑视图：items[id] -> Item，children[parent_id] -> 有序 id 列表。"""

    def __init__(self) -> None:
        self.items: Dict[str, Item] = {}
        self.children: Dict[str, List[str]] = {}

    # ---- 构造 ----
    def add(self, item: Item, order: Optional[int] = None, unique: bool = False) -> Item:
        if unique:
            item = replace(item, id=self.make_id(item.parent_id, item.kind, item.name, item.url))
        self.items[item.id] = item
        bucket = self.children.setdefault(item.parent_id or ROOT_ID, [])
        if item.id in bucket:
            bucket.remove(item.id)
        if order is None:
            bucket.append(item.id)
        else:
            bucket.insert(max(0, min(order, len(bucket))), item.id)
        self.children.setdefault(item.id, [])
        return item

    @staticmethod
    def base_id(parent_id: Optional[str], kind: str, name: str, url: Optional[str]) -> str:
        """节点的主 id（不含去重后缀）。约定：书签按 URL，目录按 父/名。"""
        if kind == "bookmark":
            return "u:" + (norm_url(url) or ("name:" + norm_name(name)))
        return "f:%s/%s" % (parent_id or "", norm_name(name))

    def find_id(self, parent_id: Optional[str], kind: str, name: str, url: Optional[str]) -> Optional[str]:
        """查找已存在节点的 id（不做新建）。返回 None 表示树里没有。"""
        base = self.base_id(parent_id, kind, name, url)
        if base in self.items:
            return base
        # 注意不要用 make_id 来找：它是"分配新 id"的语义，会跳过已存在的
        second = "%s#2" % base
        return second if second in self.items else None

    def make_id(self, parent_id: Optional[str], kind: str, name: str, url: Optional[str]) -> str:
        """分配一个未被占用的新 id。"""
        base = self.base_id(parent_id, kind, name, url)
        cand, i = base, 2
        while cand in self.items:
            cand = "%s#%d" % (base, i)
            i += 1
        return cand

    # ---- 查询 ----
    def folders(self) -> List[str]:
        return [i for i, it in self.items.items() if it.kind == "folder"]

    def bookmarks(self) -> List[str]:
        return [i for i, it in self.items.items() if it.kind == "bookmark"]

    def path_ids(self, item_id: str) -> List[str]:
        out: List[str] = []
        seen = set()
        cur: Optional[str] = item_id
        while cur and cur in self.items and cur not in seen:
            seen.add(cur)
            out.append(cur)
            cur = self.items[cur].parent_id
        return list(reversed(out))

    def path_names(self, item_id: str) -> List[str]:
        return [self.items[i].name for i in self.path_ids(item_id)]

    def display_path(self, item_id: str) -> str:
        names = self.path_names(item_id)
        if names and names[0] in ("bar", "other", "mobile"):
            names = [CONTAINER_LABELS.get(names[0], names[0])] + names[1:]
        return "/".join(names)

    def label(self, item_id: str) -> str:
        it = self.items.get(item_id)
        if it is None:
            return "<%s>" % item_id
        if it.kind == "bookmark":
            return "%s → %s" % (it.name, it.url)
        return "[目录] %s" % (self.display_path(item_id) or it.name)

    def children_of(self, folder_id: Optional[str]) -> List[str]:
        return list(self.children.get(folder_id if folder_id else ROOT_ID, []))

    def subtree_ids(self, folder_id: str) -> List[str]:
        out: List[str] = []
        stack = [folder_id]
        seen = set()
        while stack:
            cur = stack.pop()
            if cur in seen or cur not in self.items:
                continue
            seen.add(cur)
            out.append(cur)
            if self.items[cur].kind == "folder":
                stack.extend(reversed(self.children_of(cur)))
        return out

    def sort_key(self, item_id: str) -> Tuple[Any, ...]:
        it = self.items.get(item_id)
        if it is None:
            return (2, item_id, "")
        return (0 if it.kind == "folder" else 1,
                norm_name(it.name).casefold(), norm_url(it.url or ""))

    def stats(self) -> Dict[str, int]:
        c = Counter(it.kind for it in self.items.values())
        return {"bookmarks": c.get("bookmark", 0), "folders": c.get("folder", 0)}

    def clone(self) -> "Tree":
        t = Tree()
        t.items = {k: replace(v) for k, v in self.items.items()}
        t.children = {k: list(v) for k, v in self.children.items()}
        return t


# --------------------------------------------------------------------------------------
# 中间格式：与浏览器无关的一棵树（保留 Safari 原始字段以便无损写回）
# --------------------------------------------------------------------------------------


@dataclass
class Mid:
    kind: str
    name: str
    url: Optional[str] = None
    mtime: Optional[float] = None
    children: List["Mid"] = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SkipRules:
    """要跳过的容器规则。默认见 DEFAULT_SKIP_FOLDERS，可通过 config.sync.skip_folders 覆盖。

    匹配范围：Safari 的 WebBookmarkSpecialID（如 com.apple.WebBookmarkSpecialID.ReadingList）、
    容器标题，以及以 '.' 开头的容器。
    """

    patterns: Tuple[str, ...] = ()

    @staticmethod
    def from_config(values: Optional[Sequence[str]]) -> "SkipRules":
        if values is None:
            values = DEFAULT_SKIP_FOLDERS
        return SkipRules(tuple(norm_name(v).casefold() for v in values if str(v).strip()))

    def matches(self, name: Any = None, special_id: Any = None, extra: Sequence[Any] = ()) -> bool:
        if not self.patterns:
            return False
        candidates = [norm_name(v).casefold() for v in
                      [name, special_id] + [e for e in extra] if isinstance(v, str) and v.strip()]
        for cand in candidates:
            if cand.startswith("."):  # 隐藏容器
                return True
            for pat in self.patterns:
                if cand == pat or cand.startswith(pat + ".") or pat in cand:
                    return True
        return False


def container_of_top(name: str) -> Optional[str]:
    """Chrome 侧的顶层容器名 → 内部容器 id。"""
    n = norm_name(name).lower()
    if n in ("bar", "bookmark_bar", "bookmarks bar", "bookmarksbar", "个人收藏", "喜好项目"):
        return BAR_ID
    if n in ("other", "other bookmarks"):
        return OTHER_ID
    if n in ("mobile", "mobile bookmarks"):
        return MOBILE_ID
    return None


def mid_to_tree(root: Mid, skip_folders: Any = None) -> Tree:
    """
    把浏览器读出来的树转成统一树。
    三个容器一一对应：bar=书签栏/个人收藏，other=其他书签/书签菜单，mobile=移动设备书签。
    容器名称由各浏览器后端在读写时自行映射，因此两个浏览器的统一树结构完全一致。
    """
    tree = Tree()
    skip = skip_folders if isinstance(skip_folders, SkipRules) else SkipRules.from_config(skip_folders)
    for cid in CONTAINERS:
        tree.add(Item(id=cid, kind="folder", name=cid, parent_id=ROOT_ID, mtime=None))
    tree.children[ROOT_ID] = list(CONTAINERS)

    def walk(node: Mid, parent_id: str) -> None:
        if node.kind == "folder" and skip.matches(node.name, node.extra.get("special_id")):
            return
        # 名称原样保留（只在比较指纹里做归一化），避免改写用户的标题
        item = tree.add(Item(id="", kind=node.kind, name=node.name, parent_id=parent_id,
                             url=node.url, mtime=node.mtime), unique=True)
        if node.kind == "folder":
            for child in node.children:
                walk(child, item.id)

    mobile_holder: Optional[str] = None
    for top in root.children:
        if top.kind != "folder":
            # 顶层直接挂着的书签（解析外部 HTML 时常见）归入"其他书签"
            walk(top, OTHER_ID)
            continue
        if skip.matches(top.name, top.extra.get("special_id")):
            continue  # 跳过 Safari 的阅读列表等特殊容器（连同整棵子树）
        target = container_of_top(top.name)
        if target == MOBILE_ID:
            # 统一树的约定：移动设备书签 = 其他书签/<移动设备书签> 这个普通目录，
            # 名字统一用 MOBILE_FOLDER_NAME，各浏览器各自的叫法在读写时映射。
            if mobile_holder is None:
                mobile_holder = tree.find_id(OTHER_ID, "folder", MOBILE_FOLDER_NAME, None)
                if mobile_holder is None:
                    mobile_holder = tree.make_id(OTHER_ID, "folder", MOBILE_FOLDER_NAME, None)
                    tree.add(Item(id=mobile_holder, kind="folder", name=MOBILE_FOLDER_NAME,
                                  parent_id=OTHER_ID, mtime=top.mtime))
            for child in top.children:
                walk(child, mobile_holder)
            continue
        target = target or OTHER_ID  # 未知顶层容器并入"其他书签"
        for child in top.children:
            walk(child, target)
    return tree


def tree_to_mid(tree: Tree) -> Mid:
    root = Mid(kind="folder", name="root")
    for cid in CONTAINERS:
        node = Mid(kind="folder", name=cid, mtime=tree.items[cid].mtime if cid in tree.items else None)
        for child_id in tree.children_of(cid):
            sub = _mid_node(tree, child_id)
            if sub is not None:
                node.children.append(sub)
        root.children.append(node)
    return root


def _mid_node(tree: Tree, item_id: str) -> Optional[Mid]:
    it = tree.items.get(item_id)
    if it is None:
        return None
    if it.kind == "bookmark":
        return Mid(kind="bookmark", name=it.name, url=it.url, mtime=it.mtime)
    node = Mid(kind="folder", name=it.name, mtime=it.mtime)
    for cid in tree.children_of(item_id):
        sub = _mid_node(tree, cid)
        if sub is not None:
            node.children.append(sub)
    return node


# --------------------------------------------------------------------------------------
# Chrome 后端
# --------------------------------------------------------------------------------------


class ChromeBackend:
    name = "chrome"
    label = "Chrome"
    is_safari = False

    def __init__(self, user_data_dir: Path, profile: str = "Default") -> None:
        self.user_data_dir = Path(user_data_dir)
        self.profile = profile

    @property
    def profile_dir(self) -> Path:
        return self.user_data_dir / self.profile

    @property
    def bookmarks_path(self) -> Path:
        return self.profile_dir / "Bookmarks"

    @property
    def exists(self) -> bool:
        return self.bookmarks_path.exists()

    # ---- 读 ----
    def read_raw(self) -> Dict[str, Any]:
        with open(self.bookmarks_path, "rb") as fh:
            return json.loads(fh.read().decode("utf-8"))

    def read_tree(self, skip_folders: Any = None) -> Tree:
        raw = self.read_raw()
        roots = raw.get("roots") or {}

        def convert(node: Dict[str, Any]) -> Optional[Mid]:
            ntype = node.get("type")
            if ntype == "url":
                return Mid(kind="bookmark", name=node.get("name", ""), url=node.get("url", ""),
                           mtime=chrome_ts_to_epoch(node.get("date_modified") or node.get("date_added")))
            if ntype != "folder":
                return None
            children = []
            for child in node.get("children") or []:
                if isinstance(child, dict):
                    conv = convert(child)
                    if conv is not None:
                        children.append(conv)
            return Mid(kind="folder", name=node.get("name", ""),
                       mtime=chrome_ts_to_epoch(node.get("date_modified") or node.get("date_added")),
                       children=children)

        root = Mid(kind="folder", name="chrome")
        for key, internal in (("bookmark_bar", "bar"), ("other", "other")):
            node = roots.get(key)
            folder = Mid(kind="folder", name=internal)
            if isinstance(node, dict):
                for child in node.get("children") or []:
                    if isinstance(child, dict):
                        conv = convert(child)
                        if conv is not None:
                            folder.children.append(conv)
            root.children.append(folder)
        synced = roots.get("synced") or {}
        mobile = Mid(kind="folder", name="mobile", extra={"special_id": "mobile"})
        for child in synced.get("children") or []:
            if not isinstance(child, dict):
                continue
            # synced 下既有 Mobile bookmarks 文件夹，也可能直接挂着 url 节点
            if child.get("type") == "folder":
                for sub in child.get("children") or []:
                    if isinstance(sub, dict):
                        conv = convert(sub)
                        if conv is not None:
                            mobile.children.append(conv)
            elif child.get("type") == "url":
                conv = convert(child)
                if conv is not None:
                    mobile.children.append(conv)
        root.children.append(mobile)
        return mid_to_tree(root, skip_folders=skip_folders)

    def build_native_map(self, raw: Dict[str, Any], tree: Tree) -> Dict[str, Dict[str, Any]]:
        """把统一树里的节点与 Chrome 现有 JSON 节点对上（用于保留 id/guid/date_added）。"""
        mapping: Dict[str, Dict[str, Any]] = {}
        roots = raw.get("roots") or {}

        # used 初始为"树里还没被认领的 id"。认领顺序必须和 mid_to_tree 建树时一致，
        # 否则同一个 URL 的多个副本会认领到同一个 id（导致写回时丢书签或生成重复节点）。
        used: set = set(tree.items)

        def claim(base: str) -> Optional[str]:
            """按 base、base#2、base#3… 的顺序认领下一个可用的节点 id。"""
            cand, i = base, 2
            while cand not in tree.items or cand not in used:
                if cand not in tree.items and cand not in used:
                    return None      # 树里没有这个节点（不在本次认领范围内）
                cand = "%s#%d" % (base, i)
                i += 1
            used.discard(cand)
            return cand

        def walk_json(node: Dict[str, Any], parent_id: str) -> None:
            if not isinstance(node, dict):
                return
            if node.get("type") == "url":
                target = claim("u:" + norm_url(node.get("url", "")))
                if target and tree.items[target].kind == "bookmark":
                    mapping[target] = node
                return
            if node.get("type") != "folder":
                return
            target = claim("f:%s/%s" % (parent_id, norm_name(node.get("name", ""))))
            if not target:
                return
            mapping[target] = node
            for child in node.get("children") or []:
                walk_json(child, target)

        for key, internal in (("bookmark_bar", BAR_ID), ("other", OTHER_ID)):
            if isinstance(roots.get(key), dict):
                for child in roots[key].get("children") or []:
                    walk_json(child, internal)
        synced = roots.get("synced") or {}
        holder = tree.find_id(OTHER_ID, "folder", MOBILE_FOLDER_NAME, None)
        if holder:
            for child in synced.get("children") or []:
                if not isinstance(child, dict):
                    continue
                if child.get("type") == "folder":
                    for sub in child.get("children") or []:
                        walk_json(sub, holder)
                elif child.get("type") == "url":
                    walk_json(child, holder)
        return mapping

    # ---- 写 ----
    def write_tree(self, tree: Tree, native_map: Optional[Dict[str, Dict[str, Any]]] = None,
                   dry_run: bool = False) -> List[str]:
        native_map = native_map or {}
        raw = self.read_raw()
        roots = raw.setdefault("roots", {})
        steps: List[str] = []
        today = epoch_to_chrome_ts(None)

        def build(item_id: str) -> Optional[Dict[str, Any]]:
            it = tree.items.get(item_id)
            if it is None:
                return None
            native = native_map.get(item_id)
            if it.kind == "bookmark":
                node = dict(native) if native else {}
                node.update({
                    "type": "url",
                    "name": it.name,
                    "url": clean_url_for_write(it.url or ""),
                })
                node.setdefault("date_added", epoch_to_chrome_ts(it.mtime))
                node.setdefault("date_last_used", "0")
                node.setdefault("guid", str(uuid.uuid4()))
                node.setdefault("id", str(uuid.uuid4()))
                was_new = native is None
                if was_new:
                    node["date_added"] = epoch_to_chrome_ts(it.mtime)
                if norm_url(node.get("url", "")) != norm_url(native.get("url", "") if native else ""):
                    node["date_last_used"] = "0"
                return node
            node = dict(native) if native else {}
            node.update({
                "type": "folder",
                "name": it.name,
                "children": [c for c in (build(cid) for cid in tree.children_of(item_id)) if c is not None],
            })
            node.setdefault("date_added", epoch_to_chrome_ts(it.mtime))
            node.setdefault("date_last_used", "0")
            node.setdefault("guid", str(uuid.uuid4()))
            node.setdefault("id", str(uuid.uuid4()))
            node["date_modified"] = today
            return node

        holder_id = tree.find_id(OTHER_ID, "folder", MOBILE_FOLDER_NAME, None)
        for key, container, display in (("bookmark_bar", BAR_ID, "书签栏"),
                                        ("other", OTHER_ID, "其他书签")):
            node = roots.get(key)
            if not isinstance(node, dict):
                node = {
                    "children": [], "date_added": today, "date_last_used": "0",
                    "date_modified": "0", "guid": str(uuid.uuid4()), "id": str(uuid.uuid4()),
                    "name": CHROME_BAR_NAME if key == "bookmark_bar" else CHROME_OTHER_NAME,
                    "type": "folder",
                }
                roots[key] = node
            # 统一树里的"移动设备书签"目录在 Chrome 侧要落到 synced，不能留在 other 下
            skip_ids = {holder_id} if (key == "other" and holder_id) else set()
            new_children = [c for c in (build(cid) for cid in tree.children_of(container)
                                        if cid not in skip_ids) if c is not None]
            if mid_digest(new_children) != mid_digest(node.get("children") or []):
                node["children"] = new_children
                node["date_modified"] = today
                steps.append("%s：%d 个子项" % (display, len(new_children)))
            elif native_map:
                node["children"] = new_children

        # synced（移动设备书签）：统一树里它是 其他书签/<移动设备书签>，这里还原成 synced 结构
        synced = roots.get("synced")
        if not isinstance(synced, dict):
            synced = {"children": [], "date_added": today, "date_last_used": "0",
                      "date_modified": "0", "guid": str(uuid.uuid4()), "id": str(uuid.uuid4()),
                      "name": CHROME_MOBILE_NAME, "type": "folder"}
            roots["synced"] = synced
        mobile_json = None
        for child in synced.get("children") or []:
            if isinstance(child, dict) and child.get("type") == "folder":
                mobile_json = child
                break
        if mobile_json is None:
            # 尽量复用原有容器节点的 guid / id（重命名或整理结构时保持身份稳定）
            mobile_json = dict(native_map.get(holder_id) or {})
            mobile_json.update({"children": [], "name": mobile_json.get("name") or CHROME_MOBILE_NAME,
                                "type": "folder"})
            mobile_json.setdefault("date_added", today)
            mobile_json.setdefault("date_last_used", "0")
            mobile_json.setdefault("date_modified", "0")
            mobile_json.setdefault("guid", str(uuid.uuid4()))
            mobile_json.setdefault("id", str(uuid.uuid4()))
            if not any(isinstance(c, dict) and c.get("type") == "folder"
                       for c in synced.get("children") or []):
                synced.setdefault("children", []).append(mobile_json)
        # 旧版 Chrome 会把书签直接挂在 synced 下，这里统一挪进 Mobile bookmarks 文件夹。
        # 用 native_map 认领原节点（保留 guid / id），避免整理结构时给书签换身份、触发多余的云端同步。
        holder = holder_id
        known = set(native_map)
        for child in list(synced.get("children") or []):
            if not isinstance(child, dict) or child.get("type") != "url":
                continue
            synced["children"].remove(child)
            claimed = None
            if holder:
                claimed = Tree.base_id(holder, "bookmark", child.get("name", ""), child.get("url", ""))
                if claimed not in known or tree.items.get(claimed) is None:
                    claimed = None
            moved = dict(native_map.get(claimed)) if claimed else dict(child)
            moved.update({"type": "url", "name": child.get("name", ""), "url": child.get("url", ""),
                          "date_last_used": "0"})
            moved.setdefault("date_added", child.get("date_added", today))
            moved.setdefault("guid", str(uuid.uuid4()))
            moved.setdefault("id", str(uuid.uuid4()))
            mobile_json.setdefault("children", []).append(moved)
            steps.append("整理：synced 下的书签移入「%s」" % mobile_json.get("name", CHROME_MOBILE_NAME))
        if holder:
            wanted = tree.items[holder].name
            if mobile_json.get("name") != wanted:
                mobile_json["name"] = wanted
                steps.append("移动设备书签目录更名为「%s」" % wanted)
        new_mobile = [c for c in (build(cid) for cid in tree.children_of(holder)) if c is not None] \
            if holder else []
        if mid_digest(new_mobile) != mid_digest(mobile_json.get("children") or []):
            mobile_json["children"] = new_mobile
            mobile_json["date_modified"] = today
            steps.append("移动设备书签：%d 个子项" % len(new_mobile))
        else:
            mobile_json["children"] = new_mobile

        raw.pop("checksum", None)
        raw["checksum"] = hashlib.md5(json_dumps_compact(raw).encode("utf-8")).hexdigest()
        data = json.dumps(raw, ensure_ascii=False, indent=3).encode("utf-8")
        if not dry_run:
            atomic_write(self.bookmarks_path, data, keep_mode_from=self.bookmarks_path)
        return steps


# --------------------------------------------------------------------------------------
# Safari 后端
# --------------------------------------------------------------------------------------


class SafariBackend:
    name = "safari"
    label = "Safari"
    is_safari = True

    def __init__(self, bookmarks_path: Path) -> None:
        self.bookmarks_path = Path(bookmarks_path)

    @property
    def exists(self) -> bool:
        return self.bookmarks_path.exists()

    # ---- 读 ----
    def read_raw(self) -> Dict[str, Any]:
        with open(self.bookmarks_path, "rb") as fh:
            return plistlib.load(fh)

    def read_tree(self, skip_folders: Any = None) -> Tree:
        raw = self.read_raw()
        root = Mid(kind="folder", name="safari")
        # Safari 无移动设备分区；若用户自建同名目录，走普通目录逻辑即可

        def convert(node: Dict[str, Any]) -> Optional[Mid]:
            ntype = node.get("WebBookmarkType")
            if ntype == "WebBookmarkTypeLeaf":
                title = node.get("URIDictionary", {}).get("title") if isinstance(node.get("URIDictionary"), dict) else None
                return Mid(kind="bookmark",
                           name=title or node.get("Title") or "",
                           url=node.get("URLString", ""),
                           mtime=safari_ts_to_epoch(node.get("WebBookmarkModificationDate")),
                           extra={"safari": dict(node)})
            if ntype != "WebBookmarkTypeList":
                return None
            children = []
            for child in node.get("Children") or []:
                if isinstance(child, dict):
                    conv = convert(child)
                    if conv is not None:
                        children.append(conv)
            return Mid(kind="folder", name=node.get("Title", ""),
                       mtime=safari_ts_to_epoch(node.get("WebBookmarkModificationDate")),
                       children=children,
                       extra={"safari": dict(node),
                              "special_id": node.get("WebBookmarkSpecialID")})

        for child in raw.get("Children") or []:
            if isinstance(child, dict):
                conv = convert(child)
                if conv is not None:
                    root.children.append(conv)
        return mid_to_tree(root, skip_folders=skip_folders)

    # ---- 写 ----
    def write_tree(self, tree: Tree, native_map: Optional[Dict[str, Dict[str, Any]]] = None,
                   dry_run: bool = False) -> List[str]:
        native_map = native_map or {}
        raw = self.read_raw()
        steps: List[str] = []
        modified = epoch_to_safari_ts(None)

        def build(item_id: str) -> Optional[Dict[str, Any]]:
            it = tree.items.get(item_id)
            if it is None:
                return None
            native = native_map.get(item_id) or {}
            node = dict(native)
            if it.kind == "bookmark":
                node.update({
                    "WebBookmarkType": "WebBookmarkTypeLeaf",
                    "URLString": clean_url_for_write(it.url or ""),
                    "URIDictionary": dict(node.get("URIDictionary") or {}, title=it.name),
                })
                node["WebBookmarkUUID"] = node.get("WebBookmarkUUID") or str(uuid.uuid4()).upper()
            else:
                node.update({
                    "WebBookmarkType": "WebBookmarkTypeList",
                    "Title": it.name,
                    "Children": [c for c in (build(cid) for cid in tree.children_of(item_id)) if c is not None],
                })
                node["WebBookmarkUUID"] = node.get("WebBookmarkUUID") or str(uuid.uuid4()).upper()
            node["WebBookmarkModificationDate"] = modified
            return node

        def find_top(titles: Sequence[str]) -> Optional[Dict[str, Any]]:
            wanted = {norm_name(t) for t in titles}
            for child in raw.get("Children") or []:
                if isinstance(child, dict) and norm_name(child.get("Title", "")) in wanted:
                    return child
            return None

        changed = False

        bar_tree = tree.children_of(BAR_ID)
        bar_json = find_top(SAFARI_BAR_TITLES)
        new_bar = [c for c in (build(cid) for cid in bar_tree) if c is not None]
        if bar_json is None:
            bar_json = {"Children": new_bar, "Title": "BookmarksBar",
                        "WebBookmarkType": "WebBookmarkTypeList",
                        "WebBookmarkUUID": str(uuid.uuid4()).upper(),
                        "WebBookmarkModificationDate": modified}
            raw.setdefault("Children", []).append(bar_json)
            steps.append("新建容器 个人收藏：%d 个子项" % len(new_bar))
            changed = True
        elif mid_digest(new_bar) != mid_digest(bar_json.get("Children") or []):
            bar_json["Children"] = new_bar
            bar_json["WebBookmarkModificationDate"] = modified
            steps.append("个人收藏：%d 个子项" % len(new_bar))
            changed = True

        menu_json = find_top(SAFARI_MENU_TITLES)
        # 统一树把"移动设备书签"表达为 其他书签/<移动设备书签>，Safari 侧直接落成同名目录
        menu_children = [c for c in (build(cid) for cid in tree.children_of(OTHER_ID)) if c is not None]
        new_menu = menu_children
        if menu_json is None:
            menu_json = {"Children": new_menu, "Title": "BookmarksMenu",
                         "WebBookmarkType": "WebBookmarkTypeList",
                         "WebBookmarkUUID": str(uuid.uuid4()).upper(),
                         "WebBookmarkModificationDate": modified}
            raw.setdefault("Children", []).append(menu_json)
            steps.append("新建容器 书签菜单：%d 个子项" % len(new_menu))
            changed = True
        elif mid_digest(new_menu) != mid_digest(menu_json.get("Children") or []):
            menu_json["Children"] = new_menu
            menu_json["WebBookmarkModificationDate"] = modified
            steps.append("书签菜单：%d 个子项" % len(new_menu))
            changed = True

        if not changed:
            return steps

        raw["WebBookmarkModificationDate"] = modified
        data = plistlib.dumps(raw, fmt=plistlib.FMT_BINARY)
        if not dry_run:
            atomic_write(self.bookmarks_path, data, keep_mode_from=self.bookmarks_path)
        return steps


# --------------------------------------------------------------------------------------
# 三方合并
# --------------------------------------------------------------------------------------


@dataclass
class Change:
    id: str
    kind: str            # add | update | delete
    side: str            # chrome | safari
    label: str
    ts: Optional[float] = None
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "side": self.side, "label": self.label,
                "ts": self.ts, "time": fmt_ts(self.ts), "detail": self.detail}


@dataclass
class MergeResult:
    merged: Tree
    changes: Dict[str, List[Change]] = field(default_factory=dict)
    conflicts: List[str] = field(default_factory=list)
    tombstones: List[str] = field(default_factory=list)
    blocked_deletes: int = 0        # 被熔断拦下的删除条数（>0 说明检测到异常）


def tree_from_state(base: Dict[str, Any]) -> Tree:
    tree = Tree()
    for item_id, rec in (base.get("items") or {}).items():
        tree.items[item_id] = Item.from_state(item_id, rec)
    for parent, kids in (base.get("children") or {}).items():
        tree.children[parent] = [k for k in kids if k in tree.items]
    for cid in CONTAINERS:
        if cid not in tree.items:
            tree.add(Item(id=cid, kind="folder", name=cid, parent_id=ROOT_ID))
    tree.children[ROOT_ID] = list(CONTAINERS)
    return tree


# 非对称同步用的伪容器 id：把"书签栏/其他书签/移动设备书签"放进同一个根下
PSEUDO_ROOT = "scope:root"
PSEUDO_BAR = "scope:bar"
PSEUDO_OTHER = "scope:other"
PSEUDO_MOBILE = "scope:mobile"
PSEUDO_LABELS = {PSEUDO_BAR: "书签栏", PSEUDO_OTHER: "其他书签", PSEUDO_MOBILE: "移动设备书签"}
PSEUDO_IDS = (PSEUDO_BAR, PSEUDO_OTHER, PSEUDO_MOBILE)
# 写进 Safari 时，书签栏的内容直接铺开、不套一层
UNWRAPPED = (PSEUDO_BAR,)


def unpack_containers(tree: Tree) -> Tree:
    """
    把"书签栏/其他书签/移动设备书签"三个容器拆成同一个根下的三个命名目录。

    用于非对称同步：Chrome 侧保留自己的容器结构，而 Safari 侧只有一个目标目录。
    书签栏的内容在写进 Safari 时会直接铺在目标目录下（不套一层「书签栏」），
    其他书签 / 移动设备书签 则会各建一个同名子目录，避免混在一起。
    """
    out = Tree()
    for cid, label in ((PSEUDO_BAR, CONTAINER_LABELS[BAR_ID]),
                       (PSEUDO_OTHER, CONTAINER_LABELS[OTHER_ID]),
                       (PSEUDO_MOBILE, "移动设备书签")):
        out.add(Item(id=cid, kind="folder", name=label, parent_id=PSEUDO_ROOT))
    out.children[PSEUDO_ROOT] = list(PSEUDO_IDS)
    # "移动设备书签"在统一树里是 other 下的一个普通目录（Chrome 侧的 synced 结构），
    # 这里把它还原成独立的伪容器，避免和其它书签混在一起
    mobile_names = {norm_name(MOBILE_FOLDER_NAME)}
    for src_cid, dst_cid in ((BAR_ID, PSEUDO_BAR), (OTHER_ID, PSEUDO_OTHER),
                             (MOBILE_ID, PSEUDO_MOBILE)):
        for kid in tree.children_of(src_cid):
            if kid not in tree.items:
                continue
            node = tree.items[kid]
            if (dst_cid == PSEUDO_OTHER and node.kind == "folder"
                    and norm_name(node.name) in mobile_names):
                # 这一层只是"移动设备书签"的容器，内容直接放进伪容器，不保留这层壳
                for grandkid in tree.children_of(kid):
                    if grandkid in tree.items:
                        _add_subtree(out, tree, grandkid, PSEUDO_MOBILE)
                continue
            _add_subtree(out, tree, kid, dst_cid)
    return out


def pack_container(tree: Tree, orig: Tree, container_id: str, unwrap: bool = False) -> Tree:
    """
    把 unpack 出来的命名目录装回某个浏览器的完整树。

    unwrap=True 时，指定目录（书签栏）的内容直接铺进该容器，不保留那一层。
    """
    out = orig.clone()
    for pid, key in ((PSEUDO_BAR, BAR_ID), (PSEUDO_OTHER, OTHER_ID), (PSEUDO_MOBILE, MOBILE_ID)):
        if key not in out.items:
            out.add(Item(id=key, kind="folder", name=key, parent_id=ROOT_ID))
        out.children[key] = []
        if pid not in tree.items:
            continue
        kids = tree.children_of(pid)
        if unwrap and pid in UNWRAPPED:
            for kid in kids:
                if kid in tree.items:
                    _add_subtree(out, tree, kid, key)
        elif kids:
            # 非书签栏的容器：保留一个同名子目录作为"位置标记"
            name = PSEUDO_LABELS.get(pid, pid)
            marker = f"{key}:{name}"
            out.add(Item(id=marker, kind="folder", name=name, parent_id=key, mtime=None))
            for kid in kids:
                if kid in tree.items:
                    _add_subtree(out, tree, kid, marker)
    return out


def empty_scope_tree() -> Tree:
    """空的同步输入树（只含三个伪容器）。用于"配置了目录但该目录还不存在"。"""
    out = Tree()
    for pid in PSEUDO_IDS:
        out.add(Item(id=pid, kind="folder", name=PSEUDO_LABELS[pid], parent_id=PSEUDO_ROOT))
    out.children[PSEUDO_ROOT] = list(PSEUDO_IDS)
    return out


def in_scope_tree(tree: Tree, folder_id: Optional[str]) -> Optional[Tree]:
    """取某个浏览器的"同步输入"：指定目录时只取该目录内容，否则整棵树按容器拆分。"""
    if folder_id is None:
        return unpack_containers(tree)
    if folder_id not in tree.items:
        return None
    sub = scoped_clone(tree, folder_id, tree.items[folder_id].name)
    # 目标目录下的内容当作"书签栏"（直接铺开的那一层）
    out = Tree()
    for pid in PSEUDO_IDS:
        out.add(Item(id=pid, kind="folder", name=PSEUDO_LABELS[pid], parent_id=PSEUDO_ROOT))
    out.children[PSEUDO_ROOT] = list(PSEUDO_IDS)
    if folder_id in sub.items:
        for kid in sub.children_of(folder_id):
            if kid in sub.items:
                _add_subtree(out, sub, kid, PSEUDO_BAR)
    return out


def resolve_scope_folder(tree: Tree, scope: str) -> Optional[str]:
    """把 "书签栏/同步" 这样的路径解析成树里的目录 id；找不到返回 None。"""
    parts = [p for p in str(scope).split("/") if p.strip()]
    if not parts:
        return None

    # 单段写法（如 "sync_chrome"）：优先当成"某个容器下的同名目录"，其次才当成容器名。
    # 否则像 "sync_chrome" 这种名字会被误判（历史 bug：曾被解析成容器 bar）。
    if len(parts) == 1:
        for cid in CONTAINERS:
            found = tree.find_id(cid, "folder", parts[0].strip(), None)
            if found:
                return found

    label_to_id = {v: k for k, v in CONTAINER_LABELS.items()}
    head = label_to_id.get(parts[0].strip())
    if head is None:
        for cid in CONTAINERS:
            found = tree.find_id(cid, "folder", parts[0].strip(), None)
            if found:
                head = cid
                break
        if head is None:
            return None
    cursor: Optional[str] = head
    for part in parts[1:]:
        if cursor is None:
            return None
        cursor = tree.find_id(cursor, "folder", part.strip(), None)
    return cursor


def scoped_clone(tree: Tree, folder_id: Optional[str], name: str = "同步") -> Tree:
    """
    只保留 scope 目录（含）以下的内容，并把该目录本身作为根。

    注意：这一侧**没有**该目录时要返回"只含该目录的空树"，而不是整棵树的副本 ——
    否则范围外的全部内容都会被当成"范围内"，范围同步就形同虚设。
    """
    if folder_id is None:
        return tree.clone()
    if folder_id not in tree.items:
        empty = Tree()
        empty.add(Item(id=folder_id, kind="folder", name=name, parent_id=ROOT_ID))
        empty.children[ROOT_ID] = [folder_id]
        return empty
    out = Tree()
    is_root = True
    for item_id in tree.subtree_ids(folder_id):
        if item_id not in tree.items:
            continue
        item = tree.items[item_id]
        if is_root:
            # 把这层目录当成根：parent 置空，id 用回它自己的 id（便于写回时定位）
            out.add(Item(id=item_id, kind="folder", name=item.name, parent_id=ROOT_ID,
                         mtime=item.mtime))
            out.children[ROOT_ID] = [item_id]
            is_root = False
            continue
        out.add(Item(id=item_id, kind=item.kind, name=item.name, parent_id=item.parent_id,
                     url=item.url, mtime=item.mtime))
    if is_root:                      # 空目录
        out.add(Item(id=folder_id, kind="folder", name=tree.items[folder_id].name,
                     parent_id=ROOT_ID))
        out.children[ROOT_ID] = [folder_id]
    return out


def ensure_scope_folder(target: Tree, folder_id: str, name: str) -> Tree:
    """目标树里没有这个 scope 目录时补出来（按路径 id 落在容器下），保证写回有落点。"""
    if folder_id in target.items:
        return target
    out = target.clone()
    container = BAR_ID if folder_id.startswith("f:%s/" % BAR_ID) else OTHER_ID
    if container not in out.items:
        out.add(Item(id=container, kind="folder", name=container, parent_id=ROOT_ID))
    out.add(Item(id=folder_id, kind="folder", name=name, parent_id=container, mtime=None))
    return out


def assemble_for_browser(original: Tree, merged: Tree, folder_id: str, name: str = "同步") -> Tree:
    """
    为某个浏览器组装"最终应写入的完整树"：
      - 范围内（scope 目录内部）：用合并结果
      - 范围外：保留**该浏览器自己**的内容，一格不动

    不能两边共用同一棵树：合并结果里的范围外部分来自 Chrome，直接写给 Safari 会让
    Safari 把它自己范围外的书签当成"多余项"删掉（实测发生过）。

    实现上只替换 scope 目录的子节点，其余原样保留 —— 不要"摘掉目录再挂回"，
    那样很容易把目录从父节点的子项列表里弄丢（踩过一次）。
    """
    out = original.clone()
    if folder_id not in out.items:
        out = ensure_scope_folder(out, folder_id, name)
    # 清掉该目录原有的内容
    for item_id in list(original.subtree_ids(folder_id)):
        if item_id == folder_id:
            continue
        out.items.pop(item_id, None)
        out.children.pop(item_id, None)
    out.children[folder_id] = []
    # 挂上合并结果里该目录的内容
    if folder_id in merged.items:
        for item_id in merged.children_of(folder_id):
            if item_id in merged.items:
                _add_subtree(out, merged, item_id, folder_id)
    return out


def splice_scoped_into(target: Tree, scoped: Tree, folder_id: str) -> Tree:
    """
    把 scope 合并结果写回完整树：只替换该目录的子节点，容器与其它内容原样保留。
    """
    if folder_id not in target.items:
        target.add(Item(id=folder_id, kind="folder", name=scoped.items[folder_id].name
                        if folder_id in scoped.items else "同步",
                        parent_id=OTHER_ID, mtime=None))
    merged = target.clone()
    # 清掉该目录原有的整棵子树
    for item_id in target.subtree_ids(folder_id):
        if item_id == folder_id:
            continue
        merged.items.pop(item_id, None)
        merged.children.pop(item_id, None)
    merged.children[folder_id] = []
    # 把 scoped 里该目录的内容挂回来
    for item_id in scoped.subtree_ids(folder_id):
        if item_id == folder_id or item_id not in scoped.items:
            continue
        item = scoped.items[item_id]
        merged.add(Item(id=item_id, kind=item.kind, name=item.name, parent_id=item.parent_id,
                        url=item.url, mtime=item.mtime))
    if folder_id in scoped.items:
        cur = target.items[folder_id]
        merged.items[folder_id] = replace(cur, name=scoped.items[folder_id].name)
    return merged


def looks_flattened(current: Tree, base: Tree) -> Tuple[bool, str]:
    """
    识别"目录结构被抹平"：书签数量大致不变，但目录层数/数量明显减少。

    这是 iOS Safari 往返的典型特征：iOS 上不显示第一级目录、内容直接铺开，
    再经 iCloud 回到 macOS 时目录就没了。此时若按普通规则同步，那些"消失的目录"
    会被当成删除，连目录里的书签一起删掉（真实事故就是这样丢了 368 条）。
    """
    cur_folders = [i for i, it in current.items.items() if it.kind == "folder" and i not in CONTAINERS]
    base_folders = [i for i, it in base.items.items() if it.kind == "folder" and i not in CONTAINERS]
    cur_books = sum(1 for it in current.items.values() if it.kind == "bookmark")
    base_books = sum(1 for it in base.items.values() if it.kind == "bookmark")
    lost = [i for i in base_folders if i not in current.items]
    if len(lost) < 3:
        return False, ""
    if len(lost) < max(3, int(len(base_folders) * 0.3)):
        return False, ""
    # 书签数量明显减少 → 更可能是真的批量删除，交给删除熔断处理，不在这里拦
    if cur_books < base_books * 0.9:
        return False, ""
    return True, ("目录数量从 %d 降到 %d（少了 %d 个），但书签数量基本没变（%d → %d）"
                  % (len(base_folders), len(cur_folders), len(lost), base_books, cur_books))


def detect_changes(current: Tree, base: Tree, side: str) -> Dict[str, Change]:
    """当前树 vs 基线：找出这一侧真实的增 / 改 / 删。"""
    changes: Dict[str, Change] = {}
    for item_id, item in current.items.items():
        if item_id in CONTAINERS:
            continue
        base_item = base.items.get(item_id)
        if base_item is None:
            changes[item_id] = Change(item_id, "add", side, current.label(item_id), item.mtime)
            continue
        if item.fingerprint() != base_item.fingerprint():
            detail = []
            if norm_name(item.name) != norm_name(base_item.name):
                detail.append("改名")
            if item.kind == "bookmark" and norm_url(item.url) != norm_url(base_item.url):
                detail.append("换地址")
            if item.parent_id != base_item.parent_id:
                detail.append("移动")
            changes[item_id] = Change(item_id, "update", side, current.label(item_id), item.mtime,
                                      "、".join(detail) or "内容变化")
    for item_id, base_item in base.items.items():
        if item_id in CONTAINERS or item_id in current.items:
            continue
        # 父目录整体没了就不必单独报（按子树根处理）
        if any(p in base.items and p not in current.items for p in base.path_ids(item_id)[:-1]):
            continue
        changes[item_id] = Change(item_id, "delete", side, base.label(item_id), base_item.mtime,
                                  "在 %s 中已删除" % side)
    return changes


def _remove_subtree(tree: Tree, item_id: str) -> List[str]:
    """把以 item_id 为根的整棵子树从树里摘掉，返回被移除的 id。"""
    removed: List[str] = []
    for sid in reversed(tree.subtree_ids(item_id)):
        tree.items.pop(sid, None)
        tree.children.pop(sid, None)
        removed.append(sid)
    for kids in tree.children.values():
        if item_id in kids:
            kids.remove(item_id)
    return removed


def _set_parent(tree: Tree, item_id: str, parent_id: Optional[str]) -> None:
    for kids in tree.children.values():
        if item_id in kids:
            kids.remove(item_id)
    tree.children.setdefault(parent_id or ROOT_ID, []).append(item_id)
    tree.items[item_id] = replace(tree.items[item_id], parent_id=parent_id)


def _add_subtree(dst: Tree, src: Tree, item_id: str, new_parent: Optional[str]) -> List[str]:
    """把 src 中 item_id 的整棵子树复制到 dst 的 new_parent 下（父目录按需补建）。"""
    added: List[str] = []
    if new_parent is not None and new_parent not in dst.items and new_parent in src.items:
        pitem = src.items[new_parent]
        dst.add(Item(id=new_parent, kind="folder", name=pitem.name,
                     parent_id=pitem.parent_id, mtime=pitem.mtime))

    def rec(sid: str, parent_id: Optional[str]) -> None:
        item = src.items.get(sid)
        if item is None or item.kind not in ("folder", "bookmark"):
            return
        if sid in dst.items:
            # id 已被占用（例如同 URL 的重复书签），换一个不冲突的 id
            dst.add(Item(id=sid, kind=item.kind, name=item.name, parent_id=parent_id,
                         url=item.url, mtime=item.mtime), unique=True)
            new_id = dst.children[parent_id or ROOT_ID][-1]
            added.append(new_id)
        else:
            dst.add(Item(id=sid, kind=item.kind, name=item.name, parent_id=parent_id,
                         url=item.url, mtime=item.mtime))
            new_id = sid
            added.append(new_id)
        if item.kind == "folder":
            for cid in src.children_of(sid):
                rec(cid, new_id)

    rec(item_id, new_parent)
    return added


def merge(base: Tree, chrome_tree: Tree, safari_tree: Tree, prefer: str = "newest",
          extra_sources: Optional[Dict[str, Tree]] = None,
          max_delete_ratio: float = 0.05, max_delete_count: int = 10,
          no_delete: bool = False, scope_anchor: Optional[str] = None,
          verbose: int = 0) -> MergeResult:
    """
    三方合并：
      base        = 上次同步后的统一状态
      chrome_tree = Chrome 当前书签
      safari_tree = Safari 当前书签
      extra_sources = 额外并入的来源（如 import 进来的书签文件）
    冲突规则（prefer=newest 时）：比较两侧的修改时间，新者胜；
      删除 vs 修改 → 时间新者胜；删除 vs 新增 → 以删除为准；新增 vs 新增 → 保留一份。
    """
    changes: Dict[str, Dict[str, Change]] = {
        "chrome": detect_changes(chrome_tree, base, "chrome"),
        "safari": detect_changes(safari_tree, base, "safari"),
    }
    merged = base.clone()
    result = MergeResult(merged=merged,
                         changes={side: list(ch.values()) for side, ch in changes.items()})
    resolved: Dict[str, str] = {}   # item_id -> 采用哪一侧的版本
    actions: Dict[str, str] = {}    # item_id -> add / update / delete
    sources = {"chrome": chrome_tree, "safari": safari_tree}

    def pick(a: Change, b: Change, side_a: str, side_b: str) -> str:
        if prefer in ("chrome", "safari"):
            return prefer
        ta = a.ts if a.ts is not None else now_ts()
        tb = b.ts if b.ts is not None else now_ts()
        return side_a if ta >= tb else side_b

    # ---- 第一遍：逐侧判定每个条目的最终命运 ----
    for side in ("chrome", "safari"):
        other_side = "safari" if side == "chrome" else "chrome"
        for item_id, change in changes[side].items():
            if item_id in resolved:
                continue
            other = changes[other_side].get(item_id)
            decision = change.kind
            if other is not None:
                pair = {change.kind, other.kind}
                # 冲突说明：每对冲突只在"胜者"那一轮记录一次（胜者负责应用）
                if pair == {"delete"}:
                    decision = "delete"
                elif pair == {"add"}:
                    decision = "add"
                    result.conflicts.append("「%s」两边都新增 → 保留一份（%s / %s）" % (
                        change.label, fmt_ts(change.ts), fmt_ts(other.ts)))
                elif pair == {"update"}:
                    winner = pick(change, other, side, other_side)
                    decision = "update"
                    if winner != side:
                        continue  # 交给胜者那一轮应用
                    result.conflicts.append("「%s」两边都修改过 → 取 %s 的版本（%s / %s）" % (
                        change.label, winner, fmt_ts(change.ts), fmt_ts(other.ts)))
                elif pair == {"delete", "add"}:
                    decision = "delete"  # 一边删、另一边又新增：以删除为准
                    result.conflicts.append("「%s」一边删除一边新增 → 以删除为准（%s / %s）" % (
                        change.label, fmt_ts(change.ts), fmt_ts(other.ts)))
                elif pair == {"delete", "update"}:
                    winner = pick(change, other, side, other_side)
                    if winner != side:
                        continue
                    decision = change.kind
                    result.conflicts.append("「%s」一边删除一边修改 → 按 %s 处理（%s / %s）" % (
                        change.label, "删除" if decision == "delete" else "保留修改",
                        fmt_ts(change.ts), fmt_ts(other.ts)))
                elif pair == {"add", "update"}:
                    winner = pick(change, other, side, other_side)
                    if winner != side:
                        continue
                    decision = "add"
                    result.conflicts.append("「%s」一边新增一边修改 → 采用新增的版本（%s / %s）" % (
                        change.label, fmt_ts(change.ts), fmt_ts(other.ts)))
            # winner：该条目最终采用哪一侧的版本（用于取内容）
            # actions：该条目最终要执行的动作（add / update / delete）
            resolved[item_id] = side
            actions[item_id] = decision

    # ---- 删除熔断 ----
    # 只删"一边没了"就当用户删了，是危险的：一侧文件若因 iCloud 覆盖、文件损坏或被重置
    # 而瞬间变空/极少，工具会把这场故障当成用户意图，忠实复制到另一个浏览器 ——
    # 一次故障变成两边全灭（实际发生过：Safari 文件被重写后，Chrome 被删到只剩 8 条）。
    # 因此单次同步的删除量设上限：超过就整体拒绝删除，只做新增/修改，并留下明确告警。
    delete_ids = [i for i, d in actions.items() if d == "delete"]
    if delete_ids and no_delete:
        result.blocked_deletes = len(delete_ids)
        result.conflicts.append(
            "已开启 no_delete：本次 %d 条删除被全部忽略，只执行新增/修改（可在配置里关掉）"
            % len(delete_ids))
        actions = {k: v for k, v in actions.items() if v != "delete"}
    baseline_size = max(1, sum(1 for v in base.items.values() if v.kind == "bookmark"))
    ratio_limit = float(max_delete_ratio or 0)
    count_limit = int(max_delete_count or 0)
    allowed = int(baseline_size * ratio_limit) if ratio_limit > 0 else 0
    allowed = max(allowed, count_limit)
    if delete_ids and allowed > 0 and len(delete_ids) > allowed:
        result.blocked_deletes = len(delete_ids)
        result.conflicts.append(
            "⚠️ 本次要删除 %d 条（基线共 %d 条，上限 %d 条）—— 已在删除前熔断。"
            "这一般意味着某一侧的书签文件被清空或大面积改写（iCloud 覆盖、文件损坏等），"
            "而不是你真的删了这么多。已只执行新增/修改；确认确实要删请加 --allow-mass-delete 重跑。"
            % (len(delete_ids), baseline_size, allowed))
        actions = {k: v for k, v in actions.items() if v != "delete"}

    # ---- 第二遍：把结果套到统一树上（先删后增改，保证父子顺序） ----
    for item_id in sorted([i for i, d in actions.items() if d == "delete"],
                          key=lambda i: -len(merged.path_ids(i))):
        if item_id in merged.items:
            # 先把 URL 抄下来再删：_remove_subtree 之后条目就从 merged 里消失了，
            # 而删除记录（墓碑）必须带上 URL —— 被删条目随后也会从基线移除，
            # 只留 id 就再也拼不回"被删的是哪个 URL"。
            doomed = [(rid, merged.items[rid].url or
                       (base.items[rid].url if rid in base.items else None))
                      for rid in merged.subtree_ids(item_id) if rid in merged.items]
            _remove_subtree(merged, item_id)
            result.tombstones.extend(doomed)
            log("  - %s" % base.label(item_id), verbose, 2)

    for item_id, winner in resolved.items():
        if actions.get(item_id) == "delete":
            continue
        src = sources[winner]
        item = src.items.get(item_id)
        if item is None:
            continue
        parent = item.parent_id
        if parent is not None and parent not in merged.items:
            # 限定范围同步时，"无父可依"的条目必须留在范围内，
            # 否则会被塞进容器（书签栏/其他书签）里 —— 那就越界了
            parent = scope_anchor if (scope_anchor and scope_anchor in merged.items) else (
                OTHER_ID if OTHER_ID in merged.items else None)
        if item_id in merged.items:
            # 保留原节点，只更新名称 / 地址（移动走 _set_parent）
            cur = merged.items[item_id]
            merged.items[item_id] = replace(cur, name=item.name,
                                            url=item.url if item.kind == "bookmark" else cur.url,
                                            mtime=item.mtime)
            if cur.parent_id != parent:
                _set_parent(merged, item_id, parent)
            log("  ~ %s" % src.label(item_id), verbose, 2)
        else:
            _add_subtree(merged, src, item_id, parent)
            log("  + %s" % src.label(item_id), verbose, 2)

    # ---- 额外来源（import） ----
    # 注意：_add_subtree 会把整棵子树一起搬过来（包括已经在 merged 里的节点，用新 id 安置），
    # 所以这里只处理"父节点不是额外来源自身节点"的子树根，避免重复搬运。
    for extra in (extra_sources or {}).values():
        for item_id in list(extra.items):
            if item_id in CONTAINERS:
                continue
            parent_id = extra.items[item_id].parent_id
            if parent_id not in CONTAINERS and parent_id in extra.items:
                continue  # 它的父节点也在导入树里，会被一起搬过去
            if item_id in merged.items:
                continue  # 与现有书签重复（例如同 URL），跳过
            parent = parent_id if parent_id in merged.items else (
                OTHER_ID if OTHER_ID in merged.items else None)
            _add_subtree(merged, extra, item_id, parent)

    # ---- 收尾：容器必须存在，清掉孤儿 ----
    for cid in CONTAINERS:
        if cid not in merged.items:
            merged.add(Item(id=cid, kind="folder", name=cid, parent_id=ROOT_ID))
        merged.children.setdefault(cid, [])
    merged.children.pop(ROOT_ID, None)
    valid = set(merged.items)
    for parent, kids in list(merged.children.items()):
        merged.children[parent] = [k for k in kids if k in valid and k != parent]
    return result


# --------------------------------------------------------------------------------------
# 把统一树套用回某一侧（用于打印"将要做的改动"）
# --------------------------------------------------------------------------------------


def plan_changes(target: Tree, current: Tree) -> List[str]:
    """列出"要把 current 变成 target 需要做哪些事"（用于演练输出）。"""
    steps: List[str] = []
    for item_id in list(current.items):
        if item_id in CONTAINERS:
            continue
        if item_id not in target.items:
            steps.append("- 删除 %s" % current.label(item_id))
    for item_id in sorted(target.items, key=lambda i: len(target.path_ids(i))):
        if item_id in CONTAINERS:
            continue
        titem = target.items[item_id]
        citem = current.items.get(item_id)
        if citem is None:
            steps.append("+ 新增 %s" % target.label(item_id))
            continue
        if citem.parent_id != titem.parent_id:
            steps.append("→ 移动 %s 到 %s" % (titem.name, target.display_path(item_id)))
        if norm_name(citem.name) != norm_name(titem.name):
            steps.append("~ 改名 %r → %r" % (citem.name, titem.name))
        if titem.kind == "bookmark" and norm_url(citem.url) != norm_url(titem.url):
            steps.append("~ 换地址 %s → %s" % (citem.url, titem.url))
    return steps


# --------------------------------------------------------------------------------------
# 配置与状态
# --------------------------------------------------------------------------------------


DEFAULT_CONFIG: Dict[str, Any] = {
    "chrome": {
        "user_data_dir": "~/Library/Application Support/Google/Chrome",
        "profile": "Default",
    },
    "safari": {
        "bookmarks_path": "~/Library/Safari/Bookmarks.plist",
    },
    "mode": "manual",                 # manual | scheduled | watch（见 bmsync mode --help）
    "watch": {
        "interval": 5.0,              # 轮询间隔（秒）
        "settle": 3.0,               # 文件连续多久没变化才算"写完了"（秒）
        "resync_after": 60.0,         # 启动时/长时间无变化后，至少多久做一次兜底同步（秒）
        "bridge_port": 8765,          # 给 Chrome 扩展用的本地桥接端口（0 = 关闭）
        "bridge_timeout": 90.0,       # 扩展多久没心跳就认为它不在线（秒）
    },
    "sync": {
        "direction": "both",
        "prefer": "newest",
        "skip_folders": list(DEFAULT_SKIP_FOLDERS),
        "backup_keep": 20,            # 分级保留：30 天以上只留这么多份
        "backup_min_interval": 300,   # 距上次备份不足这么多秒且"实质内容"没变，就不新增备份
        "max_delete_ratio": 0.05,     # 单次同步最多允许删除的比例（超过则熔断，保护数据）
        "max_delete_count": 10,       # 单次同步最多允许删除的绝对条数（与比例取较大者）
        "scope": "",                  # 对称范围：两侧同名目录之间同步（如 "书签栏/同步"）
        "scope_chrome": "",           # 非对称：Chrome 侧参加同步的目录（空 = 整个书签树）
        "scope_safari": "",           # 非对称：Safari 侧接收/回写的目录（如 "sync_chrome"）
        "no_delete": False,           # true = 永不删除，只做新增/修改（最保守）
        "require_browsers_closed": True,
    },
    "state_dir": str(APP_DIR / "state"),
    "log_file": str(APP_DIR / "state" / "bmsync.log"),
}


def expand(path: Any) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(path))))


def write_config_keys(path: Path, updates: Dict[str, Any]) -> None:
    """把若干键合并写回配置文件（保留其它内容与未知键）。"""
    data: Dict[str, Any] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            data = {}
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            data[key].update(value)
        else:
            data[key] = value
    atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))


def config_target(cfg: Dict[str, Any], explicit: Optional[str]) -> Path:
    """确定"模式"要写进哪个配置文件。"""
    if explicit:
        return expand(explicit)
    return Path(cfg.get("_config_path") or (APP_DIR / "config.json"))


def watch_settings(cfg: Dict[str, Any]) -> Dict[str, float]:
    w = cfg.get("watch") or {}
    def num(key: str, default: float, low: float = 0.1) -> float:
        try:
            return max(low, float(w.get(key, default)))
        except (TypeError, ValueError):
            return default
    return {"interval": num("interval", 5.0), "settle": num("settle", 3.0),
            "resync_after": num("resync_after", 60.0)}


def load_config(path: Optional[Path]) -> Dict[str, Any]:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    explicit = Path(path).expanduser() if path else None
    if explicit is not None and not explicit.exists():
        # 显式指定的配置文件不存在时如果静默回退到默认配置，很容易在错误的浏览器配置上跑同步
        eprint("警告：指定的配置文件不存在：%s" % explicit)
        eprint("      将改用默认配置（%s）。要自定义请先运行 bmsync init-config。" % (APP_DIR / "config.json"))
    candidates = [explicit] if explicit else [APP_DIR / "config.json", Path.cwd() / "config.json"]
    for cand in candidates:
        if cand and cand.exists():
            try:
                user_cfg = json.loads(cand.read_text(encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001
                eprint("配置文件解析失败：%s（%s）" % (cand, exc))
                continue
            for key, value in user_cfg.items():
                if isinstance(value, dict) and isinstance(cfg.get(key), dict):
                    cfg[key].update(value)
                else:
                    cfg[key] = value
            cfg["_config_path"] = str(cand)
            break
    return cfg


def state_paths(cfg: Dict[str, Any]) -> Dict[str, Path]:
    d = expand(cfg["state_dir"])
    return {"dir": d, "state": d / "state.json", "backups": d / "backups",
            "log": expand(cfg.get("log_file") or (d / "bmsync.log")),
            "lock": d / "bmsync.lock", "watch_pid": d / "watch.pid",
            "bridge": d / "bridge.json"}


def bridge_state_path(cfg: Dict[str, Any]) -> Path:
    return state_paths(cfg)["bridge"]


def bridge_active(cfg: Dict[str, Any]) -> bool:
    """Chrome 扩展是否在线（最近有心跳）。在线时由扩展写 Chrome，bmsync 不再改它的文件。"""
    path = bridge_state_path(cfg)
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
        age = now_ts() - float(info.get("last_seen", 0))
        return age <= float((cfg.get("watch") or {}).get("bridge_timeout", 90.0))
    except (OSError, ValueError, TypeError):
        return False


def empty_state() -> Dict[str, Any]:
    return {"version": STATE_VERSION, "base": {"items": {}, "children": {}},
            "history": [], "tombstones": {}}


def load_state(sp: Dict[str, Path]) -> Dict[str, Any]:
    p = sp["state"]
    if not p.exists():
        return empty_state()
    try:
        state = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        eprint("状态文件损坏，按首次同步处理：%s" % exc)
        return empty_state()
    state.setdefault("base", {"items": {}, "children": {}})
    state.setdefault("history", [])
    state.setdefault("tombstones", {})
    return state


def save_state(sp: Dict[str, Path], state: Dict[str, Any], tree: Tree,
               summary: Dict[str, Any], tombstones: Iterable[Any]) -> None:
    """
    tombstones 支持两种形式：id 字符串，或 (id, url) 元组。
    记录 URL 是必要的：书签被删掉后，它的条目也随之从基线移除，
    只留 id 的话就再也拼不回"被删的是哪个 URL"（桥接要靠它判断能否从 Chrome 删除）。
    """
    state["version"] = STATE_VERSION
    state["base"] = {
        "items": {i: it.to_state() for i, it in tree.items.items()},
        "children": {k: list(v) for k, v in tree.children.items()},
    }
    state["saved_at"] = now_ts()
    state["saved_at_human"] = fmt_ts(state["saved_at"])
    tomb = state.setdefault("tombstones", {})
    stamp = now_ts()
    for entry in tombstones:
        if isinstance(entry, (tuple, list)) and len(entry) == 2:
            item_id, url = entry
            tomb[item_id] = {"ts": stamp, "url": url}
        else:
            tomb[entry] = {"ts": stamp, "url": None}
    cutoff = stamp - TOMBSTONE_TTL

    def alive(rec: Any) -> bool:
        if isinstance(rec, (int, float)):          # 旧格式：直接是时间戳
            return rec > cutoff
        return isinstance(rec, dict) and float(rec.get("ts", 0)) > cutoff

    state["tombstones"] = {k: v for k, v in tomb.items() if alive(v)}
    history = state.setdefault("history", [])
    history.append({"ts": now_ts(), "human": fmt_ts(now_ts()), "summary": summary})
    state["history"] = history[-50:]
    atomic_write(sp["state"], json.dumps(state, ensure_ascii=False, indent=1).encode("utf-8"))


def log_failure(sp: Dict[str, Path], reason: str, code: int) -> None:
    """把失败原因写进 bmsync.log。定时任务跑失败时这是唯一的线索。"""
    append_log(sp, ["[%s] 同步失败（退出码 %d）：%s" % (fmt_ts(now_ts()), code, reason)])


@contextmanager
def file_lock(path: Path, blocking: bool = False):
    """基于 flock 的单实例锁。拿不到锁就抛 RuntimeError（非阻塞模式）。"""
    import fcntl
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        yield fh
    except BlockingIOError:
        raise RuntimeError("另一个 bmsync 实例正在运行（锁文件：%s）" % path)
    finally:
        try:
            import fcntl as _f
            _f.flock(fh.fileno(), _f.LOCK_UN)
        except OSError:
            pass
        fh.close()


def append_log(sp: Dict[str, Path], lines: Sequence[str]) -> None:
    try:
        sp["log"].parent.mkdir(parents=True, exist_ok=True)
        with open(sp["log"], "a", encoding="utf-8") as fh:
            for line in lines:
                fh.write(line.rstrip("\n") + "\n")
    except OSError:
        pass


def _backup_name(path: Path) -> str:
    return "chrome" if path.name == "Bookmarks" else safe_filename(path.parent.name or path.name)


def prune_backups(backups_dir: Path, tag: str, keep: int = 20) -> List[str]:
    """
    分级保留：越旧的备份留得越稀，既不爆量又能覆盖"最近"和"久远"两种回滚需求。

      最近 24 小时   → 全部保留（按内容去重后本来就不多）
      24 小时 ~ 7 天 → 每小时留 1 份
      7 ~ 30 天      → 每天留 1 份
      30 天以上      → 只留最近 keep 份

    文件名格式：<tag>.<时间戳>-<内容哈希前12位><后缀>
    """
    files = sorted(backups_dir.glob("%s.*" % tag))
    parsed = []
    for f in files:
        m = re.match(r"^%s\.(\d{8}-\d{6})-([0-9a-f]{12})" % re.escape(tag), f.name)
        try:
            ts = datetime.strptime(m.group(1), "%Y%m%d-%H%M%S").timestamp() if m else f.stat().st_mtime
        except (ValueError, OSError):
            ts = f.stat().st_mtime
        parsed.append((ts, f))
    parsed.sort()
    now = now_ts()
    hour, day = 3600, 86400
    kept: set = set()
    seen_hours: set = set()
    seen_days: set = set()
    for ts, f in reversed(parsed):            # 从新到旧扫描
        age = now - ts
        if age <= day:
            kept.add(f)
        elif age <= 7 * day:
            key = int(ts // hour)
            if key not in seen_hours:
                seen_hours.add(key)
                kept.add(f)
        elif age <= 30 * day:
            key = int(ts // day)
            if key not in seen_days:
                seen_days.add(key)
                kept.add(f)
    older = [f for ts, f in parsed if now - ts > 30 * day]
    kept.update(older[-keep:] if keep > 0 else [])
    removed = []
    for _, f in parsed:
        if f not in kept:
            try:
                f.unlink()
                removed.append(f.name)
            except OSError:
                pass
    return removed


# 判定"实质内容"时忽略的易变字段。
# Chrome 每次保存书签都会重写 checksum / date_last_used / date_added 等；
# Safari 的 plist 则会被系统周期性重写（ModificationDate、SyncData 等）。
# 这些字段变了不代表书签变了 —— 否则光是切个标签页就会触发一次同步。
VOLATILE_KEYS = frozenset({
    "checksum", "sync_metadata", "date_last_used", "date_modified", "date_added",
    "meta_info", "id", "guid",
    "WebBookmarkModificationDate", "WebBookmarkSyncData", "WebBookmarkSync",
    "SyncData", "WebBookmarkOrderIndex", "WebBookmarkOrder", "WebBookmarkServerID",
    "WebBookmarkParentID", "WebBookmarkExternalID", "WebBookmarkSyncKey",
    "WebBookmarkSyncBlob", "WebBookmarkTitleLastModified",
})

# 只对这些字段算指纹：它们才是"用户看得见的内容"。
# 刻意排除 WebBookmarkUUID / WebBookmarkSpecialID —— 它们是 Safari 的内部标识，
# 会被系统重写时重新分配，但不代表书签内容有任何变化。
CONTENT_KEYS = ("WebBookmarkType", "URLString", "Title", "URIDictionary",
                "Children", "type", "url", "name", "roots")


def _strip_volatile(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _strip_volatile(v) for k, v in obj.items() if k not in VOLATILE_KEYS}
    if isinstance(obj, (list, tuple)):
        return [_strip_volatile(v) for v in obj]
    return obj


def content_fingerprint(data: bytes) -> str:
    """
    对"书签的实质内容"算指纹，忽略易变字段。用于两条判断：
      1. 备份去重：内容没变就不新增备份；
      2. 实时同步触发：书签没变就不空转同步（浏览器切标签页也会重写文件）。
    解析失败时退化为对原始字节取哈希（宁可多同步，不能漏）。
    """
    if not data:
        return hashlib.sha256(b"").hexdigest()
    stripped: Any = None
    try:
        obj = json.loads(data.decode("utf-8"))
        stripped = _strip_volatile(obj) if isinstance(obj, dict) and "roots" in obj else obj
    except (ValueError, UnicodeDecodeError):
        try:
            obj = plistlib.loads(data)
        except Exception:  # noqa: BLE001
            return hashlib.sha256(data).hexdigest()
        stripped = _plist_content(obj) if isinstance(obj, dict) and "Children" in obj else obj
    return hashlib.sha256(json_dumps_compact(stripped).encode("utf-8")).hexdigest()


def _plist_content(node: Any) -> Any:
    """把 Safari 的 plist 归约成"只含内容字段"的结构，便于跨版本稳定比较。"""
    if isinstance(node, dict):
        out = {}
        for key in CONTENT_KEYS:
            if key in node:
                out[key] = _plist_content(node[key])
        return out
    if isinstance(node, (list, tuple)):
        return [_plist_content(v) for v in node]
    return node


def content_diff(old: Any, new: Any, path: str = "", out: Optional[List[str]] = None,
                 limit: int = 40) -> List[str]:
    """比较两份"已剥离易变字段"的内容，列出差异位置。用于诊断"到底什么在变"。"""
    out = out if out is not None else []
    if len(out) >= limit:
        return out
    if type(old) is not type(new):
        out.append("%s: 类型变化 %s → %s" % (path or "/", type(old).__name__, type(new).__name__))
        return out
    if isinstance(old, dict):
        for key in sorted(set(old) | set(new)):
            if key not in old:
                out.append("%s/%s: 新增" % (path, key))
            elif key not in new:
                out.append("%s/%s: 消失" % (path, key))
            else:
                content_diff(old[key], new[key], "%s/%s" % (path, key), out, limit)
    elif isinstance(old, list):
        if len(old) != len(new):
            out.append("%s: 数量 %d → %d" % (path or "/", len(old), len(new)))
        for i in range(min(len(old), len(new))):
            content_diff(old[i], new[i], "%s[%d]" % (path, i), out, limit)
    elif old != new:
        out.append("%s: %r → %r" % (path or "/", str(old)[:60], str(new)[:60]))
    return out


def normalized_content(data: bytes) -> Any:
    """把原始字节归约成"只含内容字段"的结构（JSON 或 plist 都能处理）。"""
    try:
        obj = json.loads(data.decode("utf-8"))
        return _strip_volatile(obj) if isinstance(obj, dict) and "roots" in obj else obj
    except (ValueError, UnicodeDecodeError):
        obj = plistlib.loads(data)
        return _plist_content(obj) if isinstance(obj, dict) and "Children" in obj else obj


def file_content_fingerprint(path: Path, cache: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """
    文件内容的实质指纹。带缓存：先看 (size, mtime_ns)，没变就直接复用上次结果，
    避免每次轮询都解析整个书签文件。
    """
    try:
        st = path.stat()
    except OSError:
        return None
    stamp = (st.st_size, st.st_mtime_ns)
    if cache is not None:
        cached = cache.get(str(path))
        if cached and cached[0] == stamp:
            return cached[1]
    try:
        digest = content_fingerprint(path.read_bytes())
    except OSError:
        return None
    if cache is not None:
        cache[str(path)] = (stamp, digest)
    return digest


def backup_file(path: Path, backups_dir: Path, keep: int = 20,
                content: Optional[bytes] = None,
                min_interval: float = 0.0) -> Tuple[Optional[Path], bool]:
    """
    写盘前备份。返回 (备份路径, 是否真的新建了备份)。

    关键改进：**内容没变就不备份**。原先每次同步都无条件复制一份，实时模式下每 20 秒
    就会多出两个文件；而同步本身是幂等的，绝大多数次的内容完全相同，那些备份毫无价值。
    """
    if not path.exists():
        return None, False
    backups_dir.mkdir(parents=True, exist_ok=True)
    tag = _backup_name(path)
    try:
        data = content if content is not None else path.read_bytes()
    except OSError as exc:
        eprint("备份失败（继续执行）：%s：%s" % (path, exc))
        return None, False
    digest = content_fingerprint(data)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = backups_dir / ("%s.%s-%s%s" % (tag, stamp, digest[:12], path.suffix or ".bak"))

    # 与最近一份备份"实质内容"相同 → 直接复用，不新增
    latest = None
    for existing in sorted(backups_dir.glob("%s.*" % tag), reverse=True):
        if existing.is_file() and not existing.name.startswith("."):
            latest = existing
            break
    if latest is not None:
        try:
            if content_fingerprint(latest.read_bytes()) == digest:
                return latest, False
        except OSError:
            pass
        # 实质内容不同、但距上次备份太近（浏览器在频繁改写文件）→ 先不备份，
        # 等到间隔够了再留一份，避免实时模式把备份目录刷爆
        if min_interval > 0:
            try:
                if now_ts() - latest.stat().st_mtime < min_interval:
                    return latest, False
            except OSError:
                pass
    try:
        atomic_write(dest, data, keep_mode_from=path)
    except OSError as exc:
        eprint("备份失败（继续执行）：%s：%s" % (dest, exc))
        return None, False
    prune_backups(backups_dir, tag, keep)
    return dest, True


# --------------------------------------------------------------------------------------
# 权限 / 进程检测
# --------------------------------------------------------------------------------------


def is_process_running(names: Sequence[str]) -> Optional[str]:
    try:
        out = subprocess.run(["ps", "-Ao", "comm="], capture_output=True, text=True, timeout=10).stdout
    except Exception:  # noqa: BLE001
        return None
    for line in out.splitlines():
        base = os.path.basename(line.strip())
        if base in names:
            return base
    return None


def check_readable(path: Path) -> Tuple[bool, str]:
    if not path.exists():
        return False, "文件不存在"
    try:
        with open(path, "rb") as fh:
            fh.read(1)
    except PermissionError:
        return False, ("无权限读取（macOS TCC 保护：请在“系统设置 → 隐私与安全性 → "
                       "完全磁盘访问权限”中勾选运行本脚本的程序）")
    except OSError as exc:
        return False, "读取失败：%s" % exc
    return True, "可读"


def check_writable(path: Path) -> Tuple[bool, str]:
    if not path.exists():
        parent = path.parent
        if parent.exists() and os.access(parent, os.W_OK):
            return True, "文件不存在，目录可写（将新建）"
        return False, "文件与目录都不可写"
    if os.access(path, os.W_OK) and os.access(path.parent, os.W_OK):
        return True, "可写"
    return False, "不可写（权限或 TCC 限制）"


# --------------------------------------------------------------------------------------
# 导出 / 导入
# --------------------------------------------------------------------------------------


def tree_to_html(tree: Tree, title: str = "Bookmarks") -> str:
    lines = [
        "<!DOCTYPE NETSCAPE-Bookmark-file-1>",
        "<!-- This is an automatically generated file. DO NOT EDIT! -->",
        '<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">',
        "<TITLE>%s</TITLE>" % html_escape(title),
        "<H1>%s</H1>" % html_escape(title),
        "<DL><p>",
    ]

    def emit(item_id: str, indent: int) -> None:
        it = tree.items.get(item_id)
        if it is None:
            return
        pad = "    " * indent
        if it.kind == "bookmark":
            lines.append('%s<DT><A HREF="%s" ADD_DATE="%d">%s</A>' % (
                pad, html_escape(clean_url_for_write(it.url), quote=True),
                int(it.mtime) if it.mtime else int(now_ts()), html_escape(it.name)))
        else:
            lines.append("%s<DT><H3>%s</H3>" % (pad, html_escape(it.name)))
            lines.append("%s<DL><p>" % pad)
            for cid in tree.children_of(item_id):
                emit(cid, indent + 1)
            lines.append("%s</DL><p>" % pad)

    for cid in CONTAINERS:
        for child in tree.children_of(cid):
            emit(child, 1)
    lines.append("</DL><p>")
    return "\n".join(lines) + "\n"


def tree_to_plain(tree: Tree) -> Dict[str, Any]:
    def conv(item_id: str) -> Dict[str, Any]:
        it = tree.items[item_id]
        if it.kind == "bookmark":
            return {"type": "bookmark", "name": it.name, "url": it.url,
                    "modified": fmt_ts(it.mtime) if it.mtime else None}
        return {"type": "folder", "name": it.name,
                "children": [conv(c) for c in tree.children_of(item_id)]}

    return {"generator": "bmsync %s" % __version__, "exported": fmt_ts(now_ts()),
            "roots": [{"name": CONTAINER_LABELS[cid], "children": [conv(c) for c in tree.children_of(cid)]}
                      for cid in CONTAINERS]}


def parse_html_bookmarks(path: Path) -> Mid:
    from html.parser import HTMLParser

    def parse_add_date(v: Optional[str]) -> Optional[float]:
        try:
            return float(v) if v else None
        except ValueError:
            return None

    class P(HTMLParser):
        """
        解析 Netscape 书签格式。两个坑：
          1. HTMLParser 会在元素自身文本之前触发 </h3>，所以标题必须在 handle_data 里取；
          2. 标题取到后不能立刻清空 pending —— 紧随其后的 <DL> 还要靠它判断"进入这个目录"。
        因此 pending 只在 <DT>/<DD>/下一个 <H3>/<A> 到来时才清空。
        """

        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.root = Mid(kind="folder", name="bookmarks")
            self.stack: List[Mid] = [self.root]
            self.pending: Optional[Mid] = None

        @property
        def current(self) -> Mid:
            return self.stack[-1]

        def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
            tag = tag.lower()
            a = {k.lower(): (v or "") for k, v in attrs}
            if tag in ("dt", "dd"):
                self.pending = None
            elif tag == "h3":
                self.pending = None
                folder = Mid(kind="folder", name="", mtime=parse_add_date(a.get("add_date")))
                self.current.children.append(folder)
                self.pending = folder
            elif tag == "a":
                self.pending = None
                node = Mid(kind="bookmark", name="", url=a.get("href", ""),
                           mtime=parse_add_date(a.get("add_date")))
                self.current.children.append(node)
                self.pending = node
            elif tag == "dl":
                if self.pending is not None and self.pending.kind == "folder":
                    self.stack.append(self.pending)   # 进入刚出现的目录
                    self.pending = None
                else:
                    self.stack.append(self.current)   # 保持当前层级

        def handle_endtag(self, tag: str) -> None:
            if tag.lower() == "dl" and self.stack:
                self.stack.pop()

        def handle_data(self, data: str) -> None:
            # 只填标题，不清空 pending：紧接着的 <DL> 还要靠它判断"进入这个目录"
            if self.pending is not None and not self.pending.name:
                text = data.strip()
                if text:
                    self.pending.name = text

    parser = P()
    parser.feed(path.read_text(encoding="utf-8", errors="replace"))

    def prune(node: Mid) -> None:
        node.children = [c for c in node.children if c.kind == "bookmark" or c.children]
        for c in node.children:
            prune(c)

    prune(parser.root)
    return parser.root


def parse_json_bookmarks(path: Path) -> Mid:
    data = json.loads(path.read_text(encoding="utf-8"))

    def wrap(node: Dict[str, Any]) -> Optional[Mid]:
        if not isinstance(node, dict):
            return None
        if node.get("type") == "url" or "url" in node or "URLString" in node:
            return Mid(kind="bookmark", name=node.get("name") or node.get("Title") or "",
                       url=node.get("url") or node.get("URLString") or "")
        out = Mid(kind="folder", name=node.get("name") or node.get("Title") or "")
        for child in node.get("children") or node.get("Children") or []:
            sub = wrap(child)
            if sub is not None:
                out.children.append(sub)
        return out

    root = Mid(kind="folder", name="imported")
    if isinstance(data, dict) and isinstance(data.get("roots"), list):
        for node in data["roots"]:
            sub = wrap(node)
            if sub is not None:
                root.children.append(sub)
    elif isinstance(data, dict) and isinstance(data.get("roots"), dict):
        for key, name in (("bookmark_bar", "bar"), ("other", "other"), ("synced", "mobile")):
            node = data["roots"].get(key)
            if isinstance(node, dict):
                folder = wrap(node)
                if folder is not None:
                    folder.name = name
                    root.children.append(folder)
    elif isinstance(data, list):
        for node in data:
            sub = wrap(node)
            if sub is not None:
                root.children.append(sub)
    else:
        sub = wrap(data)
        if sub is not None:
            root.children.append(sub)
    return root


# --------------------------------------------------------------------------------------
# 命令实现
# --------------------------------------------------------------------------------------


def build_backends(cfg: Dict[str, Any]) -> Tuple[ChromeBackend, SafariBackend]:
    chrome = ChromeBackend(expand(cfg["chrome"]["user_data_dir"]), cfg["chrome"].get("profile", "Default"))
    safari_path = expand(cfg["safari"]["bookmarks_path"])
    if not safari_path.exists():
        for cand in SAFARI_BOOKMARK_PATHS:
            p = HOME / cand
            if p.exists():
                safari_path = p
                break
    return chrome, SafariBackend(safari_path)


def cmd_doctor(cfg: Dict[str, Any], args: argparse.Namespace) -> int:
    chrome, safari = build_backends(cfg)
    sp = state_paths(cfg)
    ok = True

    print("bmsync %s 环境自检" % __version__)
    print("-" * 70)
    print("配置文件   : %s" % cfg.get("_config_path", "（内置默认值）"))
    print("状态目录   : %s" % sp["dir"])

    print("\n[Chrome]")
    print("  User Data: %s" % chrome.user_data_dir)
    print("  配置目录 : %s" % chrome.profile)
    readable, msg_r = check_readable(chrome.bookmarks_path)
    writable, msg_w = check_writable(chrome.bookmarks_path)
    print("  Bookmarks: %s" % chrome.bookmarks_path)
    print("  读取     : %s（%s）" % ("正常" if readable else "失败", msg_r))
    print("  写入     : %s（%s）" % ("正常" if writable else "失败", msg_w))
    if chrome.user_data_dir.exists():
        profiles = sorted(p.name for p in chrome.user_data_dir.iterdir()
                          if p.is_dir() and (p / "Bookmarks").exists())
        print("  可用配置 : %s" % (", ".join(profiles) or "（未发现）"))
    ok = ok and readable and writable

    print("\n[Safari]")
    print("  Bookmarks: %s" % safari.bookmarks_path)
    readable_s, msg_rs = check_readable(safari.bookmarks_path)
    writable_s, msg_ws = check_writable(safari.bookmarks_path)
    print("  读取     : %s（%s）" % ("正常" if readable_s else "失败", msg_rs))
    print("  写入     : %s（%s）" % ("正常" if writable_s else "失败", msg_ws))
    if not readable_s:
        print("  ↳ 处理办法：系统设置 → 隐私与安全性 → 完全磁盘访问权限 → 打开开关并添加")
        print("     运行本脚本的程序（Terminal / iTerm / VS Code / DeepSeek Harness 等），")
        print("     然后完全退出并重新启动该程序。注意：这不是 chmod/sudo 能解决的，属于 TCC。")
    ok = ok and readable_s and writable_s

    print("\n[进程状态]")
    for label, names in (("Chrome", CHROMIUM_PROCS), ("Safari", ["Safari"])):
        running = is_process_running(names)
        print("  %-6s: %s" % (label, ("运行中（%s）—— 同步前需退出" % running) if running else "未运行"))

    print("\n[备份与定时]")
    print("  备份目录 : %s" % sp["backups"])
    print("  日志     : %s" % sp["log"])
    installed = launchd_plist_path(LAUNCHD_LABEL).exists()
    print("  定时任务 : %s" % ("已安装（%s）" % launchd_plist_path(LAUNCHD_LABEL) if installed
                               else "未安装（bmsync install-scheduler）"))
    print("-" * 70)
    print("结论：%s" % ("一切就绪，可以运行 bmsync sync" if ok else "存在权限问题，请按上面提示处理后再同步"))
    return 0 if ok else 2


def cmd_status(cfg: Dict[str, Any], args: argparse.Namespace) -> int:
    chrome, safari = build_backends(cfg)
    sp = state_paths(cfg)
    state = load_state(sp)
    print("bmsync %s 状态" % __version__)
    print("-" * 70)
    problems: List[str] = []
    for backend in (chrome, safari):
        readable, msg = check_readable(backend.bookmarks_path)
        if not readable:
            problems.append("%s：%s" % (backend.label, msg))
            print("%-7s: 无法读取 —— %s" % (backend.label, msg))
            continue
        try:
            tree = backend.read_tree(skip_folders=cfg["sync"].get("skip_folders") or [])
            st = tree.stats()
            print("%-7s: %d 个书签 / %d 个目录   %s" % (
                backend.label, st["bookmarks"], st["folders"], backend.bookmarks_path))
        except Exception as exc:  # noqa: BLE001
            problems.append("%s：解析失败 %s" % (backend.label, exc))
            print("%-7s: 解析失败 —— %s" % (backend.label, exc))
    base = state.get("base", {}).get("items", {})
    print("\n基线记录 : %d 个条目" % len(base))
    print("上次同步 : %s" % state.get("saved_at_human", "从未同步"))
    for record in (state.get("history") or [])[-3:]:
        s = record.get("summary", {})
        print("  · %s  chrome +%s ~%s -%s | safari +%s ~%s -%s" % (
            record.get("human", "?"), s.get("chrome_add", 0), s.get("chrome_upd", 0),
            s.get("chrome_del", 0), s.get("safari_add", 0), s.get("safari_upd", 0), s.get("safari_del", 0)))
    if problems:
        print("\n提示：%s" % "；".join(problems))
        print("      详见 bmsync doctor")
    return 0


def cmd_export(cfg: Dict[str, Any], args: argparse.Namespace) -> int:
    chrome, safari = build_backends(cfg)
    out_dir = expand(args.out) if args.out else (state_paths(cfg)["dir"] / "export")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    made: List[Path] = []
    for name in (args.browser or ["chrome", "safari"]):
        backend = chrome if name == "chrome" else safari
        readable, msg = check_readable(backend.bookmarks_path)
        if not readable:
            eprint("跳过 %s：%s" % (backend.label, msg))
            continue
        tree = backend.read_tree(skip_folders=cfg["sync"].get("skip_folders") or [])
        if args.format == "html":
            target = out_dir / ("%s-%s.html" % (name, stamp))
            atomic_write(target, tree_to_html(tree, "%s bookmarks" % backend.label).encode("utf-8"))
        else:
            target = out_dir / ("%s-%s.json" % (name, stamp))
            atomic_write(target, json.dumps(tree_to_plain(tree), ensure_ascii=False, indent=2).encode("utf-8"))
        made.append(target)
        print("已导出 %-6s → %s" % (backend.label, target))
    if not made:
        return 1
    print("\n导出的 HTML 可在浏览器里手动导入：Chrome 书签管理器 → 导入书签；Safari 文件 → 导入书签。")
    return 0


def cmd_import(cfg: Dict[str, Any], args: argparse.Namespace) -> int:
    """把外部导出的书签文件（HTML/JSON）并入两个浏览器。"""
    src = expand(args.file)
    if not src.exists():
        eprint("文件不存在：%s" % src)
        return 1
    fmt = args.format
    if fmt == "auto":
        fmt = "html" if src.suffix.lower() in (".html", ".htm") else "json"
    mid = parse_html_bookmarks(src) if fmt == "html" else parse_json_bookmarks(src)
    imported = mid_to_tree(mid, skip_folders=())
    stats = imported.stats()
    print("已解析 %s：%d 个书签 / %d 个目录" % (src, stats["bookmarks"], stats["folders"]))
    if not stats["bookmarks"]:
        eprint("没有解析到任何书签，请确认文件格式（支持浏览器导出的 HTML 或本工具导出的 JSON）。")
        return 1

    chrome, safari = build_backends(cfg)
    sp = state_paths(cfg)
    state = load_state(sp)
    holder = Item(id="import:" + safe_filename(src.stem), kind="folder",
                  name="导入书签", parent_id=OTHER_ID, mtime=now_ts())
    extra = Tree()
    extra.add(holder)
    for item_id in list(imported.items):
        if item_id in CONTAINERS:
            continue
        parent = imported.items[item_id].parent_id
        _add_subtree(extra, imported, item_id, holder.id if parent in CONTAINERS else None)
    print("导入内容会放在「其他书签/%s」下，与现有书签按 URL 去重。" % holder.name)
    return run_sync(cfg, chrome, safari, sp, state, args, extra_sources={"import": extra})


# --------------------------------------------------------------------------------------
# 核心同步
# --------------------------------------------------------------------------------------


def run_sync(cfg: Dict[str, Any], chrome: ChromeBackend, safari: SafariBackend, sp: Dict[str, Path],
             state: Dict[str, Any], args: argparse.Namespace,
             extra_sources: Optional[Dict[str, Tree]] = None) -> int:
    verbose = getattr(args, "verbose", 1) or 0
    dry_run = bool(getattr(args, "dry_run", False))
    direction = getattr(args, "direction", None) or cfg["sync"].get("direction", "both")
    if direction == "chrome2safari" and not getattr(args, "prefer", None):
        prefer = "chrome"        # 单向：一律以源为准
    elif direction == "safari2chrome" and not getattr(args, "prefer", None):
        prefer = "safari"
    else:
        prefer = getattr(args, "prefer", None) or cfg["sync"].get("prefer", "newest")
    skip = cfg["sync"].get("skip_folders") or []
    if getattr(args, "allow_mass_delete", False):
        # 显式确认：把上限抬到无穷（但仍在结果里报告实际删了多少）
        cfg["sync"] = dict(cfg["sync"])
        cfg["sync"]["max_delete_ratio"] = 0
        cfg["sync"]["max_delete_count"] = 0

    # 1) 权限
    for backend in (chrome, safari):
        readable, msg = check_readable(backend.bookmarks_path)
        if not readable:
            reason = "无法读取 %s 书签：%s（%s）" % (backend.label, msg, backend.bookmarks_path)
            eprint("[错误] %s" % reason)
            if backend.is_safari:
                eprint("       解决：系统设置 → 隐私与安全性 → 完全磁盘访问权限 → 勾选运行本脚本的程序，并重启它。")
                eprint("       注意：launchd/cron 拉起的进程也要单独授权（见 README 第 3 节）。")
            log_failure(sp, reason, 2)
            return 2
        if not dry_run:
            writable, msg_w = check_writable(backend.bookmarks_path)
            if not writable:
                reason = "无法写入 %s 书签：%s（%s）" % (backend.label, msg_w, backend.bookmarks_path)
                eprint("[错误] %s" % reason)
                eprint("       若是定时任务报这个错，说明 launchd 没有磁盘访问权限，见 README 第 3 节。")
                log_failure(sp, reason, 2)
                return 2

    # 2) 进程
    if not dry_run and not getattr(args, "force", False) and cfg["sync"].get("require_browsers_closed", True):
        running = []
        for backend in (chrome, safari):
            names = CHROMIUM_PROCS if backend is chrome else ["Safari"]
            proc = is_process_running(names)
            if proc:
                running.append("%s（%s）" % (backend.label, proc))
        if running:
            reason = "浏览器仍在运行：%s" % "、".join(running)
            eprint("[错误] 请先退出：%s" % "、".join(running))
            eprint("       浏览器运行时会用内存中的旧数据覆盖改动；确认无碍可加 --force。")
            log_failure(sp, reason, 3)
            return 3

    # 3) 读取
    chrome_tree = chrome.read_tree(skip_folders=skip)
    safari_tree = safari.read_tree(skip_folders=skip)
    log("Chrome: %d 书签 / %d 目录" % (chrome_tree.stats()["bookmarks"], chrome_tree.stats()["folders"]), verbose)
    log("Safari: %d 书签 / %d 目录" % (safari_tree.stats()["bookmarks"], safari_tree.stats()["folders"]), verbose)

    base = tree_from_state(state["base"]) if state["base"].get("items") else None
    first_run = base is None
    if first_run:
        base = Tree()
        for cid in CONTAINERS:
            base.add(Item(id=cid, kind="folder", name=cid, parent_id=ROOT_ID))
        log("首次同步：以两个浏览器的并集建立基线（不会推断删除）", verbose)

    # 4) 方向：单向同步 = 只把"源"的改动应用到"目标"，目标自己的内容保持不变。
    #    做法是把目标当前内容当作"没有改动"的一方参与合并（源里没有的条目不因此被删）。
    left, right = chrome_tree, safari_tree
    if direction == "chrome2safari":
        right = base.clone()
        for item_id in safari_tree.items:
            if item_id not in right.items:
                _add_subtree(right, safari_tree, item_id, safari_tree.items[item_id].parent_id)
    elif direction == "safari2chrome":
        left = base.clone()
        for item_id in chrome_tree.items:
            if item_id not in left.items:
                _add_subtree(left, chrome_tree, item_id, chrome_tree.items[item_id].parent_id)

    # 5) 非对称同步：一侧给整棵树/指定目录，另一侧全部收进一个目录
    asym_chrome = (getattr(args, "scope_chrome", None) or cfg["sync"].get("scope_chrome") or "").strip()
    asym_safari = (getattr(args, "scope_safari", None) or cfg["sync"].get("scope_safari") or "").strip()
    if asym_chrome or asym_safari:
        return run_sync_asymmetric(cfg, chrome, safari, sp, state, args,
                                   asym_chrome, asym_safari)

    # 5) 合并（可限定在某个目录内进行）
    scope = (getattr(args, "scope", None) or cfg["sync"].get("scope") or "").strip()
    no_delete = bool(cfg["sync"].get("no_delete")) or bool(getattr(args, "no_delete", False))
    scope_ids: Dict[str, Optional[str]] = {"chrome": None, "safari": None}
    scope_first = False
    scope_write_trees: Dict[str, Tree] = {}
    if scope:
        scope_ids["chrome"] = resolve_scope_folder(chrome_tree, scope)
        scope_ids["safari"] = resolve_scope_folder(safari_tree, scope)
        if scope_ids["chrome"] is None and scope_ids["safari"] is None:
            eprint("[错误] 两侧都找不到目录「%s」。请先在两个浏览器里建好同名目录，"
                   "或用 --scope 指定正确的路径（如 \"书签栏/同步\"）。" % scope)
            return 2
        log("同步范围：%s（Chrome %s / Safari %s）" % (
            scope,
            "已找到" if scope_ids["chrome"] else "待创建",
            "已找到" if scope_ids["safari"] else "待创建"), verbose)
        # 两侧都挂到同一个锚点 id 上，便于合并与写回
        anchor = scope_ids["chrome"] or scope_ids["safari"]
        scope_ids["chrome"] = anchor
        scope_ids["safari"] = anchor
        left = scoped_clone(left, anchor)
        right = scoped_clone(right, anchor)
        # scope 变化（或首次启用）时，用当前内容重建基线，避免把范围外的东西当成被删
        scope_first = state.get("scope") != scope or not state.get("base", {}).get("items")
        if scope_first:
            base = Tree()
            base.add(Item(id=anchor, kind="folder",
                          name=(chrome_tree.items.get(anchor) or safari_tree.items.get(anchor)).name
                          if (anchor in chrome_tree.items or anchor in safari_tree.items) else "同步",
                          parent_id=ROOT_ID))
            base.children[ROOT_ID] = [anchor]
            # 把 scope 目录本身及其现有内容作为基线；左右两边内容都并入，
            # 这样第一轮只会"补齐缺失"，不会把范围外的东西带进来
            for src in (left, right):
                for kid in src.children_of(anchor):
                    if kid in src.items and kid not in base.items:
                        _add_subtree(base, src, kid, anchor)
            log("首次在该范围内同步：以两侧并集建立基线，本轮不推断删除", verbose)
        state["scope"] = scope

    # 结构抹平检测：iOS 往返会把目录抹掉，此时绝不能把"目录消失"当成删除
    if not getattr(args, "allow_flatten", False):
        for side, tree in (("Chrome", left), ("Safari", right)):
            flat, why = looks_flattened(tree, base)
            if flat:
                no_delete = True
                msg = ("⚠️ 检测到 %s 的目录结构疑似被抹平（%s）。本轮**不执行任何删除**，"
                       "只做新增/修改。这通常是 iOS Safari 往返或 iCloud 同步造成的，"
                       "不是你真的删了目录。确认要按现状同步请加 --allow-flatten。" % (side, why))
                eprint(msg)
                log("  " + msg, verbose)
                append_log(sp, ["[%s] %s" % (fmt_ts(now_ts()), msg)])
                break

    # 范围首次播种时不能推断删除：基线刚由两侧并集建立，"某一侧没有"只说明还没同步过去，
    # 不代表用户删除（否则会把仅存在于另一侧的书签当删除处理）
    seed_no_delete = bool(scope) and scope_first
    result = merge(base, left, right, prefer=prefer, extra_sources=extra_sources,
                   max_delete_ratio=float(cfg["sync"].get("max_delete_ratio", 0.05) or 0),
                   max_delete_count=int(cfg["sync"].get("max_delete_count", 10) or 0),
                   no_delete=(no_delete or seed_no_delete),
                   scope_anchor=(scope_ids.get("chrome") if scope else None),
                   verbose=verbose)
    merged = result.merged
    counts = {side: Counter(c.kind for c in changes) for side, changes in result.changes.items()}
    plan = {"chrome": plan_changes(merged, chrome_tree), "safari": plan_changes(merged, safari_tree)}

    if scope:
        # 合并只在 scope 内进行，写回前拼回完整树：容器与范围外的内容原样不动
        anchor = scope_ids["chrome"]
        if anchor:
            scope_name = (result.merged.items.get(anchor).name
                          if anchor in result.merged.items else "同步")
            write_chrome = assemble_for_browser(chrome_tree, result.merged, anchor, scope_name)
            write_safari = assemble_for_browser(safari_tree, result.merged, anchor, scope_name)
            # 两侧各自的写盘树（范围外保留各自原样）
            scope_write_trees = {"chrome": write_chrome, "safari": write_safari}
            merged = write_chrome           # merged 仍用于状态与计划展示
        plan = {"chrome": plan_changes(write_chrome if anchor else merged, chrome_tree),
                "safari": plan_changes(write_safari if anchor else merged, safari_tree)}

    summary = {
        "scope": scope, "no_delete": no_delete,
        "chrome_add": counts["chrome"].get("add", 0), "chrome_upd": counts["chrome"].get("update", 0),
        "chrome_del": counts["chrome"].get("delete", 0),
        "safari_add": counts["safari"].get("add", 0), "safari_upd": counts["safari"].get("update", 0),
        "safari_del": counts["safari"].get("delete", 0),
        "merged_bookmarks": merged.stats()["bookmarks"], "merged_folders": merged.stats()["folders"],
        "plan_chrome": len(plan["chrome"]), "plan_safari": len(plan["safari"]),
        "dry_run": dry_run, "direction": direction, "prefer": prefer, "first_run": first_run,
    }

    if verbose:
        print("")
        print("改动汇总（相对上次同步）：")
        for side in ("chrome", "safari"):
            c = counts[side]
            print("  %-6s 新增 %d / 修改 %d / 删除 %d" % (side, c.get("add", 0), c.get("update", 0), c.get("delete", 0)))
        print("  合并结果：%d 书签 / %d 目录" % (merged.stats()["bookmarks"], merged.stats()["folders"]))
        if result.conflicts:
            print("  ⚠ 冲突 %d 处（按 %s 处理）：" % (len(result.conflicts), "修改时间" if prefer == "newest" else prefer))
            for line in result.conflicts[:10]:
                print("     - %s" % line)
        if getattr(args, "list_changes", False):
            for side in ("chrome", "safari"):
                for change in result.changes.get(side, []):
                    sign = {"add": "+", "update": "~", "delete": "-"}[change.kind]
                    print("  [%s] %s %s%s" % (side, sign, change.label,
                                              " (%s)" % change.detail if change.detail else ""))

    # 6) 演练输出
    if dry_run:
        max_show = getattr(args, "max_show", 40)
        print("\n[演练] 不会写入任何文件。将要执行的改动：")
        for side, steps in plan.items():
            print("  %s：%d 处" % (side, len(steps)))
            for step in steps[:max_show]:
                print("     %s" % step)
            if len(steps) > max_show:
                print("     …（其余 %d 处省略，用 --max-show 调整）" % (len(steps) - max_show))
    else:
        # 7) 备份 + 写入
        chrome_handled_by_extension = bridge_active(cfg)
        baseline = merged
        if chrome_handled_by_extension:
            # 扩展负责把改动应用到 Chrome，而它什么时候应用完只有它自己知道。
            # 如果此刻就把 merged 整体记为基线，下一轮会把「Chrome 里还没有的条目」
            # 误判成「被删除」，于是把书签从两边一起抹掉 —— 实测确实会丢数据。
            #
            # 正确语义：基线只记录**两个浏览器都已经有了**的内容（按内容指纹取交集）。
            # 只在一边的东西不算"已同步"，下一轮会再被检测成新增并重试 —— 新增是幂等的，
            # 而且绝不能像删除那样丢数据。
            in_chrome = {it.fingerprint() for it in chrome_tree.items.values()}
            shared = Tree()
            for _cid in CONTAINERS:
                shared.add(Item(id=_cid, kind="folder", name=_cid, parent_id=ROOT_ID))
            for _iid, _item in merged.items.items():
                if _iid in CONTAINERS:
                    continue
                if _item.fingerprint() not in in_chrome:
                    continue          # Chrome 里还没有 → 留到下一轮重试，不写进基线
                if _item.parent_id is None or _item.parent_id not in shared.items:
                    continue
                shared.add(Item(id=_iid, kind=_item.kind, name=_item.name,
                                parent_id=_item.parent_id, url=_item.url, mtime=_item.mtime))
            for _iid, _item in merged.items.items():
                if _iid in CONTAINERS or _iid in shared.items:
                    continue
                if _item.kind == "folder" and _item.parent_id in shared.items:
                    shared.add(Item(id=_iid, kind="folder", name=_item.name,
                                    parent_id=_item.parent_id, mtime=_item.mtime))
            # 注意：写盘仍然用完整的 merged（否则会把 Safari 自己新增、还没进 Chrome 的
            # 书签一起删掉）；只有"记基线"才用交集树。
            baseline = shared
        keep = cfg["sync"].get("backup_keep", 20)
        backups = []
        min_interval = float(cfg["sync"].get("backup_min_interval", 300) or 0)
        for backend in (chrome, safari):
            b, created = backup_file(backend.bookmarks_path, sp["backups"], keep,
                                     min_interval=min_interval)
            if b:
                backups.append("%s%s" % (b, "" if created else "（内容未变，复用已有备份）"))
        try:
            # direction 表示"以谁为源"：chrome2safari 只写目标 Safari，safari2chrome 只写目标 Chrome
            if direction != "chrome2safari":
                if chrome_handled_by_extension:
                    # 扩展在线 → Chrome 由它通过官方书签 API 应用改动。
                    # 此时若再去改 Bookmarks 文件，会和扩展互相覆盖（Chrome 的内存内容最终胜出）。
                    log("  Chrome 写入：交给 Chrome 扩展（桥接在线，直接改文件会被浏览器覆盖）", verbose)
                else:
                    tree_c = scope_write_trees.get("chrome", merged)
                    steps_c = chrome.write_tree(tree_c, chrome.build_native_map(chrome.read_raw(), tree_c))
                    log("  Chrome 写入：%s" % ("；".join(steps_c) or "无变化"), verbose)
            if direction != "safari2chrome":
                steps_s = safari.write_tree(scope_write_trees.get("safari", merged))
                log("  Safari 写入：%s" % ("；".join(steps_s) or "无变化"), verbose)
        except Exception:
            eprint("[错误] 写入失败，原文件已备份在：%s" % sp["backups"])
            raise

        save_state(sp, state, baseline, summary, result.tombstones)
        lines = ["[%s] %s" % (fmt_ts(now_ts()), json.dumps(summary, ensure_ascii=False))]
        for side, steps in plan.items():
            for step in steps:
                lines.append("    %-7s %s" % (side, step))
        for b in backups:
            lines.append("    backup  %s" % b)
        append_log(sp, lines)
        log("\n状态已保存：%s" % sp["state"], verbose)
        log("改动记录已写入：%s（定时任务就是靠这里确认是否生效）" % sp["log"], verbose)
        if backups:
            log("备份：%s" % "、".join(backups), verbose)

    if getattr(args, "json", False):
        print(json.dumps({"summary": summary, "conflicts": result.conflicts,
                          "plan": plan,
                          "changes": {side: [c.to_dict() for c in changes]
                                      for side, changes in result.changes.items()}},
                         ensure_ascii=False, indent=2))
    return 0


def cmd_sync(cfg: Dict[str, Any], args: argparse.Namespace) -> int:
    chrome, safari = build_backends(cfg)
    sp = state_paths(cfg)
    state = load_state(sp)
    return run_sync(cfg, chrome, safari, sp, state, args)


# --------------------------------------------------------------------------------------
# 本地桥接：让运行中的 Chrome 也能被写入（通过配套扩展走官方书签 API）
# --------------------------------------------------------------------------------------
#
# 为什么需要它：Chrome 把书签放在内存里，退出/保存时整份重写 Bookmarks 文件。
# 直接改文件对运行中的 Chrome 无效 —— 几秒后就被内存内容覆盖。唯一可靠的办法是用
# Chrome 官方的 chrome.bookmarks API，也就是一个扩展。这里起一个只监听 127.0.0.1
# 的小 HTTP 服务，扩展定时拉取"需要新增/删除哪些书签"并应用。

BRIDGE_PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>bmsync 桥接</title>
<style>body{font:14px/1.6 -apple-system,sans-serif;max-width:640px;margin:40px auto;padding:0 20px}
code{background:#f2f2f2;padding:1px 5px;border-radius:3px}</style></head><body>
<h2>bmsync 本地桥接运行中</h2>
<p>这个端口是给 Chrome 扩展用的，只监听本机回环地址，不对外暴露。</p>
<ul>
<li><code>GET /ping</code> —— 扩展心跳</li>
<li><code>GET /delta</code> —— 取出"需要应用到 Chrome 的书签增删"</li>
<li><code>GET /recent</code> —— 最近变动（待应用 + 同步历史 + 扩展回报）</li>
<li><code>POST /applied</code> —— 扩展报告已应用</li>
</ul>
<p>浏览器里打开这个页面通常说明你误点了扩展的地址；关掉即可。</p>
</body></html>"""


def _bridge_containers(tree: Tree) -> Dict[str, List[Any]]:
    """把统一树里各容器的书签整理成扩展能直接用的形式。"""
    out: Dict[str, List[Any]] = {}
    for cid, label in CONTAINER_LABELS.items():
        items = []
        for item_id in tree.subtree_ids(cid):
            it = tree.items.get(item_id)
            if it is None or it.kind != "bookmark":
                continue
            path = [CONTAINER_LABELS.get(p, tree.items[p].name)
                    for p in tree.path_ids(item_id)[1:-1]
                    if p in tree.items and p not in CONTAINERS]
            items.append({"title": it.name, "url": it.url, "path": path})
        out[label] = items
    return out


def _bridge_delta(sp: Dict[str, Path], cfg: Dict[str, Any]) -> Dict[str, Any]:
    """算出"Chrome 扩展需要执行什么"：相对 Chrome 当前树，多了哪些、少了哪些。"""
    state = load_state(sp)
    merged = tree_from_state(state["base"]) if state["base"].get("items") else Tree()
    chrome, _ = build_backends(cfg)
    try:
        current = chrome.read_tree(skip_folders=cfg["sync"].get("skip_folders") or [])
    except Exception as exc:  # noqa: BLE001
        return {"error": "无法读取 Chrome 书签：%s" % exc, "add": [], "remove": [], "rename": []}

    def rel_path(tree: Tree, item_id: str) -> List[str]:
        """书签相对容器的路径（含容器名），例如 ["书签栏","工具"]。"""
        names = []
        for pid in tree.path_ids(item_id):
            if pid in CONTAINERS:
                names.append(CONTAINER_LABELS.get(pid, pid))
            else:
                node = tree.items.get(pid)
                names.append(node.name if node else pid)
        return names

    want: Dict[str, Any] = {}          # 规范化 URL -> 期望的书签
    for item_id, node in merged.items.items():
        if node.kind == "bookmark" and node.url:
            want.setdefault(norm_url(node.url), node)
    have: Dict[str, Any] = {}          # 规范化 URL -> Chrome 现有的书签
    for item_id, node in current.items.items():
        if node.kind == "bookmark" and node.url:
            have.setdefault(norm_url(node.url), node)

    # 容器一律用稳定标识（Chrome 的固定文件夹 ID：1=书签栏，2=其他书签）。
    # 不能用标题：Chrome 的文件夹标题随界面语言变化（英文是 "Bookmarks bar"），
    # 扩展按标题找容器会找不到、把书签塞错地方甚至静默失败。
    def container_and_path(tree: Tree, path_names: List[str]) -> Tuple[str, List[str]]:
        head = path_names[0] if path_names else CONTAINER_LABELS[OTHER_ID]
        if head == CONTAINER_LABELS[BAR_ID]:
            return "bar", path_names[1:]
        return "other", path_names[1:]

    add, rename = [], []
    for url, node in want.items():
        full = rel_path(merged, node.id)
        container, path = container_and_path(merged, full[:-1] if full else [])
        if url not in have:
            add.append({"title": node.name, "url": node.url, "container": container, "path": path})
        elif norm_name(have[url].name) != norm_name(node.name):
            rename.append({"url": node.url, "title": node.name, "currentTitle": have[url].name})
    # 删除必须极其保守：只有"上次同步时两边都确认过（在基线里）"、而这次 Safari 那边
    # 确实已经没有了的书签，才允许从 Chrome 删除。
    #
    # 为什么不能只比"Chrome 有、merged 没有"：merged 是从**持久化基线**算出来的，而基线
    # 是上一次同步结束才写盘的。用户刚在 Chrome 里加的书签会先落进 Chrome 文件，此时基线
    # 还不知道它 —— 若据此判定删除，扩展就会把用户刚加的书签删掉（实测发生过，日志里
    # 表现为 同步 safari_del:1 → 扩展 remove:1）。加上"必须在基线里"这个前提就安全了：
    # 未同步过的东西只会被"新增/保留"，永远不会被删。
    state = load_state(sp)
    base_items = state.get("base", {}).get("items") or {}
    dead_urls = set()
    for item_id, rec in (state.get("tombstones") or {}).items():
        url = rec.get("url") if isinstance(rec, dict) else None
        if not url and item_id in base_items:       # 旧格式：借基线里的条目补出 URL
            url = base_items[item_id].get("url")
        if url:
            dead_urls.add(norm_url(url))
    remove = []
    for url, node in have.items():
        if url in want or url not in dead_urls:
            continue
        full = rel_path(current, node.id)
        container, path = container_and_path(current, full[:-1] if full else [])
        remove.append({"url": node.url, "title": node.name, "container": container, "path": path})

    # 同样的熔断：即使基线被污染，也不允许一次让扩展删掉一大批书签
    base_size = max(1, sum(1 for v in base_items.values() if v.get("kind") == "bookmark"))
    ratio_limit = float((cfg.get("sync") or {}).get("max_delete_ratio", 0.05) or 0)
    count_limit = int((cfg.get("sync") or {}).get("max_delete_count", 10) or 0)
    allowed = int(base_size * ratio_limit) if ratio_limit > 0 else 0
    allowed = max(allowed, count_limit)
    blocked = 0
    if remove and allowed > 0 and len(remove) > allowed:
        blocked = len(remove)
        append_log(sp, ["[%s] ⚠️ 桥接熔断：本次要删除 %d 条（上限 %d 条），已拒绝下发。"
                        "这一般意味着某一侧文件被清空或大面积改写，而不是你真的删了这么多。"
                        % (fmt_ts(now_ts()), len(remove), allowed)])
        remove = []
    return {"add": add, "remove": remove, "rename": rename,
            "generated": fmt_ts(now_ts()),
            "blocked_removes": blocked,
            "counts": {"add": len(add), "remove": len(remove), "rename": len(rename)}}


def bridge_recent(sp: Dict[str, Path], cfg: Dict[str, Any], limit: int = 20) -> Dict[str, Any]:
    """
    给扩展弹窗用的"最近变动"：
      - pending：当前还没落地到 Chrome 的改动（扩展马上要做的）
      - history：同步历史（每次的时间与增删改数量），外加扩展回报记录
    """
    state = load_state(sp)
    pending = _bridge_delta(sp, cfg)
    events: List[Dict[str, Any]] = []
    for rec in (state.get("history") or [])[-limit:]:
        summary = rec.get("summary") or {}
        events.append({
            "kind": "sync",
            "at": rec.get("human") or fmt_ts(rec.get("ts")),
            "chrome_add": summary.get("chrome_add", 0), "chrome_upd": summary.get("chrome_upd", 0),
            "chrome_del": summary.get("chrome_del", 0),
            "safari_add": summary.get("safari_add", 0), "safari_upd": summary.get("safari_upd", 0),
            "safari_del": summary.get("safari_del", 0),
            "merged": summary.get("merged_bookmarks"),
        })
    # 扩展回报（"已应用到 Chrome"）也当作一条记录，方便对照
    try:
        log_lines = sp["log"].read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        log_lines = []
    for line in log_lines[-200:]:
        if "扩展已应用改动：" not in line:
            continue
        stamp = line[1:20] if line.startswith("[") else ""
        payload = line.split("扩展已应用改动：", 1)[1]
        try:
            info = json.loads(payload)
        except ValueError:
            continue
        added = info.get("added", info.get("add", 0)) or 0
        removed = info.get("removed", info.get("remove", 0)) or 0
        renamed = info.get("renamed", info.get("rename", 0)) or 0
        if not (added or removed or renamed):
            continue                     # 空回报不值得占位
        events.append({"kind": "applied", "at": stamp, "added": added,
                       "removed": removed, "renamed": renamed})
    events.sort(key=lambda e: e.get("at") or "", reverse=True)
    return {
        "pending": pending,
        "events": events[:limit],
        "counts": pending.get("counts") or {},
        "generated": fmt_ts(now_ts()),
    }


def _bridge_touch(sp: Dict[str, Path]) -> None:
    try:
        atomic_write(sp["bridge"], json.dumps({"last_seen": now_ts()}).encode("utf-8"))
    except OSError:
        pass


def start_bridge(cfg: Dict[str, Any], sp: Dict[str, Path], port: int) -> Optional[Any]:
    """启动桥接服务（后台线程）。返回 server 对象，失败返回 None。"""
    import http.server
    import threading
    from urllib.parse import urlparse

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:      # 别把访问日志打到 stdout
            pass

        def _guard(self, fn: Any) -> None:
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                # 处理器里抛异常会让连接直接断掉，客户端只能看到 RemoteDisconnected，
                # 完全不知道发生了什么；这里统一下降级成 JSON 错误
                try:
                    self._send(500, json.dumps({"error": "%s: %s" % (type(exc).__name__, exc)},
                                               ensure_ascii=False).encode("utf-8"))
                except OSError:
                    pass
                append_log(sp, ["[%s] 桥接处理 %s 出错：%s: %s"
                                % (fmt_ts(now_ts()), self.path, type(exc).__name__, exc)])

        def do_GET(self) -> None:                        # noqa: N802
            self._guard(self._handle_get)

        def _handle_get(self) -> None:
            path = urlparse(self.path).path
            _bridge_touch(sp)
            if path == "/ping":
                self._send(200, json.dumps({"ok": True, "app": "bmsync",
                                            "version": __version__}).encode("utf-8"))
            elif path == "/delta":
                self._send(200, json.dumps(_bridge_delta(sp, cfg), ensure_ascii=False)
                           .encode("utf-8"))
            elif path == "/recent":
                self._send(200, json.dumps(bridge_recent(sp, cfg), ensure_ascii=False)
                           .encode("utf-8"))
            elif path == "/":
                self._send(200, BRIDGE_PAGE.encode("utf-8"), "text/html; charset=utf-8")
            else:
                self._send(404, b'{"error":"not found"}')

        def do_POST(self) -> None:                       # noqa: N802
            self._guard(self._handle_post)

        def _handle_post(self) -> None:
            _bridge_touch(sp)
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            path = urlparse(self.path).path
            if path == "/applied":
                try:
                    info = json.loads(body.decode("utf-8"))
                except ValueError:
                    info = {}
                append_log(sp, ["[%s] 扩展已应用改动：%s" % (fmt_ts(now_ts()),
                                                        json.dumps(info, ensure_ascii=False)[:200])])
                self._send(200, b'{"ok":true}')
            else:
                self._send(404, b'{"error":"not found"}')

    try:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as exc:
        eprint("[错误] 无法启动桥接服务（端口 %d）：%s" % (port, exc))
        eprint("       换个端口：bmsync watch --bridge-port 8888")
        return None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# --------------------------------------------------------------------------------------
# 实时同步（watch）
# --------------------------------------------------------------------------------------


def file_fingerprint(path: Path) -> Optional[Tuple[int, int]]:
    """文件指纹：(大小, mtime 纳秒)。文件不存在返回 None。"""
    try:
        st = path.stat()
        return (st.st_size, st.st_mtime_ns)
    except OSError:
        return None


def watch_fingerprints(backends: Sequence[Any]) -> Dict[str, Any]:
    return {b.name: file_fingerprint(b.bookmarks_path) for b in backends}


def watch_verify(cfg: Dict[str, Any], args: argparse.Namespace) -> int:
    """watch --verify：只做一次"有没有变化 + 能否同步"的检查，用来确认实时模式是否生效。"""
    chrome, safari = build_backends(cfg)
    sp = state_paths(cfg)
    print("bmsync %s 实时同步自检" % __version__)
    print("-" * 70)
    for backend in (chrome, safari):
        fp = file_fingerprint(backend.bookmarks_path)
        readable, msg = check_readable(backend.bookmarks_path)
        writable, msg_w = check_writable(backend.bookmarks_path)
        print("[%s] %s" % (backend.label, backend.bookmarks_path))
        print("  指纹  : %s" % ("大小 %s / mtime %s" % fp if fp else "文件不存在"))
        print("  读取  : %s（%s）" % ("正常" if readable else "失败", msg))
        print("  写入  : %s（%s）" % ("正常" if writable else "失败", msg_w))
    w = watch_settings(cfg)
    print("\n监听参数：轮询 %.1fs / 静默判定 %.1fs / 兜底同步 %.0fs" % (w["interval"], w["settle"], w["resync_after"]))
    if cfg["sync"].get("require_browsers_closed", True):
        running = [b.label for b in (chrome, safari)
                   if is_process_running(CHROMIUM_PROCS if b is chrome else ["Safari"])]
        if running:
            print("⚠️ %s 正在运行。浏览器把书签放在内存里，会整份重写书签文件，"
                  "本工具写进去的内容可能被覆盖；" % "、".join(running))
            print("   双向都可靠的做法：退出浏览器 → 触发一次同步 → 再打开。")
            print("   （这一提示来自 require_browsers_closed=true，可在 config.json 里关掉）")
    print("同步日志：%s" % sp["log"])
    print("-" * 70)
    print("结论：%s" % ("参数与权限就绪，可以运行 bmsync watch" if all(
        check_readable(b.bookmarks_path)[0] and check_writable(b.bookmarks_path)[0] for b in (chrome, safari))
        else "存在权限问题，先跑 bmsync doctor"))
    return 0


def cmd_watch_why(cfg: Dict[str, Any], args: argparse.Namespace) -> int:
    """
    watch --why：盯住两个书签文件一段时间，报告"文件变了但内容是否真的变了"。
    用来定位"我没动书签，为什么一直提示写入"。
    """
    chrome, safari = build_backends(cfg)
    backends = (chrome, safari)
    seconds = float(getattr(args, "why", 0) or 60)
    print("bmsync %s 观察模式：盯 %s 书签文件 %.0f 秒" % (__version__, "两个", seconds))
    print("期间请不要增删书签，正常浏览即可。")
    print("-" * 70)
    cache: Dict[str, Any] = {}
    last_stat = watch_fingerprints(backends)
    last_content = {b.name: file_content_fingerprint(b.bookmarks_path, cache) for b in backends}
    last_norm = {}
    for b in backends:
        try:
            last_norm[b.name] = normalized_content(b.bookmarks_path.read_bytes())
        except OSError:
            last_norm[b.name] = None
    events: Dict[str, int] = {"stat": 0, "content": 0}
    started = now_ts()
    while now_ts() - started < seconds:
        time.sleep(1.0)
        current = watch_fingerprints(backends)
        for backend in backends:
            name = backend.name
            if current.get(name) == last_stat.get(name):
                continue
            events["stat"] += 1
            digest = file_content_fingerprint(backend.bookmarks_path, cache)
            tag = "内容也变了 ⚠" if digest != last_content.get(name) else "内容未变（噪音）"
            print("[%s] %-6s 文件被重写 → %s" % (fmt_ts(now_ts())[-8:], backend.label, tag))
            if digest != last_content.get(name):
                events["content"] += 1
                try:
                    now_norm = normalized_content(backend.bookmarks_path.read_bytes())
                except OSError:
                    now_norm = None
                diffs = content_diff(last_norm.get(name), now_norm) if last_norm.get(name) is not None else []
                for line in diffs[:12]:
                    print("       差异：%s" % line)
                if len(diffs) > 12:
                    print("       （其余 %d 处省略）" % (len(diffs) - 12))
                if not diffs:
                    print("       未发现内容字段差异（可能只是顺序/结构重排）")
                last_norm[name] = now_norm
                last_content[name] = digest
        last_stat = current
    print("-" * 70)
    print("观察 %.0f 秒：文件被重写 %d 次，其中内容真的变化 %d 次"
          % (seconds, events["stat"], events["content"]))
    if events["stat"] and not events["content"]:
        print("结论：只是浏览器在重写文件，书签内容没有变化 —— 实时同步不会因此触发。")
    elif events["content"]:
        print("结论：上面的『差异』就是让实时同步触发的字段。请把它发给我，"
              "我把这些字段加入忽略列表（它们应当属于易变元数据）。")
    else:
        print("结论：这段时间文件没有任何写入。")
    return 0


def watch_log(sp: Dict[str, Path], message: str, verbose: int) -> None:
    """实时模式的运行日志：既写 bmsync.log，也按详细程度打到终端。"""
    append_log(sp, ["[%s] watch: %s" % (fmt_ts(now_ts()), message)])
    log("  %s" % message, verbose)


def cmd_watch(cfg: Dict[str, Any], args: argparse.Namespace) -> int:
    """
    实时同步：轮询两个书签文件，发现变化就触发一次同步。

    为什么是轮询而不是 FSEvents：浏览器保存书签是「写临时文件 + 原子替换」，
    路径级事件对这种方式不友好；mtime+size 轮询在所有文件系统上都可靠，
    而且读取开销极小（每秒不到 1ms）。真正的限制在浏览器侧——它们把书签放在内存里，
    退出时才整份写回，所以"实时"的粒度是浏览器落盘的频率。
    """
    if getattr(args, "why", None):
        return cmd_watch_why(cfg, args)
    if getattr(args, "verify", False):
        return watch_verify(cfg, args)
    if getattr(args, "stop", False):
        return stop_watch_daemon(cfg, args)

    verbose = getattr(args, "verbose", 1) or 0
    chrome, safari = build_backends(cfg)
    backends = (chrome, safari)
    sp = state_paths(cfg)
    w = watch_settings(cfg)
    once = float(getattr(args, "once", 0) or 0)
    lock_path = expand(getattr(args, "lock", None) or sp["lock"])

    try:
        with file_lock(lock_path):
            pass
    except RuntimeError as exc:
        eprint("[错误] %s" % exc)
        return 3

    verbose_eff = verbose if once else 0      # 常驻模式默认不打刷屏汇总

    for backend in backends:                   # 启动前先确认权限
        readable, msg = check_readable(backend.bookmarks_path)
        if not readable:
            eprint("[错误] 无法读取 %s：%s" % (backend.label, msg))
            eprint("       实时模式需要持续读取书签文件；先跑 bmsync doctor 处理权限。")
            return 2
        if not check_writable(backend.bookmarks_path)[0]:
            eprint("[错误] 无法写入 %s：%s" % (backend.label, check_writable(backend.bookmarks_path)[1]))
            return 2

    bridge_port = int(getattr(args, "bridge_port", 0) or (cfg.get("watch") or {}).get("bridge_port", 8765) or 0)
    bridge_server = None
    if bridge_port and not getattr(args, "no_bridge", False):
        bridge_server = start_bridge(cfg, sp, bridge_port)
        if bridge_server:
            print("  桥接：http://127.0.0.1:%d（Chrome 扩展连这里，%s）"
                  % (bridge_port, "已就绪" if bridge_active(cfg) else "等待扩展连接"))

    if once:
        log("实时同步自检（只跑一轮）", 1, verbose)
    else:
        print("bmsync 实时同步已启动")
        print("  监听：%s" % "、".join(str(b.bookmarks_path) for b in backends))
        print("  参数：轮询 %.1fs / 静默判定 %.1fs / 兜底同步 %.0fs"
              % (w["interval"], w["settle"], w["resync_after"]))
        print("  判定：只比对书签的实质内容；浏览器重写文件但书签没变时不会同步")
        print("  日志：%s" % sp["log"])
        print("  退出：Ctrl+C（后台运行用 bmsync watch-daemon start）")
        print("  提示：浏览器落盘前内存里的改动看不到，所以粒度取决于浏览器保存书签的频率。")
        print("")
    flush = True

    # 启动时先做一次兜底同步（补上"上次同步之后、watch 启动之前"的变化）
    if not getattr(args, "no_initial", False):
        run_sync(cfg, chrome, safari, sp, load_state(sp),
                 argparse.Namespace(dry_run=False, force=True, prefer=None, direction=None,
                                    list_changes=False, max_show=0, quiet=False, json=False,
                                    verbose=verbose_eff))
        watch_log(sp, "启动时兜底同步完成", verbose)
        flush = False
    watch_log(sp, "开始监听（轮询 %.1fs）" % w["interval"], verbose)

    # 变化检测状态：last_seen 是"已同步过的指纹"，pending 是"正在等待静默的那一份"。
    # 注意 pending 必须和 raw_change 同为"变化的那部分"，否则两者永远不相等、计时器会被反复重置。
    # 两级判断：先用 (大小, mtime) 做廉价初筛，再用"实质内容指纹"确认。
    # 只靠 mtime 会把"浏览器切个标签页就整份重写文件"也算成变化，导致空转同步。
    fp_cache: Dict[str, Any] = {}
    last_seen = watch_fingerprints(backends)
    last_content = {b.name: file_content_fingerprint(b.bookmarks_path, fp_cache) for b in backends}
    last_sync = now_ts()
    settle_since: Optional[float] = None
    pending: Optional[Dict[str, Any]] = None
    watch_started = now_ts()
    skipped_noise = 0

    try:
        with file_lock(lock_path):
            while True:
                time.sleep(w["interval"])
                current = watch_fingerprints(backends)
                stat_change = {name: fp for name, fp in current.items() if fp != last_seen.get(name)}

                # 文件被动过 → 再看实质内容有没有变。切标签页、改 checksum、系统重写 plist
                # 都会让文件变化但书签没变，这类"噪音"直接记下指纹、不进入同步流程。
                raw_change: Dict[str, Any] = {}
                if stat_change:
                    for name, fp in stat_change.items():
                        backend = next(b for b in backends if b.name == name)
                        if file_content_fingerprint(backend.bookmarks_path, fp_cache) != last_content.get(name):
                            raw_change[name] = fp
                        else:
                            skipped_noise += 1
                            log("  %s 文件被重写但书签内容未变，跳过同步（累计 %d 次）"
                                % (backend.label, skipped_noise), 2, verbose)
                    # 只有噪音时才在这里提前记账；真正的内容变化要等静默结束、同步完成后再记账，
                    # 否则下一个 tick 会认为"没有变化"，把静默计时重置掉，真变化永远触发不了。
                    if not raw_change:
                        last_seen.update(current)

                changed: Dict[str, Any] = {}
                if raw_change:
                    if raw_change != pending:
                        # 文件刚被改动（或又改了）：重新开始静默计时
                        pending = raw_change
                        settle_since = now_ts()
                        log("  检测到 %s 有写入，等待静默 %.1fs…"
                            % ("、".join(raw_change), w["settle"]), 2, verbose)
                    elif settle_since is not None and now_ts() - settle_since >= w["settle"]:
                        # 连续 settle 秒没有再变化，认为浏览器写完了
                        changed = raw_change
                else:
                    # 文件恢复原样（浏览器可能替换后又回退）
                    if pending is not None:
                        pending = None
                        settle_since = None

                if changed:
                    who = "、".join({"chrome": "Chrome", "safari": "Safari"}.get(k, k) for k in changed)
                    watch_log(sp, "%s 书签文件有变化，开始同步" % who, verbose)
                    if cfg["sync"].get("require_browsers_closed", True):
                        # 实时模式下没法"拒绝同步"，但必须说清风险：运行中的浏览器
                        # 把书签放在内存里，随时可能整份重写文件、覆盖掉我们写进去的内容
                        running = [b.label for b in backends
                                   if is_process_running(CHROMIUM_PROCS if b is chrome else ["Safari"])]
                        if running:
                            watch_log(sp, "⚠️ %s 正在运行：写入的内容可能被它用内存数据覆盖。"
                                          "让变更可靠落地的做法是退出浏览器后再同步（改 "
                                          "require_browsers_closed 可关闭此提示）" % "、".join(running), verbose)
                    code = run_sync(cfg, chrome, safari, sp, load_state(sp),
                                    argparse.Namespace(dry_run=False, force=True, prefer=None,
                                                       direction=None, list_changes=False,
                                                       max_show=0, quiet=True, json=False,
                                                       verbose=verbose_eff))
                    watch_log(sp, "同步结束（退出码 %d）" % code, verbose)
                    last_sync = now_ts()
                    settle_since, pending = None, None
                    last_seen = watch_fingerprints(backends)   # 以写回后的状态为基准
                    last_content = {b.name: file_content_fingerprint(b.bookmarks_path, fp_cache)
                                    for b in backends}
                    flush = True
                else:
                    # 长时间没有变化时也兜底同步一次（对齐"另一边被外部工具改动"等情况）
                    if now_ts() - last_sync >= w["resync_after"]:
                        run_sync(cfg, chrome, safari, sp, load_state(sp),
                                 argparse.Namespace(dry_run=False, force=True, prefer=None,
                                                    direction=None, list_changes=False, max_show=0,
                                                    quiet=True, json=False, verbose=verbose_eff))
                        watch_log(sp, "兜底同步完成（%.0fs 无变化）" % w["resync_after"], verbose)
                        last_sync = now_ts()
                        last_seen = watch_fingerprints(backends)
                        last_content = {b.name: file_content_fingerprint(b.bookmarks_path, fp_cache)
                                        for b in backends}

                if flush:
                    sys.stdout.flush()
                    flush = False
                if once and now_ts() - watch_started >= once:
                    log("自检结束：已监听 %.1fs" % (now_ts() - watch_started), 1, verbose)
                    return 0
    except KeyboardInterrupt:
        print("")
        watch_log(sp, "实时同步已停止（Ctrl+C）", verbose)
        if bridge_server is not None:
            bridge_server.shutdown()
        return 0
    except Exception as exc:  # noqa: BLE001
        # 常驻服务里异常退出会很难发现，这里务必留下记录
        watch_log(sp, "实时同步异常退出：%s: %s" % (type(exc).__name__, exc), verbose)
        eprint("[错误] 实时同步异常退出：%s: %s" % (type(exc).__name__, exc))
        if verbose >= 2:
            raise
        return 1


def run_sync_asymmetric(cfg: Dict[str, Any], chrome: ChromeBackend, safari: SafariBackend,
                        sp: Dict[str, Path], state: Dict[str, Any], args: argparse.Namespace,
                        scope_chrome: str, scope_safari: str) -> int:
    """
    非对称同步：一侧（默认整个 Chrome 书签树）镜像进另一侧的一个目录。

    典型用法：Chrome 保持原有目录结构，Safari 侧全部收进 sync_chrome 一个文件夹：
      "scope_safari": "sync_chrome", "scope_chrome": ""
    映射规则：
      - Chrome 的「书签栏」内容直接铺在 sync_chrome 下（不套一层）
      - 「其他书签」「移动设备书签」各在 sync_chrome 下建一个同名子目录
      - 反向：sync_chrome 根下的新增/改动回写到 Chrome 的书签栏
    """
    verbose = getattr(args, "verbose", 1) or 0
    dry_run = bool(getattr(args, "dry_run", False))
    skip = cfg["sync"].get("skip_folders") or []

    for backend in (chrome, safari):
        readable, msg = check_readable(backend.bookmarks_path)
        if not readable:
            eprint("[错误] 无法读取 %s 书签：%s" % (backend.label, msg))
            return 2
        if not dry_run and not check_writable(backend.bookmarks_path)[0]:
            eprint("[错误] 无法写入 %s 书签：%s"
                   % (backend.label, check_writable(backend.bookmarks_path)[1]))
            return 2

    chrome_tree = chrome.read_tree(skip_folders=skip)
    safari_tree = safari.read_tree(skip_folders=skip)

    # 解析两侧的输入范围
    c_folder = resolve_scope_folder(chrome_tree, scope_chrome) if scope_chrome else None
    s_folder = resolve_scope_folder(safari_tree, scope_safari) if scope_safari else None
    if scope_chrome and c_folder is None:
        eprint("[错误] Chrome 里找不到目录「%s」" % scope_chrome)
        return 2
    if scope_safari and s_folder is None:
        log("提示：Safari 里还没有「%s」目录，本次会创建它" % scope_safari, verbose)

    # 注意区分两种情况：
    #   scope 未配置  → 用整棵树
    #   scope 已配置但目录还不存在 → 必须是"空"，绝不能用整棵树
    # （用整棵树会把 Safari 的全部书签当成同步源，反向灌进 Chrome，实测发生过）
    left = in_scope_tree(chrome_tree, c_folder) if scope_chrome else in_scope_tree(chrome_tree, None)
    if scope_safari:
        right = in_scope_tree(safari_tree, s_folder) if s_folder else empty_scope_tree()
    else:
        right = in_scope_tree(safari_tree, None)

    base = tree_from_state(state["base"]) if state.get("base", {}).get("items") else None
    first_run = base is None or state.get("asym_scope") != (scope_chrome, scope_safari)
    if first_run:
        base = Tree()
        for pid in PSEUDO_IDS:
            base.add(Item(id=pid, kind="folder", name=PSEUDO_LABELS[pid], parent_id=PSEUDO_ROOT))
        base.children[PSEUDO_ROOT] = list(PSEUDO_IDS)
        for src in (left, right):
            for kid in src.children_of(PSEUDO_BAR):
                if kid in src.items and kid not in base.items:
                    _add_subtree(base, src, kid, PSEUDO_BAR)
        log("首次在该范围同步：以 Chrome 现有内容建立基线", verbose)
    state["asym_scope"] = [scope_chrome, scope_safari]

    # 反向要回写，但只认 sync_chrome 里的内容，且只回写到 Chrome 的书签栏：
    # 先把 Google 侧（left）作为基准，再把 Safari 目标目录里的新增/改动并进来。
    # 这样 Safari 的其它书签（草稿区）永远不会被复制到 Chrome。
    # 反向来源只取 Safari 目标目录里的内容（草稿区永不进入同步）
    rev_source = empty_scope_tree()
    for kid in right.children_of(PSEUDO_BAR):
        if kid in right.items:
            _add_subtree(rev_source, right, kid, PSEUDO_BAR)
    left_for_merge = left
    right_for_merge = rev_source
    result = merge(base, left_for_merge, right_for_merge,
                   prefer=getattr(args, "prefer", None) or cfg["sync"].get("prefer", "newest"),
                   max_delete_ratio=float(cfg["sync"].get("max_delete_ratio", 0.05) or 0),
                   max_delete_count=int(cfg["sync"].get("max_delete_count", 10) or 0),
                   no_delete=bool(cfg["sync"].get("no_delete")) or bool(getattr(args, "no_delete", False))
                   or first_run,
                   scope_anchor=PSEUDO_BAR, verbose=verbose)
    merged_scope = result.merged

    counts = {side: Counter(c.kind for c in changes) for side, changes in result.changes.items()}
    if verbose:
        print("")
        print("非对称同步：Chrome %s → Safari「%s」" % (scope_chrome or "（整个书签树）", scope_safari))
        for side in ("chrome", "safari"):
            c = counts[side]
            print("  %-6s 新增 %d / 修改 %d / 删除 %d"
                  % (side, c.get("add", 0), c.get("update", 0), c.get("delete", 0)))
        print("  合并结果：%d 书签 / %d 目录"
              % (merged_scope.stats()["bookmarks"], merged_scope.stats()["folders"]))
        for line in result.conflicts[:8]:
            print("  ⚠ %s" % line)

    # 各自组装写盘树：Chrome 只回填它原本的容器，Safari 只回填目标目录
    write_chrome = _chrome_scope_write_tree(chrome_tree, merged_scope, c_folder)
    write_safari = _safari_scope_write_tree(safari_tree, merged_scope, s_folder, scope_safari)

    if dry_run:
        print("\n[演练] 不会写入任何文件。")
        for label, target, cur in (("Chrome", write_chrome, chrome_tree),
                                   ("Safari", write_safari, safari_tree)):
            steps = plan_changes(target, cur)
            print("  %s：%d 处" % (label, len(steps)))
            for step in steps[:getattr(args, "max_show", 40)]:
                print("     %s" % step)
        return 0

    keep = cfg["sync"].get("backup_keep", 20)
    min_interval = float(cfg["sync"].get("backup_min_interval", 300) or 0)
    backups = []
    for backend in (chrome, safari):
        b, created = backup_file(backend.bookmarks_path, sp["backups"], keep,
                                 min_interval=min_interval)
        if b:
            backups.append("%s%s" % (b, "" if created else "（内容未变，复用）"))

    direction = getattr(args, "direction", None) or cfg["sync"].get("direction", "both")
    if direction != "chrome2safari":
        if bridge_active(cfg):
            log("  Chrome 写入：交给 Chrome 扩展", verbose)
        else:
            steps = chrome.write_tree(write_chrome,
                                      chrome.build_native_map(chrome.read_raw(), write_chrome))
            log("  Chrome 写入：%s" % ("；".join(steps) or "无变化"), verbose)
    if direction != "safari2chrome":
        steps = safari.write_tree(write_safari)
        log("  Safari 写入：%s" % ("；".join(steps) or "无变化"), verbose)

    summary = {"asym": True, "scope_chrome": scope_chrome, "scope_safari": scope_safari,
               "chrome_add": counts["chrome"].get("add", 0),
               "chrome_del": counts["chrome"].get("delete", 0),
               "safari_add": counts["safari"].get("add", 0),
               "safari_del": counts["safari"].get("delete", 0),
               "merged_bookmarks": merged_scope.stats()["bookmarks"]}
    save_state(sp, state, merged_scope, summary, result.tombstones)
    append_log(sp, ["[%s] %s" % (fmt_ts(now_ts()), json.dumps(summary, ensure_ascii=False))]
               + ["    backup  %s" % b for b in backups])
    log("\n状态已保存：%s" % sp["state"], verbose)
    return 0


def _chrome_scope_write_tree(chrome_tree: Tree, merged_scope: Tree,
                             c_folder: Optional[str]) -> Tree:
    """
    Chrome 侧写盘树：**保持 Chrome 自己的容器结构不变**。
      - scope_chrome 为空（整个书签树）时：书签栏/其他书签/移动设备书签 分别回填
      - scope_chrome 指定目录时：只回填那个目录
    """
    out = chrome_tree.clone()
    if c_folder is None:
        mapping = ((PSEUDO_BAR, BAR_ID), (PSEUDO_OTHER, OTHER_ID), (PSEUDO_MOBILE, None))
        for pid, container in mapping:
            kids = [k for k in merged_scope.children_of(pid) if k in merged_scope.items]
            if container is None:
                # 移动设备书签：统一树里是 other 下的一个普通目录
                holder = out.make_id(OTHER_ID, "folder", MOBILE_FOLDER_NAME, None)
                if kids:
                    if holder not in out.items:
                        out.add(Item(id=holder, kind="folder", name=MOBILE_FOLDER_NAME,
                                     parent_id=OTHER_ID))
                    for item_id in list(out.subtree_ids(holder)):
                        if item_id != holder:
                            out.items.pop(item_id, None)
                            out.children.pop(item_id, None)
                    out.children[holder] = []
                    for kid in kids:
                        _add_subtree(out, merged_scope, kid, holder)
                continue
            # 清掉该容器原有内容再回填
            for item_id in list(out.children_of(container)):
                if item_id in out.items:
                    out.items.pop(item_id, None)
                    out.children.pop(item_id, None)
                    out.children.pop(item_id, None)
            out.children[container] = []
            for kid in kids:
                _add_subtree(out, merged_scope, kid, container)
        return out
    # 只回填指定目录
    for item_id in list(out.subtree_ids(c_folder)):
        if item_id != c_folder:
            out.items.pop(item_id, None)
            out.children.pop(item_id, None)
    out.children[c_folder] = []
    for kid in merged_scope.children_of(PSEUDO_BAR):
        if kid in merged_scope.items:
            _add_subtree(out, merged_scope, kid, c_folder)
    return out


def _safari_scope_write_tree(safari_tree: Tree, merged_scope: Tree,
                             s_folder: Optional[str], scope_name: str) -> Tree:
    """
    Safari 侧：把「书签栏」那部分收进目标目录，容器与目录外内容原样保留。

    目标目录应当直接位于**书签栏（个人收藏）**下 —— 这是用户的直觉位置。
    历史 bug：早期版本把目录建在了「书签菜单」(other) 下，多出一层；这里会把它搬回书签栏。
    """
    out = safari_tree.clone()
    target: Optional[str] = None

    if s_folder is not None and s_folder in out.items:
        parent = out.items[s_folder].parent_id
        if parent == BAR_ID:
            target = s_folder
        else:
            # 建错位置（例如旧版本建在「书签菜单」里）：整棵搬进书签栏
            for item_id in list(out.subtree_ids(s_folder)):
                if item_id != s_folder:
                    out.items.pop(item_id, None)
                    out.children.pop(item_id, None)
            if s_folder in out.children.get(parent, []):
                out.children[parent].remove(s_folder)
            out.items.pop(s_folder, None)
            out.children.pop(s_folder, None)

    if target is None:
        target = out.make_id(BAR_ID, "folder", scope_name, None)
        if target not in out.items:
            out.add(Item(id=target, kind="folder", name=scope_name, parent_id=BAR_ID,
                         mtime=None))

    # 清掉目标目录原有内容
    if target in safari_tree.items:
        for item_id in list(safari_tree.subtree_ids(target)):
            if item_id == target:
                continue
            out.items.pop(item_id, None)
            out.children.pop(item_id, None)
    out.children[target] = []

    # 书签栏内容铺开
    for kid in merged_scope.children_of(PSEUDO_BAR):
        if kid in merged_scope.items:
            _add_subtree(out, merged_scope, kid, target)
    # 其他书签 / 移动设备书签 各建同名子目录
    for pid in (PSEUDO_OTHER, PSEUDO_MOBILE):
        kids = [k for k in merged_scope.children_of(pid) if k in merged_scope.items]
        if not kids:
            continue
        marker = out.make_id(target, "folder", PSEUDO_LABELS[pid], None)
        if marker not in out.items:
            out.add(Item(id=marker, kind="folder", name=PSEUDO_LABELS[pid],
                         parent_id=target, mtime=None))
        for kid in kids:
            _add_subtree(out, merged_scope, kid, marker)
    return out


def find_stray_scope_folders(tree: Tree, scope_name: str, keep_id: Optional[str]) -> List[str]:
    """找出同名、但不在书签栏里的重复同步目录（旧版本建错位置留下的）。"""
    strays = []
    for container in CONTAINERS:
        for kid in tree.children_of(container):
            if kid == keep_id or kid not in tree.items:
                continue
            node = tree.items[kid]
            if node.kind == "folder" and norm_name(node.name) == norm_name(scope_name):
                strays.append(kid)
    return strays


# --------------------------------------------------------------------------------------
# 定时任务
# --------------------------------------------------------------------------------------

def launchd_dir() -> Path:
    """
    LaunchAgents 目录。默认 ~/Library/LaunchAgents；
    可用环境变量 BMSYNC_LAUNCHD_DIR 覆盖（测试用，也方便放到非标准位置）。
    """
    override = os.environ.get("BMSYNC_LAUNCHD_DIR")
    return expand(override) if override else (HOME / "Library/LaunchAgents")


LAUNCHD_LABEL = "com.bmsync.sync"
# launchd 的 stdout/stderr 落在这里：~/Library/Logs 是系统认可的日志位置，launchd 必定可写。
# 放在仓库的 state/ 下会有隐患——若仓库在外部卷或受保护目录，launchd 可能创建不了日志文件，
# 于是任务失败时连一行线索都没有（表现为 .err.log 不存在）。
LAUNCHD_LOG_DIR = HOME / "Library/Logs/bmsync"


def launchd_domain() -> str:
    return "gui/%d" % os.getuid()


def launchctl(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl"] + list(argv), capture_output=True, text=True)


def cmd_install_scheduler(cfg: Dict[str, Any], args: argparse.Namespace) -> int:
    script = Path(__file__).resolve()
    # 优先用系统自带的 python3：路径稳定，且方便在"完全磁盘访问权限"里单独勾选
    python = "/usr/bin/python3" if Path("/usr/bin/python3").exists() else (sys.executable or "python3")
    interval = max(5, int(args.interval)) * 60
    # 必须转成绝对路径：相对路径会被 launchd 按 WorkingDirectory 解析，容易踩坑
    log_dir = (expand(args.log_dir).resolve() if getattr(args, "log_dir", None) else LAUNCHD_LOG_DIR)
    dry_run = bool(getattr(args, "dry_run", False))

    # 权限没就绪就别装了：定时任务失败时很难查（连日志都可能创建不出来）。
    # 演练模式不写任何文件，跳过这项检查。
    if not dry_run:
        blockers = []
        for backend in build_backends(cfg):
            readable, msg = check_readable(backend.bookmarks_path)
            if not readable:
                blockers.append("%s 读取失败：%s" % (backend.label, msg))
                continue
            writable, msg_w = check_writable(backend.bookmarks_path)
            if not writable:
                blockers.append("%s 写入失败：%s" % (backend.label, msg_w))
        if blockers:
            eprint("未安装定时任务：当前权限还不足以同步，先解决下面的问题再装。")
            for b in blockers:
                eprint("  ✗ %s" % b)
            eprint("  提示：先跑 bmsync doctor 完整自检。定时任务由 launchd 拉起，")
            eprint("        需要把 %s 单独加入“完全磁盘访问权限”，不会继承终端的授权。" % python)
            return 2

        try:
            log_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            eprint("无法创建日志目录 %s：%s" % (log_dir, exc))
            eprint("可以换一个位置：bmsync install-scheduler --log-dir ~/somewhere")
            return 1
    # 只有配置文件确实存在时才传 --config，否则让它走默认查找逻辑
    argv = [python, str(script)]
    config_arg = str(Path(args.config).resolve()) if args.config else str(APP_DIR / "config.json")
    if Path(config_arg).exists():
        argv += ["--config", config_arg]
    else:
        print("提示：%s 不存在，将使用内置默认配置。建议先运行 bmsync init-config 生成配置文件。" % config_arg)
    argv += ["sync", "--force", "--quiet"]
    plist = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": argv,
        "StartInterval": interval,
        "RunAtLoad": False,
        "StandardOutPath": str(log_dir / "scheduler.out.log"),
        "StandardErrorPath": str(log_dir / "scheduler.err.log"),
        "WorkingDirectory": str(APP_DIR),
    }
    data = plistlib.dumps(plist, fmt=plistlib.FMT_XML)
    if dry_run:
        print("将写入：%s（每 %d 分钟一次）\n" % (launchd_plist_path(LAUNCHD_LABEL), interval // 60))
        print(data.decode("utf-8"))
        print("演练模式：未写入文件、未调用 launchctl。")
        return 0
    launchd_plist_path(LAUNCHD_LABEL).parent.mkdir(parents=True, exist_ok=True)
    atomic_write(launchd_plist_path(LAUNCHD_LABEL), data)

    # 必须先把旧定义从 launchd 内存里卸掉再装新的。只写文件不重新 bootstrap 的话，
    # 内存里留的还是上一份定义（可能指向已失效的解释器路径），表现为 `launchctl start`
    # 立刻失败、退出码 78（EX_CONFIG）、连日志文件都不会被创建。
    bootout = launchctl("bootout", "%s/%s" % (launchd_domain(), LAUNCHD_LABEL))
    bootstrapped = launchctl("bootstrap", launchd_domain(), str(launchd_plist_path(LAUNCHD_LABEL)))
    if bootstrapped.returncode != 0:
        # 老系统或特殊环境下的兜底：load -w 也能加载，但不如 bootstrap 可靠
        legacy = launchctl("load", "-w", str(launchd_plist_path(LAUNCHD_LABEL)))
        if legacy.returncode != 0:
            eprint("加载定时任务失败。")
            eprint("  bootstrap: %s" % (bootstrapped.stderr.strip() or bootstrapped.stdout.strip()))
            eprint("  load     : %s" % (legacy.stderr.strip() or legacy.stdout.strip()))
            eprint("请手动执行：launchctl bootstrap %s %s" % (launchd_domain(), launchd_plist_path(LAUNCHD_LABEL)))
            return 1
    print("已写入并加载定时任务：%s（每 %d 分钟一次）" % (launchd_plist_path(LAUNCHD_LABEL), interval // 60))
    if bootout.returncode == 0:
        print("（已先卸载内存中的旧定义，避免残留旧配置）")
    del bootout
    print("查看：launchctl print %s/%s | grep -E 'last exit code|state'" % (launchd_domain(), LAUNCHD_LABEL))
    print("立即运行：launchctl kickstart -k %s/%s" % (launchd_domain(), LAUNCHD_LABEL))

    # 立刻跑一次，当场验证（否则要等一个周期才知道行不行）
    if getattr(args, "verify", True):
        print("\n正在立即触发一次以验证配置……")
        launchctl("kickstart", "-k", "%s/%s" % (launchd_domain(), LAUNCHD_LABEL))
        code = None
        for _ in range(20):                     # 最多等 10 秒
            time.sleep(0.5)
            info = launchctl("print", "%s/%s" % (launchd_domain(), LAUNCHD_LABEL))
            m = re.search(r"last exit code = (\d+)", info.stdout)
            if m:
                code = int(m.group(1))
                break
        log_path = state_paths(cfg)["log"]
        if code == 0:
            print("  ✅ 退出码 0：定时任务可以正常同步（细节见 %s）" % log_path)
        elif code is None:
            print("  ⚠️  没读到退出码，请手动确认：launchctl print %s/%s" % (launchd_domain(), LAUNCHD_LABEL))
        else:
            print("  ❌ 退出码 %d：任务启动了但同步没成功。" % code)
            if code == 2:
                print("     多半是定时任务没有磁盘访问权限：系统设置 → 隐私与安全性 → 完全磁盘访问权限")
                print("     点 + 号 → Cmd+Shift+G → 输入 %s → 添加后重新运行本命令。" % python)
            elif code == 3:
                print("     浏览器没退出：退出 Chrome/Safari 后重新触发。")
            print("     具体原因见：%s" % log_path)
            if log_path.exists():
                tail = log_path.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-3:]
                for line in tail:
                    print("       | %s" % line)
    print("任务日志：%s/scheduler.{out,err}.log" % log_dir)
    print("改动记录：%s（每次同步的成功/失败都会写在这里）" % state_paths(cfg)["log"])
    print("")
    print("提醒：launchctl start 是异步的，执行后请用下面两条命令确认是否真的跑成功：")
    print("  1) launchctl print gui/$(id -u)/%s | grep -E 'last exit code|state'" % LAUNCHD_LABEL)
    print("     退出码 0 = 成功；2 = 文件不可读写（多半是没给完全磁盘访问权限）；3 = 浏览器没退出")
    print("  2) tail -20 %s" % state_paths(cfg)["log"])
    # 只有在验证失败（多半是权限）时才展开讲授权步骤，成功时不制造无谓的焦虑
    print("说明：launchd 拉起的进程不继承终端的权限；如果上面显示失败，")
    print("      就到 系统设置 → 隐私与安全性 → 完全磁盘访问权限，点 + 号 →")
    print("      Cmd+Shift+G 输入 %s 添加，然后重新运行本命令。" % python)
    return 0


def cmd_uninstall_scheduler(cfg: Dict[str, Any], args: argparse.Namespace) -> int:
    launchctl("bootout", "%s/%s" % (launchd_domain(), LAUNCHD_LABEL))
    launchctl("unload", str(launchd_plist_path(LAUNCHD_LABEL)))          # 兜底：老式加载方式的清理
    if not launchd_plist_path(LAUNCHD_LABEL).exists():
        print("已卸载定时任务（未发现 plist 文件）。")
        return 0
    try:
        launchd_plist_path(LAUNCHD_LABEL).unlink()
    except OSError as exc:
        # 已从 launchd 卸载就算成功；删不掉 plist 只影响"下次登录是否还加载"，
        # 不该让 mode 切换整个失败
        eprint("警告：已从 launchd 卸载，但删除 plist 失败：%s（%s）" % (launchd_plist_path(LAUNCHD_LABEL), exc))
        return 0
    print("已移除 %s" % launchd_plist_path(LAUNCHD_LABEL))
    return 0


WATCH_LABEL = "com.bmsync.watch"


def launchd_plist_path(label: str) -> Path:
    return launchd_dir() / ("%s.plist" % label)


def watch_daemon_plist(cfg: Dict[str, Any], args: argparse.Namespace, python: str) -> Dict[str, Any]:
    script = Path(__file__).resolve()
    log_dir = (expand(args.log_dir).resolve() if getattr(args, "log_dir", None) else LAUNCHD_LOG_DIR)
    argv = [python, str(script)]
    config_arg = str(Path(args.config).resolve()) if args.config else str(APP_DIR / "config.json")
    if Path(config_arg).exists():
        argv += ["--config", config_arg]
    argv += ["watch", "--no-initial"]        # 常驻：启动时不做兜底同步，避免开机时抢跑
    return {
        "Label": WATCH_LABEL,
        "ProgramArguments": argv,
        "RunAtLoad": True,
        "KeepAlive": True,                   # 崩了/被杀掉自动拉起
        "ThrottleInterval": 10,
        "StandardOutPath": str(log_dir / "watch.out.log"),
        "StandardErrorPath": str(log_dir / "watch.err.log"),
        "WorkingDirectory": str(APP_DIR),
    }


def watch_daemon_status() -> Tuple[bool, str]:
    res = launchctl("print", "%s/%s" % (launchd_domain(), WATCH_LABEL))
    if res.returncode != 0:
        return False, "未运行"
    pid = re.search(r"\bpid = (\d+)", res.stdout)
    code = re.search(r"last exit code = (\d+)", res.stdout)
    bits = []
    if pid:
        bits.append("PID %s" % pid.group(1))
    if code:
        bits.append("上次退出码 %s" % code.group(1))
    return True, "运行中（%s）" % ("，".join(bits) if bits else "已加载")


def start_watch_daemon(cfg: Dict[str, Any], args: argparse.Namespace) -> int:
    python = "/usr/bin/python3" if Path("/usr/bin/python3").exists() else (sys.executable or "python3")
    blockers = []
    for backend in build_backends(cfg):
        if not check_readable(backend.bookmarks_path)[0]:
            blockers.append("%s 读取失败" % backend.label)
        elif not check_writable(backend.bookmarks_path)[0]:
            blockers.append("%s 写入失败" % backend.label)
    if blockers:
        eprint("未启动常驻监听：%s" % "；".join(blockers))
        eprint("  先跑 bmsync doctor 处理权限（launchd 需要单独授予完全磁盘访问权限）。")
        return 2
    log_dir = (expand(args.log_dir).resolve() if getattr(args, "log_dir", None) else LAUNCHD_LOG_DIR)
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        eprint("无法创建日志目录 %s：%s" % (log_dir, exc))
        return 1
    data = plistlib.dumps(watch_daemon_plist(cfg, args, python), fmt=plistlib.FMT_XML)
    if getattr(args, "dry_run", False):
        print("将写入：%s\n" % launchd_plist_path(WATCH_LABEL))
        print(data.decode("utf-8"))
        print("演练模式：未写入、未调用 launchctl。")
        return 0
    launchd_plist_path(WATCH_LABEL).parent.mkdir(parents=True, exist_ok=True)
    atomic_write(launchd_plist_path(WATCH_LABEL), data)
    launchctl("bootout", "%s/%s" % (launchd_domain(), WATCH_LABEL))
    res = launchctl("bootstrap", launchd_domain(), str(launchd_plist_path(WATCH_LABEL)))
    if res.returncode != 0:
        legacy = launchctl("load", "-w", str(launchd_plist_path(WATCH_LABEL)))
        if legacy.returncode != 0:
            eprint("启动失败：%s" % (res.stderr.strip() or legacy.stderr.strip()))
            return 1
    set_mode(cfg, args, "watch", quiet=True)
    print("实时监听已作为常驻服务启动：%s" % launchd_plist_path(WATCH_LABEL))
    print("  它会在登录后自动启动，退出后自动拉起（KeepAlive）。")
    print("  查看状态：bmsync watch-daemon status")
    print("  查看输出：%s" % (log_dir / "watch.err.log"))
    print("  停止    ：bmsync watch-daemon stop")
    return 0


def stop_watch_daemon(cfg: Dict[str, Any], args: argparse.Namespace) -> int:
    existed = launchd_plist_path(WATCH_LABEL).exists()
    launchctl("bootout", "%s/%s" % (launchd_domain(), WATCH_LABEL))
    launchctl("unload", str(launchd_plist_path(WATCH_LABEL)))
    if existed and not getattr(args, "keep_plist", False):
        launchd_plist_path(WATCH_LABEL).unlink()
    print("已停止实时监听服务（%s）" % ("并移除 plist" if existed else "未发现 plist，仅确保已卸载"))
    return 0


def cmd_watch_daemon(cfg: Dict[str, Any], args: argparse.Namespace) -> int:
    action = args.action
    if action == "start":
        return start_watch_daemon(cfg, args)
    if action == "stop":
        return stop_watch_daemon(cfg, args)
    if action == "restart":
        stop_watch_daemon(cfg, argparse.Namespace(**{**vars(args), "keep_plist": True}))
        return start_watch_daemon(cfg, args)
    # status
    running, desc = watch_daemon_status()
    sp = state_paths(cfg)
    mode = (cfg.get("mode") or "manual")
    w = watch_settings(cfg)
    print("bmsync 实时监听服务")
    print("-" * 70)
    print("服务状态 : %s" % desc)
    print("plist    : %s%s" % (launchd_plist_path(WATCH_LABEL),
                               "" if launchd_plist_path(WATCH_LABEL).exists() else "（不存在）"))
    print("当前模式 : %s" % mode)
    print("监听参数 : 轮询 %.1fs / 静默 %.1fs / 兜底 %.0fs" % (w["interval"], w["settle"], w["resync_after"]))
    print("同步日志 : %s" % sp["log"])
    print("服务日志 : %s" % (LAUNCHD_LOG_DIR / "watch.err.log"))
    if not running:
        print("\n启动：bmsync watch-daemon start    （或前台运行观察：bmsync watch）")
    return 0 if running else 1


MODE_LABELS = {
    "manual": "手动：不自动同步，需要时自己跑 bmsync sync",
    "scheduled": "定时：由 launchd 每 N 分钟同步一次（bmsync install-scheduler）",
    "watch": "实时：常驻监听书签文件，一变就同步（bmsync watch / watch-daemon start）",
}


def set_mode(cfg: Dict[str, Any], args: argparse.Namespace, mode: str, quiet: bool = False) -> None:
    target = config_target(cfg, getattr(args, "config", None))
    write_config_keys(target, {"mode": mode})
    cfg["mode"] = mode
    if not quiet:
        print("工作模式已设为：%s —— %s" % (mode, MODE_LABELS.get(mode, "")))
        print("配置写入：%s" % target)


def cmd_mode(cfg: Dict[str, Any], args: argparse.Namespace) -> int:
    """查看或切换工作模式，并把另一种自动方式清理掉，避免两套机制互相打架。"""
    if not args.mode:
        mode = cfg.get("mode") or "manual"
        print("bmsync 工作模式")
        print("-" * 70)
        for key, label in MODE_LABELS.items():
            print("  %s %-10s %s" % ("→" if key == mode else " ", key, label))
        print("\n当前：%s" % mode)
        sched = launchd_plist_path(LAUNCHD_LABEL).exists()
        running, desc = watch_daemon_status() if launchd_plist_path(WATCH_LABEL).exists() else (False, "未安装")
        print("  定时任务  : %s" % ("已安装" if sched else "未安装"))
        print("  常驻监听  : %s" % desc)
        if (mode == "scheduled" and not sched) or (mode == "watch" and not running):
            print("\n注意：模式与已安装的服务不一致，按下面提示补齐即可。")
        print("\n切换：bmsync mode manual | scheduled | watch")
        return 0

    mode = args.mode
    if mode == "manual":
        set_mode(cfg, args, mode)
        stopped = []
        if launchd_plist_path(LAUNCHD_LABEL).exists():
            try:
                cmd_uninstall_scheduler(cfg, args)
                stopped.append("定时任务")
            except OSError as exc:
                eprint("警告：移除定时任务时出错（不影响模式设置）：%s" % exc)
        if launchd_plist_path(WATCH_LABEL).exists():
            try:
                stop_watch_daemon(cfg, args)
                stopped.append("常驻监听")
            except OSError as exc:
                eprint("警告：停止常驻监听时出错（不影响模式设置）：%s" % exc)
        print("已停止自动同步%s。" % ("（%s）" % "、".join(stopped) if stopped else ""))
        return 0
    if mode == "scheduled":
        set_mode(cfg, args, mode)
        if launchd_plist_path(WATCH_LABEL).exists():
            try:
                stop_watch_daemon(cfg, args)
                print("已停止常驻监听，避免两套机制同时写书签。")
            except OSError as exc:
                eprint("警告：停止常驻监听时出错（不影响模式设置）：%s" % exc)
        print("接下来运行：bmsync install-scheduler --interval 60")
        return 0
    # watch
    set_mode(cfg, args, mode)
    if launchd_plist_path(LAUNCHD_LABEL).exists():
        print("检测到已安装定时任务：两者同时运行会重复写书签，建议二选一。")
        if not getattr(args, "keep_scheduler", False):
            cmd_uninstall_scheduler(cfg, args)
            print("已自动移除定时任务（想保留请加 --keep-scheduler）。")
    print("接下来二选一：")
    print("  前台观察：bmsync watch                （Ctrl+C 退出）")
    print("  后台常驻：bmsync watch-daemon start   （登录自启、掉线自动拉起）")
    return 0


def cmd_init_config(cfg: Dict[str, Any], args: argparse.Namespace) -> int:
    target = expand(args.out) if args.out else (APP_DIR / "config.json")
    if target.exists() and not args.force:
        eprint("已存在：%s（用 --force 覆盖）" % target)
        return 1
    sp = state_paths(cfg)
    data = {
        "chrome": {"user_data_dir": "~/Library/Application Support/Google/Chrome", "profile": "Default"},
        "safari": {"bookmarks_path": "~/Library/Safari/Bookmarks.plist"},
        "mode": "manual",
        "sync": {"direction": "both", "prefer": "newest", "skip_folders": list(DEFAULT_SKIP_FOLDERS),
                 "backup_keep": 20, "backup_min_interval": 300, "require_browsers_closed": True,
                 "max_delete_ratio": 0.05, "max_delete_count": 10,
                 "scope": "", "scope_chrome": "", "scope_safari": ""},
        "state_dir": str(sp["dir"]),
        "log_file": str(sp["log"]),
    }
    atomic_write(target, json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))
    print("已生成配置：%s" % target)
    return 0


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def preprocess_globals(argv: Sequence[str]) -> Tuple[List[str], Dict[str, Any]]:
    """
    把 -v/--verbose/--json/-t/--config 从参数里统一摘出来。

    返回 (剩余参数, {"verbose": int, "json": bool, "config": str|None})。

    为什么不在主解析器上直接定义：argparse 解析完子命令后不会再回主解析器找参数，所以
    `sync -v`、`sync --config x.json` 这种写法会报 "unrecognized arguments"；而在主/子
    解析器上各定义一份同名选项，又会在同一个 namespace 上互相覆盖取值（子解析器的 default
    会盖掉主解析器已累计的结果，SUPPRESS 也不可靠）。所以这几个全局选项统一在这里处理，
    写在子命令前后都有效。
    """
    rest: List[str] = []
    opts: Dict[str, Any] = {"verbose": 1, "json": False, "config": None}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--":                      # 之后的一律原样传递
            rest.extend(argv[i:])
            break
        if arg in ("-t", "--json"):
            opts["json"] = True
        elif arg == "--verbose":
            opts["verbose"] += 1
        elif arg == "-v":
            opts["verbose"] += 1
        elif arg.startswith("-v") and not arg.startswith("--") and set(arg[1:]) == {"v"}:
            opts["verbose"] += len(arg) - 1  # -vv / -vvv
        elif arg == "--config" or (arg.startswith("--config=") and arg != "--config="):
            if arg.startswith("--config="):
                opts["config"] = arg.split("=", 1)[1]
            else:
                i += 1
                if i >= len(argv):
                    raise SystemExit("bmsync: 错误：--config 后面缺少文件路径")
                opts["config"] = argv[i]
        else:
            rest.append(arg)
        i += 1
    return rest, opts


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bmsync", description="Chrome ↔ Safari 书签双向同步（macOS，仅用 Python 标准库）",
        epilog="推荐流程：bmsync doctor → bmsync sync --dry-run -v → bmsync sync")
    parser.add_argument("--version", action="version", version="bmsync %s" % __version__)
    # --config / --json / -v 属于全局选项，写在子命令前后都允许，
    # 由 preprocess_globals() 统一抽取后塞回 namespace（见 main）。这里只登记帮助文本。
    parser.add_argument("--config", help="配置文件路径（默认脚本目录或当前目录下的 config.json）")
    parser.add_argument("--json", "-t", action="store_true", help="以 JSON 输出结果（可写在子命令后）")
    parser.add_argument("-v", "--verbose", action="count", default=1, help="更详细输出（可 -vv，可写在子命令后）")
    sub = parser.add_subparsers(dest="command")

    def new_sub(name: str, **kwargs: Any) -> argparse.ArgumentParser:
        return sub.add_parser(name, **kwargs)

    p = new_sub("doctor", help="环境自检：权限、路径、进程、定时任务")
    p.set_defaults(func=cmd_doctor)

    p = new_sub("status", help="查看两边书签数量与上次同步信息")
    p.set_defaults(func=cmd_status)

    p = new_sub("sync", help="执行同步（核心命令）"
                "｜退出码：0 成功 / 2 文件不可读写 / 3 浏览器未退出 / 1 其他错误")
    p.add_argument("--dry-run", "-n", action="store_true", help="只演练，不写任何文件")
    p.add_argument("--force", action="store_true", help="即使浏览器在运行也写入")
    p.add_argument("--prefer", choices=["newest", "chrome", "safari"], help="冲突时以哪边为准")
    p.add_argument("--direction", choices=["both", "chrome2safari", "safari2chrome"], help="同步方向")
    p.add_argument("--list-changes", action="store_true", help="逐条列出改动")
    p.add_argument("--max-show", type=int, default=40, help="演练时最多显示多少条")
    p.add_argument("--quiet", action="store_true", help="静默模式（定时任务用）")
    p.add_argument("--allow-mass-delete", action="store_true",
                   help="允许单次删除大量书签（默认熔断，需要显式确认）")
    p.add_argument("--scope", metavar="目录",
                   help='对称范围：只同步两侧的同名目录（如 "书签栏/同步"）')
    p.add_argument("--scope-chrome", metavar="目录",
                   help="非对称：Chrome 侧参加同步的目录（留空 = 整个书签树）")
    p.add_argument("--scope-safari", metavar="目录",
                   help='非对称：把内容收进 Safari 的这个目录（如 "sync_chrome"）')
    p.add_argument("--no-delete", action="store_true",
                   help="本次同步不删除任何书签，只做新增/修改（最保守）")
    p.add_argument("--allow-flatten", action="store_true",
                   help="目录结构疑似被抹平时也按现状同步（默认拒绝删除）")
    p.set_defaults(func=cmd_sync)

    p = new_sub("export", help="导出为 HTML（可手动导入）或 JSON")
    p.add_argument("--format", choices=["html", "json"], default="html")
    p.add_argument("--out", help="输出目录")
    p.add_argument("--browser", action="append", choices=["chrome", "safari"], help="只导出指定浏览器")
    p.set_defaults(func=cmd_export)

    p = new_sub("import", help="把外部书签文件并入后同步")
    p.add_argument("file", help="HTML 或 JSON 书签文件")
    p.add_argument("--format", choices=["auto", "html", "json"], default="auto")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--max-show", type=int, default=40)
    p.set_defaults(func=cmd_import)

    p = new_sub("init-config", help="生成可编辑的配置文件")
    p.add_argument("--out")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init_config)

    p = new_sub("watch", help="实时同步：监听书签文件变化并自动同步")
    p.add_argument("--once", type=float, metavar="秒", help="跑指定秒数后退出（用于自测）")
    p.add_argument("--verify", action="store_true", help="只自检一次：权限、指纹、参数")
    p.add_argument("--why", type=float, metavar="秒", nargs="?", const=60,
                   help="观察一段时间，报告文件被重写时书签内容是否真的变了（默认 60 秒）")
    p.add_argument("--stop", action="store_true", help="停止常驻监听服务")
    p.add_argument("--no-initial", action="store_true", help="启动时不做兜底同步")
    p.add_argument("--lock", help="锁文件路径（默认 state/bmsync.lock）")
    p.add_argument("--bridge-port", type=int, help="本地桥接端口（默认取配置，0 = 关闭）")
    p.add_argument("--no-bridge", action="store_true", help="不启动桥接服务")
    p.set_defaults(func=cmd_watch)

    p = new_sub("watch-daemon", help="把实时同步装成常驻服务（登录自启、掉线拉起）")
    p.add_argument("action", nargs="?", choices=["status", "start", "stop", "restart"], default="status")
    p.add_argument("--log-dir", help="服务日志目录（默认 ~/Library/Logs/bmsync）")
    p.add_argument("--dry-run", "-n", action="store_true", help="只打印将要写入的 plist")
    p.add_argument("--keep-plist", action="store_true", help="stop 时保留 plist 文件")
    p.set_defaults(func=cmd_watch_daemon)

    p = new_sub("mode", help="查看/切换工作模式：manual | scheduled | watch")
    p.add_argument("mode", nargs="?", choices=["manual", "scheduled", "watch"], help="留空则只显示当前模式")
    p.add_argument("--keep-scheduler", action="store_true", help="切到 watch 时保留已安装的定时任务")
    p.set_defaults(func=cmd_mode)

    p = new_sub("install-scheduler", help="安装 launchd 定时同步任务")
    p.add_argument("--interval", type=int, default=60, help="间隔分钟（默认 60，最小 5）")
    p.add_argument("--log-dir", help="任务日志目录（默认 ~/Library/Logs/bmsync）")
    p.add_argument("--no-verify", dest="verify", action="store_false", default=True,
                   help="安装后不立即触发一次验证")
    p.add_argument("--dry-run", "-n", action="store_true", help="只打印将要写入的 plist，不安装")
    p.set_defaults(func=cmd_install_scheduler)

    p = new_sub("uninstall-scheduler", help="移除定时同步任务")
    p.set_defaults(func=cmd_uninstall_scheduler)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    cleaned, globals_ = preprocess_globals(raw)
    parser = build_parser()
    args = parser.parse_args(cleaned)
    args.verbose = globals_["verbose"]
    args.json = globals_["json"]
    if globals_["config"] is not None:
        args.config = globals_["config"]
    if not getattr(args, "command", None):
        parser.print_help()
        return 1
    cfg = load_config(Path(args.config) if args.config else None)
    if getattr(args, "quiet", False):
        # --quiet 只压掉正常输出；报错仍然打到 stderr，否则定时任务失败时会毫无线索
        args.verbose = 0
    try:
        return args.func(cfg, args)
    except KeyboardInterrupt:
        eprint("\n已中断。")
        return 130
    except Exception as exc:  # noqa: BLE001
        if getattr(args, "verbose", 1) >= 2:
            raise
        eprint("[错误] %s: %s" % (type(exc).__name__, exc))
        eprint("       加 -vv 查看完整调用栈。")
        return 1


if __name__ == "__main__":
    sys.exit(main())
