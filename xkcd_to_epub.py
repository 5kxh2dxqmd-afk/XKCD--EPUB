#!/usr/bin/env python3
"""
XKCD to EPUB Downloader
------------------------
A Tkinter GUI that downloads XKCD comics (via the official xkcd.com JSON
API) and packages them into a single EPUB file. Downloads run in a thread
pool for speed and the UI stays responsive throughout.

Dependencies:
    pip install requests --break-system-packages

Usage:
    python xkcd_to_epub.py
"""

import os
import queue
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from tkinter import (Tk, Toplevel, StringVar, IntVar, DoubleVar, filedialog, messagebox,
                      ttk, N, S, E, W, END, DISABLED, NORMAL)
from tkinter.scrolledtext import ScrolledText

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class Tooltip:
    """A small hover tooltip for any Tk widget."""

    def __init__(self, widget, text, wraplength=260):
        self.widget = widget
        self.text = text
        self.wraplength = wraplength
        self.tip = None
        widget.bind("<Enter>", self.show)
        widget.bind("<Leave>", self.hide)

    def show(self, _event=None):
        if self.tip or not self.text:
            return
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self.tip = tw = Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        label = ttk.Label(tw, text=self.text, justify="left", wraplength=self.wraplength,
                           background="#2b2b2b", foreground="#f0f0f0",
                           padding=(8, 5), font=("Segoe UI", 8))
        label.pack()

    def hide(self, _event=None):
        if self.tip:
            self.tip.destroy()
            self.tip = None

API_LATEST = "https://xkcd.com/info.0.json"
API_COMIC = "https://xkcd.com/{}/info.0.json"
USER_AGENT = "xkcd-to-epub-script/2.0 (personal use)"
DEFAULT_WORKERS = 8


def make_session(pool_size):
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    retry = Retry(total=3, backoff_factor=0.5,
                  status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry, pool_maxsize=max(pool_size, 4))
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def get_latest_num(session):
    r = session.get(API_LATEST, timeout=15)
    r.raise_for_status()
    return r.json()["num"]


def fetch_one(session, num):
    """Fetch metadata + image for a single comic number."""
    r = session.get(API_COMIC.format(num), timeout=15)
    if r.status_code == 404:
        return {"num": num, "skipped": True}
    r.raise_for_status()
    comic = r.json()

    img_url = comic.get("img", "")
    ext = img_url.rsplit(".", 1)[-1].lower()
    if ext not in ("png", "jpg", "jpeg", "gif"):
        ext = "png"

    img_resp = session.get(img_url, timeout=25)
    img_resp.raise_for_status()

    return {
        "num": comic["num"],
        "title": comic.get("title", f"Comic {comic['num']}"),
        "alt": comic.get("alt", ""),
        "img_name": f"{comic['num']}.{ext}",
        "img_bytes": img_resp.content,
        "ext": ext,
        "year": comic.get("year", ""),
        "month": comic.get("month", ""),
        "day": comic.get("day", ""),
        "skipped": False,
    }


# ---------------------------------------------------------------------------
# EPUB building (hand-rolled, no external epub library required)
# ---------------------------------------------------------------------------

CONTAINER_XML = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""

CHAPTER_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>{title}</title></head>
<body>
  <h1>#{num}: {title}</h1>
  <p><img src="images/{img_name}" alt="{alt_escaped}" /></p>
  <p><em>{alt_escaped}</em></p>
  <p>Date: {year}-{month}-{day}</p>
</body>
</html>
"""


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
                  .replace(">", "&gt;").replace('"', "&quot;"))


class EpubBuilder:
    def __init__(self, title="XKCD Archive", author="Randall Munroe"):
        self.title = title
        self.author = author
        self.comics = []

    def add_comic(self, c):
        self.comics.append(c)

    def save(self, path):
        self.comics.sort(key=lambda c: c["num"])

        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
            z.writestr("META-INF/container.xml", CONTAINER_XML)

            manifest_items, spine_items, nav_points = [], [], []

            for c in self.comics:
                chap_name = f"chap_{c['num']}.xhtml"
                html = CHAPTER_TEMPLATE.format(
                    title=esc(c["title"]), num=c["num"], img_name=c["img_name"],
                    alt_escaped=esc(c["alt"]), year=c["year"], month=c["month"], day=c["day"],
                )
                z.writestr(f"OEBPS/{chap_name}", html)
                z.writestr(f"OEBPS/images/{c['img_name']}", c["img_bytes"])

                media_type = ("image/png" if c["ext"] == "png"
                              else "image/jpeg" if c["ext"] in ("jpg", "jpeg")
                              else "image/gif")
                manifest_items.append(
                    f'<item id="chap{c["num"]}" href="{chap_name}" media-type="application/xhtml+xml"/>')
                manifest_items.append(
                    f'<item id="img{c["num"]}" href="images/{c["img_name"]}" media-type="{media_type}"/>')
                spine_items.append(f'<itemref idref="chap{c["num"]}"/>')
                nav_points.append(
                    f'<navPoint id="navPoint-{c["num"]}" playOrder="{c["num"]}">'
                    f'<navLabel><text>#{c["num"]}: {esc(c["title"])}</text></navLabel>'
                    f'<content src="{chap_name}"/></navPoint>')

            opf = f"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="BookId" version="2.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>{esc(self.title)}</dc:title>
    <dc:creator>{esc(self.author)}</dc:creator>
    <dc:identifier id="BookId">urn:uuid:xkcd-epub-archive</dc:identifier>
    <dc:language>en</dc:language>
  </metadata>
  <manifest>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
    {''.join(manifest_items)}
  </manifest>
  <spine toc="ncx">
    {''.join(spine_items)}
  </spine>
</package>
"""
            z.writestr("OEBPS/content.opf", opf)

            ncx = f"""<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head><meta name="dtb:uid" content="urn:uuid:xkcd-epub-archive"/></head>
  <docTitle><text>{esc(self.title)}</text></docTitle>
  <navMap>
    {''.join(nav_points)}
  </navMap>
</ncx>
"""
            z.writestr("OEBPS/toc.ncx", ncx)


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class App:
    PAD = 10

    def __init__(self, root):
        self.root = root
        root.title("XKCD → EPUB")
        root.geometry("580x520")
        root.minsize(520, 440)

        self._setup_style()

        self.status_q = queue.Queue()
        self.cancel_flag = threading.Event()
        self.start_time = None

        outer = ttk.Frame(root, padding=self.PAD, style="App.TFrame")
        outer.grid(sticky=(N, S, E, W))
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        outer.columnconfigure(0, weight=1)

        ttk.Label(outer, text="XKCD → EPUB", style="Title.TLabel").grid(
            row=0, column=0, sticky=W, pady=(0, 12))

        # --- range picker ---
        range_box = ttk.LabelFrame(outer, text="📚 Comic range", padding=self.PAD)
        range_box.grid(row=1, column=0, sticky=(E, W), pady=(0, 10))
        range_box.columnconfigure(1, weight=1)
        range_box.columnconfigure(3, weight=1)

        ttk.Label(range_box, text="From #").grid(row=0, column=0, sticky=W)
        self.start_var = IntVar(value=1)
        ttk.Entry(range_box, textvariable=self.start_var, width=8).grid(
            row=0, column=1, sticky=W, padx=(4, 16))

        ttk.Label(range_box, text="To #").grid(row=0, column=2, sticky=W)
        self.end_var = StringVar(value="")
        ttk.Entry(range_box, textvariable=self.end_var, width=8).grid(
            row=0, column=3, sticky=W, padx=(4, 8))
        ttk.Button(range_box, text="Use latest", command=self.fill_latest).grid(
            row=0, column=4, sticky=W)

        workers_label = ttk.Label(range_box, text="Parallel downloads  ⓘ", style="Info.TLabel")
        workers_label.grid(row=1, column=0, sticky=W, pady=(10, 0))
        Tooltip(workers_label,
                "How many comics the app fetches from xkcd.com at the same time.\n\n"
                "Each comic needs two web requests (its info + its image). Since "
                "waiting for a server reply is mostly idle time, doing several at "
                "once finishes the whole batch much faster than one-at-a-time.\n\n"
                "Higher = faster, but sends more requests to xkcd.com per second. "
                "8 is a good default; try lower (2-4) if you see errors, or higher "
                "(12-16) on a fast connection.")
        self.workers_var = IntVar(value=DEFAULT_WORKERS)
        workers_spin = ttk.Spinbox(range_box, from_=1, to=16, textvariable=self.workers_var, width=5)
        workers_spin.grid(row=1, column=1, sticky=W, pady=(10, 0))
        Tooltip(workers_spin, "Number of comics downloaded simultaneously (1-16).")
        ttk.Label(range_box, text="comics fetched at once", style="Hint.TLabel").grid(
            row=1, column=2, columnspan=3, sticky=W, pady=(10, 0), padx=(4, 0))

        # --- output ---
        out_box = ttk.LabelFrame(outer, text="💾 Output", padding=self.PAD)
        out_box.grid(row=2, column=0, sticky=(E, W), pady=(0, 10))
        out_box.columnconfigure(0, weight=1)

        self.out_var = StringVar(value=os.path.join(os.path.expanduser("~"), "xkcd_archive.epub"))
        ttk.Entry(out_box, textvariable=self.out_var).grid(row=0, column=0, sticky=(E, W))
        ttk.Button(out_box, text="Browse…", command=self.browse).grid(row=0, column=1, padx=(8, 0))

        # --- progress ---
        prog_box = ttk.Frame(outer)
        prog_box.grid(row=3, column=0, sticky=(E, W), pady=(0, 6))
        prog_box.columnconfigure(0, weight=1)

        self.progress_var = DoubleVar(value=0)
        self.progress = ttk.Progressbar(prog_box, orient="horizontal",
                                         variable=self.progress_var, maximum=100)
        self.progress.grid(row=0, column=0, sticky=(E, W))
        self.pct_label = ttk.Label(prog_box, text="0%", width=6, anchor=E)
        self.pct_label.grid(row=0, column=1, padx=(8, 0))

        self.status_var = StringVar(value="Ready.")
        ttk.Label(outer, textvariable=self.status_var, style="Status.TLabel").grid(
            row=4, column=0, sticky=W, pady=(2, 8))

        # --- log ---
        log_box = ttk.LabelFrame(outer, text="📜 Log", padding=6)
        log_box.grid(row=5, column=0, sticky=(N, S, E, W))
        outer.rowconfigure(5, weight=1)
        log_box.columnconfigure(0, weight=1)
        log_box.rowconfigure(0, weight=1)

        self.log = ScrolledText(log_box, height=10, state=DISABLED, wrap="word",
                                 background="#1e1e1e", foreground="#d4d4d4",
                                 insertbackground="#d4d4d4", relief="flat", borderwidth=0)
        self.log.grid(row=0, column=0, sticky=(N, S, E, W))

        # --- buttons ---
        btn_box = ttk.Frame(outer)
        btn_box.grid(row=6, column=0, sticky=(E, W), pady=(10, 0))
        btn_box.columnconfigure(0, weight=1)

        self.start_btn = ttk.Button(btn_box, text="Download and Build EPUB",
                                     style="Accent.TButton", command=self.start)
        self.start_btn.grid(row=0, column=0, sticky=W)
        self.cancel_btn = ttk.Button(btn_box, text="Cancel", command=self.cancel, state=DISABLED)
        self.cancel_btn.grid(row=0, column=1, padx=(8, 0))

        self.root.after(80, self.poll_queue)

    def _setup_style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        bg = "#f4f5f7"
        accent = "#2563eb"
        self.root.configure(background=bg)
        style.configure("App.TFrame", background=bg)
        style.configure("TLabelframe", background=bg)
        style.configure("TLabelframe.Label", background=bg, font=("Segoe UI", 9, "bold"))
        style.configure("TFrame", background=bg)
        style.configure("TLabel", background=bg, font=("Segoe UI", 9))
        style.configure("Title.TLabel", font=("Segoe UI", 15, "bold"), background=bg)
        style.configure("Status.TLabel", font=("Segoe UI", 9), foreground="#444", background=bg)
        style.configure("Info.TLabel", font=("Segoe UI", 9), foreground=accent, background=bg)
        style.configure("Hint.TLabel", font=("Segoe UI", 8), foreground="#888", background=bg)
        style.configure("TCheckbutton", background=bg)
        style.configure("Accent.TButton", font=("Segoe UI", 9, "bold"), padding=(10, 6))
        style.configure("TButton", padding=(8, 4))
        style.map("Accent.TButton", background=[("active", accent)])
        style.configure("TProgressbar", troughcolor="#e2e4e8", background=accent, thickness=14)

    def log_line(self, text):
        self.log.configure(state=NORMAL)
        self.log.insert(END, text + "\n")
        self.log.see(END)
        self.log.configure(state=DISABLED)

    def fill_latest(self):
        try:
            session = make_session(1)
            n = get_latest_num(session)
            self.end_var.set(str(n))
        except Exception as e:
            messagebox.showerror("Error", f"Could not fetch latest comic number:\n{e}")

    def browse(self):
        path = filedialog.asksaveasfilename(defaultextension=".epub",
                                             filetypes=[("EPUB files", "*.epub")],
                                             initialfile="xkcd_archive.epub")
        if path:
            self.out_var.set(path)

    def start(self):
        try:
            start_num = int(self.start_var.get())
        except (ValueError, TypeError):
            messagebox.showerror("Error", "Start number must be an integer.")
            return

        end_raw = self.end_var.get().strip()
        out_path = self.out_var.get().strip()
        if not out_path:
            messagebox.showerror("Error", "Please choose an output file path.")
            return

        try:
            workers = max(1, min(16, int(self.workers_var.get())))
        except (ValueError, TypeError):
            workers = DEFAULT_WORKERS

        self.cancel_flag.clear()
        self.start_btn.config(state=DISABLED)
        self.cancel_btn.config(state=NORMAL)
        self.progress_var.set(0)
        self.pct_label.config(text="0%")
        self.log.configure(state=NORMAL)
        self.log.delete("1.0", END)
        self.log.configure(state=DISABLED)
        self.status_var.set("Starting…")
        self.start_time = time.time()

        t = threading.Thread(target=self.worker, args=(start_num, end_raw, out_path, workers), daemon=True)
        t.start()

    def cancel(self):
        self.cancel_flag.set()
        self.status_var.set("Cancelling…")

    def worker(self, start_num, end_raw, out_path, workers):
        session = make_session(workers)
        try:
            if end_raw:
                end_num = int(end_raw)
            else:
                end_num = get_latest_num(session)

            nums = list(range(start_num, end_num + 1))
            total = max(1, len(nums))
            self.status_q.put(("log", f"Fetching comics #{start_num}-{end_num} "
                                       f"with {workers} parallel workers..."))

            builder = EpubBuilder()
            done = skipped = errors = 0

            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(fetch_one, session, n): n for n in nums}
                for fut in as_completed(futures):
                    if self.cancel_flag.is_set():
                        for f in futures:
                            f.cancel()
                        self.status_q.put(("log", "Cancelled by user."))
                        self.status_q.put(("finish", None))
                        return

                    num = futures[fut]
                    try:
                        result = fut.result()
                    except Exception as e:
                        self.status_q.put(("log", f"#{num}: error - {e}"))
                        errors += 1
                        done += 1
                        self.status_q.put(("progress", (done, total)))
                        continue

                    if result.get("skipped"):
                        skipped += 1
                    else:
                        builder.add_comic(result)
                        self.status_q.put(("log", f"#{result['num']}: {result['title']}"))

                    done += 1
                    self.status_q.put(("progress", (done, total)))

            if self.cancel_flag.is_set():
                self.status_q.put(("finish", None))
                return

            self.status_q.put(("status", f"Building EPUB ({len(builder.comics)} comics)..."))
            builder.save(out_path)
            elapsed = time.time() - self.start_time
            self.status_q.put(("log", f"Saved {len(builder.comics)} comics to {out_path} "
                                       f"({skipped} skipped, {errors} errors) in {elapsed:.1f}s"))
            self.status_q.put(("status", "Done."))
            self.status_q.put(("finish", None))

        except Exception as e:
            self.status_q.put(("log", f"Failed: {e}"))
            self.status_q.put(("status", "Failed."))
            self.status_q.put(("finish", None))

    def poll_queue(self):
        try:
            while True:
                kind, val = self.status_q.get_nowait()
                if kind == "progress":
                    done, total = val
                    pct = 100.0 * done / total
                    self.progress_var.set(pct)
                    self.pct_label.config(text=f"{pct:.0f}%")
                    self.status_var.set(f"{done}/{total} comics processed...")
                elif kind == "status":
                    self.status_var.set(val)
                elif kind == "log":
                    self.log_line(val)
                elif kind == "finish":
                    self.start_btn.config(state=NORMAL)
                    self.cancel_btn.config(state=DISABLED)
        except queue.Empty:
            pass
        self.root.after(80, self.poll_queue)


def main():
    root = Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
