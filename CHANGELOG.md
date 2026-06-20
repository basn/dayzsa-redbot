# Changelog

## 2026-06-20

- Improve queue handling for DayZ monitor by adding Steam A2S_INFO fallback queries for queue/status data.
- Parse DayZ `lqsN` queue values from A2S payloads and expose queue when launcher API payloads omit it.
- Add fallback queue extraction from raw A2S payload bytes to handle keyword-layout variations.
- Optimize status path to skip A2S fallback when launcher API already contains `online`, `max_players`, and `queue`.
