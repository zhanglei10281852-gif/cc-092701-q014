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

课程任务运营接口使用 `/api/compute` 前缀，实训班级报名预留与候补递补接口使用 `/api/enroll` 前缀，身份、角色、审计和系统接口分别位于 `/api/auth`、`/api/roles`、`/api/audit` 与 `/api/system`。

## 报名预留流程（/api/enroll）

- 课程维护总量（total_capacity），班级维护配额（quota）与课程周期（starts_at/ends_at），两层都按待确认预留与已确认占用记账。
- 报名先形成带确认截止时间的待确认预留；确认在同一事务内原子扣减班级配额与课程总量；名额不足时进入候补并记录名次。
- 退课、过期未确认（`/api/enroll/maintenance/expire-pending` 或确认时惰性触发）与转班都按“先释放、再递补”的固定顺序处理，候补按名次升序递补，跳过与学员已占用时段冲突的候补。
- 同一学员在重叠时段只能持有一个有效占用；报名与转班按幂等键复用原结果；候补名次与占用全部落库，服务重启后不变。
- 每次递补写入优先规则、释放来源和当时的版本边界（班级/课程版本与配额快照），可通过 `/api/enroll/promotions/{id}` 或 `/api/enroll/classes/{id}/promotions` 查询。

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
app/enroll/        课程总量、班级配额、报名预留、候补递补和转班
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
