# Repository Guidance

- Read [the download queue priority policy](docs/queue-priority.md) before
  changing queue ordering, classification, or rebalancing behavior.
- Treat that document as the intended behavior, not proof that the current
  implementation already conforms.
- Use `queue_priority_order()` as the single ranking path consumed by
  `rebalance_downloads()`; avoid parallel sorting logic in the rebalance loop.
- Keep manual pauses ineligible and never auto-resume them, including in
  transition-grace and stale-download rescue paths. Test those paths when they
  change.
- Queue tests should cover both ranking rules and the rebalance-to-client-order
  path. Patch `threading.Thread` when importing `app` in tests so its poller
  does not start.
- Keep normal logs compact and title-free. Per-download titles belong only in
  bounded `QUEUE_DIAGNOSTICS` output; route errors through the log sanitizer
  and never log credentials or tokens.
- Run `python -m unittest discover -v` for the repository test suite.
- When queue policy changes, update this policy, regression tests, and the
  README summary in the same change.