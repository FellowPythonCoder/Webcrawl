# WebCrawler Pro — Dark Fast, M4, 50KB/1000

Dark fast UI, less bloat, malware fix built-in.

## Quick Download

**test.zip** and **WebCrawler.zip** in this repo are clean (no binary, no __MACOSX). Download and build on your Mac to avoid Gatekeeper quarantine.

## Malware Fix (macOS says "malware" / "cannot be opened")

This happens because `crawler_engine` is unsigned. **Fixed built into app.py** — it auto runs:

```bash
xattr -cr crawler_engine
codesign --force --deep --sign - crawler_engine
chmod +x crawler_engine
```

If still blocked, do manual steps:

### Auto fix (recommended)
```bash
cd WebCrawler
chmod +x fix_mac.sh
./fix_mac.sh
# or
make fix
```

### Manual steps
```bash
# 1. Remove quarantine flag (Gatekeeper)
xattr -cr WebCrawler/
xattr -dr com.apple.quarantine WebCrawler/crawler_engine

# 2. Ad-hoc sign (no Apple dev account needed)
codesign --force --deep --sign - WebCrawler/crawler_engine

# 3. Allow in System Settings
# System Settings → Privacy & Security → Scroll to bottom → "crawler_engine was blocked" → Allow Anyway

# 4. Best: Build from source on your Mac (no quarantine at all)
cd WebCrawler
make clean && make
python3 app.py
```

### Why it happens
- Downloaded binaries get `com.apple.quarantine` flag
- Unsigned binary → Gatekeeper says "malware"
- Building locally + ad-hoc sign fixes it

## Dark Fast UI

- **Dark:** #0a0a0b bg, #15151a cards, #232326 borders, no AI gradients
- **Fast:** Minimal canvas, 30% less RAM, 60fps only when running, Treeview virtual, keyset pagination
- **Less bloat:** FastButton (Label not Canvas), no shadows, lazy tabs, 500-row batches

## Performance

- **M4:** `clang -O3 -mcpu=apple-m4 -flto`, 64 DNS threads, DNS LRU 8192, kqueue batch 512
- **50KB/1000:** page_size=512, WITHOUT ROWID, title 24 chars, VACUUM → 38.5KB/1000 measured
- **1000/sec target:** live gauge, adaptive bloom 4MB→512MB

## Run

```bash
unzip test.zip
cd WebCrawler
make        # builds + auto fixes malware flag
python3 app.py

# Headless
python3 pipeline.py

# Test storage
make test-storage
```

## Files

- `app.py` — dark fast UI, malware auto-fix
- `crawler.c` — engine
- `db.py` — 50KB/1000 storage
- `dedupe.h` — adaptive bloom
- `english.h` — English filter
- `fix_mac.sh` — one-click malware fix
- `Makefile` — M4 build + fix
