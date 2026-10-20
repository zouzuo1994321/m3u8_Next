"""「更多功能」面板 —— 逆向自猫抓 cat-catch。

猫抓把能力分散在两处: 列表页顶部的**按钮条**（视频录制 / 录制webRTC /
屏幕捕捉 / 模拟手机 / 自动下载 / 数据发送）和一个「更多功能」弹出层
（分片筛选 / 参数 / 预处理 / 找真 KEY）。本项目把两处合并成一个双页签面板,
避免主界面再堆一排勾选框。

页签一「分片与下载」（对应 ``js/m3u8.js`` 的 ``#moreFeaturesBox``）
- 分片正则筛选 + 反选, 实时显示「命中 N / 共 M」
- 分片参数追加（``tsAddArg``, 破分片级 token）
- 数据预处理: 剥离 PNG/JPG 伪装头
- 跳过解密 / 仅音频
- 疑似密钥验证（来自深度搜索）

页签二「录制与自动化」（对应 ``recorder.js`` / ``webrtc.js`` /
``recorder2.js`` / ``background.js`` / ``popup.js``）
- 视频录制 / 录制webRTC / 屏幕捕捉（``MediaRecorder`` → webm）
- 模拟手机（切 iPhone UA 重载）
- 自动下载（捕获完成自动入队）
- 数据发送（POST 捕获结果到外部服务）

对话框只负责编辑状态与调用回调, 不做网络/页面操作——那些动作在主窗口里,
通过 ``hooks`` 传进来, 便于单测。
"""
from __future__ import annotations

from typing import Callable, Dict, List, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QCheckBox,
    QDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import fragtools


def default_state() -> Dict:
    """更多功能的默认状态（直接映射到 DownloadConfig 字段 + 主窗口开关）。"""
    return {
        # ---- 分片与下载 ----
        "strip_image": False,
        "skip_decrypt": False,
        "audio_only": False,
        "extra_query": "",
        "selected": None,        # None = 全部分片; list[int] = 只下这些
        "regex": "",
        # ---- 录制与自动化 ----
        "rec_mime": "",          # 空 = 由页面挑第一个可用编码
        "rec_index": 0,          # 录制第几个 video/audio 元素
        "simulate_mobile": False,
        "auto_download": False,
        "adblock": True,         # 广告拦截默认开
        "send_url": "",
    }


class ToolsDialog(QDialog):
    """更多功能设置面板（双页签）。"""

    def __init__(self, parent=None, state: Optional[Dict] = None,
                 info=None,
                 on_log: Optional[Callable[[str], None]] = None,
                 fetch_fragments: Optional[Callable[[], List[str]]] = None,
                 key_candidates: Optional[List[bytes]] = None,
                 brute_force: Optional[Callable[[List[bytes]], Optional[bytes]]] = None,
                 hooks: Optional[Dict[str, Callable]] = None):
        super().__init__(parent)
        self.setWindowTitle("更多功能")
        self.setMinimumWidth(600)
        self._state = dict(state or default_state())
        self._state.update({k: v for k, v in (state or {}).items()
                            if k not in self._state} if state else {})
        self._info = info
        self._log = on_log or (lambda m: None)
        self._fetch = fetch_fragments
        self._keys = list(key_candidates or [])
        self._brute = brute_force
        self._hooks: Dict[str, Callable] = dict(hooks or {})
        self._urls: List[str] = []
        self._flags: List[bool] = []
        self._recording = ""          # "" | element | webrtc | screen

        lay = QVBoxLayout(self)
        lay.setSpacing(8)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_tab_fragments(), "分片与下载")
        self.tabs.addTab(self._build_tab_tools(), "录制与自动化")
        lay.addWidget(self.tabs, 1)

        bottom = QHBoxLayout()
        self.lbl_summary = QLabel()
        self.lbl_summary.setObjectName("hintLabel")
        self.lbl_summary.setWordWrap(True)
        bottom.addWidget(self.lbl_summary, 1)
        btn_ok = QPushButton("完成")
        btn_ok.setObjectName("primaryBtn")
        btn_ok.clicked.connect(self.accept)
        bottom.addWidget(btn_ok)
        btn_cancel = QPushButton("取消")
        btn_cancel.clicked.connect(self.reject)
        bottom.addWidget(btn_cancel)
        lay.addLayout(bottom)

        self._refresh_summary()

    # ================================================================ 页签一
    def _build_tab_fragments(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setSpacing(10)
        lay.addWidget(self._build_filter_box())
        lay.addWidget(self._build_pipeline_box())
        lay.addWidget(self._build_key_box())
        lay.addStretch(1)
        return w

    # ---------------------------------------------------------------- 分片筛选
    def _build_filter_box(self) -> QGroupBox:
        box = QGroupBox("分片筛选")
        v = QVBoxLayout(box)
        v.setSpacing(6)

        row = QHBoxLayout()
        row.addWidget(QLabel("正则:"))
        self.ed_regex = QLineEdit(self._state.get("regex", ""))
        self.ed_regex.setPlaceholderText(r"匹配分片地址, 例如 seg_\d+\.ts$")
        self.ed_regex.returnPressed.connect(self._apply_regex)
        row.addWidget(self.ed_regex, 1)
        btn = QPushButton("筛选")
        btn.clicked.connect(self._apply_regex)
        row.addWidget(btn)
        btn_inv = QPushButton("反选")
        btn_inv.clicked.connect(self._invert)
        row.addWidget(btn_inv)
        btn_all = QPushButton("全选")
        btn_all.clicked.connect(self._select_all)
        row.addWidget(btn_all)
        v.addLayout(row)

        self.lbl_filter = QLabel("尚未读取分片列表")
        self.lbl_filter.setObjectName("hintLabel")
        v.addWidget(self.lbl_filter)

        if self._fetch is not None:
            btn_load = QPushButton("读取分片列表")
            btn_load.clicked.connect(self._load_fragments)
            v.addWidget(btn_load, 0, Qt.AlignLeft)
        return box

    def _load_fragments(self):
        try:
            self._urls = list(self._fetch() or [])
        except Exception as e:
            self.lbl_filter.setText(f"读取失败: {e}")
            self._log(f"更多功能: 读取分片列表失败: {e}")
            return
        if not self._urls:
            self.lbl_filter.setText("该地址没有分片列表(可能是直链 mp4)")
            return
        prev = self._state.get("selected")
        if prev is None:
            self._flags = [True] * len(self._urls)
        else:
            keep = set(prev)
            self._flags = [i in keep for i in range(len(self._urls))]
        self.lbl_filter.setText(f"已读取 {len(self._urls)} 个分片")
        self._refresh_summary()
        self._log(f"更多功能: 读取到 {len(self._urls)} 个分片")

    def _apply_regex(self):
        if not self._urls:
            self._load_fragments()
            if not self._urls:
                return
        pattern = self.ed_regex.text().strip()
        try:
            self._flags = fragtools.select_by_regex(self._urls, pattern)
        except ValueError as e:
            self.lbl_filter.setText(str(e))
            return
        self._state["regex"] = pattern
        hit = sum(1 for f in self._flags if f)
        self.lbl_filter.setText(f"正则命中 {hit} / {len(self._urls)}")
        self._sync_selected()
        self._refresh_summary()

    def _invert(self):
        if not self._flags:
            return
        self._flags = fragtools.invert_selection(self._flags)
        hit = sum(1 for f in self._flags if f)
        self.lbl_filter.setText(f"已反选: 选中 {hit} / {len(self._urls)}")
        self._sync_selected()
        self._refresh_summary()

    def _select_all(self):
        if not self._flags:
            return
        self._flags = [True] * len(self._flags)
        self._state["selected"] = None
        self.lbl_filter.setText(f"已全选 {len(self._urls)} 个分片")
        self._refresh_summary()

    def _sync_selected(self):
        if not self._flags:
            self._state["selected"] = None
            return
        if all(self._flags):
            self._state["selected"] = None       # 全选等价于不筛选
        else:
            self._state["selected"] = fragtools.selected_indices(self._flags)

    # ---------------------------------------------------------------- 下载管线
    def _build_pipeline_box(self) -> QGroupBox:
        box = QGroupBox("下载管线")
        v = QVBoxLayout(box)
        v.setSpacing(6)

        row = QHBoxLayout()
        row.addWidget(QLabel("分片参数:"))
        self.ed_query = QLineEdit(self._state.get("extra_query", ""))
        self.ed_query.setPlaceholderText("追加到每个分片地址, 如 token=abc&exp=79")
        self.ed_query.textChanged.connect(self._on_query)
        row.addWidget(self.ed_query, 1)
        v.addLayout(row)

        self.ck_strip = QCheckBox("数据预处理: 剥离 PNG/JPG 伪装头")
        self.ck_strip.setChecked(bool(self._state.get("strip_image")))
        self.ck_strip.toggled.connect(
            lambda b: (self._state.__setitem__("strip_image", b),
                       self._refresh_summary()))
        v.addWidget(self.ck_strip)

        self.ck_skip = QCheckBox("跳过解密(保存密文分片)")
        self.ck_skip.setChecked(bool(self._state.get("skip_decrypt")))
        self.ck_skip.toggled.connect(
            lambda b: (self._state.__setitem__("skip_decrypt", b),
                       self._refresh_summary()))
        v.addWidget(self.ck_skip)

        self.ck_audio = QCheckBox("仅音频(合并时丢弃视频轨, 需 ffmpeg)")
        self.ck_audio.setChecked(bool(self._state.get("audio_only")))
        self.ck_audio.toggled.connect(
            lambda b: (self._state.__setitem__("audio_only", b),
                       self._refresh_summary()))
        v.addWidget(self.ck_audio)
        return box

    def _on_query(self, text: str):
        self._state["extra_query"] = text.strip()

    # ---------------------------------------------------------------- 密钥
    def _build_key_box(self) -> QGroupBox:
        box = QGroupBox("疑似密钥(来自深度搜索)")
        v = QVBoxLayout(box)
        self.lst_keys = QListWidget()
        self.lst_keys.setMaximumHeight(90)
        if self._keys:
            for k in self._keys:
                self.lst_keys.addItem(f"{fragtools.key_to_hex(k)}    "
                                      f"({fragtools.key_to_b64(k)})")
        else:
            self.lst_keys.addItem("(未捕获到疑似密钥, 请先跑一次深度搜索)")
        v.addWidget(self.lst_keys)

        row = QHBoxLayout()
        self.lbl_key = QLabel("")
        self.lbl_key.setObjectName("hintLabel")
        self.lbl_key.setWordWrap(True)
        row.addWidget(self.lbl_key, 1)
        btn = QPushButton("验证真密钥")
        btn.setEnabled(bool(self._keys) and self._brute is not None)
        btn.clicked.connect(self._verify_keys)
        row.addWidget(btn)
        v.addLayout(row)
        return box

    def _verify_keys(self):
        if self._brute is None or not self._keys:
            return
        self.lbl_key.setText("正在用首个分片验证…")
        try:
            key = self._brute(self._keys)
        except Exception as e:
            self.lbl_key.setText(f"验证失败: {e}")
            return
        if key:
            self.lbl_key.setText(f"✅ 找到真密钥: {fragtools.key_to_hex(key)}")
            self._log(f"更多功能: 爆破得到真密钥 {fragtools.key_to_hex(key)}")
        else:
            self.lbl_key.setText("❌ 候选中没有能解出合法容器的密钥")
            self._log("更多功能: 疑似密钥全部验证失败")

    # ================================================================ 页签二
    def _build_tab_tools(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setSpacing(10)
        lay.addWidget(self._build_record_box())
        lay.addWidget(self._build_automation_box())
        lay.addStretch(1)
        return w

    def _hook(self, name: str, *args):
        fn = self._hooks.get(name)
        if fn is None:
            return False, "该能力在当前环境不可用"
        try:
            return fn(*args)
        except Exception as e:
            return False, f"{e}"

    def _build_record_box(self) -> QGroupBox:
        box = QGroupBox("录制(MediaRecorder → webm)")
        v = QVBoxLayout(box)
        v.setSpacing(6)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.btn_rec_video = QPushButton("视频录制")
        self.btn_rec_video.setToolTip("录制页面内正在播放的 video/audio 元素")
        self.btn_rec_video.clicked.connect(lambda: self._toggle_record("element"))
        self.btn_rec_rtc = QPushButton("录制webRTC")
        self.btn_rec_rtc.setToolTip(
            "代理 RTCPeerConnection 收集远端轨道后录制\n"
            "(页面需先建立连接, 即视频已经开始播放)")
        self.btn_rec_rtc.clicked.connect(lambda: self._toggle_record("webrtc"))
        self.btn_rec_screen = QPushButton("屏幕捕捉")
        self.btn_rec_screen.setToolTip("getDisplayMedia 录屏(会弹出画面源选择)")
        self.btn_rec_screen.clicked.connect(lambda: self._toggle_record("screen"))
        row.addWidget(self.btn_rec_video)
        row.addWidget(self.btn_rec_rtc)
        row.addWidget(self.btn_rec_screen)
        row.addStretch(1)
        v.addLayout(row)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("编码:"))
        self.cb_mime = QComboBox()
        self.cb_mime.setMinimumWidth(220)
        self.cb_mime.currentIndexChanged.connect(self._on_mime)
        row2.addWidget(self.cb_mime)
        row2.addWidget(QLabel("画面源:"))
        self.cb_src = QComboBox()
        self.cb_src.setMinimumWidth(180)
        self.cb_src.currentIndexChanged.connect(self._on_src)
        row2.addWidget(self.cb_src)
        row2.addStretch(1)
        v.addLayout(row2)

        self.lbl_rec = QLabel("未录制")
        self.lbl_rec.setObjectName("hintLabel")
        self.lbl_rec.setWordWrap(True)
        v.addWidget(self.lbl_rec)

        # 录制中每秒刷新时长/体积(对应猫抓录制按钮上的计时)
        self._rec_timer = QTimer(self)
        self._rec_timer.setInterval(1000)
        self._rec_timer.timeout.connect(self._tick_rec)

        self._reload_mimes()
        self._reload_sources()
        return box

    def _tick_rec(self):
        if not self._recording:
            self._rec_timer.stop()
            return
        ok, st = self._hook("stats")
        if not ok or not isinstance(st, dict) or not st:
            return
        if st.get("state") == "error":
            self.lbl_rec.setText(f"录制异常: {st.get('error') or '录制中断'}")
            self._recording = ""
            self._rec_timer.stop()
            self._set_rec_buttons("", False)
            return
        secs = int(st.get("secs") or 0)
        mb = float(st.get("bytes") or 0) / 1048576.0
        self.lbl_rec.setText(
            f"● 录制中（{self._recording}）· 已录 {secs} 秒 / {mb:.2f} MB · "
            f"{int(st.get('chunks') or 0)} 片")

    def _reload_mimes(self):
        self.cb_mime.blockSignals(True)
        self.cb_mime.clear()
        mimes = []
        try:
            mimes = list(self._hook("mimes")[1] or [])
        except Exception:
            mimes = []
        if not mimes:
            self.cb_mime.addItem("(当前环境无可用编码)", "")
        else:
            for m in mimes:
                self.cb_mime.addItem(m, m)
            cur = self._state.get("rec_mime") or mimes[0]
            idx = max(0, self.cb_mime.findData(cur))
            self.cb_mime.setCurrentIndex(idx)
        self.cb_mime.blockSignals(False)
        self._on_mime()

    def _on_mime(self):
        self._state["rec_mime"] = self.cb_mime.currentData() or ""

    def _reload_sources(self):
        self.cb_src.blockSignals(True)
        self.cb_src.clear()
        try:
            srcs = list(self._hook("sources")[1] or [])
        except Exception:
            srcs = []
        if not srcs:
            self.cb_src.addItem("(页面内没有 video/audio)", -1)
        else:
            for s in srcs:
                tag = f"#{s.get('i')} <{s.get('kind')}>"
                if s.get("w"):
                    tag += f" {s.get('w')}x{s.get('h')}"
                if s.get("playing"):
                    tag += " ▶"
                tag += f" {s.get('label', '')[:40]}"
                self.cb_src.addItem(tag, int(s.get("i") or 0))
        self.cb_src.blockSignals(False)
        self._on_src()

    def _on_src(self):
        v = self.cb_src.currentData()
        self._state["rec_index"] = int(v) if isinstance(v, int) and v >= 0 else 0

    def _toggle_record(self, kind: str):
        if self._recording:
            self._stop_record()
            return
        self._reload_mimes()
        self._reload_sources()
        ok, msg = self._hook("record", kind,
                             self._state.get("rec_index", 0),
                             self._state.get("rec_mime", ""))
        if ok:
            self._recording = kind
            self._set_rec_buttons(kind, True)
            self.lbl_rec.setText(f"● 录制中（{kind}）· {msg}")
            self._rec_timer.start()
        else:
            self.lbl_rec.setText(f"启动失败: {msg}")
        self._log(f"更多功能·录制: {msg}")
        self._refresh_summary()

    def _stop_record(self):
        ok, msg = self._hook("stop")
        self._recording = ""
        self._rec_timer.stop()
        self._set_rec_buttons("", False)
        self.lbl_rec.setText(msg)
        self._log(f"更多功能·录制: {msg}")
        self._refresh_summary()

    def _set_rec_buttons(self, kind: str, on: bool):
        for k, b, label in (("element", self.btn_rec_video, "视频录制"),
                            ("webrtc", self.btn_rec_rtc, "录制webRTC"),
                            ("screen", self.btn_rec_screen, "屏幕捕捉")):
            if on and k == kind:
                b.setText("■ 停止录制")
            else:
                b.setText(label)
            b.setEnabled((not self._recording) or (k == kind))
        # 「停止」统一由当前录制按钮承担
        if on:
            for k, b, _ in (("element", self.btn_rec_video, ""),
                            ("webrtc", self.btn_rec_rtc, ""),
                            ("screen", self.btn_rec_screen, "")):
                if k != kind:
                    b.setEnabled(False)

    def _build_automation_box(self) -> QGroupBox:
        box = QGroupBox("自动化")
        v = QVBoxLayout(box)
        v.setSpacing(6)

        row = QHBoxLayout()
        self.btn_mobile = QPushButton("模拟手机")
        self.btn_mobile.setCheckable(True)
        self.btn_mobile.setChecked(bool(self._state.get("simulate_mobile")))
        self.btn_mobile.setToolTip("把浏览器 UA 切成 iPhone 后重载页面\n"
                                  "(部分站点会给出移动端更易抓的清单)")
        self.btn_mobile.toggled.connect(self._on_mobile)
        row.addWidget(self.btn_mobile)

        self.btn_auto = QPushButton("自动下载")
        self.btn_auto.setCheckable(True)
        self.btn_auto.setChecked(bool(self._state.get("auto_download")))
        self.btn_auto.setToolTip("解析结束后自动把最佳一条加入下载队列")
        self.btn_auto.toggled.connect(self._on_auto)
        row.addWidget(self.btn_auto)

        self.btn_adb = QPushButton("广告拦截")
        self.btn_adb.setCheckable(True)
        self.btn_adb.setChecked(bool(self._state.get("adblock", True)))
        self.btn_adb.setToolTip(
            "拦截页面内的广告/统计请求(网络层, 立即生效)并隐藏广告位(元素层,\n"
            "下次加载页面生效)。视频地址永不拦截")
        self.btn_adb.toggled.connect(self._on_adblock)
        row.addWidget(self.btn_adb)
        row.addStretch(1)
        v.addLayout(row)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("数据发送:"))
        self.ed_send = QLineEdit(self._state.get("send_url", ""))
        self.ed_send.setPlaceholderText(
            "http://127.0.0.1:8080/hook —— POST 本次捕获结果(JSON)")
        self.ed_send.textChanged.connect(
            lambda t: self._state.__setitem__("send_url", t.strip()))
        row2.addWidget(self.ed_send, 1)
        self.btn_send = QPushButton("发送")
        self.btn_send.clicked.connect(self._do_send)
        row2.addWidget(self.btn_send)
        v.addLayout(row2)

        self.lbl_auto = QLabel("")
        self.lbl_auto.setObjectName("hintLabel")
        self.lbl_auto.setWordWrap(True)
        v.addWidget(self.lbl_auto)

        self._sync_toggle_text()
        return box

    def _sync_toggle_text(self):
        self.btn_mobile.setText("模拟手机: 开" if self.btn_mobile.isChecked()
                                else "模拟手机")
        self.btn_auto.setText("自动下载: 开" if self.btn_auto.isChecked()
                              else "自动下载")
        self.btn_adb.setText("广告拦截: 开" if self.btn_adb.isChecked()
                             else "广告拦截")

    def _on_mobile(self, on: bool):
        ok, msg = self._hook("mobile", on)
        self._state["simulate_mobile"] = bool(on) if ok else False
        if not ok:
            self.btn_mobile.blockSignals(True)
            self.btn_mobile.setChecked(False)
            self.btn_mobile.blockSignals(False)
        self.lbl_auto.setText(msg)
        self._sync_toggle_text()
        self._log(f"更多功能·模拟手机: {msg}")
        self._refresh_summary()

    def _on_auto(self, on: bool):
        ok, msg = self._hook("auto", on)
        self._state["auto_download"] = bool(on) if ok else False
        if not ok:
            self.btn_auto.blockSignals(True)
            self.btn_auto.setChecked(False)
            self.btn_auto.blockSignals(False)
        self.lbl_auto.setText(msg)
        self._sync_toggle_text()
        self._log(f"更多功能·自动下载: {msg}")
        self._refresh_summary()

    def _on_adblock(self, on: bool):
        ok, msg = self._hook("adblock", on)
        self._state["adblock"] = bool(on) if ok else not on
        if not ok:
            self.btn_adb.blockSignals(True)
            self.btn_adb.setChecked(not on)
            self.btn_adb.blockSignals(False)
        self.lbl_auto.setText(msg)
        self._sync_toggle_text()
        self._log(f"更多功能·广告拦截: {msg}")
        self._refresh_summary()

    def _do_send(self):
        url = self.ed_send.text().strip()
        if not url:
            self.lbl_auto.setText("请先填写接收地址")
            return
        ok, msg = self._hook("send", url)
        self.lbl_auto.setText(("✅ " if ok else "❌ ") + msg)
        self._log(f"更多功能·数据发送: {msg}")

    # ---------------------------------------------------------------- 汇总
    def _refresh_summary(self):
        bits = []
        sel = self._state.get("selected")
        if sel is not None:
            bits.append(f"分片勾选 {len(sel)} 个")
        if self._state.get("strip_image"):
            bits.append("剥离伪装头")
        if self._state.get("skip_decrypt"):
            bits.append("跳过解密")
        if self._state.get("audio_only"):
            bits.append("仅音频")
        if self._state.get("extra_query"):
            bits.append("追加分片参数")
        if self._recording:
            bits.append(f"录制中({self._recording})")
        if self._state.get("simulate_mobile"):
            bits.append("模拟手机")
        if self._state.get("auto_download"):
            bits.append("自动下载")
        if self._state.get("adblock"):
            bits.append("广告拦截")
        if self._state.get("send_url"):
            bits.append("已配置数据发送")
        self.lbl_summary.setText("已启用: " + ("、".join(bits) if bits
                                              else "全部默认(无额外处理)"))

    # ---------------------------------------------------------------- 对外
    def result_state(self) -> Dict:
        return dict(self._state)

    def closeEvent(self, event):
        # 关窗时若还在录制, 先停掉并落盘, 避免留下半截文件
        self._stop_if_recording()
        super().closeEvent(event)

    def done(self, r: int) -> None:
        """点「完成/取消」走的是 ``hide()``, **不会**触发 ``closeEvent``。

        不在这里收尾的话: 录制中直接点「完成」, 面板关了但录制仍在后台跑,
        再打开面板时按钮又显示"视频录制"(与真实状态不一致)。
        """
        self._stop_if_recording()
        super().done(r)

    def _stop_if_recording(self) -> None:
        if not self._recording:
            return
        try:
            self._hook("stop")
        except Exception:
            pass
        self._recording = ""
        try:
            self._rec_timer.stop()
        except Exception:
            pass
