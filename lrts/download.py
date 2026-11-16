"""批量下载：用 App 的新鲜 sc 重放 batchGetListenPath，翻页拉整本音频。

原理
----
懒人听书 App 请求 batchGetListenPath 时，`sc` 签名只覆盖 URL 的公共 query
参数，**不覆盖 POST body**。App 每次播放都会生成一个新的 sc（约 1 小时有效）。
因此只要抓一次带 fresh sc 的请求，就能复用该 sc 任意改 body 里的 `sections`，
一次最多 50 集，翻页即可拉取任意集的音频直链——无需自己计算签名。

抓 sc 的方式：用 mitmproxy 挂 `addons/sniffer.py`（纯记录，不下载），
在 App 里播放任意一集即在 `data/samples/req_samples.jsonl` 留下一条样本。
"""
from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import common as C
from .common import http

BATCH = 50              # 服务端单次上限约 50 集（>50 返回 "size过大"）
DL_WORKERS = 8          # 并发下载数
RETRY_PER_EPISODE = 2   # 单集重试次数

SAMPLES = os.path.join(C.DATA_DIR, "samples", "req_samples.jsonl")
OUT = os.path.join(C.DATA_DIR, "audio")


def load_fresh(samples_path=SAMPLES):
    """取最新一条 batchGetListenPath 样本（含 fresh sc）。

    返回 (host, path, headers, body_template, ts)。
    """
    if not os.path.exists(samples_path):
        raise SystemExit(f"找不到抓包样本 {samples_path}\n"
                         f"请先用 mitmproxy 抓一次（见 README 的「抓 sc」步骤）")
    recs = []
    for line in open(samples_path, encoding="utf-8", errors="replace"):
        try:
            d = json.loads(line)
        except Exception:
            continue
        if "batchGetListenPath" in (d.get("path") or ""):
            recs.append(d)
    if not recs:
        raise SystemExit("样本中没有 batchGetListenPath 记录，请在 App 播放一集后重抓")
    d = recs[-1]
    headers = {k: v for k, v in d["headers"].items()
               if k not in ("Host", "Content-Length", "Accept-Encoding", "Connection")}
    headers["Content-Type"] = "application/json"
    base = json.loads(d["body"])
    return d["host"], d["path"], headers, base, d.get("ts", 0)


def request_batch(host, path, headers, base, book, secs, retries=4):
    """请求一批直链。返回 pathList。失败指数退避重试，仍失败抛异常。"""
    body = dict(base)
    body["entityId"] = int(book)
    body["sections"] = ",".join(f"{s}_{book}{s:08d}" for s in secs)
    body["trackIds"] = ",".join(["0"] * len(secs))     # trackId 仅提示，可填 0
    data = json.dumps(body, separators=(",", ":")).encode()
    last = None
    for attempt in range(retries):
        try:
            _, raw = http(f"https://{host}{path}", data=data, headers=headers,
                          method="POST", timeout=40, retries=1)
            j = json.loads(raw.decode())
            if j.get("status") != 0:
                # sc 过期 / 服务异常：重试无意义，直接抛
                raise RuntimeError(f"status={j.get('status')} msg={j.get('msg')}")
            return (j.get("data") or {}).get("pathList") or []
        except Exception as e:      # noqa: BLE001
            last = e
            if attempt < retries - 1:
                time.sleep(2 ** attempt)     # 1, 2, 4 ...
    raise last


def _already_have(dst_dir, sec):
    """已存在且体积正常则返回该路径（容忍带/不带标题两种命名）。"""
    import glob
    for p in glob.glob(os.path.join(dst_dir, f"第{sec}集*.m4a")):
        if os.path.getsize(p) > 10000:
            return p
    return None


def download_one(item, dst_dir, sec_to_name, book_name):
    """下载单集，返回 (section, path|None, size, status)。"""
    url = item.get("path")
    sec = item.get("section")
    if not url:
        return sec, None, 0, "nourl"
    existing = _already_have(dst_dir, sec)
    if existing:
        return sec, existing, os.path.getsize(existing), "skip"
    fn = os.path.join(dst_dir, C.episode_filename(sec, sec_to_name.get(sec), book_name))
    last = None
    for attempt in range(RETRY_PER_EPISODE + 1):
        try:
            _, data = http(url, headers={"User-Agent": C.UA}, timeout=90,
                           retries=1, direct=True)
            if len(data) < 1000:
                raise RuntimeError(f"文件过小 {len(data)}B")
            tmp = fn + ".part"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, fn)
            return sec, fn, len(data), "ok"
        except Exception as e:      # noqa: BLE001
            last = e
            if attempt < RETRY_PER_EPISODE:
                time.sleep(1.5 * (attempt + 1))
    return sec, None, 0, f"ERR {type(last).__name__}:{last}"


def _backup_before_run(dst_dir, book, keep=3):
    """下载前把已存在书目录快照到 data/backup/，防止误删/误操作丢文件。

    只备份已存在且有音频的目录；每次一份时间戳快照，最多保留最近 keep 份。
    失败静默（不影响下载）。
    """
    try:
        import glob
        import shutil
        if not os.path.isdir(dst_dir):
            return
        if not glob.glob(os.path.join(dst_dir, "第*集*.m4a")):
            return  # 还没文件，无需备份
        bdir = os.path.join(C.DATA_DIR, "backup")
        os.makedirs(bdir, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        base = os.path.basename(dst_dir.rstrip("/"))
        snap = os.path.join(bdir, f"{base}__{stamp}")
        shutil.copytree(dst_dir, snap)
        print(f"[备份] 已将现有目录快照到 {snap}")
        # 只保留最近 keep 份该书的快照
        olds = sorted(glob.glob(os.path.join(bdir, f"{base}__*")))
        for old in olds[:-keep]:
            shutil.rmtree(old, ignore_errors=True)
    except Exception as e:      # noqa: BLE001
        print(f"[备份] 跳过（{type(e).__name__}: {e}）")


def fetch_name_from_sample(book, samples_path=SAMPLES):
    """从抓包样本里找该书的 bookInfoPage 请求，用其 sc 现调一次拿书名。失败返回 None。"""
    best = None
    if not os.path.exists(samples_path):
        return None
    for line in open(samples_path, encoding="utf-8", errors="replace"):
        try:
            d = json.loads(line)
        except Exception:
            continue
        p = d.get("path") or ""
        if "bookInfoPage" in p and f"id={book}" in p:
            if best is None or d.get("ts", 0) > best.get("ts", 0):
                best = d
    if not best:
        return None
    try:
        hdrs = {k: v for k, v in best["headers"].items()
                if k not in ("Host", "Content-Length", "Accept-Encoding", "Connection")}
        _, raw = http(f"https://{best['host']}{best['path']}", headers=hdrs,
                      timeout=20, retries=1)
        j = json.loads(raw.decode())
        det = (j.get("data") or {}).get("detail") or {}
        return det.get("name")
    except Exception:
        return None


def download_book(book, start=1, end=0, dry=False, no_menu=False,
                  samples_path=SAMPLES, name=None):
    """下载一本书。返回统计 dict。

    name: 自定义输出目录名（书名）。None 时尝试自动获取。
    """
    book = str(book)
    host, path, headers, base, ts = load_fresh(samples_path)
    age = (time.time() - ts) / 60 if ts else 0
    print(f"[*] fresh sc 生成于 {time.strftime('%H:%M:%S', time.localtime(ts))}（{age:.0f} 分钟前）")
    if age > 55:
        print("[!] 警告：sc 可能已过期，建议先在 App 播放一集再跑")

    book_name = name or C.get_book_name(book) or fetch_name_from_sample(book)
    sections = {}
    total = 0
    if no_menu:
        print("[*] --no-menu：跳过目录，按 section 下载（命名 第N集）")
    else:
        print(f"[*] 获取 {book} 目录…（优先本地缓存，其次在线）")
        try:
            meta, sections = C.get_all_sections(book)
            total = meta.get("total") or (max(sections) if sections else 0)
        except Exception as e:      # noqa: BLE001
            print(f"[!] 拉目录失败：{e}")
            print("[!] 降级为 --no-menu（按 section 下载，命名 第N集；"
                  "稍后可跑 rename 补章节名）")
    if not book_name:
        book_name = f"book_{book}"
    print(f"[*] 书名: {book_name}  总集数: {total or '未知'}  章节名: {len(sections)}")

    dst_dir = os.path.join(OUT, C.safe_dirname(book_name))
    os.makedirs(dst_dir, exist_ok=True)
    _backup_before_run(dst_dir, book)

    end = end or total or (max(sections) if sections else 0)
    if not end:
        raise SystemExit("无法确定总集数：请用 --end N 指定")
    all_secs = list(range(start, end + 1))
    print(f"[*] 目标 {start}..{end}（共 {len(all_secs)}），每批 {BATCH}，并发 {DL_WORKERS}")

    if dry:
        for i in range(0, len(all_secs), BATCH):
            chunk = all_secs[i:i + BATCH]
            pl = request_batch(host, path, headers, base, book, chunk)
            print(f"  批次 {chunk[0]}..{chunk[-1]}: {len(pl)} 集")
        return {"dry": True, "sections": len(all_secs)}

    ok = skip = 0
    total_bytes = 0
    failed: set[int] = set()
    for i in range(0, len(all_secs), BATCH):
        chunk = all_secs[i:i + BATCH]
        try:
            pl = request_batch(host, path, headers, base, book, chunk)
        except Exception as e:      # noqa: BLE001
            print(f"  [!] 批次 {chunk[0]}..{chunk[-1]} 失败（{e}），逐集降级重试")
            pl = []
            for s in chunk:
                try:
                    pl += request_batch(host, path, headers, base, book, [s])
                except Exception:
                    failed.add(s)
        # 用 as_completed 让每集一下完就立即打印（并发完成顺序）
        with ThreadPoolExecutor(max_workers=DL_WORKERS) as ex:
            futures = [ex.submit(download_one, it, dst_dir, sections, book_name) for it in pl]
            for fut in as_completed(futures):
                sec, fn, sz, st = fut.result()
                fname = os.path.basename(fn) if fn else f"第{sec}集"
                if st == "ok":
                    ok += 1; total_bytes += sz
                    print(f"    ✔ 第{sec}集  {fname}  {sz/1048576:.2f}MB  "
                          f"[总 {ok+skip}/{len(all_secs)}]", flush=True)
                elif st == "skip":
                    skip += 1
                    print(f"    · 第{sec}集  已存在，跳过  [{ok+skip}/{len(all_secs)}]", flush=True)
                else:
                    failed.add(sec)
                    print(f"    ✘ 第{sec}集  失败：{st}", flush=True)
        returned = {it.get("section") for it in pl}
        for s in chunk:
            if s not in returned:
                failed.add(s)
        print(f"  批次 {chunk[0]}..{chunk[-1]}: ok={ok} skip={skip} 失败={len(failed)}", flush=True)

    _write_manifest(dst_dir, book, book_name, ok + skip)
    if failed:
        fp = os.path.join(dst_dir, "_failed.txt")
        with open(fp, "w") as f:
            f.write(",".join(str(s) for s in sorted(failed)))
        print(f"[!] {len(failed)} 集失败，已写 {fp}（重跑本命令自动补下）")
    print(f"[*] 完成：新增 {ok} 集 / {total_bytes/1048576:.1f} MB，跳过 {skip}，失败 {len(failed)}")

    # 自检并修复书名 / 章节名
    print("[*] 自检书名与章节名…")
    try:
        from . import rename as R
        v = R.verify_and_fix(dst_dir, book, book_name)
        if v.get("final_dir"):
            dst_dir = v["final_dir"]
    except Exception as e:      # noqa: BLE001
        print(f"[!] 自检出错（不影响已下载文件）：{e}")

    print(f"[*] 目录: {dst_dir}")
    return {"ok": ok, "skip": skip, "failed": sorted(failed), "dir": dst_dir}


def _write_manifest(dst_dir, book, book_name, count):
    mf = os.path.join(dst_dir, "_manifest.json")
    try:
        import glob
        n = len(glob.glob(os.path.join(dst_dir, "第*集*.m4a")))
        total = sum(os.path.getsize(p) for p in glob.glob(os.path.join(dst_dir, "第*集*.m4a")))
        json.dump({"book": book_name, "bookId": str(book), "episodes": n,
                   "sizeGB": round(total / 1073741824, 2)},
                  open(mf, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    except Exception:
        pass
