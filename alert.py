"""
kabu-alert
監視銘柄（watchlist.json）だけを15分足で監視し、
パラボリックSARが「下向き → 上向き」に転換した確定足をDiscordへ通知する。

重要:
- GitHub ActionsはUTCで動くため、判定は必ずAsia/Tokyo(JST)で行う。
- 未確定の15分足は判定対象から除外する。
- 監視対象はwatchlist.jsonに登録された銘柄だけ。
- 同じ転換足はalert_state.jsonで重複通知しない。
- 古い日の転換を翌日に通知しない。
- 過去の転換足をさかのぼって通知せず、最新確定足だけを判定する。
"""

import json
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
import pandas as pd
import yfinance as yf

DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")
HIST_PERIOD = "60d"
INTERVAL = "15m"
STATE_FILE = "alert_state.json"
JST = ZoneInfo("Asia/Tokyo")


def now_jst():
    return datetime.now(JST)


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def get_history(code):
    """Yahoo Financeから15分足を取得し、JSTへ統一する。"""
    try:
        ticker = yf.Ticker(f"{code}.T")
        hist = ticker.history(
            period=HIST_PERIOD,
            interval=INTERVAL,
            auto_adjust=False,
            prepost=False,
        )

        if hist is None or hist.empty:
            return None

        if hist.index.tz is None:
            hist.index = hist.index.tz_localize("UTC")

        hist.index = hist.index.tz_convert(JST)
        hist = hist.sort_index()

        return hist

    except Exception as e:
        print(f"{code}: 15分足取得エラー: {e}")
        return None


def calculate_parabolic_sar(
    high,
    low,
    af_start=0.02,
    af_step=0.02,
    af_max=0.20,
):
    """一般的なパラボリックSARの上昇/下降トレンドを計算する。"""

    n = len(high)

    if n < 3:
        return pd.Series(dtype=int), pd.Series(dtype=float)

    h = high.reset_index(drop=True).astype(float)
    l = low.reset_index(drop=True).astype(float)

    sar = [0.0] * n
    trend = [1] * n
    ep = [0.0] * n
    af = [af_start] * n

    # 初期方向を最初の2本から決定
    if h.iloc[1] >= h.iloc[0]:
        trend[0] = 1
        sar[0] = l.iloc[0]
        ep[0] = h.iloc[0]
    else:
        trend[0] = -1
        sar[0] = h.iloc[0]
        ep[0] = l.iloc[0]

    for i in range(1, n):

        prev_trend = trend[i - 1]
        prev_sar = sar[i - 1]
        prev_ep = ep[i - 1]
        prev_af = af[i - 1]

        if prev_trend == 1:

            new_sar = prev_sar + prev_af * (prev_ep - prev_sar)

            if i >= 2:
                new_sar = min(
                    new_sar,
                    l.iloc[i - 1],
                    l.iloc[i - 2],
                )
            else:
                new_sar = min(
                    new_sar,
                    l.iloc[i - 1],
                )

            if l.iloc[i] < new_sar:

                trend[i] = -1
                sar[i] = prev_ep
                ep[i] = l.iloc[i]
                af[i] = af_start

            else:

                trend[i] = 1
                sar[i] = new_sar

                if h.iloc[i] > prev_ep:

                    ep[i] = h.iloc[i]
                    af[i] = min(
                        prev_af + af_step,
                        af_max,
                    )

                else:

                    ep[i] = prev_ep
                    af[i] = prev_af

        else:

            new_sar = prev_sar + prev_af * (prev_ep - prev_sar)

            if i >= 2:
                new_sar = max(
                    new_sar,
                    h.iloc[i - 1],
                    h.iloc[i - 2],
                )
            else:
                new_sar = max(
                    new_sar,
                    h.iloc[i - 1],
                )

            if h.iloc[i] > new_sar:

                trend[i] = 1
                sar[i] = prev_ep
                ep[i] = h.iloc[i]
                af[i] = af_start

            else:

                trend[i] = -1
                sar[i] = new_sar

                if l.iloc[i] < prev_ep:

                    ep[i] = l.iloc[i]
                    af[i] = min(
                        prev_af + af_step,
                        af_max,
                    )

                else:

                    ep[i] = prev_ep
                    af[i] = prev_af

    return (
        pd.Series(
            trend,
            index=high.index,
            dtype=int,
        ),
        pd.Series(
            sar,
            index=high.index,
            dtype=float,
        ),
    )


def prepare_data(code):
    hist = get_history(code)

    if hist is None or len(hist) < 30:
        return None

    now = now_jst()

    # 未確定15分足を除外
    # 例: 10:00足は10:15以降に確定足として扱う
    hist = hist[
        hist.index + timedelta(minutes=15) <= now
    ]

    if hist.empty:
        return None

    # 日本市場の立会時間帯のみ
    start_time = datetime.strptime(
        "09:00",
        "%H:%M",
    ).time()

    end_time = datetime.strptime(
        "15:15",
        "%H:%M",
    ).time()

    hist = hist[
        (hist.index.time >= start_time)
        &
        (hist.index.time <= end_time)
    ]

    if len(hist) < 30:
        return None

    trend, sar = calculate_parabolic_sar(
        hist["High"],
        hist["Low"],
    )

    hist = hist.copy()
    hist["SAR_trend"] = trend
    hist["SAR"] = sar

    return hist


def find_latest_buy_transition(hist):
    """
    最新確定15分足だけで
    SAR 下向き → 上向き
    を判定する。
    """

    if hist is None or len(hist) < 2:
        return None

    today = now_jst().date()

    day = hist[
        hist.index.date == today
    ]

    if len(day) < 2:
        return None

    prev_bar = day.iloc[-2]
    latest_bar = day.iloc[-1]

    prev_trend = int(
        prev_bar["SAR_trend"]
    )

    latest_trend = int(
        latest_bar["SAR_trend"]
    )

    print(
        "  → 判定:"
        f" 前足 {prev_bar.name.strftime('%H:%M')}"
        f" SAR {'上' if prev_trend == 1 else '下'}"
        " →"
        f" 最新足 {latest_bar.name.strftime('%H:%M')}"
        f" SAR {'上' if latest_trend == 1 else '下'}"
    )

    if (
        prev_trend == -1
        and latest_trend == 1
    ):
        return latest_bar

    return None


def send_discord(
    code,
    name,
    bar,
    state,
):

    if not DISCORD_WEBHOOK:
        print(
            "❌ DISCORD_WEBHOOK が設定されていません"
        )
        return False

    bar_time = bar.name

    state_key = (
        f"{code}_buy_sar"
    )

    bar_key = (
        bar_time.isoformat()
    )

    if state.get(state_key) == bar_key:

        print(
            f"  → {code} は通知済み:"
            f" {bar_time.strftime('%m/%d %H:%M')}"
        )

        return False

    price = float(
        bar["Close"]
    )

    sar = float(
        bar["SAR"]
    )

    detected_time = now_jst()

    msg = (
        "🔔 **【パラボリック買いサイン】**\n"
        f"**{code} {name}**\n\n"
        "🎯 パラボリックSAR **下向き → 上向き**\n"
        f"💰 終値: **{price:,.0f}円**\n"
        f"📍 SAR: **{sar:,.0f}円**\n"
        f"⏰ 対象足: **{bar_time.strftime('%Y/%m/%d %H:%M')} JST**\n"
        f"📡 検出時刻: **{detected_time.strftime('%H:%M:%S')} JST**\n\n"
        "📱 SBIアプリでチャートを確認してください。"
    )

    try:

        response = requests.post(
            DISCORD_WEBHOOK,
            json={"content": msg},
            timeout=15,
        )

        if (
            response.status_code < 200
            or response.status_code >= 300
        ):

            print(
                "❌ Discord送信失敗:"
                f" HTTP {response.status_code}"
                f" {response.text[:300]}"
            )

            return False

        state[state_key] = bar_key

        print(
            f"✅ Discord通知:"
            f" {code} {name}"
            f" / {bar_time.strftime('%m/%d %H:%M')}"
            f" / 検出 {detected_time.strftime('%H:%M:%S')}"
        )

        return True

    except Exception as e:

        print(
            f"❌ Discord送信エラー: {e}"
        )

        return False


def main():

    now = now_jst()

    print("=" * 60)

    print(
        "=== kabu-alert 開始 "
        f"{now.strftime('%Y/%m/%d %H:%M:%S')}"
        " JST ==="
    )

    if now.weekday() >= 5:

        print(
            "土日のため終了"
        )

        return

    current_minutes = (
        now.hour * 60
        + now.minute
    )

    # 9:00～15:30のみ実行
    if (
        current_minutes < 9 * 60
        or current_minutes > 15 * 60 + 30
    ):

        print(
            "取引時間外（JST）のため終了"
        )

        return

    try:

        with open(
            "watchlist.json",
            "r",
            encoding="utf-8",
        ) as f:

            data = json.load(f)

        watchlist = data.get(
            "watchlist",
            [],
        )

    except Exception as e:

        print(
            "❌ watchlist.json"
            f" 読み込みエラー: {e}"
        )

        return

    if not watchlist:

        print(
            "監視銘柄なし"
        )

        return

    print(
        f"監視銘柄数: {len(watchlist)}"
    )

    print(
        "対象: watchlist.jsonに登録された銘柄のみ"
        " / 最新15分確定足のみ"
    )

    state = load_state()

    alert_count = 0

    for stock in watchlist:

        code = str(
            stock.get(
                "code",
                "",
            )
        ).strip()

        name = str(
            stock.get(
                "name",
                code,
            )
        ).strip()

        if not code:
            continue

        print(
            f"\n--- チェック:"
            f" {code} {name} ---"
        )

        hist = prepare_data(code)

        if hist is None:

            print(
                "  → 15分足データ不足"
            )

            continue

        latest = hist.iloc[-1]

        print(
            "  → 最新確定足:"
            f" {latest.name.strftime('%Y/%m/%d %H:%M')}"
            " JST"
        )

        bar = find_latest_buy_transition(
            hist
        )

        if bar is None:

            trend_text = (
                "上向き"
                if int(
                    latest["SAR_trend"]
                ) == 1
                else "下向き"
            )

            print(
                "  → 買い転換なし"
                f" / SAR {trend_text}"
            )

            continue

        print(
            "  → 🎯 買い転換検出:"
            f" {bar.name.strftime('%Y/%m/%d %H:%M')}"
            " JST"
        )

        if send_discord(
            code,
            name,
            bar,
            state,
        ):

            alert_count += 1

    save_state(state)

    print(
        f"\n=== 完了"
        f" / Discord新規通知"
        f" {alert_count}件 ==="
    )

    print("=" * 60)


if __name__ == "__main__":
    main()
