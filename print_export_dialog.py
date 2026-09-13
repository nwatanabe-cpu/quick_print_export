import os
from qgis.PyQt.QtCore import QRectF
from qgis.PyQt.QtGui import QColor
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
    QgsCoordinateReferenceSystem,
    QgsPointXY,
)
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

# 座標・縮尺ラベルの外観(サイズは adjustSizeToText() で文字量に応じて自動決定)
BASE_FONT_SIZE_PT = 8
COORD_FONT_SIZE_PT = BASE_FONT_SIZE_PT + 4  # 「2段階上げる」= 2pt刻みで2段階
LABEL_BG_COLOR = QColor(255, 255, 255, 220)  # 文字バッファ代わりの半透明白背景
LABEL_MARGIN_MM = 3.0    # 用紙端からラベルまでの余白
LATLON_DECIMALS = 6      # 緯度経度の小数桁数(6桁で約10cm精度)

# スケールバー(縮尺ラベルの直下、右下寄せ)
# 印刷上の物理サイズは「区間数 × 1区間の実距離 ÷ 縮尺」で決まる仕組みのため、
# ここでは "紙の上で何mmにしたいか" から逆算して1区間の実距離を求める。
SCALE_BAR_BASE_TOTAL_WIDTH_MM = 85.0
SCALE_BAR_SHRINK = 1.0 / 8.0
SCALE_BAR_TARGET_WIDTH_MM = SCALE_BAR_BASE_TOTAL_WIDTH_MM * SCALE_BAR_SHRINK
SCALE_BAR_SEGMENTS = 2
SCALE_BAR_BOX_HEIGHT_MM = 1.5   # 目盛りボックス自体の高さ(文字ラベル分は別途自動で足される)
SCALE_BAR_FONT_PT = 6
SCALE_BAR_RESERVED_HEIGHT_MM = 7.0  # レイアウト上でスケールバーに確保しておく縦スペースの目安


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
        self._preview_layout_name = "座標指定プレビュー"

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
        self.btn_preview = QPushButton("プレビュー")
        self.btn_export = QPushButton("出力実行")
        self.btn_close = QPushButton("閉じる")
        h_run.addStretch()
        h_run.addWidget(self.btn_preview)
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
        self.btn_preview.clicked.connect(self._do_preview)
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
            self._add_top_right_label(layout, map_item, w_mm, h_mm)
            self._add_bottom_left_label(layout, map_item, w_mm, h_mm)
            self._add_scale_label_and_bar(layout, map_item, w_mm, h_mm)

        return layout, map_item

    def _to_latlon(self, x, y):
        """プロジェクトCRSの座標(x, y)をEPSG:4326の(経度, 緯度)に変換する"""
        src_crs = self.canvas.mapSettings().destinationCrs()
        dst_crs = QgsCoordinateReferenceSystem("EPSG:4326")
        transform = QgsCoordinateTransform(src_crs, dst_crs, QgsProject.instance())
        pt = transform.transform(QgsPointXY(x, y))
        return pt.x(), pt.y()  # 経度, 緯度

    def _make_corner_label(self, layout, text, corner, w_mm, h_mm, y_top=None):
        """バッファ(半透明白背景)付きの角ラベルを1個作成し、文字サイズに合わせて
        自動リサイズしたうえで指定の角に配置する。戻り値は (label, 幅mm, 高さmm)。
        """
        label = QgsLayoutItemLabel(layout)
        label.setText(text)
        font = label.font()
        font.setPointSize(COORD_FONT_SIZE_PT)
        font.setBold(True)
        label.setFont(font)

        # 文字バッファの代わりに半透明の背景ボックスを敷く
        label.setBackgroundEnabled(True)
        label.setBackgroundColor(LABEL_BG_COLOR)
        label.setMarginX(2.0)
        label.setMarginY(1.5)

        layout.addLayoutItem(label)
        label.adjustSizeToText()  # 文字量に合わせてボックスサイズを自動決定

        size = label.sizeWithUnits()
        label_w = size.width()
        label_h = size.height()

        if corner == "top-right":
            x = w_mm - label_w - LABEL_MARGIN_MM
            y = LABEL_MARGIN_MM
        elif corner == "bottom-left":
            x = LABEL_MARGIN_MM
            y = h_mm - label_h - LABEL_MARGIN_MM
        elif corner == "bottom-right":
            x = w_mm - label_w - LABEL_MARGIN_MM
            y = y_top if y_top is not None else (h_mm - label_h - LABEL_MARGIN_MM)
        else:
            raise ValueError(f"unknown corner: {corner}")

        label.attemptMove(QgsLayoutPoint(x, y, QgsUnitTypes.LayoutMillimeters))
        return label, label_w, label_h

    def _add_top_right_label(self, layout, map_item, w_mm, h_mm):
        """右上座標ラベル(緯度経度)を図面右上隅に配置"""
        ext = map_item.extent()
        lon, lat = self._to_latlon(ext.xMaximum(), ext.yMaximum())
        text = "経度={0:.{2}f}°  緯度={1:.{2}f}°".format(lon, lat, LATLON_DECIMALS)
        return self._make_corner_label(layout, text, "top-right", w_mm, h_mm)

    def _add_bottom_left_label(self, layout, map_item, w_mm, h_mm):
        """左下座標ラベル(緯度経度)を図面左下隅に配置"""
        ext = map_item.extent()
        lon, lat = self._to_latlon(ext.xMinimum(), ext.yMinimum())
        text = "経度={0:.{2}f}°  緯度={1:.{2}f}°".format(lon, lat, LATLON_DECIMALS)
        return self._make_corner_label(layout, text, "bottom-left", w_mm, h_mm)

    def _add_scale_label_and_bar(self, layout, map_item, w_mm, h_mm):
        """縮尺ラベルを図面右下隅に配置し、その直下にスケールバーを配置する"""
        scale = map_item.scale()
        text = "縮尺 1:{0:.0f}".format(scale)
        # スケールバー分の縦スペースをあらかじめ下に確保しておく
        y_top = h_mm - LABEL_MARGIN_MM - SCALE_BAR_RESERVED_HEIGHT_MM
        # ラベル自身の高さは adjustSizeToText 後でないと分からないため、
        # いったん確保領域の上端を仮のy座標として渡す
        label, label_w, label_h = self._make_corner_label(
            layout, text, "bottom-right", w_mm, h_mm, y_top=y_top
        )
        # ラベルの下端を基準にスケールバーを配置
        label_bottom = y_top + label_h
        self._add_scale_bar(layout, map_item, w_mm, label_x=w_mm - LABEL_MARGIN_MM, y=label_bottom + 1.0)

    def _add_scale_bar(self, layout, map_item, w_mm, label_x, y):
        """指定位置にスケールバーを配置する(右端がlabel_xに揃うよう右詰め)。
        印刷上の物理幅は「区間数 × 1区間の実距離 ÷ 縮尺」で決まるため、
        目標のmm幅から逆算して1区間あたりの実距離(m)を求める。
        """
        scalebar = QgsLayoutItemScaleBar(layout)
        layout.addLayoutItem(scalebar)
        scalebar.setLinkedMap(map_item)
        scalebar.setStyle("Single Box")
        scalebar.setUnits(QgsUnitTypes.DistanceMeters)
        scalebar.setUnitLabel("m")
        scalebar.setNumberOfSegments(SCALE_BAR_SEGMENTS)
        scalebar.setNumberOfSegmentsLeft(0)
        scalebar.setHeight(SCALE_BAR_BOX_HEIGHT_MM)

        text_format = scalebar.textFormat()
        text_format.setSize(SCALE_BAR_FONT_PT)
        scalebar.setTextFormat(text_format)

        scale_denom = map_item.scale()
        units_per_segment = (SCALE_BAR_TARGET_WIDTH_MM * scale_denom) / (1000.0 * SCALE_BAR_SEGMENTS)
        scalebar.setUnitsPerSegment(units_per_segment)

        # 右詰めにするため、実際の幅が確定してから位置を合わせ直す
        scalebar.attemptMove(QgsLayoutPoint(0, y, QgsUnitTypes.LayoutMillimeters))
        actual_w = scalebar.sizeWithUnits().width()
        x = label_x - actual_w
        scalebar.attemptMove(QgsLayoutPoint(x, y, QgsUnitTypes.LayoutMillimeters))
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

    # ---------------- プレビュー ----------------

    def _do_preview(self):
        extent = self._get_extent_from_fields()
        if extent.isEmpty():
            QMessageBox.warning(self, "範囲エラー", "左下座標・右上座標を正しく入力してください。")
            return

        layout, map_item = self._build_layout_and_map(extent, add_annotations=True)
        layout.setName(self._preview_layout_name)

        manager = QgsProject.instance().layoutManager()
        # 前回のプレビューが残っていれば置き換える(実行のたびに増殖させない)
        existing = manager.layoutByName(self._preview_layout_name)
        if existing is not None:
            manager.removeLayout(existing)
        manager.addLayout(layout)

        self.iface.openLayoutDesigner(layout)
        self.lbl_status.setText(
            "プレビューを開きました(レイアウトデザイナー: 「{0}」)。".format(self._preview_layout_name)
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
