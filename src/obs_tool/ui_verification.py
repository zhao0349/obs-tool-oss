"""源码态与编译发行版共用的 Windows 真实桌面交互端到端自检模块（`--verify-ui`）。

设计目标：
1. 在隔离的验证目录（`OBS_UI_VERIFY_DIR`）下自动生成合成 `.dat` 观测文件（绝不触碰任何真实生产数据）；
2. 启动完整的 `Window` 主界面与 Qt 事件循环，通过定时器步进模拟真实用户操作：
   - 打开帮助/关于对话框并截图验证版本号、作者与联系链接；
   - 弹出并取消/确认 Windows 原生文件夹与多选文件对话框（`IFileOpenDialog`）；
   - 验证智能水深识别取消保护、待核查水深勾选确认与估算结果回填；
   - 创建 4 层自定义分层规则，验证自动排序、重复拦截与规则库持久化；
   - 验证非连续多行批量编辑（跨年时间段与水深修改）及日期/时间秒级弹出选择器；
   - 运行完整后台批处理，验证 CSV 18 列表头输出、结果表格预览、曲线上下文子进程加载、滚轮缩放、片段复用详情弹窗与大小写冲突拦截。
"""

from __future__ import annotations

import csv
from datetime import datetime, timedelta
import faulthandler
import json
import os
from pathlib import Path

from PySide6.QtCore import (
    QCoreApplication,
    QDateTime,
    QObject,
    QPoint,
    QPointF,
    Qt,
    QTimer,
    Slot,
)
from PySide6.QtGui import QImageReader, QWheelEvent
from PySide6.QtWidgets import QApplication

from .gui import Window
from .native_shell import command_dialog, dialog_window
from .rule_store import load_rules

root = Path(os.environ.get("OBS_UI_VERIFY_DIR", "validation/ui-v3-source")).resolve()
root.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("OBS_SETTINGS_DIR", str(root / "settings"))
os.environ.setdefault("OBS_JOB_DIR", str(root / "internal"))
fault = (root / "fault.log").open("w")
faulthandler.enable(fault)
events: list[str] = []


def record(message: str) -> None:
    """将当前完成的验收里程碑追加写入验证目录下的 `events.json`，便于外部脚本审计。"""
    events.append(message)
    (root / "events.json").write_text(
        json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8"
    )


app = QApplication([])


class Probe(QObject):
    """基于 Qt 事件循环与状态机步进的端到端桌面交互验收驱动器。"""

    def __init__(self) -> None:
        """构造合成 `.dat` 观测文件与输出目录，显示主窗口并启动步进定时器与 120 秒看门狗。"""
        super().__init__()
        self.window = Window()
        self.window.show()
        self.step = 0
        self.sent = False
        self.done = False
        self.input = root / "输入DAT"
        self.output = root / "输出CSV"
        self.input.mkdir(exist_ok=True)
        self.output.mkdir(exist_ok=True)

        # 生成 A#、B#、C# 三个具有不同最大水深比例（5m / 7m / 10m）的跨年合成 DAT 文件
        for point_code, depth_scale in [("A", 1), ("B", 1.4), ("C", 2)]:
            dat_lines: list[str] = []
            for minute_offset, base_depth in enumerate(
                [0, 0.5, 1, 2, 3, 4, 5, 4, 3, 2, 1, 0, 0, 0, 0]
            ):
                obs_time = datetime(2025, 12, 31, 23, 55) + timedelta(
                    minutes=minute_offset
                )
                dat_lines.append(
                    f"{obs_time:%H:%M:%S %m/%d/%Y} {base_depth * depth_scale} 2 20 1 30 6"
                )
            (self.input / (point_code + "#.dat")).write_text(
                "OBS3A #0000 Firmware Version: 6.20\n"
                "Sensor                            Data Columns\n"
                " 1 Depth Mean\n"
                " 2 OBS-1 (NTU) Mean\n"
                " 4 Temperature (deg. C) Mean\n"
                " 5 Conductivity(mS/cm) Mean\n"
                " 8 Salinity (PSU) Mean\n"
                " 9 Battery (V) Mean\n"
                + "\n".join(dat_lines),
                encoding="utf-8",
            )

        self.window.output.setText(str(self.output))
        self.window.pending_folder = str(self.input)
        self.cancel_checked = False

        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.advance)
        self.timer.start(200)

        self.watchdog = QTimer(self)
        self.watchdog.setSingleShot(True)
        self.watchdog.timeout.connect(self.timeout)
        self.watchdog.start(120000)

        from . import __version__
        from .branding import ICON_PATH
        from .record_store import record_directory

        runtime_dir = Path(QCoreApplication.applicationDirPath()).resolve()
        runtime_locations = dict(
            version=__version__,
            application=str(QCoreApplication.applicationFilePath()),
            icon=str(ICON_PATH),
            settings=os.environ["OBS_SETTINGS_DIR"],
            jobs=os.environ["OBS_JOB_DIR"],
            records=str(record_directory(self.output)),
            png_supported=b"png" in QImageReader.supportedImageFormats(),
            icon_loaded=not app.windowIcon().pixmap(32, 32).isNull(),
        )
        assert runtime_locations["icon_loaded"] and runtime_locations["png_supported"]
        # 确保运行期可写数据目录（配置、任务队列、核查记录）均不污染应用程序安装目录
        assert not any(
            Path(runtime_locations[dir_key]).resolve().is_relative_to(runtime_dir)
            for dir_key in ("settings", "jobs", "records")
        )
        (root / "runtime.json").write_text(
            json.dumps(runtime_locations, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        record("started")

    def wait_native(self, accept: bool) -> bool:
        """轮询等待原生文件选择器窗口句柄出现后发送确认或取消指令，返回子进程是否已完整退出。"""
        picker = self.window.file_picker
        if not self.sent:
            if not dialog_window(int(picker.process.processId())):
                return False
            if accept:
                command_dialog(int(picker.process.processId()), accept=True)
            else:
                picker.reject()
            self.sent = True
        return picker.ended

    @Slot()
    def advance(self) -> None:
        """按步骤编号 `self.step` 推进桌面 UI 自动化验收，捕获任何断言失败并立即终止进程。"""
        try:
            main_win = self.window
            if self.step == 0:
                from . import (
                    AUTHOR,
                    AUTHOR_EMAIL,
                    AUTHOR_URL,
                    ISSUES_URL,
                    PROJECT_URL,
                    __version__,
                )

                main_win.show_help()
                main_win.help_dialog.tabs.setCurrentIndex(1)
                assert (
                    __version__ in main_win.help_dialog.about.toPlainText()
                    and AUTHOR in main_win.help_dialog.about.toPlainText()
                    and AUTHOR_EMAIL in main_win.help_dialog.about.toPlainText()
                )
                about_html = main_win.help_dialog.about.toHtml()
                for link in (
                    AUTHOR_URL,
                    f"mailto:{AUTHOR_EMAIL}",
                    PROJECT_URL,
                    ISSUES_URL,
                    f"{PROJECT_URL}/blob/main/LICENSE",
                    f"{PROJECT_URL}/blob/main/ASSET_LICENSE.md",
                    f"{PROJECT_URL}/blob/main/docs/THIRD_PARTY_NOTICES.md",
                ):
                    assert f'href="{link}"' in about_html
                assert main_win.help_dialog.about.openExternalLinks()
                main_win.help_dialog.grab().save(str(root / "about.png"))
                main_win.help_dialog.accept()
                record("help About version, author and contact links verified")

                main_win.advanced_button.click()
                assert main_win.dialog.isVisible()
                main_win.dialog.reject()
                record("advanced cancelled")

                main_win.folder_button.click()
                self.sent = False
            elif self.step == 1:
                if not self.wait_native(False):
                    self.timer.start(100)
                    return
                assert not main_win.points
                assert main_win.analysis_worker is None
                record("native folder cancelled without DAT read")

                main_win.folder_button.click()
                self.sent = False
            elif self.step == 2:
                if not self.wait_native(True):
                    self.timer.start(100)
                    return
                record("native OneDrive folder accepted without estimation")
                assert len(main_win.points) == 3 and main_win.analysis_worker is None
                main_win.execute()
                main_win.cancel.click()
            elif self.step == 3:
                if main_win.analysis_worker and main_win.analysis_worker.isRunning():
                    self.timer.start(100)
                    return
                if not self.cancel_checked:
                    assert len(main_win.points) == 3
                    assert (main_win.analysis_worker.job / "cancel").exists()
                    record("depth analysis cancellation preserved input list")
                    self.cancel_checked = True
                    main_win.execute()
                    self.timer.start(100)
                    return
                if main_win.task_state == "等待水深确认":
                    for checkbox in main_win.depth_review.checks:
                        checkbox.setChecked(True)
                    main_win.depth_review.validate_accept()
                    self.timer.start(100)
                    return
                if main_win.worker and main_win.worker.isRunning():
                    self.timer.start(100)
                    return
                if not all(point.depth is not None for point in main_win.points):
                    self.timer.start(100)
                    return
                assert [point.depth for point in main_win.points] == [5, 7, 10]
                assert all(
                    point.depth_source == "estimated" for point in main_win.points
                )
                record("smart depths 5/7/10 verified")

                main_win.advanced_button.click()
                main_win.dialog.create_button.click()
                layer_editor = main_win.dialog.create_dialog.editor
                for test_ratio in [0.05, 0.1, 0.9, 0.95]:
                    layer_editor.ratio.setValue(test_ratio)
                    assert layer_editor.ratio.value() == test_ratio
                for test_ratio in [0.8, 0.2, 0.6, 0.4]:
                    layer_editor.ratio.setValue(test_ratio)
                    layer_editor.add_button.click()
                assert layer_editor.positions == [0.2, 0.4, 0.6, 0.8]
                layer_editor.ratio.setValue(0.2)
                layer_editor.add_button.click()
                assert len(layer_editor.positions) == 4
                main_win.dialog.create_dialog.grab().save(
                    str(root / "create-rule.png")
                )
                main_win.dialog.create_dialog.save()
                assert not main_win.dialog.create_dialog.isVisible()
                main_win.dialog.apply_settings()
                assert not main_win.dialog.isVisible()

                main_win.table.setCurrentCell(0, 1)
                main_win.edit_button.click()
                scheme_combo = main_win.point_dialog.layer_choice.combo
                scheme_combo.setCurrentIndex(scheme_combo.findText("自定义规则1"))
                main_win.point_dialog.validate_accept()
                assert main_win.points[0].layer_positions == [0.2, 0.4, 0.6, 0.8]
                assert main_win.points[0].layer_count == 4
                assert load_rules()[0]["positions"] == [0.2, 0.4, 0.6, 0.8]
                record("four layers, sorting, duplicate guard and persistence verified")
            elif self.step == 4:
                main_win.checked_codes = {
                    main_win.points[0].code,
                    main_win.points[2].code,
                }
                main_win.populate()
                main_win.bulk_button.click()
                bulk_dlg = main_win.bulk_dialog
                bulk_dlg.change_depth.setChecked(True)
                bulk_dlg.depth.setValue(8)
                bulk_dlg.start_mode.setCurrentIndex(1)
                bulk_dlg.start.setDateTime(
                    QDateTime.fromString("2025-12-31 23:00:00", "yyyy-MM-dd HH:mm:ss")
                )
                bulk_dlg.end_mode.setCurrentIndex(1)
                bulk_dlg.end.setDateTime(
                    QDateTime.fromString("2026-01-01 01:00:00", "yyyy-MM-dd HH:mm:ss")
                )
                bulk_dlg.validate_accept()
                assert [point.depth for point in main_win.points] == [8, 7, 8]
                assert main_win.points[0].layer_positions == [0.2, 0.4, 0.6, 0.8]
                assert main_win.points[1].start is None
                record(
                    "noncontiguous bulk depth/cross-year time, unchanged fields and custom scheme verified"
                )

                main_win.table.setCurrentCell(0, 0)
                main_win.mapping_button.click()
                main_win.mapping_dialog.reject()
                main_win.edit_button.click()
                point_dlg = main_win.point_dialog
                values_before = point_dlg.values()
                point_dlg.start.date_button.click()
                point_dlg.start.popup.grid.move_page(1)
                point_dlg.start.popup.close()
                assert point_dlg.values() == values_before
                point_dlg.start.time_button.click()
                point_dlg.start.popup.columns[2].step(59)
                point_dlg.start.popup.cancel.click()
                assert point_dlg.values() == values_before
                point_dlg.start.time_button.click()
                point_dlg.start.popup.columns[2].step(59)
                point_dlg.start.popup.confirm.click()
                assert point_dlg.values()[2].endswith(":59")
                point_dlg.grab().save(str(root / "datetime.png"))
                point_dlg.reject()
                record(
                    "point date/time flyouts confirm/cancel and second precision verified"
                )
                main_win.files_button.click()
                self.sent = False
            elif self.step == 5:
                if not self.wait_native(False):
                    self.timer.start(100)
                    return
                record("native DAT multiselect dialog cancelled")
                main_win.output.setText(str(self.output))
                main_win.output_button.click()
                self.sent = False
            elif self.step == 6:
                if not self.wait_native(True):
                    self.timer.start(100)
                    return
                assert main_win.output.text() == str(self.output)
                record("native output folder accepted")
                main_win.start.click()
                assert main_win.worker is not None
            elif self.step == 7:
                if main_win.worker.ended and not main_win.report:
                    raise RuntimeError(main_win.summary.text())
                if main_win.worker.isRunning() or not main_win.report:
                    self.timer.start(100)
                    return
                point_a_id = next(
                    point.point_id for point in main_win.points if point.code == "A"
                )
                assert main_win.report["status"] == "completed"
                assert main_win.results[point_a_id]["layer_count"] == 4
                assert main_win.preview.columnCount() == 18
                with (main_win.directory / "A#_output.csv").open(
                    encoding="utf-8-sig", newline=""
                ) as csv_stream:
                    csv_rows = list(csv.reader(csv_stream))
                assert len(csv_rows[1]) == 18
                assert "0.4H 温度" in csv_rows[1]
                valid_row_index = next(
                    row_idx
                    for row_idx, row_dict in enumerate(
                        main_win.results[point_a_id]["rows"]
                    )
                    if row_dict["segment"] is not None
                )
                main_win.preview.setCurrentCell(valid_row_index, 0)
                assert len(main_win.curve.row["selections"]) == 4
                main_win.grab().save(str(root / "preview.png"))
                record("CSV, preview and process worker verified")
            elif self.step == 8:
                result_view = main_win.result_view
                if result_view.context_loader.process is not None:
                    self.timer.start(100)
                    return
                assert (
                    result_view.curve_context is not None
                    and result_view.curve_context["observation_count"] == 15
                )
                result_view.toggle.setChecked(True)
                result_view.aux.setCurrentIndex(0)
                previous_view_window = result_view.curve.view
                wheel_event = QWheelEvent(
                    QPointF(250, 100),
                    QPointF(result_view.curve.mapToGlobal(QPoint(250, 100))),
                    QPoint(),
                    QPoint(0, 120),
                    Qt.MouseButton.NoButton,
                    Qt.KeyboardModifier.NoModifier,
                    Qt.ScrollPhase.ScrollUpdate,
                    False,
                )
                app.sendEvent(result_view.curve, wheel_event)
                assert result_view.curve.view != previous_view_window
                result_view.curve.scroll.setValue(
                    result_view.curve.scroll.maximum()
                )
                result_view.curve.reset_zoom()
                assert "片段复用" in result_view.details.toPlainText()
                result_view.open_detail_window()
                assert (
                    result_view.detail_dialog.isVisible()
                    and not result_view.detail_dialog.windowIcon().isNull()
                )
                result_view.detail_dialog.grab().save(str(root / "curve-detail.png"))
                result_view.detail_dialog.reject()

                point_count_before = len(main_win.points)
                main_win.add_files([str(self.input / "A#.dat")])
                assert len(main_win.points) == point_count_before
                conflict_file = self.input / "a#extra.dat"
                conflict_file.write_text("not processed", encoding="utf-8")
                try:
                    main_win.add_files([str(conflict_file)])
                except ValueError:
                    pass
                else:
                    raise AssertionError("case conflict accepted")
                assert len(main_win.points) == point_count_before
                record(
                    "curve context subprocess, zoom/scroll, reuse/source details, independent icon and DAT conflict verified"
                )
                main_win.clear_button.click()
                assert not main_win.points
                assert main_win.preview.rowCount() == 0
                record("clear verified")
                self.done = True
                record("passed")
                self.watchdog.stop()
                main_win.close()
                app.quit()
                return
            self.step += 1
            self.timer.start(200)
        except Exception as exc:
            record("FAILED: " + repr(exc))
            self.watchdog.stop()
            if main_win.file_picker and main_win.file_picker.isVisible():
                main_win.file_picker.reject()
            app.exit(1)

    @Slot()
    def timeout(self) -> None:
        """120 秒看门狗超时回调：记录 `timeout`、关闭可能挂起的原生选择器并以退出码 2 终止。"""
        record("timeout")
        if self.window.file_picker and self.window.file_picker.isVisible():
            self.window.file_picker.reject()
        app.exit(2)


probe = Probe()
code = app.exec()
record("event loop ended")
raise SystemExit(code if probe.done else code or 1)
