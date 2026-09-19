#!/usr/bin/env python3
"""Headless mode — optimized for M4, 1000/sec target, 50KB per 1000 storage.
Ctrl+C stops and saves. Export: python3 -c "import db; db.export_csv('sites.csv')"
"""
import os
import subprocess
import sys
import time

import db

APP_DIR = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.join(APP_DIR, "crawler_engine")
TARGET_LIMIT = 100_000_000
BATCH = 500  # smaller batch for lower memory

def build_engine():
    if os.path.exists(ENGINE):
        return True
    src = os.path.join(APP_DIR, "crawler.c")
    # Try M4 flags first
    for cmd in (
        ["clang","-O3","-mcpu=apple-m4","-flto","-pthread",src,"-o",ENGINE],
        ["clang","-O3","-march=native","-flto","-pthread",src,"-o",ENGINE],
        ["clang","-O3","-pthread",src,"-o",ENGINE],
    ):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if r.returncode == 0 and os.path.exists(ENGINE):
                print(f"Built engine: {' '.join(cmd)}")
                return True
        except Exception:
            continue
    return False

def main():
    db.init_db()
    if not build_engine() and not os.path.exists(ENGINE):
        print("crawler_engine missing. Build: clang -O3 -pthread crawler.c -o crawler_engine")
        sys.exit(1)
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        want = 20000 if hard == resource.RLIM_INFINITY else min(hard, 20000)
        if want > soft:
            resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
    except Exception:
        pass

    proc = subprocess.Popen([ENGINE], stdout=subprocess.PIPE, text=True, errors="replace",
                            bufsize=1<<20, cwd=APP_DIR)
    batch, total, new, t0, last_ui = [], db.get_total_in_db(), 0, time.time(), 0.0
    stats = "-"
    print("English-only crawler running — M4 optimized, 50KB/1000 target — Ctrl+C to stop")
    try:
        for line in proc.stdout:
            if line.startswith("RESULT\t"):
                p = line.rstrip("\n").split("\t",2)
                if len(p)>=2 and p[1]:
                    batch.append((p[1], p[2] if len(p)>2 else ""))
                if len(batch)>=BATCH:
                    new+=db.insert_sites_batch(batch); batch=[]
            elif line.startswith("STATS\t"):
                stats=line.rstrip("\n").split("\t")
            now=time.time()
            if now-last_ui>=0.5:
                last_ui=now
                s=stats if isinstance(stats,list) and len(stats)>8 else None
                extra=f" | live {s[2]} queued {s[3]} non-en {s[8]}" if s else ""
                rate=new/max(now-t0,1)
                size=db.db_size_bytes()
                per=size/max(total+new,1) if total+new>0 else 0
                print(f"\rnew {new:,} {rate:,.0f}/s | db {total+new:,} | {size/1024:.1f}KB ({per:.1f}B/site){extra}   ", end="", flush=True)
            if total+new>=TARGET_LIMIT or db.db_size_bytes()>=db.MAX_DB_BYTES*0.98:
                print("\nTarget/storage budget reached."); break
    except KeyboardInterrupt:
        pass
    finally:
        proc.terminate()
        if batch:
            new+=db.insert_sites_batch(batch)
        print(f"\nSaved. {db.get_total_in_db():,} sites, {db.db_size_bytes()/1024:.1f}KB ({db.db_size_bytes()/max(db.get_total_in_db(),1):.1f} B/site)")

if __name__=="__main__":
    main()
