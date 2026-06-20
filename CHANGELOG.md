# Changelog

## 2026-06-20

- Add A2S_RULES fallback for queue extraction when `A2S_INFO` does not expose DayZ queue fields.
- Fix missing `struct` import used by A2S rules parsing, which previously caused queue fallback to fail and return `unknown`.
- Improve queue handling for DayZ monitor by adding Steam A2S_INFO fallback queries for queue/status data.
- Parse DayZ `lqsN` queue values from A2S payloads and expose queue when launcher API payloads omit it.
- Add fallback queue extraction from raw A2S payload bytes to handle keyword-layout variations.
- Optimize status path to skip A2S fallback when launcher API already contains `online`, `max_players`, and `queue`.
- Harden A2S socket + parse flow with retries and byte-level queue fallback so transient parse/transport issues no longer collapse to unknown queue.
