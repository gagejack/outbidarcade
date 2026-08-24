# see.io site contract: FastAPI on :8080, state ONLY under /data (the one
# path that survives deploys — the volume is mounted at runtime).
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8080
# One worker on purpose. The rate limiter (main.py) and the board cache (db.py)
# both keep state in process memory, so extra workers would each get their own
# copy: limits would multiply by worker count and caches would diverge after a
# write. Routes are sync def, so FastAPI already serves them from a threadpool.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
