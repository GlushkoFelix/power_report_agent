# 评测报告（批次合并） · run_20260919_17 + run_20260919_18 + run_20260919_19

> **本报告由多个批次合并生成**（非单批跑出）：单批 120 题在真实模型下约需 60–70 万 tokens，
> 额度受限时分两批执行（如 q001–q060 / q061–q120），本报告把两批逐题明细合并为对外口径。
> 工具：`01.code/tools/eval_merge.py`；指标算法与 `eval_service._aggregate` 一致。

- 合并批次：run_20260919_17（56/60）、run_20260919_18（13/15）、run_20260919_19（22/22）
- 模型：qwen3.7-flash；提示词版本：prompt-v3.9
- 题量：97；执行正确率：**93.8%**（91/97，目标 ≥85%）
- 结论命中率：**79.4%**
- 平均耗时：9571 ms；P95 耗时：18445 ms（目标 ≤8000 ms）
- 重试总次数：1

## 一、分类正确率

### 按题型

| 题型 | 题量 | 正确 | 正确率 |
| --- | --- | --- | --- |
| stat | 25 | 22 | 88.0% |
| agg | 25 | 24 | 96.0% |
| compare | 10 | 10 | 100.0% |
| anomaly | 7 | 7 | 100.0% |
| structure | 15 | 15 | 100.0% |
| multi_turn | 15 | 13 | 86.7% |

### 按难度

| 难度 | 题量 | 正确 | 正确率 |
| --- | --- | --- | --- |
| easy | 26 | 26 | 100.0% |
| medium | 52 | 48 | 92.3% |
| hard | 19 | 17 | 89.5% |

### 按陷阱题（口径红线）

| 分组 | 题量 | 正确 | 正确率 | 覆盖红线 |
| --- | --- | --- | --- | --- |
| 陷阱题 | 18 | 17 | 94.4% | R1、R3、R6、R7 |

## 二、失败分布

| 差异类型 | 次数 |
| --- | --- |
| value_mismatch | 4 |
| row_count_mismatch | 1 |
| column_count_mismatch | 1 |

| 错误码 | 次数 |
| --- | --- |
| （无） | 0 |

> 判分档命中分布：第一档 91 题、第二档 0 题、第三档 0 题

## 三、逐题明细

| 题号 | 来源批次 | 题型 | 难度 | 陷阱 | 结果 | 判分档 | 差异类型 | 耗时(ms) | 重试 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| q001 | run_20260919_17 | stat | easy | — | ✅ | 1 | — | 6301 | 0 |
| q002 | run_20260919_17 | stat | easy | — | ✅ | 1 | — | 7251 | 0 |
| q003 | run_20260919_17 | stat | medium | — | ✅ | 1 | — | 8826 | 0 |
| q004 | run_20260919_17 | stat | medium | — | ✅ | 1 | — | 9751 | 0 |
| q005 | run_20260919_17 | stat | easy | — | ✅ | 1 | — | 5019 | 0 |
| q006 | run_20260919_17 | stat | easy | — | ✅ | 1 | — | 5244 | 0 |
| q007 | run_20260919_17 | stat | easy | — | ✅ | 1 | — | 4791 | 0 |
| q008 | run_20260919_17 | stat | easy | — | ✅ | 1 | — | 5123 | 0 |
| q009 | run_20260919_17 | stat | medium | — | ✅ | 1 | — | 7376 | 0 |
| q010 | run_20260919_17 | stat | medium | — | ✅ | 1 | — | 4510 | 0 |
| q011 | run_20260919_17 | stat | medium | R | ✅ | 1 | — | 7552 | 0 |
| q012 | run_20260919_17 | stat | medium | R | ✅ | 1 | — | 7111 | 0 |
| q013 | run_20260919_17 | stat | medium | R | ✅ | 1 | — | 7684 | 0 |
| q014 | run_20260919_17 | stat | hard | R | ✅ | 1 | — | 8408 | 0 |
| q015 | run_20260919_17 | stat | hard | R | ❌ | — | row_count_mismatch | 10812 | 1 |
| q016 | run_20260919_17 | stat | easy | — | ✅ | 1 | — | 5409 | 0 |
| q017 | run_20260919_17 | stat | easy | — | ✅ | 1 | — | 4736 | 0 |
| q018 | run_20260919_17 | stat | easy | — | ✅ | 1 | — | 4659 | 0 |
| q019 | run_20260919_17 | stat | easy | — | ✅ | 1 | — | 4324 | 0 |
| q020 | run_20260919_17 | stat | easy | — | ✅ | 1 | — | 4895 | 0 |
| q021 | run_20260919_17 | stat | medium | — | ❌ | — | value_mismatch | 6670 | 0 |
| q022 | run_20260919_17 | stat | medium | — | ✅ | 1 | — | 7197 | 0 |
| q023 | run_20260919_17 | stat | medium | — | ✅ | 1 | — | 6739 | 0 |
| q024 | run_20260919_17 | stat | medium | — | ❌ | — | value_mismatch | 7496 | 0 |
| q025 | run_20260919_17 | stat | medium | — | ✅ | 1 | — | 7695 | 0 |
| q026 | run_20260919_17 | agg | easy | — | ✅ | 1 | — | 9509 | 0 |
| q027 | run_20260919_17 | agg | easy | — | ✅ | 1 | — | 9575 | 0 |
| q028 | run_20260919_17 | agg | easy | — | ✅ | 1 | — | 8950 | 0 |
| q029 | run_20260919_17 | agg | medium | — | ✅ | 1 | — | 9713 | 0 |
| q030 | run_20260919_17 | agg | easy | — | ✅ | 1 | — | 9301 | 0 |
| q031 | run_20260919_17 | agg | medium | — | ✅ | 1 | — | 8341 | 0 |
| q032 | run_20260919_17 | agg | medium | — | ✅ | 1 | — | 8912 | 0 |
| q033 | run_20260919_17 | agg | medium | — | ✅ | 1 | — | 8887 | 0 |
| q034 | run_20260919_17 | agg | hard | — | ❌ | — | value_mismatch | 9142 | 0 |
| q035 | run_20260919_17 | agg | hard | — | ✅ | 1 | — | 9415 | 0 |
| q036 | run_20260919_17 | agg | hard | R | ✅ | 1 | — | 9209 | 0 |
| q037 | run_20260919_17 | agg | hard | R | ✅ | 1 | — | 10114 | 0 |
| q038 | run_20260919_17 | agg | hard | R | ✅ | 1 | — | 9913 | 0 |
| q039 | run_20260919_17 | agg | hard | R | ✅ | 1 | — | 8402 | 0 |
| q040 | run_20260919_17 | agg | medium | — | ✅ | 1 | — | 8486 | 0 |
| q041 | run_20260919_17 | agg | medium | — | ✅ | 1 | — | 8708 | 0 |
| q042 | run_20260919_17 | agg | medium | — | ✅ | 1 | — | 8293 | 0 |
| q043 | run_20260919_17 | agg | medium | — | ✅ | 1 | — | 9626 | 0 |
| q044 | run_20260919_17 | agg | medium | — | ✅ | 1 | — | 9558 | 0 |
| q045 | run_20260919_17 | agg | medium | — | ✅ | 1 | — | 10315 | 0 |
| q046 | run_20260919_17 | agg | medium | — | ✅ | 1 | — | 8903 | 0 |
| q047 | run_20260919_17 | agg | medium | — | ✅ | 1 | — | 9874 | 0 |
| q048 | run_20260919_17 | agg | medium | — | ✅ | 1 | — | 10481 | 0 |
| q049 | run_20260919_17 | agg | medium | — | ✅ | 1 | — | 10032 | 0 |
| q050 | run_20260919_17 | agg | medium | — | ✅ | 1 | — | 9244 | 0 |
| q051 | run_20260919_17 | compare | easy | — | ✅ | 1 | — | 10253 | 0 |
| q052 | run_20260919_17 | compare | easy | — | ✅ | 1 | — | 8366 | 0 |
| q053 | run_20260919_17 | compare | easy | — | ✅ | 1 | — | 9129 | 0 |
| q054 | run_20260919_17 | compare | medium | R | ✅ | 1 | — | 8190 | 0 |
| q055 | run_20260919_17 | compare | medium | R | ✅ | 1 | — | 8113 | 0 |
| q056 | run_20260919_17 | compare | medium | R | ✅ | 1 | — | 9098 | 0 |
| q057 | run_20260919_17 | compare | hard | R | ✅ | 1 | — | 10086 | 0 |
| q058 | run_20260919_17 | compare | hard | R | ✅ | 1 | — | 8946 | 0 |
| q059 | run_20260919_17 | compare | medium | — | ✅ | 1 | — | 8476 | 0 |
| q060 | run_20260919_17 | compare | medium | — | ✅ | 1 | — | 8963 | 0 |
| q106 | run_20260919_18 | multi_turn | medium | — | ✅ | 1 | — | 18602 | 0 |
| q107 | run_20260919_18 | multi_turn | medium | — | ✅ | 1 | — | 18570 | 0 |
| q108 | run_20260919_18 | multi_turn | medium | — | ✅ | 1 | — | 16976 | 0 |
| q109 | run_20260919_18 | multi_turn | easy | — | ✅ | 1 | — | 14039 | 0 |
| q110 | run_20260919_18 | multi_turn | medium | — | ✅ | 1 | — | 22454 | 0 |
| q111 | run_20260919_18 | multi_turn | medium | — | ✅ | 1 | — | 18445 | 0 |
| q112 | run_20260919_18 | multi_turn | easy | — | ✅ | 1 | — | 14485 | 0 |
| q113 | run_20260919_18 | multi_turn | easy | — | ✅ | 1 | — | 23053 | 0 |
| q114 | run_20260919_18 | multi_turn | hard | — | ✅ | 1 | — | 15752 | 0 |
| q115 | run_20260919_18 | multi_turn | hard | — | ✅ | 1 | — | 17181 | 0 |
| q116 | run_20260919_18 | multi_turn | medium | — | ✅ | 1 | — | 13421 | 0 |
| q117 | run_20260919_18 | multi_turn | medium | — | ✅ | 1 | — | 13750 | 0 |
| q118 | run_20260919_18 | multi_turn | medium | — | ❌ | — | value_mismatch | 16984 | 0 |
| q119 | run_20260919_18 | multi_turn | medium | — | ❌ | — | column_count_mismatch | 16843 | 0 |
| q120 | run_20260919_18 | multi_turn | medium | — | ✅ | 1 | — | 13235 | 0 |
| q084 | run_20260919_19 | anomaly | medium | — | ✅ | 1 | — | 7036 | 0 |
| q085 | run_20260919_19 | anomaly | medium | — | ✅ | 1 | — | 6963 | 0 |
| q086 | run_20260919_19 | anomaly | hard | — | ✅ | 1 | — | 6917 | 0 |
| q087 | run_20260919_19 | anomaly | hard | — | ✅ | 1 | — | 7076 | 0 |
| q088 | run_20260919_19 | anomaly | hard | — | ✅ | 1 | — | 6993 | 0 |
| q089 | run_20260919_19 | anomaly | hard | — | ✅ | 1 | — | 8174 | 0 |
| q090 | run_20260919_19 | anomaly | hard | — | ✅ | 1 | — | 6914 | 0 |
| q091 | run_20260919_19 | structure | easy | — | ✅ | 1 | — | 8820 | 0 |
| q092 | run_20260919_19 | structure | easy | — | ✅ | 1 | — | 9521 | 0 |
| q093 | run_20260919_19 | structure | easy | — | ✅ | 1 | — | 4867 | 0 |
| q094 | run_20260919_19 | structure | easy | — | ✅ | 1 | — | 11138 | 0 |
| q095 | run_20260919_19 | structure | hard | — | ✅ | 1 | — | 9594 | 0 |
| q096 | run_20260919_19 | structure | easy | — | ✅ | 1 | — | 6722 | 0 |
| q097 | run_20260919_19 | structure | medium | — | ✅ | 1 | — | 7054 | 0 |
| q098 | run_20260919_19 | structure | medium | R | ✅ | 1 | — | 12450 | 0 |
| q099 | run_20260919_19 | structure | medium | R | ✅ | 1 | — | 8966 | 0 |
| q100 | run_20260919_19 | structure | medium | R | ✅ | 1 | — | 8344 | 0 |
| q101 | run_20260919_19 | structure | hard | R | ✅ | 1 | — | 9299 | 0 |
| q102 | run_20260919_19 | structure | medium | — | ✅ | 1 | — | 9909 | 0 |
| q103 | run_20260919_19 | structure | medium | — | ✅ | 1 | — | 9588 | 0 |
| q104 | run_20260919_19 | structure | medium | — | ✅ | 1 | — | 9612 | 0 |
| q105 | run_20260919_19 | structure | medium | — | ✅ | 1 | — | 10562 | 0 |

## 四、错题复盘（生成 vs 标准）

### q015 · 全期峰段用电量占总用电量的比例是多少？

- 标准 SQL：`SELECT SUM(peak_kwh) * 1.0 / SUM(consumption_kwh) AS peak_ratio FROM report_daily`
- 生成 SQL/代码：`SELECT peak_ratio FROM report_daily LIMIT 1000`
- 差异：行数不符：gold 1 行，生成 1000 行

### q021 · 2006 年平均每天用多少电？

- 标准 SQL：`SELECT AVG(consumption_kwh) AS avg_kwh FROM report_daily WHERE year = 2006`
- 生成 SQL/代码：`SELECT AVG(daily_avg_kwh) AS avg_daily_consumption FROM report_daily WHERE year = 2006 LIMIT 1000`
- 差异：取值不一致（差异类型：value_mismatch）

### q024 · 2009 年平均每天用多少电？

- 标准 SQL：`SELECT AVG(consumption_kwh) AS avg_kwh FROM report_daily WHERE year = 2009`
- 生成 SQL/代码：`SELECT AVG(daily_avg_kwh) AS avg_daily_kwh FROM report_daily WHERE year = 2009 LIMIT 1000`
- 差异：取值不一致（差异类型：value_mismatch）

### q034 · 按星期几看平均每天用电量。

- 标准 SQL：`SELECT weekday, AVG(consumption_kwh) AS avg_kwh FROM report_daily GROUP BY weekday ORDER BY weekday`
- 生成 SQL/代码：`SELECT weekday, AVG(daily_avg_kwh) AS avg_daily_kwh FROM report_daily GROUP BY weekday ORDER BY weekday LIMIT 1000`
- 差异：取值不一致（差异类型：value_mismatch）

### q118 · 2007 年用电量最高的那天用了多少电？ / 那最低的那天呢？

- 标准 SQL：`SELECT date, consumption_kwh FROM report_daily WHERE year = 2007 AND consumption_kwh IS NOT NULL ORDER BY consumption_kwh ASC LIMIT 1`
- 生成 SQL/代码：`SELECT date, consumption_kwh FROM report_daily WHERE year = 2007 ORDER BY consumption_kwh ASC LIMIT 1`
- 差异：取值不一致（差异类型：value_mismatch）

### q119 · 按星期几汇总总用电量。 / 只看工作日。

- 标准 SQL：`SELECT weekday, SUM(consumption_kwh) AS consumption_kwh FROM report_daily WHERE is_weekend = 0 GROUP BY weekday ORDER BY weekday`
- 生成 SQL/代码：`SELECT SUM(consumption_kwh) AS total_consumption_kwh FROM report_daily WHERE is_weekend = 0 LIMIT 1000`
- 差异：列数不符：gold 2 列（['weekday', 'consumption_kwh']），生成 1 列（['total_consumption_kwh']）

