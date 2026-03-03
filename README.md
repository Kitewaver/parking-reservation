# SUUMO賃貸物件スクレイピングツール

## 概要
SUUMOから賃貸物件情報を取得し、想定賃料を計算・グラフ化するツール

## 機能
- 物件情報の自動取得
- 50.08m²と54.46m²の想定賃料計算
- 週次での賃料推移グラフ作成
- CSV形式での履歴保存

## 必要なパッケージ
```bash
pip install selenium pandas matplotlib --break-system-packages
```

## 使用方法
```bash
python3 suumo.py
```

## 自動実行設定（cron）
```bash
# 毎週月曜日 9:00に実行
0 9 * * 1 cd /home/taki && /usr/bin/python3 /home/taki/suumo.py >> /home/taki/suumo_cron.log 2>&1
```

## ファイル構成
- `suumo.py` - メインスクリプト
- `suumo_history.csv` - 履歴データ
- `suumo_trend.png` - 賃料推移グラフ
- `suumo_list_YYYY-MM-DD.csv` - 日次詳細データ

## 注意事項
- Chromiumとchromedriverが必要
- スクレイピングは利用規約を確認の上、節度を持って実行してください
