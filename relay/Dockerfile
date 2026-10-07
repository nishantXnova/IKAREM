FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py static/ ./
ENV IKAREM_DB_URL=sqlite:////data/relay.db IKAREM_DEMO=false
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/readyz')"
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
