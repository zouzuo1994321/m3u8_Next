"""API 探针: 验证 PySide6 6.11 的 QWebEngineUrlRequestInterceptor 接口可用性。"""
import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from PySide6.QtWebEngineCore import (
    QWebEngineProfile,
    QWebEngineUrlRequestInfo,
    QWebEngineUrlRequestInterceptor,
)
from PySide6.QtWebEngineWidgets import QWebEngineView

app = QApplication(sys.argv)
app.setApplicationName("api-probe")

# 1. defaultProfile + setUrlRequestInterceptor
p = QWebEngineProfile.defaultProfile()
print("defaultProfile:", p)
print("has setUrlRequestInterceptor:", hasattr(p, "setUrlRequestInterceptor"))

# 2. ResourceType 枚举成员
for name in ("ResourceTypeMedia", "ResourceTypeXhr", "ResourceTypeScript",
             "ResourceTypeStylesheet", "ResourceTypeImage", "ResourceTypeMainFrame"):
    print(f"  {name}:", hasattr(QWebEngineUrlRequestInfo, name))

# 3. 拦截器实例化 + 挂载
class Probe(QWebEngineUrlRequestInterceptor):
    def interceptRequest(self, info):
        pass

itc = Probe()
p.setUrlRequestInterceptor(itc)
print("interceptor installed OK")

# 4. requestUrl / resourceType 调用面
view = QWebEngineView()
page = view.page()
print("page:", page)
print("page has title:", hasattr(page, "title"))
print("page has loadFinished:", hasattr(page, "loadFinished"))

# 5. QWebEngineUrlRequestInfo 方法面(通过文档属性确认)
import inspect
members = [m for m in dir(QWebEngineUrlRequestInfo) if not m.startswith("_")]
print("RequestInfo members:", members)

QTimer.singleShot(100, app.quit)
app.exec()
print("PROBE DONE")
