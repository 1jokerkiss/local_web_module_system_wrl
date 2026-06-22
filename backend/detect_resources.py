from __future__ import annotations

import os

try:
    import psutil
except Exception as exc:
    raise SystemExit(f"psutil is required: {exc}")

cpu = max(1, int(os.cpu_count() or 1))
total_gb = float(psutil.virtual_memory().total) / (1024 ** 3)
reserve_gb = max(4.0, total_gb * 0.20)
per_worker_gb = float(os.environ.get("LOCAL_WEB_MEMORY_PER_WORKER_GB", "3") or 3)

by_memory = max(1, int(max(0.0, total_gb - reserve_gb) // per_worker_gb))
by_cpu = max(1, int(cpu * 0.75))
max_slots = max(1, min(by_cpu, by_memory))
suggested = max(1, int(max_slots * 0.5))
total_threads = max(1, int(cpu * 0.75))
max_threads_per_child = max(
    1,
    min(4, max(1, total_threads // max(1, suggested))),
)

print(f"set LOCAL_WEB_DETECTED_CPU_COUNT={cpu}")
print(f"set LOCAL_WEB_DETECTED_MEMORY_GB={total_gb:.1f}")
print(f"set LOCAL_WEB_SUGGESTED_PROCESS_SLOTS={suggested}")
print(f"set LOCAL_WEB_MAX_PROCESS_SLOTS={max_slots}")
print(f"set LOCAL_WEB_TOTAL_COMPUTE_THREADS={total_threads}")
print(f"set LOCAL_WEB_MAX_THREADS_PER_CHILD={max_threads_per_child}")
