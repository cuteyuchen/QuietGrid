# Stock perpetual weekend H1.1

Conclusion: `H1_1_METHOD_OR_SAMPLE_INVALID`

- Technical gate: `FAIL`
- Development blocks: `{'O': 50, 'W': 13}`
- R blocks by seed: `{'10': 0, '17': 0, '3': 0, '31': 0, '59': 0, '97': 0}`
- O before/after R: `742` / `742`
- Forward OOS read: `False`
- B0-B5 run: `False`

## Technical checks

| Check | Result |
| --- | --- |
| `eight_core_data_audits_pass` | **PASS** |
| `input_hashes_match` | **PASS** |
| `wor_overlap_zero` | **PASS** |
| `o_not_deleted_by_r` | **PASS** |
| `o_count_before_equals_after` | **PASS** |
| `at_least_12_w_development_blocks` | **PASS** |
| `at_least_30_o_development_blocks` | **PASS** |
| `at_least_10_r_development_blocks_each_seed` | **FAIL** |
| `three_complete_development_months` | **PASS** |
| `three_traditional_symbols_complete` | **FAIL** |
| `all_duration_metrics_have_per_hour_fields` | **PASS** |
| `june_is_research_validation_exposed` | **PASS** |
| `future_forward_oos_not_computed` | **PASS** |
| `full_pytest_passed` | **PASS** |

Failed checks: `at_least_10_r_development_blocks_each_seed, three_traditional_symbols_complete`

All duration-dependent primary metrics use their per-hour fields. The window-level values remain diagnostic only.

June is labeled `RESEARCH_VALIDATION_EXPOSED`; it is not claimed as sealed OOS.

Economic gates and bootstrap were not evaluated after the technical gate failed.
