FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PENSTOCK_WORKSPACE=/workspace
RUN mkdir /workspace && chown 65532:65532 /workspace
COPY src/ src/
COPY server/ server/
COPY examples/ examples/
COPY penstock.py .
USER 65532:65532
EXPOSE 8765
CMD ["sh","-c","python penstock.py init && exec python penstock.py serve --bind 0.0.0.0"]
