"""按 App 真实章节名重命名已下载的音频。

getBookMenu 免签名，返回每集 section -> name（name 已含"第N集_章节名"）。
本模块把 `data/audio/<书名>/` 下的 `第N集[_旧标题].m4a` 统一改成 App 的命名。
自带目录缓存，重复运行幂等。
"""
from __future__ import annotations

import json
import os
import re

from . import common as C

OUT = os.path.join(C.DATA_DIR, "audio")


def _books(arg=None):
    """枚举待处理的书目录，返回 [(dir_path, bookId)]。"""
    res = []
    if not os.path.isdir(OUT):
        return res
    for d in os.listdir(OUT):
        dp = os.path.join(OUT, d)
        if not os.path.isdir(dp):
            continue
        bid = None
        mf = os.path.join(dp, "_manifest.json")
        if os.path.exists(mf):
            try:
                bid = str(json.load(open(mf, encoding="utf-8")).get("bookId") or "")
            except Exception:
                pass
        if arg and bid != str(arg):
            continue
        if bid:
            res.append((dp, bid))
    return res


def rename_book(dpath, book):
    """重命名一本书。返回 (renamed, skipped)。"""
    print(f"\n[*] {dpath}  (bookId={book})")
    try:
        _, sections = C.get_all_sections(book)
    except Exception as e:      # noqa: BLE001
        print(f"  [!] 拉目录失败（{e}），跳过")
        return 0, 0
    print(f"  [*] 拿到 {len(sections)} 集章节名")
    bn = C.safe_name(os.path.basename(dpath).split("|")[0])
    renamed = skipped = 0
    for f in os.listdir(dpath):
        m = re.fullmatch(r"第(\d+)集.*\.m4a", f)
        if not m:
            continue
        sec = int(m.group(1))
        name = sections.get(sec)
        if not name:
            skipped += 1
            continue
        newname = C.episode_filename(sec, name, None)
        # 标题若等于书名，则退化为纯 "第N集"
        if newname == f"第{sec}集_{bn}.m4a":
            newname = f"第{sec}集.m4a"
        src = os.path.join(dpath, f)
        dst = os.path.join(dpath, newname)
        if src == dst or os.path.exists(dst):
            skipped += 1
            continue
        os.rename(src, dst)
        renamed += 1
    print(f"  [*] 改名 {renamed}，跳过 {skipped}")
    return renamed, skipped


def rename_all(book=None):
    """重命名所有（或指定）书。返回总计 (renamed, skipped)。"""
    targets = _books(book)
    if not targets:
        print("没有可处理的书（需要 data/audio/<书名>/_manifest.json 且含 bookId）")
        return 0, 0
    tr = ts = 0
    for dpath, bid in targets:
        r, s = rename_book(dpath, bid)
        tr += r; ts += s
    return tr, ts
