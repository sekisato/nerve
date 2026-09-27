# NERVE Observatory v0

NERVE Observatory は、Phase 0 truth store を読むための localhost 専用計器盤である。売買、wallet 接続、外部 API 呼び出し、スコア計算、outcome 解決は行わない。

## 起動

```bash
DB_PATH=/path/to/nerve.db nerve observatory
```

既定の URL は `http://127.0.0.1:3000` である。既定で `0.0.0.0` へ公開しない。

Observatory reader は通常の `NerveStore` を生成せず、SQLite URI の `mode=ro` と `PRAGMA query_only=ON` を使う。したがって UI/API の起動ではテーブル作成、WAL 設定、INSERT、UPDATE、DELETE、ALTER、DROP を行えない。

## 情報構成

- Run Status: snapshot、token、pool、decision、expired、unresolved、measurement error
- Snapshot Tape: 観測時刻と immutable identity
- Decision Trace: SENTINEL snapshot、arm clock、deadline、quote、execution observation、forward horizon
- Forward Lab: 1m / 5m / 15m / 30m の最新 market outcome
- Calibration: model / question version / horizon ごとの Brier、log loss、ECE、reliability bins
- System Health: capture、arm、outcome の計測成否

CONTROL は `CONTROL · ABSTAIN` と表示する。これは current NERVE strategy の performance baseline ではなく、alpha を推論できない abstention baseline である。

## API

すべて同一 origin の GET endpoint である。

- `/api/observatory/summary`
- `/api/observatory/snapshots?limit=100`
- `/api/observatory/snapshot/{snapshot_id}`
- `/api/observatory/arms`
- `/api/observatory/calibration`
- `/api/observatory/health`

POST、PUT、PATCH、DELETE は `405 Method Not Allowed` で拒否する。

## Phase 0.1 truth hardening

`measurement_events` は capture 成功、capture 失敗、arm 失敗、outcome 失敗を追記する。計測失敗が core verdict を変更することはない。イベント自体を DB に保存できない場合は Python logging が最終 fallback になる。

`forward_outcomes` は `outcome_id` のみを event identity とし、同じ snapshot / horizon / status の retry や correction を複数保存できる。`recorded_at` と rowid で最新 event を決定する。旧 Phase 0 schema は `NerveStore` 起動時に transaction 内でテーブルを再構築し、コピー前後の行数を検証する。既存 event は削除・更新しない。

## UI donor の境界

AwesomeJev Pump Pulse から、コンパクトな top bar、暖色の research-terminal palette、flat card、badge、responsive grid という視覚的特徴だけを参照した。旧 research DB、Jev、DexScreener、CoinGecko、Helius、Jupiter、wallet、browser-local outcome、Setup Quality、execution control は移植していない。
