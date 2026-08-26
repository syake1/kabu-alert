"""
kabu-alert
監視銘柄（watchlist.json）だけを5分足で監視し、
パラボリックSARが「下向き → 上向き」に転換した確定足を買いサインとして、
「上向き → 下向き」に転換した確定足を空売りサインとしてDiscordへ通知する。

重要:
- GitHub ActionsはUTCで動くため、判定は必ずAsia/Tokyo(JST)で行う。
- 未確定の5分足は判定対象から除外する。
- 監視対象はwatchlist.jsonに登録された銘柄だけ。
- 同じ転換足はalert_state.jsonで重複通知しない（買い・空売りは別キーで管理）。
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
INTERVAL = "5m"
BAR_MINUTES = 5
MAX_SIGNAL_DELAY = timedelta(minutes=10)
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
    """Yahoo Financeから5分足を取得し、JSTへ統一する。"""
    try:
        ticker = yf.Ticker(f"{code}.T")
        hist = ticker.history(period=HIST_PERIOD, interval=INTERVAL, auto_adjust=False, prepost=False)
        if hist is None or hist.empty:
            return None
        if hist.index.tz is None:
            hist.index = hist.index.tz_localize("UTC")
        hist.index = hist.index.tz_convert(JST)
        return hist.sort_index()
    except Exception as e:
        print(f"{code}: 5分足取得エラー: {e}")
        return None


def calculate_parabolic_sar(high, low, af_start=0.02, af_step=0.02, af_max=0.20):
    n = len(high)
    if n < 3:
        return pd.Series(dtype=int), pd.Series(dtype=float)
    h = high.reset_index(drop=True).astype(float)
    l = low.reset_index(drop=True).astype(float)
    sar = [0.0] * n
    trend = [1] * n
    ep = [0.0] * n
    af = [af_start] * n
    if h.iloc[1] >= h.iloc[0]:
        trend[0], sar[0], ep[0] = 1, l.iloc[0], h.iloc[0]
    else:
        trend[0], sar[0], ep[0] = -1, h.iloc[0], l.iloc[0]
    for i in range(1, n):
        pt, ps, pe, pa = trend[i-1], sar[i-1], ep[i-1], af[i-1]
        ns = ps + pa * (pe - ps)
        if pt == 1:
            ns = min(ns, l.iloc[i-1], l.iloc[i-2]) if i >= 2 else min(ns, l.iloc[i-1])
            if l.iloc[i] < ns:
                trend[i], sar[i], ep[i], af[i] = -1, pe, l.iloc[i], af_start
            else:
                trend[i], sar[i] = 1, ns
                if h.iloc[i] > pe:
                    ep[i], af[i] = h.iloc[i], min(pa + af_step, af_max)
                else:
                    ep[i], af[i] = pe, pa
        else:
            ns = max(ns, h.iloc[i-1], h.iloc[i-2]) if i >= 2 else max(ns, h.iloc[i-1])
            if h.iloc[i] > ns:
                trend[i], sar[i], ep[i], af[i] = 1, pe, h.iloc[i], af_start
            else:
                trend[i], sar[i] = -1, ns
                if l.iloc[i] < pe:
                    ep[i], af[i] = l.iloc[i], min(pa + af_step, af_max)
                else:
                    ep[i], af[i] = pe, pa
    return pd.Series(trend, index=high.index, dtype=int), pd.Series(sar, index=high.index, dtype=float)


def prepare_data(code):
    hist = get_history(code)
    if hist is None or len(hist) < 30:
        return None
    now = now_jst()
    hist = hist[hist.index + timedelta(minutes=BAR_MINUTES) <= now]
    if hist.empty:
        return None
    start_time = datetime.strptime("09:00", "%H:%M").time()
    end_time = datetime.strptime("15:25", "%H:%M").time()
    hist = hist[(hist.index.time >= start_time) & (hist.index.time <= end_time)]
    if len(hist) < 30:
        return None
    trend, sar = calculate_parabolic_sar(hist["High"], hist["Low"])
    hist = hist.copy()
    hist["SAR_trend"], hist["SAR"] = trend, sar
    return hist


def _find_latest_transition(hist, from_trend, to_trend, now=None):
    if hist is None or len(hist) < 2:
        return None
    now = now or now_jst()
    day = hist[hist.index.date == now.date()]
    if len(day) < 2:
        return None
    prev_bar, latest_bar = day.iloc[-2], day.iloc[-1]
    signal_delay = now - (latest_bar.name + timedelta(minutes=BAR_MINUTES))
    if signal_delay > MAX_SIGNAL_DELAY:
        print(f"  → 最新データが古いため判定を見送り: {latest_bar.name.strftime('%H:%M')}足 / 確定から{int(signal_delay.total_seconds() // 60)}分")
        return None
    prev_trend, latest_trend = int(prev_bar["SAR_trend"]), int(latest_bar["SAR_trend"])
    print(f"  → 判定: 前足 {prev_bar.name.strftime('%H:%M')} SAR {'上' if prev_trend == 1 else '下'} → 最新足 {latest_bar.name.strftime('%H:%M')} SAR {'上' if latest_trend == 1 else '下'}")
    return latest_bar if prev_trend == from_trend and latest_trend == to_trend else None


def find_latest_buy_transition(hist, now=None):
    return _find_latest_transition(hist, -1, 1, now)


def find_latest_short_transition(hist, now=None):
    return _find_latest_transition(hist, 1, -1, now)


def send_discord(code, name, bar, state, kind="buy"):
    if not DISCORD_WEBHOOK:
        print("❌ DISCORD_WEBHOOK が設定されていません")
        return False
    bar_time = bar.name
    state_key = f"{code}_buy_sar_5m" if kind == "buy" else f"{code}_short_sar_5m"
    bar_key = bar_time.isoformat()
    if state.get(state_key) == bar_key:
        return False
    price, sar = float(bar["Close"]), float(bar["SAR"])
    detected_time = now_jst()
    if kind == "buy":
        title, direction = "🔔 **【5分足 パラボリック買いサイン】**", "🎯 パラボリックSAR **下向き → 上向き**"
    else:
        title, direction = "🔻 **【5分足 パラボリック空売りサイン】**", "🎯 パラボリックSAR **上向き → 下向き**"
    msg = (f"{title}\n**{code} {name}**\n\n{direction}\n💰 終値: **{price:,.0f}円**\n📍 SAR: **{sar:,.0f}円**\n⏰ 対象足: **{bar_time.strftime('%Y/%m/%d %H:%M')} JST**\n📡 検出時刻: **{detected_time.strftime('%H:%M:%S')} JST**\n\n📱 SBIアプリで15分足チャートも確認してください。")
    try:
        response = requests.post(DISCORD_WEBHOOK, json={"content": msg}, timeout=15)
        if not 200 <= response.status_code < 300:
            print(f"❌ Discord送信失敗: HTTP {response.status_code} {response.text[:300]}")
            return False
        state[state_key] = bar_key
        print(f"✅ Discord通知: {code} {name} / {bar_time.strftime('%m/%d %H:%M')} / {kind}")
        return True
    except Exception as e:
        print(f"❌ Discord送信エラー: {e}")
        return False


def main():
    now = now_jst()
    print("=" * 60)
    print(f"=== kabu-alert 5分足 開始 {now.strftime('%Y/%m/%d %H:%M:%S')} JST ===")
    if now.weekday() >= 5:
        print("土日のため終了"); return
    current_minutes = now.hour * 60 + now.minute
    if current_minutes < 9 * 60 or current_minutes > 15 * 60 + 30:
        print("取引時間外（JST）のため終了"); return
    try:
        with open("watchlist.json", "r", encoding="utf-8") as f:
            watchlist = json.load(f).get("watchlist", [])
    except Exception as e:
        print(f"❌ watchlist.json 読み込みエラー: {e}"); return
    if not watchlist:
        print("監視銘柄なし"); return
    print(f"監視銘柄数: {len(watchlist)}")
    print("対象: watchlist.json登録銘柄 / 最新5分確定足 / 買い・空売り両方向")
    state, alert_count = load_state(), 0
    for stock in watchlist:
        code = str(stock.get("code", "")).strip()
        name = str(stock.get("name", code)).strip()
        if not code: continue
        print(f"\n--- チェック: {code} {name} ---")
        hist = prepare_data(code)
        if hist is None:
            print("  → 5分足データ不足"); continue
        latest = hist.iloc[-1]
        print(f"  → 最新確定足: {latest.name.strftime('%Y/%m/%d %H:%M')} JST")
        buy_bar = find_latest_buy_transition(hist)
        if buy_bar is not None and send_discord(code, name, buy_bar, state, "buy"):
            alert_count += 1
        short_bar = find_latest_short_transition(hist)
        if short_bar is not None and send_discord(code, name, short_bar, state, "short"):
            alert_count += 1
        if buy_bar is None and short_bar is None:
            print(f"  → 転換なし / SAR {'上向き' if int(latest['SAR_trend']) == 1 else '下向き'}")
    save_state(state)
    print(f"\n=== 完了 / Discord新規通知 {alert_count}件 ===")
    print("=" * 60)


if __name__ == "__main__":
    main()
