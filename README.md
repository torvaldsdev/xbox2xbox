# xbox2xbox

**A compatibility layer that runs Xbox Series X|S titles natively on Windows.**

[Agent guide](AGENTS.md)

## About

xbox2xbox is a compatibility layer, not an emulator. The Xbox Series X|S CPU is
AMD Zen 2 — the same x86-64 architecture as a Windows PC — so a title's code runs
natively inside the host process, unmodified. xbox2xbox supplies the platform
underneath it: reimplemented system libraries, a D3D12.X graphics bridge over host
D3D12, a GFX10 (RDNA 2) shader translator to DXIL, and conversion of console asset
formats to host formats. There is no separate runtime process and no instruction
emulation.

The layer is general: everything except the title-specific crate is platform code
that must work with any Xbox Series title. Development is currently driven by one
title, but no platform fix may special-case it.

## Status

**Skeleton.** The architecture, contracts and roadmap are specified in
[AGENTS.md](AGENTS.md); the crates are being built out phase by phase. Pull
requests that touch the measured sources get an automatic progress delta
report — see [Progress](#progress-tracking) for what it counts.

## How it works

| Crate | Role |
|-------|------|
| `x2x-core` | configuration, asset-policy guard, logging |
| `x2x-pe` | read-only PE32+ and import-library analysis |
| `x2x-loader` | map, bind and start the title image; TLS redirect; crash reporter |
| `x2x-hle` | console system APIs (`src/modules/`, one file per DLL), host pass-through policy |
| `x2x-gpu` | D3D12.X objects and command lists over host D3D12 |
| `x2x-shader` | console shader containers → DXIL: `gfx10` decode, IR/structure/range/resource analysis, `emit` |
| `x2x-formats` | texture tiling, BCPack, XMA2, Opus, zlib |
| `x2x-pipeline` | converter trait, content-addressed cache |
| `x2x-title` | the only place title-specific knowledge lives |
| `apps/x2x` | CLI: `inspect`, `census`, `lib-exports`, `shader-info`, `translate`, `run`, `config` |

## Progress tracking

Progress is counted from the code, never from running games, so it says nothing
about whether a title runs. Two metrics:

* **HLE exports** — every `hle_export!(Name)` registration in
  [`crates/x2x-hle/src/modules`](crates/x2x-hle/src/modules) is *declared*; it is
  *implemented* when its body is not a stub (a stub only returns `E_NOTIMPL`, calls
  `missing`, or is `unimplemented!`/`todo!`). The total grows as more exports are
  declared; it is not the count of every function the console DLLs export.
* **GFX10 opcodes decoded** — the decoder tables in
  [`crates/x2x-shader/src/gfx10`](crates/x2x-shader/src/gfx10) against the AMD
  RDNA 1 + RDNA 2 instruction list ([`tools/rdna_isa.txt`](tools/rdna_isa.txt)).
  Decoded means the decoder recognizes the opcode; it does not mean the translation
  to DXIL is implemented or correct.

[`tools/progress.py`](tools/progress.py) counts both metrics from the source
tree: summary lines by default, `--json` for a machine-readable snapshot,
`--compare BASE HEAD` for a markdown delta between two snapshots. GitHub Actions
posts that delta as a comment on pull requests that touch the measured sources.

## Building

A Rust toolchain and the Visual Studio Build Tools ("Desktop development with
C++" workload, for the MSVC linker and Windows SDK) are required. The build and
test instructions live in [AGENTS.md](AGENTS.md); tests use synthetic fixtures and
require neither a game nor a GPU.

## Legal

This project is intended for interoperability, research, preservation and
compatibility. It does not include, distribute or require copyrighted game
software, firmware, cryptographic keys or proprietary libraries. No game-derived
asset is committed to this repository; users supply their own legally obtained
titles and are responsible for complying with applicable laws and license terms.

xbox2xbox is an independent project and is not affiliated with, authorized or
endorsed by Microsoft Corporation. "Xbox" is a trademark of Microsoft Corporation.

## License

BSD 3-Clause. See [LICENSE](LICENSE).