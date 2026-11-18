#!/usr/bin/env python3
"""
validate_ddl.py — Valida scripts DDL (extraidos con extract_ddl.py, DBeaver,
pg_dump, a mano...) contra una base PostgreSQL real y genera una version
"segura" lista para ejecutar dentro de BEGIN; ... ROLLBACK;.

Que valida (contra la BD + lo que el propio script crea antes):
  - CREATE TABLE      -> si ya existe: se omite y se generan ADD COLUMN para las
                         columnas faltantes. Colision de nombres (pkey, secuencias).
  - FOREIGN KEY       -> tabla/columnas origen, tabla/columnas referenciadas,
                         que la referencia sea PK/UNIQUE, compatibilidad de tipos,
                         constraint duplicado (mismo nombre o misma firma) y
                         filas huerfanas en tablas con datos.
  - ADD COLUMN        -> tabla existe, columna duplicada, NOT NULL sin DEFAULT
                         sobre tabla con datos.
  - CREATE INDEX      -> nombre ya usado, tabla/columnas existen.
  - PK/UNIQUE/CHECK   -> tabla/columnas, constraint duplicado.

Mitigaciones en el script generado:
  - Reordena: tablas -> columnas -> indices/constraints -> FKs (evita referencias
    hacia adelante).
  - FKs declaradas inline (REFERENCES dentro de CREATE TABLE) se separan en
    ALTER TABLE para poder validarlas/omitirlas sin perder la tabla.
  - IF NOT EXISTS en tablas, columnas e indices.
  - FKs y constraints envueltos en DO $$ ... $$ con chequeo en tiempo de
    ejecucion (to_regclass / pg_constraint): si la relacion no existe en la BD
    donde se corre, emite WARNING y sigue en vez de abortar la transaccion.
  - Lo invalido queda COMENTADO con el motivo, nunca se borra.
  - Envuelve todo en BEGIN; ... ROLLBACK; (o COMMIT con --commit).

Uso:
    python validate_ddl.py ddl/*.sql --env /ruta/.env
    python validate_ddl.py carpeta_con_sqls/ --env .env --dry-run
    python validate_ddl.py extract.sql --db mi_bd --host 10.0.0.5 --port 5433
    python validate_ddl.py extract.sql --no-db        # sin BD: solo guards en runtime

--dry-run ejecuta el script resultante sentencia por sentencia (SAVEPOINT por
sentencia) dentro de una transaccion y SIEMPRE hace ROLLBACK: detecta lo que el
analisis estatico no ve (permisos, locks, sintaxis rara).

Exit code: 0 sin errores, 1 si hay errores (o warnings con --strict).
"""
import argparse
import datetime as dt
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ddl_common import (Catalog, add_db_arguments, connect, is_serial, norm_ident,
                        norm_type, qi, qname, resolve_db_config, short_name, sql_lit,
                        type_family, MAX_IDENT)

# -------------------------------------------------------------------------
# Lexer / splitter
# -------------------------------------------------------------------------
_DOLLAR = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")


def _lex(text):
    """Yield (kind, text, offset). kind: code | comment | semi."""
    i, n = 0, len(text)
    buf_start, buf = 0, []

    def flush():
        if buf:
            yield ("code", "".join(buf), buf_start)
            buf.clear()

    while i < n:
        c = text[i]
        if text.startswith("--", i):
            yield from flush()
            j = text.find("\n", i)
            j = n if j < 0 else j
            yield ("comment", text[i:j], i)
            i = j
            continue
        if text.startswith("/*", i):
            yield from flush()
            depth, j = 1, i + 2
            while j < n and depth:
                if text.startswith("/*", j):
                    depth, j = depth + 1, j + 2
                elif text.startswith("*/", j):
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            yield ("comment", text[i:j], i)
            i = j
            continue
        if c == ";":
            yield from flush()
            yield ("semi", ";", i)
            i += 1
            continue
        if not buf:
            buf_start = i
        if c in ("'", '"'):
            j = i + 1
            while j < n:
                if text[j] == c:
                    if j + 1 < n and text[j + 1] == c:
                        j += 2
                        continue
                    break
                j += 1
            buf.append(text[i:j + 1])
            i = j + 1
            continue
        if c == "$":
            m = _DOLLAR.match(text, i)
            if m:
                tag = m.group(0)
                end = text.find(tag, m.end())
                end = n if end < 0 else end + len(tag)
                buf.append(text[i:end])
                i = end
                continue
        buf.append(c)
        i += 1
    yield from flush()


@dataclass
class RawStmt:
    code: str
    comments: list
    src: str


def split_sql(text, fname):
    out, code, comments, first_off = [], [], [], None
    for kind, tok, off in _lex(text):
        if kind == "comment":
            if not "".join(code).strip():
                comments.append(tok)
            continue
        if kind == "code":
            if first_off is None and tok.strip():
                first_off = off + (len(tok) - len(tok.lstrip()))
            code.append(tok)
            continue
        # semi
        stmt = "".join(code).strip()
        if stmt:
            line = text.count("\n", 0, first_off) + 1
            out.append(RawStmt(stmt, comments, f"{fname}:{line}"))
        code, comments, first_off = [], [], None
    stmt = "".join(code).strip()
    if stmt:
        line = text.count("\n", 0, first_off or 0) + 1
        out.append(RawStmt(stmt, comments, f"{fname}:{line}"))
    return out


def split_top(s, sep=","):
    """Split por separador a nivel 0 de parentesis, respetando comillas."""
    parts, depth, cur, i = [], 0, [], 0
    while i < len(s):
        c = s[i]
        if c in ("'", '"'):
            j = i + 1
            while j < len(s) and not (s[j] == c and (j + 1 >= len(s) or s[j + 1] != c)):
                j += 2 if s[j] == c else 1
            cur.append(s[i:j + 1])
            i = j + 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        if c == sep and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(c)
        i += 1
    if "".join(cur).strip():
        parts.append("".join(cur).strip())
    return parts


# -------------------------------------------------------------------------
# Parser
# -------------------------------------------------------------------------
I_ = r'(?:"(?:[^"]|"")+"|[A-Za-z_][A-Za-z0-9_$]*)'
QN = rf"{I_}(?:\s*\.\s*{I_})?"
FLAGS = re.I | re.S
REF_ACTIONS = (r"(?:\s+(?:ON\s+(?:DELETE|UPDATE)\s+(?:SET\s+NULL|SET\s+DEFAULT|CASCADE|RESTRICT|NO\s+ACTION)"
               r"|MATCH\s+(?:FULL|PARTIAL|SIMPLE)|NOT\s+DEFERRABLE|DEFERRABLE"
               r"|INITIALLY\s+(?:DEFERRED|IMMEDIATE)|NOT\s+VALID))*")
INLINE_REF = re.compile(
    rf"(?:CONSTRAINT\s+({I_})\s+)?REFERENCES\s+({QN})\s*(?:\(([^)]*)\))?({REF_ACTIONS})", FLAGS)
TYPE_END = re.compile(
    r"\b(CONSTRAINT|NOT\s+NULL|NULL|DEFAULT|PRIMARY\s+KEY|UNIQUE|REFERENCES|CHECK|COLLATE|GENERATED)\b", re.I)
TABLE_CONS = re.compile(
    rf"^(?:CONSTRAINT\s+({I_})\s+)?(PRIMARY\s+KEY|UNIQUE|FOREIGN\s+KEY|CHECK|EXCLUDE)\b(.*)$", FLAGS)


def parse_qn(s, default_schema):
    parts = [p for p in split_top(s.strip(), ".")]
    if len(parts) == 2:
        return norm_ident(parts[0]), norm_ident(parts[1])
    return default_schema, norm_ident(parts[0])


def parse_cols(s):
    return [norm_ident(c) for c in split_top(s or "") if c.strip()]


def ws(s):
    return re.sub(r"\s+", " ", (s or "").strip())


@dataclass
class Item:
    kind: str                  # table | column | index | constraint | fk | other
    src: str
    comments: list = field(default_factory=list)
    data: dict = field(default_factory=dict)
    sql: str = ""              # SQL final a emitir
    msgs: list = field(default_factory=list)   # [(sev, texto)]
    guard: bool = False
    derived: bool = False      # generado por el validador (no estaba en el script)

    SEV_ORDER = {"info": 0, "warn": 1, "skip": 2, "error": 3}

    def add(self, sev, msg):
        self.msgs.append((sev, msg))

    @property
    def status(self):
        sevs = [s for s, _ in self.msgs if s != "info"]
        if not sevs:
            return "ok"
        return max(sevs, key=self.SEV_ORDER.get)

    @property
    def label(self):
        d = self.data
        if self.kind == "table":
            return f"TABLE {d['key'][1]}"
        if self.kind == "column":
            return f"COLUMN {d['key'][1]}.{d['col']['name']}"
        if self.kind == "index":
            return f"INDEX {d.get('name') or '(sin nombre)'}"
        if self.kind == "fk":
            return f"FK {d['name']} ({d['key'][1]} -> {d['rkey'][1]})"
        if self.kind == "constraint":
            return f"CONSTRAINT {d['name']} ({d['key'][1]})"
        return ws(self.data.get("raw", ""))[:70]


def parse_coldef(text):
    m = re.match(rf"\s*({I_})\s+(.*)$", text, FLAGS)
    if not m:
        return None
    name, rest = norm_ident(m.group(1)), m.group(2)
    fk = None
    rm = INLINE_REF.search(rest)
    if rm:
        fk = {"name": norm_ident(rm.group(1)) if rm.group(1) else None,
              "ref": rm.group(2), "rcols": rm.group(3), "actions": ws(rm.group(4))}
        rest = (rest[:rm.start()] + rest[rm.end():])
    tm = TYPE_END.search(rest)
    typ = (rest[:tm.start()] if tm else rest).strip()
    cons = rest[tm.start():] if tm else ""
    return {
        "name": name, "raw_name": m.group(1), "type": typ,
        "notnull": bool(re.search(r"\bNOT\s+NULL\b", cons, re.I)),
        "pk": bool(re.search(r"\bPRIMARY\s+KEY\b", cons, re.I)),
        "unique": bool(re.search(r"\bUNIQUE\b", cons, re.I)),
        "default": bool(re.search(r"\bDEFAULT\b", cons, re.I)) or is_serial(typ),
        "fk": fk,
        "text": ws(f"{m.group(1)} {rest}"),
    }


def parse_table_constraint(text):
    m = TABLE_CONS.match(text.strip())
    if not m:
        return None
    name = norm_ident(m.group(1)) if m.group(1) else None
    kind = ws(m.group(2)).upper()
    rest = m.group(3)
    if kind == "FOREIGN KEY":
        fm = re.match(rf"\s*\(([^)]*)\)\s*REFERENCES\s+({QN})\s*(?:\(([^)]*)\))?({REF_ACTIONS})\s*$",
                      rest, FLAGS)
        if not fm:
            return {"kind": "raw", "name": name, "text": ws(text)}
        return {"kind": "f", "name": name, "cols": parse_cols(fm.group(1)),
                "ref": fm.group(2), "rcols": fm.group(3), "actions": ws(fm.group(4))}
    cols = None
    cm = re.match(r"\s*\(([^)]*)\)", rest)
    if kind in ("PRIMARY KEY", "UNIQUE") and cm:
        cols = parse_cols(cm.group(1))
    return {"kind": {"PRIMARY KEY": "p", "UNIQUE": "u", "CHECK": "c", "EXCLUDE": "x"}[kind],
            "name": name, "cols": cols, "body": ws(f"{m.group(2)}{rest}"), "text": ws(text)}


def _fk_item(key, cols, fk, default_schema, src, comments, inline=False):
    rkey = parse_qn(fk["ref"], default_schema)
    name = fk.get("name") or short_name(f"{key[1]}_{'_'.join(cols)}_fkey")
    actions = fk.get("actions", "")
    not_valid = bool(re.search(r"\bNOT\s+VALID\b", actions, re.I))
    actions = ws(re.sub(r"\bNOT\s+VALID\b", "", actions, flags=re.I))
    return Item("fk", src, comments, {
        "key": key, "name": name, "cols": cols, "rkey": rkey,
        "rcols": parse_cols(fk["rcols"]) if fk.get("rcols") else None,
        "actions": actions, "not_valid": not_valid, "inline": inline,
    })


def parse_statement(rs, default_schema):
    """RawStmt -> list[Item]"""
    code = rs.code
    head = ws(code[:60]).upper()

    if re.match(r"^(BEGIN|COMMIT|ROLLBACK|END|START\s+TRANSACTION|ABORT)\b", head):
        return []

    # ---- CREATE TABLE -------------------------------------------------
    m = re.match(rf"^CREATE\s+(?:UNLOGGED\s+)?TABLE\s+(IF\s+NOT\s+EXISTS\s+)?({QN})\s*\((.*)\)(.*)$",
                 code, FLAGS)
    if m:
        key = parse_qn(m.group(2), default_schema)
        cols, cons, fks = [], [], []
        for part in split_top(m.group(3)):
            tc = parse_table_constraint(part)
            if tc:
                if tc["kind"] == "f":
                    fks.append(_fk_item(key, tc["cols"], tc, default_schema, rs.src, [], True))
                else:
                    cons.append(tc)
                continue
            cd = parse_coldef(part)
            if cd is None:
                cons.append({"kind": "raw", "name": None, "text": ws(part)})
                continue
            if cd["fk"]:
                fks.append(_fk_item(key, [cd["name"]], cd["fk"], default_schema, rs.src, [], True))
            cols.append(cd)
        tbl = Item("table", rs.src, rs.comments, {
            "key": key, "cols": cols, "cons": cons, "tail": ws(m.group(4)), "raw": code})
        return [tbl] + fks

    # ---- ALTER TABLE ----------------------------------------------------
    m = re.match(rf"^ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?({QN})\s+(.*)$", code, FLAGS)
    if m:
        key = parse_qn(m.group(1), default_schema)
        items = []
        for action in split_top(m.group(2)):
            am = re.match(r"^ADD\s+(?!COLUMN\b)(.*)$", action, FLAGS)
            tc = parse_table_constraint(am.group(1)) if am else None
            if tc and tc["kind"] == "f":
                items.append(_fk_item(key, tc["cols"], tc, default_schema, rs.src, rs.comments))
                continue
            if tc and tc["kind"] in ("p", "u", "c"):
                items.append(Item("constraint", rs.src, rs.comments, {
                    "key": key, "name": tc["name"] or short_name(
                        f"{key[1]}_{'_'.join(tc['cols'] or ['check'])}_"
                        f"{ {'p': 'pkey', 'u': 'key', 'c': 'check'}[tc['kind']] }"),
                    "ckind": tc["kind"], "cols": tc["cols"], "body": tc["body"]}))
                continue
            cm = re.match(r"^ADD\s+(?:COLUMN\s+)?(IF\s+NOT\s+EXISTS\s+)?(.*)$", action, FLAGS)
            if cm and not tc:
                cd = parse_coldef(cm.group(2))
                if cd:
                    items.append(Item("column", rs.src, rs.comments, {"key": key, "col": cd}))
                    if cd["fk"]:
                        items.append(_fk_item(key, [cd["name"]], cd["fk"], default_schema,
                                              rs.src, [], True))
                    continue
            items.append(Item("other", rs.src, rs.comments,
                              {"raw": f"ALTER TABLE {m.group(1)} {action}"}))
        return items

    # ---- CREATE INDEX -------------------------------------------------
    m = re.match(rf"^CREATE\s+(UNIQUE\s+)?INDEX\s+(CONCURRENTLY\s+)?(IF\s+NOT\s+EXISTS\s+)?({I_})?\s*"
                 rf"ON\s+(?:ONLY\s+)?({QN})\s*(USING\s+\w+\s*)?\((.*)\)(.*)$", code, FLAGS)
    if m:
        key = parse_qn(m.group(5), default_schema)
        cols, simple = [], True
        for part in split_top(m.group(7)):
            pm = re.match(rf"^({I_})(\s+(ASC|DESC|NULLS\s+(FIRST|LAST)|[a-z_][a-z0-9_]*_ops))*\s*$",
                          part.strip(), re.I)
            if pm:
                cols.append(norm_ident(pm.group(1)))
            else:
                simple = False
        return [Item("index", rs.src, rs.comments, {
            "key": key, "name": norm_ident(m.group(4)) if m.group(4) else None,
            "unique": bool(m.group(1)), "concurrently": bool(m.group(2)),
            "using": ws(m.group(6)), "body": m.group(7).strip(), "tail": ws(m.group(8)),
            "cols": cols, "simple": simple, "raw": code})]

    return [Item("other", rs.src, rs.comments, {"raw": code})]


# -------------------------------------------------------------------------
# Validador
# -------------------------------------------------------------------------
class Validator:
    def __init__(self, catalog, opts):
        self.cat = catalog
        self.o = opts
        self.schema = opts.schema

    def _tbl(self, key):
        return qname(*key)

    def _known_missing(self, item, key, what="tabla"):
        """Tabla no encontrada: error si hay BD, warning (guard runtime) si no."""
        if self.cat.online:
            item.add("error", f"no existe la {what} {key[0]}.{key[1]}")
        else:
            item.add("warn", f"{what} {key[0]}.{key[1]} no verificable sin BD (guard en runtime)")
            item.guard = True

    # ---- orquestacion ---------------------------------------------------
    def run(self, items):
        groups = {"table": [], "column": [], "index": [], "constraint": [], "fk": [], "other": []}
        for it in items:
            groups[it.kind].append(it)
        out = []
        for it in groups["table"]:
            out.append(it)
            out.extend(self.v_table(it))
        derived_cols = [x for x in out if x.kind == "column"]
        out = [x for x in out if x.kind != "column"]
        for it in derived_cols + groups["column"]:
            self.v_column(it)
            out.append(it)
        for it in groups["index"]:
            self.v_index(it)
            out.append(it)
        for it in groups["constraint"]:
            self.v_constraint(it)
            out.append(it)
        for it in groups["fk"]:
            self.v_fk(it)
            out.append(it)
        for it in groups["other"]:
            self.v_other(it)
            out.append(it)
        return out

    # ---- CREATE TABLE ---------------------------------------------------
    def v_table(self, it):
        d, key = it.data, it.data["key"]
        derived = []
        cols_sql = [c["text"] for c in d["cols"]] + [c["text"] for c in d["cons"]]
        it.sql = (f"CREATE TABLE IF NOT EXISTS {self._tbl(key)} (\n\t"
                  + ",\n\t".join(cols_sql) + "\n)" + (f" {d['tail']}" if d["tail"] else "") + ";")
        if self.cat.has_table(key):
            origin = self.cat.origin(key)
            it.add("skip", "la tabla ya existe" + (" en la BD" if origin == "db" else " (creada antes en el script)"))
            if origin != "db":
                d["no_deploy"] = "duplicada en el script"
            existing = self.cat.cols(key)
            for c in d["cols"]:
                if c["name"] not in existing:
                    ci = Item("column", it.src, [], {"key": key, "col": c}, derived=True)
                    ci.add("info", "columna faltante en tabla existente (derivada del CREATE TABLE)")
                    derived.append(ci)
                elif type_family(existing[c["name"]]["type"]) != type_family(c["type"]):
                    it.add("warn", f"columna {c['name']}: BD={existing[c['name']]['type']} "
                                   f"script={c['type']} (no se altera)")
            return derived

        # colisiones en el espacio de nombres pg_class
        for c in d["cols"]:
            if is_serial(c["type"]):
                seq = short_name(f"{key[1]}_{c['name']}_seq")
                if self.cat.has_relname((key[0], seq)):
                    it.add("error", f"ya existe la relacion {seq} (secuencia del serial)")
        pk_name = None
        for c in d["cols"]:
            if c["pk"]:
                pk_name = short_name(f"{key[1]}_pkey")
        for tc in d["cons"]:
            if tc["kind"] in ("p", "u") and tc.get("name"):
                if self.cat.has_relname((key[0], tc["name"])):
                    it.add("error", f"ya existe una relacion llamada {tc['name']} (indice de PK/UNIQUE)")
                if len(tc["name"]) > MAX_IDENT:
                    it.add("warn", f"{tc['name']} excede 63 chars, PostgreSQL lo trunca")
        if pk_name and self.cat.has_relname((key[0], pk_name)):
            it.add("error", f"ya existe una relacion llamada {pk_name}")
        if len(key[1]) > MAX_IDENT:
            it.add("error", f"nombre de tabla de {len(key[1])} chars (> 63)")
        if it.status == "error":
            return derived

        self.cat.add_table(key, {c["name"]: {"type": c["type"], "notnull": c["notnull"] or c["pk"]}
                                 for c in d["cols"]})
        for c in d["cols"]:
            if is_serial(c["type"]):
                self.cat.add_relname((key[0], short_name(f"{key[1]}_{c['name']}_seq")))
            if c["pk"]:
                self.cat.add_relname((key[0], pk_name))
                self.cat.add_constraint(key, pk_name, "p", [c["name"]])
            elif c["unique"]:
                self.cat.add_constraint(key, short_name(f"{key[1]}_{c['name']}_key"), "u", [c["name"]])
        for tc in d["cons"]:
            if tc["kind"] in ("p", "u"):
                nm = tc["name"] or short_name(f"{key[1]}_{'pkey' if tc['kind'] == 'p' else '_'.join(tc['cols'] or []) + '_key'}")
                self.cat.add_relname((key[0], nm))
                self.cat.add_constraint(key, nm, tc["kind"], tc["cols"])
            elif tc.get("name"):
                self.cat.add_constraint(key, tc["name"], tc["kind"])
        return derived

    # ---- ADD COLUMN -----------------------------------------------------
    def v_column(self, it):
        key, c = it.data["key"], it.data["col"]
        it.sql = f"ALTER TABLE {self._tbl(key)} ADD COLUMN IF NOT EXISTS {c['text']};"
        if not self.cat.has_table(key):
            self._known_missing(it, key)
            return
        existing = self.cat.cols(key)
        if c["name"] in existing:
            it.add("skip", "la columna ya existe")
            if type_family(existing[c["name"]]["type"]) != type_family(c["type"]):
                it.add("warn", f"tipo distinto: BD={existing[c['name']]['type']} script={c['type']}")
            return
        if c["notnull"] and not c["default"]:
            rows = self.cat.has_rows(key)
            if rows:
                it.add("error", "NOT NULL sin DEFAULT en tabla con datos: fallaria. "
                                "Agrega DEFAULT o crea la columna NULL, puebla y luego SET NOT NULL")
            elif rows is None and self.cat.origin(key) == "db":
                it.add("warn", "NOT NULL sin DEFAULT: falla si la tabla tiene filas")
        if self.cat.origin(key) == "db" and not self.cat.online:
            it.guard = True
        if it.status != "error":
            self.cat.add_column(key, c["name"], c["type"], c["notnull"])

    # ---- CREATE INDEX -----------------------------------------------------
    def v_index(self, it):
        d, key = it.data, it.data["key"]
        name = d["name"]
        if d["concurrently"]:
            it.add("warn", "CONCURRENTLY no corre dentro de una transaccion: se quito")
        it.sql = (f"CREATE {'UNIQUE ' if d['unique'] else ''}INDEX IF NOT EXISTS "
                  f"{qi(name) + ' ' if name else ''}ON {self._tbl(key)} "
                  f"{d['using'] + ' ' if d['using'] else ''}({d['body']})"
                  f"{' ' + d['tail'] if d['tail'] else ''};")
        if not name:
            it.add("warn", "indice sin nombre: no se puede detectar si ya existe")
            it.sql = it.sql.replace(" IF NOT EXISTS", "")
        elif self.cat.has_relname((key[0], name)):
            it.add("skip", f"ya existe una relacion llamada {name}")
            return
        if not self.cat.has_table(key):
            self._known_missing(it, key)
            return
        missing = [c for c in d["cols"] if c not in self.cat.cols(key)]
        if missing:
            it.add("error", f"columnas inexistentes en {key[1]}: {', '.join(missing)}")
            return
        if not d["simple"]:
            it.add("info", "indice con expresiones: columnas no verificadas")
        if name:
            self.cat.add_relname((key[0], name))
        if d["unique"] and d["simple"]:
            self.cat.uniques.setdefault(key, []).append(tuple(d["cols"]))

    # ---- PK / UNIQUE / CHECK ----------------------------------------------
    def v_constraint(self, it):
        d, key = it.data, it.data["key"]
        it.sql = f"ALTER TABLE {self._tbl(key)} ADD CONSTRAINT {qi(d['name'])} {d['body']};"
        it.guard = True
        if not self.cat.has_table(key):
            self._known_missing(it, key)
            return
        if self.cat.has_constraint(key, d["name"]):
            it.add("skip", "el constraint ya existe")
            return
        if d["ckind"] in ("p", "u") and self.cat.has_relname((key[0], d["name"])):
            it.add("error", f"ya existe una relacion llamada {d['name']}")
            return
        if d["ckind"] == "p" and key in self.cat.pkeys:
            it.add("skip", f"la tabla ya tiene PK {self.cat.pkeys[key]}")
            d["no_deploy"] = "la tabla ya tiene PK"
            return
        missing = [c for c in (d["cols"] or []) if c not in self.cat.cols(key)]
        if missing:
            it.add("error", f"columnas inexistentes: {', '.join(missing)}")
            return
        self.cat.add_constraint(key, d["name"], d["ckind"], d["cols"])
        if d["ckind"] in ("p", "u"):
            self.cat.add_relname((key[0], d["name"]))

    # ---- FOREIGN KEY ------------------------------------------------------
    def v_fk(self, it):
        d = it.data
        key, rkey, cols = d["key"], d["rkey"], d["cols"]
        it.guard = True

        def build():
            rc = f" ({', '.join(qi(c) for c in d['rcols'])})" if d["rcols"] else ""
            nv = " NOT VALID" if d["not_valid"] else ""
            act = f" {d['actions']}" if d["actions"] else ""
            it.sql = (f"ALTER TABLE {self._tbl(key)} ADD CONSTRAINT {qi(d['name'])} "
                      f"FOREIGN KEY ({', '.join(qi(c) for c in cols)}) "
                      f"REFERENCES {self._tbl(rkey)}{rc}{act}{nv};")

        build()
        if d["inline"]:
            it.add("info", "FK inline separada del CREATE TABLE")
        if len(d["name"]) > MAX_IDENT:
            it.add("warn", "nombre de constraint > 63 chars (PostgreSQL lo trunca)")

        if not self.cat.has_table(key):
            self._known_missing(it, key, "tabla origen")
            return
        miss = [c for c in cols if c not in self.cat.cols(key)]
        if miss:
            it.add("error", f"columnas origen inexistentes en {key[1]}: {', '.join(miss)}")
            return
        if self.cat.has_constraint(key, d["name"]):
            it.add("skip", "ya existe un constraint con ese nombre")
            return

        if not self.cat.has_table(rkey):
            self._known_missing(it, rkey, "tabla referenciada")
            return

        rcols = d["rcols"] or list(self.cat.pkeys.get(rkey, ()))
        if not rcols:
            if self.cat.online or self.cat.origin(rkey) == "script":
                it.add("error", f"{rkey[1]} no tiene PK y la FK no indica columnas")
            return
        if not d["rcols"]:
            d["rcols"] = rcols
            build()
        rmiss = [c for c in rcols if c not in self.cat.cols(rkey)]
        if rmiss:
            it.add("error", f"columnas referenciadas inexistentes en {rkey[1]}: {', '.join(rmiss)}")
            return
        if len(rcols) != len(cols):
            it.add("error", "cantidad de columnas origen/destino distinta")
            return
        if not self.cat.is_unique_set(rkey, rcols):
            it.add("error", f"{rkey[1]}({', '.join(rcols)}) no es PK ni UNIQUE")
            return
        if (key[0], key[1], tuple(cols), rkey[0], rkey[1], tuple(rcols)) in self.cat.fk_sigs:
            it.add("skip", "ya existe una FK equivalente con otro nombre")
            d["no_deploy"] = "FK equivalente ya existe con otro nombre"
            return

        for c, rc in zip(cols, rcols):
            t1, t2 = self.cat.cols(key)[c]["type"], self.cat.cols(rkey)[rc]["type"]
            if type_family(t1) != type_family(t2):
                it.add("error", f"tipos incompatibles {c} {t1} -> {rc} {t2}")
                return
            if norm_type(t1) != norm_type(t2):
                it.add("info", f"tipos distintos pero compatibles: {c} {t1} -> {rc} {t2}")

        # datos huerfanos (solo si la tabla origen ya tiene datos)
        new_cols = any(self.cat.col_from_script(key, c) for c in cols)
        if (self.o.data_checks and not new_cols and self.cat.origin(key) == "db"
                and self.cat.has_rows(key)):
            if self.cat.origin(rkey) == "db":
                n = self.cat.count_orphans(key, cols, rkey, rcols)
            else:  # tabla destino nueva (vacia): todo valor no nulo es huerfano
                n = self._count_notnull(key, cols)
            if n is None:
                it.add("warn", "no se pudo verificar filas huerfanas (timeout/permisos)")
            elif n > 0:
                if self.o.fk_not_valid:
                    d["not_valid"] = True
                    build()
                    it.add("warn", f"{n} fila(s) huerfana(s): se agrega NOT VALID")
                else:
                    it.add("error", f"{n} fila(s) huerfana(s) en {key[1]}: la FK fallaria "
                                    f"(usa --fk-not-valid o limpia los datos)")
                    return

        self.cat.add_constraint(key, d["name"], "f", cols, (rkey, rcols))

    def _count_notnull(self, key, cols):
        if not self.cat.online:
            return None
        cond = " OR ".join(f"{qi(c)} IS NOT NULL" for c in cols)
        cur = self.cat.conn.cursor()
        try:
            cur.execute(f"SELECT count(*) FROM {self._tbl(key)} WHERE {cond}")
            return cur.fetchone()[0]
        except Exception:  # noqa: BLE001
            return None
        finally:
            cur.close()
            self.cat.conn.rollback()

    # ---- otros ----------------------------------------------------------
    def v_other(self, it):
        raw = it.data["raw"].strip()
        it.sql = raw.rstrip(";") + ";"
        if re.match(r"^(DROP|TRUNCATE|DELETE)\b", raw, re.I) or re.search(r"\bDROP\s+(COLUMN|CONSTRAINT)\b", raw, re.I):
            if not self.o.allow_drop:
                it.add("error", "sentencia destructiva: comentada (usa --allow-drop)")
            else:
                it.add("warn", "sentencia destructiva")
            return
        it.add("warn", "sentencia no analizada: se emite tal cual")


# -------------------------------------------------------------------------
# Emision
# -------------------------------------------------------------------------
def _guarded(it, schema):
    """Envuelve en DO block con chequeo en runtime."""
    d = it.data
    key = d["key"]
    checks = [f"to_regclass({sql_lit(qname(*key))}) IS NULL",
              f"'no existe la tabla {key[0]}.{key[1]}'"]
    conds = [checks]
    if it.kind == "fk":
        conds.append([f"to_regclass({sql_lit(qname(*d['rkey']))}) IS NULL",
                      f"'no existe la tabla referenciada {d['rkey'][0]}.{d['rkey'][1]}'"])
    exists = None
    if it.kind in ("fk", "constraint"):
        exists = (f"EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = "
                  f"to_regclass({sql_lit(qname(*key))}) AND conname = {sql_lit(d['name'])})")
    lab = sql_lit(it.label)
    body = []
    kw = "IF"
    for cond, msg in conds:
        body.append(f"    {kw} {cond} THEN\n        RAISE WARNING '[ddl] % omitido: %', {lab}, {msg};")
        kw = "ELSIF"
    if exists:
        body.append(f"    ELSIF {exists} THEN\n        RAISE NOTICE '[ddl] % ya existe', {lab};")
    body.append(f"    ELSE\n        {it.sql}\n    END IF;")
    return "DO $ddl$\nBEGIN\n" + "\n".join(body) + "\nEND\n$ddl$;"


def _emit(it, opts):
    """SQL ejecutable de un item (con guard si corresponde)."""
    if it.guard and opts.guard and it.kind != "other":
        return _guarded(it, opts.schema)
    return it.sql


def _commented(sql):
    return "\n".join("-- " + ln for ln in sql.splitlines())


def render(items, opts, meta):
    out, executable = [], []
    stats = {"ok": 0, "warn": 0, "skip": 0, "error": 0}
    for it in items:
        stats[it.status] += 1

    hdr = [
        "-- =====================================================================",
        "-- DDL validado por odoo-ddl-extractor / validate_ddl.py",
        f"-- Fecha  : {meta['date']}",
        f"-- BD     : {meta['db']}",
        f"-- Fuente : {', '.join(meta['sources'])}",
        f"-- Result.: {stats['ok']} ok, {stats['warn']} warn, {stats['skip']} omitidos, {stats['error']} errores",
        "-- =====================================================================",
    ] + [f"-- NOTA: {n}" for n in meta.get("notes", [])] + [
        "",
        "BEGIN;",
        "",
    ]
    if opts.lock_timeout:
        hdr += [f"SET LOCAL lock_timeout = {sql_lit(opts.lock_timeout)};", ""]
        executable.append(f"SET LOCAL lock_timeout = {sql_lit(opts.lock_timeout)}")
    out += hdr

    section = None
    titles = {"table": "TABLAS", "column": "COLUMNAS", "index": "INDICES",
              "constraint": "CONSTRAINTS (PK/UNIQUE/CHECK)", "fk": "FOREIGN KEYS", "other": "OTRAS SENTENCIAS"}
    for it in items:
        if it.kind != section:
            section = it.kind
            out += ["-- ---------------------------------------------------------------------",
                    f"-- {titles[section]}",
                    "-- ---------------------------------------------------------------------", ""]
        st = it.status
        head = f"-- [{st.upper()}] {it.label}  ({it.src}{', derivada' if it.derived else ''})"
        out.append(head)
        for c in it.comments:
            if c.strip("- ").strip():
                out.append(c if c.startswith("--") else _commented(c))
        for sev, msg in it.msgs:
            out.append(f"--   {sev}: {msg}")
        if st in ("error", "skip"):
            out.append(_commented(it.sql))
        else:
            sql = _emit(it, opts)
            out.append(sql)
            executable.append(sql.rstrip(";"))
        out.append("")

    out += ["ROLLBACK;" if not opts.commit else "COMMIT;",
            "-- COMMIT;" if not opts.commit else "-- ROLLBACK;", ""]
    return "\n".join(out), executable, stats


def render_report(items, stats, meta, dry):
    L = ["# Validacion DDL", "",
         f"- **Fecha:** {meta['date']}", f"- **BD:** `{meta['db']}`",
         f"- **Fuentes:** {', '.join(f'`{s}`' for s in meta['sources'])}", "",
         "| ok | warn | omitidos | errores |", "|---|---|---|---|",
         f"| {stats['ok']} | {stats['warn']} | {stats['skip']} | {stats['error']} |", ""]

    if meta.get("notes"):
        L += ["## Notas del extractor", ""] + [f"- {n}" for n in meta["notes"]] + [""]

    missing = {}
    for it in items:
        for sev, msg in it.msgs:
            m = re.search(r"no existe la tabla (?:referenciada |origen )?(\S+)", msg)
            if m and sev == "error":
                missing.setdefault(m.group(1), []).append(it.label)
    if missing:
        L += ["## Relaciones inexistentes en la BD", "",
              "| tabla | sentencias afectadas |", "|---|---|"]
        for t, labs in sorted(missing.items(), key=lambda x: -len(x[1])):
            L.append(f"| `{t}` | {len(labs)}: {'; '.join(labs[:6])}{' ...' if len(labs) > 6 else ''} |")
        L.append("")

    for sev, title in (("error", "Errores (comentados en el script)"),
                       ("warn", "Advertencias"), ("skip", "Omitidos (ya existen)")):
        rows = [it for it in items if it.status == sev]
        if not rows:
            continue
        L += [f"## {title}", "", "| sentencia | origen | detalle |", "|---|---|---|"]
        for it in rows:
            det = "<br>".join(f"{s}: {m}" for s, m in it.msgs if s != "info")
            L.append(f"| {it.label} | `{it.src}` | {det} |")
        L.append("")

    dep = meta.get("deploy")
    if dep:
        L += ["## Script de despliegue (DBA)", "",
              f"- **Archivo:** `{Path(dep['path']).name}`",
              f"- **Sentencias incluidas:** {dep['included']}",
              f"- **Base destino verificada:** {dep['target'] or 'no especificada'}",
              f"- **Si falta algun objeto al final:** {'se revierte todo (ROLLBACK)' if dep['strict'] else 'COMMIT parcial + WARNING'}", ""]
        if dep["excluded"]:
            L += ["Excluidas del despliegue:", "", "| sentencia | motivo |", "|---|---|"]
            L += [f"| {lab} | {why} |" for lab, why in dep["excluded"]]
            L.append("")

    if dry is not None:
        L += ["## Dry-run (ejecucion real + ROLLBACK)", ""]
        if not dry["errors"]:
            L.append(f"Sin errores en {dry['n']} sentencias.")
        else:
            L += ["| # | sentencia | error |", "|---|---|---|"]
            for i, sql, err in dry["errors"]:
                L.append(f"| {i} | `{ws(sql)[:90]}` | {ws(err)} |")
        if dry["notices"]:
            L += ["", "Avisos de los guards:", ""] + [f"- {ws(n)}" for n in dry["notices"]]
        L.append("")
    return "\n".join(L)


# -------------------------------------------------------------------------
# Script de despliegue (para el DBA / produccion)
# -------------------------------------------------------------------------
def deploy_items(items):
    """(incluidos, excluidos). Incluye OK/WARN y tambien lo OMITIDO por existir
    en la BD de validacion: todo es idempotente y produccion puede diferir de test."""
    inc, exc = [], []
    for it in items:
        if it.status == "error":
            exc.append((it, "; ".join(m for s, m in it.msgs if s == "error")))
        elif it.data.get("no_deploy"):
            exc.append((it, it.data["no_deploy"]))
        else:
            inc.append(it)
    return inc, exc


def _expect_check(it):
    """Condicion SQL que es TRUE si el objeto del item NO existe tras ejecutar."""
    d = it.data
    if it.kind == "table":
        return f"to_regclass({sql_lit(qname(*d['key']))}) IS NULL"
    if it.kind == "column":
        return (f"NOT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = to_regclass("
                f"{sql_lit(qname(*d['key']))}) AND attname = {sql_lit(d['col']['name'])} "
                f"AND NOT attisdropped)")
    if it.kind == "index" and d.get("name"):
        return f"to_regclass({sql_lit(qname(d['key'][0], d['name']))}) IS NULL"
    if it.kind in ("fk", "constraint"):
        return (f"NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = to_regclass("
                f"{sql_lit(qname(*d['key']))}) AND conname = {sql_lit(d['name'])})")
    return None


def render_deploy(items, opts, meta, dry=None):
    """Devuelve (script, ejecutables_para_dry_run)."""
    inc, exc = deploy_items(items)
    strict = getattr(opts, "deploy_strict", True)
    target = getattr(opts, "target_db", None)
    counts = {}
    for it in inc:
        counts[it.kind] = counts.get(it.kind, 0) + 1
    names = {"table": "tablas", "column": "columnas", "index": "indices",
             "constraint": "constraints", "fk": "FKs", "other": "otras"}
    detail = ", ".join(f"{names[k]} {v}" for k, v in counts.items()) or "-"
    fname = meta.get("deploy_name", "script.deploy.sql")

    H = [
        "-- =====================================================================",
        "-- SCRIPT DE DESPLIEGUE  -  listo para ejecutar",
        "-- =====================================================================",
        f"-- Generado      : {meta['date']}  (DDL Extractor / validate_ddl.py)",
        f"-- Validado en   : {meta['db']}",
        f"-- Base destino  : {target + '  (se verifica al inicio)' if target else 'no especificada'}",
        f"-- Fuente        : {', '.join(meta['sources'])}",
        f"-- Sentencias    : {len(inc)}  ({detail})",
        f"-- Excluidas     : {len(exc)}" + ("  (ver lista abajo)" if exc else ""),
    ]
    if dry is not None:
        H.append(f"-- Dry-run       : {dry['n']} sentencias, {len(dry['errors'])} con error "
                 f"(en {meta['db']}, con ROLLBACK)")
    H += [
        f"-- Modo          : {'REVERSION TOTAL: si al final falta algun objeto esperado, ROLLBACK completo (no se aplica nada)' if strict else 'PARCIAL: se hace COMMIT de lo aplicado; lo faltante solo se reporta (WARNING)'}",
        "--",
        "-- COMO EJECUTAR",
        "--   1. Respaldo:  pg_dump -Fc -h <host> -p <puerto> -U <usuario> <base> > respaldo.dump",
        f"--   2. psql:      psql -h <host> -p <puerto> -U <usuario> -d <base> -v ON_ERROR_STOP=1 -f {fname}",
        "--      DBeaver:   abrir el archivo y 'Ejecutar script' (Alt+X)",
        "--   3. Revisar la salida: NOTICE '[deploy] verificacion OK' = aplicado completo.",
        "--      WARNING '[ddl] ... omitido' = objeto saltado porque falta su dependencia en esta base.",
        "--",
        "-- Todo corre en UNA transaccion (BEGIN ... COMMIT): ante cualquier error no se aplica nada.",
        "-- El script es idempotente: re-ejecutarlo no duplica objetos.",
    ]
    if exc:
        H += ["--", "-- EXCLUIDAS (NO se ejecutan; revisar con el equipo de desarrollo):"]
        for it, why in exc:
            H.append(f"--   - {it.label}  [{it.src}]: {why}")
    H += ["-- =====================================================================", ""]

    body, executable = ["BEGIN;", ""], []
    if opts.lock_timeout:
        body += [f"SET LOCAL lock_timeout = {sql_lit(opts.lock_timeout)};", ""]
        executable.append(f"SET LOCAL lock_timeout = {sql_lit(opts.lock_timeout)}")
    if target:
        body += ["-- Verificacion de base destino",
                 "DO $pre$\nBEGIN\n"
                 f"    IF current_database() <> {sql_lit(target)} THEN\n"
                 f"        RAISE EXCEPTION '[deploy] base incorrecta: % (se esperaba %)', "
                 f"current_database(), {sql_lit(target)};\n"
                 "    END IF;\nEND\n$pre$;", ""]

    section = None
    titles = {"table": "TABLAS", "column": "COLUMNAS", "index": "INDICES",
              "constraint": "CONSTRAINTS", "fk": "FOREIGN KEYS", "other": "OTRAS SENTENCIAS"}
    for n, it in enumerate(inc, 1):
        if it.kind != section:
            section = it.kind
            body += [f"-- ---- {titles[section]} " + "-" * (60 - len(titles[section])), ""]
        body.append(f"-- [{n}/{len(inc)}] {it.label}  ({it.src})")
        sql = _emit(it, opts)
        body += [sql, ""]
        executable.append(sql.rstrip(";"))

    checks = [(it.label, c) for it in inc for c in [_expect_check(it)] if c]
    if checks:
        lines = ["-- Verificacion final: cada objeto esperado debe existir",
                 "DO $verify$", "DECLARE", "    faltan text[] := '{}';", "BEGIN"]
        for label, cond in checks:
            lines.append(f"    IF {cond} THEN faltan := array_append(faltan, {sql_lit(label)}::text); END IF;")
        lvl = "EXCEPTION" if strict else "WARNING"
        lines += [
            "    IF cardinality(faltan) > 0 THEN",
            f"        RAISE {lvl} '[deploy] % de {len(checks)} objeto(s) NO aplicados: %',",
            "            cardinality(faltan), array_to_string(faltan, ' | ');",
            "    ELSE",
            f"        RAISE NOTICE '[deploy] verificacion OK: {len(checks)} objeto(s) presentes';",
            "    END IF;", "END", "$verify$;", ""]
        verify = "\n".join(lines)
        body.append(verify)
        executable.append(verify.rstrip().rstrip(";"))

    body += ["COMMIT;", ""]
    return "\n".join(H + body), executable


def deploy_path(out):
    out = Path(out)
    stem = out.name[:-4] if out.name.endswith(".sql") else out.name
    stem = stem[:-10] if stem.endswith(".validated") else stem
    return out.with_name(stem + ".deploy.sql")


# -------------------------------------------------------------------------
# Dry-run
# -------------------------------------------------------------------------
def dry_run(args, executable):
    import psycopg2
    conn = psycopg2.connect(host=args.host, port=args.port, dbname=args.db, user=args.user,
                            password=args.password, connect_timeout=10,
                            application_name="odoo-ddl-extractor-dryrun")
    conn.autocommit = False
    cur = conn.cursor()
    errors = []
    try:
        for i, sql in enumerate(executable, 1):
            cur.execute("SAVEPOINT ddl_sp")
            try:
                cur.execute(sql)
                cur.execute("RELEASE SAVEPOINT ddl_sp")
            except Exception as e:  # noqa: BLE001
                cur.execute("ROLLBACK TO SAVEPOINT ddl_sp")
                errors.append((i, sql, str(e).strip()))
    finally:
        notices = [n.strip() for n in conn.notices if "[ddl]" in n]
        conn.rollback()
        conn.close()
    return {"n": len(executable), "errors": errors, "notices": notices}


# -------------------------------------------------------------------------
# API para extract_ddl.py
# -------------------------------------------------------------------------
def validate_text(sql_text, source_name, catalog, opts, db_label, notes=None):
    raws = split_sql(sql_text, source_name)
    items = [x for rs in raws for x in parse_statement(rs, opts.schema)]
    items = Validator(catalog, opts).run(items)
    meta = {"date": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "db": db_label,
            "sources": [source_name], "notes": notes or []}
    script, executable, stats = render(items, opts, meta)
    return items, script, executable, stats, meta


def validate_files(files, catalog, opts, db_label):
    """Valida varios archivos como un solo script (referencias cruzadas)."""
    raws = []
    for f in files:
        f = Path(f)
        raws += split_sql(f.read_text(encoding="utf-8-sig"), f.name)
    items = [x for rs in raws for x in parse_statement(rs, opts.schema)]
    items = Validator(catalog, opts).run(items)
    meta = {"date": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "db": db_label,
            "sources": [Path(f).name for f in files], "notes": []}
    script, executable, stats = render(items, opts, meta)
    return items, script, executable, stats, meta


def write_outputs(items, script, executable, stats, meta, opts, out, log=print):
    """Escribe .validated.sql, .deploy.sql (opcional) y .report.md; corre dry-run.
    El dry-run ejecuta el script de despliegue si se genera (es el que va a produccion)."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(script, encoding="utf-8")
    want_deploy = getattr(opts, "deploy", True)
    dep_path = deploy_path(out) if want_deploy else None
    dep_exec = None
    if want_deploy:
        meta["deploy_name"] = dep_path.name
        _, dep_exec = render_deploy(items, opts, meta)
    dry = None
    if getattr(opts, "dry_run", False):
        target = "despliegue" if want_deploy else "validado"
        log(f"[dry-run] ejecutando script de {target} con SAVEPOINT por sentencia y ROLLBACK final...")
        dry = dry_run(opts, dep_exec if want_deploy else executable)
        log(f"[dry-run] {dry['n']} sentencias, {len(dry['errors'])} con error")
        for i, _, err in dry["errors"]:
            log(f"   #{i}: {ws(err)}")
    dep_script = None
    if want_deploy:
        dep_script, _ = render_deploy(items, opts, meta, dry)
        dep_path.write_text(dep_script, encoding="utf-8")
        inc, exc = deploy_items(items)
        meta["deploy"] = {"path": str(dep_path), "included": len(inc),
                          "excluded": [(it.label, why) for it, why in exc],
                          "target": getattr(opts, "target_db", None),
                          "strict": getattr(opts, "deploy_strict", True)}
    rep = report_path(out)
    rep.write_text(render_report(items, stats, meta, dry), encoding="utf-8")
    return {"out": out, "report": rep, "deploy": dep_path, "deploy_script": dep_script, "dry": dry}


def default_output_path(files):
    files = [Path(f) for f in files]
    if len(files) == 1:
        return files[0].with_name(files[0].stem + ".validated.sql")
    return files[0].parent / f"validated_{dt.datetime.now():%Y%m%d_%H%M%S}.sql"


def report_path(out):
    out = Path(out)
    return out.with_name(out.name.replace(".sql", "") + ".report.md")


def default_opts(**kw):
    ns = argparse.Namespace(schema="public", guard=True, commit=False, lock_timeout="15s",
                            data_checks=True, fk_not_valid=False, allow_drop=False,
                            deploy=True, target_db=None, deploy_strict=True, dry_run=False)
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


# -------------------------------------------------------------------------
# CLI
# -------------------------------------------------------------------------
def collect_inputs(paths, fail=sys.exit):
    files = []
    for p in paths:
        p = Path(p)
        if p.is_dir():
            files += sorted(x for x in p.rglob("*.sql") if ".validated" not in x.name)
        elif p.exists():
            files.append(p)
        else:
            fail(f"No existe: {p}")
    if not files:
        fail("No se encontraron archivos .sql")
    return files


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="+", help="Archivos .sql o carpetas (se procesan todos juntos, en orden)")
    add_db_arguments(ap)
    ap.add_argument("--no-db", action="store_true", help="Sin BD: solo parsea y agrega guards en runtime")
    ap.add_argument("-o", "--out", help="Archivo .sql de salida (default: <entrada>.validated.sql)")
    ap.add_argument("--commit", action="store_true", help="Terminar con COMMIT en vez de ROLLBACK")
    ap.add_argument("--no-guard", dest="guard", action="store_false",
                    help="No envolver FKs/constraints en DO blocks con chequeo en runtime")
    ap.add_argument("--fk-not-valid", action="store_true",
                    help="Si hay filas huerfanas, crear la FK con NOT VALID en vez de comentarla")
    ap.add_argument("--no-data-checks", dest="data_checks", action="store_false",
                    help="No consultar datos (filas huerfanas, tablas con filas)")
    ap.add_argument("--allow-drop", action="store_true", help="Permitir DROP/TRUNCATE/DELETE")
    ap.add_argument("--lock-timeout", default="15s", help="SET LOCAL lock_timeout (vacio = no setear)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Ejecutar el resultado en la BD con SAVEPOINTs y ROLLBACK final")
    ap.add_argument("--strict", action="store_true", help="Exit 1 tambien con warnings")
    dg = ap.add_argument_group("script de despliegue (.deploy.sql)")
    dg.add_argument("--no-deploy", dest="deploy", action="store_false",
                    help="No generar el .deploy.sql para el DBA")
    dg.add_argument("--target-db", default=None,
                    help="Nombre de la BD de produccion: el script aborta si se corre en otra")
    dg.add_argument("--deploy-lenient", dest="deploy_strict", action="store_false",
                    help="Si al final falta algun objeto, hacer COMMIT parcial y solo avisar (default: revertir todo)")
    args = ap.parse_args()

    files = collect_inputs(args.inputs)
    if args.no_db:
        if args.dry_run:
            sys.exit("--dry-run requiere BD")
        catalog, db_label = Catalog(None), "(sin BD)"
    else:
        args = resolve_db_config(args)
        conn = connect(args)
        catalog = Catalog(conn, (args.schema,))
        db_label = f"{args.user}@{args.host}:{args.port}/{args.db}"
        print(f"[bd] catalogo: {len(catalog.tables)} tablas/vistas en '{args.schema}'")

    items, script, executable, stats, meta = validate_files(files, catalog, args, db_label)
    out = Path(args.out) if args.out else default_output_path(files)
    res = write_outputs(items, script, executable, stats, meta, args, out)
    dry = res["dry"]

    print(f"\n{stats['ok']} ok | {stats['warn']} warn | {stats['skip']} omitidos | {stats['error']} errores")
    for it in items:
        if it.status in ("error", "warn"):
            det = "; ".join(m for s, m in it.msgs if s == it.status)
            print(f"  [{it.status.upper():5}] {it.label}: {det}")
    print(f"\nRevision  : {out}")
    if res["deploy"]:
        print(f"Despliegue: {res['deploy']}  (para el DBA, termina en COMMIT)")
    print(f"Reporte   : {res['report']}")

    bad = stats["error"] or (dry and dry["errors"]) or (args.strict and stats["warn"])
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
