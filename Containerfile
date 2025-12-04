# BUILDER
FROM python:3.9-slim as compiler
LABEL org.opencontainers.image.source="https://github.com/oats-center/ASREC"
ENV PYTHONUNBUFFERED 1
WORKDIR /usr/src/app

# Activate virtualenv
RUN python -m venv /opt/venv

# Make sure we use the virtualenv
ENV PATH="/opt/venv/bin:$PATH"

# Copy requirements and build with pip
COPY ./requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt


# RUNTIME
FROM python:3.9-slim as runner
WORKDIR /usr/src/app

# Copy compiled venv from builder
COPY --from=compiler /opt/venv /opt/venv

# Make sure we use the virtualenv
ENV PATH="/opt/venv/bin:$PATH"



# Copy script over and run
COPY watcher.py .
CMD [ "python", "./watcher.py" ]
