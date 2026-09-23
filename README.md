# 换电站运营管理平台（纯后端）

新能源物流车换电站后台管理的纯后端 API 服务，提供站点、车辆、**持久化换电预约**与换电记录的统一管理能力。

## 技术栈

- FastAPI + Uvicorn
- SQLAlchemy + SQLite（本地文件，开箱即用）
- PyJWT（JWT 鉴权）
- 密码哈希用标准库 `hashlib.pbkdf2_hmac`，无额外依赖

所有数据本地、离线可运行，不依赖任何外部服务。

## 运行

```bash
pip install -r requirements.txt
python run.py
```

服务启动在 `http://127.0.0.1:7634`，首次启动自动建表并灌入种子数据。
交互式文档：`http://127.0.0.1:7634/docs`。

数据库可用环境变量 `APP_DATABASE_URL` 覆盖（默认项目根目录 `data.db`）。

## 内置账号

首次启动自动创建唯一管理员（本平台只有 admin 一个角色）：

- 用户名：`admin`
- 密码：`admin123`

## 已实现的基础功能

- 登录签发 JWT、获取当前用户（`/api/auth/login`、`/api/auth/me`）
- 换电站增删改查与可预约量查询（`/api/stations`、`/api/stations/{id}/capacity`）
- 车辆增删改查（`/api/vehicles`）
- 换电预约：创建占位、续期、取消、过期释放、按状态/时间查询（`/api/reservations`）
- 换电登记（`/api/swaps`，**必须凭匹配且有效的预约**，原子联动预约状态、车辆电量与站点库存）
- 仪表盘统计（`/api/dashboard/stats`，含锁定量与有效预约数）
- 健康检查（`/api/health`）

除 `login` 与 `health` 外，所有接口均需携带 `Authorization: Bearer <token>`。

## 预约模型与一致性保证

- **容量锁定**：`stations.battery_held` 记录被有效预约锁定的数量；可预约量唯一口径为
  `battery_ready - battery_held`，运营查询与换电接口共用，杜绝人工超卖。
- **状态机**：`held`（待履约）→ `fulfilled`（已履约）/ `cancelled`（已取消）/ `expired`（已过期）。
  取消与过期都会归还容量；履约时锁定容量转为实际扣减（ready、held 各减一）。
- **不重叠**：同一车辆同时只能有一份 `held` 预约；爽约到期或取消后才能再次预约。
- **调用方幂等**：创建时可带 `idempotency_key`，相同键的并发/串行重试返回同一份预约，不重复占位。
- **原子换电**：预约校验、状态流转、库存扣减、换电记录（一对一外键关联预约）、车辆电量更新
  在同一事务提交。
- **并发确定**：SQLite 写事务统一以 `BEGIN IMMEDIATE` 开启并配合条件 UPDATE，
  多个请求争夺最后一份名额时恰好一个成功，其余确定失败。
- **过期安全释放**：启动扫描 + 每次业务操作惰性过期 + 后台守护线程（30 秒一轮）三重保障；
  服务重启后仍能识别待履约与停机期间到期的预约。
- **固定时钟**：业务时间取自 `app.clock.clock`，测试可冻结/推进，可确定性验证
  创建、续期、取消、过期、履约与并发争用完整状态流。

## 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/reservations` | 创建预约并锁定一份容量（可带 `idempotency_key`、`ttl_minutes`） |
| GET | `/api/reservations` | 列表查询，支持 `status`(held/fulfilled/cancelled/expired/active)、`vehicle_id`、`station_id`、`created_from`、`created_to` |
| GET | `/api/reservations/{id}` | 预约详情 |
| POST | `/api/reservations/{id}/renew` | 续期（body：`ttl_minutes`） |
| POST | `/api/reservations/{id}/cancel` | 取消并释放容量 |
| POST | `/api/reservations/expire-sweep` | 手动触发过期释放，返回 `{released: n}` |
| GET | `/api/stations/{id}/capacity` | 查询可预约量（ready/held/available_capacity） |
| POST | `/api/swaps` | 凭预约码换电（body 含 `reservation_code`） |

## 测试

```bash
pip install -r requirements.txt
pytest -q
```

## 编码说明

源码与数据均为 UTF-8；FastAPI 响应为 UTF-8 JSON，中文不转义、不乱码。
Windows 控制台若为 GBK，仅影响终端打印观感，不影响接口返回。
