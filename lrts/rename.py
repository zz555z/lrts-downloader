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
    if not sections:
        print("  [!] 目录为空，无法补全章节名。")
        C.print_menu_rate_limit_hint()
        return 0, 0
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
        if book:
            # 指定了书号但没找到带该 bookId 的书目录：尝试直接对整个 audio 下的目录处理
            candidates = [os.path.join(OUT, d) for d in os.listdir(OUT)
                          if os.path.isdir(os.path.join(OUT, d))] if os.path.isdir(OUT) else []
            if not candidates:
                print(f"没有可处理的书目录（data/audio 为空）")
                return 0, 0
            print(f"[*] 未找到含 bookId={book} 的 _manifest.json，"
                  f"将对 data/audio 下所有目录按 bookId={book} 的目录重命名")
            targets = [(d, str(book)) for d in candidates]
        else:
            print("没有可处理的书（需要 data/audio/<书名>/_manifest.json 且含 bookId；"
                  "或用 `python cli.py rename --book <书号>` 指定书号）")
            return 0, 0
    tr = ts = 0
    for dpath, bid in targets:
        r, s = rename_book(dpath, bid)
        tr += r; ts += s
    return tr, ts


def verify_and_fix(dst_dir, book, book_name=None):
    """下载完成后自检并修复：①书目录名 ②每集章节名。

    返回 dict：{dir_ok, dir_fixed, ep_total, ep_with_title, ep_fixed, problems}
    """
    import glob
    book = str(book)
    result = {"dir_ok": True, "dir_fixed": False, "ep_total": 0,
              "ep_with_title": 0, "ep_fixed": 0, "problems": [],
              "final_dir": dst_dir}

    # ---- ① 书目录名校验 ----
    dname = os.path.basename(dst_dir.rstrip("/"))
    looks_bad = bool(re.fullmatch(r"book_\d+", dname)) or not dname.strip()
    real_name = book_name or C.get_book_name(book) or fetch_name_via_sample(book)
    if looks_bad and real_name:
        target = os.path.join(OUT, C.safe_dirname(real_name))
        if os.path.abspath(target) != os.path.abspath(dst_dir):
            try:
                # 若目标已存在（书名目录已建），把文件搬进去——同名不覆盖，只搬缺失的
                if os.path.exists(target):
                    moved = 0
                    for f in os.listdir(dst_dir):
                        src_f = os.path.join(dst_dir, f)
                        dst_f = os.path.join(target, f)
                        if os.path.isdir(src_f):
                            # 子目录（罕见）整体搬，不覆盖
                            if not os.path.exists(dst_f):
                                os.replace(src_f, dst_f); moved += 1
                        else:
                            if not os.path.exists(dst_f):
                                os.replace(src_f, dst_f); moved += 1
                            # 同名文件：目标已有，说明是同一集（内容一致），保留目标、丢弃源副本
                    # 只有在源目录已空时才删壳；否则保留并警告（防丢）
                    remain = os.listdir(dst_dir)
                    if not remain:
                        os.rmdir(dst_dir)
                    else:
                        result["problems"].append(
                            f"合并后旧目录仍有 {len(remain)} 个文件（可能重名冲突），已保留 {dst_dir}")
                else:
                    os.rename(dst_dir, target)
                print(f"  [自检] 书目录名不规范，已修正：{dname} -> {real_name}")
                result["dir_ok"] = False
                result["dir_fixed"] = True
                dst_dir = target
                book_name = real_name
                result["final_dir"] = dst_dir
            except Exception as e:      # noqa: BLE001
                result["problems"].append(f"书目录改名失败：{e}")

    # ---- ② 每集章节名校验 ----
    files = [f for f in os.listdir(dst_dir) if f.endswith(".m4a")]
    result["ep_total"] = len(files)
    try:
        # get_all_sections 已内置读取抓包缓存(data/samples/menu_)与在线目录
        _, sections = C.get_all_sections(book)
    except Exception:
        sections = {}

    for f in files:
        m = re.fullmatch(r"第(\d+)集(.*)\.m4a", f)
        if not m:
            continue
        has_title = bool(m.group(2).strip("_ "))
        if has_title:
            result["ep_with_title"] += 1

    # 修复：纯"第N集"但目录里有章节名 → 补上
    fixed = 0
    if sections:
        for f in list(os.listdir(dst_dir)):
            m = re.fullmatch(r"第(\d+)集\.m4a", f)   # 纯第N集无标题
            if not m:
                continue
            sec = int(m.group(1))
            name = sections.get(sec)
            if not name:
                continue
            newname = C.episode_filename(sec, name, None)
            bn = C.safe_name(os.path.basename(dst_dir).split("|")[0])
            if newname == f"第{sec}集_{bn}.m4a":
                continue
            src = os.path.join(dst_dir, f)
            dst = os.path.join(dst_dir, newname)
            if not os.path.exists(dst):
                os.rename(src, dst)
                fixed += 1
        if fixed:
            print(f"  [自检] 补全 {fixed} 集的章节名")
            result["ep_fixed"] = fixed

    # 汇总问题
    if result["ep_total"] and result["ep_with_title"] == 0 and not sections:
        result["problems"].append(
            "所有集都只有'第N集'无章节名；未能拿到目录（getBookMenu限流/无抓包缓存），"
            "可稍后运行 `python cli.py rename --book %s` 补全" % book)
    elif result["ep_total"] and result["ep_with_title"] < result["ep_total"] - result["ep_fixed"]:
        miss = result["ep_total"] - result["ep_with_title"] - result["ep_fixed"]
        if miss > 0:
            result["problems"].append(f"{miss} 集仍缺章节名（目录不全）")

    # 更新 manifest 书名
    try:
        mf = os.path.join(dst_dir, "_manifest.json")
        if os.path.exists(mf):
            j = json.load(open(mf, encoding="utf-8"))
            j["book"] = os.path.basename(dst_dir)
            json.dump(j, open(mf, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    except Exception:
        pass

    # 打印结论
    if not result["problems"] and not result["dir_fixed"] and not result["ep_fixed"]:
        print(f"  [自检] ✔ 书名与章节名正常（{result['ep_total']} 集，"
              f"{result['ep_with_title']} 集带章节名）")
    for p in result["problems"]:
        print(f"  [自检] ⚠ {p}")
    return result


def fetch_name_via_sample(book):
    """从抓包样本的 bookInfoPage 现调拿书名（延迟导入避免循环依赖）。"""
    try:
        from . import download as D
        return D.fetch_name_from_sample(book)
    except Exception:
        return None
