"""B站登录步骤2：连接已有浏览器，等待扫码完成并保存 Cookie

需要先执行 bili_get_qr.py 获取二维码并保持浏览器运行。

使用:
  python scripts/init/bili_wait_login.py
  python scripts/init/bili_wait_login.py --timeout 120
"""

import sys
import os
import signal
import json
import asyncio
import argparse
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR.parent))

from common import SCRIPT_DIR, setup_logging

logger = setup_logging("bili_wait_login")
BILI_ENV_PATH = SCRIPT_DIR / "env" / ".bili.env"
SESSION_PATH = Path("/tmp/bili_browser_session.json")


def _check_nodriver():
    try:
        import nodriver  # noqa: F401
    except ImportError:
        print(
            f"错误: 未安装 nodriver 包。\n"
            f"请运行: {sys.executable} -m pip install 'nodriver>=0.50'",
            file=sys.stderr,
        )
        sys.exit(1)


def _stop_chrome(chrome_pid: int):
    """关闭 Chrome 进程（nodriver 的 browser.stop() 无法停止外部启动的浏览器）"""
    try:
        os.kill(chrome_pid, signal.SIGTERM)
        logger.info(f"已终止 Chrome 进程 (PID: {chrome_pid})")
    except ProcessLookupError:
        logger.info(f"Chrome 进程已不存在 (PID: {chrome_pid})")
    except PermissionError:
        logger.warning(f"无权限终止 Chrome 进程 (PID: {chrome_pid})")


async def _wait_login(timeout: int = 180) -> dict:
    import nodriver as uc

    if not SESSION_PATH.exists():
        raise FileNotFoundError(
            f"会话文件不存在: {SESSION_PATH}，请先执行 bili_get_qr.py 获取二维码"
        )

    with open(SESSION_PATH) as f:
        session = json.load(f)

    chrome_pid = session.get("chrome_pid")
    logger.info(f"连接到已有浏览器 {session['host']}:{session['port']}...")
    browser = await uc.Browser.create(host=session["host"], port=session["port"])
    tab = browser.main_tab

    logger.info("等待扫码登录...")
    logged_in = False
    for i in range(timeout):
        await tab.sleep(1)
        if i % 3 == 0:
            cookies = await browser.cookies.get_all()
            has_sessdata = has_dedeuserid = False
            for c in cookies:
                name = c.name if hasattr(c, "name") else c.get("name", "")
                value = c.value if hasattr(c, "value") else c.get("value", "")
                if name == "SESSDATA" and value:
                    has_sessdata = True
                if name == "DedeUserID" and value:
                    has_dedeuserid = True
            if has_sessdata and has_dedeuserid:
                logged_in = True
                logger.info("检测到登录成功")
                break

    if not logged_in:
        browser.stop()
        if chrome_pid:
            _stop_chrome(chrome_pid)
        SESSION_PATH.unlink(missing_ok=True)
        raise TimeoutError("登录超时")

    await tab.sleep(2)
    raw_cookies = await browser.cookies.get_all()
    cookie_dict = {}
    for c in raw_cookies:
        name = c.name if hasattr(c, "name") else c.get("name", "")
        value = c.value if hasattr(c, "value") else c.get("value", "")
        if name and value:
            cookie_dict[name] = value

    logger.info(f"提取到 {len(cookie_dict)} 个 Cookie: {list(cookie_dict.keys())}")
    browser.stop()
    if chrome_pid:
        _stop_chrome(chrome_pid)

    SESSION_PATH.unlink(missing_ok=True)
    return cookie_dict


def _save(cookie_dict: dict):
    """将 Cookie 写入 .bili.env"""
    BILI_ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    BILI_ENV_PATH.write_text(
        f"bili_cookies_json={json.dumps(cookie_dict)}\n",
        encoding="utf-8",
    )
    logger.info(f"已保存 {len(cookie_dict)} 个 Cookie 到 {BILI_ENV_PATH}")


def main():
    _check_nodriver()
    parser = argparse.ArgumentParser(description="B站登录 - 步骤2：等待扫码并保存Cookie")
    parser.add_argument("--timeout", type=int, default=180, help="扫码超时（秒）")
    args = parser.parse_args()

    try:
        cookie_dict = asyncio.run(_wait_login(timeout=args.timeout))
    except TimeoutError:
        print(json.dumps({"action": "timeout", "message": "登录超时"}, ensure_ascii=False))
        sys.exit(1)
    except Exception as e:
        msg = f"登录失败: {e}"
        print(msg, file=sys.stderr)
        logger.error(msg)
        sys.exit(1)

    if not cookie_dict.get("SESSDATA"):
        logger.error("未获取到 SESSDATA Cookie")
        sys.exit(1)

    _save(cookie_dict)
    print(json.dumps({
        "action": "login_success",
        "message": f"登录成功，已保存 {len(cookie_dict)} 个 Cookie",
        "cookies": list(cookie_dict.keys()),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
