# Market Data Freshness

Trade freshness and book freshness are independent:

- REST bootstrap book with valid bid/ask is accepted as bootstrap-fresh.
- A fresh trade does not change book freshness.
- A `bookTicker` updates book freshness and replaces the displayed top of book.
- A stale book blocks new non-reducing maker orders with `BOOK_DATA_STALE`.
- Reduce-only orders remain allowed while the book is stale.
- Restart restores persisted market cursors and stream freshness timestamps.

Result: PASS
