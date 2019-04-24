ifeq ($(HOSTNAME),cache)
COMPOSE_FILE = docker-compose.prod.yml
else
COMPOSE_FILE = docker-compose.dev.yml
endif

deploy:
	docker-compose -f $(COMPOSE_FILE) up --build --remove-orphans -d
# "test" is just an alias for "deploy"
test: deploy

# This gets automatically run by a webhook when a PR gets merged to the github repo
# This is kind of dirty, should probably migrate to docker watchtower instead
self-upgrade:
	git pull
	docker-compose -f docker-compose.prod.yml up --build -d cache

# This builds the docker image but doesn't actually bring it up.
build:
	docker-compose -f $(COMPOSE_FILE) build --pull

# Stops the docker container btu doesn't delete anything
stop:
	docker-compose -f $(COMPOSE_FILE) stop

# Stops the docker container and destroys various bits of state like networking interfaces
down:
	docker-compose -f $(COMPOSE_FILE) down --remove-orphans

# Launches an interactive shell inside of the `cache` service
shell:
	docker-compose -f $(COMPOSE_FILE) exec cache /bin/bash

# Displays a live view of the logs of the `cache` service
logs:
	# Use this line to see all the logs through docker-compose's native logging
	#docker-compose -f $(COMPOSE_FILE) logs -f
	# Us this line to just `tail -f` the application logs
	docker-compose -f $(COMPOSE_FILE) exec cache /bin/bash -c 'tail -f /var/log/cache/{cache,cache.err}.log'
