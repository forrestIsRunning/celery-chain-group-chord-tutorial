"""
Celery DAG Tasks — 调用 Model Service + Retry 机制演示

四个任务使用不同的 retry 策略:
  TASK_A: autoretry_for + retry_backoff（自动重试 + 指数退避）
  TASK_B: self.retry() 手动重试（完全控制重试逻辑）
  TASK_C: autoretry_for + retry_jitter（自动重试 + 随机抖动防惊群）
  TASK_D: 无 retry（纯聚合，不调模型）

Scenario（通过 payload["scenario"] 控制）:
  happy         — 全部正常
  flaky         — A 前2次失败后成功
  slow          — A 调用模型延迟 8s（触发 soft_time_limit）
  down          — A 始终失败（耗尽重试）
  partial_fail  — A 正常，B 始终失败，C 正常
"""

import requests
from celery.exceptions import SoftTimeLimitExceeded
from director import task

MODEL_SERVICE_URL = "http://model-service:9000/v1/predict"

# ── 场景参数映射 ──────────────────────────────────────────
SCENARIO_CONFIG = {
    "happy": {
        "a": {"fail_times": 0, "delay_seconds": 0},
        "b": {"fail_times": 0, "delay_seconds": 0},
        "c": {"fail_times": 0, "delay_seconds": 0},
    },
    "flaky": {
        "a": {"fail_times": 2, "delay_seconds": 0},  # A 前2次失败，第3次成功
        "b": {"fail_times": 0, "delay_seconds": 0},
        "c": {"fail_times": 0, "delay_seconds": 0},
    },
    "slow": {
        "a": {"fail_times": 0, "delay_seconds": 8},  # A 慢响应 8s
        "b": {"fail_times": 0, "delay_seconds": 0},
        "c": {"fail_times": 0, "delay_seconds": 0},
    },
    "down": {
        "a": {"fail_times": 999, "delay_seconds": 0},  # A 永远失败
        "b": {"fail_times": 0, "delay_seconds": 0},
        "c": {"fail_times": 0, "delay_seconds": 0},
    },
    "partial_fail": {
        "a": {"fail_times": 0, "delay_seconds": 0},
        "b": {"fail_times": 999, "delay_seconds": 0},  # B 永远失败
        "c": {"fail_times": 0, "delay_seconds": 0},
    },
}


def call_model(text: str, task_type: str, request_id: str,
               fail_times: int = 0, delay_seconds: float = 0,
               timeout: float = 6) -> dict:
    """调用 Model Service 的通用函数"""
    payload = {
        "text": text,
        "task_type": task_type,
        "request_id": request_id,
        "fail_times": fail_times,
        "delay_seconds": delay_seconds,
    }
    resp = requests.post(MODEL_SERVICE_URL, json=payload, timeout=timeout)
    resp.raise_for_status()  # 4xx/5xx → 抛出 HTTPError
    return resp.json()


def get_scenario_config(payload: dict, task_key: str) -> dict:
    """从 payload 中获取当前 scenario 对应的 task 配置"""
    scenario = payload.get("scenario", "happy")
    return SCENARIO_CONFIG.get(scenario, SCENARIO_CONFIG["happy"]).get(task_key, {})


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TASK_A: autoretry_for + retry_backoff（指数退避自动重试）
#
#   Celery 概念:
#   - autoretry_for: 遇到指定异常时自动调 self.retry()
#   - retry_backoff=2: 指数退避，间隔 = 2^retries 秒 (2s, 4s, 8s)
#   - retry_backoff_max=30: 退避上限 30 秒
#   - max_retries=3: 最多重试 3 次
#   - soft_time_limit=6: 超过 6 秒抛 SoftTimeLimitExceeded
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@task(
    name="TASK_A",
    bind=True,
    autoretry_for=(requests.exceptions.RequestException, SoftTimeLimitExceeded),
    retry_backoff=2,
    retry_backoff_max=30,
    retry_jitter=False,
    max_retries=3,
    soft_time_limit=6,
)
def task_a(self, *args, **kwargs):
    payload = kwargs.get("payload", {})
    raw = payload.get("raw", "hello world")
    retries = self.request.retries
    config = get_scenario_config(payload, "a")

    print(f"\n{'='*60}")
    print(f"[A] === 第 {retries + 1} 次尝试 (max_retries={self.max_retries}) ===")
    print(f"[A] 收到输入: raw={raw!r}")
    print(f"[A] Scenario 配置: {config}")
    print(f"[A] Retry 策略: autoretry_for + backoff=2 (2s→4s→8s)")

    request_id = f"task_a_{self.request.id}"
    print(f"[A] 调用模型: POST {MODEL_SERVICE_URL}")
    print(f"[A]   request_id={request_id}, task_type=preprocess")

    result = call_model(
        text=raw,
        task_type="preprocess",
        request_id=request_id,
        timeout=5,
        **config,
    )

    output = {
        "processed": result["result"]["processed"],
        "length": result["result"]["length"],
        "model": result["model"],
        "retries_used": retries,
    }
    print(f"[A] 模型返回成功 (第 {result.get('call_count', '?')} 次调用)")
    print(f"[A] 输出: {output}")
    print(f"{'='*60}\n")
    return output


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TASK_B: self.retry() 手动重试
#
#   Celery 概念:
#   - bind=True: 第一个参数是 self (task instance)
#   - self.retry(exc=exc, countdown=N): 手动触发重试
#   - countdown: 固定等待 N 秒后重试（不是指数退避）
#   - 可以在 retry 前加自定义逻辑（日志、清理、降级等）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@task(
    name="TASK_B",
    bind=True,
    max_retries=3,
)
def task_b(self, *args, **kwargs):
    upstream = args[0] if args else {}
    payload = kwargs.get("payload", {})
    retries = self.request.retries
    config = get_scenario_config(payload, "b")

    print(f"\n{'='*60}")
    print(f"[B] === 第 {retries + 1} 次尝试 (max_retries={self.max_retries}) ===")
    print(f"[B] 收到上游 A 的数据: {upstream}")
    print(f"[B] Retry 策略: self.retry() 手动重试, countdown=3s 固定间隔")

    request_id = f"task_b_{self.request.id}"
    text = upstream.get("processed", "")

    try:
        print(f"[B] 调用模型: task_type=embed")
        result = call_model(
            text=text,
            task_type="embed",
            request_id=request_id,
            timeout=5,
            **config,
        )
    except requests.exceptions.RequestException as exc:
        # ↓↓↓ 手动重试的核心 ↓↓↓
        print(f"[B] !! 调用失败: {exc}")
        print(f"[B] !! 手动触发 self.retry(countdown=3)")
        print(f"[B] !! 将在 3 秒后重试...")
        raise self.retry(exc=exc, countdown=3)

    output = {
        "b_result": f"embedding by B: dim={result['result']['dimension']}",
        "embedding": result["result"]["embedding"],
        "model": result["model"],
        "retries_used": retries,
    }
    print(f"[B] 模型返回成功")
    print(f"[B] 输出: {output}")
    print(f"{'='*60}\n")
    return output


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TASK_C: autoretry_for + retry_jitter（防惊群）
#
#   Celery 概念:
#   - retry_jitter=True: 在 backoff 基础上加随机抖动
#     → 避免大量 worker 同时重试造成"惊群效应"
#   - 实际等待 = backoff * random(0, 1)，所以每次重试间隔不同
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@task(
    name="TASK_C",
    bind=True,
    autoretry_for=(requests.exceptions.RequestException,),
    retry_backoff=2,
    retry_backoff_max=15,
    retry_jitter=True,
    max_retries=3,
)
def task_c(self, *args, **kwargs):
    upstream = args[0] if args else {}
    payload = kwargs.get("payload", {})
    retries = self.request.retries
    config = get_scenario_config(payload, "c")

    print(f"\n{'='*60}")
    print(f"[C] === 第 {retries + 1} 次尝试 (max_retries={self.max_retries}) ===")
    print(f"[C] 收到上游 A 的数据: {upstream}")
    print(f"[C] Retry 策略: autoretry_for + jitter (随机退避防惊群)")

    request_id = f"task_c_{self.request.id}"
    text = upstream.get("processed", "")

    print(f"[C] 调用模型: task_type=classify")
    result = call_model(
        text=text,
        task_type="classify",
        request_id=request_id,
        timeout=5,
        **config,
    )

    output = {
        "c_result": f"classified by C: {result['result']['label']} ({result['result']['confidence']})",
        "label": result["result"]["label"],
        "confidence": result["result"]["confidence"],
        "model": result["model"],
        "retries_used": retries,
    }
    print(f"[C] 模型返回成功")
    print(f"[C] 输出: {output}")
    print(f"{'='*60}\n")
    return output


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TASK_D: 无 retry（纯聚合任务）
#
#   不调模型，只汇总 B 和 C 的结果。
#   group 后的任务收到的 args[0] 是 [B_result, C_result] 列表。
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@task(name="TASK_D", bind=True)
def task_d(self, *args, **kwargs):
    results = args[0] if args else []

    print(f"\n{'='*60}")
    print(f"[D] === 聚合任务（无 retry）===")
    print(f"[D] 收到并行结果列表 ({len(results)} 项): {results}")

    b_data = next((r for r in results if "b_result" in r), {})
    c_data = next((r for r in results if "c_result" in r), {})

    final = {
        "final": "merge complete",
        "from_b": b_data.get("b_result", "N/A"),
        "from_c": c_data.get("c_result", "N/A"),
        "b_retries": b_data.get("retries_used", 0),
        "c_retries": c_data.get("retries_used", 0),
    }
    print(f"[D] 最终输出: {final}")
    print(f"{'='*60}\n")
    return final
