#!/usr/bin/env python3
"""
extract_ddl.py — Extractor de DDL para modelos/campos nuevos en modulos Odoo.

Se ejecuta a nivel de REPOSITORIO (fuera de los modulos, uno por repo). Se le
puede pasar:
  - una o varias rutas de modulo explicitas, o
  - --repo <ruta> para que autodetecte todos los modulos (carpetas con
    __manifest__.py) dentro de ese repositorio (default: directorio actual).

Compara los campos definidos en los modelos Python (fields.Char,
fields.Many2one, etc.) contra el esquema real de PostgreSQL. Todo lo que
exista en el codigo pero no en la base de datos se considera "nuevo" y se
emite como DDL (CREATE TABLE / ALTER TABLE ADD COLUMN).

Uso:
    # modulo(s) explicito(s), desde cualquier ubicacion
    python extract_ddl.py mod_a mod_b --db odoo_db --host localhost --user odoo --password odoo

    # autodetectar todos los modulos del repo (parado en la raiz del repo)
    python extract_ddl.py --db odoo_db

    # autodetectar en una ruta especifica
    python extract_ddl.py --repo /ruta/al/repo --db odoo_db

Config alternativa via variables de entorno:
    DDL_DB_HOST, DDL_DB_PORT, DDL_DB_NAME, DDL_DB_USER, DDL_DB_PASSWORD

Tambien intenta leer un odoo.conf (ini) si se pasa --odoo-conf.

Salida (por cada modulo procesado):
    <modulo>/ddl/extract_<YYYYmmdd_HHMMSS>.sql   -> DDL generado
    <modulo>/.ddl_snapshot.json                   -> snapshot de campos conocidos
"""
import argparse
import ast
import configparser
import datetime as dt
import json
import os
import sys
from pathlib import Path

try:
    import psycopg2
except ImportError:
    psycopg2 = None

# -------------------------------------------------------------------------
# Mapeo de tipos de campo Odoo -> tipo de columna PostgreSQL
# -------------------------------------------------------------------------
FIELD_TYPE_MAP = {
    "Char": "VARCHAR",
    "Text": "TEXT",
    "Html": "TEXT",
    "Integer": "INTEGER",
    # "Float" y "Many2one" se resuelven en column_ddl_type() porque su tipo
    # depende de kwargs del campo (digits / convencion de ids del proyecto).
    "Monetary": "NUMERIC",
    "Boolean": "BOOLEAN",
    "Date": "DATE",
    "Datetime": "TIMESTAMP",
    "Selection": "VARCHAR",
    "Binary": "BYTEA",
    "Reference": "VARCHAR",
    "Json": "JSONB",
}
# One2many no genera columna ni tabla propia (es el inverso de un Many2one).
# Many2many SI se maneja, pero via tabla relacional (ver build_m2m_ddl), no columna.
RELATIONAL_NO_COLUMN = {"One2many", "Many2many"}

# Este proyecto usa BIGINT para ids/FKs (confirmado por el usuario, ver comparacion
# contra DDL real de DBeaver). Si en otro proyecto la convencion es int4 estandar,
# cambiar a "INTEGER".
ID_COLUMN_TYPE = "BIGINT"

AUDIT_COLUMNS = [
    ("create_uid", ID_COLUMN_TYPE, "res.users", "SET NULL"),
    ("create_date", "TIMESTAMP", None, None),
    ("write_uid", ID_COLUMN_TYPE, "res.users", "SET NULL"),
    ("write_date", "TIMESTAMP", None, None),
]

VALID_ONDELETE = {"SET NULL", "CASCADE", "RESTRICT", "NO ACTION", "SET DEFAULT"}


class OdooModel:
    def __init__(self, name, table, fields_, inherit=False, source_file=""):
        self.name = name          # _name o _inherit
        self.table = table        # nombre de tabla postgres
        self.fields = fields_     # dict field_name -> field_info
        self.inherit = inherit    # True si es _inherit (extiende modelo existente)
        self.source_file = source_file


def odoo_model_to_table(model_name: str) -> str:
    return model_name.replace(".", "_")


def literal_or_none(node):
    try:
        return ast.literal_eval(node)
    except Exception:
        return None


def parse_field_call(call: ast.Call):
    """Extrae tipo y kwargs relevantes de fields.X(...)"""
    if not isinstance(call.func, ast.Attribute):
        return None
    if not isinstance(call.func.value, ast.Name) or call.func.value.id != "fields":
        return None
    ftype = call.func.attr  # Char, Many2one, etc.

    info = {"type": ftype, "size": None, "required": False,
             "comodel": None, "selection": None,
             "relation": None, "column1": None, "column2": None,
             "digits": None, "ondelete": None}

    # args posicionales: Char("Label"), Selection([...]) etc.
    # Many2one/Many2many: primer posicional es el comodel_name.
    # Many2many legado admite ademas: (comodel, relation, column1, column2, ...)
    if call.args:
        first = literal_or_none(call.args[0])
        if ftype in ("Many2one", "Many2many") and isinstance(first, str):
            info["comodel"] = first
        elif ftype == "Selection":
            info["selection"] = literal_or_none(call.args[0])

        if ftype == "Many2many":
            positional = [literal_or_none(a) for a in call.args]
            if len(positional) > 1 and isinstance(positional[1], str):
                info["relation"] = positional[1]
            if len(positional) > 2 and isinstance(positional[2], str):
                info["column1"] = positional[2]
            if len(positional) > 3 and isinstance(positional[3], str):
                info["column2"] = positional[3]

    for kw in call.keywords:
        if kw.arg == "size":
            info["size"] = literal_or_none(kw.value)
        elif kw.arg == "required":
            info["required"] = literal_or_none(kw.value) is True
        elif kw.arg == "comodel_name":
            info["comodel"] = literal_or_none(kw.value)
        elif kw.arg == "compute":
            info["compute"] = literal_or_none(kw.value)
        elif kw.arg == "store":
            info["store"] = literal_or_none(kw.value)
        elif kw.arg == "relation":
            info["relation"] = literal_or_none(kw.value)
        elif kw.arg == "column1":
            info["column1"] = literal_or_none(kw.value)
        elif kw.arg == "column2":
            info["column2"] = literal_or_none(kw.value)
        elif kw.arg == "digits":
            info["digits"] = literal_or_none(kw.value)
        elif kw.arg == "ondelete":
            info["ondelete"] = literal_or_none(kw.value)

    return info


def parse_models_from_file(path: Path):
    """Parsea un archivo .py y devuelve lista de OdooModel encontrados."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return []

    models = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        is_odoo_model = any(
            (isinstance(b, ast.Attribute) and b.attr in ("Model", "TransientModel", "AbstractModel"))
            or (isinstance(b, ast.Name) and b.id in ("Model", "TransientModel", "AbstractModel"))
            for b in node.bases
        )
        if not is_odoo_model:
            continue

        _name = None
        _inherit = None
        model_fields = {}

        for stmt in node.body:
            if isinstance(stmt, ast.Assign):
                target_names = [t.id for t in stmt.targets if isinstance(t, ast.Name)]
                if "_name" in target_names:
                    _name = literal_or_none(stmt.value)
                elif "_inherit" in target_names:
                    _inherit = literal_or_none(stmt.value)
                elif isinstance(stmt.value, ast.Call):
                    finfo = parse_field_call(stmt.value)
                    if finfo:
                        model_fields[target_names[0]] = finfo

        model_name = _name or _inherit
        if not model_name:
            continue

        models.append(OdooModel(
            name=model_name,
            table=odoo_model_to_table(model_name),
            fields_=model_fields,
            inherit=(_name is None and _inherit is not None),
            source_file=str(path),
        ))
    return models


def scan_module(module_path: Path):
    models_dir = module_path / "models"
    if not models_dir.exists():
        models_dir = module_path  # fallback: modulo plano
    all_models = []
    for py_file in sorted(models_dir.rglob("*.py")):
        all_models.extend(parse_models_from_file(py_file))
    return all_models


# -------------------------------------------------------------------------
# Introspeccion de PostgreSQL
# -------------------------------------------------------------------------
def get_db_connection(args):
    if psycopg2 is None:
        sys.exit("Falta psycopg2. Instalar con: pip install psycopg2-binary --break-system-packages")
    return psycopg2.connect(
        host=args.host, port=args.port, dbname=args.db,
        user=args.user, password=args.password,
    )


def table_exists(cur, table):
    cur.execute("""
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = %s
    """, (table,))
    return cur.fetchone() is not None


def existing_columns(cur, table):
    cur.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s
    """, (table,))
    return {row[0] for row in cur.fetchall()}


def column_ddl_type(finfo):
    ftype = finfo["type"]
    if ftype in RELATIONAL_NO_COLUMN:
        return None

    if ftype == "Many2one":
        return ID_COLUMN_TYPE

    if ftype == "Float":
        digits = finfo.get("digits")
        if isinstance(digits, (tuple, list)) and len(digits) == 2:
            precision, scale = digits
            return f"NUMERIC({precision},{scale})"
        # Sin digits explicitos: Odoo usa double precision (float8) por defecto.
        return "DOUBLE PRECISION"

    pg_type = FIELD_TYPE_MAP.get(ftype, "VARCHAR")
    if pg_type == "VARCHAR" and finfo.get("size"):
        pg_type = f"VARCHAR({finfo['size']})"
    return pg_type


def fk_constraint_name(table: str, fname: str) -> str:
    """Nombre de constraint FK. Postgres limita identificadores a 63 chars;
    si se pasa, trunca y agrega un hash corto para evitar colisiones."""
    name = f"{table}_{fname}_fkey"
    if len(name) <= 63:
        return name
    import hashlib
    h = hashlib.md5(name.encode()).hexdigest()[:8]
    return f"{name[:54]}_{h}"


def m2o_fk_statement(table: str, fname: str, comodel: str, ondelete: str = None):
    """Genera el ALTER TABLE ADD CONSTRAINT ... FOREIGN KEY para un Many2one
    (o create_uid/write_uid). Devuelve None si no se conoce la tabla destino."""
    if not comodel:
        return None
    comodel_table = odoo_model_to_table(comodel)
    od = (ondelete or "set null").upper()
    if od not in VALID_ONDELETE:
        od = "SET NULL"
    cname = fk_constraint_name(table, fname)
    return (f"ALTER TABLE {table} ADD CONSTRAINT {cname} "
            f"FOREIGN KEY ({fname}) REFERENCES {comodel_table}(id) ON DELETE {od};")


def m2m_relation_names(table1: str, table2: str, finfo: dict):
    """Calcula (relation, column1, column2) siguiendo la convencion de nombres
    que usa Odoo cuando el campo Many2many no especifica relation/column1/column2."""
    relation = finfo.get("relation")
    column1 = finfo.get("column1")
    column2 = finfo.get("column2")

    if not relation:
        t1, t2 = sorted([table1, table2])
        relation = f"{t1}_{t2}_rel"
    if not column1:
        column1 = f"{table1}_id"
    if not column2:
        column2 = f"{table2}_id"
    if column1 == column2:
        # Many2many auto-referente (mismo modelo en ambos lados)
        column1, column2 = f"{column1}1", f"{column2}2"

    return relation, column1, column2


# -------------------------------------------------------------------------
# Generacion de DDL
# -------------------------------------------------------------------------
def build_ddl(models, cur):
    statements = []
    new_snapshot = {}
    seen_relations = set()  # evita duplicar la misma tabla puente en la misma corrida

    for model in models:
        table = model.table
        new_snapshot[model.name] = {"table": table, "fields": list(model.fields.keys())}

        if not table_exists(cur, table):
            # CREATE TABLE completo (modelo nuevo)
            cols = ["id SERIAL PRIMARY KEY"]
            fk_statements = []
            for fname, finfo in model.fields.items():
                pg_type = column_ddl_type(finfo)
                if pg_type is None:
                    continue
                not_null = " NOT NULL" if finfo.get("required") else ""
                cols.append(f"{fname} {pg_type}{not_null}")
                if finfo["type"] == "Many2one":
                    fk = m2o_fk_statement(table, fname, finfo.get("comodel"), finfo.get("ondelete"))
                    if fk:
                        fk_statements.append(fk)
            for cname, ctype, comodel, ondelete in AUDIT_COLUMNS:
                cols.append(f"{cname} {ctype}")
                if comodel:
                    fk_statements.append(m2o_fk_statement(table, cname, comodel, ondelete))
            ddl = f"-- Modelo nuevo: {model.name} ({model.source_file})\n"
            ddl += f"CREATE TABLE {table} (\n    " + ",\n    ".join(cols) + "\n);"
            statements.append(ddl)
            if fk_statements:
                statements.append(
                    f"-- Foreign keys de {model.name}\n" + "\n".join(fk_statements)
                )
        else:
            # Tabla existe -> revisar columnas nuevas
            cols_in_db = existing_columns(cur, table)
            for fname, finfo in model.fields.items():
                if finfo["type"] == "Many2many":
                    continue  # se procesa aparte, no genera columna
                if fname in cols_in_db:
                    continue
                if finfo.get("compute") and not finfo.get("store"):
                    continue  # campo calculado no almacenado, sin columna
                pg_type = column_ddl_type(finfo)
                if pg_type is None:
                    continue
                not_null = " NOT NULL" if finfo.get("required") else ""
                stmt = (
                    f"-- Campo nuevo: {model.name}.{fname} ({model.source_file})\n"
                    f"ALTER TABLE {table} ADD COLUMN {fname} {pg_type}{not_null};"
                )
                if finfo["type"] == "Many2one":
                    fk = m2o_fk_statement(table, fname, finfo.get("comodel"), finfo.get("ondelete"))
                    if fk:
                        stmt += f"\n{fk}"
                statements.append(stmt)

        # Many2many: tabla puente (aplica tanto si el modelo es nuevo como si ya existia)
        for fname, finfo in model.fields.items():
            if finfo["type"] != "Many2many":
                continue
            if finfo.get("compute") and not finfo.get("store"):
                continue  # M2M computado no almacenado: se calcula en Python, sin tabla en BD
            comodel = finfo.get("comodel")
            comodel_table = odoo_model_to_table(comodel) if comodel else None
            relation, column1, column2 = m2m_relation_names(table, comodel_table or "unknown", finfo)

            if relation in seen_relations:
                continue
            if table_exists(cur, relation):
                seen_relations.add(relation)
                continue

            comment = f"-- Many2many nuevo: {model.name}.{fname} -> {comodel or '?'} ({model.source_file})\n"
            ddl = (
                f"{comment}"
                f"CREATE TABLE {relation} (\n"
                f"    {column1} INTEGER NOT NULL REFERENCES {table}(id) ON DELETE CASCADE,\n"
                f"    {column2} INTEGER NOT NULL"
                + (f" REFERENCES {comodel_table}(id) ON DELETE CASCADE" if comodel_table else "")
                + f",\n    PRIMARY KEY ({column1}, {column2})\n);"
            )
            statements.append(ddl)
            seen_relations.add(relation)

    return statements, new_snapshot


def load_snapshot(module_path: Path):
    snap_file = module_path / ".ddl_snapshot.json"
    if snap_file.exists():
        return json.loads(snap_file.read_text(encoding="utf-8"))
    return {}


def save_snapshot(module_path: Path, snapshot: dict):
    snap_file = module_path / ".ddl_snapshot.json"
    snap_file.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False), encoding="utf-8")


# -------------------------------------------------------------------------
# Config (odoo.conf opcional)
# -------------------------------------------------------------------------
def apply_odoo_conf(args):
    if not args.odoo_conf:
        return args

    conf_path = Path(args.odoo_conf).resolve()
    if not conf_path.exists():
        sys.exit(f"--odoo-conf apunta a un archivo que no existe: {conf_path}")

    cp = configparser.ConfigParser()
    read_ok = cp.read(conf_path)
    if not read_ok:
        sys.exit(f"No se pudo leer/parsear --odoo-conf: {conf_path}")
    if "options" not in cp:
        sys.exit(f"El archivo {conf_path} no tiene seccion [options]. Secciones encontradas: {cp.sections()}")

    opt = cp["options"]
    args.host = args.host or opt.get("db_host", fallback="localhost")
    args.port = args.port or opt.getint("db_port", fallback=5432)
    args.user = args.user or opt.get("db_user", fallback="odoo")
    args.password = args.password or opt.get("db_password", fallback="odoo")

    db_name = opt.get("db_name", fallback=None)
    if db_name and db_name.lower() not in ("false", "none", ""):
        args.db = args.db or db_name
    else:
        print(f"[aviso] {conf_path.name} no define db_name (o esta vacio/False, tipico si Odoo "
              f"selecciona la BD con -d). Pasa --db explicitamente.")

    print(f"[odoo-conf] {conf_path} -> host={args.host} port={args.port} user={args.user} db={args.db}")
    return args


def is_module_dir(path: Path) -> bool:
    return (path / "__manifest__.py").exists() or (path / "__openerp__.py").exists()


def discover_modules(repo_path: Path):
    """Busca subcarpetas de primer nivel que sean modulos Odoo (con __manifest__.py)."""
    if is_module_dir(repo_path):
        return [repo_path]
    return [p for p in sorted(repo_path.iterdir()) if p.is_dir() and is_module_dir(p)]


def process_module(module_path: Path, args, cur_factory):
    """Procesa un solo modulo: escanea, genera DDL y guarda snapshot. Devuelve #sentencias."""
    models = scan_module(module_path)
    if not models:
        print(f"[{module_path.name}] Sin modelos Odoo detectados, se omite.")
        return 0

    cur = cur_factory()
    try:
        statements, snapshot = build_ddl(models, cur)
    finally:
        if hasattr(cur, "connection"):
            cur.connection.close()

    if not statements:
        print(f"[{module_path.name}] Sin modelos ni campos nuevos respecto a la base de datos.")
        save_snapshot(module_path, snapshot)
        return 0

    out_dir = module_path / "ddl"
    out_dir.mkdir(exist_ok=True)
    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_file = out_dir / f"extract_{timestamp}.sql"
    out_file.write_text("\n\n".join(statements) + "\n", encoding="utf-8")

    save_snapshot(module_path, snapshot)

    print(f"[{module_path.name}] {len(statements)} sentencia(s) DDL generadas -> {out_file}")
    return len(statements)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("module_path", nargs="*", default=None,
                         help="Ruta(s) a modulo(s) Odoo. Si se omite: escanea el directorio actual "
                              "(o el indicado con --repo) buscando modulos (carpetas con __manifest__.py).")
    parser.add_argument("--repo", default=".",
                         help="Ruta del repositorio a escanear cuando no se indican modulos explicitos (default: .)")
    # OJO: default=None aqui a proposito. Si estos llevaran un valor por
    # defecto (ej. "localhost"), "args.host or opt.get(...)" en apply_odoo_conf
    # nunca tomaria el valor del --odoo-conf porque el default ya es truthy.
    # Prioridad final: flag de linea de comandos > --odoo-conf > variable de entorno > default duro.
    parser.add_argument("--db", default=None)
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--user", default=None)
    parser.add_argument("--password", default=None)
    parser.add_argument("--odoo-conf", help="Ruta a odoo.conf para leer credenciales de BD")
    parser.add_argument("--no-db", action="store_true",
                         help="No conectar a BD: asume tablas vacias (todo se reporta como nuevo)")
    args = parser.parse_args()
    args = apply_odoo_conf(args)

    # Ahora sí, variables de entorno y defaults duros como ultimo recurso.
    args.db = args.db or os.environ.get("DDL_DB_NAME")
    args.host = args.host or os.environ.get("DDL_DB_HOST") or "localhost"
    args.port = args.port or int(os.environ.get("DDL_DB_PORT", 5432))
    args.user = args.user or os.environ.get("DDL_DB_USER") or "odoo"
    args.password = args.password or os.environ.get("DDL_DB_PASSWORD") or "odoo"

    if args.module_path:
        module_paths = [Path(p).resolve() for p in args.module_path]
        for mp in module_paths:
            if not mp.exists():
                sys.exit(f"Ruta de modulo no existe: {mp}")
    else:
        repo_path = Path(args.repo).resolve()
        if not repo_path.exists():
            sys.exit(f"Ruta de repositorio no existe: {repo_path}")
        module_paths = discover_modules(repo_path)
        if not module_paths:
            sys.exit(f"No se encontraron modulos (__manifest__.py) en {repo_path}")

    if args.no_db:
        class _FakeCur:
            def execute(self, *a, **k): pass
            def fetchone(self): return None
            def fetchall(self): return []
        def cur_factory():
            return _FakeCur()
    else:
        if not args.db:
            sys.exit("Falta --db (o DDL_DB_NAME / odoo.conf)")
        # una conexion por modulo, se cierra al terminar cada uno
        def cur_factory():
            conn = get_db_connection(args)
            return conn.cursor()  # psycopg2 cursor expone .connection nativamente

    total = 0
    for module_path in module_paths:
        total += process_module(module_path, args, cur_factory)

    if len(module_paths) > 1:
        print(f"\nTotal: {total} sentencia(s) DDL en {len(module_paths)} modulo(s).")


if __name__ == "__main__":
    main()
