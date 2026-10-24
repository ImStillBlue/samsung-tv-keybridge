#!/bin/sh
set -eu
# Publisher URLs and verified digests only. Extract the Debian package rather
# than installing it: its binfmt registration scripts must never run.
mkdir -p /opt/dotnet /opt/nuget-feed /opt/keybridge
case "$(uname -m)" in
    aarch64)
        sdk_arch=arm64
        sdk_sha=cb8454865ecb99ce557bd0a5741d3dc84657a45ea00f9b2a0f0593e94e4e661e898a5690df90cf0175bf5982973c19985a168998aaa975b7ac7a3bef2ecd05d2
        ;;
    x86_64)
        sdk_arch=x64
        sdk_sha=04395f991ab50e4755ce1ae53e23592a7420b71b82160883bae3194dd1dfd5dcaed78743e4e0b4dd51ea43c49ec84b5643630707b3854f1471265dc98490d2f9
        ;;
    *) exit 1 ;;
esac
curl --fail --location --proto '=https' --tlsv1.2 \
    "https://builds.dotnet.microsoft.com/dotnet/Sdk/6.0.428/dotnet-sdk-6.0.428-linux-${sdk_arch}.tar.gz" \
    --output /tmp/dotnet.tar.gz
printf '%s  %s\n' \
    "$sdk_sha" \
    /tmp/dotnet.tar.gz | sha512sum --check -
tar -xzf /tmp/dotnet.tar.gz -C /opt/dotnet

# SDK 6.0.428's netcoreapp3.1 reference packs, restored from this feed only.
curl --fail --location --proto '=https' --tlsv1.2 \
    https://api.nuget.org/v3-flatcontainer/microsoft.netcore.app.ref/3.1.0/microsoft.netcore.app.ref.3.1.0.nupkg \
    --output /opt/nuget-feed/microsoft.netcore.app.ref.3.1.0.nupkg
printf '%s  %s\n' \
    9ee02f1f0989dacdce6f5a8d0c7d7eb95ddac0e65a5a5695dc57a74e63d45b23 \
    /opt/nuget-feed/microsoft.netcore.app.ref.3.1.0.nupkg | sha256sum --check -
curl --fail --location --proto '=https' --tlsv1.2 \
    https://api.nuget.org/v3-flatcontainer/microsoft.aspnetcore.app.ref/3.1.10/microsoft.aspnetcore.app.ref.3.1.10.nupkg \
    --output /opt/nuget-feed/microsoft.aspnetcore.app.ref.3.1.10.nupkg
printf '%s  %s\n' \
    e750f55e4a8530f1c9cce9add4743405fd67f4ad1abb0132c5326ea80ffcc476 \
    /opt/nuget-feed/microsoft.aspnetcore.app.ref.3.1.10.nupkg | sha256sum --check -

curl --fail --location --proto '=https' --tlsv1.2 \
    https://download.tizen.org/sdk/tizenstudio/official/binary/sdb_4.2.36_ubuntu-64.zip \
    --output /tmp/sdb.zip
printf '%s  %s\n' \
    addf825e1a886a9f220cb3d287e74438cae0ffafe5821b9baca9d4f6c7de5124 \
    /tmp/sdb.zip | sha256sum --check -
python -m zipfile -e /tmp/sdb.zip /tmp/sdb
printf '%s  %s\n' \
    eddcacc9c66cadf5d35b145f7afc360487755db50701c43eb8041af5937c9c97 \
    /tmp/sdb/data/tools/sdb | sha256sum --check -
cp /tmp/sdb/data/tools/sdb /opt/keybridge/sdb.real

# SHA256 from Debian bookworm's arm64 Packages index. Retain the exact package
# version and digest together; do not replace this with apt's floating candidate.
if [ "$sdk_arch" = arm64 ]; then
curl --fail --location --proto '=https' --tlsv1.2 \
    https://deb.debian.org/debian/pool/main/q/qemu/qemu-user-static_7.2+dfsg-7+deb12u18+b3_arm64.deb \
    --output /tmp/qemu.deb
printf '%s  %s\n' \
    e420c417c12b42ca774795b62888d5388da0e08c1ef263dea1490e5ce4e32a0b \
    /tmp/qemu.deb | sha256sum --check -
test "$(dpkg-deb -f /tmp/qemu.deb Architecture)" = arm64
dpkg-deb --extract /tmp/qemu.deb /tmp/qemu
cp /tmp/qemu/usr/bin/qemu-x86_64-static /opt/keybridge/qemu-x86_64-static
mkdir -p /opt/keybridge/licenses
cp /tmp/qemu/usr/share/doc/qemu-user-static/copyright /opt/keybridge/licenses/qemu-copyright
chmod 0755 /opt/keybridge/qemu-x86_64-static
fi
chmod 0755 /opt/keybridge/sdb.real
rm -rf /tmp/dotnet.tar.gz /tmp/sdb.zip /tmp/sdb /tmp/qemu.deb /tmp/qemu
