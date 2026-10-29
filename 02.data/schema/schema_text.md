# 智电析报 · 报表 Schema 说明（供分析助手使用）

- 口径版本：`caliber-v1.0`
- 数据来源：UCI Individual Household Electric Power Consumption（#235），2006-12-16 17:24:00 → 2010-11-26 21:02:00
- 数据规模：2,075,259 分钟行；缺失 25,979 行（1.25%），时间轴连续（缺口 0 分钟）
- 时段口径：谷 00:00–07:59 / 平 08:00–17:59 / 峰 18:00–23:59（左闭右开）

## 一、可用报表（表）

### report_minute — 分钟级明细表
- 说明：分钟级明细（207 万行；仅入库，不导出 CSV）
- 行数：2,075,259
- 时间范围：2006-12-16 17:24:00 → 2010-11-26 21:02:00

| 字段 | 类型 | 单位 | 可否 SUM | 说明 |
| --- | --- | --- | --- | --- |
| `dt` | TEXT | — | — | 时间戳 'YYYY-MM-DD HH:MM:SS'（唯一） |
| `year` | INTEGER | — | **否** |  |
| `month` | INTEGER | — | **否** | 1-12 |
| `day` | INTEGER | — | **否** | 1-31 |
| `hour` | INTEGER | — | **否** | 0-23 |
| `weekday` | INTEGER | 0=周一 | **否** | 0=周一 … 6=周日 |
| `is_weekend` | INTEGER | — | **否** | 1=周六/周日 |
| `period` | TEXT | — | — | peak 峰 / flat 平 / valley 谷 |
| `gap_kw` | REAL | kW | **否** | 总有功功率(kW) |
| `gap_kwh` | REAL | kWh | 是 | 分钟电量 = gap_kw / 60 |
| `sm1_kwh` | REAL | kWh | 是 | 厨房（原始 Wh / 1000） |
| `sm2_kwh` | REAL | kWh | 是 | 洗衣房 |
| `sm3_kwh` | REAL | kWh | 是 | 热水器与空调 |
| `unmetered_kwh` | REAL | kWh | 是 | 未计量 = gap_kw/60 − sm1 − sm2 − sm3 |
| `voltage` | REAL | V | — | 电压(V) |
| `intensity` | REAL | A | — | 电流(A) |
| `reactive_power_kw` | REAL | kW | **否** | 无功功率(kW) |
| `is_missing` | INTEGER | — | **否** | 1=原始行缺失（数值列全为 NULL） |

### report_hourly — 小时报
- 说明：小时报（约 3.46 万行）
- 行数：34,589
- 时间范围：2006-12-16 17:00:00 → 2010-11-26 21:00:00

| 字段 | 类型 | 单位 | 可否 SUM | 说明 |
| --- | --- | --- | --- | --- |
| `dt_hour` | TEXT | — | — | 'YYYY-MM-DD HH:00:00'（唯一） |
| `year` | INTEGER | — | **否** |  |
| `month` | INTEGER | — | **否** |  |
| `day` | INTEGER | — | **否** |  |
| `hour` | INTEGER | — | **否** |  |
| `weekday` | INTEGER | 0=周一 | **否** |  |
| `is_weekend` | INTEGER | — | **否** |  |
| `period` | TEXT | — | — | 该小时所属时段（一小时完整落在单一时段内） |
| `consumption_kwh` | REAL | kWh | 是 | 小时总电量 |
| `sm1_kwh` | REAL | kWh | 是 |  |
| `sm2_kwh` | REAL | kWh | 是 |  |
| `sm3_kwh` | REAL | kWh | 是 |  |
| `unmetered_kwh` | REAL | kWh | 是 |  |
| `peak_kwh` | REAL | kWh | 是 | 时段电量（按时段正交拆分之一） |
| `flat_kwh` | REAL | kWh | 是 |  |
| `valley_kwh` | REAL | kWh | 是 |  |
| `active_minutes` | INTEGER | 分钟 | **否** | 有效分钟数 |
| `expected_minutes` | INTEGER | 分钟 | **否** | 自然时间长度（恒 60） |
| `coverage` | REAL | 比率 | **否** | 比率，不可 SUM |
| `avg_power_kw` | REAL | kW | **否** | 功率，不可 SUM |
| `max_power_kw` | REAL | kW | **否** |  |
| `min_power_kw` | REAL | kW | **否** |  |
| `avg_voltage` | REAL | V | — |  |
| `avg_intensity` | REAL | A | — |  |

### report_daily — 日报
- 说明：日报（约 1442 行）
- 行数：1,442
- 时间范围：2006-12-16 → 2010-11-26

| 字段 | 类型 | 单位 | 可否 SUM | 说明 |
| --- | --- | --- | --- | --- |
| `date` | TEXT | — | — | 'YYYY-MM-DD'（唯一） |
| `year` | INTEGER | — | **否** |  |
| `month` | INTEGER | — | **否** |  |
| `day` | INTEGER | — | **否** |  |
| `weekday` | INTEGER | 0=周一 | **否** |  |
| `is_weekend` | INTEGER | — | **否** |  |
| `consumption_kwh` | REAL | kWh | 是 |  |
| `sm1_kwh` | REAL | kWh | 是 |  |
| `sm2_kwh` | REAL | kWh | 是 |  |
| `sm3_kwh` | REAL | kWh | 是 |  |
| `unmetered_kwh` | REAL | kWh | 是 |  |
| `peak_kwh` | REAL | kWh | 是 |  |
| `flat_kwh` | REAL | kWh | 是 |  |
| `valley_kwh` | REAL | kWh | 是 |  |
| `active_minutes` | INTEGER | 分钟 | **否** |  |
| `expected_minutes` | INTEGER | 分钟 | **否** |  |
| `coverage` | REAL | 比率 | **否** | 比率，不可 SUM |
| `avg_power_kw` | REAL | kW | **否** |  |
| `max_power_kw` | REAL | kW | **否** |  |
| `min_power_kw` | REAL | kW | **否** |  |
| `avg_voltage` | REAL | V | — |  |
| `avg_intensity` | REAL | A | — |  |
| `daily_avg_kwh` | REAL | kWh | 是 | 按有效数据折算；active_minutes=0 时 NULL |
| `load_factor` | REAL | 比率 | **否** | 比率，不可 SUM |
| `sm1_ratio` | REAL | 比率 | **否** |  |
| `sm2_ratio` | REAL | 比率 | **否** |  |
| `sm3_ratio` | REAL | 比率 | **否** |  |
| `unmetered_ratio` | REAL | 比率 | **否** |  |
| `peak_ratio` | REAL | 比率 | **否** |  |
| `flat_ratio` | REAL | 比率 | **否** |  |
| `valley_ratio` | REAL | 比率 | **否** |  |
| `peak_hour` | INTEGER | 整点(0-23) | **否** | 当日峰值整点 0-23（18 表示 18:00–18:59） |
| `is_anomaly` | INTEGER | — | **否** |  |
| `anomaly_reason` | TEXT | — | — | 命中规则名，多规则以 | 分隔 |
| `is_partial_period` | INTEGER | — | **否** | 1=不完整自然日（首/末日） |
| `partial_reason` | TEXT | — | — |  |

### report_monthly — 月报
- 说明：月报（48 行）
- 行数：48
- 时间范围：2006-12 → 2010-11

| 字段 | 类型 | 单位 | 可否 SUM | 说明 |
| --- | --- | --- | --- | --- |
| `year_month` | TEXT | — | — | 'YYYY-MM'（唯一） |
| `year` | INTEGER | — | **否** |  |
| `month` | INTEGER | — | **否** |  |
| `days_in_month` | INTEGER | 天 | **否** |  |
| `consumption_kwh` | REAL | kWh | 是 |  |
| `sm1_kwh` | REAL | kWh | 是 |  |
| `sm2_kwh` | REAL | kWh | 是 |  |
| `sm3_kwh` | REAL | kWh | 是 |  |
| `unmetered_kwh` | REAL | kWh | 是 |  |
| `peak_kwh` | REAL | kWh | 是 |  |
| `flat_kwh` | REAL | kWh | 是 |  |
| `valley_kwh` | REAL | kWh | 是 |  |
| `active_minutes` | INTEGER | 分钟 | **否** |  |
| `expected_minutes` | INTEGER | 分钟 | **否** | 当月天数 * 1440 |
| `coverage` | REAL | 比率 | **否** |  |
| `avg_power_kw` | REAL | kW | **否** |  |
| `max_power_kw` | REAL | kW | **否** |  |
| `min_power_kw` | REAL | kW | **否** |  |
| `daily_avg_kwh` | REAL | kWh | 是 |  |
| `mom_rate` | REAL | 比率 | **否** | 环比（比率，不可 SUM） |
| `yoy_rate` | REAL | 比率 | **否** | 同比（比率，不可 SUM） |
| `is_anomaly` | INTEGER | — | **否** |  |
| `anomaly_reason` | TEXT | — | — |  |
| `is_partial_period` | INTEGER | — | **否** | 1=不完整自然月（首/末月） |
| `partial_reason` | TEXT | — | — |  |

### report_energy_structure — 用电结构报表
- 说明：用电结构报表（6 行：2006–2010 五年 + 全期）
- 行数：6

| 字段 | 类型 | 单位 | 可否 SUM | 说明 |
| --- | --- | --- | --- | --- |
| `period` | TEXT | — | — | '2006'…'2010' / '全期'（唯一） |
| `is_total_row` | INTEGER | — | — | 1=全期合计行 |
| `year` | INTEGER | — | **否** | 全期行为 NULL |
| `kitchen_kwh` | REAL | kWh | 是 |  |
| `laundry_kwh` | REAL | kWh | 是 |  |
| `water_heating_ac_kwh` | REAL | kWh | 是 |  |
| `other_unmetered_kwh` | REAL | kWh | 是 |  |
| `kitchen_ratio` | REAL | 比率 | **否** |  |
| `laundry_ratio` | REAL | 比率 | **否** |  |
| `water_heating_ac_ratio` | REAL | 比率 | **否** |  |
| `other_unmetered_ratio` | REAL | 比率 | **否** |  |
| `peak_kwh` | REAL | kWh | 是 |  |
| `flat_kwh` | REAL | kWh | 是 |  |
| `valley_kwh` | REAL | kWh | 是 |  |
| `peak_ratio` | REAL | 比率 | **否** |  |
| `flat_ratio` | REAL | 比率 | **否** |  |
| `valley_ratio` | REAL | 比率 | **否** |  |
| `total_kwh` | REAL | kWh | 是 | 周期总电量 |
| `structure_closed` | REAL | — | **否** | 用途四项之和 − total_kwh（应≈0） |
| `time_closed` | REAL | — | **否** | 时段三项之和 − total_kwh（应≈0） |

## 二、口径红线（必须遵守，违反即答案错误）

- **R1**：量纲：`*_kwh` 是电量（可相加）；`*_kw` 是功率（**不可相加**，只能求平均/最大/最小）；`_kwh` 已由 kW÷60、Wh÷1000 统一换算，**不要再次换算**。
- **R2**：未计量用电 `unmetered_kwh` = `gap_kwh` − `sm1_kwh` − `sm2_kwh` − `sm3_kwh`（代表其他电器），报表已直接提供该列，**不要自行推导公式**。
- **R3**：两套**正交**拆分：按用途（`sm1_kwh` 厨房 / `sm2_kwh` 洗衣房 / `sm3_kwh` 热水空调 / `unmetered_kwh` 未计量）与按时段（`peak_kwh` 峰 / `flat_kwh` 平 / `valley_kwh` 谷）。两套各自求和都等于总量，**混加会得到约 2 倍错误结果**，禁止在同一聚合里混用。
- **R4**：`expected_minutes` 是自然时间长度（小时 60 / 日 1440 / 月 = 天数×1440），与是否有数据无关；`coverage` = `active_minutes` / `expected_minutes`，**首末周期覆盖率为低值是边界效应，不是异常**。
- **R5**：日均用电 `daily_avg_kwh` = 电量 × 1440 / `active_minutes`（按有效数据折算），**不可 SUM**；`active_minutes = 0` 时该值为 NULL。
  **口径区分（重要，易错）**：问「平均每天用多少电 / 全期日均是多少」时，取 **`consumption_kwh` 的算术平均**（即全期总电量 ÷ 有数据的天数，写作 `AVG(consumption_kwh)`）；`daily_avg_kwh` 是"按有效数据折算到整日"的派生值，只在**单日折算或跨缺采日可比**时使用（例如「7 月 3 日的日均用电是多少」），**不要用它求全期平均**（两者口径不同：实测 26.21 vs 26.02 kWh/日）。
- **R6**：所有比率型字段（`*_ratio`、`coverage`、`load_factor`、`mom_rate`、`yoy_rate`）**不可 SUM**——跨周期汇总必须「分子求和 ÷ 分母求和」重算。
- **R7**：结构报表中 `period = '全期'` 的行（`is_total_row = 1`）是各年份行的合计，**求和时绝不可与年份行一起 SUM**（否则结果翻倍）。

### 二·补 默认口径（**无需询问用户，直接按此执行**；v1.19.6 补，实测错题最大来源）

- **包含全部行**：聚合与统计默认纳入**所有记录**，包括首日/首月等**不完整周期**（`is_partial_period = 1`）
  以及含 NULL 的行（NULL 由 `SUM` / `AVG` / `COUNT(列)` 等聚合函数**自然忽略**，不需要手动过滤）。
  **禁止自行添加** `WHERE is_partial_period = 0` 或 `WHERE xxx IS NOT NULL` 之类条件，
  除非用户**明确**要求「只看完整周期 / 排除缺失数据」。
  例：「日报里一共记录了多少天？」→ `SELECT COUNT(*)`（`FROM` 用**本报表**表名；含不完整首末日）；
  **不是** `... WHERE is_partial_period = 0`。
- **时间维度用现成整数列**：问「某年各月」「各年度」时用 `year` / `month` **整数列**，
  例：`SELECT month, SUM(consumption_kwh) AS kwh WHERE year = 2006 GROUP BY month ORDER BY month`
  （`FROM` 用本报表表名）；
  **不要**用 `STRFTIME('%Y-%m', date)` 拼字符串（与报表列口径不一致）。
- **口径细节不发起澄清**：指标已明确时直接给出结果；缺时间范围按「全期」、缺粒度按问题自然推断。

### 二·补 2 查询形态与结果形状（**必须遵守**；v2.1 补，逐题复盘 30 题真实验证得出）

- **「按 X 看 / 按 X 汇总」= 分组聚合**：必须 `GROUP BY X`、**一行一组**；不要 `SELECT X, 明细列`
  或只 `ORDER BY X` 把全部明细行列出来。
  例：「按小时看平均功率」→ `SELECT hour, AVG(avg_power_kw) AS avg_power_kw GROUP BY hour ORDER BY hour`（24 行）。
- **若本报表已有现成的粒度列，直接用它，不要再 GROUP BY**：月报有 `year_month`、小时报有 `dt_hour` / `hour`；
  问「按月看 / 改成按月汇总」在**月报**上 = `SELECT year_month, 指标 ORDER BY year_month`
  （**48 行、一月一行**），**不要** `GROUP BY month`（那会把跨年同月合并成 12 行，**实测因此丢 2 题**）。
- **本报表没有该粒度列时才是"跨年合并"**：例如在**日报**上按「月 / 星期 / 小时」**汇总**，用 `month` / `weekday` / `hour`
  这类**周期整数列**分组，**不要带 `year`**；用户点名年份时才加 `WHERE year = 2006`。
  例：「按月份**汇总**全期的总用电量」（日报）→ `SELECT month, SUM(consumption_kwh) AS consumption_kwh GROUP BY month ORDER BY month`（**2 列 12 行**）。
- **问「看变化 / 趋势 / 走势」时保留时间轴，不要跨年合并**（与上一条的"汇总"区分，**实测各丢过题**）：
  日报「按月看**变化**」（未指定年份）→ 用 `substr(date, 1, 7)` 得**逐月**（**48 行**），
  例：`SELECT substr(date, 1, 7) AS year_month, AVG(avg_voltage) AS avg_voltage GROUP BY year_month ORDER BY year_month`；
  只有「按月**汇总 / 合计**」才合并成 12 行。
  > 上两条的 `FROM` 一律用**本报表**表名；**示例里不写别的报表表名**——SQL 白名单只放行本会话的活跃报表，
  > 照抄别的表名会被守卫拦截。
- **结构报表的「全期」= 取 `is_total_row = 1` 的那一行**：该行已直接给出 `total_kwh` / `*_ratio` /
  `structure_closed` / `time_closed` 等**现成字段**，`WHERE is_total_row = 1` 后直接 SELECT 这些列即可；
  **不要**用 `is_total_row = 0` 的年份行去 SUM 或重算比率（既绕远又易错）。
- **分清「按 A、B、C 汇总 / 分组」与「A、B、C 分别是多少」**（两者结果形状不同，**实测各丢过题**）：
  - 「**按**峰、平、谷**时段汇总**总用电量」→ **按维度分组聚合**：本报表有现成时段列时 `GROUP BY period`，
    返回 **2 列**（维度 + 指标），例：`SELECT period, SUM(consumption_kwh) AS consumption_kwh GROUP BY period ORDER BY period`；
  - 「峰段、平段、谷段的用电量**分别是多少**」→ **宽表并排**：每指标**一列**、一行给完，
    例：`SELECT SUM(peak_kwh) AS peak_kwh, SUM(flat_kwh) AS flat_kwh, SUM(valley_kwh) AS valley_kwh`。
  两种写法都**禁止**用 `UNION ALL` 拼成"名称 / 数值"两列长表。
- **比率 / 占比按 0–1 返回**：`*_ratio` / `coverage` / `load_factor` 的取值本身就是 0–1（0.35 即 35%），
  **不要 `* 100`**；需要百分比表述时放进解读文字说明，查询结果保持 0–1。
- **问「占比 / 比例」时结果只含【维度列（若问题要求分组）+ 比率列】**：**不要**把分子 / 分母的绝对值列
  也一起 SELECT 出来，**也不要把仅用于过滤的维度列带进 SELECT**（**实测因此丢 3 题**）。
  例：「全期峰段用电量占总用电量的比例是多少？」→ 只返回 `peak_ratio` **一列**
  （**不是** `peak_kwh, total_kwh, peak_ratio` 三列）；
  「2007 年各用途用电量的占比分别是多少？」→ 只返回四个 `*_ratio` 列（**年份只在 `WHERE` 里**，不进 SELECT）；
  「各年度未计量用电占比分别是多少？」→ 只有问题要求「按年度分别看」时才返回 `year, unmetered_ratio` 两列。
- **`anomaly_reason` 按原值整体计数**：该列是多个原因用 `|` 拼接的文本；问「异常原因分别出现了多少次」时
  **直接 `GROUP BY anomaly_reason`**（原样分组，`WHERE is_anomaly = 1`），
  **不要**按 `|` 拆分成多条原因分别计数（与标准口径不一致，**实测因此丢 1 题**）。
- **分组维度列返回原始值**：`is_weekend` 给 `0/1`、`period` 给原值，**不要自造中文标签列**
  （如 `CASE WHEN is_weekend = 1 THEN '周末' ...` 会与标准结果口径不符，**实测因此丢 1 题**）；
  需要中文说明时写进解读文字。
- **「日均用电量」按列取值，「平均每天用多少电」按均值**（R5 的措辞区分，**实测因此丢 1 题**）：
  - 问「**各年度的日均用电量** / 日均用电量是多少」→ 取 **`daily_avg_kwh`** 列（聚合用 `AVG(daily_avg_kwh)`）；
  - 问「**平均每天用多少电** / 全期日均是多少」→ 取 **`AVG(consumption_kwh)`**（总电量 ÷ 有数据天数）。

## 三、SQLite 可用函数（生成 SQL 时只能用这些）

- SUM / AVG / MIN / MAX / COUNT / COUNT(DISTINCT ...)
- ROUND(x, n) / ABS(x) / CAST(x AS INTEGER)
- SQRT(x) / POWER(x, y) / EXP(x) / LN(x) / LOG10(x)
- strftime('%Y', date) / datetime(...) / julianday(...)
- CASE WHEN ... THEN ... ELSE ... END / COALESCE(x, 0) / NULLIF(x, 0)
- 注意：**没有 STDDEV_POP / STDDEV_SAMP / VAR_POP**，标准差请写成 SQRT(AVG(x*x) - AVG(x)*AVG(x))
- 注意：**没有 MEDIAN / PERCENTILE**，如需中位数请用 pandas 路径或自行排序取中

## 四、常用问题示例

- 全期总用电量是多少？
- 哪个月用电量最高？
- 峰段用电占比多少？
- 厨房用电占总用电的比例是多少？
- 有哪些用电异常日？
- 周末与工作日的日均用电对比如何？
- 2010 年比 2009 年用电增长多少？
- 未计量用电有多少？

> 生成回答时请：① 明确使用哪张表与哪个时段口径；② 数值带单位；③ 跨周期汇总比率须重算（R6）；④ 首末周期不完整需提示（`is_partial_period`）。