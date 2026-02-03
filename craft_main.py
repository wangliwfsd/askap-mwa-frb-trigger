# main.py
import multiprocessing as mp
import traceback
from typing import Any, Dict, Optional
import time

def _hook_worker(module_name: str, context: Dict[str, Any]) -> None:
    """
    子进程里执行：import hook + 调用 hook.run(context)
    无论 hook 有语法错误、import 错误、运行时异常，都不会影响主程序。
    """
    try:
        module = __import__(module_name)
        run = getattr(module, "run", None)
        if not callable(run):
            print(f"[hook] Module '{module_name}' has no callable 'run(context)' function.")
            return
        run(context)
    except Exception:
        print("[hook] crashed (ignored). Traceback:")
        traceback.print_exc()


def trigger_hook(
    module_name: str,
    context: Optional[Dict[str, Any]] = None,
    timeout_s: Optional[float] = None,
) -> None:
    """
    触发 hook（异步、不阻塞主程序），并隔离到子进程。
    - module_name: hook 模块名（例如 "hook" 对应 hook.py）
    - context: 传给 hook.run(context) 的字典
    - timeout_s: 超时秒数；超时则终止子进程（可选）
    """
    if context is None:
        context = {}

    p = mp.Process(target=_hook_worker, args=(module_name, context), daemon=True)
    p.start()



def main() -> None:
    print("[main] start")

    # 主逻辑
    for i in range(3):
        time.sleep(0.1)
        print(f"[main] working... step={i}")


    # 触发 hook：注意这里传模块名 "hook"，不会在主进程 import hook
    trigger_hook(
        module_name="askap_mwa_frb_trigger",
        context={"event": "main_finished", "value": 123},
        timeout_s=5.0,   # 不需要超时可改成 None
    )

    for i in range(3):
        time.sleep(0.1)
        print(f"[main] working... step={i}")
    print("[main] continue (hook runs separately)")
    print("[main] done")




if __name__ == "__main__":

    main()
