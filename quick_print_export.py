import os
from qgis.PyQt.QtWidgets import QAction
from qgis.PyQt.QtGui import QIcon


class QuickPrintExportPlugin:
    """レイアウトマネージャーを使わず、範囲座標・縮尺・回転角を直接指定して
    現在のキャンバスをPDF/PNG出力するプラグイン。
    """

    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.dialog = None

    def initGui(self):
        icon_path = os.path.join(os.path.dirname(__file__), "icon.png")
        icon = QIcon(icon_path) if os.path.exists(icon_path) else QIcon()
        self.action = QAction(icon, "座標指定 印刷/エクスポート", self.iface.mainWindow())
        self.action.triggered.connect(self.run)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu("&座標指定印刷", self.action)

    def unload(self):
        self.iface.removePluginMenu("&座標指定印刷", self.action)
        self.iface.removeToolBarIcon(self.action)
        if self.dialog is not None:
            self.dialog.close()
            self.dialog = None

    def run(self):
        from .print_export_dialog import PrintExportDialog
        if self.dialog is None:
            self.dialog = PrintExportDialog(self.iface)
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()
