"""Trabajos que ejecuta la GUI (sin dependencias de Qt, testeables por separado).

Cada funcion recibe `log(str)` y devuelve un dict "result" homogeneo:
    outputs : [(etiqueta, ruta_sql, ruta_reporte|None, script)]
    items   : [(origen, Item)]
    stats   : {"ok","warn","skip","error"}
    dry     : [(origen, dry_dict)]
    notes   : [str]
"""
import argparse
import contextlib
import io
import re
from pathlib import Path

import extract_ddl as ex
import validate_ddl as vd
from ddl_common import Catalog, connect


class _LogStream(io.TextIOBase):
    """Redirige print() del core al log de la GUI."""

    def __init__(self, log):
        self.log, self.buf = log, ""

    def write(self, s):
        self.buf += s
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            if line.strip():
                self.log(line)
        return len(s)


def make_args(conn, opts, **extra):
    """conn: dict host/port/db/user/password/schema/offline. opts: dict de opciones."""
    ns = argparse.Namespace(
        host=conn.get("host") or "localhost", port=int(conn.get("port") or 5432),
        db=conn.get("db"), user=conn.get("user") or "odoo", password=conn.get("password") or "",
        schema=conn.get("schema") or "public",
        guard=opts.get("guard", True), commit=opts.get("commit", False),
        lock_timeout=opts.get("lock_timeout", "15s"), data_checks=opts.get("data_checks", True),
        fk_not_valid=opts.get("fk_not_valid", False), allow_drop=opts.get("allow_drop", False),
        dry_run=opts.get("dry_run", False) and not conn.get("offline"),
        deploy=opts.get("deploy", True), target_db=opts.get("target_db") or None,
        deploy_strict=opts.get("deploy_strict", True),
    )
    for k, v in extra.items():
        setattr(ns, k, v)
    return ns


def _catalog(conn, args, log):
    if conn.get("offline"):
        log("[bd] modo sin BD: validacion estatica + guards en runtime")
        return Catalog(None), "(sin BD)"
    with contextlib.redirect_stdout(_LogStream(log)):
        c = connect(args)
    cat = Catalog(c, (args.schema,))
    log(f"[bd] catalogo: {len(cat.tables)} tablas/vistas en '{args.schema}'")
    return cat, f"{args.user}@{args.host}:{args.port}/{args.db}"


def _stats_add(acc, st):
    for k in acc:
        acc[k] += (st or {}).get(k, 0)


def test_connection(conn, log):
    args = make_args(conn, {})
    with contextlib.redirect_stdout(_LogStream(log)):
        c = connect(args)
    cur = c.cursor()
    cur.execute("SELECT version()")
    version = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM pg_tables WHERE schemaname = %s", (args.schema,))
    n = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM pg_tables WHERE schemaname = %s AND tablename = 'ir_module_module'",
                (args.schema,))
    is_odoo = cur.fetchone()[0] > 0
    odoo = None
    if is_odoo:
        cur.execute("SELECT latest_version FROM ir_module_module WHERE name = 'base'")
        row = cur.fetchone()
        odoo = row[0] if row else None
    c.close()
    return {"version": version.split(",")[0], "tables": n, "odoo": odoo}


def run_validate(conn, opts, files, out_path, log):
    args = make_args(conn, opts)
    errs = []
    files = vd.collect_inputs(files, fail=lambda m: errs.append(m))
    if errs:
        raise ValueError(errs[0])
    log(f"[validar] {len(files)} archivo(s): {', '.join(f.name for f in files)}")
    cat, label = _catalog(conn, args, log)
    items, script, executable, stats, meta = vd.validate_files(files, cat, args, label)
    out = Path(out_path) if out_path else vd.default_output_path(files)
    w = vd.write_outputs(items, script, executable, stats, meta, args, out, log=log)
    log(f"[validar] {stats['ok']} ok | {stats['warn']} warn | {stats['skip']} omitidos | "
        f"{stats['error']} errores")
    log(f"[validar] revision  : {w['out']}")
    if w["deploy"]:
        log(f"[validar] despliegue: {w['deploy']}  (para el DBA, termina en COMMIT)")
    log(f"[validar] reporte   : {w['report']}")
    src = out.name
    outputs = [(src, out, w["report"], script)]
    if w["deploy"]:
        outputs.append(("DBA (produccion)", w["deploy"], w["report"], w["deploy_script"]))
    return {"outputs": outputs, "items": [(src, it) for it in items], "stats": stats,
            "dry": [(src, w["dry"])] if w["dry"] else [], "notes": [],
            "deploy": [str(w["deploy"])] if w["deploy"] else []}


def find_modules(paths):
    mods = []
    for p in paths:
        p = Path(p)
        found = ex.discover_modules(p) if p.is_dir() else []
        for m in found:
            if m not in mods:
                mods.append(m)
    return mods


def run_extract(conn, opts, module_paths, log):
    args = make_args(conn, opts, odoo_version=int(opts.get("odoo_version", 17)),
                     id_type=opts.get("id_type", "BIGINT"), raw=opts.get("raw", False))
    module_paths = [Path(p) for p in module_paths]
    for mp in module_paths:
        if not ex.is_module_dir(mp):
            raise ValueError(f"No es un modulo Odoo (falta __manifest__.py): {mp}")
    cat, label = _catalog(conn, args, log)
    scanned, model_tables = ex.scan_all(module_paths)
    result = {"outputs": [], "items": [], "stats": {"ok": 0, "warn": 0, "skip": 0, "error": 0},
              "dry": [], "notes": [], "deploy": []}
    for mp, models in scanned:
        r = ex.process_module(mp, models, args, cat, model_tables, label, log=log)
        result["notes"] += [f"{mp.name}: {n}" for n in r["notes"]]
        if r["out_file"]:
            result["outputs"].append((mp.name, r["out_file"], r["report"], r["script"]))
        if r.get("deploy"):
            result["outputs"].append((f"DBA: {mp.name}", r["deploy"], r["report"], r["deploy_script"]))
            result["deploy"].append(str(r["deploy"]))
        result["items"] += [(mp.name, it) for it in r["items"]]
        _stats_add(result["stats"], r["stats"])
        if r["dry"]:
            result["dry"].append((mp.name, r["dry"]))
    return result


MISSING_RX = re.compile(r"no existe la tabla (?:referenciada |origen )?(\S+)")


def missing_relations(items):
    """{tabla: [(origen, label)]} para errores por tabla inexistente."""
    out = {}
    for src, it in items:
        for sev, msg in it.msgs:
            m = MISSING_RX.search(msg)
            if m and sev in ("error", "warn"):
                out.setdefault(m.group(1), []).append((src, it.label))
    return out
