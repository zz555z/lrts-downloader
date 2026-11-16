"""mitmproxy 证书检查与安装引导。

抓 sc 依赖 mitmproxy 解密 App 的 HTTPS 流量，前提是系统（及 App）信任 mitmproxy
的根证书。本模块在 grab_sc 启动时自动检查；未信任则引导用户一键安装。

macOS：
  - 检测：security verify-cert -c <certpem>
  - 安装：sudo security add-trusted-cert -d -r trustRoot \
            -k /Library/Keychains/System.keychain <certpem>
"""
from __future__ import annotations

import os
import platform
import subprocess


def cert_path(confdir):
    return os.path.join(confdir, "mitmproxy-ca-cert.pem")


def is_trusted(confdir):
    """证书是否已被系统信任。"""
    cert = cert_path(confdir)
    if not os.path.exists(cert):
        return False
    if platform.system() != "Darwin":
        # 非 macOS 无法可靠检测，返回 None 表示"未知"
        return None
    try:
        r = subprocess.run(["security", "verify-cert", "-c", cert],
                           capture_output=True, text=True)
        return "successful" in (r.stdout + r.stderr)
    except Exception:
        return False


def ensure_cert(confdir, interactive=True):
    """确保 confdir 下的 CA 已受信任。

    返回 True 表示可以继续；False 表示无法抓（用户放弃或环境不支持）。
    会尽量自动完成安装；必要时提示用户操作。
    """
    if not os.path.exists(cert_path(confdir)):
        # 证书还没生成：需要先让 mitmproxy 生成一次
        print("[*] 首次运行：正在生成 mitmproxy 证书…")
        _generate(confdir)
        if not os.path.exists(cert_path(confdir)):
            print("[!] 证书生成失败，请检查 mitmproxy 是否安装正确")
            return False

    trusted = is_trusted(confdir)
    if trusted:
        print("[✓] mitmproxy 证书已验证受信任")
        return True

    if platform.system() != "Darwin":
        print("[!] 非 macOS：请手动把证书安装到系统信任列表后重试：")
        print(f"    {cert_path(confdir)}")
        return interactive and _ask_continue()

    print("\n" + "!" * 60)
    print(" 需要安装并信任 mitmproxy 证书，否则抓不到 App 的 HTTPS 流量。")
    print(f" 证书文件：{cert_path(confdir)}")
    print("!" * 60)
    print(" 方式一（推荐，自动安装到系统钥匙串，需要 sudo 密码）：")

    if interactive:
        try:
            ans = input(" 现在自动安装并信任？[Y/n] ").strip().lower()
        except EOFError:
            ans = "y"
        if ans in ("", "y", "yes"):
            if _install_darwin(confdir):
                print("[✓] 证书已安装并设为信任")
                return True
            print("[!] 自动安装失败，请用下面的方式二手动安装")

    print("\n 方式二（手动）：")
    print("   1) 双击打开证书文件（或在「钥匙串访问」里导入）：")
    print(f"      open \"{cert_path(confdir)}\"")
    print("   2) 在「钥匙串访问 → 系统」里找到 mitmproxy 证书，双击")
    print("   3) 展开「信任」，把「使用此证书时」设为『始终信任』")
    print("   4) 关闭窗口，输入密码确认")
    print("\n 装好后重新运行本脚本即可。")
    return interactive and _ask_continue()


def _generate(confdir):
    """启动一次 mitmproxy 生成证书后立刻退出。"""
    from shutil import which
    mitm = which("mitmdump") or "/opt/homebrew/bin/mitmdump"
    os.makedirs(confdir, exist_ok=True)
    try:
        p = subprocess.Popen([mitm, "--listen-port", "0", "--set", f"confdir={confdir}"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # 等证书出现
        import time
        for _ in range(40):
            time.sleep(0.25)
            if os.path.exists(cert_path(confdir)):
                break
        p.terminate()
        try:
            p.wait(timeout=3)
        except Exception:
            p.kill()
    except Exception as e:      # noqa: BLE001
        print(f"[!] 生成证书出错：{e}")


def _install_darwin(confdir):
    cert = cert_path(confdir)
    try:
        r = subprocess.run(
            ["sudo", "security", "add-trusted-cert", "-d", "-r", "trustRoot",
             "-k", "/Library/Keychains/System.keychain", cert],
            text=True)
        if r.returncode != 0:
            return False
        return is_trusted(confdir)
    except Exception as e:      # noqa: BLE001
        print(f"[!] 安装证书出错：{e}")
        return False


def _ask_continue():
    try:
        ans = input(" 仍然继续尝试抓取吗？(可能抓不到) [y/N] ").strip().lower()
    except EOFError:
        return False
    return ans in ("y", "yes")
