cache.julialang.org
===================

Python flask-based redirector that automagically caches files on S3 so that we don't have to deal with sourceforge (and others) downtime.

## Deployment

Deploying/rebuilding the caching server is as easy as running `make` within a checked-out copy of the code.  As an example, when code changes are committed, and a deployed version of this code (Such as that which lives at `cache.julialang.org`) must be updated, doing so is as simple as SSH'ing into the server, navigating to the directory holding the code, and running `make`.  To stop the server from running, use `make down`.  Note that the `docker-compose.yml` file used by this `make` process is dependent on the hostname of the computer it is running on, so as to provide easy `dev`/`prod` separation.

## Viewing logs
To easily see logs coming from a running cache instance, simply run `make logs`.
