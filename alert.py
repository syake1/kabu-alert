"""
アンチグラビティ・コア Pro+ アラートシステム
15分足でパラボリック転換を検知してDiscordに通知
・買いシグナル：パラボリック上転換＋BB中央線上抜け
・空売りシグナル：パラボリック下転換＋BB中央線下抜け
"""
import json
import os
import requests
import pandas as pd
import numpy as np
import yfinance as yf
from datetime import datetime

DISCORD_WEBHOOK = os.environ.get('DISCORD_WEBHOOK', '')

# ================================================================
# 指標計算
# ================================================================
def calculate_bb(data, window=20, num_std=2):
    mid = data['Close'].rolling(window).mean()
    std = data['Close'].rolling(window).std()
    return mid + num_std * std, mid, mid - num_std * std

def calculate_parabolic_sar(high, low, close, af_start=0.02, af_step=0.02, af_max=0.20):
    """パラボリックSARを計算"""
    n = len(close)
    sar   = [0.0] * n
    trend = [1]   * n
    ep    = [0.0] * n
    af    = [af_start] * n

    sar[0]   = low.iloc[0]
    ep[0]    = high.iloc[0]
    trend[0] = 1

    for i in range(1, n):
        prev_sar   = sar[i-1]
        prev_trend = trend[i-1]
        prev_ep    = ep[i-1]
        prev_af    = af[i-1]

        if prev_trend == 1:
            new_sar = prev_sar + prev_af * (prev_ep - prev_sar)
            new_sar = min(new_sar, low.iloc[i-1], low.iloc[max(0,i-2)])
            if low.iloc[i] < new_sar:
                trend[i] = -1; sar[i] = prev_ep; ep[i] = low.iloc[i]; af[i] = af_start
            else:
                trend[i] = 1; sar[i] = new_sar
                if high.iloc[i] > prev_ep:
                    ep[i] = high.iloc[i]; af[i] = min(prev_af + af_step, af_max)
                else:
                    ep[i] = prev_ep; af[i] = prev_af
        else:
            new_sar = prev_sar + prev_af * (prev_ep - prev_sar)
            new_sar = max(new_sar, high.iloc[i-1], high.iloc[max(0,i-2)])
            if high.iloc[i] > new_sar:
                trend[i] = 1; sar[i] = prev_ep; ep[i] = high.iloc[i]; af[i] = af_start
            else:
                trend[i] = -1; sar[i] = new_sar
                if low.iloc[i] < prev_ep:
                    ep[i] = low.iloc[i]; af[i] = min(prev_af + af_step, af_max)
                else:
                    ep[i] = prev_ep; af[i] = prev_af

    return pd.Series(trend, index=close.index), pd.Series(sar, index=close.index)

# ================================================================
# ★ 買いシグナルチェック（パラボリック上転換）
# ================================================================
def check_buy_signal(code, name, days):
    try:
        tk   = yf.Ticker(f"{code}.T")
        hist = tk.history(period="5d", interval="15m")
        if len(hist) < 20:
            return None

        bb_up, bb_mid, bb_lo = calculate_bb(hist)
        hist['BB_upper'] = bb_up
        hist['BB_mid']   = bb_mid
        hist['BB_lower'] = bb_lo
        trend, sar = calculate_parabolic_sar(hist['High'], hist['Low'], hist['Close'])
        hist['SAR_trend'] = trend
        hist['SAR']       = sar

        latest = hist.iloc[-1]
        prev   = hist.iloc[-2]

        current_price  = float(latest['Close'])
        bb_mid_val     = float(latest['BB_mid'])
        bb_lo_val      = float(latest['BB_lower'])
        bb_upper_val   = float(latest['BB_upper'])
        sar_trend_now  = int(hist['SAR_trend'].iloc[-1])
        sar_trend_prev = int(hist['SAR_trend'].iloc[-2])

        # ★ 必須条件：パラボリック上転換
        if not (sar_trend_prev == -1 and sar_trend_now == 1):
            return None

        signals = ["🎯 パラボリック上転換✅（必須）"]

        # BB中央線を陽線で上抜け
        if (latest['Close'] > bb_mid_val and
            latest['Open']  < bb_mid_val and
            latest['Close'] > latest['Open']):
            signals.append("📈 BB中央線上抜け✅")

        # 包み足陽線
        if (prev['Close'] < prev['Open'] and
            latest['Close'] > latest['Open'] and
            latest['Close'] > prev['Open'] and
            latest['Open']  < prev['Close']):
            signals.append("⚡ 包み足陽線✅")

        # 下ヒゲ陽線
        body       = abs(latest['Close'] - latest['Open'])
        lower_wick = min(latest['Close'], latest['Open']) - latest['Low']
        if latest['Close'] > latest['Open'] and body > 0 and lower_wick >= body * 1.5:
            signals.append("🔥 下ヒゲ陽線✅")

        # BB位置
        bb_range = bb_upper_val - bb_lo_val
        bb_pos   = ((current_price - bb_lo_val) / bb_range * 100) if bb_range > 0 else 50
        if bb_pos < 40:
            signals.append(f"📊 BB下限付近({bb_pos:.0f}%)✅")

        stop_price   = round(float(hist['SAR'].iloc[-1]), 0)
        target_price = round(bb_upper_val, 0)

        priority = "🌟🌟🌟 最優先" if days >= 3 else "⭐⭐ 優先" if days >= 2 else "⭐ 監視"

        return {
            "type":     "buy",
            "code":     code,
            "name":     name,
            "days":     days,
            "priority": priority,
            "price":    current_price,
            "stop":     stop_price,
            "target":   target_price,
            "bb_pos":   bb_pos,
            "signals":  signals,
            "time":     datetime.now().strftime('%m/%d %H:%M'),
        }
    except Exception as e:
        print(f"{code} 買いチェックエラー: {e}")
        return None

# ================================================================
# ★ 空売りシグナルチェック（パラボリック下転換）
# ================================================================
def check_short_signal(code, name, days):
    try:
        tk   = yf.Ticker(f"{code}.T")
        hist = tk.history(period="5d", interval="15m")
        if len(hist) < 20:
            return None

        bb_up, bb_mid, bb_lo = calculate_bb(hist)
        hist['BB_upper'] = bb_up
        hist['BB_mid']   = bb_mid
        hist['BB_lower'] = bb_lo
        trend, sar = calculate_parabolic_sar(hist['High'], hist['Low'], hist['Close'])
        hist['SAR_trend'] = trend
        hist['SAR']       = sar

        latest = hist.iloc[-1]
        prev   = hist.iloc[-2]

        current_price  = float(latest['Close'])
        bb_mid_val     = float(latest['BB_mid'])
        bb_lo_val      = float(latest['BB_lower'])
        bb_upper_val   = float(latest['BB_upper'])
        sar_trend_now  = int(hist['SAR_trend'].iloc[-1])
        sar_trend_prev = int(hist['SAR_trend'].iloc[-2])

        # ★ 必須条件：パラボリック下転換
        if not (sar_trend_prev == 1 and sar_trend_now == -1):
            return None

        signals = ["🎯 パラボリック下転換✅（必須）"]

        # BB中央線を陰線で下抜け
        if (latest['Close'] < bb_mid_val and
            latest['Open']  > bb_mid_val and
            latest['Close'] < latest['Open']):
            signals.append("📉 BB中央線下抜け✅")

        # 被せ線
        if (prev['Close'] >= prev['Open'] and
            latest['Open'] > prev['Close'] and
            latest['Close'] < prev['Open']):
            signals.append("🔻 被せ線✅")

        # 上ヒゲ陰線
        body       = abs(latest['Close'] - latest['Open'])
        upper_wick = latest['High'] - max(latest['Close'], latest['Open'])
        if latest['Close'] < latest['Open'] and body > 0 and upper_wick >= body * 1.5:
            signals.append("⬇️ 上ヒゲ陰線✅")

        # 陰線転換
        if prev['Close'] >= prev['Open'] and latest['Close'] < latest['Open']:
            if "被せ線" not in " ".join(signals) and "上ヒゲ陰線" not in " ".join(signals):
                signals.append("↓ 陰線転換✅")

        # BB位置（上限付近からの反転）
        bb_range = bb_upper_val - bb_lo_val
        bb_pos   = ((current_price - bb_lo_val) / bb_range * 100) if bb_range > 0 else 50
        if bb_pos > 60:
            signals.append(f"📊 BB上限付近({bb_pos:.0f}%)✅")

        stop_price   = round(float(hist['SAR'].iloc[-1]), 0)
        target_price = round(bb_lo_val, 0)

        priority = "🌟🌟🌟 最優先" if days >= 3 else "⭐⭐ 優先" if days >= 2 else "⭐ 監視"

        return {
            "type":     "short",
            "code":     code,
            "name":     name,
            "days":     days,
            "priority": priority,
            "price":    current_price,
            "stop":     stop_price,
            "target":   target_price,
            "bb_pos":   bb_pos,
            "signals":  signals,
            "time":     datetime.now().strftime('%m/%d %H:%M'),
        }
    except Exception as e:
        print(f"{code} 空売りチェックエラー: {e}")
        return None

# ================================================================
# Discord通知
# ================================================================
def send_discord(result):
    if not DISCORD_WEBHOOK:
        print("Discord Webhook未設定")
        return

    signals_str = "\n".join(result['signals'])
    is_buy      = result['type'] == 'buy'

    if is_buy:
        header   = "🔔 **【買いシグナル点灯】**"
        action   = "📱 SBIアプリで確認→**成行買い**を検討"
        stop_label   = "損切り（SAR下）"
        target_label = "利確目標（BB上限）"
        stop_emoji   = "🔴"
        target_emoji = "🟢"
    else:
        header   = "🔔 **【空売りシグナル点灯】**"
        action   = "📱 SBIアプリで確認→**空売り成行**を検討"
        stop_label   = "損切り（SAR上）"
        target_label = "利確目標（BB下限）"
        stop_emoji   = "🔴"
        target_emoji = "🟢"

    msg = f"""
{header}
{result['priority']}
**{result['code']} {result['name']}**
📅 {result['days']}日連続スキャン出現

**📊 シグナル：**
{signals_str}

**💰 価格情報：**
現在値: **{result['price']:,.0f}円**
{stop_emoji} {stop_label}: {result['stop']:,.0f}円
{target_emoji} {target_label}: {result['target']:,.0f}円

⏰ {result['time']}
{action}
"""
    requests.post(DISCORD_WEBHOOK, json={"content": msg.strip()})
    print(f"✅ 通知送信: {result['code']} {result['name']} ({result['type']})")

# ================================================================
# メイン処理
# ================================================================
def main():
    now = datetime.now()
    print(f"=== アラートチェック開始 {now.strftime('%Y/%m/%d %H:%M')} ===")

    # 土日は実行しない
    if now.weekday() >= 5:
        print("土日のため終了")
        return

    # 取引時間外は実行しない
    hour = now.hour; minute = now.minute
    if not (9 <= hour < 15 or (hour == 15 and minute <= 30)):
        print("取引時間外のため終了")
        return

    # 監視リスト読み込み
    try:
        with open('watchlist.json', 'r', encoding='utf-8') as f:
            data = json.load(f)
        watchlist = data.get('watchlist', [])
    except:
        print("watchlist.json読み込みエラー")
        return

    if not watchlist:
        print("監視銘柄なし")
        return

    print(f"監視銘柄数: {len(watchlist)}")

    # 連続日数が多い順に並べ替え
    watchlist = sorted(watchlist, key=lambda x: x.get('days', 0), reverse=True)

    alert_count = 0
    for stock in watchlist:
        code  = stock['code']
        name  = stock['name']
        days  = stock.get('days', 1)
        mode  = stock.get('mode', 'both')  # 'buy', 'short', 'both'

        print(f"チェック中: {code} {name} ({days}日連続) mode={mode}")

        # 買いシグナルチェック
        if mode in ('buy', 'both'):
            result = check_buy_signal(code, name, days)
            if result:
                print(f"🔔 買いシグナル！: {code} {name}")
                send_discord(result)
                alert_count += 1
            else:
                print(f"  → 買いシグナルなし")

        # 空売りシグナルチェック
        if mode in ('short', 'both'):
            result = check_short_signal(code, name, days)
            if result:
                print(f"🔔 空売りシグナル！: {code} {name}")
                send_discord(result)
                alert_count += 1
            else:
                print(f"  → 空売りシグナルなし")

    print(f"=== 完了 アラート{alert_count}件 ===")

if __name__ == "__main__":
    main()
