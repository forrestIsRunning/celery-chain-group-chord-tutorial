# Celery Chain, Group & Chord Tutorial

A hands-on tutorial for Celery's three core orchestration primitives — **chain**, **group**, and **chord** — demonstrated through a real `A → (B | C) → D` DAG pipeline with retry strategies, a mock model service, and [celery-director](https://github.com/ovh/celery-director) visualization.

[Chinese version (中文版)](./README_CN.md)

## What You'll Learn

- How **chain** links tasks sequentially via callbacks embedded in messages
- How **group** dispatches tasks in parallel to different worker processes
- How **chord** (group + callback) uses a Redis atomic counter as a barrier
- Why Celery workers are **stateless** and all state lives in Redis
- Four different **retry strategies**: autoretry with backoff, manual retry, jitter, and no-retry

## The DAG

```
TASK_A ──► TASK_B ──┐
    │               ├──► TASK_D (merge)
    └────► TASK_C ──┘
```

| Task | Role | Retry Strategy |
|------|------|----------------|
| A | Call model service to preprocess text | `autoretry_for` + exponential backoff |
| B | Call model service to generate embeddings | Manual `self.retry()` with fixed delay |
| C | Call model service to classify text | `autoretry_for` + jitter (thundering herd prevention) |
| D | Aggregate results from B and C | None |

## Quick Start

```bash
git clone https://github.com/forrestIsRunning/celery-chain-group-chord-tutorial.git
cd celery-chain-group-chord-tutorial

# Start all services (Redis + Model Service + Director Web + Worker)
docker-compose up

# Wait for "celery@xxx ready" in worker logs, then trigger a workflow:
curl -X POST http://localhost:8000/api/workflows \
  -H "Content-Type: application/json" \
  -d '{"project":"demo","name":"PIPELINE","payload":{"raw":"hello world","scenario":"happy"}}'
```

**Director UI**: http://localhost:8000 | **Model Service**: http://localhost:9000/health

---

## Architecture

```
┌──────────┐     ┌──────────────┐     ┌──────────────┐     ┌──────────┐
│  Director │────►│ Redis db 0   │◄───►│  Worker(s)   │────►│  Model   │
│  Web UI   │     │ (Broker)     │     │  (Celery)    │     │  Service │
│  :8000    │     │              │     │              │     │  :9000   │
└──────────┘     │ Redis db 1   │     └──────────────┘     └──────────┘
                  │ (Backend)    │       stateless           Flask mock
                  └──────────────┘       scale horizontally  LLM API
                    source of truth
```

---

## Core Concept: What Is a Broker?

Think of Celery like a delivery platform:

| Analogy | Celery Component | Role |
|---------|-----------------|------|
| Customer places order | `apply_async()` | Submit a task to the queue |
| **Dispatch center** | **Broker (Redis db 0)** | **Receive, store, and distribute task messages** |
| Delivery driver | Worker | Pick up tasks and execute them |
| Delivery receipt | Result Backend (Redis db 1) | Store task results |

The broker is just a **message queue**. It doesn't execute any logic — it only stores messages and distributes them in order. All orchestration logic (sequencing, parallelism) is carried within the **`callbacks` field inside each message body**.

```
Producer                       Broker (Redis)                    Consumer (Worker)
                         ┌─────────────────────┐
  apply_async() ───────► │  celery queue         │ ──────────►  Worker LPOP picks task
                         │  [msg1, msg2, msg3]  │              executes, stores result
                         └─────────────────────┘

  Message format:
  {
    "task": "TASK_A",
    "id": "uuid-xxx",
    "args": [...],
    "kwargs": {"payload": {...}},
    "callbacks": [full signature of TASK_B]    ← key: next step is embedded here
  }
```

### Redis Plays Two Roles

| Redis DB | Role | What It Stores |
|----------|------|----------------|
| db 0 | Broker (message queue) | Pending task messages |
| db 1 | Result Backend | Task results + chord counters |

In production, the broker is typically RabbitMQ (stronger delivery guarantees) while the backend stays Redis (fast reads/writes).

---

## How the DAG Executes: Step by Step

### Phase 0 — Canvas Construction

When you `POST /api/workflows`, Director's `builder.py` reads `workflows.yml` and builds a Celery **Canvas**:

```python
canvas = chain(
    start.si(wf_id),              # mark workflow as started
    TASK_A.s(kwargs={payload}),   # your task A
    group(                        # B and C in parallel
        TASK_B.s(kwargs={payload}),  # ← group inside chain auto-converts to chord
        TASK_C.s(kwargs={payload}),
    ),
    TASK_D.s(kwargs={payload}),   # chord callback
    end.si(wf_id),                # mark workflow as completed
)

canvas.apply_async()
# ^ only sends the FIRST task (start) to Redis broker — not all of them
```

The three primitives:
- **`chain(A, B, C)`** — Sequential: A finishes → triggers B → triggers C
- **`group(B, C)`** — Parallel: B and C are sent to the queue simultaneously
- **`chord(group, callback)`** — Barrier: wait for all group members to finish, then trigger callback

### Phase 1 — chain: A Completes, Triggers Next Task

```
  Redis Broker (db 0)              Worker Process
  ┌───────────────────┐
  │ celery queue:      │
  │   [start]          │ ─────►  Worker picks start, executes it
  └───────────────────┘         returns None
                                  │
                                  ▼  Worker inspects the callbacks field in start's message
                                     callbacks = [TASK_A's full signature]
                                  │
                                  ▼  Worker builds TASK_A message, sends to broker
  ┌───────────────────┐
  │ celery queue:      │
  │   [TASK_A]         │ ─────►  Worker picks TASK_A(args=(None,), kwargs={payload})
  └───────────────────┘         Calls model service, returns {processed, length}
                                  │
                                  ▼  Worker writes result to Redis Backend (db 1)
                                     key: celery-task-meta-<task_a_uuid>
                                     val: {"status":"SUCCESS","result":{"processed":"HELLO WORLD",...}}
```

**There is no "event notification" system.** The worker that finished A reads the callbacks from A's own message body, constructs the next message, and pushes it to the broker. The baton is passed via messages, not events.

### Phase 2 — group: Parallel Dispatch + Chord Counter

```
                                  Worker finishes TASK_A, inspects callbacks
                                  callbacks = chord(group(B, C), callback=TASK_D)
                                  │
                                  ▼  Worker does three things:
                                     ① Sends TASK_B to broker (with A's result as args[0])
                                     ② Sends TASK_C to broker (with A's result as args[0])
                                     ③ Sets chord counter in Redis (db 1):
                                        key: chord-unlock-<group_id>
                                        val: 2  (waiting for 2 tasks)

  ┌───────────────────┐
  │ celery queue:      │
  │   [TASK_B, TASK_C] │ ─────►  Worker-8 picks TASK_B  (parallel!)
  └───────────────────┘ ─────►  Worker-9 picks TASK_C  (parallel!)
```

B and C are pushed into the queue simultaneously. Any idle worker process can pick either one. This is what "parallel" means — two independent messages consumed by different processes.

### Phase 3 — chord: Atomic Counter as a Barrier

This is the most critical part — how does Celery know both B and C are done?

```
  Worker-8 finishes TASK_B:
    ① Stores result:   celery-task-meta-<task_b_uuid>
    ② Atomic Redis op: DECR chord-unlock-<group_id>  →  returns 1
    ③ 1 ≠ 0 → not all done yet, stop here

  Worker-9 finishes TASK_C:
    ① Stores result:   celery-task-meta-<task_c_uuid>
    ② Atomic Redis op: DECR chord-unlock-<group_id>  →  returns 0
    ③ 0 == 0 → all done! Trigger chord callback:
       - Read B and C results from Redis
       - Assemble list: [B_result, C_result]
       - Send TASK_D to broker with args=([B_result, C_result],)

  ┌───────────────────┐
  │ celery queue:      │
  │   [TASK_D]         │ ─────►  Worker executes TASK_D, merges B + C results
  └───────────────────┘
```

`DECR` is a Redis atomic decrement. Even if B and C finish simultaneously on different machines, Redis guarantees only one reaches 0. No double-trigger, no missed trigger.

### Phase 4 — Completion

```
  TASK_D finishes → callback = end.si(wf_id) → marks workflow as SUCCESS
```

### Actual Timeline (From Logs)

```
12:08:02.210  [A] attempt #1 → call model → success
12:08:02.230  [B] attempt #1 → call model → success     ┐ parallel (Worker-8)
12:08:02.249  [C] attempt #1 → call model → success     ┘ parallel (Worker-9)
12:08:02.261  [D] received [B_result, C_result] → merged → done
```

---

## Are Celery Workers Stateless?

**Yes. Workers are stateless. All state lives in Redis.**

```
  ┌──────────┐       ┌──────────────────────────────────┐
  │ Worker 1  │◄────►│  Redis                            │
  │ Worker 2  │◄────►│    db 0: Broker (pending msgs)    │
  │ Worker N  │◄────►│    db 1: Backend (results+chords) │
  └──────────┘       └──────────────────────────────────┘
    stateless           stateful (single source of truth)
```

This means:
- Workers can be killed/restarted at any time without breaking the DAG
- You can scale workers horizontally (more machines) as long as they share the same Redis
- A worker doesn't remember what it has executed or know what the full DAG looks like
- Each worker does one thing: pick message → execute → store result → check callbacks → send next message

The "shape" of the DAG is never stored in any worker's memory — it's carried forward through **callback chains embedded in messages** at runtime.

---

## Test Scenarios

Control different failure modes via the `scenario` field in the payload:

### Case 1: Happy Path

```bash
curl -X POST http://localhost:8000/api/workflows \
  -H "Content-Type: application/json" \
  -d '{"project":"demo","name":"PIPELINE","payload":{"raw":"hello world","scenario":"happy"}}'
```

All tasks complete on the first attempt. No retries.

### Case 2: Flaky — Fail Twice, Succeed on Third

```bash
curl -X POST http://localhost:8000/api/workflows \
  -H "Content-Type: application/json" \
  -d '{"project":"demo","name":"PIPELINE","payload":{"raw":"hello world","scenario":"flaky"}}'
```

TASK_A hits a 500 error twice, then succeeds. Observe exponential backoff:
- Attempt 1 → fail → wait **2s**
- Attempt 2 → fail → wait **4s**
- Attempt 3 → success

### Case 3: Slow — Model Timeout

```bash
curl -X POST http://localhost:8000/api/workflows \
  -H "Content-Type: application/json" \
  -d '{"project":"demo","name":"PIPELINE","payload":{"raw":"hello world","scenario":"slow"}}'
```

Model service delays 8s, exceeding the 5s request timeout. Triggers `ReadTimeout` with retries at 2s → 4s → 8s intervals.

### Case 4: Down — Model Permanently Unavailable

```bash
curl -X POST http://localhost:8000/api/workflows \
  -H "Content-Type: application/json" \
  -d '{"project":"demo","name":"PIPELINE","payload":{"raw":"hello world","scenario":"down"}}'
```

TASK_A exhausts all 3 retries. Workflow status becomes `error`. B/C/D remain `pending`.

### Case 5: Partial Failure — B Fails, C Succeeds

```bash
curl -X POST http://localhost:8000/api/workflows \
  -H "Content-Type: application/json" \
  -d '{"project":"demo","name":"PIPELINE","payload":{"raw":"hello world","scenario":"partial_fail"}}'
```

A succeeds → B retries 3x and fails, C succeeds → chord dependency failed → `ChordError` → TASK_D never runs → workflow `error`.

---

## Inspecting Task Status & Errors

Every task's status and result (including error details) can be queried after execution. There are three ways to do this:

### Method 1: Director REST API (Recommended)

The Director API returns the full workflow with every task's status, result, and error traceback:

```bash
# Get workflow detail by ID
curl -s http://localhost:8000/api/workflows/<workflow_id> | python3 -m json.tool
```

**Success task** returns its result data:
```json
{
  "key": "TASK_A",
  "status": "success",
  "result": {
    "processed": "HELLO WORLD",
    "length": 11,
    "model": "text-preprocessor-v1",
    "retries_used": 0
  }
}
```

**Failed task** returns the exception and full traceback:
```json
{
  "key": "TASK_B",
  "status": "error",
  "result": {
    "exception": "500 Server Error: INTERNAL SERVER ERROR for url: http://model-service:9000/v1/predict",
    "traceback": "Traceback (most recent call last):\n  File \".../celery/app/trace.py\", ...\nrequests.exceptions.HTTPError: 500 Server Error: ..."
  }
}
```

**Pending task** (never reached due to upstream failure):
```json
{
  "key": "TASK_D",
  "status": "pending",
  "result": null
}
```

The five possible task states in Director: `pending` → `progress` → `success` / `error` / `canceled`.

### Method 2: Redis Result Backend (Direct)

Each task result is stored in Redis db 1 as a JSON string, keyed by `celery-task-meta-<task_id>`:

```bash
# Query directly from Redis
redis-cli -n 1 GET "celery-task-meta-<task_id>"
```

```json
{
  "status": "FAILURE",
  "result": {
    "exc_type": "HTTPError",
    "exc_message": ["500 Server Error: INTERNAL SERVER ERROR for url: ..."],
    "exc_module": "requests.exceptions"
  },
  "traceback": "Traceback (most recent call last): ...",
  "task_id": "12c5fa43-f070-4004-bf13-75707ba72e1d",
  "parent_id": "3a75c9e1-616f-4859-b6e4-b71f98ecd8b4",
  "group_id": "d2c294fc-51f2-404f-b180-a6f4d3c41e1b",
  "date_done": "2026-03-22T12:34:23.958757+00:00"
}
```

Note: Celery uses uppercase states (`PENDING`, `STARTED`, `SUCCESS`, `FAILURE`, `RETRY`) while Director uses lowercase (`pending`, `progress`, `success`, `error`).

### Method 3: Celery Python API (`AsyncResult`)

In application code, use `AsyncResult` to query any task by its ID:

```python
from celery import Celery

app = Celery(broker="redis://localhost:6379/0", backend="redis://localhost:6379/1")
result = app.AsyncResult("12c5fa43-f070-4004-bf13-75707ba72e1d")

result.state      # 'SUCCESS' | 'FAILURE' | 'PENDING' | 'RETRY' | 'STARTED'
result.result     # return value (success) or exception instance (failure)
result.traceback  # full traceback string (failure only)
result.date_done  # completion timestamp
```

### Where Does the Task ID Come From?

```
  ┌────────────────────┐     ┌──────────────────────────────────────────┐
  │  POST /api/workflows│     │  Response:                               │
  │  (trigger workflow) │────►│  {"id": "69766e17-..."}  ← workflow_id   │
  └────────────────────┘     └──────────────────────────────────────────┘
                                       │
                                       ▼
  ┌────────────────────────────┐     ┌──────────────────────────────────┐
  │  GET /api/workflows/<wf_id>│────►│  "tasks": [                      │
  └────────────────────────────┘     │    {"id":"3a75c...", "key":"A"}, │
                                      │    {"id":"12c5f...", "key":"B"}, │ ← task_id
                                      │    ...                          │
                                      │  ]                              │
                                      └──────────────────────────────────┘
```

The workflow ID is returned when you trigger it. Each task's ID is found inside the workflow detail response. You can then use any of the three methods above to inspect individual task status and errors.

---

## Retry Strategy Comparison

| Task | Strategy | Key Parameters | Behavior |
|------|----------|----------------|----------|
| A | `autoretry_for` | `retry_backoff=2, retry_jitter=False` | Auto-retry on exception, exponential backoff 2→4→8s |
| B | `self.retry()` | `countdown=3` | Manual retry with fixed 3s interval |
| C | `autoretry_for` | `retry_backoff=2, retry_jitter=True` | Auto-retry + random jitter to prevent thundering herd |
| D | None | — | Aggregation only, no model calls |

---

## Project Structure

```
celery-chain-group-chord-tutorial/
├── docker-compose.yml          # 5 services orchestration
├── .env                        # Director configuration
├── pyproject.toml              # Python dependencies (uv)
├── workflows.yml               # DAG definition: A → (B|C) → D
├── tasks/
│   ├── __init__.py
│   └── pipeline.py             # 4 Celery tasks with retry strategies
├── model_service/
│   └── app.py                  # Mock LLM inference API (Flask)
└── static/                     # Director UI local assets (offline CDN)
```

## Tech Stack

| Component | Role |
|-----------|------|
| [Redis](https://redis.io) | Celery broker (db 0) + result backend (db 1) |
| [Celery](https://docs.celeryq.dev) | Distributed task execution |
| [celery-director](https://github.com/ovh/celery-director) | DAG definition (YAML) + web UI |
| [uv](https://github.com/astral-sh/uv) | Fast Python package installer |
| Docker Compose | One-command local setup |

## License

MIT
