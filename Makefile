ifeq ($(HOSTNAME),cache2)
COMPOSE_FILE = docker-compose.prod.yml
else
COMPOSE_FILE = docker-compose.dev.yml
endif


deploy:
	docker-compose -f $(COMPOSE_FILE) up --build --remove-orphans -d

test:
	docker-compose -f $(COMPOSE_FILE) up --build --remove-orphans -d

stop:
	docker-compose -f $(COMPOSE_FILE) stop

down:
	docker-compose -f $(COMPOSE_FILE) down

shell:
	docker-compose -f $(COMPOSE_FILE) exec cache /bin/bash

logs:
	#docker-compose -f $(COMPOSE_FILE) logs -f
	docker-compose -f $(COMPOSE_FILE) exec cache /bin/bash -c 'tail -f /var/log/cache/{cache,cache.err}.log'
