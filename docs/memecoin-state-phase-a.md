# Phase A Memecoin State

Phase A は予測器でも売買判断器でもない。Phase 0 の `ObservationSnapshot` を先に保存した後、明示的なlab-side collectorがSolana memecoinの状態を独立した不変事実として採取する。`Spine`、`SENTINEL`、`Risk`、`Executor`からprovider通信を呼ぶ経路はない。

## 実行

```bash
SOLANA_RPC_URL=https://your-solana-rpc.example \
DB_PATH=data/nerve.db \
nerve meme-state collect --limit 20
```

特定snapshotだけを対象にする場合は`--snapshot-id`を使う。`SOLANA_RPC_URL`がない場合もhard failureにはせず、RPC由来のfactを`UNAVAILABLE`として記録する。DexScreenerは公開API、Solanaは標準JSON-RPCのみを使い、有料providerは必須にしない。

## 事実契約

各factは`OBSERVED`、`UNAVAILABLE`、`STALE`、`INVALID`、`CONFLICT`、`NOT_APPLICABLE`のいずれかを持つ。valueは`value_num`、`value_int`、`value_text`、`value_bool`の型別カラムに保存する。未知値を0、空文字、falseへ変換しない。`source`、`source_observed_at`、`fetched_at`、`age_ms`、`reason`、`details_json`を併記する。

`memecoin_state_observations`と`memecoin_state_facts`はappend-onlyである。同じsnapshotへの複数attemptを許し、latest APIは履歴を削除せず最新の`success`または`partial`を選ぶ。

## DexScreener

公式documented endpoint `GET /token-pairs/v1/solana/{tokenAddress}`のみを使う。snapshotのpoolと完全一致するpairを優先し、それがない場合はbase token一致候補からliquidity USD最大、同値ならpair address辞書順で選ぶ。候補metadataと`pair_selection_reason`を保存する。

buys/sellsはtransaction countであり、unique buyerとは呼ばない。

## Solana RPC

`getTokenSupply`、`getTokenLargestAccounts`、`getMultipleAccounts`のread-only methodを使う。`top1/5/10/20_token_accounts_pct`の分母はraw token supplyであり、holder数ではない。owner集約は上位20 token accountのうち解決できた部分だけで、全holder分布を意味しない。

## Pump

実装時に確認した公式[pump-public-docs](https://github.com/pump-fun/pump-public-docs)のcommitは`81091419e4457566469d4e2a27f64ed84d42419c`。確認対象は`docs/PUMP_PROGRAM_README.md`、`docs/PUMP_SWAP_README.md`、`idl/pump.json`、`idl/pump_amm.json`である。root LICENSEがないためIDL全体はvendorせず、documented layoutに基づく最小限のoriginal decoderだけを実装した。

- Pump program: `6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P`
- PumpSwap program: `pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA`
- BondingCurve PDA seed: `["bonding-curve", mint]`

account ownerとAnchor discriminatorを検証する。短いlegacy accountの追加fieldは捏造せず`UNAVAILABLE`にする。`coin_creator`はcoin creator feeが帰属するaddressであり、deployer、funding wallet、creation userとは呼ばない。

`curve_complete=true`だけではmigrationを証明しない。lifecycleは次を区別する。

- `PUMP_CURVE_ACTIVE`
- `PUMP_CURVE_COMPLETE_MIGRATION_UNKNOWN`
- `PUMP_CANONICAL_PUMPSWAP_VERIFIED`
- `NOT_PUMP`
- `UNKNOWN`

canonical PumpSwapはDexScreenerを候補発見にだけ使い、on-chain owner、Pool discriminator、base mint、index 0、WSOL quote mint、Pump pool-authority由来creatorを検証した場合だけ成立する。

## 未実装の意味論

`unique_buyers`、`sniper_supply_pct`、`bundler_supply_pct`は数値化せず`UNAVAILABLE`として理由を保存する。buy transaction count、token account concentration、同一owner集約だけでは、それぞれを証明できないためである。Phase AにはJev、Setup Quality、alpha score、weighted compositeを含めない。

## Observatory

既存の`mode=ro`、`PRAGMA query_only=ON`、GET-only、same-origin制約を維持する。ブラウザはNERVE SQLiteだけを読み、DexScreenerやSolana RPCへ直接接続しない。選択snapshotの全factについてvalue、status、source、provider timestamp、fetch timestamp、reason、detailsを表示する。
