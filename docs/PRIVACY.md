# Campfire 개인정보처리방침

최종 수정: 2026-09-22 · 대상: Chrome 확장 프로그램 "Campfire" 및 함께 쓰는 Campfire 데스크톱 앱

Campfire는 AI 서비스로 무언가를 **보내기 전에** 개인정보(PII)와 프롬프트 인젝션을 찾아
가려주는 로컬 게이트웨이입니다. 그래서 확장은 사용자가 보내려는 내용을 **읽습니다**.
이 문서는 그 내용이 어디로 가고 어디에 남는지를 적은 것입니다.

## 한 줄 요약

검사 대상 내용은 **사용자 PC 안(`127.0.0.1`)에서만** 처리됩니다. 개발자 서버는 없고,
광고·분석·추적 도구를 넣지 않았으며, 어떤 데이터도 판매하거나 제3자에게 넘기지 않습니다.

## 1. 확장이 읽는 것

지원 사이트(chatgpt.com, claude.ai, gemini.google.com, copilot.microsoft.com, grok.com,
perplexity.ai)에서만 동작하며, 그 사이트에서 다음을 읽습니다.

- 사용자가 입력창에 넣고 전송하려는 **프롬프트 본문**
- 사용자가 **첨부하려는 파일의 내용과 파일명**
- 그 밖에 검사 결과를 화면에 보여주는 데 필요한 최소한의 페이지 요소

읽는 시점은 "사용자가 전송/첨부를 시도한 순간"이며, 그 외 시간에 페이지를 훑거나
방문 기록을 수집하지 않습니다. 지원 목록에 없는 사이트에서는 아무것도 하지 않습니다.

## 2. 그 내용이 가는 곳

읽은 내용은 같은 PC에서 실행 중인 Campfire 데스크톱 앱의 로컬 엔진
(`http://127.0.0.1:48200`~`48209`)으로만 전송되어 검사·마스킹됩니다.
확장 코드에는 이 로컬 주소 외의 전송 대상이 없습니다.

데스크톱 앱이 인터넷에 접속하는 경우는 다음뿐입니다.

- **탐지 모델 다운로드**(최초 1회). 모델 파일만 내려받고, 사용자 데이터를 올리지 않습니다.
- **선택 기능 — 인젝션 위치 정밀 특정**: 사용자가 직접 Upstage API 키를 설정한 경우에만
  켜지고, 그때는 1단계에서 이미 "인젝션 의심"으로 판정된 **해당 구간**이 Upstage
  (`api.upstage.ai`)로 전송됩니다. **배포되는 설치 파일에는 이 키가 들어 있지 않으므로
  기본 상태에서는 꺼져 있고**, 외부로 나가는 내용도 없습니다.

## 3. 남는 것 (보관)

- **로컬 검사 기록(SQLite)**: 데스크톱 앱이 대시보드 통계를 위해 PC 안에 검사 이력을
  저장합니다. 저장 항목은 작업 ID·파일명·출처·시각·상태·탐지 건수·유형별 개수·
  탐지 위치(오프셋)로, **원문 텍스트나 그 스니펫은 저장하지 않습니다.**
- **확장 설정(`chrome.storage.local`)**: 보호 on/off 등 설정값.
- **세션 캐시(`chrome.storage.session`)**: 탐지된 로컬 엔진 주소·포트. 브라우저를 닫으면 사라집니다.

모두 사용자 PC에만 있습니다. 삭제는 앱 제거 및 확장 삭제로 이루어집니다.

## 4. 하지 않는 것

- 개발자 또는 제3자 서버로 사용자 데이터 전송 — 하지 않습니다
- 데이터 판매·양도, 광고·신용평가 등 목적 외 사용 — 하지 않습니다
- 분석/추적 SDK, 원격 코드 로딩(`eval`, 외부 스크립트) — 넣지 않았습니다
- 방문 기록·검색 기록 수집 — 하지 않습니다

## 5. 권한을 왜 요구하는가

| 권한 | 이유 |
|---|---|
| `activeTab`, `scripting` | 사용자가 전송을 시도한 그 탭에서 내용을 가로채고 마스킹 결과를 되돌려 넣기 위해 |
| `sidePanel` | 검사 결과를 보여주고 전송/취소를 사용자가 직접 결정(HITL)하게 하기 위해 |
| `storage` | 보호 on/off 설정과 로컬 엔진 주소 캐시 저장 |
| host: `127.0.0.1`, `localhost` | 로컬 엔진에 검사를 요청하기 위해 (외부 서버 아님) |
| host: 6개 AI 서비스 도메인 | 그 사이트에서만 전송 전 검사를 걸기 위해 |

## 6. 문의

<연락처 이메일> · 이슈: https://github.com/theagares/campfire/issues

---

## English summary

Campfire is a local-first gateway that detects and masks personal data and prompt
injection **before** you send anything to an AI service. The extension therefore reads
the prompt text and file contents you are about to submit on six supported AI sites.

That content is sent **only** to a local engine on your own machine
(`http://127.0.0.1:48200`-`48209`). There is no developer server, no analytics, no ads,
no tracking, no remotely hosted code, and no sale or transfer of data to third parties.

The desktop app reaches the internet only to download detection models (no user data is
uploaded), and — **only if the user supplies their own Upstage API key, which shipped
builds do not contain** — to ask an external LLM to pinpoint an already-flagged injection
span. Local scan history is stored in SQLite as metadata only (job id, file name, source,
timestamp, counts, offsets); **no raw text or text snippets are stored.**

Contact: <contact email> · https://github.com/theagares/campfire/issues
