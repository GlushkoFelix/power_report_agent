# 评测报告（批次合并） · run_20260919_10 + run_20260919_11 + run_20260919_12 + run_20260919_13

> **本报告由多个批次合并生成**（非单批跑出）：单批 120 题在真实模型下约需 60–70 万 tokens，
> 额度受限时分两批执行（如 q001–q060 / q061–q120），本报告把两批逐题明细合并为对外口径。
> 工具：`01.code/tools/eval_merge.py`；指标算法与 `eval_service._aggregate` 一致。

- 合并批次：run_20260919_10（10/10）、run_20260919_11（54/55）、run_20260919_12（40/41）、run_20260919_13（10/14）
- 模型：kimi-k3；提示词版本：prompt-v3.7
- 题量：120；执行正确率：**95.0%**（114/120，目标 ≥85%）
- 结论命中率：**82.1%**
- 平均耗时：13846 ms；P95 耗时：27847 ms（目标 ≤8000 ms）
- 重试总次数：0

## 一、分类正确率

### 按题型

| 题型 | 题量 | 正确 | 正确率 |
| --- | --- | --- | --- |
| stat | 25 | 25 | 100.0% |
| agg | 25 | 25 | 100.0% |
| compare | 20 | 19 | 95.0% |
| anomaly | 20 | 19 | 95.0% |
| structure | 15 | 15 | 100.0% |
| multi_turn | 15 | 11 | 73.3% |

### 按难度

| 难度 | 题量 | 正确 | 正确率 |
| --- | --- | --- | --- |
| easy | 27 | 26 | 96.3% |
| medium | 71 | 68 | 95.8% |
| hard | 22 | 20 | 90.9% |

### 按陷阱题（口径红线）

| 分组 | 题量 | 正确 | 正确率 | 覆盖红线 |
| --- | --- | --- | --- | --- |
| 陷阱题 | 18 | 18 | 100.0% | R1、R3、R6、R7 |

## 二、失败分布

| 差异类型 | 次数 |
| --- | --- |
| empty_result | 5 |
| value_mismatch | 1 |

| 错误码 | 次数 |
| --- | --- |
| E_LLM | 5 |

> 判分档命中分布：第一档 114 题、第二档 0 题、第三档 0 题

## 三、逐题明细

| 题号 | 来源批次 | 题型 | 难度 | 陷阱 | 结果 | 判分档 | 差异类型 | 耗时(ms) | 重试 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| q005 | run_20260919_10 | stat | easy | — | ✅ | 1 | — | 9265 | 0 |
| q020 | run_20260919_10 | stat | easy | — | ✅ | 1 | — | 7358 | 0 |
| q027 | run_20260919_10 | agg | easy | — | ✅ | 1 | — | 14012 | 0 |
| q045 | run_20260919_10 | agg | medium | — | ✅ | 1 | — | 15448 | 0 |
| q057 | run_20260919_10 | compare | hard | R | ✅ | 1 | — | 14731 | 0 |
| q066 | run_20260919_10 | compare | medium | — | ✅ | 1 | — | 11835 | 0 |
| q078 | run_20260919_10 | anomaly | medium | — | ✅ | 1 | — | 10204 | 0 |
| q087 | run_20260919_10 | anomaly | hard | — | ✅ | 1 | — | 11282 | 0 |
| q096 | run_20260919_10 | structure | easy | — | ✅ | 1 | — | 11120 | 0 |
| q112 | run_20260919_10 | multi_turn | easy | — | ✅ | 1 | — | 21279 | 0 |
| q001 | run_20260919_11 | stat | easy | — | ✅ | 1 | — | 19425 | 0 |
| q002 | run_20260919_11 | stat | easy | — | ✅ | 1 | — | 13999 | 0 |
| q003 | run_20260919_11 | stat | medium | — | ✅ | 1 | — | 11955 | 0 |
| q004 | run_20260919_11 | stat | medium | — | ✅ | 1 | — | 12811 | 0 |
| q006 | run_20260919_11 | stat | easy | — | ✅ | 1 | — | 7116 | 0 |
| q007 | run_20260919_11 | stat | easy | — | ✅ | 1 | — | 12734 | 0 |
| q008 | run_20260919_11 | stat | easy | — | ✅ | 1 | — | 8629 | 0 |
| q009 | run_20260919_11 | stat | medium | — | ✅ | 1 | — | 11851 | 0 |
| q010 | run_20260919_11 | stat | medium | — | ✅ | 1 | — | 7664 | 0 |
| q011 | run_20260919_11 | stat | medium | R | ✅ | 1 | — | 10843 | 0 |
| q012 | run_20260919_11 | stat | medium | R | ✅ | 1 | — | 12292 | 0 |
| q013 | run_20260919_11 | stat | medium | R | ✅ | 1 | — | 10801 | 0 |
| q014 | run_20260919_11 | stat | hard | R | ✅ | 1 | — | 11801 | 0 |
| q015 | run_20260919_11 | stat | hard | R | ✅ | 1 | — | 12842 | 0 |
| q016 | run_20260919_11 | stat | easy | — | ✅ | 1 | — | 12726 | 0 |
| q017 | run_20260919_11 | stat | easy | — | ✅ | 1 | — | 7759 | 0 |
| q018 | run_20260919_11 | stat | easy | — | ✅ | 1 | — | 8278 | 0 |
| q019 | run_20260919_11 | stat | easy | — | ✅ | 1 | — | 8262 | 0 |
| q021 | run_20260919_11 | stat | medium | — | ✅ | 1 | — | 10162 | 0 |
| q022 | run_20260919_11 | stat | medium | — | ✅ | 1 | — | 11371 | 0 |
| q023 | run_20260919_11 | stat | medium | — | ✅ | 1 | — | 11852 | 0 |
| q024 | run_20260919_11 | stat | medium | — | ✅ | 1 | — | 10780 | 0 |
| q025 | run_20260919_11 | stat | medium | — | ✅ | 1 | — | 11925 | 0 |
| q026 | run_20260919_11 | agg | easy | — | ✅ | 1 | — | 14343 | 0 |
| q028 | run_20260919_11 | agg | easy | — | ✅ | 1 | — | 16704 | 0 |
| q029 | run_20260919_11 | agg | medium | — | ✅ | 1 | — | 13478 | 0 |
| q030 | run_20260919_11 | agg | easy | — | ✅ | 1 | — | 15037 | 0 |
| q031 | run_20260919_11 | agg | medium | — | ✅ | 1 | — | 15949 | 0 |
| q032 | run_20260919_11 | agg | medium | — | ✅ | 1 | — | 14120 | 0 |
| q033 | run_20260919_11 | agg | medium | — | ✅ | 1 | — | 12773 | 0 |
| q034 | run_20260919_11 | agg | hard | — | ✅ | 1 | — | 16939 | 0 |
| q035 | run_20260919_11 | agg | hard | — | ✅ | 1 | — | 12205 | 0 |
| q036 | run_20260919_11 | agg | hard | R | ✅ | 1 | — | 14539 | 0 |
| q037 | run_20260919_11 | agg | hard | R | ✅ | 1 | — | 14537 | 0 |
| q038 | run_20260919_11 | agg | hard | R | ✅ | 1 | — | 15567 | 0 |
| q039 | run_20260919_11 | agg | hard | R | ✅ | 1 | — | 14187 | 0 |
| q040 | run_20260919_11 | agg | medium | — | ✅ | 1 | — | 13534 | 0 |
| q041 | run_20260919_11 | agg | medium | — | ✅ | 1 | — | 13751 | 0 |
| q042 | run_20260919_11 | agg | medium | — | ✅ | 1 | — | 14345 | 0 |
| q043 | run_20260919_11 | agg | medium | — | ✅ | 1 | — | 13748 | 0 |
| q044 | run_20260919_11 | agg | medium | — | ✅ | 1 | — | 12382 | 0 |
| q046 | run_20260919_11 | agg | medium | — | ✅ | 1 | — | 15307 | 0 |
| q047 | run_20260919_11 | agg | medium | — | ✅ | 1 | — | 14079 | 0 |
| q048 | run_20260919_11 | agg | medium | — | ✅ | 1 | — | 13780 | 0 |
| q049 | run_20260919_11 | agg | medium | — | ✅ | 1 | — | 27435 | 0 |
| q050 | run_20260919_11 | agg | medium | — | ✅ | 1 | — | 14275 | 0 |
| q051 | run_20260919_11 | compare | easy | — | ✅ | 1 | — | 13933 | 0 |
| q052 | run_20260919_11 | compare | easy | — | ❌ | — | empty_result | 6087 | 0 |
| q053 | run_20260919_11 | compare | easy | — | ✅ | 1 | — | 14482 | 0 |
| q054 | run_20260919_11 | compare | medium | R | ✅ | 1 | — | 12016 | 0 |
| q055 | run_20260919_11 | compare | medium | R | ✅ | 1 | — | 12942 | 0 |
| q056 | run_20260919_11 | compare | medium | R | ✅ | 1 | — | 12000 | 0 |
| q058 | run_20260919_11 | compare | hard | R | ✅ | 1 | — | 14108 | 0 |
| q059 | run_20260919_11 | compare | medium | — | ✅ | 1 | — | 18103 | 0 |
| q060 | run_20260919_11 | compare | medium | — | ✅ | 1 | — | 12818 | 0 |
| q061 | run_20260919_12 | compare | medium | — | ✅ | 1 | — | 13205 | 0 |
| q062 | run_20260919_12 | compare | hard | — | ✅ | 1 | — | 13502 | 0 |
| q063 | run_20260919_12 | compare | medium | — | ✅ | 1 | — | 12794 | 0 |
| q064 | run_20260919_12 | compare | medium | — | ✅ | 1 | — | 14394 | 0 |
| q065 | run_20260919_12 | compare | medium | — | ✅ | 1 | — | 12493 | 0 |
| q067 | run_20260919_12 | compare | medium | — | ✅ | 1 | — | 15975 | 0 |
| q068 | run_20260919_12 | compare | medium | — | ✅ | 1 | — | 10979 | 0 |
| q069 | run_20260919_12 | compare | medium | — | ✅ | 1 | — | 13428 | 0 |
| q070 | run_20260919_12 | compare | medium | — | ✅ | 1 | — | 11473 | 0 |
| q071 | run_20260919_12 | anomaly | easy | — | ✅ | 1 | — | 10175 | 0 |
| q072 | run_20260919_12 | anomaly | medium | — | ✅ | 1 | — | 14408 | 0 |
| q073 | run_20260919_12 | anomaly | medium | — | ✅ | 1 | — | 7702 | 0 |
| q074 | run_20260919_12 | anomaly | medium | — | ✅ | 1 | — | 6997 | 0 |
| q075 | run_20260919_12 | anomaly | medium | — | ✅ | 1 | — | 13488 | 0 |
| q076 | run_20260919_12 | anomaly | medium | — | ✅ | 1 | — | 10704 | 0 |
| q077 | run_20260919_12 | anomaly | medium | — | ✅ | 1 | — | 12060 | 0 |
| q079 | run_20260919_12 | anomaly | hard | — | ✅ | 1 | — | 15436 | 0 |
| q080 | run_20260919_12 | anomaly | hard | — | ✅ | 1 | — | 11406 | 0 |
| q081 | run_20260919_12 | anomaly | medium | — | ✅ | 1 | — | 10909 | 0 |
| q082 | run_20260919_12 | anomaly | medium | — | ✅ | 1 | — | 11810 | 0 |
| q083 | run_20260919_12 | anomaly | medium | — | ❌ | — | empty_result | 6070 | 0 |
| q084 | run_20260919_12 | anomaly | medium | — | ✅ | 1 | — | 12514 | 0 |
| q085 | run_20260919_12 | anomaly | medium | — | ✅ | 1 | — | 9709 | 0 |
| q086 | run_20260919_12 | anomaly | hard | — | ✅ | 1 | — | 10456 | 0 |
| q088 | run_20260919_12 | anomaly | hard | — | ✅ | 1 | — | 10844 | 0 |
| q089 | run_20260919_12 | anomaly | hard | — | ✅ | 1 | — | 14043 | 0 |
| q090 | run_20260919_12 | anomaly | hard | — | ✅ | 1 | — | 11252 | 0 |
| q091 | run_20260919_12 | structure | easy | — | ✅ | 1 | — | 14358 | 0 |
| q092 | run_20260919_12 | structure | easy | — | ✅ | 1 | — | 11828 | 0 |
| q093 | run_20260919_12 | structure | easy | — | ✅ | 1 | — | 8207 | 0 |
| q094 | run_20260919_12 | structure | easy | — | ✅ | 1 | — | 13508 | 0 |
| q095 | run_20260919_12 | structure | hard | — | ✅ | 1 | — | 11919 | 0 |
| q097 | run_20260919_12 | structure | medium | — | ✅ | 1 | — | 12789 | 0 |
| q098 | run_20260919_12 | structure | medium | R | ✅ | 1 | — | 13447 | 0 |
| q099 | run_20260919_12 | structure | medium | R | ✅ | 1 | — | 16958 | 0 |
| q100 | run_20260919_12 | structure | medium | R | ✅ | 1 | — | 13199 | 0 |
| q101 | run_20260919_12 | structure | hard | R | ✅ | 1 | — | 13674 | 0 |
| q102 | run_20260919_12 | structure | medium | — | ✅ | 1 | — | 13107 | 0 |
| q103 | run_20260919_12 | structure | medium | — | ✅ | 1 | — | 11361 | 0 |
| q104 | run_20260919_12 | structure | medium | — | ✅ | 1 | — | 11424 | 0 |
| q105 | run_20260919_12 | structure | medium | — | ✅ | 1 | — | 13742 | 0 |
| q106 | run_20260919_13 | multi_turn | medium | — | ✅ | 1 | — | 28129 | 0 |
| q107 | run_20260919_13 | multi_turn | medium | — | ❌ | — | empty_result | 6085 | 0 |
| q108 | run_20260919_13 | multi_turn | medium | — | ✅ | 1 | — | 28826 | 0 |
| q109 | run_20260919_13 | multi_turn | easy | — | ✅ | 1 | — | 26160 | 0 |
| q110 | run_20260919_13 | multi_turn | medium | — | ✅ | 1 | — | 27847 | 0 |
| q111 | run_20260919_13 | multi_turn | medium | — | ❌ | — | empty_result | 6086 | 0 |
| q113 | run_20260919_13 | multi_turn | easy | — | ✅ | 1 | — | 35840 | 0 |
| q114 | run_20260919_13 | multi_turn | hard | — | ❌ | — | empty_result | 6120 | 0 |
| q115 | run_20260919_13 | multi_turn | hard | — | ❌ | — | value_mismatch | 34273 | 0 |
| q116 | run_20260919_13 | multi_turn | medium | — | ✅ | 1 | — | 20074 | 0 |
| q117 | run_20260919_13 | multi_turn | medium | — | ✅ | 1 | — | 22257 | 0 |
| q118 | run_20260919_13 | multi_turn | medium | — | ✅ | 1 | — | 22965 | 0 |
| q119 | run_20260919_13 | multi_turn | medium | — | ✅ | 1 | — | 28417 | 0 |
| q120 | run_20260919_13 | multi_turn | medium | — | ✅ | 1 | — | 26856 | 0 |

## 四、错题复盘（生成 vs 标准）

### q052 · 峰段、平段、谷段的用电量分别是多少？

- 标准 SQL：`SELECT SUM(peak_kwh) AS peak_kwh, SUM(flat_kwh) AS flat_kwh, SUM(valley_kwh) AS valley_kwh FROM report_daily`
- 生成 SQL/代码：`—`
- 差异：生成结果为空（未执行成功或没有数据）（错误码：E_LLM）

### q083 · 2008 年有多少天被判定为用电异常？

- 标准 SQL：`SELECT COUNT(*) AS anomaly_days FROM report_daily WHERE is_anomaly = 1 AND year = 2008`
- 生成 SQL/代码：`—`
- 差异：生成结果为空（未执行成功或没有数据）（错误码：E_LLM）

### q107 · 按月看看 2008 年的用电情况。 / 那 2009 年呢？

- 标准 SQL：`SELECT month, SUM(consumption_kwh) AS consumption_kwh FROM report_daily WHERE year = 2009 GROUP BY month ORDER BY month`
- 生成 SQL/代码：`—`
- 差异：生成结果为空（未执行成功或没有数据）（错误码：E_LLM）

### q111 · 峰平谷时段的用电量分别是多少？ / 只看峰段，按月汇总。

- 标准 SQL：`SELECT year_month, peak_kwh FROM report_monthly ORDER BY year_month`
- 生成 SQL/代码：`—`
- 差异：生成结果为空（未执行成功或没有数据）（错误码：E_LLM）

### q114 · 全期有多少天被判定为用电异常？ / 这些异常日主要分布在哪些年份？

- 标准 SQL：`SELECT year, COUNT(*) AS anomaly_days FROM report_daily WHERE is_anomaly = 1 GROUP BY year ORDER BY year`
- 生成 SQL/代码：`—`
- 差异：生成结果为空（未执行成功或没有数据）（错误码：E_LLM）

### q115 · 全期各用途用电量的占比是多少？ / 按时段再看一遍。

- 标准 SQL：`SELECT peak_ratio, flat_ratio, valley_ratio FROM report_energy_structure WHERE is_total_row = 1`
- 生成 SQL/代码：`SELECT SUM(peak_kwh) AS peak_kwh, SUM(flat_kwh) AS flat_kwh, SUM(valley_kwh) AS valley_kwh FROM report_energy_structure WHERE is_total_row = 0 LIMIT 1000`
- 差异：取值不一致（差异类型：value_mismatch）

