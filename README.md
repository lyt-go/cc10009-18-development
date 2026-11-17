# 分布式网页抓取调度平台

面向分布式网页抓取任务调度、节点协同、请求分配、反爬策略编排、数据校验入库与抓取质量大盘的采集工程管理平台。

这是一个前后端分离的管理平台：前端 React + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 React + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 采集节点 | `worker` | 工作节点 | 节点编号、主机地址、并发槽位数 |
| 请求分配 | `dispatch` | 分配策略 | 策略编号、目标域名、分配算法 |
| 抓取作业 | `crawljob` | 作业定义 | 作业编号、目标站点、起始URL |
| URL过滤 | `urlfilter` | 过滤规则 | 规则编号、包含模式、排除模式 |
| DNS解析 | `dnsresolver` | 解析配置 | 配置编号、域名模式、DNS服务器 |
| 内容提取 | `contentextract` | 提取器 | 提取器编号、内容类型、正文选择器 |
| 存储策略 | `storagestrategy` | 存储方案 | 方案编号、存储类型、连接参数 |
| 渲染任务 | `rendertask` | 渲染作业 | 作业编号、目标URL、浏览器版本 |
| 渲染结果 | `renderresult` | 渲染结果 | 结果编号、等待条件、页面版本、结果版本、诊断信息 |
| 渲染队列 | `renderqueue` | 队列位置 | 队列编号、作业编号、容量占用、等待策略版本 |
| 调度落点 | `renderlanding` | 落点清单 | 落点编号、结果快照、容量回写、已刷新业务记录 |
| 采集质量 | `qualityreport` | 质量报告 | 报告编号、作业编号、总URL数 |
| 带宽控制 | `bandwidth` | 带宽策略 | 策略编号、节点标识、下载带宽上限 |
| 重试队列 | `retryqueue` | 重试条目 | 条目编号、失败URL、失败原因码 |
| 翻页管理 | `pagemarker` | 翻页规则 | 规则编号、目标站点、翻页类型 |
| 增量标记 | `incrementmark` | 增量水位 | 水位编号、目标源、增量字段名 |
| 配置同步 | `configsync` | 同步任务 | 任务编号、源节点、目标节点 |
| 采集API | `scrapeapi` | API定义 | API编号、API名称、请求路径 |
| 数据抽样 | `datasample` | 抽样方案 | 方案编号、数据源、抽样方式 |
| 采集告警 | `alertpolicy` | 告警策略 | 策略编号、监控指标、触发条件 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 渲染结果链路

渲染任务页在「渲染作业」之外还有三个数据集，复核人员可直接切换查看：

| 数据集 | 接口前缀 | 说明 |
| --- | --- | --- |
| 渲染结果 | `/api/renderresult` | 等待条件、版本线、诊断信息与重试边界 |
| 队列状态 | `/api/renderqueue` | 一个作业一个队列位置，容量占用与回收原因 |
| 调度落点 | `/api/renderlanding` | 落点侧读取队列状态、回写容量、接收结果快照 |

关键规则：

- 同一渲染作业重复下发只占一个队列位置；队列仍在但容量被回收时只重新占位，不新增行。
- 等待条件全部满足且容量已确认才生成成功页面；超时、用户终止、连接中断、页面异常
  都保留已满足/未满足条件与可重试诊断边界，不写成成功页面。
- 同名结果冲突后保留新旧两个版本；超时释放槽位后，晚到结果不能覆盖较新的成功结果。
- 同一份结果按处理键幂等处理，并发处理由服务内锁串行，结果版本不分叉。
- 节点离线时队列容量立即回收；重新排队沿用原等待策略及其版本。
- 结果快照与处理结果同步到调度落点清单，并刷新关联的渲染作业与采集质量记录。

回归测试：`cd backend && .venv/bin/python -m pytest tests/ -q`。

领域：数据工程
子方向：数据采集、爬虫与RPA
