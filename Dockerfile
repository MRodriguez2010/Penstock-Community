FROM node:22-bookworm-slim AS workers
WORKDIR /deps
COPY package*.json ./
RUN npm ci --omit=dev --ignore-scripts --no-audit --no-fund

FROM python:3.12-slim-bookworm
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PENSTOCK_WORKSPACE=/workspace
RUN mkdir /workspace && chown 65532:65532 /workspace
COPY --from=workers /usr/local/bin/node /usr/local/bin/node
COPY --from=workers /deps/node_modules /app/node_modules
COPY requirements-*.txt ./
RUN pip install --no-cache-dir -r requirements-managed.txt -r requirements-aws.txt
COPY runtime/ runtime/
COPY LICENSE .
COPY src/ src/
COPY server/ server/
COPY examples/ examples/
COPY admin/ admin/
COPY penstock.py .
USER 65532:65532
EXPOSE 8765
CMD ["sh","-c","python penstock.py init && exec python penstock.py serve --bind 0.0.0.0"]
