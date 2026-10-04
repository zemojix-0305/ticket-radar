"""命令行入口。

    radar init                      生成示例配置
    radar adapters                  查看可用适配器
    radar onboard [平台]            接入引导：Cookie 怎么取、接口地址怎么找
    radar login  <平台>             开个普通浏览器让你登录，Cookie 自动写进 .env
    radar sniff  <演出页地址>       用已存的登录态列出该页调用的数据接口
    radar find   <平台> <关键词>    搜演出，拿到能填进配置的 performance_id / tour_id
    radar channels                  选通知渠道：列出各渠道免费额度与准备成本
    radar probe <url>               打印一个接口的 JSON 结构（接新平台用）
    radar infer raw.json            让 AI 推断字段路径（接新平台用，可选）
    radar models                    列出 LLM 网关的可用模型
    radar check  -c tasks.yaml      单次查询，只打印不推送（调配置用）
    radar status -c tasks.yaml      看此刻各任务有没有票，--push 可发到手机
    radar run    -c tasks.yaml      常驻监控
    radar debug-raw -c tasks.yaml   打印 12306 原始字段下标（排查字段漂移）
    radar notify-test -c tasks.yaml 给所有渠道发一条测试推送
    radar preview -c tasks.yaml     预览推送长什么样（不用等真实放票）
    radar history -c tasks.yaml     查看余票变更历史
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import os
import sys
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any

import httpx
import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from . import __version__
from .adapters import available_adapters, create_adapter, dig, shape
from .assist import LLMConfig, LLMNotConfigured, infer_params, list_models
from .config import MIN_INTERVAL_SECONDS, AppConfig, load_config
from .engine import ChangeDetector, format_message
from .models import SeatAvailability, Snapshot
from .notifier import Message, build_notifiers
from .store import Store
from .view import snapshot_to_dict

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="多平台余票监控与提醒框架（只读 / 低频 / 只提醒不代下单）",
)
console = Console()
err_console = Console(stderr=True)

#: 适配器的一句话说明。加了新适配器记得同步这里——表格是用户第一眼看到的东西。
def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        datefmt="%H:%M:%S",
        handlers=[RichHandler(console=err_console, show_path=False, rich_tracebacks=True)],
    )
    # httpx 的 DEBUG 日志太吵
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


ConfigOpt = Annotated[Path, typer.Option("-c", "--config", help="配置文件路径")]


def _load(config: ConfigOpt) -> AppConfig:
    try:
        return load_config(config)
    except FileNotFoundError as exc:
        err_console.print(f"[red]{exc}[/red]")
        err_console.print("提示：先运行 [bold]radar init[/bold] 生成示例配置。")
        raise typer.Exit(code=2) from exc
    except Exception as exc:
        err_console.print(f"[red]配置校验失败：{exc}[/red]")
        raise typer.Exit(code=2) from exc


def _picker(config: AppConfig, only: str | None):
    tasks = config.enabled_tasks
    if only:
        tasks = [t for t in tasks if t.id == only]
        if not tasks:
            err_console.print(f"[red]未找到启用的任务 id={only}[/red]")
            raise typer.Exit(code=2)
    if not tasks:
        err_console.print("[yellow]没有启用的任务，请检查配置里的 enabled 字段。[/yellow]")
        raise typer.Exit(code=1)
    return tasks


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


@app.command()
def init(
    path: Annotated[Path, typer.Option("-o", "--output", help="输出路径")] = Path("tasks.yaml"),
    force: Annotated[bool, typer.Option("--force", help="覆盖已存在的文件")] = False,
) -> None:
    """生成示例配置文件和 .env 模板。"""
    if path.exists() and not force:
        err_console.print(f"[yellow]{path} 已存在，加 --force 覆盖。[/yellow]")
        raise typer.Exit(code=1)

    from .config import dump_example_config

    dump_example_config(path)
    console.print(f"[green]已写入[/green] {path}")

    env_example = Path(".env.example")
    if not env_example.exists():
        repo_root = Path(__file__).resolve().parent.parent
        src = repo_root / ".env.example"
        if src.exists():
            env_example.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
            console.print(f"[green]已写入[/green] {env_example}")

    console.print("\n下一步：")
    console.print("  1. 复制 [bold].env.example[/bold] 为 [bold].env[/bold] 并填写密钥（可选）")
    console.print(f"  2. 编辑 [bold]{path}[/bold] 里的 from / to / date")
    console.print(f"  3. [bold]radar check -c {path}[/bold] 单次试跑，确认能查到余票")
    console.print(f"  4. [bold]radar run -c {path}[/bold] 开始常驻监控")


# ---------------------------------------------------------------------------
# adapters
# ---------------------------------------------------------------------------


@app.command()
def adapters() -> None:
    """列出可用适配器：能处理什么需求、做不到什么。"""
    from .capability import capabilities, category_label

    caps = capabilities()
    table = Table(title=f"可用适配器（ticket-radar {__version__}）", show_lines=False)
    table.add_column("名称", style="bold cyan")
    table.add_column("类别")
    table.add_column("能搜")
    table.add_column("票档级")
    table.add_column("需登录")
    table.add_column("平台最小间隔")
    table.add_column("状态")

    for name in available_adapters():
        inst = create_adapter(name)
        cap = caps.get(name)
        if cap is None:
            table.add_row(name, "-", "-", "-", "-", f"{inst.min_interval:.0f} 秒", "-")
            continue
        status = "[green]可用[/green]" if cap.available() else "[red]接不了[/red]"
        # summary 放在名称下面，不占额外一列——表格已经有 7 列了
        name_cell = f"{name}\n[dim]{cap.summary}[/dim]" if cap.summary else name
        table.add_row(
            name_cell,
            category_label(cap.category),
            "能" if cap.can_search else "[dim]不能[/dim]",
            "能" if cap.seat_level else "[dim]不能[/dim]",
            "是" if inst.requires_credentials else "否",
            f"{inst.min_interval:.0f} 秒",
            status,
        )

    console.print(table)

    # 「做不到什么」用表格装不下，单独列出来。
    # 这是全表里最有价值的一栏——同类项目这里通常是一片沉默。
    limited = [(n, c) for n, c in caps.items() if c.limitation]
    if limited:
        lines = []
        for name, cap in sorted(limited):
            tag = "[red]接不了[/red] " if not cap.available() else ""
            lines.append(f"{tag}[bold]{name}[/bold]：{cap.limitation}")
        console.print(
            Panel(
                "\n".join(lines),
                title="各平台的边界（做不到什么）",
                border_style="dim",
            )
        )

    console.print(
        Panel(
            f"所有适配器的轮询间隔都受 [bold]{MIN_INTERVAL_SECONDS} 秒[/bold] 合规下限约束，"
            "且同平台的请求会被串行化——就算你配 10 个任务，"
            "对同一平台的请求也不会超过这个频率。\n\n"
            "[dim]本项目的定位是余票信息聚合，不是抢票工具。[/dim]",
            title="限流说明",
            border_style="dim",
        )
    )


# ---------------------------------------------------------------------------
# onboard：Cookie 怎么取、接口地址怎么找
# ---------------------------------------------------------------------------


def _env_path() -> str:
    """把 .env 的真实路径打出来，省得用户猜它在哪。"""
    from .config import find_dotenv

    found = find_dotenv(Path.cwd())
    return str(found) if found else str(Path.cwd() / ".env")


def _credential_env() -> dict[str, str]:
    """体检凭据时用的合并视图：``.env`` 文件 ∪ 进程环境变量。

    为什么不能只看 ``os.environ``：``onboard`` 的职责就是体检那个 ``.env``，
    它该直接读那个文件，而不是赌「启动时有没有人替我加载过」。
    环境变量仍优先——CI 注入的值、或临时 ``NTFY_TOPIC=x radar run`` 说了算。
    """
    from .config import parse_dotenv

    merged: dict[str, str] = {}
    path = Path(_env_path())
    with contextlib.suppress(OSError):
        merged.update(parse_dotenv(path.read_text(encoding="utf-8-sig")))
    merged.update(os.environ)
    return merged


_STATUS_MARK = {
    "ok": "[green]✓[/green]",
    "empty": "[red]✗[/red]",
    "too_short": "[yellow]![/yellow]",
    "incomplete": "[yellow]![/yellow]",
    "planned": "[dim]·[/dim]",
    "not_needed": "[green]✓[/green]",
}


def _render_steps(steps: tuple[str, ...]) -> None:
    for index, step in enumerate(steps, 1):
        console.print(f"  [cyan]{index}.[/cyan] {step}")


def _render_overview() -> None:
    from .onboarding import check_credential, iter_guides

    table = Table(title="平台接入状态", show_lines=False)
    table.add_column("平台", style="bold")
    table.add_column("命令里用的键", style="cyan")
    table.add_column("凭据")
    table.add_column("接上之后能给你什么")

    for guide in iter_guides():
        status = check_credential(guide, _credential_env())
        mark = _STATUS_MARK.get(status.level, "")
        table.add_row(guide.label, guide.key, f"{mark} {status.headline}", guide.what_you_get)

    console.print(table)
    console.print(
        Panel(
            "看某个平台的详细步骤：[bold]radar onboard <键>[/bold]，"
            "例如 [bold]radar onboard damai[/bold]\n"
            f"凭据填在：{_env_path()}\n"
            "[dim]改完 .env 再跑一次本命令，就能看到状态变化。[/dim]",
            title="下一步",
            border_style="dim",
        )
    )


def _render_guide(guide: Any) -> None:
    from .onboarding import check_credential

    console.print(Panel(guide.what_you_get, title=guide.label, border_style="cyan"))

    if guide.planned:
        console.print("\n[bold]为什么它现在还不能直接用[/bold]")
        for line in guide.caveats:
            console.print(f"  · {line}")
        return

    status = check_credential(guide, _credential_env())
    mark = _STATUS_MARK.get(status.level, "")
    line = f"\n[bold]当前凭据状态：[/bold]{mark} {status.headline}"
    if status.hint:
        line += f"  [dim]{status.hint}[/dim]"
    console.print(line)

    if guide.no_credentials:
        # 公开接口没有「第 1 步：取 Cookie」。硬塞一段取 Cookie 的教程会把人
        # 骗去干一件不必要的事——这正是「实测推翻旧假设」后必须改掉的那类文案。
        console.print("\n[bold]这个平台的关键接口是公开的，不用登录[/bold]，直接找 id：")
        _render_steps(guide.api_steps)
    else:
        console.print(
            f"\n[dim]嫌手工麻烦？直接跑 [bold]radar login {guide.key}[/bold]——"
            "开个普通浏览器让你登录（滑块和平时一样好使），"
            "Cookie 自动写进 .env，全程不用碰 F12。[/dim]"
        )
        if guide.login_hint:
            console.print(f"  [bold green]推荐登录方式：[/bold green]{guide.login_hint}")

        console.print(f"\n[bold]第 1 步｜把 Cookie 填进 {guide.env_key}[/bold]")
        _render_steps(guide.cookie_steps)

        console.print("\n[bold]第 2 步｜找到那场演出的接口地址[/bold]")
        _render_steps(guide.api_steps)

        console.print("\n[bold]第 3 步｜剩下的交给我[/bold]")
        console.print("  把接口地址贴过来即可。我会先打印它的真实响应结构，")
        console.print("  再让配置助手推断字段路径——推断结果必须通过真实解析器验证，")
        console.print("  猜错会被当场戳穿，最后写进 tasks.yaml 用 radar check 复核。")
        console.print(
            f"  [dim]你也可以自己跑：radar probe \"<地址>\" --cookie-env {guide.env_key}[/dim]"
        )

    if guide.sample_task:
        if guide.no_credentials:
            console.print("\n  [dim]配置直接抄这段（id 用 radar find 拿）：[/dim]")
        else:
            console.print("\n  [dim]接好之后它的配置大概长这样（不用你手写）：[/dim]")
        console.print(Syntax(guide.sample_task, "yaml", line_numbers=False))

    if guide.caveats:
        console.print("\n[bold]注意[/bold]")
        for line in guide.caveats:
            console.print(f"  · {line}")


@app.command()
def onboard(
    platform: Annotated[
        str | None,
        typer.Argument(
            help="平台键：damai / maoyan / moretickets / fenwandao / jwc；留空看总览"
        ),
    ] = None,
    open_page: Annotated[
        bool, typer.Option("--open", help="顺便用默认浏览器打开该平台的登录页")
    ] = False,
) -> None:
    """接入引导：某个平台的 Cookie 怎么取、接口地址怎么找。

    只讲「只有人能做的那一步」——登录、复制 Cookie、在 F12 里找到接口地址。
    剩下的探测、推断、验证、写配置都由程序接手。
    """
    from .onboarding import iter_guides, resolve

    if platform is None:
        _render_overview()
        return

    guide = resolve(platform)
    if guide is None:
        known = "、".join(g.key for g in iter_guides())
        err_console.print(
            f"[red]认不出 {platform!r}[/red]。可用：{known}（也支持中文名，如 大麦）"
        )
        raise typer.Exit(code=1)

    _render_guide(guide)
    if open_page and guide.login_url:
        console.print(f"\n[dim]已用默认浏览器打开：{guide.login_url}[/dim]")
        webbrowser.open(guide.login_url)


@app.command()
def login(
    platform: Annotated[
        str, typer.Argument(help="平台键：damai / maoyan / moretickets / fenwandao")
    ],
    timeout: Annotated[int, typer.Option("--timeout", help="等你登录的最长秒数")] = 600,
    automated: Annotated[
        bool,
        typer.Option(
            "--automated",
            help="改用自动化窗口登录。默认不用——滑块能认出自动化窗口，"
            "然后无论怎么划都会判「位置不对」",
        ),
    ] = False,
) -> None:
    """打开一个**普通**浏览器让你登录，Cookie 自动写进 .env——不用碰 F12。

    刻意不用自动化窗口：**登录由你完成，Cookie 由浏览器自己给出**，
    程序只是不再给这件事使绊子。登录状态留在项目目录下，同一个平台只需登一次。

    登录完想找那场演出的接口地址？跑 `radar sniff <演出页地址>`。
    """
    from .browser import (
        PROFILE_RELATIVE,
        BrowserUnavailable,
        capture,
        close_browser,
        find_edge,
        launch_manual_browser,
        read_cookies,
        site_url,
        wait_for_browser_exit,
        watch_for_enter,
        write_env_value,
    )
    from .onboarding import check_credential, iter_guides, resolve

    guide = resolve(platform)
    if guide is None or not guide.login_url:
        if guide is not None:
            # 平台认得出来但没法自动登录——那就把「为什么」讲清楚，
            # 否则用户只会看到一句没头没脑的「不支持」，然后反复重试。
            err_console.print(f"[yellow]{guide.label} 没法自动登录[/yellow]")
            for line in guide.caveats:
                err_console.print(f"  · {line}")
            raise typer.Exit(code=1)
        available = "、".join(g.key for g in iter_guides() if g.login_url)
        err_console.print(f"[red]{platform!r} 不支持自动登录[/red]。可用：{available}")
        raise typer.Exit(code=1)

    env_path = Path(_env_path())
    profile_dir = env_path.parent / PROFILE_RELATIVE

    if guide.no_credentials:
        # 别把用户拉去登录一件**不需要登录**的平台。开个浏览器、扫码、
        # 关窗、写 .env——全程做完发现压根用不上，那种被耍的感觉很差。
        console.print(
            Panel(
                f"[bold]{guide.label} 不需要登录[/bold]"
                "（它的关键接口是公开的，带上 Cookie 也不会更准）。\n\n"
                "直接搜出你要盯的那场就行：\n"
                f"  [bold]radar find {guide.key} <关键词>[/bold]\n"
                "把输出里的 id 填进 tasks.yaml 的 params，收工。",
                title="这一步可以省掉",
                border_style="green",
            )
        )
        raise typer.Exit(code=0)

    hint = (
        f"\n[bold green]推荐登录方式[/bold green]：{guide.login_hint}\n"
        if guide.login_hint
        else ""
    )

    cookies = ""
    cookie_names: list[str] = []
    requests_seen: list[str] = []

    if automated:
        console.print(
            Panel(
                f"[bold]接下来是自动化窗口[/bold]，请在里面登录 {guide.label}。\n"
                f"{hint}\n"
                "[yellow]注意[/yellow]：自动化窗口会被部分平台的风控识别"
                "（典型症状是滑块怎么划都判「位置不对」）。\n"
                "如果撞上这个，别加参数去绕——去掉 --automated，用默认方式登录。",
                title="自动化登录（非默认）",
                border_style="yellow",
            )
        )
        try:
            result = asyncio.run(
                capture(
                    login_url=guide.login_url,
                    profile_dir=profile_dir,
                    timeout=timeout,
                )
            )
        except BrowserUnavailable as exc:
            err_console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        cookies = result.cookies
        cookie_names = result.cookie_names
        requests_seen = result.requests
    else:
        console.print(
            Panel(
                f"[bold]接下来会打开一个普通 Edge 窗口[/bold]"
                f"（不是自动化窗口，所以滑块会正常工作）。\n"
                f"{hint}\n"
                "请这样做：\n"
                "  1. 在这个窗口里登录（遇到欢迎页 / 「登录以同步」提示，直接跳过）\n"
                "  2. 顺手打开你要盯的那场演出页\n"
                "  3. [bold]把这个浏览器窗口全部关掉[/bold]"
                "（或回到这里按回车），程序自动继续\n\n"
                f"[dim]浏览器用独立配置，存在 {profile_dir}；"
                "登录状态会留着，下次不用重登。\n"
                "Cookie 由浏览器自己交出，不经过任何第三方。[/dim]",
                title="登录",
                border_style="cyan",
            )
        )
        try:
            proc = launch_manual_browser(
                url=guide.login_url, profile_dir=profile_dir, edge=find_edge()
            )
        except BrowserUnavailable as exc:
            err_console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc

        how = wait_for_browser_exit(proc, timeout=timeout, stop_event=watch_for_enter())
        if how == "timeout":
            console.print("[yellow]等太久了[/yellow]，先按现在拿到的状态收尾。")

        # 「按回车」这个出口不会关浏览器；而 Edge 关掉窗口后还会把主进程
        # 留在后台。两种情况都让 profile 一直被占着，紧接着的读取必然失败。
        # 所以这里主动把它请走再读——登录态已经落盘，关掉不影响。
        if how != "closed":
            console.print("[dim]请浏览器退出，然后读取 Cookie…[/dim]")
        if not close_browser(proc):
            err_console.print(
                "[red]关不掉那个浏览器[/red]——它还占着配置目录。\n"
                "手动把 Edge 完全退出（任务栏、托盘都看一眼），再跑一次 "
                "radar login。"
            )
            raise typer.Exit(code=1)

        try:
            cookies, cookie_names = asyncio.run(
                read_cookies(profile_dir=profile_dir, site_url=site_url(guide.login_url))
            )
        except BrowserUnavailable as exc:
            err_console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc

    if not cookies:
        err_console.print(
            "[yellow]一条 Cookie 都没拿到[/yellow]——多半是浏览器里还没登录成功。\n"
            "再跑一次，这次登录完再把浏览器窗口关掉。"
        )
        raise typer.Exit(code=1)

    created = write_env_value(env_path, guide.env_key, cookies)
    console.print(
        f"\n[green]✓[/green] 已把 {len(cookie_names)} 条 Cookie "
        f"{'写入' if created else '更新到'} [bold]{env_path}[/bold] 的 {guide.env_key}"
    )
    console.print(f"  [dim]包含：{'、'.join(cookie_names)}[/dim]")

    status = check_credential(guide, {guide.env_key: cookies})
    mark = _STATUS_MARK.get(status.level, "")
    line = f"  体检：{mark} {status.headline}"
    if status.hint:
        line += f"  [dim]{status.hint}[/dim]"
    console.print(line)
    if not status.ok:
        console.print(
            "  [yellow]看着像还没真正登录成功[/yellow]——"
            "登录之后才会出现的那几个字段没拿到。\n"
            "  [dim]再跑一次，这次一定等页面显示已登录再把窗口关掉。[/dim]"
        )

    if requests_seen:
        console.print("\n[bold]顺便记下它访问过的数据接口[/bold]（按出现顺序）：")
        for index, found in enumerate(requests_seen, 1):
            console.print(f"  {index}. {found}")
        console.print(
            "\n[dim]如果里面有那场演出的票档接口，把它发我，"
            "我就能直接把监控配好。[/dim]"
        )
    else:
        console.print(
            "\n[dim]想找那场演出的接口地址？把演出页地址交给 "
            "[bold]radar sniff <地址>[/bold]，它用刚存下的登录态打开页面，"
            "把调用的接口列出来。[/dim]"
        )


@app.command()
def sniff(
    url: Annotated[str, typer.Argument(help="要观察的页面地址，例如某场演出的详情页")],
    limit: Annotated[int, typer.Option("--limit", help="最多列几个接口")] = 12,
    show: Annotated[
        bool, typer.Option("--show", help="显示浏览器窗口（排查页面为什么没出接口时用）")
    ] = False,
) -> None:
    """用已保存的登录态打开一个页面，列出它调用的数据接口。

    先跑 `radar login <平台>` 登录一次，再把要盯的演出页地址丢进来，
    它会告诉你哪个请求是票档接口——省掉在 F12 的 Network 面板里翻找的功夫。
    """
    from .browser import PROFILE_RELATIVE, BrowserUnavailable, sniff_requests

    env_path = Path(_env_path())
    profile_dir = env_path.parent / PROFILE_RELATIVE
    if not profile_dir.is_dir():
        err_console.print(
            "[red]还没有登录过任何平台[/red]，所以没有登录态可用。\n"
            "先跑一次 [bold]radar login <平台>[/bold]。"
        )
        raise typer.Exit(code=1)

    console.print(f"[dim]用 {profile_dir} 里的登录态打开页面…[/dim]")
    try:
        found = asyncio.run(
            sniff_requests(
                url=url, profile_dir=profile_dir, headless=not show, limit=limit
            )
        )
    except BrowserUnavailable as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    if not found:
        err_console.print(
            "[yellow]没看到数据接口[/yellow]。可能是页面还没加载完，或者它压根不发 xhr。\n"
            "加 [bold]--show[/bold] 看看到底打开成什么样；也可以换个页面地址再试。"
        )
        raise typer.Exit(code=1)

    console.print(f"\n[bold]这个页面调用了 {len(found)} 个数据接口[/bold]（按出现顺序）：")
    for index, item in enumerate(found, 1):
        console.print(f"  {index}. {item}")
    console.print(
        "\n[dim]把其中像「票档 / 场次 / 余量」的那条发我，我就能把监控配好。[/dim]"
    )


# ---------------------------------------------------------------------------
# find：按关键词搜演出，把「演出名」翻译成平台内部 id
# ---------------------------------------------------------------------------


#: ``radar find`` 认识的平台 → (展示名, 该平台主键字段名, 等价命令示例)
#: 只有「余票接口要的是内部 id、而这个 id 只能从搜索接口拿」的平台才需要它。
FIND_TABLE: dict[str, tuple[str, str, str]] = {
    "maoyan": ("猫眼演出", "performance_id", "radar find maoyan 陈粒"),
    "moretickets": ("摩天轮票务", "tour_id", "radar find moretickets Jay Chou"),
}


def _find_platform(name: str) -> str:
    """把用户输入解析成 find 支持的平台 key（认英文 key 也认中文名）。"""
    from .onboarding import ALIASES

    raw = (name or "").strip()
    lowered = raw.lower()
    if lowered in FIND_TABLE:
        return lowered
    mapped = ALIASES.get(raw) or ALIASES.get(lowered)
    if mapped in FIND_TABLE:
        return mapped
    raise typer.BadParameter(
        f"不认识的平台 {name!r}。支持：{'、'.join(FIND_TABLE)}（也认「猫眼」「摩天轮」）"
    )


def _find_cells(key: str, row: dict[str, Any]) -> tuple[str, ...]:
    """把一条候选渲染成表格的列（顺序与表头一致）。"""
    if key == "maoyan":
        from .adapters.maoyan import status_label

        raw_status = row.get("ticketStatus")
        return (
            str(row.get("performanceId") or ""),
            str(row.get("name") or ""),
            str(row.get("cityName") or ""),
            str(row.get("showTimeRange") or ""),
            str(row.get("priceRange") or ""),
            status_label(raw_status) or str(raw_status or ""),
        )
    price = row.get("price") if isinstance(row.get("price"), dict) else {}
    return (
        str(row.get("tourId") or ""),
        str(row.get("title") or row.get("showName") or ""),
        str(row.get("location") or ""),
        str(row.get("showDate") or ""),
        str(price.get("minSalePrice") or ""),
        str(row.get("status") or ""),
    )


def _find_id(key: str, row: dict[str, Any]) -> str:
    field = "performanceId" if key == "maoyan" else "tourId"
    return str(row.get(field) or "")


@app.command()
def find(
    platform: Annotated[
        str, typer.Argument(help="平台：maoyan / moretickets（也认「猫眼」「摩天轮」）")
    ],
    keyword: Annotated[str, typer.Argument(help="演出关键词，如 陈粒")],
    as_json: Annotated[
        bool, typer.Option("--json", help="输出 JSON，便于脚本处理")
    ] = False,
    city: Annotated[
        str | None, typer.Option("--city", help="只保留城市名含该字串的候选")
    ] = None,
    size: Annotated[int, typer.Option("-n", "--size", help="最多取几条候选")] = 20,
    verbose: Annotated[bool, typer.Option("-v", "--verbose")] = False,
) -> None:
    """按关键词搜演出，拿到能直接填进 tasks.yaml 的 id。

    猫眼和摩天轮的余票接口都**不需要登录**，所以这一步不碰 .env：

        radar find maoyan 陈粒
        radar find moretickets Jay Chou --city 广州
        radar find 猫眼 陈粒 --json

    挑中意的那一行，把第一列抄进配置：

        params:
          performance_id: "501675"              # 猫眼
          tour_id: "6a2a300f941d1b00014a8828"   # 摩天轮

    为什么要有这一步：余票接口要的是平台内部 id，不是演出名；而站内搜索页
    是前端路由，抓不到能直接复用的地址。所以由 CLI 充当这个翻译层——
    这也是「不用点 F12」的一部分。
    """
    _setup_logging(verbose)
    key = _find_platform(platform)
    label, id_field, example = FIND_TABLE[key]

    async def _run() -> list[dict[str, Any]]:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
            headers={"Accept-Language": "zh-CN,zh;q=0.9"},
        ) as client:
            if key == "maoyan":
                from .adapters.maoyan import search_performances

                return await search_performances(client, keyword, size=size)
            from .adapters.moretickets import search_tours

            return await search_tours(client, keyword, length=size)

    try:
        rows = asyncio.run(_run())
    except Exception as exc:
        err_console.print(f"[red]{label}搜索失败：{exc}[/red]")
        raise typer.Exit(code=1) from exc

    if city:
        lowered = city.strip()
        rows = [r for r in rows if lowered in json.dumps(r, ensure_ascii=False)]
    if not rows:
        hint = (
            "摩天轮是二手票平台，冷门或纯内地场次可能根本没有挂单——这是常态，不是故障。"
            if key == "moretickets"
            else "换个更短的关键词（比如只留人名）再试。"
        )
        err_console.print(f"[yellow]{label}没搜到与「{keyword}」相关的演出。[/yellow]\n{hint}")
        raise typer.Exit(code=1)

    if as_json:
        typer.echo(
            json.dumps(
                {"platform": key, "keyword": keyword, "items": rows},
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    table = Table(title=f"{label}　「{keyword}」的候选（{len(rows)} 条）")
    table.add_column("#", justify="right", style="dim")
    for column in (id_field, "演出", "城市", "时间", "票价", "状态"):
        table.add_column(column, style="bold cyan" if column == id_field else None)
    for index, row in enumerate(rows, 1):
        table.add_row(str(index), *_find_cells(key, row))
    console.print(table)
    if key == "maoyan":
        console.print(
            "[dim]「状态」一列来自搜索索引，可能滞后于详情页"
            "（实测同一时刻列表报「预售」、详情报「在售中」）。监控时以详情为准。[/dim]"
        )
    console.print(
        f"\n[dim]把 {id_field} 那一列抄进 tasks.yaml 的 params，例如：[/dim]\n"
        f"  params:\n    {id_field}: \"{_find_id(key, rows[0])}\"\n"
        f"[dim]或者干脆用关键词让它自己搜：params: {{keyword: \"{keyword}\"}}[/dim]\n"
        f"[dim]（等价命令：{example}）[/dim]"
    )


# ---------------------------------------------------------------------------
# channels：帮我选一个通知渠道
# ---------------------------------------------------------------------------


@app.command()
def channels(config: ConfigOpt = Path("tasks.yaml")) -> None:
    """列出全部通知渠道的免费额度与准备成本，并显示当前配置状态。"""
    from .notifier import NotConfiguredError
    from .notifier.catalog import CHANNELS
    from .notifier.registry import create_notifier

    # 配置文件不存在也照样能看目录——用户可能还没 init
    status: dict[str, str] = {}
    try:
        cfg = load_config(config)
    except FileNotFoundError:
        cfg = None
    if cfg is not None:
        for item in cfg.notify:
            if not item.enabled:
                status[item.type] = "[dim]已关闭[/dim]"
                continue
            try:
                create_notifier(item.type, item.options)
                status[item.type] = "[green]已就绪[/green]"
            except NotConfiguredError:
                status[item.type] = "[yellow]未填凭据[/yellow]"
            except Exception:
                status[item.type] = "[red]配置有误[/red]"

    table = Table(title=f"通知渠道（ticket-radar {__version__}）", show_lines=False)
    table.add_column("渠道", style="bold cyan")
    table.add_column("免费额度")
    table.add_column("微信直达")
    table.add_column("需要你准备")
    table.add_column("当前状态")

    for info in CHANNELS:
        table.add_row(
            info.name,
            info.quota,
            "[green]是[/green]" if info.wechat else "否",
            info.needs,
            status.get(info.name, "[dim]未启用[/dim]"),
        )

    console.print(table)

    console.print(
        Panel(
            "本项目的默认推荐是 [bold cyan]ntfy[/bold cyan] 和 [bold cyan]bark[/bold cyan]：\n"
            "这两个都不需要注册账号，官方公共实例免费用，而且都是开源项目、"
            "可以用 Docker 一行自建——不受任何商业政策变动影响。\n\n"
            "对开源项目来说这一点很重要：[bold]不能把使用者的门槛建在别人的付费墙上[/bold]。\n"
            "聚合类服务（pushplus / serverchan）直达微信最省事，但免费额度由平台说了算，"
            "所以没有放进默认配置。\n\n"
            "[dim]配置方法见 README「通知渠道」一节。[/dim]",
            title="怎么选",
            border_style="dim",
        )
    )


# ---------------------------------------------------------------------------
# probe：接新平台的入口
# ---------------------------------------------------------------------------


@app.command()
def probe(
    url: Annotated[str, typer.Argument(help="要探查的接口 URL（浏览器 F12 → Copy as cURL 里取）")],
    cookie: Annotated[
        str | None, typer.Option("--cookie", help="Cookie 串；建议改用 --cookie-env 免得进历史记录")
    ] = None,
    cookie_env: Annotated[
        str | None, typer.Option("--cookie-env", help="从该环境变量名读取 Cookie，如 DAMAI_COOKIE")
    ] = None,
    header: Annotated[
        list[str] | None, typer.Option("-H", "--header", help="附加请求头，格式 K:V，可重复")
    ] = None,
    method: Annotated[str, typer.Option("-X", "--method", help="请求方法")] = "GET",
    body: Annotated[str | None, typer.Option("--data", help="请求体（JSON 字符串）")] = None,
    path: Annotated[
        str | None, typer.Option("--path", help="只看这个子路径，如 data.result")
    ] = None,
    depth: Annotated[int, typer.Option("-d", "--depth", help="展开层数")] = 3,
    max_items: Annotated[
        int, typer.Option("-n", "--max-items", help="每层最多显示几个键")
    ] = 12,
    save: Annotated[
        Path | None, typer.Option("--save", help="把响应原文存到文件，便于慢慢翻")
    ] = None,
) -> None:
    """请求一个接口并打印它的 JSON 结构。

    这是接新平台的第一步：先看清结构，再写配置。

        radar probe "https://show.maoyan.com/..." --cookie-env MAOYAN_COOKIE

    拿到结构树之后，把 items_path / seat_name / seat_status 抄进 tasks.yaml 即可，
    不用改代码。
    """
    _setup_logging(False)

    resolved_cookie = cookie
    if not resolved_cookie and cookie_env:
        resolved_cookie = os.environ.get(cookie_env, "")
        if not resolved_cookie:
            err_console.print(f"[yellow]环境变量 {cookie_env} 为空。[/yellow]")

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    for raw in header or []:
        name, _, value = raw.partition(":")
        if name.strip():
            headers[name.strip()] = value.strip()
    if resolved_cookie:
        headers["Cookie"] = resolved_cookie

    json_body = None
    if body:
        try:
            json_body = json.loads(body)
        except json.JSONDecodeError as exc:
            err_console.print(f"[red]--data 不是合法 JSON：{exc}[/red]")
            raise typer.Exit(code=2) from exc

    async def _run() -> None:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0), follow_redirects=True
        ) as client:
            try:
                resp = await client.request(
                    method.upper(), url, headers=headers, json=json_body
                )
            except httpx.HTTPError as exc:
                err_console.print(f"[red]请求失败：{exc}[/red]")
                raise typer.Exit(code=1) from exc

            console.print(
                f"[bold]{method.upper()}[/bold] {url}\n"
                f"状态码 [bold]{resp.status_code}[/bold]　"
                f"类型 {resp.headers.get('content-type', '?')}　"
                f"长度 {len(resp.content)} 字节"
            )
            if save:
                save.write_text(resp.text, encoding="utf-8")
                console.print(f"[green]已保存[/green] {save}")

            try:
                data = resp.json()
            except ValueError:
                console.print("[yellow]响应不是 JSON，前 600 字符：[/yellow]")
                console.print(Syntax(resp.text[:600], "html", word_wrap=True))
                console.print(
                    "[dim]提示：返回 HTML 通常是撞上了登录页或风控页——"
                    "补全 Cookie / Referer，或降低请求频率，不要尝试绕过。[/dim]"
                )
                return

            if path:
                sub = dig(data, path)
                # dig 找不到时返回 MISSING（一个裸 object() 实例），
                # 所以「类型恰为 object」就等于「该路径不存在」。
                if type(sub) is object:
                    err_console.print(f"[red]路径 {path!r} 不存在。[/red]")
                    console.print("整个响应的结构：")
                    console.print(shape(data, depth=depth, max_items=max_items))
                    return
                console.print(f"[cyan]路径 {path} 的结构：[/cyan]")
                console.print(shape(sub, depth=depth, max_items=max_items))
                return

            console.print(shape(data, depth=depth, max_items=max_items))
            console.print(
                "\n[dim]把上面这棵树里的路径填进 tasks.yaml 的 items_path / "
                "seat_name / seat_status 即可，不用改代码。[/dim]"
            )

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# infer / models：配置助手（可选的开发期工具）
# ---------------------------------------------------------------------------


@app.command()
def infer(
    payload: Annotated[
        Path, typer.Argument(help="接口返回的 JSON 文件（用 radar probe --save 拿到）")
    ],
    intent: Annotated[
        str, typer.Option("--intent", help="这个接口是干嘛的；写得越具体，推断越准")
    ] = "",
    url: Annotated[
        str | None, typer.Option("--url", help="接口地址，帮模型理解字段含义")
    ] = None,
    attempts: Annotated[
        int, typer.Option("--attempts", help="验证不通过时最多重试几次")
    ] = 2,
    out: Annotated[
        Path | None, typer.Option("-o", "--out", help="把生成的 YAML 片段写到文件")
    ] = None,
    show_raw: Annotated[
        bool, typer.Option("--show-raw", help="同时打印模型的原始回复（排查用）")
    ] = False,
) -> None:
    """让 AI 帮你推断字段路径 —— 新接一个平台时用。

    典型流程：

        radar probe "<接口URL>" --cookie-env XXX_COOKIE --save raw.json
        radar infer raw.json --intent "各大票档的余量" --url "<接口URL>"

    输出是一段可以直接合并进 tasks.yaml 的 params，而且**出来之前已经用真实
    解析器验证过能解出单元和票档**——模型猜错会被自动退回重试。

    这个命令完全可选：不配 LLM_API_KEY 时，监控本身照常运行。
    """
    import yaml

    _setup_logging(False)

    if not payload.exists():
        err_console.print(f"[red]文件不存在：{payload}[/red]")
        err_console.print(
            "提示：先用 [bold]radar probe <url> --save raw.json[/bold] 抓一份响应。"
        )
        raise typer.Exit(code=2)

    try:
        data = json.loads(payload.read_text(encoding="utf-8"))
    except ValueError as exc:
        err_console.print(f"[red]{payload} 不是合法 JSON：{exc}[/red]")
        raise typer.Exit(code=2) from exc

    try:
        llm = LLMConfig.from_env()
    except LLMNotConfigured as exc:
        err_console.print(f"[yellow]{exc}[/yellow]")
        raise typer.Exit(code=2) from exc

    async def _run() -> None:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(llm.timeout, connect=15.0)
        ) as client:
            console.print(
                f"用 [bold]{llm.model}[/bold] 推断字段路径"
                f"（{llm.base_url}，最多试 {attempts} 次）…"
            )
            try:
                result = await infer_params(
                    data, llm=llm, client=client, intent=intent, url=url, attempts=attempts
                )
            except RuntimeError as exc:
                err_console.print(f"[red]{exc}[/red]")
                raise typer.Exit(code=1) from exc

        for line in result.trail:
            console.print(f"[dim]· {line}[/dim]")

        if result.raw and show_raw:
            console.print(
                Panel(Text(result.raw), title="模型原始回复", border_style="dim")
            )

        if not result.ok:
            err_console.print(f"[red]推断失败：{result.note}[/red]")
            console.print(
                "[dim]可以换个模型（radar models 看列表），"
                "或把 --intent 写具体些再试。[/dim]"
            )
            raise typer.Exit(code=1)

        console.print(f"[green]{result.note}[/green]")
        snippet = yaml.safe_dump(
            {"params": result.params}, allow_unicode=True, sort_keys=False
        )
        console.print()
        console.print(
            Panel(
                Syntax(snippet, "yaml", word_wrap=True),
                title="[bold]把这段合并进 tasks.yaml 里对应任务的 params[/bold]",
                subtitle=f"第 {result.attempts} 次尝试通过验证",
                border_style="green",
            )
        )
        if out:
            out.write_text(snippet, encoding="utf-8")
            console.print(f"[green]已写入[/green] {out}")

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run())


@app.command()
def models(
    timeout: Annotated[float, typer.Option("--timeout", help="请求超时秒数")] = 30.0,
) -> None:
    """列出 LLM 网关的可用模型（配 LLM_MODEL 时省得靠猜）。"""
    _setup_logging(False)

    try:
        llm = LLMConfig.from_env()
    except LLMNotConfigured as exc:
        err_console.print(f"[yellow]{exc}[/yellow]")
        raise typer.Exit(code=2) from exc

    async def _run() -> list[str]:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=15.0)
        ) as client:
            return await list_models(llm, client=client)

    try:
        names = asyncio.run(_run())
    except RuntimeError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    console.print(f"[bold]{llm.base_url}[/bold] 共 {len(names)} 个可用模型：")
    for name in names:
        marker = "　[green]← 当前 LLM_MODEL[/green]" if name == llm.model else ""
        console.print(f"  · {name}{marker}")


# ---------------------------------------------------------------------------
# check：单次查询
# ---------------------------------------------------------------------------


def _render_snapshot(snapshot: Snapshot, task_name: str, watch_types: list[str]) -> None:
    if not snapshot.trains:
        console.print(f"[yellow]任务 {task_name}：本次未返回任何条目。[/yellow]")
        if snapshot.platform == "rail12306":
            console.print(
                "[dim]可能原因：日期超出预售期、区间无车、车站名不对、或触发了风控。[/dim]"
            )
        else:
            console.print(
                "[dim]可能原因：这场还没开卖、id 不对，"
                "或者平台（如摩天轮这种二手票）此刻确实没有挂单。[/dim]"
            )
        return

    # 演出和列车共用同一套模型（「有没有票」本质是同一个问题），但**表头不能共用**：
    # 给一场演唱会显示「到达 / 历时」两列全是「-」，是把复用做得过了头。
    rail = snapshot.platform == "rail12306"
    table = Table(title=f"{task_name}　（{len(snapshot.trains)} 条）", show_lines=False)
    table.add_column("编号", style="bold")
    if rail:
        table.add_column("出发")
        table.add_column("到达")
        table.add_column("历时")
    else:
        table.add_column("时间")
        table.add_column("场馆")
    table.add_column("余票", overflow="fold")

    available = 0
    for _code, train in sorted(
        snapshot.trains.items(), key=lambda kv: (kv[1].depart_time or "99:99", kv[0])
    ):
        seats = [
            s
            for s in train.seats.values()
            if (not watch_types or s.seat_type in watch_types)
        ]
        if any(s.available for s in seats):
            available += 1
        seat_text = "　".join(
            f"[green]{s.label}[/green]" if s.available else f"[dim]{s.label}[/dim]"
            for s in seats
        ) or "[dim]—[/dim]"
        if rail:
            table.add_row(
                train.train_code,
                train.depart_time or "-",
                train.arrive_time or "-",
                train.duration or "-",
                seat_text,
            )
        else:
            table.add_row(
                train.train_code,
                train.depart_time or "-",
                train.to_station or "-",
                seat_text,
            )

    console.print(table)
    console.print(f"其中 [green]{available}[/green] 条有你关注的票档有余量。")



@app.command()
def check(
    config: ConfigOpt = Path("tasks.yaml"),
    only: Annotated[str | None, typer.Option("--only", help="只跑指定任务 id")] = None,
    as_json: Annotated[
        bool,
        typer.Option(
            "--json",
            help="输出结构化 JSON，便于用脚本/jq 自己筛选要盯哪几趟车",
        ),
    ] = False,
    with_price: Annotated[
        bool,
        typer.Option(
            "--with-price",
            help="额外补查票价（每趟命中 watch 的车多一次请求，建议只在手动查询时用）",
        ),
    ] = False,
    verbose: Annotated[bool, typer.Option("-v", "--verbose")] = False,
) -> None:
    """单次查询并打印余票，不写库、不推送。用来调通配置。

    加 ``--json`` 会输出机器可读的结构化结果（含乘车日期、每趟车的时间/历时/
    各席别余票，以及是否命中当前 watch 规则），方便先「看一遍再挑」。

    例：只看二等座有余票、且 12 点前出发的车
    ::

        radar check --json | jq '[.tasks[].trains[]
            | select(.depart < "12:00")
            | select(.seats["二等座"].available)]'
    """
    _setup_logging(verbose)  # 日志走 stderr，不会污染 stdout 的 JSON
    cfg = _load(config)
    tasks = _picker(cfg, only)
    collected: list[dict[str, Any]] = []

    async def _run() -> None:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
            headers={"Accept-Language": "zh-CN,zh;q=0.9"},
        ) as client:
            for task in tasks:
                adapter = create_adapter(task.adapter, cfg.credentials_for(task))
                if not as_json:
                    console.rule(f"[bold]{task.display_name}[/bold]　{task.adapter}")
                try:
                    snapshot = await adapter.fetch(task, client)
                except Exception as exc:
                    if as_json:
                        collected.append(
                            {"task_id": task.id, "task_name": task.display_name, "error": str(exc)}
                        )
                        continue
                    err_console.print(f"[red]抓取失败：{exc}[/red]")
                    continue
                if with_price:
                    # 只查命中 watch 的车次，不是全部——否则 warn 多的任务会打爆请求数
                    codes = {
                        c
                        for c, t in snapshot.trains.items()
                        if task.watch.matches_train(c)
                        and task.watch.matches_depart_time(t.depart_time)
                    }
                    try:
                        snapshot = await adapter.enrich_prices(snapshot, task, client, codes)
                    except Exception as exc:
                        err_console.print(f"[yellow]补查票价失败（余票数据不受影响）：{exc}[/yellow]")
                if as_json:
                    collected.append(snapshot_to_dict(task, snapshot))
                else:
                    _render_snapshot(snapshot, task.display_name, task.watch.seat_types)

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run())

    if as_json:
        # ensure_ascii=False：中文车站名/席别直接可读，也方便眼睛扫一遍
        typer.echo(json.dumps({"tasks": collected}, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# debug-raw：字段漂移排查
# ---------------------------------------------------------------------------


@app.command("debug-raw")
def debug_raw(
    config: ConfigOpt = Path("tasks.yaml"),
    only: Annotated[str | None, typer.Option("--only", help="只跑指定任务 id")] = None,
) -> None:
    """打印 12306 原始返回体的字段下标。

    余票接口没有官方文档，字段位置靠社区逆向。当余票解析不出东西时，
    用这个命令看真实字段，然后到 tasks.yaml 的 params 里用
    train_fields / seat_fields 覆盖，不必改代码。
    """
    _setup_logging(False)
    cfg = _load(config)
    tasks = [t for t in _picker(cfg, only) if t.adapter == "rail12306"]
    if not tasks:
        err_console.print("[yellow]没有启用中的 rail12306 任务。[/yellow]")
        raise typer.Exit(code=1)

    from .adapters.rail12306 import Rail12306Adapter

    async def _run() -> None:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0), follow_redirects=True) as client:
            for task in tasks:
                adapter = create_adapter(task.adapter, cfg.credentials_for(task))
                if not isinstance(adapter, Rail12306Adapter):
                    continue
                data = await adapter.fetch_raw(task, client)
                rows = (data.get("data") or {}).get("result") or []
                console.rule(f"[bold]{task.display_name}[/bold]　原始结果 {len(rows)} 行")
                if not rows:
                    console.print("[yellow]返回 0 行。[/yellow]")
                    continue
                for i, field in enumerate(rows[0].split("|")):
                    console.print(f"[cyan]{i:>3}[/cyan]  {field!r}")
                console.print("\n[dim]对照 radar/adapters/rail12306.py 里的"
                              " TRAIN_FIELDS / SEAT_FIELDS 核对下标。[/dim]")

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# notify-test
# ---------------------------------------------------------------------------


@app.command("notify-test")
def notify_test(config: ConfigOpt = Path("tasks.yaml")) -> None:
    """给所有已启用的通知渠道发一条测试消息，验证密钥是否配好。"""
    _setup_logging(False)
    cfg = _load(config)

    async def _run() -> None:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            notifiers = build_notifiers(cfg, client)
            if not notifiers:
                err_console.print(
                    "[yellow]没有可用的通知渠道。请检查 .env 里的密钥和 tasks.yaml 的 notify 段。[/yellow]"
                )
                raise typer.Exit(code=1)
            message = Message(
                title="【余票监控】测试推送",
                body=(
                    "这是一条测试消息。\n\n"
                    "收到它说明该渠道的密钥配置正确，余票提醒能正常送达。\n\n"
                    "本工具只做余票提醒，不会替你下单——"
                    "收到提醒后请自行前往官方渠道购买。"
                ),
            )
            from .notifier import NotifierHub

            hub = NotifierHub(notifiers)
            try:
                failures = await hub.send(message)
            except RuntimeError as exc:
                err_console.print(f"[red]{exc}[/red]")
                await hub.aclose()
                raise typer.Exit(code=1) from exc
            if failures:
                for name, error in failures.items():
                    err_console.print(f"[yellow]渠道 {name} 失败：{error}[/yellow]")
            else:
                console.print("[green]全部渠道推送成功。[/green]")
            await hub.aclose()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# preview
# ---------------------------------------------------------------------------


@app.command()
def preview(
    config: ConfigOpt = Path("tasks.yaml"),
    only: Annotated[str | None, typer.Option("--only", help="只预览指定任务 id")] = None,
    no_price: Annotated[
        bool,
        typer.Option("--no-price", help="不补查票价（省掉每趟车一次额外请求）"),
    ] = False,
    verbose: Annotated[bool, typer.Option("-v", "--verbose")] = False,
) -> None:
    """预览推送长什么样，不用等真实放票。

    真实抓取一次，然后把「上一轮快照」假定为全部无票，
    再走一遍和 radar run 完全相同的变化检测、补价、消息渲染。

    唯一伪造的就是那个旧快照——请求、解析、归一、diff、补价、渲染全是真的。
    觉得内容太杂就收窄任务的 watch.train_codes / watch.seat_types。

    默认会为命中的车次补查票价（每趟车一次请求），和 radar run 真正推送时
    的行为一致；不想要这些请求就加 ``--no-price``。
    """
    _setup_logging(verbose)
    cfg = _load(config)
    tasks = _picker(cfg, only)

    async def _run() -> None:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
            headers={"Accept-Language": "zh-CN,zh;q=0.9"},
        ) as client:
            for task in tasks:
                adapter = create_adapter(task.adapter, cfg.credentials_for(task))
                console.rule(f"[bold]{task.display_name}[/bold]　{task.adapter}")
                try:
                    current = await adapter.fetch(task, client)
                except Exception as exc:
                    err_console.print(f"[red]抓取失败：{exc}[/red]")
                    continue

                # 伪造上一轮：所有单元的余票清零，模拟「刚才没票、现在放票了」
                previous = Snapshot(
                    task_id=current.task_id,
                    platform=current.platform,
                    captured_at=current.captured_at - timedelta(minutes=5),
                    # 参数指纹要和当前轮一致，否则语义上等于「换了一批货」，
                    # 以后若把指纹校验挪进 diff，预览会莫名其妙变成空
                    params_fingerprint=current.params_fingerprint,
                    trains={
                        code: dataclasses.replace(
                            train,
                            seats={
                                seat_type: SeatAvailability(
                                    seat_type=seat_type,
                                    raw="无",
                                    count=0,
                                    available=False,
                                )
                                for seat_type in train.seats
                            },
                        )
                        for code, train in current.trains.items()
                    },
                )

                changes = ChangeDetector.diff(previous, current, task.watch)
                console.print(
                    f"抓取到 {len(current.trains)} 个单元，"
                    f"按 watch 规则筛出 [bold]{len(changes)}[/bold] 条变化"
                )
                if not changes:
                    console.print(
                        "[yellow]没有可预览的内容。[/yellow]"
                        "[dim]放宽 watch.train_codes / seat_types，"
                        "或换一条临近发车的线路试试。[/dim]"
                    )
                    continue

                if not no_price:
                    # 和 radar run 一致：只在真要渲染通知时才补价
                    try:
                        current = await adapter.enrich_prices(
                            current,
                            task,
                            client,
                            {c.train_code for c in changes},
                        )
                    except Exception as exc:
                        err_console.print(f"[yellow]补查票价失败（余票部分照常预览）：{exc}[/yellow]")

                message = format_message(task, changes, current)
                console.print()
                console.print(
                    Panel(
                        Text(message.body),
                        title=f"[bold]{message.title}[/bold]",
                        subtitle="渠道实际收到的原文（Markdown）",
                        border_style="green",
                    )
                )

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run())


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


#: 状态播报里每个任务最多列几项「有票」明细。
#:
#: 取 2 而不是 5，是因为播报是**在手机上看**的，而结论只有第一行。
#: 实测过一屏放 5 条明细的后果：手机上「有票 193/211」这条结论被顶到
#: 看不见的地方，用户以为没抓��数据。**列得多不等于讲得清楚**——
#: 想知道全部随时可以 `radar status`，播报只负责把结论说清楚。
MAX_STATUS_ITEMS = 2

#: 每个车次最多列几个席别。同上：手机上一行别超过一屏的三分之一。
MAX_SEATS_PER_TRAIN = 2


def _seat_text(seat: Any) -> str:
    """席别的一句话描述。

    优先用平台原话：大麦的「热卖」、12306 的「有」都是用户自己就能搜到的词，
    换成「有票」反而丢信息。只有拿到确切张数时才改用「N 张」。
    """
    if seat.count is not None:
        return f"{seat.seat_type} {seat.count} 张"
    if seat.raw:
        return f"{seat.seat_type} {seat.raw}"
    return f"{seat.seat_type} 有票"


def _status_block(task: Any, snapshot: Any | None) -> list[str]:
    """把一个任务的**当前**状态压成几行。

    为什么播报里非要带状态，而不是只列任务名：
    监控的规则是「变化才提醒」，于是「一开始就有票」这件事永远等不到推送——
    用户会以为大麦那条没接上。把当前状态直接摊开，疑心病才好治。
    """
    lines = [f"**{task.display_name}**"]
    if snapshot is None:
        lines.append("　本轮没抓到数据（网络或凭据问题），下一轮会自动重试")
        return lines

    context = snapshot.context_line()
    if context:
        lines.append(f"　{context}")

    trains = snapshot.trains
    if not trains:
        lines.append("　没有可售项")
        return lines

    hot: list[str] = []
    for code, train in trains.items():
        available = [s for s in train.seats.values() if s.available]
        if not available:
            continue
        when = f"　{train.depart_time}" if train.depart_time else ""
        detail = "、".join(_seat_text(s) for s in available[:MAX_SEATS_PER_TRAIN])
        hot.append(f"　· {code}{when}　{detail}")

    if hot:
        # 结论加粗并放在明细**前面**：手机上先看到的应该是「有票还是没票」，
        # 而不是一屏车次。明细是给想看细节的人解馋的，不是主体。
        lines.append(f"　**有票：{len(hot)}/{len(trains)} 项**")
        lines.extend(hot[:MAX_STATUS_ITEMS])
        if len(hot) > MAX_STATUS_ITEMS:
            lines.append(
                f"　　…… 另有 {len(hot) - MAX_STATUS_ITEMS} 项，"
                "跑 `radar status` 看全部"
            )
    else:
        lines.append(f"　**暂无余票**（{len(trains)} 项全部无票）")
    return lines


def _status_listing(tasks: list[Any], snapshots: dict[str, Any] | None) -> str:
    """任务清单。给了快照就带状态，没给就退化成纯名单。"""
    if not snapshots:
        return "\n".join(f"· {t.display_name}" for t in tasks)
    blocks: list[str] = []
    for task in tasks:
        blocks.extend(_status_block(task, snapshots.get(task.id)))
    return "\n".join(blocks)


def _startup_message(tasks: list[Any], snapshots: dict[str, Any] | None = None) -> Message:
    """启动播报的内容。

    抽成纯函数是为了能单测：这段话要解决的正是「安静是不是代表挂了」，
    说得含糊就等于没说，所以关键那两句值得钉住。
    """
    return Message(
        title="【余票监控】已启动",
        body=(
            f"正在盯 {len(tasks)} 个任务：\n\n"
            + _status_listing(tasks, snapshots)
            + "\n\n**之后只有检测到余票变化才会再提醒你。**"
            "\n长时间安静 = 没有变化，不是程序停了。"
            "\n关掉命令行窗口就会停止监控。"
        ),
        url=tasks[0].link if tasks else None,
    )


def _status_message(tasks: list[Any], snapshots: dict[str, Any]) -> Message:
    """``radar status --push`` 的内容：此刻各任务是什么状态。

    和启动播报刻意分开：那条的尾句是「安静 = 没变化」，
    这条只是回答「现在到底有没有票」，两句混用会互相削弱。
    """
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    return Message(
        title="【余票状态】当前快照",
        body=(
            f"{len(tasks)} 个任务　抓取时间 {stamp}\n\n"
            + _status_listing(tasks, snapshots)
            + "\n\n这是**此刻**的状态，不是变化提醒。"
            "\n只有状态发生变化时，监控才会主动叫你。"
        ),
        url=tasks[0].link if tasks else None,
    )


async def _warm_up(monitor: Any, tasks: list[Any]) -> dict[str, Any]:
    """播报前先各抓一轮。

    两个作用：① 建立 diff 基线（首轮本来就要抓，这不算额外开销）；
    ② 拿到「当前有没有票」——「本来就有票」不产生任何变更事件，
    不主动抓一次就永远播报不出来。

    单个任务失败不影响其他任务，也不影响播报本身：失败的那条会显示
    「本轮没抓到数据」，比整条播报发不出去有用得多。
    """
    results = await asyncio.gather(
        *(monitor.poll_once(t, notify=False) for t in tasks), return_exceptions=True
    )
    snapshots: dict[str, Any] = {}
    for task, result in zip(tasks, results, strict=True):
        if isinstance(result, BaseException):
            err_console.print(f"[yellow]{task.display_name} 首次抓取失败：{result}[/yellow]")
            # 必须把失败记进健康统计：预热绕开了 `_poll_with_backoff`，
            # 不记的话 `radar health` 会把这个抓失败的任务显示成「正常」——
            # 那正是本项目招牌功能要消灭的「抓错了还报平安」。
            monitor.record_failure(task.id, result)
            snapshots[task.id] = None
            continue
        snapshot = monitor.store.latest_snapshot(task.id)
        snapshots[task.id] = snapshot
        if snapshot is not None:
            # 这一轮刚抓的，让 run 的第一轮直接用它。
            # 不复用的话，平台级限流会把首轮推到一整个 interval 之后
            # （大麦 300 秒），「启动后一直没反应」的感觉就是这么来的。
            monitor.reuse_next(task.id, snapshot)
    return snapshots


@app.command()
def run(
    config: ConfigOpt = Path("tasks.yaml"),
    once: Annotated[
        bool, typer.Option("--once", help="每任务只跑一轮就退出（不重试，失败也退出）")
    ] = False,
    no_notify: Annotated[
        bool, typer.Option("--no-notify", help="不推送，只写库（观察模式）")
    ] = False,
    announce: Annotated[
        bool,
        typer.Option(
            "--announce",
            help="启动时先推一条「已启动」，用来确认手机端真的收得到提醒",
        ),
    ] = False,
    verbose: Annotated[bool, typer.Option("-v", "--verbose")] = False,
) -> None:
    """常驻监控余票变化，检测到关注的变化就推送。

    推送里会带上「乘车日期」和票价。票价是按车次单独请求拿的，
    所以只在**确实要发通知**时才补查（单次最多查 MAX_PRICE_LOOKUPS 趟车），
    常态轮询依然保持每轮 1 次请求。
    """
    _setup_logging(verbose)
    cfg = _load(config)
    tasks = cfg.enabled_tasks
    if not tasks:
        err_console.print("[yellow]没有启用的任务。[/yellow]")
        raise typer.Exit(code=1)

    from .engine import Monitor

    store = Store(cfg.storage.path)

    async def _run() -> None:
        async with Monitor(cfg, store) as monitor:
            if not once and not no_notify and not monitor.hub:
                err_console.print(
                    "[yellow]警告：没有可用通知渠道，余票变化只会写入数据库。[/yellow]"
                )
            console.print(
                Panel(
                    f"监控 {len(tasks)} 个任务，数据库 [bold]{cfg.storage.path}[/bold]\n"
                    "[dim]首轮只建立基线，不会推送。检测到变化后才会提醒。[/dim]\n"
                    "[dim]按 Ctrl+C 退出。[/dim]",
                    title="ticket-radar 已启动",
                    border_style="green",
                )
            )
            if announce and not no_notify and monitor.hub:
                # 「安静」是这个程序的常态（没变化就不推），于是「它到底还活着吗」
                # 就成了一个真实困扰——尤其刚启动的那几分钟。
                # 主动报一次到，把「在跑」和「没变化」这两件事分开。
                #
                # 但光报「活着」还不够：大麦这类平台要盯的是「缺货 → 热卖」，
                # 如果一开始就在卖，用户永远等不到那条推送，只会觉得没接上。
                # 所以播报前先真抓一轮，把各任务的当前状态一并摊开。
                try:
                    snapshots = await _warm_up(monitor, tasks)
                    await monitor.hub.send(_startup_message(tasks, snapshots))
                except Exception as exc:  # 播报失败不能拖垮监控本身
                    err_console.print(f"[yellow]启动播报没发出去（不影响监控）：{exc}[/yellow]")
            with contextlib.suppress(asyncio.CancelledError):
                await monitor.run(once=once, notify=not no_notify)

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        console.print("\n[dim]已停止。[/dim]")
    finally:
        store.close()


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@app.command()
def status(
    config: ConfigOpt = Path("tasks.yaml"),
    only: Annotated[str | None, typer.Option("--only", help="只看指定任务 id")] = None,
    push: Annotated[
        bool, typer.Option("--push", help="把这份状态推到通知渠道（手机上就能看）")
    ] = False,
    verbose: Annotated[bool, typer.Option("-v", "--verbose")] = False,
) -> None:
    """看一眼各任务**此刻**的余票状态。

    和 ``check`` 的分工：``check`` 是调配置用的，只看终端、绝不写库不推送；
    ``status`` 回答的是「现在到底有没有票」，加 ``--push`` 直接发到手机上。

    为什么需要它：常驻监控只在**变化**时开口，「一开始就有票」和
    「一直有票」这两件事它永远不会说——想确认就得主动问一次。

    注意它同样会抓一轮（顺便成为监控的对比基线），所以别当成免费操作狂刷。
    """
    _setup_logging(verbose)
    cfg = _load(config)
    tasks = _picker(cfg, only)
    store = Store(cfg.storage.path)

    async def _run() -> None:
        from .engine import Monitor

        async with Monitor(cfg, store) as monitor:
            snapshots = await _warm_up(monitor, tasks)

            for task in tasks:
                # 第一行是任务名，Panel 标题已经有了，去掉免得重复
                lines = _status_block(task, snapshots.get(task.id))[1:]
                console.print(
                    Panel(
                        "\n".join(line.replace("**", "") for line in lines),
                        title=f"[bold]{task.display_name}[/bold]",
                        border_style="cyan",
                    )
                )

            if not push:
                console.print("[dim]加 --push 可以把这份状态发到手机上。[/dim]")
                return
            if not monitor.hub:
                err_console.print("[yellow]没有可用通知渠道，先看 `radar channels`。[/yellow]")
                return
            try:
                await monitor.hub.send(_status_message(tasks, snapshots))
                console.print("[green]已推送到通知渠道。[/green]")
            except Exception as exc:
                err_console.print(f"[red]推送失败：{exc}[/red]")

    try:
        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(_run())
    finally:
        store.close()


# ---------------------------------------------------------------------------
# health：监控自己的健康自检
# ---------------------------------------------------------------------------


@app.command()
def health(
    config: ConfigOpt = Path("tasks.yaml"),
    only: Annotated[str | None, typer.Option("--only", help="只看指定任务 id")] = None,
    push: Annotated[
        bool, typer.Option("--push", help="把健康汇总推到通知渠道（手机上就能看）")
    ] = False,
    verbose: Annotated[bool, typer.Option("-v", "--verbose")] = False,
) -> None:
    """检查**监控自身**是否健康：每个任务最近一轮抓得可信吗、有没有连续报错。

    和 ``status`` 的分工：``status`` 回答「现在有没有票」，
    ``health`` 回答「监控还活着吗、它抓到的数据靠不靠谱」。

    它会真抓一轮（顺便成为对比基线），对每轮结果做语义自检，
    然后列出每个任务：最近成功时间、条目数、自检结论、连续失败次数。
    ``--push`` 把这份汇总发到手机——等价于手动要一次健康心跳。
    """
    _setup_logging(verbose)
    cfg = _load(config)
    tasks = _picker(cfg, only)
    store = Store(cfg.storage.path)

    async def _run() -> None:
        from .engine import Monitor

        async with Monitor(cfg, store) as monitor:
            # 真抓一轮：建立基线 + 触发自检（自检结论写进 monitor 的健康统计）
            await _warm_up(monitor, tasks)
            rows = monitor.health_summary()

            table = Table(title="监控健康检查", show_lines=False)
            table.add_column("任务", style="bold")
            table.add_column("状态")
            table.add_column("最近成功")
            table.add_column("条目")
            for row in rows:
                table.add_row(
                    row.name,
                    row.status_line().replace("[green]", "").replace("[/green]", "")
                    .replace("[yellow]", "").replace("[/yellow]", "")
                    .replace("[red]", "").replace("[/red]", ""),
                    row.last_success.astimezone().strftime("%m-%d %H:%M")
                    if row.last_success else "—",
                    str(row.items),
                )
            console.print(table)

            if not push:
                console.print("[dim]加 --push 可以把这份汇总发到手机上。[/dim]")
                return
            if not monitor.hub:
                err_console.print("[yellow]没有可用通知渠道，先看 `radar channels`。[/yellow]")
                return
            try:
                await monitor.hub.send(_health_message(rows))
                console.print("[green]已推送健康汇总。[/green]")
            except Exception as exc:
                err_console.print(f"[red]推送失败：{exc}[/red]")

    try:
        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(_run())
    finally:
        store.close()


def _health_message(rows: list[Any]) -> Message:
    from .engine import format_health_message

    return format_health_message(rows)


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------

#: 体检结果到颜色的映射。**「未检查」必须有自己的颜色**——
#: 把它涂成绿色就等于用体检报告掩盖盲区。
_DOCTOR_STYLE = {
    "ok": "[green]正常[/green]",
    "auth": "[yellow]凭据问题[/yellow]",
    "broken": "[red]接口异常[/red]",
    "unsupported": "[dim]平台不支持[/dim]",
    "unknown": "[dim]未检查[/dim]",
}


@app.command()
def doctor(
    config: ConfigOpt = Path("tasks.yaml"),
    platform: Annotated[
        str | None, typer.Option("--platform", help="只体检这一个适配器")
    ] = None,
    platforms_only: Annotated[
        bool, typer.Option("--platforms-only", help="只做平台探活，不实测任务")
    ] = False,
) -> None:
    """真发请求做体检：任务抓不抓得到、平台连不连得上、凭据还有效没。

    和 ``radar onboard`` 的分工很明确：

    * ``onboard`` 只看**配置**——凭据填了没有。它会说「✓ 已填写」，
      但大麦那种几小时就过期的 Cookie，填着也能早就失效了。
    * ``doctor`` 真的去试一次。所以它可能消耗平台的一次请求配额，
      **别写进每分钟跑的 cron**。

    体检分两层：

    * **任务实测**：拿你配置里的真实参数抓一轮，直接回答「我这个任务还能用吗」。
      这一层不落库，不会污染你的余票历史。
    * **平台探活**：对没有任务覆盖的平台发一个最小请求（拉站表 / 搜一个词 /
      取一次 token），回答「这个平台本身还连得上吗」。代价都压到最低，
      大麦甚至**不联网**就能算出 Cookie 什么时候过期。
    """
    from .adapters import create_adapter, list_adapters
    from .adapters.base import HEALTH_OK, AdapterError, AuthError

    cfg = _load(config)
    enabled = [t for t in cfg.tasks if t.enabled]
    if platform:
        enabled = [t for t in enabled if t.adapter == platform]

    rows: list[tuple[str, str, str]] = []

    def _add(label: str, status: str, note: str) -> None:
        rows.append((label, status, note))

    async def _run() -> None:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            if not platforms_only:
                for t in enabled:
                    label = f"任务 {t.display_name}"
                    try:
                        adapter = create_adapter(t.adapter, cfg.credentials_for(t))
                        snap = await adapter.fetch(t, client)
                        _add(label, HEALTH_OK, f"抓到 {len(snap.trains)} 个条目")
                    except AuthError as exc:
                        _add(label, "auth", str(exc)[:100])
                    except AdapterError as exc:
                        # 凭据没填 / Cookie 过期 都可能走到这里。分档靠异常类型，
                        # 猜不出来就别猜——原话给用户看。
                        _add(label, "broken", str(exc)[:100])
                    except Exception as exc:
                        _add(label, "broken", f"{type(exc).__name__}: {exc}"[:100])
                    # 体检也是请求，得给平台留喘息。别把配额一次打光。
                    await asyncio.sleep(0.4)

            covered = {t.adapter for t in enabled}
            for name in list_adapters():
                if name in covered or (platform and name != platform):
                    continue
                cap = create_adapter(name).capability
                if not cap.available():
                    # 接不了的平台不请求，也不假装检查过
                    _add(f"平台 {name}", "unsupported", cap.limitation)
                    continue
                # 没有任务时按同名凭据组回退——`credentials: amadeus` 是惯例
                creds = dict(cfg.credentials.get(name, {}))
                try:
                    status, note = await create_adapter(name, creds).doctor(client)
                except Exception as exc:
                    status, note = "broken", f"{type(exc).__name__}: {exc}"[:100]
                _add(f"平台 {name}", status, note)
                await asyncio.sleep(0.4)

    try:
        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(_run())
    except FileNotFoundError as exc:
        err_console.print(f"[yellow]{exc}[/yellow]")
        raise typer.Exit(code=1) from exc

    if not rows:
        console.print("[yellow]没有可体检的对象：配置里没有启用的任务，也没有已注册适配器。[/yellow]")
        return

    table = Table(title="真发请求体检", show_lines=False)
    table.add_column("对象", style="bold")
    table.add_column("状态")
    table.add_column("说明")
    for label, status, note in rows:
        table.add_row(label, _DOCTOR_STYLE.get(status, status), note)
    console.print(table)

    bad = [r for r in rows if r[1] in ("auth", "broken")]
    if bad:
        console.print(
            f"[yellow]{len(bad)} 项需要处理。[/yellow] "
            "凭据问题按提示重登或补填；接口异常先降频再试，别绕过平台防护。"
        )
    console.print("[dim]体检不落库，不影响监控历史；但会消耗平台的请求配额。[/dim]")


# ---------------------------------------------------------------------------
# ask
# ---------------------------------------------------------------------------


@app.command()
def ask(
    text: Annotated[str, typer.Argument(help="用一句话或一条链接说出你想盯什么")],
    search: Annotated[
        bool, typer.Option("--search", help="进一步定位到具体场次（会请求平台）")
    ] = False,
    pick: Annotated[
        int | None, typer.Option("--pick", help="直接选第 N 个候选（从 1 开始）")
    ] = None,
) -> None:
    """把「你想盯什么」翻译成需求，定位到具体场次，并给出能直接用的配置。

    分两步，按需付费：

    * **不加 ``--search``**：只解析，**一个网络请求都不发**。
      随便试，不碰风控、不耗配额。
    * **加 ``--search``**：真的去搜平台，把候选场次列出来。
      只有一个候选时会自动**验证**它（用真实接口抓一次），
      并输出可以直接粘进 tasks.yaml 的片段。

    比如::

        radar ask 帮我盯一下十月十号陈粒深圳场那张票
        radar ask 陈粒深圳场 --search
        radar ask https://detail.damai.cn/item.htm?id=123456 --search
    """
    import httpx

    from .capability import category_label, route
    from .intent import parse_intent
    from .target import _city_matches as _city_ok
    from .target import resolve, to_yaml_fragment, verify

    parsed = parse_intent(text)
    routing = route(parsed.requirement())

    table = Table(title="需求解析", show_lines=False)
    table.add_column("项", style="bold")
    table.add_column("值")
    table.add_row("原文", parsed.raw)
    table.add_row("平台", parsed.platform or "—")
    if parsed.target_id:
        table.add_row("场次 id", f"{parsed.target_id}（写进 params 的 {parsed.id_key}）")
    table.add_row("关键词", parsed.keyword or "—")
    if parsed.route_from and parsed.route_to:
        table.add_row("线路", f"{parsed.route_from} → {parsed.route_to}")
    if parsed.city:
        table.add_row("城市", parsed.city)
    if parsed.date:
        table.add_row("日期", parsed.date)
    table.add_row("类别", category_label(parsed.category) if parsed.category else "未识别")
    table.add_row("置信度", f"{parsed.confidence:.0%}")
    console.print(table)

    for note in parsed.notes:
        console.print(f"[yellow]! {note}[/yellow]")

    console.print(f"\n{routing.explain()}")

    if not search:
        console.print(
            "\n[dim]加 --search 可以继续定位到具体场次。"
            "确认无误后把 id 填进 tasks.yaml 就能开始监控。[/dim]"
        )
        return

    async def _run() -> None:
        async with httpx.AsyncClient(timeout=httpx.Timeout(25.0)) as client:
            resolved = await resolve(client, parsed)

            for note in resolved.notes:
                console.print(f"[cyan]· {note}[/cyan]")
            for platform, err in resolved.failed:
                err_console.print(f"[yellow]{platform} 搜索失败：{err[:80]}[/yellow]")

            if not resolved.targets:
                if resolved.rejected:
                    console.print(
                        f"\n[yellow]搜到过 {len(resolved.rejected)} 条，但没有一条同时满足"
                        f"演出名和城市——所以不算找到。[/yellow]"
                    )
                    rt = Table(title="搜到但不匹配的（供你判断是不是关键词写错了）")
                    rt.add_column("平台")
                    rt.add_column("演出")
                    rt.add_column("地点")
                    rt.add_column("时间")
                    rt.add_column("不匹配的原因", style="dim")
                    want_city = parsed.city
                    for t in resolved.rejected[:8]:
                        why = []
                        if want_city and not _city_ok(t.city, want_city):
                            why.append(f"不在{want_city}")
                        if parsed.keyword and parsed.keyword.lower() not in t.label.lower():
                            why.append(f"名字不含「{parsed.keyword}」")
                        rt.add_row(t.platform, t.label, t.place, t.when, "、".join(why))
                    console.print(rt)
                else:
                    console.print(
                        "[yellow]没定位到具体目标。[/yellow]"
                        "可以在需求里补城市、日期，或直接贴一条演出详情链接。"
                    )
                return

            if len(resolved.targets) > 1:
                st = Table(title=f"候选场次（{len(resolved.targets)} 个）")
                st.add_column("#", style="bold")
                st.add_column("平台")
                st.add_column("id")
                st.add_column("场次")
                st.add_column("地点")
                st.add_column("时间")
                st.add_column("票价")
                st.add_column("状态")
                for i, t in enumerate(resolved.targets, 1):
                    st.add_row(
                        str(i), t.platform, t.target_id or "—",
                        t.label, t.place, t.when, t.price or "—", t.status or "—",
                    )
                console.print(st)

            # 选一个：显式 --pick 优先；只有一个候选时自动选。
            # 多个候选时**不替用户选**——选错场次比多问一句糟糕得多。
            chosen = None
            if pick is not None:
                if 1 <= pick <= len(resolved.targets):
                    chosen = resolved.targets[pick - 1]
                else:
                    err_console.print(f"[yellow]--pick 要在 1~{len(resolved.targets)} 之间[/yellow]")
                    return
            elif resolved.unique:
                chosen = resolved.targets[0]

            if chosen is None:
                console.print(
                    f"[dim]有 {len(resolved.targets)} 个候选，"
                    f"确认是哪个之后加 --pick N（比如 --pick 1）。[/dim]"
                )
                return

            console.print(f"\n[bold]选中：[/bold]{chosen.title}")
            ok, note = await verify(client, chosen)
            if not ok:
                err_console.print(f"[red]验证失败：{note}[/red]")
                err_console.print(
                    "[yellow]这个目标盯不到。多半是 id 不对（平台搜索索引会滞后），"
                    "或者该场次已经结束。[/yellow]"
                )
                return
            console.print(f"[green]{note}[/green]")
            console.print("\n[bold]可以粘进 tasks.yaml 的片段：[/bold]")
            console.print(to_yaml_fragment(chosen))

    try:
        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(_run())
    except Exception as exc:
        err_console.print(f"[red]定位失败：{exc}[/red]")


# ---------------------------------------------------------------------------
# watch
# ---------------------------------------------------------------------------


@app.command()
def watch(
    text: Annotated[str, typer.Argument(help="用一句话或一条链接说出你想盯什么")],
    config: ConfigOpt = Path("tasks.yaml"),
    pick: Annotated[
        int | None, typer.Option("--pick", help="多个候选时选第 N 个（从 1 开始）")
    ] = None,
    interval: Annotated[
        int | None, typer.Option("--interval", help="轮询间隔秒数；不给就按平台选一个稳妥的")
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="只打印要写的内容，不动文件")
    ] = False,
) -> None:
    """一句话 → 找到场次 → 验证 → 写进配置。

    这是 ``radar ask --search`` 的完整版：定位完之后直接把任务写进
    tasks.yaml，你不用碰 YAML。

    它只做这些事：解析需求、搜平台、消歧、真抓一次验证、追加一条任务。
    **不会**自动启动监控——``radar run`` 是常驻进程，替你启动会让命令
    看起来像卡住了。想直接跑就 ``radar watch ... && radar run``。

    写入是**文本插入**，你原有的注释和配置一个字都不动；重复 id 会被拒绝。
    """
    import httpx

    from .capability import route
    from .intent import parse_intent
    from .target import _city_matches as _city_ok
    from .target import resolve, verify
    from .watcher import append_task, build_task_block, suggest_interval

    parsed = parse_intent(text)
    routing = route(parsed.requirement())

    console.print(f"[bold]需求：[/bold]{parsed.raw}")
    detail = "　".join(
        x
        for x in (
            f"关键词 {parsed.keyword}" if parsed.keyword else "",
            f"城市 {parsed.city}" if parsed.city else "",
            f"线路 {parsed.route_from}→{parsed.route_to}"
            if parsed.route_from and parsed.route_to
            else "",
            f"日期 {parsed.date}" if parsed.date else "",
        )
        if x
    )
    if detail:
        console.print(f"[dim]{detail}[/dim]")
    console.print(f"{routing.explain()}\n")

    outcome = {"ok": False, "target": None, "note": ""}

    async def _run() -> None:
        async with httpx.AsyncClient(timeout=httpx.Timeout(25.0)) as client:
            resolved = await resolve(client, parsed)
            for note in resolved.notes:
                console.print(f"[cyan]· {note}[/cyan]")

            if not resolved.targets:
                if resolved.rejected:
                    console.print(
                        f"\n[yellow]搜到过 {len(resolved.rejected)} 条，但没有一条同时满足"
                        "演出名和城市。[/yellow]"
                    )
                    for t in resolved.rejected[:5]:
                        why = []
                        if parsed.city and not _city_ok(t.city, parsed.city):
                            why.append(f"不在{parsed.city}")
                        if parsed.keyword and parsed.keyword.lower() not in t.label.lower():
                            why.append(f"名字不含「{parsed.keyword}」")
                        console.print(f"  · {t.platform}　{t.label}　{'、'.join(why)}")
                    console.print(
                        "\n[dim]演出名在平台上是外文时，用 `radar find` 直接拿 id 再看这段。[/dim]"
                    )
                else:
                    console.print("[yellow]没定位到目标。换个说法，或贴一条详情页链接。[/yellow]")
                return

            if len(resolved.targets) > 1:
                st = Table(title=f"候选场次（{len(resolved.targets)} 个）")
                st.add_column("#", style="bold")
                st.add_column("平台")
                st.add_column("场次")
                st.add_column("地点")
                st.add_column("时间")
                st.add_column("票价")
                for i, t in enumerate(resolved.targets, 1):
                    st.add_row(str(i), t.platform, t.label, t.place, t.when, t.price or "—")
                console.print(st)

            chosen = None
            if pick is not None:
                if 1 <= pick <= len(resolved.targets):
                    chosen = resolved.targets[pick - 1]
                else:
                    err_console.print(f"[yellow]--pick 要在 1~{len(resolved.targets)} 之间[/yellow]")
                    return
            elif resolved.unique:
                chosen = resolved.targets[0]

            if chosen is None:
                console.print(
                    f"\n[yellow]{len(resolved.targets)} 个候选，得先说是哪个。[/yellow]"
                    f"加 --pick N（1~{len(resolved.targets)}）再跑一次。"
                )
                return

            console.print(f"\n[bold]选中：[/bold]{chosen.title}")
            ok, note = await verify(client, chosen)
            if not ok:
                err_console.print(f"[red]验证失败：{note}[/red]")
                err_console.print(
                    "[yellow]没有写进配置。id 可能是错的（平台搜索索引会滞后），"
                    "或者场次已经结束。[/yellow]"
                )
                return
            console.print(f"[green]{note}[/green]")
            outcome["ok"] = True
            outcome["target"] = chosen
            outcome["note"] = note

    try:
        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(_run())
    except Exception as exc:
        err_console.print(f"[red]定位失败：{exc}[/red]")
        return

    target = outcome["target"]
    if not outcome["ok"] or target is None:
        return

    gap = interval or suggest_interval(parsed, target)
    block, task_id = build_task_block(target, interval=gap)

    console.print(f"\n[bold]将追加到 {config}：[/bold]")
    console.print(f"[dim]{block}[/dim]")

    if dry_run:
        console.print("[dim]--dry-run：没有写文件。[/dim]")
        return

    path = Path(config)
    if not path.exists():
        err_console.print(f"[yellow]{config} 不存在，先跑 `radar init` 生成。[/yellow]")
        raise typer.Exit(code=1)

    result = append_task(
        path.read_text(encoding="utf-8"), block, task_id, path=str(path)
    )
    if not result.ok:
        err_console.print(f"[red]没写入：{result.reason}[/red]")
        if result.line:
            err_console.print(f"  已在第 {result.line} 行找到同名任务")
        raise typer.Exit(code=1)

    path.write_text(result.text, encoding="utf-8")
    console.print(f"\n[green]已写入 {path} 第 {result.line} 行[/green]（id: {task_id}）")
    console.print("原有的注释和配置都没动。")
    console.print(
        f"\n下一步：\n"
        f"  radar check -c {config} --only {task_id}   # 先跑一次确认\n"
        f"  radar run   -c {config}                    # 开始监控"
    )


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------


@app.command()
def serve(
    config: ConfigOpt = Path("tasks.yaml"),
    host: Annotated[str, typer.Option("--host", help="监听地址；默认只有本机能访问")] = "127.0.0.1",
    port: Annotated[int, typer.Option("-p", "--port", help="监听端口")] = 8787,
    points: Annotated[
        int, typer.Option("--points", help="曲线最多回溯多少条快照（默认 300）")
    ] = 300,
    browser: Annotated[
        bool, typer.Option("--browser/--no-browser", help="启动后自动打开浏览器")
    ] = True,
) -> None:
    """打开本地看板：看余票曲线，点选车次生成配置。

    看板**只读**：打开页面不会向任何平台发请求，唯一会发请求的是
    你点「立即扫描」。它默认只监听 127.0.0.1，且不做鉴权——
    读的是你自己的余票历史，不该默认暴露到局域网上。

    真想让同网段的手机也能看，再加 ``--host 0.0.0.0``；
    但要清楚那意味着同网段任何人都能读到你的查询配置。
    """
    _setup_logging(False)
    cfg = _load(config)

    from .serve import Board, build_server

    store = Store(cfg.storage.path)
    board = Board(config=cfg, store=store, config_path=config, points=points)
    try:
        httpd = build_server(board, host, port)
    except OSError as exc:
        store.close()
        err_console.print(f"[red]无法监听 {host}:{port} —— {exc}[/red]")
        err_console.print("端口多半被占了，换一个： [bold]radar serve --port 8788[/bold]")
        raise typer.Exit(code=1) from exc

    shown_host = "127.0.0.1" if host in ("0.0.0.0", "") else host
    url = f"http://{shown_host}:{port}/"
    lines = [
        f"看板地址　[bold]{url}[/bold]",
        f"[dim]配置 {config}　数据库 {cfg.storage.path}[/dim]",
        "[dim]看板只读；唯一会向平台发请求的是你点「立即扫描」。[/dim]",
    ]
    if host not in ("127.0.0.1", "localhost"):
        lines.append(
            "[yellow]注意：--host 已放开，同网段任何人都能访问，且没有鉴权。[/yellow]"
        )
    lines.append("[dim]按 Ctrl+C 停止。[/dim]")
    console.print(Panel("\n".join(lines), title="ticket-radar 看板", border_style="blue"))

    if browser:
        with contextlib.suppress(Exception):
            webbrowser.open(url)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        console.print("\n[dim]看板已停止。[/dim]")
    finally:
        httpd.server_close()
        store.close()


# ---------------------------------------------------------------------------
# history
# ---------------------------------------------------------------------------


def _local_time(value: Any) -> str:
    """把库里存的 UTC 时间转成本地时间再显示。

    入库统一用 UTC（跨机器、跨时区都不歧义），但**给人看的必须转本地**：
    否则 UTC+8 的用户会看到一条写着 8 小时前的「历史」，
    第一反应是程序记错了或者坏了。

    解析失败就原样截断返回——展示层不该因为一个脏时间戳直接崩掉。
    """
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)[:19].replace("T", " ")
    if parsed.tzinfo is None:
        # 老库里的时间戳可能没带时区，按 UTC 解释（这正是入库时的约定）
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S")


@app.command()
def history(
    config: ConfigOpt = Path("tasks.yaml"),
    only: Annotated[str | None, typer.Option("--only", help="只看指定任务")] = None,
    limit: Annotated[int, typer.Option("-n", "--limit", help="显示条数")] = 30,
    days: Annotated[int | None, typer.Option("--days", help="只看最近 N 天")] = None,
) -> None:
    """查看余票变更历史。"""
    cfg = _load(config)
    store = Store(cfg.storage.path)
    try:
        rows = store.history(task_id=only, limit=limit, days=days)
    finally:
        store.close()

    if not rows:
        console.print("[dim]暂无变更记录。[/dim]")
        return

    table = Table(title=f"余票变更历史（最近 {len(rows)} 条）")
    table.add_column("时间")
    table.add_column("任务")
    table.add_column("编号", style="bold")
    table.add_column("票档")
    table.add_column("变化")
    table.add_column("已推送")

    for r in rows:
        before = r["before_count"] or 0
        after = r["after_count"] or 0
        color = "green" if after > before else "dim"
        table.add_row(
            _local_time(r["detected_at"]),
            r["task_id"],
            r["train_code"],
            r["seat_type"],
            f"[{color}]{before} → {after}[/{color}]  {r['kind']}",
            "✓" if r["notified"] else "—",
        )
    console.print(table)


# ---------------------------------------------------------------------------
# version
# ---------------------------------------------------------------------------


@app.command()
def version() -> None:
    """显示版本。"""
    console.print(f"ticket-radar {__version__}")


def main() -> None:  # pragma: no cover
    # 先加载 .env：models / infer / channels / adapters 这些命令不经过
    # load_config，但也需要读到 LLM_API_KEY、NTFY_TOPIC 之类的值。
    from .config import bootstrap_dotenv

    bootstrap_dotenv()
    try:
        app()
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = ["app", "main"]
