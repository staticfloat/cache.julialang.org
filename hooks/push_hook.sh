#!/bin/bash

# We know that the code is mounted in /code, so go there
cd /cache.julialang.org

# Update the code and redeploy
make self-upgrade
