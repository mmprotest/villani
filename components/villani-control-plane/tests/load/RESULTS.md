# Latest load smoke result

- Date: 2026-07-11
- Database: PostgreSQL 16 Alpine, local Docker Desktop on Windows
- Application path: v2 schema validation, ingestion service, SQLAlchemy persistence, and
  same-transaction outbox writes
- Events: 100,000
- Duration: 319.061 seconds
- Measured throughput: 313.4 events/second
- PostgreSQL database size after the run: 235,011,095 bytes
- Test result: passed

This is a development-host smoke measurement, not a production SLO or capacity claim.
