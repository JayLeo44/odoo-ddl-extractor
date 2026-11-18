# Manual de usuario — DDL Extractor

Versión 3.0 · Windows 11 y Ubuntu 22.04+

## 1. Qué hace

DDL Extractor tiene dos funciones:

- **Validar DDL.** Toma uno o varios scripts `.sql` (extraídos de DBeaver, `pg_dump`, del propio extractor o escritos a mano) y los compara contra una base PostgreSQL real. Detecta lo que haría fallar el script y genera dos archivos:
  - un **script de revisión** (`.validated.sql`), con cada sentencia anotada y que termina en `ROLLBACK`;
  - un **script para el DBA** (`.deploy.sql`), limpio, que termina en `COMMIT` y está listo para ejecutar en producción (ver §9).
- **Extraer DDL desde módulos Odoo.** Lee los modelos Python de uno o varios módulos y genera el DDL de las tablas y columnas que todavía no existen en la base. Ese DDL pasa por el mismo validador.

Nunca modifica la base. Lo único que ejecuta es el **dry-run**, que siempre termina en `ROLLBACK`.

## 2. Requisitos

| | Windows | Ubuntu |
|---|---|---|
| Sistema | Windows 10/11 x64 | Ubuntu 22.04, 24.04 o posterior (x64) |
| Red | Acceso al puerto de PostgreSQL (ej. `192.168.3.150:5433`) | Igual |
| Permisos en la BD | Usuario con lectura del catálogo. Para el dry-run, permiso de crear objetos (se revierten) | Igual |
| Python | No hace falta con el instalador | No hace falta con el `.deb` |

## 3. Instalación

### 3.1 Windows 11

1. Descarga `DDL-Extractor-<versión>-win64-setup.exe`.
2. Ejecútalo. Si aparece **"Windows protegió su PC"** (SmartScreen, porque el instalador no está firmado), haz clic en **Más información → Ejecutar de todas formas**.
3. Por defecto se instala solo para tu usuario y no pide administrador. En el asistente puedes elegir instalar para todos los usuarios.
4. Opciones del asistente:
   - **Acceso directo en el escritorio.**
   - **Agregar `ddl-extractor-cli` al PATH**, si quieres usar la línea de comandos desde cualquier terminal.
5. Abre **DDL Extractor** desde el menú Inicio.

**Versión portable:** descomprime `DDL-Extractor-<versión>-win64-portable.zip` en cualquier carpeta y ejecuta `ddl-extractor.exe`.

### 3.2 Ubuntu

```bash
sudo apt install ./ddl-extractor_<versión>_amd64.deb
```

`apt` instala solo las librerías gráficas que Qt necesita. Después, abre **DDL Extractor** desde el menú de aplicaciones o ejecuta `ddl-extractor` en una terminal.

**Versión portable** (otras distribuciones, sin root):

```bash
tar xzf ddl-extractor-<versión>-linux-amd64.tar.gz
./ddl-extractor/ddl-extractor
```

### 3.3 Desde el código fuente (cualquier sistema)

Ver [DESARROLLO.md](DESARROLLO.md), sección "Entorno de desarrollo".

## 4. La ventana principal

```
┌──────────────────────── Conexión a PostgreSQL ────────────────────────┐
│ Perfil [▼]  Guardar · Eliminar        Cargar .env · Cargar odoo.conf   │
│ Host · Puerto · Base · Usuario · Contraseña · Esquema                  │
│ [ ] Sin BD   [ ] Guardar contraseña          Probar conexión  estado   │
├──────── Acción ────────────┬──────────────── Resultados ──────────────┤
│ Validar DDL | Extraer      │ Resultados | Relaciones faltantes | SQL  │
│  lista de archivos/módulos │ generado | Dry-run | Log                 │
│  opciones                  │                                          │
│  [ Validar ]               │                                          │
└────────────────────────────┴──────────────────────────────────────────┘
```

## 5. Configurar la conexión

Tienes tres formas de cargar los datos:

- **Cargar .env:** lee un archivo con `db_host`, `db_port`, `db_user`, `db_password` y `db_name`. También acepta las variantes `DDL_DB_*` y `PG*`.
  ```ini
  db_host=192.168.3.150
  db_port=5433
  db_user=odoo
  db_password=********
  db_name=test_cresio_20260202
  ```
- **Cargar odoo.conf:** lee la sección `[options]` (`db_host`, `db_port`, `db_user`, `db_password`, `db_name`).
- **A mano**, en los campos.

Después pulsa **Probar conexión**. Si todo está bien, verás la versión de PostgreSQL, la cantidad de tablas y, si la base es de Odoo, su versión. La versión de Odoo detectada se copia sola a la pestaña *Extraer*.

**Perfiles.** **Guardar perfil** guarda la conexión con un nombre para reutilizarla. La contraseña se guarda solo si marcas *Guardar contraseña en el perfil*, y queda en texto plano en tu configuración de usuario (ver §11). El último perfil usado se recuerda.

**Sin BD.** Marca *Sin BD* si no tienes acceso a la base. El análisis queda en lo estático: orden de sentencias, sintaxis y `IF NOT EXISTS`. Todas las referencias se protegen con *guards* que se evalúan cuando el script corre (ver §8.3). En este modo no hay dry-run.

## 6. Validar DDL

1. Ve a la pestaña **Validar DDL**.
2. Agrega archivos con **Agregar .sql**, o una carpeta completa con **Agregar carpeta** (toma todos los `.sql` de forma recursiva).
   - Todos los archivos se validan **juntos y en orden**, como un solo script. Así, si `b.sql` referencia una tabla que crea `a.sql`, la referencia se reconoce.
3. **Salida:** es opcional. Si la dejas vacía se genera `<archivo>.validated.sql` (o `validated_<fecha>.sql` si hay varios archivos) junto al primer archivo. Al lado siempre quedan el `.deploy.sql` (script para el DBA) y el `.report.md`.
4. Ajusta las opciones (ver §6.1) y pulsa **Validar**.

### 6.1 Opciones

| Opción | Por defecto | Qué hace |
|---|---|---|
| **Dry-run** | ✔ | Ejecuta el script resultante en la base, sentencia por sentencia, con `SAVEPOINT`, y al final hace **ROLLBACK**. Detecta lo que el análisis estático no ve: permisos, locks, sintaxis poco común. |
| **Guards en runtime** | ✔ | Envuelve las FKs y constraints en bloques `DO $$ … $$`. Si en la base donde se ejecuta falta la tabla, muestra un `WARNING` y sigue, en vez de abortar toda la transacción. |
| **Chequear datos** | ✔ | Cuenta filas huérfanas antes de crear una FK y revisa si la tabla tiene datos antes de agregar una columna `NOT NULL`. En tablas grandes puede tardar. Cada consulta tiene un límite de 60 s. |
| **FK con huérfanos → NOT VALID** | ✘ | Si hay huérfanos, crea la FK como `NOT VALID` (no verifica las filas existentes) en lugar de comentarla. |
| **Permitir DROP/TRUNCATE/DELETE** | ✘ | Si no está marcada, las sentencias destructivas quedan comentadas. |
| **Terminar con COMMIT** | ✘ | El script termina con `COMMIT` en vez de `ROLLBACK`. Pide confirmación. |
| **lock_timeout** | `15s` | Agrega `SET LOCAL lock_timeout`, para que el script no se quede bloqueado esperando una tabla en uso. Déjalo vacío para no agregarlo. |
| **Generar script (DDL validados)** | ✔ | Genera además `<nombre>.deploy.sql`: los DDL validados, listos para ejecutar en producción (ver §9). |
| **BD destino** | vacío | Nombre de la base de producción. Si se indica, el `.deploy.sql` **se niega a correr en otra base**. |
| **Si falta algún objeto al final, revertir TODO (recomendado)** | ✔ | El `.deploy.sql` verifica al final que cada objeto esperado exista. **Marcado:** si falta alguno, hace ROLLBACK completo y la base queda como estaba. **Desmarcado** (el texto se pone en rojo): hace COMMIT de lo que se pudo aplicar y solo avisa lo que faltó, es decir, un **despliegue parcial**. |

## 7. Extraer DDL desde módulos Odoo

1. Ve a la pestaña **Extraer desde módulos**.
2. **Agregar módulo** agrega la carpeta de un módulo (debe tener `__manifest__.py`). **Escanear repo** agrega todos los módulos que encuentre en un repositorio.
3. Opciones propias de esta pestaña:
   - **Odoo:** la versión de Odoo. Cambia el tipo de algunos campos: desde la 16, los campos traducibles se guardan como `JSONB`, y en la 18 cambia `company_dependent`.
   - **Tipo id M2O:** `BIGINT` (convención del proyecto) o `INTEGER` (estándar de Odoo).
   - **DDL crudo:** no valida y genera el DDL tal cual (comportamiento de la v1).
4. Pulsa **Extraer y validar**.

Por cada módulo se generan:

- `<módulo>/ddl/extract_<fecha>.sql`: el script de revisión.
- `<módulo>/ddl/extract_<fecha>.deploy.sql`: el script para el DBA.
- `<módulo>/ddl/extract_<fecha>.report.md`: el reporte.
- `<módulo>/.ddl_snapshot.json`: el snapshot de campos.

> La comparación es contra el esquema **real**. Para ver solo lo nuevo, apunta a una base que todavía no tenga el `-u` del módulo (staging, un dump anterior).

En la pestaña **Log** también vas a ver *notas* del extractor, por ejemplo:

- `AbstractModel, sin tabla`
- `_inherit de tabla inexistente stock_picking (¿módulo base no instalado?)`: el modelo extiende una tabla que no existe en esa base, así que sus campos se omiten en lugar de generar un `CREATE TABLE` incorrecto.

## 8. Interpretar los resultados

### 8.1 Estados

| Estado | Significado | En el script |
|---|---|---|
| **OK** | Se puede ejecutar | Activa |
| **WARN** | Se ejecuta, pero conviene revisarla | Activa |
| **OMITIDO** | Ya existe en la base (tabla, columna, índice, constraint o FK equivalente) | Comentada |
| **ERROR** | Fallaría o depende de algo que no existe | **Comentada**, con el motivo |

### 8.2 Pestañas

- **Resultados:** una fila por sentencia. Puedes filtrar por estado o buscar texto. **Doble clic** en una fila salta a esa sentencia en el SQL generado.
- **Relaciones faltantes:** tablas referenciadas que no existen en la base, con cuántas sentencias dependen de cada una. Es lo primero que conviene revisar.
- **SQL generado:** el script final con resaltado de sintaxis. En el combo *Archivo* eliges entre el script de revisión y el de despliegue (**DBA (produccion)**). Botones *Copiar*, *Guardar como…*, *Abrir carpeta* y *Abrir reporte*. Si extrajiste varios módulos, elige el archivo en el combo.
- **Dry-run:** las sentencias que fallaron al ejecutarse de verdad (con ROLLBACK). Si está vacía, el script corre limpio en esa base.
- **Log:** el detalle de la ejecución.

### 8.3 Cómo se ve un guard

```sql
DO $ddl$
BEGIN
    IF to_regclass('public.mi_tabla') IS NULL THEN
        RAISE WARNING '[ddl] % omitido: %', 'FK …', 'no existe la tabla public.mi_tabla';
    ELSIF to_regclass('public.budget_general') IS NULL THEN
        RAISE WARNING '[ddl] % omitido: %', 'FK …', 'no existe la tabla referenciada …';
    ELSIF EXISTS (SELECT 1 FROM pg_constraint WHERE …) THEN
        RAISE NOTICE '[ddl] % ya existe', 'FK …';
    ELSE
        ALTER TABLE … ADD CONSTRAINT … FOREIGN KEY …;
    END IF;
END
$ddl$;
```

Si ejecutas el script en una base distinta a la que validaste, los guards evitan que una FK sin su tabla aborte toda la transacción. En psql y DBeaver vas a ver los `WARNING [ddl] …` en la salida.

### 8.4 Mensajes frecuentes y qué hacer

| Mensaje | Causa | Qué hacer |
|---|---|---|
| `no existe la tabla referenciada X` | El módulo dueño de X no está instalado en esa base | Instalar el módulo, crear X antes, o aceptar la FK comentada y aplicarla después |
| `N fila(s) huerfana(s)` | Hay valores en la columna FK que no existen en la tabla destino | Limpiar los datos (`UPDATE … SET col = NULL WHERE …`) o usar *FK con huérfanos → NOT VALID* |
| `NOT NULL sin DEFAULT en tabla con datos` | `ADD COLUMN … NOT NULL` fallaría sobre filas existentes | Agregar `DEFAULT`, o crear la columna como `NULL`, poblarla y después `SET NOT NULL` |
| `X no es PK ni UNIQUE` | La FK apunta a columnas sin restricción única | Corregir la referencia o crear el índice UNIQUE |
| `tipos incompatibles` | Por ejemplo `varchar → integer` | Corregir el tipo de la columna |
| `tipos distintos pero compatibles` (info) | `int8 → int4`. Es normal con la convención `BIGINT` | Nada |
| `ya existe una relacion llamada X` | Nombre de índice o PK ocupado por otro objeto | Renombrar el constraint o índice |
| `sentencia no analizada` | Tipo de sentencia que el validador no interpreta (`COMMENT ON`, `CREATE VIEW`, …) | Revisarla a mano. Se emite tal cual |
| `sentencia destructiva: comentada` | `DROP`/`TRUNCATE`/`DELETE` | Revisar. Activar *Permitir DROP* solo si es intencional |

## 9. Script para el DBA (`.deploy.sql`)

### 9.1 Revisión o despliegue

| | `.validated.sql` (revisión) | `.deploy.sql` (despliegue) |
|---|---|---|
| Para quién | Desarrollo | DBA / producción |
| Termina en | `ROLLBACK` | **`COMMIT`** |
| Contenido | Cada sentencia con su estado (OK/WARN/OMITIDO/ERROR) y lo inválido comentado | Solo lo que se ejecuta, numerado `[n/N]`, más un encabezado con instrucciones |
| Lo que ya existía en la base de validación | Comentado como OMITIDO | **Incluido** (es idempotente), porque producción puede no tenerlo |
| Lo que dio ERROR | Comentado con el motivo | **Excluido** y listado en el encabezado |
| Verificación de base destino | No | Sí, si se indicó *BD destino* |
| Verificación final de objetos | No | Sí (revierte todo si falta algo) |

### 9.2 Qué garantiza

1. **Una sola transacción:** si algo falla, no se aplica nada.
2. **Base correcta:** con *BD destino* se aborta de entrada si `current_database()` no coincide.
3. **Idempotente:** usa `IF NOT EXISTS` y guards, así que correrlo dos veces no duplica nada.
4. **Tolerante a diferencias entre test y producción:** si en producción falta una tabla referenciada, el guard la salta con `WARNING` en lugar de cortar en el primer error.
5. **Reversión total si falta algo:** al final verifica que existan todas las tablas, columnas, índices, constraints y FKs esperadas. Si falta alguna, aborta con la lista completa:
   ```
   ERROR:  [deploy] 1 de 34 objeto(s) NO aplicados: FK budget_ceiling_import_line_task_id_fkey (...)
   ```
   Si todo está bien:
   ```
   NOTICE:  [deploy] verificacion OK: 34 objeto(s) presentes
   ```

### 9.3 Antes de entregarlo al DBA

1. Valida contra una base lo más parecida posible a producción (una copia reciente), con **Dry-run** activado. El dry-run ejecuta justamente el `.deploy.sql`.
2. Revisa en el reporte la sección **Excluidas del despliegue**. Esas sentencias no van a producción hasta que se resuelvan.
3. Completa **BD destino** con el nombre de la base de producción.
4. Entrega solo el `.deploy.sql`. El encabezado ya trae las instrucciones de ejecución.

### 9.4 Ejecución (DBA)

1. **Respaldo:** `pg_dump -Fc -h <host> -p <puerto> -U <usuario> <base> > respaldo.dump`.
2. Ejecutar:
   ```bash
   psql -h <host> -p <puerto> -U <usuario> -d <base> -v ON_ERROR_STOP=1 -f script.deploy.sql
   ```
   o en DBeaver con *Ejecutar script* (Alt+X).
3. Confirmar que la salida muestre `[deploy] verificacion OK`.
4. Opcional: volver a validar el DDL original contra producción. Todo debería salir **OMITIDO**.

## 10. Línea de comandos

El instalador incluye `ddl-extractor-cli`. Con el código fuente, usa `python validate_ddl.py` / `python extract_ddl.py`.

```bash
ddl-extractor-cli validate ddls/ --env .env --dry-run -o ddls/validado.sql --target-db odoo_prod
ddl-extractor-cli extract mod_a mod_b --env .env
ddl-extractor-cli validate archivo.sql --no-db
ddl-extractor-cli validate --help
```

Opciones del script de despliegue: `--target-db <base>`, `--deploy-lenient` (COMMIT parcial si falta algún objeto, en vez de revertir todo) y `--no-deploy` (no generarlo).

| Código de salida | Significado |
|---|---|
| `0` | Sin errores |
| `1` | Hay errores (o warnings, con `--strict`) |

Sirve como paso de control en CI.

## 11. Dónde se guarda la configuración

| Sistema | Ubicación |
|---|---|
| Windows | Registro: `HKEY_CURRENT_USER\Software\odoo-ddl-extractor\ddl-extractor` |
| Ubuntu | `~/.config/odoo-ddl-extractor/ddl-extractor.conf` |

Ahí quedan los perfiles, la última carpeta usada y el tamaño de la ventana. Para empezar de cero, borra esa clave o ese archivo.

## 12. Solución de problemas

| Problema | Solución |
|---|---|
| `Python was not found` (al usar el código fuente en Windows) | Instalar Python: `winget install -e --id Python.Python.3.10`. Desactivar los alias de la Store en *Configuración → Aplicaciones → Configuración avanzada → Alias de ejecución* |
| `Activate.ps1 no se puede cargar… ejecución de scripts deshabilitada` | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, o usar `.\.venv\Scripts\python.exe` directamente |
| `No se pudo conectar … timeout / Network is unreachable` | No hay ruta al servidor: VPN, firewall, `listen_addresses` o `pg_hba.conf` del servidor |
| `password authentication failed` | Usuario o contraseña incorrectos |
| `permission denied` en el dry-run | El usuario no puede crear objetos. Pedir permisos o desmarcar *Dry-run* |
| `canceling statement due to lock timeout` | Una tabla está bloqueada por otra sesión (Odoo corriendo). Repetir fuera de horario o subir `lock_timeout` |
| Ubuntu: `Could not load the Qt platform plugin "xcb"` | `sudo apt install libxcb-cursor0` (el `.deb` ya lo declara como dependencia) |
| Ubuntu: la app no aparece en el menú | Cerrar sesión y volver a entrar, o ejecutar `ddl-extractor` en una terminal |
| El chequeo de huérfanos tarda mucho | Desmarcar *Chequear datos*. Las FKs igual quedan protegidas por los guards |

## 13. Desinstalar

- **Windows:** *Configuración → Aplicaciones → DDL Extractor → Desinstalar*. Si lo agregaste al PATH, también se quita de ahí.
- **Ubuntu:** `sudo apt remove ddl-extractor`.
