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

    # ---- 第二遍：把结果套到统一树上（先删后增改，保证父子顺序） ----
    for item_id in sorted([i for i, d in actions.items() if d == "delete"],
                          key=lambda i: -len(merged.path_ids(i))):
        if item_id in merged.items:
            result.tombstones.extend(_remove_subtree(merged, item_id))
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
            parent = OTHER_ID if OTHER_ID in merged.items else None
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
    "sync": {
        "direction": "both",
        "prefer": "newest",
        "skip_folders": list(DEFAULT_SKIP_FOLDERS),
        "backup_keep": 20,
        "require_browsers_closed": True,
    },
    "state_dir": str(APP_DIR / "state"),
    "log_file": str(APP_DIR / "state" / "bmsync.log"),
}


def expand(path: Any) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(path))))


def load_config(path: Optional[Path]) -> Dict[str, Any]:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    candidates = [Path(path)] if path else [APP_DIR / "config.json", Path.cwd() / "config.json"]
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
            "log": expand(cfg.get("log_file") or (d / "bmsync.log"))}


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
               summary: Dict[str, Any], tombstones: Iterable[str]) -> None:
    state["version"] = STATE_VERSION
    state["base"] = {
        "items": {i: it.to_state() for i, it in tree.items.items()},
        "children": {k: list(v) for k, v in tree.children.items()},
    }
    state["saved_at"] = now_ts()
    state["saved_at_human"] = fmt_ts(state["saved_at"])
    tomb = state.setdefault("tombstones", {})
    for item_id in tombstones:
        tomb[item_id] = now_ts()
    cutoff = now_ts() - TOMBSTONE_TTL
    state["tombstones"] = {k: v for k, v in tomb.items() if isinstance(v, (int, float)) and v > cutoff}
    history = state.setdefault("history", [])
    history.append({"ts": now_ts(), "human": fmt_ts(now_ts()), "summary": summary})
    state["history"] = history[-50:]
    atomic_write(sp["state"], json.dumps(state, ensure_ascii=False, indent=1).encode("utf-8"))


def append_log(sp: Dict[str, Path], lines: Sequence[str]) -> None:
    try:
        sp["log"].parent.mkdir(parents=True, exist_ok=True)
        with open(sp["log"], "a", encoding="utf-8") as fh:
            for line in lines:
                fh.write(line.rstrip("\n") + "\n")
    except OSError:
        pass


def make_backup(path: Path, backups_dir: Path, keep: int = 20) -> Optional[Path]:
    if not path.exists():
        return None
    backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    tag = "chrome" if path.name == "Bookmarks" else safe_filename(path.parent.name or path.name)
    dest = backups_dir / ("%s.%s%s" % (tag, stamp, path.suffix or ".bak"))
    try:
        shutil.copy2(path, dest)
    except OSError as exc:
        eprint("备份失败（继续执行）：%s：%s" % (dest, exc))
        return None
    existing = sorted(backups_dir.glob("%s.*" % tag))
    if keep > 0:
        for stale in existing[:-keep]:
            try:
                stale.unlink()
            except OSError:
                pass
    return dest


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
    installed = LAUNCHD_PATH.exists()
    print("  定时任务 : %s" % ("已安装（%s）" % LAUNCHD_PATH if installed else "未安装（bmsync install-scheduler）"))
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

    # 1) 权限
    for backend in (chrome, safari):
        readable, msg = check_readable(backend.bookmarks_path)
        if not readable:
            eprint("[错误] 无法读取 %s 书签：%s\n       文件：%s" % (backend.label, msg, backend.bookmarks_path))
            if backend.is_safari:
                eprint("       解决：系统设置 → 隐私与安全性 → 完全磁盘访问权限 → 勾选运行本脚本的程序，并重启它。")
            return 2
        if not dry_run:
            writable, msg_w = check_writable(backend.bookmarks_path)
            if not writable:
                eprint("[错误] 无法写入 %s 书签：%s\n       文件：%s" % (backend.label, msg_w, backend.bookmarks_path))
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
            eprint("[错误] 请先退出：%s" % "、".join(running))
            eprint("       浏览器运行时会用内存中的旧数据覆盖改动；确认无碍可加 --force。")
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

    # 5) 合并
    result = merge(base, left, right, prefer=prefer, extra_sources=extra_sources, verbose=verbose)
    merged = result.merged
    counts = {side: Counter(c.kind for c in changes) for side, changes in result.changes.items()}
    plan = {"chrome": plan_changes(merged, chrome_tree), "safari": plan_changes(merged, safari_tree)}

    summary = {
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
        keep = cfg["sync"].get("backup_keep", 20)
        backups = []
        for backend in (chrome, safari):
            b = make_backup(backend.bookmarks_path, sp["backups"], keep)
            if b:
                backups.append(str(b))
        try:
            # direction 表示"以谁为源"：chrome2safari 只写目标 Safari，safari2chrome 只写目标 Chrome
            if direction != "chrome2safari":
                steps_c = chrome.write_tree(merged, chrome.build_native_map(chrome.read_raw(), merged))
                log("  Chrome 写入：%s" % ("；".join(steps_c) or "无变化"), verbose)
            if direction != "safari2chrome":
                steps_s = safari.write_tree(merged)
                log("  Safari 写入：%s" % ("；".join(steps_s) or "无变化"), verbose)
        except Exception:
            eprint("[错误] 写入失败，原文件已备份在：%s" % sp["backups"])
            raise

        save_state(sp, state, merged, summary, result.tombstones)
        lines = ["[%s] %s" % (fmt_ts(now_ts()), json.dumps(summary, ensure_ascii=False))]
        for side, steps in plan.items():
            for step in steps:
                lines.append("    %-7s %s" % (side, step))
        for b in backups:
            lines.append("    backup  %s" % b)
        append_log(sp, lines)
        log("\n状态已保存：%s" % sp["state"], verbose)
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
# 定时任务
# --------------------------------------------------------------------------------------

LAUNCHD_LABEL = "com.bmsync.sync"
LAUNCHD_PATH = HOME / ("Library/LaunchAgents/%s.plist" % LAUNCHD_LABEL)


def cmd_install_scheduler(cfg: Dict[str, Any], args: argparse.Namespace) -> int:
    script = Path(__file__).resolve()
    # 优先用系统自带的 python3：路径稳定，且方便在"完全磁盘访问权限"里单独勾选
    python = "/usr/bin/python3" if Path("/usr/bin/python3").exists() else (sys.executable or "python3")
    interval = max(5, int(args.interval)) * 60
    log_dir = state_paths(cfg)["dir"]
    log_dir.mkdir(parents=True, exist_ok=True)
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
    if getattr(args, "dry_run", False):
        print("将写入：%s（每 %d 分钟一次）\n" % (LAUNCHD_PATH, interval // 60))
        print(data.decode("utf-8"))
        print("演练模式：未写入文件、未调用 launchctl。")
        return 0
    LAUNCHD_PATH.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(LAUNCHD_PATH, data)
    subprocess.run(["launchctl", "unload", str(LAUNCHD_PATH)], capture_output=True)
    res = subprocess.run(["launchctl", "load", str(LAUNCHD_PATH)], capture_output=True, text=True)
    print("已写入定时任务：%s（每 %d 分钟一次）" % (LAUNCHD_PATH, interval // 60))
    if res.returncode != 0:
        eprint("launchctl load 失败：%s%s" % (res.stdout, res.stderr))
        eprint("可手动执行：launchctl load -w %s" % LAUNCHD_PATH)
        return 1
    print("查看：launchctl list | grep bmsync     立即运行：launchctl start %s" % LAUNCHD_LABEL)
    print("注意：launchd 拉起的进程同样受 TCC 限制，需要给 %s 授予完全磁盘访问权限" % python)
    print("      （在“完全磁盘访问权限”里点 + 号，按 Cmd+Shift+G 输入上面的路径即可添加）。")
    return 0


def cmd_uninstall_scheduler(cfg: Dict[str, Any], args: argparse.Namespace) -> int:
    if not LAUNCHD_PATH.exists():
        print("未安装定时任务。")
        return 0
    subprocess.run(["launchctl", "unload", str(LAUNCHD_PATH)], capture_output=True)
    LAUNCHD_PATH.unlink()
    print("已移除 %s" % LAUNCHD_PATH)
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
        "sync": {"direction": "both", "prefer": "newest", "skip_folders": list(DEFAULT_SKIP_FOLDERS),
                 "backup_keep": 20, "require_browsers_closed": True},
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

    p = new_sub("sync", help="执行同步（核心命令）")
    p.add_argument("--dry-run", "-n", action="store_true", help="只演练，不写任何文件")
    p.add_argument("--force", action="store_true", help="即使浏览器在运行也写入")
    p.add_argument("--prefer", choices=["newest", "chrome", "safari"], help="冲突时以哪边为准")
    p.add_argument("--direction", choices=["both", "chrome2safari", "safari2chrome"], help="同步方向")
    p.add_argument("--list-changes", action="store_true", help="逐条列出改动")
    p.add_argument("--max-show", type=int, default=40, help="演练时最多显示多少条")
    p.add_argument("--quiet", action="store_true", help="静默模式（定时任务用）")
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

    p = new_sub("install-scheduler", help="安装 launchd 定时同步任务")
    p.add_argument("--interval", type=int, default=60, help="间隔分钟（默认 60，最小 5）")
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
