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

# 컨테이너 안에서는 모든 인터페이스에 붙는다 — Traefik 이 도커 네트워크로 접근한다.
# 외부 노출 통제는 포트를 publish 하지 않는 것으로 한다(VPS compose 참조).
# ⚠ 줄 이어쓰기 중간에는 주석도, 두 번째 ENV 도 넣을 수 없다.
#    2026-08-08 에 그렇게 넣었다가 이미지 빌드가 통째로 막혔다(IT팀 2026-08-18 발견).
ENV PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=4434 \
    BILI_DOWNLOAD_TTL_HOURS=24

EXPOSE 4434

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request,os,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','4434')+'/api/health', timeout=4).status==200 else 1)"

CMD ["python", "web/server.py"]
