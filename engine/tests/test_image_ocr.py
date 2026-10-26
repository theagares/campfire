"""이미지 속 글자 검사 — 모델 없이: 파서 표시, 표시 바꾸기(위치), 거르기·미검사, GPU 없음."""

from __future__ import annotations

import asyncio
import io

import pytest
from PIL import Image

from app import config
from app.core.detectors import image_ocr
from app.core.parser import STATUS_OK, STATUS_UNSUPPORTED, parse_document
from app.core.parser.docx import extract_docx
from app.core.parser.images import MARK_RE
from app.core.parser.pptx import extract_pptx


def _png(w: int = 400, h: int = 120, color=(255, 255, 255)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "PNG")
    return buf.getvalue()


def _docx_with_picture() -> bytes:
    from docx import Document

    d = Document()
    d.add_paragraph("첫 문단")
    d.add_picture(io.BytesIO(_png()))
    d.add_paragraph("끝 문단")
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def _pptx_with_picture() -> bytes:
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    slide.shapes.add_picture(io.BytesIO(_png()), Inches(1), Inches(1))
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def test_기능을_끄면_파서_결과가_그대로고_이미지_파일은_미지원():
    b = _docx_with_picture()
    assert extract_docx(b) == extract_docx(b, None) == "첫 문단\n\n끝 문단"
    assert parse_document(_png(), "image/png", "shot.png")[1] == STATUS_UNSUPPORTED


def test_DOCX_표_칸_안의_그림도_그_자리에_표시가_남는다():
    from docx import Document

    d = Document()
    d.add_paragraph("앞 문단")
    t = d.add_table(rows=1, cols=2)
    t.cell(0, 0).text = "캡처"
    t.cell(0, 1).paragraphs[0].add_run().add_picture(io.BytesIO(_png()))
    d.add_paragraph("뒤 문단")
    buf = io.BytesIO()
    d.save(buf)
    images: list[bytes] = []
    parts = extract_docx(buf.getvalue(), images).split("\n\n")
    assert len(images) == 1 and parts[0] == "앞 문단" and parts[2] == "뒤 문단"
    assert parts[1].startswith("캡처 | ") and MARK_RE.search(parts[1])


def test_켜면_이미지_파일과_DOCX_PPTX_속_그림_자리에_표시가_남는다():
    images: list[bytes] = []
    text, status, _ = parse_document(_png(), "image/png", "shot.png", images)
    assert status == STATUS_OK and MARK_RE.fullmatch(text) and len(images) == 1

    images = []
    text = extract_docx(_docx_with_picture(), images)
    assert len(images) == 1 and text.split("\n\n")[0] == "첫 문단" and MARK_RE.search(text) and text.endswith("끝 문단")

    images = []
    assert MARK_RE.search(extract_pptx(_pptx_with_picture(), images)) and len(images) == 1


def _fake_reader(monkeypatch, results):
    calls = []

    async def read(blob):
        calls.append(blob)
        return results.pop(0)

    monkeypatch.setattr(image_ocr.get(), "read", read)
    return calls


def test_표시를_읽은_글로_바꾸고_이미지_위치를_바꾼_뒤_글_기준으로_적는다(monkeypatch):
    a, b = _png(color=(250, 250, 250)), _png(color=(240, 240, 240))
    _fake_reader(monkeypatch, [
        {"text": "가나다", "tiles": [{"box": [0, 0, 10, 10], "start": 0, "end": 3}], "secs": 1},
        {"text": "주민 900101-1234567", "tiles": [{"box": [5, 5, 50, 20], "start": 3, "end": 17}], "secs": 1},
    ])
    images = [a, b]
    text, infos = asyncio.run(image_ocr.fill("앞\n0\n중간\n1\n뒤", images))
    assert [i["status"] for i in infos] == ["read", "read"]
    assert text == "앞\n[이미지 1]\n가나다\n중간\n[이미지 2]\n주민 900101-1234567\n뒤"
    assert text[infos[0]["start"]:infos[0]["end"]] == "[이미지 1]\n가나다"
    assert text[infos[1]["start"]:infos[1]["end"]] == "[이미지 2]\n주민 900101-1234567"  # 첫 이미지 글 길이만큼 밀렸다


def test_작은_그림은_건너뛰고_못_읽은_그림은_미검사(monkeypatch):
    _fake_reader(monkeypatch, [None])
    images = [_png(40, 20), _png()]
    text, infos = asyncio.run(image_ocr.fill("0\n1", images))
    assert [i["status"] for i in infos] == ["skipped", "unchecked"]
    assert infos[1]["reason"] == image_ocr.UNCHECKED_REASON
    assert text == "\n"  # 둘 다 글 없이 빠진다


def test_같은_그림은_한_번만_읽는다(monkeypatch):
    calls = _fake_reader(monkeypatch, [{"text": "로고 아님", "tiles": [], "secs": 1}])
    blob = _png()
    text, infos = asyncio.run(image_ocr.fill("0 1", [blob, blob]))
    assert len(calls) == 1 and [i["status"] for i in infos] == ["read", "read"] and text.count("로고 아님") == 2


def test_GPU_가_없다고_한_번_보면_다시_띄우지_않는다(monkeypatch):
    proc = image_ocr.VarcoOcrProcess()
    monkeypatch.setattr(image_ocr.model_status, "image_ocr_ready", lambda: True)
    spawned = []

    async def ensure():
        spawned.append(1)
        raise RuntimeError("image_ocr: 예상치 못한 준비 응답: {'ready': False, 'error': 'no_gpu'}")

    monkeypatch.setattr(proc, "_ensure_process", ensure)
    assert asyncio.run(proc.read(_png())) is None and proc.no_gpu
    assert asyncio.run(proc.read(_png())) is None and spawned == [1]


def test_GPU_torch_는_이미지_검사_사이드카에만_앞세우고_따로_지정한_파이썬이면_그대로(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "IMAGE_OCR_GPU_TORCH_DIR", tmp_path / "gpu_torch")
    monkeypatch.setattr(config, "IMAGE_OCR_PYTHON_OVERRIDE", "")
    monkeypatch.setattr(image_ocr.model_status, "image_ocr_accelerator", lambda: "cuda")
    monkeypatch.delenv("PYTHONPATH", raising=False)
    assert image_ocr.VarcoOcrProcess()._spawn_env() == {"PYTHONPATH": str(tmp_path / "gpu_torch")}
    monkeypatch.setattr(config, "IMAGE_OCR_PYTHON_OVERRIDE", r"C:\cuda\python.exe")
    assert image_ocr.VarcoOcrProcess()._spawn_env() == {}


def test_Apple_MPS는_번들_torch를_쓰고_지원안되는_연산만_CPU로_폴백한다(monkeypatch):
    monkeypatch.setattr(config, "IMAGE_OCR_PYTHON_OVERRIDE", "")
    monkeypatch.setattr(image_ocr.model_status, "image_ocr_accelerator", lambda: "mps")
    assert image_ocr.VarcoOcrProcess()._spawn_env() == {"PYTORCH_ENABLE_MPS_FALLBACK": "1"}


@pytest.mark.parametrize("enabled", [False, True])
def test_켰을_때만_이미지_형식이_지원_목록에_있다(monkeypatch, enabled):
    import importlib

    monkeypatch.setenv("SECUREDOC_IMAGE_OCR_ENABLED", "1" if enabled else "0")
    try:
        cfg = importlib.reload(config)
        assert (".png" in cfg.SUPPORTED_EXTENSIONS) is enabled
    finally:
        monkeypatch.delenv("SECUREDOC_IMAGE_OCR_ENABLED", raising=False)
        importlib.reload(config)
