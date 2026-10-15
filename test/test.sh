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
TEST_ID="$$"
TEST_NET="sydafa-test-net-${TEST_ID}"
CONF_DIR="/tmp/sydafa-e2e-conf-${TEST_ID}"
SRC_PREFIX="test-${TEST_ID}-"
TEST_CONTAINER="sydafa-test-${TEST_ID}"
TEST_STATE_VOL="sydafa-test-state-${TEST_ID}"
TARGET_CONTAINER_PREFIX="sydafa-${SRC_PREFIX}"
TARGET_VOLUME_PREFIX="sydafa-${SRC_PREFIX}"

# Свободный порт на хосте, гарантирующий отсутствие коллизий (с 8000, 8090 и параллельными запусками)
TEST_PORT=$(python3 -c 'import socket; s=socket.socket(); s.bind(("", 0)); print(s.getsockname()[1]); s.close()')

cleanup() {
  echo "Очистка тестового окружения (TEST_ID=${TEST_ID})..."
  # 1. Принудительно удаляем контейнеры СУБД с префиксом этого тестового прогона
  local c_ids
  c_ids=$(docker ps -aq --filter "name=^${TARGET_CONTAINER_PREFIX}" 2>/dev/null || true)
  if [ -n "$c_ids" ]; then
    echo "$c_ids" | xargs docker rm -f >/dev/null 2>&1 || true
  fi
  # Удаляем тестовый API-контейнер sydafa
  docker rm -f "$TEST_CONTAINER" >/dev/null 2>&1 || true

  # 2. Удаляем тома данных СУБД и том состояния теста
  local v_ids
  v_ids=$(docker volume ls -q --filter "name=^${TARGET_VOLUME_PREFIX}" 2>/dev/null || true)
  if [ -n "$v_ids" ]; then
    echo "$v_ids" | xargs docker volume rm -f >/dev/null 2>&1 || true
  fi
  docker volume rm -f "$TEST_STATE_VOL" >/dev/null 2>&1 || true

  # 3. Удаляем изолированную сеть
  docker network rm "$TEST_NET" >/dev/null 2>&1 || true

  # 4. Удаляем временную папку конфига
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

  docker volume create "$TEST_STATE_VOL" >/dev/null

  echo "Запуск сервиса sydafa для MSSQL (порт ${TEST_PORT}, префикс ${SRC_PREFIX})..."
  docker run -d --name "$TEST_CONTAINER" \
    --network "$TEST_NET" \
    -p "${TEST_PORT}:8000" \
    -e SYDAFA_CONTAINER_PREFIX="$SRC_PREFIX" \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v "$CONF_DIR/init.json:/app/config/init.json:ro" \
    -v "${TEST_STATE_VOL}:/state" \
    sydafa:latest

  echo "Ожидание готовности API sydafa (источник MSSQL поднимается лениво)..."
  until curl -s "http://localhost:${TEST_PORT}/health" | grep -q '"status":"ok"'; do
    sleep 2
  done

  SYDAFA_E2E_PREFIX="$SRC_PREFIX" python3 test/test_e2e_source.py --url "http://localhost:${TEST_PORT}" --source mssql
  local rc=$?
  if [ $rc -ne 0 ]; then
    TOTAL_FAIL=$((TOTAL_FAIL + 1))
  else
    TOTAL_PASS=$((TOTAL_PASS + 1))
  fi

  echo "Остановка MSSQL и очистка перед следующей фазой..."
  docker rm -f "$TEST_CONTAINER" >/dev/null 2>&1 || true
  local c_ids
  c_ids=$(docker ps -aq --filter "name=^${TARGET_CONTAINER_PREFIX}" 2>/dev/null || true)
  if [ -n "$c_ids" ]; then
    echo "$c_ids" | xargs docker rm -f >/dev/null 2>&1 || true
  fi
  local v_ids
  v_ids=$(docker volume ls -q --filter "name=^${TARGET_VOLUME_PREFIX}" 2>/dev/null || true)
  if [ -n "$v_ids" ]; then
    echo "$v_ids" | xargs docker volume rm -f >/dev/null 2>&1 || true
  fi
  docker volume rm -f "$TEST_STATE_VOL" >/dev/null 2>&1 || true
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

  docker volume create "$TEST_STATE_VOL" >/dev/null

  echo "Запуск сервиса sydafa для Oracle (порт ${TEST_PORT}, префикс ${SRC_PREFIX})..."
  docker run -d --name "$TEST_CONTAINER" \
    --network "$TEST_NET" \
    -p "${TEST_PORT}:8000" \
    -e SYDAFA_CONTAINER_PREFIX="$SRC_PREFIX" \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v "$CONF_DIR/init.json:/app/config/init.json:ro" \
    -v "${TEST_STATE_VOL}:/state" \
    sydafa:latest

  echo "Ожидание готовности API sydafa (источник Oracle поднимается лениво)..."
  until curl -s "http://localhost:${TEST_PORT}/health" | grep -q '"status":"ok"'; do
    sleep 3
  done

  SYDAFA_E2E_PREFIX="$SRC_PREFIX" python3 test/test_e2e_source.py --url "http://localhost:${TEST_PORT}" --source oracle
  local rc=$?
  if [ $rc -ne 0 ]; then
    TOTAL_FAIL=$((TOTAL_FAIL + 1))
  else
    TOTAL_PASS=$((TOTAL_PASS + 1))
  fi

  echo "Остановка Oracle и очистка..."
  docker rm -f "$TEST_CONTAINER" >/dev/null 2>&1 || true
  local c_ids
  c_ids=$(docker ps -aq --filter "name=^${TARGET_CONTAINER_PREFIX}" 2>/dev/null || true)
  if [ -n "$c_ids" ]; then
    echo "$c_ids" | xargs docker rm -f >/dev/null 2>&1 || true
  fi
  local v_ids
  v_ids=$(docker volume ls -q --filter "name=^${TARGET_VOLUME_PREFIX}" 2>/dev/null || true)
  if [ -n "$v_ids" ]; then
    echo "$v_ids" | xargs docker volume rm -f >/dev/null 2>&1 || true
  fi
  docker volume rm -f "$TEST_STATE_VOL" >/dev/null 2>&1 || true
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
