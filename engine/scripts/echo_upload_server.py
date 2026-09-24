"""
scripts/echo_upload_server.py
프록시 검증용 에코 서버. "사이트가 실제로 무엇을 받았는지" 를 화면에 띄운다.

프록시가 본문을 갈아끼웠는지는 우리 쪽 로그로도 볼 수 있지만, 그건 우리가 우리를
믿는 것이다. 받는 쪽에 원문이 안 닿았다는 걸 보려면 받는 쪽이 보여 줘야 한다.

  python scripts/echo_upload_server.py            # 8899 포트

Chrome 은 기본적으로 loopback 에 프록시를 안 쓴다. 검증할 땐 반드시:
  --proxy-bypass-list="<-loopback>"
"""

from __future__ import annotations

import html
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8899

FORM = """<!doctype html><meta charset="utf-8">
<title>업로드 에코</title>
<style>
 body{font:15px/1.6 system-ui;margin:40px;max-width:760px}
 .box{border:1px solid #ccc;border-radius:8px;padding:16px;margin:16px 0}
 pre{background:#f6f6f6;padding:12px;border-radius:6px;white-space:pre-wrap;word-break:break-all}
 h1{font-size:20px} .hit{color:#b00} .ok{color:#080}
</style>
<h1>업로드 에코 서버</h1>
<p>여기로 올라온 파일을 <b>받은 그대로</b> 보여준다.</p>
<form class="box" method="post" action="/upload" enctype="multipart/form-data">
  <input type="file" name="file" id="f">
  <button type="submit">업로드</button>
</form>

<div class="box">
  <p>OS 파일 창 없이 보낸다 — 검증 대상은 파일 피커가 아니라 multipart 가로채기다.</p>
  <button id="send">PII 든 파일 보내기</button>
  <div id="out"></div>
</div>
<script>
const SAMPLE = `근로계약서 (테스트용 가짜 데이터)

성명: 홍길동
주민등록번호: 900101-1234567
연락처: 010-1234-5678
이메일: hong@example.com
주소: 서울특별시 강남구 테헤란로 123
`;
document.getElementById('send').onclick = async () => {
  const out = document.getElementById('out');
  out.textContent = '보내는 중… (검토 대기로 붙들릴 수 있다)';
  const fd = new FormData();
  fd.append('conversation_id', 'demo-1');
  fd.append('file', new File([SAMPLE], '계약서.txt', {type:'text/plain'}));
  try {
    const r = await fetch('/upload', {method:'POST', body: fd});
    document.open(); document.write(await r.text()); document.close();
  } catch (e) { out.textContent = '실패: ' + e; }
};
</script>
"""


def _parts(body: bytes, boundary: bytes):
    for chunk in body.split(b"--" + boundary)[1:-1]:
        head, sp, payload = chunk.lstrip(b"\r\n").partition(b"\r\n\r\n")
        if not sp:
            continue
        if payload.endswith(b"\r\n"):
            payload = payload[:-2]
        m = re.search(rb'filename="([^"]*)"', head)
        yield (m.group(1).decode("utf-8", "replace") if m else None), head, payload


class Handler(BaseHTTPRequestHandler):
    def _send(self, body: str, code: int = 200) -> None:
        raw = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        self._send(FORM)

    def do_POST(self) -> None:  # noqa: N802
        ctype = self.headers.get("Content-Type", "")
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        m = re.search(r'boundary="?([^";]+)"?', ctype)
        if not m:
            self._send(FORM + "<p class=hit>multipart 가 아니다</p>", 400)
            return

        out = [FORM, "<div class=box><h2>받은 내용</h2>"]
        out.append(f"<p>Content-Length: <b>{length}</b></p>")
        for filename, head, payload in _parts(body, m.group(1).encode()):
            if filename is None:
                continue
            text = payload.decode("utf-8", "replace")
            # 원문이 닿았는지 한눈에 보이게 표시한다.
            # 값만 본다. 예전엔 "주민" 글자도 셌는데 그건 마스킹 후에도 남는
            # **레이블**이라 항상 유출로 오탐했다.
            leaked = bool(
                re.search(r"\d{6}-\d{7}", text)
                or re.search(r"01\d-\d{3,4}-\d{4}", text)
                or re.search(r"[\w.]+@[\w.]+\.\w+", text)
            )
            verdict = (
                "<span class=hit>원문이 그대로 도착했다</span>"
                if leaked
                else "<span class=ok>원문 흔적 없음</span>"
            )
            out.append(f"<p>파일명: <b>{html.escape(filename)}</b> · {len(payload)} bytes · {verdict}</p>")
            out.append("<pre>" + html.escape(text[:2000]) + "</pre>")
            out.append("<details><summary>파트 헤더</summary><pre>"
                       + html.escape(head.decode("utf-8", "replace")) + "</pre></details>")
        out.append("</div>")
        self._send("".join(out))

    def log_message(self, fmt, *args):  # 조용히
        sys.stderr.write("[echo] " + fmt % args + "\n")


if __name__ == "__main__":
    print(f"[echo] http://localhost:{PORT} 에서 대기", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
