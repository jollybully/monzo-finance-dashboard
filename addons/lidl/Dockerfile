FROM python:3.12-slim

WORKDIR /app

COPY addons/lidl/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY addons/__init__.py ./addons/__init__.py
COPY addons/receipts ./addons/receipts
COPY addons/lidl ./addons/lidl

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

CMD ["python", "-m", "addons.lidl"]
