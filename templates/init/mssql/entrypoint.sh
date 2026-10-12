#!/usr/bin/env bash
set -e

# Start SQL Server in background
/opt/mssql/bin/sqlservr &
pid=$!

# Wait for SQL Server to be ready
echo "Waiting for SQL Server to start..."
until /opt/mssql-tools18/bin/sqlcmd -C -S localhost -U sa -P "$MSSQL_SA_PASSWORD" -Q 'SELECT 1' >/dev/null 2>&1; do
    sleep 1
done

echo "SQL Server is ready. Ensuring superuser sydafa/sydafa exists..."
/opt/mssql-tools18/bin/sqlcmd -C -S localhost -U sa -P "$MSSQL_SA_PASSWORD" -Q "
IF NOT EXISTS (SELECT * FROM sys.server_principals WHERE name = 'sydafa')
BEGIN
    CREATE LOGIN sydafa WITH PASSWORD = 'sydafa', CHECK_POLICY = OFF;
    ALTER SERVER ROLE sysadmin ADD MEMBER sydafa;
END"

echo "Superuser sydafa/sydafa initialized successfully."
wait "$pid"
