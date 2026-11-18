#!/usr/bin/env bash
# Genera el paquete .deb (Ubuntu 22.04+) y un .tar.gz portable.
#
#   bash packaging/linux/build_deb.sh            # usa python3.10 si existe, si no python3
#   PYTHON=python3.12 bash packaging/linux/build_deb.sh
#
# Compatibilidad: el binario queda atado a la glibc de la maquina donde se compila.
# Compilar en Ubuntu 22.04 => funciona en 22.04, 24.04 y posteriores.
#
# Requisitos de compilacion: python3-venv, dpkg-dev (dpkg-deb), binutils.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

PY="${PYTHON:-$(command -v python3.10 || command -v python3)}"
VENV=".venv-build"
echo ">> Python: $($PY --version) ($PY)"

if [ ! -x "$VENV/bin/python" ]; then
  "$PY" -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install --upgrade pip -q
"$VENV/bin/python" -m pip install -q -r requirements.txt -r requirements-gui.txt -r requirements-build.txt

echo ">> PyInstaller"
"$VENV/bin/python" -m PyInstaller --noconfirm --clean packaging/ddl_extractor.spec \
  --distpath dist --workpath build/pyinstaller

VER="$("$VENV/bin/python" -c 'import ddl_gui; print(ddl_gui.__version__)')"
ARCH="$(dpkg --print-architecture)"
NAME="ddl-extractor"
PKG="build/deb/${NAME}_${VER}_${ARCH}"

echo ">> Armando .deb ${VER} (${ARCH})"
rm -rf "$PKG"
mkdir -p "$PKG/DEBIAN" "$PKG/opt/$NAME" "$PKG/usr/bin" "$PKG/usr/share/applications" \
         "$PKG/usr/share/icons/hicolor/256x256/apps" "$PKG/usr/share/doc/$NAME"
cp -a "dist/$NAME/." "$PKG/opt/$NAME/"
ln -s "/opt/$NAME/ddl-extractor" "$PKG/usr/bin/ddl-extractor"
ln -s "/opt/$NAME/ddl-extractor-cli" "$PKG/usr/bin/ddl-extractor-cli"
install -m 644 assets/icon_256.png "$PKG/usr/share/icons/hicolor/256x256/apps/$NAME.png"
install -m 644 LICENSE "$PKG/usr/share/doc/$NAME/copyright"
install -m 644 docs/MANUAL_USUARIO.md "$PKG/usr/share/doc/$NAME/"

cat > "$PKG/usr/share/applications/$NAME.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=DDL Extractor
GenericName=Validador de DDL PostgreSQL
Comment=Extrae y valida DDL de modulos Odoo contra PostgreSQL
Exec=/opt/$NAME/ddl-extractor
Icon=$NAME
Terminal=false
Categories=Development;Database;
Keywords=odoo;postgresql;ddl;sql;
StartupWMClass=ddl-extractor
EOF

SIZE_KB="$(du -sk "$PKG" | cut -f1)"
# glibc minima = la de la maquina de compilacion (PyInstaller enlaza contra ella)
GLIBC="$(getconf GNU_LIBC_VERSION | awk '{print $2}')"
cat > "$PKG/DEBIAN/control" <<EOF
Package: $NAME
Version: $VER
Section: devel
Priority: optional
Architecture: $ARCH
Maintainer: JayLeo44 <noreply@github.com>
Installed-Size: $SIZE_KB
Depends: libc6 (>= ${GLIBC}), libegl1, libgl1, libfontconfig1, libxkbcommon0, libxkbcommon-x11-0, libxcb-cursor0, libxcb-icccm4, libxcb-image0, libxcb-keysyms1, libxcb-render-util0, libxcb-shape0, libxcb-xinerama0, libxcb-xkb1, libdbus-1-3
Homepage: https://github.com/JayLeo44/odoo-ddl-extractor
Description: Extractor y validador de DDL para Odoo / PostgreSQL
 Interfaz grafica (PySide6) y CLI para extraer DDL desde modulos Odoo y
 validar scripts DDL contra una base PostgreSQL: detecta relaciones
 inexistentes, objetos duplicados y datos huerfanos, y genera un script
 seguro dentro de BEGIN/ROLLBACK.
EOF

cat > "$PKG/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database -q /usr/share/applications || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q -t /usr/share/icons/hicolor || true
exit 0
EOF
cp "$PKG/DEBIAN/postinst" "$PKG/DEBIAN/postrm"
chmod 755 "$PKG/DEBIAN/postinst" "$PKG/DEBIAN/postrm"

mkdir -p dist
dpkg-deb --build --root-owner-group "$PKG" "dist/${NAME}_${VER}_${ARCH}.deb" >/dev/null
tar -C dist -czf "dist/${NAME}-${VER}-linux-${ARCH}.tar.gz" "$NAME"

echo ">> Listo:"
ls -lh "dist/${NAME}_${VER}_${ARCH}.deb" "dist/${NAME}-${VER}-linux-${ARCH}.tar.gz"
echo "   Instalar: sudo apt install ./dist/${NAME}_${VER}_${ARCH}.deb"
