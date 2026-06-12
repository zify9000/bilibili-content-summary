"""B站登录步骤1：启动 Chromium headless，获取二维码

浏览器进程与本脚本完全分离，步骤2执行时浏览器保持运行。

输出 JSON 包含 QR 图片路径，供 agent 展示给用户扫码。

使用:
  python scripts/init/bili_get_qr.py
  python scripts/init/bili_get_qr.py --no-sandbox
  python scripts/init/bili_get_qr.py --browser-path /opt/chrome/google-chrome
"""

import sys
import json
import asyncio
import subprocess
import argparse
from pathlib import Path
from urllib.request import urlretrieve
from tempfile import mkdtemp

_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR.parent))

from common import setup_logging

logger = setup_logging("bili_get_qr")
QR_IMAGE_PATH = Path("/tmp/bili_login_qr.png")
SESSION_PATH = Path("/tmp/bili_browser_session.json")

LOGIN_URL = "https://www.bilibili.com/"


def _check_nodriver():
    try:
        import nodriver  # noqa: F401
    except ImportError:
        print(
            f"错误: 未安装 nodriver 包。\n"
            f"请运行: {sys.executable} -m pip install 'nodriver>=0.50'\n"
            f"当前 Python: {sys.executable}",
            file=sys.stderr,
        )
        sys.exit(1)


def _find_chrome(browser_path: str = "") -> str:
    """查找 Chrome/Chromium 可执行文件路径"""
    if browser_path:
        path = Path(browser_path)
        if path.exists() and path.is_file():
            return str(path)
        raise FileNotFoundError(f"指定的浏览器路径不存在: {browser_path}")

    from nodriver.core.config import find_chrome_executable
    return find_chrome_executable()


def _free_port() -> int:
    import socket
    free_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    free_socket.bind(("127.0.0.1", 0))
    free_socket.listen(5)
    port: int = free_socket.getsockname()[1]
    free_socket.close()
    return port


def _build_chrome_args(port: int, sandbox: bool) -> list:
    """构建 Chrome headless 启动参数"""
    user_data_dir = mkdtemp(prefix="bili_uc_")
    args = [
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-service-autorun",
        "--no-default-browser-check",
        "--homepage=about:blank",
        "--no-pings",
        "--password-store=basic",
        "--disable-infobars",
        "--disable-breakpad",
        "--disable-dev-shm-usage",
        "--disable-session-crashed-bubble",
        "--disable-search-engine-choice-screen",
        f"--user-data-dir={user_data_dir}",
        "--disable-features=IsolateOrigins,site-per-process",
        "--headless=new",
        "--noerrdialogs",
        "--ozone-platform=headless",
        "--ozone-override-screen-size=800,600",
        "--use-angle=swiftshader-webgl",
        "--remote-debugging-host=127.0.0.1",
        f"--remote-debugging-port={port}",
    ]
    if not sandbox:
        args.append("--no-sandbox")
    return args


def _start_chrome_detached(chrome_path: str, port: int, sandbox: bool) -> subprocess.Popen:
    """启动 Chrome 并与当前进程组分离，脚本退出后浏览器继续运行"""
    args = [chrome_path] + _build_chrome_args(port, sandbox)
    logger.info(f"启动浏览器（分离模式）: {chrome_path}")
    logger.debug(f"浏览器参数: {' '.join(args)}")

    process = subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    logger.info(f"浏览器 PID: {process.pid}, 调试端口: {port}")
    return process


async def _connect_and_get_qr(host: str, port: int, chrome_pid: int) -> str:
    """连接已有浏览器，打开B站首页，获取二维码图片"""
    import nodriver as uc

    browser = await uc.Browser.create(host=host, port=port)

    session_info = {
        "host": browser.config.host,
        "port": browser.config.port,
        "websocket_url": browser.websocket_url,
        "chrome_pid": chrome_pid,
    }
    SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SESSION_PATH, "w") as f:
        json.dump(session_info, f)
    logger.info(f"浏览器会话信息已保存: {SESSION_PATH}")

    tab = browser.main_tab
    await tab.get(LOGIN_URL)
    await tab.sleep(5)

    # 检查是否已登录
    cookies = await browser.cookies.get_all()
    for c in cookies:
        name = c.name if hasattr(c, "name") else c.get("name", "")
        if name == "DedeUserID":
            return "already_logged_in"

    # 点击登录按钮
    await tab.evaluate("""
        (() => {
            const all = document.querySelectorAll('*');
            for (const el of all) {
                if (el.innerText && el.innerText.trim() === '登录' && el.tagName !== 'BODY' && el.tagName !== 'HTML') {
                    el.click(); return true;
                }
            }
            return false;
        })()
    """)
    await tab.sleep(5)

    # 提取二维码
    import base64
    for attempt in range(15):
        if attempt > 0:
            await tab.sleep(2)
        try:
            qr_result = await tab.evaluate("""
                (() => {
                    const imgs = document.querySelectorAll('img');
                    for (const img of imgs) {
                        if (img.width > 100 && img.height > 100 && /passport\\.bilibili\\.com/.test(img.src))
                            return JSON.stringify({found: true, src: img.src});
                    }
                    for (const img of imgs) {
                        if (img.width > 100 && img.height > 100 && img.width === img.height && img.src.startsWith('data:image/'))
                            return JSON.stringify({found: true, src: img.src});
                    }
                    const canvases = document.querySelectorAll('canvas');
                    for (const c of canvases) {
                        if (c.width > 100 && c.height > 100)
                            return JSON.stringify({found: true, src: c.toDataURL('image/png')});
                    }
                    return JSON.stringify({found: false});
                })()
            """)
            qr_data = json.loads(qr_result)
            if qr_data.get("found"):
                qr_url = qr_data["src"]
                if qr_url.startswith("data:"):
                    _, encoded = qr_url.split(",", 1)
                    with open(QR_IMAGE_PATH, "wb") as f:
                        f.write(base64.b64decode(encoded))
                else:
                    urlretrieve(qr_url, str(QR_IMAGE_PATH))
                logger.info(f"二维码已保存: {QR_IMAGE_PATH}")
                return qr_url
        except Exception as e:
            logger.debug(f"尝试 {attempt + 1}/15 获取二维码失败: {e}")

    # 兜底：截图
    await tab.save_screenshot(str(QR_IMAGE_PATH))
    logger.warning("未能提取二维码图片，已保存截图作为兜底")
    return "screenshot"


def main():
    _check_nodriver()
    parser = argparse.ArgumentParser(description="B站登录 - 步骤1：获取二维码")
    parser.add_argument("--no-sandbox", dest="sandbox", action="store_false",
                        help="禁用 Chromium 沙箱（Docker/snap 环境可能需要）")
    parser.add_argument("--browser-path", type=str, default="",
                        help="指定 Chromium 浏览器路径")
    args = parser.parse_args()

    try:
        chrome_path = _find_chrome(args.browser_path)
        port = _free_port()
        chrome_proc = _start_chrome_detached(chrome_path, port, args.sandbox)
        qr_url = asyncio.run(_connect_and_get_qr(host="127.0.0.1", port=port, chrome_pid=chrome_proc.pid))
    except Exception as e:
        msg = f"获取二维码失败: {e}"
        print(msg, file=sys.stderr)
        logger.error(msg)
        sys.exit(1)

    if qr_url == "already_logged_in":
        print(json.dumps({
            "action": "already_logged_in",
            "message": "浏览器已处于登录状态，可直接执行步骤2提取 Cookie。",
            "session_file": str(SESSION_PATH),
        }, ensure_ascii=False))
    else:
        print(json.dumps({
            "action": "qr_ready",
            "qr_image_path": str(QR_IMAGE_PATH),
            "qr_image_url": qr_url[:100] if qr_url != "screenshot" else "screenshot",
            "session_file": str(SESSION_PATH),
            "message": "二维码已就绪，请用 B站 App 扫描。完成后执行步骤2脚本等待登录。",
        }, ensure_ascii=False))


if __name__ == "__main__":
    main()
