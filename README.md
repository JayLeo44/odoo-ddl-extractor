# odoo-ddl-extractor

Extractor **y validador** de DDL para modulos Odoo contra PostgreSQL, con interfaz grafica (**DDL Extractor**) para Windows 11 y Ubuntu, y CLI.

| Documento | Para quien |
|---|---|
| [Manual de usuario](docs/MANUAL_USUARIO.md) | Instalar y usar la app / CLI |
| [Roadmap](docs/ROADMAP.md) | Funcionalidad actual, proximas versiones, decisiones tecnicas |
| [Desarrollo y empaquetado](docs/DESARROLLO.md) | Arquitectura, entorno dev, builds Windows/Ubuntu, release |

## Instalacion rapida

- **Windows 11:** `DDL-Extractor-<v>-win64-setup.exe` (o el `.zip` portable). Generarlo: `packaging\windows\build.ps1`.
- **Ubuntu 22.04+:** `sudo apt install ./ddl-extractor_<v>_amd64.deb`. Generarlo: `packaging/linux/build_deb.sh`.
- **Desde codigo:** `pip install -r requirements.txt -r requirements-gui.txt` y `python run_gui.py`.

El paquete instala `ddl-extractor` (GUI) y `ddl-extractor-cli` (`validate` / `extract`).

- `extract_ddl.py` parsea los modelos Python (via AST, sin levantar Odoo), compara contra el esquema real y genera el DDL de lo que falta: `CREATE TABLE`, `ADD COLUMN`, tablas puente `Many2many` y `FOREIGN KEY` con su `ON DELETE` real.
- `validate_ddl.py` valida **cualquier** script DDL (del extractor, DBeaver, pg_dump, a mano) contra una base concreta y genera una version segura para ejecutar en `BEGIN; ... ROLLBACK;`.

Desde v2 el extractor pasa su salida por el validador automaticamente.

## Que resuelve

Un DDL extraido de una base (o generado desde el codigo) casi nunca corre limpio en otra: faltan tablas referenciadas por FKs (modulos no instalados), hay tablas que ya existen, columnas `NOT NULL` sobre tablas con datos, FKs con filas huerfanas, nombres de constraint/indice repetidos... Un solo error aborta la transaccion completa.

El validador detecta todo eso **antes** de ejecutar y genera:

- `<nombre>.validated.sql`: el script de **revisión**, anotado y terminado en `ROLLBACK`.
- `<nombre>.deploy.sql`: el script para el **DBA**, limpio, terminado en `COMMIT`, idempotente, con verificación de base destino (`--target-db`) y verificación final que revierte todo si falta algún objeto. Ver [Manual §9](docs/MANUAL_USUARIO.md#9-script-para-el-dba-deploysql).
- `<nombre>.report.md`: el reporte.

Mitigaciones que aplica:

| Mitigacion | Como |
|---|---|
| Referencias hacia adelante | Reordena: tablas -> columnas -> indices/constraints -> FKs |
| FK a tabla inexistente | Se **comenta** con el motivo (no aborta) y se lista en el reporte |
| FK inline (`REFERENCES` en `CREATE TABLE`) | Se separa en `ALTER TABLE` para poder omitirla sin perder la tabla |
| Tabla/columna/indice ya existe | `IF NOT EXISTS` + marcado como omitido |
| Tabla existe pero le faltan columnas | Genera `ADD COLUMN IF NOT EXISTS` derivados |
| BD de ejecucion distinta a la validada | FKs y constraints en `DO $$ ... $$` con chequeo `to_regclass`/`pg_constraint` en runtime: `RAISE WARNING` y sigue |
| Filas huerfanas | Se detectan; FK comentada o `NOT VALID` con `--fk-not-valid` |
| `NOT NULL` sin `DEFAULT` en tabla con datos | Error con sugerencia |
| Locks largos en tablas calientes | `SET LOCAL lock_timeout = '15s'` |
| `DROP`/`TRUNCATE`/`DELETE` | Comentados salvo `--allow-drop` |
| Lo que el analisis estatico no ve | `--dry-run`: ejecuta todo con `SAVEPOINT` por sentencia y `ROLLBACK` final |

## Instalacion (solo CLI, desde codigo)

```bash
python -m venv .venv
pip install -r requirements.txt
```

## Credenciales

Prioridad: **flag > `--odoo-conf` > `--env` > variables `DDL_DB_*` > default (`localhost:5432`, `odoo/odoo`)**.

`.env` (ver `.env.example`), acepta `db_*`, `DDL_DB_*` o `PG*`:

```ini
db_host=192.168.3.150
db_port=5433
db_user=odoo
db_password=secreto
db_name=mi_base
```

## Validar DDL existentes

```bash
# un archivo -> extract.validated.sql + extract.validated.report.md
python validate_ddl.py extract.sql --env .env

# todos los .sql de una carpeta, validados juntos (referencias cruzadas entre archivos)
python validate_ddl.py ddls/ --env .env -o ddls/validado.sql

# validacion estatica + ejecucion real con ROLLBACK
python validate_ddl.py ddls/ --env .env --dry-run

# sin BD: solo parsea, reordena y envuelve todo en guards de runtime
python validate_ddl.py extract.sql --no-db
```

Opciones: `--target-db <bd_prod>`, `--deploy-lenient`, `--no-deploy`, `--commit` (el script de revisión termina con `COMMIT`), `--fk-not-valid`, `--no-data-checks`, `--no-guard`, `--allow-drop`, `--lock-timeout 30s`, `--schema otro`, `--strict` (exit 1 tambien con warnings).

Exit code `1` si hay errores: sirve como gate en CI.

## Extraer DDL desde el codigo

```bash
# modulo(s) explicito(s)
python extract_ddl.py mod_a mod_b --env .env

# autodetectar modulos del repo (carpetas con __manifest__.py)
python extract_ddl.py --repo /ruta/al/repo --odoo-conf odoo17_test.conf

# extraer + ejecutar en dry-run
python extract_ddl.py mod_a --env .env --dry-run

# sin BD (todo nuevo; _inherit va por ADD COLUMN con guard)
python extract_ddl.py mod_a --no-db

# DDL crudo sin validar (comportamiento v1)
python extract_ddl.py mod_a --env .env --raw
```

Como la comparacion es contra el esquema **real**, apunta a una base que represente el estado **antes** de tus cambios (staging, dump previo) para ver solo lo nuevo.

### Salida por modulo

- `<modulo>/ddl/extract_<ts>.sql` — DDL validado (o `_raw.sql` con `--raw`).
- `<modulo>/ddl/extract_<ts>.report.md` — reporte: relaciones inexistentes, errores, omitidos, dry-run.
- `<modulo>/.ddl_snapshot.json` — snapshot de campos por modelo.

Recomendado excluir `ddl/` y `.ddl_snapshot.json` del control de versiones.

## Mapeo Odoo -> PostgreSQL

- `Many2one`, `create_uid`, `write_uid` -> `BIGINT` (convencion del proyecto; `--id-type INTEGER` para int4 estandar).
- `ondelete` por defecto igual que Odoo: `restrict` si `required=True`, si no `set null`.
- `Float` con `digits` -> `NUMERIC`; sin `digits` -> `DOUBLE PRECISION` (igual que Odoo).
- `Char`/`Text`/`Html` con `translate` -> `JSONB` en Odoo >= 16 (`--odoo-version`).
- `Binary`/`Image` -> sin columna (van a `ir_attachment`) salvo `attachment=False` -> `BYTEA`.
- Campos `compute`/`related` sin `store=True` -> sin columna. `company_dependent` -> sin columna (< 18) o `JSONB` (>= 18).
- `Many2many` -> tabla puente con la convencion de nombres de Odoo + indice inverso.
- `One2many` -> se ignora.
- `AbstractModel` y `_auto = False` -> sin tabla. `_table` se respeta. `_inherit` en lista toma el primer modelo.
- Varias clases que extienden el mismo modelo en un modulo se fusionan.
- `_inherit` de una tabla que no existe en la BD (modulo base no instalado) -> se omite y se reporta en lugar de generar un `CREATE TABLE` incorrecto.

## Archivos

| archivo | rol |
|---|---|
| `extract_ddl.py` | Extractor (codigo Odoo -> DDL) |
| `validate_ddl.py` | Validador/saneador de DDL contra una BD |
| `ddl_common.py` | Config (.env/odoo.conf), catalogo del esquema, helpers |
| `ddl_gui/` | Interfaz grafica PySide6 |
| `run_gui.py` / `cli.py` | Entrypoints GUI / CLI unificada |
| `packaging/` | PyInstaller, Inno Setup (Windows), .deb (Ubuntu) |
| `docs/` | Manual, roadmap, guia de desarrollo |

## Licencia

MIT. Ver [LICENSE](LICENSE).
