# varco_ocr — 이미지 속 글자 검사 (선택 기능)

| 파일 | 설명 |
|---|---|
| `runtime/varco_ocr_inference.py` | 엔진이 띄우는 하위 프로세스(stdio JSON). 조각내기 → 묶어 읽기 → 칸·상자 잇기 → 쓰레기 거르기 |
| `models/` | NCSOFT VARCO-VISION-2.0-1.7B (Hugging Face 커밋 `ed09f37445518b1564d1ef3c6e26fbd7c1b2c818`, 약 4.3GB) — 설정에서 켜면 내려받는다 |
| `gpu_torch/` | Windows GPU 실행 환경 — NVIDIA면 torch 2.14.1+cu130, Intel Arc면 torch 2.14.1+xpu. 앱 파이썬의 CPU torch 대신 이 사이드카에만 PYTHONPATH로 앞세운다 |

- 라이선스: **CC BY-NC 4.0 (비상업)**. 바탕 LLM 은 Qwen3-1.7B, 비전 인코더는 SigLIP2.
- NVIDIA CUDA, Windows 11의 Intel Arc XPU, Apple Silicon MPS에서 돈다. 지원 GPU가 없으면 모델을 받지 않고,
  글자 검사 대상 이미지가 든 입력을 막는다. cu130은 NVIDIA 드라이버 580 이상, XPU는 최신 Intel 그래픽 드라이버가 필요하다.
- Apple Silicon(M1~M4)은 앱에 번들된 arm64 torch/torchvision을 쓰므로 별도 `gpu_torch/`를 받지 않는다.
  Intel Mac은 MPS가 없어 지원하지 않는다.
- 화면 캡처 기준으로 맞췄다(사진 문서는 지원하지 않음). 실험 기록은 사용자 PC `D:/Dev/hackathon/varco_ocr` (2026-10-25~26).
  - 그림 4장 + 캡처 34장 + 킨텍스 시연 문서 캡처 6장으로 확인. 원본 파일에서 잡히던 PII 111개 중 106개가 캡처에서도 가려졌다.
