FROM python:3.12-slim

# Prevent Python from writing .pyc and buffering stdout
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    gfortran \
    libopenblas-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt

RUN pip install --no-cache-dir -r /app/requirements.txt

COPY udp_to_triggerbuffer.py /app/udp_to_triggerbuffer.py

# 用 exec 形式：每个 token 都是一个数组元素
ENTRYPOINT ["python3", "/app/udp_to_triggerbuffer.py"]

# 给一套默认参数（你也可以在 docker run 后面覆盖它们）
CMD ["224.1.1.1:4900", "--endpoint", "http://mro.mwa128t.org/trigger/triggerbuffer", "--project-id", "C001", "--past-seconds", "120", "--obstime", "600", "--use-start-zero", "--workers", "1", "-v"]