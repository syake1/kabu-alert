"""
買いスキャンCSVから監視リストを自動更新するスクリプト
毎日のスキャン結果CSVをここに貼り付けて実行すると
watchlist.jsonが更新されます
"""
import json
import pandas as pd
from datetime import datetime
import sys
import os

def update_watchlist(csv_path):
    """
    買い候補CSVを読み込んでwatchlist.jsonを更新
    連続出現日数をカウントして優先度を設定
    """
    # 既存の監視リストを読み込み
    watchlist_path = 'watchlist.json'
    try:
        with open(watchlist_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        existing = {s['code']: s for s in data.get('watchlist', [])}
    except:
        existing = {}

    # 新しいCSVを読み込み
    try:
        df = pd.read_csv(csv_path, encoding='utf-8-sig')
    except:
        try:
            df = pd.read_csv(csv_path, encoding='shift-jis')
        except Exception as e:
            print(f"CSV読み込みエラー: {e}")
            return

    # 買い候補のみ抽出
    if '判定' in df.columns:
        buy_df = df[df['判定'].str.contains('買い候補', na=False)]
    else:
        buy_df = df

    if buy_df.empty:
        print("買い候補なし")
        return

    # 連続日数を更新
    new_watchlist = []
    today = datetime.now().strftime('%Y/%m/%d')

    for _, row in buy_df.iterrows():
        code = str(row.get('コード', '')).strip()
        name = str(row.get('会社名', code)).strip()

        if not code:
            continue

        if code in existing:
            # 既存銘柄は日数を+1
            days = existing[code].get('days', 0) + 1
        else:
            # 新規銘柄は1日目
            days = 1

        new_watchlist.append({
            "code": code,
            "name": name,
            "days": days,
            "first_seen": existing.get(code, {}).get('first_seen', today),
            "last_seen": today,
        })
        print(f"✅ {code} {name} → {days}日連続")

    # 前日まで出ていたが今日は出なかった銘柄は削除
    removed = [s for s in existing.values() if s['code'] not in [s['code'] for s in new_watchlist]]
    for s in removed:
        print(f"❌ {s['code']} {s['name']} → リストから削除")

    # 保存
    data = {
        "watchlist": new_watchlist,
        "updated": today,
        "count": len(new_watchlist)
    }
    with open(watchlist_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"\n✅ watchlist.json更新完了: {len(new_watchlist)}銘柄")
    print(f"  連続3日以上: {len([s for s in new_watchlist if s['days'] >= 3])}銘柄 🌟🌟🌟")
    print(f"  連続2日: {len([s for s in new_watchlist if s['days'] == 2])}銘柄 ⭐⭐")
    print(f"  新規1日: {len([s for s in new_watchlist if s['days'] == 1])}銘柄 ⭐")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        # 引数なしの場合はサンプル
        print("使い方: python update_watchlist.py 買い候補_20260716.csv")
        print("\nサンプル実行...")
        # サンプルデータで動作確認
        sample = pd.DataFrame([
            {"コード": "6507", "会社名": "シンフォニア", "判定": "🔥 買い候補（最強）"},
            {"コード": "7409", "会社名": "AeroEdge", "判定": "✅ 買い候補"},
        ])
        sample.to_csv('sample.csv', index=False, encoding='utf-8-sig')
        update_watchlist('sample.csv')
        os.remove('sample.csv')
    else:
        update_watchlist(sys.argv[1])
