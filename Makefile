ifeq ($(HOSTNAME),cache2)
COMPOSE_FILE = docker-compose.prod.yml
else
COMPOSE_FILE = docker-compose.dev.yml
endif

deploy:
	docker-compose -f $(COMPOSE_FILE) up --build --remove-orphans -d

build:
	docker-compose -f $(COMPOSE_FILE) build --pull

test:
	docker-compose -f $(COMPOSE_FILE) up --build --remove-orphans -d

stop:
	docker-compose -f $(COMPOSE_FILE) stop

down:
	docker-compose -f $(COMPOSE_FILE) down

shell:
	docker-compose -f $(COMPOSE_FILE) exec cache /bin/bash

logs:
	# Use this line to see all the logs through docker-compose's native logging
	#docker-compose -f $(COMPOSE_FILE) logs -f
	# Us this line to just `tail -f` the application logs
	docker-compose -f $(COMPOSE_FILE) exec cache /bin/bash -c 'tail -f /var/log/cache/{cache,cache.err}.log'
