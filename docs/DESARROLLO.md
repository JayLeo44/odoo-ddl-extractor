# Guía de desarrollo y empaquetado

## Arquitectura

```
                ┌─────────────── ddl_gui/ (PySide6) ───────────────┐
                │ app.py      ventana, paneles, QThread workers    │
                │ jobs.py     run_validate / run_extract (sin Qt)  │
                │ highlighter.py                                   │
                └──────────────┬───────────────────────────────────┘
cli.py ─────────┐              │
validate_ddl.py ┼──────────────┤  mismo core
extract_ddl.py ─┘              ▼
        ┌──────────────────────────────────────────────────────────┐
        │ ddl_common.py   config (.env/odoo.conf/flags), conexión,  │
        │                 Catalog (pg_catalog + catálogo virtual)   │
        │ validate_ddl.py lexer → parser → Validator → render       │
        │ extract_ddl.py  AST de modelos → DDL crudo → validador    │
        └──────────────────────────────────────────────────────────┘
```

### Flujo del validador

1. `split_sql`: un lexer que respeta comentarios, comillas y `$tag$` corta el texto en sentencias y guarda el archivo:línea de cada una.
2. `parse_statement`: cada sentencia se convierte en uno o más `Item` (`table`, `column`, `index`, `constraint`, `fk`, `other`). Las FKs inline se separan.
3. `Validator.run`: agrupa por tipo, en orden tabla → columna → índice → constraint → FK → otros, valida contra `Catalog` y registra en el catálogo virtual lo que el script crea.
4. `render`: arma el script con `BEGIN`, `lock_timeout`, secciones, guards `DO $$` y lo inválido comentado, y además la lista de sentencias ejecutables para el dry-run.
5. `render_deploy`: genera el `.deploy.sql` (incluye OK/WARN/OMITIDO, excluye ERROR, agrega la verificación de base destino y la verificación final que revierte todo si falta algún objeto, y termina en `COMMIT`).
6. `dry_run`: ejecuta cada sentencia del script de despliegue con `SAVEPOINT` y hace `ROLLBACK` al final.
7. `render_report`: genera el reporte Markdown.

`write_outputs()` orquesta los pasos 5 a 7 y lo usan por igual la CLI, el extractor y la GUI.

### Estados de un `Item`

Cada mensaje tiene una severidad `info` < `warn` < `skip` < `error`. El estado del ítem es la severidad máxima, sin contar `info`.

## Entorno de desarrollo

### Windows

```powershell
cd odoo-ddl-extractor
py -3.10 -m venv .venv
.\.venv\Scripts\Activate.ps1          # o usar .\.venv\Scripts\python.exe
python -m pip install -r requirements.txt -r requirements-gui.txt
python run_gui.py                     # GUI
python validate_ddl.py --help         # CLI
```

### Ubuntu

```bash
sudo apt install python3-venv libxcb-cursor0
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-gui.txt
python run_gui.py
```

### Base de pruebas local

```bash
docker run -d --name pg-ddl -e POSTGRES_USER=odoo -e POSTGRES_PASSWORD=odoo -p 5433:5432 postgres:16
```

## Estructura del repositorio

```
cli.py                    CLI unificada (validate / extract)
run_gui.py                lanzador de la GUI (entrypoint de PyInstaller)
ddl_common.py             config, conexión, Catalog, helpers
validate_ddl.py           validador
extract_ddl.py            extractor
ddl_gui/                  GUI PySide6
assets/                   icon.png, icon_256.png, icon.ico
docs/                     MANUAL_USUARIO.md, ROADMAP.md, DESARROLLO.md
packaging/
  ddl_extractor.spec      PyInstaller (GUI + CLI en un onedir)
  windows/build.ps1       build + ZIP + instalador
  windows/installer.iss   Inno Setup
  linux/build_deb.sh      build + .deb + .tar.gz
requirements*.txt         runtime / gui / build
```

## Empaquetado

PyInstaller **no hace compilación cruzada**: cada instalador se genera en su sistema.

### Windows (`.exe` instalador + `.zip` portable)

Requisitos: Python 3.10 (`py -3.10`) e Inno Setup 6 (`winget install -e --id JRSoftware.InnoSetup`).

```powershell
powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1
```

Resultado en `dist\`:

- `ddl-extractor\` (carpeta onedir)
- `DDL-Extractor-<v>-win64-portable.zip`
- `DDL-Extractor-<v>-win64-setup.exe`

### Ubuntu (`.deb` + `.tar.gz`)

Compila en **Ubuntu 22.04** para que el paquete sirva en 22.04 y 24.04. En 24.04, el `.deb` solo funciona en 24.04 o posterior.

```bash
sudo apt install python3.10-venv dpkg-dev binutils
bash packaging/linux/build_deb.sh
sudo apt install ./dist/ddl-extractor_<v>_amd64.deb
```

Con Docker, desde cualquier Linux:

```bash
docker run --rm -v "$PWD":/src -w /src ubuntu:22.04 bash -c \
  "apt-get update && apt-get install -y python3-venv python3-pip dpkg-dev binutils && bash packaging/linux/build_deb.sh"
```

### Tamaño

PyInstaller incluye solo los módulos Qt que se importan, y el spec además excluye WebEngine, QML, Multimedia y otros. Referencia: unos 90–110 MB instalado y unos 40–50 MB comprimido.

## Versionado y release

- La versión se define en un solo lugar: `ddl_gui/__init__.py` (`__version__`). Los scripts de build la leen de ahí.
- SemVer: `MAYOR.MENOR.PARCHE`.

**Checklist de release**

1. Actualizar `__version__` y el historial en `docs/ROADMAP.md`.
2. Probar el validador contra una base real con `--dry-run`.
3. Build en Windows y en Ubuntu 22.04.
4. Instalar cada paquete en una máquina limpia, abrir la GUI y correr una validación.
5. Crear el tag `vX.Y.Z` y publicar los cuatro artefactos en GitHub Releases. Se automatiza en el ítem 3.1.3 del roadmap.

## Convenciones

- El core (`ddl_common`, `validate_ddl`, `extract_ddl`) **no importa Qt**. La GUI llama al core solo a través de `ddl_gui/jobs.py`.
- Los errores de configuración del core usan `sys.exit(msg)`. La GUI captura `SystemExit` y lo muestra.
- Los resultados que vuelven de un worker a la GUI se entregan con señales que reciben **slots de QObjects del hilo principal**, nunca lambdas sueltas (con una lambda, el callback correría en el hilo del worker).
- Mensajes del core sin tildes, para que se lean bien en consolas Windows con cualquier codepage. La documentación sí lleva tildes.
