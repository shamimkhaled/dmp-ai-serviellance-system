-- Keycloak persistence (separate from VMS policeai). Init runs only on empty volumes.
SELECT 'CREATE DATABASE keycloak'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'keycloak')\gexec
