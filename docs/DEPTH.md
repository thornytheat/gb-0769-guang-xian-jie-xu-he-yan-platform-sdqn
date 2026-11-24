# 深度登记 光纤接续核验台

- 规模：中型
- 核心规则：校准双向链程后做单调一对一最优配对，按带符号均值验设计接头。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 轨迹文件采信 | 检查原行与隔离原因 | POST /api/trace | `app/modules/trace/service.py` | `tests/test_trace.py` |
| M2 距离与方向校准 | 核对双端坐标与起偏 | POST /api/calibration | `app/modules/calibration/service.py` | `tests/test_calibration.py` `tests/test_settings.py` |
| M3 双端事件最优配对 | 核查相邻冲突及孤测 | POST /api/pairs | `app/modules/pairs/optimal.py` | `tests/test_pairs.py` `tests/test_pair_oracle.py` |
| M4 接续净损耗判定 | 对照带符号读数和均值 | POST /api/loss | `app/modules/loss/service.py` | `tests/test_loss.py` |
| M5 设计与实测对账 | 核对逐接头是否缺证 | POST /api/design | `app/modules/design/service.py` | `tests/test_design.py` |
| M6 批次留档复跑 | 留档和重跑同一轨迹组 | POST /api/inspections | `app/modules/archive/storage.py` | `tests/test_archive.py` |
| M7 光纤证据签发 | 复核签发条件并下载 | GET /api/certificate/<id> | `app/modules/certificate/service.py` | `tests/test_certificate.py` |
| M8 双波长排障工单 | 按两波长判断派发建议、领取与修订 | POST /api/workorders | `app/modules/workorder/service.py` | `tests/test_workorder.py` |

## 陷阱登记

| TRAP | 现象 | 朴素错误 | 处理 | 位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-02 | A 端首列 BOM | 事件列读不到 | 清理首 BOM | `app/modules/trace/reader.py` | `tests/test_trace.py::test_bom_and_repeated_header` |
| TRAP-04 | A 端续行表头 | 当配对记录 | 按分段另计 | `app/modules/trace/reader.py` | `tests/test_trace.py::test_bom_and_repeated_header` |
| TRAP-05 | A-Y 列尾多字段 | 错配字段值 | 原行隔离 | `app/modules/trace/reader.py` | `tests/test_trace.py::test_extra_fields_keep_raw_location` |
| TRAP-07 | A-N 哨兵、B-G 空值 | 当成零损耗 | 两种原因分别留档 | `app/reasons.py` | `tests/test_trace.py::test_missing_sentinel_and_blank_are_isolated` |
| TRAP-10 | 0.2005 dB 半毫精度 | 先舍入误过限 | 用两倍毫 dB 判界 | `app/modules/loss/verdict.py` | `tests/test_loss.py::test_double_mdb_preserves_half_increment` |
| TRAP-14 | A-C 同键异距 | 后写覆盖 | 整组隔离 | `app/modules/trace/service.py` | `tests/test_trace.py::test_conflicting_records_both_withdrawn` |
| TRAP-15 | A-02 米和千米并存 | 两条虚假轨迹 | 归一毫米后比对 | `app/modules/trace/units.py` | `tests/test_trace.py::test_equivalent_units_dedupe` |
| TRAP-19 | A100 可匹配 B97 或 B101 | 最近优先导致 A105 无法配对 | 先最大配对数再最小间差 | `app/modules/pairs/optimal.py` | `tests/test_pairs.py::test_global_pairing_beats_greedy_and_keeps_competition` |
| TRAP-21 | A700 和 B820 均无对侧 | 只报一端孤测 | 分别记录缺 A 或缺 B | `app/modules/pairs/service.py` | `tests/test_pairs.py::test_opposite_ends_missing_in_both_directions` |
| TRAP-22 | 单波长超限、另一波长缺测 | 直接按超限派重熔 | 缺测波长不作判据，派补测工单并禁止重熔 | `app/modules/workorder/model.py` | `tests/test_workorder.py::test_wavelength_missing_one_end_is_unmeasured_not_over` |
| TRAP-23 | 仅长波超限 | 与短波超限同样重熔 | 弯折特征优先，派弯折排查且禁止重熔 | `app/modules/workorder/model.py` | `tests/test_workorder.py::test_two_over_limit_wavelengths_refuse_joint_and_long_only_checks_bend` |
| TRAP-24 | 重跑或补录重试 | 同一接头重复开工单 | 按批次+接头定工单号，内容定补录号，幂等 | `app/modules/workorder/service.py` | `tests/test_workorder.py::test_rerun_does_not_dispatch_twice` `::test_retried_measurement_is_idempotent` |
| TRAP-25 | 晚到测量改变判断 | 覆盖已领工单的依据 | 追加修订版本，领取时依据单独留存 | `app/modules/workorder/storage.py` | `tests/test_workorder.py::test_claim_keeps_original_basis_after_late_measurement` |

## 对账规则

- 6 个设计接头 = 3 通过 + 1 超限 + 2 缺证。
- 12 条窗内端点 = 2×(4 已挂接 + 1 孤配) + 2 单端。窗外测量另计 1 条。
- F-01 双向均值 (−0.100+0.300)/2=0.1000 dB，F-03 为 (0.33+0.31)/2=0.3200 dB，超出 0.200 dB。

输入为设备导出的事件表。二进制曲线解码、坡度自校与电子签章未纳入该工作台。
