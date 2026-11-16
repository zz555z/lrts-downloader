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


def _fingerprint(cert_pem):
    """证书 SHA1 指纹（用于在钥匙串里比对是否是同一张证书）。"""
    try:
        r = subprocess.run(["openssl", "x509", "-in", cert_pem, "-noout",
                            "-fingerprint", "-sha1"], capture_output=True, text=True)
        # 形如 sha1 Fingerprint=AA:BB:...
        line = [x for x in (r.stdout + r.stderr).splitlines() if "Fingerprint" in x]
        if line:
            return line[0].split("=", 1)[1].strip()
    except Exception:
        pass
    return None


def _installed_in_keychain(cert_pem):
    """该证书是否已存在于任一钥匙串（登录 or 系统）。"""
    fp = _fingerprint(cert_pem)
    if not fp:
        return False
    for keychain in ("login.keychain-db", "System.keychain"):
        try:
            r = subprocess.run(["security", "find-certificate", "-a", "-Z", "-c", "mitmproxy",
                                "/Library/Keychains/System.keychain"
                                if keychain == "System.keychain"
                                else os.path.expanduser("~/Library/Keychains/login.keychain-db")],
                               capture_output=True, text=True)
            if fp.replace(":", "").upper() in r.stdout.replace(":", "").upper() \
               or fp.upper() in r.stdout.upper():
                return True
        except Exception:
            continue
    return False


def is_trusted(confdir):
    """证书是否已被系统信任（且确实是这张证书）。

    判定：证书存在 + 该指纹已装入钥匙串 + verify-cert 通过。
    """
    cert = cert_path(confdir)
    if not os.path.exists(cert):
        return False
    if platform.system() != "Darwin":
        return None  # 非 macOS 无法可靠检测
    # 1) 必须在某个钥匙串里（说明装过）
    if not _installed_in_keychain(cert):
        return False
    # 2) 必须受信
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
        print("[✓] mitmproxy 证书已安装且受信任，无需重复安装")
        return True

    if platform.system() != "Darwin":
        print("[!] 非 macOS：请手动把证书安装到系统信任列表后重试：")
        print(f"    {cert_path(confdir)}")
        return interactive and _ask_continue()

    # 区分状态，给明确提示
    installed = _installed_in_keychain(cert_path(confdir))
    print("\n" + "!" * 60)
    if installed:
        print(" 检测到证书已安装，但【未被信任】。需要把它设为『始终信任』。")
    else:
        print(" 检测到 mitmproxy 证书【尚未安装/信任】，装一次即可（之后不会再问）。")
    print(" 为什么需要：抓 sc 要解密 App 的 HTTPS，必须信任 mitmproxy 根证书。")
    print(f" 证书文件：{cert_path(confdir)}")
    print("!" * 60)
    print(" 方式一（推荐，自动安装到系统钥匙串并信任，需要 sudo 密码）：")

    if interactive:
        try:
            ans = input(" 现在自动安装并信任？[Y/n] ").strip().lower()
        except EOFError:
            ans = "y"
        if ans in ("", "y", "yes"):
            if _install_darwin(confdir):
                print("[✓] 证书已安装并设为信任（以后不再询问）")
                return True
            print("[!] 自动安装失败，请用下面的方式二手动安装")

    print("\n 方式二（手动）：")
    print("   1) 双击打开证书文件（或在「钥匙串访问」里导入）：")
    print(f"      open \"{cert_path(confdir)}\"")
    print("   2) 在「钥匙串访问 → 系统」里找到 mitmproxy 证书，双击")
    print("   3) 展开「信任」，把「使用此证书时」设为『始终信任』")
    print("   4) 关闭窗口，输入密码确认")
    print("\n 装好后重新运行本脚本即可（脚本会自动检测到，不再询问）。")
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
