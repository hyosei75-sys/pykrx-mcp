#!/usr/bin/env python3
"""
pykrx MCP — 한국거래소(KRX) 시세를 MCP 도구로 노출하는 원격 서버.

왜 만들었나
-----------
- KIS Open API는 `0219E0` 같은 **영문 섞인 종목코드**를 읽지 못합니다.
- KIS 일봉은 1회 100건 제한이라 12개월 차트를 못 그립니다(약 5개월에서 끊김).
- 국내 수급(투자자별 순매매)·공매도 데이터를 주는 커넥터가 없습니다.

pykrx는 KRX 공식 데이터를 그대로 읽으므로 위 3가지가 한 번에 해결됩니다.

전송 방식
---------
Streamable HTTP. Claude 커스텀 커넥터가 바로 붙습니다.
인증은 `Authorization: Bearer <MCP_TOKEN>` 헤더 하나로 끝냅니다.

⚠️ 이 서버는 **읽기 전용**입니다. 주문·이체 기능은 없고 앞으로도 넣지 마세요.
⚠️ 매매 의견을 생성하지 않습니다. 숫자와 기준일만 돌려줍니다.
"""

from __future__ import annotations

import os
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import pandas as pd
from pykrx import stock

# MCP SDK 2.x 에서 FastMCP 가 MCPServer 로 이름이 바뀌었습니다.
# 어느 버전이 깔려도 돌아가게 둘 다 시도합니다.
try:
    from mcp.server.mcpserver import MCPServer as _Server  # mcp >= 2.0
except ImportError:  # pragma: no cover
    from mcp.server.fastmcp import FastMCP as _Server  # mcp < 2.0

from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("pykrx-mcp")

KST = timezone(timedelta(hours=9))

# 지수 코드 (KRX 기준)
INDEX_CODES: dict[str, str] = {
    "KOSPI": "1001",
    "KOSDAQ": "2001",
    "KOSPI200": "1028",
}

MAX_ROWS = 400  # 응답 폭주 방지. 12개월 영업일 ≈ 245.


# ─────────────────────────────────────────────────────────────
# 날짜 유틸
# ─────────────────────────────────────────────────────────────

def today_kst() -> datetime:
    """Railway는 UTC로 돌기 때문에 한국 날짜를 직접 계산해야 합니다."""
    return datetime.now(KST)


def ymd(d: datetime) -> str:
    return d.strftime("%Y%m%d")


def months_ago(n: int) -> str:
    return ymd(today_kst() - timedelta(days=int(n * 30.5) + 5))


def days_ago(n: int) -> str:
    return ymd(today_kst() - timedelta(days=n + 10))  # 주말·공휴일 여유


# ─────────────────────────────────────────────────────────────
# pykrx 호출 래퍼
#
# pykrx는 버전에 따라 함수명이 `get_x` / `get_x_by_date` 로 갈립니다.
# 배포 환경에서 조용히 AttributeError 가 나지 않도록 둘 다 시도합니다.
# ─────────────────────────────────────────────────────────────

def _call(primary: str, fallback: str, *args) -> pd.DataFrame:
    fn = getattr(stock, primary, None) or getattr(stock, fallback, None)
    if fn is None:
        raise RuntimeError(f"pykrx에 {primary}/{fallback} 가 없습니다. 버전을 확인하세요.")
    return fn(*args)


def _ohlcv(code: str, start: str, end: str) -> pd.DataFrame:
    return _call("get_market_ohlcv", "get_market_ohlcv_by_date", start, end, code)


def _etf_ohlcv(code: str, start: str, end: str) -> pd.DataFrame:
    return _call("get_etf_ohlcv_by_date", "get_etf_ohlcv", start, end, code)


def _index_ohlcv(idx: str, start: str, end: str) -> pd.DataFrame:
    """
    ⚠️ `stock.get_index_ohlcv` 는 **인자 개수로 동작이 갈리는 오버로드 함수**입니다.
       (3인자 = 기간별 지수 시세 / 1인자 = 특정일 전종목 시세)
       분기를 잘못 타면 '지수명' 을 인덱스로 쓰는 전종목 DataFrame 이 돌아와
       `KeyError: '지수명'` 이 납니다.
       모호함을 없애기 위해 **`get_index_ohlcv_by_date` 를 직접** 부릅니다.

    ⚠️ `name_display=False` 가 필수입니다. 기본값(True)이면 pykrx가 OHLCV를
       정상적으로 받아온 **뒤에** 지수 이름을 따로 조회하는데
       (`df.columns.name = get_index_ticker_name(ticker)`),
       그 조회가 실패하면 데이터를 다 받아놓고도 `KeyError: '지수명'` 으로
       통째로 날아갑니다. 이름은 우리가 INDEX_CODES 로 이미 알고 있으니 끕니다.
    """
    fn = getattr(stock, "get_index_ohlcv_by_date", None)
    if fn is not None:
        return fn(start, end, idx, "d", False)
    return stock.get_index_ohlcv(start, end, idx, "d", False)


def _is_etf(code: str) -> bool:
    """영문이 섞인 코드는 거의 전부 신규 ETF/ETN 입니다."""
    return not code.isdigit()


def _frame(code: str, start: str, end: str) -> tuple[pd.DataFrame, str]:
    """
    일반 종목이면 주식 OHLCV, ETF면 ETF OHLCV.
    둘 다 비면 반대쪽도 시도합니다 (코드 형태만으로는 확실치 않기 때문).
    """
    order = ["etf", "stock"] if _is_etf(code) else ["stock", "etf"]
    last_err: Exception | None = None
    for kind in order:
        try:
            df = _etf_ohlcv(code, start, end) if kind == "etf" else _ohlcv(code, start, end)
            if df is not None and not df.empty:
                return df, kind
        except Exception as e:  # pykrx는 없는 코드에 예외를 던지기도 합니다
            last_err = e
    if last_err:
        log.warning("조회 실패 %s: %s", code, last_err)
    return pd.DataFrame(), "unknown"


def _rows(df: pd.DataFrame, limit: int = MAX_ROWS) -> list[dict[str, Any]]:
    """DataFrame → JSON 직렬화 가능한 행 목록. 날짜는 YYYY-MM-DD."""
    if df is None or df.empty:
        return []
    df = df.tail(limit)
    out: list[dict[str, Any]] = []
    for idx, row in df.iterrows():
        rec: dict[str, Any] = {"date": pd.Timestamp(idx).strftime("%Y-%m-%d")}
        for col, val in row.items():
            if pd.isna(val):
                rec[str(col)] = None
            elif hasattr(val, "item"):
                rec[str(col)] = val.item()
            else:
                rec[str(col)] = val
        out.append(rec)
    return out


def _name(code: str) -> str | None:
    for fn in ("get_market_ticker_name", "get_etf_ticker_name"):
        f = getattr(stock, fn, None)
        if f is None:
            continue
        try:
            n = f(code)
            if n:
                return str(n)
        except Exception:
            continue
    return None


# ─────────────────────────────────────────────────────────────
# MCP 도구
# ─────────────────────────────────────────────────────────────

mcp = _Server("pykrx-korea-market")


@mcp.tool()
def health() -> dict[str, Any]:
    """서버 상태와 pykrx 응답 가능 여부를 확인합니다."""
    ok, detail = True, "ok"
    try:
        df = _index_ohlcv(INDEX_CODES["KOSPI"], days_ago(10), ymd(today_kst()))
        if df.empty:
            ok, detail = False, "pykrx가 빈 응답을 돌려줬습니다 (KRX 점검/휴장 가능)"
    except Exception as e:
        # 예외 메시지만으로는 원인을 못 찾는 경우가 많아 타입과 pykrx 버전을 같이 남깁니다.
        ok, detail = False, f"pykrx 호출 실패: {type(e).__name__}: {e}"
    return {
        "ok": ok,
        "detail": detail,
        "pykrx_version": getattr(__import__("pykrx"), "__version__", "unknown"),
        "server_time_kst": today_kst().strftime("%Y-%m-%d %H:%M:%S"),
        "source": "KRX via pykrx",
    }


@mcp.tool()
def get_quote(code: str) -> dict[str, Any]:
    """
    종목(주식·ETF·ETN)의 최근 거래일 시세를 조회합니다.

    `0219E0` 처럼 영문이 섞인 코드도 조회됩니다.
    반환되는 `as_of` 는 **실제 체결이 있었던 거래일**이며, 장중 실시간가가 아니라
    해당 거래일의 종가입니다. 브리핑에 쓸 때 반드시 기준일을 함께 표기하세요.

    Args:
        code: 6자리 종목코드 (예: "005930", "0219E0")
    """
    code = code.strip().upper()
    end = ymd(today_kst())
    df, kind = _frame(code, months_ago(13), end)

    if df.empty:
        return {
            "code": code,
            "found": False,
            "error": "해당 코드의 시세를 찾지 못했습니다. search_ticker 로 코드를 확인하세요.",
            "source": "KRX via pykrx",
        }

    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) >= 2 else None
    close = float(last.get("종가", float("nan")))
    prev_close = float(prev.get("종가")) if prev is not None else None

    change = None
    change_pct = None
    if prev_close:
        change = round(close - prev_close, 2)
        change_pct = round((close - prev_close) / prev_close * 100, 2)

    # 52주 고저는 최근 1년 구간에서 직접 계산합니다 (pykrx가 직접 주지 않음).
    yr = df.tail(250)
    return {
        "code": code,
        "name": _name(code),
        "found": True,
        "kind": kind,
        "as_of": pd.Timestamp(df.index[-1]).strftime("%Y-%m-%d"),
        "as_of_note": "해당 거래일 종가. 장중 실시간가 아님.",
        "close": close,
        "open": float(last.get("시가")) if "시가" in last else None,
        "high": float(last.get("고가")) if "고가" in last else None,
        "low": float(last.get("저가")) if "저가" in last else None,
        "volume": int(last.get("거래량")) if "거래량" in last and not pd.isna(last.get("거래량")) else None,
        "change": change,
        "change_pct": change_pct,
        "high_52w": float(yr["고가"].max()) if "고가" in yr else None,
        "low_52w": float(yr["저가"].min()) if "저가" in yr else None,
        "nav": float(last["NAV"]) if "NAV" in last and not pd.isna(last["NAV"]) else None,
        "source": "KRX via pykrx",
    }


@mcp.tool()
def get_ohlcv(code: str, months: int = 12) -> dict[str, Any]:
    """
    종목의 일봉 OHLCV 이력을 조회합니다. **KIS의 100건 제한이 없습니다.**

    12개월 차트(일목균형표 78봉 포함)를 그리는 데 쓰세요.

    Args:
        code: 6자리 종목코드
        months: 조회 개월 수 (기본 12, 최대 24)
    """
    code = code.strip().upper()
    months = max(1, min(int(months), 24))
    df, kind = _frame(code, months_ago(months), ymd(today_kst()))
    bars = _rows(df)
    return {
        "code": code,
        "name": _name(code),
        "kind": kind,
        "months": months,
        "count": len(bars),
        "first_date": bars[0]["date"] if bars else None,
        "last_date": bars[-1]["date"] if bars else None,
        "bars": bars,
        "source": "KRX via pykrx",
    }


@mcp.tool()
def get_index_ohlcv(
    index: Literal["KOSPI", "KOSDAQ", "KOSPI200"] = "KOSPI",
    months: int = 12,
) -> dict[str, Any]:
    """
    국내 지수의 일봉 이력을 조회합니다. 12개월 지수 차트용.

    Args:
        index: KOSPI | KOSDAQ | KOSPI200
        months: 조회 개월 수 (기본 12, 최대 24)
    """
    months = max(1, min(int(months), 24))
    idx = INDEX_CODES[index]
    try:
        df = _index_ohlcv(idx, months_ago(months), ymd(today_kst()))
    except Exception as e:
        return {"index": index, "count": 0, "bars": [],
                "error": f"{type(e).__name__}: {e}",
                "source": "KRX via pykrx"}
    bars = _rows(df)
    return {
        "index": index,
        "krx_code": idx,
        "months": months,
        "count": len(bars),
        "first_date": bars[0]["date"] if bars else None,
        "last_date": bars[-1]["date"] if bars else None,
        "bars": bars,
        "source": "KRX via pykrx",
    }


@mcp.tool()
def get_investor_trading(code: str, days: int = 20) -> dict[str, Any]:
    """
    투자자별 순매매(개인·외국인·기관)를 조회합니다. 국내 시장 고유 수급 데이터입니다.

    ⚠️ 수급은 **사실 기록**입니다. 이걸 근거로 매수/매도 의견을 만들지 마세요.

    Args:
        code: 6자리 종목코드
        days: 조회 일수 (기본 20, 최대 120)
    """
    code = code.strip().upper()
    days = max(1, min(int(days), 120))
    try:
        df = _call(
            "get_market_trading_value_by_date",
            "get_market_trading_value_by_investor",
            days_ago(days), ymd(today_kst()), code,
        )
    except Exception as e:
        return {"code": code, "rows": [], "error": f"조회 실패: {e}",
                "source": "KRX via pykrx"}
    rows = _rows(df, limit=days)
    return {
        "code": code,
        "name": _name(code),
        "unit": "원 (순매수 금액)",
        "count": len(rows),
        "rows": rows,
        "note": "양수=순매수, 음수=순매도. 사실 기록이며 매매 권유가 아닙니다.",
        "source": "KRX via pykrx",
    }


@mcp.tool()
def get_short_selling(code: str, days: int = 20) -> dict[str, Any]:
    """
    공매도 거래량·잔고를 조회합니다.

    Args:
        code: 6자리 종목코드
        days: 조회 일수 (기본 20, 최대 120)
    """
    code = code.strip().upper()
    days = max(1, min(int(days), 120))
    out: dict[str, Any] = {"code": code, "name": _name(code), "source": "KRX via pykrx"}

    for key, primary, fallback in (
        ("volume", "get_shorting_volume_by_date", "get_shorting_status_by_date"),
        ("balance", "get_shorting_balance_by_date", "get_shorting_investor_volume_by_date"),
    ):
        try:
            df = _call(primary, fallback, days_ago(days), ymd(today_kst()), code)
            out[key] = _rows(df, limit=days)
        except Exception as e:
            out[key] = []
            out[f"{key}_error"] = str(e)

    return out


@mcp.tool()
def get_etf_detail(code: str, days: int = 20) -> dict[str, Any]:
    """
    ETF의 NAV·괴리율·추적오차를 조회합니다. ETF가 아니면 비어서 돌아옵니다.

    Args:
        code: 6자리 ETF 코드 (예: "0219E0", "069500")
        days: 조회 일수 (기본 20, 최대 120)
    """
    code = code.strip().upper()
    days = max(1, min(int(days), 120))
    start, end = days_ago(days), ymd(today_kst())
    out: dict[str, Any] = {"code": code, "name": _name(code), "source": "KRX via pykrx"}

    try:
        out["ohlcv_nav"] = _rows(_etf_ohlcv(code, start, end), limit=days)
    except Exception as e:
        out["ohlcv_nav"], out["ohlcv_nav_error"] = [], str(e)

    for key, fn_name in (("price_deviation", "get_etf_price_deviation"),
                         ("tracking_error", "get_etf_tracking_error")):
        fn = getattr(stock, fn_name, None)
        if fn is None:
            out[key] = []
            out[f"{key}_error"] = f"pykrx에 {fn_name} 없음"
            continue
        try:
            out[key] = _rows(fn(start, end, code), limit=days)
        except Exception as e:
            out[key], out[f"{key}_error"] = [], str(e)

    return out


@mcp.tool()
def search_ticker(query: str, limit: int = 20) -> dict[str, Any]:
    """
    종목명 일부로 코드를 찾습니다. 주식과 ETF를 함께 검색합니다.

    Args:
        query: 종목명 일부 (예: "커버드콜", "삼성전자")
        limit: 최대 결과 수 (기본 20, 최대 50)
    """
    query = query.strip()
    limit = max(1, min(int(limit), 50))
    # 휴장일에는 당일 목록이 비므로 최근 영업일로 되돌아갑니다.
    base = None
    for back in range(0, 10):
        d = ymd(today_kst() - timedelta(days=back))
        try:
            if stock.get_market_ticker_list(d, market="ALL"):
                base = d
                break
        except Exception:
            continue
    if base is None:
        return {"query": query, "results": [], "error": "종목 목록을 가져오지 못했습니다."}

    results: list[dict[str, str]] = []
    try:
        codes = list(stock.get_market_ticker_list(base, market="ALL"))
    except Exception:
        codes = []
    try:
        codes += list(stock.get_etf_ticker_list(base))
    except Exception:
        pass

    for c in codes:
        if len(results) >= limit:
            break
        n = _name(c)
        if n and query in n:
            results.append({"code": c, "name": n})

    return {
        "query": query,
        "base_date": base,
        "count": len(results),
        "results": results,
        "source": "KRX via pykrx",
    }


# ─────────────────────────────────────────────────────────────
# HTTP 앱 — Bearer 토큰 인증
# ─────────────────────────────────────────────────────────────

TOKEN = os.environ.get("MCP_TOKEN", "").strip()
PUBLIC_PATHS = ("/", "/healthz")


class TokenAuth:
    """
    토큰을 두 가지 방법으로 받습니다.

    1) 헤더:  Authorization: Bearer <MCP_TOKEN>
    2) 경로:  /t/<MCP_TOKEN>/mcp      ← 접두사를 떼고 /mcp 로 넘깁니다

    (2)가 필요한 이유: Claude 커스텀 커넥터의 "요청 헤더" 기능은 커스텀 헤더
    이름마다 Anthropic 승인이 필요하고, `Authorization` 은 Claude가 OAuth용으로
    이미 쓰고 있어 커스텀 헤더로 지정할 수 없습니다. 경로에 토큰을 넣으면
    헤더 설정 없이 "No sign in" 만으로 붙습니다.

    ⚠️ 경로 방식은 토큰이 URL에 노출됩니다. 접근 로그에 남을 수 있으니
       이 URL을 공유하지 마세요. 유출이 의심되면 Render에서 MCP_TOKEN 을
       새 값으로 바꾸면 기존 URL은 즉시 무효가 됩니다.

    BaseHTTPMiddleware 가 아니라 순수 ASGI 미들웨어인 이유는 경로를 고쳐 써야
    하기 때문입니다.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        path: str = scope.get("path", "")

        if path in PUBLIC_PATHS or not TOKEN:
            return await self.app(scope, receive, send)

        # (2) 경로 방식
        prefix = f"/t/{TOKEN}"
        if path == prefix or path.startswith(prefix + "/"):
            rest = path[len(prefix):] or "/"
            scope = dict(scope)
            scope["path"] = rest
            scope["raw_path"] = rest.encode()
            return await self.app(scope, receive, send)

        # (1) 헤더 방식
        for k, v in scope.get("headers", []):
            if k == b"authorization" and v.decode(errors="ignore") == f"Bearer {TOKEN}":
                return await self.app(scope, receive, send)

        await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)


async def root(_request):
    return PlainTextResponse(
        "pykrx MCP server (읽기 전용 · KRX via pykrx)\n"
        "MCP endpoint: /mcp  (Authorization: Bearer <MCP_TOKEN>)\n"
        "          또는 /t/<MCP_TOKEN>/mcp  (헤더 불필요)\n"
    )


async def healthz(_request):
    return JSONResponse({"ok": True, "kst": today_kst().strftime("%Y-%m-%d %H:%M:%S")})


# ⚠️ MCP SDK는 DNS 리바인딩 보호가 기본 ON 이라, Railway 도메인으로 들어오는 요청을
#    Host 헤더 불일치로 거부합니다. 이게 "커넥터가 안 붙는" 가장 흔한 원인입니다.
#    ALLOWED_HOSTS 를 지정하면 그 목록만 허용하고, 비워두면 보호를 끕니다.
#    (끄더라도 위 Bearer 토큰 검사는 그대로 적용됩니다.)
_hosts = [h.strip() for h in os.environ.get("ALLOWED_HOSTS", "").split(",") if h.strip()]
_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=bool(_hosts),
    allowed_hosts=_hosts,
    allowed_origins=_hosts,
)

_mcp_app = mcp.streamable_http_app(
    streamable_http_path="/mcp",
    # 호스팅 환경에서 인스턴스가 재시작돼도 세션이 깨지지 않도록 무상태로.
    stateless_http=True,
    transport_security=_security,
)

# ⚠️ MCP 앱을 바깥 Starlette 에 Mount 하지 마세요.
#    하위 앱의 lifespan 이 실행되지 않아 모든 MCP 요청이
#    "Task group is not initialized" 500 으로 떨어집니다.
#    대신 MCP 앱을 그대로 쓰고 라우트와 미들웨어만 얹습니다.
app = _mcp_app
app.router.routes.insert(0, Route("/", root))
app.router.routes.insert(1, Route("/healthz", healthz))
app.add_middleware(TokenAuth)


if __name__ == "__main__":
    import uvicorn

    if not TOKEN:
        log.warning("MCP_TOKEN 이 비어 있습니다 — 서버가 인증 없이 공개됩니다!")
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
