"""잡 목록 정리 — '아직 안 받은 산출물의 링크'를 지우지 않는다는 불변식.

/api/progress 폴링마다 prune_jobs 가 돈다. 이 함수가 잘못 자르면 사용자는 링크를 되찾을
방법이 없다(파일 목록을 보여주는 라우트가 없어, 파일명을 모르면 /files/ 를 부를 수 없다).
특히 대기 중인 잡을 상한 계산에 넣으면 '배치가 클수록 방금 끝난 잡이 먼저 밀려나는'
정반대 동작이 된다 — 실제로 한 번 그렇게 깨졌기에 여기서 못 박아 둔다.

실행: python -m pytest tests/test_prune_jobs.py -q
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "web"))
import server  # noqa: E402


@pytest.fixture
def bili(tmp_path, monkeypatch):
    """운영 상태(jobs·downloads)를 건드리지 않고 격리해서 돌린다."""
    monkeypatch.setattr(server, "DOWNLOAD_DIR", tmp_path)
    monkeypatch.setattr(server, "jobs", {})
    monkeypatch.setattr(server, "jobs_order", [])
    return tmp_path


def add(tmp_path, job_id, status, files=(), on_disk=True):
    for name in files:
        if on_disk:
            (tmp_path / name).write_bytes(b"\0" * 10)
    server.jobs[job_id] = {
        "id": job_id, "url": f"https://www.bilibili.com/video/{job_id}",
        "quality": "best", "simulate": False, "status": status, "pct": 100.0,
        "speed": "-", "eta": "-", "log": [], "files": list(files),
        "width": None, "height": None, "resolution": "", "created_at": 0,
    }
    server.jobs_order.append(job_id)


def test_대기잡이_완료잡을_밀어내지_않는다(bili):
    # 130건 배치 도중: 35건 완료(파일 실재) + 95건 대기. 아직 아무것도 버릴 때가 아니다.
    for i in range(35):
        add(bili, f"d{i}", "done", [f"done{i}.mp4"])
    for i in range(95):
        add(bili, f"q{i}", "queued")

    server.prune_jobs()

    done = [j for j in server.jobs.values() if j["status"] == "done"]
    assert len(done) == 35, "대기 잡이 상한을 잡아먹어 완료 링크가 사라지면 안 된다"
    assert len(server.jobs_order) == 130


def test_끝난_잡만_상한을_받는다(bili):
    for i in range(130):
        add(bili, f"d{i}", "done", [f"done{i}.mp4"])

    server.prune_jobs()

    assert len(server.jobs_order) == server.MAX_JOBS
    assert "d0" not in server.jobs, "버려야 한다면 가장 오래된 것부터"
    assert "d129" in server.jobs, "최근 결과물은 남긴다"


def test_방금_실패한_잡은_살아남는다(bili):
    """상한을 채운 뒤 새로 실패한 잡이 즉시 사라지면 사용자는 실패 사유를 볼 수 없다.

    이 함수는 폴링(약 1.2초)마다 돈다. 예전 규칙('파일 없는 잡부터 버린다')에서는
    파일을 들고 있는 완료 잡 100건이 차 있을 때 **방금 실패한 잡이 유일한 '파일 없는 잡'**
    이라 1순위로 버려졌다 — 실패 카드가 한 번도 화면에 뜨지 못했다.
    """
    for i in range(server.MAX_JOBS):          # 상한을 파일 든 완료로 가득 채운다
        add(bili, f"old{i}", "done", [f"o{i}.mp4"])
    add(bili, "just_failed", "error")         # 방금 실패 — 파일이 없다
    server.jobs["just_failed"]["log"].append("[오류] 영상을 받을 수 없습니다")

    server.prune_jobs()                       # 끝난 잡 101건 → 1건 초과

    assert "just_failed" in server.jobs, "방금 실패한 잡이 사라지면 사유를 볼 방법이 없다"
    assert "old0" not in server.jobs, "가장 오래된 잡이 대신 밀려난다"


def test_방금_만료된_잡은_같은_호출에서_지워지지_않는다(bili):
    """'만료(파일 삭제됨)' 뱃지를 한 번은 보여줘야 왜 안 열리는지 알 수 있다."""
    for i in range(server.MAX_JOBS):
        add(bili, f"old{i}", "done", [f"o{i}.mp4"])
    add(bili, "vanished", "done", ["gone.mp4"])
    (bili / "gone.mp4").unlink()              # 보존 기간이 지나 파일만 사라진 상태

    server.prune_jobs()                       # 같은 호출에서 expired 전환 + 상한 정리

    assert "vanished" in server.jobs
    assert server.jobs["vanished"]["status"] == "expired"
    assert any("보존 기간" in line for line in server.jobs["vanished"]["log"])


def test_오래된_것부터_버린다(bili):
    """유일하게 안전한 기준은 나이다 — 방금 생긴 잡은 절대 먼저 버려지지 않는다."""
    for i in range(server.MAX_JOBS + 5):
        add(bili, f"j{i}", "done", [f"f{i}.mp4"])

    server.prune_jobs()

    assert not any(f"j{i}" in server.jobs for i in range(5)), "오래된 5건이 밀려난다"
    assert all(f"j{i}" in server.jobs for i in range(5, server.MAX_JOBS + 5))


def test_파일이_사라지면_만료로_내린다(bili):
    add(bili, "gone", "done", ["gone.mp4"])
    (bili / "gone.mp4").unlink()

    server.prune_jobs()

    assert server.jobs["gone"]["status"] == "expired", "'완료'인 채 404 링크를 광고하면 안 된다"
    assert any("보존 기간" in line for line in server.jobs["gone"]["log"])


def test_링크를_버릴_때는_로그로_알린다(bili, capsys):
    for i in range(103):
        add(bili, f"d{i}", "done", [f"done{i}.mp4"])

    server.prune_jobs()

    out = capsys.readouterr().out
    assert out.count("상한 초과") == 3, "조용히 버리지 않는다 — 버린 건마다 한 줄"
    assert "done0.mp4" in out, "파일은 남아 있으므로 이름을 남겨 회수할 수 있게 한다"


def test_상한_이하면_아무것도_안_버린다(bili):
    for i in range(server.MAX_JOBS):
        add(bili, f"d{i}", "done", [f"done{i}.mp4"])

    server.prune_jobs()

    assert len(server.jobs_order) == server.MAX_JOBS
