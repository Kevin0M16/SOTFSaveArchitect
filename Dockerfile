# Use an official Python runtime as a parent image
FROM python:3.11-slim

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file into the container
COPY requirements.txt .

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code into the container
COPY . .

# Tell Docker that the container will listen on port 5000
EXPOSE 5000

# Create the upload directory
RUN mkdir -p /app/uploads

# Define the command to run the application using Gunicorn
# This is a production-ready server, unlike app.run()
# --workers 4 : A good starting point, adjust based on your server's CPU cores
# --timeout 120: Increase timeout for potentially long file processing
CMD ["gunicorn", "--workers", "4", "--timeout", "120", "--limit-request-line", "0", "--bind", "0.0.0.0:5000", "app:app"]
