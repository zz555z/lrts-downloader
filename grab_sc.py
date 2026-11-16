#!/usr/bin/env python3
"""一键抓取懒人听书的"新鲜 sc"。

流程：
  1. 启动 mitmproxy（只记录请求的插件 addons/sniffer.py）
  2. 自动开启系统代理指向它
  3. 提示你在 App 里打开一本小说、播放任意一集
  4. 轮询抓包文件，一旦抓到 batchGetListenPath 请求（含新鲜 sc），自动：
       - 关闭系统代理
       - 停止 mitmproxy
       - 打印抓到的书号 / sc / 时间
  5. 之后即可用 `python cli.py dl --book <书号>` 下载

安全：脚本用 try/finally 保证退出时一定关代理、停 mitmproxy，避免断网。

用法：
  python grab_sc.py                # 默认监听 8080、Wi-Fi
  python grab_sc.py --port 8080 --service "Wi-Fi" --timeout 180

说明：
  - 需要 sudo 修改系统代理，脚本会在终端里提示你输入密码。
  - 首次使用需先信任 mitmproxy 证书（见 README），否则抓不到 HTTPS。
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
import urllib.parse

ROOT = os.path.dirname(os.path.abspath(__file__))
SNIFFER = os.path.join(ROOT, "addons", "sniffer.py")
SAMPLES = os.path.join(ROOT, "data", "samples", "req_samples.jsonl")
# mitmproxy 配置目录：优先复用"已被系统信任"的证书目录，避免新生成的 CA 未受信导致抓不到
DEFAULT_CONFDIR = os.path.join(tempfile.gettempdir(), "lrts_mitm_conf")
# 可能已受信的候选目录（本项目同级旧目录 / 项目内）
CANDIDATE_CONFDIRS = [
    os.path.join(os.path.dirname(ROOT), "app-scrape", ".mitmproxy"),
    os.path.join(ROOT, ".mitmproxy"),
    DEFAULT_CONFDIR,
]


def _cert_trusted(confdir):
    """判断 confdir 下的 mitmproxy CA 是否已被系统信任。"""
    cert = os.path.join(confdir, "mitmproxy-ca-cert.pem")
    if not os.path.exists(cert):
        return False
    try:
        r = subprocess.run(["security", "verify-cert", "-c", cert],
                           capture_output=True, text=True)
        return "successful" in (r.stdout + r.stderr)
    except Exception:
        return False


def resolve_confdir(explicit=None):
    """选一个可用的 confdir。

    - 显式指定：优先用它（若里面还没证书，mitmproxy 会自动生成，但需你信任）
    - 否则：优先返回"已受信"的候选目录；都不受信时用默认目录
    """
    if explicit:
        return explicit
    for d in CANDIDATE_CONFDIRS:
        if _cert_trusted(d):
            print(f"[*] 复用已受信任的证书目录：{d}")
            return d
    print("[!] 没找到已受信任的 mitmproxy 证书，将使用默认目录生成新证书")
    print("    首次使用需按 README「信任证书」步骤操作，否则抓不到 HTTPS。")
    return DEFAULT_CONFDIR


# ---------------------------------------------------------------- 系统代理
def _run(cmd, check=False):
    """执行命令。sudo 命令会由系统交互式询问密码。"""
    return subprocess.run(cmd, check=check, text=True)


def set_proxy(service, port, enable=True):
    host = "127.0.0.1" if enable else ""
    p = str(port) if enable else "0"
    if platform.system() != "Darwin":
        print(f"[!] 非 macOS：请手动把系统代理设为 {host}:{p}（关闭时留空）")
        return
    print(f"[*] {'开启' if enable else '关闭'}系统代理 {service} -> {host or '(空)'}:{p}")
    _run(["sudo", "networksetup", "-setwebproxy", service, host, p])
    _run(["sudo", "networksetup", "-setsecurewebproxy", service, host, p])
    if not enable:
        _run(["sudo", "networksetup", "-setwebproxystate", service, "off"])
        _run(["sudo", "networksetup", "-setsecurewebproxystate", service, "off"])


def proxy_is_open(service):
    if platform.system() != "Darwin":
        return True
    try:
        out = subprocess.run(["networksetup", "-getwebproxy", service],
                             capture_output=True, text=True).stdout
        return "Server: 127.0.0.1" in out
    except Exception:
        return True


# ---------------------------------------------------------------- mitmproxy
def find_mitmdump():
    for p in ("/opt/homebrew/bin/mitmdump", "/usr/local/bin/mitmdump"):
        if os.path.exists(p):
            return p
    from shutil import which
    return which("mitmdump")


def start_mitm(port, confdir=None):
    mitm = find_mitmdump()
    if not mitm:
        raise SystemExit("找不到 mitmdump，请先 `brew install mitmproxy` 或 pip install mitmproxy")
    confdir = confdir or DEFAULT_CONFDIR
    os.makedirs(confdir, exist_ok=True)
    logf = open(os.path.join(confdir, "mitm.log"), "w")
    proc = subprocess.Popen(
        [mitm, "-s", SNIFFER, "--listen-port", str(port), "--set", f"confdir={confdir}"],
        stdout=logf, stderr=subprocess.STDOUT)
    # 等端口就绪
    for _ in range(20):
        time.sleep(0.3)
        if proc.poll() is not None:
            raise SystemExit("mitmdump 启动失败，请检查是否已安装并信任证书")
        try:
            import socket
            s = socket.create_connection(("127.0.0.1", port), timeout=0.5)
            s.close()
            print(f"[*] mitmproxy 已启动（: {port}，日志 {os.path.join(confdir,'mitm.log')}）")
            return proc, logf
        except OSError:
            continue
    proc.terminate()
    raise SystemExit("mitmproxy 端口未就绪")


# ---------------------------------------------------------------- 检测抓包
def sample_count():
    if not os.path.exists(SAMPLES):
        return 0
    with open(SAMPLES, "rb") as f:
        return sum(1 for _ in f)


def latest_batch_sample():
    """返回最新一条 batchGetListenPath 样本 dict，没有返回 None。"""
    if not os.path.exists(SAMPLES):
        return None
    best = None
    for line in open(SAMPLES, encoding="utf-8", errors="replace"):
        try:
            d = json.loads(line)
        except Exception:
            continue
        if "batchGetListenPath" in (d.get("path") or ""):
            if best is None or d.get("ts", 0) >= best.get("ts", 0):
                best = d
    return best


def parse_book(sample):
    try:
        body = json.loads(sample.get("body") or "{}")
        return body.get("entityId")
    except Exception:
        return None


# ---------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser(description="一键抓取懒人听书新鲜 sc")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--service", default="Wi-Fi", help="网络服务名（macOS，默认 Wi-Fi）")
    ap.add_argument("--timeout", type=int, default=180, help="等待播放的最长秒数")
    ap.add_argument("--confdir", default=None,
                    help="mitmproxy 配置目录（默认自动复用已受信任的证书目录）")
    args = ap.parse_args()

    baseline = sample_count()
    print("=" * 60)
    print(" 懒人听书 · 抓取新鲜 sc")
    print("=" * 60)

    mitm = logf = None
    proxy_on = False
    interrupted = False

    def cleanup():
        nonlocal proxy_on
        print("\n[*] 清理：关闭系统代理、停止 mitmproxy …")
        if proxy_on:
            set_proxy(args.service, args.port, enable=False)
            proxy_on = False
        if mitm and mitm.poll() is None:
            mitm.terminate()
            try:
                mitm.wait(timeout=5)
            except Exception:
                mitm.kill()
        if logf:
            logf.close()

    try:
        confdir = resolve_confdir(args.confdir)
        mitm, logf = start_mitm(args.port, confdir)
        set_proxy(args.service, args.port, enable=True)
        proxy_on = True

        print("\n" + "▶" * 30)
        print(" 现在请打开【懒人听书 App】，找到你想下载的书，")
        print(" 点任意一集【播放】，让它响几秒即可。")
        print(f" （最多等待 {args.timeout} 秒，抓到后会自动收尾）")
        print("▶" * 30 + "\n")

        waited = 0
        got = None
        while waited < args.timeout:
            time.sleep(1)
            waited += 1
            cur = sample_count()
            if cur > baseline:
                smp = latest_batch_sample()
                if smp:
                    got = smp
                    break
                # 有新增但还不是 batchGetListenPath，继续等
                if waited % 5 == 0:
                    print(f"  …已记录 {cur - baseline} 条请求，继续等你点播放")
            elif waited % 15 == 0:
                print(f"  …已等待 {waited}s（还没抓到播放请求，请确认已点播放）")

        if got:
            book = parse_book(got)
            ts = got.get("ts", 0)
            age = time.time() - ts if ts else 0
            q = dict(urllib.parse.parse_qsl(got["path"].split("?", 1)[-1]))
            sc = q.get("sc", "?")
            print("\n" + "✅" * 30)
            print(f" 抓到！书号 bookId = {book}")
            print(f" sc = {sc[:16]}…")
            print(f" 请求时间 = {time.strftime('%H:%M:%S', time.localtime(ts))}（{age:.0f}s 前）")
            print("✅" * 30)
            print(f"\n下一步下载整本：\n  python cli.py dl --book {book}\n")
            return 0
        else:
            print(f"\n[!] {args.timeout}s 内没抓到 batchGetListenPath 请求。")
            print("    请确认：1) mitmproxy 证书已信任 2) App 真的开始播放了 3) 代理确实生效")
            return 1
    except KeyboardInterrupt:
        interrupted = True
        print("\n[!] 已中断")
        return 130
    finally:
        cleanup()
        print("[*] 收尾完成，网络已恢复正常。")


if __name__ == "__main__":
    sys.exit(main())
