-- ============================================================================
-- 智电析报 · 报表数据库 DDL（唯一事实源之一）
-- 文件：01.code/scripts/sql/reports.sql
-- 目标库：02.data/reports.db
-- 口径版本：caliber v1.0（对应 PRD v1.3.0 第 8 章「数据口径与报表规范」）
--
-- 重要约束（见 PRD 7.4 / 6.3 S8）：
--   1) 本库【只放报表数据】；应用运行期一律以只读方式打开（file:...?mode=ro）
--   2) 仅两处可写：① 数据清洗脚本（离线）② 上传入库服务（受控代码，独占写连接）
--   3) LLM 生成的 SQL 只允许 SELECT，且表名必须命中当前会话白名单
--   4) 时间统一 TEXT ISO8601：'YYYY-MM-DD HH:MM:SS'
--   5) 单位约定：*_kwh = 电量(kWh，可 SUM)；*_kw = 功率(kW，不可 SUM)；
--      *_ratio / coverage / load_factor = 比率(不可 SUM)
-- ============================================================================

PRAGMA foreign_keys = ON;

-- ----------------------------------------------------------------------------
-- 1. report_minute —— 分钟级明细（207 万行；仅入库，不导出 CSV）
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS report_minute (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    dt                TEXT    NOT NULL,              -- 时间戳 'YYYY-MM-DD HH:MM:SS'（唯一）
    year              INTEGER NOT NULL,
    month             INTEGER NOT NULL,              -- 1-12
    day               INTEGER NOT NULL,              -- 1-31
    hour              INTEGER NOT NULL,              -- 0-23
    weekday           INTEGER NOT NULL,              -- 0=周一 … 6=周日
    is_weekend        INTEGER NOT NULL DEFAULT 0,    -- 1=周六/周日
    period            TEXT    NOT NULL,              -- peak 峰 / flat 平 / valley 谷
    gap_kw            REAL,                          -- 总有功功率(kW)
    gap_kwh           REAL,                          -- 分钟电量 = gap_kw / 60
    sm1_kwh           REAL,                          -- 厨房（原始 Wh / 1000）
    sm2_kwh           REAL,                          -- 洗衣房
    sm3_kwh           REAL,                          -- 热水器与空调
    unmetered_kwh     REAL,                          -- 未计量 = gap_kw/60 − sm1 − sm2 − sm3
    voltage           REAL,                          -- 电压(V)
    intensity         REAL,                          -- 电流(A)
    reactive_power_kw REAL,                          -- 无功功率(kW)
    is_missing        INTEGER NOT NULL DEFAULT 0,    -- 1=原始行缺失（数值列全为 NULL）
    CHECK (period IN ('peak', 'flat', 'valley')),
    CHECK (is_weekend IN (0, 1)),
    CHECK (is_missing IN (0, 1)),
    CHECK (hour BETWEEN 0 AND 23),
    CHECK (weekday BETWEEN 0 AND 6)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_report_minute_dt     ON report_minute (dt);
CREATE INDEX        IF NOT EXISTS ix_report_minute_ym     ON report_minute (year, month);
CREATE INDEX        IF NOT EXISTS ix_report_minute_ymd    ON report_minute (year, month, day);
CREATE INDEX        IF NOT EXISTS ix_report_minute_period ON report_minute (period);
CREATE INDEX        IF NOT EXISTS ix_report_minute_hour   ON report_minute (hour);
CREATE INDEX        IF NOT EXISTS ix_report_minute_we     ON report_minute (is_weekend);

-- ----------------------------------------------------------------------------
-- 2. report_hourly —— 小时报（约 3.46 万行）
--    expected_minutes 恒为 60（自然时间长度）；coverage = active_minutes / 60
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS report_hourly (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    dt_hour          TEXT    NOT NULL,             -- 'YYYY-MM-DD HH:00:00'（唯一）
    year             INTEGER NOT NULL,
    month            INTEGER NOT NULL,
    day              INTEGER NOT NULL,
    hour             INTEGER NOT NULL,
    weekday          INTEGER NOT NULL,
    is_weekend       INTEGER NOT NULL DEFAULT 0,
    period           TEXT    NOT NULL,             -- 该小时所属时段（一小时完整落在单一时段内）
    consumption_kwh  REAL,                         -- 小时总电量
    sm1_kwh          REAL,
    sm2_kwh          REAL,
    sm3_kwh          REAL,
    unmetered_kwh    REAL,
    peak_kwh         REAL,                         -- 时段电量（按时段正交拆分之一）
    flat_kwh         REAL,
    valley_kwh       REAL,
    active_minutes   INTEGER NOT NULL DEFAULT 0,   -- 有效分钟数
    expected_minutes INTEGER NOT NULL DEFAULT 60,  -- 自然时间长度（恒 60）
    coverage         REAL,                         -- 比率，不可 SUM
    avg_power_kw     REAL,                         -- 功率，不可 SUM
    max_power_kw     REAL,
    min_power_kw     REAL,
    avg_voltage      REAL,
    avg_intensity    REAL,
    CHECK (period IN ('peak', 'flat', 'valley')),
    CHECK (is_weekend IN (0, 1)),
    CHECK (hour BETWEEN 0 AND 23)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_report_hourly_dt     ON report_hourly (dt_hour);
CREATE INDEX        IF NOT EXISTS ix_report_hourly_ym     ON report_hourly (year, month);
CREATE INDEX        IF NOT EXISTS ix_report_hourly_ymd    ON report_hourly (year, month, day);
CREATE INDEX        IF NOT EXISTS ix_report_hourly_period ON report_hourly (period);
CREATE INDEX        IF NOT EXISTS ix_report_hourly_hour   ON report_hourly (hour);
CREATE INDEX        IF NOT EXISTS ix_report_hourly_we     ON report_hourly (is_weekend);

-- ----------------------------------------------------------------------------
-- 3. report_daily —— 日报（约 1442 行）
--    expected_minutes 恒为 1440；daily_avg_kwh = consumption_kwh * 1440 / active_minutes
--    active_minutes = 0 时 daily_avg_kwh 为 NULL（见 PRD 8.6 边界规则）
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS report_daily (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    date              TEXT    NOT NULL,            -- 'YYYY-MM-DD'（唯一）
    year              INTEGER NOT NULL,
    month             INTEGER NOT NULL,
    day               INTEGER NOT NULL,
    weekday           INTEGER NOT NULL,
    is_weekend        INTEGER NOT NULL DEFAULT 0,
    consumption_kwh   REAL,
    sm1_kwh           REAL,
    sm2_kwh           REAL,
    sm3_kwh           REAL,
    unmetered_kwh     REAL,
    peak_kwh          REAL,
    flat_kwh          REAL,
    valley_kwh        REAL,
    active_minutes    INTEGER NOT NULL DEFAULT 0,
    expected_minutes  INTEGER NOT NULL DEFAULT 1440,
    coverage          REAL,                        -- 比率，不可 SUM
    avg_power_kw      REAL,
    max_power_kw      REAL,
    min_power_kw      REAL,
    avg_voltage       REAL,
    avg_intensity     REAL,
    daily_avg_kwh     REAL,                        -- 按有效数据折算；active_minutes=0 时 NULL
    load_factor       REAL,                        -- 比率，不可 SUM
    sm1_ratio         REAL,
    sm2_ratio         REAL,
    sm3_ratio         REAL,
    unmetered_ratio   REAL,
    peak_ratio        REAL,
    flat_ratio        REAL,
    valley_ratio      REAL,
    peak_hour         INTEGER,                     -- 当日峰值整点 0-23（18 表示 18:00–18:59）
    is_anomaly        INTEGER NOT NULL DEFAULT 0,
    anomaly_reason    TEXT,                        -- 命中规则名，多规则以 | 分隔
    is_partial_period INTEGER NOT NULL DEFAULT 0,  -- 1=不完整自然日（首/末日）
    partial_reason    TEXT,
    CHECK (is_weekend IN (0, 1)),
    CHECK (is_anomaly IN (0, 1)),
    CHECK (is_partial_period IN (0, 1)),
    CHECK (peak_hour IS NULL OR peak_hour BETWEEN 0 AND 23)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_report_daily_date    ON report_daily (date);
CREATE INDEX        IF NOT EXISTS ix_report_daily_ym      ON report_daily (year, month);
CREATE INDEX        IF NOT EXISTS ix_report_daily_we      ON report_daily (is_weekend);
CREATE INDEX        IF NOT EXISTS ix_report_daily_anom    ON report_daily (is_anomaly);
CREATE INDEX        IF NOT EXISTS ix_report_daily_partial ON report_daily (is_partial_period);

-- ----------------------------------------------------------------------------
-- 4. report_monthly —— 月报（48 行）
--    expected_minutes = 当月天数 * 1440；mom_rate 环比 / yoy_rate 同比（比率，不可 SUM）
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS report_monthly (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    year_month        TEXT    NOT NULL,            -- 'YYYY-MM'（唯一）
    year              INTEGER NOT NULL,
    month             INTEGER NOT NULL,
    days_in_month     INTEGER NOT NULL,
    consumption_kwh   REAL,
    sm1_kwh           REAL,
    sm2_kwh           REAL,
    sm3_kwh           REAL,
    unmetered_kwh     REAL,
    peak_kwh          REAL,
    flat_kwh          REAL,
    valley_kwh        REAL,
    active_minutes    INTEGER NOT NULL DEFAULT 0,
    expected_minutes  INTEGER NOT NULL DEFAULT 0,  -- 当月天数 * 1440
    coverage          REAL,
    avg_power_kw      REAL,
    max_power_kw      REAL,
    min_power_kw      REAL,
    daily_avg_kwh     REAL,
    mom_rate          REAL,                        -- 环比（比率，不可 SUM）
    yoy_rate          REAL,                        -- 同比（比率，不可 SUM）
    is_anomaly        INTEGER NOT NULL DEFAULT 0,
    anomaly_reason    TEXT,
    is_partial_period INTEGER NOT NULL DEFAULT 0,  -- 1=不完整自然月（首/末月）
    partial_reason    TEXT,
    CHECK (is_anomaly IN (0, 1)),
    CHECK (is_partial_period IN (0, 1)),
    CHECK (month BETWEEN 1 AND 12)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_report_monthly_ym      ON report_monthly (year_month);
CREATE INDEX        IF NOT EXISTS ix_report_monthly_year    ON report_monthly (year);
CREATE INDEX        IF NOT EXISTS ix_report_monthly_anom    ON report_monthly (is_anomaly);
CREATE INDEX        IF NOT EXISTS ix_report_monthly_partial ON report_monthly (is_partial_period);

-- ----------------------------------------------------------------------------
-- 5. report_energy_structure —— 用电结构报表（6 行：2006–2010 五年 + 全期）
--    两套正交拆分：按用途（4 项）/ 按时段（3 项），各自求和 = total_kwh，禁止混加
--    period='全期' 行为各年份行合计（is_total_row=1），求和时不可与年份行一起 SUM
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS report_energy_structure (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    period                  TEXT    NOT NULL,      -- '2006'…'2010' / '全期'（唯一）
    is_total_row            INTEGER NOT NULL DEFAULT 0,  -- 1=全期合计行
    year                    INTEGER,               -- 全期行为 NULL
    -- 按用途拆分（4 项之和 = total_kwh）
    kitchen_kwh             REAL,
    laundry_kwh             REAL,
    water_heating_ac_kwh    REAL,
    other_unmetered_kwh     REAL,
    kitchen_ratio           REAL,
    laundry_ratio           REAL,
    water_heating_ac_ratio  REAL,
    other_unmetered_ratio   REAL,
    -- 按时段拆分（3 项之和 = total_kwh）
    peak_kwh                REAL,
    flat_kwh                REAL,
    valley_kwh              REAL,
    peak_ratio              REAL,
    flat_ratio              REAL,
    valley_ratio            REAL,
    -- 合计与闭合校验
    total_kwh               REAL,                  -- 周期总电量
    structure_closed        REAL,                  -- 用途四项之和 − total_kwh（应≈0）
    time_closed             REAL,                  -- 时段三项之和 − total_kwh（应≈0）
    CHECK (is_total_row IN (0, 1))
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_report_structure_period ON report_energy_structure (period);

-- ----------------------------------------------------------------------------
-- 6. 用户上传的动态表（不在此文件内静态定义）
--    命名规则见 PRD F1.3：文件名 → 小写 → 非字母数字转下划线 → 冲突追加序号
--    动态表由「上传入库服务」在独占写连接中创建；创建后立即转为只读使用
--    DDL 由服务按上传文件的列结构动态生成（类型经类型推断 + 时间列识别）
-- ----------------------------------------------------------------------------
