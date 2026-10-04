"""借一个真实浏览器，替用户完成「只有人能做的两步」：登录、找到接口地址。

为什么要有它
------------
票务平台的 Cookie 只能从登录态里来。之前把「开 F12 → 翻 Network →
从请求头里复制 Cookie」这一步丢给用户，对不熟悉开发者工具的人（也就是
大多数人）来说这一步根本走不通。而本项目的定位就是给普通人用的——
不能把最关键的一步卡在这儿。

所以反过来：**我们借一个浏览器去替用户拿**。用户只需要在弹出来的窗口里
登录，其余（取 Cookie、拼成请求头格式、写进 .env、顺手记下页面访问过
哪些接口）全由程序完成。Cookie 是浏览器自己交出来的，连 HttpOnly 的
也在，比手工从 F12 里捞更完整。

登录窗口**刻意不是自动化的**（2026-09 改）
------------------------------------------
一开始这里是用 Playwright 启动浏览器让用户登录的。踩坑了：**阿里云滑块
会认出自动化浏览器**——用户反馈「我明明划到了指定位置，它就是说我位置不对」。
滑块失败不是手抖，是页面发现自己在被程序驱动。

正解不是「把滑块划得更准」，那等于去骗风控（本项目明确不做设备指纹伪装）。
正解是**别让登录这一步沾上自动化**：

1. 用 ``subprocess`` 直接拉起一个**普通 Edge**（不加任何自动化参数，
   没有 ``navigator.webdriver``，没有 CDP）。用户在里面正常登录，
   滑块和平时一样好使；
2. 用户关掉窗口后，再用 Playwright 以**无头**方式打开同一个 profile，
   让浏览器把 Cookie 交出来。这一步没有人在操作，也就无所谓自动化。

登录由人完成、Cookie 由浏览器自己给出——我们只是不再给用户的登录使绊子。

几条刻意的选择
--------------
**复用系统已装的 Edge**，不下载 Chromium：省掉 500MB 和一个可能失败的下载步骤。

**持久 profile**：登录态留在项目目录下，同一个平台只要登录一次；
几个平台共用一个 profile，各记各的会话——这也是将来接教务系统的地基。

**强制直连**。会话里可能被注入 ``http_proxy`` 之类的环境变量（CI、沙箱、
公司代理都会这么干），Chromium 会照单全收，结果是连百度都打不开，
大麦直接返回 500。启动时剥掉这些变量并加 ``--no-proxy-server``。

**只报数量，不回显 Cookie 值**。终端回滚缓冲、日志、截图都不该出现登录态。

**不读用户日常浏览器的 Cookie 库**。Edge 的 Cookie 现在用应用绑定加密
（``Local State`` 里同时有 ``DPAPI`` 和 ``APPB`` 两套密钥，新 Cookie 走后者），
离线解不开；而且那是用户整个浏览器的数据，本项目只该碰自己这个 profile。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

#: 这些环境变量会让 Chromium 把所有请求塞进代理，必须剥掉
PROXY_ENV_KEYS = (
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
)

#: 浏览器 profile 默认落在 .env 旁边
PROFILE_RELATIVE = Path("data") / "browser-profile"

#: 抓到的请求里，这些关键词多半是埋点/监控，不是票档接口
NOISE_MARKERS = (
    "sensorsdata",
    "google-analytics",
    "googletagmanager",
    "doubleclick",
    "umeng",
    "cnzz",
    "growingio",
    "appsflyer",
    "monitor",
    "beacon",
    "arms.aliyuncs",
    "hm.baidu.com",
    "tlog",
)

#: 只有这两类请求才可能是数据接口
API_RESOURCE_TYPES = ("xhr", "fetch")


class BrowserUnavailable(RuntimeError):
    """没装 playwright，或者浏览器起不来。文案要能直接照做。"""


@dataclass
class CaptureResult:
    """一次抓取的收获。``cookies`` 是拼好的 Cookie 串，可能为空。"""

    site_url: str
    cookies: str = ""
    cookie_names: list[str] = field(default_factory=list)
    requests: list[str] = field(default_factory=list)

    @property
    def cookie_count(self) -> int:
        return len(self.cookie_names)


# ---------------------------------------------------------------------------
# 纯函数（好测，也不依赖浏览器）
# ---------------------------------------------------------------------------


def clean_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """复制一份环境变量，去掉会让 Chromium 走代理的那几个。"""
    source = os.environ if env is None else env
    return {k: v for k, v in source.items() if k not in PROXY_ENV_KEYS}


def site_url(url: str) -> str:
    """把任意页面地址收敛成站点根地址，用作查 Cookie 的坐标。"""
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return url
    return f"{parts.scheme}://{parts.netloc}/"


#: 走淘宝账号体系的平台：它们的请求除了自己的域，还要带上阿里系基础设施的 Cookie。
ALIBABA_PLATFORMS = ("damai.cn",)

#: 阿里系基础设施域（签名 / 风控 / 统计）。缺了它们，mtop 签名可能过不了。
ALIBABA_INFRA = (
    "taobao.com",
    "alibaba.com",
    "alipay.com",
    "mmstat.com",
    "aliapp.org",
)


def site_root(host: str) -> str:
    """取近似注册域：``ipassport.damai.cn`` → ``damai.cn``。

    同一个站的不同子域属于同一家（``www.damai.cn`` 与 ``ipassport.damai.cn``），
    而 ``.maoyan.com`` 与它们无关。用它来判断「是不是同一个平台的 Cookie」。
    """
    cleaned = host.lstrip(".").lower()
    parts = cleaned.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else cleaned


def relevant_cookies(
    cookies: Sequence[Mapping[str, Any]], *, site_host: str = ""
) -> list[Mapping[str, Any]]:
    """只留「这个平台自己的」Cookie。

    规则两条：

    1. **同一个注册域**（含子域）——``ipassport.damai.cn`` 是大麦的登录域，
       漏了它等于没拿到完整登录态，而只问 ``https://www.damai.cn/`` 就会漏。
    2. **阿里系基础设施**——但仅当目标平台本身走淘宝账号体系（大麦就是）。
       否则会把别的平台、乃至用户逛过的其它站点的 Cookie 一起塞进请求头。

    实测：放宽前收集到 19 条（混进了猫眼的 ``_lxsdk``），收窄后 17 条且更干净。
    """
    host = site_host.lstrip(".").lower()
    root = site_root(host)
    wants_alibaba = root in ALIBABA_PLATFORMS

    picked: list[Mapping[str, Any]] = []
    for cookie in cookies:
        domain = str(cookie.get("domain") or "").lstrip(".").lower()
        if not domain:
            continue
        if root and site_root(domain) == root:
            picked.append(cookie)
            continue
        if wants_alibaba and site_root(domain) in ALIBABA_INFRA:
            picked.append(cookie)
    return picked


def join_cookies(cookies: Sequence[Mapping[str, Any]]) -> str:
    """把浏览器给的 Cookie 列表拼成 ``name=value; name=value``。

    同一个名字可能同时存在于 ``.damai.cn`` 和 ``www.damai.cn`` 两个域上
    （还有不同 path）。越具体的越贴合当前页面，所以按「path 长度、域名长度」
    排序，保留权重最高的那个；否则会把过期的那份发出去。
    """
    best: dict[str, tuple[int, str]] = {}
    for cookie in cookies:
        name = str(cookie.get("name") or "").strip()
        if not name:
            continue
        value = str(cookie.get("value") or "")
        weight = len(str(cookie.get("path") or "")) * 100 + len(str(cookie.get("domain") or ""))
        current = best.get(name)
        if current is None or weight >= current[0]:
            best[name] = (weight, value)
    return "; ".join(f"{name}={value}" for name, (_, value) in best.items())


def interesting_requests(
    entries: Sequence[tuple[str, str]], *, limit: int = 12
) -> list[str]:
    """从页面访问过的请求里挑出「像数据接口」的那些。

    ``entries`` 是 ``(url, resource_type)``。只留 xhr / fetch，
    滤掉埋点与监控，按首次出现顺序去重。
    """
    seen: set[str] = set()
    picked: list[str] = []
    for url, kind in entries:
        if kind not in API_RESOURCE_TYPES:
            continue
        lowered = url.lower()
        if any(marker in lowered for marker in NOISE_MARKERS):
            continue
        if url in seen:
            continue
        seen.add(url)
        picked.append(url)
        if len(picked) >= limit:
            break
    return picked


def write_env_value(path: str | Path, key: str, value: str) -> bool:
    """就地更新 ``.env`` 里的一个键，其余内容原样保留。返回是否新建了该键。

    值一律用双引号包起来：Cookie 里带分号和空格，不加引号会被
    :func:`radar.config.parse_dotenv` 的行尾注释规则误伤。
    """
    target = Path(path)
    try:
        text = target.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        text = ""
    except OSError as exc:  # pragma: no cover - 依赖真实文件系统
        raise RuntimeError(f"读不了 {target}：{exc}") from exc

    line = f'{key}="{value}"'
    pattern = re.compile(
        rf"(?m)^[ \t]*(?:export[ \t]+)?{re.escape(key)}[ \t]*=.*$"
    )
    if pattern.search(text):
        new_text = pattern.sub(lambda _match: line, text, count=1)
        created = False
    else:
        # 原文件末尾没换行时先补一个，否则新键会和最后一行黏在一起
        body = text if (not text or text.endswith("\n")) else text + "\n"
        new_text = body + line + "\n"
        created = True

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(new_text, encoding="utf-8")
    return created


# ---------------------------------------------------------------------------
# 普通浏览器：登录这一步不该沾自动化
# ---------------------------------------------------------------------------

#: Edge 的常见安装位置，按顺序试。用户机器上装在哪一档都得能找到。
EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)

#: **绝不出现**在登录窗口命令行里的参数。
#: 这几个一串上，页面就能通过 ``navigator.webdriver`` 或 CDP 痕迹认出自己
#: 正在被程序驱动——阿里云滑块就是这么发现我们的，然后无论怎么划都判「位置不对」。
AUTOMATION_FLAGS = (
    "--enable-automation",
    "--headless",
    "--headless=new",
    "--remote-debugging-port",
    "--remote-debugging-pipe",
)


def find_edge(env: Mapping[str, str] | None = None) -> Path | None:
    """找系统里的 Edge。找不到返回 None，调用方给降级提示。"""
    source = os.environ if env is None else env
    local = source.get("LOCALAPPDATA")
    candidates = list(EDGE_CANDIDATES)
    if local:
        candidates.append(str(Path(local) / "Microsoft" / "Edge" / "Application" / "msedge.exe"))
    for raw in candidates:
        path = Path(raw)
        if path.is_file():
            return path
    return None


def edge_argv(edge: Path | str, profile_dir: Path | str, url: str) -> list[str]:
    """拼出启动**普通** Edge 的命令行。

    这个函数是「滑块能不能过」的关键所在，所以单独拎出来好测：
    返回的列表里不允许出现 :data:`AUTOMATION_FLAGS` 里的任何一项。
    """
    argv = [
        str(edge),
        f"--user-data-dir={profile_dir}",
        # 独立 profile 首启会弹欢迎页和「登录以同步」；这两个参数关掉大部分
        "--no-first-run",
        "--no-default-browser-check",
        # 会话注入的 http_proxy 会把 Chromium 送进代理，页面直接打不开
        "--no-proxy-server",
        # 关掉「启动增强」：默认它会在窗口关掉后把主进程留在后台，
        # 于是 profile 一直被占着，紧接着的无头读取会单例冲突直接失败。
        "--disable-background-mode",
    ]
    for flag in AUTOMATION_FLAGS:
        if any(a.startswith(flag) for a in argv):  # pragma: no cover - 防御性
            raise AssertionError(f"登录窗口不能带自动化参数：{flag}")
    argv.append(url)
    return argv


def launch_manual_browser(
    *,
    url: str,
    profile_dir: Path | str,
    edge: Path | str | None = None,
    env: Mapping[str, str] | None = None,
) -> subprocess.Popen:
    """拉起一个普通 Edge 窗口让用户手工登录。返回进程句柄。"""
    executable = Path(str(edge)) if edge else find_edge()
    if executable is None:
        raise BrowserUnavailable(
            "没找到 Edge。这个功能借系统里已装的 Edge 用，不额外下载浏览器。\n"
            "装了 Edge 还看不到？把你的 msedge.exe 完整路径告诉我。\n"
            "实在不行就自己从浏览器取 Cookie：radar onboard <平台> 里有步骤。"
        )

    Path(profile_dir).mkdir(parents=True, exist_ok=True)
    argv = edge_argv(executable, profile_dir, url)
    return subprocess.Popen(  # noqa: S603 - 参数是本地构造的固定列表
        argv,
        env=clean_env(env),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def wait_for_browser_exit(
    proc: subprocess.Popen,
    *,
    timeout: float = 600.0,
    stop_event: threading.Event | None = None,
    poll_interval: float = 0.5,
) -> str:
    """等用户把浏览器关掉。返回 ``closed`` / ``enter`` / ``timeout``。

    两个出口都给，是因为 Edge 有「启动增强」——窗口关完了后台进程可能还赖着，
    只等进程退出会白等到超时。所以顺手留一个「按回车」的快捷键。
    """
    deadline = time.monotonic() + timeout
    while True:
        if proc.poll() is not None:
            return "closed"
        if stop_event is not None and stop_event.is_set():
            return "enter"
        if time.monotonic() >= deadline:
            return "timeout"
        time.sleep(poll_interval)


def close_browser(proc: subprocess.Popen, *, wait: float = 12.0) -> bool:
    """确保那个登录浏览器**真的退出**，返回是否已退出。

    为什么非做不可：Edge 的「启动增强」会在窗口关掉之后把主进程留在后台。
    于是 profile 仍被占用，紧接着的无头读取会单例冲突——表现为一句
    ``TargetClosedError``，看着像「Cookie 取不到」，其实是浏览器没走。
    用户遇到的是「回车之后没反应」，根子在这儿。
    """
    if proc.poll() is not None:
        return True

    # taskkill 不带 /F 相当于「好好请它关」：发 WM_CLOSE，让 Chromium 自己收尾、
    # 把 Cookie 落盘。直接 terminate() 是强杀，可能丢掉还没落盘的登录态。
    with contextlib.suppress(Exception):
        subprocess.run(  # noqa: S603 - 参数是本地构造的固定列表
            ["taskkill", "/PID", str(proc.pid), "/T"],
            capture_output=True,
            timeout=20,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return True
        time.sleep(0.3)

    with contextlib.suppress(Exception):
        proc.kill()
        proc.wait(timeout=5)
    return proc.poll() is not None


def watch_for_enter() -> threading.Event:
    """起一个守护线程等回车，返回一个「用户按了回车」的信号。

    守护线程在解释器退出时直接丢弃，不拦路——所以用户中途改主意关掉窗口，
    进程也不会卡在退出阶段干等。
    """
    stop = threading.Event()

    def worker() -> None:
        with contextlib.suppress(EOFError, KeyboardInterrupt, OSError):
            input()
            stop.set()

    threading.Thread(target=worker, daemon=True, name="radar-wait-enter").start()
    return stop


# ---------------------------------------------------------------------------
# 浏览器编排
# ---------------------------------------------------------------------------


#: 缺依赖时的统一说法——能照做，不是一句 ImportError
_MISSING_PLAYWRIGHT = (
    "没装 playwright，用不了这个功能。装一行就行：\n"
    "  pip install playwright\n"
    "本功能复用你系统里已装的 Edge，不会额外下载浏览器。\n"
    "装完再跑一次即可；不装也能用项目其它部分，只是要自己从浏览器取 Cookie。"
)


#: 浏览器配置被占用时的说法。用户看到的**不该**是一句 TargetClosedError。
_PROFILE_BUSY_HINT = (
    "打不开浏览器配置——多半是上次那个登录窗口还开着"
    "（Edge 关掉窗口后，进程有时还赖在后台）。\n"
    "把 Edge 完全退出（任务栏、托盘都看一眼）再试一次。\n"
    "原始错误：{err}"
)


async def read_cookies(
    *,
    profile_dir: str | Path,
    site_url: str,
    headless: bool = True,
) -> tuple[str, list[str]]:
    """打开指定 profile，把票务平台的 Cookie 交出来。

    这一步用 Playwright 起无头浏览器是安全的——**用户此刻已经不操作了**，
    登录早就在普通浏览器里完成了。风控爱认不认都无所谓。

    取的是 ``context.cookies()`` 即**全部域**，再按白名单筛。
    只看主域会漏：大麦的登录取证横跨 ``.damai.cn`` 与 ``ipassport.damai.cn``，
    实测只问 ``https://www.damai.cn/`` 会少 4 条。
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover - 取决于运行环境
        raise BrowserUnavailable(_MISSING_PLAYWRIGHT) from exc

    try:
        async with async_playwright() as pw:
            context = await pw.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                channel="msedge",
                headless=headless,
                args=["--no-proxy-server"],
                env=clean_env(),
            )
            try:
                raw = await context.cookies()
            finally:
                with contextlib.suppress(Exception):
                    await context.close()
    except Exception as exc:  # noqa: BLE001
        raise BrowserUnavailable(
            _PROFILE_BUSY_HINT.format(err=f"{type(exc).__name__}: {str(exc)[:120]}")
        ) from exc

    picked = relevant_cookies(raw, site_host=urlsplit(site_url).hostname or "")
    return (
        join_cookies(picked),
        sorted({str(c.get("name")) for c in picked if c.get("name")}),
    )


async def sniff_requests(
    *,
    profile_dir: str | Path,
    url: str,
    headless: bool = True,
    limit: int = 12,
) -> list[str]:
    """用已保存的登录态打开一个页面，把它调用的数据接口列出来。

    对应原来 ``radar login`` 里「顺手记下接口」那一半功能。现在单独成命令，
    因为登录改用普通浏览器后，程序在登录期间看不到网络请求了。
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover - 取决于运行环境
        raise BrowserUnavailable(_MISSING_PLAYWRIGHT) from exc

    entries: list[tuple[str, str]] = []
    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            channel="msedge",
            headless=headless,
            args=["--no-proxy-server"],
            env=clean_env(),
        )
        context.on("request", lambda req: entries.append((req.url, req.resource_type)))
        page = context.pages[0] if context.pages else await context.new_page()
        with contextlib.suppress(Exception):
            await page.goto(url, timeout=30000, wait_until="domcontentloaded")
            await page.wait_for_timeout(3500)
        with contextlib.suppress(Exception):
            await context.close()

    return interesting_requests(entries, limit=limit)


async def _wait_for_enter(prompt: str, done: asyncio.Event) -> None:
    """起一个守护线程等回车，用事件把结果传回事件循环。

    刻意**不用** ``asyncio.to_thread(input, ...)``：那个线程取消不掉，
    用户要是改主意去关浏览器窗口，进程会卡在退出阶段干等它。
    ``asyncio.run`` 收尾时会 join 默认线程池，而守护线程在解释器退出时
    直接丢弃，不拦路。
    """
    loop = asyncio.get_running_loop()

    def worker() -> None:
        try:
            input(prompt)
        except (EOFError, KeyboardInterrupt):
            pass
        finally:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(done.set)

    threading.Thread(target=worker, daemon=True, name="radar-wait-enter").start()


async def capture(
    *,
    login_url: str,
    profile_dir: str | Path,
    timeout: float = 600.0,
    headless: bool = False,
    echo: Callable[[str], None] | None = None,
) -> CaptureResult:
    """打开浏览器、等用户登录，然后把 Cookie 与接口清单取回来。

    结束条件有两个，哪个先到都算数：用户按回车，或用户把浏览器窗口关掉。
    之所以不自动判断「登录成功没」，是因为他可能还要在页面里点几层
    （挑场次、看票档），什么时候算弄完只有他自己知道。
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover - 取决于运行环境
        raise BrowserUnavailable(_MISSING_PLAYWRIGHT) from exc

    say = echo or (lambda _msg: None)
    profile_dir = Path(profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)
    result = CaptureResult(site_url=site_url(login_url))
    entries: list[tuple[str, str]] = []

    async with async_playwright() as pw:
        try:
            context = await pw.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                channel="msedge",
                headless=headless,
                args=["--no-proxy-server"],
                env=clean_env(),
            )
        except Exception as exc:
            raise BrowserUnavailable(
                "浏览器没起来。常见原因有两个：\n"
                f"  1) 系统里没有 Edge（这个是原始错误：{str(exc)[:160]}）\n"
                "  2) Edge 正在更新，稍等一分钟再试\n"
                "实在不行就自己从浏览器取 Cookie：radar onboard <平台> 里有步骤。"
            ) from exc

        context.on("request", lambda req: entries.append((req.url, req.resource_type)))
        page = context.pages[0] if context.pages else await context.new_page()
        try:
            await page.goto(login_url, timeout=30000)
        except Exception as exc:  # noqa: BLE001 - 打不开也不该让整件事失败
            say(f"试用登录页时没能加载（{str(exc)[:80]}），你可以自己在浏览器里输入网址。")

        # 用户可能在任意时刻关掉窗口，所以持续留一份 Cookie 快照兜底
        snapshot: list[Mapping[str, Any]] = []

        async def keep_snapshot() -> None:
            while True:
                try:
                    fresh = await context.cookies(result.site_url)
                except Exception:  # noqa: BLE001 - 浏览器一关，循环自然结束
                    return
                if fresh:
                    snapshot.clear()
                    snapshot.extend(fresh)
                await asyncio.sleep(2.0)

        poller = asyncio.create_task(keep_snapshot())
        done = asyncio.Event()
        context.on("close", lambda _ctx: done.set())
        await _wait_for_enter(
            "登录完成后回到这个窗口按回车（或者直接关掉浏览器窗口）…", done
        )
        try:
            await asyncio.wait_for(done.wait(), timeout=timeout if timeout > 0 else None)
        except asyncio.TimeoutError:
            say("等太久了，先按现在拿到的状态收尾。")

        poller.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await poller

        # 浏览器还活着就抓最新的一份；已经关了就用快照
        raw: Sequence[Mapping[str, Any]] = snapshot
        with contextlib.suppress(Exception):
            live = await context.cookies()
            if live:
                raw = live

        picked = relevant_cookies(
            raw, site_host=urlsplit(result.site_url).hostname or ""
        )
        result.cookies = join_cookies(picked)
        result.cookie_names = sorted(
            {str(c.get("name")) for c in picked if c.get("name")}
        )
        result.requests = interesting_requests(entries)

        with contextlib.suppress(Exception):
            await context.close()

    return result


__all__ = [
    "ALIBABA_INFRA",
    "ALIBABA_PLATFORMS",
    "API_RESOURCE_TYPES",
    "AUTOMATION_FLAGS",
    "EDGE_CANDIDATES",
    "NOISE_MARKERS",
    "PROFILE_RELATIVE",
    "PROXY_ENV_KEYS",
    "BrowserUnavailable",
    "CaptureResult",
    "capture",
    "clean_env",
    "close_browser",
    "edge_argv",
    "find_edge",
    "interesting_requests",
    "join_cookies",
    "launch_manual_browser",
    "read_cookies",
    "relevant_cookies",
    "site_root",
    "site_url",
    "sniff_requests",
    "wait_for_browser_exit",
    "watch_for_enter",
    "write_env_value",
]
