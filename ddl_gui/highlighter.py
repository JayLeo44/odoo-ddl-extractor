"""Resaltado de sintaxis SQL para el visor del script generado."""
import re

from PySide6.QtGui import QColor, QFont, QSyntaxHighlighter, QTextCharFormat

KEYWORDS = (
    "add alter and begin by cascade check column commit constraint create default deferrable "
    "delete do drop else elsif end exists foreign from if in index initially into is key "
    "not null on only or primary raise references restrict rollback select set table then "
    "unique update using valid warning notice where with lock_timeout local"
).split()
TYPES = ("int2 int4 int8 integer bigint smallint serial serial4 serial8 bigserial varchar "
         "character varying text numeric decimal boolean bool date timestamp jsonb json bytea "
         "float8 double precision real").split()


def _fmt(color, bold=False, italic=False):
    f = QTextCharFormat()
    f.setForeground(QColor(color))
    if bold:
        f.setFontWeight(QFont.Bold)
    f.setFontItalic(italic)
    return f


class SqlHighlighter(QSyntaxHighlighter):
    def __init__(self, document, dark=False):
        super().__init__(document)
        c = {
            "kw": "#c586c0" if dark else "#7a1fa2",
            "type": "#4ec9b0" if dark else "#00796b",
            "str": "#ce9178" if dark else "#b3541e",
            "num": "#b5cea8" if dark else "#1565c0",
            "comment": "#6a9955" if dark else "#6b7b6b",
        }
        self.rules = [
            (re.compile(r"\b(" + "|".join(KEYWORDS) + r")\b", re.I), _fmt(c["kw"], bold=True)),
            (re.compile(r"\b(" + "|".join(TYPES) + r")\b", re.I), _fmt(c["type"])),
            (re.compile(r"\b\d+\b"), _fmt(c["num"])),
            (re.compile(r"'(?:[^']|'')*'"), _fmt(c["str"])),
            (re.compile(r"\$ddl\$"), _fmt(c["kw"], bold=True)),
        ]
        self.comment = _fmt(c["comment"], italic=True)
        self.status = {
            "[ERROR]": _fmt("#e53935", bold=True),
            "[WARN]": _fmt("#fb8c00", bold=True),
            "[SKIP]": _fmt("#9e9e9e", bold=True),
            "[OK]": _fmt("#43a047", bold=True),
        }

    def highlightBlock(self, text):
        for rx, fmt in self.rules:
            for m in rx.finditer(text):
                self.setFormat(m.start(), m.end() - m.start(), fmt)
        idx = text.find("--")
        if idx >= 0:
            self.setFormat(idx, len(text) - idx, self.comment)
            for tag, fmt in self.status.items():
                j = text.find(tag, idx)
                if j >= 0:
                    self.setFormat(j, len(tag), fmt)
