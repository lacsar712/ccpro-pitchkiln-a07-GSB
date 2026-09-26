# PitchKiln-01 · 灶台值守看板

Django 5 + PostgreSQL：灶台瓦片看板 + 右侧抽屉探针时间线，无 Vue/React SPA。

## 技术栈

- Django 5、PostgreSQL
- Session 登录
- HTMX：局部刷新灶台网格与抽屉
- Docker Compose：`web` + `db`

## 端口与数据库

| 服务 | 端口 |
|------|------|
| Web  | **4710** |
| Postgres | **6110**（容器内 5432） |

数据库账号：`pitchkiln` / `pitchkiln` / 库名 `pitchkiln`

## 快速启动

```bash
cd PitchKiln/PitchKiln-01
docker compose up --build -d
```

浏览器打开：http://localhost:4710

演示账号：

- `admin` / `123456`（超级用户）
- `worker` / `123456`（普通用户）

容器启动时会自动：`migrate` → `seed_data` → `collectstatic` → `gunicorn`

## 本地开发（可选）

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
pip install -r requirements.txt
# 确保本机 Postgres 监听 6110，或先 docker compose up -d db
set POSTGRES_HOST=localhost
set POSTGRES_PORT=6110
python manage.py migrate
python manage.py seed_data
python manage.py runserver 0.0.0.0:4710
```

## 业务模型

1. **ResinLot（来脂批）**：`lotCode`、`originPlace`、`arrivalKg`、`receivedAt`
2. **FireHearth（灶台）**：`lane`、`tag`（唯一）、`resinGrade`、相位 `cold|charging|ramping|holding|drawing`
3. **CookRun（熬制值守）**：归属灶台与来脂批、`openedAt`、`closedAt`（可空）、`targetSoftPointC`
4. **SoftPointProbe（软化点探针）**：归属值守、`sampledAt`、`softPointC`、`samplerName`
5. **MaintenanceSeal（检修封条）**：`hearth`、`startedAt`（开始时刻）、`plannedReleaseDate`（计划解除日）、`faultSummary`（故障摘要）、`placedBy`（挂条人）、`releasedAt`（实解时刻，可空）

**业务规则**：将灶台相位切到 `drawing`（出胶）时，进行中的 CookRun 必须至少有一条 SoftPointProbe 的 `softPointC ≤ 95`。逻辑在 `apps/kiln/services/floor_rules.py`，由相位切换入口调用。

## 检修封条

逻辑在 `apps/kiln/services/maintenance.py`，挂条拦截、写操作拦截、解除判定共用同一个「未解除封条」查询（`active_seal_for`）。

- **挂条条件**：仅 `cold`（冷灶）且无未收灶值守的灶台可挂条；同一灶台已有未解除封条时不可再挂（数据库部分唯一约束 `uniq_active_seal_per_hearth` 兜底）。前端只在满足条件时渲染挂条表单，但**后端仍会重新校验**——直接 POST 给开灶中或非冷灶的灶台挂条同样失败。
- **解除**：仅主管（`is_staff`）可解除，解除时写入 `releasedAt`（实解时刻）。
- **拦截范围**：封条未解除期间，该灶的**开灶、改相位、登记探针、收灶**等一切写操作一律拦截（视图层统一拦截 + 服务层 `change_hearth_phase` 复核），抽屉中以中文「在修」说明替代全部写操作表单。来脂批登记不属于单灶写操作，不在拦截范围内。
- **界面**：灶台瓦片标「在修」斜纹与计划解除日；抽屉顶部显示封条横幅（故障摘要 / 开始时刻 / 计划解除日 / 挂条人）；左侧班次条有「修」入口与未解除封条计数角标。

## 界面

- 首页：**灶台值守看板** — 左侧班次条 + 按过道排布的灶台瓦片；点瓦片打开右侧抽屉（值守、探针时间线、改相位 / 登记探针 / 开灶）
- 次页：**来脂批** — 卡片时间线，非宽表 CRUD

## 种子数据

```bash
python manage.py seed_data
```

幂等：已有灶台则只保证账号存在。样例地名仅用「松脂坳 / 桐油坑」系。种子含一台冷灶（`坑火-备灶`）挂未解除的检修封条，用于演示在修拦截。

## 目录结构

```
PitchKiln-01/
  manage.py
  requirements.txt
  Dockerfile
  entrypoint.sh
  docker-compose.yml
  config/
  apps/kiln/          # 模型、视图、floor_rules、种子
  templates/floor/    # 值守看板 + 抽屉
  templates/resin/    # 来脂批时间线
  static/css/         # 值守台 ops-console 样式
```
