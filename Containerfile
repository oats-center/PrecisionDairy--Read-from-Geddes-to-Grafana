# BUILDER
 # FROM sets the Docker image that is a a standalone, executable file used to create a container. It changes depending on the type of app (python, ubuntu, php, etc)
   # Look for the images here : https://hub.docker.com/
 # LABEL tells GitHub where the source is located
 # WORKDIR defines a virtual environment to work
FROM python:3.14.0-slim-bookworm as builder
LABEL org.opencontainers.image.source="https://github.com/oats-center/ASREC"
WORKDIR /usr/src/app

# Activate virtualenv
RUN python -m venv /opt/venv

# Make sure we use the virtualenv
ENV PATH="/opt/venv/bin:$PATH"

# Copy requirements and build with 'pip' in the directory defined at WORKDIR
COPY requirements.txt ./
# Since it is using a python image, it is possible to run 'pip' because the image contains the programming languaje and the packages administrator, which means that operates as a python interface
RUN pip install --no-cache-dir -r requirements.txt


# RUNTIME
FROM python:3.14.0-slim-bookworm as runtime
WORKDIR /usr/src/app

# Copy compiled venv from builder
COPY --from=builder /opt/venv /opt/venv

# Make sure we use the virtualenv
ENV PATH="/opt/venv/bin:$PATH"



# Copy the script that has the app we want to execute
# CMD allows to run  what we ant to execute, the first parameter is the 
COPY watcher.py .
CMD [ "python", "./watcher.py" ]
