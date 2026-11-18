"""
ddl_common.py — Utilidades compartidas por extract_ddl.py y validate_ddl.py:

  - Resolucion de credenciales: flags > --odoo-conf > --env (.env) > variables
    de entorno DDL_DB_* > defaults.
  - Conexion a PostgreSQL.
  - Catalogo del esquema (cargado UNA vez via pg_catalog) + "catalogo virtual"
    que acumula lo que el script DDL va creando, para validar referencias que
    existen en la BD o que se crean antes en el mismo script.
  - Helpers de identificadores / tipos.
"""
import configparser
import hashlib
import os
import re
import sys
from pathlib import Path

try:
    import psycopg2
except ImportError:  # se valida al conectar
    psycopg2 = None


# -------------------------------------------------------------------------
# Configuracion de conexion
# -------------------------------------------------------------------------
# Alias aceptados en el .env (insensible a mayusculas) -> clave interna
ENV_KEYS = {
    "host": ("db_host", "ddl_db_host", "pghost", "host"),
    "port": ("db_port", "ddl_db_port", "pgport", "port"),
    "db": ("db_name", "ddl_db_name", "pgdatabase", "database", "db"),
    "user": ("db_user", "ddl_db_user", "pguser", "user"),
    "password": ("db_password", "ddl_db_password", "pgpassword", "password"),
}


def read_env_file(path):
    """Parser minimo de .env: KEY=VALUE, ignora comentarios/lineas vacias,
    quita comillas y 'export '. Devuelve dict con claves en minuscula."""
    data = {}
    p = Path(path)
    if not p.exists():
        sys.exit(f"--env apunta a un archivo que no existe: {p}")
    for raw in p.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        data[k.strip().lower()] = v
    return data


def _from_env_dict(env):
    out = {}
    for key, aliases in ENV_KEYS.items():
        for a in aliases:
            if env.get(a):
                out[key] = env[a]
                break
    return out


def _from_odoo_conf(path):
    conf_path = Path(path).resolve()
    if not conf_path.exists():
        sys.exit(f"--odoo-conf apunta a un archivo que no existe: {conf_path}")
    cp = configparser.ConfigParser()
    if not cp.read(conf_path):
        sys.exit(f"No se pudo leer/parsear --odoo-conf: {conf_path}")
    if "options" not in cp:
        sys.exit(f"{conf_path} no tiene seccion [options]. Secciones: {cp.sections()}")
    opt = cp["options"]
    out = {}
    for ck, key in (("db_host", "host"), ("db_port", "port"), ("db_user", "user"),
                    ("db_password", "password"), ("db_name", "db")):
        val = opt.get(ck, fallback=None)
        if val and val.lower() not in ("false", "none", ""):
            out[key] = val
    if "db" not in out:
        print(f"[aviso] {conf_path.name} no define db_name (tipico si Odoo usa -d). Pasa --db.")
    return out


# API publica (GUI)
env_to_config = _from_env_dict
odoo_conf_to_config = _from_odoo_conf


def add_db_arguments(parser):
    g = parser.add_argument_group("conexion a BD")
    # default=None a proposito: permite saber si el flag fue pasado
    g.add_argument("--db", default=None)
    g.add_argument("--host", default=None)
    g.add_argument("--port", type=int, default=None)
    g.add_argument("--user", default=None)
    g.add_argument("--password", default=None)
    g.add_argument("--env", dest="env_file", default=None,
                   help="Archivo .env con db_host/db_port/db_user/db_password/db_name")
    g.add_argument("--odoo-conf", default=None, help="odoo.conf ([options] db_*)")
    g.add_argument("--schema", default="public", help="Esquema destino (default: public)")


def resolve_db_config(args):
    """Prioridad: flag > --odoo-conf > --env > variables DDL_DB_* > default."""
    layers = []
    if args.odoo_conf:
        layers.append(_from_odoo_conf(args.odoo_conf))
    if args.env_file:
        layers.append(_from_env_dict(read_env_file(args.env_file)))
    layers.append(_from_env_dict({k.lower(): v for k, v in os.environ.items()
                                  if k.upper().startswith("DDL_DB_")}))
    defaults = {"host": "localhost", "port": "5432", "user": "odoo", "password": "odoo"}

    for key in ("host", "port", "db", "user", "password"):
        if getattr(args, key, None):
            continue
        for layer in layers:
            if layer.get(key):
                setattr(args, key, layer[key])
                break
        else:
            setattr(args, key, defaults.get(key))
    args.port = int(args.port)
    return args


def connect(args):
    if psycopg2 is None:
        sys.exit("Falta psycopg2. Instalar con: pip install psycopg2-binary")
    if not args.db:
        sys.exit("Falta el nombre de BD (--db, db_name en .env u odoo.conf)")
    try:
        conn = psycopg2.connect(host=args.host, port=args.port, dbname=args.db,
                                user=args.user, password=args.password,
                                connect_timeout=10,
                                application_name="odoo-ddl-extractor")
    except Exception as e:  # noqa: BLE001
        sys.exit(f"No se pudo conectar a {args.user}@{args.host}:{args.port}/{args.db}: {e}")
    conn.set_session(readonly=True, autocommit=False)
    print(f"[bd] conectado a {args.user}@{args.host}:{args.port}/{args.db}")
    return conn


# -------------------------------------------------------------------------
# Identificadores
# -------------------------------------------------------------------------
_SIMPLE_IDENT = re.compile(r"^[a-z_][a-z0-9_$]*$")
# Palabras reservadas que suelen aparecer como nombres de columna en Odoo
_RESERVED = {
    "all", "analyse", "analyze", "and", "any", "array", "as", "asc", "asymmetric",
    "both", "case", "cast", "check", "collate", "column", "constraint", "create",
    "current_date", "current_role", "current_time", "current_timestamp",
    "current_user", "default", "deferrable", "desc", "distinct", "do", "else", "end",
    "except", "false", "fetch", "for", "foreign", "from", "grant", "group", "having",
    "in", "initially", "intersect", "into", "lateral", "leading", "limit", "localtime",
    "localtimestamp", "not", "null", "offset", "on", "only", "or", "order", "placing",
    "primary", "references", "returning", "select", "session_user", "some",
    "symmetric", "table", "then", "to", "trailing", "true", "union", "unique", "user",
    "using", "variadic", "when", "where", "window", "with",
}
MAX_IDENT = 63


def norm_ident(tok):
    """'"Foo"' -> 'Foo'; 'Foo' -> 'foo' (regla de PostgreSQL)."""
    tok = tok.strip()
    if len(tok) >= 2 and tok[0] == '"' and tok[-1] == '"':
        return tok[1:-1].replace('""', '"')
    return tok.lower()


def qi(name):
    """Quote identifier solo si hace falta."""
    if _SIMPLE_IDENT.match(name) and name not in _RESERVED:
        return name
    return '"' + name.replace('"', '""') + '"'


def qname(schema, name):
    return f"{qi(schema)}.{qi(name)}"


def sql_lit(s):
    return "'" + str(s).replace("'", "''") + "'"


def short_name(name):
    """Recorta a 63 chars con hash (evita truncado silencioso de PostgreSQL)."""
    if len(name) <= MAX_IDENT:
        return name
    h = hashlib.md5(name.encode()).hexdigest()[:8]
    return f"{name[:54]}_{h}"


# -------------------------------------------------------------------------
# Tipos
# -------------------------------------------------------------------------
_TYPE_ALIASES = [
    (r"^(serial4|serial)$", "integer"),
    (r"^(serial8|bigserial)$", "bigint"),
    (r"^(serial2|smallserial)$", "smallint"),
    (r"^(int4|int|integer)$", "integer"),
    (r"^(int8|bigint)$", "bigint"),
    (r"^(int2|smallint)$", "smallint"),
    (r"^(varchar|character varying)(\(.*\))?$", "varchar"),
    (r"^(char|character|bpchar)(\(.*\))?$", "char"),
    (r"^(numeric|decimal)(\(.*\))?$", "numeric"),
    (r"^(float8|double precision)$", "float8"),
    (r"^(float4|real)$", "float4"),
    (r"^(bool|boolean)$", "boolean"),
    (r"^(timestamp|timestamp without time zone)(\(\d+\))?$", "timestamp"),
    (r"^(timestamptz|timestamp with time zone)(\(\d+\))?$", "timestamptz"),
]
_FAMILY = {
    "smallint": "int", "integer": "int", "bigint": "int",
    "varchar": "text", "text": "text", "char": "text",
    "numeric": "num", "float8": "num", "float4": "num",
}


def norm_type(t):
    t = re.sub(r"\s+", " ", (t or "").strip().lower())
    for pat, canon in _TYPE_ALIASES:
        if re.match(pat, t):
            return canon
    return t


def type_family(t):
    n = norm_type(t)
    return _FAMILY.get(n, n)


def is_serial(t):
    return re.match(r"^\s*(small|big)?serial[248]?\s*$", t or "", re.I) is not None


# -------------------------------------------------------------------------
# Catalogo
# -------------------------------------------------------------------------
class Catalog:
    """Vista del esquema real + lo que el script crea (catalogo virtual).

    Claves: (schema, table). Todos los nombres ya normalizados.
    """

    def __init__(self, conn=None, schemas=("public",)):
        self.conn = conn
        self.tables = {}        # (s,t) -> {"kind": r|v|..., "rows": est, "origin": db|script}
        self.columns = {}       # (s,t) -> {col: {"type": str, "notnull": bool}}
        self.relnames = set()   # (s,name) espacio de nombres pg_class (tablas, indices, seq)
        self.constraints = {}   # (s,t) -> set(conname)
        self.uniques = {}       # (s,t) -> list[tuple(cols)]  PK/UNIQUE
        self.pkeys = {}         # (s,t) -> tuple(cols)
        self.fk_sigs = set()    # (s,t,cols,rs,rt,rcols)
        self._has_rows_cache = {}
        if conn is not None:
            self._load(schemas)

    # ---- carga desde BD ---------------------------------------------------
    def _load(self, schemas):
        cur = self.conn.cursor()
        cur.execute("""
            SELECT n.nspname, c.relname, c.relkind, c.reltuples
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = ANY(%s)
        """, (list(schemas),))
        for s, name, kind, tuples in cur.fetchall():
            self.relnames.add((s, name))
            if kind in ("r", "p", "v", "m", "f"):
                self.tables[(s, name)] = {"kind": kind, "rows": tuples, "origin": "db"}

        cur.execute("""
            SELECT n.nspname, c.relname, a.attname,
                   format_type(a.atttypid, a.atttypmod), a.attnotnull
            FROM pg_attribute a
            JOIN pg_class c ON c.oid = a.attrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = ANY(%s) AND c.relkind IN ('r','p','v','m','f')
              AND a.attnum > 0 AND NOT a.attisdropped
        """, (list(schemas),))
        for s, t, col, typ, nn in cur.fetchall():
            self.columns.setdefault((s, t), {})[col] = {"type": typ, "notnull": nn}

        cur.execute("""
            SELECT n.nspname, c.relname, con.conname, con.contype,
                   ARRAY(SELECT a.attname FROM unnest(con.conkey) WITH ORDINALITY k(n, o)
                         JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.n
                         ORDER BY k.o),
                   rn.nspname, rc.relname,
                   ARRAY(SELECT a.attname FROM unnest(con.confkey) WITH ORDINALITY k(n, o)
                         JOIN pg_attribute a ON a.attrelid = con.confrelid AND a.attnum = k.n
                         ORDER BY k.o)
            FROM pg_constraint con
            JOIN pg_class c ON c.oid = con.conrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            LEFT JOIN pg_class rc ON rc.oid = con.confrelid
            LEFT JOIN pg_namespace rn ON rn.oid = rc.relnamespace
            WHERE n.nspname = ANY(%s)
        """, (list(schemas),))
        for s, t, cname, ctype, cols, rs, rt, rcols in cur.fetchall():
            self.constraints.setdefault((s, t), set()).add(cname)
            if ctype in ("p", "u"):
                self.uniques.setdefault((s, t), []).append(tuple(cols))
                if ctype == "p":
                    self.pkeys[(s, t)] = tuple(cols)
            elif ctype == "f":
                self.fk_sigs.add((s, t, tuple(cols), rs, rt, tuple(rcols)))

        # indices UNIQUE sin constraint (CREATE UNIQUE INDEX) tambien sirven para FK
        cur.execute("""
            SELECT n.nspname, c.relname,
                   ARRAY(SELECT a.attname FROM unnest(i.indkey::int2[]) WITH ORDINALITY k(n, o)
                         JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.n
                         ORDER BY k.o)
            FROM pg_index i
            JOIN pg_class c ON c.oid = i.indrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = ANY(%s) AND i.indisunique AND i.indpred IS NULL
              AND 0 <> ALL(i.indkey::int2[])
        """, (list(schemas),))
        for s, t, cols in cur.fetchall():
            u = self.uniques.setdefault((s, t), [])
            if tuple(cols) not in u:
                u.append(tuple(cols))
        cur.close()
        self.conn.rollback()

    # ---- consultas ----------------------------------------------------------
    @property
    def online(self):
        return self.conn is not None

    def has_table(self, key):
        return key in self.tables

    def origin(self, key):
        return self.tables.get(key, {}).get("origin")

    def cols(self, key):
        return self.columns.get(key, {})

    def has_relname(self, key):
        return key in self.relnames

    def has_constraint(self, key, name):
        return name in self.constraints.get(key, set())

    def is_unique_set(self, key, cols):
        cols = tuple(cols)
        return any(tuple(u) == cols or set(u) == set(cols)
                   for u in self.uniques.get(key, []))

    def has_rows(self, key):
        """True/False si la tabla (de BD) tiene filas. None si no se puede saber."""
        if self.origin(key) != "db":
            return False
        if not self.online:
            return None
        if key in self._has_rows_cache:
            return self._has_rows_cache[key]
        cur = self.conn.cursor()
        try:
            cur.execute(f"SELECT EXISTS (SELECT 1 FROM {qname(*key)} LIMIT 1)")
            val = cur.fetchone()[0]
        except Exception:  # noqa: BLE001
            val = None
        finally:
            cur.close()
            self.conn.rollback()
        self._has_rows_cache[key] = val
        return val

    def count_orphans(self, key, cols, rkey, rcols, timeout_ms=60000):
        """Filas de key cuyo FK no existe en rkey. None si no se pudo calcular."""
        if not self.online:
            return None
        src = " AND ".join(f"s.{qi(c)} IS NOT NULL" for c in cols)
        join = " AND ".join(f"r.{qi(rc)} = s.{qi(c)}" for c, rc in zip(cols, rcols))
        sql = (f"SELECT count(*) FROM {qname(*key)} s WHERE {src} "
               f"AND NOT EXISTS (SELECT 1 FROM {qname(*rkey)} r WHERE {join})")
        cur = self.conn.cursor()
        try:
            cur.execute(f"SET LOCAL statement_timeout = {int(timeout_ms)}")
            cur.execute(sql)
            return cur.fetchone()[0]
        except Exception:  # noqa: BLE001
            return None
        finally:
            cur.close()
            self.conn.rollback()

    # ---- registro de lo que crea el script ------------------------------
    def add_table(self, key, columns, origin="script"):
        self.tables[key] = {"kind": "r", "rows": 0, "origin": origin}
        self.relnames.add(key)
        self.columns[key] = dict(columns)

    def add_column(self, key, col, typ, notnull=False):
        self.columns.setdefault(key, {})[col] = {"type": typ, "notnull": notnull, "origin": "script"}

    def col_from_script(self, key, col):
        return self.origin(key) == "script" or self.cols(key).get(col, {}).get("origin") == "script"

    def add_relname(self, key):
        self.relnames.add(key)

    def add_constraint(self, key, name, kind=None, cols=None, ref=None):
        self.constraints.setdefault(key, set()).add(name)
        if kind in ("p", "u") and cols:
            self.uniques.setdefault(key, []).append(tuple(cols))
            if kind == "p":
                self.pkeys[key] = tuple(cols)
        if kind == "f" and ref:
            rkey, rcols = ref
            self.fk_sigs.add((key[0], key[1], tuple(cols), rkey[0], rkey[1], tuple(rcols)))
