FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    OMP_NUM_THREADS=4 \
    MKL_NUM_THREADS=4

COPY requirements.lock.txt .
RUN pip install --no-cache-dir --disable-pip-version-check --extra-index-url https://download.pytorch.org/whl/cpu -r requirements.lock.txt

RUN useradd --system --no-create-home mlapp && install -d -o mlapp /app/artifacts /models
COPY . .
USER mlapp
EXPOSE 8002
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8002", "--workers", "1"]
