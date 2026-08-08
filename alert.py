"""
アンチグラビティ・コア Pro+ アラートシステム
15分足でパラボリック転換を検知してDiscordに通知
・買いシグナル：パラボリック上転換＋BB中央線上抜け
・空売りシグナル：パラボリック下転換＋BB中央線下抜け

【改善点 2026/08/06】
・GitHub ActionsのScheduled実行は混雑時に数十分〜数時間遅延することがあるため、
  「直近1本の転換」だけでなく「直近LOOKBACK_BARS本の間に転換がなかったか」を
  遡ってチェックするように変更（見逃し防止）。
・同じ転換を何度も通知しないよう、通知済みの足の時刻を alert_state.json に
  記録し、次回以降はそれより新しい転換のみを通知する（重複通知防止）。
"""
import json
import os
import requests
import pandas as pd
import numpy as np
import yfinance as yf
from datetime import datetime

DISCORD_WEBHOOK = os.environ.get('DISCORD_WEBHOOK', '')

# yfinanceの15分足は最大60日分まで取得可能（それ以上は制限される）
HIST_PERIOD = "60d"

# Actionsの実行遅延を考慮し、直近何本まで遡って転換をチェックするか
# （15分足×8本＝2時間分。GitHub Actionsの遅延実績を踏まえた余裕を持った値）
LOOKBACK_BARS = 8

# 継続シグナル（トレンド継続中の新高値・新安値更新）を判定する際の遡り本数
# （15分足×20本＝5時間分＝ほぼ1取引日）
CONTINUATION_LOOKBACK = 20

STATE_FILE = 'alert_state.json'

# ================================================================
# 状態管理（重複通知防止）
# ================================================================
def load_state():
    try:
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(state):
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def state_key(code, signal_type):
    return f"{code}_{signal_type}"

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

def get_history(code):
    """60日分の15分足を取得（キャッシュせず毎回取得）"""
    tk = yf.Ticker(f"{code}.T")
    hist = tk.history(period=HIST_PERIOD, interval="15m")
    return hist

def prepare_data(code):
    """
    履歴取得＋BB・SAR計算をまとめて行う（転換チェック・継続チェックで共有）。
    データ不足の場合はNoneを返す。
    """
    hist = get_history(code)
    if len(hist) < 30:
        return None

    bb_up, bb_mid, bb_lo = calculate_bb(hist)
    hist['BB_upper'] = bb_up
    hist['BB_mid']   = bb_mid
    hist['BB_lower'] = bb_lo
    trend, sar = calculate_parabolic_sar(hist['High'], hist['Low'], hist['Close'])
    hist['SAR_trend'] = trend
    hist['SAR']       = sar
    return hist

def debug_log_sar(code, name, hist):
    """直近5本のSARトレンド推移をログ出力（原因調査用）"""
    trend, sar = calculate_parabolic_sar(hist['High'], hist['Low'], hist['Close'])
    tail = trend.tail(5)
    lines = []
    for ts, t in tail.items():
        arrow = "↑" if t == 1 else "↓"
        lines.append(f"    {ts.strftime('%m/%d %H:%M')} : {arrow} ({t})")
    print(f"  [DEBUG] {code} {name} 直近SARトレンド:")
    print("\n".join(lines))

def find_recent_transition(hist, want_prev, want_now, lookback=LOOKBACK_BARS):
    """
    直近lookback本の中で、want_prev→want_nowの転換が起きた「最新の」箇所を探す。
    見つかればそのインデックス位置（hist内の絶対位置）を返す。見つからなければNone。
    """
    trend = hist['SAR_trend']
    n = len(trend)
    start = max(1, n - lookback)
    # 新しい方から遡って最初に見つかった転換を採用（＝直近の転換）
    for i in range(n - 1, start - 1, -1):
        if int(trend.iloc[i-1]) == want_prev and int(trend.iloc[i]) == want_now:
            return i
    return None

def is_new_high(hist, lookback=CONTINUATION_LOOKBACK):
    """直近lookback本の中で、最新足のCloseが最高値（過去分含む）を更新しているか"""
    n = len(hist)
    if n < lookback + 1:
        return False
    window = hist['Close'].iloc[n - lookback - 1:n]
    return float(window.iloc[-1]) == float(window.max()) and float(window.iloc[-1]) > float(window.iloc[:-1].max())

def is_new_low(hist, lookback=CONTINUATION_LOOKBACK):
    """直近lookback本の中で、最新足のCloseが最安値（過去分含む）を更新しているか"""
    n = len(hist)
    if n < lookback + 1:
        return False
    window = hist['Close'].iloc[n - lookback - 1:n]
    return float(window.iloc[-1]) == float(window.min()) and float(window.iloc[-1]) < float(window.iloc[:-1].min())

# ================================================================
# ★ 買いシグナルチェック（パラボリック上転換）
# ================================================================
def check_buy_signal(code, name, days, state, hist):
    try:
        debug_log_sar(code, name, hist)

        # ★ 直近LOOKBACK_BARS本の間に「上転換」がなかったか遡ってチェック
        #   （Actionsの実行遅延で直近1本だけを見ると見逃すことがあるため）
        idx = find_recent_transition(hist, want_prev=-1, want_now=1)
        if idx is None:
            return None

        bar = hist.iloc[idx]
        prev_bar = hist.iloc[idx - 1]
        bar_time_str = bar.name.isoformat()

        # 重複通知防止：この転換をすでに通知済みなら何もしない
        key = state_key(code, 'buy')
        if state.get(key) == bar_time_str:
            print(f"  → {code} 買いシグナルは通知済み（対象足: {bar.name.strftime('%m/%d %H:%M')}）")
            return None

        current_price  = float(bar['Close'])
        bb_mid_val     = float(bar['BB_mid'])
        bb_lo_val      = float(bar['BB_lower'])
        bb_upper_val   = float(bar['BB_upper'])

        signals = ["🎯 パラボリック上転換✅（必須）"]

        # BB中央線を陽線で上抜け
        if (bar['Close'] > bb_mid_val and
            bar['Open']  < bb_mid_val and
            bar['Close'] > bar['Open']):
            signals.append("📈 BB中央線上抜け✅")

        # 包み足陽線
        if (prev_bar['Close'] < prev_bar['Open'] and
            bar['Close'] > bar['Open'] and
            bar['Close'] > prev_bar['Open'] and
            bar['Open']  < prev_bar['Close']):
            signals.append("⚡ 包み足陽線✅")

        # 下ヒゲ陽線
        body       = abs(bar['Close'] - bar['Open'])
        lower_wick = min(bar['Close'], bar['Open']) - bar['Low']
        if bar['Close'] > bar['Open'] and body > 0 and lower_wick >= body * 1.5:
            signals.append("🔥 下ヒゲ陽線✅")

        # BB位置
        bb_range = bb_upper_val - bb_lo_val
        bb_pos   = ((current_price - bb_lo_val) / bb_range * 100) if bb_range > 0 else 50
        if bb_pos < 40:
            signals.append(f"📊 BB下限付近({bb_pos:.0f}%)✅")

        stop_price   = round(float(bar['SAR']), 0)
        target_price = round(bb_upper_val, 0)

        priority = "🌟🌟🌟 最優先" if days >= 3 else "⭐⭐ 優先" if days >= 2 else "⭐ 監視"

        # 通知済みとして記録
        state[key] = bar_time_str

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
            "bar_time": bar.name.strftime('%m/%d %H:%M'),
        }
    except Exception as e:
        print(f"{code} 買いチェックエラー: {e}")
        return None

# ================================================================
# ★ 逆張り：BB下限タッチ検知（タッチの瞬間を即通知）
#   SAR転換を待たず、下落相場の中でもBB下限に触れた瞬間を拾う。
#   kabujiji側のスクリーニング基準（1.005倍）に合わせている。
#   1日1回まで通知（同じ足で何度も鳴らさないよう日付単位でdedup）
# ================================================================
def check_buy_bb_touch(code, name, days, state, hist):
    try:
        bar = hist.iloc[-1]
        bb_lo_val = float(bar['BB_lower'])
        low_val   = float(bar['Low'])
        close_val = float(bar['Close'])

        touched = (low_val <= bb_lo_val * 1.005) or (close_val <= bb_lo_val * 1.005)
        if not touched:
            return None

        day_str = bar.name.strftime('%Y-%m-%d')
        key = state_key(code, 'buy_bbtouch')
        if state.get(key) == day_str:
            return None  # 本日はすでに通知済み

        current_price = float(bar['Close'])
        bb_mid_val   = float(bar['BB_mid'])
        bb_upper_val = float(bar['BB_upper'])

        signals = ["🎯 BB下限タッチ✅（逆張り・必須）"]

        # 下ヒゲ陽線（反発の初期サイン）
        body       = abs(bar['Close'] - bar['Open'])
        lower_wick = min(bar['Close'], bar['Open']) - bar['Low']
        if bar['Close'] > bar['Open'] and body > 0 and lower_wick >= body * 1.5:
            signals.append("🔥 下ヒゲ陽線✅")

        sar_trend = int(bar['SAR_trend'])
        if sar_trend == 1:
            signals.append("📈 SARすでに上向き")
        else:
            signals.append("⚠️ SARはまだ下向き（先回りエントリー）")

        stop_price   = round(bb_lo_val * 0.98, 0)   # BB下限からさらに2%下を損切りに
        target_price = round(bb_mid_val, 0)          # 中央線を利確目安に
        priority = "🌟🌟🌟 最優先" if days >= 3 else "⭐⭐ 優先" if days >= 2 else "⭐ 監視"

        state[key] = day_str

        return {
            "type":     "buy",
            "bb_touch": True,
            "code":     code,
            "name":     name,
            "days":     days,
            "priority": priority,
            "price":    current_price,
            "stop":     stop_price,
            "target":   target_price,
            "bb_pos":   0.0,
            "signals":  signals,
            "time":     datetime.now().strftime('%m/%d %H:%M'),
            "bar_time": bar.name.strftime('%m/%d %H:%M'),
        }
    except Exception as e:
        print(f"{code} BB下限タッチチェックエラー: {e}")
        return None

# ================================================================
# ★ 空売りシグナルチェック（パラボリック下転換）
# ================================================================
def check_short_signal(code, name, days, state, hist):
    try:
        # ★ 直近LOOKBACK_BARS本の間に「下転換」がなかったか遡ってチェック
        idx = find_recent_transition(hist, want_prev=1, want_now=-1)
        if idx is None:
            return None

        bar = hist.iloc[idx]
        prev_bar = hist.iloc[idx - 1]
        bar_time_str = bar.name.isoformat()

        # 重複通知防止
        key = state_key(code, 'short')
        if state.get(key) == bar_time_str:
            print(f"  → {code} 空売りシグナルは通知済み（対象足: {bar.name.strftime('%m/%d %H:%M')}）")
            return None

        current_price  = float(bar['Close'])
        bb_mid_val     = float(bar['BB_mid'])
        bb_lo_val      = float(bar['BB_lower'])
        bb_upper_val   = float(bar['BB_upper'])

        signals = ["🎯 パラボリック下転換✅（必須）"]

        # BB中央線を陰線で下抜け
        if (bar['Close'] < bb_mid_val and
            bar['Open']  > bb_mid_val and
            bar['Close'] < bar['Open']):
            signals.append("📉 BB中央線下抜け✅")

        # 被せ線
        if (prev_bar['Close'] >= prev_bar['Open'] and
            bar['Open'] > prev_bar['Close'] and
            bar['Close'] < prev_bar['Open']):
            signals.append("🔻 被せ線✅")

        # 上ヒゲ陰線
        body       = abs(bar['Close'] - bar['Open'])
        upper_wick = bar['High'] - max(bar['Close'], bar['Open'])
        if bar['Close'] < bar['Open'] and body > 0 and upper_wick >= body * 1.5:
            signals.append("⬇️ 上ヒゲ陰線✅")

        # 陰線転換
        if prev_bar['Close'] >= prev_bar['Open'] and bar['Close'] < bar['Open']:
            if "被せ線" not in " ".join(signals) and "上ヒゲ陰線" not in " ".join(signals):
                signals.append("↓ 陰線転換✅")

        # BB位置（上限付近からの反転）
        bb_range = bb_upper_val - bb_lo_val
        bb_pos   = ((current_price - bb_lo_val) / bb_range * 100) if bb_range > 0 else 50
        if bb_pos > 60:
            signals.append(f"📊 BB上限付近({bb_pos:.0f}%)✅")

        stop_price   = round(float(bar['SAR']), 0)
        target_price = round(bb_lo_val, 0)

        priority = "🌟🌟🌟 最優先" if days >= 3 else "⭐⭐ 優先" if days >= 2 else "⭐ 監視"

        # 通知済みとして記録
        state[key] = bar_time_str

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
            "bar_time": bar.name.strftime('%m/%d %H:%M'),
        }
    except Exception as e:
        print(f"{code} 空売りチェックエラー: {e}")
        return None

# ================================================================
# ★ 買い継続シグナルチェック（トレンド継続中の新高値更新）
#   SARの転換イベントが起きていなくても、既に上昇トレンドが続いている
#   銘柄が新高値を更新したタイミングで拾う。
# ================================================================
def check_buy_continuation(code, name, days, state, hist):
    try:
        last_trend = int(hist['SAR_trend'].iloc[-1])
        if last_trend != 1:
            return None  # 上昇トレンド中でなければ対象外

        if not is_new_high(hist):
            return None

        bar = hist.iloc[-1]
        bar_time_str = bar.name.isoformat()

        # 重複通知防止：同じ足をすでに通知済みなら何もしない
        key = state_key(code, 'buy_cont')
        if state.get(key) == bar_time_str:
            return None

        current_price = float(bar['Close'])
        bb_upper_val   = float(bar['BB_upper'])
        bb_lo_val      = float(bar['BB_lower'])
        bb_range = bb_upper_val - bb_lo_val
        bb_pos   = ((current_price - bb_lo_val) / bb_range * 100) if bb_range > 0 else 50

        stop_price   = round(float(bar['SAR']), 0)
        target_price = round(bb_upper_val, 0)
        priority = "🌟🌟🌟 最優先" if days >= 3 else "⭐⭐ 優先" if days >= 2 else "⭐ 監視"

        state[key] = bar_time_str

        return {
            "type":     "buy",
            "continuation": True,
            "code":     code,
            "name":     name,
            "days":     days,
            "priority": priority,
            "price":    current_price,
            "stop":     stop_price,
            "target":   target_price,
            "bb_pos":   bb_pos,
            "signals":  [f"📈 上昇トレンド継続中の新高値更新✅（直近{CONTINUATION_LOOKBACK}本）"],
            "time":     datetime.now().strftime('%m/%d %H:%M'),
            "bar_time": bar.name.strftime('%m/%d %H:%M'),
        }
    except Exception as e:
        print(f"{code} 買い継続チェックエラー: {e}")
        return None

# ================================================================
# ★ 空売り継続シグナルチェック（トレンド継続中の新安値更新）
# ================================================================
def check_short_continuation(code, name, days, state, hist):
    try:
        last_trend = int(hist['SAR_trend'].iloc[-1])
        if last_trend != -1:
            return None  # 下降トレンド中でなければ対象外

        if not is_new_low(hist):
            return None

        bar = hist.iloc[-1]
        bar_time_str = bar.name.isoformat()

        key = state_key(code, 'short_cont')
        if state.get(key) == bar_time_str:
            return None

        current_price = float(bar['Close'])
        bb_upper_val   = float(bar['BB_upper'])
        bb_lo_val      = float(bar['BB_lower'])
        bb_range = bb_upper_val - bb_lo_val
        bb_pos   = ((current_price - bb_lo_val) / bb_range * 100) if bb_range > 0 else 50

        stop_price   = round(float(bar['SAR']), 0)
        target_price = round(bb_lo_val, 0)
        priority = "🌟🌟🌟 最優先" if days >= 3 else "⭐⭐ 優先" if days >= 2 else "⭐ 監視"

        state[key] = bar_time_str

        return {
            "type":     "short",
            "continuation": True,
            "code":     code,
            "name":     name,
            "days":     days,
            "priority": priority,
            "price":    current_price,
            "stop":     stop_price,
            "target":   target_price,
            "bb_pos":   bb_pos,
            "signals":  [f"📉 下降トレンド継続中の新安値更新✅（直近{CONTINUATION_LOOKBACK}本）"],
            "time":     datetime.now().strftime('%m/%d %H:%M'),
            "bar_time": bar.name.strftime('%m/%d %H:%M'),
        }
    except Exception as e:
        print(f"{code} 空売り継続チェックエラー: {e}")
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

    is_cont     = result.get('continuation', False)
    is_bb_touch = result.get('bb_touch', False)

    if is_buy:
        if is_bb_touch:
            header = "🔔 **【逆張り・BB下限タッチ点灯】**"
        elif is_cont:
            header = "🔔 **【買い継続シグナル点灯】**"
        else:
            header = "🔔 **【買いシグナル点灯】**"
        action   = "📱 SBIアプリで確認→**成行買い**を検討"
        stop_label   = "損切り（BB下限-2%）" if is_bb_touch else "損切り（SAR下）"
        target_label = "利確目標（BB中央線）" if is_bb_touch else "利確目標（BB上限）"
        stop_emoji   = "🔴"
        target_emoji = "🟢"
    else:
        header   = "🔔 **【空売り継続シグナル点灯】**" if is_cont else "🔔 **【空売りシグナル点灯】**"
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

⏰ 通知時刻: {result['time']}（対象足: {result['bar_time']}）
{action}
"""
    resp = requests.post(DISCORD_WEBHOOK, json={"content": msg.strip()})
    print(f"✅ 通知送信: {result['code']} {result['name']} ({result['type']}) status={resp.status_code}")

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
    except Exception as e:
        print(f"watchlist.json読み込みエラー: {e}")
        return

    if not watchlist:
        print("監視銘柄なし")
        return

    print(f"監視銘柄数: {len(watchlist)}")
    print(f"見逃し防止のため直近{LOOKBACK_BARS}本（約{LOOKBACK_BARS*15}分）まで遡ってチェックします")

    state = load_state()

    # 連続日数が多い順に並べ替え
    watchlist = sorted(watchlist, key=lambda x: x.get('days', 0), reverse=True)

    alert_count = 0
    for stock in watchlist:
        code  = stock['code']
        name  = stock['name']
        days  = stock.get('days', 1)
        mode  = stock.get('mode', 'both')  # 'buy', 'short', 'both'

        print(f"チェック中: {code} {name} ({days}日連続) mode={mode}")

        hist = prepare_data(code)
        if hist is None:
            print(f"  → {code} データ不足")
            continue

        # 買いシグナルチェック（①転換 → ②転換なければ継続）
        if mode in ('buy', 'both'):
            result = check_buy_signal(code, name, days, state, hist)
            if result:
                print(f"🔔 買いシグナル（転換）！: {code} {name}")
                send_discord(result)
                alert_count += 1
            else:
                result = check_buy_continuation(code, name, days, state, hist)
                if result:
                    print(f"🔔 買いシグナル（継続）！: {code} {name}")
                    send_discord(result)
                    alert_count += 1
                else:
                    print(f"  → 買いシグナルなし")

            # ③ 逆張り：BB下限タッチは①②と独立してチェック（同時に成立してもOK）
            #   下落相場でもタッチの瞬間を逃さないための追加シグナル
            bb_result = check_buy_bb_touch(code, name, days, state, hist)
            if bb_result:
                print(f"🔔 逆張りシグナル（BB下限タッチ）！: {code} {name}")
                send_discord(bb_result)
                alert_count += 1

        # 空売りシグナルチェック（①転換 → ②転換なければ継続）
        if mode in ('short', 'both'):
            result = check_short_signal(code, name, days, state, hist)
            if result:
                print(f"🔔 空売りシグナル（転換）！: {code} {name}")
                send_discord(result)
                alert_count += 1
            else:
                result = check_short_continuation(code, name, days, state, hist)
                if result:
                    print(f"🔔 空売りシグナル（継続）！: {code} {name}")
                    send_discord(result)
                    alert_count += 1
                else:
                    print(f"  → 空売りシグナルなし")

    save_state(state)
    print(f"=== 完了 アラート{alert_count}件 ===")

if __name__ == "__main__":
    main()
