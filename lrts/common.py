"""公共工具：HTTP（带重试）、目录抓取（含缓存）、文件名清洗。

设计要点：
  - getBookMenu 是懒人听书 H5 的免签名公开接口，可直接拉全书章节名；
    但它有频率风控（连续翻几十页会返回 {"status":114,"msg":"非法请求AA"}），
    因此这里做了本地缓存（默认 7 天）+ 翻页间隔，避免频繁请求。
  - 所有网络请求带指数退避重试。
"""
from __future__ import annotations

import json
import os
import re
import ssl
import time
import urllib.request

# ---- 基础配置 -------------------------------------------------------------
H5 = "https://m.lrts.me"
UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_3 like Mac OS X) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1")

# 项目内数据目录（缓存 / 下载输出都放这里）
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(_ROOT, "data")
CACHE_DIR = os.path.join(DATA_DIR, "cache")
os.makedirs(CACHE_DIR, exist_ok=True)

CACHE_TTL = 7 * 86400          # 目录缓存有效期：7 天
MENU_PAGE_DELAY = 0.6          # 翻页间隔（秒），防触发风控

# 音频直链下载用的 SSL 上下文（部分 CDN 证书链不全）+ 强制直连（不走系统代理）
CTX_NOVERIFY = ssl.create_default_context()
CTX_NOVERIFY.check_hostname = False
CTX_NOVERIFY.verify_mode = ssl.CERT_NONE
OPENER_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# ---- HTTP -----------------------------------------------------------------
def http(url, *, data=None, headers=None, method=None, timeout=30,
         retries=3, backoff=1.5, ctx=CTX_NOVERIFY, direct=False):
    """带指数退避重试的 HTTP 请求。返回 (status, body_bytes)，失败抛最后异常。

    direct=True 时使用不走系统代理的 opener（下载音频直链用）。
    """
    hdrs = {"User-Agent": UA, **(headers or {})}
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
            if direct:
                r = OPENER_DIRECT.open(req, timeout=timeout)
            else:
                r = urllib.request.urlopen(req, timeout=timeout, context=ctx)
            return r.status, r.read()
        except Exception as e:          # noqa: BLE001 - 需要兜住所有网络异常
            last = e
            if attempt < retries - 1:
                time.sleep(backoff ** attempt)
    raise last


# ---- 章节目录 -------------------------------------------------------------
def fetch_menu_page(book_id, page, retries=3):
    """拉 getBookMenu 单页（每页 30 集）。返回解析后的 dict。"""
    url = f"{H5}/ajax/getBookMenu?bookId={book_id}&pageNum={page}"
    _, body = http(url, headers={"Referer": f"{H5}/book/{book_id}"},
                   timeout=20, retries=retries)
    try:
        return json.loads(body.decode("utf-8", "replace"))
    except Exception:
        return {}


def get_all_sections(book_id, verbose=True, use_cache=True):
    """拉全书目录，返回 (meta, sections)。

    meta     : {"total": 总集数}
    sections : {section:int -> name:str}（name 为 App 原始显示名，如 "第017集_试探"）

    带本地缓存（默认 7 天）。若接口被风控（status=114），抛 RuntimeError。
    """
    cache_path = os.path.join(CACHE_DIR, f"menu_{book_id}.json")
    if use_cache and os.path.exists(cache_path):
        try:
            data = json.load(open(cache_path, encoding="utf-8"))
            if time.time() - data.get("_ts", 0) < CACHE_TTL and data.get("sections"):
                if verbose:
                    print(f"  [缓存] {book_id}: {len(data['sections'])} 集")
                return data["meta"], {int(k): v for k, v in data["sections"].items()}
        except Exception:
            pass

    sections: dict[int, str] = {}
    meta = {"total": None}
    page = 1
    while True:
        j = fetch_menu_page(book_id, page)
        st = j.get("status")
        if page == 1 and st != 0:
            raise RuntimeError(
                f"getBookMenu 返回异常 status={st} msg={j.get('msg')} "
                f"（可能触发风控/非法请求AA，请稍后再试，或先在 App 播放一集）")
        if page == 1:
            meta["total"] = j.get("sections")
        lst = j.get("list") or []
        if not lst:
            break
        for x in lst:
            sections[int(x["section"])] = x.get("name") or ""
        if verbose:
            print(f"  目录 page {page}: +{len(lst)} (累计 {len(sections)})", end="\r")
        page += 1
        if page > 500:
            break
        time.sleep(MENU_PAGE_DELAY)
    if verbose:
        print()

    if sections:
        try:
            json.dump({"_ts": time.time(), "meta": meta,
                       "sections": {str(k): v for k, v in sections.items()}},
                      open(cache_path, "w", encoding="utf-8"), ensure_ascii=False)
        except Exception:
            pass
    return meta, sections


def get_book_name(book_id):
    """拿书名。优先本地已下目录清单 / 缓存，其次请求 H5。失败返回 None。"""
    audio_out = os.path.join(DATA_DIR, "audio")
    if os.path.isdir(audio_out):
        for d in os.listdir(audio_out):
            mf = os.path.join(audio_out, d, "_manifest.json")
            if os.path.exists(mf):
                try:
                    j = json.load(open(mf, encoding="utf-8"))
                    if str(j.get("bookId")) == str(book_id) and j.get("book"):
                        return j["book"]
                except Exception:
                    pass
    # sniffer 抓 bookInfoPage 时缓存的书名（最可靠，App 接口不受 H5 风控影响）
    sp = os.path.join(os.path.dirname(CACHE_DIR), "samples", f"name_{book_id}.json")
    if os.path.exists(sp):
        try:
            j = json.load(open(sp, encoding="utf-8"))
            if j.get("name"):
                return j["name"]
        except Exception:
            pass
    cp = os.path.join(CACHE_DIR, f"name_{book_id}.json")
    if os.path.exists(cp):
        try:
            j = json.load(open(cp, encoding="utf-8"))
            if time.time() - j.get("_ts", 0) < CACHE_TTL and j.get("name"):
                return j["name"]
        except Exception:
            pass
    try:
        _, body = http(f"{H5}/book/{book_id}", headers={"Referer": H5},
                       timeout=20, retries=1)
        html = body.decode("utf-8", "replace")
        m = re.search(r"<title>([^<]{2,120})</title>", html)
        if m:
            name = re.sub(r"\s*[-–|]\s*懒人听书.*$", "", m.group(1)).strip()
            if name:
                json.dump({"_ts": time.time(), "name": name},
                          open(cp, "w", encoding="utf-8"), ensure_ascii=False)
                return name
    except Exception:
        pass
    return None


# ---- 命名清洗 -------------------------------------------------------------
# 有声书标题里常见的引流广告后缀
_AD_PATTERNS = [
    r"[|｜\s]*听友群.*$",
    r"[|｜\s]*[+＋]V[：:].*$",
    r"[|｜\s]*(微信|公众号|QQ群|加群|进群).*$",
    r"[|｜\s]*guanyz\w*",
]


def safe_name(s, n=120):
    """清洗为合法文件名：去广告后缀、去非法字符、限长。"""
    s = s or ""
    for pat in _AD_PATTERNS:
        s = re.sub(pat, "", s, flags=re.I)
    s = re.sub(r'[\\/:*?"<>|\r\n]', "", s).strip(" _|｜-—")
    return s[:n]


def episode_filename(section, app_name=None, book_name=None):
    """生成文件名：第{section}集[_标题].m4a

    app_name  形如 "第017集_试探" 或 "第017集"，取其标题部分。
    book_name 若标题等于书名则视为无标题，只留 "第N集"。
    """
    title = ""
    if app_name:
        m = re.match(r"第[\d]+集[\s_—\-－:：|｜]*", app_name)
        title = app_name[m.end():] if m else app_name
        title = safe_name(title)
    if book_name:
        bn = safe_name(book_name.split("|")[0])
        if title == bn or title == safe_name(book_name):
            title = ""
    if title and re.fullmatch(r"[\W_]*", title):
        title = ""
    return f"第{section}集" + (f"_{title}" if title else "") + ".m4a"


def safe_dirname(s, n=100):
    """目录名清洗：仅去真正非法字符，保留 | 用于书名/演播信息分隔。"""
    s = s or ""
    s = re.sub(r'[\\/:*?"<>\r\n]', "", s).strip()
    return s[:n] or "untitled"
