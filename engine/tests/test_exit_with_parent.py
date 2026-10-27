"""앱(부모)이 사라지면 엔진이 따라 꺼지는가 — stdin 파이프가 닫히면 SIGTERM(uvicorn 정상 종료)을 받는다.

앱이 강제 종료되면 엔진이 부모 없이 남아 예전 강제 마스킹 단어로 낮은 포트를 쥐고 있었다(맥 실측).
"""

import subprocess
import sys
import textwrap
from pathlib import Path

ENGINE_DIR = Path(__file__).resolve().parents[1]


def test_stdin_eof_raises_sigterm():
    code = textwrap.dedent("""
        import os, signal, time
        signal.signal(signal.SIGTERM, lambda *_: os._exit(7))
        from app.main import exit_with_parent
        exit_with_parent()
        print("ready", flush=True)
        while True:  # uvicorn 루프처럼 시그널 처리기가 돌 틈을 준다
            time.sleep(0.1)
    """)
    proc = subprocess.Popen([sys.executable, "-c", code], cwd=ENGINE_DIR,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "ready"
        proc.stdin.close()  # 앱 프로세스가 끝난 것과 같다
        assert proc.wait(timeout=8) == 7
    finally:
        proc.kill()
