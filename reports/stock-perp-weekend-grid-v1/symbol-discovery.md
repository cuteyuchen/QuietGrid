# Binance 美股相关永续合约发现

- 发现时间（UTC）：`2026-07-24T18:00:16.019000+00:00`
- 官方 exchangeInfo：`https://fapi.binance.com/fapi/v1/exchangeInfo`
- 发现分支：`codex/profit-protection-backtest-v2.3`
- 发现 Git：`1cec0efe54947d5f6a18314b669c2db808f1a21c`
- 数据此前已查看：`True`

候选范围来自官方 exchangeInfo；NYSE/Nasdaq 映射来自 Nasdaq Trader 公共目录。本表的 Tier A-Core 是冻结前的保守预分类，最终等级以数据审计和窗口 manifest 为准。

| Symbol | Underlying | Contract | Status | Onboard | Listing | Months(est.) | Weekends(est.) | Tier | Exclusion |
| --- | --- | --- | --- | --- | --- | ---: | ---: | --- | --- |
| `AAOIUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-06-08` | Applied Optoelectronics, Inc. - Common Stock | 0 | 6 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `AAPLUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-04-06` | Apple Inc. - Common Stock | 2 | 15 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `ADBEUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-06-11` | Adobe Inc. - Common Stock | 0 | 6 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `ALABUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-06-22` | Astera Labs, Inc. - Common Stock | 0 | 4 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `AMATUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-06-08` | Applied Materials, Inc. - Common Stock | 0 | 6 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `AMDUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-05-06` | Advanced Micro Devices, Inc. - Common Stock | 1 | 11 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `AMZNUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-02-09` | Amazon.com, Inc. - Common Stock | 4 | 23 | **TIER_A_CORE** | - |
| `APPUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-07-10` | Applovin Corporation - Class A Common Stock | 0 | 2 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `ARMUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-05-26` | Arm Holdings plc - American Depositary Shares | 1 | 8 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `ASMLUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-06-11` | ASML Holding N.V. - New York Registry Shares | 0 | 6 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `ASTSUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-06-01` | AST SpaceMobile, Inc. - Class A Common Stock | 1 | 7 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `AVGOUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-04-20` | Broadcom Inc. - Common Stock | 2 | 13 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `AXTIUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-06-08` | AXT Inc - Common Stock | 0 | 6 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `BABAUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-04-20` | Alibaba Group Holding Limited American Depositary Shares each representing eight Ordinary share | 2 | 13 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `BBXUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-06-01` | - | 1 | 7 | **EXCLUDED** | no_nasdaq_or_nyse_listing_match |
| `BEUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-05-26` | Bloom Energy Corporation Class A Common Stock | 1 | 8 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `BMNRUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-06-11` | BitMine Immersion Technologies, Inc. Common Stock | 0 | 6 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `BNCUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-07-09` | CEA Industries Inc. - Common Stock | 0 | 2 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `BOTUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-07-09` | RoboStrategy, Inc. - Common Stock | 0 | 2 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `BRKBUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-05-18` | Berkshire Hathaway Inc. New Common Stock | 1 | 9 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `BSPUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-07-02` | Bending Spoons S.p.A. - Ordinary Shares | 0 | 3 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `BXUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-06-08` | Blackstone Inc. Common Stock | 0 | 6 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `CATUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-07-02` | Caterpillar, Inc. Common Stock | 0 | 3 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `CBRSUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-05-19` | Cerebras Systems Inc. - Class A Common Stock | 1 | 9 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `CIENUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-06-22` | Ciena Corporation Common Stock | 0 | 4 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `COHRUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-05-26` | Coherent Corp. Common Stock | 1 | 8 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `COINUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-02-09` | Coinbase Global, Inc. - Class A Common Stock | 4 | 23 | **TIER_A_CORE** | - |
| `COSTUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-06-09` | Costco Wholesale Corporation - Common Stock | 0 | 6 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `CRCLUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-02-09` | Circle Internet Group, Inc. Class A Common Stock | 4 | 23 | **TIER_A_CORE** | - |
| `CRDOUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-06-08` | Credo Technology Group Holding Ltd - Ordinary Shares | 0 | 6 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `CRMUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-06-03` | Salesforce, Inc. Common Stock | 0 | 7 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `CRWDUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-06-08` | CrowdStrike Holdings, Inc. - Class A Common Stock | 0 | 6 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `CRWVUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-05-18` | CoreWeave, Inc. - Class A Common Stock | 1 | 9 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `CSCOUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-05-15` | Cisco Systems, Inc. - Common Stock | 1 | 10 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `DELLUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-06-03` | Dell Technologies Inc. Class C Common Stock  | 0 | 7 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `DISUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-05-15` | Walt Disney Company (The) Common Stock | 1 | 10 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `DKNGUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-06-10` | DraftKings Inc. - Class A Common Stock | 0 | 6 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `DRAMUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-05-18` | Roundhill Memory ETF | 1 | 9 | **EXCLUDED** | listed_security_is_etf_not_company_stock |
| `EBAYUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-06-09` | eBay Inc. - Common Stock | 0 | 6 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `EWJUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-03-19` | iShares MSCI Japan Index Fund | 3 | 18 | **EXCLUDED** | listed_security_is_etf_not_company_stock |
| `EWTUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-06-01` | iShares MSCI Taiwan ETF | 1 | 7 | **EXCLUDED** | listed_security_is_etf_not_company_stock |
| `EWYUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-03-16` | iShares MSCI South Korea ETF | 3 | 18 | **EXCLUDED** | listed_security_is_etf_not_company_stock |
| `EWZUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-06-10` | iShares MSCI Brazil ETF | 0 | 6 | **EXCLUDED** | listed_security_is_etf_not_company_stock |
| `FLEXUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-07-02` | Flex Ltd. - Ordinary Shares | 0 | 3 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `FLNCUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-05-18` | Fluence Energy, Inc. - Class A Common Stock | 1 | 9 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `FWDIUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-07-09` | Forward Industries, Inc. - Common Stock | 0 | 2 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `GEVUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-07-10` | GE Vernova Inc. Common Stock | 0 | 2 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `GLWUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-06-11` | Corning Incorporated Common Stock | 0 | 6 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `GMEUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-06-10` | GameStop Corporation Common Stock | 0 | 6 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `GOOGLUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-03-26` | Alphabet Inc. - Class A Common Stock | 3 | 17 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `HDUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-05-15` | Home Depot, Inc. (The) Common Stock | 1 | 10 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `HIMSUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-06-09` | Hims & Hers Health, Inc. Class A Common Stock | 0 | 6 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `HK0700USDT` | `HK_EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-07-22` | - | 0 | 0 | **EXCLUDED** | not_us_equity_underlying, no_nasdaq_or_nyse_listing_match, no_nonzero_weekend_probe |
| `HK1810USDT` | `HK_EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-07-22` | - | 0 | 0 | **EXCLUDED** | not_us_equity_underlying, no_nasdaq_or_nyse_listing_match, no_nonzero_weekend_probe |
| `HOODUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-02-02` | Robinhood Markets, Inc. - Class A Common Stock | 4 | 24 | **TIER_A_CORE** | - |
| `HPEUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-06-08` | Hewlett Packard Enterprise Company Common Stock | 0 | 6 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `IBMUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-06-03` | International Business Machines Corporation Common Stock | 0 | 7 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `INTCUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-02-02` | Intel Corporation - Common Stock | 4 | 24 | **TIER_A_CORE** | - |
| `INTWUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-07-09` | GraniteShares 2x Long INTC Daily ETF | 0 | 2 | **EXCLUDED** | listed_security_is_etf_not_company_stock |
| `IRENUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-06-03` | IREN Limited - Ordinary Shares | 0 | 7 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `IWMUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-06-08` | iShares Russell 2000 Index Fund | 0 | 6 | **EXCLUDED** | listed_security_is_etf_not_company_stock |
| `JPMUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-05-18` | JP Morgan Chase & Co. Common Stock | 1 | 9 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `KLACUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-06-22` | KLA Corporation  - Common Stock | 0 | 4 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `KORUUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-06-22` | Direxion Daily South Korea Bull 3X ETF | 0 | 4 | **EXCLUDED** | listed_security_is_etf_not_company_stock |
| `KSTRUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-07-02` | KraneShares China Technology & Semiconductor STAR 50 Index ETF | 0 | 3 | **EXCLUDED** | listed_security_is_etf_not_company_stock |
| `LITEUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-05-15` | Lumentum Holdings Inc. - Common Stock | 1 | 10 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `LLYUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-06-01` | Eli Lilly and Company Common Stock | 1 | 7 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `LRCXUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-06-22` | Lam Research Corporation - Common Stock | 0 | 4 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `METAUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-03-26` | Meta Platforms, Inc. - Class A Common Stock | 3 | 17 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `MINIMAXUSDT` | `HK_EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-07-17` | - | 0 | 1 | **EXCLUDED** | not_us_equity_underlying, no_nasdaq_or_nyse_listing_match |
| `MRVLUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-05-15` | Marvell Technology, Inc. - Common Stock | 1 | 10 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `MSFTUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-04-20` | Microsoft Corporation - Common Stock | 2 | 13 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `MSTRUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-02-09` | Strategy Inc - Class A Common Stock | 4 | 23 | **TIER_A_CORE** | - |
| `MUUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-04-07` | Micron Technology, Inc. - Common Stock | 2 | 15 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `MUUUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-07-16` | Direxion Daily MU Bull 2X ETF | 0 | 1 | **EXCLUDED** | listed_security_is_etf_not_company_stock |
| `MVLLUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-06-29` | GraniteShares 2x Long MRVL Daily ETF | 0 | 3 | **EXCLUDED** | listed_security_is_etf_not_company_stock |
| `NBISUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-05-26` | Nebius Group N.V. - Class A Ordinary Shares | 1 | 8 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `NFLXUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-06-09` | Netflix, Inc. - Common Stock | 0 | 6 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `NOKUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-06-01` | Nokia Corporation Sponsored American Depositary Shares | 1 | 7 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `NOWUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-06-03` | ServiceNow, Inc. Common Stock | 0 | 7 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `NVDAUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-03-26` | NVIDIA Corporation - Common Stock | 3 | 17 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `NVOUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-06-01` | Novo Nordisk A/S Common Stock | 1 | 7 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `ONDSUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-06-03` | Ondas Inc - Common Stock | 0 | 7 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `ORCLUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-05-15` | Oracle Corporation Common Stock | 1 | 10 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `PANWUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-07-21` | Palo Alto Networks, Inc. - Common Stock | 0 | 0 | **TIER_A_SHORT** | no_nonzero_weekend_probe |
| `PAYPUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-03-23` | PayPay Corporation - American Depository Shares | 3 | 17 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `PENGUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-07-21` | Penguin Solutions, Inc. - Common Stock | 0 | 0 | **TIER_A_SHORT** | no_nonzero_weekend_probe |
| `PLTRUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-02-09` | Palantir Technologies Inc. - Class A Common Stock | 4 | 23 | **TIER_A_CORE** | - |
| `POPMARTUSDT` | `HK_EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-07-23` | - | 0 | 0 | **EXCLUDED** | not_us_equity_underlying, no_nasdaq_or_nyse_listing_match, no_nonzero_weekend_probe |
| `QCOMUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-05-06` | QUALCOMM Incorporated - Common Stock | 1 | 11 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `QNTXUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-05-29` | - | 1 | 8 | **EXCLUDED** | no_nasdaq_or_nyse_listing_match |
| `QQQUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-04-06` | Invesco QQQ Trust, Series 1 | 2 | 15 | **EXCLUDED** | listed_security_is_etf_not_company_stock |
| `RIVNUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-06-10` | Rivian Automotive, Inc. - Class A Common Stock | 0 | 6 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `RKLBUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-05-18` | Rocket Lab Corporation - Common Stock | 1 | 9 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `SHAZUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-07-21` | SharonAI Holdings, Inc. - Class A Common Stock | 0 | 0 | **TIER_A_SHORT** | no_nonzero_weekend_probe |
| `SKHYUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-07-10` | SK hynix Inc. - American Depositary Shares | 0 | 2 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `SMCIUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-06-22` | Super Micro Computer, Inc. - Common Stock | 0 | 4 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `SNDKUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-04-07` | Sandisk Corporation - Common Stock When-Issued | 2 | 15 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `SNOWUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-07-10` | Snowflake Inc. Common Stock | 0 | 2 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `SNXXUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-07-09` | Tradr 2X Long SNDK Daily ETF | 0 | 2 | **EXCLUDED** | listed_security_is_etf_not_company_stock |
| `SOFIUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-07-21` | SoFi Technologies, Inc.  - Common Stock | 0 | 0 | **TIER_A_SHORT** | no_nonzero_weekend_probe |
| `SONYUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-06-22` | Sony Group Corporation American Depositary Shares  | 0 | 4 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `SOXLUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-05-15` | Direxion Daily Semiconductor Bull 3X ETF | 1 | 10 | **EXCLUDED** | listed_security_is_etf_not_company_stock |
| `SOXSUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-07-16` | Direxion Daily Semiconductor Bear 3X ETF | 0 | 1 | **EXCLUDED** | listed_security_is_etf_not_company_stock |
| `SPCXUSD1` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-07-20` | Space Exploration Technologies Corp. - Class A Common Stock | 0 | 0 | **EXCLUDED** | not_usdt_margined, no_nonzero_weekend_probe |
| `SPCXUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-05-21` | Space Exploration Technologies Corp. - Class A Common Stock | 1 | 9 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `SPYUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-04-06` | State Street SPDR S&P 500 ETF Trust | 2 | 15 | **EXCLUDED** | listed_security_is_etf_not_company_stock |
| `SQQQUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-06-29` | ProShares UltraPro Short QQQ | 0 | 3 | **EXCLUDED** | listed_security_is_etf_not_company_stock |
| `STRCUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-07-02` | Strategy Inc - Variable Rate Series A Perpetual Stretch Preferred Stock | 0 | 3 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `STXXUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-06-11` | Tradr 2X Long STX Daily ETF | 0 | 6 | **EXCLUDED** | listed_security_is_etf_not_company_stock |
| `TENCENTUSDT` | `HK_EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-07-22` | - | 0 | 0 | **EXCLUDED** | not_us_equity_underlying, no_nasdaq_or_nyse_listing_match, no_nonzero_weekend_probe |
| `TERUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-07-02` | Teradyne, Inc. - Common Stock | 0 | 3 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `TQQQUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-06-29` | ProShares UltraPro QQQ | 0 | 3 | **EXCLUDED** | listed_security_is_etf_not_company_stock |
| `TSLAUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-01-28` | Tesla, Inc.  - Common Stock | 5 | 25 | **TIER_A_CORE** | - |
| `TSMUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-04-06` | Taiwan Semiconductor Manufacturing Company Ltd. | 2 | 15 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `TTWOUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-07-02` | Take-Two Interactive Software, Inc. - Common Stock | 0 | 3 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `TXNUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-07-02` | Texas Instruments Incorporated - Common Stock | 0 | 3 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `TZAUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-07-16` | Direxion Small Cap Bear 3X ETF | 0 | 1 | **EXCLUDED** | listed_security_is_etf_not_company_stock |
| `UBERUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-05-15` | Uber Technologies, Inc. Common Stock | 1 | 10 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `URNMUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-06-09` | Sprott Uranium Miners ETF | 0 | 6 | **EXCLUDED** | listed_security_is_etf_not_company_stock |
| `USARUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-05-06` | USA Rare Earth, Inc. - Common Stock | 1 | 11 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `UVXYUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-06-11` | ProShares Ultra VIX Short Term Futures ETF | 0 | 6 | **EXCLUDED** | listed_security_is_etf_not_company_stock |
| `VRTUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-07-10` | Vertiv Holdings, LLC Class A Common Stock | 0 | 2 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `VUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-05-18` | Visa Inc. | 1 | 9 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `WDCUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-05-26` | Western Digital Corporation - Common Stock | 1 | 8 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `WENUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-07-09` | Wendy's Company (The) - Common Stock | 0 | 2 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `WMTUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-05-18` | Walmart Inc. - Common Stock | 1 | 9 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |
| `XBIUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-07-09` | State Street SPDR S&P Biotech ETF | 0 | 2 | **EXCLUDED** | listed_security_is_etf_not_company_stock |
| `XLEUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-06-10` | State Street Energy Select Sector SPDR ETF | 0 | 6 | **EXCLUDED** | listed_security_is_etf_not_company_stock |
| `ZHIPUUSDT` | `HK_EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-07-17` | - | 0 | 1 | **EXCLUDED** | not_us_equity_underlying, no_nasdaq_or_nyse_listing_match |
| `ZMUSDT` | `EQUITY` | `TRADIFI_PERPETUAL` | `TRADING` | `2026-06-10` | Zoom Communications, Inc. - Class A Common Stock | 0 | 6 | **TIER_A_SHORT** | insufficient_complete_months_or_weekends |

总 exchangeInfo 符号：`845`；
EQUITY/HK_EQUITY：`131`；
预分类 Tier A-Core：`8`；
预分类 Tier A-Short：`90`；
排除：`33`。
