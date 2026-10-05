# Versions

| Item | Value |
|------|-------|
| etcd | `main` at `64f26db45f5feb2c491a9eb82768a2f41fc857df` (2026-10-01, "Merge pull request #22505") |
| bbolt, control | `go.etcd.io/bbolt v1.5.0`, as pinned by etcd main (`server/go.mod`) |
| bbolt, treatment | `v1.5.0` plus [`scripts/bbolt-remove-madv-random.patch`](../scripts/bbolt-remove-madv-random.patch): deletes the `unix.Madvise(b, syscall.MADV_RANDOM)` block in `bolt_unix.go` `mmap()` (same change as etcd-io/bbolt#940). Branch `remove-madv-random-v1.5`, commit `decad5a` |
| Go | `1.27.1` (etcd's `.go-version`) |
| OS image | AWS `ami-0dfb1e7a5a62899ce` = `RHEL-10.2.0_HVM-20260908-x86_64-0-Hourly2-GP3` |
| Kernel | `6.12.0-211.53.1.el10_2.x86_64`, MGLRU on (`/sys/kernel/mm/lru_gen/enabled` = `0x0007`) |
| Instance | `m7i.4xlarge` (16 vCPU, 64 GiB), dedicated tenancy; gp3 data volume, 3000 IOPS, 125 MiB/s |

Both etcd binaries come from the same tree and toolchain; the treatment build only adds a
`go work edit -replace go.etcd.io/bbolt=<patched v1.5.0>`. `go version -m` shows
`go.etcd.io/bbolt v1.5.0` for control and the replace for treatment. At run time every run
checked the `VmFlags` of etcd's `member/snap/db` mapping: control has `rr` (VM_RAND_READ),
treatment does not.

## Binary checksums

The same binaries were copied to every VM and verified before each run.

```
ETCD_GIT_SHA=64f26db45f5feb2c491a9eb82768a2f41fc857df
GO_VERSION=go1.27.1
SHA_control_etcd=336e343f1e469683ff66b1d733ec87d103fa3a91d7329dfbc2cede9cbe85f7e0
SHA_control_etcdctl=636156f17153ae4c559c8f33161b0b0bd3dac6cd57b1a52b85cab9e0f4246902
SHA_control_benchmark=443f02022ced61635662970504cb1f1ddcbf2b65509a42d2b1e40e9bd8dcfcb5
SHA_treatment_etcd=7f67730ed5cec0589216a179f2a9ca6b4180428b058d8720909b448f22dc31b0
```

`etcdctl` and `benchmark` are shared: both variants use the control build of the client
tools, so only the server differs.
