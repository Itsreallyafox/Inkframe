#!/usr/bin/env bash
# Build the native dither library with gcc: inkframe/render/libdither.so.
#
# Usage, from anywhere (the script works relative to the repo root):
#     bash scripts/build_dither.sh
#
# This is the same compile as install.sh build_native (which the Pi runs on
# every deploy that touches inkframe/render/*.c), plus -std=c11 -Wall -Wextra
# so warnings show up while developing.  Like install.sh it builds next to the
# target and renames, so a running process keeps its mapping of the old file.
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
src=$root/inkframe/render/dither.c
out=$root/inkframe/render/libdither.so

command -v gcc >/dev/null 2>&1 || {
  echo "build_dither.sh: gcc not found (on Windows use scripts/build_dither.ps1)" >&2
  exit 1
}

gcc -O2 -std=c11 -Wall -Wextra -shared -fPIC -o "$out.tmp" "$src" -lm
mv -f "$out.tmp" "$out"
echo "built $out"
