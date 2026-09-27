# Phase A.2 — Transaction Chronology Primitives

## 目的

Phase A.2 は Solana 上で実際に観測できた取引順序を、判断ではなく証拠として保存する。`sniper`、`bundler`、`organic`、売買推奨などの意味判断は行わない。収集は明示的な lab-side CLI からのみ実行され、SCANNER、SENTINEL、Risk、Executor の待ち時間や判定には影響しない。

参照した公式資料は `pump-fun/pump-public-docs` の commit `81091419e4457566469d4e2a27f64ed84d42419c` である。フル IDL は同梱せず、必要な discriminator と account layout だけを独自実装した。

## 対応 instruction

Pump program (`6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P`) は次を識別する。

| instruction | discriminator (hex) | evidence |
|---|---|---|
| `create` | `181ec828051c0777` | mint, creation_user, creation_creator |
| `create_v2` | `d6904cec5f8b31b4` | mint, creation_user, creation_creator |
| `buy` | `66063d1201daebea` | user, mint, side, raw amounts |
| `buy_exact_sol_in` | `38fc74089edfcd5f` | user, mint, side, raw amounts |
| `buy_v2` | `b817ee6167c5d33d` | user, mint, side, raw amounts |
| `buy_exact_quote_in_v2` | `c2ab1c46684d5b2f` | user, mint, side, raw amounts |
| `sell` | `33e685a4017f83ad` | user, mint, side, raw amounts |
| `sell_v2` | `5df6823ce7e940b2` | user, mint, side, raw amounts |
| `migrate` | `9beae792ec9ea21e` | user, mint, pool |

PumpSwap program (`pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA`) は `buy` (`66063d1201daebea`)、`buy_exact_quote_in` (`c62c1552b4d9e870`)、`sell` (`33e685a4017f83ad`) を識別し、pool、user、base_mint、side を保存する。未知の variant は既知として扱わない。外側 instruction と CPI の両方を読み、`outer:3` / `outer:4/inner:2` のような path を保持する。

## 履歴とcoverage契約

`getSignaturesForAddress` は `confirmed`、有限ページ、最大1,000署名に制限する。各署名の `getTransaction` 成否、unsupported version、decode failure、ページ数、要求上限、最古の返却署名を記録する。creation に到達し、途中欠損・失敗・打切りがない場合だけ `complete_since_creation` とし、`unique_buyers_since_creation` を OBSERVED にする。それ以外は部分観測値と完全履歴の主張を分ける。

`creation_user` は create instruction の user account、`creation_creator` は instruction data の creator field であり、常に別フィールドとして保持する。現在の BondingCurve `coin_creator` を代用しない。

## cohort・same-slot・現在残高

early cohort は最初の10 distinct buyersを標準とする。ただし10番目と同じslotで初回購入したwalletを全て含め、slot内の恣意的な順序で分割しない。同一slotのdistinct buyer数は記述統計であり、協調やbundlingの判定ではない。

現在残高は cohort walletごとに `getTokenAccountsByOwner` のmint filterで読み、上限20 walletとする。全walletを取得できた場合だけ現在token合計とcurrent supply shareを公開する。これはbeneficial ownershipやsniper ownershipを証明しない。

## optional probe

funding probeは既定0、最大20 buyersで、walletの先行20署名だけを見る。初回buy前2時間以内のparsed System Program inbound native SOL transferのうち最も近い1件を証拠として保存する。見つからないことはself-fundedを意味しない。

creator history probeは既定0、最大500署名で、`creation_creator` addressに現れるdocumented Pump create instructionをmint単位で重複排除する。有限上限に達した場合はtruncatedとし、生涯launch数とは呼ばない。

## 時刻とlook-ahead防止

chronologyは `started_at`、`ready_at`、`source_cutoff_at`、含有slot/time範囲を保存する。将来の実験contextは `max(snapshot.captured_at, state.ready_at, chronology.ready_at)` より前に利用できない。snapshot/state/chronologyのhashとversionから決定的context hashを作るpure helperも用意したが、Phase A.2ではControlArmや既存forward outcomeへ接続しない。

## 実行

```text
nerve meme-chronology collect --snapshot-id <id> --max-signatures 500 \
  --funding-buyers 0 --creator-history-signatures 0
```

ObservatoryはSQLite `mode=ro` と `PRAGMA query_only=ON` を維持し、保存済みのchronologyだけを表示する。ブラウザからSolana RPCへの外部fetchは行わない。
