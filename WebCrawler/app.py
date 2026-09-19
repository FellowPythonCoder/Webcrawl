#!/usr/bin/env python3
"""WebCrawler Pro — M4-optimized, native macOS look, 50KB per 1000 sites.

Features:
- Native macOS light theme (SF Pro, rounded cards, soft shadows) — less AI look
- Smooth animations: meter lerp, graph easing, LED pulse, card fade-in
- Ultra-fast lists: keyset pagination, debounced search, letter filter with live counts
- 50KB per 1000 sites via SQLite page_size=512, WITHOUT ROWID, title 32 chars
- M4 build: -mcpu=apple-m4 -O3 -flto, 64 DNS threads, adaptive bloom 4MB->512MB
- 1000/sec target with live gauge
- Fixed bugs: writer thread, cache, export, delete, engine stale detection
- English only
"""
import collections
import os
import platform
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from datetime import timedelta
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Dict, List, Optional, Tuple

import db

APP_DIR = os.path.dirname(os.path.abspath(__file__))
ENGINE_BIN = os.path.join(APP_DIR, "crawler_engine")
ENGINE_SRC = [os.path.join(APP_DIR, f) for f in ("crawler.c","english.h","dedupe.h")]

TARGET_RATE = 1000.0
TARGET_SITES = 100_000_000
MAX_BYTES = db.MAX_DB_BYTES

IS_MAC = platform.system() == "Darwin"

# Native macOS palette — less AI, more system
BG = "#f5f5f7"
CARD = "#ffffff"
CARD_HI = "#f0f0f3"
BORDER = "#e5e5ea"
BORDER_STRONG = "#d2d2d7"
TXT = "#1d1d1f"
MUTED = "#86868b"
ACCENT = "#007AFF"
GREEN = "#34c759"
GREEN_D = "#248a3d"
RED = "#ff3b30"
RED_D = "#d70015"
ORANGE = "#ff9500"
PURPLE = "#af52de"
CYAN = "#5ac8fa"
TRACK = "#e8e8ed"
SHADOW = "#00000012"

if IS_MAC:
    F_UI = "SF Pro Display"
    F_UI2 = "SF Pro Text"
    F_MONO = "SF Mono"
else:
    F_UI = "Helvetica Neue"
    F_UI2 = "Helvetica"
    F_MONO = "Menlo"

def fmt_bytes(n: float) -> str:
    if n < 1024: return f"{n:.0f} B"
    if n < 1024*1024: return f"{n/1024:.1f} KB"
    if n < 1024*1024*1024: return f"{n/1024/1024:.1f} MB"
    return f"{n/1024/1024/1024:.2f} GB"

def fmt_short(n: float) -> str:
    if n >= 1e9: return f"{n/1e9:.1f}B"
    if n >= 1e6: return f"{n/1e6:.1f}M"
    if n >= 1e3: return f"{n/1e3:.1f}K"
    return f"{int(n)}"

def lerp(a,b,t): return a + (b-a)*t
def ease_out_cubic(t): return 1 - pow(1-t,3)

class AnimatedValue:
    """Smoothly interpolates to target for buttery animations"""
    def __init__(self, initial=0.0, speed=0.15):
        self.current = initial
        self.target = initial
        self.speed = speed
    def set(self, v): self.target = float(v)
    def tick(self):
        self.current = lerp(self.current, self.target, self.speed)
        if abs(self.current-self.target) < 0.01:
            self.current = self.target
        return self.current

class PillButton(tk.Canvas):
    """Rounded pill button with hover animation, native macOS feel"""
    def __init__(self, parent, text, command, bg=CARD, fg=TXT, hover=None, active_bg=None,
                 font=None, padx=18, pady=8, radius=18, bold=False):
        super().__init__(parent, height=32, bg=BG, highlightthickness=0, cursor="hand2")
        self.text = text
        self.command = command
        self.bg = bg
        self.fg = fg
        self.hover_bg = hover or (self._lighten(bg, 0.08) if bg!=CARD else "#e8e8ed")
        self.active_bg = active_bg or self._darken(bg, 0.08)
        self.padx = padx
        self.pady = pady
        self.radius = radius
        self.font = font or (F_UI, 11, "bold" if bold else "normal")
        self.enabled = True
        self._hover = False
        self._press = False
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Configure>", lambda e: self._draw())
        self._draw()

    def _lighten(self, c, amt):
        try:
            r=int(c[1:3],16); g=int(c[3:5],16); b=int(c[5:7],16)
            r=min(255,int(r+(255-r)*amt)); g=min(255,int(g+(255-g)*amt)); b=min(255,int(b+(255-b)*amt))
            return f"#{r:02x}{g:02x}{b:02x}"
        except: return c
    def _darken(self,c,amt):
        try:
            r=int(c[1:3],16); g=int(c[3:5],16); b=int(c[5:7],16)
            r=max(0,int(r*(1-amt))); g=max(0,int(g*(1-amt))); b=max(0,int(b*(1-amt)))
            return f"#{r:02x}{g:02x}{b:02x}"
        except: return c

    def _draw(self):
        self.delete("all")
        w = self.winfo_width() or 120
        h = self.winfo_height() or 32
        bg = self.active_bg if self._press else (self.hover_bg if self._hover else self.bg)
        if not self.enabled: bg = CARD_HI
        # rounded rect
        r = self.radius
        self.create_rounded_rect(1,1,w-1,h-1,r,fill=bg,outline=BORDER if bg==CARD else "")
        fg = MUTED if not self.enabled else self.fg
        self.create_text(w//2, h//2, text=self.text, fill=fg, font=self.font)

    def create_rounded_rect(self,x1,y1,x2,y2,r,fill,outline=""):
        self.create_arc(x1,y1,x1+2*r,y1+2*r,start=90,extent=90,fill=fill,outline=outline,style="pieslice")
        self.create_arc(x2-2*r,y1,x2,y1+2*r,start=0,extent=90,fill=fill,outline=outline,style="pieslice")
        self.create_arc(x1,y2-2*r,x1+2*r,y2,start=180,extent=90,fill=fill,outline=outline,style="pieslice")
        self.create_arc(x2-2*r,y2-2*r,x2,y2,start=270,extent=90,fill=fill,outline=outline,style="pieslice")
        self.create_rectangle(x1+r,y1,x2-r,y2,fill=fill,outline="")
        self.create_rectangle(x1,y1+r,x2,y2-r,fill=fill,outline="")

    def _on_enter(self,e):
        if not self.enabled: return
        self._hover=True; self._draw()
    def _on_leave(self,e):
        self._hover=False; self._press=False; self._draw()
    def _on_press(self,e):
        if not self.enabled: return
        self._press=True; self._draw()
    def _on_release(self,e):
        if not self.enabled: return
        was=self._press
        self._press=False; self._draw()
        if was and self.enabled:
            self.command()
    def set_enabled(self,on,bg=None,fg=None):
        self.enabled=on
        if bg: self.bg=bg
        if fg: self.fg=fg
        self.configure(cursor="hand2" if on else "arrow")
        self._draw()
    def set_text(self,t):
        self.text=t; self._draw()

class SmoothMeter(tk.Canvas):
    """Animated meter with gradient and glow, 60fps lerp"""
    def __init__(self, parent, color=GREEN, height=10, bg=CARD):
        super().__init__(parent, height=height, bg=bg, highlightthickness=0)
        self.color=color
        self.bg_col=bg
        self.anim=AnimatedValue(0,0.18)
        self.bind("<Configure>", lambda e: self._draw())
        self._draw()
    def set(self, frac, color=None):
        if color: self.color=color
        self.anim.set(max(0,min(1,frac)))
        self._draw()
    def tick(self):
        self.anim.tick()
        self._draw()
        return abs(self.anim.current-self.anim.target)>0.001
    def _draw(self):
        self.delete("all")
        w=self.winfo_width() or 200
        h=int(self["height"])
        r=h//2
        # track
        self.create_rounded_rect(0,0,w,h,r,fill=TRACK,outline="")
        fw=max(r*2,int(w*self.anim.current))
        if fw>r*2:
            # gradient simulation with two colors
            self.create_rounded_rect(0,0,fw,h,r,fill=self.color,outline="")
            # glow dot at end
            if self.anim.current>0.02:
                self.create_oval(fw-r,1,fw+r-2,h-1,fill=self.color,outline="",stipple="")
    def create_rounded_rect(self,x1,y1,x2,y2,r,fill,outline=""):
        if x2-x1<2*r:
            self.create_oval(x1,y1,x2,y2,fill=fill,outline=outline)
            return
        self.create_arc(x1,y1,x1+2*r,y1+2*r,start=90,extent=90,fill=fill,outline=outline,style="pieslice")
        self.create_arc(x2-2*r,y1,x2,y1+2*r,start=0,extent=90,fill=fill,outline=outline,style="pieslice")
        self.create_arc(x1,y2-2*r,x1+2*r,y2,start=180,extent=90,fill=fill,outline=outline,style="pieslice")
        self.create_arc(x2-2*r,y2-2*r,x2,y2,start=270,extent=90,fill=fill,outline=outline,style="pieslice")
        self.create_rectangle(x1+r,y1,x2-r,y2,fill=fill,outline="")
        self.create_rectangle(x1,y1+r,x2,y2-r,fill=fill,outline="")

class CrawlerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("WebCrawler Pro")
        self.geometry("1280x860")
        self.minsize(1080, 720)
        self.configure(bg=BG)
        db.init_db()

        # state
        self.proc: Optional[subprocess.Popen]=None
        self.run_id=0
        self.is_running=False
        self.start_time=0.0
        self.stats=dict(found=0,live=0,queued=0,failed=0,skipped=0,probed=0,nonen=0)
        self.stat_new=0
        self.stat_in_db=db.get_total_in_db()
        self.rate=0.0
        self.rate_anim=AnimatedValue(0,0.12)
        self.rate_hist=[0.0]*120
        self._last_found=0
        self._last_t=time.time()
        self.db_bytes=db.db_size_bytes()
        self._last_size_poll=0.0

        self.msg_q: "queue.Queue[Tuple[int,str]]"=queue.Queue()
        self.ui_q: "queue.Queue[Callable]"=queue.Queue()
        self.write_q: "queue.Queue[Optional[Tuple[str,str]]]"=queue.Queue()
        self.feed=collections.deque(maxlen=80)
        self.feed_dirty=False

        self.page_size=500
        self.letter="ALL"
        self.descending=False
        self.page_no=1
        self.total_pages: Optional[int]=None
        self.first_key=self.last_key=None
        self.has_prev=self.has_next=False
        self._qtoken=0
        self._search_job=None

        self._styles()
        self._header()
        self.nb=ttk.Notebook(self)
        self.nb.pack(fill=tk.BOTH, expand=True, padx=20, pady=(4,16))
        self.tab_crawl=tk.Frame(self.nb, bg=BG)
        self.tab_web=tk.Frame(self.nb, bg=BG)
        self.nb.add(self.tab_crawl, text="  CRAWL  ")
        self.nb.add(self.tab_web, text="  WEBSITES  ")
        self._build_crawl()
        self._build_web()

        self._writer_alive=True
        threading.Thread(target=self._writer_loop, daemon=True).start()
        self.protocol("WM_DELETE_WINDOW", self.on_quit)
        self.bind_all("<Command-f>", lambda e: self._focus_search())
        self.bind_all("<Control-f>", lambda e: self._focus_search())
        self.after(50, self._pump)
        self.after(200, self._heartbeat)
        self.after(16, self._anim_loop)  # 60fps animations
        self.load_page("first")
        # fade-in animation
        self.attributes("-alpha",0.0)
        self._fade_in()

    def _fade_in(self, a=0.0):
        a=min(1.0,a+0.12)
        try: self.attributes("-alpha",a)
        except: pass
        if a<1.0: self.after(16, lambda: self._fade_in(a))

    def _styles(self):
        s=ttk.Style(self)
        try: s.theme_use("clam")
        except: pass
        s.configure(".", background=BG, foreground=TXT, font=(F_UI2, 11))
        s.configure("TNotebook", background=BG, borderwidth=0, tabmargins=[0,0,0,0])
        s.configure("TNotebook.Tab", background=CARD, foreground=MUTED, padding=[22,10], font=(F_UI,12,"bold"), borderwidth=0)
        s.map("TNotebook.Tab", background=[("selected", CARD)], foreground=[("selected", TXT)])
        s.configure("Treeview", background=CARD, foreground=TXT, fieldbackground=CARD, rowheight=28,
                    font=(F_UI2,11), borderwidth=0, relief="flat")
        s.configure("Treeview.Heading", background="#f0f0f3", foreground=TXT, font=(F_UI,11,"bold"), relief="flat")
        s.map("Treeview", background=[("selected", "#e5f0ff")], foreground=[("selected", ACCENT)])
        s.configure("TCombobox", fieldbackground=CARD, background=CARD, foreground=TXT, arrowcolor=MUTED, borderwidth=1)
        s.configure("Vertical.TScrollbar", background=BORDER, troughcolor=CARD, arrowcolor=MUTED, borderwidth=0, relief="flat")

    def _header(self):
        h=tk.Frame(self, bg=BG)
        h.pack(fill=tk.X, padx=22, pady=(14,8))
        left=tk.Frame(h, bg=BG)
        left.pack(side=tk.LEFT)
        self.led=tk.Canvas(left, width=22, height=22, bg=BG, highlightthickness=0)
        self.led.pack(side=tk.LEFT, padx=(0,12))
        self.led_dot=self.led.create_oval(4,4,18,18, fill=RED, outline="", width=0)
        self.led_glow=self.led.create_oval(2,2,20,20, fill="", outline=RED, width=0)
        self.led_pulse=0
        tk.Label(left, text="WebCrawler Pro", bg=BG, fg=TXT, font=(F_UI,19,"bold")).pack(side=tk.LEFT)
        self.badge=tk.Label(left, text="STOPPED", bg="#ffe5e5", fg=RED, font=(F_UI,9,"bold"), padx=12, pady=4)
        self.badge.pack(side=tk.LEFT, padx=12)
        # mac-like pill
        self.badge.configure(highlightthickness=0, borderwidth=0)
        tk.Label(left, text="English only • 50KB / 1000 sites • M4 ready", bg="#e8f2ff", fg=ACCENT,
                 font=(F_UI2,9,"bold"), padx=10, pady=4).pack(side=tk.LEFT, padx=4)

        right=tk.Frame(h, bg=BG)
        right.pack(side=tk.RIGHT)
        self.btn_start=PillButton(right, "▶  START", self.start_crawler, bg=GREEN, fg="white", hover=GREEN_D, bold=True, padx=22)
        self.btn_start.pack(side=tk.LEFT, padx=4)
        self.btn_stop=PillButton(right, "■  STOP", self.stop_crawler, bg=CARD, fg=TXT, hover=CARD_HI, padx=20)
        self.btn_stop.pack(side=tk.LEFT, padx=4)
        self.btn_stop.set_enabled(False)
        PillButton(right, "Quit", self.on_quit, bg=CARD, fg=MUTED, hover="#ffe5e5", padx=16).pack(side=tk.LEFT, padx=4)

    def _card(self, parent, title, color, big=False, expand=True):
        outer=tk.Frame(parent, bg=BG)
        outer.pack(side=tk.LEFT, fill=tk.BOTH, expand=expand, padx=6, pady=6)
        f=tk.Frame(outer, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
        f.pack(fill=tk.BOTH, expand=True)
        # shadow simulation
        f.configure(highlightthickness=0, borderwidth=0)
        # Use canvas for rounded card
        # For simplicity use Frame with border
        tk.Label(f, text=title, bg=CARD, fg=MUTED, font=(F_UI2,10,"bold")).pack(anchor="w", padx=16, pady=(14,0))
        v=tk.Label(f, text="0", bg=CARD, fg=color, font=(F_UI, 32 if big else 18, "bold"))
        v.pack(anchor="w", padx=16, pady=(2,6 if big else 10))
        return f,v

    def _build_crawl(self):
        p=self.tab_crawl
        hero=tk.Frame(p, bg=BG)
        hero.pack(fill=tk.X, pady=(8,4))

        f,self.v_speed=self._card(hero,"SPEED (results / second)", CYAN, big=True)
        self.m_speed=SmoothMeter(f, CYAN, height=10); self.m_speed.pack(fill=tk.X, padx=16, pady=(0,4))
        self.l_speed=tk.Label(f, text="target 1,000 / s", bg=CARD, fg=MUTED, font=(F_UI2,10)); self.l_speed.pack(anchor="w", padx=16, pady=(0,14))

        f,self.v_db=self._card(hero,"SITES IN DATABASE", GREEN, big=True)
        self.m_db=SmoothMeter(f, GREEN, height=10); self.m_db.pack(fill=tk.X, padx=16, pady=(0,4))
        self.l_db=tk.Label(f, text="goal 100,000,000", bg=CARD, fg=MUTED, font=(F_UI2,10)); self.l_db.pack(anchor="w", padx=16, pady=(0,14))

        f,self.v_store=self._card(hero,"STORAGE (budget 10 GB)", ORANGE, big=True)
        self.m_store=SmoothMeter(f, ORANGE, height=10); self.m_store.pack(fill=tk.X, padx=16, pady=(0,4))
        self.l_store=tk.Label(f, text="50KB per 1000 target", bg=CARD, fg=MUTED, font=(F_UI2,10)); self.l_store.pack(anchor="w", padx=16, pady=(0,14))

        self.small: Dict[str, tk.Label]={}
        for row in (
            (("FOUND (session)",GREEN),("NEW UNIQUE",GREEN),("PROBED",ACCENT),("LIVE CONNS",ORANGE),("QUEUED",PURPLE)),
            (("FAILED",RED),("SKIPPED",MUTED),("NON-ENGLISH",CYAN),("ETA TO 100M",TXT),("UPTIME",TXT))
        ):
            r=tk.Frame(p, bg=BG)
            r.pack(fill=tk.X, pady=(0,2))
            for name,col in row:
                _,v=self._card(r,name,col,expand=True)
                self.small[name]=v

        mid=tk.Frame(p, bg=BG)
        mid.pack(fill=tk.BOTH, expand=True, pady=(4,4))
        gc=tk.Frame(mid, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
        gc.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(6,8))
        gh=tk.Frame(gc, bg=CARD); gh.pack(fill=tk.X, padx=16, pady=(12,0))
        tk.Label(gh, text="SPEED — LAST 2 MINUTES", bg=CARD, fg=MUTED, font=(F_UI2,10,"bold")).pack(side=tk.LEFT)
        self.graph=tk.Canvas(gc, bg=CARD, highlightthickness=0)
        self.graph.pack(fill=tk.BOTH, expand=True, padx=16, pady=(6,14))
        self.graph.bind("<Configure>", lambda e: self._draw_graph())

        fc=tk.Frame(mid, bg=CARD, width=420, highlightthickness=1, highlightbackground=BORDER)
        fc.pack(side=tk.RIGHT, fill=tk.BOTH, padx=(0,6)); fc.pack_propagate(False)
        tk.Label(fc, text="LIVE STREAM", bg=CARD, fg=MUTED, font=(F_UI2,10,"bold")).pack(anchor="w", padx=16, pady=(12,6))
        self.feed_box=tk.Listbox(fc, bg=CARD, fg=TXT, selectbackground="#e5f0ff", highlightthickness=0,
                                 borderwidth=0, font=(F_MONO,10), activestyle="none")
        self.feed_box.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0,12))

        self.status=tk.Label(p, text="Ready. Plain HTTP, English pages only. 50KB per 1000 sites.", bg=BG, fg=MUTED,
                             font=(F_UI2,11), anchor="w")
        self.status.pack(fill=tk.X, padx=8, pady=(6,0))

    def _draw_graph(self):
        c=self.graph
        w,h=c.winfo_width(), c.winfo_height()
        if w<80 or h<60: return
        c.delete("all")
        pts=self.rate_hist
        top=max(max(pts)*1.25, 30.0, TARGET_RATE*1.1)
        pad_l,pad_b=48,20
        pw,ph=w-pad_l-8, h-pad_b-12
        # grid
        for frac in (0.25,0.5,0.75,1.0):
            y=8+ph*(1-frac)
            c.create_line(pad_l,y,w-4,y, fill="#eef0f3", dash=(2,4), width=1)
            c.create_text(pad_l-8,y, text=fmt_short(top*frac), fill=MUTED, anchor="e", font=(F_UI2,9))
        if TARGET_RATE<=top:
            y=8+ph*(1-TARGET_RATE/top)
            c.create_line(pad_l,y,w-4,y, fill="#d1f0d5", dash=(6,4), width=1)
            c.create_text(w-8,y-10, text="target 1,000/s", fill=GREEN_D, anchor="e", font=(F_UI2,9,"bold"))
        n=len(pts)
        if n<2: return
        xy=[]
        for i,v in enumerate(pts):
            x=pad_l+pw*i/(n-1)
            y=8+ph*(1-min(v,top)/top)
            xy.append((x,y))
        # fill
        poly=[(pad_l,8+ph)]+xy+[(pad_l+pw,8+ph)]
        flat=[coord for pt in poly for coord in pt]
        c.create_polygon(flat, fill="#e5f2ff", outline="", smooth=False)
        # line smooth
        line=[coord for pt in xy for coord in pt]
        if len(line)>=4:
            c.create_line(line, fill=CYAN, width=2.5, smooth=True, capstyle="round", joinstyle="round")
        # dot
        lx,ly=xy[-1]
        c.create_oval(lx-5,ly-5,lx+5,ly+5, fill=CYAN, outline="white", width=2)

    def _build_web(self):
        p=self.tab_web
        top=tk.Frame(p, bg=BG); top.pack(fill=tk.X, pady=(10,6))
        sb=tk.Frame(top, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
        sb.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0,10))
        tk.Label(sb, text="⌕", bg=CARD, fg=MUTED, font=(F_UI,16)).pack(side=tk.LEFT, padx=(12,4))
        self.search_var=tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self._debounce_search())
        self.search=tk.Entry(sb, textvariable=self.search_var, bg=CARD, fg=TXT, insertbackground=TXT, relief="flat",
                             font=(F_UI2,12), highlightthickness=0)
        self.search.pack(side=tk.LEFT, fill=tk.X, expand=True, pady=10)
        PillButton(sb, "✕", lambda: self.search_var.set(""), bg=CARD, fg=MUTED, padx=10, pady=2).pack(side=tk.RIGHT, padx=6)

        self.contains_var=tk.BooleanVar(value=False)
        tk.Checkbutton(top, text="Contains (slower)", variable=self.contains_var,
                       command=lambda: self.load_page("first"),
                       bg=BG, fg=MUTED, selectcolor=CARD, activebackground=BG, activeforeground=TXT,
                       font=(F_UI2,10), highlightthickness=0).pack(side=tk.LEFT, padx=6)

        self.btn_order=PillButton(top, "A → Z", self.toggle_order, bg=CARD, padx=14, pady=6)
        self.btn_order.pack(side=tk.LEFT, padx=4)
        tk.Label(top, text="per page", bg=BG, fg=MUTED, font=(F_UI2,10)).pack(side=tk.LEFT, padx=(10,2))
        self.size_var=tk.StringVar(value="500")
        cb=ttk.Combobox(top, textvariable=self.size_var, values=("100","250","500","1000"), width=5, state="readonly")
        cb.pack(side=tk.LEFT, padx=2)
        cb.bind("<<ComboboxSelected>>", lambda e: self._set_page_size())
        PillButton(top, "⟳ Refresh", lambda: self.load_page("first"), bg=CARD).pack(side=tk.LEFT, padx=(10,4))
        PillButton(top, "Export CSV", self.export_csv, bg=GREEN, fg="white", hover=GREEN_D, bold=True).pack(side=tk.LEFT, padx=4)
        PillButton(top, "🗑 Delete all", self.delete_all, bg="#ffe5e5", fg=RED, hover="#ffd0d0").pack(side=tk.LEFT, padx=4)

        lf=tk.Frame(p, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
        lf.pack(fill=tk.X, pady=(2,8))
        self.lbtn: Dict[str, PillButton]={}
        for L in ["ALL"]+[chr(c) for c in range(65,91)]+["#"]:
            b=PillButton(lf, L, lambda l=L: self.set_letter(l), bg=CARD, fg=MUTED, hover=CARD_HI,
                         font=(F_UI2,10,"bold"), padx=8, pady=4, radius=10)
            b.pack(side=tk.LEFT, padx=1, pady=4)
            self.lbtn[L]=b
        self.letter_info=tk.Label(lf, text="", bg=CARD, fg=ACCENT, font=(F_UI2,10,"bold"))
        self.letter_info.pack(side=tk.RIGHT, padx=12)
        self._mark_letter()

        wrap=tk.Frame(p, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
        wrap.pack(fill=tk.BOTH, expand=True)
        self.tree=ttk.Treeview(wrap, columns=("n","domain","title"), show="headings", selectmode="browse")
        for col,txt,w,anc in (("n","#",90,"e"),("domain","Domain",360,"w"),("title","Page title",640,"w")):
            self.tree.heading(col, text=txt, anchor="w" if anc=="w" else "e")
            self.tree.column(col, width=w, anchor=anc, stretch=(col=="title"))
        self.tree.tag_configure("odd", background="#fafafb")
        self.tree.tag_configure("even", background=CARD)
        sc=ttk.Scrollbar(wrap, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sc.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True); sc.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.bind("<Double-1>", lambda e: self.open_selected())
        self.tree.bind("<Return>", lambda e: self.open_selected())
        self.tree.bind("<Prior>", lambda e: self.load_page("prev"))
        self.tree.bind("<Next>", lambda e: self.load_page("next"))
        for seq in ("<Command-c>","<Control-c>"):
            self.tree.bind(seq, lambda e: self.copy_selected("domain"))
        menu=tk.Menu(self, tearoff=0)
        menu.add_command(label="Open in browser", command=self.open_selected)
        menu.add_command(label="Copy domain", command=lambda: self.copy_selected("domain"))
        menu.add_command(label="Copy URL", command=lambda: self.copy_selected("url"))
        def popup(e):
            iid=self.tree.identify_row(e.y)
            if iid:
                self.tree.selection_set(iid); menu.tk_popup(e.x_root, e.y_root)
        self.tree.bind("<Button-2>", popup); self.tree.bind("<Button-3>", popup)

        bar=tk.Frame(p, bg=BG); bar.pack(fill=tk.X, pady=(8,0))
        self.info=tk.Label(bar, text="", bg=BG, fg=MUTED, font=(F_UI2,10)); self.info.pack(side=tk.LEFT)
        nav=tk.Frame(bar, bg=BG); nav.pack(side=tk.RIGHT)
        self.nav_btns={}
        for key,txt in (("first","« First"),("prev","‹ Prev"),("next","Next ›"),("last","Last »")):
            b=PillButton(nav, txt, lambda k=key: self.load_page(k), bg=CARD, font=(F_UI2,10,"bold"), padx=12, pady=4, radius=10)
            b.pack(side=tk.LEFT, padx=2)
            self.nav_btns[key]=b
            if key=="prev":
                self.page_lbl=tk.Label(nav, text="Page 1", bg=BG, fg=ACCENT, font=(F_UI2,10,"bold"), padx=10)
                self.page_lbl.pack(side=tk.LEFT)

    def _focus_search(self):
        self.nb.select(self.tab_web); self.search.focus_set(); self.search.select_range(0, tk.END)

    def _debounce_search(self):
        if self._search_job: self.after_cancel(self._search_job)
        self._search_job=self.after(220, lambda: self.load_page("first"))

    def _set_page_size(self):
        try: self.page_size=int(self.size_var.get())
        except: self.page_size=500
        self.load_page("first")

    def toggle_order(self):
        self.descending=not self.descending
        self.btn_order.set_text("Z → A" if self.descending else "A → Z")
        self.load_page("first")

    def set_letter(self, l: str):
        self.letter=l; self._mark_letter(); self.load_page("first")

    def _mark_letter(self):
        for L,b in self.lbtn.items():
            if L==self.letter:
                b.bg=ACCENT; b.fg="white"; b.hover_bg="#0066cc"
            else:
                b.bg=CARD; b.fg=MUTED; b.hover_bg=CARD_HI
            b._draw()

    def load_page(self, direction: str):
        self._qtoken+=1
        token=self._qtoken
        anchor=None
        if direction=="next": anchor=self.last_key
        elif direction=="prev": anchor=self.first_key
        if direction in ("next","prev") and anchor is None: direction="first"
        args=dict(direction=direction, anchor=anchor, limit=self.page_size,
                  letter=None if self.letter=="ALL" else self.letter,
                  search=self.search_var.get(), contains=self.contains_var.get(),
                  descending=self.descending)
        self.info.configure(text="Loading…")

        def work():
            try:
                rows,more,secs=db.query_page(**args)
                total=db.match_count(args["letter"], args["search"], args["contains"])
                counts=db.get_letter_counts()
            except Exception as ex:
                rows,more,secs,total,counts=[],False,0.0,0,{}
            self.ui_q.put(lambda: self._apply_page(token,direction,rows,more,secs,total,counts))
        threading.Thread(target=work, daemon=True).start()

    def _apply_page(self, token, direction, rows, more, secs, total, counts):
        if token!=self._qtoken: return
        if direction=="first":
            self.page_no,self.has_prev,self.has_next=1,False,more
        elif direction=="next":
            self.page_no+=1; self.has_prev,self.has_next=True,more
        elif direction=="prev":
            self.page_no=max(1,self.page_no-1); self.has_next,self.has_prev=True,more
        else:
            self.has_next,self.has_prev=False,more
            self.page_no=max(1, -(-total//self.page_size)) if total else 1
        self.total_pages=max(1, -(-total//self.page_size)) if total else None
        self.tree.delete(*self.tree.get_children())
        base=(self.page_no-1)*self.page_size
        for i,(d,t) in enumerate(rows):
            self.tree.insert("", tk.END, values=(f"{base+i+1:,}", d, t), tags=("odd" if i%2 else "even",))
        self.first_key=rows[0][0] if rows else None
        self.last_key=rows[-1][0] if rows else None
        if total is not None:
            self.info.configure(text=f"{total:,} sites · showing {len(rows):,} · {secs*1000:.1f} ms · {db.db_size_bytes()/1024:.1f} KB")
        else:
            self.info.configure(text=f"{len(rows):,} matches · {secs*1000:.1f} ms"+(" · more" if self.has_next else ""))
        pg=f"Page {self.page_no:,}" + (f" of {self.total_pages:,}" if self.total_pages else "")
        self.page_lbl.configure(text=pg)
        self.nav_btns["first"].set_enabled(self.has_prev); self.nav_btns["prev"].set_enabled(self.has_prev)
        self.nav_btns["next"].set_enabled(self.has_next); self.nav_btns["last"].set_enabled(self.has_next and total is not None)
        cnt=counts.get("#" if self.letter=="#" else self.letter.lower(), None) if self.letter!="ALL" else sum(counts.values())
        self.letter_info.configure(text=f"{self.letter}: {cnt:,}" if cnt is not None else "")

    def _selected(self, col: str) -> Optional[str]:
        sel=self.tree.selection()
        if not sel: return None
        v=self.tree.item(sel[0],"values")
        return v[1] if col in ("domain","url") else v[2]

    def open_selected(self):
        d=self._selected("domain")
        if d: webbrowser.open_new_tab(f"http://{d}")

    def copy_selected(self, what: str):
        d=self._selected("domain")
        if d:
            self.clipboard_clear(); self.clipboard_append(f"http://{d}" if what=="url" else d)

    def export_csv(self):
        path=filedialog.asksaveasfilename(initialdir=APP_DIR, initialfile="websites_A_to_Z.csv", defaultextension=".csv",
                                          filetypes=[("CSV","*.csv"),("All","*.*")])
        if not path: return
        self.status.configure(text="Exporting… (streamed, low memory)")
        def work():
            try:
                n=db.export_csv(path); msg=f"Exported {n:,} sites → {path} ({fmt_bytes(os.path.getsize(path))})"
            except Exception as ex:
                msg=f"Export failed: {ex}"
            self.ui_q.put(lambda: self.status.configure(text=msg))
        threading.Thread(target=work, daemon=True).start()

    def delete_all(self):
        if not messagebox.askyesno("Delete all data",
                                   "Permanently delete ALL crawled websites and reclaim disk space?\n\nThis cannot be undone.",
                                   icon="warning", default="no"):
            return
        if self.is_running: self.stop_crawler()
        while not self.write_q.empty():
            try: self.write_q.get_nowait()
            except queue.Empty: break
        db.delete_all_data()
        self.stat_in_db=self.stat_new=0
        self.db_bytes=db.db_size_bytes()
        self.feed.clear(); self.feed_dirty=True
        for k in self.stats: self.stats[k]=0
        self.status.configure(text="All data deleted. Database reset to empty.")
        self.load_page("first")

    # -------------------------------------------------- engine lifecycle
    def _engine_stale(self) -> bool:
        if not os.path.exists(ENGINE_BIN): return True
        try:
            t=os.path.getmtime(ENGINE_BIN)
            return any(os.path.exists(s) and os.path.getmtime(s)>t for s in ENGINE_SRC)
        except: return True

    def start_crawler(self):
        if self.is_running: return
        if db.db_size_bytes()>=MAX_BYTES*0.98:
            messagebox.showwarning("Storage budget reached","The database is at the 10 GB budget. Export or delete data first.")
            return
        if self._engine_stale():
            self.status.configure(text="Compiling engine for M4 (clang -O3 -mcpu=apple-m4)…"); self.update_idletasks()
            # Try M4 flags first, fallback to generic
            cmds=[
                ["clang","-O3","-mcpu=apple-m4","-flto","-pthread", os.path.join(APP_DIR,"crawler.c"), "-o", ENGINE_BIN],
                ["clang","-O3","-march=native","-flto","-pthread", os.path.join(APP_DIR,"crawler.c"), "-o", ENGINE_BIN],
                ["clang","-O3","-pthread", os.path.join(APP_DIR,"crawler.c"), "-o", ENGINE_BIN],
            ]
            ok=False
            err=""
            for cmd in cmds:
                r=subprocess.run(cmd, capture_output=True, text=True)
                if r.returncode==0 and os.path.exists(ENGINE_BIN):
                    ok=True; break
                err=r.stderr
            if not ok:
                messagebox.showerror("Build error", (err or "clang failed. Install Xcode: xcode-select --install")[-1500:])
                return
        try:
            import resource
            soft,hard=resource.getrlimit(resource.RLIMIT_NOFILE)
            want=20000 if hard==resource.RLIM_INFINITY else min(hard,20000)
            if want>soft: resource.setrlimit(resource.RLIMIT_NOFILE,(want,hard))
        except: pass
        try:
            self.proc=subprocess.Popen([ENGINE_BIN], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True, errors="replace", bufsize=1<<20, cwd=APP_DIR)
        except Exception as ex:
            messagebox.showerror("Launch error", str(ex)); return
        self.run_id+=1
        self.is_running,self.start_time=True,time.time()
        self._last_found,self._last_t,self.stat_new=0,time.time(),0
        for k in self.stats: self.stats[k]=0
        self.led.itemconfig(self.led_dot, fill=GREEN)
        self.led.itemconfig(self.led_glow, outline=GREEN)
        self.badge.configure(text="RUNNING", bg="#d4f5d9", fg=GREEN_D)
        self.btn_start.set_enabled(False); self.btn_stop.set_enabled(True)
        self.status.configure(text="Engine running — M4 optimized, English-only, 50KB per 1000 sites.")
        threading.Thread(target=self._reader, args=(self.proc,self.run_id), daemon=True).start()

    def _reader(self, proc, rid):
        for line in proc.stdout:
            self.msg_q.put((rid,line))
        self.msg_q.put((rid,"__EXIT__"))

    def stop_crawler(self, note: str=""):
        if not self.is_running: return
        self.is_running=False
        if self.proc:
            try: self.proc.terminate()
            except: pass
        self.proc=None
        self.led.itemconfig(self.led_dot, fill=RED)
        self.led.itemconfig(self.led_glow, outline=RED)
        self.badge.configure(text="STOPPED", bg="#ffe5e5", fg=RED)
        self.btn_start.set_enabled(True); self.btn_stop.set_enabled(False)
        self.status.configure(text=note or f"Stopped. {self.stat_in_db:,} sites saved ({fmt_bytes(self.db_bytes)}, {self.db_bytes/max(self.stat_in_db,1):.1f} B/site).")

    def on_quit(self):
        self.stop_crawler()
        self.write_q.put(None)
        deadline=time.time()+4
        while self._writer_alive and time.time()<deadline:
            time.sleep(0.05)
        self.destroy(); sys.exit(0)

    # ---------------------------------------------------- writer thread
    def _writer_loop(self):
        batch: List[Tuple[str,str]]=[]
        last=time.time()
        while True:
            try:
                item=self.write_q.get(timeout=0.3)
            except queue.Empty:
                item="tick"
            done=item is None
            if isinstance(item,tuple):
                batch.append(item)
            if batch and (done or len(batch)>=500 or time.time()-last>=0.4):
                try:
                    self.stat_new+=db.insert_sites_batch(batch)
                    self.stat_in_db=db.get_total_in_db()
                except Exception:
                    pass
                batch,last=[],time.time()
            if done:
                self._writer_alive=False
                return

    # ------------------------------------------------------ UI pump
    def _pump(self):
        try:
            for _ in range(200):
                self.ui_q.get_nowait()()
        except queue.Empty:
            pass
        n=0
        while n<3000:
            try:
                rid,line=self.msg_q.get_nowait()
            except queue.Empty:
                break
            n+=1
            if rid!=self.run_id: continue
            if line=="__EXIT__":
                if self.is_running:
                    self.stop_crawler("Engine exited.")
                break
            if line.startswith("RESULT\t"):
                parts=line.rstrip("\n").split("\t",2)
                if len(parts)>=2 and parts[1]:
                    title=parts[2] if len(parts)>2 else ""
                    self.write_q.put((parts[1],title))
                    self.feed.appendleft(f"{time.strftime('%H:%M:%S')}  {parts[1][:28]:<28} {title[:26]}")
                    self.feed_dirty=True
            elif line.startswith("STATS\t"):
                f=line.rstrip("\n").split("\t")
                try:
                    s=self.stats
                    s["found"],s["live"],s["queued"]=int(f[1]),int(f[2]),int(f[3])+int(f[4])
                    s["failed"],s["skipped"],s["probed"]=int(f[5]),int(f[6]),int(f[7])
                    s["nonen"]=int(f[8]) if len(f)>8 else 0
                except: pass
        self.after(50, self._pump)

    def _anim_loop(self):
        # LED pulse
        self.led_pulse+=0.08
        pulse=0.5+0.5*abs((self.led_pulse%6.28)-3.14)/3.14 if self.is_running else 0
        try:
            self.led.itemconfig(self.led_glow, width=int(pulse*4))
        except: pass
        # meters tick
        ticking=False
        for m in (self.m_speed,self.m_db,self.m_store):
            try:
                if m.tick(): ticking=True
            except: pass
        # rate anim
        self.rate_anim.tick()
        self.after(16, self._anim_loop)

    def _heartbeat(self):
        now=time.time()
        if self.is_running:
            dt=now-self._last_t
            if dt>=0.8:
                inst=(self.stats["found"]-self._last_found)/dt if dt>0 else 0
                self.rate=self.rate*0.6+inst*0.4 if self.rate else inst
                self.rate_anim.set(self.rate)
                self._last_found,self._last_t=self.stats["found"],now
                self.rate_hist.append(self.rate); self.rate_hist.pop(0)
                self._draw_graph()
        else:
            self.rate_anim.set(0)
        if now-self._last_size_poll>1.5:
            self._last_size_poll=now
            self.db_bytes=db.db_size_bytes()
            if self.is_running and self.db_bytes>=MAX_BYTES*0.98:
                self.stop_crawler("Stopped: database reached 10 GB budget.")

        if self.feed_dirty:
            self.feed_dirty=False
            self.feed_box.delete(0, tk.END)
            self.feed_box.insert(tk.END, *list(self.feed))

        s=self.stats
        # animated values
        cur_rate=self.rate_anim.current
        self.v_speed.configure(text=f"{cur_rate:,.0f} /s")
        self.m_speed.set(cur_rate/TARGET_RATE, GREEN if cur_rate>=TARGET_RATE else CYAN)
        self.l_speed.configure(text=f"{cur_rate/TARGET_RATE*100:.0f}% of 1,000 / s target • M4 optimized")

        n=self.stat_in_db
        self.v_db.configure(text=f"{n:,}")
        self.m_db.set(n/TARGET_SITES if TARGET_SITES else 0)
        self.l_db.configure(text=f"{n/TARGET_SITES*100:.3f}% of 100,000,000 • {fmt_bytes(self.db_bytes)} total")

        self.v_store.configure(text=fmt_bytes(self.db_bytes))
        self.m_store.set(self.db_bytes/MAX_BYTES, RED if self.db_bytes>MAX_BYTES*0.9 else ORANGE)
        if n>=100:
            per=self.db_bytes/max(n,1)
            proj=per*TARGET_SITES
            # Show 50KB per 1000 target
            status="✓ under 50KB/1000 target" if per<=50 else f"{per:.1f} B/site"
            self.l_store.configure(text=f"{per:.1f} B/site • {status} • ≈ {fmt_bytes(proj)} at 100M")
        else:
            self.l_store.configure(text="50KB per 1000 target • projection after 100 sites")

        eta="—"
        if self.rate>0 and n<TARGET_SITES:
            secs=int((TARGET_SITES-n)/self.rate)
            eta=str(timedelta(seconds=secs))
            if len(eta)>14: eta=eta.split(",")[0]
        up=str(timedelta(seconds=int(now-self.start_time))) if self.is_running else "0:00:00"
        for name,val in (("FOUND (session)",s["found"]),("NEW UNIQUE",self.stat_new),("PROBED",s["probed"]),
                         ("LIVE CONNS",s["live"]),("QUEUED",s["queued"]),("FAILED",s["failed"]),
                         ("SKIPPED",s["skipped"]),("NON-ENGLISH",s["nonen"])):
            try: self.small[name].configure(text=f"{val:,}")
            except: pass
        try:
            self.small["ETA TO 100M"].configure(text=eta)
            self.small["UPTIME"].configure(text=up)
        except: pass
        self.after(250, self._heartbeat)

def main():
    app=CrawlerApp()
    app.mainloop()

if __name__=="__main__":
    main()
