#!/bin/bash
# fix_mac.sh — fixes macOS Gatekeeper "malware" block for crawler_engine
# Run this if macOS says "crawler_engine is malware" or "cannot be opened"
# M4 Mac fix: removes quarantine + ad-hoc codesign

set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
BIN="$DIR/crawler_engine"

echo "=== WebCrawler Pro — macOS Malware Fix ==="
echo "Dir: $DIR"

# 1. Remove quarantine (Gatekeeper flag)
echo "[1/4] Removing quarantine..."
xattr -cr "$DIR" 2>/dev/null || true
xattr -d com.apple.quarantine "$BIN" 2>/dev/null || true
xattr -dr com.apple.quarantine "$DIR" 2>/dev/null || true

# 2. Ensure executable
echo "[2/4] chmod +x..."
chmod +x "$BIN" 2>/dev/null || true
chmod +x "$DIR"/*.sh 2>/dev/null || true

# 3. Ad-hoc codesign (no Apple dev account needed)
echo "[3/4] Ad-hoc codesigning..."
if command -v codesign >/dev/null 2>&1; then
    codesign --force --deep --sign - "$BIN" 2>/dev/null && echo "  codesigned $BIN" || echo "  codesign failed (ok)"
    # Also sign the whole folder if needed
    codesign --force --deep --sign - "$DIR" 2>/dev/null || true
else
    echo "  codesign not found, skipping"
fi

# 4. Verify
echo "[4/4] Verifying..."
ls -lh "$BIN" 2>/dev/null || echo "  binary not built yet — run make"
if [ -f "$BIN" ]; then
    echo "  executable: $(test -x "$BIN" && echo YES || echo NO)"
    echo "  quarantine: $(xattr -p com.apple.quarantine "$BIN" 2>&1 | head -1 || echo 'none (good)')"
    echo "  signature: $(codesign -dv "$BIN" 2>&1 | head -1 || echo 'ad-hoc')"
fi

echo ""
echo "Fix done. Now try:"
echo "  ./crawler_engine    # should not show malware popup"
echo "  python3 app.py"
echo ""
echo "If still blocked:"
echo "  System Settings → Privacy & Security → Scroll down → Allow Anyway → Allow crawler_engine"
echo "  Or: Right-click app.py → Open"
echo ""
echo "Alternative: Build from source on your Mac (no download = no quarantine):"
echo "  make clean && make"
