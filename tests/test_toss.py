import unittest
from datetime import date
from unittest.mock import patch

from predash.toss import Toss


class FakeResponse:
    def __init__(self, payload, status=200, headers=None):
        self.payload = payload
        self.status_code = status
        self.headers = headers or {}

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("HTTP error")


class TossAdapterTests(unittest.TestCase):
    def make_client(self):
        client = Toss(settings={
            "key": "client-id", "secret": "client-secret", "account": "12345"
        })
        client.token = "test-token"
        client.expires = 9999999999
        return client

    @patch("predash.toss.requests.request")
    def test_balance_normalizes_holdings(self, request):
        request.return_value = FakeResponse({
            "result": {
                "items": [{
                    "symbol": "005930", "name": "삼성전자", "quantity": "2",
                    "lastPrice": "70000", "averagePurchasePrice": "65000",
                    "evaluationAmount": "140000", "profitLossAmount": "10000"
                }],
                "totalEvaluationAmount": "140000",
                "totalProfitLossAmount": "10000"
            }
        })
        result = self.make_client().balance()
        self.assertEqual(result["value"], 140000)
        self.assertEqual(result["positions"][0]["code"], "005930")
        self.assertEqual(result["positions"][0]["weight"], 100)

    @patch("predash.toss.requests.request")
    def test_daily_bars_range_maps_adjusted_candles(self, request):
        request.return_value = FakeResponse({
            "result": {"candles": [
                {"timestamp": "2026-11-20T00:00:00+09:00", "closePrice": "120",
                 "volume": "100", "highPrice": "121", "lowPrice": "118"},
                {"timestamp": "2026-11-19T00:00:00+09:00", "closePrice": "110",
                 "volume": "90", "highPrice": "112", "lowPrice": "108"}
            ]}
        })
        rows = self.make_client().daily_bars_range(
            "005930", date(2026, 11, 19), date(2026, 11, 20)
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["close"], 110)
        self.assertEqual(rows[1]["turnover"], 12000)

    @patch("predash.toss.requests.request")
    def test_index_bars_maps_market_indicator_candles(self, request):
        request.return_value = FakeResponse({
            "result": {"candles": [
                {"timestamp": "2026-11-20T00:00:00+09:00", "closePrice": "3000"}
            ]}
        })
        rows = self.make_client().index_bars("0001", date(2026, 11, 20))
        self.assertEqual(rows[0]["stck_bsop_date"], "20261120")
        self.assertEqual(rows[0]["bstp_nmix_prpr"], "3000")

    @patch("predash.toss.requests.request")
    def test_fills_normalizes_closed_order_execution(self, request):
        request.return_value = FakeResponse({
            "result": {"orders": [{
                "orderId": "order-1", "symbol": "005930", "name": "삼성전자",
                "side": "BUY", "orderedAt": "2026-11-19T10:00:00+09:00",
                "execution": {"filledQuantity": "3", "averageFilledPrice": "70000",
                              "filledAmount": "210000",
                              "filledAt": "2026-11-19T10:01:00+09:00"}
            }]}
        })
        payload = self.make_client().fills(30)
        row = payload["rows"][0]
        self.assertEqual(row["sll_buy_dvsn_cd"], "02")
        self.assertEqual(row["tot_ccld_qty"], "3")
        self.assertEqual(row["ord_dt"], "20261119")


if __name__ == "__main__":
    unittest.main()
