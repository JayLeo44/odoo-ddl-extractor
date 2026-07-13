# odoo-ddl-extractor

Extractor de DDL para modelos y campos nuevos en modulos Odoo, comparando contra PostgreSQL.

Parsea los modelos Python de cada modulo Odoo (via AST, sin necesidad de levantar el framework de Odoo), compara contra el esquema real de PostgreSQL (`information_schema`) y genera el DDL de todo lo que existe en el codigo pero todavia no en la base de datos: `CREATE TABLE`, `ALTER TABLE ADD COLUMN`, tablas puente para `Many2many` (respetando la convencion de nombres de Odoo) y `FOREIGN KEY` con su `ON DELETE` real.

## Que resuelve

Cuando se agregan modelos o campos nuevos en un modulo custom, hay que aplicar el DDL correspondiente en la base de datos (via `-u` o manualmente). En proyectos con muchos modulos y varios desarrolladores, comparar a mano el codigo contra el esquema real antes de un deploy es tedioso y propenso a error. Este script automatiza esa comparacion y deja un artefacto `.sql` listo para revisar.

## Como detecta "lo nuevo"

La comparacion es siempre contra el esquema **real** de la base de datos indicada (no contra un snapshot). Por eso:

- Si la base ya tiene el modulo actualizado (le corriste `-u`), no va a reportar nada nuevo, aunque el codigo tenga cambios recientes.
- Para ver el DDL completo de lo que agregaste, apunta a una base que represente el estado **antes** de tus cambios (staging, un dump previo, etc.), o usa `--no-db` para un dump completo sin comparar.

## Instalacion

```bash
pip install psycopg2-binary --break-system-packages
```

## Uso

El script se ejecuta a nivel de repositorio (fuera de los modulos, uno por repo), no dentro de cada modulo.

### 1. Comparar contra una BD donde los cambios NO estan aplicados (extrae solo lo nuevo)

```bash
python extract_ddl.py farmamia_slimstock_integration --odoo-conf /ruta/a/odoo17_test.conf
```

Usa el `.conf` apuntando a una base "vieja" (sin el `-u` corrido). Solo lista columnas/tablas/FKs que faltan ahi.

### 2. Mismo caso pero con credenciales sueltas (sin `.conf`)

```bash
python extract_ddl.py farmamia_slimstock_integration --db mi_base_vieja --host 192.168.112.116 --port 5433 --user odoo --password farma_test17
```

### 3. Varios modulos explicitos en una sola corrida

```bash
python extract_ddl.py mod_a mod_b mod_c --odoo-conf odoo17_test.conf
```

### 4. Autodetectar TODOS los modulos del repo (parado en la raiz, sin pasar nombre)

```bash
python extract_ddl.py --odoo-conf odoo17_test.conf
```

Busca subcarpetas de primer nivel que tengan `__manifest__.py` (o `__openerp__.py`).

### 5. Autodetectar modulos en una ruta especifica del repo (no la actual)

```bash
python extract_ddl.py --repo /ruta/al/repo --odoo-conf odoo17_test.conf
```

### 6. Override puntual de un dato del `.conf`

```bash
python extract_ddl.py farmamia_slimstock_integration --odoo-conf odoo17_test.conf --host localhost
```

El flag de linea de comandos siempre gana sobre el `.conf`.

### 7. Modo sin BD - dump completo del modelo tal cual esta en codigo

```bash
python extract_ddl.py farmamia_slimstock_integration --no-db
```

Asume que ninguna tabla existe. Util para inspeccion rapida; no filtra "solo lo nuevo".

### 8. Variables de entorno en vez de flags (util en CI)

```bash
export DDL_DB_HOST=192.168.112.116
export DDL_DB_PORT=5433
export DDL_DB_NAME=farmamia_cresio_20251002
export DDL_DB_USER=odoo
export DDL_DB_PASSWORD=farma_test17
python extract_ddl.py farmamia_slimstock_integration
```

## Prioridad de configuracion

Si se combinan varias fuentes (flags, `--odoo-conf`, variables de entorno), el orden de resolucion es:

**flag de linea de comandos > `--odoo-conf` > variable de entorno > default duro (`localhost:5432/odoo`)**

## Que genera

Por cada modulo procesado:

- `<modulo>/ddl/extract_<YYYYmmdd_HHMMSS>.sql` — el DDL generado en esa corrida.
- `<modulo>/.ddl_snapshot.json` — snapshot historico de los campos conocidos por modelo.

Se recomienda excluir ambos del control de versiones (ver `.gitignore`), ya que se regeneran en cada ejecucion.

## Detalles de mapeo Odoo -> PostgreSQL

- `Many2one` y `create_uid` / `write_uid` -> `BIGINT` (ajustable via `ID_COLUMN_TYPE` en el script si tu proyecto usa `int4` estandar).
- `Float` sin `digits` -> `DOUBLE PRECISION`; con `digits=(p, s)` -> `NUMERIC(p,s)`.
- `Many2many` -> genera la tabla puente (`CREATE TABLE`) siguiendo la convencion de nombres de Odoo (`relation`/`column1`/`column2` explicitos o calculados por orden alfabetico de las tablas), incluyendo el caso auto-referente. Los `Many2many` computados sin `store=True` se ignoran (no tienen tabla en BD).
- `Many2one` genera ademas su `ALTER TABLE ADD CONSTRAINT ... FOREIGN KEY ... ON DELETE <ondelete real del campo>` (default `SET NULL` si el campo no especifica `ondelete`).
- `One2many` se ignora (es el inverso de un `Many2one`, sin representacion propia en BD).

## Licencia

MIT. Ver [LICENSE](LICENSE).
