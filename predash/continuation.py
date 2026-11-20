"""Broker-neutral universe-wide 5-session consecutive-riser scanner.

Read-only market data only. The scan is explicit/manual because a full-market
scan requires one daily-chart request per listed common stock.
"""
from __future__ import annotations

from datetime import date, datetime, time as clock_time, timedelta
from io import BytesIO
from statistics import mean
from zoneinfo import ZoneInfo
import re
import time
import zipfile

import requests

KST = ZoneInfo("Asia/Seoul")
MASTER_URLS = {
    "코스피": ("https://new.real.download.dws.co.kr/common/master/kospi_code.mst.zip", 228),
    "코스닥": ("https://new.real.download.dws.co.kr/common/master/kosdaq_code.mst.zip", 222),
}
DAILY_PATH = "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
QUOTE_PATH = "/uapi/domestic-stock/v1/quotations/inquire-price"
DAILY_TR_ID = "FHKST03010100"
QUOTE_TR_ID = "FHKST01010100"


class ContinuationScanError(RuntimeError):
    """Raised when a reliable full-market scan cannot be completed."""


def _flag(value: str) -> bool:
    return value.strip().upper() not in ("", "0", "N", "NO")


def _parse_master(payload: bytes, market: str, tail_size: int) -> list[dict]:
    """Parse KIS fixed-width master: 9-byte ticker, 12-byte ISIN, name, flags."""
    try:
        with zipfile.ZipFile(BytesIO(payload)) as archive:
            candidates = [name for name in archive.namelist() if name.lower().endswith(".mst")]
            if not candidates:
                raise ContinuationScanError(f"{market} 마스터 파일에 MST 데이터가 없습니다.")
            raw = archive.read(candidates[0])
    except (zipfile.BadZipFile, OSError) as exc:
        raise ContinuationScanError(f"{market} 마스터 압축파일을 해석하지 못했습니다.") from exc

    symbols = []
    for line in raw.splitlines():
        line = line.rstrip(b"\r")
        if len(line) <= tail_size + 21:
            continue
        prefix, tail = line[:-tail_size], line[-tail_size:]
        try:
            code = prefix[:9].decode("cp949").strip()[-6:].zfill(6)
            name = prefix[21:].decode("cp949").strip()
            group = tail[:2].decode("ascii", errors="ignore").strip()
        except UnicodeDecodeError:
            continue
        if not re.fullmatch(r"\d{6}", code) or not name:
            continue

        # ST is a listed stock. ETPs and other security groups are excluded.
        if group != "ST":
            continue
        if re.search(r"(?:스팩|기업인수목적|우(?:B|C)?)$", name):
            continue

        if market == "코스피":
            etp, spac, halt, liquidation, managed, preferred = (
                tail[22:23], tail[29:30], tail[34:35], tail[35:36], tail[36:37], tail[158:159]
            )
        else:
            etp, spac, halt, liquidation, managed, preferred = (
                tail[18:19], tail[24:25], tail[53:54], tail[54:55], tail[55:56], tail[153:154]
            )
        if _flag(etp.decode("ascii", errors="ignore")):
            continue
        if _flag(spac.decode("ascii", errors="ignore")):
            continue
        if preferred.decode("ascii", errors="ignore").strip() in ("1", "2"):
            continue
        if any(_flag(value.decode("ascii", errors="ignore"))
               for value in (halt, liquidation, managed)):
            continue

        symbols.append({"code": code, "name": name, "market": market})

    if not symbols:
        raise ContinuationScanError(f"{market} 마스터에서 분석 대상 종목을 찾지 못했습니다.")
    return symbols


def load_universe(timeout: int = 30) -> list[dict]:
    """Load the public KOSPI/KOSDAQ symbol universe; broker credentials are not used."""
    all_symbols = []
    for market, (url, tail_size) in MASTER_URLS.items():
        try:
            response = requests.get(url, timeout=(5, timeout))
            response.raise_for_status()
        except requests.RequestException as exc:
            raise ContinuationScanError(
                f"{market} 종목 마스터 다운로드 실패. KIS 마스터 서버 연결을 확인하세요."
            ) from exc
        all_symbols.extend(_parse_master(response.content, market, tail_size))

    unique = {}
    for item in all_symbols:
        unique[item["code"]] = item
    return sorted(unique.values(), key=lambda row: (row["market"], row["code"]))


def latest_completed_session_end(now: datetime | None = None) -> date:
    now = now or datetime.now(KST)
    # Avoid treating an unfinished same-day candle as a completed session.
    target = now.date()
    if now.time() < clock_time(15, 40):
        target -= timedelta(days=1)
    return target


def _parse_bars(rows: list[dict]) -> list[dict]:
    by_date = {}
    for row in rows:
        try:
            day = datetime.strptime(str(row["stck_bsop_date"]), "%Y%m%d").date()
            close = float(str(row["stck_clpr"]).replace(",", ""))
            volume = int(float(str(row.get("acml_vol", "0")).replace(",", "")))
            turnover = float(str(row.get("acml_tr_pbmn", "0")).replace(",", ""))
        except (KeyError, TypeError, ValueError):
            continue
        if close > 0:
            by_date[day] = {
                "date": day, "close": close,
                "volume": max(volume, 0), "turnover": max(turnover, 0.0),
            }
    return sorted(by_date.values(), key=lambda row: row["date"])


def fetch_daily_bars(client, code: str, start_day: date, end_day: date) -> list[dict]:
    # Broker adapters normalize their own chart response to the scanner schema.
    if hasattr(client, "daily_bars_range"):
        rows = client.daily_bars_range(code, start_day, end_day)
        if not isinstance(rows, list):
            raise ContinuationScanError("증권사 일봉 응답 형식이 올바르지 않습니다.")
        return rows

    # Backward-compatible KIS path.
    client.authorize()
    headers = {
        "authorization": "Bearer " + client.token,
        "appkey": client.key,
        "appsecret": client.secret,
        "tr_id": DAILY_TR_ID,
        "custtype": "P",
    }
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": code,
        "FID_INPUT_DATE_1": start_day.strftime("%Y%m%d"),
        "FID_INPUT_DATE_2": end_day.strftime("%Y%m%d"),
        "FID_PERIOD_DIV_CODE": "D",
        "FID_ORG_ADJ_PRC": "0",
    }
    _, data = client.call("GET", DAILY_PATH, headers=headers, params=params)
    if str(data.get("rt_cd")) != "0":
        message_code = str(data.get("msg_cd", ""))
        if message_code.startswith("EGW002"):
            raise ContinuationScanError("증권사 API 호출 한도 응답을 받았습니다. 잠시 후 다시 시도하세요.")
        raise client.result_error(data, "연속 상승 일봉 조회 실패")
    rows = data.get("output2")
    if not isinstance(rows, list):
        raise ContinuationScanError("KIS 일봉 응답 형식이 올바르지 않습니다.")
    return _parse_bars(rows)


def evaluate_five_session_rise(item: dict, bars: list[dict], expected_dates=None) -> dict | None:
    """Five consecutive close increases; 25 bars also enable 5-vs-20 averages."""
    if len(bars) < 6:
        return None
    six = bars[-6:]
    if expected_dates is not None and [row["date"] for row in six] != list(expected_dates):
        return None
    closes = [row["close"] for row in six]
    if not all(closes[index] > closes[index - 1] for index in range(1, 6)):
        return None

    recent = bars[-5:]
    prior = bars[-25:-5] if len(bars) >= 25 else []
    avg_recent_volume = mean(row["volume"] for row in recent)
    avg_recent_turnover = mean(row["turnover"] for row in recent)
    avg_prior_volume = mean(row["volume"] for row in prior) if prior else None
    avg_prior_turnover = mean(row["turnover"] for row in prior) if prior else None

    def growth(current, previous):
        if previous is None or previous <= 0:
            return None
        return round((current / previous - 1) * 100, 2)

    return {
        "종목코드": item["code"],
        "종목명": item["name"],
        "시장": item["market"],
        "기준일": six[-1]["date"].isoformat(),
        "5일누적상승률(%)": round((closes[-1] / closes[0] - 1) * 100, 2),
        "최근일상승률(%)": round((closes[-1] / closes[-2] - 1) * 100, 2),
        "기준일종가(원)": int(closes[-1]),
        "5일평균거래량(주)": int(avg_recent_volume),
        "거래량증가율(%)": growth(avg_recent_volume, avg_prior_volume),
        "5일평균거래대금(억원)": round(avg_recent_turnover / 100_000_000, 2),
        "거래대금증가율(%)": growth(avg_recent_turnover, avg_prior_turnover),
        "PBR": None,
        "ROE(%)": None,
        "3년매출변화(%)": None,
        "3년영업이익변화(%)": None,
        "최근영업현금흐름(억원)": None,
        "재무점검(4)": "자료 미연결",
    }


def scan_market(client, *, on_progress=None, bar_cache=None, universe_cache=None,
                pause_seconds: float = 0.15, top_n: int = 5) -> dict:
    """Scan all KOSPI/KOSDAQ common stocks. Partial scans are never certified."""
    if not 1 <= top_n <= 20:
        raise ValueError("top_n은 1~20 범위여야 합니다.")
    if pause_seconds < 0:
        raise ValueError("pause_seconds는 0 이상이어야 합니다.")

    end_day = latest_completed_session_end()
    start_day = end_day - timedelta(days=65)
    if universe_cache:
        universe = universe_cache
    else:
        universe = load_universe()
        if isinstance(universe_cache, list):
            universe_cache.extend(universe)
    cache = bar_cache if bar_cache is not None else {}
    if bar_cache is not None:
        for key in list(bar_cache):
            if isinstance(key, tuple) and len(key) == 3 and key[2] != end_day.isoformat():
                bar_cache.pop(key, None)
    market_sessions = {}
    for market, index_code in (("코스피", "0001"), ("코스닥", "1001")):
        try:
            index_rows = client.index_bars(index_code, end_day)
            dates = sorted({datetime.strptime(str(row["stck_bsop_date"]), "%Y%m%d").date()
                            for row in index_rows if row.get("stck_bsop_date")})
            dates = [day for day in dates if day <= end_day]
            if len(dates) < 6:
                raise ContinuationScanError(f"{market} 거래일 달력이 6거래일 미만입니다.")
            market_sessions[market] = dates[-6:]
        except Exception as exc:
            raise ContinuationScanError(f"{market} 거래일 확인 실패: {str(exc)[:140]}") from exc
    qualified, failures = [], []

    for index, item in enumerate(universe, start=1):
        cache_key = (item["code"], start_day.isoformat(), end_day.isoformat())
        try:
            bars = cache.get(cache_key)
            if bars is None:
                bars = fetch_daily_bars(client, item["code"], start_day, end_day)
                cache[cache_key] = bars
                if pause_seconds:
                    time.sleep(pause_seconds)
            bars = [bar for bar in bars if bar["date"] <= end_day]
            result = evaluate_five_session_rise(item, bars, market_sessions[item["market"]])
            if result:
                qualified.append(result)
        except Exception as exc:
            message = str(exc)
            if any(term in message for term in ("인증 거부", "HTTP 401", "HTTP 403",
                                                  "HTTP 429", "호출 제한")):
                raise ContinuationScanError(
                    "증권사 인증 또는 호출 제한으로 전체 스캔을 중단했습니다. "
                    "키 환경과 호출 간격을 확인하세요. " + message[:180]
                ) from exc
            failures.append({"종목코드": item["code"], "종목명": item["name"],
                             "오류": message[:160]})
        if on_progress:
            on_progress(index, len(universe), item["name"])

    qualified.sort(key=lambda row: (
        -row["5일누적상승률(%)"],
        -(row["5일평균거래대금(억원)"] or 0),
        row["종목코드"],
    ))
    return {
        "기준일": end_day.isoformat(),
        "대상종목수": len(universe),
        "조회성공": len(universe) - len(failures),
        "조회실패": len(failures),
        "전체조회완료": len(failures) == 0,
        "연속상승종목수": len(qualified),
        "top": qualified[:top_n],
        "failures": failures[:50],
        "순위기준": "5일 누적 상승률 내림차순, 동률이면 5일 평균 거래대금 내림차순",
        "가격기준": "증권사 수정주가 기준",
    }


def _amount(rows, sections, account_ids, account_names):
    for row in rows:
        if row.get("sj_div") not in sections:
            continue
        if row.get("account_id") not in account_ids and row.get("account_nm") not in account_names:
            continue
        raw = row.get("thstrm_amount")
        if raw in (None, ""):
            continue
        try:
            return float(str(raw).replace(",", ""))
        except ValueError:
            continue
    return None


def enrich_candidate(client, official, code: str, asof: date) -> dict:
    """Add KIS PBR and optional DART 3-year sales/profit/ROE/operating cash flow."""
    result = {"PBR": None, "ROE(%)": None, "3년매출변화(%)": None,
              "3년영업이익변화(%)": None, "최근영업현금흐름(억원)": None,
              "재무점검(4)": "자료 미연결", "재무기준": None}

    # KIS exposes PBR on its quote endpoint. Toss stock master currently
    # does not guarantee a PBR field, so do not infer or fabricate it.
    if hasattr(client, "base") and hasattr(client, "key") and hasattr(client, "secret"):
        try:
            client.authorize()
            headers = {
                "authorization": "Bearer " + client.token, "appkey": client.key,
                "appsecret": client.secret, "tr_id": QUOTE_TR_ID, "custtype": "P",
            }
            _, quote = client.call("GET", QUOTE_PATH, headers=headers, params={
                "FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code,
            })
            if str(quote.get("rt_cd")) == "0" and isinstance(quote.get("output"), dict):
                raw_pbr = quote["output"].get("pbr")
                if raw_pbr not in (None, ""):
                    value = float(str(raw_pbr).replace(",", ""))
                    if value >= 0:
                        result["PBR"] = round(value, 2)
        except Exception:
            pass

    if official is None or not getattr(official, "dart_key", ""):
        result["재무점검(4)"] = "DART 키 미연결"
        return result

    try:
        corp = official.corp(code)
        years = list(range(asof.year - 1, asof.year - 6, -1))
        chosen = None
        for basis in ("CFS", "OFS"):
            annual = {}
            for year in years:
                payload = official.dart("fnlttSinglAcntAll.json", corp_code=corp,
                                         bsns_year=str(year), reprt_code="11011", fs_div=basis)
                if not payload:
                    continue
                rows = payload.get("list", [])
                revenue = _amount(rows, ("IS", "CIS"), ("ifrs-full_Revenue",),
                                  ("매출액", "수익(매출액)"))
                profit = _amount(rows, ("IS", "CIS"), ("dart_OperatingIncomeLoss",),
                                 ("영업이익", "영업이익(손실)"))
                net_income = _amount(rows, ("IS", "CIS"),
                                     ("ifrs-full_ProfitLoss", "ifrs-full_ProfitLossAttributableToOwnersOfParent"),
                                     ("당기순이익", "당기순이익(손실)",
                                      "지배기업의 소유주에게 귀속되는 당기순이익(손실)"))
                equity = _amount(rows, ("BS",),
                                 ("ifrs-full_Equity", "ifrs-full_EquityAttributableToOwnersOfParent"),
                                 ("자본총계", "자본총계(비지배지분 포함)"))
                ocf = _amount(rows, ("CF",),
                              ("ifrs-full_CashFlowsFromUsedInOperatingActivities",
                               "dart_CashFlowsFromUsedInOperatingActivities"),
                              ("영업활동현금흐름", "영업활동으로 인한 현금흐름"))
                if revenue is not None and profit is not None:
                    annual[year] = {"revenue": revenue, "profit": profit,
                                    "net_income": net_income, "equity": equity, "ocf": ocf}
            ordered = sorted(annual)
            if len(ordered) >= 3:
                # Use the latest three consecutive filed years, not scattered observations.
                triples = [ordered[i:i+3] for i in range(len(ordered)-2)
                           if ordered[i+2] - ordered[i] == 2]
                if triples:
                    chosen = (basis, annual, triples[-1])
                    break

        if chosen:
            basis, annual, selected_years = chosen
            first, middle, latest = (annual[year] for year in selected_years)
            result["3년매출변화(%)"] = round((latest["revenue"] / first["revenue"] - 1) * 100, 2) if first["revenue"] > 0 else None
            result["3년영업이익변화(%)"] = round((latest["profit"] / first["profit"] - 1) * 100, 2) if first["profit"] > 0 else None
            result["최근영업현금흐름(억원)"] = round(latest["ocf"] / 100_000_000, 1) if latest["ocf"] is not None else None
            previous_equity = annual.get(selected_years[-1] - 1, {}).get("equity")
            if latest["net_income"] is not None and latest["equity"] and previous_equity:
                average_equity = (latest["equity"] + previous_equity) / 2
                if average_equity > 0:
                    result["ROE(%)"] = round(latest["net_income"] / average_equity * 100, 2)
            result["재무기준"] = f"DART {basis} · {selected_years[0]}~{selected_years[-1]}"
        checks = [
            result["PBR"] is not None and result["PBR"] < 1,
            result["ROE(%)"] is not None and result["ROE(%)"] >= 15,
            result["3년영업이익변화(%)"] is not None and result["3년영업이익변화(%)"] > 0,
            result["최근영업현금흐름(억원)"] is not None and result["최근영업현금흐름(억원)"] > 0,
        ]
        known = sum(value is not None for value in (
            result["PBR"], result["ROE(%)"], result["3년영업이익변화(%)"],
            result["최근영업현금흐름(억원)"]))
        result["재무점검(4)"] = f"{sum(checks)}/4 (확인 {known}/4)"
        return result
    except Exception as exc:
        result["재무점검(4)"] = "DART 조회 보류"
        result["재무오류"] = str(exc)[:160]
        return result
