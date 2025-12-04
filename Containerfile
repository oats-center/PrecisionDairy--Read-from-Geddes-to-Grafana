# BUILDER
FROM python:3.12.12
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
FROM python:3.12.12

WORKDIR /usr/src/app

# Copy compiled venv from builder
COPY --from=builder /opt/venv /opt/venv

# Make sure we use the virtualenv
ENV PATH="/opt/venv/bin:$PATH"

# Copy health check script
#COPY healthcheck.py .
#HEALTHCHECK CMD ["python", "./healthcheck.py"]

# Copy script over and run
COPY watcher.py .
CMD [ "python", "./watcher.py" ]
