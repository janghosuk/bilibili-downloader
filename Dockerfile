# biliblil Downloader (웹 버전) — VPS 배포용 이미지
#
# 빌드: docker build -t origin/bilibili-downloader .
# 실행: docker run -p 4434:4434 -v bili_dl:/app/web/downloads origin/bilibili-downloader
#
# ⚠ downloads/ 는 반드시 볼륨으로 빼세요 — 컨테이너 삭제 시 받던 파일이 같이 사라집니다.
#    (보존 기간은 BILI_DOWNLOAD_TTL_HOURS, 기본 24시간 후 자동 삭제)
FROM python:3.12-slim

# yt-dlp 가 영상+음성 스트림을 병합할 때 ffmpeg 를 호출한다. 없으면 고화질 다운로드가 실패한다.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY web/ ./web/

ENV PYTHONUNBUFFERED=1 \
    PORT=4434 \
    BILI_DOWNLOAD_TTL_HOURS=24

EXPOSE 4434

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request,os,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','4434')+'/api/health', timeout=4).status==200 else 1)"

CMD ["python", "web/server.py"]
