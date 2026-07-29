#!/usr/bin/env bash
# MomentShift build script — auto-cleans old build dirs before building.
# Usage: ./build.sh [version]   (version defaults to the one in metadata.py)
set -euo pipefail
cd "$(dirname "$0")"

PY=".venv/Scripts/python.exe"
SPEC="build.spec"

# --- 1. Clean old build dirs (no堆积) -----------------------------------
echo "[build] cleaning old build dirs..."
rm -rf build dist_build dist_build_v0* build_work_v0* 2>/dev/null || true
echo "[build] old dirs cleaned"

# --- 2. Build with fresh dirs -------------------------------------------
DIST="dist_build"
WORK="build_work"
echo "[build] running PyInstaller..."
$PY -m PyInstaller "$SPEC" --noconfirm --distpath "$DIST" --workpath "$WORK"

# --- 3. Copy to deliverable dir -----------------------------------------
VERSION="${1:-}"
if [ -z "$VERSION" ]; then
    VERSION=$($PY -c "from momentshift.metadata import VERSION; print(VERSION)" 2>/dev/null || echo "unknown")
    # If import fails (no PYTHONPATH), extract from source
    if [ "$VERSION" = "unknown" ]; then
        VERSION=$(grep -oP 'VERSION = "\K[^"]+' src/momentshift/metadata.py)
    fi
fi

DELIVER="../测试/MomentShift-v${VERSION}"
echo "[build] copying to $DELIVER..."
mkdir -p "$DELIVER"
cp -r "$DIST/MomentShift/." "$DELIVER/"
echo "[build] done! Deliverable: $DELIVER/MomentShift.exe"

# --- 4. Clean work dir (keep dist for debugging) ------------------------
rm -rf "$WORK"
echo "[build] work dir cleaned"
