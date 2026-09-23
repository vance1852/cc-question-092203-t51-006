# 换电站运营管理平台（纯后端）

新能源物流车换电站后台管理的纯后端 API 服务，提供站点、车辆和换电记录的统一管理能力。

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

## 内置账号

首次启动自动创建唯一管理员（本平台只有 admin 一个角色）：

- 用户名：`admin`
- 密码：`admin123`

## 已实现的基础功能

- 登录签发 JWT、获取当前用户（`/api/auth/login`、`/api/auth/me`）
- 换电站增删改查与实时可预约量（`/api/stations`、`/api/stations/{id}/capacity`）
- 车辆增删改查（`/api/vehicles`）
- 换电预约：创建占位、续期、取消、过期释放、按状态/时间查询（`/api/reservations`）
- 换电记录查询与凭预约履约（`/api/swaps`，原子核销预约、扣减库存、更新车辆电量）
- 仪表盘统计（`/api/dashboard/stats`）
- 健康检查（`/api/health`）

除 `login` 与 `health` 外，所有接口均需携带 `Authorization: Bearer <token>`。

## 预约与容量模型

- 站点字段：`battery_ready`（满电电池）与 `reserved_count`（已被有效预约锁定数）；
  **可预约量 `available = battery_ready - reserved_count`**，运营页面、容量接口与下单/换电使用同一口径。
- 预约状态流转：`pending → fulfilled / cancelled / expired`。
  - 创建：在 `BEGIN IMMEDIATE` 事务内用条件 UPDATE 锁定一份容量
    （`WHERE battery_ready - reserved_count > 0`），并发争抢最后名额时结果确定。
  - 同一车辆有效预约的时间窗 `[reserved_for, expires_at]` 不得重叠。
  - `idempotency_key`：调用方重试携带相同键返回同一预约，不重复占位。
  - 取消/到期：原子把锁定容量还回站点；到期在写操作前、查询前及服务启动时统一扫描，**重启后可继续识别**待履约与已过期预约。
  - 续期：仅 `pending` 可续，延长到期时间，锁定容量不变，仍校验时间窗重叠。
  - 履约：`POST /api/swaps` 只能消费“属于该车辆/站点且仍 `pending`、未过期”的预约；
    预约核销、库存扣减、车辆电量更新、换电记录写入在**同一事务**内原子完成，重复消费只有一次成功。

### 主要接口

- `POST /api/reservations`：`{vehicle_id, station_id, reserved_for?, expires_at?, idempotency_key?}`
- `POST /api/reservations/{id}/renew`：`{extend_minutes?}` 或 `{expires_at?}`
- `POST /api/reservations/{id}/cancel`
- `GET /api/reservations?status=&vehicle_id=&station_id=&time_from=&time_to=`
- `POST /api/swaps`：`{reservation_id, soc_before, soc_after}`
- `GET /api/stations/{id}/capacity`：`{battery_ready, reserved_count, available}`

## 测试

```bash
pip install -r requirements.txt
pytest -q
```

测试使用可固定/推进的业务时钟（`app/clock.py`），可在确定时间下验证创建、续期、取消、过期、履约与多线程并发争抢的完整状态流。

## 编码说明

源码与数据均为 UTF-8；FastAPI 响应为 UTF-8 JSON，中文不转义、不乱码。
Windows 控制台若为 GBK，仅影响终端打印观感，不影响接口返回。
