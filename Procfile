simulation: lsof -ti:5002 | xargs kill -9 2>/dev/null; .venv/bin/python simulation_service.py server
coordinator: lsof -ti:5003 | xargs kill -9 2>/dev/null; .venv/bin/uvicorn coordinator_service:app --host 0.0.0.0 --port 5003
