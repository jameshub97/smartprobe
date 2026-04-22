# simulation_service is managed by docker-compose (container on port 5002).
# Do NOT add a simulation: entry here — it would fight with the Docker container.
coordinator: lsof -ti:5003 | xargs kill -9 2>/dev/null; .venv/bin/uvicorn coordinator_service:app --host 0.0.0.0 --port 5003 --no-access-log
