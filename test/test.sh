#!/usr/bin/env bash
# Единый скрипт тестирования sydafa.
# Запуск:
#   ./test/test.sh                  — полный цикл: unit-тесты (сборка) + сквозной e2e (MSSQL -> Oracle)
#   ./test/test.sh --unit           — только unit-тесты при сборке образа
#   ./test/test.sh --e2e            — только сквозное e2e-тестирование
#   ./test/test.sh --source <name>  — e2e-тестирование только для указанного источника (mssql или oracle)
set -uo pipefail
cd "$(dirname "$0")/.."

RUN_UNIT=true
RUN_E2E=true
SELECTED_SOURCE=""

while [ $# -gt 0 ]; do
  case "$1" in
    --unit)
      RUN_UNIT=true
      RUN_E2E=false
      shift
      ;;
    --e2e)
      RUN_UNIT=false
      RUN_E2E=true
      shift
      ;;
    --source)
      SELECTED_SOURCE="$2"
      RUN_UNIT=false
      RUN_E2E=true
      shift 2
      ;;
    --help|-h)
      echo "Использование: $0 [--unit | --e2e | --source <mssql|oracle> | --help]"
      echo "  (без аргументов) Запустить unit-тесты и полный сквозной e2e для всех источников"
      echo "  --unit           Запустить только unit-тесты (внутри Dockerfile)"
      echo "  --e2e            Запустить только e2e-тесты (MSSQL и Oracle)"
      echo "  --source <name>  Запустить e2e-тесты только для указанного источника (mssql или oracle)"
      exit 0
      ;;
    *)
      echo "Неизвестный аргумент: $1"
      echo "Используйте --help для справки."
      exit 1
      ;;
  esac
done

# ==================== 1. Unit-тесты ====================
if [ "$RUN_UNIT" = true ]; then
  echo "==> [1/2] Запуск unit-тестов при сборке образа sydafa:latest..."
  docker build -t sydafa:latest .
  rc=$?
  if [ $rc -ne 0 ]; then
    echo "ОШИБКА: Сборка образа и unit-тесты завершились сбоем!"
    exit $rc
  fi
  echo "Unit-тесты успешно пройдены."
  if [ "$RUN_E2E" = false ]; then
    exit 0
  fi
fi

# ==================== 2. E2E-тестирование ====================
WORK_DIR=$(pwd)
TEST_NET="sydafa-test-net"
CONF_DIR="/tmp/sydafa-e2e-conf-$$"
SRC_PREFIX="sydafa-test-"

cleanup() {
  echo "Очистка тестового окружения..."
  docker rm -f sydafa-test "${SRC_PREFIX}mssql-source" "${SRC_PREFIX}oracle-source" >/dev/null 2>&1 || true
  docker volume rm -f sydafa-test-state "${SRC_PREFIX}mssql-data" "${SRC_PREFIX}oracle-data" >/dev/null 2>&1 || true
  docker network rm "$TEST_NET" >/dev/null 2>&1 || true
  rm -rf "$CONF_DIR"
}
trap cleanup EXIT INT TERM

cleanup

# Если образ ещё не собран, собираем его
if ! docker image inspect sydafa:latest >/dev/null 2>&1; then
  echo "==> Сборка образа sydafa:latest..."
  docker build -t sydafa:latest .
fi

docker network create "$TEST_NET" >/dev/null 2>&1
mkdir -p "$CONF_DIR"

TOTAL_PASS=0
TOTAL_FAIL=0

test_mssql() {
  echo ""
  echo "=================================================="
  echo ">>> ФАЗА: Тестирование источника MSSQL <<<"
  echo "=================================================="

  cat << 'EOF' > "$CONF_DIR/init.json"
{
  "sources": {
    "mssql": {
      "type": "mssql",
      "operations": []
    }
  }
}
EOF

  docker volume create sydafa-test-state >/dev/null

  echo "Запуск сервиса sydafa для MSSQL..."
  docker run -d --name sydafa-test \
    --network "$TEST_NET" \
    -p 8000:8000 \
    -e SYDAFA_CONTAINER_PREFIX="$SRC_PREFIX" \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v "$CONF_DIR/init.json:/app/config/init.json:ro" \
    -v sydafa-test-state:/state \
    sydafa:latest

  echo "Ожидание готовности API sydafa (источник MSSQL поднимается лениво)..."
  until curl -s http://localhost:8000/health | grep -q '"status":"ok"'; do
    sleep 2
  done

  python3 test/test_e2e_source.py --url http://localhost:8000 --source mssql
  local rc=$?
  if [ $rc -ne 0 ]; then
    TOTAL_FAIL=$((TOTAL_FAIL + 1))
  else
    TOTAL_PASS=$((TOTAL_PASS + 1))
  fi

  echo "Остановка MSSQL и очистка перед следующей фазой..."
  docker rm -f sydafa-test "${SRC_PREFIX}mssql-source" >/dev/null 2>&1
  docker volume rm -f sydafa-test-state "${SRC_PREFIX}mssql-data" >/dev/null 2>&1
  rm -f "$CONF_DIR/init.json"
}

test_oracle() {
  echo ""
  echo "=================================================="
  echo ">>> ФАЗА: Тестирование источника Oracle <<<"
  echo "=================================================="

  cat << 'EOF' > "$CONF_DIR/init.json"
{
  "sources": {
    "oracle": {
      "type": "oracle",
      "operations": []
    }
  }
}
EOF

  docker volume create sydafa-test-state >/dev/null

  echo "Запуск сервиса sydafa для Oracle..."
  docker run -d --name sydafa-test \
    --network "$TEST_NET" \
    -p 8000:8000 \
    -e SYDAFA_CONTAINER_PREFIX="$SRC_PREFIX" \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v "$CONF_DIR/init.json:/app/config/init.json:ro" \
    -v sydafa-test-state:/state \
    sydafa:latest

  echo "Ожидание готовности API sydafa (источник Oracle поднимается лениво)..."
  until curl -s http://localhost:8000/health | grep -q '"status":"ok"'; do
    sleep 3
  done

  python3 test/test_e2e_source.py --url http://localhost:8000 --source oracle
  local rc=$?
  if [ $rc -ne 0 ]; then
    TOTAL_FAIL=$((TOTAL_FAIL + 1))
  else
    TOTAL_PASS=$((TOTAL_PASS + 1))
  fi

  echo "Остановка Oracle и очистка..."
  docker rm -f sydafa-test "${SRC_PREFIX}oracle-source" >/dev/null 2>&1
  docker volume rm -f sydafa-test-state "${SRC_PREFIX}oracle-data" >/dev/null 2>&1
  rm -f "$CONF_DIR/init.json"
}

if [ -z "$SELECTED_SOURCE" ] || [ "$SELECTED_SOURCE" = "mssql" ]; then
  test_mssql
fi

if [ -z "$SELECTED_SOURCE" ] || [ "$SELECTED_SOURCE" = "oracle" ]; then
  test_oracle
fi

echo ""
echo "=================================================="
echo "Итоговый результат e2e: Успешно=$TOTAL_PASS, Сбоев=$TOTAL_FAIL"
echo "=================================================="

[ "$TOTAL_FAIL" -eq 0 ]
