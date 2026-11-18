#!/usr/bin/env python3
"""
extract_ddl.py — Extractor de DDL para modelos/campos nuevos en modulos Odoo.

Se ejecuta a nivel de REPOSITORIO. Se le puede pasar:
  - una o varias rutas de modulo explicitas, o
  - --repo <ruta> para autodetectar los modulos (carpetas con __manifest__.py).

Compara los campos definidos en los modelos Python (via AST, sin levantar Odoo)
contra el esquema real de PostgreSQL y emite el DDL de lo que falta.

Desde v2 el DDL generado pasa por validate_ddl.py antes de escribirse:
  - FKs hacia tablas inexistentes en la BD quedan comentadas con el motivo.
  - FKs/constraints envueltos en DO blocks con chequeo en runtime.
  - IF NOT EXISTS, orden tablas -> columnas -> indices -> FKs.
  - Todo dentro de BEGIN; ... ROLLBACK; (o COMMIT con --commit).
  - Reporte .report.md al lado del .sql.
Usa --raw para obtener el DDL sin validar (comportamiento v1).

Uso:
    python extract_ddl.py mod_a mod_b --env /ruta/.env
    python extract_ddl.py --repo /ruta/al/repo --odoo-conf odoo.conf
    python extract_ddl.py mod_a --db odoo_db --host localhost --port 5433 --user odoo --password x
    python extract_ddl.py mod_a --no-db            # dump completo, guards en runtime

Credenciales (prioridad): flag > --odoo-conf > --env > DDL_DB_* > defaults.

Salida (por modulo):
    <modulo>/ddl/extract_<ts>.sql          -> DDL validado (o crudo con --raw)
    <modulo>/ddl/extract_<ts>.report.md    -> reporte de validacion
    <modulo>/.ddl_snapshot.json            -> snapshot de campos conocidos
"""
import argparse
import ast
import datetime as dt
import json
import sys
from pathlib import Path

from ddl_common import Catalog, add_db_arguments, connect, qi, resolve_db_config, short_name
import validate_ddl as vd

# -------------------------------------------------------------------------
# Mapeo de tipos Odoo -> PostgreSQL
# -------------------------------------------------------------------------
FIELD_TYPE_MAP = {
    "Char": "VARCHAR",
    "Text": "TEXT",
    "Html": "TEXT",
    "Integer": "INTEGER",
    "Many2oneReference": "INTEGER",
    "Monetary": "NUMERIC",
    "Boolean": "BOOLEAN",
    "Date": "DATE",
    "Datetime": "TIMESTAMP",
    "Selection": "VARCHAR",
    "Reference": "VARCHAR",
    "Json": "JSONB",
    "Properties": "JSONB",
    "Binary": "BYTEA",
    "Image": "BYTEA",
}
NO_COLUMN = {"One2many", "Many2many", "PropertiesDefinition"}
TRANSLATABLE = {"Char", "Text", "Html"}

# Convencion de este proyecto para ids de Many2one/create_uid/write_uid.
# Odoo estandar usa int4 -> --id-type INTEGER.
ID_COLUMN_TYPE = "BIGINT"

VALID_ONDELETE = {"SET NULL", "CASCADE", "RESTRICT", "NO ACTION", "SET DEFAULT"}
MODEL_BASES = {"Model", "TransientModel", "AbstractModel"}


class OdooModel:
    def __init__(self, name, table, fields_, inherit=False, source_file="",
                 abstract=False, auto=True):
        self.name = name
        self.table = table
        self.fields = fields_
        self.inherit = inherit          # solo _inherit (extiende modelo existente)
        self.source_file = source_file
        self.abstract = abstract
        self.auto = auto                # _auto = False -> vista SQL, sin DDL


def odoo_model_to_table(model_name: str) -> str:
    return model_name.replace(".", "_")


def literal_or_none(node):
    try:
        return ast.literal_eval(node)
    except Exception:  # noqa: BLE001
        return None


def parse_field_call(call: ast.Call):
    """Extrae tipo y kwargs relevantes de fields.X(...)"""
    if not isinstance(call.func, ast.Attribute):
        return None
    if not isinstance(call.func.value, ast.Name) or call.func.value.id != "fields":
        return None
    ftype = call.func.attr

    info = {"type": ftype, "size": None, "required": False, "comodel": None,
            "relation": None, "column1": None, "column2": None, "digits": None,
            "ondelete": None, "compute": None, "store": None, "related": None,
            "translate": False, "attachment": None, "company_dependent": False}

    if call.args:
        first = literal_or_none(call.args[0])
        if ftype in ("Many2one", "Many2many", "One2many") and isinstance(first, str):
            info["comodel"] = first
        if ftype == "Many2many":
            pos = [literal_or_none(a) for a in call.args]
            for idx, key in ((1, "relation"), (2, "column1"), (3, "column2")):
                if len(pos) > idx and isinstance(pos[idx], str):
                    info[key] = pos[idx]

    for kw in call.keywords:
        if kw.arg is None:
            continue
        val = literal_or_none(kw.value)
        if kw.arg == "comodel_name":
            info["comodel"] = val
        elif kw.arg == "required":
            info["required"] = val is True
        elif kw.arg == "translate":
            # translate=True o translate=<callable> (html_translate) -> traducible
            info["translate"] = val is True or (val is None and not isinstance(kw.value, ast.Constant))
        elif kw.arg == "compute":
            info["compute"] = val if val is not None else "<callable>"
        elif kw.arg == "related":
            info["related"] = val
        elif kw.arg in ("size", "store", "relation", "column1", "column2",
                        "digits", "ondelete", "attachment", "company_dependent"):
            info[kw.arg] = val if kw.arg != "digits" else (val if val is not None else "<expr>")
    return info


def _class_attr(stmt):
    """(nombre, nodo_valor) para Assign / AnnAssign simples."""
    if isinstance(stmt, ast.Assign):
        names = [t.id for t in stmt.targets if isinstance(t, ast.Name)]
        return (names[0], stmt.value) if names else (None, None)
    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value:
        return stmt.target.id, stmt.value
    return None, None


def parse_models_from_file(path: Path):
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return []

    models = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        bases = {b.attr if isinstance(b, ast.Attribute) else getattr(b, "id", None) for b in node.bases}
        if not bases & MODEL_BASES:
            continue

        _name = _inherit = _table = None
        auto = True
        model_fields = {}
        for stmt in node.body:
            attr, value = _class_attr(stmt)
            if not attr:
                continue
            if attr == "_name":
                _name = literal_or_none(value)
            elif attr == "_inherit":
                _inherit = literal_or_none(value)
            elif attr == "_table":
                _table = literal_or_none(value)
            elif attr == "_auto":
                auto = literal_or_none(value) is not False
            elif isinstance(value, ast.Call):
                finfo = parse_field_call(value)
                if finfo:
                    model_fields[attr] = finfo

        if isinstance(_inherit, (list, tuple)):
            _inherit = _inherit[0] if _inherit else None
        model_name = _name or _inherit
        if not model_name or not isinstance(model_name, str):
            continue
        abstract = "AbstractModel" in bases
        models.append(OdooModel(
            name=model_name,
            table=_table or odoo_model_to_table(model_name),
            fields_=model_fields,
            inherit=(_name is None or _name == _inherit),
            source_file=str(path),
            abstract=abstract,
            auto=auto,
        ))
    return models


def scan_module(module_path: Path):
    """Escanea y FUSIONA clases del mismo modelo (varias clases _inherit del
    mismo modelo en el modulo => un solo set de campos)."""
    models_dir = module_path / "models"
    if not models_dir.exists():
        models_dir = module_path
    merged = {}
    for py_file in sorted(models_dir.rglob("*.py")):
        for m in parse_models_from_file(py_file):
            if m.name in merged:
                base = merged[m.name]
                base.fields.update(m.fields)
                base.inherit = base.inherit and m.inherit
                base.abstract = base.abstract and m.abstract
                base.auto = base.auto and m.auto
                if not m.inherit:
                    base.table = m.table
                    base.source_file = m.source_file
            else:
                merged[m.name] = m
    return list(merged.values())


# -------------------------------------------------------------------------
# Tipos / nombres
# -------------------------------------------------------------------------
def is_stored(finfo, odoo_version):
    if finfo.get("store") is False:
        return False
    if finfo.get("store") is True:
        return True
    if finfo.get("compute") or finfo.get("related"):
        return False
    if finfo.get("company_dependent") and odoo_version < 18:
        return False  # ir.property, sin columna
    if finfo["type"] in ("Binary", "Image") and finfo.get("attachment") is not False:
        return False  # se guarda en ir_attachment
    return True


def column_ddl_type(finfo, odoo_version=17, id_type=ID_COLUMN_TYPE):
    ftype = finfo["type"]
    if ftype in NO_COLUMN:
        return None
    if ftype == "Many2one":
        return id_type
    if finfo.get("company_dependent") and odoo_version >= 18:
        return "JSONB"
    if ftype in TRANSLATABLE and finfo.get("translate") and odoo_version >= 16:
        return "JSONB"
    if ftype == "Float":
        # Odoo: con digits -> numeric (sin precision fija); sin digits -> float8
        return "NUMERIC" if finfo.get("digits") is not None else "DOUBLE PRECISION"
    pg_type = FIELD_TYPE_MAP.get(ftype, "VARCHAR")
    if pg_type == "VARCHAR" and isinstance(finfo.get("size"), int):
        pg_type = f"VARCHAR({finfo['size']})"
    return pg_type


def m2o_ondelete(finfo):
    od = finfo.get("ondelete")
    if not od:
        # misma regla que Odoo: required => restrict, si no => set null
        od = "restrict" if finfo.get("required") else "set null"
    od = str(od).upper()
    return od if od in VALID_ONDELETE else "SET NULL"


def fk_statement(table, fname, comodel_table, ondelete):
    cname = short_name(f"{table}_{fname}_fkey")
    return (f"ALTER TABLE {qi(table)} ADD CONSTRAINT {qi(cname)} "
            f"FOREIGN KEY ({qi(fname)}) REFERENCES {qi(comodel_table)}(id) ON DELETE {ondelete};")


def m2m_relation_names(table1, table2, finfo):
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
        column1, column2 = f"{column1}1", f"{column2}2"
    return relation, column1, column2


# -------------------------------------------------------------------------
# Generacion de DDL
# -------------------------------------------------------------------------
def build_ddl(models, cat, opts, model_tables):
    """Devuelve (statements, snapshot, avisos). cat: Catalog (online u offline)."""
    statements, notes = [], []
    snapshot = {}
    seen_relations = set()
    schema = opts.schema
    audit = [("create_uid", opts.id_type, "res_users", "SET NULL"),
             ("create_date", "TIMESTAMP", None, None),
             ("write_uid", opts.id_type, "res_users", "SET NULL"),
             ("write_date", "TIMESTAMP", None, None)]

    def comodel_table(comodel):
        return model_tables.get(comodel) or odoo_model_to_table(comodel)

    for model in models:
        table = model.table
        key = (schema, table)
        snapshot[model.name] = {"table": table, "fields": sorted(model.fields)}

        if model.abstract or not model.auto:
            notes.append(f"{model.name}: {'AbstractModel' if model.abstract else '_auto=False'}, sin tabla")
            continue

        stored = {f: i for f, i in model.fields.items() if is_stored(i, opts.odoo_version)}

        if not cat.has_table(key) and model.inherit and cat.online:
            # extiende un modelo cuya tabla no existe: modulo base no instalado
            notes.append(f"{model.name}: _inherit de tabla inexistente {table} "
                         f"(modulo base no instalado?). Campos omitidos: {', '.join(sorted(stored)) or '-'}")
            continue
        if not cat.has_table(key) and not model.inherit:
            # Modelo nuevo. (Sin BD, un _inherit va por ADD COLUMN con guard, no CREATE TABLE.)
            cols = ["id SERIAL NOT NULL"]
            fks = []
            for fname, finfo in stored.items():
                pg_type = column_ddl_type(finfo, opts.odoo_version, opts.id_type)
                if pg_type is None:
                    continue
                cols.append(f"{qi(fname)} {pg_type}{' NOT NULL' if finfo.get('required') else ''}")
                if finfo["type"] == "Many2one" and finfo.get("comodel"):
                    fks.append(fk_statement(table, fname, comodel_table(finfo["comodel"]), m2o_ondelete(finfo)))
            for cname, ctype, ref, od in audit:
                if cname in stored:
                    continue
                cols.append(f"{cname} {ctype}")
                if ref:
                    fks.append(fk_statement(table, cname, ref, od))
            cols.append(f"CONSTRAINT {qi(short_name(table + '_pkey'))} PRIMARY KEY (id)")
            statements.append(f"-- Modelo nuevo: {model.name} ({model.source_file})\n"
                              f"CREATE TABLE {qi(table)} (\n    " + ",\n    ".join(cols) + "\n);")
            if fks:
                statements.append(f"-- Foreign keys de {model.name}\n" + "\n".join(fks))
        else:
            in_db = cat.cols(key)
            for fname, finfo in stored.items():
                if fname in in_db:
                    continue
                pg_type = column_ddl_type(finfo, opts.odoo_version, opts.id_type)
                if pg_type is None:
                    continue
                stmt = (f"-- Campo nuevo: {model.name}.{fname} ({model.source_file})\n"
                        f"ALTER TABLE {qi(table)} ADD COLUMN {qi(fname)} {pg_type}"
                        f"{' NOT NULL' if finfo.get('required') else ''};")
                if finfo["type"] == "Many2one" and finfo.get("comodel"):
                    stmt += "\n" + fk_statement(table, fname, comodel_table(finfo["comodel"]), m2o_ondelete(finfo))
                statements.append(stmt)

        # Many2many: tabla puente
        for fname, finfo in model.fields.items():
            if finfo["type"] != "Many2many":
                continue
            if (finfo.get("compute") or finfo.get("related")) and not finfo.get("store"):
                continue
            comodel = finfo.get("comodel")
            ctable = comodel_table(comodel) if comodel else None
            relation, c1, c2 = m2m_relation_names(table, ctable or "unknown", finfo)
            if relation in seen_relations or cat.has_table((schema, relation)):
                seen_relations.add(relation)
                continue
            statements.append(
                f"-- Many2many nuevo: {model.name}.{fname} -> {comodel or '?'} ({model.source_file})\n"
                f"CREATE TABLE {qi(relation)} (\n"
                f"    {qi(c1)} INTEGER NOT NULL REFERENCES {qi(table)}(id) ON DELETE CASCADE,\n"
                f"    {qi(c2)} INTEGER NOT NULL"
                + (f" REFERENCES {qi(ctable)}(id) ON DELETE CASCADE" if ctable else "")
                + f",\n    PRIMARY KEY ({qi(c1)}, {qi(c2)})\n);\n"
                f"CREATE INDEX IF NOT EXISTS {qi(short_name(relation + '_' + c2 + '_' + c1 + '_idx'))} "
                f"ON {qi(relation)} ({qi(c2)}, {qi(c1)});")
            seen_relations.add(relation)

    return statements, snapshot, notes


def load_snapshot(module_path: Path):
    f = module_path / ".ddl_snapshot.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}


def save_snapshot(module_path: Path, snapshot: dict):
    (module_path / ".ddl_snapshot.json").write_text(
        json.dumps(snapshot, indent=2, ensure_ascii=False), encoding="utf-8")


def is_module_dir(path: Path) -> bool:
    return (path / "__manifest__.py").exists() or (path / "__openerp__.py").exists()


def discover_modules(repo_path: Path):
    if is_module_dir(repo_path):
        return [repo_path]
    return [p for p in sorted(repo_path.iterdir()) if p.is_dir() and is_module_dir(p)]


def process_module(module_path, models, args, cat, model_tables, db_label, log=print):
    """Procesa un modulo. Devuelve dict con el resultado (usado por CLI y GUI):
    module, out_file, report, stats, items, notes, dry, n, errors, raw."""
    res = {"module": module_path.name, "out_file": None, "report": None, "stats": None,
           "items": [], "notes": [], "dry": None, "n": 0, "errors": 0, "raw": args.raw,
           "script": "", "deploy": None, "deploy_script": None}
    if not models:
        log(f"[{module_path.name}] Sin modelos Odoo detectados, se omite.")
        return res

    statements, snapshot, notes = build_ddl(models, cat, args, model_tables)
    res["notes"] = notes
    for n in notes:
        log(f"[{module_path.name}]   nota: {n}")
    save_snapshot(module_path, snapshot)

    if not statements:
        log(f"[{module_path.name}] Sin modelos ni campos nuevos respecto a la base de datos.")
        return res

    out_dir = module_path / "ddl"
    out_dir.mkdir(exist_ok=True)
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = "_raw" if args.raw else ""
    out_file = out_dir / f"extract_{ts}{suffix}.sql"
    k = 1
    while out_file.exists():
        k += 1
        out_file = out_dir / f"extract_{ts}{suffix}_{k}.sql"
    raw = "\n\n".join(statements) + "\n"
    res["out_file"] = out_file

    if args.raw:
        out_file.write_text(raw, encoding="utf-8")
        res.update(script=raw, n=len(statements))
        log(f"[{module_path.name}] {len(statements)} bloque(s) DDL crudo -> {out_file}")
        return res

    items, script, executable, stats, meta = vd.validate_text(raw, out_file.name, cat, args,
                                                              db_label, notes)
    w = vd.write_outputs(items, script, executable, stats, meta, args, out_file, log=lambda m: None)
    dry = w["dry"]
    res.update(items=items, stats=stats, dry=dry, report=w["report"], script=script, n=len(items),
               deploy=w["deploy"], deploy_script=w["deploy_script"],
               errors=stats["error"] + (len(dry["errors"]) if dry else 0))

    log(f"[{module_path.name}] {stats['ok']} ok | {stats['warn']} warn | {stats['skip']} omitidos | "
        f"{stats['error']} errores -> {out_file}")
    if w["deploy"]:
        log(f"[{module_path.name}] despliegue (DBA) -> {w['deploy']}")
    for it in items:
        if it.status == "error":
            log(f"    [ERROR] {it.label}: " + "; ".join(m for s, m in it.msgs if s == "error"))
    if dry:
        log(f"    [dry-run] {dry['n']} sentencias, {len(dry['errors'])} con error")
        for i, _, err in dry["errors"]:
            log(f"      #{i}: {vd.ws(err)}")
    return res


def scan_all(module_paths):
    """Escaneo global: [(path, models)] y mapa modelo -> tabla (para _table de comodelos)."""
    scanned = [(mp, scan_module(mp)) for mp in module_paths]
    model_tables = {m.name: m.table for _, ms in scanned for m in ms if not m.inherit}
    return scanned, model_tables


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("module_path", nargs="*", default=None, help="Ruta(s) a modulo(s) Odoo")
    ap.add_argument("--repo", default=".", help="Repo a escanear si no se indican modulos (default: .)")
    add_db_arguments(ap)
    ap.add_argument("--no-db", action="store_true",
                    help="No conectar a BD: todo se reporta como nuevo, FKs con guard en runtime")
    ap.add_argument("--odoo-version", type=int, default=17,
                    help="Version de Odoo (afecta translate->jsonb en >=16, company_dependent en >=18)")
    ap.add_argument("--id-type", default=ID_COLUMN_TYPE, help="Tipo de columnas Many2one (default BIGINT)")
    ap.add_argument("--raw", action="store_true", help="No validar: DDL crudo como en v1")
    ap.add_argument("--commit", action="store_true", help="Terminar el script con COMMIT")
    ap.add_argument("--no-guard", dest="guard", action="store_false")
    ap.add_argument("--fk-not-valid", action="store_true")
    ap.add_argument("--no-data-checks", dest="data_checks", action="store_false")
    ap.add_argument("--lock-timeout", default="15s")
    ap.add_argument("--dry-run", action="store_true",
                    help="Ejecutar el DDL validado en la BD con SAVEPOINTs y ROLLBACK final")
    ap.add_argument("--no-deploy", dest="deploy", action="store_false",
                    help="No generar el .deploy.sql para el DBA")
    ap.add_argument("--target-db", default=None, help="BD de produccion (el .deploy.sql la verifica)")
    ap.add_argument("--deploy-lenient", dest="deploy_strict", action="store_false",
                    help="Si al final falta algun objeto, hacer COMMIT parcial y solo avisar (default: revertir todo)")
    args = ap.parse_args()
    args.allow_drop = False

    if args.module_path:
        module_paths = [Path(p).resolve() for p in args.module_path]
        for mp in module_paths:
            if not mp.exists():
                sys.exit(f"Ruta de modulo no existe: {mp}")
    else:
        repo = Path(args.repo).resolve()
        if not repo.exists():
            sys.exit(f"Ruta de repositorio no existe: {repo}")
        module_paths = discover_modules(repo)
        if not module_paths:
            sys.exit(f"No se encontraron modulos (__manifest__.py) en {repo}")

    if args.no_db:
        if args.dry_run:
            sys.exit("--dry-run requiere BD")
        cat, db_label = Catalog(None), "(sin BD)"
    else:
        args = resolve_db_config(args)
        cat = Catalog(connect(args), (args.schema,))
        db_label = f"{args.user}@{args.host}:{args.port}/{args.db}"

    # Escaneo global primero: permite resolver _table de comodelos definidos en otros modulos
    scanned, model_tables = scan_all(module_paths)

    total = errors = 0
    for mp, models in scanned:
        r = process_module(mp, models, args, cat, model_tables, db_label)
        total, errors = total + r["n"], errors + r["errors"]

    if len(module_paths) > 1:
        print(f"\nTotal: {total} sentencia(s) en {len(module_paths)} modulo(s), {errors} error(es).")
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
