"""
app/core/detectors/injection/backbone.py
인젝션 백본(EXAONE-4.0-1.2B, 약 2.4GB)의 로컬 존재 여부 판별 + 다운로드.

왜 별도 모듈인가: 이 판별을 두 곳이 함께 봐야 한다.
  - llm_mcp.py: 서브프로세스에 오프라인 플래그를 걸지 말지 결정(캐시가 없는데 걸면
    받아올 길이 막혀 그대로 실패한다 — 실사용자 macOS 신규 설치에서 재현).
  - adapters/http_api/models.py: /models/status 의 준비 상태와 /models/fetch 의
    다운로드 단계.

백본은 설치 파일에도 우리 모델 릴리스에도 들어있지 않다. model.safetensors 하나가
2.4GB 라 GitHub 릴리스 에셋 상한(2 GiB)을 넘어 그대로는 올릴 수 없기 때문이다.
그래서 HuggingFace 에서 받아 캐싱하되, 그 다운로드를 앱이 진행 상황을 보여줄 수 있는
/models/fetch 흐름 안에서 하도록 여기 모아둔다.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from app import config

# from_pretrained 가 쓰지 않는 대용량 중복 포맷은 받지 않는다(safetensors 만 있으면 된다).
IGNORE_PATTERNS = ["*.bin", "*.onnx", "*.msgpack", "*.h5", "*.gguf", "*.pth"]


def hf_cache_root() -> Path:
    """transformers/huggingface_hub 이 실제로 보는 캐시 경로(우선순위 동일)."""
    if os.environ.get("HF_HUB_CACHE"):
        return Path(os.environ["HF_HUB_CACHE"])
    if os.environ.get("HF_HOME"):
        return Path(os.environ["HF_HOME"]) / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def model_id() -> str | None:
    """인젝션 런타임이 로드할 백본 모델 id(예: "LGAI-EXAONE/EXAONE-4.0-1.2B").

    런타임 스크립트가 extract_config.json 의 "model" 을 그대로
    AutoModelForCausalLM.from_pretrained 에 넘기므로 같은 값을 본다.
    """
    try:
        cfg = json.loads((config.INJECTION_ENGINE_DIR / "extract_config.json").read_text(encoding="utf-8"))
    except Exception:
        return None
    value = cfg.get("model")
    return value if isinstance(value, str) and value else None


def is_cached() -> bool:
    """백본이 이미 로컬에 있으면 True.

    개발 기기에는 이 캐시가 이미 있고, 사용자 홈(~/.cache/huggingface)에 있어 앱을
    재설치해도 지워지지 않는다 — 0.1.7~0.1.9 를 반복 설치하며 검증했는데도 캐시 없는
    기기의 실패가 드러나지 않았던 이유다.
    """
    mid = model_id()
    if not mid:
        return False
    # extract_config.json 이 로컬 경로를 직접 가리키는 경우(향후 번들 전환 대비)
    if Path(mid).is_dir():
        return True
    snapshots = hf_cache_root() / f"models--{mid.replace('/', '--')}" / "snapshots"
    if not snapshots.is_dir():
        return False
    # 스냅샷 폴더만 있고 config.json 이 없으면 받다 만 상태다 — 캐시로 치지 않는다.
    return any((s / "config.json").is_file() for s in snapshots.iterdir() if s.is_dir())


def total_download_bytes() -> int | None:
    """받아야 할 총 바이트. 진행률(%) 계산용이며, 실패하면 None(퍼센트 없이 진행)."""
    mid = model_id()
    if not mid:
        return None
    try:
        from fnmatch import fnmatch

        from huggingface_hub import HfApi

        info = HfApi().model_info(mid, files_metadata=True)
        total = 0
        for sibling in info.siblings or []:
            name = sibling.rfilename
            if any(fnmatch(name, pat) for pat in IGNORE_PATTERNS):
                continue
            total += sibling.size or 0
        return total or None
    except Exception:
        return None


def repo_cache_dir() -> Path | None:
    """이 백본이 캐시되는 디렉터리(<cache>/models--org--name). 로컬 경로 모델이면 None."""
    mid = model_id()
    if not mid or Path(mid).is_dir():
        return None
    return hf_cache_root() / f"models--{mid.replace('/', '--')}"


def downloaded_bytes() -> int:
    """지금까지 디스크에 실제로 받아진 바이트.

    진행률을 tqdm 훅이 아니라 디스크 실측으로 구하는 이유: tqdm_class 로 바이트
    진행바를 가로채는 방식은 huggingface_hub 버전·다운로드 백엔드에 따라 아예 안
    불린다(실사용자 macOS: 다운로드는 도는데 진행률이 계속 0%). 디스크에 쌓이는
    양은 어떤 백엔드를 쓰든 똑같이 늘어나므로 이쪽이 훨씬 안정적이다.

    심볼릭 링크는 세지 않는다 — HF 캐시는 blobs/ 에 실제 파일을 두고 snapshots/ 에서
    그걸 심볼릭 링크로 가리키는데(macOS/Linux), 둘 다 세면 두 배로 잡힌다. 심볼릭
    링크를 못 만드는 Windows 에서는 snapshots/ 에 실파일이 들어가고 blobs/ 가 비므로
    같은 계산이 그대로 맞는다(실측 확인).
    """
    root = repo_cache_dir()
    if root is None or not root.is_dir():
        return 0
    total = 0
    for path in root.rglob("*"):
        try:
            if path.is_symlink() or not path.is_file():
                continue
            total += path.stat().st_size
        except OSError:
            continue  # 다운로드 중 사라지는 임시 파일 등은 건너뛴다
    return total


def download() -> None:
    """백본을 HF 캐시로 내려받는다(블로킹 — 호출부가 스레드로 돌릴 것).

    진행 상황은 이 함수가 알려주지 않는다. 호출부가 downloaded_bytes() 를 주기적으로
    읽어 보고한다(위 주석 참고).
    """
    mid = model_id()
    if not mid:
        raise RuntimeError("extract_config.json 에서 백본 모델 id 를 읽지 못했습니다")

    from huggingface_hub import snapshot_download

    snapshot_download(mid, ignore_patterns=IGNORE_PATTERNS)
