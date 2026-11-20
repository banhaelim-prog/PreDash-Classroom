"""Read-only Toss Securities Open API adapter for PreDash."""
from __future__ import annotations

import re
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

BASE = "https://openapi.tossinvest.com"

from predash.kis import BrokerError


def account_settings():
    """Credentials are supplied by the current Streamlit session."""
    import streamlit as st
    values = st.session_state.get("classroom_credentials", {})
    if values.get("provider") == "toss":
        return {
            "provider": "toss",
            "mode": "real",
            "key": values.get("client_id", ""),
            "secret": values.get("client_secret", ""),
            "account": str(values.get("account_seq", "")),
        }
    from predash.kis import account_settings as kis_settings
    settings = kis_settings()
    settings["provider"] = "kis"
    return settings


def _number(value, label="수치"):
    try:
        result = float(str(value).replace(",", ""))
        if result != result or abs(result) == float("inf"):
            raise ValueError
        return result
    except (TypeError, ValueError):
        raise BrokerError(f"토스증권 응답의 {label} 값을 확인할 수 없습니다.") from None


class Toss:
    """Toss read-only adapter. No order submission methods are implemented."""

    def __init__(self, *, settings):
        self.key = str(settings.get("key", "")).strip()
        self.secret = str(settings.get("secret", "")).strip()
        self.account = str(settings.get("account", "")).strip()
        if not self.key or not self.secret:
            raise BrokerError("토스증권 Open API client_id와 client_secret을 입력하세요.")
        if not self.account.isdigit():
            raise BrokerError("토스증권 계좌 목록에서 확인한 accountSeq를 입력하세요.")
        self.mode = "real"
        self.token = None
        self.expires = 0

    def _request(self, method, path, *, account=False, **kwargs):
        self.authorize()
        headers = dict(kwargs.pop("headers", {}) or {})
        headers["Authorization"] = "Bearer " + self.token
        if account:
            headers["X-Tossinvest-Account"] = self.account
        try:
            response = requests.request(
                method, BASE + path, headers=headers, timeout=(5, 25), **kwargs
            )
            if response.status_code == 403:
                raise BrokerError(
                    "토스증권 API 호출이 403으로 거부됐습니다. WTS > 설정 > Open API > "
                    "허용 IP 관리에 현재 Streamlit 서버의 외부 IP가 등록됐는지 확인하세요."
                )
            if response.status_code == 401:
                self.token = None
                raise BrokerError("토스증권 인증 토큰이 거부됐습니다. client_id·client_secret을 확인하세요.")
            if response.status_code == 429:
                wait = response.headers.get("Retry-After", "1")
                raise BrokerError(f"토스증권 API 호출 한도 초과(429). {wait}초 후 다시 시도하세요.")
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError
        except BrokerError:
            raise
        except (requests.RequestException, ValueError):
            raise BrokerError("토스증권 API 연결 또는 응답 확인에 실패했습니다.") from None
        if isinstance(data.get("error"), dict):
            code = str(data["error"].get("code", "unknown"))
            safe = code if re.fullmatch(r"[a-z0-9-]{2,48}", code) else "unknown"
            raise BrokerError(f"토스증권 API 오류: {safe}")
        return data

    def authorize(self):
        if self.token and time.time() < self.expires:
            return
        try:
            response = requests.post(
                BASE + "/oauth2/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.key,
                    "client_secret": self.secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=(5, 20),
            )
            if response.status_code == 403:
                raise BrokerError("토스증권 인증이 403으로 거부됐습니다. Open API 허용 IP를 확인하세요.")
            response.raise_for_status()
            data = response.json()
        except BrokerError:
            raise
        except (requests.RequestException, ValueError):
            raise BrokerError("토스증권 액세스 토큰 발급에 실패했습니다.") from None
        token = data.get("access_token")
        if not token:
            error = data.get("error", {})
            code = error.get("code", "token-issue-failed") if isinstance(error, dict) else "token-issue-failed"
            raise BrokerError(f"토스증권 토큰 발급 실패: {str(code)[:48]}")
        self.token = token
        self.expires = time.time() + max(60, int(data.get("expires_in", 3600)) - 120)

    def accounts(self):
        data = self._request("GET", "/api/v1/accounts")
        result = data.get("result")
        if not isinstance(result, list):
            raise BrokerError("토스증권 계좌 목록 응답 형식을 확인할 수 없습니다.")
        return result

    def balance(self):
        data = self._request("GET", "/api/v1/holdings", account=True)
        result = data.get("result")
        if not isinstance(result, dict):
            raise BrokerError("토스증권 보유자산 응답 형식을 확인할 수 없습니다.")
        items = result.get("items")
        if not isinstance(items, list):
            raise BrokerError("토스증권 보유종목 목록이 응답에 없습니다.")
        positions = []
        for row in items:
            if not isinstance(row, dict):
                continue
            code = str(row.get("symbol", "")).strip()
            qty = _number(row.get("quantity", 0), "보유수량")
            if qty <= 0:
                continue
            price = _number(row.get("lastPrice", 0), "현재가")
            avg = _number(row.get("averagePurchasePrice", 0), "평균매입가")
            value = _number(row.get("evaluationAmount", qty * price), "평가금액")
            pnl = _number(row.get("profitLossAmount", value - qty * avg), "평가손익")
            positions.append({
                "code": code, "name": str(row.get("name") or code),
                "quantity": qty, "average_cost": avg, "price": price,
                "value": value, "pnl": pnl,
            })
        total = sum(p["value"] for p in positions)
        for p in positions:
            p["weight"] = p["value"] / total * 100 if total > 0 else 0
        total_value = result.get("totalEvaluationAmount", total)
        total_pnl = result.get("totalProfitLossAmount", sum(p["pnl"] for p in positions))
        return {
            "positions": positions, "value": _number(total_value, "총 평가금액"),
            "pnl": _number(total_pnl, "총 평가손익"), "cash": None,
            "mode": "real", "provider": "toss",
            "fetched": datetime.now(ZoneInfo("Asia/Seoul")).isoformat(),
        }

    def _candles(self, symbol, *, count=200, adjusted=True, before=None, indicator=False):
        if indicator:
            path = f"/api/v1/market-indicators/{symbol}/candles"
            params = {"interval": "1d", "count": min(200, count)}
        else:
            path = "/api/v1/candles"
            params = {"symbol": symbol, "interval": "1d",
                      "count": min(200, count), "adjusted": str(adjusted).lower()}
        if before:
            params["before"] = before
        data = self._request("GET", path, params=params)
        result = data.get("result")
        candles = result.get("candles") if isinstance(result, dict) else None
        if not isinstance(candles, list):
            raise BrokerError("토스증권 일봉 응답 형식을 확인할 수 없습니다.")
        return candles

    @staticmethod
    def _day(value):
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
        except ValueError:
            raise BrokerError("토스증권 캔들 날짜를 해석할 수 없습니다.") from None

    def daily_bars_range(self, code, start_day, end_day):
        candles = self._candles(code, count=200, adjusted=True)
        rows = []
        for item in candles:
            day = self._day(item.get("timestamp"))
            if start_day <= day <= end_day:
                close = _number(item.get("closePrice"), "종가")
                volume = _number(item.get("volume", 0), "거래량")
                rows.append({
                    "date": day, "close": close, "volume": int(volume),
                    "turnover": close * volume,
                })
        return sorted(rows, key=lambda row: row["date"])

    def index_bars(self, code, as_of=None):
        symbol = {"0001": "KOSPI", "1001": "KOSDAQ"}.get(code, code)
        candles = self._candles(symbol, count=200, indicator=True)
        end = as_of or datetime.now(ZoneInfo("Asia/Seoul")).date()
        rows = []
        for item in candles:
            day = self._day(item.get("timestamp"))
            if day <= end:
                rows.append({
                    "stck_bsop_date": day.strftime("%Y%m%d"),
                    "bstp_nmix_prpr": item.get("closePrice"),
                })
        return rows

    def daily_bars(self, code, end_day):
        start = end_day - timedelta(days=60)
        candles = self._candles(code, count=200, adjusted=False)
        rows = []
        for item in candles:
            day = self._day(item.get("timestamp"))
            if start <= day < end_day:
                rows.append({
                    "stck_bsop_date": day.strftime("%Y%m%d"),
                    "stck_hgpr": item.get("highPrice"),
                    "stck_lwpr": item.get("lowPrice"),
                    "stck_clpr": item.get("closePrice"),
                })
        return sorted(rows, key=lambda row: row["stck_bsop_date"])

    def investor_flow(self, code):
        data = self._request("GET", f"/api/v1/stocks/{code}/investor-trading")
        result = data.get("result")
        rows = result if isinstance(result, list) else result.get("items", []) if isinstance(result, dict) else []
        if not rows:
            raise BrokerError("토스증권 투자자별 매매동향 자료가 없습니다.")
        # Provider fields are not silently translated until the returned schema is verified.
        raise BrokerError("토스증권 투자자별 매매동향의 필드 매핑 검증이 필요합니다.")

    def market_flow(self, code, as_of=None):
        symbol = {"0001": "KOSPI", "1001": "KOSDAQ"}.get(code)
        if not symbol:
            raise BrokerError("지원하지 않는 시장입니다.")
        self._request("GET", f"/api/v1/market-indicators/{symbol}/investor-trading")
        raise BrokerError("토스증권 시장 투자자별 매매대금의 기간·필드 매핑 검증이 필요합니다.")

    def fills(self, days=30):
        raise BrokerError(
            "토스증권 주문내역 조회는 KIS 체결 응답과 구조가 다릅니다. "
            "실제 주문 상세의 체결 필드 확인 후 매매습관 화면에 연결해야 합니다."
        )
