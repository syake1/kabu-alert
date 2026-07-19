# 🔔 kabu-alert - 株価アラートシステム

30分足でパラボリック上転換を検知してDiscordに通知するシステムです。

## 仕組み

```
毎日スキャン（kabujiji）
↓
買い候補CSVをupdate_watchlist.pyで登録
↓
GitHub Actionsが30分ごとに監視
↓
パラボリック上転換を検知
↓
Discordに通知
↓
10時の休憩にスマホで確認
↓
成行買い
```

## セットアップ

### 1. GitHub Secretsに設定
```
DISCORD_WEBHOOK = あなたのDiscord Webhook URL
```

### 2. 毎日の使い方

**①スキャン結果CSVで監視リストを更新：**
```bash
python update_watchlist.py 買い候補_20260716.csv
```

**②watchlist.jsonをGitHubにプッシュ**

これだけでGitHub Actionsが自動で監視を開始します。

## 通知条件

### 必須条件
- ✅ **パラボリック上転換**（下落トレンド→上昇トレンドへの転換）

### 追加条件（あればより強いシグナル）
- 📈 BB中央線を陽線で上抜け
- ⚡ 包み足陽線
- 🔥 下ヒゲ陽線
- 📊 BB下限付近からの反転

## 優先度

| 連続出現日数 | 優先度 |
|---|---|
| 3日以上 | 🌟🌟🌟 最優先 |
| 2日 | ⭐⭐ 優先 |
| 1日 | ⭐ 監視 |

## 監視銘柄の手動追加

`watchlist.json`を直接編集：
```json
{
  "watchlist": [
    {"code": "6507", "name": "シンフォニア", "days": 2},
    {"code": "7409", "name": "AeroEdge", "days": 3}
  ]
}
```

## ConoHaへの移行（将来）

安定稼働が確認できたらConoHa VPSで常時監視に移行。
より細かい間隔（5分ごと）での監視が可能になります。
