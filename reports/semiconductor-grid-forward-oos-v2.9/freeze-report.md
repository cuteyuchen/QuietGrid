# Semiconductor Grid Forward OOS v2.9 Freeze Report

## Current conclusion

`INSUFFICIENT_FORWARD_OOS`

## Freeze answers

- 31111 frozen: YES (`c65c75506f4070608ddbfb9a9b3731dc8dab2fee0261c33bda36278a495a1774`)
- v2.8 source commit: `7a24b18c0f027853fcfd939e8d2cb375f2e99c59`
- Candidate freeze commit: `190c2e49c797ba1b8d1b986270d866b16f8cd201`
- v2.9 branch: `codex/semiconductor-grid-forward-oos-v2.9`
- Exposure cutoff: `2026-08-08T20:45:23.438783+00:00`
- Latest research-input timestamp: `2026-07-25T08:34:59.999000+00:00`
- Latest phase window seen: `2026-07-13T08:00:00+00:00`
- Latest Regime/Gate window seen: `2026-07-20T08:00:00+00:00`
- First eligible Forward OOS window: `NONE_YET`
- 31121 status: `DIAGNOSTIC_CONTROL_ONLY`
- EX-MU status: `NEW_POST_HOC_RESEARCH_CANDIDATE`
- Automatic trading remains disabled: `YES`
- Complete Forward OOS windows: `0/8`
- compileall: `PASS (python -m compileall core strategy scripts tests)`
- pytest: `PASS (801 passed, 0 failed)`

`NONE_YET` means the frozen input contains no complete window whose start is
strictly after the exposure cutoff. It must not be replaced by an already
started, partially present, Regime-blocked, or previously researched window.
