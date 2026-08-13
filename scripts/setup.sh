#!/usr/bin/env bash
# DeepGloss 一键环境初始化:检查依赖 → 启动数据库 → 安装依赖 → 建表
# 用法(Git Bash / WSL / Linux / macOS):bash scripts/setup.sh
set -euo pipefail

echo "=============================================="
echo "  DeepGloss 环境初始化"
echo "=============================================="

# [1/4] 检查 Docker
echo ""
echo "[1/4] 检查 Docker..."
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

# [2/4] 启动数据库(Postgres pgvector + Redis)
echo ""
echo "[2/4] 启动 Postgres(pgvector)+ Redis..."
docker compose up -d
echo "  ✓ 数据库容器已启动"

# [3/4] 安装 Python 依赖
echo ""
echo "[3/4] 安装 Python 依赖(可编辑安装)..."
pip install -e ".[dev]"

# [4/4] 初始化数据库表
echo ""
echo "[4/4] 初始化数据库表..."
python scripts/init_db.py

echo ""
echo "=============================================="
echo "  ✅ 初始化完成!"
echo "  下一步:"
echo "    1. cp .env.example .env   # 填入 LLM_API_KEY 等"
echo "    2. uvicorn api.main:app --reload"
echo "  打开 http://localhost:8000/docs 查看 API 文档"
echo "=============================================="
