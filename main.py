"""Bilibili 다운로더 - yt-dlp 기반 GUI"""
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


def _setup_bundled_ffmpeg():
    """PyInstaller로 빌드된 exe라면 번들된 ffmpeg/ffprobe를 PATH 앞에 추가한다."""
    if getattr(sys, "frozen", False):
        bundle_dir = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    else:
        bundle_dir = os.path.dirname(os.path.abspath(__file__))

    if os.path.isfile(os.path.join(bundle_dir, "ffmpeg.exe")):
        os.environ["PATH"] = bundle_dir + os.pathsep + os.environ.get("PATH", "")


_setup_bundled_ffmpeg()

try:
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError
except ImportError:
    YoutubeDL = None
    DownloadError = Exception


class BilibiliDownloader(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Bilibili 다운로더")
        self.geometry("760x600")
        self.minsize(640, 500)

        self.cancel_flag = threading.Event()
        self.download_thread = None
        self.log_queue = queue.Queue()

        self._build_ui()
        self._poll_log_queue()
        self._check_dependencies()

    # ---------- UI ----------
    def _build_ui(self):
        pad = {"padx": 10, "pady": 6}

        url_frame = ttk.Frame(self)
        url_frame.pack(fill="x", **pad)
        ttk.Label(url_frame, text="URL").pack(side="left")
        self.url_var = tk.StringVar()
        ttk.Entry(url_frame, textvariable=self.url_var).pack(
            side="left", fill="x", expand=True, padx=6
        )
        ttk.Button(url_frame, text="붙여넣기", command=self._paste_url).pack(side="left")
        ttk.Button(url_frame, text="지우기", command=lambda: self.url_var.set("")).pack(
            side="left", padx=(4, 0)
        )

        path_frame = ttk.Frame(self)
        path_frame.pack(fill="x", **pad)
        ttk.Label(path_frame, text="저장 위치").pack(side="left")
        default_dir = str(Path.home() / "Downloads" / "Bilibili")
        self.path_var = tk.StringVar(value=default_dir)
        ttk.Entry(path_frame, textvariable=self.path_var).pack(
            side="left", fill="x", expand=True, padx=6
        )
        ttk.Button(path_frame, text="찾아보기", command=self._browse).pack(side="left")

        opts_frame = ttk.LabelFrame(self, text="옵션")
        opts_frame.pack(fill="x", **pad)

        row1 = ttk.Frame(opts_frame)
        row1.pack(fill="x", padx=8, pady=4)
        ttk.Label(row1, text="화질").pack(side="left")
        self.quality_var = tk.StringVar(value="best")
        ttk.Combobox(
            row1,
            textvariable=self.quality_var,
            values=["best", "1080p", "720p", "480p", "360p", "audio only"],
            state="readonly",
            width=12,
        ).pack(side="left", padx=6)

        ttk.Label(row1, text="형식").pack(side="left", padx=(12, 0))
        self.format_var = tk.StringVar(value="mp4")
        ttk.Combobox(
            row1,
            textvariable=self.format_var,
            values=["mp4", "mkv", "webm", "mp3", "m4a"],
            state="readonly",
            width=8,
        ).pack(side="left", padx=6)

        self.subs_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row1, text="자막 포함", variable=self.subs_var).pack(
            side="left", padx=(12, 0)
        )
        self.playlist_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row1, text="재생목록 전체", variable=self.playlist_var).pack(
            side="left", padx=(12, 0)
        )

        row2 = ttk.Frame(opts_frame)
        row2.pack(fill="x", padx=8, pady=(0, 4))
        ttk.Label(row2, text="브라우저 쿠키 사용 (HTTP 412 대응 / 1080p+)").pack(side="left")
        self.browser_var = tk.StringVar(value="none")
        ttk.Combobox(
            row2,
            textvariable=self.browser_var,
            values=["none", "edge", "chrome", "firefox", "brave", "vivaldi", "opera"],
            state="readonly",
            width=10,
        ).pack(side="left", padx=6)

        row3 = ttk.Frame(opts_frame)
        row3.pack(fill="x", padx=8, pady=(0, 6))
        ttk.Label(row3, text="쿠키 파일 (선택, 위 옵션 대신)").pack(side="left")
        self.cookies_var = tk.StringVar()
        ttk.Entry(row3, textvariable=self.cookies_var).pack(
            side="left", fill="x", expand=True, padx=6
        )
        ttk.Button(row3, text="...", width=3, command=self._browse_cookies).pack(side="left")

        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill="x", **pad)
        self.download_btn = ttk.Button(btn_frame, text="다운로드", command=self._start_download)
        self.download_btn.pack(side="left")
        self.cancel_btn = ttk.Button(
            btn_frame, text="취소", command=self._cancel_download, state="disabled"
        )
        self.cancel_btn.pack(side="left", padx=6)
        ttk.Button(btn_frame, text="폴더 열기", command=self._open_folder).pack(side="right")

        self.progress_var = tk.DoubleVar()
        self.progress = ttk.Progressbar(self, variable=self.progress_var, maximum=100)
        self.progress.pack(fill="x", **pad)

        self.status_var = tk.StringVar(value="대기 중")
        ttk.Label(self, textvariable=self.status_var, anchor="w").pack(fill="x", padx=10)

        log_frame = ttk.LabelFrame(self, text="로그")
        log_frame.pack(fill="both", expand=True, **pad)
        self.log_text = scrolledtext.ScrolledText(log_frame, height=14, wrap="word")
        self.log_text.pack(fill="both", expand=True)

    # ---------- Helpers ----------
    def _check_dependencies(self):
        if YoutubeDL is None:
            self._log("⚠ yt-dlp 미설치. 터미널에서 'pip install yt-dlp' 실행 후 재시작하세요.")
            self.download_btn.config(state="disabled")
        if shutil.which("ffmpeg") is None:
            self._log(
                "⚠ ffmpeg 미설치. 영상+오디오 병합/포맷 변환에 필요합니다. "
                "https://www.gyan.dev/ffmpeg/builds/ 에서 받아 PATH에 추가하세요."
            )

    def _paste_url(self):
        try:
            self.url_var.set(self.clipboard_get().strip())
        except tk.TclError:
            pass

    def _browse(self):
        path = filedialog.askdirectory(initialdir=self.path_var.get() or str(Path.home()))
        if path:
            self.path_var.set(path)

    def _browse_cookies(self):
        path = filedialog.askopenfilename(
            filetypes=[("Cookies (Netscape)", "*.txt"), ("All files", "*.*")]
        )
        if path:
            self.cookies_var.set(path)

    def _open_folder(self):
        path = self.path_var.get()
        if os.path.isdir(path):
            os.startfile(path)
        else:
            messagebox.showinfo("알림", "폴더가 아직 존재하지 않습니다.")

    def _log(self, msg):
        self.log_queue.put(ANSI_RE.sub("", str(msg)))

    def _poll_log_queue(self):
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self.log_text.insert("end", msg + "\n")
                self.log_text.see("end")
        except queue.Empty:
            pass
        self.after(100, self._poll_log_queue)

    # ---------- Download ----------
    def _build_ydl_opts(self):
        out_dir = self.path_var.get().strip() or str(Path.home() / "Downloads" / "Bilibili")
        os.makedirs(out_dir, exist_ok=True)

        quality = self.quality_var.get()
        fmt = self.format_var.get()
        audio_only = quality == "audio only" or fmt in ("mp3", "m4a")

        if audio_only:
            audio_codec = fmt if fmt in ("mp3", "m4a") else "mp3"
            opts = {
                "format": "bestaudio/best",
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": audio_codec,
                        "preferredquality": "192",
                    }
                ],
            }
        else:
            if quality == "best":
                fmt_str = "bestvideo+bestaudio/best"
            else:
                h = quality.replace("p", "")
                fmt_str = f"bestvideo[height<={h}]+bestaudio/best[height<={h}]"
            opts = {"format": fmt_str, "merge_output_format": fmt}

        opts.update(
            {
                "outtmpl": os.path.join(out_dir, "%(title)s [%(id)s].%(ext)s"),
                "progress_hooks": [self._progress_hook],
                "noprogress": True,
                "quiet": True,
                "no_warnings": False,
                "no_color": True,
                "noplaylist": not self.playlist_var.get(),
                "writesubtitles": self.subs_var.get(),
                "writeautomaticsub": self.subs_var.get(),
                "subtitleslangs": ["zh-CN", "zh", "en", "ko"],
                "embedsubtitles": self.subs_var.get(),
                "retries": 5,
                "fragment_retries": 5,
                "http_headers": {
                    "User-Agent": USER_AGENT,
                    "Referer": "https://www.bilibili.com/",
                    "Origin": "https://www.bilibili.com",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,ko;q=0.7",
                },
            }
        )

        cookies = self.cookies_var.get().strip()
        if cookies and os.path.isfile(cookies):
            opts["cookiefile"] = cookies
        else:
            browser = self.browser_var.get()
            if browser and browser != "none":
                opts["cookiesfrombrowser"] = (browser,)

        return opts

    def _progress_hook(self, d):
        if self.cancel_flag.is_set():
            raise DownloadError("사용자 취소")

        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes", 0)
            speed = d.get("speed") or 0
            eta = d.get("eta")

            if total:
                pct = downloaded / total * 100
                self.progress_var.set(pct)
                speed_str = f"{speed / 1024 / 1024:.1f} MB/s" if speed else "-"
                eta_str = f"{eta}s" if eta is not None else "-"
                self.status_var.set(
                    f"다운로드 {pct:.1f}% | {speed_str} | ETA {eta_str}"
                )
        elif status == "finished":
            self.status_var.set("후처리(병합/변환) 중...")
            self.progress_var.set(100)

    def _start_download(self):
        if YoutubeDL is None:
            messagebox.showerror(
                "오류",
                "yt-dlp가 설치되어 있지 않습니다.\n터미널에서 'pip install yt-dlp' 실행 후 재시작하세요.",
            )
            return

        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("입력 필요", "URL을 입력하세요.")
            return

        self.cancel_flag.clear()
        self.download_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self.progress_var.set(0)
        self.status_var.set("준비 중...")
        self._log(f"▶ 시작: {url}")

        self.download_thread = threading.Thread(
            target=self._download_worker, args=(url,), daemon=True
        )
        self.download_thread.start()

    def _download_worker(self, url):
        def _try_download(opts):
            opts["logger"] = _YdlLogger(self._log)
            with YoutubeDL(opts) as ydl:
                ydl.download([url])

        try:
            opts = self._build_ydl_opts()
            try:
                _try_download(opts)
            except Exception as e:
                msg = str(e)
                cookie_failed = (
                    "cookiesfrombrowser" in opts
                    and (
                        "Could not copy" in msg
                        or "cookie database" in msg.lower()
                        or "Permission denied" in msg
                    )
                )
                if cookie_failed and not self.cancel_flag.is_set():
                    self._log(
                        "⚠ 브라우저 쿠키 추출 실패 (브라우저가 실행 중일 수 있음). "
                        "쿠키 없이 재시도합니다..."
                    )
                    opts.pop("cookiesfrombrowser", None)
                    _try_download(opts)
                else:
                    raise

            if not self.cancel_flag.is_set():
                self._log("✓ 완료")
                self.status_var.set("완료")
        except DownloadError as e:
            if self.cancel_flag.is_set():
                self._log("✗ 취소됨")
                self.status_var.set("취소됨")
            else:
                self._log(f"✗ 실패: {e}")
                self.status_var.set("실패")
        except Exception as e:
            self._log(f"✗ 예외: {e}")
            self.status_var.set("실패")
        finally:
            self.after(0, lambda: self.download_btn.config(state="normal"))
            self.after(0, lambda: self.cancel_btn.config(state="disabled"))

    def _cancel_download(self):
        self.cancel_flag.set()
        self._log("취소 요청...")


class _YdlLogger:
    def __init__(self, log_fn):
        self.log_fn = log_fn

    def debug(self, msg):
        if msg.startswith("[debug]"):
            return
        self.log_fn(msg)

    def info(self, msg):
        self.log_fn(msg)

    def warning(self, msg):
        self.log_fn(f"⚠ {msg}")

    def error(self, msg):
        self.log_fn(f"✗ {msg}")


if __name__ == "__main__":
    BilibiliDownloader().mainloop()
