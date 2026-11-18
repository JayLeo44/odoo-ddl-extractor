# Roadmap — odoo-ddl-extractor / DDL Extractor

## Visión

Tener una sola herramienta, de escritorio y CLI, para llevar cambios de esquema de módulos Odoo a cualquier base PostgreSQL **sin sorpresas**: saber antes de ejecutar qué va a fallar, por qué y qué hacer, y generar siempre un script que se pueda revisar, repetir y revertir.

**Principios**

1. **Nunca modificar la base sin intención explícita.** Todo termina en `ROLLBACK` salvo que se pida lo contrario.
2. **No perder información.** Lo inválido se comenta con su motivo, nunca se borra.
3. **Idempotencia.** Ejecutar dos veces el mismo script no rompe nada (`IF NOT EXISTS` + guards).
4. **Una sola lógica.** La GUI y la CLI usan el mismo core (`ddl_common`, `validate_ddl`, `extract_ddl`).
5. **Multiplataforma.** Windows 11 y Ubuntu 22.04+ como plataformas de primera clase.

## Estado actual: v3.0.0

### Historial

| Versión | Contenido |
|---|---|
| v1 | Extractor CLI: AST de modelos → `CREATE TABLE` / `ADD COLUMN` / tablas M2M / FKs, comparando contra `information_schema` |
| v2 | Validador de DDL contra la base, guards en runtime, dry-run, `.env`, corrección de mapeos Odoo |
| **v3.0** | **GUI PySide6, instaladores Windows (Inno Setup) y Ubuntu (.deb), CLI unificada, script de despliegue para el DBA, manual de usuario** |

### Funcionalidad implementada

**Conexión y configuración**

- [x] Credenciales por flag, `.env` (`db_*`, `DDL_DB_*`, `PG*`), `odoo.conf` y variables de entorno, con prioridad definida
- [x] Perfiles de conexión guardados (GUI), contraseña opcional
- [x] Prueba de conexión con detección de versión de PostgreSQL y de Odoo
- [x] Modo sin BD (análisis estático + guards)

**Validador (`validate_ddl.py`)**

- [x] Lexer propio: comentarios `--` y `/* */` anidados, comillas, `$tag$`, y número de línea de origen
- [x] Interpreta `CREATE TABLE`, `ALTER TABLE` (varias acciones), `ADD COLUMN`, `ADD CONSTRAINT` (FK/PK/UNIQUE/CHECK) y `CREATE [UNIQUE] INDEX`
- [x] Catálogo cargado una sola vez desde `pg_catalog` + catálogo virtual con lo que crea el script
- [x] Reordena: tablas → columnas → índices → constraints → FKs
- [x] FKs inline separadas del `CREATE TABLE`
- [x] Chequeos de FK: tabla y columnas origen y destino, PK/UNIQUE, tipos, duplicado por nombre o por firma, huérfanos
- [x] `ADD COLUMN` derivados cuando la tabla existe pero le faltan columnas
- [x] `NOT NULL` sin `DEFAULT` sobre tabla con datos
- [x] Colisiones en `pg_class` (pkey, secuencias de `serial`, índices) y nombres de más de 63 caracteres
- [x] Guards `DO $$` con `to_regclass` / `pg_constraint`
- [x] `--fk-not-valid`, `--allow-drop`, `--lock-timeout`, `--commit`, `--strict`
- [x] Dry-run con `SAVEPOINT` por sentencia y `ROLLBACK` final
- [x] Reporte Markdown con relaciones inexistentes agregadas
- [x] **Script para el DBA (`.deploy.sql`)**: termina en `COMMIT`, idempotente, incluye lo existente en la base de validación, excluye y lista los errores, verifica la base destino (`--target-db`) y hace una verificación final que revierte todo si falta algún objeto (`--deploy-lenient` para COMMIT parcial)
- [x] El dry-run ejecuta el script de despliegue (el que va a producción)
- [x] Exit code útil para CI

**Extractor (`extract_ddl.py`)**

- [x] Detección de módulos (`__manifest__.py`), escaneo global de comodelos para `_table`
- [x] Fusión de clases del mismo modelo, `_inherit` como lista, `_table`, `_auto=False`, `AbstractModel`
- [x] Campos no almacenados: `compute`/`related` sin `store`, `Binary`/`Image` en adjunto, `company_dependent`
- [x] Mapeo por versión de Odoo: `translate` → `JSONB` (16+), `company_dependent` → `JSONB` (18+)
- [x] `Float` con `digits` → `NUMERIC`; `ondelete` por defecto igual que Odoo
- [x] Tabla puente M2M con su índice inverso
- [x] `_inherit` de una tabla inexistente → nota (no genera un `CREATE TABLE` falso)
- [x] Integración con el validador; `--raw` para el comportamiento v1

**GUI (`ddl_gui/`)**

- [x] Pestañas *Validar DDL* y *Extraer desde módulos*
- [x] Resultados con filtro, búsqueda y estado por color; doble clic salta al SQL
- [x] Pestaña de relaciones faltantes
- [x] Visor SQL con resaltado, buscar, copiar, guardar, abrir carpeta y reporte
- [x] Pestañas de dry-run y log
- [x] Trabajo en segundo plano (QThread) sin congelar la interfaz
- [x] Manual y roadmap integrados (Ayuda)
- [x] Soporta tema claro y oscuro del sistema (Fusion)

**Distribución**

- [x] PyInstaller *onedir* con dos ejecutables: `ddl-extractor` (GUI) y `ddl-extractor-cli`
- [x] Instalador Windows (Inno Setup): por usuario sin admin, accesos directos, PATH opcional, desinstalador
- [x] ZIP portable para Windows
- [x] `.deb` para Ubuntu (dependencias Qt declaradas, `.desktop`, icono) + `.tar.gz` portable

## Próximas versiones

Prioridad: **P1** imprescindible, **P2** importante, **P3** deseable.

### v3.1 — Calidad y release automatizado

| # | Ítem | Prioridad | Notas |
|---|---|---|---|
| 3.1.1 | Tests automatizados `pytest` (lexer, parser, validador, extractor) | P1 | Fixtures con DDL reales (DBeaver, pg_dump) |
| 3.1.2 | Tests de integración contra PostgreSQL 13–17 en contenedor | P1 | `testcontainers` o servicio en CI |
| 3.1.3 | GitHub Actions: lint + tests + build en `windows-latest` y `ubuntu-22.04` | P1 | Publicar `.exe`, `.zip`, `.deb` y `.tar.gz` en *Releases* al crear un tag `v*` |
| 3.1.4 | Contraseñas en el llavero del SO (`keyring`: Credential Manager / Secret Service) | P1 | Reemplaza el texto plano de los perfiles |
| 3.1.5 | Firma de código del instalador de Windows | P2 | Evita la advertencia de SmartScreen. Requiere certificado |
| 3.1.6 | Textos con tildes e i18n (es/en) en GUI y CLI | P2 | `QTranslator` + `.ts` |
| 3.1.7 | Cancelar una ejecución en curso | P2 | `pg_cancel_backend` + bandera en el worker |
| 3.1.8 | Barra de progreso por fase (catálogo, validación, dry-run) | P3 | |

### v3.2 — Validador más completo

| # | Ítem | Prioridad |
|---|---|---|
| 3.2.1 | `ALTER COLUMN TYPE / SET NOT NULL / DROP NOT NULL / SET DEFAULT`, con chequeo de datos (conversiones, nulos) | P1 |
| 3.2.2 | Corrección automática de `NOT NULL` en tablas con datos: `ADD COLUMN NULL` → `UPDATE` con default → `SET NOT NULL` | P1 |
| 3.2.3 | FKs en dos pasos: `NOT VALID` + `VALIDATE CONSTRAINT` (menos bloqueo en tablas grandes) | P2 |
| 3.2.4 | Interpretar `CREATE SEQUENCE`, `CREATE VIEW`, `COMMENT ON`, `ALTER SEQUENCE OWNED BY`, `SET DEFAULT nextval(...)` | P2 |
| 3.2.5 | Varios esquemas en un mismo script | P2 |
| 3.2.6 | Detectar ciclos de FKs y FKs a tablas particionadas | P3 |
| 3.2.7 | Estimar el costo de lock por sentencia (tamaño de tabla, `ACCESS EXCLUSIVE`) | P3 |
| 3.2.8 | Reglas configurables (ignorar tablas, severidades) por archivo `.ddlrc` | P3 |

### v3.3 — Extractor más fiel a Odoo

| # | Ítem | Prioridad |
|---|---|---|
| 3.3.1 | `_sql_constraints` → `UNIQUE` / `CHECK` | P1 |
| 3.3.2 | `index=True` / `index='btree_not_null'` / `'trigram'` → `CREATE INDEX` | P1 |
| 3.3.3 | Odoo 18+: `models.Constraint`, `models.Index`, `models.UniqueIndex` | P1 |
| 3.3.4 | `_inherits` (delegación): FK al padre `ON DELETE CASCADE` | P2 |
| 3.3.5 | `default=` literal → `DEFAULT` en `ADD COLUMN NOT NULL` | P2 |
| 3.3.6 | `Selection` con claves enteras → `INTEGER` | P3 |
| 3.3.7 | Campos eliminados en el código → `DROP COLUMN` sugerido (comentado) | P2 |
| 3.3.8 | Diff entre dos commits Git del módulo, sin base | P2 |

### v4.0 — Gestión de cambios

| # | Ítem | Prioridad |
|---|---|---|
| 4.0.1 | Comparar dos bases (origen ↔ destino) y generar el DDL de diferencias | P1 |
| 4.0.2 | Aplicar desde la GUI con doble confirmación, chequeo previo de respaldo y log de ejecución | P2 |
| 4.0.3 | Historial de validaciones y ejecuciones (SQLite local) | P2 |
| 4.0.4 | Reporte HTML/PDF exportable | P2 |
| 4.0.5 | Script de rollback para el DBA: el `.deploy.sql` registra en una tabla de auditoría lo que realmente creó, y el `.rollback.sql` revierte solo eso | P2 |
| 4.0.8 | Hash (SHA-256) del `.deploy.sql` en el reporte, para detectar si alguien lo editó después de validarlo | P2 |
| 4.0.6 | AppImage / Flatpak; repositorio apt y paquete `winget` | P3 |
| 4.0.7 | Actualización automática (chequeo de *Releases*) | P3 |

## Decisiones técnicas

| Decisión | Alternativas | Motivo |
|---|---|---|
| **PySide6 (Qt 6)** para la GUI | Tkinter, web local (FastAPI) | Aspecto nativo en Windows y Ubuntu, tablas y editor potentes, tema oscuro, licencia LGPL |
| **PyInstaller onedir** | onefile, Nuitka, cx_Freeze | Arranque rápido (onefile se descomprime en cada ejecución), menos falsos positivos de antivirus, soporte maduro para PySide6 |
| **Inno Setup** | WiX/MSI, NSIS | Simple, instalación por usuario sin admin, español incluido |
| **.deb + .tar.gz** | Snap, Flatpak, AppImage | Instalación directa con `apt` en Ubuntu. AppImage/Flatpak quedan para v4 |
| **Ubuntu 22.04 como base de compilación** | 20.04 | Las ruedas de PySide6 requieren glibc ≥ 2.34. Lo compilado en 22.04 funciona en 22.04 y posteriores |
| **Python 3.10 como mínimo** | 3.12+ | Compatible con los entornos de Odoo 16–18. La 3.10 ya no recibe parches de seguridad (oct-2026): pasar el build a 3.12 en v3.1 manteniendo el código compatible con 3.10 |
| **Parser propio en vez de `sqlglot`/`pglast`** | `pglast` (parser real de PG) | Sin dependencias nativas y control total del rewrite. Evaluar `pglast` en v3.2 si crece la cantidad de sentencias soportadas |
| **Guards `DO $$` en vez de abortar** | Fallar rápido | Un script se valida en una base y se ejecuta en otra: es mejor degradar con WARNING que perder toda la transacción |
| **Despliegue con verificación final que revierte todo** | Cortar en el primer error | Los guards recolectan todos los faltantes y la verificación final aborta con la lista completa: el DBA recibe un diagnóstico entero en una sola corrida, sin aplicar nada a medias |
| **El `.deploy.sql` incluye lo OMITIDO** | Solo lo nuevo | La base de validación (test) puede diferir de producción. Al ser idempotente, incluirlo no tiene costo y evita huecos |

## Matriz de compatibilidad

| Componente | Soportado |
|---|---|
| Sistema (instalador) | Windows 10/11 x64, Ubuntu 22.04 / 24.04 x64 |
| Python (código fuente) | 3.10 – 3.13 |
| PostgreSQL | 12 – 17 (probado en 16) |
| Odoo (extractor) | 14 – 19 (`--odoo-version`) |

## Riesgos conocidos

- **Parser heurístico.** Sentencias DDL poco comunes caen en *"sentencia no analizada"* y se emiten tal cual. El dry-run es la red de seguridad.
- **Chequeo de huérfanos en tablas grandes.** Puede tardar. Hay timeout de 60 s y se puede desactivar.
- **Contraseña en texto plano en perfiles.** Opcional y avisado. Se resuelve en 3.1.4.
- **SmartScreen** en Windows hasta firmar el instalador (3.1.5).
