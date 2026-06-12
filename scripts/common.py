"""公共工具：配置加载、日志、环境变量、飞书消息、重试"""
import os
import sys
import time as time_module
import logging
from datetime import datetime, timedelta
from pathlib import Path

os.environ["TZ"] = "Asia/Shanghai"
time_module.tzset()

SCRIPT_DIR = Path(__file__).parent
CONFIG_DIR = SCRIPT_DIR / "config"
TMP_DIR = SCRIPT_DIR / "tmp"
TMP_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR = SCRIPT_DIR / "log"

BASE_CONFIG_PATH = CONFIG_DIR / "base.yaml"
SUMMARY_RESULT_PATH = TMP_DIR / "summary_result.json"


def setup_logging(name: str) -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_level = os.environ.get("BILI_SUMMARY_LOG_LEVEL", "INFO").upper()
    today = datetime.now().strftime("%Y%m%d")
    fmt = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 清理超过 7 天的旧日志
    _cleanup_old_logs(LOG_DIR, name, keep_days=7)

    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, log_level, logging.INFO))
    logger.propagate = False

    if not any(isinstance(h, logging.FileHandler) for h in logger.handlers):
        file_handler = logging.FileHandler(
            LOG_DIR / f"{name}_{today}.log", encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)

    if not any(isinstance(h, logging.StreamHandler) for h in logger.handlers):
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setFormatter(fmt)
        logger.addHandler(stream_handler)

    return logger


def _cleanup_old_logs(log_dir: Path, name: str, keep_days: int = 7):
    """删除超过 keep_days 天的旧日志文件"""
    cutoff = datetime.now() - timedelta(days=keep_days)
    for log_file in sorted(log_dir.glob(f"{name}_*.log")):
        try:
            mtime = datetime.fromtimestamp(log_file.stat().st_mtime)
            if mtime < cutoff:
                log_file.unlink()
        except (ValueError, OSError):
            pass


# ── 环境变量 ──

def _load_env_file(filename: str):
    """加载 env 文件中的 key=value 到 os.environ，自动展开 ${VAR} 引用"""
    env_path = SCRIPT_DIR / "env" / filename
    if not env_path.exists():
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            sep = "=" if "=" in line else (":" if ":" in line else None)
            if sep:
                k, v = line.split(sep, 1)
                raw = v.strip().strip('"').strip("'")
                os.environ[k.strip()] = os.path.expandvars(raw)


def load_bili_env():
    """加载 .bili.env"""
    _load_env_file(".bili.env")


def load_asr_env():
    """[已废弃] 加载 ASR 凭据。现由 load_llm_env() 统一加载 .llm.env。
    保留此函数以兼容旧调用方，后续版本将移除。"""
    load_llm_env()


def load_feishu_env():
    """加载 .feishu.env"""
    _load_env_file(".feishu.env")


def load_llm_env():
    """加载 .llm.env（含 LLM / ASR / OCR 配置）"""
    _load_env_file(".llm.env")


# ── 凭据读取 ──

def get_bili_cookies() -> dict:
    """从环境变量读取 B站 Cookie 字典"""
    import json as _json
    raw = os.environ.get("bili_cookies_json", "")
    if raw:
        try:
            return _json.loads(raw)
        except _json.JSONDecodeError:
            pass
    # 回退：仅 SESSDATA
    sessdata = os.environ.get("bili_sessdata", "")
    return {"SESSDATA": sessdata} if sessdata else {}


def get_asr_creds() -> tuple:
    """从环境变量读取 ASR 凭据，返回 (base_url, api_key, model)"""
    return (
        os.environ.get("asr_base_url", ""),
        os.environ.get("asr_api_key", ""),
        os.environ.get("asr_model", ""),
    )


def get_llm_creds() -> tuple:
    """从环境变量读取 LLM 凭据，返回 (model, base_url, api_key)"""
    return (
        os.environ.get("llm_model", ""),
        os.environ.get("llm_base_url", ""),
        os.environ.get("llm_api_key", ""),
    )


def get_ocr_creds() -> tuple:
    """从环境变量读取 OCR 凭据，返回 (model, base_url, api_key)"""
    return (
        os.environ.get("ocr_model", ""),
        os.environ.get("ocr_base_url", ""),
        os.environ.get("ocr_api_key", ""),
    )


def get_feishu_creds() -> tuple:
    """从环境变量读取飞书凭据，返回 (app_id, app_secret, chat_id)"""
    return (
        os.environ.get("feishu_app_id", ""),
        os.environ.get("feishu_app_secret", ""),
        os.environ.get("feishu_chat_id", ""),
    )


def validate_creds(*required_pairs) -> list:
    """校验凭据，返回问题列表。空列表表示无问题。

    用法: validate_creds(("asr_base_url", base_url), ("asr_api_key", api_key))
    """
    issues = []
    for name, value in required_pairs:
        if not value:
            issues.append(f"{name} 为空")
        elif "${" in str(value):
            issues.append(f"{name} 包含未展开的 ${{...}} 变量引用")
    return issues


# ── 配置 ──

def load_base_config() -> dict:
    import yaml
    cfg = {}
    if BASE_CONFIG_PATH.exists():
        with open(BASE_CONFIG_PATH) as f:
            cfg = yaml.safe_load(f) or {}
    return cfg


# ── 飞书 ──

def get_feishu_token(app_id: str, app_secret: str) -> str:
    """获取飞书 tenant_access_token"""
    import requests
    resp = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret},
        timeout=10,
    )
    result = resp.json()
    if result.get("code") != 0:
        raise RuntimeError(f"获取飞书 token 失败: code={result.get('code')} msg={result.get('msg')}")
    return result["tenant_access_token"]


def send_feishu_message(token: str, chat_id: str, payload: dict):
    """发送飞书消息"""
    import requests
    url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload,
        timeout=15,
    )
    result = resp.json()
    if result.get("code") != 0:
        raise RuntimeError(f"飞书发送失败: code={result.get('code')} msg={result.get('msg')}")
    return result["data"]["message_id"]


# ── 重试装饰器 ──

def retry(times=3, delay=5, backoff=2, logger=None):
    def decorator(func):
        def wrapper(*args, **kwargs):
            _logger = logger or logging.getLogger(func.__module__)
            current_delay = delay
            for attempt in range(1, times + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if attempt == times:
                        raise
                    _logger.warning(f"第{attempt}次失败: {e}，{current_delay}秒后重试")
                    time_module.sleep(current_delay)
                    current_delay *= backoff
            return None
        return wrapper
    return decorator
