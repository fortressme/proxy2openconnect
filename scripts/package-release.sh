#!/bin/sh
set -eu

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
VERSION="$(tr -d '\r\n' < "$ROOT_DIR/VERSION")"
OUTPUT_DIR="${1:-$ROOT_DIR/dist}"
ARCHIVE="$OUTPUT_DIR/proxy2openconnect-$VERSION.tar.gz"

case "$VERSION" in
  ''|*[!0-9A-Za-z.-]*)
    echo "Invalid VERSION: $VERSION" >&2
    exit 1
    ;;
esac

mkdir -p "$OUTPUT_DIR"
tar -C "$ROOT_DIR" -czf "$ARCHIVE" \
  .dockerignore \
  .env.example \
  .gitattributes \
  .gitignore \
  CHANGELOG.md \
  CONTRIBUTING.md \
  Dockerfile \
  LICENSE \
  README.md \
  RELEASING.md \
  SECURITY.md \
  THIRD_PARTY_NOTICES.md \
  VERSION \
  app \
  defaults \
  docker-compose.yml \
  entrypoint.sh \
  requirements.txt \
  scripts

echo "$ARCHIVE"
