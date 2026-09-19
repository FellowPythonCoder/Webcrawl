# WebCrawler Pro — M4 optimized build
# Targets 1000/sec, 50KB per 1000 sites, adaptive bloom

CC=clang
SRC=crawler.c
BIN=crawler_engine

# Detect Apple Silicon
UNAME_M := $(shell uname -m)
ifeq ($(UNAME_M),arm64)
  # M4 / M3 / M1 / M2 — use best available
  CFLAGS_M4=-O3 -mcpu=apple-m4 -flto -pthread -DNDEBUG
  CFLAGS_NATIVE=-O3 -march=native -flto -pthread -DNDEBUG
  CFLAGS_FALLBACK=-O3 -pthread -DNDEBUG
else
  CFLAGS_M4=-O3 -march=native -flto -pthread -DNDEBUG
  CFLAGS_NATIVE=-O3 -march=native -flto -pthread -DNDEBUG
  CFLAGS_FALLBACK=-O3 -pthread -DNDEBUG
endif

all: $(BIN)

$(BIN): $(SRC) english.h dedupe.h
	@echo "Building for $(UNAME_M)..."
	@$(CC) $(CFLAGS_M4) $(SRC) -o $(BIN) 2>/dev/null && echo "Built with -mcpu=apple-m4" || \
	($(CC) $(CFLAGS_NATIVE) $(SRC) -o $(BIN) 2>/dev/null && echo "Built with -march=native" || \
	($(CC) $(CFLAGS_FALLBACK) $(SRC) -o $(BIN) && echo "Built with generic -O3"))
	@ls -lh $(BIN)
	@echo "Ready. Run: python3 app.py or python3 pipeline.py"

clean:
	rm -f $(BIN) crawler.db crawler.db-wal crawler.db-shm
	rm -rf __pycache__ *.pyc

run: $(BIN)
	python3 app.py

headless: $(BIN)
	python3 pipeline.py

test-storage:
	python3 -c 'import db,os,random,string; db.delete_all_data(); rows=[("".join(random.choices(string.ascii_lowercase,k=8))+".com"," ".join("".join(random.choices(string.ascii_lowercase,k=5)) for _ in range(3))) for _ in range(1000)]; db.insert_sites_batch(rows); print(f"Size for 1000: {os.path.getsize(db.DB_PATH)} bytes = {os.path.getsize(db.DB_PATH)/1000:.1f} B/site = {os.path.getsize(db.DB_PATH)/1024:.1f} KB")'

.PHONY: all clean run headless test-storage
