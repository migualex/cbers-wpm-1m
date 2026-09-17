# -*- coding: utf-8 -*-
# Created by Miguel Alexandre da Cunha
import os
import math
from datetime import datetime

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsSettings,
    QgsWkbTypes,
    QgsVectorFileWriter,
    QgsCoordinateTransformContext,
)
from qgis.core import QgsRasterLayer, QgsProject
from qgis.gui import QgsFileWidget
from qgis.PyQt import QtCore
from qgis.PyQt.QtCore import Qt, QDate, QSize, QThread, pyqtSignal, QTimer
from qgis.PyQt.QtGui import QIcon, QPixmap, QPalette
from qgis.PyQt.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QPushButton,
    QTabWidget,
    QWidget,
    QRadioButton,
    QButtonGroup,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QDateEdit,
    QSpinBox,
    QDoubleSpinBox,
    QComboBox,
    QCheckBox,
    QPlainTextEdit,
    QProgressBar,
    QMessageBox,
    QSizePolicy,
    QFrame,
    QScrollArea,
    QAbstractItemView,
    QApplication,
)

from .tasks import CbersWpmTask, CbersWpmSearchTask
from .core.pipeline import fetch_thumbnail_bytes, check_entire_tile_resources, PipelineError

FIXED_THREADS = 1
FIXED_CONTRAST_STRETCH = 800
FIXED_SEARCH_WINDOW_DAYS = 60

POINT_BUFFER_MIN_KM = 10
POINT_BUFFER_MAX_KM = 40

class ThumbnailWorker(QThread):
    """Baixa uma única miniatura (PNG) em background, sob demanda (ao selecionar
    uma cena na lista de resultados), sem travar a interface."""
    thumbnail_ready = pyqtSignal(QPixmap)
    failed = pyqtSignal(str)

    def __init__(self, url, parent=None):
        super().__init__(parent)
        self.url = url

    def run(self):
        try:
            data = fetch_thumbnail_bytes(self.url)
            pixmap = QPixmap()
            if not pixmap.loadFromData(data):
                self.failed.emit("Não foi possível decodificar a miniatura")
                return
            self.thumbnail_ready.emit(pixmap)
        except Exception as exc:
            self.failed.emit(str(exc)[:120])

PLUGIN_DIR = os.path.dirname(__file__)

STYLE_SHEET = """
QDialog {
    background-color: #fcfcfd;
    font-size: 12px;
}
QTabWidget::pane {
    border: none;
    border-top: 1px solid #edeef1;
    background: #fcfcfd;
}
QScrollArea#roiScroll {
    background: #fcfcfd;
    border: none;
}
QScrollArea#roiScroll > QWidget > QWidget {
    background: #fcfcfd;
}
QTabBar {
    qproperty-drawBase: 0;
}
QTabBar::tab {
    background: transparent;
    color: #9a9fa6;
    padding: 6px 10px;
    margin-right: 4px;
    border: none;
    border-bottom: 2px solid transparent;
    font-weight: 600;
    min-height: 18px;
    min-width: 100px;
}
QTabBar::tab:hover {
    color: #55595f;
}
QTabBar::tab:selected {
    color: #2c2f33;
    border-bottom: 2px solid #6fb897;
}
QGroupBox {
    font-weight: 600;
    color: #2c2f33;
    border: none;
    border-top: 1px solid #eef0f2;
    border-radius: 0px;
    margin-top: 10px;
    padding: 10px 2px 2px 2px;
    background-color: transparent;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 0px;
    top: 6px;
    padding: 0 2px;
    color: #4f9c7c;
    font-size: 11px;
    letter-spacing: 0.5px;
}
QLabel {
    color: #3c4046;
}
QLabel[hint="true"] {
    color: #a6abb2;
    font-size: 10.5px;
}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QDateEdit, QPlainTextEdit, QTableWidget {
    border: 1px solid #e6e8eb;
    border-radius: 6px;
    padding: 5px 8px;
    background: #ffffff;
    color: #2c2f33;
    selection-background-color: #cdeade;
    selection-color: #1f5c42;
}
QSpinBox:disabled, QDoubleSpinBox:disabled {
    background: #f5f6f7;
    color: #b7bbc0;
    border: 1px solid #edeef1;
}
QLineEdit:hover, QSpinBox:hover, QDoubleSpinBox:hover, QComboBox:hover, QDateEdit:hover {
    border: 1px solid #d7dbdf;
}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus, QDateEdit:focus {
    border: 1px solid #8fcdae;
    background: #ffffff;
}
QTableWidget {
    gridline-color: #f0f1f3;
}
QHeaderView::section {
    background-color: #fafbfb;
    color: #9a9fa6;
    border: none;
    border-bottom: 1px solid #edeef1;
    padding: 6px;
    font-weight: 600;
    font-size: 10.5px;
}
QComboBox::drop-down, QDateEdit::drop-down {
    border: none;
    width: 20px;
}
QPushButton {
    background-color: #ffffff;
    border: 1px solid #e2e5e8;
    border-radius: 6px;
    padding: 7px 14px;
    color: #4a4f56;
    font-weight: 500;
}
QPushButton:hover {
    background-color: #f5f6f7;
    border: 1px solid #d7dbdf;
}
QPushButton#runButton {
    background-color: #6fb897;
    color: #ffffff;
    font-weight: 600;
    border: none;
    padding: 5px 14px;
    font-size: 11px;
    border-radius: 6px;
}
QPushButton#runButton:hover {
    background-color: #5fa686;
}
QPushButton#runButton:disabled {
    background-color: #d7ebe1;
    color: #ffffff;
}
QPushButton#cancelButton {
    background-color: #ffffff;
    color: #d98a7d;
    border: 1px solid #f0d9d4;
    padding: 5px 12px;
    border-radius: 6px;
    font-weight: 600;
    font-size: 11px;
}
QPushButton#cancelButton:hover {
    background-color: #fdf3f1;
}
QPushButton#cancelButton:disabled {
    background-color: #ffffff;
    color: #e5cec8;
    border: 1px solid #f5eae7;
}
QPushButton#drawButton {
    background-color: #eef4fc;
    color: #5b84d6;
    border: 1px solid #dde8f9;
    font-weight: 600;
}
QPushButton#drawButton:hover {
    background-color: #e2ecfb;
}
QProgressBar {
    border: none;
    border-radius: 3px;
    background: #eef0f2;
    height: 6px;
    text-align: center;
    color: transparent;
}
QProgressBar::chunk {
    background-color: #6fb897;
    border-radius: 3px;
}
QPlainTextEdit#logBox {
    background-color: #fafbfb;
    color: #4a4f56;
    font-family: Consolas, "Courier New", monospace;
    font-size: 10.5px;
    border: 1px solid #edeef1;
    border-radius: 6px;
}
QRadioButton {
    color: #3c4046;
    font-weight: 500;
    padding: 4px;
    spacing: 8px;
}
QCheckBox {
    color: #3c4046;
    spacing: 8px;
}
QCheckBox:disabled, QLabel:disabled {
    color: #b7bbc0;
}
QFrame#headerFrame {
    background-color: transparent;
    border-bottom: 1px solid #eef0f2;
}
QLabel#headerTitle {
    color: #2c2f33;
    font-size: 17px;
    font-weight: 700;
}
QLabel#headerSubtitle {
    color: #9a9fa6;
    font-size: 11px;
}
"""

DARK_STYLE_SHEET = """
QDialog {
    background-color: #202124;
    font-size: 12px;
}
QTabWidget::pane {
    border: none;
    border-top: 1px solid #35373c;
    background: #202124;
}
QScrollArea#roiScroll {
    background: #202124;
    border: none;
}
QScrollArea#roiScroll > QWidget > QWidget {
    background: #202124;
}
QTabBar {
    qproperty-drawBase: 0;
}
QTabBar::tab {
    background: transparent;
    color: #85898f;
    padding: 6px 10px;
    margin-right: 4px;
    border: none;
    border-bottom: 2px solid transparent;
    font-weight: 600;
    min-height: 18px;
    min-width: 100px;
}
QTabBar::tab:hover {
    color: #d5d7db;
}
QTabBar::tab:selected {
    color: #f0f1f3;
    border-bottom: 2px solid #6fb897;
}
QGroupBox {
    font-weight: 600;
    color: #e4e6eb;
    border: none;
    border-top: 1px solid #35373c;
    border-radius: 0px;
    margin-top: 10px;
    padding: 10px 2px 2px 2px;
    background-color: transparent;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 0px;
    top: 6px;
    padding: 0 2px;
    color: #7fcba6;
    font-size: 11px;
    letter-spacing: 0.5px;
}
QLabel {
    color: #c7c9cd;
}
QLabel[hint="true"] {
    color: #75797f;
    font-size: 10.5px;
}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QDateEdit, QPlainTextEdit, QTableWidget {
    border: 1px solid #3a3d42;
    border-radius: 6px;
    padding: 5px 8px;
    background: #2b2d31;
    color: #e4e6eb;
    selection-background-color: #2f5d47;
    selection-color: #d7ffe9;
}
QSpinBox:disabled, QDoubleSpinBox:disabled {
    background: #26282c;
    color: #6b6f75;
    border: 1px solid #35373c;
}
QLineEdit:hover, QSpinBox:hover, QDoubleSpinBox:hover, QComboBox:hover, QDateEdit:hover {
    border: 1px solid #4a4d52;
}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus, QDateEdit:focus {
    border: 1px solid #4f9c7c;
    background: #2b2d31;
}
QTableWidget {
    gridline-color: #35373c;
    color: #e4e6eb;
}
QHeaderView::section {
    background-color: #26282c;
    color: #85898f;
    border: none;
    border-bottom: 1px solid #35373c;
    padding: 6px;
    font-weight: 600;
    font-size: 10.5px;
}
QComboBox::drop-down, QDateEdit::drop-down {
    border: none;
    width: 20px;
}
QPushButton {
    background-color: #2b2d31;
    border: 1px solid #3a3d42;
    border-radius: 6px;
    padding: 7px 14px;
    color: #d5d7db;
    font-weight: 500;
}
QPushButton:hover {
    background-color: #35373c;
    border: 1px solid #4a4d52;
}
QPushButton#runButton {
    background-color: #4f9c7c;
    color: #ffffff;
    font-weight: 600;
    border: none;
    padding: 5px 14px;
    font-size: 11px;
    border-radius: 6px;
}
QPushButton#runButton:hover {
    background-color: #438a6d;
}
QPushButton#runButton:disabled {
    background-color: #2c4339;
    color: #7fa693;
}
QPushButton#cancelButton {
    background-color: #2b2d31;
    color: #e0998a;
    border: 1px solid #4a3a37;
    padding: 5px 12px;
    border-radius: 6px;
    font-weight: 600;
    font-size: 11px;
}
QPushButton#cancelButton:hover {
    background-color: #35302f;
}
QPushButton#cancelButton:disabled {
    background-color: #2b2d31;
    color: #6b5652;
    border: 1px solid #3a3230;
}
QPushButton#drawButton {
    background-color: #223049;
    color: #8fb1ef;
    border: 1px solid #2d3e5c;
    font-weight: 600;
}
QPushButton#drawButton:hover {
    background-color: #283a5a;
}
QProgressBar {
    border: none;
    border-radius: 3px;
    background: #35373c;
    height: 6px;
    text-align: center;
    color: transparent;
}
QProgressBar::chunk {
    background-color: #6fb897;
    border-radius: 3px;
}
QPlainTextEdit#logBox {
    background-color: #1a1b1e;
    color: #c7c9cd;
    font-family: Consolas, "Courier New", monospace;
    font-size: 10.5px;
    border: 1px solid #35373c;
    border-radius: 6px;
}
QRadioButton {
    color: #c7c9cd;
    font-weight: 500;
    padding: 4px;
    spacing: 8px;
}
QCheckBox {
    color: #c7c9cd;
    spacing: 8px;
}
QCheckBox:disabled, QLabel:disabled {
    color: #6b6f75;
}
QFrame#headerFrame {
    background-color: transparent;
    border-bottom: 1px solid #35373c;
}
QLabel#headerTitle {
    color: #f0f1f3;
    font-size: 17px;
    font-weight: 700;
}
QLabel#headerSubtitle {
    color: #85898f;
    font-size: 11px;
}
"""

class CbersWpmDialog(QDialog):

    def _is_dark_theme(self):
        """Detecta se o QGIS está em tema escuro olhando a luminosidade da cor
        de fundo padrão da janela (herdada do tema ativo do QGIS/SO), em vez
        de assumir um tema fixo - assim acompanha automaticamente a troca de
        tema do QGIS sem precisar de um botão manual."""
        color = self.palette().color(QPalette.Window)
        luminance = 0.299 * color.red() + 0.587 * color.green() + 0.114 * color.blue()
        return luminance < 128

    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.canvas = iface.mapCanvas() if iface else None
        self.task = None
        self.search_task = None
        self.available_scenes = {}
        self._thumb_worker = None
        self.pending_runs = []
        self._run_index = 0
        self._all_outputs = []
        self._run_cancelled = False

        self.setWindowTitle("WPM 1-meter spatial resolution")
        icon_path = os.path.join(PLUGIN_DIR, "icons", "icon.png")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))
        self.resize(760, 660)
        self.setMinimumSize(700, 600)
        self.setStyleSheet(DARK_STYLE_SHEET if self._is_dark_theme() else STYLE_SHEET)

        self._build_ui()
        self._load_settings()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(8)
        root.setContentsMargins(14, 10, 14, 10)

        header = QFrame()
        header.setObjectName("headerFrame")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(2, 2, 2, 8)
        header_layout.setSpacing(10)

        icon_label = QLabel()
        icon_path = os.path.join(PLUGIN_DIR, "icons", "icon.png")
        icon_pixmap = QIcon(icon_path).pixmap(64, 64)
        if not icon_pixmap.isNull():
            icon_label.setPixmap(icon_pixmap)
        icon_label.setFixedSize(64, 64)
        icon_label.setScaledContents(True)
        header_layout.addWidget(icon_label, alignment=Qt.AlignTop)

        text_layout = QVBoxLayout()
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(2)
        title = QLabel("WPM 1-meter spatial resolution")
        title.setObjectName("headerTitle")
        text_layout.addWidget(title)
        header_layout.addLayout(text_layout, stretch=1)

        root.addWidget(header)

        self.tabs = QTabWidget()
        self.tabs.tabBar().setMinimumHeight(30)
        root.addWidget(self.tabs, stretch=1)

        roi_scroll = QScrollArea()
        roi_scroll.setObjectName("roiScroll")
        roi_scroll.setWidgetResizable(True)
        roi_scroll.setFrameShape(QFrame.NoFrame)
        roi_tab_content = self._build_roi_tab()
        roi_scroll.setWidget(roi_tab_content)
        self.tabs.addTab(roi_scroll, "ROI")
        self.tabs.addTab(self._build_search_tab(), "Processamento")
        self.tabs.addTab(self._build_output_tab(), "Produto Final")
        self.tabs.addTab(self._build_log_tab(), "Execução")

        footer = QHBoxLayout()
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setVisible(False)
        footer.addWidget(self.progress_bar, stretch=1)

        self.cancel_button = QPushButton("Cancelar")
        self.cancel_button.setObjectName("cancelButton")
        self.cancel_button.setEnabled(False)
        self.cancel_button.setMaximumHeight(28)
        self.cancel_button.setMaximumWidth(90)
        self.cancel_button.clicked.connect(self.cancel_processing)
        footer.addWidget(self.cancel_button)

        self.run_button = QPushButton("▶  Executar processamento")
        self.run_button.setObjectName("runButton")
        self.run_button.setMaximumHeight(28)
        self.run_button.setMaximumWidth(190)
        self.run_button.clicked.connect(self.run_processing)
        footer.addWidget(self.run_button)

        root.addLayout(footer)

    def _build_roi_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(2, 2, 2, 2)

        group = QGroupBox("Área de Interesse (ROI)")
        group.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        glayout = QVBoxLayout(group)

        self.roi_source_group = QButtonGroup(self)
        self.radio_roi_file = QRadioButton("Arquivo vetorial (Shapefile / GeoPackage)")
        self.radio_roi_single_coord = QRadioButton("Coordenada (Lat / Long)")

        for rb in (self.radio_roi_file, self.radio_roi_single_coord):
            rb.setStyleSheet("QRadioButton { font-weight: normal; }")
        self.radio_roi_file.setChecked(True)
        for i, rb in enumerate([self.radio_roi_file, self.radio_roi_single_coord]):
            self.roi_source_group.addButton(rb, i)
            glayout.addWidget(rb)

        self.roi_stack = QStackedWidget()
        self.roi_stack.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        glayout.addWidget(self.roi_stack)

        file_page = QWidget()
        file_layout = QFormLayout(file_page)
        file_layout.setContentsMargins(0, 0, 0, 0)
        self.roi_file_widget = QgsFileWidget()
        self.roi_file_widget.setStorageMode(QgsFileWidget.GetFile)
        self.roi_file_widget.setFilter("Vetores (*.gpkg *.shp);;GeoPackage (*.gpkg);;Shapefile (*.shp)")
        file_layout.addRow("Arquivo do ROI:", self.roi_file_widget)
        self.roi_stack.addWidget(file_page)

        single_coord_page = QWidget()
        single_coord_layout = QFormLayout(single_coord_page)
        single_coord_layout.setContentsMargins(0, 0, 0, 0)

        self.single_lat_edit = QLineEdit()
        self.single_lat_edit.setPlaceholderText("-15.7942")
        self.single_lat_edit.setMaximumWidth(120)
        single_coord_layout.addRow("Latitude:", self.single_lat_edit)

        self.single_lon_edit = QLineEdit()
        self.single_lon_edit.setPlaceholderText("-47.8825")
        self.single_lon_edit.setMaximumWidth(120)
        single_coord_layout.addRow("Longitude:", self.single_lon_edit)

        self.buffer_distance_spin = QSpinBox()
        self.buffer_distance_spin.setRange(POINT_BUFFER_MIN_KM, POINT_BUFFER_MAX_KM)
        self.buffer_distance_spin.setSingleStep(1)
        self.buffer_distance_spin.setValue(POINT_BUFFER_MIN_KM)
        self.buffer_distance_spin.setSuffix(" km")
        self.buffer_distance_spin.setMaximumWidth(120)
        single_coord_layout.addRow("Tamanho do ROI:", self.buffer_distance_spin)

        self.roi_stack.addWidget(single_coord_page)

        layout.addWidget(group)

        self.extent_group = QGroupBox("Extensão do Processamento")
        extent_layout = QVBoxLayout(self.extent_group)

        self.process_entire_tile_check = QCheckBox(
            "Processar o tile inteiro")
        extent_layout.addWidget(self.process_entire_tile_check)

        entire_tile_hint = QLabel(
            "Atenção: exige mais memória RAM e espaço de armazenamento em disco.")
        entire_tile_hint.setProperty("hint", "true")
        entire_tile_hint.setWordWrap(True)
        extent_layout.addWidget(entire_tile_hint)

        layout.addWidget(self.extent_group)

        self.radio_roi_file.toggled.connect(lambda c: c and self.roi_stack.setCurrentIndex(0))
        self.radio_roi_single_coord.toggled.connect(lambda c: c and self.roi_stack.setCurrentIndex(1))

        layout.addStretch(1)

        return widget

    def _build_search_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)

        search_group = QGroupBox("Busca de Cenas Disponíveis")
        search_form = QFormLayout(search_group)

        self.start_date_edit = QDateEdit()
        self.start_date_edit.setCalendarPopup(True)
        self.start_date_edit.setDisplayFormat("dd/MM/yyyy")
        self.start_date_edit.setDate(QDate.currentDate().addDays(-FIXED_SEARCH_WINDOW_DAYS))
        self.start_date_edit.setMaximumWidth(120)

        self.end_date_edit = QDateEdit()
        self.end_date_edit.setCalendarPopup(True)
        self.end_date_edit.setDisplayFormat("dd/MM/yyyy")
        self.end_date_edit.setDate(QDate.currentDate())
        self.end_date_edit.setMaximumWidth(120)

        date_range_row = QHBoxLayout()
        date_range_row.addWidget(self.start_date_edit)
        date_range_row.addWidget(QLabel("até"))
        date_range_row.addWidget(self.end_date_edit)
        date_range_row.addStretch(1)
        search_form.addRow("Período de busca:", date_range_row)

        search_row = QHBoxLayout()
        self.search_button = QPushButton("🔍 Buscar imagens disponíveis")
        self.search_button.setStyleSheet("QPushButton { font-weight: normal; }")
        self.search_button.clicked.connect(self._search_scenes)
        search_row.addWidget(self.search_button)
        self.search_status_label = QLabel("")
        self.search_status_label.setProperty("hint", "true")
        search_row.addWidget(self.search_status_label, stretch=1)
        search_form.addRow("", search_row)

        browser_row = QHBoxLayout()

        self.scene_table = QTableWidget(0, 3)
        self.scene_table.setHorizontalHeaderLabels(["Data", "Tile", "Dias"])
        self.scene_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.scene_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.scene_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.scene_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.scene_table.setMinimumHeight(140)
        self.scene_table.itemSelectionChanged.connect(self._on_scene_selection_changed)
        browser_row.addWidget(self.scene_table, stretch=2)

        self.scene_thumbnail_label = QLabel("Selecione uma cena para ver a miniatura")
        self.scene_thumbnail_label.setAlignment(Qt.AlignCenter)
        self.scene_thumbnail_label.setMinimumSize(220, 220)
        self.scene_thumbnail_label.setWordWrap(True)
        thumb_border = "#3a3d42" if self._is_dark_theme() else "#dfe1e6"
        thumb_bg = "#26282c" if self._is_dark_theme() else "#fafafb"
        self.scene_thumbnail_label.setStyleSheet(
            "QLabel {{ border: 1px solid {0}; border-radius: 6px; background: {1}; color: #888; }}"
            .format(thumb_border, thumb_bg))
        browser_row.addWidget(self.scene_thumbnail_label, stretch=1)

        search_form.addRow("Imagens encontradas:", browser_row)

        layout.addWidget(search_group)

        tclt_group = QGroupBox("Processamento TCLT (fusão / registro / PCA)")
        tclt_form = QFormLayout(tclt_group)

        self.tclt_exe_widget = QgsFileWidget()
        self.tclt_exe_widget.setStorageMode(QgsFileWidget.GetFile)
        self.tclt_exe_widget.setFilter("Executável (*.exe)")
        tclt_form.addRow("Executável tclt.exe:", self.tclt_exe_widget)

        layout.addWidget(tclt_group)
        layout.addStretch(1)
        return widget

    def _build_output_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)

        type_group = QGroupBox("Produtos")
        type_form = QFormLayout(type_group)

        self.gen_rgb_check = QCheckBox("Visualização RGB (PCA / Fusão Pancromática)")
        self.gen_rgb_check.setChecked(True)
        self.gen_ngb_check = QCheckBox("Banda Bruta NGB (NIR / Green / Blue)")
        self.gen_ngb_check.setChecked(False)

        type_form.addRow(self.gen_rgb_check)
        type_form.addRow(self.gen_ngb_check)

        layout.addWidget(type_group)

        out_group = QGroupBox("Diretório")
        out_form = QFormLayout(out_group)
        self.output_dir_widget = QgsFileWidget()
        self.output_dir_widget.setStorageMode(QgsFileWidget.GetDirectory)
        out_form.addRow("Pasta de saída:", self.output_dir_widget)
        layout.addWidget(out_group)

        format_group = QGroupBox("Formato de Saída")
        format_form = QFormLayout(format_group)

        format_row = QHBoxLayout()
        self.radio_format_group = QButtonGroup(self)
        self.radio_tiff = QRadioButton("GeoTIFF")
        self.radio_jp2 = QRadioButton("JPEG2000 (JP2)")
        for rb in (self.radio_tiff, self.radio_jp2):
            rb.setStyleSheet("QRadioButton { font-weight: normal; }")
        self.radio_jp2.setChecked(True)
        self.radio_format_group.addButton(self.radio_tiff, 0)
        self.radio_format_group.addButton(self.radio_jp2, 1)
        format_row.addWidget(self.radio_tiff)
        format_row.addWidget(self.radio_jp2)
        format_row.addStretch(1)
        format_form.addRow("Formato:", format_row)

        self.lossless_check = QCheckBox("Sem perdas (lossless)")
        self.lossless_check.setChecked(True)
        self.jp2_quality_label = QLabel("Qualidade JP2:")
        self.jp2_quality_spin = QSpinBox()
        self.jp2_quality_spin.setRange(1, 100)
        self.jp2_quality_spin.setValue(100)
        jp2_row = QHBoxLayout()
        jp2_row.addWidget(self.lossless_check)
        jp2_row.addSpacing(20)
        jp2_row.addWidget(self.jp2_quality_label)
        jp2_row.addWidget(self.jp2_quality_spin)
        jp2_row.addStretch(1)
        format_form.addRow("", jp2_row)

        self.radio_jp2.toggled.connect(self._update_format_controls)
        self.lossless_check.toggled.connect(self._update_format_controls)

        layout.addWidget(format_group)
        layout.addStretch(1)

        self._update_format_controls()
        return widget

    def _update_format_controls(self):
        is_jp2 = self.radio_jp2.isChecked()
        self.lossless_check.setEnabled(is_jp2)
        quality_enabled = is_jp2 and not self.lossless_check.isChecked()
        self.jp2_quality_spin.setEnabled(quality_enabled)
        self.jp2_quality_label.setEnabled(quality_enabled)

    def _build_log_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        self.log_box = QPlainTextEdit()
        self.log_box.setObjectName("logBox")
        self.log_box.setReadOnly(True)
        layout.addWidget(self.log_box)
        clear_btn = QPushButton("Limpar log")
        clear_btn.clicked.connect(self.log_box.clear)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(clear_btn)
        layout.addLayout(row)
        return widget

    def _build_bounding_box_coords(self, lon, lat, dist_km):
        """Calcula um bounding-box quadrado de dist_km ao redor da coordenada."""
        lat_deg = dist_km / 111.0
        lon_deg = dist_km / (111.0 * math.cos(math.radians(lat)))

        min_lon = lon - lon_deg
        max_lon = lon + lon_deg
        min_lat = lat - lat_deg
        max_lat = lat + lat_deg

        return [
            (min_lon, min_lat),
            (max_lon, min_lat),
            (max_lon, max_lat),
            (min_lon, max_lat),
            (min_lon, min_lat)
        ]

    def _collect_roi_params(self):
        params = {}

        if self.radio_roi_file.isChecked():
            path = self.roi_file_widget.filePath()
            if not path:
                raise ValueError("Selecione um arquivo de ROI (Shapefile ou GeoPackage).")
            params["roi_shapefile"] = path
            params["roi_coordinates"] = None

        elif self.radio_roi_single_coord.isChecked():
            lat_str = self.single_lat_edit.text().strip().replace(",", ".")
            lon_str = self.single_lon_edit.text().strip().replace(",", ".")
            if not lon_str or not lat_str:
                raise ValueError("Informe os valores de Latitude e Longitude.")
            try:
                lat = float(lat_str)
                lon = float(lon_str)
            except ValueError:
                raise ValueError("Latitude ou Longitude inválida.")

            params["roi_shapefile"] = None
            box_side_km = min(max(self.buffer_distance_spin.value(), POINT_BUFFER_MIN_KM), POINT_BUFFER_MAX_KM)
            params["roi_coordinates"] = self._build_bounding_box_coords(lon, lat, box_side_km / 2.0)

        params["process_entire_tile"] = self.process_entire_tile_check.isChecked()

        return params

    def _search_scenes(self):
        try:
            roi_params = self._collect_roi_params()
        except ValueError as exc:
            QMessageBox.warning(self, "ROI incompleto", str(exc))
            return

        start_qdate = self.start_date_edit.date()
        end_qdate = self.end_date_edit.date()
        if start_qdate > end_qdate:
            QMessageBox.warning(self, "Período inválido", "A data inicial não pode ser posterior à data final.")
            return

        params = dict(roi_params)
        params["date_start"] = start_qdate.toString("yyyy-MM-dd")
        params["date_end"] = end_qdate.toString("yyyy-MM-dd")

        self.scene_table.setRowCount(0)
        self.available_scenes = {}
        self._reset_thumbnail_preview()
        self.search_status_label.setText("Buscando...")
        self.search_button.setEnabled(False)
        self.run_button.setEnabled(False)

        self.search_task = CbersWpmSearchTask("Busca de cenas CBERS-4A/WPM", params)
        self.search_task.messageLogged.connect(self._append_log)
        self.search_task.taskCompleted.connect(self._on_search_finished_ok)
        self.search_task.taskTerminated.connect(self._on_search_finished_error)
        QgsApplication.taskManager().addTask(self.search_task)

    def _on_search_finished_ok(self):
        results = self.search_task.results or []
        self._populate_scene_table(results)
        if results:
            self.search_status_label.setText("{} cena(s) encontrada(s).".format(len(results)))
        else:
            self.search_status_label.setText(
                "Nenhuma cena encontrada nesse período - tente ampliar o período de busca ou revisar o ROI.")
        self.search_button.setEnabled(True)
        self.run_button.setEnabled(True)
        self.search_task = None

    def _on_search_finished_error(self):
        msg = (self.search_task.error_message if self.search_task else None) or "A busca foi interrompida."
        self.search_status_label.setText("Erro na busca.")
        self.search_button.setEnabled(True)
        self.run_button.setEnabled(True)
        QMessageBox.critical(self, "Erro na busca de cenas", msg)
        self.search_task = None

    def _populate_scene_table(self, results):
        self.scene_table.setRowCount(0)
        self.available_scenes = {}
        for entry in results:
            self.available_scenes[entry["id"]] = entry
            row = self.scene_table.rowCount()
            self.scene_table.insertRow(row)

            tile_txt = entry["tile"] or "?"

            date_item = QTableWidgetItem(entry["date"])
            date_item.setData(Qt.UserRole, entry["id"])
            self.scene_table.setItem(row, 0, date_item)
            self.scene_table.setItem(row, 1, QTableWidgetItem(tile_txt))
            self.scene_table.setItem(row, 2, QTableWidgetItem(str(entry["days_from_start"])))

    def _selected_scene_entries(self):
        seen_ids = set()
        entries = []
        for it in self.scene_table.selectedItems():
            entry_id = self.scene_table.item(it.row(), 0).data(Qt.UserRole)
            if entry_id in seen_ids:
                continue
            seen_ids.add(entry_id)
            entry = self.available_scenes.get(entry_id)
            if entry is not None:
                entries.append(entry)
        return entries

    def _selected_scene_groups_by_date(self):
        """Agrupa as cenas selecionadas por data. Cada grupo (data) é processado
        como uma execução independente do pipeline; as execuções são feitas em
        sequência, da data mais antiga para a mais recente."""
        entries = self._selected_scene_entries()
        groups = {}
        for entry in entries:
            groups.setdefault(entry["date"], []).append(entry)
        return [(d, groups[d]) for d in sorted(groups.keys())]

    def _on_scene_selection_changed(self):
        entries = self._selected_scene_entries()

        per_date_tile = {}
        conflict = False
        for entry in entries:
            key = (entry["date"], entry["tile"])
            if key in per_date_tile and per_date_tile[key] != entry["id"]:
                conflict = True
            per_date_tile[key] = entry["id"]

        n_dates = len(set(entry["date"] for entry in entries))
        if conflict:
            self.search_status_label.setText(
                "Selecione apenas UMA cena por tile em cada data - há conflito de tile na mesma data.")
        elif entries and n_dates > 1:
            self.search_status_label.setText(
                "{} cena(s) selecionada(s) em {} data(s) - serão processadas em sequência, "
                "da mais antiga para a mais recente.".format(len(entries), n_dates))
        elif entries:
            self.search_status_label.setText("{} cena(s) selecionada(s).".format(len(entries)))

        if entries:
            self._request_thumbnail(entries[-1])
        else:
            self._reset_thumbnail_preview()

    def _reset_thumbnail_preview(self):
        self.scene_thumbnail_label.setPixmap(QPixmap())
        self.scene_thumbnail_label.setText("Selecione uma cena para ver a miniatura")

    def _request_thumbnail(self, entry):
        url = entry.get("thumbnail_url")
        if not url:
            self.scene_thumbnail_label.setPixmap(QPixmap())
            self.scene_thumbnail_label.setText("Sem miniatura disponível para esta cena")
            return

        if getattr(self, "_thumb_worker", None) is not None:
            try:
                self._thumb_worker.thumbnail_ready.disconnect()
                self._thumb_worker.failed.disconnect()
            except TypeError:
                pass
            self._thumb_worker.quit()
            self._thumb_worker.wait(200)

        self.scene_thumbnail_label.setPixmap(QPixmap())
        self.scene_thumbnail_label.setText("Carregando miniatura...")

        self._thumb_worker = ThumbnailWorker(url, self)
        self._thumb_worker.thumbnail_ready.connect(self._show_thumbnail)
        self._thumb_worker.failed.connect(self._on_thumbnail_failed)
        self._thumb_worker.start()

    def _show_thumbnail(self, pixmap):
        self.scene_thumbnail_label.setText("")
        fitted = pixmap.scaled(
            self.scene_thumbnail_label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.scene_thumbnail_label.setPixmap(fitted)

    def _on_thumbnail_failed(self, msg):
        self.scene_thumbnail_label.setPixmap(QPixmap())
        self.scene_thumbnail_label.setText("Erro ao carregar miniatura: {}".format(msg))

    def _collect_common_params(self):
        """Parâmetros compartilhados por todas as datas selecionadas (ROI/extensão,
        produtos a gerar, executável TCLT, pasta e formato de saída)."""
        params = {}

        if not self.gen_rgb_check.isChecked() and not self.gen_ngb_check.isChecked():
            raise ValueError("Selecione pelo menos um produto para gerar (RGB e/ou NGB).")

        params["generate_rgb"] = self.gen_rgb_check.isChecked()
        params["generate_ngb"] = self.gen_ngb_check.isChecked()

        params.update(self._collect_roi_params())

        tclt_exe = self.tclt_exe_widget.filePath()
        if not tclt_exe:
            raise ValueError("Informe o caminho do executável tclt.exe.")
        params["tclt_exe"] = tclt_exe
        params["threads"] = FIXED_THREADS

        output_dir = self.output_dir_widget.filePath()
        if not output_dir:
            raise ValueError("Informe a pasta de saída dos produtos finais.")
        params["final_output_dir"] = output_dir
        params["contrast_stretch"] = FIXED_CONTRAST_STRETCH

        params["output_format"] = "JP2" if self.radio_jp2.isChecked() else "TIFF"
        params["lossless"] = self.lossless_check.isChecked()
        params["jp2_quality"] = self.jp2_quality_spin.value()
        params["output_crs"] = None

        return params

    def _collect_date_group_params(self):
        """Retorna uma lista [(data, params), ...] - uma entrada por data
        selecionada na tabela de cenas, em ordem cronológica (mais antiga
        primeiro), pronta para ser processada sequencialmente."""
        common_params = self._collect_common_params()

        groups = self._selected_scene_groups_by_date()
        if not groups:
            raise ValueError(
                "Busque as imagens disponíveis (aba Processamento) e selecione ao menos uma cena antes de executar.")

        date_group_params = []
        for date_str, entries in groups:
            stac_items = []
            tiles_seen = {}
            for entry in entries:
                tile = entry["tile"]
                if tile in tiles_seen and tiles_seen[tile] != entry["id"]:
                    raise ValueError(
                        "Selecione apenas uma cena por tile na data {} (conflito no tile {}).".format(date_str, tile))
                tiles_seen[tile] = entry["id"]
                stac_items.append(entry["item"])

            if not stac_items:
                continue

            params = dict(common_params)
            params["stac_items"] = stac_items
            params["target_date"] = date_str
            date_group_params.append((date_str, params))

        if not date_group_params:
            raise ValueError("Nenhuma cena válida selecionada - refaça a busca e selecione novamente.")

        return date_group_params

    def run_processing(self):
        try:
            self.pending_runs = self._collect_date_group_params()
        except ValueError as exc:
            QMessageBox.warning(self, "Parâmetros incompletos", str(exc))
            return

        if self.pending_runs and self.pending_runs[0][1].get("process_entire_tile"):
            stac_items = []
            seen_ids = set()
            for _, group_params in self.pending_runs:
                for it in group_params.get("stac_items", []):
                    item_id = it.get("id")
                    if item_id in seen_ids:
                        continue
                    seen_ids.add(item_id)
                    stac_items.append(it)

            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                check_entire_tile_resources(stac_items)
            except PipelineError as exc:
                QMessageBox.critical(self, "Recursos insuficientes", str(exc))
                return
            finally:
                QApplication.restoreOverrideCursor()

        self._save_settings()
        self.tabs.setCurrentIndex(3)
        self.log_box.clear()

        total = len(self.pending_runs)
        if total > 1:
            self._append_log(
                "Iniciando processamento sequencial de {} data(s): {}.".format(
                    total, ", ".join(d for d, _ in self.pending_runs)))
        else:
            self._append_log("Iniciando processamento...")

        self.run_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.progress_bar.setVisible(True)

        self._unload_layers_from_output_dir(self.pending_runs[0][1]["final_output_dir"])

        self._run_index = 0
        self._all_outputs = []
        self._run_cancelled = False
        self._start_next_run()

    def _start_next_run(self):
        date_str, params = self.pending_runs[self._run_index]
        total = len(self.pending_runs)
        if total > 1:
            self._append_log("--- Processando data {} ({}/{}) ---".format(date_str, self._run_index + 1, total))

        self.task = CbersWpmTask("WPM 1-meter spatial resolution - {}".format(date_str), params)
        self.task.messageLogged.connect(self._append_log)
        self.task.taskCompleted.connect(self._on_task_finished_ok)
        self.task.taskTerminated.connect(self._on_task_finished_error)
        QgsApplication.taskManager().addTask(self.task)

    def cancel_processing(self):
        if self.task is not None:
            self._run_cancelled = True
            self.task.cancel()
            self._append_log("Cancelamento solicitado — aguardando o TCLT encerrar...")
            self.cancel_button.setEnabled(False)

    def _on_task_finished_ok(self):
        date_str, _ = self.pending_runs[self._run_index]
        total = len(self.pending_runs)
        outputs = self.task.outputs or []
        self._append_log(
            "Data {} concluída com sucesso.".format(date_str) if total > 1 else "Processamento concluído com sucesso.")
        if outputs:
            self._append_log("Produtos gerados:")
            for p in outputs:
                self._append_log("  • {}".format(p))
            self._load_outputs_into_qgis(outputs)
        self._all_outputs.extend(outputs)

        self._run_index += 1
        if self._run_index < total and not self._run_cancelled:
            self._start_next_run()
            return

        total_dates = total
        outputs = self._all_outputs
        self._reset_run_state()
        self.pending_runs = []
        self._append_log("Processamento concluído com sucesso.")
        QMessageBox.information(
            self, "Concluído",
            "Processamento concluído com sucesso.\n\n{} produto(s) gerado(s) em {} data(s), em:\n{}\n\n"
            "Lembrete: se necessário, ajuste o contraste da imagem pelo histograma (Propriedades da Camada > Simbologia).".format(
                len(outputs), total_dates, self.output_dir_widget.filePath())
        )

    def _unload_layers_from_output_dir(self, output_dir):
        """Remove do projeto QGIS qualquer camada raster cujo arquivo esteja dentro
        de output_dir, liberando o handle do GDAL antes de sobrescrever o arquivo."""
        if not output_dir:
            return
        output_dir = os.path.normcase(os.path.normpath(output_dir))
        project = QgsProject.instance()
        to_remove = []
        for layer in project.mapLayers().values():
            source = layer.source().split("|")[0]
            try:
                source_dir = os.path.normcase(os.path.normpath(os.path.dirname(source)))
            except Exception:
                continue
            if source_dir == output_dir:
                to_remove.append(layer.id())
        if to_remove:
            project.removeMapLayers(to_remove)
            if self.canvas is not None:
                self.canvas.refresh()

    def _load_outputs_into_qgis(self, output_paths):
        """Carrega automaticamente os produtos selecionados (.tif/.jp2) no painel
        de Camadas do QGIS."""
        project = QgsProject.instance()
        for path in output_paths:
            if not path or not os.path.exists(path):
                self._append_log("Aviso: produto não encontrado para carregar: {}".format(path))
                continue
            layer_name = os.path.splitext(os.path.basename(path))[0]
            layer = QgsRasterLayer(path, layer_name)
            if not layer.isValid():
                self._append_log("Aviso: não foi possível carregar a camada '{}' no QGIS.".format(layer_name))
                continue
            project.addMapLayer(layer)
            self._append_log("Camada adicionada ao projeto: {}".format(layer_name))
        if self.canvas is not None:
            self.canvas.refresh()

    def _on_task_finished_error(self):
        msg = self.task.error_message or "O processamento foi interrompido."
        was_user_cancel = self.task.exception is None and "cancelad" in msg.lower()
        total = len(self.pending_runs)
        completed = self._run_index
        date_str = self.pending_runs[self._run_index][0] if self.pending_runs else None

        if was_user_cancel:
            self._append_log("Processamento cancelado.")
        else:
            label = "Data {}: ".format(date_str) if total > 1 and date_str else ""
            self._append_log("ERRO: {}{}".format(label, msg))

        self._reset_run_state()
        self.pending_runs = []

        if not was_user_cancel:
            if total > 1:
                msg = "{}\n\n({} de {} data(s) processada(s) com sucesso antes do erro.)".format(
                    msg, completed, total)
            QMessageBox.critical(self, "Erro no processamento", msg)

    def _reset_run_state(self):
        self.run_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.progress_bar.setVisible(False)
        self.task = None

    def _append_log(self, message):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_box.appendPlainText("[{}] {}".format(timestamp, message))

    def _load_settings(self):
        s = QgsSettings()
        self.tclt_exe_widget.setFilePath(s.value("cbers_wpm/tclt_exe", "", type=str))
        self.output_dir_widget.setFilePath(s.value("cbers_wpm/output_dir", "", type=str))

    def _save_settings(self):
        s = QgsSettings()
        s.setValue("cbers_wpm/tclt_exe", self.tclt_exe_widget.filePath())
        s.setValue("cbers_wpm/output_dir", self.output_dir_widget.filePath())

    def closeEvent(self, event):
        if self.search_task is not None and self.search_task.isActive():
            self.search_task.cancel()
        if getattr(self, "_thumb_worker", None) is not None and self._thumb_worker.isRunning():
            self._thumb_worker.quit()
            self._thumb_worker.wait(200)
        if self.task is not None and self.task.isActive():
            reply = QMessageBox.question(
                self, "Processamento em andamento",
                "Um processamento ainda está em execução. Deseja cancelá-lo e fechar?",
                QMessageBox.Yes | QMessageBox.No)
            if reply != QMessageBox.Yes:
                event.ignore()
                return
            self.task.cancel()
        super().closeEvent(event)