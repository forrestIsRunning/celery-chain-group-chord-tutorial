# Celery DAG Demo

使用 [celery-director](https://github.com/ovh/celery-director) 构建的 Celery DAG 演示项目。

## DAG 结构

```
A → (B | C) → D
```

- **TASK_A**: 调用模型服务预处理文本（autoretry + 指数退避）
- **TASK_B**: 调用模型服务生成 embedding（手动 self.retry）
- **TASK_C**: 调用模型服务分类文本（autoretry + jitter 防惊群）
- **TASK_D**: 聚合 B 和 C 的结果（无 retry）

## 技术栈

| 组件 | 作用 |
|------|------|
| Redis | Celery Broker + Result Backend |
| celery-director | DAG 定义 + Web 可视化 |
| Model Service | 模拟 LLM 推理 API (Flask) |
| Docker Compose | 一键启动所有服务 |

## 快速开始

```bash
# 启动所有服务 (Redis + Model Service + Director Web + Worker)
docker-compose up

# 等待 worker 日志显示 "celery@xxx ready" 后触发 workflow
```

## 测试场景

通过 payload 中的 `scenario` 参数控制不同的测试 case：

### Case 1: Happy Path — 全部成功

```bash
curl -X POST http://localhost:8000/api/workflows \
  -H "Content-Type: application/json" \
  -d '{"project":"demo","name":"PIPELINE","payload":{"raw":"hello world","scenario":"happy"}}'
```

所有任务正常完成，无 retry。

### Case 2: Flaky — 模型前 2 次失败，第 3 次成功

```bash
curl -X POST http://localhost:8000/api/workflows \
  -H "Content-Type: application/json" \
  -d '{"project":"demo","name":"PIPELINE","payload":{"raw":"hello world","scenario":"flaky"}}'
```

TASK_A 使用 `autoretry_for` + `retry_backoff=2`，可以观察到：
- 第 1 次尝试 → 失败 → 等 2s
- 第 2 次尝试 → 失败 → 等 4s
- 第 3 次尝试 → 成功

### Case 3: Slow — 模型慢响应触发超时

```bash
curl -X POST http://localhost:8000/api/workflows \
  -H "Content-Type: application/json" \
  -d '{"project":"demo","name":"PIPELINE","payload":{"raw":"hello world","scenario":"slow"}}'
```

模型服务延迟 8 秒，超过 `soft_time_limit=6`，触发 `SoftTimeLimitExceeded` 后自动重试。

### Case 4: Down — 模型完全不可用

```bash
curl -X POST http://localhost:8000/api/workflows \
  -H "Content-Type: application/json" \
  -d '{"project":"demo","name":"PIPELINE","payload":{"raw":"hello world","scenario":"down"}}'
```

TASK_A 重试 3 次后彻底失败，workflow 状态变为 `error`。

### Case 5: Partial Fail — B 失败 C 成功

```bash
curl -X POST http://localhost:8000/api/workflows \
  -H "Content-Type: application/json" \
  -d '{"project":"demo","name":"PIPELINE","payload":{"raw":"hello world","scenario":"partial_fail"}}'
```

A 成功 → B 重试 3 次后失败、C 成功 → group 中有任务失败 → workflow `error`。

## Retry 策略对比

| Task | 策略 | 关键参数 | 说明 |
|------|------|----------|------|
| A | `autoretry_for` | `retry_backoff=2, retry_jitter=False` | 遇异常自动重试，指数退避 2→4→8s |
| B | `self.retry()` | `countdown=3` | 手动控制重试，固定间隔 3s |
| C | `autoretry_for` | `retry_backoff=2, retry_jitter=True` | 自动重试 + 随机抖动防惊群 |
| D | 无 | — | 纯聚合任务 |

## 查看结果

- **Director UI**: http://localhost:8000
- **Model Service**: http://localhost:9000/health
- **Worker 日志**: `docker-compose logs -f worker`
- **查看 workflow 状态**: `curl http://localhost:8000/api/workflows`

## 项目结构

```
celery-dag-demo/
├── docker-compose.yml          # 4 个服务编排
├── .env                        # Director 配置
├── pyproject.toml              # Python 依赖
├── workflows.yml               # DAG 定义: A → (B|C) → D
├── tasks/
│   ├── __init__.py
│   └── pipeline.py             # 4 个 Celery 任务 + retry 策略
├── model_service/
│   └── app.py                  # Mock 模型推理 API
└── static/                     # Director UI 本地静态资源
```
