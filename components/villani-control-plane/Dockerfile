FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY components/villani-ops /src/villani-ops
COPY components/villani-control-plane /src/villani-control-plane
RUN pip install --no-cache-dir /src/villani-ops /src/villani-control-plane

WORKDIR /src/villani-control-plane
EXPOSE 8000
CMD ["uvicorn", "villani_control_plane.main:app", "--host", "0.0.0.0", "--port", "8000"]
