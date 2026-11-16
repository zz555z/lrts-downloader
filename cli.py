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
        start, end = C.ask_range(total)
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
