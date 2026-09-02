# ppsfinding — 나라장터(G2B) 입찰공고 키워드 모니터링

조달청 나라장터의 **물품·용역 입찰공고**와 **사전규격(발주예고)** 을
`IoT / BEMS / FM용역 / 통합관제 / 에너지 / AI` 6개 키워드 축으로 자동 수집해
**Notion 데이터베이스에 적재**한다. 실행은 GitHub Actions 가 **평일 08:00(KST)에 1회** 담당한다.

```
GitHub Actions (cron: 평일 08:00 KST)
        │
        ├─ 1. 수집   config/sources.yaml  → 조달청 OpenAPI (data.go.kr)
        ├─ 2. 정규화 normalize.py         → 응답 필드명 차이를 흡수한 공통 레코드
        ├─ 3. 필터   config/keywords.yaml → 동의어 매칭 + 제외어 + 관련도 점수
        └─ 4. 적재   notion_sink.py       → Notion DB upsert (중복키 = 공고번호)
```

별도 서버·DB 없이 GitHub Actions 무료 러너와 Notion 만으로 동작한다.

---

## 1. 왜 웹 스크래핑이 아니라 OpenAPI 인가

조달청은 공공데이터포털에 [「조달청_나라장터 입찰공고정보서비스」(데이터 15129394)](https://www.data.go.kr/data/15129394/openapi.do)를
공식 개방하고 있고, **업무구분(물품/용역/공사/외자)마다 오퍼레이션이 분리**되어 있다.
`g2b.go.kr` 화면을 직접 크롤링하는 방식보다 안정적이고 약관 리스크가 없다.

관련 서비스(확장 여지):
[낙찰정보](https://www.data.go.kr/data/15129397/openapi.do) ·
[계약정보](https://www.data.go.kr/data/15129427/openapi.do) ·
[계약과정통합공개](https://www.data.go.kr/data/15129459/openapi.do)

### ⚠️ 아직 검증되지 않은 부분 (반드시 읽을 것)

`config/sources.yaml` 의 **오퍼레이션 경로와 날짜 파라미터명은 "후보"** 다.
이 저장소를 작성한 환경에서 `data.go.kr` 접근이 차단되어 API 문서 원문을 직접 열어
확인하지 못했기 때문이다. 추정으로 하나를 확정해 두는 대신, 다음과 같이 설계했다.

- 각 소스는 여러 **variant**(경로 + 날짜 파라미터 조합)를 가진다.
- 클라이언트가 순서대로 1건씩 호출해 보고, **최초로 정상 응답(resultCode `00`)한 조합**을 그 실행 동안 사용한다.
- `probe` 명령으로 어떤 조합이 살아있는지 먼저 확인할 수 있다.

**최초 세팅 시 `API 엔드포인트 진단` 워크플로를 반드시 한 번 돌리고**,
살아있는 variant 만 남기고 나머지는 `config/sources.yaml` 에서 지우면 매 실행 호출 수가 줄어든다.
특히 **사전규격(`prestd_*`) 소스는 오퍼레이션명 근거가 가장 약하므로** 진단 결과에 따라 수정이 필요할 가능성이 높다.

---

## 2. 준비물

| 항목 | 발급처 | GitHub Secret 이름 |
|---|---|---|
| 조달청 OpenAPI 인증키 | data.go.kr 회원가입 → 「조달청_나라장터 입찰공고정보서비스」 활용신청 → **일반 인증키(Decoding)** | `G2B_SERVICE_KEY` |
| Notion 인테그레이션 토큰 | notion.so/my-integrations → New integration → Internal Integration Secret | `NOTION_TOKEN` |
| Notion 데이터베이스 ID | 대상 DB 페이지 URL 의 32자리 hex | `NOTION_DATABASE_ID` |

> **인증키 주의**: 포털은 Encoding/Decoding 두 가지 키를 준다. 이 코드는 `requests` 가 직접
> URL 인코딩하므로 반드시 **Decoding 키**를 넣어야 한다. Encoding 키를 넣으면 이중 인코딩되어
> `SERVICE_KEY_IS_NOT_REGISTERED_ERROR` 가 난다.
>
> **Notion 연결 주의**: 토큰을 만든 뒤 **대상 데이터베이스 페이지에서 `···` → 연결 → 해당 인테그레이션을 추가**해야 한다.
> 이 단계를 빠뜨리면 `object_not_found` 가 난다.
>
> **DB ID 찾는 법**: DB 를 전체 페이지로 열었을 때 URL `https://www.notion.so/<workspace>/<32자리hex>?v=...` 의 32자리 hex.

Secrets 등록: 저장소 → Settings → Secrets and variables → Actions → New repository secret

---

## 3. Notion DB 스키마

| 속성 | 타입 | 설명 |
|---|---|---|
| 공고명 | Title | 입찰공고명 (사전규격은 품명) |
| 공고번호 | Text | **중복 판정 키.** 입찰공고=`공고번호-차수`, 사전규격=`PS-등록번호` |
| 업무구분 | Select | 물품 / 용역 / 사전규격 |
| 공고종류 | Text | 원문 표기 그대로 |
| 수요기관 / 공고기관 | Text | |
| 공고일시 / 입찰마감일시 / 개찰일시 | Date | KST(+09:00) ISO-8601. 파싱 불가하면 **비워 둠**(임의 추정 안 함) |
| 추정가격 | Number (₩) | 원문에 없으면 비움 |
| 계약방법 | Text | |
| 매칭키워드 | Multi-select | 걸린 키워드 축 |
| 관련도 | Number | 가중치 합산 점수. 정렬 기준 |
| 검토상태 | Select | 신규 → 검토중 → 제안준비 → 참여 / 미참여 |
| 담당자 | Person | 수동 배정용 |
| 공고링크 | URL | 원문에 상세 URL 이 있을 때만 채움 |
| 참조번호 | Text | |
| 수집일시 | Created time | 중복 판정 조회 범위 기준 |
| 비고 | Text | 수동 메모용 |

---

## 4. 키워드 사전 (`config/keywords.yaml`)

키워드는 **2단 필터**로 동작한다.

1. **서버 측 1차 필터** — `search_terms` 를 API 의 공고명 부분일치 파라미터로 넘겨 조회한다.
2. **로컬 2차 필터** — 받아온 공고명·수요기관을 `terms` 에 대조해 점수를 매기고, `min_score` 미만은 버린다.

| 축 | 대표 확장어 | 오탐 방지 |
|---|---|---|
| IoT | 사물인터넷, 스마트센서, LoRa, NB-IoT | `IoT` 는 영문 단어 경계 검사 (BIOTECH 등 오탐 차단) |
| BEMS | FEMS, 건물에너지관리, 에너지관리시스템 | `EMS` 는 응급/구급 제외 |
| FM용역 | 시설관리, 시설물관리, 종합관리용역, 유지관리 | `FM` 은 라디오·주파수·방송국 제외 |
| 통합관제 | 관제센터, 관제시스템, 스마트관제 | 항공/해상/교통관제 제외 |
| 에너지 | 에너지진단, 에너지절감, ESCO, 신재생, 태양광 | 단독 `에너지` 는 weight 1 이라 혼자서는 통과 못 함 |
| AI | 인공지능, 머신러닝, 딥러닝, 지능형 | 조류인플루엔자(AI)·가금 전역 제외 |

- **점수 규칙**: 같은 단어는 1회만 가산 → 축 점수 = 매칭된 term weight 합 → 총점 = 축 점수 합
- **통과 조건**: 총점 ≥ `min_score`(기본 3) **그리고** 매칭 축 수 ≥ `min_axes`(기본 1)
- **표기 흔들림 흡수**: `통합 관제`, `통합·관제` 처럼 공백/구분기호가 낀 표기도 잡는다
- **전역 제외어**: 급식·청소용역·경비용역·제초·방역소독 등은 공고 자체를 버린다

튜닝은 YAML 만 고치면 되고, 코드 수정이 필요 없다.
`min_score` 를 올리면 정밀도가, 내리면 재현율이 올라간다.

---

## 5. 실행

### GitHub Actions

| 워크플로 | 트리거 | 용도 |
|---|---|---|
| `입찰공고 수집` | cron `0 23 * * 0-4` (= 평일 08:00 KST) + 수동 | 실제 수집·적재 |
| `API 엔드포인트 진단` | 수동 | 살아있는 오퍼레이션 조합 확인 |
| `테스트` | push / PR | pytest |

수동 실행 시 `days`(조회 기간), `mode`, `dry_run` 을 지정할 수 있다.

### 로컬

```bash
pip install -r requirements.txt
export PYTHONPATH=src
export G2B_SERVICE_KEY='...'          # Decoding 키
export NOTION_TOKEN='secret_...'
export NOTION_DATABASE_ID='...'

# 1) 어떤 오퍼레이션이 살아있는지 + 응답 필드 키 확인
python -m g2b_watch.cli probe --dump

# 2) Notion 에 쓰지 않고 결과만 확인
python -m g2b_watch.cli collect --days 3 --no-notion

# 3) 실제 적재
python -m g2b_watch.cli collect --days 2
```

주요 옵션

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `--days` | 2 | 조회 기간. 실행 누락 대비로 하루보다 넉넉히 잡는다 |
| `--mode` | keyword | `keyword`=검색어별 호출 / `full`=기간 전체 스캔 후 로컬 필터 |
| `--dedup-days` | 14 | 중복 판정에 볼 Notion 최근 적재 범위 |
| `--min-score` | — | YAML 값을 임시로 덮어씀 (튜닝용) |
| `--limit` | — | 관련도 높은 순 상위 N건만 적재 |
| `--dry-run` | off | Notion 조회는 건너뛰고 쓰기만 생략 |
| `--no-notion` | off | Notion 단계를 아예 건너뜀 |
| `--json-out` | — | 결과를 JSON 파일로도 저장 |

`keyword` 모드는 호출 수가 `검색어 23개 × 소스 4개`로 늘지만 전송량이 작다.
`full` 모드는 호출 수가 적은 대신 기간 내 전체 공고를 받아 로컬에서 거른다.
포털의 일일 트래픽 한도(개발계정은 제한이 있음)에 걸리면 `full` 로 바꾸는 것을 검토한다.

---

## 6. 중복 방지

로컬 상태 파일을 두지 않고 **Notion DB 자체를 원장**으로 삼는다.
적재 직전에 최근 `--dedup-days` 일 안에 생성된 페이지의 `공고번호` 를 모두 읽어 집합을 만들고,
거기에 없는 건만 생성한다. 워크플로가 두 번 돌아도 같은 공고가 두 줄 생기지 않는다.

---

## 7. 트러블슈팅

| 증상 | 원인 / 조치 |
|---|---|
| `SERVICE_KEY_IS_NOT_REGISTERED_ERROR` | Encoding 키를 넣었거나 활용신청 승인 전. **Decoding 키**로 교체 |
| `사용 가능한 오퍼레이션을 찾지 못했습니다` | `probe --dump` 실행 후 `config/sources.yaml` 의 variants 수정 |
| Notion `object_not_found` | 인테그레이션을 대상 DB 에 연결하지 않았음 |
| Notion `validation_error … is not a property that exists` | DB 속성 이름을 바꿨다. `notion_sink.py` 의 속성명과 맞출 것 |
| 결과가 0건 | `--min-score 1 --no-notion` 으로 점수 분포부터 확인 |
| 노이즈가 많음 | `keywords.yaml` 의 `exclude` / `exclude_global` 보강, `min_score` 상향 |
| `max_pages 도달` 경고 | `config/sources.yaml` 의 `max_pages` 상향 |

---

## 8. 구조

```
config/
  sources.yaml      API 엔드포인트 후보 + 호출 설정
  keywords.yaml     키워드 축 · 가중치 · 제외어
src/g2b_watch/
  config.py         YAML → 타입 있는 설정 객체
  g2b_client.py     JSON/XML 양쪽 파싱, variant 탐색, 페이징, 재시도
  normalize.py      응답 필드명 후보 매핑 → 공통 레코드
  matcher.py        키워드 매칭 · 점수 계산
  notion_sink.py    중복 조회 + 페이지 생성
  cli.py            probe / collect
tests/              네트워크 없이 도는 단위·통합 테스트 21건
```

---

## 9. 다음 단계 후보

- 낙찰정보 API 연동 → 경쟁사·낙찰가율 분석
- 사전규격 → 입찰공고 자동 연결(같은 사업의 진행 단계 추적)
- 첨부 규격서 다운로드 및 본문 키워드 매칭(제목만으로는 놓치는 건 보완)
- 알림 채널(메일/Slack) 추가 — 현재는 Notion 적재만
