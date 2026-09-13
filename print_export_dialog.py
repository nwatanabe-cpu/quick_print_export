import os
from qgis.PyQt.QtCore import QRectF
from qgis.PyQt.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QGroupBox,
    QFormLayout,
    QComboBox,
    QCheckBox,
    QRadioButton,
    QButtonGroup,
    QPushButton,
    QLabel,
    QFileDialog,
    QMessageBox,
    QDoubleSpinBox,
    QAbstractSpinBox,
)
import math
from qgis.core import (
    QgsProject,
    QgsPrintLayout,
    QgsLayoutItemMap,
    QgsLayoutItemLabel,
    QgsLayoutItemScaleBar,
    QgsLayoutPoint,
    QgsLayoutSize,
    QgsUnitTypes,
    QgsRectangle,
    QgsLayoutExporter,
    QgsCoordinateTransform,
)


def _nice_number(value):
    """スケールバーの1区間あたりの距離を 1/2/5 * 10^n の "きりのいい" 数値に丸める"""
    if value <= 0:
        return 1.0
    exponent = math.floor(math.log10(value))
    fraction = value / (10 ** exponent)
    if fraction < 1.5:
        nice = 1
    elif fraction < 3:
        nice = 2
    elif fraction < 7:
        nice = 5
    else:
        nice = 10
    return nice * (10 ** exponent)
from qgis.gui import QgsMapToolExtent

# 用紙サイズプリセット(mm, 縦向き基準)
PAPER_SIZES_MM = {
    "A4": (210.0, 297.0),
    "A3": (297.0, 420.0),
    "A2": (420.0, 594.0),
    "A1": (594.0, 841.0),
    "A0": (841.0, 1189.0),
    "カスタム": (297.0, 420.0),
}


def _make_coord_spinbox():
    sb = QDoubleSpinBox()
    sb.setDecimals(3)
    sb.setRange(-99999999.0, 99999999.0)
    sb.setButtonSymbols(QAbstractSpinBox.NoButtons)  # スクロールでの誤操作防止
    sb.setMinimumWidth(130)
    return sb


class PrintExportDialog(QDialog):
    def __init__(self, iface):
        super().__init__(iface.mainWindow())
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self._prev_map_tool = None
        self._extent_tool = None

        self.setWindowTitle("座標指定 印刷/エクスポート")
        self._build_ui()
        self._connect_signals()

        # 初期値: 現在のキャンバス表示範囲
        self._set_fields_from_extent(self.canvas.extent())
        self._on_paper_changed()

    # ---------------- UI構築 ----------------

    def _build_ui(self):
        root = QVBoxLayout(self)

        # --- 出力設定 ---
        grp_out = QGroupBox("出力設定")
        form_out = QFormLayout()
        self.cmb_format = QComboBox()
        self.cmb_format.addItems(["PDF", "PNG"])
        self.spn_dpi = QDoubleSpinBox()
        self.spn_dpi.setRange(72, 1200)
        self.spn_dpi.setValue(300)
        self.spn_dpi.setDecimals(0)
        form_out.addRow("出力形式:", self.cmb_format)
        form_out.addRow("解像度(DPI, PNG時):", self.spn_dpi)
        grp_out.setLayout(form_out)
        root.addWidget(grp_out)

        # --- 用紙設定 ---
        grp_paper = QGroupBox("用紙設定")
        v_paper = QVBoxLayout()
        h1 = QHBoxLayout()
        self.cmb_paper = QComboBox()
        self.cmb_paper.addItems(list(PAPER_SIZES_MM.keys()))
        self.cmb_paper.setCurrentText("A3")
        h1.addWidget(QLabel("用紙サイズ:"))
        h1.addWidget(self.cmb_paper)

        self.rb_portrait = QRadioButton("縦")
        self.rb_landscape = QRadioButton("横")
        self.rb_landscape.setChecked(True)
        self.bg_orientation = QButtonGroup(self)
        self.bg_orientation.addButton(self.rb_portrait)
        self.bg_orientation.addButton(self.rb_landscape)
        h1.addWidget(self.rb_portrait)
        h1.addWidget(self.rb_landscape)
        v_paper.addLayout(h1)

        h2 = QHBoxLayout()
        self.spn_paper_w = QDoubleSpinBox()
        self.spn_paper_h = QDoubleSpinBox()
        for sb in (self.spn_paper_w, self.spn_paper_h):
            sb.setRange(10.0, 5000.0)
            sb.setDecimals(1)
            sb.setSuffix(" mm")
        h2.addWidget(QLabel("幅:"))
        h2.addWidget(self.spn_paper_w)
        h2.addWidget(QLabel("高さ:"))
        h2.addWidget(self.spn_paper_h)
        v_paper.addLayout(h2)
        grp_paper.setLayout(v_paper)
        root.addWidget(grp_paper)

        # --- 縮尺・回転 ---
        grp_scale = QGroupBox("縮尺・回転")
        form_scale = QFormLayout()
        self.chk_auto_scale = QCheckBox("自動縮尺(範囲を用紙いっぱいに合わせる)")
        self.chk_auto_scale.setChecked(True)
        self.spn_scale = QDoubleSpinBox()
        self.spn_scale.setRange(1, 1000000)
        self.spn_scale.setDecimals(0)
        self.spn_scale.setValue(2000)
        self.spn_scale.setPrefix("1 / ")
        self.spn_scale.setEnabled(False)
        self.spn_rotation = QDoubleSpinBox()
        self.spn_rotation.setRange(-360.0, 360.0)
        self.spn_rotation.setDecimals(2)
        self.spn_rotation.setSuffix(" °")
        form_scale.addRow(self.chk_auto_scale)
        form_scale.addRow("縮尺:", self.spn_scale)
        form_scale.addRow("図面角度:", self.spn_rotation)
        grp_scale.setLayout(form_scale)
        root.addWidget(grp_scale)

        # --- 印刷範囲(座標) ---
        grp_extent = QGroupBox("印刷範囲(座標)")
        v_extent = QVBoxLayout()

        h3 = QHBoxLayout()
        self.btn_pick_canvas_extent = QPushButton("現在のキャンバス表示")
        self.btn_pick_full_extent = QPushButton("全レイヤの表示範囲")
        self.btn_drag_extent = QPushButton("キャンバスでドラッグ指定")
        h3.addWidget(self.btn_pick_canvas_extent)
        h3.addWidget(self.btn_pick_full_extent)
        h3.addWidget(self.btn_drag_extent)
        v_extent.addLayout(h3)

        form_coord = QFormLayout()
        self.spn_xmin = _make_coord_spinbox()
        self.spn_ymin = _make_coord_spinbox()
        self.spn_xmax = _make_coord_spinbox()
        self.spn_ymax = _make_coord_spinbox()

        row1 = QHBoxLayout()
        row1.addWidget(QLabel("X="))
        row1.addWidget(self.spn_xmin)
        row1.addWidget(QLabel("Y="))
        row1.addWidget(self.spn_ymin)
        form_coord.addRow("用紙左下座標:", row1)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("X="))
        row2.addWidget(self.spn_xmax)
        row2.addWidget(QLabel("Y="))
        row2.addWidget(self.spn_ymax)
        form_coord.addRow("用紙右上座標:", row2)

        v_extent.addLayout(form_coord)

        self.btn_recompute = QPushButton("範囲・縮尺を計算(用紙の縦横比に合わせる)")
        v_extent.addWidget(self.btn_recompute)

        self.lbl_status = QLabel("")
        self.lbl_status.setWordWrap(True)
        v_extent.addWidget(self.lbl_status)

        grp_extent.setLayout(v_extent)
        root.addWidget(grp_extent)

        # --- 実行ボタン ---
        h_run = QHBoxLayout()
        self.btn_export = QPushButton("出力実行")
        self.btn_close = QPushButton("閉じる")
        h_run.addStretch()
        h_run.addWidget(self.btn_export)
        h_run.addWidget(self.btn_close)
        root.addLayout(h_run)

    def _connect_signals(self):
        self.cmb_paper.currentIndexChanged.connect(self._on_paper_changed)
        self.rb_portrait.toggled.connect(self._on_orientation_changed)
        self.chk_auto_scale.toggled.connect(lambda checked: self.spn_scale.setEnabled(not checked))

        self.btn_pick_canvas_extent.clicked.connect(
            lambda: self._set_fields_from_extent(self.canvas.extent())
        )
        self.btn_pick_full_extent.clicked.connect(self._use_full_layer_extent)
        self.btn_drag_extent.clicked.connect(self._start_drag_extent)
        self.btn_recompute.clicked.connect(self._recompute)
        self.btn_export.clicked.connect(self._do_export)
        self.btn_close.clicked.connect(self.close)

    # ---------------- 用紙サイズ処理 ----------------

    def _on_paper_changed(self):
        name = self.cmb_paper.currentText()
        w, h = PAPER_SIZES_MM[name]
        is_custom = name == "カスタム"
        self.spn_paper_w.setEnabled(is_custom)
        self.spn_paper_h.setEnabled(is_custom)
        if not is_custom:
            self._apply_orientation(w, h)
        else:
            self.spn_paper_w.setValue(w)
            self.spn_paper_h.setValue(h)

    def _on_orientation_changed(self):
        name = self.cmb_paper.currentText()
        if name == "カスタム":
            return
        w, h = PAPER_SIZES_MM[name]
        self._apply_orientation(w, h)

    def _apply_orientation(self, w_portrait, h_portrait):
        if self.rb_portrait.isChecked():
            self.spn_paper_w.setValue(w_portrait)
            self.spn_paper_h.setValue(h_portrait)
        else:
            self.spn_paper_w.setValue(h_portrait)
            self.spn_paper_h.setValue(w_portrait)

    # ---------------- 座標フィールド ----------------

    def _set_fields_from_extent(self, rect):
        self.spn_xmin.setValue(rect.xMinimum())
        self.spn_ymin.setValue(rect.yMinimum())
        self.spn_xmax.setValue(rect.xMaximum())
        self.spn_ymax.setValue(rect.yMaximum())

    def _get_extent_from_fields(self):
        return QgsRectangle(
            self.spn_xmin.value(),
            self.spn_ymin.value(),
            self.spn_xmax.value(),
            self.spn_ymax.value(),
        )

    def _use_full_layer_extent(self):
        project = QgsProject.instance()
        project_crs = project.crs()
        combined = QgsRectangle()
        combined.setMinimal()
        for layer in project.mapLayers().values():
            try:
                ext = layer.extent()
            except AttributeError:
                continue
            if ext.isNull() or ext.isEmpty():
                continue
            if layer.crs() != project_crs:
                transform = QgsCoordinateTransform(layer.crs(), project_crs, project)
                try:
                    ext = transform.transformBoundingBox(ext)
                except Exception:
                    continue
            combined.combineExtentWith(ext)
        if combined.isNull() or combined.isEmpty():
            QMessageBox.warning(self, "範囲取得エラー", "有効なレイヤの範囲が取得できませんでした。")
            return
        self._set_fields_from_extent(combined)

    def _start_drag_extent(self):
        self._prev_map_tool = self.canvas.mapTool()
        self._extent_tool = QgsMapToolExtent(self.canvas)
        self._extent_tool.extentChanged.connect(self._on_canvas_extent_picked)
        self.canvas.setMapTool(self._extent_tool)
        self.lbl_status.setText("キャンバス上でドラッグして範囲を指定してください。")

    def _on_canvas_extent_picked(self, rect):
        self._set_fields_from_extent(rect)
        self.canvas.setMapTool(self._prev_map_tool)
        self._extent_tool = None
        self.lbl_status.setText("範囲を取得しました。「範囲・縮尺を計算」で用紙比率に合わせてください。")

    # ---------------- レイアウト構築 ----------------

    def _build_layout_and_map(self, extent, add_annotations=False):
        layout = QgsPrintLayout(QgsProject.instance())
        layout.initializeDefaults()
        w_mm = self.spn_paper_w.value()
        h_mm = self.spn_paper_h.value()
        page = layout.pageCollection().page(0)
        page.setPageSize(QgsLayoutSize(w_mm, h_mm, QgsUnitTypes.LayoutMillimeters))

        map_item = QgsLayoutItemMap(layout)
        map_item.attemptMove(QgsLayoutPoint(0, 0, QgsUnitTypes.LayoutMillimeters))
        map_item.attemptResize(QgsLayoutSize(w_mm, h_mm, QgsUnitTypes.LayoutMillimeters))
        map_item.setCrs(self.canvas.mapSettings().destinationCrs())
        layout.addLayoutItem(map_item)

        map_item.setExtent(extent)  # 用紙アスペクト比に合わせて自動調整される
        if self.spn_rotation.value():
            map_item.setMapRotation(self.spn_rotation.value())

        if not self.chk_auto_scale.isChecked():
            map_item.setScale(self.spn_scale.value())

        if add_annotations:
            self._add_coordinate_label(layout, map_item, w_mm, h_mm)
            self._add_scale_bar(layout, map_item, w_mm, h_mm)

        return layout, map_item

    def _add_coordinate_label(self, layout, map_item, w_mm, h_mm):
        """右上座標・左下座標・縮尺を示すラベルを用紙下部に追加する"""
        ext = map_item.extent()
        scale = map_item.scale()
        text = (
            "右上座標: X={0:.0f}m  Y={1:.0f}m\n"
            "左下座標: X={2:.0f}m  Y={3:.0f}m\n"
            "縮尺: 1:{4:.0f}".format(
                ext.xMaximum(), ext.yMaximum(),
                ext.xMinimum(), ext.yMinimum(),
                scale,
            )
        )
        label = QgsLayoutItemLabel(layout)
        label.setText(text)
        font = label.font()
        font.setPointSize(8)
        label.setFont(font)

        label_w, label_h = 85.0, 16.0
        margin_bottom, gap, scale_bar_h = 5.0, 2.0, 8.0
        x = 5.0
        y = h_mm - margin_bottom - scale_bar_h - gap - label_h

        layout.addLayoutItem(label)
        label.attemptMove(QgsLayoutPoint(x, y, QgsUnitTypes.LayoutMillimeters))
        label.attemptResize(QgsLayoutSize(label_w, label_h, QgsUnitTypes.LayoutMillimeters))
        return label

    def _add_scale_bar(self, layout, map_item, w_mm, h_mm):
        """ラベル(縮尺表記)のすぐ下にスケールバーを追加する"""
        scalebar = QgsLayoutItemScaleBar(layout)
        scalebar.setLinkedMap(map_item)
        scalebar.setStyle("Single Box")
        scalebar.setUnits(QgsUnitTypes.DistanceMeters)
        scalebar.setUnitLabel("m")

        segments = 4
        extent_width = map_item.extent().width()
        units_per_segment = _nice_number(extent_width / segments) if extent_width > 0 else 100
        scalebar.setNumberOfSegments(segments)
        scalebar.setNumberOfSegmentsLeft(0)
        scalebar.setUnitsPerSegment(units_per_segment)

        label_w, label_h = 85.0, 16.0
        margin_bottom, gap, scale_bar_h = 5.0, 2.0, 8.0
        x = 5.0
        label_y = h_mm - margin_bottom - scale_bar_h - gap - label_h
        y = label_y + label_h + gap

        layout.addLayoutItem(scalebar)
        scalebar.attemptMove(QgsLayoutPoint(x, y, QgsUnitTypes.LayoutMillimeters))
        scalebar.attemptResize(QgsLayoutSize(label_w, scale_bar_h, QgsUnitTypes.LayoutMillimeters))
        scalebar.update()
        return scalebar

    def _recompute(self):
        extent = self._get_extent_from_fields()
        if extent.isEmpty():
            QMessageBox.warning(self, "範囲エラー", "左下座標・右上座標を正しく入力してください。")
            return
        layout, map_item = self._build_layout_and_map(extent)

        final_extent = map_item.extent()
        final_scale = map_item.scale()

        self._set_fields_from_extent(final_extent)
        if self.chk_auto_scale.isChecked():
            self.spn_scale.blockSignals(True)
            self.spn_scale.setValue(round(final_scale))
            self.spn_scale.blockSignals(False)

        self.lbl_status.setText(
            "計算結果 — 縮尺: 1/{0}  幅: {1:.1f}m  高さ: {2:.1f}m".format(
                round(final_scale),
                final_extent.width(),
                final_extent.height(),
            )
        )

    # ---------------- 出力 ----------------

    def _do_export(self):
        extent = self._get_extent_from_fields()
        if extent.isEmpty():
            QMessageBox.warning(self, "範囲エラー", "左下座標・右上座標を正しく入力してください。")
            return

        fmt = self.cmb_format.currentText()
        filters = {"PDF": "PDFファイル (*.pdf)", "PNG": "PNG画像 (*.png)"}
        default_ext = {"PDF": ".pdf", "PNG": ".png"}
        path, _ = QFileDialog.getSaveFileName(self, "出力先を選択", "", filters[fmt])
        if not path:
            return
        if not path.lower().endswith(default_ext[fmt]):
            path += default_ext[fmt]

        layout, map_item = self._build_layout_and_map(extent, add_annotations=True)
        exporter = QgsLayoutExporter(layout)

        if fmt == "PDF":
            settings = QgsLayoutExporter.PdfExportSettings()
            result = exporter.exportToPdf(path, settings)
        else:
            settings = QgsLayoutExporter.ImageExportSettings()
            settings.dpi = self.spn_dpi.value()
            result = exporter.exportToImage(path, settings)

        if result == QgsLayoutExporter.Success:
            self.lbl_status.setText(f"出力しました: {path}")
            QMessageBox.information(self, "出力完了", f"出力しました:\n{path}")
        else:
            QMessageBox.critical(self, "出力エラー", f"出力に失敗しました(コード: {result})")

    def closeEvent(self, event):
        if self._extent_tool is not None:
            self.canvas.setMapTool(self._prev_map_tool)
            self._extent_tool = None
        super().closeEvent(event)
