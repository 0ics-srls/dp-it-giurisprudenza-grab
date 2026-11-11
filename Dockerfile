# dp-it-giurisprudenza-grab production image (Python). proc-protocol stdin loop on main.py. PyPI deps only.
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py ./
COPY src ./src
ENV PYTHONPATH=/app/src
ENTRYPOINT ["python", "main.py"]
