FROM python:3.11-alpine3.23

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY components/villani-ops /src/villani-ops
COPY components/villani-control-plane /src/villani-control-plane
RUN pip install --no-cache-dir /src/villani-ops /src/villani-control-plane \
    && pip uninstall --yes setuptools wheel jaraco.context \
    && apk add --no-cache libbz2 musl libcrypto3 libffi gdbm xz-libs libncursesw libpanelw libnsl readline libtirpc libuuid zlib libssl3 \
    && apk del --no-cache .python-rundeps sqlite-libs \
    && rm -f /usr/local/lib/python3.11/lib-dynload/_sqlite3*.so \
    && rm -rf /root/.cache
RUN adduser --disabled-password --uid 10001 villani && chown -R villani:villani /src/villani-control-plane

WORKDIR /src/villani-control-plane
USER 10001
EXPOSE 8000
CMD ["uvicorn", "villani_control_plane.main:app", "--host", "0.0.0.0", "--port", "8000"]
