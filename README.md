# 懒人听书下载器 · lrts-downloader

把「懒人听书」App 里的有声书整本下载到本地，并按 App 的真实章节名归档。

> ⚠️ **仅供个人学习 / 备份你自己已购或免费可听的音频使用。** 请遵守平台服务条款，
> 不要用于大规模抓取、二次分发或任何商业用途。因使用本工具产生的任何后果由使用者自负。

---

## 它解决什么问题

懒人听书的音频地址接口 `batchGetListenPath` 需要签名参数 `sc`，而 `sc` 由 App
内被加固的安全库在运行时生成，**无法静态计算**。

但我们发现一个关键事实：

> **`sc` 只对 URL 的公共 query 参数签名，不覆盖 POST body。**

所以只要从 App 的真实请求里拿到**一个新鲜的 `sc`**（约 1 小时有效），就能**复用它**
任意修改请求 body 里的 `sections`，一次拉最多 50 集的音频直链，翻页即可下载整本——
**完全不需要自己计算签名**。

本工具就是围绕这个思路构建的：

1. **抓 `sc`**：用 mitmproxy 插件记录 App 发出的请求（只记录，不下载）。
2. **批量下载**：读那一条请求里的 `sc`，自己构造 body，翻页拉直链并下载。
3. **按章节名归档**：用免签名的 `getBookMenu` 接口拿每集真实标题，重命名文件。

---

## 特性

- ✅ 零第三方依赖即可下载 / 改名（只需 Python 3.9+ 标准库）
- ✅ **断点续传**：已存在的集自动跳过，重跑只补缺的
- ✅ **失败可追溯**：失败集号写入 `_failed.txt`，重跑自动补下
- ✅ **单集 + 整批双重试**：网络抖动不丢集
- ✅ **目录本地缓存（7 天）**：避免频繁翻页触发接口风控
- ✅ **自动命名**：`第N集_章节名.m4a`，与 App 显示一致；自动清理标题里的引流广告
- ✅ **风控容错**：目录接口不可用时自动降级，不中断下载

---

## 目录结构

```
lrts-downloader/
├── cli.py                # 命令行入口（dl / rename / menu）
├── lrts/
│   ├── __init__.py
│   ├── common.py         # HTTP 重试、目录抓取+缓存、命名清洗
│   ├── download.py       # 批量下载核心
│   └── rename.py         # 按章节名重命名
├── addons/
│   └── sniffer.py        # mitmproxy 插件（只记录请求/拿 sc，不下载）
├── data/                 # 运行产物（已 gitignore）
│   ├── samples/          # 抓包样本 req_samples.jsonl
│   ├── cache/            # 目录缓存 menu_<bookId>.json
│   └── audio/            # 下载的音频
├── requirements.txt
├── LICENSE
└── README.md
```

---

## 安装

```bash
pip install -r requirements.txt   # 仅抓 sc 需要 mitmproxy；下载/改名为纯标准库
```

> 完全不想装 mitmproxy 也能用：只需手动准备一条 `batchGetListenPath` 请求样本
> （见下方「方式 B」）。

---

## 使用

### 第 0 步 · 拿到 fresh sc（二选一）

**方式 A：mitmproxy 自动抓（推荐）**

1. 安装并信任 mitmproxy 证书（首次）。
2. 启动插件（只记录、不下载）：

   ```bash
   mitmdump -s addons/sniffer.py --listen-port 8080 --set confdir=/tmp/mitm
   ```

3. 把系统代理指向 `127.0.0.1:8080`（macOS）：
   ```bash
   sudo networksetup -setwebproxy "Wi-Fi" 127.0.0.1 8080
   sudo networksetup -setsecurewebproxy "Wi-Fi" 127.0.0.1 8080
   ```
4. **在懒人听书 App 里打开目标书，播放任意一集**（几秒即可）。
   插件会把这次请求写进 `data/samples/req_samples.jsonl`。
5. 用完**记得关代理**：
   ```bash
   sudo networksetup -setwebproxy "Wi-Fi" "" 0
   sudo networksetup -setsecurewebproxy "Wi-Fi" "" 0
   ```

**方式 B：手动导入样本**

任意方式抓到一条 `POST /yyting/v3/gateway/batchGetListenPath` 请求，整理成一行 JSON
写入 `data/samples/req_samples.jsonl`，字段如下：

```json
{"ts": 1700000000, "method": "POST", "host": "shapi.mting.info",
 "path": "/yyting/v3/gateway/batchGetListenPath?...&sc=xxxx&...",
 "headers": {"User-Agent": "..."}, "body": "{\"entityId\":43913,\"sections\":\"...\",...}"}
```

> `ts` 用于判断 sc 是否新鲜（超过约 55 分钟会提示重抓）。

### 第 1 步 · 下载整本

```bash
# 下载整本（自动探测总集数、自动命名书名目录）
python cli.py dl --book 43913

# 只下某区间
python cli.py dl --book 43913 --start 100 --end 200

# 先空跑探测（不下载）
python cli.py dl --book 43913 --dry

# 书名自动获取失败时，手动指定输出目录名
python cli.py dl --book 43913 --name "全球高武"

# 目录接口被风控时，跳过目录、按 section 下载（命名 第N集，之后再 rename 补标题）
python cli.py dl --book 43913 --no-menu
```

> `book` 就是书号，可从 App 的分享链接或 URL 里看到（8 位数字）。

### 第 2 步 · 按 App 章节名重命名（可选）

如果下载时目录没拿到（用了 `--no-menu`），或有旧文件需要统一：

```bash
python cli.py rename              # 处理 data/audio 下所有书
python cli.py rename --book 43913 # 只处理某本
```

### 查看目录 / 验证缓存

```bash
python cli.py menu --book 43913 --show 20
```

---

## 常见问题

**Q：提示 `非法请求AA` / `status=114`？**
`getBookMenu` 目录接口有频率风控（连续翻几十页会被临时封）。等几十分钟自动解除，
或直接用 `--no-menu` 先下载音频（标题稍后再补）。

**Q：提示 sc 可能过期？**
App 的 sc 有效期约 1 小时。重新按「第 0 步」在 App 播一集拿新的即可。

**Q：为什么一次最多 50 集？**
服务端限制，超过返回 `size过大`。工具已自动按 50 一页翻。

**Q：下载的音频能正常播放吗？**
能，是标准 M4A（`ftyp` 头）。下载器会校验体积，过小的记为失败。

**Q：目录名 / 文件名里的 `|` 是什么？**
书名常带演播信息，形如 `书名|演播|类型`。`|` 是 macOS/Linux 合法文件名字符，故保留。

---

## 免责声明

本项目仅用于技术学习与个人音频备份。请勿用于侵犯版权、大规模抓取或商业用途。
使用本工具即表示你已了解并同意自行承担相关风险与责任。
