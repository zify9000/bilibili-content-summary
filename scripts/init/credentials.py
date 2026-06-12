"""凭据初始化：写入 LLM/ASR/OCR/飞书凭据（统一到 .llm.env）

B站 Cookie 扫码请使用独立脚本：
  步骤1: python3 scripts/init/bili_get_qr.py
  步骤2: python3 scripts/init/bili_wait_login.py
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from common import SCRIPT_DIR, setup_logging

logger = setup_logging("credentials")

ENV_DIR = SCRIPT_DIR / "env"
LLM_ENV_PATH = ENV_DIR / ".llm.env"
FEISHU_ENV_PATH = ENV_DIR / ".feishu.env"


def write_env_file(path: Path, entries: dict):
    """将 key=value 写入 env 文件"""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{k}={v}" for k, v in entries.items() if v]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info(f"已写入 {path}")


def main():
    parser = argparse.ArgumentParser(description="B站内容摘要 — 凭据初始化（LLM/ASR/OCR/飞书）")
    # ASR 凭据（写入 .llm.env 的 asr_* 字段）
    parser.add_argument("--asr-base-url", default="", help="ASR 服务地址")
    parser.add_argument("--asr-api-key", default="", help="ASR API 密钥")
    parser.add_argument("--asr-model", default="", help="ASR 模型名")
    # LLM 凭据
    parser.add_argument("--llm-model", default="", help="LLM 模型名")
    parser.add_argument("--llm-base-url", default="", help="LLM API 地址")
    parser.add_argument("--llm-api-key", default="", help="LLM API 密钥")
    # OCR 凭据
    parser.add_argument("--ocr-model", default="", help="OCR 模型名（如 mimo-v2.5）")
    parser.add_argument("--ocr-base-url", default="", help="OCR API 地址")
    parser.add_argument("--ocr-api-key", default="", help="OCR API 密钥")
    # 飞书凭据
    parser.add_argument("--feishu-app-id", default="", help="飞书应用 ID")
    parser.add_argument("--feishu-app-secret", default="", help="飞书应用密钥")
    parser.add_argument("--feishu-chat-id", default="", help="飞书群聊 ID")
    args = parser.parse_args()

    changed = False

    # LLM / ASR / OCR 凭据（统一写入 .llm.env）
    if args.llm_api_key or args.asr_api_key or args.ocr_api_key or args.ocr_model:
        # 读取已有 llm 配置并合并
        llm_entries = {}
        if LLM_ENV_PATH.exists():
            for line in LLM_ENV_PATH.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    llm_entries[k.strip()] = v.strip()

        if args.llm_api_key:
            llm_entries["llm_model"] = args.llm_model
            llm_entries["llm_base_url"] = args.llm_base_url
            llm_entries["llm_api_key"] = args.llm_api_key

        if args.asr_api_key or args.asr_base_url:
            llm_entries["asr_model"] = args.asr_model
            llm_entries["asr_base_url"] = args.asr_base_url
            llm_entries["asr_api_key"] = args.asr_api_key

        if args.ocr_model or args.ocr_base_url or args.ocr_api_key:
            llm_entries["ocr_model"] = args.ocr_model
            llm_entries["ocr_base_url"] = args.ocr_base_url
            llm_entries["ocr_api_key"] = args.ocr_api_key

        write_env_file(LLM_ENV_PATH, llm_entries)
        changed = True

    # 飞书凭据
    if args.feishu_app_id:
        write_env_file(FEISHU_ENV_PATH, {
            "feishu_app_id": args.feishu_app_id,
            "feishu_app_secret": args.feishu_app_secret,
            "feishu_chat_id": args.feishu_chat_id,
        })
        changed = True

    if not changed:
        logger.info("未提供任何凭据，无操作")
        print("用法: python3 scripts/init/credentials.py [--llm-* ...] [--asr-* ...] [--ocr-model ...] [--feishu-* ...]")
        print("B站 Cookie 扫码: python3 scripts/init/bili_get_qr.py && python3 scripts/init/bili_wait_login.py")


if __name__ == "__main__":
    main()
