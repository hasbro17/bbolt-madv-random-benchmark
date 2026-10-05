# Golden datasets

Built 2026-10-02 on VM A with the control binary and no memory limit
(`scripts/make-golden.sh --name history --keys 350000 --passes 4 --compacted-name compacted`).
Sizing: a probe of 50,000 keys measured 6,164 bytes per key revision (a 4096-byte value
plus header spans two 4 KiB pages), so 350,000 keys x 4 passes = 1.4M revisions ~ 8.6 GB.

| Golden | Keys | Revision | Compacted at | db bytes | db sha256 | Used by |
|---|--:|--:|--:|--:|---|---|
| history | 350,000 | 1,400,001 | (none) | 8,638,984,192 | `d3bd09a557abfe4d6580a4152d5a0fb49174c50dd085b5456b2442111baa2102` | S1, S2, S3a (+ref, MGLRU-off), gates |
| compacted | 350,000 | 1,400,001 | 1,400,001 (no defrag) | 8,638,984,192 | `caa258cc1009f6e0e1fda510068d76b5eed6a0b56f403f58f43f7267ac397dcb` | S3b (+ref) |

Keys: 16 bytes (varint, as `benchmark stm` reads them); values: 4096 bytes. S3a compacts
the history golden at revision 1,400,001, removing 1,050,000 old revisions.
