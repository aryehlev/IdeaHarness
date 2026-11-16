#!/bin/sh
# IdeaHarness installer.
#
#   curl -fsSL https://raw.githubusercontent.com/aryehlev/IdeaHarness/main/install.sh | sh
#
# Installs the `ideaharness` command. Needs only python3 (3.9+), curl and tar: IdeaHarness has
# no dependencies, so nothing is pip-installed. Run it again to upgrade.
#
#   IDEAHARNESS_VERSION   tag to install, e.g. v0.2.0 (default: main)
#   IDEAHARNESS_HOME      where the code, your profile and runs live (default: ~/.ideaharness)
#   IDEAHARNESS_BIN_DIR   where the command goes (default: ~/.local/bin)
#   IDEAHARNESS_URL       tarball to install from instead of GitHub

set -eu

REPO="aryehlev/IdeaHarness"
VERSION="${IDEAHARNESS_VERSION:-main}"
HOME_DIR="${IDEAHARNESS_HOME:-$HOME/.ideaharness}"
BIN_DIR="${IDEAHARNESS_BIN_DIR:-$HOME/.local/bin}"
if [ "$VERSION" = "main" ]; then
  DEFAULT_URL="https://github.com/$REPO/archive/refs/heads/main.tar.gz"
else
  DEFAULT_URL="https://github.com/$REPO/archive/refs/tags/$VERSION.tar.gz"
fi
URL="${IDEAHARNESS_URL:-$DEFAULT_URL}"

say() { printf '%s\n' "$*"; }
fail() { printf 'ideaharness install: %s\n' "$*" >&2; exit 1; }

[ -n "$HOME_DIR" ] && [ "$HOME_DIR" != "/" ] || fail "IDEAHARNESS_HOME must be a real directory"

PY=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 &&
     "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
    PY=$(command -v "$candidate")
    break
  fi
done
[ -n "$PY" ] || fail "needs Python 3.9 or later on your PATH"
command -v curl >/dev/null 2>&1 || fail "needs curl"
command -v tar >/dev/null 2>&1 || fail "needs tar"

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT INT TERM

say "Downloading IdeaHarness ($VERSION)..."
curl -fsSL "$URL" -o "$TMP/src.tar.gz" || fail "download failed: $URL"
tar -xzf "$TMP/src.tar.gz" -C "$TMP" || fail "could not unpack $URL"
SRC=$(find "$TMP" -mindepth 2 -maxdepth 2 -type d -name ideaharness | head -n 1)
[ -n "$SRC" ] && [ -f "$SRC/cli.py" ] || fail "the download does not contain the ideaharness package"

mkdir -p "$HOME_DIR" "$BIN_DIR"
rm -rf "$HOME_DIR/app.new"
mkdir -p "$HOME_DIR/app.new"
cp -R "$SRC" "$HOME_DIR/app.new/ideaharness"
rm -rf "$HOME_DIR/app"
mv "$HOME_DIR/app.new" "$HOME_DIR/app"

cat > "$BIN_DIR/ideaharness" <<EOF
#!/bin/sh
# Installed by IdeaHarness install.sh. Remove with: rm -rf "$HOME_DIR/app" "$BIN_DIR/ideaharness"
export IDEAHARNESS_HOME="\${IDEAHARNESS_HOME:-$HOME_DIR}"
PYTHONPATH="$HOME_DIR/app\${PYTHONPATH:+:\$PYTHONPATH}" exec "$PY" -m ideaharness "\$@"
EOF
chmod +x "$BIN_DIR/ideaharness"

say "Installed $("$BIN_DIR/ideaharness" --version) at $BIN_DIR/ideaharness"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *)
    say ""
    say "$BIN_DIR is not on your PATH yet. Add this line to your ~/.zshrc or ~/.bashrc:"
    say "  export PATH=\"$BIN_DIR:\$PATH\""
    ;;
esac
say ""
say "Next:"
say "  ideaharness doctor    # see which agent harnesses you have"
say "  ideaharness           # build your profile, then hunt for an idea"
