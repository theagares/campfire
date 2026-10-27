"""앱(부모)이 사라지면 엔진이 따라 꺼지는가 — stdin 파이프가 닫히면 SIGTERM(uvicorn 정상 종료)을 받는다.

앱이 강제 종료되면 엔진이 부모 없이 남아 예전 강제 마스킹 단어로 낮은 포트를 쥐고 있었다(맥 실측).
"""

import subprocess
import sys
import textwrap
import threading
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


def test_watching_stdin_does_not_block_spawned_children():
    """감시 중에도 파서 프로세스 풀(spawn)의 자식이 뜬다.

    Windows 에서 막힌 stdin read 가 걸려 있으면 자식 Python 이 시작하며 물려받은 stdin 을 확인하다 멈췄다 —
    v0.2.52 설치본에서 파일 검사가 전부 "parser worker did not start within 10s" 로 실패했다.
    """
    code = textwrap.dedent("""
        import asyncio
        from app.main import exit_with_parent
        from app.core.parser.process_pool import ParserProcessPool

        async def main():
            pool = ParserProcessPool(1, 0, worker_start_timeout=15)
            try:
                (text, status, _), _ = await pool.parse(b"hello", "text/plain", "a.txt",
                                                        collect_images=False, timeout=15)
                print(status, text, flush=True)
            except Exception as exc:
                print("error", exc, flush=True)
            finally:
                pool.close()

        if __name__ == "__main__":
            exit_with_parent()
            asyncio.run(main())
    """)
    proc = subprocess.Popen([sys.executable, "-c", code], cwd=ENGINE_DIR,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    out: list[str] = []
    reader = threading.Thread(target=lambda: out.append(proc.stdout.readline()), daemon=True)
    try:
        reader.start()
        reader.join(40)
        assert out and out[0].strip() == "ok hello", out
    finally:
        proc.kill()
