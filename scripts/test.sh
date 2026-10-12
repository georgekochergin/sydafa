#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

echo "1. Запуск unit-тестов..."
docker build -t sydafa:latest .

echo "2. Запуск сквозного последовательного e2e-тестирования СУБД..."
./scripts/test-e2e.sh
