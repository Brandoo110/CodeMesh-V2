# CodeMesh 项目命令快捷方式
#
# Python 环境：项目专属 .venv（Python 3.14，PEP 405 标准 venv）
# 为什么不用 miniconda：CodeMesh 是纯 Python 项目，不需要 CUDA/GPU/多版本切换
# 详见 docs/ui-design-plan.md §0 + docs/decisions/0006-web-ui-stack-fastapi-nextjs.md

PYTHON := .venv/bin/python
PIP := .venv/bin/pip

.PHONY: help venv install ui-backend ui-frontend test clean

help:
	@echo "CodeMesh Makefile commands:"
	@echo ""
	@echo "  make venv         Create .venv (Python venv, PEP 405)"
	@echo "  make install      pip install -e .[web,skills,tokens] into .venv"
	@echo ""
	@echo "  make ui-backend   Run FastAPI on :8000 (uvicorn reload)"
	@echo "  make ui-frontend  Run Next.js on :3000 (pnpm dev) — 另开 terminal"
	@echo ""
	@echo "  make test         Run all Python tests via .venv python"
	@echo "  make clean        Remove .venv, __pycache__, *.egg-info"
	@echo ""
	@echo "Quickstart for Web UI (Phase 0 demo):"
	@echo "  Terminal 1:  make ui-backend"
	@echo "  Terminal 2:  make ui-frontend"
	@echo "  Browser:     http://localhost:3000 + curl http://localhost:8000/api/health"

venv:
	python3 -m venv .venv
	$(PIP) install --upgrade pip wheel

install:
	$(PIP) install -e ".[web,skills,tokens]"

ui-backend:
	$(PYTHON) -m uvicorn web.server:app --reload --port 8000

ui-frontend:
	cd frontend && pnpm dev

test:
	@for t in tests/test_*.py; do \
		name=$$(basename $$t .py); \
		echo "▶ Running tests.$$name"; \
		$(PYTHON) -m unittest -v "tests.$$name" || exit 1; \
	done

clean:
	rm -rf .venv __pycache__ */__pycache__ *.egg-info build dist
	find . -name "*.pyc" -delete
