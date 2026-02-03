# hook.py
from typing import Any, Dict
import time


def run(context: Dict[str, Any]) -> None:
    """
    约定：主程序会调用 hook.run(context)
    你可以在这里写任何 hook 逻辑：上报、写日志、发请求等。
    """
    print("[hook] received context:", context)
    
    # 模拟耗时任务
    time.sleep(1)

    # 你可以试试打开下面这行来模拟 hook 崩溃（不会影响主程序）
    # raise RuntimeError("hook boom!")

    print("[hook] finished OK")
