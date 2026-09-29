# 职业教学任务运营服务

这是一个面向职业院校教务团队、授课教师和课程管理员的 Python 后端服务，用于管理课程任务模板、学员提交、执行队列、教师工作者、结果版本和运营干预。服务保留登录、角色权限、会话、审计和配额等基础能力，所有业务状态与审计事件写入本地 SQLite 数据库，适合在单个应用容器中离线运行。

## 运行环境

- Python 3.11
- SQLite 3（由 Python 标准库提供）
- FastAPI 与 Uvicorn

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

默认数据库位于 `./data/compute-operations.db`，可复制 `.env.example` 并设置 `TOWNSHIP_DATABASE_PATH` 指向其他本地路径。

## 初始化与启动

```bash
python -m app.cli init-db
python -m app.cli check-db
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

健康检查：

```bash
curl -sS http://127.0.0.1:8432/api/system/health
```

课程任务运营接口使用 `/api/compute` 前缀，身份、角色、审计和系统接口分别位于 `/api/auth`、`/api/roles`、`/api/audit` 与 `/api/system`。

新学期临时增开的数控、电商实训班使用 `/api/enrollment` 前缀，提供课程周期、班级配额、学员预留/确认/退课/转班、候补递补与事件追溯能力，规则说明见 `/api/enrollment/meta/rules`。

## 实训报名预留流程

- **课程周期与双层配额**：课程有总量 `total_capacity`，班级有各自 `capacity`；占用分 `reserved`（待确认预留）与 `confirmed`（已确认）两类，数据库 `CHECK` 保证两层均不超额。
- **预留与确认**：`POST /api/enrollment/reservations` 有名额时原子占用课程与班级名额并生成确认期限；满员时按报名顺序进入候补。`POST /api/enrollment/confirmations` 在单条更新语句内把 `reserved` 转为 `confirmed`，课程与班级计数同事务原子扣减。
- **退课、过期与转班**：退课立即释放；过期扫描按 `reserved_expires_at`、报名记录 id 升序释放；转班先释放原班再占用目标班（目标班满则保留原名额并进入目标班候补）。释放后按确定顺序递补：**priority 数值大者优先，相同优先级按候补序号先到先得，再相同按报名记录 id**。
- **重叠时段**：学员在时间重叠的班级间不得同时保有预留/确认名额（首尾相接允许）；候补不阻断报名，递补前再次校验冲突并产生 `promote_skip` 事件。
- **幂等与恢复**：预留、确认、退课、转班均以 `(student_key, idempotency_key)` 记录请求摘要与原始响应，重复请求（含过期确认的拒绝结论）复用原结果；候补名次、占用计数与版本全部落盘，服务重启后不变。
- **可追溯管理接口**：每次释放、递补、扩缩容共享一个 `batch_key`；`GET /api/enrollment/events/batches/{batch_key}` 说明该批次每次递补使用的优先规则、释放来源（`cancel`/`expire`/`transfer`/`capacity_increase`）、触发事件 id 以及课程与班级当时的版本边界。

## 测试与编译检查

```bash
python -m pytest
python -m compileall -q app tests
```

本地冒烟命令：

```bash
python -m app.cli smoke
python -m app.cli compute-demo
```

## 目录结构

```text
app/compute/       任务模板、配额、提交、领取、回执和人工干预
app/enrollment/     实训课程周期、班级配额、预留确认、退课转班与候补递补追溯
app/api/            登录、角色、审计和系统管理接口
app/core/           时钟、安全、异常和分页能力
app/repositories/   SQLite 查询与事务封装
app/services/       身份、审计和后台任务服务
app/database.py     SQLite 连接、事务、表结构和权限初始化
tests/              领域、接口、调度和身份回归测试
tools/              本地维护脚本
```

## 数据一致性

SQLite 连接启用外键、WAL 和忙等待策略。提交、领取、回执和人工干预在即时事务中完成；租约、配额与结果版本使用可注入时钟，便于复现跨日和恢复边界。会话令牌只保存摘要，审计与人工干预记录不会写入明文密码或令牌。
