# Download Queue Priority Policy

This document defines the intended download ordering for Throttlarr. Treat it as
the canonical behavior when changing queue classification, sorting, or
rebalancing. The implementation may not yet satisfy every rule; update the code
and regression tests to bring it into alignment.

## Eligibility

- Completed and stale downloads are excluded from automatic ranking.
- Manually paused downloads are excluded and must never be auto-resumed.

## Priority Order

Apply these rules from top to bottom. A higher-priority rule wins over every
lower-priority rule.

1. **Manual resume:** A download the user manually resumed receives the highest
   priority.
2. **Prefetcharr targets:** Matching titles for the trigger season and later
   seasons rank ahead of normal downloads.
   - In the trigger season, episodes at or after the trigger episode qualify.
     Earlier episodes fall back to normal TV priority.
   - An unknown trigger season or episode means all seasons or episodes qualify,
     respectively.
   - Matching individual episodes rank before matching season packs.
   - A Prefetcharr-targeted download remains in this tier while in progress.
3. **Early-finish promotion:** A waiting download may move ahead of normal
   in-progress work when completing it first is expected to finish sooner.
   - A waiting movie may move ahead of the top-ranked normal in-progress
     download when the movie's full size is less than that download's remaining
     bytes.
   - A waiting TV episode may move ahead of the top-ranked normal in-progress
     TV episode when the waiting episode's full size is less than that episode's
     remaining bytes.
   - These promotions must not jump ahead of manual-resume or
     Prefetcharr-targeted downloads. Re-evaluate them as the queue changes.
4. **Other in-progress downloads:** Prioritize the smallest remaining amount
   first.
5. **Normal TV episodes:** Prefer newer seasons, then earlier episodes within
   each season. Individual episodes rank before season packs.
6. **Other waiting movies:** Movies not promoted by rule 3 follow normal TV;
   sort smaller movies first.
7. **Stable tie-breakers:** Use queue age, then source, then ID to keep equal
   priority items in a deterministic order.

## Throughput Coordination

Priority ranking decides which downloads should get precedence; it does not
predict or guarantee their completion times. Actual speeds are coordinated
separately across SABnzbd and qBittorrent:

- Apply the configured aggregate speed limit across both clients rather than
   allowing each client to independently consume the full limit.
- Focus available bandwidth on downloads in ranked order. The highest-ranked
   active download gets first claim on capacity; if it cannot use all the
   available or historically observed throughput, let the next-ranked active
   downloads use the remainder, continuing down the queue. Priority is a demand
   order, not an exclusive reservation of bandwidth for one download.
- Coordinate this cascade across SABnzbd and qBittorrent using each client's
   observed throughput and aggregate speed limit. Synchronize the ranked order
   to qBittorrent so its own queue favors the same downloads. SABnzbd remains
   responsible for scheduling within its queue, with its speed limit applied at
   the client level.
- Treat limits and queue positions as coordination signals, not precise
   per-download speed guarantees. Actual throughput varies, so re-evaluate the
   active queue and client limits as downloads progress.

## Implementation and Tests

Prefetcharr matching needs both the trigger season and episode when available.
Tests for queue changes should cover the relevant rule boundaries, including
manual pauses and resumes, Prefetcharr trigger boundaries, season packs,
early-finish promotions, stable ties, client queue-position synchronization,
and shared-cap remainder allocation. Update this document and the tests when
the intended policy changes.