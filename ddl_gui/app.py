"""Ventana principal de DDL Extractor (PySide6)."""
import json
import sys
import traceback
from pathlib import Path

from PySide6.QtCore import QObject, QSettings, QThread, QUrl, Qt, Signal, Slot
from PySide6.QtGui import (QAction, QColor, QDesktopServices, QFontDatabase, QGuiApplication,
                           QIcon, QKeySequence, QTextCursor)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog,
                               QDialogButtonBox, QFileDialog, QGridLayout,
                               QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
                               QLineEdit, QListWidget, QMainWindow, QMessageBox, QPlainTextEdit,
                               QPushButton, QSpinBox, QSplitter, QStatusBar, QTableWidget,
                               QTableWidgetItem, QTabWidget, QTextBrowser, QVBoxLayout, QWidget)

# Permite ejecutar desde el repo sin instalar (python -m ddl_gui)
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from ddl_common import env_to_config, odoo_conf_to_config, read_env_file  # noqa: E402
from ddl_gui import APP_NAME, ORG_NAME, __version__, jobs  # noqa: E402
from ddl_gui.highlighter import SqlHighlighter  # noqa: E402

STATUS_COLORS = {"error": "#e53935", "warn": "#fb8c00", "skip": "#9e9e9e", "ok": "#43a047"}
STATUS_TEXT = {"error": "ERROR", "warn": "WARN", "skip": "OMITIDO", "ok": "OK"}
KIND_TEXT = {"table": "Tabla", "column": "Columna", "index": "Indice", "constraint": "Constraint",
             "fk": "FK", "other": "Otra"}


def resource_path(*parts):
    """Ruta a recursos (docs/, assets/) tanto en el repo como empaquetado con PyInstaller."""
    base = Path(getattr(sys, "_MEIPASS", _ROOT))
    return base.joinpath(*parts)


def is_dark():
    try:
        return QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark
    except Exception:  # noqa: BLE001
        return QApplication.palette().window().color().lightness() < 128


# -------------------------------------------------------------------------
# Worker generico
# -------------------------------------------------------------------------
class Worker(QObject):
    """Corre fn en un QThread. Emite (job_id, payload) para que el receptor
    (un QObject del hilo principal) despache el callback correcto via conexion encolada."""
    log = Signal(str)
    done = Signal(int, object)
    failed = Signal(int, str)

    def __init__(self, job_id, fn, *args):
        super().__init__()
        self.job_id, self.fn, self.args = job_id, fn, args

    @Slot()
    def run(self):
        try:
            self.done.emit(self.job_id, self.fn(*self.args, self.log.emit))
        except SystemExit as e:  # el core usa sys.exit para errores de config/conexion
            self.failed.emit(self.job_id, str(e.code if e.code is not None else e))
        except Exception as e:  # noqa: BLE001
            self.failed.emit(self.job_id, f"{e}\n\n{traceback.format_exc()}")


# -------------------------------------------------------------------------
# Panel de conexion
# -------------------------------------------------------------------------
class ConnectionPanel(QGroupBox):
    changed = Signal()
    odoo_detected = Signal(int)

    def __init__(self, settings, run_job):
        super().__init__("Conexion a PostgreSQL")
        self.s, self.run_job = settings, run_job
        self.profile = QComboBox()
        self.profile.setMinimumWidth(180)
        self.profile.currentTextChanged.connect(self._load_profile)
        self.host, self.db, self.user = QLineEdit(), QLineEdit(), QLineEdit()
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(5432)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        self.schema = QLineEdit("public")
        self.offline = QCheckBox("Sin BD (solo analisis estatico + guards)")
        self.offline.toggled.connect(self._toggle_offline)
        self.save_pwd = QCheckBox("Guardar contrasena en el perfil")
        self.save_pwd.setToolTip("Se guarda en texto plano en la configuracion del usuario.")
        self.status = QLabel("Sin probar")
        self.status.setStyleSheet("color: gray")

        for w, ph in ((self.host, "192.168.3.150"), (self.db, "nombre_bd"), (self.user, "odoo")):
            w.setPlaceholderText(ph)

        btn_env = QPushButton("Cargar .env")
        btn_env.clicked.connect(self.load_env)
        btn_conf = QPushButton("Cargar odoo.conf")
        btn_conf.clicked.connect(self.load_conf)
        btn_test = QPushButton("Probar conexion")
        btn_test.clicked.connect(self.test)
        btn_save = QPushButton("Guardar perfil")
        btn_save.clicked.connect(self.save_profile)
        btn_del = QPushButton("Eliminar")
        btn_del.clicked.connect(self.delete_profile)

        g = QGridLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel("Perfil"))
        top.addWidget(self.profile, 1)
        for b in (btn_save, btn_del):
            top.addWidget(b)
        top.addSpacing(16)
        for b in (btn_env, btn_conf):
            top.addWidget(b)
        g.addLayout(top, 0, 0, 1, 6)
        g.addWidget(QLabel("Host"), 1, 0)
        g.addWidget(self.host, 1, 1)
        g.addWidget(QLabel("Puerto"), 1, 2)
        g.addWidget(self.port, 1, 3)
        g.addWidget(QLabel("Base"), 1, 4)
        g.addWidget(self.db, 1, 5)
        g.addWidget(QLabel("Usuario"), 2, 0)
        g.addWidget(self.user, 2, 1)
        g.addWidget(QLabel("Contrasena"), 2, 2)
        g.addWidget(self.password, 2, 3)
        g.addWidget(QLabel("Esquema"), 2, 4)
        g.addWidget(self.schema, 2, 5)
        g.addWidget(self.offline, 3, 0, 1, 2)
        g.addWidget(self.save_pwd, 3, 2, 1, 2)
        test_row = QHBoxLayout()
        test_row.addWidget(btn_test)
        test_row.addWidget(self.status, 1)
        g.addLayout(test_row, 3, 4, 1, 2)
        g.setColumnStretch(1, 1)
        g.setColumnStretch(5, 1)
        self._refresh_profiles()

    # ---- perfiles ------------------------------------------------------
    def _profiles(self):
        try:
            return json.loads(self.s.value("profiles", "{}"))
        except Exception:  # noqa: BLE001
            return {}

    def _refresh_profiles(self, select=None):
        profs = self._profiles()
        self.profile.blockSignals(True)
        self.profile.clear()
        self.profile.addItem("(sin perfil)")
        self.profile.addItems(sorted(profs))
        self.profile.blockSignals(False)
        target = select or self.s.value("last_profile", "")
        if target and target in profs:
            self.profile.setCurrentText(target)
            self._load_profile(target)

    def _load_profile(self, name):
        p = self._profiles().get(name)
        if not p:
            return
        self.set_config(p)
        self.save_pwd.setChecked(bool(p.get("password")))
        self.s.setValue("last_profile", name)

    def save_profile(self):
        cur = self.profile.currentText()
        name, ok = QInputDialog.getText(self, "Guardar perfil", "Nombre del perfil:",
                                        text="" if cur == "(sin perfil)" else cur)
        if not ok or not name.strip():
            return
        profs = self._profiles()
        cfg = self.config()
        cfg.pop("offline", None)
        if not self.save_pwd.isChecked():
            cfg["password"] = ""
        profs[name.strip()] = cfg
        self.s.setValue("profiles", json.dumps(profs))
        self._refresh_profiles(name.strip())

    def delete_profile(self):
        name = self.profile.currentText()
        profs = self._profiles()
        if name in profs and QMessageBox.question(self, "Eliminar", f"Eliminar perfil '{name}'?") \
                == QMessageBox.Yes:
            profs.pop(name)
            self.s.setValue("profiles", json.dumps(profs))
            self._refresh_profiles()

    # ---- carga de archivos -------------------------------------------
    def load_env(self):
        path, _ = QFileDialog.getOpenFileName(self, "Seleccionar .env", self.s.value("last_dir", ""),
                                              "Archivos env (*.env .env *);;Todos (*)")
        if path:
            self.s.setValue("last_dir", str(Path(path).parent))
            self.set_config(env_to_config(read_env_file(path)))
            self._set_status(f"Cargado {Path(path).name}", "gray")

    def load_conf(self):
        path, _ = QFileDialog.getOpenFileName(self, "Seleccionar odoo.conf", self.s.value("last_dir", ""),
                                              "Config (*.conf *.cfg *.ini);;Todos (*)")
        if path:
            self.s.setValue("last_dir", str(Path(path).parent))
            try:
                self.set_config(odoo_conf_to_config(path))
                self._set_status(f"Cargado {Path(path).name}", "gray")
            except SystemExit as e:
                QMessageBox.warning(self, "odoo.conf", str(e.code))

    # ---- estado --------------------------------------------------------
    def set_config(self, cfg):
        if cfg.get("host"):
            self.host.setText(str(cfg["host"]))
        if cfg.get("port"):
            self.port.setValue(int(cfg["port"]))
        if cfg.get("db"):
            self.db.setText(str(cfg["db"]))
        if cfg.get("user"):
            self.user.setText(str(cfg["user"]))
        if "password" in cfg:
            self.password.setText(str(cfg.get("password") or ""))
        if cfg.get("schema"):
            self.schema.setText(cfg["schema"])
        self.changed.emit()

    def config(self):
        return {"host": self.host.text().strip(), "port": self.port.value(),
                "db": self.db.text().strip(), "user": self.user.text().strip(),
                "password": self.password.text(), "schema": self.schema.text().strip() or "public",
                "offline": self.offline.isChecked()}

    def _toggle_offline(self, on):
        for w in (self.host, self.port, self.db, self.user, self.password):
            w.setEnabled(not on)
        self.changed.emit()

    def _set_status(self, text, color):
        self.status.setText(text)
        self.status.setStyleSheet(f"color: {color}")

    def test(self):
        cfg = self.config()
        if cfg["offline"]:
            self._set_status("Modo sin BD", "gray")
            return
        if not cfg["db"]:
            QMessageBox.warning(self, "Conexion", "Falta el nombre de la base.")
            return
        self._set_status("Conectando...", "gray")

        def ok(r):
            extra = f" | Odoo {r['odoo']}" if r["odoo"] else " | sin ir_module_module"
            self._set_status(f"OK: {r['version']} | {r['tables']} tablas{extra}", STATUS_COLORS["ok"])
            if r["odoo"]:
                try:
                    self.odoo_detected.emit(int(str(r["odoo"]).split(".")[0]))
                except ValueError:
                    pass

        def bad(msg):
            self._set_status("Error de conexion", STATUS_COLORS["error"])
            QMessageBox.critical(self, "Conexion", msg.split("\n\n")[0])

        self.run_job(jobs.test_connection, (cfg,), ok, bad, busy=False)


# -------------------------------------------------------------------------
# Opciones comunes
# -------------------------------------------------------------------------
class OptionsBox(QGroupBox):
    def __init__(self, extract=False):
        super().__init__("Opciones")
        f = QGridLayout(self)
        self.dry_run = QCheckBox("Dry-run (ejecutar + ROLLBACK)")
        self.dry_run.setChecked(True)
        self.dry_run.setToolTip("Ejecuta el script resultante sentencia por sentencia con SAVEPOINT "
                                "y siempre hace ROLLBACK al final.")
        self.guard = QCheckBox("Guards en runtime (DO $$)")
        self.guard.setChecked(True)
        self.guard.setToolTip("FKs y constraints con chequeo to_regclass/pg_constraint al ejecutar.")
        self.data_checks = QCheckBox("Chequear datos (huerfanos, NOT NULL)")
        self.data_checks.setChecked(True)
        self.fk_not_valid = QCheckBox("FK con huerfanos -> NOT VALID")
        self.allow_drop = QCheckBox("Permitir DROP/TRUNCATE/DELETE")
        self.commit = QCheckBox("Terminar con COMMIT")
        self.commit.setToolTip("El script generado termina con COMMIT en vez de ROLLBACK.")
        self.commit.setStyleSheet(f"color: {STATUS_COLORS['error']}")
        self.lock_timeout = QLineEdit("15s")
        self.lock_timeout.setMaximumWidth(80)
        widgets = [self.dry_run, self.guard, self.data_checks, self.fk_not_valid,
                   self.allow_drop, self.commit]
        for i, w in enumerate(widgets):
            f.addWidget(w, i // 2, i % 2)
        lt = QHBoxLayout()
        lt.addWidget(QLabel("lock_timeout"))
        lt.addWidget(self.lock_timeout)
        lt.addStretch()
        f.addLayout(lt, 3, 0)
        # --- script de despliegue para el DBA
        self.deploy = QCheckBox("Generar script (DDL validados)")
        self.deploy.setChecked(True)
        self.deploy.setToolTip("Script limpio que termina en COMMIT, listo para produccion:\n"
                               "sin comentarios de revision, con instrucciones, lista de excluidas\n"
                               "y verificacion final de objetos.")
        self.target_db = QLineEdit()
        self.target_db.setPlaceholderText("BD destino (opcional, ej. odoo_prod)")
        self.target_db.setToolTip("Si se indica, el script aborta si se ejecuta en otra base.")
        self.deploy_strict = QCheckBox("Si falta algun objeto al final, revertir TODO (recomendado)")
        self.deploy_strict.setChecked(True)
        self.deploy_strict.setStyleSheet("font-weight: bold")
        self.deploy_strict.setToolTip(
            "Al terminar, el script verifica que existan TODAS las tablas, columnas, indices y FKs.\n"
            "Marcado: si falta alguna, hace ROLLBACK completo y la base queda como estaba.\n"
            "Desmarcado: hace COMMIT de lo que se pudo y solo avisa lo que falto (despliegue PARCIAL).")
        self.deploy_strict.toggled.connect(
            lambda on: self.deploy_strict.setStyleSheet(
                "font-weight: bold" if on else f"font-weight: bold; color: {STATUS_COLORS['error']}"))
        self.deploy.toggled.connect(self.target_db.setEnabled)
        self.deploy.toggled.connect(self.deploy_strict.setEnabled)
        f.addWidget(self.deploy, 5, 0)
        f.addWidget(self.target_db, 5, 1)
        f.addWidget(self.deploy_strict, 6, 0, 1, 2)
        if extract:
            self.allow_drop.hide()
            self.odoo_version = QComboBox()
            self.odoo_version.addItems(["14", "15", "16", "17", "18", "19"])
            self.odoo_version.setCurrentText("17")
            self.id_type = QComboBox()
            self.id_type.addItems(["BIGINT", "INTEGER"])
            self.id_type.setToolTip("Tipo de las columnas Many2one/create_uid/write_uid")
            self.raw = QCheckBox("DDL crudo (sin validar)")
            row = QHBoxLayout()
            row.addWidget(QLabel("Odoo"))
            row.addWidget(self.odoo_version)
            row.addWidget(QLabel("Tipo id M2O"))
            row.addWidget(self.id_type)
            row.addStretch()
            f.addLayout(row, 3, 1)
            f.addWidget(self.raw, 4, 0)

    def values(self):
        v = {"dry_run": self.dry_run.isChecked(), "guard": self.guard.isChecked(),
             "data_checks": self.data_checks.isChecked(), "fk_not_valid": self.fk_not_valid.isChecked(),
             "allow_drop": self.allow_drop.isChecked(), "commit": self.commit.isChecked(),
             "lock_timeout": self.lock_timeout.text().strip(),
             "deploy": self.deploy.isChecked(), "target_db": self.target_db.text().strip(),
             "deploy_strict": self.deploy_strict.isChecked()}
        if hasattr(self, "odoo_version"):
            v.update(odoo_version=int(self.odoo_version.currentText()),
                     id_type=self.id_type.currentText(), raw=self.raw.isChecked())
        return v


class PathList(QWidget):
    """Lista de rutas con botones agregar/quitar."""

    def __init__(self, settings, buttons):
        super().__init__()
        self.s = settings
        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.list.setAcceptDrops(True)
        col = QVBoxLayout()
        for text, cb in buttons:
            b = QPushButton(text)
            b.clicked.connect(cb)
            col.addWidget(b)
        rm = QPushButton("Quitar")
        rm.clicked.connect(lambda: [self.list.takeItem(self.list.row(i)) for i in self.list.selectedItems()])
        clr = QPushButton("Limpiar")
        clr.clicked.connect(self.list.clear)
        col.addWidget(rm)
        col.addWidget(clr)
        col.addStretch()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.list, 1)
        lay.addLayout(col)

    def add(self, paths):
        existing = set(self.paths())
        for p in paths:
            p = str(Path(p))
            if p not in existing:
                self.list.addItem(p)
                existing.add(p)
        if paths:
            self.s.setValue("last_dir", str(Path(paths[0]).parent))

    def paths(self):
        return [self.list.item(i).text() for i in range(self.list.count())]


# -------------------------------------------------------------------------
# Tabs de accion
# -------------------------------------------------------------------------
class ValidateTab(QWidget):
    def __init__(self, settings, on_run):
        super().__init__()
        self.s = settings
        self.files = PathList(settings, [("Agregar .sql", self.add_files), ("Agregar carpeta", self.add_dir)])
        self.out = QLineEdit()
        self.out.setPlaceholderText("(automatico: <archivo>.validated.sql)")
        browse = QPushButton("...")
        browse.setMaximumWidth(36)
        browse.clicked.connect(self.pick_out)
        self.opts = OptionsBox()
        run = QPushButton("Validar")
        run.setDefault(True)
        run.setMinimumHeight(34)
        run.setStyleSheet("font-weight: bold")
        run.clicked.connect(on_run)
        self.run_btn = run

        out_row = QHBoxLayout()
        out_row.addWidget(QLabel("Salida"))
        out_row.addWidget(self.out, 1)
        out_row.addWidget(browse)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Archivos DDL a validar (se procesan juntos, en orden):"))
        lay.addWidget(self.files, 1)
        lay.addLayout(out_row)
        lay.addWidget(self.opts)
        lay.addWidget(run)

    def add_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Archivos DDL", self.s.value("last_dir", ""),
                                                "SQL (*.sql *.txt *.ddl);;Todos (*)")
        self.files.add(paths)

    def add_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Carpeta con .sql", self.s.value("last_dir", ""))
        if d:
            self.files.add([d])

    def pick_out(self):
        p, _ = QFileDialog.getSaveFileName(self, "Guardar script validado", self.s.value("last_dir", ""),
                                           "SQL (*.sql)")
        if p:
            self.out.setText(p)


class ExtractTab(QWidget):
    def __init__(self, settings, on_run):
        super().__init__()
        self.s = settings
        self.mods = PathList(settings, [("Agregar modulo", self.add_module),
                                        ("Escanear repo", self.scan_repo)])
        self.opts = OptionsBox(extract=True)
        run = QPushButton("Extraer y validar")
        run.setMinimumHeight(34)
        run.setStyleSheet("font-weight: bold")
        run.clicked.connect(on_run)
        self.run_btn = run
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Modulos Odoo (carpetas con __manifest__.py). "
                             "El DDL se guarda en <modulo>/ddl/:"))
        lay.addWidget(self.mods, 1)
        lay.addWidget(self.opts)
        lay.addWidget(run)

    def add_module(self):
        d = QFileDialog.getExistingDirectory(self, "Modulo Odoo", self.s.value("last_dir", ""))
        if d:
            self.mods.add([d])

    def scan_repo(self):
        d = QFileDialog.getExistingDirectory(self, "Repositorio", self.s.value("last_dir", ""))
        if not d:
            return
        found = jobs.find_modules([d])
        if not found:
            QMessageBox.information(self, "Escanear", "No se encontraron modulos (__manifest__.py).")
        self.mods.add([str(m) for m in found])


# -------------------------------------------------------------------------
# Panel de resultados
# -------------------------------------------------------------------------
class ResultsPanel(QTabWidget):
    def __init__(self):
        super().__init__()
        self.result = None
        mono = QFontDatabase.systemFont(QFontDatabase.FixedFont)

        # -- resultados
        w = QWidget()
        self.badges = {}
        top = QHBoxLayout()
        for k in ("ok", "warn", "skip", "error"):
            b = QLabel(f"{STATUS_TEXT[k]}: 0")
            b.setStyleSheet(f"background:{STATUS_COLORS[k]};color:white;border-radius:4px;padding:3px 10px;"
                            "font-weight:bold")
            b.setMinimumWidth(90)
            b.setAlignment(Qt.AlignCenter)
            self.badges[k] = b
            top.addWidget(b)
        top.addStretch()
        self.filter = QComboBox()
        self.filter.addItems(["Todos", "Errores", "Warnings", "Omitidos", "OK", "Errores + warnings"])
        self.filter.currentIndexChanged.connect(self._apply_filter)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Buscar...")
        self.search.textChanged.connect(self._apply_filter)
        top.addWidget(QLabel("Filtro"))
        top.addWidget(self.filter)
        top.addWidget(self.search)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Estado", "Tipo", "Objeto", "Origen", "Detalle"])
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.verticalHeader().hide()
        self.table.setWordWrap(True)
        h = self.table.horizontalHeader()
        for i, mode in enumerate([QHeaderView.ResizeToContents] * 2 + [QHeaderView.Interactive] * 2
                                 + [QHeaderView.Stretch]):
            h.setSectionResizeMode(i, mode)
        self.table.setColumnWidth(2, 340)
        self.table.setColumnWidth(3, 150)
        self.table.cellDoubleClicked.connect(self._goto_sql)
        v = QVBoxLayout(w)
        v.addLayout(top)
        v.addWidget(self.table)
        hint = QLabel("Doble clic en una fila para verla en el SQL generado.")
        hint.setStyleSheet("color: gray")
        v.addWidget(hint)
        self.addTab(w, "Resultados")

        # -- relaciones faltantes
        self.missing = QTableWidget(0, 3)
        self.missing.setHorizontalHeaderLabels(["Tabla inexistente", "Sentencias", "Detalle"])
        self.missing.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.missing.verticalHeader().hide()
        self.missing.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.missing.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.addTab(self.missing, "Relaciones faltantes")

        # -- SQL
        w2 = QWidget()
        bar = QHBoxLayout()
        self.out_combo = QComboBox()
        self.out_combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.out_combo.setMinimumContentsLength(28)
        self.out_combo.currentIndexChanged.connect(self._show_output)
        b_copy = QPushButton("Copiar")
        b_copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.sql.toPlainText()))
        b_save = QPushButton("Guardar como...")
        b_save.clicked.connect(self._save_as)
        b_dir = QPushButton("Abrir carpeta")
        b_dir.clicked.connect(lambda: self._open(self._current_output(1), folder=True))
        b_rep = QPushButton("Abrir reporte")
        b_rep.clicked.connect(lambda: self._open(self._current_output(2)))
        self.find = QLineEdit()
        self.find.setPlaceholderText("Buscar en SQL (Enter)")
        self.find.returnPressed.connect(lambda: self._find(self.find.text()))
        for x in (QLabel("Archivo"), self.out_combo):
            bar.addWidget(x)
        bar.addWidget(self.find, 1)
        for x in (b_copy, b_save, b_dir, b_rep):
            bar.addWidget(x)
        self.sql = QPlainTextEdit()
        self.sql.setReadOnly(True)
        self.sql.setFont(mono)
        self.sql.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.hl = SqlHighlighter(self.sql.document(), dark=is_dark())
        v2 = QVBoxLayout(w2)
        v2.addLayout(bar)
        v2.addWidget(self.sql)
        self.addTab(w2, "SQL generado")

        # -- dry-run
        self.dry = QTableWidget(0, 4)
        self.dry.setHorizontalHeaderLabels(["Origen", "#", "Error", "Sentencia"])
        self.dry.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.dry.verticalHeader().hide()
        self.dry.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.dry.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.addTab(self.dry, "Dry-run")

        # -- log
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(mono)
        self.log.setMaximumBlockCount(20000)
        self.addTab(self.log, "Log")

    # ---- carga -----------------------------------------------------------
    @Slot(str)
    def append_log(self, text):
        self.log.appendPlainText(text)

    def show_result(self, r):
        self.result = r
        st = r["stats"]
        for k, b in self.badges.items():
            b.setText(f"{STATUS_TEXT[k]}: {st.get(k, 0)}")

        self.table.setRowCount(0)
        rows = list(r["items"])
        self.table.setRowCount(len(rows))
        for i, (src, it) in enumerate(rows):
            status = it.status
            main = [f"{s}: {m}" for s, m in it.msgs if s != "info"]
            info = [m for s, m in it.msgs if s == "info"]
            detail = "\n".join(main) or ("; ".join(info) if info else "-")
            cells = [STATUS_TEXT[status], KIND_TEXT.get(it.kind, it.kind), it.label, it.src, detail]
            for j, text in enumerate(cells):
                cell = QTableWidgetItem(text)
                if j == 0:
                    cell.setForeground(QColor(STATUS_COLORS[status]))
                    f = cell.font()
                    f.setBold(True)
                    cell.setFont(f)
                    cell.setData(Qt.UserRole, status)
                    cell.setData(Qt.UserRole + 1, src)
                if j == 4 and not main:
                    cell.setForeground(QColor("#8a8a8a"))
                self.table.setItem(i, j, cell)
        self.table.resizeRowsToContents()
        self._apply_filter()

        miss = jobs.missing_relations(r["items"])
        self.missing.setRowCount(len(miss))
        for i, (t, refs) in enumerate(sorted(miss.items(), key=lambda x: -len(x[1]))):
            self.missing.setItem(i, 0, QTableWidgetItem(t))
            self.missing.setItem(i, 1, QTableWidgetItem(str(len(refs))))
            self.missing.setItem(i, 2, QTableWidgetItem("; ".join(f"[{s}] {lab}" for s, lab in refs)))
        self.setTabText(1, f"Relaciones faltantes ({len(miss)})")

        self.out_combo.blockSignals(True)
        self.out_combo.clear()
        for label, path, rep, _ in r["outputs"]:
            self.out_combo.addItem(f"{label}  ->  {Path(path).name}", str(path))
        self.out_combo.blockSignals(False)
        self._show_output(0)

        errs = [(src, e) for src, d in r["dry"] for e in d["errors"]]
        self.dry.setRowCount(len(errs))
        for i, (src, (n, sql, err)) in enumerate(errs):
            for j, text in enumerate([src, str(n), " ".join(err.split()), " ".join(sql.split())[:300]]):
                self.dry.setItem(i, j, QTableWidgetItem(text))
        ran = sum(d["n"] for _, d in r["dry"])
        self.setTabText(3, f"Dry-run ({len(errs)} err / {ran})" if r["dry"] else "Dry-run")
        self.setCurrentIndex(0)

    def _apply_filter(self):
        mode = self.filter.currentIndex()
        allowed = {0: None, 1: {"error"}, 2: {"warn"}, 3: {"skip"}, 4: {"ok"}, 5: {"error", "warn"}}[mode]
        q = self.search.text().lower().strip()
        for i in range(self.table.rowCount()):
            st = self.table.item(i, 0).data(Qt.UserRole)
            vis = allowed is None or st in allowed
            if vis and q:
                vis = any(q in (self.table.item(i, j).text().lower()) for j in range(5))
            self.table.setRowHidden(i, not vis)

    def _current_output(self, idx):
        if not self.result or not self.result["outputs"]:
            return None
        i = max(self.out_combo.currentIndex(), 0)
        return self.result["outputs"][i][idx]

    def _show_output(self, i):
        if self.result and 0 <= i < len(self.result["outputs"]):
            self.sql.setPlainText(self.result["outputs"][i][3])
        else:
            self.sql.clear()

    def _goto_sql(self, row, _col):
        src = self.table.item(row, 0).data(Qt.UserRole + 1)
        label = self.table.item(row, 2).text()
        for i, (lab, *_rest) in enumerate(self.result["outputs"]):
            if lab == src:
                self.out_combo.setCurrentIndex(i)
                break
        self.setCurrentIndex(2)
        self.sql.moveCursor(QTextCursor.Start)
        self._find(f"] {label}")

    def _find(self, text):
        if text and not self.sql.find(text):
            self.sql.moveCursor(QTextCursor.Start)
            self.sql.find(text)
        self.sql.centerCursor()
        self.sql.horizontalScrollBar().setValue(0)

    def _save_as(self):
        if not self.sql.toPlainText():
            return
        p, _ = QFileDialog.getSaveFileName(self, "Guardar SQL", str(self._current_output(1) or ""), "SQL (*.sql)")
        if p:
            Path(p).write_text(self.sql.toPlainText(), encoding="utf-8")

    def _open(self, path, folder=False):
        if not path:
            return
        p = Path(path)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(p.parent if folder else p)))


# -------------------------------------------------------------------------
# Ventana principal
# -------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.s = QSettings(ORG_NAME, "ddl-extractor")
        self.setWindowTitle(f"{APP_NAME} {__version__}")
        icon = resource_path("assets", "icon.png")
        if icon.exists():
            self.setWindowIcon(QIcon(str(icon)))
        self._jobs, self._job_seq = {}, 0

        self.conn = ConnectionPanel(self.s, self.run_job)
        self.validate_tab = ValidateTab(self.s, self.do_validate)
        self.extract_tab = ExtractTab(self.s, self.do_extract)
        self.conn.odoo_detected.connect(
            lambda v: self.extract_tab.opts.odoo_version.setCurrentText(str(v)))
        self.actions = QTabWidget()
        self.actions.addTab(self.validate_tab, "Validar DDL")
        self.actions.addTab(self.extract_tab, "Extraer desde modulos")
        self.results = ResultsPanel()

        split = QSplitter(Qt.Horizontal)
        split.addWidget(self.actions)
        split.addWidget(self.results)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 5)
        split.setSizes([520, 1000])

        central = QWidget()
        lay = QVBoxLayout(central)
        lay.addWidget(self.conn)
        lay.addWidget(split, 1)
        self.setCentralWidget(central)
        self.setStatusBar(QStatusBar())
        self._menus()

        geo = self.s.value("geometry")
        if geo:
            self.restoreGeometry(geo)
        else:
            self.resize(1500, 900)

    def _menus(self):
        m = self.menuBar().addMenu("&Archivo")
        a = QAction("Salir", self)
        a.setShortcut(QKeySequence.Quit)
        a.triggered.connect(self.close)
        m.addAction(a)
        h = self.menuBar().addMenu("A&yuda")
        for text, doc in (("Manual de usuario", "MANUAL_USUARIO.md"), ("Roadmap", "ROADMAP.md")):
            act = QAction(text, self)
            act.triggered.connect(lambda _=False, d=doc, t=text: self.show_doc(d, t))
            h.addAction(act)
        h.actions()[0].setShortcut(QKeySequence.HelpContents)
        about = QAction("Acerca de", self)
        about.triggered.connect(lambda: QMessageBox.about(
            self, APP_NAME,
            f"<b>{APP_NAME} {__version__}</b><br>Extractor y validador de DDL para Odoo / PostgreSQL."
            "<br><br><a href='https://github.com/JayLeo44/odoo-ddl-extractor'>"
            "github.com/JayLeo44/odoo-ddl-extractor</a><br>Licencia MIT"))
        h.addAction(about)

    def show_doc(self, name, title):
        p = resource_path("docs", name)
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.resize(900, 750)
        tb = QTextBrowser()
        tb.setOpenExternalLinks(True)
        if p.exists():
            tb.setSearchPaths([str(p.parent)])
            tb.setMarkdown(p.read_text(encoding="utf-8"))
        else:
            tb.setPlainText(f"No se encontro {p}")
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(dlg.reject)
        v = QVBoxLayout(dlg)
        v.addWidget(tb)
        v.addWidget(bb)
        dlg.exec()

    # ---- ejecucion en segundo plano -------------------------------------
    def run_job(self, fn, args, on_done, on_fail=None, busy=True):
        self._job_seq += 1
        jid = self._job_seq
        thread = QThread(self)
        worker = Worker(jid, fn, *args)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        # receptores = slots de QObjects del hilo principal -> conexion encolada (thread-safe)
        worker.log.connect(self.results.append_log)
        worker.done.connect(self._job_done)
        worker.failed.connect(self._job_failed)
        self._jobs[jid] = {"thread": thread, "worker": worker, "done": on_done,
                           "fail": on_fail or self._fail, "busy": busy}
        if busy:
            self._busy(True)
        thread.start()

    def _end_job(self, jid):
        job = self._jobs.pop(jid, None)
        if job:
            job["thread"].quit()
            job["thread"].wait(5000)
            job["worker"].deleteLater()
            job["thread"].deleteLater()
            if job["busy"]:
                self._busy(False)
        return job

    @Slot(int, object)
    def _job_done(self, jid, result):
        job = self._end_job(jid)
        if job:
            job["done"](result)

    @Slot(int, str)
    def _job_failed(self, jid, msg):
        job = self._end_job(jid)
        if job:
            job["fail"](msg)

    def _busy(self, on):
        for b in (self.validate_tab.run_btn, self.extract_tab.run_btn):
            b.setEnabled(not on)
        if on:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            self.statusBar().showMessage("Procesando...")
        else:
            QApplication.restoreOverrideCursor()

    def _fail(self, msg):
        self.results.append_log(f"[error] {msg}")
        self.statusBar().showMessage("Error", 8000)
        self.results.setCurrentIndex(4)
        QMessageBox.critical(self, "Error", msg.split("\n\n")[0][:1500])

    def _done(self, r):
        self.results.show_result(r)
        st = r["stats"]
        dry_err = sum(len(d["errors"]) for _, d in r["dry"])
        msg = (f"{st['ok']} ok, {st['warn']} warn, {st['skip']} omitidos, {st['error']} errores"
               + (f" | dry-run: {dry_err} con error" if r["dry"] else ""))
        for n in r["notes"]:
            self.results.append_log(f"[nota] {n}")
        if r.get("deploy"):
            msg += f" | script DBA: {Path(r['deploy'][0]).name}" + (
                f" (+{len(r['deploy']) - 1})" if len(r["deploy"]) > 1 else "")
        self.statusBar().showMessage(msg)

    def _check_conn(self, opts):
        c = self.conn.config()
        if not c["offline"] and not c["db"]:
            QMessageBox.warning(self, "Conexion", "Completa la conexion o marca 'Sin BD'.")
            return None
        if opts.get("commit") and QMessageBox.warning(
                self, "COMMIT", "El script generado terminara con COMMIT.\nContinuar?",
                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return None
        return c

    def do_validate(self):
        t = self.validate_tab
        files = t.files.paths()
        if not files:
            QMessageBox.information(self, "Validar", "Agrega al menos un archivo o carpeta.")
            return
        opts = t.opts.values()
        c = self._check_conn(opts)
        if c is None:
            return
        self.results.append_log("-" * 60)
        self.run_job(jobs.run_validate, (c, opts, files, t.out.text().strip() or None), self._done)

    def do_extract(self):
        t = self.extract_tab
        mods = t.mods.paths()
        if not mods:
            QMessageBox.information(self, "Extraer", "Agrega al menos un modulo.")
            return
        opts = t.opts.values()
        c = self._check_conn(opts)
        if c is None:
            return
        self.results.append_log("-" * 60)
        self.run_job(jobs.run_extract, (c, opts, mods), self._done)

    def closeEvent(self, e):
        self.s.setValue("geometry", self.saveGeometry())
        for job in list(self._jobs.values()):
            job["thread"].quit()
            job["thread"].wait(2000)
        super().closeEvent(e)


def main():
    if sys.platform == "win32":
        try:  # icono propio en la barra de tareas
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("odoo.ddl.extractor")
        except Exception:  # noqa: BLE001
            pass
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(ORG_NAME)
    app.setApplicationVersion(__version__)
    app.setStyle("Fusion")
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
