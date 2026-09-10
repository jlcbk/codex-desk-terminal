# tests/fixtures/protocol — P0.4-draft 协议 fixtures 清单

状态：DRAFT（P0.4-draft，待 A0 审查冻结）。真源：`docs/INTERFACES.md` §2 示例与 §3 规则表。
映射验收行：`docs/DEVELOPMENT_PLAN.md` §5 P0.4（「schema 校验样本；确认 null/unknown、上限、序号和本地电池边界」）与 §8 测试策略表「协议」行（「null/缺字段/未知版本、超长字符串/数组、非法 UTF-8、超大JSON/嵌套 | schema+设备有界解析；拒绝后状态不变」）。

配套 schema：`protocol/state.schema.json`（DRAFT，待 A0）。
配套 C 类型：`shared/state/codex_state.h`（DRAFT，待 A0）。

## 运行

```sh
/Users/cui/.local/bin/uv run --with jsonschema python scripts/check_protocol.py
```

预期输出：15/15 PASS，退出码 0。`valid_*` 必须通过 schema 与全部解析器级检查；`invalid_*` 必须被列出的一层拒绝。

## 检查层说明

| 层 | 名称 | 依据 |
|---|---|---|
| schema | JSON Schema draft-2020-12 校验 | `protocol/state.schema.json` |
| utf8 | 合法 UTF-8 严格解码 | §8 协议行「非法 UTF-8」 |
| size | 整包 ≤16384 字节 | §3 完整消息行 |
| depth | 嵌套深度 ≤12（根对象计 1，容器每层 +1；§2 示例深度为 6） | §3 完整消息行 |
| bytes | 各字符串字段 UTF-8 字节上限（读 schema `cdt-utf8-max-bytes` 注解） | §3 字符串行、bridge_epoch 行 |
| consistency | threads_total ≥ len(threads)、usage.windows_total ≥ len(windows)、thread id 唯一、selected_thread_id 为 null 或指向已包含线程 | §3 threads/usage/selected_thread_id 行 |

JSON Schema 无法表达跨字段一致性与字节级限制（maxLength 按字符计），故由 consistency/bytes/size/depth 层执行；schema 与检查器共同构成设备有界解析的完整前置条件（P1.4 实现同一规则集）。

## fixtures

| 编号 | 文件 | 场景 | 预期 | 决定层 | 对应规则 / §8 协议行用例 |
|---|---|---|---|---|---|
| F01 | valid_minimal.json | 最小合法包：threads=[]、usage.available=false 且 windows=[]，可空字段全部 null，seq=0 | accept | — | P0.4「确认 null/unknown、上限、序号」；§3 usage 行「缺额度 available=false、windows=[]」 |
| F02 | valid_full.json | INTERFACES §2 示例原样（含中文 activity/summary、plan 三步、usage 窗口） | accept | — | §2 示例即协议样本（P0.4「schema 校验样本」） |
| F03 | valid_unicode.json | 中文 project/activity/summary/label/id 混合英文，全部在字节上限内 | accept | — | §8 协议行之外的字节边界内合法性；§6「项目名/摘要允许中文」 |
| F04 | valid_max_sizes.json | 边界顶格：bridge_epoch=64B、id/turn_id/window id=128B、project=96B（32 汉字）、activity/summary=192B（64 汉字）、plan.text=128B（thread 0）、label=48B、seq=2^53-1、8 线程×8 步骤×4 窗口；整包 13310B ≤16384 | accept | — | §3 全部上限行的上边界；P0.4「上限、序号」 |
| F05 | invalid_state_enum.json | thread.state="sleeping"（不在六状态枚举） | reject | schema | §3 state 行「未知枚举拒绝该快照」；§8「未知版本」同类的未知枚举 |
| F06 | invalid_missing_required.json | 顶层缺必填字段 seq | reject | schema | §8「null/缺字段」；§3 末段「缺必填字段拒绝整包，不能半应用」 |
| F07 | invalid_version.json | schema_version=2 | reject | schema | §8「未知版本」；§3 schema_version/kind 行「未知主版本拒绝并显示协议不兼容」 |
| F08 | invalid_string_overlong.json | activity=65 个汉字=195B>192B（注意 65 字符≤maxLength 192，schema 放行，正是 cdt-utf8-max-bytes 注解存在的理由） | reject | bytes | §8「超长字符串」；§3 字符串行 activity≤192B |
| F09 | invalid_threads_over.json | threads 数组 9 项（threads_total=9 一致，仅数组越上限） | reject | schema | §8「超长字符串/数组」；§3 threads 行「最多 8 项」 |
| F10 | invalid_usage_percent.json | usage 窗口 used_percent=150 | reject | schema | §3 usage 行「百分比 0–100 或 null」（数值越界） |
| F11 | invalid_selected_id.json | selected_thread_id="thread-ghost" 不指向 threads 中任何 id（schema 无法表达，通过 schema 校验） | reject | consistency | §3 selected_thread_id 行「null或必须指向已包含线程」；§8「schema+设备有界解析」 |
| F12 | invalid_nested_depth.json | 未知附加字段 vendor_depth_probe 嵌套数组达深度 13（根对象计 1）。additionalProperties:true 使 schema 前向兼容放行未知字段，此用例由检查器的深度检查拒绝而非 schema | reject | depth | §8「超大JSON/嵌套」；§3 完整消息行「嵌套深度≤12」与末段「未知附加字段必须计入限制」 |
| F13 | invalid_oversize.json | 整包 17860B>16384B：以未知附加字段 vendor_padding（17200 个 A）构造，schema 对未知字段无长度限制故放行，由检查器整包字节检查拒绝 | reject | size | §8「超大JSON」；§3 完整消息行「UTF-8 JSON≤16384字节」 |
| F14 | invalid_utf8.bin | 二进制 fixture：外形为 state JSON，含 0xFF 0xFE 与截断序列 0xC3 0x28 | reject | utf8 | §8「非法 UTF-8」；§3 字符串行「按UTF-8完整码点」 |

## 已知预算张力（记录给 A0，非契约矛盾）

8 线程 × 8 步骤 × 全部步骤文本顶满 128B，加上其余字符串顶格，将超过 16384B 整包预算（粗算 >17KB）。因此 F04 中 plan.text 仅在 thread 0 的 8 个步骤顶满 128B，threads 1–7 步骤用短文本；每类字符串字段仍至少一处顶满字节上限。这与 §3 裁剪顺序行（「仍超16KiB则逐步减少非选中低优先级线程/步骤」）的设计一致：全上限组合不是合法稳态，Bridge 必须裁剪。

## 覆盖缺口（P1.4/P1.5 接手）

- seq 同 epoch 重复/回退（IGNORED_STALE_SEQ）需 StateStore 已应用 seq 上下文，fixture 级无法表达；由 P1.4 有界解析/store 测试覆盖（§3 seq 行、§7「收到已经应用的同epoch同seq全包应ACK但不再渲染」）。
- telemetry.schema.json 为草案（INTERFACES §1 未定义字段），暂无 fixtures；A0 冻结后补充。
- 深度恰为 12（合法）与 13（非法）的边界对：F12 只覆盖非法侧；合法侧深度 12 由 F04（深度 6）未覆盖，可在 P1.5 补 F15。
