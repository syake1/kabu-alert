import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

import alert


JST = ZoneInfo("Asia/Tokyo")


def history_at(times):
    index = pd.DatetimeIndex(
        [datetime(2026, 8, 14, hour, minute, tzinfo=JST) for hour, minute in times]
    )
    size = len(index)
    return pd.DataFrame(
        {
            "Open": range(100, 100 + size),
            "High": range(102, 102 + size),
            "Low": range(98, 98 + size),
            "Close": range(101, 101 + size),
        },
        index=index,
    )


class PrepareDataTests(unittest.TestCase):
    def test_excludes_unconfirmed_bar(self):
        hist = history_at([(9, 0)] * 30 + [(10, 0), (10, 15)])
        # 重複時刻は計算自体には影響しないため、確定足フィルターだけを検証する。
        now = datetime(2026, 8, 14, 10, 29, tzinfo=JST)

        with patch.object(alert, "get_history", return_value=hist), patch.object(
            alert, "now_jst", return_value=now
        ):
            result = alert.prepare_data("1234")

        self.assertEqual(result.index[-1].strftime("%H:%M"), "10:00")


class TransitionTests(unittest.TestCase):
    def transition_history(self):
        hist = history_at([(9, 45), (10, 0)])
        hist["SAR_trend"] = [-1, 1]
        hist["SAR"] = [105.0, 98.0]
        return hist

    def test_accepts_recent_confirmed_transition(self):
        now = datetime(2026, 8, 14, 10, 20, tzinfo=JST)
        result = alert.find_latest_buy_transition(self.transition_history(), now=now)
        self.assertIsNotNone(result)
        self.assertEqual(result.name.strftime("%H:%M"), "10:00")

    def test_rejects_transition_delayed_by_about_an_hour(self):
        now = datetime(2026, 8, 14, 11, 3, tzinfo=JST)
        result = alert.find_latest_buy_transition(self.transition_history(), now=now)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
