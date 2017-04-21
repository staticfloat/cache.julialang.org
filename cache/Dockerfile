FROM python

# For debugging only
RUN apt update && apt install -y gcc python-dev vim
RUN pip install ipython

# Install our secret AWS credentials where boto can find 'em
ARG AWS_ACCESS_KEY
ARG AWS_SECRET_KEY

# Make the ~/.aws/config file
RUN mkdir ~/.aws && \
    echo "[default]"         > ~/.aws/config && \
    echo "region=us-east-1" >> ~/.aws/config

# Make the ~/.aws/credentials file
RUN echo "[default]"                                  > ~/.aws/credentials && \
    echo "aws_access_key_id = ${AWS_ACCESS_KEY}"     >> ~/.aws/credentials && \
    echo "aws_secret_access_key = ${AWS_SECRET_KEY}" >> ~/.aws/credentials


# Copy our code up
WORKDIR /app
COPY requirements.txt /app/
RUN pip install -r requirements.txt
RUN rm -f requirements.txt
COPY *.py /app/

CMD ["python", "cache.py"]
STOPSIGNAL INT
