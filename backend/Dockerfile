FROM python:3.11-slim
WORKDIR /app
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/app/ ./app/
COPY data/queries.csv ./data/queries.csv
EXPOSE 8000
CMD ["sh", "-c", "python -m app.core.load_dataset ./data/queries.csv && python -m uvicorn app.main:app --host 0.0.0.0 --port 8000"]
