"""网站 Cookie 收集 —— 供嗅探/下载/预览复用浏览器的登录态。

很多视频 CDN（如 streambucket.xyz）会校验 Cookie + Referer + User-Agent
组合，纯脚本请求不带 Cookie 就会被 403。这里通过
QWebEngineCookieStore 读取内置浏览器已保存的 Cookie（含 HttpOnly），
按请求地址的域名匹配后拼成 Cookie 请求头。
"""
from __future__ import annotations

from urllib.parse import urlparse

from PySide6.QtCore import QObject


def _as_text(v) -> str:
    """QNetworkCookie 的字段在不同绑定下可能是 str / bytes / QByteArray。"""
    if isinstance(v, str):
        return v
    try:
        return bytes(v).decode("utf-8", "ignore")
    except Exception:
        return str(v)


class ProfileCookies(QObject):
    """监听 QWebEngineProfile 的 Cookie 变化, 支持按 URL 生成 Cookie 头。"""

    def __init__(self, profile, parent=None):
        super().__init__(parent)
        # domain -> {name: value}
        self._jar = {}
        self._store = None
        try:
            self._store = profile.cookieStore()
            self._store.cookieAdded.connect(self._on_added)
            self._store.cookieRemoved.connect(self._on_removed)
            self._store.loadAllCookies()
        except Exception:
            self._store = None

    # ------------------------------------------------------------------ 事件
    def _on_added(self, cookie):
        try:
            domain = _as_text(cookie.domain())
            name = _as_text(cookie.name())
            value = _as_text(cookie.value())
            if not domain or not name:
                return
            key = domain.lstrip(".").lower()
            self._jar.setdefault(key, {})[name] = value
        except Exception:
            pass

    def _on_removed(self, cookie):
        try:
            domain = _as_text(cookie.domain())
            name = _as_text(cookie.name())
            key = domain.lstrip(".").lower()
            if key in self._jar:
                self._jar[key].pop(name, None)
                if not self._jar[key]:
                    del self._jar[key]
        except Exception:
            pass

    # ------------------------------------------------------------------ 查询
    def header_for(self, url: str) -> str:
        """返回适用于该 URL 的 Cookie 请求头值(无匹配则空串)。"""
        try:
            host = (urlparse(url).hostname or "").lower()
        except Exception:
            return ""
        if not host or not self._jar:
            return ""
        pairs = []
        for domain, cookies in self._jar.items():
            if host == domain or host.endswith("." + domain):
                for name, value in cookies.items():
                    pairs.append(f"{name}={value}")
        return "; ".join(pairs)
