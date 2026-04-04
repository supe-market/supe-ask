FROM python:3.12-slim AS runtime

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY supe-ask/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY supe-ask/src ./src
WORKDIR /app/src

CMD ["python", "-m", "supe_ask.runner_entrypoint"]
