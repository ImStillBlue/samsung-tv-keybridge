# Third-party source

The integration uses `chris-ritsen/samsung-tv-root`, pinned by commit and file
checksums in `upstream.lock.json`. Its downloaded source carries the Unlicense;
the builder verifies and retains the upstream `LICENSE` alongside that source.

The archive bootstrap and managed input code identify adapted upstream portions
in source comments. Upstream author attribution and license must be preserved
when distributing those portions.

Python dependencies are recorded in `controller/uv.lock`; their individual
licenses continue to apply. Samsung SDK binaries are not included in this
source repository.

Both app builds download the pinned Samsung SDK; only ARM64 downloads the Debian QEMU package
declared in `packaging/fetch-build-inputs.sh`. QEMU's copyright/license notice is
retained in the image at `/opt/keybridge/licenses/qemu-copyright`. Private amd64
runtime libraries come from the digest-pinned Debian Python image; Python and
OpenSSL run natively on aarch64 and amd64. No downloaded binary is committed to this repo.

Original KeyBridge code is MIT licensed, Copyright (c) 2026 ImStillBlue; see
LICENSE. Adapted upstream portions remain under the Unlicense, not relicensed.
