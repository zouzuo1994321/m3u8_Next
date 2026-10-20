"""任务表交互功能测试: 多选清空 / 重排序同步 / 输出路径捕获 / 跳过下载中任务。

用法: QT_QPA_PLATFORM=offscreen python tests/test_tasktable.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QItemSelectionModel, Qt
from PySide6.QtWidgets import QApplication

from m3u8dl.downloader import DownloadConfig
from m3u8dl.sniffer import VideoInfo
from m3u8dl.ui.main_window import MainWindow


def add_task(win, name, state="排队中"):
    info = VideoInfo(title=name, url=f"http://x/{name}.m3u8",
                     resolution="1920x1080")
    cfg = DownloadConfig(url=info.url, save_dir="C:/Temp", save_name=name)
    tid = win._next_task_id
    win._next_task_id += 1
    win._tasks[tid] = {"info": info, "cfg": cfg, "state": state, "progress": {}}
    win._task_order.append(tid)
    return tid


def select_rows(win, rows):
    sm = win.task_table.selectionModel()
    sm.clearSelection()
    model = win.task_table.model()
    for r in rows:
        sm.select(model.index(r, 0),
                  QItemSelectionModel.Select | QItemSelectionModel.Rows)


def main():
    app = QApplication(sys.argv)
    win = MainWindow()
    # 隔离测试: 禁止 _pump_queue 真的启动下载
    win._pump_queue = lambda: None

    # ---- 1. 三个任务入表 ----
    tids = [add_task(win, f"视频{i}") for i in range(3)]
    win._refresh_task_table()
    assert win.task_table.rowCount() == 3
    print("[PASS] 3 个任务入表")

    # ---- 2. 多选清空(选中第 1、3 行) ----
    select_rows(win, [0, 2])
    win._clear_task_list()
    assert win.task_table.rowCount() == 1, \
        f"应剩 1 行, 实际 {win.task_table.rowCount()}"
    assert win._task_order == [tids[1]], f"剩余任务错误: {win._task_order}"
    print("[PASS] 多选清空: 移除选中行, 保留未选中行")

    # ---- 3. 输出路径捕获(下载完成 progress 带 path) ----
    win._active_task_id = tids[1]
    win._on_dl_progress({"state": "done", "path": "C:/Temp/视频1.mp4"})
    assert win._tasks[tids[1]].get("out_path") == "C:/Temp/视频1.mp4"
    print("[PASS] 下载完成时捕获输出路径(out_path)")

    # ---- 4. 清空跳过正在下载的任务 ----
    win._tasks[tids[1]]["state"] = "下载中"
    t4 = add_task(win, "视频3")
    win._refresh_task_table()
    assert win.task_table.rowCount() == 2
    select_rows(win, [0, 1])          # 全选(下载中 + 排队中)
    win._clear_task_list()
    assert win.task_table.rowCount() == 1
    assert win._task_order == [tids[1]], "下载中任务应被跳过保留"
    assert t4 not in win._tasks, "排队中任务应被清除"
    print("[PASS] 清空跳过正在下载的任务")

    # ---- 5. 拖动重排序同步 ----
    win._tasks[tids[1]]["state"] = "排队中"
    t5 = add_task(win, "视频4")
    t6 = add_task(win, "视频5")
    win._refresh_task_table()
    assert win.task_table.rowCount() == 3
    # 模拟 InternalMove 效果: 交换表格第 1、3 行的任务 ID
    tbl = win.task_table
    id_row0 = tbl.item(0, 0).data(Qt.UserRole)
    id_row2 = tbl.item(2, 0).data(Qt.UserRole)
    tbl.item(0, 0).setData(Qt.UserRole, id_row2)
    tbl.item(2, 0).setData(Qt.UserRole, id_row0)
    win._sync_task_order()
    assert win._task_order == [t6, t5, tids[1]], \
        f"重排序错误: {win._task_order}"
    print("[PASS] 拖动重排序后 _task_order 与表格行序一致")

    # ---- 6. 队列头部为重排后的第一个排队任务 ----
    pending = [t for t in win._task_order if win._tasks[t]["state"] == "排队中"]
    assert pending[0] == t6, "队列头部应为重排后的第一个排队任务"
    print("[PASS] 队列顺序跟随重排结果")

    # ---- 7. 任务表配置断言(多选/右键/拖动) ----
    from PySide6.QtWidgets import QAbstractItemView
    assert win.task_table.selectionMode() == QAbstractItemView.ExtendedSelection
    assert win.task_table.dragDropMode() == QAbstractItemView.InternalMove
    assert win.task_table.contextMenuPolicy() == Qt.CustomContextMenu
    print("[PASS] 任务表已启用多选/右键菜单/内部拖动")

    # ---- 8. 删除任务: 只移除记录, 不动视频文件 ----
    import tempfile
    tmpdir = Path(tempfile.mkdtemp(prefix="m3u8dl_del_"))
    f = tmpdir / "视频6.mp4"
    f.write_bytes(b"x" * 4096)
    t7 = add_task(win, "视频6")
    win._tasks[t7]["cfg"].save_dir = str(tmpdir)
    win._tasks[t7]["out_path"] = str(f)
    win._refresh_task_table()
    assert t7 in win._task_order
    win._delete_task(t7)
    assert t7 not in win._tasks, "任务记录应被移除"
    assert t7 not in win._task_order, "任务顺序中应被移除"
    assert f.exists(), "删除任务不应删除视频文件"
    print("[PASS] 删除任务仅移除记录, 保留视频文件")

    # ---- 9. 删除文件: 取消时不删; 确认后删文件并移除记录 ----
    from PySide6.QtWidgets import QMessageBox
    t8 = add_task(win, "视频7")
    win._tasks[t8]["cfg"].save_dir = str(tmpdir)
    win._tasks[t8]["out_path"] = str(f)
    win._refresh_task_table()

    orig_q = QMessageBox.question
    try:
        # 9a. 弹窗选「否」-> 文件与任务都保留
        QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.No)
        win._delete_task_file(t8)
        assert f.exists(), "取消删除时文件应保留"
        assert t8 in win._tasks, "取消删除时任务应保留"
        print("[PASS] 删除文件弹窗选「否」-> 文件与任务均保留")

        # 9b. 弹窗选「是」-> 删文件 + 移除任务
        QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
        win._delete_task_file(t8)
        assert not f.exists(), "确认后视频文件应被删除"
        assert t8 not in win._tasks, "确认后任务记录应被移除"
        print("[PASS] 删除文件弹窗选「是」-> 文件删除, 任务移除")
    finally:
        QMessageBox.question = orig_q

    # ---- 10. 正在下载的任务禁止删除 ----
    t9 = add_task(win, "视频8", state="下载中")
    win._refresh_task_table()
    info_calls = []
    orig_i = QMessageBox.information
    try:
        QMessageBox.information = staticmethod(
            lambda *a, **k: info_calls.append(a[2] if len(a) > 2 else ""))
        win._delete_task(t9)
        win._delete_task_file(t9)
    finally:
        QMessageBox.information = orig_i
    assert t9 in win._tasks, "下载中的任务不应被删除"
    assert len(info_calls) == 2, f"两次操作都应给出提示, 实际 {len(info_calls)}"
    print("[PASS] 下载中任务禁止删除/删除文件(给出提示)")

    print("\nTASK TABLE TEST OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
