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

No project-wide license for the original Theatre code has been selected yet.
Public visibility alone does not grant an open-source license.
