"""懒人听书（LRTS）下载器。

基于 App 在线播放时产生的"新鲜 sc"重放 batchGetListenPath 接口，批量下载整本音频。

模块：
  lrts.common   —— 公共工具（HTTP 重试、目录抓取+缓存、命名清洗）
  lrts.download —— 批量下载
  lrts.rename   —— 按 App 真实章节名重命名
"""
__version__ = "1.0.0"
