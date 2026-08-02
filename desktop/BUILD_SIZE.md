# 설치 파일 용량 — 무엇이 들어가고, 무엇을 뺐나

`package.json` 은 주석을 달 수 없어서, `build.extraResources.filter` 와
`build.electronLanguages` 에 왜 그 항목들이 들어있는지를 여기에 적어둔다.

## 용량 구성

Windows 설치 프로그램(`UpSecurity-Setup-*.exe`)은 v0.1.1 이후 계속 **266.9MB** 로
고정이다. 계속 커진 게 아니라 한 번 뛰고 유지되고 있다:

| 버전 | Setup.exe | 비고 |
|---|---:|---|
| v0.1.0 | 163.3MB | |
| v0.1.1 | 266.5MB | `torch`/`transformers`/`accelerate` 를 엔진 의존성에 추가(+103MB) |
| v0.1.2 ~ v0.2.4 | 266.9MB | 변동 없음 |

설치 후 디스크(약 1.8GB)의 대부분은 번들 파이썬 환경이다:

| 항목 | 크기 |
|---|---:|
| `resources/engine/.venv` | 890MB (torch 466 · transformers 49 · pymupdf 48 · sympy 39 · pandas 33 …) |
| `UpSecurity.exe` (Electron 런타임) | 192MB |
| `locales/` | 42MB |
| 나머지 Electron 리소스 | 약 50MB |

모델 가중치(약 600MB)는 설치 파일에 없다 — 첫 실행 때 GitHub 릴리스에서 받는다.

## 뺀 것

전부 "런타임에 쓰이지 않는다"가 확인된 것만 뺐다.

| 제외 패턴 | 크기 | 왜 필요 없나 |
|---|---:|---|
| `**/site-packages/torch/lib/*.lib` | 39.1MB | C++ 확장을 **컴파일할 때만** 쓰는 Windows import 라이브러리. 실행에는 같은 폴더의 `.dll` 만 있으면 된다 |
| `**/site-packages/torch/include/**` | 37.4MB | libtorch C++ 헤더. 위와 같은 이유 |
| `**/site-packages/**/tests/**` | 32.6MB | 패키지들이 함께 배포하는 자체 테스트 스위트(pandas 12.5MB, numpy 4.9MB, sympy 2MB 등) |
| `**/tcl/**`, `**/DLLs/tcl*.dll`, `**/DLLs/tk*.dll`, `**/DLLs/_tkinter.pyd` | 10MB | tkinter(GUI 툴킷). 엔진은 콘솔/HTTP 로만 동작하고 tkinter 를 임포트하지 않는다 |
| `electronLanguages: ["ko","en","en-US"]` | 40.5MB | Electron 로케일 55개 중 실제로 쓰는 건 한국어·영어뿐 |

합계 약 **160MB**(압축 전). `electronLanguages` 는 electron-builder 25.x 에서
macOS 는 `*.lproj`, 그 외 플랫폼은 `locales/*.pak` 을 정리한다 — macOS 의 영어
번들은 `en.lproj` 라서 `en` 과 `en-US` 를 모두 넣어야 영어가 지워지지 않는다.

## 빼지 않은 것

- **`torch/lib/*.dll` (312MB)** — 실제 런타임 바이너리다. `torch_cpu.dll` 하나가 291MB.
- **pandas(33MB)** — `pyhwpx` 가 요구한다(HWP 파싱). sympy·networkx·jinja2·fsspec 은
  `torch`, cryptography 는 `pdfminer.six`, pillow 는 `pdfplumber`/`python-pptx` 가
  요구한다. 설치된 패키지 중 아무도 요구하지 않는 고아 패키지는 없다.
- **`UpSecurity.exe` (192MB)** — Electron 런타임 자체라 커스텀 빌드 없이는 못 줄인다.
- **`.venv` 안 `__pycache__`(67MB)** — 설치 파일에는 이미 없다(`!**/__pycache__/**`).
  설치 후 파이썬이 임포트하면서 생기는 것이라 배포본과 무관하다.

## 검증 방법

번들 venv 를 복사해 위 항목들을 지운 뒤, 데모 문서 2건으로 탐지 결과가 그대로인지
확인한다(설치본은 건드리지 않는다):

```
탐지 결과: 카드분쟁접수서 PII 26건/인젝션 2건, 고객지원리뷰 PII 33건/인젝션 1건
→ 트림 전후 동일해야 한다
```

실제로 890.5MB → 771.3MB 로 줄인 사본에서 위 결과가 그대로 나오는 것을 확인했다.
