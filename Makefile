.PHONY: up down restart build logs retrain ps clean

# ── Primary commands ──────────────────────────────────────────────────────────

## Start all services (detached)
up:
	docker compose up -d

## Stop all services
down:
	docker compose down

## Rebuild images and restart
build:
	docker compose build --no-cache

## Restart a specific service: make restart svc=predictor
restart:
	docker compose restart $(svc)

## Tail logs for all services
logs:
	docker compose logs -f

## Tail logs for one service: make log svc=predictor
log:
	docker compose logs -f $(svc)

## Show running containers
ps:
	docker compose ps

## Forge leaderboard (quick terminal view)
leaderboard:
	curl -s http://localhost:18912/leaderboard?min_trades=3 | python3 -m json.tool

## Forge open simulated positions
forge-open:
	curl -s http://localhost:18912/open | python3 -m json.tool

# ── Retrain ───────────────────────────────────────────────────────────────────

## Run a full retrain (blocks until complete)
retrain:
	docker compose --profile retrain run --rm retrain

## Retrain with custom AUC gate: make retrain-auc auc=0.60
retrain-auc:
	MIN_AUC=$(auc) docker compose --profile retrain run --rm retrain

# ── Maintenance ───────────────────────────────────────────────────────────────

## Remove stopped containers and dangling images
clean:
	docker compose down --remove-orphans
	docker image prune -f

## Shell into a service: make shell svc=predictor
shell:
	docker compose exec $(svc) /bin/bash
