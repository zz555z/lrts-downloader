#!/usr/bin/env python3
"""懒人听书下载器 · 命令行入口。

子命令：
  dl       批量下载一本书
  rename   按 App 真实章节名重命名已下载的音频
  menu     查看某本书的目录（验证接口/缓存是否可用）

示例：
  python cli.py dl --book 43913
  python cli.py dl --book 43913 --start 100 --end 200
  python cli.py rename            # 重命名全部已下书
  python cli.py rename --book 43913
  python cli.py menu --book 43913
"""
import argparse
import sys

from lrts import common as C
from lrts import download as D
from lrts import rename as R


def _read_line(prompt):
    """读一行输入。返回 None 表示输入流已结束（EOF），便于安全退出。"""
    try:
        line = input(prompt)
    except EOFError:
        return None
    return line


def _ask_range(total):
    """交互式询问下载范围。返回 (start, end)。

    total 已知总集数时给出提示；未知则仍可自定义。
    - 选 1：全本（start=1, end=total 或 0 表示让下载器自动探测）
    - 选 2：自定义 a-b

    输入流结束（EOF）或连续空输入时，安全回退到「全本」，绝不无限循环。
    """
    total_txt = f"（全书共 {total} 集）" if total else ""
    print("\n要下载多少集？" + total_txt)
    print("  1) 全本")
    print("  2) 自定义范围（如 1-2 表示第1集到第2集）")
    eof_streak = 0
    while True:
        line = _read_line("请输入 1 或 2 后回车：")
        if line is None:                 # EOF：直接按全本处理
            print("\n[!] 输入已结束，默认下载全本")
            return 1, (total or 0)
        choice = line.strip()
        if choice == "":
            eof_streak += 1
            if eof_streak >= 3:
                print("\n[!] 连续无输入，默认下载全本")
                return 1, (total or 0)
            continue
        eof_streak = 0
        if choice == "1":
            return 1, (total or 0)
        if choice == "2":
            while True:
                s = _read_line("请输入起始集号：")
                if s is None:
                    print("\n[!] 输入已结束，默认下载全本")
                    return 1, (total or 0)
                e = _read_line("请输入结束集号：")
                if e is None:
                    print("\n[!] 输入已结束，默认下载全本")
                    return 1, (total or 0)
                try:
                    start, end = int(s.strip()), int(e.strip())
                except ValueError:
                    print("  请输入数字，例如起始 1、结束 2")
                    continue
                if start < 1 or end < start:
                    print(f"  范围不合法：应满足 1 <= 起始 <= 结束（你输入 {start}-{end}）")
                    continue
                if total and end > total:
                    print(f"  结束集号超过全书总集数 {total}，已按 {total} 处理")
                    end = total
                return start, end
        print("  请输入 1 或 2")


def cmd_dl(args):
    start, end = args.start, args.end
    # 未显式指定范围（start 默认 1 且 end 默认 0）且为交互终端时，询问用户
    explicit = (args.end != 0) or (args.start != 1)
    if not explicit and not args.dry and sys.stdin.isatty():
        # 先尝试拿总集数用于提示（失败不影响）
        total = 0
        try:
            meta, _ = C.get_all_sections(args.book, verbose=False)
            total = meta.get("total") or 0
        except Exception:
            pass
        start, end = _ask_range(total)
        print(f"[*] 已选择：第 {start} 集 ~ {'全本' if not end else end} 集\n")
    D.download_book(args.book, start=start, end=end,
                    dry=args.dry, no_menu=args.no_menu, name=args.name)


def cmd_rename(args):
    R.rename_all(book=args.book)


def cmd_menu(args):
    try:
        meta, sections = C.get_all_sections(args.book)
    except Exception as e:      # noqa: BLE001
        print(f"拉目录失败：{e}")
        sys.exit(1)
    print(f"书名: {C.get_book_name(args.book) or '?'}")
    print(f"总集数: {meta.get('total')}  章节名: {len(sections)}")
    for sec in sorted(sections)[: args.show]:
        print(f"  第{sec}集 -> {sections[sec]}")


def main():
    ap = argparse.ArgumentParser(description="懒人听书下载器")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("dl", help="批量下载一本书")
    p.add_argument("--book", required=True, help="书籍 bookId")
    p.add_argument("--start", type=int, default=1)
    p.add_argument("--end", type=int, default=0, help="结束集号（默认整本）")
    p.add_argument("--dry", action="store_true", help="只探测不下载")
    p.add_argument("--no-menu", action="store_true", help="不拉目录，按 section 下载")
    p.add_argument("--name", default=None, help="自定义书目录名（默认自动获取书名）")
    p.set_defaults(func=cmd_dl)

    p = sub.add_parser("rename", help="按 App 章节名重命名")
    p.add_argument("--book", default=None, help="bookId（默认全部）")
    p.set_defaults(func=cmd_rename)

    p = sub.add_parser("menu", help="查看目录")
    p.add_argument("--book", required=True)
    p.add_argument("--show", type=int, default=10)
    p.set_defaults(func=cmd_menu)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
