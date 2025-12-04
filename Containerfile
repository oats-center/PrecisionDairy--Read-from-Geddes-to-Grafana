# BUILDER
FROM python:3.9-slim as compiler
LABEL org.opencontainers.image.source="https://github.com/oats-center/ASREC"
WORKDIR /usr/src/app

# Activate virtualenv
RUN python -m venv /opt/venv

# Make sure we use the virtualenv
ENV PATH="/opt/venv/bin:$PATH"

# Copy requirements and build with pip
COPY requirements.txt ./
RUN pip install -r requirements.txt



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
