-- Initialization script for Oracle Free container to create common superuser sydafa/sydafa
ALTER SYSTEM SET "_oracle_script"=TRUE SCOPE=BOTH;

-- Create common user sydafa across CDB and all PDBs
CREATE USER sydafa IDENTIFIED BY sydafa CONTAINER=ALL;
GRANT DBA, SYSDBA TO sydafa CONTAINER=ALL;
