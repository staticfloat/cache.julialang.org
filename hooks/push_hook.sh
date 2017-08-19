#!/bin/bash

# We know that the code is mounted in /code, so go there
cd /code

# Update the code and redeploy
git pull && make build && make deploy
