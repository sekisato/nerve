# Phase B — Jev Semantic Reflex / Nervous System

## 境界

Phase B は immutable hard state と transaction chronology を、Jevの狭いtyped classificationへ渡すlab-side measurementである。Jevに売買、position sizing、wallet、Risk、Executorの権限は与えない。SCANNER → SENTINEL → RISK → EXECUTORの経路には接続しない。

## 検証したAPI契約

2026-09-27時点のTypeSafe公式documentationと公式Python SDKを確認した。調査時のSDK commitは `f078f1e208a0d885154dc758344ae4fce77ac168`、公開releaseはv0.7.1である。

- endpoint: `POST https://api.typesafe.ai/v1/systemone`
- authentication: `Authorization: Bearer <TYPESAFE_API_KEY>`
- default model: `jev-latest`
- request: `state`, `model`, `questions`
- response: `model`, `answers`, `usage`
- question primitive: Choice / Score / Noul

NERVEは既存依存の`httpx`と`pydantic`で、この小さなwire contractを直接検証する。`JEV_API_URL`は互換endpoint override、`JEV_MODEL`はmodel overrideとして受け付ける。API keyはconfigのsecret fieldにのみ保持し、context、DB、raw answer、Observatory、browser JavaScriptへ渡さない。

価格は権威ある公開値を確認できなかったため、ドル換算を実装・表示しない。actual call countだけを記録する。

## Frozen context

`SemanticContext`は一つの`ObservationSnapshot`、一つの`MemecoinStateObservation`、一つの`MemeChronologyObservation`へ固定される。別attemptのfactは混ぜない。

`context_ready_at`は次の最大値である。

```text
max(snapshot.captured_at, state.ready_at, chronology.ready_at)
```

Jevの`started_at`はこれより前にできない。context hashはsnapshot/state/chronologyのhashとversionからPhase A.2 helperで生成する。partial、unavailable、observed zeroはpayload内で区別したまま保持する。

## Question set v1

versionは`jev-semantic-reflex-v1`。一回のSystem One requestで以下を独立したChoiceとして問い合わせる。

- `flow_toxicity`
- `buyer_persistence`
- `coordination_like`
- `inventory_transition`
- `distribution_risk`
- `late_entry_risk`
- `fresh_demand_like`
- `overall_data_sufficiency`
- `abstain`

directional dimensionは`LOW | MEDIUM | HIGH | INSUFFICIENT`、inventoryは`ACCUMULATION | DISTRIBUTION | MIXED | STABLE | INSUFFICIENT`。confidenceは`semantic_confidence`であり、profit probabilityではない。単一scoreやweighted compositeは作らない。

promptは、UNAVAILABLEをunknown、PARTIALをincompleteとして扱い、same-slotをcoordinationの証明、shared fundingをbundlingの証明、early holdingsをsnipingの証明、bounded creator historyをlifetime historyとして扱わないよう明示する。trade recommendationは禁止する。

## Dedupe、cooldown、deadline

同じ`context_input_hash + question_set_version + requested_model_id`に成功responseが存在する場合は再課金callを行わない。新しいcontext hashだけが再評価候補になる。さらにtoken単位の`--min-recall-seconds`（既定60秒）を適用する。

deadlineは`--deadline-ms`、既定2,000ms、hard range 100–60,000ms。auditのためtransport timeoutには2秒のgraceを設け、responseがsemantic deadline後なら完全vectorを保存しつつ`EXPIRED`とする。transport自体が失敗した場合は`FAILED`である。すべてのsemantic decisionは`execution_eligible = false`である。

## Forward clock

成功・abstain・insufficient・expired responseには、decision `completed_at`をanchorとする+60/+300/+900/+1800秒のpending outcomeを作る。元snapshotのforward timestampは再利用しない。Phase Bではprice resolverを呼ばず、pendingのまま保存する。

## CLI

```text
nerve jev-reflex collect --snapshot-id <id> --limit 20 \
  --deadline-ms 2000 --min-recall-seconds 60
```

`--live-jev`がなければcontextの構築・validation・eligibility表示だけを行い、network callもsemantic result rowも作らない。live gateがあっても`TYPESAFE_API_KEY`がなければcallせず、sidecar health failureとして記録する。

## Observatory

Observatoryは保存済みcontext、decision、dimension、pending outcomeだけをSQLite read-only connectionから表示する。Jevへのbrowser fetchはなく、keyも配信しない。単一の「Jev score」は表示せず、各dimensionのstateとsemantic confidenceを分離して表示する。
