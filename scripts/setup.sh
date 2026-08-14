#!/usr/bin/env bash
# DeepGloss 一键环境初始化:检查 Docker/conda → 启动数据+模型服务 → 安装依赖 → 建表
# 用法(conda deepgloss 环境内, Git Bash / WSL / Linux / macOS):bash scripts/setup.sh
set -euo pipefail

echo "=============================================="
echo "  DeepGloss 环境初始化"
echo "=============================================="

# [1/5] 检查 conda deepgloss 环境
echo ""
echo "[1/5] 检查 conda 环境..."
if [[ "${CONDA_DEFAULT_ENV:-}" != "deepgloss" ]]; then
    echo "  ! 当前不在 deepgloss conda 环境(CONDA_DEFAULT_ENV=${CONDA_DEFAULT_ENV:-<none>})"
    echo "  → 请先: conda create -n deepgloss python=3.11 -y && conda activate deepgloss"
    exit 1
fi
echo "  ✓ conda deepgloss 环境已激活"

# [2/5] 检查 Docker
echo ""
echo "[2/5] 检查 Docker..."
if ! command -v docker &>/dev/null; then
    echo "  ✗ 未检测到 Docker,请先安装 Docker Desktop: https://www.docker.com/products/docker-desktop/"
    exit 1
fi
if ! docker info &>/dev/null 2>&1; then
    echo "  ✗ Docker 已安装,但守护进程未运行"
    echo "  → 请先启动 Docker Desktop,再重新运行本脚本"
    exit 1
fi
echo "  ✓ Docker 正常"

# [3/5] 启动数据 + 模型服务(Postgres/pgvector + Redis + TEI + Kokoro + LiteLLM)
echo ""
echo "[3/5] 启动数据 + 模型服务..."
docker compose up -d postgres redis embedding tts llm-gateway
echo "  ✓ 容器已启动(首次会下载模型权重,需要几分钟)"

# [4/5] 安装 Python 依赖
echo ""
echo "[4/5] 安装 Python 依赖(可编辑安装)..."
pip install -e ".[dev]"

# [5/5] 初始化数据库表
echo ""
echo "[5/5] 初始化数据库表..."
python scripts/init_db.py

echo ""
echo "=============================================="
echo "  ✅ 初始化完成!"
echo "  下一步:"
echo "    1. cp .env.example .env   # 填入 LLM_UPSTREAM_KEY"
echo "    2. uvicorn api.main:app --reload"
echo "  打开 http://localhost:8000/docs 查看 API 文档"
echo "=============================================="
