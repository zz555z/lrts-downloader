#!/usr/bin/env python3
"""data/audio 保护工具。

背景：data/audio 存放已下载的音频（可能很大、耗费了流量），一旦误删代价很高。
本模块提供安全删除，拒绝删除 data/audio 目录本身及其内容，避免手滑/脚本误删。

代码里所有"清理"操作应走 safe_remove()，它会对 data/audio 下的路径直接拒绝。
"""
import os

_ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(_ROOT, "data")
AUDIO_DIR = os.path.join(DATA_DIR, "audio")


def _is_under_audio(path):
    """路径是否位于 data/audio 之内（含 audio 目录本身）。"""
    try:
        ap = os.path.abspath(path)
        au = os.path.abspath(AUDIO_DIR)
        return ap == au or ap.startswith(au + os.sep)
    except Exception:
        return True  # 判断失败时保守视为受保护


def safe_remove(path, *, force=False):
    """安全删除：默认禁止删除 data/audio 下的任何东西。

    force=True 才允许（仅供极少数需要，比如用户明确选择删除某集）。
    返回 True=已删除 / False=被保护拒绝。
    """
    import shutil
    if not os.path.exists(path):
        return True
    if _is_under_audio(path) and not force:
        print(f"[保护] 拒绝删除 data/audio 下的内容：{path}")
        print("       该目录存放已下载音频，误删代价大。如确需删除请显式传 force=True。")
        return False
    if os.path.isdir(path):
        shutil.rmtree(path)
    else:
        os.remove(path)
    return True


if __name__ == "__main__":
    print(f"data/audio 受保护目录：{AUDIO_DIR}")
    print("代码中请用 safe_remove() 做删除，避免误删已下载音频。")
