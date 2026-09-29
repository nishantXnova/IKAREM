FROM python:3.11-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py ./cadence_app.py
COPY static ./static
ENV IKAREM_DB_URL=sqlite:///srv/data/cadence.db
VOLUME /srv/data
EXPOSE 8000
CMD ["uvicorn", "cadence_app:app", "--host", "0.0.0.0", "--port", "8000"]
