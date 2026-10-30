"""시험용 가짜 통합 GPU worker — combined_gpu_inference.py 와 같은 stdio 줄 프로토콜(모델 없음).

OCR 그림(base64 를 푼 글)으로 동작을 고른다: die = 답 없이 끝남, graph = graph_failed 로 답하고 끝남, oom = 오류로 답함,
slow = 0.3초 뒤 답함, 그 밖 = 읽음. 응답마다 pid 를 실어 같은 프로세스인지 본다. FAKE_OCR=0 이면 OCR 없이 뜬다.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time


def say(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


say({"ready": True, "device": "cuda", "ocr": os.environ.get("FAKE_OCR", "1") != "0"})
loaded = False
for line in sys.stdin:
    if not line.strip():
        continue
    req = json.loads(line)
    rid, cmd, pid = req.get("id"), req.get("command"), os.getpid()
    if cmd == "pii":
        say({"id": rid, "pii_entities": [[] for _ in req["texts"]], "pid": pid})
    elif cmd == "ocr_load":
        loaded = True
        say({"id": rid, "device": "cuda", "batch": 4, "budget": 4608, "graph": req.get("cuda_graph"), "pid": pid})
    elif cmd == "ocr_unload":
        loaded = False
        say({"id": rid, "reserved_mb": 0, "pid": pid})
    elif cmd == "ocr_read":
        what = base64.b64decode(req["images"][0]).decode()
        if what == "die":
            os._exit(1)
        if what == "graph":
            say({"id": rid, "error": "graph_failed: GraphFailed: illegal memory access"})
            os._exit(3)
        if what == "oom":
            say({"id": rid, "error": "OutOfMemoryError: CUDA out of memory"})
            continue
        if what == "slow":
            time.sleep(0.3)
        say({"id": rid, "results": [{"text": f"읽음:{what}", "tiles": [], "secs": 0.1}], "loaded_before": loaded,
             "pid": pid})
    else:
        say({"id": rid, "error": f"Unknown command: {cmd!r}"})
