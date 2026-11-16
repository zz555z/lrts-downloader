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


def _load_menu_cache(book_id):
    """读取目录缓存，返回 {int: name} 或 None。

    兼容两处缓存：sniffer 抓包存的 data/samples/menu_<id>.json
    与 getBookMenu 抓取缓存的 data/cache/menu_<id>.json。
    samples 里的优先级更高（App 接口抓的，不受风控影响）。
    """
    samples_p = os.path.join(os.path.dirname(CACHE_DIR), "samples", f"menu_{book_id}.json")
    cache_p = os.path.join(CACHE_DIR, f"menu_{book_id}.json")
    for p in (samples_p, cache_p):
        if not os.path.exists(p):
            continue
        try:
            d = json.load(open(p, encoding="utf-8"))
            # samples 格式: {"1":"第001集_xxx", ...}
            # cache 格式:   {"_ts":..,"meta":..,"sections":{"1":..}}
            raw = d.get("sections") if isinstance(d, dict) and "sections" in d else d
            if raw:
                return {int(k): v for k, v in raw.items() if str(k).isdigit()}
        except Exception:
            continue
    return None


def get_all_sections(book_id, verbose=True, use_cache=True):
    """拉全书目录，返回 (meta, sections)。

    meta     : {"total": 总集数}
    sections : {section:int -> name:str}（name 为 App 原始显示名，如 "第017集_试探"）

    优先用本地缓存（抓包缓存 / 7 天前的 getBookMenu 缓存）。
    若接口被风控（status=114），抛 RuntimeError。
    """
    if use_cache:
        cached = _load_menu_cache(book_id)
        if cached:
            if verbose:
                print(f"  [缓存] {book_id}: {len(cached)} 集")
            return {"total": len(cached)}, cached

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


def _looks_bad_name(name):
    """书名是否是不规范的占位名（如 book_123）。"""
    if not name or not str(name).strip():
        return True
    return bool(re.fullmatch(r"book_\d+", str(name).strip()))


def get_book_name(book_id):
    """拿书名。优先真实来源（抓包缓存 / H5），跳过 book_xxx 之类的占位名。

    注意：不再优先信已下目录的 _manifest.json——它可能残留 book_<id> 占位名，
    一旦残留会导致目录名一直修不正（文件散落）。
    """
    # 1) sniffer 抓 bookInfoPage 时缓存的书名（最可靠，App 接口不受 H5 风控影响）
    sp = os.path.join(os.path.dirname(CACHE_DIR), "samples", f"name_{book_id}.json")
    if os.path.exists(sp):
        try:
            j = json.load(open(sp, encoding="utf-8"))
            if j.get("name") and not _looks_bad_name(j["name"]):
                return j["name"]
        except Exception:
            pass
    # 2) 本地缓存的书名
    cp = os.path.join(CACHE_DIR, f"name_{book_id}.json")
    if os.path.exists(cp):
        try:
            j = json.load(open(cp, encoding="utf-8"))
            if time.time() - j.get("_ts", 0) < CACHE_TTL and j.get("name") \
               and not _looks_bad_name(j["name"]):
                return j["name"]
        except Exception:
            pass
    # 3) 已下目录里若已有规范书名，也可用（但跳过 book_xxx 占位名）
    audio_out = os.path.join(DATA_DIR, "audio")
    if os.path.isdir(audio_out):
        for d in os.listdir(audio_out):
            mf = os.path.join(audio_out, d, "_manifest.json")
            if os.path.exists(mf):
                try:
                    j = json.load(open(mf, encoding="utf-8"))
                    if str(j.get("bookId")) == str(book_id) and j.get("book") \
                       and not _looks_bad_name(j["book"]):
                        return j["book"]
                except Exception:
                    pass
    # 4) 请求 H5 页面
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


# ---- 交互：询问下载范围（cli 与 grab_sc 共用）----------------------------
def _input_or_none(prompt):
    """input() 的包装：EOF 时返回 None。"""
    try:
        return input(prompt)
    except EOFError:
        return None


def ask_range(total):
    """交互式询问下载范围。返回 (start, end)。

    total 已知总集数时给出提示。
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
        line = _input_or_none("请输入 1 或 2 后回车：")
        if line is None:
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
                s = _input_or_none("请输入起始集号：")
                if s is None:
                    print("\n[!] 输入已结束，默认下载全本")
                    return 1, (total or 0)
                e = _input_or_none("请输入结束集号：")
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
