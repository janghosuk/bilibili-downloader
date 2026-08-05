"""다운로드 총량 상한 — TTL 만으로는 디스크가 찬다.

TTL(기본 24h)은 '체류 시간'이지 '총량'이 아니다. 24시간 안에도 대용량 영상이 몰리면
디스크가 찬다. VPS 는 디스크를 원장(사용량·생성물·잡)과 공유하므로 차면 기록이 함께 멈춘다.
2026-08-05 실측: VPS 193GB 중 115GB(60%) 사용 — 여유가 생각만큼 많지 않다.

실행: python -m pytest tests/test_size_cap.py -q   (pytest 는 requirements 에 넣지 않았다 —
런타임 이미지를 무겁게 하지 않으려고. 개발 환경의 pytest 로 돌린다.)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "web"))
import server  # noqa: E402


def make(dirpath, name, size, mtime):
    f = Path(dirpath) / name
    f.write_bytes(b"\0" * size)
    import os
    os.utime(f, (mtime, mtime))
    return f


def test_상한_이하면_아무것도_안_지운다(tmp_path):
    make(tmp_path, "a.mp4", 1000, 1000)
    make(tmp_path, "b.mp4", 1000, 2000)
    freed = server.enforce_size_cap(tmp_path, max_gb=1)          # 1GB 상한, 2KB 사용
    assert freed == 0
    assert len(list(tmp_path.glob("*"))) == 2


def test_넘으면_오래된_것부터_지운다(tmp_path):
    old = make(tmp_path, "old.mp4", 600, 1000)                    # 가장 오래됨
    mid = make(tmp_path, "mid.mp4", 600, 2000)
    new = make(tmp_path, "new.mp4", 600, 3000)                    # 가장 최근
    cap_gb = 1000 / 1024 ** 3                                     # 1000바이트 상한

    freed = server.enforce_size_cap(tmp_path, max_gb=cap_gb)

    assert freed >= 800, f"1800바이트에서 1000 이하로 줄여야 한다(지운 양 {freed})"
    assert not old.exists(), "가장 오래된 것부터 지워야 한다"
    assert new.exists(), "최근 파일은 남겨야 한다 — 방금 받은 것을 지우면 쓸모가 없다"
    assert sum(f.stat().st_size for f in tmp_path.glob("*")) <= 1000


def test_상한_0이면_정리하지_않는다(tmp_path):
    make(tmp_path, "a.mp4", 5000, 1000)
    assert server.enforce_size_cap(tmp_path, max_gb=0) == 0
    assert len(list(tmp_path.glob("*"))) == 1, "0 은 '상한 없음' 이지 '전부 삭제' 가 아니다"


def test_빈_디렉터리에서_터지지_않는다(tmp_path):
    assert server.enforce_size_cap(tmp_path, max_gb=1) == 0


def test_기본값은_5GB(tmp_path):
    # 대표 결정(2026-08-05). 바꾸려면 BILI_DOWNLOAD_MAX_GB 로 덮어쓴다.
    assert server.DOWNLOAD_MAX_GB == 5


def test_하위디렉터리는_건드리지_않는다(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "keep.mp4").write_bytes(b"\0" * 5000)
    make(tmp_path, "a.mp4", 5000, 1000)
    cap_gb = 100 / 1024 ** 3
    server.enforce_size_cap(tmp_path, max_gb=cap_gb)
    assert (tmp_path / "sub" / "keep.mp4").exists(), "glob('*') 는 파일만 본다 — 폴더 안은 대상이 아니다"
