# pykrx MCP — 배포 가이드

한국거래소(KRX) 시세를 Claude 커스텀 커넥터로 노출하는 **읽기 전용** 원격 MCP 서버.

## 왜 필요한가

| 지금 문제 | 이 서버가 해결 |
|---|---|
| `0219E0` 등 영문 섞인 코드를 KIS가 못 읽음 | ✅ 네이티브 조회 |
| KIS 일봉 100건 제한 → 지수 차트가 약 5개월에서 끊김 | ✅ 12개월 전체 OHLCV |
| 국내 수급(개인·외국인·기관) 데이터 없음 | ✅ 투자자별 순매매 |
| 공매도 데이터 없음 | ✅ 거래량·잔고 |
| 한경 web_fetch 단일 장애점 | ✅ 한경은 백업으로 강등 |

## 제공 도구 (8개)

| 도구 | 설명 |
|---|---|
| `health` | 서버·pykrx 상태 확인 |
| `get_quote(code)` | 최근 거래일 시세 + 52주 고저 + NAV |
| `get_ohlcv(code, months=12)` | 일봉 이력 (최대 24개월) |
| `get_index_ohlcv(index, months=12)` | KOSPI / KOSDAQ / KOSPI200 지수 일봉 |
| `get_investor_trading(code, days=20)` | 투자자별 순매매 |
| `get_short_selling(code, days=20)` | 공매도 거래량·잔고 |
| `get_etf_detail(code, days=20)` | ETF NAV·괴리율·추적오차 |
| `search_ticker(query)` | 종목명으로 코드 검색 (주식+ETF) |

모든 응답에 `as_of` 또는 `date` 가 들어갑니다. **브리핑에서 기준일 없이 숫자만 쓰지 마세요.**

---

## 배포 (Railway)

### 1. GitHub 저장소 만들기

이 폴더(`_mcp/pykrx-mcp`) 전체를 새 GitHub 저장소에 올리세요. **private 로 두세요** —
토큰은 코드에 없지만 굳이 공개할 이유가 없습니다.

```
server.py
requirements.txt
Procfile
railway.json
README.md
```

### 2. Railway 프로젝트 생성

1. [railway.app](https://railway.app) 로그인 → **New Project** → **Deploy from GitHub repo**
2. 방금 만든 저장소 선택
3. Nixpacks가 `requirements.txt` 를 보고 Python 환경을 자동 구성합니다

### 3. 환경 변수 설정

Railway 프로젝트 → **Variables** 탭:

| 변수 | 필수 | 값 |
|---|---|---|
| `MCP_TOKEN` | ✅ **필수** | 길고 무작위한 문자열. 아래 생성법 참고 |
| `ALLOWED_HOSTS` | 선택 | 비워두면 DNS 리바인딩 보호 off (Bearer 인증은 그대로 적용). 지정 시 `xxx.up.railway.app` |
| `KRX_ID` / `KRX_PW` | 선택 | KRX 계정. 없으면 익명 요청으로 동작 (아래 참고) |

토큰 생성 (PowerShell):

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

### 4. 공개 도메인 발급

Railway → **Settings** → **Networking** → **Generate Domain**.
`https://<이름>.up.railway.app` 형태의 URL이 나옵니다.

### 5. 동작 확인

```powershell
curl https://<이름>.up.railway.app/healthz
# → {"ok":true,"kst":"..."}
```

### 6. Claude 커스텀 커넥터 등록

1. Claude → **Customize > Connectors** → **+ Add** → **Add custom connector**
2. 이름: `pykrx 한국시장`
3. URL: `https://<이름>.up.railway.app/mcp`   ← **끝에 `/mcp` 를 반드시 붙이세요**
4. 로그인 방식: **No sign in**
5. **Request headers** 에 추가:
   - Key: `Authorization`
   - Value: `Bearer <MCP_TOKEN 값>`
6. **Add**

### 7. 검증

새 대화에서 `health` 를 호출해 `ok: true` 가 나오는지, 그다음
`get_quote("0219E0")` 로 실제 가격이 오는지 확인하세요.

---

## 알아둘 것

### KRX 계정 (`KRX_ID` / `KRX_PW`)

pykrx 1.2.9 부터 KRX 로그인 세션을 지원합니다. 설정하지 않으면 시작 시
`KRX 로그인 실패: KRX_ID 또는 KRX_PW 환경 변수가 설정되지 않았습니다.` 가
로그에 찍히지만 **서버는 정상 동작합니다** — 익명 요청으로 폴백합니다.

다만 KRX가 비로그인 접근을 조이는 추세라, 안정적으로 쓰려면
[krx.co.kr](https://www.krx.co.kr) 무료 계정을 만들어 넣어두는 쪽을 권합니다.
익명 요청이 갑자기 빈 응답을 주기 시작하면 이게 원인입니다.

### 콜드 스타트

Railway는 유휴 시 인스턴스를 재우지 않으므로 브리핑이 타임아웃될 걱정은 적습니다.
다만 무료 크레딧(월 $5)을 넘으면 중단되니 사용량을 확인하세요.
Render 무료 티어로 옮기면 15분 유휴 후 잠들어 **첫 요청에 50초 이상** 걸리고,
하루 5회·몇 시간 간격으로 도는 브리핑은 항상 콜드 상태가 됩니다.

### 절대 하지 말 것

- 이 서버에 **주문·이체 기능을 넣지 마세요.** 읽기 전용으로 유지합니다.
- `MCP_TOKEN` 없이 배포하지 마세요. 공개 URL이라 누구나 호출할 수 있습니다.
- 토큰을 저장소에 커밋하지 마세요. Railway Variables 에만 두세요.

---

## 로컬 테스트

```powershell
pip install -r requirements.txt
$env:MCP_TOKEN="testtoken"
python server.py
```

다른 터미널에서:

```powershell
curl http://127.0.0.1:8000/healthz
```

### 검증 이력 (2026-10-27)

샌드박스에서 실제로 돌려 확인한 것:

- pykrx 1.2.9 에서 사용한 17개 함수 **전부 존재** 확인
- 8개 도구 모두 등록됨
- `initialize` / `tools/list` / `tools/call` **200 OK**
- 토큰 없는 요청 → **401**
- 없는 코드 조회 → 크래시 없이 `found: false` 구조화 응답

> **배포 중 가장 흔한 함정 (이미 코드에 반영됨)**
> MCP 앱을 바깥 Starlette 에 `Mount` 하면 하위 앱의 lifespan 이 실행되지 않아
> 모든 MCP 요청이 `Task group is not initialized` 500 으로 떨어집니다.
> `server.py` 는 MCP 앱을 그대로 쓰고 라우트·미들웨어만 얹는 방식으로 피했습니다.
> 구조를 고칠 때 이 부분을 되돌리지 마세요.
