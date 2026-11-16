"""mitmproxy 插件：只记录 App 请求（用于获取"新鲜 sc"），不下载音频。

为什么只记录
------------
批量下载复用 App 的 fresh sc（详见 README）。本插件只负责把 App 发出的
batchGetListenPath 等请求（含 query + body + headers）落盘到
`data/samples/req_samples.jsonl`，供 lrts.download 读取。
**它绝不下载音频**，避免和下载器重复拉流。

用法:
  mitmdump -s addons/sniffer.py --listen-port 8080 --set confdir=/tmp/mitm
"""
import json
import os
import re
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES_DIR = os.path.join(_ROOT, "data", "samples")
REQOUT = os.path.join(SAMPLES_DIR, "req_samples.jsonl")
os.makedirs(SAMPLES_DIR, exist_ok=True)

APP_HOSTS = ("mting.info", "lanrentingshu.com")


def request(flow):
    """记录 App 业务 API 请求（用于拿新鲜 sc / 发现接口）。"""
    try:
        host = flow.request.host or ""
        if not any(h in host for h in APP_HOSTS):
            return
        p = flow.request.path or ""
        if not any(k in p.lower() for k in ("/cgi", "/yyting", "/earning", "/gateway")):
            return
        rec = {"ts": time.time(), "method": flow.request.method, "host": host,
               "path": p, "headers": dict(flow.request.headers),
               "body": (flow.request.get_text(strict=False) or "")[:4000]}
        with open(REQOUT, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"[req] {flow.request.method} {host}{p[:80]}")
    except Exception:      # noqa: BLE001
        pass


def _capture_menu(flow):
    """从 bookInfoPage / ClientGetBookResource 的响应里提取 section->name，累积到
    data/samples/menu_<bookId>.json，供下载器命名使用。

    这两个是 App 接口，抓包时它们的 response 里带目录，能覆盖"当前页"的章节名。
    多次抓包/翻页会不断累积，最终覆盖尽可能多的集。
    """
    try:
        body = flow.response.get_text(strict=False) or ""
        j = json.loads(body)
    except Exception:
        return
    data = j.get("data") or {}
    book = None
    items = []
    total_sections = None  # 接口返回的真实总集数

    # bookInfoPage: data.detail.sections + data.tabList[目录].data.tracks[]
    if "bookInfoPage" in (flow.request.path or ""):
        det = data.get("detail") or {}
        book = det.get("id")
        total_sections = det.get("sections")
        # 顺手存书名，供下载器给目录命名（H5 页面常 404，App 接口最可靠）
        if book and det.get("name"):
            try:
                json.dump({"name": det["name"]},
                          open(os.path.join(SAMPLES_DIR, f"name_{book}.json"), "w",
                               encoding="utf-8"), ensure_ascii=False)
                print(f"[name] 记录书名 book={book}: {det['name']}")
            except Exception:
                pass
        for t in data.get("tabList") or []:
            if t.get("name") == "目录":
                items = (t.get("data") or {}).get("tracks") or []
                break
    # ClientGetBookResource: bookId + list[] + sections(总集数)
    elif "getbookresource" in (flow.request.path or "").lower():
        book = j.get("bookId")
        items = j.get("list") or []
        total_sections = j.get("sections")

    if not book:
        # 从请求 query 兜底取 bookId
        m = re.search(r"[?&](?:bookId|id)=(\d+)", flow.request.path or "")
        book = m.group(1) if m else None
    if not book:
        return

    # 累积式存储：存为 {_ts, meta:{total}, sections:{...}}，与下载器缓存格式一致
    path = os.path.join(SAMPLES_DIR, f"menu_{book}.json")
    store = {"_ts": 0, "meta": {"total": None}, "sections": {}}
    if os.path.exists(path):
        try:
            d = json.load(open(path, encoding="utf-8"))
            if isinstance(d.get("sections"), dict):
                store = d
            else:
                # 兼容旧的扁平格式 {"1":"第N集",...}
                store["sections"] = {str(k): v for k, v in d.items() if str(k).isdigit()}
        except Exception:
            pass
    # 记录真实总集数（一旦拿到就固定）
    if total_sections:
        store.setdefault("meta", {})["total"] = int(total_sections)
    added = 0
    for it in items:
        sec = it.get("section")
        name = it.get("name")
        if sec is not None and name and str(sec) not in store["sections"]:
            store["sections"][str(sec)] = name
            added += 1
    store["_ts"] = time.time()
    json.dump(store, open(path, "w", encoding="utf-8"), ensure_ascii=False)
    if added or total_sections:
        tot = (store.get("meta") or {}).get("total")
        print(f"[menu] 累积目录 book={book}: +{added}，已缓存 {len(store['sections'])} 集"
              f"（全书 {tot} 集）-> {path}")


def response(flow):
    host = flow.request.host or ""
    p = (flow.request.path or "").lower()

    # 音频本体：默认只打印，不落盘
    if "vb.stream.tencentmusic.com" in host:
        try:
            n = len(flow.response.raw_content or b"")
            if n > 20000:
                print(f"[stream] 音频本体 {n/1048576:.2f}MB（仅记录，不下载）")
        except Exception as e:      # noqa: BLE001
            print(f"[stream] 失败 {e}")
        return

    # 目录响应捕获：抓包时顺手把 App 的目录接口响应存下来，
    # 供下载器命名使用（不依赖会被限流的 H5 getBookMenu）。
    if any(k in p for k in ("bookinfopage", "clientgetbookresource", "getbookresource")):
        _capture_menu(flow)
        return

    if not any(k in p for k in ("listenpath", "playpath", "batchget", "getpath")):
        return

    try:
        rec = {"ts": time.time(), "host": flow.request.host,
               "path": flow.request.path, "method": flow.request.method,
               "headers": dict(flow.request.headers),
               "body": flow.request.get_text(strict=False) or ""}
        with open(REQOUT, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"[req] 已记录 {flow.request.method} {flow.request.path[:70]} body={len(rec['body'])}B")
    except Exception as e:      # noqa: BLE001
        print(f"[req] 记录失败 {e}")

    body = flow.response.get_text(strict=False) or ""
    if "pathList" not in body:
        return
    try:
        j = json.loads(body)
    except Exception:
        return
    items = (j.get("data") or {}).get("pathList") or []
    if items:
        print(f"[audio] 本次响应含 {len(items)} 条音频直链（仅记录，不下载）")
