"""Bilibili 다운로더 — 웹 버전 (LENNON 스튜디오 사내 툴)

기존 tkinter GUI(main.py)의 yt-dlp 호출 방식을 그대로 이식한 Flask 서버.
- 포트 4434, 0.0.0.0 바인딩
- 도메인 화이트리스트: bilibili.com / b23.tv / bilibili.tv
- 서버 저장 위치: ./downloads (파일명 안전화, 경로 탐색 차단)
- 동시 다운로드 2개 제한 (워커 스레드 2개 + 작업 큐)
"""
import os
import queue
import re
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, abort, jsonify, request, send_from_directory

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

PORT = int(os.environ.get("PORT") or 4434)
# 다운로드 보존 시간 — 지나면 자동 삭제한다(0 이면 정리 안 함).
# 서버에 파일이 무한정 쌓이는 것을 막는다. 컨테이너 배포 시 특히 중요.
DOWNLOAD_TTL_HOURS = float(os.environ.get("BILI_DOWNLOAD_TTL_HOURS") or 24)
MAX_WORKERS = 2
MAX_LOG_LINES = 400

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# main.py와 동일한 User-Agent / 헤더
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
HTTP_HEADERS = {
    "User-Agent": USER_AGENT,
    "Referer": "https://www.bilibili.com/",
    "Origin": "https://www.bilibili.com",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,ko;q=0.7",
}

ALLOWED_DOMAINS = ("bilibili.com", "b23.tv", "bilibili.tv")

QUALITY_LABELS = {
    "best": "최고화질",
    "1080p": "1080p",
    "720p": "720p",
    "audio": "음성만(MP3)",
}

app = Flask(__name__, static_folder=None)

jobs = {}  # job_id -> dict
jobs_order = []  # 생성 순서
jobs_lock = threading.Lock()
job_queue = queue.Queue()


# ---------- 유틸 ----------
def is_allowed_url(url: str) -> bool:
    if "://" not in url:
        url = "https://" + url
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in ALLOWED_DOMAINS)


def job_log(job: dict, msg: str):
    msg = ANSI_RE.sub("", str(msg)).rstrip()
    if not msg:
        return
    with jobs_lock:
        job["log"].append(msg)
        if len(job["log"]) > MAX_LOG_LINES:
            del job["log"][: len(job["log"]) - MAX_LOG_LINES]


class _YdlLogger:
    """main.py의 _YdlLogger와 동일한 동작 (job 로그로 전달)."""

    def __init__(self, job):
        self.job = job

    def debug(self, msg):
        if str(msg).startswith("[debug]"):
            return
        job_log(self.job, msg)

    def info(self, msg):
        job_log(self.job, msg)

    def warning(self, msg):
        job_log(self.job, f"[경고] {msg}")

    def error(self, msg):
        job_log(self.job, f"[오류] {msg}")


def make_progress_hook(job):
    def hook(d):
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes", 0)
            speed = d.get("speed") or 0
            eta = d.get("eta")
            with jobs_lock:
                if total:
                    job["pct"] = round(downloaded / total * 100, 1)
                job["speed"] = f"{speed / 1024 / 1024:.1f} MB/s" if speed else "-"
                job["eta"] = f"{eta}s" if eta is not None else "-"
        elif status == "finished":
            with jobs_lock:
                job["pct"] = 100.0
                job["speed"] = "-"
                job["eta"] = "-"
            job_log(job, "다운로드 완료 — 후처리(병합/변환) 중...")

    return hook


def build_ydl_opts(job: dict) -> dict:
    """main.py의 _build_ydl_opts를 웹 서버용으로 이식.
    outtmpl에 job prefix를 붙여 동시 작업 간 파일 추적을 분리한다."""
    quality = job["quality"]

    if quality == "audio":
        opts = {
            "format": "bestaudio/best",
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
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
        opts = {"format": fmt_str, "merge_output_format": "mp4"}

    opts.update(
        {
            "outtmpl": str(DOWNLOAD_DIR / f"{job['prefix']}%(title)s [%(id)s].%(ext)s"),
            "progress_hooks": [make_progress_hook(job)],
            "noprogress": True,
            "quiet": True,
            "no_warnings": False,
            "no_color": True,
            "noplaylist": True,
            "restrictfilenames": False,
            "windowsfilenames": True,
            "retries": 5,
            "fragment_retries": 5,
            "http_headers": dict(HTTP_HEADERS),
            "logger": _YdlLogger(job),
        }
    )
    return opts


def collect_job_files(job: dict):
    """job prefix로 시작하는 완성 파일 수집 (임시 파일 제외)."""
    skip_ext = (".part", ".ytdl", ".temp", ".tmp")
    names = []
    for p in sorted(DOWNLOAD_DIR.glob(job["prefix"] + "*")):
        if p.is_file() and not p.name.lower().endswith(skip_ext):
            names.append(p.name)
    with jobs_lock:
        job["files"] = names


# ---------- 워커 ----------
def worker_loop():
    while True:
        job_id = job_queue.get()
        with jobs_lock:
            job = jobs.get(job_id)
        if job is None:
            job_queue.task_done()
            continue
        with jobs_lock:
            job["status"] = "running"
            job["started_at"] = time.time()
        job_log(job, f"시작: {job['url']}")
        try:
            opts = build_ydl_opts(job)
            with YoutubeDL(opts) as ydl:
                if job.get("simulate"):
                    info = ydl.extract_info(job["url"], download=False)
                    title = (info or {}).get("title", "?")
                    dur = (info or {}).get("duration")
                    job_log(job, f"[시뮬레이션] 제목: {title} / 길이: {dur}s")
                else:
                    ydl.download([job["url"]])
            collect_job_files(job)
            with jobs_lock:
                job["status"] = "done"
                job["pct"] = 100.0
            job_log(job, "완료")
        except DownloadError as e:
            with jobs_lock:
                job["status"] = "error"
            job_log(job, f"실패: {e}")
        except Exception as e:  # noqa: BLE001
            with jobs_lock:
                job["status"] = "error"
            job_log(job, f"예외: {e}")
        finally:
            job_queue.task_done()


def cleanup_loop():
    """TTL 이 지난 다운로드 파일을 주기적으로 지운다(1시간마다 · 기동 직후 1회).

    잡 목록(jobs)은 메모리라 재기동 시 사라지므로, 파일 수명만 보면 된다.
    삭제 실패(다운로드 중 잠김 등)는 조용히 넘기고 다음 주기에 다시 시도한다.
    """
    while True:
        if DOWNLOAD_TTL_HOURS > 0:
            cutoff = time.time() - DOWNLOAD_TTL_HOURS * 3600
            for f in DOWNLOAD_DIR.glob("*"):
                try:
                    if f.is_file() and f.stat().st_mtime < cutoff:
                        f.unlink()
                except OSError:
                    pass
        time.sleep(3600)


for _ in range(MAX_WORKERS):
    threading.Thread(target=worker_loop, daemon=True).start()
threading.Thread(target=cleanup_loop, daemon=True).start()


# ---------- 라우트 ----------
@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.route("/api/download", methods=["POST"])
def api_download():
    data = request.get_json(silent=True) or {}
    raw = data.get("urls", "")
    if isinstance(raw, list):
        lines = [str(x) for x in raw]
    else:
        lines = str(raw).splitlines()
    quality = str(data.get("quality", "best"))
    if quality not in QUALITY_LABELS:
        quality = "best"
    simulate = bool(data.get("simulate", False))

    accepted, rejected = [], []
    for line in lines:
        url = line.strip()
        if not url:
            continue
        if not is_allowed_url(url):
            rejected.append(
                {
                    "url": url,
                    "reason": "허용되지 않은 도메인입니다. (bilibili.com / b23.tv / bilibili.tv 만 가능)",
                }
            )
            continue
        job_id = uuid.uuid4().hex[:12]
        job = {
            "id": job_id,
            "prefix": job_id + "__",
            "url": url,
            "quality": quality,
            "simulate": simulate,
            "status": "queued",
            "pct": 0.0,
            "speed": "-",
            "eta": "-",
            "log": [],
            "files": [],
            "created_at": time.time(),
        }
        with jobs_lock:
            jobs[job_id] = job
            jobs_order.append(job_id)
        job_queue.put(job_id)
        accepted.append(job_id)

    return jsonify({"accepted": accepted, "rejected": rejected})


@app.route("/api/progress")
def api_progress():
    out = []
    with jobs_lock:
        for job_id in jobs_order:
            j = jobs[job_id]
            out.append(
                {
                    "id": j["id"],
                    "url": j["url"],
                    "quality": QUALITY_LABELS.get(j["quality"], j["quality"]),
                    "simulate": j["simulate"],
                    "status": j["status"],
                    "pct": j["pct"],
                    "speed": j["speed"],
                    "eta": j["eta"],
                    "log": j["log"][-40:],
                    "files": [
                        {
                            "name": n,
                            "label": n.split("__", 1)[1] if "__" in n else n,
                        }
                        for n in j["files"]
                    ],
                }
            )
    return jsonify({"jobs": out, "queue_size": job_queue.qsize()})


@app.route("/files/<path:name>")
def serve_file(name):
    # 경로 탐색 차단: downloads 바로 아래 파일만, 구분자/상위 이동 금지
    if "/" in name or "\\" in name or ".." in name or name.startswith("."):
        abort(400)
    target = (DOWNLOAD_DIR / name).resolve()
    if target.parent != DOWNLOAD_DIR.resolve() or not target.is_file():
        abort(404)
    return send_from_directory(
        DOWNLOAD_DIR,
        name,
        as_attachment=True,
        download_name=name.split("__", 1)[1] if "__" in name else name,
    )


@app.route("/api/health")
def api_health():
    import shutil as _sh

    import yt_dlp as _y

    return jsonify(
        {
            "ok": True,
            "yt_dlp": _y.version.__version__,
            "ffmpeg": bool(_sh.which("ffmpeg")),
            "workers": MAX_WORKERS,
            "download_ttl_hours": DOWNLOAD_TTL_HOURS,
        }
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, threaded=True)
