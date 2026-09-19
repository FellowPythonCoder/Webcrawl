# WebCrawler Pro — Dark Fast, M4, 50KB/1000, malware-fix
CC=clang
SRC=crawler.c
BIN=crawler_engine
UNAME_M := $(shell uname -m)
ifeq ($(UNAME_M),arm64)
  CFLAGS_M4=-O3 -mcpu=apple-m4 -flto -pthread -DNDEBUG
  CFLAGS_NATIVE=-O3 -march=native -flto -pthread -DNDEBUG
else
  CFLAGS_M4=-O3 -march=native -flto -pthread -DNDEBUG
  CFLAGS_NATIVE=-O3 -march=native -flto -pthread -DNDEBUG
endif
CFLAGS_FALLBACK=-O3 -pthread -DNDEBUG

all: $(BIN) fix

$(BIN): $(SRC) english.h dedupe.h
	@echo "Building for $(UNAME_M)..."
	@$(CC) $(CFLAGS_M4) $(SRC) -o $(BIN) 2>/dev/null && echo "Built M4" || \
	($(CC) $(CFLAGS_NATIVE) $(SRC) -o $(BIN) 2>/dev/null && echo "Built native" || \
	($(CC) $(CFLAGS_FALLBACK) $(SRC) -o $(BIN) && echo "Built generic"))
	@ls -lh $(BIN)
	@$(MAKE) fix

fix:
	@echo "Fixing macOS Gatekeeper..."
	@chmod +x $(BIN) 2>/dev/null || true
	@xattr -cr $(BIN) 2>/dev/null || true
	@xattr -d com.apple.quarantine $(BIN) 2>/dev/null || true
	@codesign --force --deep --sign - $(BIN) 2>/dev/null && echo "Ad-hoc signed (no malware popup)" || echo "codesign skipped (Linux?)"
	@echo "Ready: python3 app.py"

clean:
	rm -f $(BIN) crawler.db
	rm -rf __pycache__

run: $(BIN)
	python3 app.py

test-storage:
	python3 -c 'import db,os,random,string; db.delete_all_data(); rows=[("".join(random.choices(string.ascii_lowercase,k=8))+".com"," ".join("".join(random.choices(string.ascii_lowercase,k=4)) for _ in range(3))) for _ in range(1000)]; db.insert_sites_batch(rows); print(f"Size 1000: {os.path.getsize(db.DB_PATH)} bytes = {os.path.getsize(db.DB_PATH)/1024:.1f} KB ({os.path.getsize(db.DB_PATH)/1000:.1f} B/site)")'

.PHONY: all clean run fix test-storage
