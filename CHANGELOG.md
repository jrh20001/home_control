# Changelog

## 2.0 — 2026-10-27

### Added

- **Lights B configuration page** (`/config_lights`): per-light `On`/`Off` timers read
  from and written to each light's own Tasmota device (`stat/tasmota_<dev>/RESULT`,
  `cmnd/tasmota_<dev>/Timer1|Timer2|Timers`), with `At time` / `Sunrise` / `Sunset`
  modes, ± offset, shared day row, per-light master switch, live IP per light
  (`STATUS5`, cached in `light_ip_cache.json`), unreachable-device warnings and
  offline-aware waiting.
- **Lights A configuration page** (`/config_lights_a`): edits the `config.json`
  `schedule` (add / edit / delete entries, one blank "add" row per light), shows a
  computed "next run" per entry, rewrites `config.json` atomically with a
  timestamped backup (owner/mode preserved), hot-reloads the server while keeping
  runtime toggle state, and regenerates the `at` queue as the service user.
- **Configuration button** on the Lights A and Lights B pages (Sprinklers already had one).
- `ruff.toml` and a full `unittest` suite (`test_config_lights.py`,
  `test_config_lights_a.py`, `test_config_timers.py`, `test_hc_server_refactor.py`,
  `test_update_crontab.py`).

### Fixed

- Sprinkler config page never populated values: Tasmota replies with one flat
  `RESULT` (`Timer1..Timer16`), but the code waited for the legacy `Timers4` chunk.
- Tasmota armed flag is `Enable`, not `Arm`; reads now accept both and writes send
  `Enable` (the device silently ignores `Arm`).
- `update_crontab.py` deleted the **entire** `at` queue; it now removes only its own
  jobs (commands containing `echo rf`) and keeps unrelated jobs.
- Entries whose time already passed today made `at` fail ("refusing to create job
  destined in the past"); they are now rolled to tomorrow and warnings are logged.
- `action()` had no subprocess timeouts and flipped the UI state even when the
  `nc`/`mosquitto_pub` command failed; now 10 s timeout, failure is surfaced and
  state only changes on success.
- Sprinkler/Lights B saves could partially publish or 500 on invalid input; both now
  validate everything first, respond `400` with an on-page message and write nothing.
- Lights A save failure left memory ahead of disk; it now reloads from disk and
  reports the error.
- Concurrency: shared MQTT state is guarded by locks, and the page wait loops no
  longer hold those locks (which deadlocked the MQTT callback thread).
- Config pages report MQTT outages and missing device replies instead of silently
  showing defaults.
- Timer/time ranges are validated (00:00–23:59, mode 0–2, duration 1–60).
- Backup names include microseconds; IP cache writes are atomic; log paths are
  absolute; malformed device data no longer raises `500`s.
- `hc_server.service` runs as `ubuntu` with `CAP_NET_BIND_SERVICE` instead of root.
- `update_crontab_test.py` was a second, destructive copy of the schedule logic; it
  now delegates to `update_crontab.py`.
- `x10_mochad_relay_asyncio.py`: non-blocking connect, proper task scheduling and
  socket cleanup.
- Removed stale `(Copy)` templates/styles from the repository.

## 1.0 — 2026-07-31

- Initial release: Lights A (X10/Mochad), Lights B and sprinklers (MQTT/Tasmota),
  sprinkler timer configuration page, sunrise/sunset schedule compiled into `at` jobs.
