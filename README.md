# ppsfinding — FM Bid Intelligence

조달청 나라장터의 **물품·용역 입찰공고**를 수집해 **우리 회사가 실제로 수주 검토할 가치가 있는
공고를 자동 선별**하고 Notion 에 적재한다. 단순 키워드 검색기가 아니라 **참가자격 판정 + 수주검토
점수**까지 내는 것이 목적이다. 실행은 GitHub Actions 가 **평일 08:00(KST)에 1회** 담당한다.

```
GitHub Actions (cron: 평일 08:00 KST)
        │
        ├─ 1. 수집   config/sources.yaml   → 조달청 OpenAPI (입찰공고 PPSSrch)
        ├─ 2. 정규화 normalize.py          → 공통 레코드 (개인정보 필드 제외)
        ├─ 3. 분류   config/keywords.yaml  → 동의어 + 제외어 + 의미조합 → 관련도
        ├─ 4. 보강   enrich.py             → 참가가능지역 · 면허제한 (보조 오퍼레이션)
        ├─ 5. 판정   qualification.py      → 🟢참여가능 / 🟡검토필요 / 🔴참여불가 + 사유코드
        ├─ 6. 점수   scoring.py            → 수주검토 점수 100점
        └─ 7. 적재   notion_sink.py        → Notion DB upsert (중복키 = 공고번호)
```

**참여불가 공고도 버리지 않는다.** 경쟁사 동향·시장 규모·발주 트렌드를 읽는 자료가 되기 때문이다.

별도 서버·DB 없이 GitHub Actions 무료 러너와 Notion 만으로 동작한다.

---

## 1. 왜 웹 스크래핑이 아니라 OpenAPI 인가

조달청은 공공데이터포털에 [「조달청_나라장터 입찰공고정보서비스」(데이터 15129394)](https://www.data.go.kr/data/15129394/openapi.do)를
공식 개방한다. `g2b.go.kr` 화면을 직접 크롤링하는 방식보다 안정적이고 약관 리스크가 없다.

### 사용 중인 오퍼레이션 (명세 확정됨)

「조달청 공공데이터 개방 OpenAPI 참고자료 v1.2」(2026.04.10) 원문 기준이다.

| 항목 | 값 |
|---|---|
| 서비스 ID | `BidPublicInfoService` (버전 3.1) |
| 엔드포인트 | `http://apis.data.go.kr/1230000/ad/BidPublicInfoService` (**https 아님**) |
| 인증 파라미터 | `ServiceKey` (**대문자 S**) |
| 물품 | `getBidPblancListInfoThngPPSSrch` |
| 용역 | `getBidPblancListInfoServcPPSSrch` |
| 조회구분 | `inqryDiv=1` (공고게시일시 기준) |
| 기간 | `inqryBgnDt` / `inqryEndDt`, `YYYYMMDDHHMM` |
| 키워드 | `bidNtceNm` (공고명 **부분일치**) |
| 처리 한도 | 30 tps |
| 전송 암호화 | 명세서 표기 **SSL 없음** — 443 포트는 연결 타임아웃 |

문서상 업무구분별로 두 계열이 있다.

- `getBidPblancListInfo{Thng,Servc}` — 등록일시·공고번호·변경일시로만 조회. **키워드 검색 불가**
- `getBidPblancListInfo{Thng,Servc}PPSSrch` — 위에 더해 공고명·기관명·추정가격 범위·참가제한지역 등 지원

키워드 모니터링이 목적이므로 **PPSSrch 계열**을 쓴다.

### ⚠️ 사전규격(발주예고)은 아직 비활성

제공된 명세서의 오퍼레이션 25종 어디에도 사전규격 목록 조회가 없다.
「조달청_나라장터 사전규격정보서비스」라는 **별도 서비스**로 분리되어 있어,
그 서비스의 활용신청과 명세서를 받기 전까지 `config/sources.yaml` 의 `prestd` 소스는 `enabled: false` 다.

입찰공고 응답에 사전규격등록번호(`bfSpecRgstNo`)가 들어 있어, 나중에 두 소스를 연결하는 것은 가능하다.

### 확장 여지

[낙찰정보](https://www.data.go.kr/data/15129397/openapi.do) ·
[계약정보](https://www.data.go.kr/data/15129427/openapi.do) ·
[계약과정통합공개](https://www.data.go.kr/data/15129459/openapi.do)

---

## 2. 준비물

| 항목 | 발급처 | GitHub Secret 이름 |
|---|---|---|
| 조달청 OpenAPI 인증키 | data.go.kr 회원가입 → 「조달청_나라장터 입찰공고정보서비스」 활용신청 → 일반 인증키 (Encoding/Decoding 무관) | `G2B_SERVICE_KEY` |
| Notion 인테그레이션 토큰 | notion.so/my-integrations → New integration → Internal Integration Secret | `NOTION_TOKEN` |
| Notion 데이터베이스 ID | 대상 DB 페이지 URL 의 32자리 hex | `NOTION_DATABASE_ID` |

> **인증키**: 포털은 같은 키를 Encoding / Decoding 두 형태로 보여준다. **둘 중 아무거나 넣어도 된다.**
> 클라이언트가 퍼센트 인코딩된 키를 감지하면 디코딩해서 쓰기 때문에, Encoding 키를 넣어도
> 이중 인코딩(`%2B` → `%252B`)으로 인한 `SERVICE_KEY_IS_NOT_REGISTERED_ERROR` 가 나지 않는다.
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
| **참여여부** | Select | **참여가능 / 검토필요 / 참여불가** — 판정 엔진 결과 |
| **수주검토점수** | Number | **Opportunity Score 100점 만점.** 70점 이상이 영업 우선검토 |
| **제한사유** | Multi-select | R01~R10 코드 |
| **판정근거** | Text | 각 코드가 붙은 이유 |
| **참가가능지역** | Text | 참가가능지역정보조회 결과 |
| **요구면허** | Text | 면허제한정보조회 결과 |
| **D-Day** | Formula | 입찰마감까지 남은 일수 |
| **영업의견** | Text | 검토 담당자 메모 |
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
3. **의미 조합 3차 필터(`combos`)** — 단어 하나로는 안 잡히는 사업을 조합으로 잡는다.

> `○○연구원 전기·기계·소방·승강기 운영관리 위탁용역` 에는 **FM 이라는 단어가 없다.**
> 하지만 전형적인 시설관리 사업이다. `(기계|전기|소방|승강기|…) AND (유지관리|운영관리|위탁|…)`
> 조합 규칙으로 FM용역 축에 +5 를 준다. 현재 조합 규칙은 설비유지관리 · 건물종합관리 · 스마트시설 3개.

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

## 4-2. 참가자격 판정 (`config/company.yaml`, `config/rules.yaml`)

### ⚠️ 먼저 회사 프로필을 채워야 한다

`config/company.yaml` 은 지금 **자리표시자**다. 여기가 비어 있으면 판정이 무의미하거나 틀린다.

```yaml
company:
  name: "(회사명 입력)"
  scale: large_enterprise        # large_enterprise | middle_standing | sme | small
  is_software_business: false
  headquarters_region: "경기도"
  branch_regions: []
  licenses: []                   # 예) 시설물유지관리업, 정보통신공사업, 전기공사업
```

`licenses` 가 비어 있으면 면허 판정을 **아예 건너뛴다**(설정 미완료를 오판으로 만들지 않기 위해).

### 판정 규칙과 근거

| 코드 | 사유 | 근거 필드 / 소스 | 판정 |
|---|---|---|---|
| R01 | 지역제한 | `getBidPblancListInfoPrtcptPsblRgn` 의 `prtcptPsblRgnNm` | 불일치 시 **참여불가** |
| R02 | 대기업 참여제한 | `infoBizYn`(정보화사업) + 회사 규모 | 항상 **검토필요** |
| R03 | 중소기업자간 경쟁 | 공고명·계약방법·낙찰방법 문자열 | 대기업·중견이면 **참여불가** |
| R04 | 소기업·소상공인 제한 | 동일 | 대기업·중견이면 **참여불가** |
| R05 | 면허 미보유 | `indstrytyLmtYn` + `getBidPblancListInfoLicenseLimit` | 불일치 **참여불가** / 목록 미확보 **검토필요** |
| R07 | 실적 제한 | `arsltCmptYn` | **검토필요** |
| R09 | 공동수급 조건 | `jntcontrctDutyRgnNm1~3` | 의무지역 불일치 시 **검토필요** |
| R10 | 기타 참가자격 제한 | `dsgntCmptYn`(지명경쟁) / `bidPrtcptLmtYn` | 지명경쟁 **참여불가**, 그 외 **검토필요** |

**R02 를 참여불가로 처리하지 않는 이유**: 정보화사업 대기업 참여제한은 사업 유형·금액·예외 인정
여부에 따라 달라지고, 대규모·복잡한 시스템 통합처럼 대기업 참여가 불가피한 사업으로 인정되는
규정도 있다. Boolean 하나로 결론 낼 수 없어 사람이 확인하도록 남긴다.

**지역제한 판정에 보조 API 가 필요한 이유**: 입찰공고 목록 응답에는 "지역제한이 걸려 있다"는
신호(`rgnLmtBidLocplcJdgmBssNm`)만 있고 **어느 지역인지는 없다.** 그래서 참가가능지역정보조회를
따로 부른다. 두 보조 오퍼레이션 모두 `inqryDiv=1` 로 기간 일괄 조회가 되므로 공고 건별 호출이
아니라 **기간당 2회**만 추가된다. `--no-enrich` 로 끌 수 있다.

지사 소재지는 공고가 **지사투찰을 허용(`brffcBidprcPermsnYn=Y`)할 때만** 인정한다.

## 4-3. 수주검토 점수 (Opportunity Score)

| 항목 | 배점 | 산출 |
|---|---:|---|
| 사업 적합성 | 30 | 키워드 관련도 / `business_fit_full_score` 정규화 |
| 사업영역 적합 | 20 | 매칭 축 중 `company.business_areas` 에 속하는 비율 |
| 사업규모 | 15 | sweet spot 구간 만점, `min_krw` 미만 0점, 가격 미상 절반 |
| 지역 | 10 | 지역제한 없음 또는 우리 지역 포함 시 만점 |
| 기술 적합성 | 10 | IoT / BEMS / 통합관제 / AI 축에 걸리면 만점 |
| 참가자격 | 15 | 참여가능 15 / 검토필요 7 / 참여불가 0 |
| **합계** | **100** | `review_threshold`(기본 70) 이상이 영업 우선검토 |

배점은 `config/rules.yaml` 에서 조정한다. 합계가 100 이 아니면 로딩 시 오류로 잡는다.

---

## 5. 실행

### ⚠️ 실행 위치 — GitHub 호스티드 러너에서는 조달청 API 에 닿지 않는다

2026-09-03 진단 결과, GitHub 호스티드 러너에서 `apis.data.go.kr` 의 **80·443 포트 모두 TCP
연결 타임아웃**이다. 인증·경로 문제가 아니라 네트워크 도달 자체가 안 된다.

```
네트워크 진단 — apis.data.go.kr
  http  :80   타임아웃 (방화벽/미개방 가능성)
  https :443  타임아웃 (방화벽/미개방 가능성)
```

따라서 수집 워크플로는 **국내 네트워크에서 실행**해야 한다. 선택지:

| 방식 | 비용 | 특징 |
|---|---|---|
| GitHub Actions **self-hosted runner** (국내 PC/서버) | 0원 | 워크플로 파일 그대로. `runs-on` 만 교체. PC 가 켜져 있어야 함 |
| 국내 VPS + cron | 월 수천원~ | 상시 동작. 워크플로 대신 cron 으로 `python -m g2b_watch.cli collect` |
| 사내 서버 / NAS 스케줄러 | 0원 | 사내 방화벽 정책 확인 필요 |

`probe` 명령이 실행 첫머리에 80/443 도달 여부를 출력하므로, 옮긴 환경에서 먼저 확인하면 된다.

### GitHub Actions

| 워크플로 | 트리거 | 용도 |
|---|---|---|
| `입찰공고 수집` | cron `0 23 * * 0-4` (= 평일 08:00 KST) + 수동 | 실제 수집·적재 |
| `API 엔드포인트 진단` | 수동 | 살아있는 오퍼레이션 조합 확인 |
| `테스트` | push / PR | pytest |

수동 실행 시 `days`(조회 기간), `mode`, `dry_run` 을 지정할 수 있다.

Secrets 가 하나라도 비어 있으면 수집 단계를 건너뛴다. **스케줄 실행은 경고만 남기고 성공** 처리하고
(세팅 전까지 매일 아침 CI 가 빨개지지 않도록), **수동 실행은 어떤 Secret 이 없는지 알리고 실패**시킨다.

### 로컬

```bash
pip install -r requirements.txt
export PYTHONPATH=src
export G2B_SERVICE_KEY='...'          # Encoding/Decoding 무관
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
| `SERVICE_KEY_IS_NOT_REGISTERED_ERROR` | 활용신청이 아직 승인되지 않았거나 키를 잘못 복사함. 포털 마이페이지에서 승인 상태 확인 |
| `사용 가능한 스킴이 없습니다` / 80·443 모두 타임아웃 | **호스트에 네트워크로 도달할 수 없다.** GitHub 호스티드 러너(해외 IP)에서 `apis.data.go.kr` 접속이 되지 않는 것으로 확인됐다. 아래 «실행 위치» 참고 |
| `사용 가능한 오퍼레이션을 찾지 못했습니다` | `probe --dump` 실행 후 `config/sources.yaml` 의 variants 수정 |
| Notion `object_not_found` | 인테그레이션을 대상 DB 에 연결하지 않았음 |
| Notion `validation_error … is not a property that exists` | DB 속성 이름을 바꿨다. `notion_sink.py` 의 속성명과 맞출 것 |
| 수집 워크플로가 "수집 건너뜀" 경고만 남기고 끝남 | Secrets 미등록. 위 2절대로 3개를 등록하면 다음 실행부터 정상 동작 |
| 결과가 0건 | `--min-score 1 --no-notion` 으로 점수 분포부터 확인 |
| 노이즈가 많음 | `keywords.yaml` 의 `exclude` / `exclude_global` 보강, `min_score` 상향 |
| `max_pages 도달` 경고 | `config/sources.yaml` 의 `max_pages` 상향 |

---

## 8. 구조

```
config/
  sources.yaml      API 엔드포인트 + 호출 설정
  keywords.yaml     키워드 축 · 가중치 · 제외어 · 의미조합
  company.yaml      회사 프로필 (⚠️ 값 입력 필요)
  rules.yaml        제한 코드 · 판정 규칙 · 점수 배점
src/g2b_watch/
  config.py         sources/keywords → 타입 있는 설정 객체
  company.py        company/rules → 타입 있는 설정 객체
  g2b_client.py     JSON/XML 파싱, 인증키 정규화, 페이징, 재시도
  normalize.py      응답 → 공통 레코드 (개인정보 필드 제외)
  matcher.py        키워드 · 의미조합 매칭 및 관련도
  enrich.py         참가가능지역 · 면허제한 보조 조회
  qualification.py  참가자격 판정 엔진
  scoring.py        수주검토 점수
  notion_sink.py    중복 조회 + 페이지 생성
  cli.py            probe / collect
tests/              네트워크 없이 도는 단위·통합 테스트 48건
```

---

## 9. 다음 단계 후보

- **사전규격정보서비스 연동** — 별도 활용신청 + 명세 확보 후 `prestd` 소스 활성화
- **LLM 분류(3차)** — 규칙으로 못 잡는 공고를 LLM 이 FM/ENERGY/IoT/BEMS/ICT 로 분류
- **AI 공고 요약** — 사업 개요·주요 업무·리스크·추천을 자동 생성
- 낙찰정보 API 연동 → 경쟁사·낙찰가율·발주처 분석
- 첨부 규격서 다운로드 및 본문 키워드 매칭(제목만으로는 놓치는 건 보완)
- Streamlit 대시보드
- 알림 채널(메일/Slack) 추가 — 현재는 Notion 적재만
