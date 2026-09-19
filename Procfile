web: PYTHONPATH=agent-system/backend/src python -m uvicorn agent_system.api.main:app --host 0.0.0.0 --port ${PORT:-8000}
worker: PYTHONPATH=agent-system/backend/src python -m agent_system.worker
release: cd agent-system/backend && PYTHONPATH=src python -m alembic upgrade head
