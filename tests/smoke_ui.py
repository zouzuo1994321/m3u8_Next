"""UI 冒烟测试(Windows: 默认平台 + WA_DontShowOnScreen, 不弹窗)。

验证:
- 主窗口结构与各区域可见性
- 预览服务启动
- 嗅探结果填充 + 选中自动填充下载参数
- 截图存档(docs/shots/)
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from m3u8dl.sniffer import VideoInfo
from m3u8dl.ui.main_window import MainWindow
from m3u8dl.ui.theme import DARK_QSS


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(DARK_QSS)
    win = MainWindow()
    win.setAttribute(Qt.WA_DontShowOnScreen, True)
    win.show()
    for _ in range(10):
        app.processEvents()
        time.sleep(0.05)

    # ---- 结构断言 ----
    assert not win.tree.isHidden(), "结果树应可见"
    assert win.web is not None, "浏览器嗅探视图应存在"
    assert win.tabs.count() == 2, "应有『实时预览』『浏览器嗅探』两个页签"
    assert win.tabs.tabText(0) == "实时预览"
    assert win.tabs.tabText(1) == "浏览器嗅探"
    assert not win.btn_download.isHidden(), "下载按钮应可见"
    assert not win.log_view.isHidden(), "日志区应可见"
    assert not win.task_table.isHidden(), "任务表应可见"
    assert win.tree.columnCount() == 5
    assert win.preview.port > 0, "预览服务应已启动"
    assert ("Copyright" in win.lbl_version.text()
            or "©" in win.lbl_version.text()), "底部版本信息应包含版权"
    assert "Build" in win.lbl_version.text(), "底部版本信息应包含构建号"
    assert win.lbl_ffmpeg.text().startswith("●"), "顶部应显示 ffmpeg 环境状态"
    assert win.menuBar() is None or not win.menuBar().actions(), \
        "菜单栏应已移除"
    from m3u8dl.ui.main_window import MEDIA_OK
    if MEDIA_OK:
        assert win.player is not None, "QMediaPlayer 预览引擎应就绪"
        assert win.video_widget is not None, "视频输出控件应存在"

    # ---- 布局约定(v1.1.12): 网址栏上移通栏 / 下载进度下移到任务表上侧 ----
    left, right = win.main_split.widget(0), win.main_split.widget(1)
    assert not left.isAncestorOf(win.ed_url), "网址栏不应再嵌在左列里"
    assert left.isAncestorOf(win.tree), "视频列表应仍在左列"
    assert not right.isAncestorOf(win.progress), "下载进度不应再挂在右列"
    assert left.isAncestorOf(win.tabs), "预览/浏览器页签应仍在左列"
    assert win.ed_url.width() > 240, \
        f"网址框应吃满通栏宽度, 实际 {win.ed_url.width()}px"

    # 窄窗(默认 650)下列宽自适应: 收起「码率」「时长」, 把宽度让给标题
    assert win.width() == 650, f"默认窗口宽度应为 650, 实际 {win.width()}"
    assert win.tree.isColumnHidden(2), "窄窗下应收起「码率」列"
    assert win.tree.isColumnHidden(3), "窄窗下应收起「时长」列"

    # ---- 布局约定(v1.1.13): 进阶三件套在右列 / 预览控制条改版 ----
    for attr in ("btn_deep", "btn_cap", "btn_adv"):
        btn = getattr(win, attr, None)
        assert btn is not None, f"进阶按钮 {attr} 应存在"
        assert right.isAncestorOf(btn), f"进阶按钮 {attr} 应已移到右列"
        assert not left.isAncestorOf(btn), f"进阶按钮 {attr} 不应再在左列"
    from m3u8dl.ui.main_window import _TickBox
    assert isinstance(win.ck_mute, _TickBox), "静音勾选应改为自绘紫色勾选框"
    assert win.ck_mute.text() == "静音"
    assert win.btn_play.objectName() == "pvBtn", "播放按钮应用 pvBtn 样式"
    assert win.btn_pv_stop.objectName() == "pvBtn", "停止按钮应用 pvBtn 样式"

    # 默认 650x940 必须装得下(控件纵向已压缩过, 再涨就放不下)
    need = win.minimumSizeHint().height()
    assert need <= 940, f"最小高度 {need}px 超过默认窗高 940px"

    # ---- 布局约定(v1.1.14): 进阶功能加框 / 左右列底边对齐 / 勾选带白勾 ----
    from PySide6.QtWidgets import QGroupBox
    adv_box = win.btn_deep.parentWidget()
    assert isinstance(adv_box, QGroupBox), "进阶功能应套一个和「下载设置」同款外框"
    assert adv_box.title() == "进阶功能", f"外框标题应为「进阶功能」, 实际 {adv_box.title()!r}"
    assert adv_box.parentWidget() is right, "进阶功能框应挂在右列"
    settings_box = win.btn_download.parentWidget()
    assert isinstance(settings_box, QGroupBox) and settings_box.title() == "下载设置"
    # 左右列底边必须齐平: 左列页签框 vs 右列下载设置框
    lt = win.tabs.mapTo(win, win.tabs.rect().bottomLeft()).y()
    rt = settings_box.mapTo(win, settings_box.rect().bottomLeft()).y()
    assert abs(lt - rt) <= 4, f"左右列底边应对齐, 实际 {lt} vs {rt}"
    for attr in ("ck_binmerge", "ck_del", "ck_nomerge", "ck_noproxy", "ck_range"):
        ck = getattr(win, attr)
        assert isinstance(ck, _TickBox), f"{attr} 应改为自绘勾选框(带对勾)"
        assert ck._on.name() == "#1e40af", f"{attr} 选中色应为深蓝, 实际 {ck._on.name()}"
    assert win.ck_binmerge.isChecked() and win.ck_del.isChecked(), \
        "默认应勾选「二进制合并」「合并后删除分片」"

    # ---- 配色收口(v1.1.15): 全站蓝色 / 预览框去描边 / 五点改蓝 ----
    from PySide6.QtWidgets import QFrame
    from m3u8dl.ui.main_window import _DotsHandle
    pv_box = win.findChild(QFrame, "pvBox")
    assert pv_box is not None, "预览区应有 #pvBox 黑底容器"
    assert "border:none" in pv_box.styleSheet(), \
        f"预览区不应再带 1px 描边, 实际 {pv_box.styleSheet()!r}"
    assert "1px solid" not in pv_box.styleSheet()
    assert win.ck_mute._on.name() == "#1e40af", \
        f"静音勾选框应改深蓝, 实际 {win.ck_mute._on.name()}"
    assert _DotsHandle._DOT.name() == "#2f6bd8", \
        f"分栏五点应为深蓝, 实际 {_DotsHandle._DOT.name()}"
    assert "#2b57cf" in DARK_QSS and "#5b93f0" in DARK_QSS, \
        "音量条填充段/滑块应改蓝色"
    assert "#7c5cd6" not in DARK_QSS, "主题里不应再有紫色残留"

    # ---- 布局收口(v1.1.16): 表单左边缘齐 / 进度框去空白 / 任务表可拖行高 ----
    # 原来每行是独立 QHBoxLayout, 标签各按字宽占位, 「线程数/超时/请求头」的
    # 输入框比上面几行左移 13~25px(实测 75/62/50)。改 QGridLayout 后必须一条线。
    _grp = settings_box
    _xs = {}
    for attr in ("ed_workdir", "ed_title", "ed_save",
                 "sp_threads", "sp_timeout", "ed_headers"):
        _w = getattr(win, attr)
        _xs[attr] = _w.mapTo(_grp, _w.rect().topLeft()).x()
    assert len(set(_xs.values())) == 1, f"表单输入框左边缘应对齐, 实际 {_xs}"
    # 「就绪」下面不该再空出一截(原来是 布局下边距 9 + 样式下内边距 12 ≈ 21px)
    _pgrp = win.progress.parentWidget()
    _gap = _pgrp.height() - win.lbl_prog.mapTo(
        _pgrp, win.lbl_prog.rect().bottomLeft()).y()
    assert _gap <= 10, f"「就绪」下方留白应压到 10px 内, 实际 {_gap}px"
    # 任务表 / 日志之间是可拖动的竖向五点分割条
    from m3u8dl.ui.main_window import _DotsSplitter
    _bs = win.bottom_split
    assert isinstance(_bs, _DotsSplitter), "任务表与日志之间应是五点分割条"
    assert _bs.orientation() == Qt.Vertical, "该分割条应为竖向(拖高度)"
    assert _bs.handleWidth() == 16, f"拖动条宽度应为 16, 实际 {_bs.handleWidth()}"
    assert _bs.widget(0).isAncestorOf(win.task_table), "上半应是任务表"
    assert _bs.widget(1) is win.log_view, "下半应是日志"
    # 高度不再是写死的 setFixedHeight
    assert win.task_table.minimumHeight() != win.task_table.maximumHeight(), \
        "任务表高度应可拖(不能再 setFixedHeight)"

    # ---- 右列对齐收口(v1.1.17): 两行数字框同列 / 三按钮留缝 / 进阶按钮齐左边 ----
    # 「超时」框要与「线程数」框同列同宽,「重试/限速」标签与两个数字框同理
    assert win.sp_threads.x() == win.sp_timeout.x(), \
        f"超时框应与线程数框左对齐, 实际 {win.sp_timeout.x()} vs {win.sp_threads.x()}"
    assert win.sp_threads.width() == win.sp_timeout.width(), \
        "两个数字框应同宽"
    _lb = {w.text(): w for w in _grp.findChildren(type(win.lbl_info))
           if w.text() in ("重试", "限速")}
    assert _lb["重试"].x() == _lb["限速"].x(), \
        f"「重试」「限速」应左对齐, 实际 {_lb['重试'].x()} vs {_lb['限速'].x()}"
    assert win.sp_retry.x() == win.sp_speed.x(), \
        f"重试框应与限速框左对齐, 实际 {win.sp_retry.x()} vs {win.sp_speed.x()}"
    assert win.sp_retry.width() == win.sp_speed.width(), "两个次数字框应同宽"
    # 三个动作按钮之间要有 8px 间隙(子布局 spacing 曾退化成 0, 三个按钮贴死)
    _gap1 = win.btn_pause.x() - (win.btn_download.x() + win.btn_download.width())
    _gap2 = win.btn_stop.x() - (win.btn_pause.x() + win.btn_pause.width())
    assert (_gap1, _gap2) == (8, 8), f"三个按钮间隙应为 8px, 实际 {_gap1}/{_gap2}"
    # 「深度搜索」的左边要与下方「开始下载」齐平
    _dy = win.btn_deep.mapTo(right, win.btn_deep.rect().topLeft()).x()
    _dn = win.btn_download.mapTo(right, win.btn_download.rect().topLeft()).x()
    assert _dy == _dn, f"「深度搜索」应与「开始下载」左对齐, 实际 {_dy} vs {_dn}"

    # ---- 图标(v1.1.18): 窗口/任务栏图标来自 assets/icon.ico ----
    assert not win.windowIcon().isNull(), "主窗口图标不应为空"
    _sizes = sorted(s.width() for s in win.windowIcon().availableSizes())
    assert 256 in _sizes and 16 in _sizes, f"图标应含多档尺寸, 实际 {_sizes}"
    from m3u8dl.ui.main_window import assets_dir
    assert (assets_dir() / "icon.ico").exists(), "assets/icon.ico 应存在"

    # ---- 勾选框两列对齐(v1.1.19): 2×2 网格, 第二列同 x, 两列有间隙 ----
    from PySide6.QtCore import QPoint
    _ck = {k: getattr(win, k).mapTo(win, QPoint(0, 0)) for k in
           ("ck_binmerge", "ck_del", "ck_nomerge", "ck_noproxy")}
    assert _ck["ck_del"].x() == _ck["ck_noproxy"].x(), \
        f"「合并后删除分片」应与「不使用系统代理」左对齐, 实际 {_ck['ck_del'].x()} vs {_ck['ck_noproxy'].x()}"
    assert _ck["ck_binmerge"].x() == _ck["ck_nomerge"].x(), "第一列两个勾选框应左对齐"
    _cgap = _ck["ck_del"].x() - _ck["ck_binmerge"].x() - win.ck_binmerge.width()
    assert _cgap >= 12, f"两列勾选框之间应有间隙, 实际 {_cgap}px"
    assert _ck["ck_nomerge"].y() > _ck["ck_binmerge"].y(), "四个勾选框应排成两行"

    # ---- 「关于」对话框高度压缩(v1.1.19): 不再写死 360 ----
    from m3u8dl.ui.about import AboutDialog
    _dlg = AboutDialog(win)
    assert _dlg.height() <= 310, f"关于框高度应压到 ≤310, 实际 {_dlg.height()}"
    assert _dlg.width() == 456, f"关于框宽度应为 456, 实际 {_dlg.width()}"
    _dlg.deleteLater()

    # ---- 五点簇可拖(v1.1.20): 抓点只挪高亮, 不碰宽高 ----
    from m3u8dl.ui.main_window import _DotsHandle
    _h = win.main_split.handle(1)
    assert isinstance(_h, _DotsHandle), "主分栏把手应是 _DotsHandle"
    _sizes0 = list(win.main_split.sizes())
    _h.set_grip_pos(0.2)
    _c1 = _h._grip_center()
    _h.set_grip_pos(0.8)
    _c2 = _h._grip_center()
    assert _c2 > _c1 + 100, f"五点簇应随位置比例移动, 实际 {_c1:.0f} -> {_c2:.0f}"
    assert list(win.main_split.sizes()) == _sizes0, "挪动五点不应改变左右列宽"
    # 抓在簇上按下 -> 进"抓点"分支(不交给 splitter 去改宽高)
    from PySide6.QtCore import QEvent, QPointF
    from PySide6.QtGui import QMouseEvent
    _cx = _h.width() / 2.0
    _cy = _h._grip_center()
    _gp = _h.mapToGlobal(QPointF(_cx, _cy).toPoint())
    _h.mousePressEvent(QMouseEvent(QEvent.MouseButtonPress, QPointF(_cx, _cy),
                                   QPointF(_gp), Qt.LeftButton, Qt.LeftButton,
                                   Qt.NoModifier))
    assert _h._grip_drag, "在五点簇上按下应进入抓点状态"
    _h.mouseReleaseEvent(QMouseEvent(QEvent.MouseButtonRelease, QPointF(_cx, _cy),
                                     QPointF(_gp), Qt.LeftButton, Qt.LeftButton,
                                     Qt.NoModifier))
    assert not _h._grip_drag, "松开后应退出抓点状态"
    assert 0.0 <= win._grip_pos("ui/grip_lr") <= 1.0, "抓点位置应可读回"
    for _k in ("ui/grip_lr", "ui/grip_tb"):
        win._user_settings().remove(_k)

    # ---- 进度文案: 只报整文件进度, 不报分片数 ----
    win._on_dl_progress({"state": "running", "percent": 45.2,
                         "bytes_done": 86_000_000, "bytes_total": 190_530_000,
                         "speed": 12_400_000, "eta": 80,
                         "done": 3, "total": 40})
    txt = win.lbl_prog.text()
    assert "分片" not in txt, f"进度文案不应出现分片数: {txt!r}"
    assert "45.2%" in txt and "/ 181.70 MB" in txt, \
        f"进度文案应含百分比与总大小: {txt!r}"
    assert win.progress.value() == 452, f"进度条数值异常: {win.progress.value()}"

    # ---- 模拟嗅探结果填充 ----
    infos = [
        VideoInfo(title="测试影片 4K", mime="application/vnd.apple.mpegurl",
                  resolution="3840x2160", bitrate_kbps=12000,
                  duration_text="12s", size_text="≈18.00 MB",
                  url="http://127.0.0.1:1/hls/master.m3u8",
                  source="http://example.com/page", variant_count=3),
        VideoInfo(title="测试影片 1080p", mime="application/vnd.apple.mpegurl",
                  resolution="1920x1080", bitrate_kbps=5000,
                  duration_text="12s", size_text="≈7.50 MB",
                  url="http://127.0.0.1:1/hls/v1080.m3u8",
                  source="http://example.com/page", variant_count=3),
        VideoInfo(title="测试影片 720p", mime="application/vnd.apple.mpegurl",
                  resolution="1280x720", bitrate_kbps=2500,
                  duration_text="12s", size_text="≈3.75 MB",
                  url="http://127.0.0.1:1/hls/v720.m3u8",
                  source="http://example.com/page", variant_count=3),
    ]
    win._on_sniff_done(infos)
    for _ in range(5):
        app.processEvents()
    assert win.tree.topLevelItemCount() == 3, "应填充 3 条结果"

    # ---- 选中自动填充 ----
    win.tree.setCurrentItem(win.tree.topLevelItem(1))
    for _ in range(5):
        app.processEvents()
    assert win.current_info is infos[1], "选中项未记录"
    assert win.ed_save.text() == "测试影片 1080p", \
        f"保存名称未自动填充: {win.ed_save.text()!r}"
    assert win.ed_title.text() == "测试影片 1080p"

    # ---- 任务入队(不实际启动, 仅验证队列机制) ----
    win.current_info = infos[0]
    win._on_download()
    assert len(win._task_order) == 1, "任务应入队"
    assert win._tasks[1]["state"] in ("排队中", "下载中"), \
        f"任务状态异常: {win._tasks[1]['state']}"

    # ---- 截图 ----
    shots = Path(__file__).resolve().parent.parent / "docs" / "shots"
    shots.mkdir(parents=True, exist_ok=True)
    win.grab().save(str(shots / "main_window.png"))

    print("UI SMOKE OK")
    sys.stdout.flush()
    # QtWebEngine 在解释器析构阶段会崩溃(off屏幕平台尤甚), 断言已全部通过,
    # 直接以 0 退出, 避免把析构崩溃误判为测试失败。
    os._exit(0)


if __name__ == "__main__":
    main()
