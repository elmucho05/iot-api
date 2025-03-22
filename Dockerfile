# Use the official Python image
FROM python:3.11

# Set the working directory inside the container
WORKDIR /app

# Copy only the requirements file first (for caching)
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Copy the rest of your project files
COPY . .

# Expose the port you want the app to run on
EXPOSE 8888

# Run FastAPI with Uvicorn on port 8888
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8888"]

