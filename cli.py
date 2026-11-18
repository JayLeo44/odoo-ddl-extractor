#!/usr/bin/env python3
"""CLI unificada (es el ejecutable ddl-extractor-cli del paquete instalado).

    ddl-extractor-cli validate <archivos|carpetas> [opciones]
    ddl-extractor-cli extract  <modulos> [opciones]

Las opciones son las mismas que validate_ddl.py / extract_ddl.py (usar --help).
"""
import sys

USAGE = __doc__


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(USAGE)
        sys.exit(0 if len(sys.argv) >= 2 else 2)
    if sys.argv[1] in ("-V", "--version"):
        from ddl_gui import __version__
        print(__version__)
        sys.exit(0)
    cmd = sys.argv.pop(1)
    if cmd == "validate":
        import validate_ddl as mod
    elif cmd == "extract":
        import extract_ddl as mod
    else:
        print(f"Comando desconocido: {cmd}\n{USAGE}")
        sys.exit(2)
    sys.argv[0] = f"ddl-extractor-cli {cmd}"
    mod.main()


if __name__ == "__main__":
    main()
