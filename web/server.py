"""Bilibili 다운로더 — 웹 버전 (LENNON 스튜디오 사내 툴)

기존 tkinter GUI(main.py)의 yt-dlp 호출 방식을 그대로 이식한 Flask 서버.
- 포트 4434(PORT), 바인드 주소는 HOST(기본 0.0.0.0 — 컨테이너 기준)
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


# 무시된 env 설정 기록. 예전에는 숫자 아닌 값이 오면 import 가 터져 컨테이너가 죽었지만
# (fail-fast) 지금은 기본값으로 뜬다. Dockerfile 의 HEALTHCHECK 는 /api/health 200 만 보므로
# 오설정 컨테이너가 healthy 로 계속 돈다 — 그래서 이 목록을 health 응답에도 함께 싣는다.
ENV_WARNINGS = []


def read_num_env(primary: str, alias: str, default: float):
    """숫자 env 를 이름 두 가지로 받는다. (값, 적용된 출처) 를 돌려준다.

    실제로 읽던 이름은 BILI_ 접두사인데 상수명·문서는 접두사가 없어서, 접두사를 빠뜨리면
    경고 한 줄 없이 기본값으로 돌던 함정이 있었다. 둘 다 받되 기존 이름(BILI_*)을 우선한다 —
    이미 BILI_* 로 돌던 배포(Dockerfile·compose)의 동작을 그대로 지키기 위해서다.
    """
    for name in (primary, alias):
        raw = os.environ.get(name)
        if raw is None or not raw.strip():
            continue
        try:
            return float(raw), name
        except ValueError:
            # 숫자가 아니면 조용히 삼키지 말고 어느 이름이 틀렸는지 알린다.
            warn = f"{name}={raw!r} 는 숫자가 아니라 무시하고 기본값({default})으로 돕니다."
            ENV_WARNINGS.append(warn)
            print(f"[bili] {warn}", flush=True)
    return float(default), "기본값"


PORT = int(os.environ.get("PORT") or 4434)
# 바인드 주소 — 기본값은 기존 동작(0.0.0.0) 유지. 컨테이너 안에서는 0.0.0.0 이 정상이고,
# 사내 PC 에서 띄울 때는 HOST=127.0.0.1 로 loopback 전용으로 좁힐 수 있다(이 API 는 무인증).
HOST = os.environ.get("HOST") or "0.0.0.0"
# 다운로드 보존 시간 — 지나면 자동 삭제한다(0 이면 정리 안 함).
# 서버에 파일이 무한정 쌓이는 것을 막는다. 컨테이너 배포 시 특히 중요.
DOWNLOAD_TTL_HOURS, TTL_ENV_SOURCE = read_num_env(
    "BILI_DOWNLOAD_TTL_HOURS", "DOWNLOAD_TTL_HOURS", 24
)
# 다운로드 총량 상한(GB) — 넘으면 오래된 것부터 지운다(0 이면 상한 없음).
# TTL 은 '체류 시간'이지 '총량'이 아니다. 24시간 안에도 대용량이 몰리면 디스크가 찬다.
# VPS 는 디스크를 원장(사용량·생성물·잡)과 공유하므로, 차면 기록이 함께 멈춘다.
DOWNLOAD_MAX_GB, MAX_GB_ENV_SOURCE = read_num_env(
    "BILI_DOWNLOAD_MAX_GB", "DOWNLOAD_MAX_GB", 5
)
MAX_WORKERS = 2
MAX_LOG_LINES = 400
# 목록에 남기는 **끝난** 잡의 상한 — 대기·진행 중인 잡은 이 상한에 넣지 않는다(prune_jobs 참조).
# jobs/jobs_order 에 append 만 있어 재기동 전까지 무한히 쌓였고, /api/progress 가 매번
# 전체를 잡당 로그 40줄과 함께 돌려주므로 응답 크기도 같이 커졌다(상시 기동 VPS 에서 특히).
MAX_JOBS = 100

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
            # 실제 산출 해상도 — 요청 화질보다 낮게 떨어져도 사용자가 알 수 있게 여기서 챙긴다.
            # (영상 스트림에만 height 가 있다. 음성 스트림은 None 이므로 큰 값만 남긴다.)
            info = d.get("info_dict") or {}
            height, width = info.get("height"), info.get("width")
            with jobs_lock:
                job["pct"] = 100.0
                job["speed"] = "-"
                job["eta"] = "-"
                if height and height > (job.get("height") or 0):
                    job["height"], job["width"] = height, width
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


def note_actual_quality(job: dict):
    """실제 해상도를 잡에 남기고, 요청 화질보다 낮으면 그 사실을 로그로 알린다.

    720p 를 요청해도 그 영상의 상위 화질이 프리미엄 전용이면 무료 화질로 떨어진다
    (build_ydl_opts 의 `/best[height<=N]` 폴백이 의도대로 동작한 결과다). 지금까지는
    요청 라벨만 돌려줘서 480x480 을 받고도 720p 를 받은 줄 알았다.
    """
    with jobs_lock:
        height, width = job.get("height"), job.get("width")
        if width and height:
            job["resolution"] = f"{width}x{height}"
        elif height:
            job["resolution"] = f"{height}p"
        else:
            job["resolution"] = ""
        resolution, quality = job["resolution"], job["quality"]
    if not height or not quality.endswith("p"):
        return
    try:
        wanted = int(quality[:-1])
    except ValueError:
        return
    if height < wanted:
        job_log(
            job,
            f"[알림] 요청 화질 {quality} 보다 낮은 {resolution} 으로 받았습니다 — "
            "이 영상은 상위 화질이 제공되지 않거나 프리미엄 전용입니다.",
        )


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
            note_actual_quality(job)
            # 주기 청소(1시간)만으로는 그 사이 몰린 대용량을 못 막는다 — 받자마자 한 번 본다.
            try:
                freed = enforce_size_cap()
                if freed:
                    job_log(job, f"[정리] 총량 상한({DOWNLOAD_MAX_GB}GB) 초과 — 오래된 파일 {freed / 1048576:.0f}MB 삭제")
            except Exception:
                pass
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


def enforce_size_cap(directory=None, max_gb=None):
    """총량이 상한을 넘으면 **오래된 파일부터** 지운다. 지운 바이트 수를 돌려준다.

    TTL 청소 뒤에 한 번 더 거는 그물이다. 삭제 실패(다운로드 중 잠김 등)는 건너뛰고
    다음 기회에 다시 시도한다 — 한 건이 걸려도 나머지는 계속 지운다.
    """
    d = DOWNLOAD_DIR if directory is None else Path(directory)
    cap_gb = DOWNLOAD_MAX_GB if max_gb is None else max_gb
    if cap_gb <= 0:
        return 0
    cap = cap_gb * 1024 ** 3
    files = []
    for f in d.glob("*"):
        try:
            if f.is_file():
                st = f.stat()
                files.append((st.st_mtime, st.st_size, f))
        except OSError:
            pass
    total = sum(size for _, size, _ in files)
    if total <= cap:
        return 0
    files.sort(key=lambda t: t[0])          # 오래된 것부터
    freed = 0
    for _, size, f in files:
        if total - freed <= cap:
            break
        try:
            f.unlink()
            freed += size
        except OSError:
            pass
    return freed


def prune_jobs():
    """죽은 링크를 지우고 잡 목록에 상한을 건다.

    TTL·총량 상한은 **파일만** 지우므로 잡은 status=done 인 채 남아 404 나는 다운로드
    링크를 계속 광고했다. 파일이 사라진 잡은 expired 로 바꿔 '왜 안 열리는지'를 말해 준다.

    상한은 **끝난 잡에만** 건다. 대기·진행 중인 잡까지 상한 계산에 넣으면 '대기가 많을수록
    방금 끝난 잡이 먼저 밀려나서', 아직 안 받은 산출물의 유일한 링크가 배치 도중에 사라진다
    (파일 목록을 보여주는 라우트가 없어 이름을 모르면 되찾을 방법이 없다).
    버리는 순서는 **생성 순서(오래된 것 먼저)** 다. 예전엔 '회수할 게 없는 잡 먼저' 로 골랐는데,
    완료 잡이 상한을 채운 상태에서 방금 실패하거나 방금 만료된 잡이 (파일이 없다는 이유로)
    1순위 희생자가 되어, 사용자가 실패 사유도 만료 뱃지도 한 번을 못 보고 사라졌다.
    파일이 남아 있는 잡을 버릴 때만 그 사실을 서버 로그로 남긴다.
    """
    try:
        existing = {p.name for p in DOWNLOAD_DIR.iterdir() if p.is_file()}
    except OSError:
        return
    dropped_with_files = []
    with jobs_lock:
        for job_id in jobs_order:
            job = jobs[job_id]
            if job["status"] != "done" or not job["files"]:
                continue
            alive = [n for n in job["files"] if n in existing]
            if len(alive) == len(job["files"]):
                continue
            job["files"] = alive
            if not alive:
                job["status"] = "expired"
                # job_log 는 같은 잠금을 다시 잡는다(비재진입) — 여기선 직접 넣는다.
                job["log"].append(
                    "[정리] 보존 기간이 지나 서버에서 삭제된 파일입니다 — 필요하면 다시 받으세요."
                )
        finished = [
            jid for jid in jobs_order
            if jobs[jid]["status"] in ("done", "error", "expired")
        ]
        excess = len(finished) - MAX_JOBS       # 대기·진행 중인 잡은 상한에서 뺀다
        if excess > 0:
            # jobs_order 가 생성 순서라 finished 도 오래된 순 — 오래된 것부터 버린다.
            # '파일 없는 잡 먼저' 로 고르면 방금 실패한 잡이 즉시 사라져 사용자가
            # 실패 사유를 볼 수 없다(만료 뱃지도 마찬가지). 나이 순만이 안전하다.
            for jid in finished[:excess]:
                if jobs[jid]["files"]:
                    dropped_with_files.append((jid, list(jobs[jid]["files"])))
                jobs.pop(jid, None)
                jobs_order.remove(jid)
    # 링크 유실은 조용히 넘기지 않는다 — 파일 자체는 남아 있으니 이름을 알면 아직 받을 수 있다.
    for jid, names in dropped_with_files:
        print(
            f"[bili] 끝난 잡 {MAX_JOBS}건 상한 초과 — 완료 잡 {jid} 을 목록에서 내렸습니다"
            f" (파일은 남아 있음: {', '.join(names)} → /files/<이름>)",
            flush=True,
        )


def cleanup_loop():
    """TTL 이 지난 다운로드 파일을 주기적으로 지운다(1시간마다 · 기동 직후 1회).

    잡 목록(jobs)은 메모리라 재기동 시 사라지지만, 상시 기동(VPS)에서는 재기동이 없으므로
    파일 청소 뒤에 잡 목록도 함께 정리한다(prune_jobs).
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
        enforce_size_cap()
        prune_jobs()
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
            "width": None,
            "height": None,
            "resolution": "",
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
    prune_jobs()  # 파일이 지워진 잡은 만료 표시, 끝난 잡은 상한까지만 — 목록이 영원히 자라지 않게.
    out = []
    with jobs_lock:
        for job_id in jobs_order:
            j = jobs[job_id]
            out.append(
                {
                    "id": j["id"],
                    "url": j["url"],
                    "quality": QUALITY_LABELS.get(j["quality"], j["quality"]),
                    "resolution": j.get("resolution", ""),
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
            "download_max_gb": DOWNLOAD_MAX_GB,
            "max_jobs": MAX_JOBS,
            # 어떤 env 이름이 실제로 먹었는지 · 무시된 설정이 있는지 — 로그를 못 봐도 여기서 보인다.
            "config_source": {"ttl": TTL_ENV_SOURCE, "max_gb": MAX_GB_ENV_SOURCE},
            "env_warnings": ENV_WARNINGS,
        }
    )


if __name__ == "__main__":
    # 어떤 env 이름이 실제로 먹었는지 기동 시 알린다 — 이름을 틀려도 조용히 기본값으로 돌던 문제.
    print(
        f"[bili] bind={HOST}:{PORT} | TTL={DOWNLOAD_TTL_HOURS}h (출처 {TTL_ENV_SOURCE})"
        f" | 총량상한={DOWNLOAD_MAX_GB}GB (출처 {MAX_GB_ENV_SOURCE}) | 잡 보관 {MAX_JOBS}건",
        flush=True,
    )
    app.run(host=HOST, port=PORT, threaded=True)
