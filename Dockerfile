FROM python:3.12.16-slim AS builder
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY requirements.lock .
RUN pip wheel --no-cache-dir --require-hashes --wheel-dir /wheels -r requirements.lock

FROM python:3.12.16-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN addgroup --system app && adduser --system --ingroup app app
WORKDIR /app
COPY --from=builder /wheels /wheels
COPY requirements.lock .
RUN pip install --no-cache-dir --no-index --require-hashes --find-links=/wheels -r requirements.lock && rm -rf /wheels
# The code and the collected static files belong to root and are read-only for the app user,
# so a bug or a hijacked worker cannot rewrite the application. Nothing under /app is written
# at run time (uploads use /tmp; the backup folders are mounted volumes).
COPY . .
RUN python manage.py collectstatic --noinput && chmod -R a+rX,go-w /app
USER app
EXPOSE 8000
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["gunicorn", "nononcents.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--timeout", "180", "--access-logfile", "-"]
