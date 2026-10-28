#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""일봉 OHLC → MACD · RSI · 일목균형표 계산.

입력(JSON, stdin 또는 --bars 파일):
  {
    "005930": [ {"date":"20260820","open":..,"high":..,"low":..,"close":..}, ... ],
    "VOO":    [ {"date":"2026-08-20","open":..,"high":..,"low":..,"close":..}, ... ]
  }
  - 날짜 오름/내림 어느 쪽이든 받습니다. 내부에서 오름차순으로 정렬합니다.
  - KIS(koreaStock-stock_get_price_history)와 yfinance(UsStockInfo-get_historical_stock_prices)
    응답을 그대로 바꿔 담으면 됩니다. 키 이름은 대소문자 무시로 찾습니다.

출력: 종목별 지표 + 사람이 읽을 수 있는 신호 문구
  python3 indicators.py --bars bars.json            # JSON 출력
  python3 indicators.py --bars bars.json --text      # 표 형태 요약
  python3 indicators.py --selftest                   # 자체 검증

필요 봉 수
  RSI(14)          : 최소 15,  권장 60+
  MACD(12,26,9)    : 최소 35,  권장 100+ (EMA 수렴)
  일목균형표(9,26,52): 최소 78,  권장 100+
  → 국내는 limit=100, 해외는 period="6mo" 로 받으면 충분합니다.
"""
import argparse
import json
import sys

# ---------- 공통 유틸 ----------


def _get(bar, *names):
    low = {str(k).lower(): v for k, v in bar.items()}
    for n in names:
        if n.lower() in low and low[n.lower()] is not None:
            return float(low[n.lower()])
    return None


def normalize(bars):
    """[{date,open,high,low,close}] 로 정규화하고 날짜 오름차순 정렬."""
    out = []
    for b in bars:
        d = b.get("date") or b.get("Date") or b.get("기준일")
        c = _get(b, "close", "Close", "종가")
        if c is None:
            continue
        out.append({
            "date": str(d),
            "high": _get(b, "high", "High", "고가") or c,
            "low": _get(b, "low", "Low", "저가") or c,
            "close": c,
            "volume": _get(b, "volume", "Volume", "거래량"),
        })
    out.sort(key=lambda x: x["date"])
    return out


def ema(values, span):
    k = 2.0 / (span + 1)
    out, prev = [], None
    for v in values:
        prev = v if prev is None else (v - prev) * k + prev
        out.append(prev)
    return out


def _round(x, nd=2):
    return None if x is None else round(x, nd)


# ---------- 지표 ----------


def rsi(closes, period=14):
    """Wilder 평활 RSI. 마지막 값과 직전 값을 돌려줍니다."""
    if len(closes) < period + 1:
        return None, None
    gains, losses = [], []
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i - 1]
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    series = []
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
        if avg_l == 0:
            series.append(100.0)
        else:
            rs = avg_g / avg_l
            series.append(100.0 - 100.0 / (1.0 + rs))
    if not series:
        return None, None
    return series[-1], (series[-2] if len(series) > 1 else None)


def macd(closes, fast=12, slow=26, signal=9):
    if len(closes) < slow + signal:
        return None
    ef, es = ema(closes, fast), ema(closes, slow)
    line = [a - b for a, b in zip(ef, es)]
    sig = ema(line[slow - 1:], signal)          # EMA 수렴 전 구간 제외
    line_t = line[slow - 1:]
    hist = [l - s for l, s in zip(line_t, sig)]
    if len(hist) < 2:
        return None
    return {
        "macd": line_t[-1], "signal": sig[-1], "hist": hist[-1],
        "hist_prev": hist[-2],
        "macd_prev": line_t[-2], "signal_prev": sig[-2],
    }



def obv(bars, window=20):
    """On-Balance Volume. 누적 거래량 + 추세·다이버전스 판정.

    OBV 자체의 절대값은 의미가 없습니다. **방향**과 **가격과의 괴리**를 봅니다.
    - 가격은 내리는데 OBV가 오르면: 매집 가능성 (강세 다이버전스)
    - 가격은 오르는데 OBV가 내리면: 분산 가능성 (약세 다이버전스)
    """
    vols = [b.get("volume") for b in bars]
    if any(v is None for v in vols) or len(bars) < window + 2:
        return None
    series, cur = [], 0.0
    for i, b in enumerate(bars):
        if i == 0:
            series.append(0.0)
            continue
        if b["close"] > bars[i - 1]["close"]:
            cur += b["volume"]
        elif b["close"] < bars[i - 1]["close"]:
            cur -= b["volume"]
        series.append(cur)

    def slope_pct(vals):
        """구간 시작 대비 끝의 변화를 규모로 정규화 (OBV는 단위가 커서 % 대신 비율)."""
        a, b_ = vals[-window], vals[-1]
        scale = max(abs(x) for x in vals[-window:]) or 1.0
        return (b_ - a) / scale

    obv_tr = slope_pct(series)
    closes = [x["close"] for x in bars]
    px_tr = (closes[-1] - closes[-window]) / closes[-window]

    THR = 0.02
    obv_dir = "상승" if obv_tr > THR else "하락" if obv_tr < -THR else "횡보"
    px_dir = "상승" if px_tr > THR else "하락" if px_tr < -THR else "횡보"

    diverg = None
    if px_dir == "하락" and obv_dir == "상승":
        diverg = "강세 다이버전스 — 가격은 내렸는데 OBV는 올랐습니다 (매집 가능성)"
    elif px_dir == "상승" and obv_dir == "하락":
        diverg = "약세 다이버전스 — 가격은 올랐는데 OBV는 내렸습니다 (분산 가능성)"

    return {
        "obv": series[-1], "obv_prev": series[-2],
        "obv_trend": obv_dir, "price_trend": px_dir,
        "obv_change_ratio": obv_tr, "price_change_pct": px_tr * 100,
        "window": window, "divergence": diverg,
    }


def ichimoku(bars, conv=9, base=26, span_b=52, shift=26):
    """일목균형표. 선행스팬은 shift 만큼 앞으로 이동한 '오늘 위치'의 값을 씁니다."""
    n = len(bars)
    if n < span_b + shift:
        return None
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    closes = [b["close"] for b in bars]

    def mid(i, p):
        if i - p + 1 < 0:
            return None
        w_h = highs[i - p + 1:i + 1]
        w_l = lows[i - p + 1:i + 1]
        return (max(w_h) + min(w_l)) / 2.0

    last = n - 1
    tenkan = mid(last, conv)
    kijun = mid(last, base)
    tenkan_prev = mid(last - 1, conv)
    kijun_prev = mid(last - 1, base)

    # 오늘 자리에 깔려 있는 구름 = shift 일 전에 산출된 선행스팬
    src = last - shift
    a_src_t, a_src_k = mid(src, conv), mid(src, base)
    senkou_a = (a_src_t + a_src_k) / 2.0 if a_src_t and a_src_k else None
    senkou_b = mid(src, span_b)

    # 후행스팬: 오늘 종가를 shift 일 전 자리에 놓고 그때 가격과 비교
    chikou_ref = closes[last - shift] if last - shift >= 0 else None

    return {
        "tenkan": tenkan, "kijun": kijun,
        "tenkan_prev": tenkan_prev, "kijun_prev": kijun_prev,
        "senkou_a": senkou_a, "senkou_b": senkou_b,
        "chikou_close": closes[last], "chikou_ref": chikou_ref,
    }


# ---------- 신호 해석 ----------


def describe(close, r, m, ich, ob=None):
    """지표를 한국어 신호 문구로. 매매 권유가 아니라 상태 서술입니다."""
    sig = []

    if r is not None:
        v = r[0]
        zone = ("과매수권" if v >= 70 else
                "과매도권" if v <= 30 else
                "중립")
        sig.append(f"RSI(14) {v:.1f} — {zone}")

    if m:
        cross = ""
        if m["hist_prev"] is not None:
            if m["hist_prev"] <= 0 < m["hist"]:
                cross = ", 시그널선 상향 돌파(골든크로스)"
            elif m["hist_prev"] >= 0 > m["hist"]:
                cross = ", 시그널선 하향 돌파(데드크로스)"
        pos = "시그널선 위" if m["hist"] > 0 else "시그널선 아래"
        base = "0선 위" if m["macd"] > 0 else "0선 아래"
        sig.append(f"MACD {m['macd']:.2f} / 시그널 {m['signal']:.2f} — {pos}, {base}{cross}")

    if ich:
        a, b = ich["senkou_a"], ich["senkou_b"]
        if a is not None and b is not None:
            top, bot = max(a, b), min(a, b)
            if close > top:
                cloud = "구름 위 (양호)"
            elif close < bot:
                cloud = "구름 아래 (약세)"
            else:
                cloud = "구름 안 (방향성 불명확)"
            color = "양운" if a >= b else "음운"
            sig.append(f"일목 구름 {bot:,.0f}~{top:,.0f} {color} — 현재가 {cloud}")

        t, k = ich["tenkan"], ich["kijun"]
        tp, kp = ich["tenkan_prev"], ich["kijun_prev"]
        if None not in (t, k, tp, kp):
            cr = ""
            if tp <= kp and t > k:
                cr = " (전환선 상향 교차)"
            elif tp >= kp and t < k:
                cr = " (전환선 하향 교차)"
            rel = "전환선 > 기준선" if t > k else "전환선 < 기준선"
            sig.append(f"전환선 {t:,.0f} / 기준선 {k:,.0f} — {rel}{cr}")

        pass
    if ob:
        sig.append(f"OBV {ob['window']}일 {ob['obv_trend']} (가격 {ob['price_trend']})"
                   + (f" — {ob['divergence'].split(' — ')[0]}" if ob["divergence"] else ""))
    if ich:
        cc, cr_ = ich["chikou_close"], ich["chikou_ref"]
        if cc is not None and cr_ is not None:
            rel = "위" if cc > cr_ else "아래"
            sig.append(f"후행스팬 — 26일 전 가격({cr_:,.0f}) {rel}")

    return sig


def verdict(close, r, m, ich, ob=None):
    """세 지표를 종합한 '기술적 상태' 판정.

    ⚠️ 매매 권유가 아닙니다. 각 지표가 강세/약세 중 어디를 가리키는지 세고,
    서로 일치하는지 엇갈리는지를 서술할 뿐입니다. 판단과 실행은 사용자 몫입니다.
    """
    votes = {}          # 지표명 → (+1 강세 / 0 중립 / -1 약세, 근거 문구)

    # --- RSI: 50선 기준 모멘텀 + 극단 구간 표시 ---
    rsi_extreme = None
    if r and r[0] is not None:
        v = r[0]
        if v >= 70:
            votes["RSI"] = (1, f"{v:.1f} 과매수권")
            rsi_extreme = "과매수"
        elif v <= 30:
            votes["RSI"] = (-1, f"{v:.1f} 과매도권")
            rsi_extreme = "과매도"
        elif v > 50:
            votes["RSI"] = (1, f"{v:.1f} 50선 위")
        elif v < 50:
            votes["RSI"] = (-1, f"{v:.1f} 50선 아래")
        else:
            votes["RSI"] = (0, "50 중립")

    # --- MACD: 시그널선 대비 + 0선 대비 ---
    if m:
        if m["hist"] > 0 and m["macd"] > 0:
            votes["MACD"] = (1, "시그널선 위 · 0선 위")
        elif m["hist"] < 0 and m["macd"] < 0:
            votes["MACD"] = (-1, "시그널선 아래 · 0선 아래")
        elif m["hist"] > 0:
            votes["MACD"] = (0, "시그널선 위지만 0선 아래")
        else:
            votes["MACD"] = (0, "시그널선 아래지만 0선 위")

    # --- 일목균형표: 구름 위치를 주 신호로, 전환/기준선을 보조로 ---
    if ich and ich["senkou_a"] is not None and ich["senkou_b"] is not None:
        top = max(ich["senkou_a"], ich["senkou_b"])
        bot = min(ich["senkou_a"], ich["senkou_b"])
        t, k = ich["tenkan"], ich["kijun"]
        tk = "" if None in (t, k) else (" · 전환선>기준선" if t > k else " · 전환선<기준선")
        if close > top:
            votes["일목"] = (1, f"구름 위{tk}")
        elif close < bot:
            votes["일목"] = (-1, f"구름 아래{tk}")
        else:
            votes["일목"] = (0, f"구름 안{tk}")

    # --- OBV: 거래량으로 본 수급 방향 ---
    if ob:
        if ob["obv_trend"] == "상승":
            votes["OBV"] = (1, f"{ob['window']}일 상승 (가격 {ob['price_trend']})")
        elif ob["obv_trend"] == "하락":
            votes["OBV"] = (-1, f"{ob['window']}일 하락 (가격 {ob['price_trend']})")
        else:
            votes["OBV"] = (0, f"{ob['window']}일 횡보")

    if not votes:
        return None

    bull = sum(1 for v, _ in votes.values() if v > 0)
    bear = sum(1 for v, _ in votes.values() if v < 0)
    neut = sum(1 for v, _ in votes.values() if v == 0)
    n = len(votes)

    if bear == n:
        stance, agree = "약세", f"{n}개 지표 모두 약세로 일치"
    elif bull == n:
        stance, agree = "강세", f"{n}개 지표 모두 강세로 일치"
    elif bear > bull:
        stance, agree = "약세 우위", f"약세 {bear} / 강세 {bull} / 중립 {neut}"
    elif bull > bear:
        stance, agree = "강세 우위", f"강세 {bull} / 약세 {bear} / 중립 {neut}"
    else:
        stance, agree = "혼조", f"강세 {bull} / 약세 {bear} / 중립 {neut} — 신호 엇갈림"

    # 상태 해석 (예측이 아니라 '지금 무엇이 보이는가')
    if stance == "약세":
        summary = "추세·모멘텀·위치가 모두 아래를 가리킵니다. 하락 추세가 진행 중인 구간입니다."
    elif stance == "강세":
        summary = "추세·모멘텀·위치가 모두 위를 가리킵니다. 상승 추세가 진행 중인 구간입니다."
    elif stance == "혼조":
        summary = "지표가 서로 다른 방향을 가리킵니다. 추세가 분명하지 않은 구간입니다."
    else:
        summary = f"{stance}이지만 지표 간 완전한 일치는 아닙니다. 아래 근거를 함께 보세요."

    caveats = []
    if rsi_extreme == "과매도" and stance.startswith("약세"):
        caveats.append("RSI가 과매도권이라 단기 되돌림이 나타날 수 있는 구간이기도 합니다. "
                       "과매도는 '바닥'이 아니라 '하락이 빨랐다'는 뜻입니다.")
    if rsi_extreme == "과매수" and stance.startswith("강세"):
        caveats.append("RSI가 과매수권이라 단기 조정이 나타날 수 있는 구간이기도 합니다. "
                       "과매수는 '고점'이 아니라 '상승이 빨랐다'는 뜻입니다.")
    if ob and ob["divergence"]:
        caveats.append(ob["divergence"] + " 가격과 거래량이 다른 방향을 가리키는 구간입니다.")
    if m and m["hist_prev"] is not None:
        if m["hist_prev"] <= 0 < m["hist"]:
            caveats.append("MACD가 직전 봉에서 시그널선을 상향 돌파했습니다 — 신호가 막 바뀐 시점입니다.")
        elif m["hist_prev"] >= 0 > m["hist"]:
            caveats.append("MACD가 직전 봉에서 시그널선을 하향 돌파했습니다 — 신호가 막 바뀐 시점입니다.")
    if ich and ich["senkou_a"] is not None and ich["senkou_b"] is not None:
        top = max(ich["senkou_a"], ich["senkou_b"])
        bot = min(ich["senkou_a"], ich["senkou_b"])
        if bot <= close <= top:
            caveats.append("현재가가 구름 안에 있어 일목균형표상 방향성이 정해지지 않은 상태입니다.")
        else:
            near = min(abs(close - top), abs(close - bot)) / close * 100
            if near < 3:
                caveats.append(f"현재가가 구름 경계에서 {near:.1f}% 거리로 근접해 있습니다 — "
                               "경계를 넘으면 일목 신호가 바뀝니다.")

    # --- 교과서적 신호 분류 ---
    # 기술적 분석에서 '관례적으로 어떤 이름으로 불리는 패턴인가'를 적습니다.
    # 사용자 상황에 맞춘 매매 권유가 아니라, 지표 해석의 일반 규약을 옮긴 것입니다.
    textbook = []
    if m and m["hist_prev"] is not None:
        if m["hist_prev"] <= 0 < m["hist"]:
            textbook.append("MACD 골든크로스 — 관례적으로 매수 신호로 분류되는 패턴")
        elif m["hist_prev"] >= 0 > m["hist"]:
            textbook.append("MACD 데드크로스 — 관례적으로 매도 신호로 분류되는 패턴")
    if rsi_extreme == "과매도":
        textbook.append("RSI 30 이하 — 관례적으로 '과매도, 반등 가능 구간'으로 해석되는 수준")
    elif rsi_extreme == "과매수":
        textbook.append("RSI 70 이상 — 관례적으로 '과매수, 조정 가능 구간'으로 해석되는 수준")
    if ich and None not in (ich["tenkan"], ich["kijun"], ich["senkou_a"],
                            ich["senkou_b"], ich["chikou_ref"]):
        top = max(ich["senkou_a"], ich["senkou_b"])
        bot = min(ich["senkou_a"], ich["senkou_b"])
        三 = (close > top, ich["tenkan"] > ich["kijun"],
             ich["chikou_close"] > ich["chikou_ref"])
        if all(三):
            textbook.append("일목균형표 삼역호전(구름 위·전환선>기준선·후행스팬 위) — "
                            "관례적으로 강세 전환 신호로 분류")
        elif not any(三):
            textbook.append("일목균형표 삼역역전(구름 아래·전환선<기준선·후행스팬 아래) — "
                            "관례적으로 약세 전환 신호로 분류")
        if ich["tenkan_prev"] is not None and ich["kijun_prev"] is not None:
            if ich["tenkan_prev"] <= ich["kijun_prev"] and ich["tenkan"] > ich["kijun"]:
                textbook.append("일목 전환선이 기준선을 상향 교차 — 관례적 강세 신호")
            elif ich["tenkan_prev"] >= ich["kijun_prev"] and ich["tenkan"] < ich["kijun"]:
                textbook.append("일목 전환선이 기준선을 하향 교차 — 관례적 약세 신호")

    if ob and ob["divergence"]:
        kind = "강세" if "강세" in ob["divergence"] else "약세"
        textbook.append(f"OBV {kind} 다이버전스 — 관례적으로 추세 전환 가능 신호로 분류되는 패턴")

    return {
        "stance": stance,
        "agreement": agree,
        "summary": summary,
        "by_indicator": {k: v[1] for k, v in votes.items()},
        "caveats": caveats,
        "textbook_signals": textbook,
        "textbook_note": ("위 분류는 기술적 분석에서 해당 패턴을 관례적으로 뭐라 부르는지 "
                          "옮긴 것입니다. 보유 수량·매입단가·투자기간·감내 가능한 손실을 "
                          "반영한 매매 권유가 아닙니다. 지표는 후행적이며 단독 적중률이 "
                          "높지 않습니다."),
        "disclaimer": "기술적 상태 서술이며 매매 권유가 아닙니다.",
    }


def analyze(bars_raw):
    bars = normalize(bars_raw)
    if len(bars) < 15:
        return {"ok": False, "reason": f"봉 부족 ({len(bars)}개)"}
    closes = [b["close"] for b in bars]
    r = rsi(closes)
    m = macd(closes)
    ich = ichimoku(bars)
    ob = obv(bars)
    res = {
        "ok": True,
        "bars": len(bars),
        "last_date": bars[-1]["date"],
        "close": closes[-1],
        "rsi14": _round(r[0]) if r and r[0] is not None else None,
        "rsi14_prev": _round(r[1]) if r and r[1] is not None else None,
        "macd": {k: _round(v, 3) for k, v in m.items()} if m else None,
        "ichimoku": {k: _round(v) for k, v in ich.items()} if ich else None,
        "obv": {k: (_round(v, 4) if isinstance(v, float) else v) for k, v in ob.items()} if ob else None,
        "signals": describe(closes[-1], r, m, ich, ob),
        "verdict": verdict(closes[-1], r, m, ich, ob),
    }
    if m is None:
        res.setdefault("warnings", []).append("MACD 계산 불가 — 봉 35개 이상 필요")
    if ich is None:
        res.setdefault("warnings", []).append("일목균형표 계산 불가 — 봉 78개 이상 필요")
    if ob is None:
        res.setdefault("warnings", []).append("OBV 계산 불가 — 거래량 누락 또는 봉 22개 미만")
    return res


# ---------- 자체 검증 ----------


def selftest():
    import math
    # 1) 단조 상승이면 RSI = 100
    up = [{"date": f"{i:03d}", "close": 100 + i, "high": 100 + i, "low": 100 + i}
          for i in range(60)]
    a = analyze(up)
    assert a["rsi14"] == 100.0, a["rsi14"]

    # 2) 단조 하락이면 RSI = 0
    dn = [{"date": f"{i:03d}", "close": 200 - i, "high": 200 - i, "low": 200 - i}
          for i in range(60)]
    assert analyze(dn)["rsi14"] == 0.0

    # 3) 횡보(평탄)면 MACD ≈ 0
    flat = [{"date": f"{i:03d}", "close": 100.0, "high": 100.0, "low": 100.0}
            for i in range(120)]
    f = analyze(flat)
    assert abs(f["macd"]["macd"]) < 1e-6, f["macd"]
    # 평탄하면 구름도 현재가와 같음
    assert abs(f["ichimoku"]["senkou_a"] - 100.0) < 1e-6

    # 4) 상승 추세면 구름 위
    u = analyze([{"date": f"{i:03d}", "close": 100 + i * 2,
                  "high": 101 + i * 2, "low": 99 + i * 2} for i in range(120)])
    assert "구름 위" in " ".join(u["signals"]), u["signals"]
    assert u["ichimoku"]["tenkan"] > u["ichimoku"]["kijun"]

    # 5) EMA 기본 동작
    assert abs(ema([1, 1, 1, 1], 3)[-1] - 1.0) < 1e-9

    # 6) 봉 부족 처리
    assert analyze([{"date": "1", "close": 1}])["ok"] is False
    short = analyze([{"date": f"{i:03d}", "close": 100 + (i % 5),
                      "high": 100 + (i % 5), "low": 100 + (i % 5)} for i in range(40)])
    assert short["ichimoku"] is None and "일목균형표 계산 불가" in " ".join(short["warnings"])

    # 7) OBV — 가격 상승 + 거래량 동반이면 OBV 상승
    volup = [{"date": f"{i:03d}", "close": 100 + i, "high": 101 + i,
              "low": 99 + i, "volume": 1000} for i in range(60)]
    o = analyze(volup)["obv"]
    assert o["obv_trend"] == "상승", o

    # 8) OBV 강세 다이버전스 — 가격은 내리는데 거래량이 상승일에 쏠림
    bars = []
    px = 200.0
    for i in range(60):
        down = (i % 4 != 0)          # 4일 중 3일 하락, 1일 상승
        px = px - 1.0 if down else px + 2.0
        bars.append({"date": f"{i:03d}", "close": px, "high": px + 1,
                     "low": px - 1, "volume": 300 if down else 5000})
    d = analyze(bars)["obv"]
    assert d["price_trend"] == "하락" and d["obv_trend"] == "상승", d
    assert "강세 다이버전스" in d["divergence"], d

    # 9) 거래량이 없으면 OBV는 None (나머지 지표는 정상)
    novol = analyze([{"date": f"{i:03d}", "close": 100 + i, "high": 101 + i,
                      "low": 99 + i} for i in range(60)])
    assert novol["obv"] is None and novol["rsi14"] is not None

    print("selftest OK — 9개 케이스 통과")


# ---------- CLI ----------

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", help="티커→일봉배열 JSON 파일 (없으면 stdin)")
    ap.add_argument("--text", action="store_true", help="사람이 읽는 요약")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        selftest()
        sys.exit(0)

    raw = open(a.bars, encoding="utf-8").read() if a.bars else sys.stdin.read()
    data = json.loads(raw)
    out = {t: analyze(b) for t, b in data.items()}

    if a.text:
        for t, r in out.items():
            if not r["ok"]:
                print(f"[{t}] {r['reason']}")
                continue
            print(f"[{t}] {r['last_date']} 종가 {r['close']:,.2f} (봉 {r['bars']}개)")
            for s in r["signals"]:
                print(f"    · {s}")
            v = r.get("verdict")
            if v:
                print(f"    ── 종합: {v['stance']} ({v['agreement']})")
                print(f"       {v['summary']}")
                for k, why in v["by_indicator"].items():
                    print(f"       · {k}: {why}")
                for c in v["caveats"]:
                    print(f"       ※ {c}")
                if v["textbook_signals"]:
                    print("       [교과서적 신호 분류]")
                    for t in v["textbook_signals"]:
                        print(f"       ▸ {t}")
            for w in r.get("warnings", []):
                print(f"    ⚠️ {w}")
            print()
    else:
        print(json.dumps(out, ensure_ascii=False, indent=1))
