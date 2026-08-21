# Read-only breakeven counterfactual replay

Run: `testnet-20260818T191954Z-9cb4120-c67920`
Generated: `2026-08-19T09:55:53.718808+00:00`

## Method and limitations

This analysis does not import into or alter the live trading path. Activation and the subsequent return to cost-adjusted breakeven are evaluated in timestamp order; MFE is never used as an exit. The replay uses retained public LastPrice trades plus position `last_price` samples. Collector gaps can miss an activation or return, so rows without sufficient ordered evidence remain unresolved.

- expected_exit_fee_rate: `0.00055`
- expected_normal_slippage_bps: `2.0`
- funding: `unavailable for every trade; assumed zero and flagged per row`
- trajectory: `ordered persisted public trades plus position lastPrice samples`
- tick_size: `inferred from retained trajectory; not exchange-certified historical metadata`
- anomalous_trade_ids: `[24, 26, 30, 32, 44]`

## Sample: all_26

### Comparison

| Policy | P&L | R | PF | Expectancy | Win % | Avg win | Avg loss | Max DD | BE exits | Losers saved | Winners damaged |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| baseline | -21.6458 | -18.9825 | 0.1679 | -0.8325 | 30.7692 | 0.5461 | -1.4453 | 23.1502 | 0 | 0 | 0 |
| 0.25% | -17.0365 | -15.6483 | 0.0628 | -0.6553 | 61.5385 | 0.0714 | -1.8179 | 17.8675 | 15 | 8 | 7 |
| 0.50% | -22.5892 | -19.7898 | 0.0711 | -0.8688 | 42.3077 | 0.1572 | -1.6212 | 24.0936 | 9 | 3 | 6 |
| 0.75% | -21.1508 | -18.6372 | 0.1302 | -0.8135 | 42.3077 | 0.2879 | -1.6212 | 22.6552 | 6 | 3 | 3 |
| 1.00% | -21.1181 | -18.6207 | 0.1710 | -0.8122 | 38.4615 | 0.4355 | -1.5920 | 22.6225 | 4 | 2 | 2 |
| 0.25R | -17.4974 | -14.1052 | 0.0580 | -0.6730 | 53.8462 | 0.0769 | -1.5478 | 18.3284 | 13 | 6 | 7 |
| 0.50R | -21.9151 | -19.3121 | 0.0988 | -0.8429 | 42.3077 | 0.2185 | -1.6212 | 23.4195 | 7 | 3 | 4 |
| 0.75R | -21.1181 | -18.6207 | 0.1710 | -0.8122 | 38.4615 | 0.4355 | -1.5920 | 22.6225 | 4 | 2 | 2 |
| 1.00R | -21.0981 | -18.6078 | 0.1717 | -0.8115 | 38.4615 | 0.4375 | -1.5920 | 22.6025 | 3 | 2 | 1 |

### Per-trade results

| Policy | Trade | Symbol | Side | Baseline | MFE % | Activated | BE exit | Counterfactual | Delta | Class |
|---|---:|---|---|---:|---:|---|---|---:|---:|---|
| 0.25% | 20 | MNTUSDT | short | 0.8309 | 3.8865 | True | False | 0.8309 | 0.0000 | unchanged |
| 0.25% | 21 | ETHUSDT | short | -0.2208 | 0.0000 | False | False | -0.2208 | 0.0000 | unchanged |
| 0.25% | 22 | MNTUSDT | short | -0.6700 | 0.0000 | False | False | -0.6700 | 0.0000 | unchanged |
| 0.25% | 25 | ETHUSDT | long | -0.3524 | 1.5801 | True | True | 0.0001 | 0.3525 | rescued loser |
| 0.25% | 19 | NEARUSDT | short | 0.0760 | 1.1779 | True | True | 0.0560 | -0.0200 | damaged winner |
| 0.25% | 26 | ETHUSDT | long | -1.7084 | 0.1464 | False | False | -1.7084 | 0.0000 | unchanged |
| 0.25% | 30 | ETHUSDT | short | -1.9673 | 0.0026 | False | False | -1.9673 | 0.0000 | unchanged |
| 0.25% | 24 | BNBUSDT | short | -4.6046 | 0.0995 | False | False | -4.6046 | 0.0000 | unchanged |
| 0.25% | 31 | HBARUSDT | long | 0.0077 | 4.7120 | True | True | 0.0037 | -0.0040 | damaged winner |
| 0.25% | 23 | XRPUSDT | long | -0.1897 | 1.9350 | True | True | 0.0096 | 0.1992 | rescued loser |
| 0.25% | 27 | ADAUSDT | short | -1.6817 | 0.0000 | False | False | -1.6817 | 0.0000 | unchanged |
| 0.25% | 29 | NEARUSDT | short | 1.2737 | 1.5009 | True | True | 0.0576 | -1.2161 | damaged winner |
| 0.25% | 36 | ETHUSDT | long | -1.7544 | -0.0000 | False | False | -1.7544 | 0.0000 | unchanged |
| 0.25% | 35 | SUIUSDT | short | -0.6380 | 0.4761 | True | True | 0.0081 | 0.6461 | rescued loser |
| 0.25% | 38 | NEARUSDT | short | -1.1547 | 0.9204 | True | True | 0.0287 | 1.1834 | rescued loser |
| 0.25% | 42 | NEARUSDT | short | -0.6761 | 0.3783 | True | True | 0.0591 | 0.7353 | rescued loser |
| 0.25% | 34 | HBARUSDT | long | 0.1545 | 1.1197 | True | True | 0.0034 | -0.1510 | damaged winner |
| 0.25% | 28 | HYPEUSDT | short | 0.5298 | 0.5636 | True | True | 0.0067 | -0.5231 | damaged winner |
| 0.25% | 39 | XRPUSDT | short | -0.6256 | 0.1604 | False | False | -0.6256 | 0.0000 | unchanged |
| 0.25% | 33 | AAVEUSDT | short | -0.8730 | 0.3673 | True | True | 0.0062 | 0.8792 | rescued loser |
| 0.25% | 37 | TAOUSDT | short | -0.5544 | 0.3616 | True | True | 0.0009 | 0.5552 | rescued loser |
| 0.25% | 43 | NEARUSDT | long | 0.8229 | 0.6923 | True | True | 0.0587 | -0.7642 | damaged winner |
| 0.25% | 32 | DOGEUSDT | short | -3.3978 | 0.4988 | True | True | 0.0126 | 3.4104 | rescued loser |
| 0.25% | 41 | APTUSDT | long | -0.1112 | 0.4527 | True | False | -0.1112 | 0.0000 | unchanged |
| 0.25% | 44 | BNBUSDT | short | -4.8348 | 0.0000 | False | False | -4.8348 | 0.0000 | unchanged |
| 0.25% | 40 | BTCUSDT | long | 0.6735 | 0.7139 | True | True | 0.0001 | -0.6734 | damaged winner |
| 0.50% | 20 | MNTUSDT | short | 0.8309 | 3.8865 | True | False | 0.8309 | 0.0000 | unchanged |
| 0.50% | 21 | ETHUSDT | short | -0.2208 | 0.0000 | False | False | -0.2208 | 0.0000 | unchanged |
| 0.50% | 22 | MNTUSDT | short | -0.6700 | 0.0000 | False | False | -0.6700 | 0.0000 | unchanged |
| 0.50% | 25 | ETHUSDT | long | -0.3524 | 1.5801 | True | True | 0.0001 | 0.3525 | rescued loser |
| 0.50% | 19 | NEARUSDT | short | 0.0760 | 1.1779 | True | True | 0.0560 | -0.0200 | damaged winner |
| 0.50% | 26 | ETHUSDT | long | -1.7084 | 0.1464 | False | False | -1.7084 | 0.0000 | unchanged |
| 0.50% | 30 | ETHUSDT | short | -1.9673 | 0.0026 | False | False | -1.9673 | 0.0000 | unchanged |
| 0.50% | 24 | BNBUSDT | short | -4.6046 | 0.0995 | False | False | -4.6046 | 0.0000 | unchanged |
| 0.50% | 31 | HBARUSDT | long | 0.0077 | 4.7120 | True | True | 0.0037 | -0.0040 | damaged winner |
| 0.50% | 23 | XRPUSDT | long | -0.1897 | 1.9350 | True | True | 0.0096 | 0.1992 | rescued loser |
| 0.50% | 27 | ADAUSDT | short | -1.6817 | 0.0000 | False | False | -1.6817 | 0.0000 | unchanged |
| 0.50% | 29 | NEARUSDT | short | 1.2737 | 1.5009 | True | True | 0.0576 | -1.2161 | damaged winner |
| 0.50% | 36 | ETHUSDT | long | -1.7544 | -0.0000 | False | False | -1.7544 | 0.0000 | unchanged |
| 0.50% | 35 | SUIUSDT | short | -0.6380 | 0.4761 | False | False | -0.6380 | 0.0000 | unchanged |
| 0.50% | 38 | NEARUSDT | short | -1.1547 | 0.9204 | True | True | 0.0287 | 1.1834 | rescued loser |
| 0.50% | 42 | NEARUSDT | short | -0.6761 | 0.3783 | False | False | -0.6761 | 0.0000 | unchanged |
| 0.50% | 34 | HBARUSDT | long | 0.1545 | 1.1197 | True | True | 0.0034 | -0.1510 | damaged winner |
| 0.50% | 28 | HYPEUSDT | short | 0.5298 | 0.5636 | True | True | 0.0067 | -0.5231 | damaged winner |
| 0.50% | 39 | XRPUSDT | short | -0.6256 | 0.1604 | False | False | -0.6256 | 0.0000 | unchanged |
| 0.50% | 33 | AAVEUSDT | short | -0.8730 | 0.3673 | False | False | -0.8730 | 0.0000 | unchanged |
| 0.50% | 37 | TAOUSDT | short | -0.5544 | 0.3616 | False | False | -0.5544 | 0.0000 | unchanged |
| 0.50% | 43 | NEARUSDT | long | 0.8229 | 0.6923 | True | True | 0.0587 | -0.7642 | damaged winner |
| 0.50% | 32 | DOGEUSDT | short | -3.3978 | 0.4988 | False | False | -3.3978 | 0.0000 | unchanged |
| 0.50% | 41 | APTUSDT | long | -0.1112 | 0.4527 | False | False | -0.1112 | 0.0000 | unchanged |
| 0.50% | 44 | BNBUSDT | short | -4.8348 | 0.0000 | False | False | -4.8348 | 0.0000 | unchanged |
| 0.50% | 40 | BTCUSDT | long | 0.6735 | 0.7139 | True | False | 0.6735 | 0.0000 | unchanged |
| 0.75% | 20 | MNTUSDT | short | 0.8309 | 3.8865 | True | False | 0.8309 | 0.0000 | unchanged |
| 0.75% | 21 | ETHUSDT | short | -0.2208 | 0.0000 | False | False | -0.2208 | 0.0000 | unchanged |
| 0.75% | 22 | MNTUSDT | short | -0.6700 | 0.0000 | False | False | -0.6700 | 0.0000 | unchanged |
| 0.75% | 25 | ETHUSDT | long | -0.3524 | 1.5801 | True | True | 0.0001 | 0.3525 | rescued loser |
| 0.75% | 19 | NEARUSDT | short | 0.0760 | 1.1779 | True | True | 0.0560 | -0.0200 | damaged winner |
| 0.75% | 26 | ETHUSDT | long | -1.7084 | 0.1464 | False | False | -1.7084 | 0.0000 | unchanged |
| 0.75% | 30 | ETHUSDT | short | -1.9673 | 0.0026 | False | False | -1.9673 | 0.0000 | unchanged |
| 0.75% | 24 | BNBUSDT | short | -4.6046 | 0.0995 | False | False | -4.6046 | 0.0000 | unchanged |
| 0.75% | 31 | HBARUSDT | long | 0.0077 | 4.7120 | True | True | 0.0037 | -0.0040 | damaged winner |
| 0.75% | 23 | XRPUSDT | long | -0.1897 | 1.9350 | True | True | 0.0096 | 0.1992 | rescued loser |
| 0.75% | 27 | ADAUSDT | short | -1.6817 | 0.0000 | False | False | -1.6817 | 0.0000 | unchanged |
| 0.75% | 29 | NEARUSDT | short | 1.2737 | 1.5009 | True | True | 0.0576 | -1.2161 | damaged winner |
| 0.75% | 36 | ETHUSDT | long | -1.7544 | -0.0000 | False | False | -1.7544 | 0.0000 | unchanged |
| 0.75% | 35 | SUIUSDT | short | -0.6380 | 0.4761 | False | False | -0.6380 | 0.0000 | unchanged |
| 0.75% | 38 | NEARUSDT | short | -1.1547 | 0.9204 | True | True | 0.0287 | 1.1834 | rescued loser |
| 0.75% | 42 | NEARUSDT | short | -0.6761 | 0.3783 | False | False | -0.6761 | 0.0000 | unchanged |
| 0.75% | 34 | HBARUSDT | long | 0.1545 | 1.1197 | True | False | 0.1545 | 0.0000 | unchanged |
| 0.75% | 28 | HYPEUSDT | short | 0.5298 | 0.5636 | False | False | 0.5298 | 0.0000 | unchanged |
| 0.75% | 39 | XRPUSDT | short | -0.6256 | 0.1604 | False | False | -0.6256 | 0.0000 | unchanged |
| 0.75% | 33 | AAVEUSDT | short | -0.8730 | 0.3673 | False | False | -0.8730 | 0.0000 | unchanged |
| 0.75% | 37 | TAOUSDT | short | -0.5544 | 0.3616 | False | False | -0.5544 | 0.0000 | unchanged |
| 0.75% | 43 | NEARUSDT | long | 0.8229 | 0.6923 | False | False | 0.8229 | 0.0000 | unchanged |
| 0.75% | 32 | DOGEUSDT | short | -3.3978 | 0.4988 | False | False | -3.3978 | 0.0000 | unchanged |
| 0.75% | 41 | APTUSDT | long | -0.1112 | 0.4527 | False | False | -0.1112 | 0.0000 | unchanged |
| 0.75% | 44 | BNBUSDT | short | -4.8348 | 0.0000 | False | False | -4.8348 | 0.0000 | unchanged |
| 0.75% | 40 | BTCUSDT | long | 0.6735 | 0.7139 | False | False | 0.6735 | 0.0000 | unchanged |
| 1.00% | 20 | MNTUSDT | short | 0.8309 | 3.8865 | True | False | 0.8309 | 0.0000 | unchanged |
| 1.00% | 21 | ETHUSDT | short | -0.2208 | 0.0000 | False | False | -0.2208 | 0.0000 | unchanged |
| 1.00% | 22 | MNTUSDT | short | -0.6700 | 0.0000 | False | False | -0.6700 | 0.0000 | unchanged |
| 1.00% | 25 | ETHUSDT | long | -0.3524 | 1.5801 | True | True | 0.0001 | 0.3525 | rescued loser |
| 1.00% | 19 | NEARUSDT | short | 0.0760 | 1.1779 | True | True | 0.0560 | -0.0200 | damaged winner |
| 1.00% | 26 | ETHUSDT | long | -1.7084 | 0.1464 | False | False | -1.7084 | 0.0000 | unchanged |
| 1.00% | 30 | ETHUSDT | short | -1.9673 | 0.0026 | False | False | -1.9673 | 0.0000 | unchanged |
| 1.00% | 24 | BNBUSDT | short | -4.6046 | 0.0995 | False | False | -4.6046 | 0.0000 | unchanged |
| 1.00% | 31 | HBARUSDT | long | 0.0077 | 4.7120 | True | True | 0.0037 | -0.0040 | damaged winner |
| 1.00% | 23 | XRPUSDT | long | -0.1897 | 1.9350 | True | True | 0.0096 | 0.1992 | rescued loser |
| 1.00% | 27 | ADAUSDT | short | -1.6817 | 0.0000 | False | False | -1.6817 | 0.0000 | unchanged |
| 1.00% | 29 | NEARUSDT | short | 1.2737 | 1.5009 | True | False | 1.2737 | 0.0000 | unchanged |
| 1.00% | 36 | ETHUSDT | long | -1.7544 | -0.0000 | False | False | -1.7544 | 0.0000 | unchanged |
| 1.00% | 35 | SUIUSDT | short | -0.6380 | 0.4761 | False | False | -0.6380 | 0.0000 | unchanged |
| 1.00% | 38 | NEARUSDT | short | -1.1547 | 0.9204 | False | False | -1.1547 | 0.0000 | unchanged |
| 1.00% | 42 | NEARUSDT | short | -0.6761 | 0.3783 | False | False | -0.6761 | 0.0000 | unchanged |
| 1.00% | 34 | HBARUSDT | long | 0.1545 | 1.1197 | True | False | 0.1545 | 0.0000 | unchanged |
| 1.00% | 28 | HYPEUSDT | short | 0.5298 | 0.5636 | False | False | 0.5298 | 0.0000 | unchanged |
| 1.00% | 39 | XRPUSDT | short | -0.6256 | 0.1604 | False | False | -0.6256 | 0.0000 | unchanged |
| 1.00% | 33 | AAVEUSDT | short | -0.8730 | 0.3673 | False | False | -0.8730 | 0.0000 | unchanged |
| 1.00% | 37 | TAOUSDT | short | -0.5544 | 0.3616 | False | False | -0.5544 | 0.0000 | unchanged |
| 1.00% | 43 | NEARUSDT | long | 0.8229 | 0.6923 | False | False | 0.8229 | 0.0000 | unchanged |
| 1.00% | 32 | DOGEUSDT | short | -3.3978 | 0.4988 | False | False | -3.3978 | 0.0000 | unchanged |
| 1.00% | 41 | APTUSDT | long | -0.1112 | 0.4527 | False | False | -0.1112 | 0.0000 | unchanged |
| 1.00% | 44 | BNBUSDT | short | -4.8348 | 0.0000 | False | False | -4.8348 | 0.0000 | unchanged |
| 1.00% | 40 | BTCUSDT | long | 0.6735 | 0.7139 | False | False | 0.6735 | 0.0000 | unchanged |
| 0.25R | 20 | MNTUSDT | short | 0.8309 | 3.8865 | True | False | 0.8309 | 0.0000 | unchanged |
| 0.25R | 21 | ETHUSDT | short | -0.2208 | 0.0000 | False | False | -0.2208 | 0.0000 | unchanged |
| 0.25R | 22 | MNTUSDT | short | -0.6700 | 0.0000 | False | False | -0.6700 | 0.0000 | unchanged |
| 0.25R | 25 | ETHUSDT | long | -0.3524 | 1.5801 | True | True | 0.0001 | 0.3525 | rescued loser |
| 0.25R | 19 | NEARUSDT | short | 0.0760 | 1.1779 | True | True | 0.0560 | -0.0200 | damaged winner |
| 0.25R | 26 | ETHUSDT | long | -1.7084 | 0.1464 | True | True | 0.0004 | 1.7088 | rescued loser |
| 0.25R | 30 | ETHUSDT | short | -1.9673 | 0.0026 | False | False | -1.9673 | 0.0000 | unchanged |
| 0.25R | 24 | BNBUSDT | short | -4.6046 | 0.0995 | False | False | -4.6046 | 0.0000 | unchanged |
| 0.25R | 31 | HBARUSDT | long | 0.0077 | 4.7120 | True | True | 0.0037 | -0.0040 | damaged winner |
| 0.25R | 23 | XRPUSDT | long | -0.1897 | 1.9350 | True | True | 0.0096 | 0.1992 | rescued loser |
| 0.25R | 27 | ADAUSDT | short | -1.6817 | 0.0000 | False | False | -1.6817 | 0.0000 | unchanged |
| 0.25R | 29 | NEARUSDT | short | 1.2737 | 1.5009 | True | True | 0.0576 | -1.2161 | damaged winner |
| 0.25R | 36 | ETHUSDT | long | -1.7544 | -0.0000 | False | False | -1.7544 | 0.0000 | unchanged |
| 0.25R | 35 | SUIUSDT | short | -0.6380 | 0.4761 | True | True | 0.0081 | 0.6461 | rescued loser |
| 0.25R | 38 | NEARUSDT | short | -1.1547 | 0.9204 | True | True | 0.0287 | 1.1834 | rescued loser |
| 0.25R | 42 | NEARUSDT | short | -0.6761 | 0.3783 | False | False | -0.6761 | 0.0000 | unchanged |
| 0.25R | 34 | HBARUSDT | long | 0.1545 | 1.1197 | True | True | 0.0034 | -0.1510 | damaged winner |
| 0.25R | 28 | HYPEUSDT | short | 0.5298 | 0.5636 | True | True | 0.0067 | -0.5231 | damaged winner |
| 0.25R | 39 | XRPUSDT | short | -0.6256 | 0.1604 | False | False | -0.6256 | 0.0000 | unchanged |
| 0.25R | 33 | AAVEUSDT | short | -0.8730 | 0.3673 | False | False | -0.8730 | 0.0000 | unchanged |
| 0.25R | 37 | TAOUSDT | short | -0.5544 | 0.3616 | False | False | -0.5544 | 0.0000 | unchanged |
| 0.25R | 43 | NEARUSDT | long | 0.8229 | 0.6923 | True | True | 0.0587 | -0.7642 | damaged winner |
| 0.25R | 32 | DOGEUSDT | short | -3.3978 | 0.4988 | True | True | 0.0126 | 3.4104 | rescued loser |
| 0.25R | 41 | APTUSDT | long | -0.1112 | 0.4527 | True | False | -0.1112 | 0.0000 | unchanged |
| 0.25R | 44 | BNBUSDT | short | -4.8348 | 0.0000 | False | False | -4.8348 | 0.0000 | unchanged |
| 0.25R | 40 | BTCUSDT | long | 0.6735 | 0.7139 | True | True | 0.0001 | -0.6734 | damaged winner |
| 0.50R | 20 | MNTUSDT | short | 0.8309 | 3.8865 | True | False | 0.8309 | 0.0000 | unchanged |
| 0.50R | 21 | ETHUSDT | short | -0.2208 | 0.0000 | False | False | -0.2208 | 0.0000 | unchanged |
| 0.50R | 22 | MNTUSDT | short | -0.6700 | 0.0000 | False | False | -0.6700 | 0.0000 | unchanged |
| 0.50R | 25 | ETHUSDT | long | -0.3524 | 1.5801 | True | True | 0.0001 | 0.3525 | rescued loser |
| 0.50R | 19 | NEARUSDT | short | 0.0760 | 1.1779 | True | True | 0.0560 | -0.0200 | damaged winner |
| 0.50R | 26 | ETHUSDT | long | -1.7084 | 0.1464 | False | False | -1.7084 | 0.0000 | unchanged |
| 0.50R | 30 | ETHUSDT | short | -1.9673 | 0.0026 | False | False | -1.9673 | 0.0000 | unchanged |
| 0.50R | 24 | BNBUSDT | short | -4.6046 | 0.0995 | False | False | -4.6046 | 0.0000 | unchanged |
| 0.50R | 31 | HBARUSDT | long | 0.0077 | 4.7120 | True | True | 0.0037 | -0.0040 | damaged winner |
| 0.50R | 23 | XRPUSDT | long | -0.1897 | 1.9350 | True | True | 0.0096 | 0.1992 | rescued loser |
| 0.50R | 27 | ADAUSDT | short | -1.6817 | 0.0000 | False | False | -1.6817 | 0.0000 | unchanged |
| 0.50R | 29 | NEARUSDT | short | 1.2737 | 1.5009 | True | True | 0.0576 | -1.2161 | damaged winner |
| 0.50R | 36 | ETHUSDT | long | -1.7544 | -0.0000 | False | False | -1.7544 | 0.0000 | unchanged |
| 0.50R | 35 | SUIUSDT | short | -0.6380 | 0.4761 | False | False | -0.6380 | 0.0000 | unchanged |
| 0.50R | 38 | NEARUSDT | short | -1.1547 | 0.9204 | True | True | 0.0287 | 1.1834 | rescued loser |
| 0.50R | 42 | NEARUSDT | short | -0.6761 | 0.3783 | False | False | -0.6761 | 0.0000 | unchanged |
| 0.50R | 34 | HBARUSDT | long | 0.1545 | 1.1197 | True | False | 0.1545 | 0.0000 | unchanged |
| 0.50R | 28 | HYPEUSDT | short | 0.5298 | 0.5636 | False | False | 0.5298 | 0.0000 | unchanged |
| 0.50R | 39 | XRPUSDT | short | -0.6256 | 0.1604 | False | False | -0.6256 | 0.0000 | unchanged |
| 0.50R | 33 | AAVEUSDT | short | -0.8730 | 0.3673 | False | False | -0.8730 | 0.0000 | unchanged |
| 0.50R | 37 | TAOUSDT | short | -0.5544 | 0.3616 | False | False | -0.5544 | 0.0000 | unchanged |
| 0.50R | 43 | NEARUSDT | long | 0.8229 | 0.6923 | True | True | 0.0587 | -0.7642 | damaged winner |
| 0.50R | 32 | DOGEUSDT | short | -3.3978 | 0.4988 | False | False | -3.3978 | 0.0000 | unchanged |
| 0.50R | 41 | APTUSDT | long | -0.1112 | 0.4527 | False | False | -0.1112 | 0.0000 | unchanged |
| 0.50R | 44 | BNBUSDT | short | -4.8348 | 0.0000 | False | False | -4.8348 | 0.0000 | unchanged |
| 0.50R | 40 | BTCUSDT | long | 0.6735 | 0.7139 | True | False | 0.6735 | 0.0000 | unchanged |
| 0.75R | 20 | MNTUSDT | short | 0.8309 | 3.8865 | True | False | 0.8309 | 0.0000 | unchanged |
| 0.75R | 21 | ETHUSDT | short | -0.2208 | 0.0000 | False | False | -0.2208 | 0.0000 | unchanged |
| 0.75R | 22 | MNTUSDT | short | -0.6700 | 0.0000 | False | False | -0.6700 | 0.0000 | unchanged |
| 0.75R | 25 | ETHUSDT | long | -0.3524 | 1.5801 | True | True | 0.0001 | 0.3525 | rescued loser |
| 0.75R | 19 | NEARUSDT | short | 0.0760 | 1.1779 | True | True | 0.0560 | -0.0200 | damaged winner |
| 0.75R | 26 | ETHUSDT | long | -1.7084 | 0.1464 | False | False | -1.7084 | 0.0000 | unchanged |
| 0.75R | 30 | ETHUSDT | short | -1.9673 | 0.0026 | False | False | -1.9673 | 0.0000 | unchanged |
| 0.75R | 24 | BNBUSDT | short | -4.6046 | 0.0995 | False | False | -4.6046 | 0.0000 | unchanged |
| 0.75R | 31 | HBARUSDT | long | 0.0077 | 4.7120 | True | True | 0.0037 | -0.0040 | damaged winner |
| 0.75R | 23 | XRPUSDT | long | -0.1897 | 1.9350 | True | True | 0.0096 | 0.1992 | rescued loser |
| 0.75R | 27 | ADAUSDT | short | -1.6817 | 0.0000 | False | False | -1.6817 | 0.0000 | unchanged |
| 0.75R | 29 | NEARUSDT | short | 1.2737 | 1.5009 | True | False | 1.2737 | 0.0000 | unchanged |
| 0.75R | 36 | ETHUSDT | long | -1.7544 | -0.0000 | False | False | -1.7544 | 0.0000 | unchanged |
| 0.75R | 35 | SUIUSDT | short | -0.6380 | 0.4761 | False | False | -0.6380 | 0.0000 | unchanged |
| 0.75R | 38 | NEARUSDT | short | -1.1547 | 0.9204 | False | False | -1.1547 | 0.0000 | unchanged |
| 0.75R | 42 | NEARUSDT | short | -0.6761 | 0.3783 | False | False | -0.6761 | 0.0000 | unchanged |
| 0.75R | 34 | HBARUSDT | long | 0.1545 | 1.1197 | True | False | 0.1545 | 0.0000 | unchanged |
| 0.75R | 28 | HYPEUSDT | short | 0.5298 | 0.5636 | False | False | 0.5298 | 0.0000 | unchanged |
| 0.75R | 39 | XRPUSDT | short | -0.6256 | 0.1604 | False | False | -0.6256 | 0.0000 | unchanged |
| 0.75R | 33 | AAVEUSDT | short | -0.8730 | 0.3673 | False | False | -0.8730 | 0.0000 | unchanged |
| 0.75R | 37 | TAOUSDT | short | -0.5544 | 0.3616 | False | False | -0.5544 | 0.0000 | unchanged |
| 0.75R | 43 | NEARUSDT | long | 0.8229 | 0.6923 | False | False | 0.8229 | 0.0000 | unchanged |
| 0.75R | 32 | DOGEUSDT | short | -3.3978 | 0.4988 | False | False | -3.3978 | 0.0000 | unchanged |
| 0.75R | 41 | APTUSDT | long | -0.1112 | 0.4527 | False | False | -0.1112 | 0.0000 | unchanged |
| 0.75R | 44 | BNBUSDT | short | -4.8348 | 0.0000 | False | False | -4.8348 | 0.0000 | unchanged |
| 0.75R | 40 | BTCUSDT | long | 0.6735 | 0.7139 | False | False | 0.6735 | 0.0000 | unchanged |
| 1.00R | 20 | MNTUSDT | short | 0.8309 | 3.8865 | True | False | 0.8309 | 0.0000 | unchanged |
| 1.00R | 21 | ETHUSDT | short | -0.2208 | 0.0000 | False | False | -0.2208 | 0.0000 | unchanged |
| 1.00R | 22 | MNTUSDT | short | -0.6700 | 0.0000 | False | False | -0.6700 | 0.0000 | unchanged |
| 1.00R | 25 | ETHUSDT | long | -0.3524 | 1.5801 | True | True | 0.0001 | 0.3525 | rescued loser |
| 1.00R | 19 | NEARUSDT | short | 0.0760 | 1.1779 | False | False | 0.0760 | 0.0000 | unchanged |
| 1.00R | 26 | ETHUSDT | long | -1.7084 | 0.1464 | False | False | -1.7084 | 0.0000 | unchanged |
| 1.00R | 30 | ETHUSDT | short | -1.9673 | 0.0026 | False | False | -1.9673 | 0.0000 | unchanged |
| 1.00R | 24 | BNBUSDT | short | -4.6046 | 0.0995 | False | False | -4.6046 | 0.0000 | unchanged |
| 1.00R | 31 | HBARUSDT | long | 0.0077 | 4.7120 | True | True | 0.0037 | -0.0040 | damaged winner |
| 1.00R | 23 | XRPUSDT | long | -0.1897 | 1.9350 | True | True | 0.0096 | 0.1992 | rescued loser |
| 1.00R | 27 | ADAUSDT | short | -1.6817 | 0.0000 | False | False | -1.6817 | 0.0000 | unchanged |
| 1.00R | 29 | NEARUSDT | short | 1.2737 | 1.5009 | False | False | 1.2737 | 0.0000 | unchanged |
| 1.00R | 36 | ETHUSDT | long | -1.7544 | -0.0000 | False | False | -1.7544 | 0.0000 | unchanged |
| 1.00R | 35 | SUIUSDT | short | -0.6380 | 0.4761 | False | False | -0.6380 | 0.0000 | unchanged |
| 1.00R | 38 | NEARUSDT | short | -1.1547 | 0.9204 | False | False | -1.1547 | 0.0000 | unchanged |
| 1.00R | 42 | NEARUSDT | short | -0.6761 | 0.3783 | False | False | -0.6761 | 0.0000 | unchanged |
| 1.00R | 34 | HBARUSDT | long | 0.1545 | 1.1197 | False | False | 0.1545 | 0.0000 | unchanged |
| 1.00R | 28 | HYPEUSDT | short | 0.5298 | 0.5636 | False | False | 0.5298 | 0.0000 | unchanged |
| 1.00R | 39 | XRPUSDT | short | -0.6256 | 0.1604 | False | False | -0.6256 | 0.0000 | unchanged |
| 1.00R | 33 | AAVEUSDT | short | -0.8730 | 0.3673 | False | False | -0.8730 | 0.0000 | unchanged |
| 1.00R | 37 | TAOUSDT | short | -0.5544 | 0.3616 | False | False | -0.5544 | 0.0000 | unchanged |
| 1.00R | 43 | NEARUSDT | long | 0.8229 | 0.6923 | False | False | 0.8229 | 0.0000 | unchanged |
| 1.00R | 32 | DOGEUSDT | short | -3.3978 | 0.4988 | False | False | -3.3978 | 0.0000 | unchanged |
| 1.00R | 41 | APTUSDT | long | -0.1112 | 0.4527 | False | False | -0.1112 | 0.0000 | unchanged |
| 1.00R | 44 | BNBUSDT | short | -4.8348 | 0.0000 | False | False | -4.8348 | 0.0000 | unchanged |
| 1.00R | 40 | BTCUSDT | long | 0.6735 | 0.7139 | False | False | 0.6735 | 0.0000 | unchanged |

## Sample: excluding_5_anomalous_fills

### Comparison

| Policy | P&L | R | PF | Expectancy | Win % | Avg win | Avg loss | Max DD | BE exits | Losers saved | Winners damaged |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| baseline | -5.1329 | -3.4668 | 0.4598 | -0.2444 | 38.0952 | 0.5461 | -0.7309 | 7.3490 | 0 | 0 | 0 |
| 0.25% | -3.9340 | -3.0513 | 0.2231 | -0.1873 | 71.4286 | 0.0753 | -0.8439 | 4.7650 | 14 | 7 | 7 |
| 0.50% | -6.0763 | -4.2741 | 0.2215 | -0.2893 | 52.3810 | 0.1572 | -0.7805 | 7.5807 | 9 | 3 | 6 |
| 0.75% | -4.6379 | -3.1215 | 0.4058 | -0.2209 | 52.3810 | 0.2879 | -0.7805 | 6.8540 | 6 | 3 | 3 |
| 1.00% | -4.6052 | -3.1051 | 0.4860 | -0.2193 | 47.6190 | 0.4355 | -0.8145 | 6.8213 | 4 | 2 | 2 |
| 0.25R | -6.1036 | -4.7202 | 0.1484 | -0.2906 | 57.1429 | 0.0886 | -0.7963 | 6.9346 | 11 | 4 | 7 |
| 0.50R | -5.4021 | -3.7965 | 0.3079 | -0.2572 | 52.3810 | 0.2185 | -0.7805 | 6.9065 | 7 | 3 | 4 |
| 0.75R | -4.6052 | -3.1051 | 0.4860 | -0.2193 | 47.6190 | 0.4355 | -0.8145 | 6.8213 | 4 | 2 | 2 |
| 1.00R | -4.5852 | -3.0922 | 0.4882 | -0.2183 | 47.6190 | 0.4375 | -0.8145 | 6.8013 | 3 | 2 | 1 |

### Per-trade results

| Policy | Trade | Symbol | Side | Baseline | MFE % | Activated | BE exit | Counterfactual | Delta | Class |
|---|---:|---|---|---:|---:|---|---|---:|---:|---|
| 0.25% | 20 | MNTUSDT | short | 0.8309 | 3.8865 | True | False | 0.8309 | 0.0000 | unchanged |
| 0.25% | 21 | ETHUSDT | short | -0.2208 | 0.0000 | False | False | -0.2208 | 0.0000 | unchanged |
| 0.25% | 22 | MNTUSDT | short | -0.6700 | 0.0000 | False | False | -0.6700 | 0.0000 | unchanged |
| 0.25% | 25 | ETHUSDT | long | -0.3524 | 1.5801 | True | True | 0.0001 | 0.3525 | rescued loser |
| 0.25% | 19 | NEARUSDT | short | 0.0760 | 1.1779 | True | True | 0.0560 | -0.0200 | damaged winner |
| 0.25% | 31 | HBARUSDT | long | 0.0077 | 4.7120 | True | True | 0.0037 | -0.0040 | damaged winner |
| 0.25% | 23 | XRPUSDT | long | -0.1897 | 1.9350 | True | True | 0.0096 | 0.1992 | rescued loser |
| 0.25% | 27 | ADAUSDT | short | -1.6817 | 0.0000 | False | False | -1.6817 | 0.0000 | unchanged |
| 0.25% | 29 | NEARUSDT | short | 1.2737 | 1.5009 | True | True | 0.0576 | -1.2161 | damaged winner |
| 0.25% | 36 | ETHUSDT | long | -1.7544 | -0.0000 | False | False | -1.7544 | 0.0000 | unchanged |
| 0.25% | 35 | SUIUSDT | short | -0.6380 | 0.4761 | True | True | 0.0081 | 0.6461 | rescued loser |
| 0.25% | 38 | NEARUSDT | short | -1.1547 | 0.9204 | True | True | 0.0287 | 1.1834 | rescued loser |
| 0.25% | 42 | NEARUSDT | short | -0.6761 | 0.3783 | True | True | 0.0591 | 0.7353 | rescued loser |
| 0.25% | 34 | HBARUSDT | long | 0.1545 | 1.1197 | True | True | 0.0034 | -0.1510 | damaged winner |
| 0.25% | 28 | HYPEUSDT | short | 0.5298 | 0.5636 | True | True | 0.0067 | -0.5231 | damaged winner |
| 0.25% | 39 | XRPUSDT | short | -0.6256 | 0.1604 | False | False | -0.6256 | 0.0000 | unchanged |
| 0.25% | 33 | AAVEUSDT | short | -0.8730 | 0.3673 | True | True | 0.0062 | 0.8792 | rescued loser |
| 0.25% | 37 | TAOUSDT | short | -0.5544 | 0.3616 | True | True | 0.0009 | 0.5552 | rescued loser |
| 0.25% | 43 | NEARUSDT | long | 0.8229 | 0.6923 | True | True | 0.0587 | -0.7642 | damaged winner |
| 0.25% | 41 | APTUSDT | long | -0.1112 | 0.4527 | True | False | -0.1112 | 0.0000 | unchanged |
| 0.25% | 40 | BTCUSDT | long | 0.6735 | 0.7139 | True | True | 0.0001 | -0.6734 | damaged winner |
| 0.50% | 20 | MNTUSDT | short | 0.8309 | 3.8865 | True | False | 0.8309 | 0.0000 | unchanged |
| 0.50% | 21 | ETHUSDT | short | -0.2208 | 0.0000 | False | False | -0.2208 | 0.0000 | unchanged |
| 0.50% | 22 | MNTUSDT | short | -0.6700 | 0.0000 | False | False | -0.6700 | 0.0000 | unchanged |
| 0.50% | 25 | ETHUSDT | long | -0.3524 | 1.5801 | True | True | 0.0001 | 0.3525 | rescued loser |
| 0.50% | 19 | NEARUSDT | short | 0.0760 | 1.1779 | True | True | 0.0560 | -0.0200 | damaged winner |
| 0.50% | 31 | HBARUSDT | long | 0.0077 | 4.7120 | True | True | 0.0037 | -0.0040 | damaged winner |
| 0.50% | 23 | XRPUSDT | long | -0.1897 | 1.9350 | True | True | 0.0096 | 0.1992 | rescued loser |
| 0.50% | 27 | ADAUSDT | short | -1.6817 | 0.0000 | False | False | -1.6817 | 0.0000 | unchanged |
| 0.50% | 29 | NEARUSDT | short | 1.2737 | 1.5009 | True | True | 0.0576 | -1.2161 | damaged winner |
| 0.50% | 36 | ETHUSDT | long | -1.7544 | -0.0000 | False | False | -1.7544 | 0.0000 | unchanged |
| 0.50% | 35 | SUIUSDT | short | -0.6380 | 0.4761 | False | False | -0.6380 | 0.0000 | unchanged |
| 0.50% | 38 | NEARUSDT | short | -1.1547 | 0.9204 | True | True | 0.0287 | 1.1834 | rescued loser |
| 0.50% | 42 | NEARUSDT | short | -0.6761 | 0.3783 | False | False | -0.6761 | 0.0000 | unchanged |
| 0.50% | 34 | HBARUSDT | long | 0.1545 | 1.1197 | True | True | 0.0034 | -0.1510 | damaged winner |
| 0.50% | 28 | HYPEUSDT | short | 0.5298 | 0.5636 | True | True | 0.0067 | -0.5231 | damaged winner |
| 0.50% | 39 | XRPUSDT | short | -0.6256 | 0.1604 | False | False | -0.6256 | 0.0000 | unchanged |
| 0.50% | 33 | AAVEUSDT | short | -0.8730 | 0.3673 | False | False | -0.8730 | 0.0000 | unchanged |
| 0.50% | 37 | TAOUSDT | short | -0.5544 | 0.3616 | False | False | -0.5544 | 0.0000 | unchanged |
| 0.50% | 43 | NEARUSDT | long | 0.8229 | 0.6923 | True | True | 0.0587 | -0.7642 | damaged winner |
| 0.50% | 41 | APTUSDT | long | -0.1112 | 0.4527 | False | False | -0.1112 | 0.0000 | unchanged |
| 0.50% | 40 | BTCUSDT | long | 0.6735 | 0.7139 | True | False | 0.6735 | 0.0000 | unchanged |
| 0.75% | 20 | MNTUSDT | short | 0.8309 | 3.8865 | True | False | 0.8309 | 0.0000 | unchanged |
| 0.75% | 21 | ETHUSDT | short | -0.2208 | 0.0000 | False | False | -0.2208 | 0.0000 | unchanged |
| 0.75% | 22 | MNTUSDT | short | -0.6700 | 0.0000 | False | False | -0.6700 | 0.0000 | unchanged |
| 0.75% | 25 | ETHUSDT | long | -0.3524 | 1.5801 | True | True | 0.0001 | 0.3525 | rescued loser |
| 0.75% | 19 | NEARUSDT | short | 0.0760 | 1.1779 | True | True | 0.0560 | -0.0200 | damaged winner |
| 0.75% | 31 | HBARUSDT | long | 0.0077 | 4.7120 | True | True | 0.0037 | -0.0040 | damaged winner |
| 0.75% | 23 | XRPUSDT | long | -0.1897 | 1.9350 | True | True | 0.0096 | 0.1992 | rescued loser |
| 0.75% | 27 | ADAUSDT | short | -1.6817 | 0.0000 | False | False | -1.6817 | 0.0000 | unchanged |
| 0.75% | 29 | NEARUSDT | short | 1.2737 | 1.5009 | True | True | 0.0576 | -1.2161 | damaged winner |
| 0.75% | 36 | ETHUSDT | long | -1.7544 | -0.0000 | False | False | -1.7544 | 0.0000 | unchanged |
| 0.75% | 35 | SUIUSDT | short | -0.6380 | 0.4761 | False | False | -0.6380 | 0.0000 | unchanged |
| 0.75% | 38 | NEARUSDT | short | -1.1547 | 0.9204 | True | True | 0.0287 | 1.1834 | rescued loser |
| 0.75% | 42 | NEARUSDT | short | -0.6761 | 0.3783 | False | False | -0.6761 | 0.0000 | unchanged |
| 0.75% | 34 | HBARUSDT | long | 0.1545 | 1.1197 | True | False | 0.1545 | 0.0000 | unchanged |
| 0.75% | 28 | HYPEUSDT | short | 0.5298 | 0.5636 | False | False | 0.5298 | 0.0000 | unchanged |
| 0.75% | 39 | XRPUSDT | short | -0.6256 | 0.1604 | False | False | -0.6256 | 0.0000 | unchanged |
| 0.75% | 33 | AAVEUSDT | short | -0.8730 | 0.3673 | False | False | -0.8730 | 0.0000 | unchanged |
| 0.75% | 37 | TAOUSDT | short | -0.5544 | 0.3616 | False | False | -0.5544 | 0.0000 | unchanged |
| 0.75% | 43 | NEARUSDT | long | 0.8229 | 0.6923 | False | False | 0.8229 | 0.0000 | unchanged |
| 0.75% | 41 | APTUSDT | long | -0.1112 | 0.4527 | False | False | -0.1112 | 0.0000 | unchanged |
| 0.75% | 40 | BTCUSDT | long | 0.6735 | 0.7139 | False | False | 0.6735 | 0.0000 | unchanged |
| 1.00% | 20 | MNTUSDT | short | 0.8309 | 3.8865 | True | False | 0.8309 | 0.0000 | unchanged |
| 1.00% | 21 | ETHUSDT | short | -0.2208 | 0.0000 | False | False | -0.2208 | 0.0000 | unchanged |
| 1.00% | 22 | MNTUSDT | short | -0.6700 | 0.0000 | False | False | -0.6700 | 0.0000 | unchanged |
| 1.00% | 25 | ETHUSDT | long | -0.3524 | 1.5801 | True | True | 0.0001 | 0.3525 | rescued loser |
| 1.00% | 19 | NEARUSDT | short | 0.0760 | 1.1779 | True | True | 0.0560 | -0.0200 | damaged winner |
| 1.00% | 31 | HBARUSDT | long | 0.0077 | 4.7120 | True | True | 0.0037 | -0.0040 | damaged winner |
| 1.00% | 23 | XRPUSDT | long | -0.1897 | 1.9350 | True | True | 0.0096 | 0.1992 | rescued loser |
| 1.00% | 27 | ADAUSDT | short | -1.6817 | 0.0000 | False | False | -1.6817 | 0.0000 | unchanged |
| 1.00% | 29 | NEARUSDT | short | 1.2737 | 1.5009 | True | False | 1.2737 | 0.0000 | unchanged |
| 1.00% | 36 | ETHUSDT | long | -1.7544 | -0.0000 | False | False | -1.7544 | 0.0000 | unchanged |
| 1.00% | 35 | SUIUSDT | short | -0.6380 | 0.4761 | False | False | -0.6380 | 0.0000 | unchanged |
| 1.00% | 38 | NEARUSDT | short | -1.1547 | 0.9204 | False | False | -1.1547 | 0.0000 | unchanged |
| 1.00% | 42 | NEARUSDT | short | -0.6761 | 0.3783 | False | False | -0.6761 | 0.0000 | unchanged |
| 1.00% | 34 | HBARUSDT | long | 0.1545 | 1.1197 | True | False | 0.1545 | 0.0000 | unchanged |
| 1.00% | 28 | HYPEUSDT | short | 0.5298 | 0.5636 | False | False | 0.5298 | 0.0000 | unchanged |
| 1.00% | 39 | XRPUSDT | short | -0.6256 | 0.1604 | False | False | -0.6256 | 0.0000 | unchanged |
| 1.00% | 33 | AAVEUSDT | short | -0.8730 | 0.3673 | False | False | -0.8730 | 0.0000 | unchanged |
| 1.00% | 37 | TAOUSDT | short | -0.5544 | 0.3616 | False | False | -0.5544 | 0.0000 | unchanged |
| 1.00% | 43 | NEARUSDT | long | 0.8229 | 0.6923 | False | False | 0.8229 | 0.0000 | unchanged |
| 1.00% | 41 | APTUSDT | long | -0.1112 | 0.4527 | False | False | -0.1112 | 0.0000 | unchanged |
| 1.00% | 40 | BTCUSDT | long | 0.6735 | 0.7139 | False | False | 0.6735 | 0.0000 | unchanged |
| 0.25R | 20 | MNTUSDT | short | 0.8309 | 3.8865 | True | False | 0.8309 | 0.0000 | unchanged |
| 0.25R | 21 | ETHUSDT | short | -0.2208 | 0.0000 | False | False | -0.2208 | 0.0000 | unchanged |
| 0.25R | 22 | MNTUSDT | short | -0.6700 | 0.0000 | False | False | -0.6700 | 0.0000 | unchanged |
| 0.25R | 25 | ETHUSDT | long | -0.3524 | 1.5801 | True | True | 0.0001 | 0.3525 | rescued loser |
| 0.25R | 19 | NEARUSDT | short | 0.0760 | 1.1779 | True | True | 0.0560 | -0.0200 | damaged winner |
| 0.25R | 31 | HBARUSDT | long | 0.0077 | 4.7120 | True | True | 0.0037 | -0.0040 | damaged winner |
| 0.25R | 23 | XRPUSDT | long | -0.1897 | 1.9350 | True | True | 0.0096 | 0.1992 | rescued loser |
| 0.25R | 27 | ADAUSDT | short | -1.6817 | 0.0000 | False | False | -1.6817 | 0.0000 | unchanged |
| 0.25R | 29 | NEARUSDT | short | 1.2737 | 1.5009 | True | True | 0.0576 | -1.2161 | damaged winner |
| 0.25R | 36 | ETHUSDT | long | -1.7544 | -0.0000 | False | False | -1.7544 | 0.0000 | unchanged |
| 0.25R | 35 | SUIUSDT | short | -0.6380 | 0.4761 | True | True | 0.0081 | 0.6461 | rescued loser |
| 0.25R | 38 | NEARUSDT | short | -1.1547 | 0.9204 | True | True | 0.0287 | 1.1834 | rescued loser |
| 0.25R | 42 | NEARUSDT | short | -0.6761 | 0.3783 | False | False | -0.6761 | 0.0000 | unchanged |
| 0.25R | 34 | HBARUSDT | long | 0.1545 | 1.1197 | True | True | 0.0034 | -0.1510 | damaged winner |
| 0.25R | 28 | HYPEUSDT | short | 0.5298 | 0.5636 | True | True | 0.0067 | -0.5231 | damaged winner |
| 0.25R | 39 | XRPUSDT | short | -0.6256 | 0.1604 | False | False | -0.6256 | 0.0000 | unchanged |
| 0.25R | 33 | AAVEUSDT | short | -0.8730 | 0.3673 | False | False | -0.8730 | 0.0000 | unchanged |
| 0.25R | 37 | TAOUSDT | short | -0.5544 | 0.3616 | False | False | -0.5544 | 0.0000 | unchanged |
| 0.25R | 43 | NEARUSDT | long | 0.8229 | 0.6923 | True | True | 0.0587 | -0.7642 | damaged winner |
| 0.25R | 41 | APTUSDT | long | -0.1112 | 0.4527 | True | False | -0.1112 | 0.0000 | unchanged |
| 0.25R | 40 | BTCUSDT | long | 0.6735 | 0.7139 | True | True | 0.0001 | -0.6734 | damaged winner |
| 0.50R | 20 | MNTUSDT | short | 0.8309 | 3.8865 | True | False | 0.8309 | 0.0000 | unchanged |
| 0.50R | 21 | ETHUSDT | short | -0.2208 | 0.0000 | False | False | -0.2208 | 0.0000 | unchanged |
| 0.50R | 22 | MNTUSDT | short | -0.6700 | 0.0000 | False | False | -0.6700 | 0.0000 | unchanged |
| 0.50R | 25 | ETHUSDT | long | -0.3524 | 1.5801 | True | True | 0.0001 | 0.3525 | rescued loser |
| 0.50R | 19 | NEARUSDT | short | 0.0760 | 1.1779 | True | True | 0.0560 | -0.0200 | damaged winner |
| 0.50R | 31 | HBARUSDT | long | 0.0077 | 4.7120 | True | True | 0.0037 | -0.0040 | damaged winner |
| 0.50R | 23 | XRPUSDT | long | -0.1897 | 1.9350 | True | True | 0.0096 | 0.1992 | rescued loser |
| 0.50R | 27 | ADAUSDT | short | -1.6817 | 0.0000 | False | False | -1.6817 | 0.0000 | unchanged |
| 0.50R | 29 | NEARUSDT | short | 1.2737 | 1.5009 | True | True | 0.0576 | -1.2161 | damaged winner |
| 0.50R | 36 | ETHUSDT | long | -1.7544 | -0.0000 | False | False | -1.7544 | 0.0000 | unchanged |
| 0.50R | 35 | SUIUSDT | short | -0.6380 | 0.4761 | False | False | -0.6380 | 0.0000 | unchanged |
| 0.50R | 38 | NEARUSDT | short | -1.1547 | 0.9204 | True | True | 0.0287 | 1.1834 | rescued loser |
| 0.50R | 42 | NEARUSDT | short | -0.6761 | 0.3783 | False | False | -0.6761 | 0.0000 | unchanged |
| 0.50R | 34 | HBARUSDT | long | 0.1545 | 1.1197 | True | False | 0.1545 | 0.0000 | unchanged |
| 0.50R | 28 | HYPEUSDT | short | 0.5298 | 0.5636 | False | False | 0.5298 | 0.0000 | unchanged |
| 0.50R | 39 | XRPUSDT | short | -0.6256 | 0.1604 | False | False | -0.6256 | 0.0000 | unchanged |
| 0.50R | 33 | AAVEUSDT | short | -0.8730 | 0.3673 | False | False | -0.8730 | 0.0000 | unchanged |
| 0.50R | 37 | TAOUSDT | short | -0.5544 | 0.3616 | False | False | -0.5544 | 0.0000 | unchanged |
| 0.50R | 43 | NEARUSDT | long | 0.8229 | 0.6923 | True | True | 0.0587 | -0.7642 | damaged winner |
| 0.50R | 41 | APTUSDT | long | -0.1112 | 0.4527 | False | False | -0.1112 | 0.0000 | unchanged |
| 0.50R | 40 | BTCUSDT | long | 0.6735 | 0.7139 | True | False | 0.6735 | 0.0000 | unchanged |
| 0.75R | 20 | MNTUSDT | short | 0.8309 | 3.8865 | True | False | 0.8309 | 0.0000 | unchanged |
| 0.75R | 21 | ETHUSDT | short | -0.2208 | 0.0000 | False | False | -0.2208 | 0.0000 | unchanged |
| 0.75R | 22 | MNTUSDT | short | -0.6700 | 0.0000 | False | False | -0.6700 | 0.0000 | unchanged |
| 0.75R | 25 | ETHUSDT | long | -0.3524 | 1.5801 | True | True | 0.0001 | 0.3525 | rescued loser |
| 0.75R | 19 | NEARUSDT | short | 0.0760 | 1.1779 | True | True | 0.0560 | -0.0200 | damaged winner |
| 0.75R | 31 | HBARUSDT | long | 0.0077 | 4.7120 | True | True | 0.0037 | -0.0040 | damaged winner |
| 0.75R | 23 | XRPUSDT | long | -0.1897 | 1.9350 | True | True | 0.0096 | 0.1992 | rescued loser |
| 0.75R | 27 | ADAUSDT | short | -1.6817 | 0.0000 | False | False | -1.6817 | 0.0000 | unchanged |
| 0.75R | 29 | NEARUSDT | short | 1.2737 | 1.5009 | True | False | 1.2737 | 0.0000 | unchanged |
| 0.75R | 36 | ETHUSDT | long | -1.7544 | -0.0000 | False | False | -1.7544 | 0.0000 | unchanged |
| 0.75R | 35 | SUIUSDT | short | -0.6380 | 0.4761 | False | False | -0.6380 | 0.0000 | unchanged |
| 0.75R | 38 | NEARUSDT | short | -1.1547 | 0.9204 | False | False | -1.1547 | 0.0000 | unchanged |
| 0.75R | 42 | NEARUSDT | short | -0.6761 | 0.3783 | False | False | -0.6761 | 0.0000 | unchanged |
| 0.75R | 34 | HBARUSDT | long | 0.1545 | 1.1197 | True | False | 0.1545 | 0.0000 | unchanged |
| 0.75R | 28 | HYPEUSDT | short | 0.5298 | 0.5636 | False | False | 0.5298 | 0.0000 | unchanged |
| 0.75R | 39 | XRPUSDT | short | -0.6256 | 0.1604 | False | False | -0.6256 | 0.0000 | unchanged |
| 0.75R | 33 | AAVEUSDT | short | -0.8730 | 0.3673 | False | False | -0.8730 | 0.0000 | unchanged |
| 0.75R | 37 | TAOUSDT | short | -0.5544 | 0.3616 | False | False | -0.5544 | 0.0000 | unchanged |
| 0.75R | 43 | NEARUSDT | long | 0.8229 | 0.6923 | False | False | 0.8229 | 0.0000 | unchanged |
| 0.75R | 41 | APTUSDT | long | -0.1112 | 0.4527 | False | False | -0.1112 | 0.0000 | unchanged |
| 0.75R | 40 | BTCUSDT | long | 0.6735 | 0.7139 | False | False | 0.6735 | 0.0000 | unchanged |
| 1.00R | 20 | MNTUSDT | short | 0.8309 | 3.8865 | True | False | 0.8309 | 0.0000 | unchanged |
| 1.00R | 21 | ETHUSDT | short | -0.2208 | 0.0000 | False | False | -0.2208 | 0.0000 | unchanged |
| 1.00R | 22 | MNTUSDT | short | -0.6700 | 0.0000 | False | False | -0.6700 | 0.0000 | unchanged |
| 1.00R | 25 | ETHUSDT | long | -0.3524 | 1.5801 | True | True | 0.0001 | 0.3525 | rescued loser |
| 1.00R | 19 | NEARUSDT | short | 0.0760 | 1.1779 | False | False | 0.0760 | 0.0000 | unchanged |
| 1.00R | 31 | HBARUSDT | long | 0.0077 | 4.7120 | True | True | 0.0037 | -0.0040 | damaged winner |
| 1.00R | 23 | XRPUSDT | long | -0.1897 | 1.9350 | True | True | 0.0096 | 0.1992 | rescued loser |
| 1.00R | 27 | ADAUSDT | short | -1.6817 | 0.0000 | False | False | -1.6817 | 0.0000 | unchanged |
| 1.00R | 29 | NEARUSDT | short | 1.2737 | 1.5009 | False | False | 1.2737 | 0.0000 | unchanged |
| 1.00R | 36 | ETHUSDT | long | -1.7544 | -0.0000 | False | False | -1.7544 | 0.0000 | unchanged |
| 1.00R | 35 | SUIUSDT | short | -0.6380 | 0.4761 | False | False | -0.6380 | 0.0000 | unchanged |
| 1.00R | 38 | NEARUSDT | short | -1.1547 | 0.9204 | False | False | -1.1547 | 0.0000 | unchanged |
| 1.00R | 42 | NEARUSDT | short | -0.6761 | 0.3783 | False | False | -0.6761 | 0.0000 | unchanged |
| 1.00R | 34 | HBARUSDT | long | 0.1545 | 1.1197 | False | False | 0.1545 | 0.0000 | unchanged |
| 1.00R | 28 | HYPEUSDT | short | 0.5298 | 0.5636 | False | False | 0.5298 | 0.0000 | unchanged |
| 1.00R | 39 | XRPUSDT | short | -0.6256 | 0.1604 | False | False | -0.6256 | 0.0000 | unchanged |
| 1.00R | 33 | AAVEUSDT | short | -0.8730 | 0.3673 | False | False | -0.8730 | 0.0000 | unchanged |
| 1.00R | 37 | TAOUSDT | short | -0.5544 | 0.3616 | False | False | -0.5544 | 0.0000 | unchanged |
| 1.00R | 43 | NEARUSDT | long | 0.8229 | 0.6923 | False | False | 0.8229 | 0.0000 | unchanged |
| 1.00R | 41 | APTUSDT | long | -0.1112 | 0.4527 | False | False | -0.1112 | 0.0000 | unchanged |
| 1.00R | 40 | BTCUSDT | long | 0.6735 | 0.7139 | False | False | 0.6735 | 0.0000 | unchanged |

## Sample: normalized_5_anomalous_fills

### Comparison

| Policy | P&L | R | PF | Expectancy | Win % | Avg win | Avg loss | Max DD | BE exits | Losers saved | Winners damaged |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| baseline | -8.8199 | -7.0244 | 0.3313 | -0.3392 | 30.7692 | 0.5461 | -0.7327 | 10.3243 | 0 | 0 | 0 |
| 0.25% | -7.2704 | -6.3087 | 0.1358 | -0.2796 | 61.5385 | 0.0714 | -0.8413 | 8.1013 | 15 | 8 | 7 |
| 0.50% | -9.7633 | -7.8317 | 0.1504 | -0.3755 | 42.3077 | 0.1572 | -0.7661 | 11.2677 | 9 | 3 | 6 |
| 0.75% | -8.3249 | -6.6791 | 0.2756 | -0.3202 | 42.3077 | 0.2879 | -0.7661 | 9.8293 | 6 | 3 | 3 |
| 1.00% | -8.2922 | -6.6626 | 0.3443 | -0.3189 | 38.4615 | 0.4355 | -0.7904 | 9.7966 | 4 | 2 | 2 |
| 0.25R | -8.8046 | -6.7833 | 0.1089 | -0.3386 | 53.8462 | 0.0769 | -0.8234 | 9.6356 | 13 | 6 | 7 |
| 0.50R | -9.0892 | -7.3540 | 0.2091 | -0.3496 | 42.3077 | 0.2185 | -0.7661 | 10.5936 | 7 | 3 | 4 |
| 0.75R | -8.2922 | -6.6626 | 0.3443 | -0.3189 | 38.4615 | 0.4355 | -0.7904 | 9.7966 | 4 | 2 | 2 |
| 1.00R | -8.2723 | -6.6497 | 0.3459 | -0.3182 | 38.4615 | 0.4375 | -0.7904 | 9.7767 | 3 | 2 | 1 |

### Per-trade results

| Policy | Trade | Symbol | Side | Baseline | MFE % | Activated | BE exit | Counterfactual | Delta | Class |
|---|---:|---|---|---:|---:|---|---|---:|---:|---|
| 0.25% | 20 | MNTUSDT | short | 0.8309 | 3.8865 | True | False | 0.8309 | 0.0000 | unchanged |
| 0.25% | 21 | ETHUSDT | short | -0.2208 | 0.0000 | False | False | -0.2208 | 0.0000 | unchanged |
| 0.25% | 22 | MNTUSDT | short | -0.6700 | 0.0000 | False | False | -0.6700 | 0.0000 | unchanged |
| 0.25% | 25 | ETHUSDT | long | -0.3524 | 1.5801 | True | True | 0.0001 | 0.3525 | rescued loser |
| 0.25% | 19 | NEARUSDT | short | 0.0760 | 1.1779 | True | True | 0.0560 | -0.0200 | damaged winner |
| 0.25% | 26 | ETHUSDT | long | -0.6350 | 0.1464 | False | False | -0.6350 | 0.0000 | unchanged |
| 0.25% | 30 | ETHUSDT | short | -1.5415 | 0.0026 | False | False | -1.5415 | 0.0000 | unchanged |
| 0.25% | 24 | BNBUSDT | short | -0.6824 | 0.0995 | False | False | -0.6824 | 0.0000 | unchanged |
| 0.25% | 31 | HBARUSDT | long | 0.0077 | 4.7120 | True | True | 0.0037 | -0.0040 | damaged winner |
| 0.25% | 23 | XRPUSDT | long | -0.1897 | 1.9350 | True | True | 0.0096 | 0.1992 | rescued loser |
| 0.25% | 27 | ADAUSDT | short | -1.6817 | 0.0000 | False | False | -1.6817 | 0.0000 | unchanged |
| 0.25% | 29 | NEARUSDT | short | 1.2737 | 1.5009 | True | True | 0.0576 | -1.2161 | damaged winner |
| 0.25% | 36 | ETHUSDT | long | -1.7544 | -0.0000 | False | False | -1.7544 | 0.0000 | unchanged |
| 0.25% | 35 | SUIUSDT | short | -0.6380 | 0.4761 | True | True | 0.0081 | 0.6461 | rescued loser |
| 0.25% | 38 | NEARUSDT | short | -1.1547 | 0.9204 | True | True | 0.0287 | 1.1834 | rescued loser |
| 0.25% | 42 | NEARUSDT | short | -0.6761 | 0.3783 | True | True | 0.0591 | 0.7353 | rescued loser |
| 0.25% | 34 | HBARUSDT | long | 0.1545 | 1.1197 | True | True | 0.0034 | -0.1510 | damaged winner |
| 0.25% | 28 | HYPEUSDT | short | 0.5298 | 0.5636 | True | True | 0.0067 | -0.5231 | damaged winner |
| 0.25% | 39 | XRPUSDT | short | -0.6256 | 0.1604 | False | False | -0.6256 | 0.0000 | unchanged |
| 0.25% | 33 | AAVEUSDT | short | -0.8730 | 0.3673 | True | True | 0.0062 | 0.8792 | rescued loser |
| 0.25% | 37 | TAOUSDT | short | -0.5544 | 0.3616 | True | True | 0.0009 | 0.5552 | rescued loser |
| 0.25% | 43 | NEARUSDT | long | 0.8229 | 0.6923 | True | True | 0.0587 | -0.7642 | damaged winner |
| 0.25% | 32 | DOGEUSDT | short | -0.3381 | 0.4988 | True | True | 0.0126 | 0.3507 | rescued loser |
| 0.25% | 41 | APTUSDT | long | -0.1112 | 0.4527 | True | False | -0.1112 | 0.0000 | unchanged |
| 0.25% | 44 | BNBUSDT | short | -0.4900 | 0.0000 | False | False | -0.4900 | 0.0000 | unchanged |
| 0.25% | 40 | BTCUSDT | long | 0.6735 | 0.7139 | True | True | 0.0001 | -0.6734 | damaged winner |
| 0.50% | 20 | MNTUSDT | short | 0.8309 | 3.8865 | True | False | 0.8309 | 0.0000 | unchanged |
| 0.50% | 21 | ETHUSDT | short | -0.2208 | 0.0000 | False | False | -0.2208 | 0.0000 | unchanged |
| 0.50% | 22 | MNTUSDT | short | -0.6700 | 0.0000 | False | False | -0.6700 | 0.0000 | unchanged |
| 0.50% | 25 | ETHUSDT | long | -0.3524 | 1.5801 | True | True | 0.0001 | 0.3525 | rescued loser |
| 0.50% | 19 | NEARUSDT | short | 0.0760 | 1.1779 | True | True | 0.0560 | -0.0200 | damaged winner |
| 0.50% | 26 | ETHUSDT | long | -0.6350 | 0.1464 | False | False | -0.6350 | 0.0000 | unchanged |
| 0.50% | 30 | ETHUSDT | short | -1.5415 | 0.0026 | False | False | -1.5415 | 0.0000 | unchanged |
| 0.50% | 24 | BNBUSDT | short | -0.6824 | 0.0995 | False | False | -0.6824 | 0.0000 | unchanged |
| 0.50% | 31 | HBARUSDT | long | 0.0077 | 4.7120 | True | True | 0.0037 | -0.0040 | damaged winner |
| 0.50% | 23 | XRPUSDT | long | -0.1897 | 1.9350 | True | True | 0.0096 | 0.1992 | rescued loser |
| 0.50% | 27 | ADAUSDT | short | -1.6817 | 0.0000 | False | False | -1.6817 | 0.0000 | unchanged |
| 0.50% | 29 | NEARUSDT | short | 1.2737 | 1.5009 | True | True | 0.0576 | -1.2161 | damaged winner |
| 0.50% | 36 | ETHUSDT | long | -1.7544 | -0.0000 | False | False | -1.7544 | 0.0000 | unchanged |
| 0.50% | 35 | SUIUSDT | short | -0.6380 | 0.4761 | False | False | -0.6380 | 0.0000 | unchanged |
| 0.50% | 38 | NEARUSDT | short | -1.1547 | 0.9204 | True | True | 0.0287 | 1.1834 | rescued loser |
| 0.50% | 42 | NEARUSDT | short | -0.6761 | 0.3783 | False | False | -0.6761 | 0.0000 | unchanged |
| 0.50% | 34 | HBARUSDT | long | 0.1545 | 1.1197 | True | True | 0.0034 | -0.1510 | damaged winner |
| 0.50% | 28 | HYPEUSDT | short | 0.5298 | 0.5636 | True | True | 0.0067 | -0.5231 | damaged winner |
| 0.50% | 39 | XRPUSDT | short | -0.6256 | 0.1604 | False | False | -0.6256 | 0.0000 | unchanged |
| 0.50% | 33 | AAVEUSDT | short | -0.8730 | 0.3673 | False | False | -0.8730 | 0.0000 | unchanged |
| 0.50% | 37 | TAOUSDT | short | -0.5544 | 0.3616 | False | False | -0.5544 | 0.0000 | unchanged |
| 0.50% | 43 | NEARUSDT | long | 0.8229 | 0.6923 | True | True | 0.0587 | -0.7642 | damaged winner |
| 0.50% | 32 | DOGEUSDT | short | -0.3381 | 0.4988 | False | False | -0.3381 | 0.0000 | unchanged |
| 0.50% | 41 | APTUSDT | long | -0.1112 | 0.4527 | False | False | -0.1112 | 0.0000 | unchanged |
| 0.50% | 44 | BNBUSDT | short | -0.4900 | 0.0000 | False | False | -0.4900 | 0.0000 | unchanged |
| 0.50% | 40 | BTCUSDT | long | 0.6735 | 0.7139 | True | False | 0.6735 | 0.0000 | unchanged |
| 0.75% | 20 | MNTUSDT | short | 0.8309 | 3.8865 | True | False | 0.8309 | 0.0000 | unchanged |
| 0.75% | 21 | ETHUSDT | short | -0.2208 | 0.0000 | False | False | -0.2208 | 0.0000 | unchanged |
| 0.75% | 22 | MNTUSDT | short | -0.6700 | 0.0000 | False | False | -0.6700 | 0.0000 | unchanged |
| 0.75% | 25 | ETHUSDT | long | -0.3524 | 1.5801 | True | True | 0.0001 | 0.3525 | rescued loser |
| 0.75% | 19 | NEARUSDT | short | 0.0760 | 1.1779 | True | True | 0.0560 | -0.0200 | damaged winner |
| 0.75% | 26 | ETHUSDT | long | -0.6350 | 0.1464 | False | False | -0.6350 | 0.0000 | unchanged |
| 0.75% | 30 | ETHUSDT | short | -1.5415 | 0.0026 | False | False | -1.5415 | 0.0000 | unchanged |
| 0.75% | 24 | BNBUSDT | short | -0.6824 | 0.0995 | False | False | -0.6824 | 0.0000 | unchanged |
| 0.75% | 31 | HBARUSDT | long | 0.0077 | 4.7120 | True | True | 0.0037 | -0.0040 | damaged winner |
| 0.75% | 23 | XRPUSDT | long | -0.1897 | 1.9350 | True | True | 0.0096 | 0.1992 | rescued loser |
| 0.75% | 27 | ADAUSDT | short | -1.6817 | 0.0000 | False | False | -1.6817 | 0.0000 | unchanged |
| 0.75% | 29 | NEARUSDT | short | 1.2737 | 1.5009 | True | True | 0.0576 | -1.2161 | damaged winner |
| 0.75% | 36 | ETHUSDT | long | -1.7544 | -0.0000 | False | False | -1.7544 | 0.0000 | unchanged |
| 0.75% | 35 | SUIUSDT | short | -0.6380 | 0.4761 | False | False | -0.6380 | 0.0000 | unchanged |
| 0.75% | 38 | NEARUSDT | short | -1.1547 | 0.9204 | True | True | 0.0287 | 1.1834 | rescued loser |
| 0.75% | 42 | NEARUSDT | short | -0.6761 | 0.3783 | False | False | -0.6761 | 0.0000 | unchanged |
| 0.75% | 34 | HBARUSDT | long | 0.1545 | 1.1197 | True | False | 0.1545 | 0.0000 | unchanged |
| 0.75% | 28 | HYPEUSDT | short | 0.5298 | 0.5636 | False | False | 0.5298 | 0.0000 | unchanged |
| 0.75% | 39 | XRPUSDT | short | -0.6256 | 0.1604 | False | False | -0.6256 | 0.0000 | unchanged |
| 0.75% | 33 | AAVEUSDT | short | -0.8730 | 0.3673 | False | False | -0.8730 | 0.0000 | unchanged |
| 0.75% | 37 | TAOUSDT | short | -0.5544 | 0.3616 | False | False | -0.5544 | 0.0000 | unchanged |
| 0.75% | 43 | NEARUSDT | long | 0.8229 | 0.6923 | False | False | 0.8229 | 0.0000 | unchanged |
| 0.75% | 32 | DOGEUSDT | short | -0.3381 | 0.4988 | False | False | -0.3381 | 0.0000 | unchanged |
| 0.75% | 41 | APTUSDT | long | -0.1112 | 0.4527 | False | False | -0.1112 | 0.0000 | unchanged |
| 0.75% | 44 | BNBUSDT | short | -0.4900 | 0.0000 | False | False | -0.4900 | 0.0000 | unchanged |
| 0.75% | 40 | BTCUSDT | long | 0.6735 | 0.7139 | False | False | 0.6735 | 0.0000 | unchanged |
| 1.00% | 20 | MNTUSDT | short | 0.8309 | 3.8865 | True | False | 0.8309 | 0.0000 | unchanged |
| 1.00% | 21 | ETHUSDT | short | -0.2208 | 0.0000 | False | False | -0.2208 | 0.0000 | unchanged |
| 1.00% | 22 | MNTUSDT | short | -0.6700 | 0.0000 | False | False | -0.6700 | 0.0000 | unchanged |
| 1.00% | 25 | ETHUSDT | long | -0.3524 | 1.5801 | True | True | 0.0001 | 0.3525 | rescued loser |
| 1.00% | 19 | NEARUSDT | short | 0.0760 | 1.1779 | True | True | 0.0560 | -0.0200 | damaged winner |
| 1.00% | 26 | ETHUSDT | long | -0.6350 | 0.1464 | False | False | -0.6350 | 0.0000 | unchanged |
| 1.00% | 30 | ETHUSDT | short | -1.5415 | 0.0026 | False | False | -1.5415 | 0.0000 | unchanged |
| 1.00% | 24 | BNBUSDT | short | -0.6824 | 0.0995 | False | False | -0.6824 | 0.0000 | unchanged |
| 1.00% | 31 | HBARUSDT | long | 0.0077 | 4.7120 | True | True | 0.0037 | -0.0040 | damaged winner |
| 1.00% | 23 | XRPUSDT | long | -0.1897 | 1.9350 | True | True | 0.0096 | 0.1992 | rescued loser |
| 1.00% | 27 | ADAUSDT | short | -1.6817 | 0.0000 | False | False | -1.6817 | 0.0000 | unchanged |
| 1.00% | 29 | NEARUSDT | short | 1.2737 | 1.5009 | True | False | 1.2737 | 0.0000 | unchanged |
| 1.00% | 36 | ETHUSDT | long | -1.7544 | -0.0000 | False | False | -1.7544 | 0.0000 | unchanged |
| 1.00% | 35 | SUIUSDT | short | -0.6380 | 0.4761 | False | False | -0.6380 | 0.0000 | unchanged |
| 1.00% | 38 | NEARUSDT | short | -1.1547 | 0.9204 | False | False | -1.1547 | 0.0000 | unchanged |
| 1.00% | 42 | NEARUSDT | short | -0.6761 | 0.3783 | False | False | -0.6761 | 0.0000 | unchanged |
| 1.00% | 34 | HBARUSDT | long | 0.1545 | 1.1197 | True | False | 0.1545 | 0.0000 | unchanged |
| 1.00% | 28 | HYPEUSDT | short | 0.5298 | 0.5636 | False | False | 0.5298 | 0.0000 | unchanged |
| 1.00% | 39 | XRPUSDT | short | -0.6256 | 0.1604 | False | False | -0.6256 | 0.0000 | unchanged |
| 1.00% | 33 | AAVEUSDT | short | -0.8730 | 0.3673 | False | False | -0.8730 | 0.0000 | unchanged |
| 1.00% | 37 | TAOUSDT | short | -0.5544 | 0.3616 | False | False | -0.5544 | 0.0000 | unchanged |
| 1.00% | 43 | NEARUSDT | long | 0.8229 | 0.6923 | False | False | 0.8229 | 0.0000 | unchanged |
| 1.00% | 32 | DOGEUSDT | short | -0.3381 | 0.4988 | False | False | -0.3381 | 0.0000 | unchanged |
| 1.00% | 41 | APTUSDT | long | -0.1112 | 0.4527 | False | False | -0.1112 | 0.0000 | unchanged |
| 1.00% | 44 | BNBUSDT | short | -0.4900 | 0.0000 | False | False | -0.4900 | 0.0000 | unchanged |
| 1.00% | 40 | BTCUSDT | long | 0.6735 | 0.7139 | False | False | 0.6735 | 0.0000 | unchanged |
| 0.25R | 20 | MNTUSDT | short | 0.8309 | 3.8865 | True | False | 0.8309 | 0.0000 | unchanged |
| 0.25R | 21 | ETHUSDT | short | -0.2208 | 0.0000 | False | False | -0.2208 | 0.0000 | unchanged |
| 0.25R | 22 | MNTUSDT | short | -0.6700 | 0.0000 | False | False | -0.6700 | 0.0000 | unchanged |
| 0.25R | 25 | ETHUSDT | long | -0.3524 | 1.5801 | True | True | 0.0001 | 0.3525 | rescued loser |
| 0.25R | 19 | NEARUSDT | short | 0.0760 | 1.1779 | True | True | 0.0560 | -0.0200 | damaged winner |
| 0.25R | 26 | ETHUSDT | long | -0.6350 | 0.1464 | True | True | 0.0004 | 0.6354 | rescued loser |
| 0.25R | 30 | ETHUSDT | short | -1.5415 | 0.0026 | False | False | -1.5415 | 0.0000 | unchanged |
| 0.25R | 24 | BNBUSDT | short | -0.6824 | 0.0995 | False | False | -0.6824 | 0.0000 | unchanged |
| 0.25R | 31 | HBARUSDT | long | 0.0077 | 4.7120 | True | True | 0.0037 | -0.0040 | damaged winner |
| 0.25R | 23 | XRPUSDT | long | -0.1897 | 1.9350 | True | True | 0.0096 | 0.1992 | rescued loser |
| 0.25R | 27 | ADAUSDT | short | -1.6817 | 0.0000 | False | False | -1.6817 | 0.0000 | unchanged |
| 0.25R | 29 | NEARUSDT | short | 1.2737 | 1.5009 | True | True | 0.0576 | -1.2161 | damaged winner |
| 0.25R | 36 | ETHUSDT | long | -1.7544 | -0.0000 | False | False | -1.7544 | 0.0000 | unchanged |
| 0.25R | 35 | SUIUSDT | short | -0.6380 | 0.4761 | True | True | 0.0081 | 0.6461 | rescued loser |
| 0.25R | 38 | NEARUSDT | short | -1.1547 | 0.9204 | True | True | 0.0287 | 1.1834 | rescued loser |
| 0.25R | 42 | NEARUSDT | short | -0.6761 | 0.3783 | False | False | -0.6761 | 0.0000 | unchanged |
| 0.25R | 34 | HBARUSDT | long | 0.1545 | 1.1197 | True | True | 0.0034 | -0.1510 | damaged winner |
| 0.25R | 28 | HYPEUSDT | short | 0.5298 | 0.5636 | True | True | 0.0067 | -0.5231 | damaged winner |
| 0.25R | 39 | XRPUSDT | short | -0.6256 | 0.1604 | False | False | -0.6256 | 0.0000 | unchanged |
| 0.25R | 33 | AAVEUSDT | short | -0.8730 | 0.3673 | False | False | -0.8730 | 0.0000 | unchanged |
| 0.25R | 37 | TAOUSDT | short | -0.5544 | 0.3616 | False | False | -0.5544 | 0.0000 | unchanged |
| 0.25R | 43 | NEARUSDT | long | 0.8229 | 0.6923 | True | True | 0.0587 | -0.7642 | damaged winner |
| 0.25R | 32 | DOGEUSDT | short | -0.3381 | 0.4988 | True | True | 0.0126 | 0.3507 | rescued loser |
| 0.25R | 41 | APTUSDT | long | -0.1112 | 0.4527 | True | False | -0.1112 | 0.0000 | unchanged |
| 0.25R | 44 | BNBUSDT | short | -0.4900 | 0.0000 | False | False | -0.4900 | 0.0000 | unchanged |
| 0.25R | 40 | BTCUSDT | long | 0.6735 | 0.7139 | True | True | 0.0001 | -0.6734 | damaged winner |
| 0.50R | 20 | MNTUSDT | short | 0.8309 | 3.8865 | True | False | 0.8309 | 0.0000 | unchanged |
| 0.50R | 21 | ETHUSDT | short | -0.2208 | 0.0000 | False | False | -0.2208 | 0.0000 | unchanged |
| 0.50R | 22 | MNTUSDT | short | -0.6700 | 0.0000 | False | False | -0.6700 | 0.0000 | unchanged |
| 0.50R | 25 | ETHUSDT | long | -0.3524 | 1.5801 | True | True | 0.0001 | 0.3525 | rescued loser |
| 0.50R | 19 | NEARUSDT | short | 0.0760 | 1.1779 | True | True | 0.0560 | -0.0200 | damaged winner |
| 0.50R | 26 | ETHUSDT | long | -0.6350 | 0.1464 | False | False | -0.6350 | 0.0000 | unchanged |
| 0.50R | 30 | ETHUSDT | short | -1.5415 | 0.0026 | False | False | -1.5415 | 0.0000 | unchanged |
| 0.50R | 24 | BNBUSDT | short | -0.6824 | 0.0995 | False | False | -0.6824 | 0.0000 | unchanged |
| 0.50R | 31 | HBARUSDT | long | 0.0077 | 4.7120 | True | True | 0.0037 | -0.0040 | damaged winner |
| 0.50R | 23 | XRPUSDT | long | -0.1897 | 1.9350 | True | True | 0.0096 | 0.1992 | rescued loser |
| 0.50R | 27 | ADAUSDT | short | -1.6817 | 0.0000 | False | False | -1.6817 | 0.0000 | unchanged |
| 0.50R | 29 | NEARUSDT | short | 1.2737 | 1.5009 | True | True | 0.0576 | -1.2161 | damaged winner |
| 0.50R | 36 | ETHUSDT | long | -1.7544 | -0.0000 | False | False | -1.7544 | 0.0000 | unchanged |
| 0.50R | 35 | SUIUSDT | short | -0.6380 | 0.4761 | False | False | -0.6380 | 0.0000 | unchanged |
| 0.50R | 38 | NEARUSDT | short | -1.1547 | 0.9204 | True | True | 0.0287 | 1.1834 | rescued loser |
| 0.50R | 42 | NEARUSDT | short | -0.6761 | 0.3783 | False | False | -0.6761 | 0.0000 | unchanged |
| 0.50R | 34 | HBARUSDT | long | 0.1545 | 1.1197 | True | False | 0.1545 | 0.0000 | unchanged |
| 0.50R | 28 | HYPEUSDT | short | 0.5298 | 0.5636 | False | False | 0.5298 | 0.0000 | unchanged |
| 0.50R | 39 | XRPUSDT | short | -0.6256 | 0.1604 | False | False | -0.6256 | 0.0000 | unchanged |
| 0.50R | 33 | AAVEUSDT | short | -0.8730 | 0.3673 | False | False | -0.8730 | 0.0000 | unchanged |
| 0.50R | 37 | TAOUSDT | short | -0.5544 | 0.3616 | False | False | -0.5544 | 0.0000 | unchanged |
| 0.50R | 43 | NEARUSDT | long | 0.8229 | 0.6923 | True | True | 0.0587 | -0.7642 | damaged winner |
| 0.50R | 32 | DOGEUSDT | short | -0.3381 | 0.4988 | False | False | -0.3381 | 0.0000 | unchanged |
| 0.50R | 41 | APTUSDT | long | -0.1112 | 0.4527 | False | False | -0.1112 | 0.0000 | unchanged |
| 0.50R | 44 | BNBUSDT | short | -0.4900 | 0.0000 | False | False | -0.4900 | 0.0000 | unchanged |
| 0.50R | 40 | BTCUSDT | long | 0.6735 | 0.7139 | True | False | 0.6735 | 0.0000 | unchanged |
| 0.75R | 20 | MNTUSDT | short | 0.8309 | 3.8865 | True | False | 0.8309 | 0.0000 | unchanged |
| 0.75R | 21 | ETHUSDT | short | -0.2208 | 0.0000 | False | False | -0.2208 | 0.0000 | unchanged |
| 0.75R | 22 | MNTUSDT | short | -0.6700 | 0.0000 | False | False | -0.6700 | 0.0000 | unchanged |
| 0.75R | 25 | ETHUSDT | long | -0.3524 | 1.5801 | True | True | 0.0001 | 0.3525 | rescued loser |
| 0.75R | 19 | NEARUSDT | short | 0.0760 | 1.1779 | True | True | 0.0560 | -0.0200 | damaged winner |
| 0.75R | 26 | ETHUSDT | long | -0.6350 | 0.1464 | False | False | -0.6350 | 0.0000 | unchanged |
| 0.75R | 30 | ETHUSDT | short | -1.5415 | 0.0026 | False | False | -1.5415 | 0.0000 | unchanged |
| 0.75R | 24 | BNBUSDT | short | -0.6824 | 0.0995 | False | False | -0.6824 | 0.0000 | unchanged |
| 0.75R | 31 | HBARUSDT | long | 0.0077 | 4.7120 | True | True | 0.0037 | -0.0040 | damaged winner |
| 0.75R | 23 | XRPUSDT | long | -0.1897 | 1.9350 | True | True | 0.0096 | 0.1992 | rescued loser |
| 0.75R | 27 | ADAUSDT | short | -1.6817 | 0.0000 | False | False | -1.6817 | 0.0000 | unchanged |
| 0.75R | 29 | NEARUSDT | short | 1.2737 | 1.5009 | True | False | 1.2737 | 0.0000 | unchanged |
| 0.75R | 36 | ETHUSDT | long | -1.7544 | -0.0000 | False | False | -1.7544 | 0.0000 | unchanged |
| 0.75R | 35 | SUIUSDT | short | -0.6380 | 0.4761 | False | False | -0.6380 | 0.0000 | unchanged |
| 0.75R | 38 | NEARUSDT | short | -1.1547 | 0.9204 | False | False | -1.1547 | 0.0000 | unchanged |
| 0.75R | 42 | NEARUSDT | short | -0.6761 | 0.3783 | False | False | -0.6761 | 0.0000 | unchanged |
| 0.75R | 34 | HBARUSDT | long | 0.1545 | 1.1197 | True | False | 0.1545 | 0.0000 | unchanged |
| 0.75R | 28 | HYPEUSDT | short | 0.5298 | 0.5636 | False | False | 0.5298 | 0.0000 | unchanged |
| 0.75R | 39 | XRPUSDT | short | -0.6256 | 0.1604 | False | False | -0.6256 | 0.0000 | unchanged |
| 0.75R | 33 | AAVEUSDT | short | -0.8730 | 0.3673 | False | False | -0.8730 | 0.0000 | unchanged |
| 0.75R | 37 | TAOUSDT | short | -0.5544 | 0.3616 | False | False | -0.5544 | 0.0000 | unchanged |
| 0.75R | 43 | NEARUSDT | long | 0.8229 | 0.6923 | False | False | 0.8229 | 0.0000 | unchanged |
| 0.75R | 32 | DOGEUSDT | short | -0.3381 | 0.4988 | False | False | -0.3381 | 0.0000 | unchanged |
| 0.75R | 41 | APTUSDT | long | -0.1112 | 0.4527 | False | False | -0.1112 | 0.0000 | unchanged |
| 0.75R | 44 | BNBUSDT | short | -0.4900 | 0.0000 | False | False | -0.4900 | 0.0000 | unchanged |
| 0.75R | 40 | BTCUSDT | long | 0.6735 | 0.7139 | False | False | 0.6735 | 0.0000 | unchanged |
| 1.00R | 20 | MNTUSDT | short | 0.8309 | 3.8865 | True | False | 0.8309 | 0.0000 | unchanged |
| 1.00R | 21 | ETHUSDT | short | -0.2208 | 0.0000 | False | False | -0.2208 | 0.0000 | unchanged |
| 1.00R | 22 | MNTUSDT | short | -0.6700 | 0.0000 | False | False | -0.6700 | 0.0000 | unchanged |
| 1.00R | 25 | ETHUSDT | long | -0.3524 | 1.5801 | True | True | 0.0001 | 0.3525 | rescued loser |
| 1.00R | 19 | NEARUSDT | short | 0.0760 | 1.1779 | False | False | 0.0760 | 0.0000 | unchanged |
| 1.00R | 26 | ETHUSDT | long | -0.6350 | 0.1464 | False | False | -0.6350 | 0.0000 | unchanged |
| 1.00R | 30 | ETHUSDT | short | -1.5415 | 0.0026 | False | False | -1.5415 | 0.0000 | unchanged |
| 1.00R | 24 | BNBUSDT | short | -0.6824 | 0.0995 | False | False | -0.6824 | 0.0000 | unchanged |
| 1.00R | 31 | HBARUSDT | long | 0.0077 | 4.7120 | True | True | 0.0037 | -0.0040 | damaged winner |
| 1.00R | 23 | XRPUSDT | long | -0.1897 | 1.9350 | True | True | 0.0096 | 0.1992 | rescued loser |
| 1.00R | 27 | ADAUSDT | short | -1.6817 | 0.0000 | False | False | -1.6817 | 0.0000 | unchanged |
| 1.00R | 29 | NEARUSDT | short | 1.2737 | 1.5009 | False | False | 1.2737 | 0.0000 | unchanged |
| 1.00R | 36 | ETHUSDT | long | -1.7544 | -0.0000 | False | False | -1.7544 | 0.0000 | unchanged |
| 1.00R | 35 | SUIUSDT | short | -0.6380 | 0.4761 | False | False | -0.6380 | 0.0000 | unchanged |
| 1.00R | 38 | NEARUSDT | short | -1.1547 | 0.9204 | False | False | -1.1547 | 0.0000 | unchanged |
| 1.00R | 42 | NEARUSDT | short | -0.6761 | 0.3783 | False | False | -0.6761 | 0.0000 | unchanged |
| 1.00R | 34 | HBARUSDT | long | 0.1545 | 1.1197 | False | False | 0.1545 | 0.0000 | unchanged |
| 1.00R | 28 | HYPEUSDT | short | 0.5298 | 0.5636 | False | False | 0.5298 | 0.0000 | unchanged |
| 1.00R | 39 | XRPUSDT | short | -0.6256 | 0.1604 | False | False | -0.6256 | 0.0000 | unchanged |
| 1.00R | 33 | AAVEUSDT | short | -0.8730 | 0.3673 | False | False | -0.8730 | 0.0000 | unchanged |
| 1.00R | 37 | TAOUSDT | short | -0.5544 | 0.3616 | False | False | -0.5544 | 0.0000 | unchanged |
| 1.00R | 43 | NEARUSDT | long | 0.8229 | 0.6923 | False | False | 0.8229 | 0.0000 | unchanged |
| 1.00R | 32 | DOGEUSDT | short | -0.3381 | 0.4988 | False | False | -0.3381 | 0.0000 | unchanged |
| 1.00R | 41 | APTUSDT | long | -0.1112 | 0.4527 | False | False | -0.1112 | 0.0000 | unchanged |
| 1.00R | 44 | BNBUSDT | short | -0.4900 | 0.0000 | False | False | -0.4900 | 0.0000 | unchanged |
| 1.00R | 40 | BTCUSDT | long | 0.6735 | 0.7139 | False | False | 0.6735 | 0.0000 | unchanged |

## Descriptive ranking on all 26 trades

| Rank | Policy | P&L | Delta vs baseline | PF | Losers saved | Winners damaged |
|---:|---|---:|---:|---:|---:|---:|
| 1 | 0.25% | -17.0365 | 4.6093 | 0.0628 | 8 | 7 |
| 2 | 0.25R | -17.4974 | 4.1484 | 0.0580 | 6 | 7 |
| 3 | 1.00R | -21.0981 | 0.5477 | 0.1717 | 2 | 1 |
| 4 | 1.00% | -21.1181 | 0.5277 | 0.1710 | 2 | 2 |
| 5 | 0.75R | -21.1181 | 0.5277 | 0.1710 | 2 | 2 |
| 6 | 0.75% | -21.1508 | 0.4950 | 0.1302 | 3 | 3 |
| 7 | 0.50R | -21.9151 | -0.2692 | 0.0988 | 3 | 4 |
| 8 | 0.50% | -22.5892 | -0.9434 | 0.0711 | 3 | 6 |

## Interpretation

No tested policy is profitable. The apparent leaders at 0.25% and 0.25R are not stable: the adjacent 0.50%/0.50R policies deteriorate sharply, seven original winners are damaged, and Profit Factor falls despite lower absolute loss. The 0.75%–1.00% and 0.75R–1.00R neighborhood is directionally more consistent and damages fewer winners, but its improvement is small and remains negative in all three samples. Therefore no threshold is authorized for trading.

Thresholds are descriptive candidates only. With 26 trades, inferred tick sizes, unavailable trade-scoped funding, and incomplete LastPrice sampling during collector degradation, this replay cannot authorize a live policy change. A candidate is considered comparatively stable only when adjacent percent and R thresholds improve expectancy/PF without a sharp increase in damaged winners.
