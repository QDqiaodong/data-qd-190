# 跨手教学账本 v1

`showdown-ledger` 在同一张教学桌的多手牌之间维护本地持久化账本。它复用 v1 单手结算（`showdown-pots`）的牌力、边池资格、并列零头与未匹配筹码退回，不重新解释下注街次，也不连接资金或游戏服务。所有金额都是整数筹码。

旧命令 `showdown-pots --source FILE --output FILE`、v1 输入输出和退出码不变。

## 状态与命令

状态是一个本地 SQLite 文件，只要求单进程依次操作，不要求并发写。下列命令都写 `--state PATH`，成功时在 stdout 返回一个 JSON 对象；非法输入、未知账本或结单 ID、禁止的变更都在 stderr 说明并以状态 2 退出，不返回部分结果，也不留下部分生效的录入或结单。

```sh
showdown-ledger --state TABLE.db init --source opening.json
showdown-ledger --state TABLE.db apply --source edits.json
showdown-ledger --state TABLE.db preview
showdown-ledger --state TABLE.db release --request-id ID --through N
showdown-ledger --state TABLE.db read-release --request-id ID
```

### init

```json
{"opening_balances": {"A": 10, "B": 10}}
```

名册 2..100 人，键沿用 v1 玩家 ID 规则（`[A-Za-z0-9_:-]{1,64}`），每人期初余额为 0..10^9 的整数。返回 `{"head": 0}`。已有账本不会被覆盖。

账本内以玩家 ID 为准，不以座位为准：换座不换人，某手缺席也不改变余额。

### apply

```json
{"edits": [{"hand_no": 1, "expected_revision": 0, "source": { ... v1 单手输入 ... }}]}
```

- 每批 1..100 条，手号 1..1000 且批内不重复；输入顺序不影响结果。
- `expected_revision: 0` 只用于新增手；新增手号必须从当前末手之后连续（ `head+1` 起）。
- 更正手使用该批之前的当前 revision；成功后 revision 加一，即使输入完全相同。
- 每个 `source` 都是原样可交给 `showdown-pots` 的 v1 输入。
- 任一版本不符、手号不合法、参与者不在名册或单手不能结算，整批不生效。
- 每手自身可结算、但更正使后续手期初余额不足时，这批更正仍会保存，供录入员继续修复。
- 返回 `{"head": 末手号, "revisions": [{"hand_no": ..., "revision": ...}]}`，按手号排序。

### preview

返回：

```json
{
  "head": 2,
  "revisions": [{"hand_no": 1, "revision": 1}, {"hand_no": 2, "revision": 1}],
  "hands": [ {"hand_no": 1, "revision": 1, "source": {...}, "report": {...},
              "opening_balances": {"A": 10, "B": 10},
              "closing_balances": {"A": 15, "B": 5}}, ... ],
  "blocking": null
}
```

从名册期初余额出发逐手检查：每个参与者的完整 `committed` 不得超过其本手期初余额，弃牌者也一样；通过后才应用该手 v1 的逐人 `net`。未匹配筹码退回在牌局结束时才到账，不能提前用于下注，因此检查用的是 `committed` 而非净额。

第一手不满足约束时，`hands` 只含此前可用前缀，`blocking` 为：

```json
{"hand_no": 2, "revision": 1,
 "shortfalls": [{"player_id": "A", "opening_balance": 5, "committed": 12}]}
```

`shortfalls` 按玩家 ID 排序。没有阻塞则 `blocking` 为 `null`。余额阻塞是正常的录入状态，不是命令错误。

### release

```sh
showdown-ledger --state TABLE.db release --request-id R2 --through 2
```

结算第 1..N 手，N ≥ 1。不能比上一份已发结单短，也不能越过当前可用前缀。已经发出的结单不会因后续更正而撤销或改写；修复前既不能用更短结单，也不能用部分更正替代。

返回：

```json
{
  "request_id": "R2",
  "through": 2,
  "previous_request_id": "R1",
  "hands": [ 前缀条目，结构同 preview ],
  "closing_balances": {"A": 10, "B": 10},
  "postings": [{"hand_no": 1, "player_id": "A", "delta": -10}, ...]
}
```

`postings` 对每手每人用本次 v1 净额减去**上一次实际发出的结单**中该手该人的净额；没有旧凭据时旧值为 0，本次未包含的玩家新值也为 0。只记录非零项，按手号、玩家 ID 排序，不跨手抵消。没有净额变化的牌谱更正仍以新 revision、原始输入和 v1 报告出现在新结单中（此时 `postings` 为空）。

把每批 `postings` 依次加到名册期初余额上，恰好得到该批 `closing_balances`；每批分录之和为 0。

请求 ID 沿用玩家 ID 字符格式。同一 ID、同一 N 的重试直接返回原结单，不新增分录，也不再重新检查当前可用前缀或不得缩短的规则——即使发起者没收到 stdout 也能安全取回。同一 ID 改 N 报错。

### read-release

```sh
showdown-ledger --state TABLE.db read-release --request-id R1
```

返回该请求当时的完整结单 JSON；之后的录入或结单不会改变它。

## 示例：两手与更正链

公共牌 `2c 3d 4h 8s 9c`；`5c 6c` 为顺子，`Kh Ks` 为一对。A、B 期初各 10。

1. 第一手各投入 5，A 用 `5c 6c` 胜；第二手 A 投入 12、B 投入 5，B 用 `5c 6c` 胜，A 退回 7。`release R1 --through 2`，两人期末各 10。
2. 只把第一手更正为 B 胜：A 在第一手后只剩 5，无法承担第二手的 12。`preview` 停在第一手，`blocking` 指向第二手；`release R2 --through 2` 与 `--through 1` 都被拒绝。`read-release R1` 仍返回原结单。
3. 再把第二手更正为各投入 5、A 胜。`release R2 --through 2` 成功，差额为第一手 A -10/B +10、第二手 A +10/B -10，两人期末仍各 10。

完整可执行版本见 `tests/test_ledger.py` 的 `test_correction_chain_and_immutable_releases`。

## 边界

仅支持单进程顺序操作；没有下注街次合法性审查、抽水、保险、多次发牌或真实资金接入。结单是可重读的本地教学记录，不是赌场结算认证。
