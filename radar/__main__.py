"""允许 `python -m radar` 调用 CLI。

注意这里必须走 ``cli.main()`` 而**不是** ``cli.app()``：
``main()`` 会先加载 ``.env`` 再进 Typer。所有双击脚本（login.cmd /
onboard.cmd / run-monitor.cmd）走的都是这条路径，一旦漏掉加载，
``radar onboard`` 就会看着一个填好的 ``.env`` 说「未填写」——
用户会以为自己的登录白做了。
"""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    main()
