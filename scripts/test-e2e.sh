#!/usr/bin/env bash
# Полноценное последовательное e2e-тестирование источников в Docker (MSSQL -> затем Oracle).
set -uo pipefail

cd "$(dirname "$0")/.."
WORK_DIR=$(pwd)
TEST_NET="sydafa-test-net"
CONF_DIR="/tmp/sydafa-e2e-conf-$$"

cleanup() {
  echo "Очистка тестового окружения..."
  docker rm -f sydafa-test mssql-source oracle-source >/dev/null 2>&1 || true
  docker volume rm -f sydafa-test-state >/dev/null 2>&1 || true
  docker network rm "$TEST_NET" >/dev/null 2>&1 || true
  rm -rf "$CONF_DIR"
}
trap cleanup EXIT INT TERM

cleanup

# 0. Сборка локального образа sydafa
echo "==> Сборка образа sydafa:latest..."
docker build -t sydafa:latest .

docker network create "$TEST_NET" >/dev/null 2>&1
mkdir -p "$CONF_DIR/sources"

TOTAL_PASS=0
TOTAL_FAIL=0

# ==================== ФАЗА 1: MSSQL ====================
echo ""
echo "=================================================="
echo ">>> ФАЗА 1: Тестирование источника MSSQL <<<"
echo "=================================================="

cat << 'EOF' > "$CONF_DIR/sources/mssql.yaml"
type: mssql
host: mssql-source
port: 1433
reader: reader
EOF

docker volume create sydafa-test-state >/dev/null

echo "Запуск сервиса sydafa для MSSQL..."
docker run -d --name sydafa-test \
  --network "$TEST_NET" \
  -p 8000:8000 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$CONF_DIR:/app/config:ro" \
  -v sydafa-test-state:/state \
  -e MSSQL_SA_PASSWORD="AdminPassword1!" \
  sydafa:latest

echo "Ожидание готовности MSSQL через sydafa API (до 180s)..."
until curl -s http://localhost:8000/health | grep -q '"status":"ok"'; do
  sleep 2
done

# Запуск тестов внутри контейнера через docker exec или на хосте
python3 tests/test_e2e_source.py --url http://localhost:8000 --source mssql
rc=$?
if [ $rc -ne 0 ]; then
  TOTAL_FAIL=$((TOTAL_FAIL + 1))
else
  TOTAL_PASS=$((TOTAL_PASS + 1))
fi

echo "Остановка MSSQL и очистка перед следующей фазой..."
docker rm -f sydafa-test mssql-source >/dev/null 2>&1
docker volume rm -f sydafa-test-state >/dev/null 2>&1
rm -f "$CONF_DIR/sources/mssql.yaml"

# ==================== ФАЗА 2: ORACLE ====================
echo ""
echo "=================================================="
echo ">>> ФАЗА 2: Тестирование источника Oracle <<<"
echo "=================================================="

cat << 'EOF' > "$CONF_DIR/sources/oracle.yaml"
type: oracle
host: oracle-source
port: 1521
reader: reader
EOF

docker volume create sydafa-test-state >/dev/null

echo "Запуск сервиса sydafa для Oracle..."
docker run -d --name sydafa-test \
  --network "$TEST_NET" \
  -p 8000:8000 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$CONF_DIR:/app/config:ro" \
  -v sydafa-test-state:/state \
  -e ORACLE_PASSWORD="admin" \
  sydafa:latest

echo "Ожидание готовности Oracle через sydafa API (до 240s)..."
until curl -s http://localhost:8000/health | grep -q '"status":"ok"'; do
  sleep 3
done

python3 tests/test_e2e_source.py --url http://localhost:8000 --source oracle
rc=$?
if [ $rc -ne 0 ]; then
  TOTAL_FAIL=$((TOTAL_FAIL + 1))
else
  TOTAL_PASS=$((TOTAL_PASS + 1))
fi

echo ""
echo "=================================================="
echo "Итоговый результат e2e: Успешно=$TOTAL_PASS, Сбоев=$TOTAL_FAIL"
echo "=================================================="

[ "$TOTAL_FAIL" -eq 0 ]
