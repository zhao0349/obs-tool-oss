"""深度-时间核查曲线自绘交互画布模块（`Curve`）。

视觉与性能设计：
1. **水文剖面坐标系**：纵轴为深度向下递增（水面在顶部，水底在底部），横轴为线性时间轴；
2. **多级色彩语义**：
   - 灰色细折线（`#94A3B8` / `#64748B`）：全时段背景原始观测序列与相邻下放片段；
   - 蓝色加粗折线（`#2563EB`）：当前整点匹配命中的下放片段；
   - 深灰实心三角（`▲`）：匹配片段的最深点峰值；
   - 暗红实心方块（`■`）：各目标水层最终入选的采样点（悬停可查看精确时间、深度及源文件行号）；
   - 灰色空心圆圈（`○`）：缺层时仅供人工核查参考的最近邻候选点；
3. **像素级自适应抽稀**：当单段连续曲线在当前视野内的点数超过画布像素宽度上限时，实时按分块保留首、尾、最浅、最深 4 个特征点，确保大跨度缩放平移丝滑流畅且绝不丢失波峰波谷。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QPushButton, QScrollBar, QToolTip, QVBoxLayout, QWidget

from .curve_context import moment as dt, time_ranges


class Curve(QWidget):
    """深度-时间核查曲线画布：支持滚轮定点缩放、水平滚动平移、一键定位目标整点及悬停查看采样来源。

    注意：本组件的所有缩放与浏览操作均为纯只读视图变换，不会修改任何采样结果。
    """

    def __init__(self) -> None:
        """初始化空白曲线画布、底部水平滚动条及右上角“定位目标”浮动按钮。"""
        super().__init__()
        self.result: dict[str, Any] | None = None
        self.row: dict[str, Any] | None = None
        self.context: dict[str, Any] | None = None
        self.hits: list[tuple[float, float, str]] = []
        self.view: tuple[datetime, datetime] | None = None
        self.default_view: tuple[datetime, datetime] | None = None
        self.browse: tuple[datetime, datetime] | None = None
        self.note: str = ""
        self.neighbors: list[int] = []

        self.setMinimumHeight(180)
        self.setMouseTracking(True)
        self.setAccessibleName("深度时间核查曲线，滚轮缩放、水平滚动及目标定位")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addStretch(1)

        self.scroll = QScrollBar(Qt.Orientation.Horizontal, self)
        layout.addWidget(self.scroll)
        self.scroll.valueChanged.connect(self.pan)

        self.locate = QPushButton(self)
        self.locate.clicked.connect(self.locate_target)
        self.locate.hide()

    def set_record(
        self,
        result: dict[str, Any] | None,
        row: dict[str, Any] | None,
        context: dict[str, Any] | None = None,
        note: str = "",
    ) -> None:
        """切换当前核查的整点结果行及其测点上下文，并重置为最佳初始时间视野。"""
        self.result = result
        self.row = row
        self.context = context
        self.note = note
        self.reset_zoom()

    def set_context(self, context: dict[str, Any] | None, note: str) -> None:
        """当后台异步加载完完整测点曲线快照后，更新背景曲线上下文并重算默认视野。"""
        self.context = context
        self.note = note
        self.reset_zoom()

    def reset_zoom(self) -> None:
        """恢复当前选中整点行对应的默认时间视野（`default_view`）与可滚动全范围（`browse`）。"""
        if self.row and self.result:
            self.default_view, self.browse, self.neighbors = time_ranges(
                self.result, self.row, self.context
            )
            self.view = self.default_view
        else:
            self.view = self.default_view = self.browse = None
        self.sync_scroll()
        self.update()

    def sync_scroll(self) -> None:
        """将当前视野窗口 `self.view` 在全时间范围 `self.browse` 中的比例同步到底部水平滚动条。"""
        was_blocked = self.scroll.blockSignals(True)
        if not self.view or not self.browse:
            self.scroll.setRange(0, 0)
        else:
            full_seconds = max(1.0, (self.browse[1] - self.browse[0]).total_seconds())
            view_seconds = (self.view[1] - self.view[0]).total_seconds()
            page_step = max(
                1, min(1_000_000, round(view_seconds / full_seconds * 1_000_000))
            )
            max_scroll_val = 1_000_000 - page_step
            self.scroll.setRange(0, max_scroll_val)
            self.scroll.setPageStep(page_step)
            self.scroll.setSingleStep(max(1, page_step // 10))
            current_offset_val = round(
                (self.view[0] - self.browse[0]).total_seconds()
                / full_seconds
                * 1_000_000
            )
            self.scroll.setValue(max(0, min(max_scroll_val, current_offset_val)))
        self.scroll.blockSignals(was_blocked)
        self.scroll.setEnabled(self.scroll.maximum() > 0)

    def set_view(self, left_time: datetime, right_time: datetime) -> None:
        """裁剪并设置当前时间视野窗口 `[left_time, right_time]`，随后同步滚动条并触发重绘。"""
        if not self.browse:
            return
        full_seconds = (self.browse[1] - self.browse[0]).total_seconds()
        clamped_span_sec = max(
            1.0, min(full_seconds, (right_time - left_time).total_seconds())
        )
        clamped_left = max(
            self.browse[0],
            min(
                self.browse[1] - timedelta(seconds=clamped_span_sec),
                left_time,
            ),
        )
        self.view = (clamped_left, clamped_left + timedelta(seconds=clamped_span_sec))
        self.sync_scroll()
        self.update()

    def pan(self, scroll_value: int) -> None:
        """响应水平滚动条拖动，保持当前时间跨度不变并平移视野窗口。"""
        if self.view and self.browse:
            current_span = self.view[1] - self.view[0]
            new_left = self.browse[0] + (self.browse[1] - self.browse[0]) * (
                scroll_value / 1_000_000
            )
            self.set_view(new_left, new_left + current_span)

    def wheelEvent(self, event) -> None:
        """以鼠标光标所在的时间点为锚点，按滚轮方向放大（0.8x）或缩小（1.25x）时间视野。"""
        if self.view:
            left_time, right_time = self.view
            current_span_sec = (right_time - left_time).total_seconds()
            cursor_fraction = max(
                0.0,
                min(
                    1.0,
                    (event.position().x() - 65) / max(1, self.width() - 100),
                ),
            )
            anchor_time = left_time + timedelta(
                seconds=current_span_sec * cursor_fraction
            )
            new_span_sec = current_span_sec * (
                0.8 if event.angleDelta().y() > 0 else 1.25
            )
            self.set_view(
                anchor_time - timedelta(seconds=new_span_sec * cursor_fraction),
                anchor_time + timedelta(seconds=new_span_sec * (1.0 - cursor_fraction)),
            )
        event.accept()

    def locate_target(self) -> None:
        """保持当前缩放跨度不变，将时间视野中心直接平移至当前整点目标时刻。"""
        if self.row and self.view:
            target_time = dt(self.row["target"])
            current_span = self.view[1] - self.view[0]
            self.set_view(
                target_time - current_span / 2,
                target_time + current_span / 2,
            )

    def mouseMoveEvent(self, event) -> None:
        """当鼠标悬停在采样点或候选点标记 10px 范围内时弹出源数据 Tooltip，否则隐藏。"""
        for hit_x, hit_y, tooltip_text in self.hits:
            if (
                abs(event.position().x() - hit_x) < 10
                and abs(event.position().y() - hit_y) < 10
            ):
                QToolTip.showText(event.globalPosition().toPoint(), tooltip_text, self)
                return
        QToolTip.hideText()

    def paintEvent(self, event) -> None:
        """绘制坐标网格、全时段背景曲线、匹配片段高亮折线、最深点三角、分层采样点及目标整点参考线。"""
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("white"))
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.hits = []

        painter.setPen(QColor("#334155"))
        painter.drawText(
            10, 17, "深度向下(m) · 蓝：匹配片段 · 灰：原始/相邻片段 · ■ 采样 · ▲ 峰值"
        )
        if not self.row or not self.view or not self.result:
            self.locate.hide()
            painter.drawText(10, 42, "选择结果记录查看曲线")
            return

        painter.drawText(
            QRectF(10, 22, max(1, self.width() - 20), 34),
            Qt.TextFlag.TextWordWrap,
            self.note,
        )

        left_time, right_time = self.view
        view_span_sec = max(1.0, (right_time - left_time).total_seconds())
        plot_width = max(1, self.width() - 100)
        plot_height = max(1, self.height() - 120 - self.scroll.height())
        plot_top = 64

        all_segments = self.result.get("segments", [])
        matched_seg_idx = self.row.get("segment")
        background_runs = (
            self.context.get("runs", [])
            if self.context
            else [
                seg.get("records", [])[:: max(1, len(seg.get("records", [])) // 2000)]
                for seg_idx, seg in enumerate(all_segments)
                if seg_idx in self.neighbors or seg_idx == matched_seg_idx
            ]
        )

        visible_depths = [
            rec["depth"]
            for run_records in background_runs
            for rec in run_records
            if left_time <= dt(rec["timestamp"]) <= right_time
        ]
        max_axis_depth = max(
            [1.0]
            + visible_depths
            + [
                sel["observation"]["depth"]
                for sel in self.row.get("selections", [])
                if sel.get("observation")
            ]
        )

        def _to_canvas_xy(record_item: dict[str, Any]) -> tuple[float, float]:
            """将观测记录的 `(timestamp, depth)` 映射为绘图区像素坐标 `(x, y)`。"""
            return (
                65.0
                + (dt(record_item["timestamp"]) - left_time).total_seconds()
                / view_span_sec
                * plot_width,
                plot_top + max(0.0, record_item["depth"]) / max_axis_depth * plot_height,
            )

        # 绘制水平深度网格线与左轴刻度（0 ~ max_axis_depth，共 5 条刻度线）
        for grid_idx in range(5):
            grid_y = plot_top + plot_height * grid_idx / 4
            painter.setPen(QColor("#E2E8F0"))
            painter.drawLine(QPointF(65, grid_y), QPointF(65 + plot_width, grid_y))
            painter.setPen(QColor("#475569"))
            painter.drawText(
                5, int(grid_y + 4), f"{max_axis_depth * grid_idx / 4:.2f}"
            )

        # 绘制垂直时间网格线与底轴时间戳（左、中、右 3 条刻度线）
        for grid_idx in range(3):
            grid_x = 65 + plot_width * grid_idx / 2
            tick_time = left_time + timedelta(seconds=view_span_sec * grid_idx / 2)
            painter.setPen(QColor("#E2E8F0"))
            painter.drawLine(
                QPointF(grid_x, plot_top), QPointF(grid_x, plot_top + plot_height)
            )
            painter.setPen(QColor("#475569"))
            painter.drawText(
                QRectF(
                    max(0.0, min(self.width() - 130.0, grid_x - 65.0)),
                    plot_top + plot_height + 4,
                    130,
                    36,
                ),
                Qt.AlignmentFlag.AlignCenter,
                tick_time.strftime("%Y-%m-%d\n%H:%M:%S"),
            )

        painter.save()
        painter.setClipRect(QRectF(64, plot_top - 1, plot_width + 2, plot_height + 2))

        def _draw_continuous_run(
            records: list[dict[str, Any]],
            stroke_color: str,
            stroke_width: float = 1.2,
            gap_threshold_sec: float = 300,
        ) -> None:
            """裁剪当前视野并执行保峰包络抽稀后绘制连续折线，遇到时间断档大于 `gap_threshold_sec` 时自动断开。"""
            if len(records) > 900:
                first_vis_idx = next(
                    (
                        idx
                        for idx, rec in enumerate(records)
                        if dt(rec["timestamp"]) >= left_time
                    ),
                    len(records),
                )
                last_vis_idx = next(
                    (
                        idx
                        for idx in range(first_vis_idx, len(records))
                        if dt(records[idx]["timestamp"]) > right_time
                    ),
                    len(records),
                )
                records = records[
                    max(0, first_vis_idx - 1) : min(len(records), last_vis_idx + 1)
                ]

            point_budget = max(200, min(900, int(plot_width) * 2))
            if len(records) > point_budget:
                block_step = max(
                    1, (len(records) * 4 + point_budget - 1) // point_budget
                )
                reduced_records: list[dict[str, Any]] = []
                for offset in range(0, len(records), block_step):
                    block = records[offset : offset + block_step]
                    key_indices = {
                        0,
                        len(block) - 1,
                        min(range(len(block)), key=lambda j: block[j]["depth"]),
                        max(range(len(block)), key=lambda j: block[j]["depth"]),
                    }
                    reduced_records.extend(block[j] for j in sorted(key_indices))
                records = reduced_records

            painter.setRenderHint(
                QPainter.RenderHint.Antialiasing, len(records) <= 500
            )
            polyline_points: list[QPointF] = []
            previous_timestamp: datetime | None = None
            painter.setPen(QPen(QColor(stroke_color), stroke_width))
            painter.setBrush(Qt.BrushStyle.NoBrush)

            def _flush_polyline() -> None:
                """将当前累积的连续像素点绘制为折线；若仅孤立单点则绘制实心小圆点。"""
                if len(polyline_points) > 1:
                    painter.drawPolyline(QPolygonF(polyline_points))
                elif polyline_points:
                    painter.drawEllipse(polyline_points[0], 2, 2)

            for rec in records:
                current_timestamp = dt(rec["timestamp"])
                if (
                    previous_timestamp
                    and (current_timestamp - previous_timestamp).total_seconds()
                    > gap_threshold_sec
                ):
                    _flush_polyline()
                    polyline_points = []
                previous_timestamp = current_timestamp
                polyline_points.append(QPointF(*_to_canvas_xy(rec)))
            _flush_polyline()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        gap_seconds = self.context.get("gap_seconds", 300) if self.context else 300
        for run_records in background_runs:
            _draw_continuous_run(
                run_records,
                "#94A3B8",
                gap_threshold_sec=float("inf") if self.context else gap_seconds,
            )

        # 绘制匹配片段（蓝色加粗）与相邻候选片段（深灰）及峰值三角标记
        for seg_idx, segment_dict in enumerate(all_segments):
            if seg_idx != matched_seg_idx and seg_idx not in self.neighbors:
                continue
            seg_records = segment_dict.get("records", [])
            sample_step = max(1, len(seg_records) // 2000)
            display_records = seg_records[::sample_step] + (
                seg_records[-1:] if sample_step > 1 else []
            )
            if (
                segment_dict.get("deepest")
                and segment_dict["deepest"] not in display_records
            ):
                display_records.append(segment_dict["deepest"])
                display_records.sort(key=lambda rec: dt(rec["timestamp"]))

            seg_color = "#2563EB" if seg_idx == matched_seg_idx else "#64748B"
            stroke_w = 2.0 if seg_idx == matched_seg_idx else 1.4
            if (
                self.context
                and segment_dict.get("start")
                and segment_dict.get("end")
            ):
                seg_begin = dt(segment_dict["start"])
                seg_finish = dt(segment_dict["end"])
                for run_records in background_runs:
                    _draw_continuous_run(
                        [
                            rec
                            for rec in run_records
                            if seg_begin <= dt(rec["timestamp"]) <= seg_finish
                        ],
                        seg_color,
                        stroke_w,
                        float("inf"),
                    )
            else:
                _draw_continuous_run(
                    display_records, seg_color, stroke_w, gap_seconds
                )

            peak_record = segment_dict.get("deepest")
            if seg_idx == matched_seg_idx and peak_record:
                peak_x, peak_y = _to_canvas_xy(peak_record)
                painter.setPen(QColor("#374151"))
                painter.setBrush(QColor("#374151"))
                painter.drawPolygon(
                    QPolygonF(
                        [
                            QPointF(peak_x, peak_y - 6),
                            QPointF(peak_x - 5, peak_y + 4),
                            QPointF(peak_x + 5, peak_y + 4),
                        ]
                    )
                )

        # 绘制各目标水层的采样点（实心红方块 ■）与缺层参考候选点（空心灰圆 ○）
        for selection_dict in self.row.get("selections", []):
            obs_record = selection_dict.get("observation")
            if not obs_record:
                continue
            obs_x, obs_y = _to_canvas_xy(obs_record)
            if selection_dict.get("display_kind") == "candidate":
                painter.setPen(QPen(QColor("#64748B"), 1))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawEllipse(QPointF(obs_x, obs_y), 5, 5)
            else:
                painter.setPen(QColor("#9F1239"))
                painter.setBrush(QColor("#9F1239"))
                painter.drawRect(QRectF(obs_x - 3, obs_y - 3, 6, 6))

            if left_time <= dt(obs_record["timestamp"]) <= right_time:
                self.hits.append(
                    (
                        obs_x,
                        obs_y,
                        f"{selection_dict['label']} · {dt(obs_record['timestamp']):%Y-%m-%d %H:%M:%S} · {obs_record['depth']:.3f} m\n"
                        f"{obs_record.get('file', '来源未记录')} 第{obs_record.get('line', '未知')}行",
                    )
                )

        # 绘制目标整点竖向虚线
        target_time = dt(self.row["target"])
        if left_time <= target_time <= right_time:
            target_x, _ = _to_canvas_xy(dict(timestamp=target_time, depth=0))
            painter.setPen(QPen(QColor("#A16207"), 1, Qt.PenStyle.DashLine))
            painter.drawLine(
                QPointF(target_x, plot_top),
                QPointF(target_x, plot_top + plot_height),
            )
            target_label = "目标 " + target_time.strftime("%m-%d %H:%M")
            label_x = max(
                65.0,
                min(
                    target_x + 3.0,
                    65.0
                    + plot_width
                    - painter.fontMetrics().horizontalAdvance(target_label),
                ),
            )
            painter.drawText(int(label_x), plot_top + 14, target_label)
        painter.restore()

        # 若存在完整曲线上下文，标注数据起点/终点虚线以及目标越界提示
        if self.context:
            for bound_key, bound_label in [("start", "数据起点"), ("end", "数据终点")]:
                bound_val = self.context.get(bound_key)
                if bound_val and left_time <= dt(bound_val) <= right_time:
                    bound_x, _ = _to_canvas_xy(dict(timestamp=bound_val, depth=0))
                    painter.setPen(QPen(QColor("#475569"), 1, Qt.PenStyle.DashLine))
                    painter.drawLine(
                        QPointF(bound_x, plot_top),
                        QPointF(bound_x, plot_top + plot_height),
                    )
                    painter.drawText(
                        int(max(65.0, min(65.0 + plot_width - 195.0, bound_x))),
                        int(
                            plot_top + plot_height - 8
                            if bound_key == "end"
                            else plot_top + plot_height - 25
                        ),
                        bound_label + " " + dt(bound_val).strftime("%Y-%m-%d %H:%M:%S"),
                    )
            ctx_start, ctx_end = self.context.get("start"), self.context.get("end")
            if ctx_start and target_time < dt(ctx_start):
                painter.drawText(70, plot_top + 30, "目标在数据开始之前")
            elif ctx_end and target_time > dt(ctx_end):
                painter.drawText(70, plot_top + 30, "目标在数据结束之后")

        # 当目标整点移出当前视野窗口时，在左/右侧展示“定位目标”快捷跳转按钮
        is_target_outside = target_time < left_time or target_time > right_time
        self.locate.setVisible(is_target_outside)
        if is_target_outside:
            self.locate.setText(
                ("← " if target_time < left_time else "→ ")
                + target_time.strftime("%Y-%m-%d %H:%M:%S")
                + " · 定位目标"
            )
            self.locate.adjustSize()
            self.locate.move(
                (
                    65
                    if target_time < left_time
                    else max(65, self.width() - self.locate.width() - 8)
                ),
                40,
            )
