"""이미지 속 글자 읽기 — NCSOFT VARCO-VISION-2.0-1.7B (일반 모델, CC BY-NC 4.0).

엔진이 하위 프로세스로 띄운다(stdio JSON 한 줄씩).
    준비: {"ready": true, "device": "cuda|xpu|mps"}  |  지원 GPU가 없으면
          {"ready": false, "error": "no_accelerator"} 후 끝낸다
    요청: {"id", "images": ["<base64 PNG/JPEG>", ...]}
    응답: {"id", "results": [{"text", "tiles": [{"box": [x0, y0, x1, y1], "start", "end"}], "secs"}]}
          start/end 는 text 안에서 그 조각 글이 있는 위치(엔진은 지금 text 만 쓴다 — 이미지도 글로 판정·출력).

화면 캡처를 기준으로 맞췄다(사진 문서는 지원하지 않는다). 실험 기록: D:/Dev/hackathon/varco_ocr (2026-10-25~26).
- 그림 통째로 넣으면 눈에 띄는 배지(대외비 등)를 건너뛰고 덩어리끼리 글이 붙는다 → 여백·선 기준으로 조각내 읽는다.
- 조각은 키우지 않는다(모델이 384 칸에 맞춰 알아서 늘린다) — 1536 으로 키울 때보다 입력 1/3, 정확도 같음.
- 조각을 묶어 한 generate 로 — 묶음 하나의 (조각 수 × 가장 긴 입력) 이 토큰 예산 안. 입력 토큰당 약 0.25MB 를 더 쓴다.
- 타임라인 칸·상자는 한 줄로 잇는다(기밀 탐지는 줄바꿈에서 문장을 자른다), 표의 가로선으로 나뉜 행은 잇지 않는다.
- 글자 없는 조각의 쓰레기(같은 글자 반복, 2~4토큰 주기 되풀이, LaTeX 그림 코드, 모델 이름)를 거른다.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path

import numpy as np
import torch
from PIL import Image

PROMPT = ("이 이미지의 글자를 마크다운으로 빠짐없이 원문 그대로 옮겨 적어 주세요. 줄이 바뀌는 곳은 줄바꿈하고, 설명은 붙이지 마세요. "
          "글자가 없으면 아무것도 쓰지 마세요.")
NO_TEXT = re.compile(r"(글자|텍스트|문자)[가는이]?\s*(없|보이지)")
SELF_NAME = re.compile(r"^\W*VARCO[\s-]*VISION\W*$", re.I)  # 빈 곳·아이콘 조각에 모델이 자기 이름을 지어낸다
DRAWING = re.compile(r"\\(begin|end)\{|\\(draw|node|path|fill|includegraphics)\b")  # 차트·도형을 그림 코드로 옮긴 줄
# 줄 전체가 LaTeX 명령뿐 — 로고·도형 조각을 '\circle' 처럼 옮긴 것. 'C:\Users\이름' 같은 경로는 앞에 다른 글이 있어 남는다
ONLY_LATEX = re.compile(r"^\s*(\\[A-Za-z]+(\{[^{}]*\})*\s*)+$")
CIRCLED = re.compile(r"\\(?:textcircled|circled)\{(\d{1,2})\}")  # 번호 목록의 ①② 를 이렇게 옮긴다
# 글자를 감싼 명령은 안의 글자만 남긴다 — 차트 범례가 '\red{선} \blue{원}' 으로 왔다. 그림 코드(\begin·\end)는 DRAWING 이 거르게 둔다
COLORED = re.compile(r"\\textcolor\{[^{}]*\}\{([^{}]*)\}")
WRAPPED = re.compile(r"\\(?!begin\b|end\b)[A-Za-z]+\{([^{}\\]*[^\W\d_][^{}\\]*)\}")
LATEX = {"cdot": "·", "rightarrow": "→", "to": "→", "leftarrow": "←", "hookrightarrow": "↪", "Rightarrow": "⇒",
         "longrightarrow": "→", "leftrightarrow": "↔", "geq": "≥", "leq": "≤", "star": "★",
         "times": "×", "pm": "±", "sim": "~", "checkmark": "✓", "mid": "|", "bullet": "•", "hline": ""}
SHAPE = re.compile(r"\\(?:circle|square|triangle)\b(?:\{[^{}]*\})?")  # 아이콘 도형('\circle{blue}') — 글자가 아니다
MAX_NEW = 2048  # 조각 하나의 출력 상한 — 실제로는 조각 넓이로 더 작게 잡는다(tile_budget)


# ── 자르기 ─────────────────────────────────────────────────────────────────────
def _spans(profile: np.ndarray, thresh: float, min_gap: int) -> list[tuple[int, int]]:
    """profile 이 thresh 를 넘는 구간들 — min_gap 보다 짧은 빈 구간은 이어 붙인다."""
    ink = np.flatnonzero(profile > thresh)
    if not len(ink):
        return []
    out = [[int(ink[0]), int(ink[0]) + 1]]
    for i in ink[1:]:
        if i - out[-1][1] < min_gap:
            out[-1][1] = int(i) + 1
        else:
            out.append([int(i), int(i) + 1])
    return [tuple(x) for x in out]


def _strip_lines(edge: np.ndarray, min_len: int) -> tuple[np.ndarray, list[int]]:
    """긴 가로·세로 선(표 테두리·칸 선·밑줄·상자 테두리)을 지우고 가로선이 있던 행을 돌려준다(행 경계).
    선이 여백을 막아 엑셀 시트·워드 쪽이 통째로 한 조각이 되고 출력이 잘렸다."""
    out = edge.copy()
    hrows: set[int] = set()
    for axis, (a, o) in enumerate(((edge, out), (edge.T, out.T))):
        cand = np.flatnonzero(a.sum(1) >= min_len)
        if not len(cand):
            continue
        d = np.diff(np.pad(a[cand].astype(np.int8), ((0, 0), (1, 1))), axis=1)
        for k, r in enumerate(cand):
            for s, e in zip(np.flatnonzero(d[k] == 1), np.flatnonzero(d[k] == -1)):
                if e - s >= min_len:
                    o[r, s:e] = False
                    if axis == 0:
                        hrows.add(int(r))
    return out, sorted(hrows)


def segment(img: Image.Image, pad: int = 4) -> list[tuple[int, tuple[int, int, int, int]]]:
    """(띠 번호, (x0, y0, x1, y1)) — 위→아래, 띠 안은 왼→오. 띠 번호 = 구역×1000 + 순번(가로선마다 구역이 바뀐다)."""
    g = np.asarray(img.convert("L"), dtype=np.int16)
    edge = np.maximum(np.abs(np.diff(g, axis=1, prepend=g[:, :1])), np.abs(np.diff(g, axis=0, prepend=g[:1]))) > 40
    h, w = edge.shape
    edge, hlines = _strip_lines(edge, max(60, int(0.05 * max(h, w))))
    prof, ink = edge.sum(1), max(2, 0.01 * w)
    bands = []
    # 행 사이 여백 기준은 높이 800px 까지만 비례 — 긴 쪽에서 문단(약 13px 간격)이 다 붙지 않게, 줄바꿈된 문장은 붙어 있게
    for y0, y1 in _spans(prof, ink, max(4, int(0.015 * min(h, 800)))):
        cuts = [y0] + [r for r in hlines if y0 < r < y1] + [y1]
        for a, b in zip(cuts, cuts[1:]):
            rows = np.flatnonzero(prof[a:b] > ink)
            if len(rows):
                bands.append((a + int(rows[0]), a + int(rows[-1]) + 1))
    bands = [b for b in bands if b[1] - b[0] >= 6]
    out, section = [], 0
    for k, (y0, y1) in enumerate(bands):
        if k and any(bands[k - 1][1] <= r < y0 for r in hlines):
            section += 1
        for x0, x1 in _spans(edge[y0:y1].sum(0), 0, max(6, int(0.02 * w))):
            if x1 - x0 < 6:
                continue
            rows = np.flatnonzero(edge[y0:y1, x0:x1].sum(1))
            yy0, yy1 = y0 + int(rows[0]), y0 + int(rows[-1]) + 1
            if yy1 - yy0 < 6:
                continue
            out.append((section * 1000 + k, (max(0, x0 - pad), max(0, yy0 - pad), min(w, x1 + pad), min(h, yy1 + pad))))
    return out


# ── 출력 정리·잇기 ─────────────────────────────────────────────────────────────
def _has_text(line: str) -> bool:
    """글자·숫자가 두 종류 이상(또는 두 글자 이하 짧은 줄) — 'O-O-O-O', 'ㅁㅁㅁ' 같은 반복을 거른다."""
    kinds = {c for c in line if unicodedata.category(c)[0] in "LN"}
    return len(kinds) > 1 or (len(kinds) == 1 and len(line.strip()) <= 2)


def _circled(n: int) -> str:
    return "⓪" if n == 0 else chr(0x2460 + n - 1) if n <= 20 else f"({n})"


def clean(raw: str) -> str:
    """마크다운 출력 → 글. 정해 둔 LaTeX 명령만 바꾼다(아무 '\\단어' 나 지우면 화면 속 'C:\\Users\\이름' 경로가 사라진다)."""
    s = re.sub(r"<\|[^>]+\|>", "", raw)
    s = re.sub(r"^\s*(#{1,6}\s+|[-*+]\s+|>\s+)", "", s, flags=re.M)
    s = re.sub(r"^\s*\|?\s*:?-{3,}.*$", "", s, flags=re.M)
    s = s.replace("**", "").replace("|", " ")
    s = re.sub(r"\\[\[\]()]", " ", s)
    s = re.sub(r"\\text\{([^{}]*)\}", r"\1", s)
    s = CIRCLED.sub(lambda m: _circled(int(m.group(1))), s)
    s = WRAPPED.sub(r"\1", COLORED.sub(r"\1", SHAPE.sub("", s)))
    s = re.sub(r"\\(" + "|".join(LATEX) + r")\b", lambda m: LATEX[m.group(1)], s)
    s = re.sub(r"\$\s*([^\w\s$]{1,3})\s*\$", r"\1", s)  # 기호 하나를 감싼 수식 '$→$' — '$5' 같은 돈 표시는 그대로
    return "\n".join(l.strip() for l in s.splitlines()
                     if _has_text(l) and not NO_TEXT.search(l) and not SELF_NAME.match(l) and not DRAWING.search(l)
                     and not ONLY_LATEX.match(l))


def join_blocks(pieces: list[str], boxes) -> tuple[str, list[tuple[int, int, int]]]:
    """조각 글을 칸·상자 단위 줄로 잇는다. 돌려주는 값: (글, [(조각 번호, 시작, 끝)]).
    - 조각 안의 여러 줄은 한 줄로.
    - 같은 구역의 이웃한 띠들이 같은 수(2개 이상)의 조각이고 칸 사이가 글자 높이 1.5배 이상이며 가로로 1:1 겹치면
      칸마다 위→아래로 잇는다(타임라인 날짜·제목·설명, 카드 제목·숫자).
    - 그 밖의 띠: 한 줄짜리 조각만 있으면 공백으로 한 줄, 여러 줄 조각(상자)이 있었으면 조각마다 한 줄."""
    bands: dict[int, list] = {}
    for i, ((bi, box), text) in enumerate(zip(boxes, pieces)):
        if text.strip():
            bands.setdefault(bi, []).append((box, text, i))
    # 조각이 모두 2글자 이하인 띠는 버린다 — 타임라인 원·아이콘 줄을 'ㅇ', '0' 으로 읽어 칸 잇기가 끊기고 글에 섞였다.
    # (엑셀 열 문자 'A B C' 줄 같은 화면 장식도 같이 빠진다. 2글자 이하는 PII 가 아니다.)
    bands = {b: row for b, row in bands.items() if any(len("".join(t.split())) > 2 for _, t, _ in row)}
    keys = sorted(bands)
    rows = [sorted(bands[b], key=lambda t: t[0][0]) for b in keys]

    def columnar(row) -> bool:
        return len(row) >= 2 and min(b[0][0] - a[0][2] for a, b in zip(row, row[1:])) >= 1.5 * min(bx[3] - bx[1] for bx, _, _ in row)

    def aligned(k: int) -> bool:
        a, b = rows[k], rows[k + 1]
        return (keys[k] // 1000 == keys[k + 1] // 1000 and len(a) == len(b) and columnar(a) and columnar(b)
                and all(x[0][0] < y[0][2] and y[0][0] < x[0][2] for x, y in zip(a, b)))

    lines: list[list[tuple[str, int]]] = []  # 줄마다 [(조각 글, 조각 번호)]
    one = lambda t: " ".join(t.split("\n"))  # noqa: E731
    i = 0
    while i < len(rows):
        j = i
        while j + 1 < len(rows) and aligned(j):
            j += 1
        if j > i:
            lines += [[(one(rows[k][c][1]), rows[k][c][2]) for k in range(i, j + 1)] for c in range(len(rows[i]))]
        elif any("\n" in t for _, t, _ in rows[i]):
            lines += [[(one(t), idx)] for _, t, idx in rows[i]]
        else:
            lines.append([(t, idx) for _, t, idx in rows[i]])
        i = j + 1
    out, spans, pos = [], [], 0
    for line in lines:
        for n, (t, idx) in enumerate(line):
            if n:
                out.append(" ")
                pos += 1
            spans.append((idx, pos, pos + len(t)))
            out.append(t)
            pos += len(t)
        out.append("\n")
        pos += 1
    return "".join(out).rstrip("\n"), spans


# ── 모델 ───────────────────────────────────────────────────────────────────────
def resolve_device() -> str | None:
    """이 사이드카에 설치된 torch가 실제로 쓸 수 있는 GPU를 고른다."""
    if torch.cuda.is_available():
        return "cuda"
    xpu = getattr(torch, "xpu", None)
    if xpu is not None and xpu.is_available():
        return "xpu"
    mps = getattr(torch.backends, "mps", None)
    if mps is not None and mps.is_available():
        return "mps"
    return None


_BYTES_PER_TOKEN = 0.25 * 1024**2  # 묶음 입력 토큰당 더 쓰는 GPU 메모리(실측 약 0.25MB)
# 출력 KV·작업 공간·조각 크기 흔들림 여유. 1.5 → 1.0GB(서버 측정, 12GB 카드 + 통합 worker 흉내: 묶음 4 → 8, 그림 44장
# 455 → 373초, 최고 예약이 상한 아래 0.27GB, CER 그대로). 0.5GB 는 넘쳤다. 넘치면 묶음을 줄여 다시 읽는다(_read_shrinking).
_HEADROOM = 1.0 * 1024**3
# CUDA 그래프 경로(GraphDecoder): 정적 KV 캐시에 잡는 출력 길이와, HF generate 보다 더 쓰는 VRAM. 서버 실측(그림 24장, 같은
# 묶음을 HF·그래프로 읽은 최대 예약 차이): 묶음 2 174MB · 4 400MB · 8 742MB · 16 1510MB(할당은 줄당 약 56MB — 정적 캐시
# 출력 몫 최대 512토큰 × 114.7KB). 예약 기준 줄당 약 95MB 라 0.1GB + 줄당 96MB 로 잡는다.
_GRAPH_NEW = 256  # 서버 측정 44장의 묶음 최대 출력은 193토큰 — 넘는 묶음만 HF generate 로 다시
_GRAPH_FIXED = 0.1 * 1024**3
_GRAPH_PER_ROW = 96 * 1024**2
# Windows 에서 남겨 둘 시스템 커밋. v0.2.62 측정: VRAM +4.8GB 일 때 사이드카 커밋 8GB — GPU 할당이 커밋으로도 잡힌다고 보고
# (WDDM 백킹, 확인 중) 큰 묶음이 PC 커밋을 바닥내지 않게 묶음 메모리를 남은 커밋으로도 묶는다.
_COMMIT_RESERVE = 2 * 1024**3


def _free_commit() -> float | None:
    """Windows 남은 시스템 커밋(바이트). 다른 OS·모르면 None. (엔진 image_ocr.free_commit_mb 와 같은 값 — 사이드카는
    엔진 패키지를 못 불러 따로 둔다.)"""
    if os.name != "nt":
        return None
    import ctypes

    class _MemoryStatus(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong)] + [
            (n, ctypes.c_ulonglong) for n in ("ullTotalPhys", "ullAvailPhys", "ullTotalPageFile", "ullAvailPageFile",
                                              "ullTotalVirtual", "ullAvailVirtual", "ullAvailExtendedVirtual")]

    status = _MemoryStatus(dwLength=ctypes.sizeof(_MemoryStatus))
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    return float(status.ullAvailPageFile)


def _nvml_free() -> float | None:
    """Windows: 카드 전체의 남은 VRAM(바이트, nvidia-smi 의 free 와 같은 값). WDDM 의 cudaMemGetInfo 는 다른 프로세스 몫을
    빼지 않은 값을 줘, 카드 전체로 다시 묶는다 — v0.2.63 후보 3차 실측(RTX 5070 12GB): 통합 worker 가 떠 카드 9.6GB 사용
    중인데 묶음 16/16994 를 잡아 카드가 11.8GB 로 차고, WDDM 이 worker 의 VRAM 을 RAM 으로 밀어내 남은 물리 메모리가 0.2GB.
    드라이버가 깐 System32\\nvml.dll 을 ctypes 로(새 의존성 없음). 카드가 여러 장이면 CUDA 장치와 맞추기 어려워, 또는 못
    쓰면 None — 그때는 지금처럼 cudaMemGetInfo 만 본다. 리눅스는 cudaMemGetInfo 가 카드 전체를 보므로 부르지 않는다."""
    if os.name != "nt":
        return None
    import ctypes

    class _Memory(ctypes.Structure):
        _fields_ = [("total", ctypes.c_ulonglong), ("free", ctypes.c_ulonglong), ("used", ctypes.c_ulonglong)]

    try:
        nvml = ctypes.WinDLL(os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "nvml.dll"))
        if nvml.nvmlInit_v2() != 0:
            return None
    except (OSError, AttributeError):
        return None
    try:
        count, handle, mem = ctypes.c_uint(), ctypes.c_void_p(), _Memory()
        if nvml.nvmlDeviceGetCount_v2(ctypes.byref(count)) != 0 or count.value != 1:
            return None
        if nvml.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(handle)) != 0:
            return None
        if nvml.nvmlDeviceGetMemoryInfo(handle, ctypes.byref(mem)) != 0:
            return None
        return float(mem.free)
    except AttributeError:
        return None
    finally:
        nvml.nvmlShutdown()


def _spare_memory(device: str) -> float | None:
    """가중치를 올린 뒤 묶음에 쓸 수 있는 메모리(바이트) = 남은 메모리(+ 이미 쓴 양 − 가중치 — 이미 쓴 양이 가중치다).
    모르면 None."""
    try:
        if device == "cuda":
            # 전체 용량이 아니라 남은 VRAM — PII·인젝션 작업자와 브라우저가 같은 카드를 쓴다(v0.2.57 RTX 5070: 다른
            # 프로세스가 약 7GB 를 써 큰 묶음이 CUDA out of memory 로 그림을 못 읽었다). Windows 는 카드 전체(NVML)로도 묶는다.
            spare = torch.cuda.mem_get_info()[0]
            card = _nvml_free()
            if card is not None:
                spare = min(spare, card)
            commit = _free_commit()
            return spare if commit is None else min(spare, commit - _COMMIT_RESERVE)
        if device == "mps":
            return torch.mps.recommended_max_memory() - torch.mps.current_allocated_memory()
        if device == "xpu":
            return torch.xpu.get_device_properties(0).total_memory - torch.xpu.memory_allocated()
    except (AttributeError, RuntimeError):
        pass
    return None


def _graph_bytes(batch: int) -> float:
    """CUDA 그래프 경로가 HF generate 보다 더 쓰는 VRAM — 정적 KV 캐시의 출력 몫(_GRAPH_NEW + 256 단위 올림)과 그래프
    메모리. 묶음 크기별 서버 실측(GraphDecoder 참고)에 맞춘 식이다."""
    return _GRAPH_FIXED + batch * _GRAPH_PER_ROW


def _memory_safe_limits(device: str, batch: int, token_budget: int, graph: bool = False) -> tuple[int, int]:
    """묶음 크기를 남은 메모리로 정한다(batch·token_budget 은 상한). 출력 상한·모델·정밀도는 바꾸지 않는다.

    예전엔 전체 용량 단계(10GB 미만 2/1500, 16GB 미만 4/3000)로 정해 12GB 카드는 비어 있어도 4/3000 이었다 — 그림 다섯 장
    (조각 57개)에 generate 24회(다른 GPU 작업자가 있으면 57회)라, 문서 속 그림을 읽는 동안 Claude 업로드가 시간 초과됐다
    (맥 v0.2.60). budget = (남은 메모리 − 여유 − 그래프 몫) / 토큰당 메모리, 1500~상한 / batch 는 budget 에 맞춰 2·4·8·16.
    그래프 몫은 묶음 크기에 따라 달라 큰 묶음부터 맞춰 본다.
    """
    spare = _spare_memory(device)
    if spare is None:
        return min(batch, 8), min(token_budget, 6000)  # 모르면 예전 기본값
    for size, need in ((16, 12000), (8, 6000), (4, 3000), (2, 0)):
        size = min(batch, size)
        extra = _graph_bytes(size) if graph else 0
        budget = max(1500, min(token_budget, int((spare - _HEADROOM - extra) / _BYTES_PER_TOKEN)))
        if budget >= need:
            break
    return size, budget


def _empty_cache(device: str) -> None:
    backend = getattr(torch, device, None)
    empty = getattr(backend, "empty_cache", None)
    if empty is not None:
        empty()


def _read_shrinking(reader: "Reader", img: Image.Image, device: str) -> dict:
    """묶음이 남은 메모리를 넘으면(OOM) 묶음을 반으로 줄여 다시 읽는다 — 가장 작은 묶음(2/1500)에서도 넘치면 그대로
    실패(그 그림은 미검사). 같은 카드의 통합 worker·브라우저가 잠깐 더 쓰면 여유분(_HEADROOM)을 넘을 수 있다."""
    while True:
        try:
            return reader.read(img)
        except torch.cuda.OutOfMemoryError:  # torch 2.5+ 에선 torch.OutOfMemoryError 와 같다(XPU 도 이것을 낸다)
            _empty_cache(device)
            if reader.batch <= 2 and reader.budget <= 1500:
                raise
            reader.batch, reader.budget = max(2, reader.batch // 2), max(1500, reader.budget // 2)


def _read_one(reader: "Reader", img: Image.Image, device: str, batch: int, token_budget: int) -> dict:
    """그림마다 남은 메모리로 묶음을 다시 정하고 읽는다 — 통합 worker 가 잠깐 더 쓰면 작게, 풀리면 다시 크게. 서버 측정(12GB +
    worker 흉내, 다른 프로세스가 그림 3장 동안 1.5GB 를 더 씀, 44장): 넘친 뒤 줄인 묶음을 그대로 두면 722초, 처음 크기로
    되돌리면 406초(OOM 5번), 남은 메모리로 다시 정하면 413초(OOM 0번)."""
    reader.batch, reader.budget = _memory_safe_limits(device, batch, token_budget, reader.graph is not None)
    return _read_shrinking(reader, img, device)


@torch.no_grad()
def load_model(model_dir: Path, device: str):
    """가중치를 텐서 하나씩 읽어 바로 가속기로. from_pretrained 는 Windows 에서 파일 크기(4.3GB)만큼 커밋을 잡아,
    다른 모델이 떠 있으면 '페이징 파일이 너무 작습니다'(1455)로 실패했다(실측)."""
    from transformers import AutoConfig, LlavaOnevisionForConditionalGeneration
    try:
        from transformers.initialization import no_init_weights
    except ImportError:  # transformers 4.x
        from transformers.modeling_utils import no_init_weights
    with torch.device(device), no_init_weights():
        model = LlavaOnevisionForConditionalGeneration._from_config(AutoConfig.from_pretrained(model_dir),
                                                                     dtype=torch.float16, attn_implementation="sdpa")
    targets = model.state_dict()
    with open(model_dir / "model.safetensors", "rb") as f:
        header = json.loads(f.read(int.from_bytes(f.read(8), "little")))
        header.pop("__metadata__", None)
        base = f.tell()

        def key(k: str) -> str:  # 체크포인트(transformers 4.53 저장) → 5.x 모델 키
            for old, new in (("language_model.model.", "model.language_model."), ("vision_tower.vision_model.", "model.vision_tower.")):
                if k.startswith(old):
                    return new + k[len(old):]
            return k if k in targets else "model." + k

        missing = set(targets) - {key(k) for k in header} - {"lm_head.weight"}
        unknown = [k for k in header if key(k) not in targets]
        if missing or unknown:
            raise RuntimeError(f"VARCO 가중치 키 불일치: 없음 {sorted(missing)[:3]}, 남음 {unknown[:3]}")
        # 버퍼 하나(가장 큰 텐서, 임베딩 약 0.6GB)에 차례로 읽는다 — 텐서마다 bytes + bytearray 두 벌을 만들면 불러오기 RAM
        # 최고치가 0.6GB 더 크고 4.3GB 를 한 번 더 복사했다(A100 측정: 8.6 → 5.1초, 최고 RSS 2.43 → 1.85GB).
        buf = memoryview(bytearray(max(m["data_offsets"][1] - m["data_offsets"][0] for m in header.values())))
        for k, meta in header.items():
            t = targets[key(k)]
            if meta["dtype"] != "F16" or list(t.shape) != meta["shape"]:
                raise RuntimeError(f"VARCO 가중치 모양 불일치: {k}")
            b0, b1 = meta["data_offsets"]
            f.seek(base + b0)
            if f.readinto(buf[:b1 - b0]) != b1 - b0:
                raise RuntimeError(f"VARCO 가중치 파일이 잘렸다: {k}")
            t.copy_(torch.frombuffer(buf[:b1 - b0], dtype=torch.float16).reshape(meta["shape"]))  # 동기 복사라 버퍼를 다시 써도 된다
    emb = model.get_input_embeddings().weight
    if model.lm_head.weight.data_ptr() != emb.data_ptr():  # tie_word_embeddings
        model.lm_head.weight = emb
    return model.eval()


def _tile_budget(box, cap: int) -> int:
    """조각 넓이로 들어갈 수 있는 글자 수를 어림한 출력 상한(글자 하나 40px²) — 어떤 쓰레기든 조각 크기 이상으로 못 뽑게.
    대시보드 꺾은선 차트 조각(106×30)이 TikZ 코드를 상한까지 뽑아 캡처 한 장이 7→82초가 됐다."""
    w, h = box[2] - box[0], box[3] - box[1]
    return max(64, min(cap, int(1.5 * w * h / 40) + 48))


def _stopper(limits: torch.Tensor, n_in: int):
    from transformers import StoppingCriteria, StoppingCriteriaList

    class Stop(StoppingCriteria):
        """되풀이(같은 토큰 12번, 마지막 32토큰이 2~4토큰 주기, 마지막 96토큰이 5~16토큰 주기)거나 조각별 상한에
        닿은 줄만 멈춘다. 긴 주기는 같은 줄의 되풀이다 — 로고 조각이 "VARCO-VISION\\n"(6토큰)을 상한 2048토큰까지
        뽑아 그림 한 장이 77초 걸렸다(줄은 clean 이 지워 글에는 안 남는다). 정상 글이 같은 줄을 6번 넘게 잇는 일은 드물다."""

        def __call__(self, input_ids, scores, **kwargs):
            t = input_ids[:, -12:]
            stop = (t == t[:, :1]).all(dim=1) & (input_ids.shape[1] >= 12)
            if input_ids.shape[1] >= 32:
                w = input_ids[:, -32:]
                for p in (2, 3, 4):
                    stop |= (w[:, p:] == w[:, :-p]).all(dim=1)
            if input_ids.shape[1] - n_in >= 96:
                w = input_ids[:, -96:]
                for p in range(5, 17):
                    stop |= (w[:, p:] == w[:, :-p]).all(dim=1)
            return stop | ((input_ids.shape[1] - n_in) >= limits.to(input_ids.device))

    return StoppingCriteriaList([Stop()])


def _stop_at(seq: list[int], n_in: int, limit: int, eos: int) -> int:
    """_stopper·EOS 와 같은 규칙으로 생성 토큰 몇 개째에서 멈추는지(그 토큰 포함). seq = 프롬프트(패딩 포함) + 생성."""
    gen = seq[n_in:]
    end = gen.index(eos) + 1 if eos in gen else len(gen)
    for k in range(1, end + 1):
        s = seq[:n_in + k]
        if len(set(s[-12:])) == 1:
            return k
        w = s[-32:]
        if any(all(w[i] == w[i + p] for i in range(32 - p)) for p in (2, 3, 4)):
            return k
        if k >= 96:
            w = s[-96:]
            if any(all(w[i] == w[i + p] for i in range(96 - p)) for p in range(5, 17)):
                return k
        if k >= limit:
            return k
    return end


class GraphFailed(RuntimeError):
    """CUDA 그래프 경로 오류 — 이 프로세스의 CUDA 문맥을 더 믿지 않는다. 사이드카는 'graph_failed' 로 답하고 끝나고,
    엔진이 그래프를 끈 사이드카로 다시 띄워 같은 그림을 다시 읽는다(image_ocr.VarcoOcrProcess)."""


class GraphDecoder:
    """HF generate 대신 직접 디코딩하고, 한 단계(토큰 → 언어 모델 → argmax → 위치·마스크 갱신)를 CUDA 그래프로 잡아
    되풀이한다 — 단계마다 커널 수백 개를 띄우는 CPU 비용이 시간을 정했다(서버 A100: HF 단계당 묶음 16 35ms·묶음 4 39ms
    → 그래프 20ms·11ms). torch.compile·Triton 없이 CUDA 드라이버 기능만 쓴다(Windows 앱에도 Triton 이 없다). 출력은 같은
    묶음의 HF generate 와 글자 하나까지 같다(같은 _stopper 규칙으로 자른다). 묶음마다 정적 캐시·그래프를 만들고 버린다."""

    def __init__(self, model, eos: int):
        from transformers import StaticCache
        self.model, self.eos, self.static_cache = model, eos, StaticCache
        self.lm = model.model.language_model

    def rows(self, inputs, limits: torch.Tensor) -> list[list[int]] | None:
        """묶음의 줄마다 생성 토큰(EOS 앞까지, 멈춘 자리까지). _GRAPH_NEW 를 넘겨도 안 끝나면 None(HF 로 다시)."""
        try:
            return self._rows(inputs, limits)
        except Exception as e:  # noqa: BLE001 - illegal memory access·OOM 무엇이든 이 프로세스를 버린다
            raise GraphFailed(f"{type(e).__name__}: {e}") from e

    def _rows(self, inputs, limits):
        ids, am = inputs["input_ids"], inputs["attention_mask"]
        B, n_in = ids.shape
        dev, maxnew = ids.device, int(limits.max())
        L = -(-(n_in + min(maxnew, _GRAPH_NEW) + 1) // 256) * 256
        cache = self.static_cache(config=self.model.config.get_text_config(), max_cache_len=L)
        pos = (am.long().cumsum(-1) - 1).masked_fill(am == 0, 1)  # 왼쪽 패딩 — HF generate 와 같은 위치
        out = self.model(**inputs, position_ids=pos, past_key_values=cache, use_cache=True, logits_to_keep=1,
                         cache_position=torch.arange(n_in, device=dev))
        tok = out.logits[:, -1].argmax(-1, keepdim=True)
        del out
        room = min(maxnew, L - n_in - 1)
        gen = torch.zeros((B, room + 1), dtype=torch.long, device=dev)
        gen[:, :1] = tok
        p, cpos = pos[:, -1:] + 1, torch.full((1,), n_in, dtype=torch.long, device=dev)
        step_t = torch.ones((1,), dtype=torch.long, device=dev)
        mask = torch.zeros((B, 1, 1, L), dtype=torch.bool, device=dev)
        mask[:, 0, 0, :n_in] = am.bool()

        def step():
            mask.index_fill_(3, cpos, True)
            h = self.lm(input_ids=tok, position_ids=p, attention_mask=mask, past_key_values=cache, cache_position=cpos,
                        use_cache=True).last_hidden_state
            nxt = self.model.lm_head(h[:, -1]).argmax(-1, keepdim=True)
            tok.copy_(nxt)
            gen.scatter_(1, step_t.expand(B, 1), nxt)
            p.add_(1)
            cpos.add_(1)
            step_t.add_(1)

        for _ in range(2):  # 데우기는 같은 스트림에서 — 옆 스트림에서 데우면 illegal memory access 가 났다(서버 시제품)
            step()
        steps = 3
        graph = torch.cuda.CUDAGraph()
        with torch.cuda.graph(graph):
            step()
        lim, finished = limits.to(dev), False
        while steps < room:
            graph.replay()
            steps += 1
            if steps % 4 == 0 and self._done(gen[:, :steps], lim):
                finished = True
                break
        torch.cuda.synchronize()
        if not finished and steps < maxnew and not self._done(gen[:, :steps], lim):
            return None
        prompt, got = ids.tolist(), gen[:, :steps].tolist()
        rows = []
        for j in range(B):
            g = got[j][:_stop_at(prompt[j] + got[j], n_in, int(limits[j]), self.eos)]
            rows.append(g[:g.index(self.eos)] if self.eos in g else g)
        return rows

    def _done(self, o: torch.Tensor, lim: torch.Tensor) -> bool:
        """줄마다 EOS·상한·되풀이(_stopper 조건)를 지금 끝자리로 본다 — 되풀이는 이어지므로 몇 단계 늦게 봐도 같다."""
        k = o.shape[1]
        fin = (o == self.eos).any(1) | (k >= lim)
        w = o[:, -12:]
        fin |= (w == w[:, :1]).all(1) & (k >= 12)
        if k >= 32:
            w = o[:, -32:]
            for p in (2, 3, 4):
                fin |= (w[:, p:] == w[:, :-p]).all(1)
        if k >= 96:
            w = o[:, -96:]
            for p in range(5, 17):
                fin |= (w[:, p:] == w[:, :-p]).all(1)
        return bool(fin.all())


class Reader:
    def __init__(self, model_dir: Path, batch: int, token_budget: int, device: str, cuda_graph: bool = False):
        from transformers import AutoProcessor
        self.device = device
        self.model = load_model(model_dir, device)
        # 불러오며 따로 만든 lm_head(약 0.6GB)를 임베딩과 묶어 버린 자리가 할당기 캐시에 남는다 — mem_get_info 가 그만큼을
        # '쓰는 중' 으로 봐 묶음이 작아졌다(A100 에 12GB+통합 worker 흉내: 2/1736 → 비운 뒤 4/4112, 그림 44장 705 → 455초).
        _empty_cache(device)
        self.proc = AutoProcessor.from_pretrained(model_dir)
        self.proc.tokenizer.padding_side = "left"
        self.eos = self.proc.tokenizer.convert_tokens_to_ids("<|im_end|>")
        self.pad = self.proc.tokenizer.pad_token_id
        self.graph = GraphDecoder(self.model, self.eos) if cuda_graph and device == "cuda" else None  # MPS·XPU 는 HF 그대로
        self.batch, self.budget = _memory_safe_limits(device, batch, token_budget, self.graph is not None)
        self._setup_token_count()

    def _setup_token_count(self) -> None:
        """조각 입력 토큰 수를 전처리 없이 세게 준비한다 — 그림 몫은 프로세서와 같은 식(_get_number_of_features), 프롬프트·
        대화 틀 몫은 그림 크기와 무관해 작은 그림 하나로 한 번만 잰다. 예전엔 묶음을 짜려고 조각마다 _encode(anyres 384칸
        자르기·정규화 — CPU 일)를 한 번 더 돌려 길이만 셌다(v0.2.62 측정: GPU 사용률 최대 5%)."""
        self.side = self.proc.image_processor.size["height"]
        probe = Image.new("RGB", (64, 64))
        self.text_tokens = self._encode([probe])["input_ids"].shape[1] - self._image_tokens(probe)

    def _image_tokens(self, crop: Image.Image) -> int:
        w, h = crop.size
        return self.proc._get_number_of_features(h, w, self.side, self.side)

    def _ntok(self, crop: Image.Image) -> int:
        return self._image_tokens(crop) + self.text_tokens

    def _encode(self, crops):
        convs = [[{"role": "user", "content": [{"type": "image", "image": c}, {"type": "text", "text": PROMPT}]}] for c in crops]
        return self.proc.apply_chat_template(convs, add_generation_prompt=True, tokenize=True, return_dict=True,
                                             return_tensors="pt", processor_kwargs={"padding": True})

    @torch.inference_mode()
    def read(self, img: Image.Image) -> dict:
        t0 = time.perf_counter()
        boxes = segment(img)
        crops = [img.crop(b) for _, b in boxes]
        order = sorted(range(len(crops)), key=lambda i: crops[i].size[0] / crops[i].size[1])  # 모양이 비슷한 것끼리
        ntok = {i: self._ntok(crops[i]) for i in order}
        groups, cur = [], []
        for i in order:
            if cur and (len(cur) >= self.batch or (len(cur) + 1) * max(ntok[j] for j in cur + [i]) > self.budget):
                groups.append(cur)
                cur = []
            cur.append(i)
        if cur:
            groups.append(cur)
        raws = [""] * len(crops)
        for idx in groups:
            inputs = self._encode([crops[i] for i in idx]).to(self.device, torch.float16)
            n_in = inputs["input_ids"].shape[1]
            limits = torch.tensor([_tile_budget(boxes[i][1], MAX_NEW) for i in idx])
            rows = self.graph.rows(inputs, limits) if self.graph is not None else None
            if rows is None:
                gen = self.model.generate(**inputs, max_new_tokens=int(limits.max()), do_sample=False,
                                          stopping_criteria=_stopper(limits, n_in), pad_token_id=self.pad)
                rows = [gen[j, n_in:].tolist() for j in range(len(idx))]
                del gen
            for j, i in enumerate(idx):
                ids = rows[j]
                if self.eos in ids:  # 묶음에서 먼저 끝난 줄 뒤에는 패딩이 붙는다
                    ids = ids[:ids.index(self.eos)]
                raws[i] = self.proc.decode([t for t in ids if t != self.pad], skip_special_tokens=False)
            del inputs
        text, spans = join_blocks([clean(r) for r in raws], boxes)
        return {"text": text, "tiles": [{"box": list(boxes[i][1]), "start": s, "end": e} for i, s, e in spans],
                "secs": round(time.perf_counter() - t0, 2)}


def _fatal(reader: Reader, e: Exception) -> bool:
    """그래프를 켠 프로세스에서 그래프가 CUDA 문맥을 망가뜨렸을 수 있는 오류 — illegal memory access 는 다음 동기화에서
    다른 자리로도 나온다. 이 프로세스는 'graph_failed' 로 답하고 끝난다(엔진이 그래프를 끄고 다시 띄워 다시 읽는다)."""
    return reader.graph is not None and (isinstance(e, GraphFailed) or "CUDA error" in str(e))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-dir", required=True)
    ap.add_argument("--batch", type=int, default=16)  # 상한 — 실제 묶음은 남은 메모리로(_memory_safe_limits)
    ap.add_argument("--token-budget", type=int, default=18000)
    ap.add_argument("--cuda-graph", type=int, default=0)  # 1 이면 CUDA 에서 GraphDecoder(엔진 config.IMAGE_OCR_CUDA_GRAPH)
    args = ap.parse_args()
    # 엔진은 이 프로세스의 stderr 를 평소엔 읽지 않는다 — 경고가 쌓여 파이프가 차면 멈출 수 있어 조용히 한다
    import warnings

    from transformers.utils import logging as hf_logging
    warnings.filterwarnings("ignore")
    hf_logging.set_verbosity_error()
    hf_logging.disable_progress_bar()
    device = resolve_device()
    if device is None:  # 지원 GPU 없는 PC는 이미지를 검사하지 않는다 — 엔진이 이미지를 막는다
        print(json.dumps({"ready": False, "error": "no_accelerator"}), flush=True)
        return
    reader = Reader(Path(args.model_dir), args.batch, args.token_budget, device, bool(args.cuda_graph))
    card = _nvml_free() if device == "cuda" else None
    print(json.dumps({"ready": True, "device": device, "batch": reader.batch, "budget": reader.budget,
                      "graph": reader.graph is not None,
                      "card_free_mb": None if card is None else round(card / 2**20)}), flush=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req_id = None
        try:
            req = json.loads(line)
            req_id = req.get("id")
            results = []
            for b64 in req["images"]:
                img = Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")
                results.append(_read_one(reader, img, device, args.batch, args.token_budget))
                _empty_cache(device)
            out = {"id": req_id, "results": results}
        except Exception as e:  # noqa: BLE001 - 한 요청의 실패로 프로세스를 죽이지 않는다
            if _fatal(reader, e):
                print(json.dumps({"id": req_id, "error": f"graph_failed: {type(e).__name__}: {e}"}, ensure_ascii=False),
                      flush=True)
                os._exit(3)
            out = {"id": req_id, "error": f"{type(e).__name__}: {e}"}
        print(json.dumps(out, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
