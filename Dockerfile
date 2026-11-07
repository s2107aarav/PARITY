FROM python:3.12-slim
WORKDIR /opt/parity
COPY requirements.lock pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.lock
COPY parity ./parity
COPY apps ./apps
RUN pip install --no-deps . && useradd --uid 10001 --create-home parity
USER 10001
ENTRYPOINT ["parity"]
