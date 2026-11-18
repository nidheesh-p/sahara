# Project Heron — status report, week 14

Owner: Ravi Lindqvist
Sponsor: Teodora Vance

## Summary

Project Heron moves the reporting data warehouse from the on-site
database server to a managed cloud warehouse. Overall status: AMBER.

## Progress this week

- 31 of 44 nightly ETL jobs ported and running in parallel with the old
  pipeline; row counts match for all ported jobs.
- Finance dashboards repointed to the new warehouse in the staging
  environment.

## Risks

- Historical sales tables (2016–2019) contain inconsistent currency codes;
  cleansing may delay cut-over by two weeks.
- Only one engineer understands the legacy stored procedures.
- Egress costs during the parallel-run period are above estimate.

## Next steps

Finish the remaining 13 jobs, schedule a dry-run cut-over, and agree the
decommission date for the old server.
