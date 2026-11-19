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

import glob
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
    for p in glob.glob(os.path.join(dst_dir, f"第{sec}集*.m4a")):
        if os.path.getsize(p) > 10000:
            return p
    return None


def download_one(item, dst_dir, sec_to_name, book_name):
    """下载单集，返回 (section, path|None, size, status)。

    采用流式写入（不把整个文件读进内存），并发下载时内存占用平稳。
    """
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
            size = C.http_stream_to_file(url, fn, timeout=90, retries=1)
            return sec, fn, size, "ok"
        except Exception as e:      # noqa: BLE001
            last = e
            if attempt < RETRY_PER_EPISODE:
                time.sleep(1.5 * (attempt + 1))
    return sec, None, 0, f"ERR {type(last).__name__}:{last}"


def _latest_sample_matching(test, samples_path=SAMPLES):
    """扫描样本文件，返回满足 test(record) 的最新（ts 最大）一条记录，无则 None。"""
    if not os.path.exists(samples_path):
        return None
    best = None
    best_ts = -1
    for line in open(samples_path, encoding="utf-8", errors="replace"):
        try:
            d = json.loads(line)
        except Exception:
            continue
        if test(d) and d.get("ts", 0) > best_ts:
            best, best_ts = d, d.get("ts", 0)
    return best


def _replay_sample(sample, timeout=20):
    """用样本自带的 headers 现调一次该 URL，返回解析后的 JSON；失败返回 None。"""
    if not sample:
        return None
    try:
        hdrs = {k: v for k, v in sample["headers"].items()
                if k not in ("Host", "Content-Length", "Accept-Encoding", "Connection")}
        _, raw = http(f"https://{sample['host']}{sample['path']}", headers=hdrs,
                      timeout=timeout, retries=1)
        return json.loads(raw.decode())
    except Exception:
        return None


def fetch_total_from_sample(book, samples_path=SAMPLES):
    """从抓包样本里找该书的 bookInfoPage / ClientGetBookResource，用其 sc 现调拿真实总集数。

    目录接口被限流时，靠这个兜底确定"全本"到底多少集。失败返回 None。
    """
    def _hit(d):
        p = d.get("path") or ""
        return (("bookInfoPage" in p) or ("getbookresource" in p.lower())) and \
               (f"id={book}" in p or f"bookId={book}" in p)

    best = _latest_sample_matching(_hit, samples_path)
    j = _replay_sample(best)
    if not j:
        return None
    if "bookInfoPage" in best["path"]:
        return ((j.get("data") or {}).get("detail") or {}).get("sections")
    return j.get("sections")


def detect_total_via_probe(host, path, headers, base, book, start=1, cap=20000):
    """目录完全拿不到时，用 batchGetListenPath 探测总集数。

    优化：先用大步长(200)向后跳，命中空则回溯到上一个有效点，再逐段精确定位，
    减少请求次数。返回最后一个有效的 section 号，失败返回 None。
    """
    def probe_sections(secs):
        try:
            pl = request_batch(host, path, headers, base, book, secs)
        except Exception as e:
            print(f"  [探测] 请求 {secs[0]}..{secs[-1]} 失败：{e}")
            return None
        return [it.get("section") for it in pl if it.get("section") is not None]

    step = 200
    last = start - 1
    s = start
    seen = set()            # 防止同样的 (s, step) 反复探测，杜绝来回震荡
    empty_streak = 0
    print(f"  [探测] 从第 {start} 集起，步长 {step}…")
    while s <= cap:
        key = (s, step)
        if key in seen:
            break
        seen.add(key)
        secs = list(range(s, min(s + step, cap + 1)))
        good = probe_sections(secs)
        if good is None:
            # 网络失败不是目录为空；同一窗口重试，避免把暂时故障当作全书末尾。
            retries_left = 2
            while retries_left and good is None:
                time.sleep(1)
                good = probe_sections(secs)
                retries_left -= 1
            if good is None:
                return None
        if good:
            last = max(good)
            empty_streak = 0
            print(f"  [探测] 第 {s}-{s+step-1} 有效，最大 section={last}")
            if len(good) < len(secs):
                # 部分响应不能证明窗口末尾就是全书末尾；从最大已知集号后小步补探。
                s = last + 1
                step = BATCH
            else:
                s += step
                step = min(step * 2, 1000)  # 连续完整窗口可加大步长
        else:
            empty_streak += 1
            if empty_streak >= 2:
                break                        # 连续两个有效空窗口才认为已越过末尾
            s += step
            step = BATCH
    return last if last >= start else None


def fetch_name_from_sample(book, samples_path=SAMPLES):
    """从抓包样本里找该书的 bookInfoPage 请求，用其 sc 现调一次拿书名。失败返回 None。"""
    def _hit(d):
        p = d.get("path") or ""
        return "bookInfoPage" in p and f"id={book}" in p

    best = _latest_sample_matching(_hit, samples_path)
    j = _replay_sample(best)
    if not j:
        return None
    det = (j.get("data") or {}).get("detail") or {}
    return det.get("name")


def download_book(book, start=1, end=0, dry=False, no_menu=False,
                  samples_path=SAMPLES, name=None):
    """下载一本书。返回统计 dict。

    name: 自定义输出目录名（书名）。None 时尝试自动获取。
    """
    book = str(book)
    if start < 1 or end < 0 or (end and end < start):
        raise SystemExit(f"下载范围无效：必须满足 start >= 1 且 end=0 或 end >= start（收到 {start}..{end}）")
    if start > 200000 or end > 200000:
        raise SystemExit(f"集号过大（程序上限 200000）：收到 {start}..{end}")
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

    end = end or total or (max(sections) if sections else 0)
    if not end:
        # 目录拿不到（限流）时的兜底：先试抓包样本里的真实总集数，再不行用 batchGet 探测
        print("[*] 目录不可用，尝试用抓包样本确定总集数…")
        t = fetch_total_from_sample(book)
        if t:
            end = int(t)
            print(f"[*] 从抓包样本得到总集数：{end}")
        else:
            print("[*] 样本无总集数，改用 batchGetListenPath 逐段探测（可能需要一会儿）…")
            t = detect_total_via_probe(host, path, headers, base, book, start=start)
            if t:
                end = int(t)
                print(f"[*] 探测到总集数：{end}")
    if not end:
        raise SystemExit(
            "无法确定总集数。请任选其一：\n"
            "  - 用 --end N 指定结束集号（如 --end 100 表示下到第100集）\n"
            "  - 在 App 目录页多滑几章后再重试（程序会缓存目录）\n"
            "  - 或先按区间下载：--start 1 --end 50")
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
        n = len(glob.glob(os.path.join(dst_dir, "第*集*.m4a")))
        total = sum(os.path.getsize(p) for p in glob.glob(os.path.join(dst_dir, "第*集*.m4a")))
        json.dump({"book": book_name, "bookId": str(book), "episodes": n,
                   "sizeGB": round(total / 1073741824, 2)},
                  open(mf, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    except Exception:
        pass
