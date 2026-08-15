FROM python:3.14-slim
WORKDIR /app
COPY bot.py .
RUN pip install --no-cache-dir "python-telegram-bot>=22.7" "python-dotenv>=1.0"
CMD ["python", "bot.py"]
