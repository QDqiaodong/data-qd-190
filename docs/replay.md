# 下注回放 `showdown-replay`

`showdown-replay` 把教学录像里的下注动作逐条回放到教学桌规则上：给出下一位的可选动作；只有真正走到终局，才把整手毛投入交给现有 v1 单手结算（`showdown_pots.service.process`），输出完整 v1 报告。

它不是联网牌桌。不处理盲注、抢注、未亮牌、抽水、保险、多次发牌或多手余额；所有金额都是整数筹码。

旧命令 `showdown-pots`、`showdown-ledger`、`showdown-audit`、`showdown-reconcile`、`showdown-corpus` 的输入、输出、退出码与契约保持不变。

## 命令行

```sh
showdown-replay --source request.json --output run/replay.json
```

成功时原子新建 `--output`（不覆盖已有目标，含符号链接），并在 stdout 打印摘要：

```json
{"settled": false, "street": "flop"}
```

或试放模式：

```json
{"branches": 2, "ok": 1}
```

非法输入、坏 JSON／坏结构、坏检查点、读写失败：stderr 说明原因，退出 2，不生成结果文件。

## 从开局回放

```json
{
  "start": {"button_seat": 0, "board": ["2c","3d","4h","8s","9c"],
            "ante": 1, "min_bet": 10,
            "players": [{"player_id": "A", "seat": 1, "stack": 101, "hole": ["5c","6c"]}, ...]},
  "actions": [{"player_id": "A", "kind": "raise_to", "to": 10}, ...]
}
```

- `players` 2..9 人；`player_id` 沿用 v1 的 `[A-Za-z0-9_:-]{1,64}`，`seat` 0..8，ID 与座位各自唯一，座位无须占满，按钮可以落在空座。
- `board` 恰好 5 张；每人 `hole` 恰好 2 张，整副明示牌（含后来弃牌者）不重复。牌面字符串沿用 v1：`23456789TJQKA` + `cdhs`。
- `ante`、`min_bet` 为 1..10^9 的整数。每人期初 `stack` 为 `ante+1`..10^9；开局先每人扣 `ante`，并计入整手毛投入，然后进入 flop。
- 每场 0..256 个动作，顺序即录像顺序，不自动跳过非法项。`kind` 为 `check`/`call`/`fold`/`raise_to`；只有 `raise_to` 另有整数 `to`（1..10^9），下注也用 `raise_to`。
- 输出 `{checkpoint,next,settlement}`；未终局时 `settlement=null`，终局时 `next=null` 且 `settlement` 是 v1 报告。

## 把停点交给另一进程

`checkpoint` 是一个自足 JSON 对象，只含派生状态、不含既往动作序列。复制它的 JSON 值到新进程的 `{"checkpoint": …, "branches": …}` 即可继续，不需要原始录像、旧动作列表、状态库或缓存：

```json
{"checkpoint": { … copy the object verbatim … },
 "branches": [{"id": "student-a", "actions": [ … ]},
              {"id": "student-b", "actions": [ … ]}]}
```

- 批次 1..64 个分支，分支 ID 沿用 v1 字符规则且批内唯一。
- 每个分支从同一原始停点出发，0..256 个动作；分支之间互不影响。
- 输出 `{"branches":[…]}`，按分支 ID ASCII 排序。成功项为 `{id,status:"ok",checkpoint,next,settlement}`；成功的中途停点可以再次这样交接。
- 结构正确但不合法的动作（没轮到、欠跟时过牌、无权加注、金额或时机违反桌规）只让**该分支**失败：`{id,status:"illegal",action_index,reason}`，`action_index` 从该分支第 1 个动作起指向第一条错误，`reason` 为非空可读原因。失败分支不给停点、不给派彩，也不把自己的行动历史或筹码交给同批其他分支；兄弟分支仍出完整结果。
- 结构本身错误（未知玩家 ID、分支 ID 重复或非法、字段类型／范围不符、分支数越界）是整份输入错误，整次命令退出 2。

检查点带明确版本 `version: 1`。未知版本或结构不符（缺字段、多字段、类型／范围错误、筹码自相矛盾）都拒绝。不要求签名、防篡改，也不验证它确实曾由某段录像产生。

### 4096 字节上限

教学终端停点消息只给检查点对象预留 4096 字节。按 UTF-8、对象键排序、无空白、无换行、非 ASCII 不转义的 JSON 编码计，任何合法输入产生的检查点都不会超出（9 人、最长 64 字符 ID、各金额取 10^9 上限时约 2.2 KB）。命令在生成检查点时即按此编码核算，超限按输入错误退出 2。

## 回放口径

- 只有三条下注街：flop → turn → river。每街开始时该街投入、当前价与“本街已行动”清零，最小完整涨幅设为 `min_bet`。
- 每街第一位从按钮之后顺时针找（座位递增并绕回 0），其后从刚行动者之后找。弃牌、余额为零者不再行动。
- 街内仍待行动者：未弃牌、有余额，且本街尚未行动或本街投入小于当前价。但只剩**一个有余额的未弃牌者**时，只有其欠跟才需要行动，且不得加注。没人待行动即闭街；两名以上未弃牌者进入下一街，第三街后摊牌；只剩一名未弃牌者立即终局。无人还能行动的街自动跳过。
- 当前价＝本街最高投入；`欠跟 = 当前价 − 本街已投入`。
  - `check` 仅在不欠跟时允许；`call` 仅在欠跟时允许，支付 `min(欠额, 余额)`；当前行动者可 `fold`，弃牌不取回已投筹码。
  - `raise_to.to` 是此人行动后的**本街累计投入**，须高于当前价且付得起。加注前还须至少另一未弃牌且有余额的玩家在场。
  - 加注权：本街未行动者有；已行动者只有当前价比**其上次行动完成时的当前价**至少高出当前最小完整涨幅时才重新有权（含上次过牌）。涨幅＝新目标 − 旧当前价；达到最小完整涨幅即完整加注，并把最小完整涨幅更新为此次涨幅。低于它只允许投光余额，此时提高当前价但不改变最小完整涨幅。短额全下可累计让某人重获加注权，却不一定给同局面的其他人加注权。
  - 合法动作结束后，以该动作结束时的当前价记录其上次行动基准；别的加注不重置它。
- 筹码从 ante 起累计为整手毛投入，不在街间提前退回。

## 输出字段

未终局的 `next`：

```json
{"player_id": "A", "street": "flop", "to_call": 10, "call_chips": 10,
 "can_check": false, "raise_to_min": 30, "raise_to_max": 100,
 "short_all_in_to": null}
```

- `street` 取 `flop`/`turn`/`river`；`to_call` 为欠额；`call_chips` 为短筹码实际能付的金额（可过牌时为 0）。
- 能完整加注时，`raise_to_min`/`raise_to_max` 给出合法完整加注目标的闭区间；否则皆为 `null`。
- `short_all_in_to` 仅当存在合法短额全下加注目标时给值，否则 `null`。

终局时 `next=null`，`settlement` 是 v1 报告：按 v1 原规则分主池和边池、处理弃牌资格、并列余筹与未匹配退回。未终局不出推测派彩。终局停点允许用空动作重读；对它提交新动作只报该分支 `illegal`。

## 验收示例：短额全下累计加注权

按钮座位 0，A..E 坐 1..5，期初筹码 101、101、15、101、21，`ante=1`、`min_bet=10`；公共牌 `2c 3d 4h 8s 9c`，底牌 A `5c 6c`、B `Kh Ks`、C `9d 9h`、D `Jc Jd`、E `8c 8h`。

录入 A 到 10、B 跟、C 全下到 14、D 跟、E 全下到 20，导出停点：

```json
{"start": {"button_seat": 0, "board": ["2c","3d","4h","8s","9c"], "ante": 1, "min_bet": 10,
  "players": [
    {"player_id":"A","seat":1,"stack":101,"hole":["5c","6c"]},
    {"player_id":"B","seat":2,"stack":101,"hole":["Kh","Ks"]},
    {"player_id":"C","seat":3,"stack":15,"hole":["9d","9h"]},
    {"player_id":"D","seat":4,"stack":101,"hole":["Jc","Jd"]},
    {"player_id":"E","seat":5,"stack":21,"hole":["8c","8h"]}]},
 "actions": [
   {"player_id":"A","kind":"raise_to","to":10},
   {"player_id":"B","kind":"call"},
   {"player_id":"C","kind":"raise_to","to":14},
   {"player_id":"D","kind":"call"},
   {"player_id":"E","kind":"raise_to","to":20}]}
```

结果 `next` 中 A 欠 10；两次短额（4+6=10）累计够一个最小完整涨幅，因此 `raise_to_min` 为 30。

- 只把 E 期初改成 20、最后全下改到 19：A 欠 9，不能加注（`raise_to_min`/`raise_to_max`/`short_all_in_to` 皆 `null`）。
- 原例 A、B 都跟：轮到 D 欠 6，D 上次面对 14，不能加注。
- 若 A 到 30、B 跟：轮到 D 欠 16，D 最小可到 40。

## 验收示例：把停点交给分支

复制上面的检查点，在新进程提交四个分支（成功后删除原录像源文件也不影响）：

```json
{"checkpoint": { … 上面输出的 checkpoint 对象 … },
 "branches": [
  {"id":"normal","actions":[
    {"player_id":"A","kind":"call"},{"player_id":"B","kind":"call"},{"player_id":"D","kind":"call"},
    {"player_id":"A","kind":"check"},{"player_id":"B","kind":"check"},{"player_id":"D","kind":"check"},
    {"player_id":"A","kind":"check"},{"player_id":"B","kind":"check"},{"player_id":"D","kind":"check"}]},
  {"id":"raise","actions":[
    {"player_id":"A","kind":"raise_to","to":30},{"player_id":"B","kind":"call"},{"player_id":"D","kind":"fold"},
    {"player_id":"A","kind":"check"},{"player_id":"B","kind":"check"},
    {"player_id":"A","kind":"check"},{"player_id":"B","kind":"check"}]},
  {"id":"fail-d","actions":[
    {"player_id":"A","kind":"call"},{"player_id":"B","kind":"call"},{"player_id":"D","kind":"raise_to","to":30}]},
  {"id":"fail-order","actions":[
    {"player_id":"A","kind":"call"},{"player_id":"B","kind":"call"},{"player_id":"D","kind":"call"},
    {"player_id":"A","kind":"check"},{"player_id":"A","kind":"check"}]}]}
```

- `normal`：池 75、24，A 净得 78。
- `raise`：池 75、18、20，A 净得 82。
- `fail-d`：让 D 试图到 30，`action_index=3`；`fail-order`：A 连过两次，`action_index=5`。两者 `status="illegal"`，不影响两个成功分支。
- 再取 `raise` 分支走到 A 到 30、B 跟时的停点，在新进程从 D 弃牌起继续，终局报告与一次走完 `raise` 分支、以及从开局直放相同。

打乱 `branches` 输入顺序不改变输出（按分支 ID ASCII 排序）。

## 边界

一次回放单手，不维护多手余额或跨手历史；不处理盲注、抢注、未亮牌、抽水、保险或多次发牌。检查点只是可复制的派生状态，不签名、不防篡改，也不证明来源。输出是教学数据模型结果，不是赌场结算认证。离线回归测试见 `tests/test_replay.py`。
